import asyncio
import functools
import inspect

import anyio
import pytest
from starlette.requests import HTTPConnection

from tests.utils import make_connection
from traffik._utils import (
    CircuitBreaker,
    CircuitState,
    MsgPackDecodeError,
    TaskTimer,
    _add_parameter_to_signature,
    _dump_data,
    get_remote_address,
    is_async_callable,
    _load_data,
    time,
)


class TestGetRemoteAddress:
    """Tests for get_remote_address utility."""

    def test_get_remote_from_x_forwarded_for_header(self):
        """Test extracting remote address from x-forwarded-for header."""

        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"192.168.1.100")],
        )
        address = get_remote_address(connection)
        assert address == "192.168.1.100"

    def test_get_remote_from_x_forwarded_for_multiple(self):
        """Test extracting first IP from x-forwarded-for with multiple IPs."""

        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"192.168.1.100, 10.0.0.1")],
        )
        address = get_remote_address(connection)
        assert address == "192.168.1.100"

    def test_get_remote_from_remote_addr_header(self):
        """Test extracting remote address from remote-addr header."""

        connection = make_connection(
            HTTPConnection,
            headers=[(b"remote-addr", b"10.20.30.40")],
        )
        address = get_remote_address(connection)
        assert address == "10.20.30.40"

    def test_get_remote_from_client_tuple(self):
        """Test extracting remote address from client tuple."""

        connection = make_connection(
            HTTPConnection,
            client=("203.0.113.42", 50000),
        )
        address = get_remote_address(connection)
        assert address == "203.0.113.42"

    def test_get_remote_returns_none_when_unavailable(self):
        """Test returns None when no remote address is available."""

        connection = make_connection(HTTPConnection, client=None)
        address = get_remote_address(connection)
        assert address is None

    def test_get_remote_prefers_x_forwarded_for_over_client(self):
        """Test x-forwarded-for is preferred over client tuple."""

        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"172.16.0.1")],
            client=("203.0.113.42", 50000),
        )
        address = get_remote_address(connection)
        assert address == "172.16.0.1"


class TestIsAsyncCallable:
    """Tests for is_async_callable utility."""

    def test_async_function(self):
        """Test with async function."""

        async def async_func():
            pass

        assert is_async_callable(async_func) is True

    def test_sync_function(self):
        """Test with regular function."""

        def sync_func():
            pass

        assert is_async_callable(sync_func) is False

    def test_async_method(self):
        """Test with async method."""

        class MyClass:
            async def async_method(self):
                pass

        obj = MyClass()
        assert is_async_callable(obj.async_method) is True

    def test_sync_method(self):
        """Test with regular method."""

        class MyClass:
            def sync_method(self):
                pass

        obj = MyClass()
        assert is_async_callable(obj.sync_method) is False

    def test_async_callable_object(self):
        """Test with object having async __call__."""

        class AsyncCallable:
            async def __call__(self):
                pass

        obj = AsyncCallable()
        assert is_async_callable(obj) is True

    def test_sync_callable_object(self):
        """Test with object having sync __call__."""

        class SyncCallable:
            def __call__(self):
                pass

        obj = SyncCallable()
        assert is_async_callable(obj) is False

    def test_partial_async_function(self):
        """Test with functools.partial wrapping async function."""

        async def async_func(a, b):
            return a + b

        partial = functools.partial(async_func, 1)
        assert is_async_callable(partial) is True

    def test_partial_sync_function(self):
        """Test with functools.partial wrapping sync function."""

        def sync_func(a, b):
            return a + b

        partial = functools.partial(sync_func, 1)
        assert is_async_callable(partial) is False

    def test_lambda(self):
        """Test with lambda function."""
        assert is_async_callable(lambda: None) is False

    def test_builtin(self):
        """Test with builtin function."""
        assert is_async_callable(len) is False


class TestSerializationUtils:
    """Tests for _dump_data and _load_data utilities."""

    def test_dump_and_load_dict(self):
        """Test serializing and deserializing dictionary."""
        data = {"key": "value", "number": 42, "nested": {"inner": "data"}}
        serialized = _dump_data(data)
        deserialized = _load_data(serialized)
        assert deserialized == data

    def test_dump_and_load_list(self):
        """Test serializing and deserializing list."""
        data = [1, 2, 3, "four", {"five": 5}]
        serialized = _dump_data(data)
        deserialized = _load_data(serialized)
        assert deserialized == data

    def test_dump_and_load_various_types(self):
        """Test serializing and deserializing various types."""
        data = {
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "str": "hello",
            "list": [1, 2, 3],
        }
        serialized = _dump_data(data)
        deserialized = _load_data(serialized)
        assert deserialized == data

    def test_dump_and_load_empty_structures(self):
        """Test serializing and deserializing empty structures."""
        data = {"empty_dict": {}, "empty_list": []}
        serialized = _dump_data(data)
        deserialized = _load_data(serialized)
        assert deserialized == data

    def test_dump_produces_string(self):
        """Test that _dump_data produces a string."""
        serialized = _dump_data({"key": "value"})
        assert isinstance(serialized, str)

    def test_dump_produces_consistent_output(self):
        """Test that _dump_data produces consistent output for same input."""
        data = {"key": "value", "number": 42}
        serialized1 = _dump_data(data)
        serialized2 = _dump_data(data)
        assert serialized1 == serialized2

    def test_load_raises_on_corrupt_data(self):
        """Test that _load_data raises exception on corrupted data."""
        with pytest.raises((
            MsgPackDecodeError,
            ValueError,
        )):  # msgpack.exceptions.UnpackException
            _load_data("not_valid_base85_data!")


class TestTimeFunction:
    """Tests for time utility."""

    def test_time_returns_float(self):
        """Test that time() returns a float."""
        t = time()
        assert isinstance(t, float)

    def test_time_is_positive(self):
        """Test that time() returns positive value."""
        t = time()
        assert t > 0

    def test_time_is_monotonic_increasing(self):
        """Test that time() returns increasing values."""
        t1 = time()
        anyio.run(anyio.sleep, 0.01)
        t2 = time()
        assert t2 >= t1

    def test_time_is_unix_timestamp(self):
        """Test that time() returns unix timestamp (reasonable range)."""
        t = time()
        # Should be within reasonable unix timestamp range (after 2020, before 2100)
        assert 1577836800 < t < 4102444800  # 2020-01-01 to 2100-01-01


class TestTaskTimer:
    """Tests for TaskTimer context manager."""

    async def test_task_timer_without_timeout(self):
        """Test TaskTimer with None timeout allows execution."""
        loop = asyncio.get_event_loop()
        async with TaskTimer(timeout=None, loop=loop) as timer:
            # Should complete without timing out
            await anyio.sleep(0.01)
        assert timer.done()
        assert not timer.timed_out()

    async def test_task_timer_allows_fast_execution(self):
        """Test TaskTimer allows execution within timeout."""
        loop = asyncio.get_event_loop()
        async with TaskTimer(timeout=0.1, loop=loop) as timer:
            await anyio.sleep(0.01)
        assert timer.done()
        assert not timer.timed_out()

    async def test_task_timer_times_out(self):
        """Test TaskTimer times out on slow execution."""
        loop = asyncio.get_event_loop()
        with pytest.raises(asyncio.TimeoutError):
            async with TaskTimer(timeout=0.05, loop=loop):
                await anyio.sleep(0.2)

    async def test_task_timer_sets_timed_out_flag(self):
        """Test TaskTimer sets timed_out flag on timeout."""
        loop = asyncio.get_event_loop()
        timer = None
        try:
            async with TaskTimer(timeout=0.05, loop=loop) as timer:
                await anyio.sleep(0.2)
        except asyncio.TimeoutError:
            assert timer is not None
            assert timer.timed_out()

    async def test_task_timer_cancelled_state(self):
        """Test TaskTimer cancelled() returns True when stopped normally."""
        loop = asyncio.get_event_loop()
        async with TaskTimer(timeout=None, loop=loop) as timer:
            pass
        assert timer.done()
        assert timer.cancelled()

    async def test_task_timer_custom_error(self):
        """Test TaskTimer with custom error."""
        loop = asyncio.get_event_loop()
        custom_error = RuntimeError("Custom timeout error")
        with pytest.raises(RuntimeError, match="Custom timeout error"):
            async with TaskTimer(timeout=0.05, loop=loop, error=custom_error):
                await anyio.sleep(0.2)

    async def test_task_timer_cannot_restart(self):
        """Test TaskTimer cannot be restarted after completion."""
        loop = asyncio.get_event_loop()
        timer = TaskTimer(timeout=None, loop=loop)
        timer.start()
        timer.stop()
        with pytest.raises(RuntimeError, match="already cancelled or timed out"):
            timer.start()


class TestAddParameterToSignature:
    """Tests for _add_parameter_to_signature utility."""

    def test_add_parameter_at_beginning(self):
        """Test adding parameter at beginning of signature."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=0)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params[0] == "new_param"
        assert params == ["new_param", "a", "b"]

    def test_add_parameter_at_end(self):
        """Test adding parameter at end of signature."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=-1)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params[-1] == "new_param"
        assert params == ["a", "b", "new_param"]

    def test_add_parameter_in_middle(self):
        """Test adding parameter in middle of signature."""

        def func(a: int, b: str, c: bool):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=1)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params == ["a", "new_param", "b", "c"]

    def test_add_parameter_with_default(self):
        """Test adding parameter with default value."""

        def func(a: int = 3):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str,
            default="default_value",
        )
        updated = _add_parameter_to_signature(func, param, index=0)
        sig = inspect.signature(updated)
        assert sig.parameters["new_param"].default == "default_value"

    def test_add_parameter_index_out_of_bounds(self):
        """Test adding parameter with invalid index raises error."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        with pytest.raises(ValueError, match="out of bounds"):
            _add_parameter_to_signature(func, param, index=10)

    def test_add_parameter_preserves_annotations(self):
        """Test that adding parameter preserves existing annotations."""

        def func(a: int, b: str) -> bool:
            return bool(a and b)

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=1)
        sig = inspect.signature(updated)
        assert sig.return_annotation is bool
        assert sig.parameters["a"].annotation is int
        assert sig.parameters["b"].annotation is str


@pytest.mark.anyio
class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    async def test_initial_state(self):
        """Test circuit breaker starts in closed state."""
        breaker = CircuitBreaker()
        assert breaker.is_open is False
        assert breaker._state == CircuitState.CLOSED
        assert breaker._failure_count == 0

    async def test_open_after_threshold(self):
        """Test circuit opens after reaching failure threshold."""
        breaker = CircuitBreaker(failure_threshold=3)

        # Record failures
        await breaker.record_failure()
        assert breaker.is_open is False

        await breaker.record_failure()
        assert breaker.is_open is False

        await breaker.record_failure()
        # Should open after 3rd failure
        assert breaker.is_open is True
        assert breaker._state == CircuitState.OPEN

    async def test_half_open_after_timeout(self):
        """Test circuit enters half-open state after recovery timeout."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,  # 100ms
        )

        # Open the circuit
        await breaker.record_failure()
        assert breaker.is_open is True

        # Should still be open immediately
        assert breaker.is_open is True

        # Wait for recovery timeout
        await anyio.sleep(0.15)

        # Call allow_execution to trigger state transition to half-open
        allowed = await breaker.allow_execution()
        assert allowed is True
        assert breaker._state == CircuitState.HALF_OPEN

    async def test_close_from_half_open(self):
        """Test circuit closes after successful operations in half-open."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
            success_threshold=2,
        )

        # Open the circuit
        await breaker.record_failure()
        assert breaker.is_open is True

        # Wait for half-open
        await anyio.sleep(0.02)
        await breaker.allow_execution()  # Trigger transition to half-open

        # Record successes
        await breaker.record_success()
        assert breaker._state == CircuitState.HALF_OPEN

        await breaker.record_success()
        # Should close after 2nd success
        assert breaker._state == CircuitState.CLOSED

    async def test_reopen_from_half_open_on_failure(self):
        """Test circuit reopens if failure occurs in half-open state."""
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        # Open the circuit
        await breaker.record_failure()

        # Wait for half-open
        await anyio.sleep(0.02)
        await breaker.allow_execution()
        assert breaker._state == CircuitState.HALF_OPEN

        # Record a failure in half-open
        await breaker.record_failure()
        assert breaker._state == CircuitState.OPEN

    async def test_reset_failures_on_success_in_closed(self):
        """Test failures reset on success when circuit is closed."""
        breaker = CircuitBreaker(failure_threshold=3)

        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker._failure_count == 2

        # Success resets counter
        await breaker.record_success()
        assert breaker._failure_count == 0
        assert breaker._state == CircuitState.CLOSED

    async def test_info(self):
        """Test info() returns current state."""
        breaker = CircuitBreaker()
        info = await breaker.info()

        assert "state" in info
        assert "failure_count" in info
        assert "success_count" in info
        assert "opened_at" in info
        assert info["state"] == CircuitState.CLOSED.value
