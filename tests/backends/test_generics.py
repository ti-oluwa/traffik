"""Generic tests (that should pass) for all backend implementations."""

import asyncio
import threading
from collections import defaultdict, deque

import pytest

from tests.conftest import BackendGen
from traffik.backends.base import get_throttle_backend
from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import BackendConnectionError, LockTimeoutError


@pytest.mark.asyncio
@pytest.mark.backend
async def test_get_key(backends: BackendGen) -> None:
    for backend in backends(namespace="generics"):
        key1 = await backend.get_key("test_key")
        assert isinstance(key1, str)
        assert key1.startswith(backend.namespace)


@pytest.mark.asyncio
@pytest.mark.backend
async def test_crud(backends: BackendGen) -> None:
    for backend in backends(namespace="generics"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("test_key")
            await backend.set(key, "test_value")
            value = await backend.get(key)
            assert value == "test_value"
            await backend.delete(key)
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_clear(backends: BackendGen) -> None:
    for backend in backends(namespace="generics"):
        async with backend(close_on_exit=True):
            key1 = await backend.get_key("key1")
            key2 = await backend.get_key("key2")
            await backend.set(key1, "value1")
            await backend.set(key2, "value2")
            await backend.reset()
            await backend.initialize()
            assert await backend.get(key1) is None
            assert await backend.get(key2) is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_context_management(backends: BackendGen) -> None:
    for backend in backends(namespace="context_test"):
        # Test that the context variable is initialized to None
        assert get_throttle_backend() is None
        async with backend(close_on_exit=True):
            assert backend.connection is not None
            # Test that the context variable is set within the context
            assert get_throttle_backend() is backend

        # Test that the context variable is reset after exiting the context
        assert get_throttle_backend() is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_expire(backends: BackendGen) -> None:
    """Test that keys with expiration are properly handled."""
    for backend in backends(namespace="expire_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("expire_test")
            # Set value with 1 second expiration
            await backend.set(key, "test_value", expire=1)

            # Value should exist immediately
            value = await backend.get(key)
            assert value == "test_value"
            # Wait for expiration (1.02 seconds)
            await asyncio.sleep(1.02)
            # Value should be expired and return None
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_namespace_isolation(backends: BackendGen) -> None:
    """Test that different backends with different namespaces don't interfere."""
    backends_list = list(backends(namespace="namespace_test_1"))
    backends_list_2 = list(backends(namespace="namespace_test_2"))

    for backend, backend2 in zip(backends_list, backends_list_2):
        async with backend(close_on_exit=True), backend2(close_on_exit=True):
            # Set values in both backends
            key1 = await backend.get_key("shared_key")
            key2 = await backend2.get_key("shared_key")

            await backend.set(key1, "value1")
            await backend2.set(key2, "value2")

            # Verify values are isolated
            assert await backend.get(key1) == "value1"
            assert await backend2.get(key2) == "value2"
            assert key1 != key2  # Keys should be different


@pytest.mark.asyncio
@pytest.mark.backend
async def test_increment(backends: BackendGen) -> None:
    """Test atomic increment operation."""
    for backend in backends(namespace="increment_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("counter")

            # First increment on non-existent key should return 1
            value = await backend.increment(key)
            assert value == 1

            # Second increment should return 2
            value = await backend.increment(key)
            assert value == 2

            # Increment by custom amount
            value = await backend.increment(key, amount=5)
            assert value == 7

            # Verify the stored value
            stored_value = await backend.get(key)
            assert stored_value == "7"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_concurrent_increment(backends: BackendGen) -> None:
    """Test that increment is atomic under concurrent operations."""
    for backend in backends(namespace="concurrent_increment"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("concurrent_counter")

            # Perform multiple concurrent increments
            results = await asyncio.gather(*[backend.increment(key) for _ in range(10)])

            # All results should be unique (atomicity guarantee)
            assert len(set(results)) == 10

            # Final value should be 10
            final_value = await backend.get(key)
            assert final_value == "10"


@pytest.mark.asyncio
@pytest.mark.backend
async def test_expire_on_existing_key(backends: BackendGen) -> None:
    """Test setting expiration on an existing key."""
    for backend in backends(namespace="expire_existing_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("test_key")

            # Set a key without expiration
            await backend.set(key, "test_value")

            # Set expiration on existing key
            result = await backend.expire(key, 1)
            assert result is True

            # Value should exist immediately
            value = await backend.get(key)
            assert value == "test_value"

            # Wait for expiration
            await asyncio.sleep(1.02)

            # Value should be expired
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_expire_on_nonexistent_key(backends: BackendGen) -> None:
    """Test setting expiration on a non-existent key."""
    for backend in backends(namespace="expire_nonexistent_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("nonexistent_key")

            # Try to set expiration on non-existent key
            result = await backend.expire(key, 1)
            assert result is False


@pytest.mark.asyncio
@pytest.mark.backend
async def test_decrement(backends: BackendGen) -> None:
    """Test atomic decrement operation."""
    for backend in backends(namespace="decrement_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("counter")

            # Set initial value
            await backend.set(key, "10")

            # First decrement should return 9
            value = await backend.decrement(key)
            assert value == 9

            # Second decrement should return 8
            value = await backend.decrement(key)
            assert value == 8

            # Decrement by custom amount
            value = await backend.decrement(key, amount=3)
            assert value == 5

            # Verify the stored value
            stored_value = await backend.get(key)
            assert stored_value is not None
            assert int(stored_value) == 5


@pytest.mark.asyncio
@pytest.mark.backend
async def test_decrement_on_nonexistent(backends: BackendGen) -> None:
    """Test decrement on non-existent key."""
    for backend in backends(namespace="decrement_nonexistent_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("nonexistent_counter")

            # Decrement on non-existent key should return -1
            value = await backend.decrement(key)
            assert value == -1

            # Verify the stored value
            stored_value = await backend.get(key)
            assert stored_value == "-1"


@pytest.mark.asyncio
@pytest.mark.backend
async def test_increment_with_ttl(backends: BackendGen) -> None:
    """Test increment with TTL operation."""
    for backend in backends(namespace="increment_ttl_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("ttl_counter")

            # First increment should set TTL and return 1
            value = await backend.increment_with_ttl(key, amount=1, ttl=2)
            assert value == 1

            # Second increment should increment and return 2
            value = await backend.increment_with_ttl(key, amount=1, ttl=2)
            assert value == 2

            # Value should exist before expiration
            stored_value = await backend.get(key)
            assert stored_value == "2"

            # Wait for TTL to expire (2 seconds + buffer)
            await asyncio.sleep(2.2)

            # Counter should be expired
            stored_value = await backend.get(key)
            assert stored_value is None

            # New increment after expiration should start fresh at 1
            value = await backend.increment_with_ttl(key, amount=1, ttl=60)
            assert value == 1


@pytest.mark.asyncio
@pytest.mark.backend
async def test_multi_get(backends: BackendGen) -> None:
    """Test getting multiple keys at once."""
    for backend in backends(namespace="get_multi_test"):
        async with backend(close_on_exit=True):
            # Set up multiple keys
            key1 = await backend.get_key("key1")
            key2 = await backend.get_key("key2")
            key3 = await backend.get_key("key3")
            key4 = await backend.get_key("nonexistent")

            await backend.set(key1, "value1")
            await backend.set(key2, "value2")
            await backend.set(key3, "value3")

            # Get multiple keys (using *args unpacking)
            values = await backend.multi_get(key1, key2, key3, key4)

            assert len(values) == 4
            assert values[0] == "value1"
            assert values[1] == "value2"
            assert values[2] == "value3"
            assert values[3] is None  # Nonexistent key


@pytest.mark.asyncio
@pytest.mark.backend
async def test_multi_get_empty(backends: BackendGen) -> None:
    """Test getting multiple keys with empty arguments."""
    for backend in backends(namespace="get_multi_empty_test"):
        async with backend(close_on_exit=True):
            # Get with no keys
            values = await backend.multi_get()
            assert values == []


@pytest.mark.asyncio
@pytest.mark.backend
async def test_multi_get_single(backends: BackendGen) -> None:
    """Test getting a single key with multi_get."""
    for backend in backends(namespace="get_multi_single_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("single_key")
            await backend.set(key, "single_value")

            # Get single key
            values = await backend.multi_get(key)

            assert len(values) == 1
            assert values[0] == "single_value"


@pytest.mark.asyncio
@pytest.mark.backend
async def test_get_key_with_args(backends: BackendGen) -> None:
    """Test get_key with additional arguments for building composite keys."""
    for backend in backends(namespace="composite_key_test"):
        # Test with positional args
        key1 = await backend.get_key("user", "123", "quota")
        assert isinstance(key1, str)
        assert key1.startswith(backend.namespace)

        # Test with keyword args
        key2 = await backend.get_key("user", user_id="123", resource="quota")
        assert isinstance(key2, str)
        assert key2.startswith(backend.namespace)

        # Keys with same params should be identical (deterministic)
        key3 = await backend.get_key("user", user_id="123", resource="quota")
        assert key2 == key3

        # Different params should produce different keys
        key4 = await backend.get_key("user", user_id="456", resource="quota")
        assert key2 != key4


@pytest.mark.asyncio
@pytest.mark.backend
async def test_delete_nonexistent(backends: BackendGen) -> None:
    """Test deleting a non-existent key."""
    for backend in backends(namespace="delete_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("nonexistent_key")
            # Deleting non-existent key should return False
            result = await backend.delete(key)
            assert result is False


@pytest.mark.asyncio
@pytest.mark.backend
async def test_delete_existing(backends: BackendGen) -> None:
    """Test deleting an existing key."""
    for backend in backends(namespace="delete_existing_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("existing_key")

            # Set a value
            await backend.set(key, "value")
            assert await backend.get(key) == "value"

            # Delete should succeed
            result = await backend.delete(key)
            assert result is True

            # Key should no longer exist
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_set_overwrite(backends: BackendGen) -> None:
    """Test that set overwrites existing values."""
    for backend in backends(namespace="set_overwrite_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("test_key")

            # Set initial value
            await backend.set(key, "value1")
            assert await backend.get(key) == "value1"

            # Overwrite with new value
            await backend.set(key, "value2")
            assert await backend.get(key) == "value2"


@pytest.mark.asyncio
@pytest.mark.backend
async def test_set_with_expire_overwrite(backends: BackendGen) -> None:
    """Test that set with expiration can overwrite existing keys."""
    for backend in backends(namespace="set_expire_overwrite_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("test_key")

            # Set with long expiration
            await backend.set(key, "value1", expire=60)
            assert await backend.get(key) == "value1"

            # Overwrite with short expiration
            await backend.set(key, "value2", expire=1)
            assert await backend.get(key) == "value2"

            # Wait for new expiration
            await asyncio.sleep(1.1)

            # Key should be expired
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_lock_basic(backends: BackendGen) -> None:
    """Test basic lock acquisition and release."""
    for backend in backends(namespace="lock_test"):
        async with backend(persistent=False, close_on_exit=True):
            lock_name = "lock:test"
            # Acquire lock
            async with await backend.lock(lock_name) as lock_context:
                # Lock should be held
                assert lock_context._lock.locked()
            # Lock should be released after exiting context
            assert not lock_context._lock.locked()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
async def test_asynchronous_lock_synchronization(backends: BackendGen) -> None:
    """Test that locks prevent concurrent access to shared resources in asyncio context."""
    for backend in backends(namespace="lock_concurrent_test"):
        async with backend(persistent=False, close_on_exit=True):
            key = await backend.get_key("shared_counter")
            await backend.set(key, "0")
            lock_name = "lock:counter"

            async def increment_with_lock(
                blocking: bool, blocking_timeout: float = 0.5
            ):
                async with await backend.lock(
                    lock_name, blocking=blocking, blocking_timeout=blocking_timeout
                ):
                    # Read current value
                    value = int(await backend.get(key) or "0")
                    # Simulate some work
                    await asyncio.sleep(0.001)
                    # Write new value
                    await backend.set(key, str(value + 1))

            # 1. Run 10 concurrent increments with locking
            await asyncio.gather(
                *[
                    increment_with_lock(blocking=True, blocking_timeout=5.0)
                    for _ in range(10)
                ]
            )

            # Final value should be exactly 10 (no race conditions)
            final_value = await backend.get(key)
            assert final_value == "10"

            # 2. Try blocking with short timeout, and a timeout error should be raised
            # Exclude `InMemoryBackend` as it does not support `blocking_timeout` yet
            if not isinstance(backend, InMemoryBackend):
                with pytest.raises(LockTimeoutError):
                    await asyncio.gather(
                        *[
                            increment_with_lock(blocking=True, blocking_timeout=0.01)
                            for _ in range(10)
                        ]
                    )

            # 3. Try non-blocking now, and a timeout error should be raised
            with pytest.raises(LockTimeoutError):
                await asyncio.gather(
                    *[increment_with_lock(blocking=False) for _ in range(5)]
                )


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
@pytest.mark.multithreaded
async def test_multithreaded_lock_synchronization(backends: BackendGen) -> None:
    """Test that locks prevent concurrent access to shared resources across real OS threads."""
    results = defaultdict(deque)
    results_lock = threading.RLock()
    errors = deque()

    no_of_backends = 0
    # Initialial cleanup, to ensure no leftover keys
    for backend in backends(namespace="lock_multithread_test", exclude=InMemoryBackend):
        key = await backend.get_key("shared_counter")
        async with backend(persistent=False, close_on_exit=True):
            await backend.delete(key)
        no_of_backends += 1

    if no_of_backends == 0:
        return  # No backends to test

    def thread_worker(thread_id: int, blocking: bool, blocking_timeout: float = 0.5):
        """Worker function to be run in separate threads."""

        async def work():
            # Exclude `InMemoryBackend` as it does not support multithreaded synchronization.
            for backend in backends(
                namespace="lock_multithread_test", exclude=InMemoryBackend
            ):
                key = await backend.get_key("shared_counter")
                async with backend(persistent=True, close_on_exit=True):
                    async with await backend.lock(
                        "lock:thread_counter",
                        blocking=blocking,
                        blocking_timeout=blocking_timeout,
                    ):
                        # Read current value
                        value = int(await backend.get(key) or "0")
                        # Simulate work
                        await asyncio.sleep(0.01)
                        # Write new value
                        new_value = value + 1
                        await backend.set(key, str(new_value))
                        with results_lock:
                            backend_name = backend.__class__.__name__
                            backend_results = results[backend_name]
                            backend_results.append((thread_id, new_value))
                            results[backend_name] = backend_results

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(work())
        except (asyncio.CancelledError, RuntimeError):
            raise
        except Exception as exc:
            with results_lock:
                errors.append((thread_id, str(exc)))
        finally:
            loop.close()

    # 1. Run 10 threads with blocking locks
    threads = []
    for i in range(10):
        thread = threading.Thread(
            target=thread_worker,
            kwargs={
                "thread_id": i,
                "blocking": True,
                "blocking_timeout": 1.5,
            },
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=5)

    # Verify no errors
    assert len(errors) == 0, f"Errors occurred: {errors}"
    # Verify all increments completed
    for backend_name, backend_results in results.items():
        assert len(backend_results) == 10, (
            f"Expected 10 results for {backend_name}, got {len(backend_results)}"
        )

    for backend in backends(namespace="lock_multithread_test", exclude=InMemoryBackend):
        key = await backend.get_key("shared_counter")
        # Final value should be exactly 10 (no race conditions)
        async with backend(close_on_exit=True):
            final_value = await backend.get(key)
            assert final_value == "10", f"Expected '10', got '{final_value}'"
            # Cleanup for next tests
            await backend.delete(key)

    # 2. Try blocking with short timeout. It should raise timeout errors
    # Clear previous results/errors
    errors.clear()
    results.clear()

    threads = []
    for i in range(10):
        thread = threading.Thread(
            target=thread_worker,
            kwargs={
                "thread_id": i,
                "blocking": True,
                "blocking_timeout": 0.01,
            },
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=5)

    # Should have timeout errors
    assert len(errors) > 0, "Expected timeout errors with short `blocking_timeout`"
    # Verify at least some contain timeout errors
    timeout_errors = [
        e for _, e in errors if "timeout" in e.lower() or "lock" in e.lower()
    ]
    assert len(timeout_errors) > 0, f"Expected timeout errors, got: {errors}"

    # Cleanup for next test
    for backend in backends(namespace="lock_multithread_test", exclude=InMemoryBackend):
        key = await backend.get_key("shared_counter")
        async with backend(close_on_exit=True):
            await backend.delete(key)

    # 3. Try non-blocking. It should raise timeout errors
    errors.clear()
    results.clear()

    threads = []
    for i in range(5):
        thread = threading.Thread(
            target=thread_worker,
            kwargs={
                "thread_id": i,
                "blocking": False,
                "blocking_timeout": None,
            },
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=5)

    # Should have timeout errors (non-blocking locks fail when already held)
    assert len(errors) > 0, "Expected timeout errors with non-blocking locks"
    timeout_errors = [
        e for _, e in errors if "timeout" in e.lower() or "lock" in e.lower()
    ]
    assert len(timeout_errors) > 0, f"Expected timeout errors, got: {errors}"

    # Final cleanup
    for backend in backends(namespace="lock_multithread_test", exclude=InMemoryBackend):
        key = await backend.get_key("shared_counter")
        async with backend(close_on_exit=True):
            await backend.delete(key)


@pytest.mark.asyncio
@pytest.mark.backend
async def test_increment_with_ttl_expiration(backends: BackendGen) -> None:
    """Test that increment_with_ttl properly expires keys."""
    for backend in backends(namespace="increment_ttl_expire_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("expiring_counter")

            # Increment with short TTL
            value = await backend.increment_with_ttl(key, amount=1, ttl=1)
            assert value == 1

            # Value should exist immediately
            stored = await backend.get(key)
            assert stored == "1"

            # Wait for expiration
            await asyncio.sleep(1.1)

            # Key should be expired
            stored = await backend.get(key)
            assert stored is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_increment_with_ttl_no_reapply(backends: BackendGen) -> None:
    """Test that `increment_with_ttl` doesn't reset TTL on existing keys."""
    for backend in backends(namespace="increment_ttl_no_reset_test"):
        async with backend(close_on_exit=True):
            key = await backend.get_key("ttl_counter")

            # First increment sets TTL of 60 seconds
            value = await backend.increment_with_ttl(key, amount=1, ttl=60)
            assert value == 1

            # Second increment should not reset TTL (uses short TTL but shouldn't apply)
            value = await backend.increment_with_ttl(key, amount=1, ttl=1)
            assert value == 2

            # Wait 1.1 seconds
            await asyncio.sleep(1.1)

            # Key should still exist because first TTL (60s) is still active
            stored = await backend.get(key)
            assert stored == "2"


@pytest.mark.asyncio
@pytest.mark.backend
async def test_close(backends: BackendGen) -> None:
    """Test that backends can be closed properly."""
    for backend in backends(namespace="close_test"):
        await backend.initialize()
        assert backend.connection is not None

        await backend.close()
        assert backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_multi_context_usage(backends: BackendGen) -> None:
    """Test that backend can handle multiple context entries/exits."""
    for backend in backends(namespace="multi_context_text"):
        key = await backend.get_key("multi_context_key")
        # First context
        async with backend(persistent=True, close_on_exit=False):
            await backend.set(key, "value1")
            assert await backend.get(key) == "value1"

        # Connection still alive (persistent)
        assert backend.connection is not None
        # Data still persist
        assert await backend.get(key) == "value1"

        # Second context (reusing same backend)
        async with backend(persistent=True, close_on_exit=False):
            # Can still access data from first context
            assert await backend.get(key) == "value1"
            await backend.set(key, "value2")

        # Verify update persists
        assert await backend.get(key) == "value2"
        # Cleanup
        await backend.reset()
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_single_backend_context_nesting(backends: BackendGen) -> None:
    """Test that a single backend can be context nested 'safely'"""
    for backend in backends(namespace="context_nesting_test", persistent=False):
        key = await backend.get_key("context_nesting_key")
        # Outer context
        async with backend(persistent=False, close_on_exit=False):
            await backend.set(key, "value1")
            assert await backend.get(key) == "value1"

            inner_context1 = backend()
            # Except when explicitly set, when a backend context is nested within another,
            # by default, it should not close on exiting the context
            assert inner_context1.close_on_exit is False
            # Since persistence is not explicitly set for the inner  context,
            # ensure the inner context (from the same backend) is persistent, although the backend is non-persistent
            # This behaviour prevent unexpected behaviour and ensure data integrity when nesting context's of a single backend
            assert inner_context1.persistent is True
            async with inner_context1:
                # Confirm value from parent context still remains
                assert await backend.get(key) == "value1"

                await backend.set(key, "value2")
                # Confirm overwrite
                assert await backend.get(key) == "value2"

                # Explicitly set `close_on_exit` and `persistent` now
                inner_context2 = backend(close_on_exit=True, persistent=False)
                assert inner_context2.close_on_exit is True
                assert inner_context2.persistent is False
                async with inner_context2:
                    # Confirm value from parent context still remains
                    assert await backend.get(key) == "value2"

            assert backend.connection is None
            # Backend should raise and error now that is connection was clsed by inner_context2
            with pytest.raises(BackendConnectionError):
                await backend.get(key)

            # Re-initialize backend and confirm data does not exists any more due to the non-persistence of inner_context2
            await backend.initialize()
            assert await backend.get(key) is None
