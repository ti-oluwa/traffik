"""
Error handling strategies for rate limiting operations.
"""

import asyncio
import typing
from datetime import datetime, timedelta

from starlette.requests import HTTPConnection

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendError
from traffik.throttles import ExceptionInfo
from traffik.types import HTTPConnectionT, WaitPeriod

__all__ = [
    "backend_fallback",
    "retry",
    "failover",
    "CircuitBreaker",
]


def backend_fallback(
    backend: ThrottleBackend[typing.Any, HTTPConnectionT],
    fallback_on: typing.Tuple[typing.Type[BaseException], ...] = (BackendError,),
) -> typing.Callable[[HTTPConnectionT, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
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
    from traffik.backends.redis import RedisBackend
    from traffik.error_handlers import backend_fallback

    primary = RedisBackend("redis://localhost:6379/0")
    fallback = InMemoryBackend(namespace="fallback")

    # Use InMemory backend if Redis fails
    error_handler = backend_fallback(
        backend=fallback,
        fallback_on=(BackendError, TimeoutError)
    )

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        backend=primary,
        on_error=error_handler
    )
    ```

    :param backend: The backend to use when primary fails
    :param fallback_on: Tuple of exception types that trigger fallback
    :return: Error handler function
    """

    async def handler(
        connection: HTTPConnectionT, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        exc = exc_info["exception"]
        if not isinstance(exc, fallback_on):
            raise exc

        if not await backend.ready():
            await backend.initialize()

        throttle = exc_info["throttle"]
        context = exc_info["context"]
        connection_id = await throttle.get_connection_id(
            connection,
            backend,  # type: ignore[arg-type]
            context=context,
        )
        key = throttle.get_namespaced_key(connection, connection_id, context=context)
        wait_ms = await throttle.strategy(
            key,
            exc_info["rate"],
            backend,  # type: ignore[arg-type]
            exc_info["cost"],
        )
        return wait_ms

    return handler


def retry(
    max_retries: int = 3,
    retry_delay: float = 0.1,
    backoff_multiplier: float = 2.0,
    retry_on: typing.Tuple[typing.Type[BaseException], ...] = (Exception,),
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
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
    :param retry_on: Exception types that trigger retry
    :return: Error handler function
    """

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        exc = exc_info["exception"]
        if not isinstance(exc, retry_on):
            raise exc

        throttle = exc_info["throttle"]
        backend = exc_info["backend"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]
        context = exc_info["context"]

        connection_id = await throttle.get_connection_id(
            connection, backend, context=context
        )
        key = throttle.get_namespaced_key(connection, connection_id, context=context)

        delay = retry_delay
        last_exc: BaseException = exc

        for attempt in range(max_retries):
            if attempt > 0:
                await asyncio.sleep(delay)
                delay *= backoff_multiplier

            try:
                return await throttle.strategy(key, rate, backend, cost)
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                last_exc = retry_exc
                continue

        raise last_exc

    return handler


class CircuitBreaker:
    """
    Circuit breaker state manager for error handling.

    Tracks failures and opens/closes the circuit based on thresholds.

    **States:**
    - `CLOSED`: Normal operation, requests pass through
    - `OPEN`: Too many failures, reject requests immediately
    - `HALF_OPEN`: Testing if backend has recovered
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
    ):
        """
        Initialize circuit breaker.

        :param failure_threshold: Number of failures before opening circuit
        :param recovery_timeout: Seconds to wait before trying half-open
        :param success_threshold: Successes needed in half-open to close circuit
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._failures = 0
        self._successes = 0
        self._state: typing.Literal["closed", "open", "half_open"] = "closed"
        self._opened_at: typing.Optional[datetime] = None

    def is_open(self) -> bool:
        """Check if circuit is open."""
        if self._state == "open":
            # Check if recovery timeout has passed
            if self._opened_at and datetime.now() - self._opened_at > timedelta(
                seconds=self.recovery_timeout
            ):
                self._state = "half_open"
                self._successes = 0
                return False
            return True
        return False

    def record_success(self) -> None:
        """Record successful operation."""
        if self._state == "half_open":
            self._successes += 1
            if self._successes >= self.success_threshold:
                self._close()
        elif self._state == "closed":
            self._failures = 0  # Reset failure count on success

    def record_failure(self) -> None:
        """Record failed operation."""
        if self._state == "half_open":
            # Failed during recovery test, reopen circuit
            self._open()
        elif self._state == "closed":
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open()

    def _open(self) -> None:
        """Open the circuit."""
        self._state = "open"
        self._opened_at = datetime.now()
        self._failures = 0

    def _close(self) -> None:
        """Close the circuit."""
        self._state = "closed"
        self._failures = 0
        self._successes = 0
        self._opened_at = None

    def info(self) -> typing.Dict[str, typing.Any]:
        """
        Get current circuit breaker state.

        :return: Dict with state, failures, successes, and opened_at
        """
        return {
            "state": self._state,
            "failures": self._failures,
            "successes": self._successes,
            "opened_at": self._opened_at,
        }


def failover(
    backend: ThrottleBackend[typing.Any, HTTPConnectionT],
    breaker: typing.Optional[CircuitBreaker] = None,
    max_retries: int = 2,
    retry_delay: float = 0.05,
) -> typing.Callable[[HTTPConnectionT, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Returns a failover error handler with circuit breaker, retry, and fallback.

    **Recommended handler for production** use. It combines multiple
    resilience patterns for maximum reliability.

    **How it works:**

    1. If circuit is `OPEN` - use fallback backend immediately
    2. If circuit is `CLOSED`/`HALF_OPEN` - retry primary with exponential backoff
    3. If retries exhausted - record failure to circuit breaker, use fallback
    4. On success - record success to circuit breaker

    **Circuit Breaker States:**

    - `CLOSED`: Normal operation, retries primary backend
    - `OPEN`: Too many failures, skips to fallback immediately
    - `HALF_OPEN`: Testing recovery, allows one request through

    **Example:**

    ```python
    from traffik.error_handlers import failover, CircuitBreaker
    from traffik.backends.redis import RedisBackend
    from traffik.backends.inmemory import InMemoryBackend

    primary = RedisBackend("redis://primary:6379/0")
    fallback = InMemoryBackend(namespace="fallback")

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
            backend=fallback,
            breaker=breaker,
            max_retries=2,
            retry_delay=0.05
        )
    )
    ```

    :param backend: Fallback backend to use when primary fails
    :param breaker: CircuitBreaker instance (creates new one if None)
    :param max_retries: Number of retries before falling back
    :param retry_delay: Initial delay between retries (seconds)
    :return: Error handler function
    """
    cb = breaker or CircuitBreaker()

    async def handler(
        connection: HTTPConnectionT, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        throttle = exc_info["throttle"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]
        context = exc_info["context"]
        primary_backend = exc_info["backend"]

        # Circuit OPEN: skip primary, go directly to fallback
        if cb.is_open():
            return await _use_fallback(connection, throttle, rate, cost, context)

        # Circuit CLOSED/HALF_OPEN: retry primary backend
        connection_id = await throttle.get_connection_id(
            connection, primary_backend, context=context
        )
        key = throttle.get_namespaced_key(connection, connection_id, context=context)

        delay = retry_delay
        for attempt in range(max_retries):
            try:
                wait_ms = await throttle.strategy(key, rate, primary_backend, cost)
                cb.record_success()
                return wait_ms
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                continue

        # All retries failed: record failure and use fallback
        cb.record_failure()
        return await _use_fallback(connection, throttle, rate, cost, context)

    async def _use_fallback(
        connection: HTTPConnectionT,
        throttle: typing.Any,
        rate: typing.Any,
        cost: int,
        context: typing.Any,
    ) -> WaitPeriod:
        """Use fallback backend for throttling."""
        if not await backend.ready():
            await backend.initialize()

        connection_id = await throttle.get_connection_id(
            connection,
            backend,
            context=context,  # type: ignore[arg-type]
        )
        key = throttle.get_namespaced_key(connection, connection_id, context=context)
        wait_ms = await throttle.strategy(
            key,
            rate,
            backend,
            cost,  # type: ignore[arg-type]
        )

        cb.record_success()
        return wait_ms

    return handler
