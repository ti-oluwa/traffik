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
from traffik.types import WaitPeriod

__all__ = [
    "backend_fallback",
    "circuit_breaker",
    "retry",
    "degraded_mode",
    "throttle_fallbacks",
    "circuit_breaker_fallback",
]


def backend_fallback(
    backend: ThrottleBackend[typing.Any, HTTPConnection],
    fallback_on: typing.Tuple[typing.Type[Exception], ...] = (
        BackendError,
        TimeoutError,
    ),
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create an error handler that switches to a fallback backend on specific errors.

    Use in high-availability setups where you have a primary backend (e.g., Redis)
    and a fallback backend (e.g., InMemory) for when the primary fails.

    **How it works:**
    1. When primary backend fails, attempts throttling with fallback backend
    2. If fallback succeeds, returns its wait period
    3. If fallback also fails, throttles the request (fail closed)

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
        fallback_on=(BackendConnectionError, TimeoutError)
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
    :return: Async error handler function
    """

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        exc = exc_info["exception"]
        if not isinstance(exc, fallback_on):
            # For other errors, fail closed
            return 1000.0

        try:
            # Ensure fallback backend is initialized
            if not await backend.ready():
                await backend.initialize()

            # Attempt throttling with fallback backend
            throttle = exc_info["throttle"]
            key = throttle.get_namespaced_key(
                connection,
                await backend.identifier(connection),
                context=None,
            )
            wait_ms = await throttle.strategy(
                key, exc_info["rate"], backend, exc_info["cost"]
            )
            return wait_ms
        except asyncio.CancelledError:
            raise
        except Exception:
            # If fallback also fails, fail closed
            return 1000.0

    return handler


class CircuitBreaker:
    """
    Circuit breaker state manager for error handling.

    Tracks failures and opens/closes the circuit based on thresholds.

    **States:**
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, reject requests immediately
    - HALF_OPEN: Testing if backend has recovered
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
        return {
            "state": self._state,
            "failures": self._failures,
            "successes": self._successes,
            "opened_at": self._opened_at,
        }


def circuit_breaker(
    circuit_breaker: typing.Optional[CircuitBreaker] = None,
    wait_ms: WaitPeriod = 5000.0,
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create an error handler with circuit breaker pattern.

    Protect against cascading failures by temporarily stopping
    requests to a failing backend, giving it time to recover.

    **How it works:**
    1. Tracks backend failures
    2. After threshold failures, opens circuit (rejects all requests)
    3. After recovery timeout, tests backend (half-open state)
    4. If tests pass, closes circuit (resumes normal operation)

    **Example:**
    ```python
    from traffik.error_handlers import circuit_breaker, CircuitBreaker

    breaker = CircuitBreaker(
        failure_threshold=5,      # Open after 5 failures
        recovery_timeout=30.0,    # Try recovery after 30 seconds
        success_threshold=2       # Need 2 successes to close circuit
    )

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        on_error=circuit_breaker(
            circuit_breaker=breaker,
            wait_ms=5000.0  # Wait 5 seconds when circuit is open
        )
    )
    ```

    :param circuit_breaker: `CircuitBreaker` instance (creates new one if None)
    :param wait_ms: Wait period when circuit is open (milliseconds)
    :return: Async error handler function
    """
    breaker = circuit_breaker or CircuitBreaker()

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        # Check if circuit is open
        if breaker.is_open():
            return wait_ms

        # Record the failure
        breaker.record_failure()

        # If circuit just opened, return wait period
        if breaker.is_open():
            return wait_ms

        # Circuit still closed or half-open, fail closed
        return 1000.0

    return handler


def retry(
    max_retries: int = 3,
    retry_delay: float = 0.1,
    backoff_multiplier: float = 2.0,
    retry_on: typing.Tuple[typing.Type[Exception], ...] = (TimeoutError,),
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create an error handler that retries failed operations.

    Use to handle transient failures that may succeed on retry (network blips,
    temporary backend unavailability).

    **How it works:**
    1. Retries the throttle operation on specific error types
    2. Uses exponential backoff between retries
    3. If all retries fail, fails closed

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
    :return: Async error handler function
    """

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        exc = exc_info["exception"]
        if not isinstance(exc, retry_on):
            return 1000.0

        throttle = exc_info["throttle"]
        backend = exc_info["backend"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]

        # Get connection identifier and key
        identifier = await backend.identifier(connection)
        key = throttle.get_namespaced_key(connection, identifier, context=None)

        delay = retry_delay
        for attempt in range(max_retries):
            if attempt > 0:
                await asyncio.sleep(delay)
                delay *= backoff_multiplier

            try:
                wait_ms = await throttle.strategy(key, rate, backend, cost)
                return wait_ms
            except asyncio.CancelledError:
                raise
            except Exception:
                # Continue to next retry
                continue

        # All retries failed, fail closed
        return 1000.0

    return handler


def degraded_mode(
    rate_multiplier: float = 2.0,
    duration: float = 300.0,
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create an error handler that enters degraded mode with relaxed limits.

    Use when strict rate limiting can't be enforced due to backend issues,
    but you still want some level of protection.

    **How it works:**
    1. On backend errors, calculates a degraded wait period
    2. Applies a multiplier to the original rate (e.g., 2x normal wait time)
    3. This provides basic protection without requiring backend access

    **Example:**
    ```python
    from traffik.error_handlers import degraded_mode

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",  # Normal: 100 req/min
        on_error=degraded_mode(
            rate_multiplier=2.0,  # Degraded: ~50 req/min (2x wait)
            duration=300.0         # Stay degraded for 5 minutes
        )
    )
    ```

    :param rate_multiplier: Multiplier for wait time (>1 means stricter)
    :param duration: How long to stay in degraded mode (seconds)
    :return: Async error handler function
    """
    entered_degraded_at: typing.Optional[datetime] = None

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        nonlocal entered_degraded_at

        # Enter degraded mode
        if entered_degraded_at is None:
            entered_degraded_at = datetime.now()

        # Check if we should exit degraded mode
        if datetime.now() - entered_degraded_at > timedelta(seconds=duration):
            entered_degraded_at = None
            # Try normal operation again
            return 1000.0  # Fail closed for this request

        # Calculate degraded wait period
        rate = exc_info["rate"]
        # Base wait time between requests at limit
        base_wait_ms = rate.expire / rate.limit if rate.limit > 0 else 1000.0
        degraded_wait_ms = base_wait_ms * rate_multiplier
        return degraded_wait_ms

    return handler


def throttle_fallbacks(
    throttles: typing.List[typing.Any],
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create an error handler that cascades through multiple throttles.

    **How it works:**
    1. If primary throttle fails, tries secondary throttle
    2. If secondary fails, tries tertiary throttle
    3. Continues until a throttle succeeds or all fail

    **Example:**
    ```python
    from traffik.error_handlers import throttle_fallbacks
    from traffik.backends.redis import RedisBackend
    from traffik.backends.inmemory import InMemoryBackend

    # Primary: Redis with strict limits
    primary = HTTPThrottle(
        uid="api:primary",
        rate="100/min",
        backend=RedisBackend("redis://primary:6379/0")
    )

    # Secondary: Redis replica with relaxed limits
    secondary = HTTPThrottle(
        uid="api:secondary",
        rate="200/min",
        backend=RedisBackend("redis://replica:6379/0")
    )

    # Tertiary: Local fallback with very relaxed limits
    tertiary = HTTPThrottle(
        uid="api:tertiary",
        rate="500/min",
        backend=InMemoryBackend()
    )

    # Primary uses cascade handler
    primary.set_error_handler(throttle_fallbacks([secondary, tertiary]))
    ```

    :param throttles: List of throttles to try in order
    :return: Async error handler function
    """

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        for throttle in throttles:
            try:
                # Try the fallback throttle
                await throttle(connection)
                return 0.0  # Fallback succeeded, allow request
            except asyncio.CancelledError:
                raise
            except Exception:
                # This fallback failed, try next one
                continue

        # All fallbacks failed, fail closed
        return 1000.0

    return handler


def circuit_breaker_fallback(
    backend: ThrottleBackend[typing.Any, HTTPConnection],
    circuit_breaker: typing.Optional[CircuitBreaker] = None,
    max_retries: int = 2,
) -> typing.Callable[[HTTPConnection, ExceptionInfo], typing.Awaitable[WaitPeriod]]:
    """
    Create a error handler combining fallback, circuit breaker, and retry.

    Combines multiple strategies for maximum reliability and resilience.

    **How it works:**
    1. Checks circuit breaker state
    2. If circuit open, uses fallback backend
    3. If circuit closed/half-open, retries primary backend
    4. If retries fail, falls back to secondary backend
    5. Records success/failure to circuit breaker

    **Example:**
    ```python
    from traffik.error_handlers import circuit_breaker_fallback, CircuitBreaker
    from traffik.backends.redis import RedisBackend
    from traffik.backends.inmemory import InMemoryBackend

    primary_backend = RedisBackend("redis://primary:6379/0")
    fallback_backend = InMemoryBackend(namespace="fallback")

    breaker = CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=30.0,
        success_threshold=2
    )

    throttle = HTTPThrottle(
        uid="api",
        rate="100/min",
        backend=primary_backend,
        on_error=circuit_breaker_fallback(
            backend=fallback_backend,
            circuit_breaker=breaker,
            max_retries=2
        )
    )
    ```

    :param backend: Backend to use when primary fails
    :param circuit_breaker: `CircuitBreaker` instance (creates new one if None)
    :param max_retries: Number of retries before falling back
    :return: Async error handler function
    """
    breaker = circuit_breaker or CircuitBreaker()

    async def handler(
        connection: HTTPConnection, exc_info: ExceptionInfo
    ) -> WaitPeriod:
        throttle = exc_info["throttle"]
        rate = exc_info["rate"]
        cost = exc_info["cost"]
        primary_backend = exc_info["backend"]

        # If circuit is open, immediately use fallback
        if breaker.is_open():
            try:
                if not await backend.ready():
                    await backend.initialize()

                identifier = await backend.identifier(connection)
                key = throttle.get_namespaced_key(connection, identifier, context=None)
                wait_ms = await throttle.strategy(key, rate, backend, cost)

                breaker.record_success()
                return wait_ms
            except asyncio.CancelledError:
                raise
            except Exception:
                return 1000.0  # Fallback failed, fail closed

        # Circuit closed or half-open: retry primary backend
        identifier = await primary_backend.identifier(connection)
        key = throttle.get_namespaced_key(connection, identifier, context=None)

        for attempt in range(max_retries):
            try:
                wait_ms = await throttle.strategy(key, rate, primary_backend, cost)
                breaker.record_success()
                return wait_ms
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.05 * (2**attempt))  # Exponential backoff
                continue

        # All retries failed, record failure and use fallback
        breaker.record_failure()

        try:
            if not await backend.ready():
                await backend.initialize()

            identifier = await backend.identifier(connection)
            key = throttle.get_namespaced_key(connection, identifier, context=None)
            wait_ms = await throttle.strategy(key, rate, backend, cost)
            return wait_ms
        except asyncio.CancelledError:
            raise
        except Exception:
            return 1000.0  # Fallback failed, fail closed

    return handler
