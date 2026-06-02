"""Tests for error handling strategies."""

import functools
import typing

import pytest
from starlette.requests import HTTPConnection

from tests.utils import make_connection, requires_throttle_type
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import failover, fallback, retry
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import Throttle, ThrottleExceptionInfo
from traffik.utils import CircuitBreaker

new_connection = functools.partial(make_connection, HTTPConnection)


@pytest.mark.anyio
@requires_throttle_type
class TestBackendFallback:
    """Tests for fallback error handler."""

    async def test_fallback_on_connection_error(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test falls back to secondary backend on connection error."""
        primary = InMemoryBackend(namespace="primary")
        secondary = InMemoryBackend(namespace="fallback")

        async with secondary(close_on_exit=True):
            handler = fallback(backend=secondary, on=(BackendConnectionError,))

            # Create mock exc_info
            throttle = throttle_type(
                uid="test-fallback-conn",
                rate="10/s",
                backend=primary,
                registry=ThrottleRegistry(),
            )
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendConnectionError("Connection failed"),
                "connection": new_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
                "key": "test-key",
            }

            # Should use fallback and return 0 (allowed)
            wait = await handler(exc_info["connection"], exc_info)
            assert wait >= 0, "Should attempt fallback"

    async def test_reraises_on_other_errors(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test re-raises errors not in on tuple."""
        secondary = InMemoryBackend(namespace="fallback")

        async with secondary(close_on_exit=True):
            handler = fallback(backend=secondary, on=(BackendConnectionError,))
            throttle = throttle_type(
                uid="test-fallback-reraise",
                rate="10/s",
                registry=ThrottleRegistry(),
            )
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": ValueError("Some other error"),
                "connection": new_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": InMemoryBackend(),
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
                "key": "test-key",
            }

            with pytest.raises(ValueError, match="Some other error"):
                await handler(exc_info["connection"], exc_info)


@pytest.mark.anyio
@requires_throttle_type
class TestRetryHandler:
    """Tests for retry error handler."""

    async def test_retries_on_timeout(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test retries operations on timeout errors."""
        call_count = 0

        async def mock_strategy(key, rate, backend, cost):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("Timeout")
            return 0.0

        throttle = throttle_type(
            uid="test-retry-timeout",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
            strategy=mock_strategy,
        )

        handler = retry(max_retries=3, retry_delay=0.01)

        exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": TimeoutError("Timeout"),
            "connection": new_connection(),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": backend,
            "context": None,
            "throttle": throttle,  # type: ignore[arg-type]
            "key": "test-key",
        }

        wait = await handler(exc_info["connection"], exc_info)
        assert call_count == 3, "Should retry 2 times after initial failure"
        assert wait == 0.0, "Should succeed after retries"

    async def test_reraises_after_max_retries(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test re-raises exception after exhausting retries."""

        async def failing_strategy(key, rate, backend, cost):
            raise TimeoutError("Always fails")

        throttle = throttle_type(
            uid="test-retry-exhaust",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
            strategy=failing_strategy,
        )

        handler = retry(max_retries=2, retry_delay=0.01)

        exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": TimeoutError("Timeout"),
            "connection": new_connection(),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": backend,
            "context": None,
            "throttle": throttle,  # type: ignore[arg-type]
            "key": "test-key",
        }

        with pytest.raises(TimeoutError):
            await handler(exc_info["connection"], exc_info)

    async def test_does_not_retry_other_errors(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test re-raises errors not in retry_on tuple."""
        handler = retry(retry_on=(TimeoutError,))
        throttle = throttle_type(
            uid="test-retry-other-err",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )

        exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": ValueError("Not a timeout"),
            "connection": new_connection(),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": backend,
            "context": None,
            "throttle": throttle,  # type: ignore[arg-type]
            "key": "test-key",
        }

        with pytest.raises(ValueError, match="Not a timeout"):
            await handler(exc_info["connection"], exc_info)


@pytest.mark.anyio
@requires_throttle_type
class TestFailover:
    """Tests for failover error handler."""

    async def test_uses_fallback_when_circuit_open(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test uses fallback backend when circuit is open."""
        primary = InMemoryBackend(namespace="primary")
        secondary = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker(failure_threshold=1)

        async with secondary(close_on_exit=True):
            # Open the circuit
            await breaker_instance.record_failure()

            handler = failover(
                backend=secondary,
                breaker=breaker_instance,
                max_retries=0,
            )

            throttle = throttle_type(
                uid="test-failover-open",
                rate="10/s",
                backend=primary,
                registry=ThrottleRegistry(),
            )
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": new_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
                "key": "test-key",
            }

            # Should use fallback and succeed
            wait = await handler(exc_info["connection"], exc_info)
            assert wait >= 0

    async def test_retries_primary_when_closed(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test retries primary backend when circuit is closed."""
        retry_count = 0

        async def counting_strategy(key, rate, backend, cost):
            nonlocal retry_count
            retry_count += 1
            if retry_count < 2:
                raise BackendError("Fail")
            return 0.0

        primary = InMemoryBackend(namespace="primary")
        secondary = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker()

        async with primary(close_on_exit=True), secondary(close_on_exit=True):
            throttle = throttle_type(
                uid="test-failover-closed",
                rate="10/s",
                backend=primary,
                registry=ThrottleRegistry(),
                strategy=counting_strategy,
            )
            handler = failover(
                backend=secondary,
                breaker=breaker_instance,
                max_retries=2,
            )

            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": new_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
                "key": "test-key",
            }

            _ = await handler(exc_info["connection"], exc_info)
            assert retry_count >= 2, "Should retry primary"

    async def test_records_failures_to_circuit_breaker(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test records failures to circuit breaker."""
        primary_fail_count = 0

        async def primary_failing_strategy(key, rate, backend, cost):
            nonlocal primary_fail_count
            # Only fail for primary backend (namespace="primary")
            if backend.namespace == "primary":
                primary_fail_count += 1
                raise BackendError("Primary fails")
            # Fallback succeeds
            return 0.0

        primary = InMemoryBackend(namespace="primary")
        secondary = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker(failure_threshold=1)

        async with primary(close_on_exit=True), secondary(close_on_exit=True):
            throttle = throttle_type(
                uid="test-failover-record",
                rate="10/s",
                backend=primary,
                strategy=primary_failing_strategy,
            )
            handler = failover(
                backend=secondary, breaker=breaker_instance, max_retries=1
            )

            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": new_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
                "key": "test-key",
            }

            # First failure - should open circuit (threshold=1)
            await handler(exc_info["connection"], exc_info)
            assert breaker_instance.is_open
