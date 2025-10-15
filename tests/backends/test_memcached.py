"""
Tests specific to the Memcached backend implementation.

Tests persistence behavior, reset functionality, and Memcached-specific features.
"""

import pytest

from traffik.backends.memcached import MemcachedBackend


@pytest.fixture(scope="function")
def memcached_backend() -> MemcachedBackend:
    """Create a Memcached backend instance for testing."""
    return MemcachedBackend()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_persistence(memcached_backend: MemcachedBackend) -> None:
    """Test that the backend persists data when set to persistent."""
    # Add some keys to the backend within the context
    async with memcached_backend(persistent=True):
        key = await memcached_backend.get_key("key1")
        await memcached_backend.set(key, "value1")

    # Test that the backend persists the keys even after exiting the context
    assert memcached_backend.connection is not None
    key = await memcached_backend.get_key("key1")
    value = await memcached_backend.get(key)
    assert value == "value1"

    # Test that the backend can be reset
    await memcached_backend.reset()
    assert memcached_backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_reset_with_data(memcached_backend: MemcachedBackend) -> None:
    """Test backend reset properly clears data."""
    key = await memcached_backend.get_key("test_key")
    async with memcached_backend(persistent=False):
        await memcached_backend.set(key, "test_value")
        # Verify data exists
        value = await memcached_backend.get(key)
        assert value == "test_value"

    # Backend should be closed after non-persistent context
    assert memcached_backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_non_persistent_cleanup(memcached_backend: MemcachedBackend) -> None:
    """Test that non-persistent mode cleans up after context exit."""
    async with memcached_backend(persistent=False):
        assert memcached_backend.connection is not None
        key = await memcached_backend.get_key("temp_key")
        await memcached_backend.set(key, "temp_value")

        # Value should exist within context
        value = await memcached_backend.get(key)
        assert value == "temp_value"

    # Connection should be closed after exiting non-persistent context
    assert memcached_backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_persistent_survives_context(memcached_backend: MemcachedBackend) -> None:
    """Test that persistent mode keeps connection alive after context exit."""
    # First context - set persistent
    async with memcached_backend(persistent=True):
        key = await memcached_backend.get_key("persistent_key")
        await memcached_backend.set(key, "persistent_value", expire=60)

    # Connection should still be alive
    assert memcached_backend.connection is not None

    # Data should still be accessible
    key = await memcached_backend.get_key("persistent_key")
    value = await memcached_backend.get(key)
    assert value == "persistent_value"

    # Cleanup
    await memcached_backend.reset()
    assert memcached_backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_connection_initialization(memcached_backend: MemcachedBackend) -> None:
    """Test that connection is properly initialized."""
    assert memcached_backend.connection is None

    async with memcached_backend():
        assert memcached_backend.connection is not None
        # Test basic connectivity with a ping-like operation
        key = await memcached_backend.get_key("init_test")
        await memcached_backend.set(key, "test")
        value = await memcached_backend.get(key)
        assert value == "test"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_custom_host_port(memcached_backend: MemcachedBackend) -> None:
    """Test backend with custom host and port configuration."""
    custom_backend = MemcachedBackend(
        host="localhost", port=11211, namespace="custom_test"
    )

    async with custom_backend(persistent=False):
        assert custom_backend.connection is not None
        key = await custom_backend.get_key("custom_key")
        await custom_backend.set(key, "custom_value")
        value = await custom_backend.get(key)
        assert value == "custom_value"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_namespace_isolation(memcached_backend: MemcachedBackend) -> None:
    """Test that different namespaces are properly isolated."""
    backend1 = MemcachedBackend(namespace="namespace1")
    backend2 = MemcachedBackend(namespace="namespace2")

    async with backend1():
        async with backend2():
            # Set same logical key in both backends
            key1 = await backend1.get_key("shared_key")
            key2 = await backend2.get_key("shared_key")

            await backend1.set(key1, "value1")
            await backend2.set(key2, "value2")

            # Values should be isolated
            assert await backend1.get(key1) == "value1"
            assert await backend2.get(key2) == "value2"

            # Keys should be different due to namespace
            assert key1 != key2


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_multiple_contexts(memcached_backend: MemcachedBackend) -> None:
    """Test that backend can handle multiple context entries/exits."""
    key = await memcached_backend.get_key("multi_context_key")

    # First context
    async with memcached_backend(persistent=True):
        await memcached_backend.set(key, "value1")
        assert await memcached_backend.get(key) == "value1"

    # Connection still alive (persistent)
    assert memcached_backend.connection is not None

    # Second context (reusing same backend)
    async with memcached_backend(persistent=True):
        # Can still access data from first context
        assert await memcached_backend.get(key) == "value1"
        await memcached_backend.set(key, "value2")

    # Verify update
    assert await memcached_backend.get(key) == "value2"

    # Cleanup
    await memcached_backend.reset()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_clear_functionality(memcached_backend: MemcachedBackend) -> None:
    """Test that clear removes all keys in the namespace."""
    async with memcached_backend(persistent=False):
        # Set multiple keys
        key1 = await memcached_backend.get_key("clear_key1")
        key2 = await memcached_backend.get_key("clear_key2")
        key3 = await memcached_backend.get_key("clear_key3")

        await memcached_backend.set(key1, "value1")
        await memcached_backend.set(key2, "value2")
        await memcached_backend.set(key3, "value3")

        # Verify all keys exist
        assert await memcached_backend.get(key1) == "value1"
        assert await memcached_backend.get(key2) == "value2"
        assert await memcached_backend.get(key3) == "value3"

        # Clear all keys
        await memcached_backend.clear()

        # Verify all keys are gone
        assert await memcached_backend.get(key1) is None
        assert await memcached_backend.get(key2) is None
        assert await memcached_backend.get(key3) is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_pool_configuration(memcached_backend: MemcachedBackend) -> None:
    """Test backend with custom pool configuration."""
    custom_backend = MemcachedBackend(
        pool_size=5, pool_minsize=2, namespace="pool_test"
    )

    async with custom_backend(persistent=False):
        assert custom_backend.connection is not None
        assert custom_backend.pool_size == 5
        assert custom_backend.pool_minsize == 2

        # Test basic operations work with custom pool
        key = await custom_backend.get_key("pool_key")
        await custom_backend.set(key, "pool_value")
        value = await custom_backend.get(key)
        assert value == "pool_value"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_expiration_respected(memcached_backend: MemcachedBackend) -> None:
    """Test that Memcached respects expiration times."""
    import asyncio

    async with memcached_backend(persistent=False):
        key = await memcached_backend.get_key("expire_key")

        # Set with 1 second expiration
        await memcached_backend.set(key, "expire_value", expire=1)

        # Value should exist immediately
        value = await memcached_backend.get(key)
        assert value == "expire_value"

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Value should be expired
        value = await memcached_backend.get(key)
        assert value is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_lock_acquisition(memcached_backend: MemcachedBackend) -> None:
    """Test that Memcached lock can be acquired and released."""
    async with memcached_backend(persistent=False):
        lock_name = "test_lock"

        # Acquire lock
        lock = await memcached_backend.get_lock(lock_name)
        async with lock:
            assert lock.locked()

        # Lock should be released
        assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_concurrent_lock_access(memcached_backend: MemcachedBackend) -> None:
    """Test that locks prevent concurrent access."""
    import asyncio

    async with memcached_backend(persistent=False):
        key = await memcached_backend.get_key("shared_counter")
        await memcached_backend.set(key, "0")
        lock_name = "counter_lock"

        async def increment_with_lock():
            lock = await memcached_backend.get_lock(lock_name)
            async with lock:
                # Read current value
                value = int(await memcached_backend.get(key) or "0")
                # Simulate work
                await asyncio.sleep(0.01)
                # Write new value
                await memcached_backend.set(key, str(value + 1))

        # Run 10 concurrent increments
        await asyncio.gather(*[increment_with_lock() for _ in range(10)])

        # Final value should be exactly 10 (no race conditions)
        final_value = await memcached_backend.get(key)
        assert final_value == "10"


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.memcached
async def test_error_handling(memcached_backend: MemcachedBackend) -> None:
    """Test that backend handles errors gracefully."""
    # Test operations before initialization
    key = await memcached_backend.get_key("error_key")

    # Getting before initialization should return None (graceful handling)
    value = await memcached_backend.get(key)
    assert value is None

    # Now initialize and test normal operations
    async with memcached_backend(persistent=False):
        await memcached_backend.set(key, "error_value")
        value = await memcached_backend.get(key)
        assert value == "error_value"
