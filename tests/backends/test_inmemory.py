import pytest

from traffik.backends.inmemory import InMemoryBackend


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_persistence(inmemory_backend: InMemoryBackend) -> None:
    """Test that the backend persists data when set to persistent."""
    # Add some keys to the backend within the context
    async with inmemory_backend(persistent=True):
        key = await inmemory_backend.get_key("key1")
        await inmemory_backend.set(key, "value1")

    # Test that the backend persists the keys even after exiting the context
    assert inmemory_backend.connection is not None
    key = await inmemory_backend.get_key("key1")
    value = await inmemory_backend.get(key)
    assert value == "value1"

    # Test that the backend can be reset
    await inmemory_backend.reset()
    assert inmemory_backend.connection is None


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.native
async def test_reset_with_data(inmemory_backend: InMemoryBackend) -> None:
    """Test backend reset properly clears data."""
    key = await inmemory_backend.get_key("test_key")
    async with inmemory_backend(persistent=False):
        await inmemory_backend.set(key, "test_value")
        # Verify data exists
        value = await inmemory_backend.get(key)
        assert value == "test_value"

    assert inmemory_backend.connection is None
