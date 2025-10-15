import asyncio

import pytest

from traffik.backends.base import get_throttle_backend
from tests.conftest import BackendsGen


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_get_key(backends: BackendsGen) -> None:
    for backend in backends(namespace="generics"):
        key1 = await backend.get_key("test_key")
        assert isinstance(key1, str)
        assert key1.startswith(backend.namespace)


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_crud(backends: BackendsGen) -> None:
    for backend in backends(namespace="generics"):
        async with backend():
            key = await backend.get_key("test_key")
            await backend.set(key, "test_value")
            value = await backend.get(key)
            assert value == "test_value"
            await backend.delete(key)
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_clear(backends: BackendsGen) -> None:
    for backend in backends(namespace="generics"):
        async with backend():
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
@pytest.mark.native
async def test_context_management(backends: BackendsGen) -> None:
    for backend in backends(namespace="context_test"):
        # Test that the context variable is initialized to None
        assert get_throttle_backend() is None
        async with backend():
            assert backend.connection is not None
            # Test that the context variable is set within the context
            assert get_throttle_backend() is backend

        # Test that the context variable is reset after exiting the context
        assert get_throttle_backend() is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_expire(backends: BackendsGen) -> None:
    """Test that keys with expiration are properly handled."""
    for backend in backends(namespace="expire_test"):
        async with backend():
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
@pytest.mark.native
async def test_multiple_namespaces(backends: BackendsGen) -> None:
    """Test that different backends with different namespaces don't interfere."""
    backends_list = list(backends(namespace="namespace_test_1"))
    backends_list_2 = list(backends(namespace="namespace_test_2"))

    for backend, backend2 in zip(backends_list, backends_list_2):
        async with backend():
            async with backend2():
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
@pytest.mark.native
async def test_increment(backends: BackendsGen) -> None:
    """Test atomic increment operation."""
    for backend in backends(namespace="increment_test"):
        async with backend():
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
@pytest.mark.native
async def test_increment_concurrent(backends: BackendsGen) -> None:
    """Test that increment is atomic under concurrent operations."""
    for backend in backends(namespace="concurrent_increment"):
        async with backend():
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
@pytest.mark.native
async def test_expire_on_existing_key(backends: BackendsGen) -> None:
    """Test setting expiration on an existing key."""
    for backend in backends(namespace="expire_existing_test"):
        async with backend():
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
@pytest.mark.native
async def test_expire_on_nonexistent_key(backends: BackendsGen) -> None:
    """Test setting expiration on a non-existent key."""
    for backend in backends(namespace="expire_nonexistent_test"):
        async with backend():
            key = await backend.get_key("nonexistent_key")

            # Try to set expiration on non-existent key
            result = await backend.expire(key, 1)
            assert result is False


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_decrement(backends: BackendsGen) -> None:
    """Test atomic decrement operation."""
    for backend in backends(namespace="decrement_test"):
        async with backend():
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
@pytest.mark.native
async def test_decrement_on_nonexistent(backends: BackendsGen) -> None:
    """Test decrement on non-existent key."""
    for backend in backends(namespace="decrement_nonexistent_test"):
        async with backend():
            key = await backend.get_key("nonexistent_counter")

            # Decrement on non-existent key should return -1
            value = await backend.decrement(key)
            assert value == -1

            # Verify the stored value
            stored_value = await backend.get(key)
            assert stored_value == "-1"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_backend_increment_with_ttl(backends: BackendsGen) -> None:
    """Test increment with TTL operation - basic functionality."""
    for backend in backends(namespace="increment_ttl_test"):
        async with backend():
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
@pytest.mark.native
async def test_multi_get(backends: BackendsGen) -> None:
    """Test getting multiple keys at once."""
    for backend in backends(namespace="get_multi_test"):
        async with backend():
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
@pytest.mark.native
async def test_multi_get_empty(backends: BackendsGen) -> None:
    """Test getting multiple keys with empty arguments."""
    for backend in backends(namespace="get_multi_empty_test"):
        async with backend():
            # Get with no keys
            values = await backend.multi_get()
            assert values == []


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_multi_get_single(backends: BackendsGen) -> None:
    """Test getting a single key with multi_get."""
    for backend in backends(namespace="get_multi_single_test"):
        async with backend():
            key = await backend.get_key("single_key")
            await backend.set(key, "single_value")

            # Get single key
            values = await backend.multi_get(key)

            assert len(values) == 1
            assert values[0] == "single_value"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_get_key_with_args(backends: BackendsGen) -> None:
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
@pytest.mark.native
async def test_delete_nonexistent(backends: BackendsGen) -> None:
    """Test deleting a non-existent key."""
    for backend in backends(namespace="delete_test"):
        async with backend():
            key = await backend.get_key("nonexistent_key")
            # Deleting non-existent key should return False
            result = await backend.delete(key)
            assert result is False


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_delete_existing(backends: BackendsGen) -> None:
    """Test deleting an existing key."""
    for backend in backends(namespace="delete_existing_test"):
        async with backend():
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
@pytest.mark.native
async def test_set_overwrite(backends: BackendsGen) -> None:
    """Test that set overwrites existing values."""
    for backend in backends(namespace="set_overwrite_test"):
        async with backend():
            key = await backend.get_key("test_key")

            # Set initial value
            await backend.set(key, "value1")
            assert await backend.get(key) == "value1"

            # Overwrite with new value
            await backend.set(key, "value2")
            assert await backend.get(key) == "value2"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_set_with_expire_overwrite(backends: BackendsGen) -> None:
    """Test that set with expiration can overwrite existing keys."""
    for backend in backends(namespace="set_expire_overwrite_test"):
        async with backend():
            key = await backend.get_key("test_key")

            # Set with long expiration
            await backend.set(key, "value1", expire=60)
            assert await backend.get(key) == "value1"

            # Overwrite with short expiration
            await backend.set(key, "value2", expire=1)
            assert await backend.get(key) == "value2"

            # Wait for new expiration
            await asyncio.sleep(1.02)

            # Key should be expired
            value = await backend.get(key)
            assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_lock_basic(backends: BackendsGen) -> None:
    """Test basic lock acquisition and release."""
    for backend in backends(namespace="lock_test"):
        async with backend():
            lock_name = "test_lock"

            # Acquire lock
            async with await backend.lock(lock_name) as lock_context:
                assert lock_context._lock.locked()
                # Lock is held
                pass

            # Lock should be released after exiting context


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_lock_prevents_concurrent_access(backends: BackendsGen) -> None:
    """Test that locks prevent concurrent access to shared resources."""
    for backend in backends(namespace="lock_concurrent_test"):
        async with backend():
            key = await backend.get_key("shared_counter")
            await backend.set(key, "0")
            lock_name = "counter_lock"

            async def increment_with_lock():
                async with await backend.lock(lock_name):
                    # Read current value
                    value = int(await backend.get(key) or "0")
                    # Simulate some work
                    await asyncio.sleep(0.01)
                    # Write new value
                    await backend.set(key, str(value + 1))

            # Run 10 concurrent increments with locking
            await asyncio.gather(*[increment_with_lock() for _ in range(10)])

            # Final value should be exactly 10 (no race conditions)
            final_value = await backend.get(key)
            assert final_value == "10"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_increment_with_ttl_expiration(backends: BackendsGen) -> None:
    """Test that increment_with_ttl properly expires keys."""
    for backend in backends(namespace="increment_ttl_expire_test"):
        async with backend():
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
@pytest.mark.native
async def test_increment_with_ttl_no_reapply(backends: BackendsGen) -> None:
    """Test that `increment_with_ttl` doesn't reset TTL on existing keys."""
    for backend in backends(namespace="increment_ttl_no_reset_test"):
        async with backend():
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
