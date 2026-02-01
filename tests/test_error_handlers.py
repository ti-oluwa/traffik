"""Tests for error handling strategies."""

import asyncio
from unittest.mock import MagicMock

import pytest
from starlette.requests import HTTPConnection

from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import (
    CircuitBreaker,
    backend_fallback,
    circuit_breaker,
    circuit_breaker_fallback,
    degraded_mode,
    retry,
    throttle_fallbacks,
)
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.rates import Rate
from traffik.throttles import ExceptionInfo


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
        import time

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
        import time

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
        import time

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
            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendConnectionError("Connection failed"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            # Should use fallback and return 0 (allowed)
            wait = await handler(exc_info["connection"], exc_info)
            assert wait >= 0, "Should attempt fallback"

    @pytest.mark.anyio
    async def test_fail_closed_on_other_errors(self):
        """Test fails closed on errors not in fallback_on tuple."""
        fallback_backend = InMemoryBackend(namespace="fallback")

        async with fallback_backend(close_on_exit=True):
            handler = backend_fallback(
                backend=fallback_backend, fallback_on=(BackendConnectionError,)
            )

            throttle = HTTPThrottle(uid="test", rate="10/s")
            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": ValueError("Some other error"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": InMemoryBackend(),
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert wait == 1000.0, "Should fail closed"

    @pytest.mark.anyio
    async def test_fail_closed_when_fallback_fails(self):
        """Test fails closed when fallback backend also fails."""
        # Create a backend that will fail
        fallback_backend = InMemoryBackend(namespace="fallback")

        async with fallback_backend(close_on_exit=True):
            # Close the backend to make operations fail
            await fallback_backend.close()

            handler = backend_fallback(backend=fallback_backend)

            throttle = HTTPThrottle(uid="test", rate="10/s")
            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Backend failed"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": InMemoryBackend(),
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert wait == 1000.0, "Should fail closed when fallback fails"


class TestCircuitBreakerHandler:
    """Tests for circuit_breaker error handler."""

    @pytest.mark.anyio
    async def test_records_failure(self):
        """Test circuit breaker records failures."""
        breaker = CircuitBreaker(failure_threshold=2)
        handler = circuit_breaker(circuit_breaker=breaker, wait_ms=5000.0)

        throttle = HTTPThrottle(uid="test", rate="10/s")
        exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": BackendError("Error"),
            "connection": MagicMock(spec=HTTPConnection),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": InMemoryBackend(),
            "throttle": throttle,  # type: ignore[arg-type]
        }

        # First failure
        await handler(exc_info["connection"], exc_info)
        assert breaker._failures == 1
        assert not breaker.is_open()

        # Second failure - circuit should open
        await handler(exc_info["connection"], exc_info)
        assert breaker.is_open()

    @pytest.mark.anyio
    async def test_returns_wait_when_open(self):
        """Test returns wait time when circuit is open."""
        breaker = CircuitBreaker(failure_threshold=1)
        handler = circuit_breaker(circuit_breaker=breaker, wait_ms=3000.0)

        # Open the circuit
        breaker.record_failure()

        throttle = HTTPThrottle(uid="test", rate="10/s")
        exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": BackendError("Error"),
            "connection": MagicMock(spec=HTTPConnection),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": InMemoryBackend(),
            "throttle": throttle,  # type: ignore[arg-type]
        }

        wait = await handler(exc_info["connection"], exc_info)
        assert wait == 3000.0

    @pytest.mark.anyio
    async def test_creates_breaker_if_none(self):
        """Test creates CircuitBreaker instance if none provided."""
        handler = circuit_breaker(wait_ms=2000.0)

        throttle = HTTPThrottle(uid="test", rate="10/s")
        exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
            "exception": BackendError("Error"),
            "connection": MagicMock(spec=HTTPConnection),
            "cost": 1,
            "rate": Rate.parse("10/s"),
            "backend": InMemoryBackend(),
            "throttle": throttle,  # type: ignore[arg-type]
        }

        # Should work without crashing
        wait = await handler(exc_info["connection"], exc_info)
        assert wait >= 0


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

            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": TimeoutError("Timeout"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert call_count == 3, "Should retry 2 times after initial failure"
            assert wait == 0.0, "Should succeed after retries"

    @pytest.mark.anyio
    async def test_fails_closed_after_max_retries(self):
        """Test fails closed after exhausting retries."""

        async def failing_strategy(key, rate, backend, cost):
            raise TimeoutError("Always fails")

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=backend)
            throttle.strategy = failing_strategy

            handler = retry(max_retries=2, retry_delay=0.01)

            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": TimeoutError("Timeout"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert wait == 1000.0, "Should fail closed"

    @pytest.mark.anyio
    async def test_does_not_retry_other_errors(self):
        """Test only retries errors in retry_on tuple."""
        handler = retry(retry_on=(TimeoutError,))

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="10/s", backend=backend)

            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": ValueError("Not a timeout"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": backend,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            assert wait == 1000.0, "Should fail closed without retry"


class TestDegradedMode:
    """Tests for degraded_mode error handler."""

    @pytest.mark.anyio
    async def test_returns_degraded_wait_time(self):
        """Test returns degraded wait time based on rate multiplier."""
        handler = degraded_mode(rate_multiplier=2.0)

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="100/s", backend=backend)
            # 100/s = 1 request per 10ms
            # Degraded 2x = 1 request per 20ms

            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("100/s"),
                "backend": backend,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            wait = await handler(exc_info["connection"], exc_info)
            # Base wait is 10ms, degraded is 20ms
            assert wait == pytest.approx(20.0, rel=0.1)

    @pytest.mark.anyio
    async def test_exits_degraded_mode_after_duration(self):
        """Test exits degraded mode after specified duration."""
        handler = degraded_mode(rate_multiplier=2.0, duration=0.1)

        backend = InMemoryBackend()
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(uid="test", rate="100/s", backend=backend)

            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("100/s"),
                "backend": backend,
                "throttle": throttle,  # type: ignore[arg-type]
            }

            # First call enters degraded mode
            wait1 = await handler(exc_info["connection"], exc_info)
            assert wait1 == pytest.approx(20.0, rel=0.1)

            # Wait for duration to pass
            await asyncio.sleep(0.15)

            # Next call should exit degraded mode
            wait2 = await handler(exc_info["connection"], exc_info)
            assert wait2 == 1000.0, "Should fail closed after exiting degraded mode"


class TestCircuitBreakerFallback:
    """Tests for circuit_breaker_fallback error handler."""

    @pytest.mark.anyio
    async def test_uses_fallback_when_circuit_open(self):
        """Test uses fallback backend when circuit is open."""
        primary = InMemoryBackend(namespace="primary")
        fallback_backend = InMemoryBackend(namespace="fallback")
        breaker = CircuitBreaker(failure_threshold=1)

        async with fallback_backend(close_on_exit=True):
            # Open the circuit
            breaker.record_failure()

            handler = circuit_breaker_fallback(
                backend=fallback_backend, circuit_breaker=breaker, max_retries=0
            )

            throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
            exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                "exception": BackendError("Error"),
                "connection": MagicMock(spec=HTTPConnection),
                "cost": 1,
                "rate": Rate.parse("10/s"),
                "backend": primary,
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
        breaker = CircuitBreaker()

        async with primary(close_on_exit=True):
            async with fallback_backend(close_on_exit=True):
                throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
                throttle.strategy = counting_strategy

                handler = circuit_breaker_fallback(
                    backend=fallback_backend, circuit_breaker=breaker, max_retries=2
                )

                exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                    "exception": BackendError("Error"),
                    "connection": MagicMock(spec=HTTPConnection),
                    "cost": 1,
                    "rate": Rate.parse("10/s"),
                    "backend": primary,
                    "throttle": throttle, # type: ignore[arg-type]
                }

                wait = await handler(exc_info["connection"], exc_info)
                assert retry_count >= 2, "Should retry primary"

    @pytest.mark.anyio
    async def test_records_failures_to_circuit_breaker(self):
        """Test records failures to circuit breaker."""

        async def failing_strategy(key, rate, backend, cost):
            raise BackendError("Always fails")

        primary = InMemoryBackend(namespace="primary")
        fallback_backend = InMemoryBackend(namespace="fallback")
        breaker = CircuitBreaker(failure_threshold=2)

        async with primary(close_on_exit=True):
            async with fallback_backend(close_on_exit=True):
                throttle = HTTPThrottle(uid="test", rate="10/s", backend=primary)
                throttle.strategy = failing_strategy

                handler = circuit_breaker_fallback(
                    backend=fallback_backend, circuit_breaker=breaker, max_retries=1
                )

                exc_info: ExceptionInfo = {  # type: ignore[typeddict-item]
                    "exception": BackendError("Error"),
                    "connection": MagicMock(spec=HTTPConnection),
                    "cost": 1,
                    "rate": Rate.parse("10/s"),
                    "backend": primary,
                    "throttle": throttle, # type: ignore[arg-type]
                }

                # First failure
                await handler(exc_info["connection"], exc_info)
                assert not breaker.is_open()

                # Second failure should open circuit
                await handler(exc_info["connection"], exc_info)
                assert breaker.is_open()


@pytest.mark.anyio
async def test_throttle_fallbacks():
    """Test throttle_fallbacks cascades through multiple throttles."""
    # This would require more complex setup with actual throttle instances
    # For now, test basic structure
    primary = InMemoryBackend()
    secondary = InMemoryBackend()

    async with primary(close_on_exit=True):
        async with secondary(close_on_exit=True):
            throttle1 = HTTPThrottle(uid="t1", rate="10/s", backend=primary)
            throttle2 = HTTPThrottle(uid="t2", rate="20/s", backend=secondary)

            handler = throttle_fallbacks([throttle2])

            # Should be callable
            assert callable(handler)
