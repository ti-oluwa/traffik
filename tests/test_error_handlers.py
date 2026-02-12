"""Tests for error handling strategies."""

import time
from unittest.mock import MagicMock

import pytest
from starlette.requests import HTTPConnection

from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import CircuitBreaker, backend_fallback, failover, retry
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.rates import Rate
from traffik.throttles import ThrottleExceptionInfo


def create_mock_connection():
    """Create a properly mocked HTTPConnection for testing."""

    mock_conn = MagicMock(spec=HTTPConnection)
    mock_conn.scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [],
        "client": ("127.0.0.1", 50000),
    }
    return mock_conn


class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state(self):
        """Test circuit breaker starts in closed state."""
        breaker = CircuitBreaker()
        assert breaker.is_open() is False
        assert breaker._state == "closed"
        assert breaker._failures == 0

    def test_open_after_threshold(self):
        """Test circuit opens after reaching failure threshold."""
        breaker = CircuitBreaker(failure_threshold=3)

        # Record failures
        breaker.record_failure()
        assert breaker.is_open() is False

        breaker.record_failure()
        assert breaker.is_open() is False

        breaker.record_failure()
        # Should open after 3rd failure
        assert breaker.is_open() is True
        assert breaker._state == "open"

    def test_half_open_after_timeout(self):
        """Test circuit enters half-open state after recovery timeout."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,  # 100ms
        )

        # Open the circuit
        breaker.record_failure()
        assert breaker.is_open() is True

        # Should still be open immediately
        assert breaker.is_open() is True

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should transition to half-open
        is_open = breaker.is_open()
        assert is_open is False
        assert breaker._state == "half_open"

    def test_close_from_half_open(self):
        """Test circuit closes after successful operations in half-open."""
        breaker = CircuitBreaker(
            failure_threshold=1, recovery_timeout=0.01, success_threshold=2
        )

        # Open the circuit
        breaker.record_failure()
        assert breaker.is_open() is True

        # Wait for half-open
        time.sleep(0.02)
        breaker.is_open()  # Trigger transition to half-open

        # Record successes
        breaker.record_success()
        assert breaker._state == "half_open"

        breaker.record_success()
        # Should close after 2nd success
        assert breaker._state == "closed"

    def test_reopen_from_half_open_on_failure(self):
        """Test circuit reopens if failure occurs in half-open state."""
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        # Open the circuit
        breaker.record_failure()

        # Wait for half-open
        time.sleep(0.02)
        breaker.is_open()
        assert breaker._state == "half_open"

        # Record a failure in half-open
        breaker.record_failure()
        assert breaker._state == "open"

    def test_reset_failures_on_success_in_closed(self):
        """Test failures reset on success when circuit is closed."""
        breaker = CircuitBreaker(failure_threshold=3)

        breaker.record_failure()
        breaker.record_failure()
        assert breaker._failures == 2

        # Success resets counter
        breaker.record_success()
        assert breaker._failures == 0
        assert breaker._state == "closed"

    def test_info(self):
        """Test info() returns current state."""
        breaker = CircuitBreaker()
        info = breaker.info()

        assert "state" in info
        assert "failures" in info
        assert "successes" in info
        assert "opened_at" in info
        assert info["state"] == "closed"


class TestBackendFallback:
    """Tests for backend_fallback error handler."""

    @pytest.mark.anyio
    async def test_fallback_on_connection_error(self):
        """Test falls back to secondary backend on connection error."""
        primary = InMemoryBackend(namespace="primary")
        fallback_backend = InMemoryBackend(namespace="fallback")

        async with fallback_backend(close_on_exit=True):
            handler = backend_fallback(
                backend=fallback_backend, fallback_on=(BackendConnectionError,)
            )

            # Create mock exc_info
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendConnectionError("Connection failed"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            # Should use fallback and return 0 (allowed)
            wait = await handler(exc_info["connection"], exc_info)
            assert wait >= 0, "Should attempt fallback"

    @pytest.mark.anyio
    async def test_reraises_on_other_errors(self):
        """Test re-raises errors not in fallback_on tuple."""
        fallback_backend = InMemoryBackend(namespace="fallback")

        async with fallback_backend(close_on_exit=True):
            handler = backend_fallback(
                backend=fallback_backend, fallback_on=(BackendConnectionError,)
            )

            throttle = HTTPThrottle(uid="test", rate="10/s")
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": ValueError("Some other error"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": InMemoryBackend(),
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            with pytest.raises(ValueError, match="Some other error"):
                await handler(exc_info["connection"], exc_info)


class TestRetryHandler:
    """Tests for retry error handler."""

    @pytest.mark.anyio
    async def test_retries_on_timeout(self):
        """Test retries operations on timeout errors."""
        call_count = 0

        async def mock_strategy(key, rate, backend, cost):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("Timeout")
            return 0.0

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=backend)
            throttle.strategy = mock_strategy

            handler = retry(max_retries=3, retry_delay=0.01)

            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": TimeoutError("Timeout"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert call_count == 3, "Should retry 2 times after initial failure"
            assert wait == 0.0, "Should succeed after retries"

    @pytest.mark.anyio
    async def test_reraises_after_max_retries(self):
        """Test re-raises exception after exhausting retries."""

        async def failing_strategy(key, rate, backend, cost):
            raise TimeoutError("Always fails")

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=backend)
            throttle.strategy = failing_strategy

            handler = retry(max_retries=2, retry_delay=0.01)

            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": TimeoutError("Timeout"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            with pytest.raises(TimeoutError):
                await handler(exc_info["connection"], exc_info)

    @pytest.mark.anyio
    async def test_does_not_retry_other_errors(self):
        """Test re-raises errors not in retry_on tuple."""
        handler = retry(retry_on=(TimeoutError,))

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=backend)

            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": ValueError("Not a timeout"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            with pytest.raises(ValueError, match="Not a timeout"):
                await handler(exc_info["connection"], exc_info)


class TestFailover:
    """Tests for failover error handler."""

    @pytest.mark.anyio
    async def test_uses_fallback_when_circuit_open(self):
        """Test uses fallback backend when circuit is open."""
        primary = InMemoryBackend(namespace="primary")
        fallback_backend = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker(failure_threshold=1)

        async with fallback_backend(close_on_exit=True):
            # Open the circuit
            breaker_instance.record_failure()

            handler = failover(
                backend=fallback_backend, breaker=breaker_instance, max_retries=0
            )

            throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
            exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": create_mock_connection(),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "context": None,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            # Should use fallback and succeed
            wait = await handler(exc_info["connection"], exc_info)
            assert wait >= 0

    @pytest.mark.anyio
    async def test_retries_primary_when_closed(self):
        """Test retries primary backend when circuit is closed."""
        retry_count = 0

        async def counting_strategy(key, rate, backend, cost):
            nonlocal retry_count
            retry_count += 1
            if retry_count < 2:
                raise BackendError("Fail")
            return 0.0

        primary = InMemoryBackend(namespace="primary")
        fallback_backend = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker()

        async with primary(close_on_exit=True):
            async with fallback_backend(close_on_exit=True):
                throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
                throttle.strategy = counting_strategy

                handler = failover(
                    backend=fallback_backend, breaker=breaker_instance, max_retries=2
                )

                exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                    "exception": BackendError("Error"),
                    "connection": create_mock_connection(),
                    "cost": 1,
                    "rate": Rate.parse("10/s"),
                    "backend": primary,
                    "context": None,
                    "throttle": throttle,  # type: ignore[arg-type]
                }

                _ = await handler(exc_info["connection"], exc_info)
                assert retry_count >= 2, "Should retry primary"

    @pytest.mark.anyio
    async def test_records_failures_to_circuit_breaker(self):
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
        fallback_backend = InMemoryBackend(namespace="fallback")
        breaker_instance = CircuitBreaker(failure_threshold=1)

        async with primary(close_on_exit=True):
            async with fallback_backend(close_on_exit=True):
                throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
                throttle.strategy = primary_failing_strategy

                handler = failover(
                    backend=fallback_backend, breaker=breaker_instance, max_retries=1
                )

                exc_info: ThrottleExceptionInfo = {  # type: ignore[typeddict-item]
                    "exception": BackendError("Error"),
                    "connection": create_mock_connection(),
                    "cost": 1,
                    "rate": Rate.parse("10/s"),
                    "backend": primary,
                    "context": None,
                    "throttle": throttle,  # type: ignore[arg-type]
                }

                # First failure - should open circuit (threshold=1)
                await handler(exc_info["connection"], exc_info)
                assert breaker_instance.is_open()
