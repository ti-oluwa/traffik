"""End-to-end tests for Redis connection-failure handling and failover.

Unlike `tests/unit/test_error_handlers.py` (which exercises `failover()`/
`retry()` generically against `InMemoryBackend` with a synthetic failing
strategy), these tests point a real `RedisBackend` at an unreachable port so
the *actual* connection-failure exception the backend raises flows through
the whole pipeline: strategy -> `handle_error` -> `on_error` callback. This
is what actually proves the resilience handlers work against a real backend
failure, not just a hand-raised `BackendError`.

An unreachable port (rather than killing/restarting a shared server) is used
so these tests are safe to run against a Redis instance shared with other
tests or managed by CI infrastructure the test runner doesn't control.
"""

import functools
import os

import pytest
from starlette.requests import HTTPConnection

from tests.conftest import REDIS_HOST, SkipBackend
from tests.utils import make_connection
from traffik._utils import CircuitBreaker
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import failover, retry
from traffik.exceptions import BackendConnectionError
from traffik.registry import ThrottleRegistry
from traffik.throttles.http import HTTPThrottle

new_connection = functools.partial(make_connection, HTTPConnection)

# A port nothing is listening on, so connection attempts fail immediately
# (connection refused) rather than hanging on a timeout.
UNREACHABLE_REDIS_URL = f"redis://{REDIS_HOST}:6399/0"


def _require_redis() -> None:
    if os.getenv("SKIP_REDIS_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")


@pytest.mark.anyio
@pytest.mark.backend
class TestRedisConnectionFailure:
    """`RedisBackend` operations against an unreachable server must raise
    the library's own `BackendConnectionError`, not a raw driver exception,
    so callers (and the error handlers below) can catch it generically.
    """

    async def test_unreachable_backend_raises_backend_connection_error(self) -> None:
        _require_redis()
        from traffik.backends.redis.aioredis import RedisBackend

        backend = RedisBackend(
            connection=UNREACHABLE_REDIS_URL, namespace="unreachable", persistent=False
        )
        throttle = HTTPThrottle(
            uid="t-unreachable",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        with pytest.raises(BackendConnectionError):
            async with backend(close_on_exit=True):
                await throttle.strategy("key", throttle.rate, backend, 1)

    async def test_hit_without_error_handler_raises_backend_connection_error(
        self,
    ) -> None:
        """With no `on_error` configured, `Throttle.__call__` must propagate
        `BackendConnectionError`, not swallow it or crash with something
        unrelated.

        The backend's own context is not entered here: against an
        unreachable server, `RedisBackend.__aenter__` fails eagerly (it
        can't even initialize), which is a different code path from "was
        connected, then a later call fails". Not entering it lets the lazy
        `_assert_ready()` check inside the strategy call raise instead,
        exercising the code path relevant to a real request served while
        the connection is down.
        """
        _require_redis()
        from traffik.backends.redis.aioredis import RedisBackend

        backend = RedisBackend(
            connection=UNREACHABLE_REDIS_URL,
            namespace="unreachable-hit",
            persistent=False,
        )
        throttle = HTTPThrottle(
            uid="t-unreachable-hit",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
            on_error="raise",
        )
        with pytest.raises(BackendConnectionError):
            await throttle(new_connection())


@pytest.mark.anyio
@pytest.mark.backend
class TestRedisFailover:
    """`failover()` against a real unreachable `RedisBackend`, falling back
    to a real `InMemoryBackend`.
    """

    async def test_hit_succeeds_via_fallback(self) -> None:
        _require_redis()
        from traffik.backends.redis.aioredis import RedisBackend

        primary = RedisBackend(
            connection=UNREACHABLE_REDIS_URL,
            namespace="failover-primary",
            persistent=False,
        )
        secondary = InMemoryBackend(namespace="failover-secondary")
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

        async with secondary(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="t-failover",
                rate="10/s",
                backend=primary,
                registry=ThrottleRegistry(),
                on_error=failover(
                    backend=secondary, breaker=cb, max_retries=1, retry_delay=0.01
                ),
            )
            # Must not raise: the fallback absorbs the connection failure.
            await throttle(new_connection())

    async def test_circuit_opens_and_short_circuits_further_attempts(self) -> None:
        """Once the failure threshold is hit, later calls must go straight
        to the fallback without even attempting the (still unreachable)
        primary -- verified by the circuit's own state, not by timing.
        """
        _require_redis()
        from traffik.backends.redis.aioredis import RedisBackend

        primary = RedisBackend(
            connection=UNREACHABLE_REDIS_URL,
            namespace="failover-circuit",
            persistent=False,
        )
        secondary = InMemoryBackend(namespace="failover-circuit-secondary")
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)

        async with secondary(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="t-failover-circuit",
                rate="10/s",
                backend=primary,
                registry=ThrottleRegistry(),
                on_error=failover(
                    backend=secondary, breaker=cb, max_retries=1, retry_delay=0.01
                ),
            )
            for _ in range(2):
                await throttle(new_connection())

            assert cb.is_open, (
                "circuit should be open after `failure_threshold` real "
                "connection failures"
            )

            # A further call must succeed via the fallback without the
            # circuit needing to attempt (and fail against) the primary
            # again -- allow_execution() would return False immediately.
            assert await cb.allow_execution() is False
            await throttle(new_connection())

    async def test_retry_handler_eventually_raises_against_dead_backend(self) -> None:
        """`retry()` has no fallback -- against a backend that never
        recovers, it must exhaust its attempts and re-raise, not hang or
        swallow the failure silently.
        """
        _require_redis()
        from traffik.backends.redis.aioredis import RedisBackend

        backend = RedisBackend(
            connection=UNREACHABLE_REDIS_URL, namespace="retry-dead", persistent=False
        )
        throttle = HTTPThrottle(
            uid="t-retry-dead",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
            on_error=retry(max_retries=2, retry_delay=0.01),
        )
        with pytest.raises(BackendConnectionError):
            await throttle(new_connection())
