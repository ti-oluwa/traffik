"""
Error handling strategies for rate limiting operations.
"""

import asyncio
import typing
import warnings

from starlette.requests import HTTPConnection

from traffik.backends.base import ThrottleBackend
from traffik.backoff import DEFAULT_BACKOFF
from traffik.exceptions import _EXEMPT_EXCEPTIONS, BackendError
from traffik.rates import Rate
from traffik.throttles import Throttle, ThrottleExceptionInfo
from traffik.types import BackoffStrategy, HTTPConnectionT, WaitPeriod
from traffik.utils import CircuitBreaker

__all__ = [
    "backend_fallback",
    "failover",
    "fallback",
    "retry",
]


def fallback(
    backend: ThrottleBackend[typing.Any, HTTPConnectionT],
    fallback_on: typing.Optional[typing.Tuple[typing.Type[BaseException], ...]] = None,
    on: typing.Tuple[typing.Type[BaseException], ...] = (BackendError,),
    initialized: bool = False,
) -> typing.Callable[
    [HTTPConnectionT, ThrottleExceptionInfo], typing.Awaitable[WaitPeriod]
]:
    """
    Returns an error handler that switches to a fallback backend on specified errors.

    Can be used in high-availability setups where you have a primary backend (e.g., Redis)
    and a fallback backend (e.g., InMemory) for when the primary fails.

    **How it works:**
    1. When primary backend fails, attempts throttling with fallback backend
    2. If fallback succeeds, returns its wait period
    3. If fallback also fails, then the exception is re-raised

    **Example:**
    ```python
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.backends.redis.aioredis import RedisBackend
    from traffik.error_handlers import fallback

    primary = RedisBackend("redis://localhost:6379/0")
    secondary = InMemoryBackend(namespace="fallback")

    # Use InMemory backend if Redis fails
    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        backend=primary,
        on_error=fallback(
            backend=secondary,
            on=(BackendError, TimeoutError)
        )
    )
    ```

    :param backend: The backend to use when primary fails
    :param initialized: Whether the fallback backend is already
        initialized. When `True`, readiness checks and initialization are skipped.
    :param on: Tuple of exception types that trigger fallback. Defaults to `BackendError`
    :return: Error handler function
    """
    if fallback_on is not None:
        warnings.warn("`fallback_on` is deprecated use `on`", UserWarning, stacklevel=2)
        on = fallback_on

    async def handler(
        connection: HTTPConnectionT, exc_info: ThrottleExceptionInfo
    ) -> WaitPeriod:
        exc = exc_info["exception"]
        if not isinstance(exc, on):
            raise exc

        if not initialized and not await backend.ready():
            await backend.initialize()

        throttle = exc_info["throttle"]
        wait_ms = await throttle.strategy(
            exc_info["key"],
            exc_info["rate"],
            backend,  # type: ignore[arg-type]
            exc_info["cost"],
        )
        return wait_ms

    return handler


backend_fallback = fallback  # backwards compatibility


def retry(
    max_retries: int = 3,
    retry_delay: float = 0.1,
    backoff_multiplier: typing.Optional[float] = None,
    backoff: typing.Optional[BackoffStrategy] = None,
    retry_on: typing.Tuple[typing.Type[BaseException], ...] = (Exception,),
) -> typing.Callable[
    [HTTPConnection, ThrottleExceptionInfo], typing.Awaitable[WaitPeriod]
]:
    """
    Returns an error handler that retries failed operations with backoff.

    Can be used to handle transient failures that may succeed on retry (network blips,
    temporary backend unavailability).

    **How it works:**

    1. On error in `retry_on`, retries the throttle operation
    2. Uses exponential backoff between retries
    3. If all retries fail, re-raises the last exception
    4. If error is not in `retry_on`, re-raises immediately

    **Example:**

    ```python
    from traffik.error_handlers import retry

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        on_error=retry(
            max_retries=3,
            retry_delay=0.1,           # Start with 100ms delay
            backoff_multiplier=2.0,    # Double delay each retry
            retry_on=(TimeoutError,)   # Only retry timeouts
        )
    )
    ```

    :param max_retries: Maximum number of retry attempts
    :param retry_delay: Initial delay between retries (seconds)
    :param backoff_multiplier: Multiplier for exponential backoff
    :param backoff: Backoff strategy for each retry. Defaults to exponential backoff.
    :param retry_on: Exception types that trigger retry
    :return: Error handler function
    """

    async def handler(
        connection: HTTPConnection, exc_info: ThrottleExceptionInfo
    ) -> WaitPeriod:
        nonlocal backoff, backoff_multiplier

        exc = exc_info["exception"]
        if not isinstance(exc, retry_on):
            raise exc

        throttle = exc_info["throttle"]
        backend = exc_info["backend"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]
        key = exc_info["key"]

        delay = retry_delay
        last_exc: BaseException = exc

        if backoff_multiplier is None and backoff is None:
            backoff = DEFAULT_BACKOFF

        uses_strategy = backoff is not None

        for attempt in range(max_retries):
            if attempt > 0:
                if uses_strategy:
                    delay = backoff(attempt, retry_delay)  # type: ignore
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(delay)
                    delay *= backoff_multiplier  # type: ignore
            try:
                return await throttle.strategy(key, rate, backend, cost)
            except _EXEMPT_EXCEPTIONS:
                raise
            except BaseException as retry_exc:  # noqa
                last_exc = retry_exc
                continue

        raise last_exc

    return handler


def failover(
    backend: ThrottleBackend[typing.Any, HTTPConnectionT],
    breaker: typing.Optional[CircuitBreaker] = None,
    max_retries: int = 2,
    retry_delay: float = 0.05,
    backoff: typing.Optional[BackoffStrategy] = None,
    initialized: bool = False,
) -> typing.Callable[
    [HTTPConnectionT, ThrottleExceptionInfo], typing.Awaitable[WaitPeriod]
]:
    """
    Returns a failover error handler with circuit breaker, retry, and fallback.

    Recommended handler for production use. It combines multiple
    resilience patterns for maximum reliability.

    **Example:**

    ```python
    from traffik.error_handlers import failover, CircuitBreaker
    from traffik.backends.redis import RedisBackend
    from traffik.backends.inmemory import InMemoryBackend

    primary = RedisBackend("redis://primary:6379/0")
    secondary = InMemoryBackend(namespace="fallback")

    breaker = CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=30.0,
        success_threshold=2
    )

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        backend=primary,
        on_error=failover(
            backend=secondary,
            breaker=breaker,
            max_retries=2,
            retry_delay=0.05
        )
    )
    ```

    :param backend: Fallback backend to use when primary fails
    :param initialized: Whether the fallback backend is already
        initialized. When `True`, readiness checks and initialization
        are skipped.
    :param breaker: CircuitBreaker instance (creates new one if None)
    :param max_retries: Number of retries before falling back
    :param retry_delay: Initial delay between retries (seconds)
    :param backoff: Backoff strategy for each retry. Defaults to exponential backoff.
    :return: Error handler function
    """
    cb = breaker or CircuitBreaker()
    backoff = backoff or DEFAULT_BACKOFF

    async def handler(
        connection: HTTPConnectionT, exc_info: ThrottleExceptionInfo
    ) -> WaitPeriod:
        throttle = exc_info["throttle"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]
        key = exc_info["key"]
        primary = exc_info["backend"]

        if not await cb.allow_execution():
            return await _use_fallback(
                throttle=throttle,
                rate=rate,
                cost=cost,
                key=key,
            )

        for attempt in range(max_retries):
            try:
                wait_ms = await throttle.strategy(key, rate, primary, cost)
                await cb.record_success()
                return wait_ms
            except _EXEMPT_EXCEPTIONS:
                raise
            except BaseException:  # noqa
                if attempt < max_retries - 1:
                    delay = backoff(attempt + 1, retry_delay)
                    await asyncio.sleep(delay)

        await cb.record_failure()
        return await _use_fallback(
            throttle=throttle,
            rate=rate,
            cost=cost,
            key=key,
        )

    async def _use_fallback(
        throttle: Throttle,
        rate: Rate,
        cost: int,
        key: str,
    ) -> WaitPeriod:
        """Use fallback backend for throttling."""
        if not initialized and not await backend.ready():
            await backend.initialize()

        wait_ms = await throttle.strategy(
            key,
            rate,
            backend,  # type: ignore[arg-type]
            cost,
        )
        return wait_ms

    return handler
