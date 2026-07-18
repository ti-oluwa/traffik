"""
Concurrency Tests for Backend Operations
"""

import asyncio

import pytest

from tests.conftest import BackendGen


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.concurrent
class TestBackendConcurrency:
    async def test_increment_high_concurrency(self, backends: BackendGen) -> None:
        """Test increment with 100 concurrent async tasks."""
        for backend in backends(namespace="concurrent_increment_100"):
            async with backend(close_on_exit=True):
                key = backend.get_key("counter")

                # 100 concurrent increments
                results = await asyncio.gather(
                    *[backend.increment(key) for _ in range(100)]
                )

                # All values should be unique (atomicity guarantee)
                assert len(set(results)) == 100, (
                    f"Duplicate values found: {sorted(results)}"
                )
                assert min(results) == 1
                assert max(results) == 100

                # Final value should be 100
                final = await backend.get(key)
                assert final == "100"

    async def test_decrement_high_concurrency(self, backends: BackendGen) -> None:
        """Test decrement with 100 concurrent async tasks."""
        for backend in backends(namespace="concurrent_decrement_100"):
            async with backend(close_on_exit=True):
                key = backend.get_key("counter")

                # Set initial value to 100
                await backend.set(key, "100")

                # 100 concurrent decrements
                results = await asyncio.gather(
                    *[backend.decrement(key) for _ in range(100)]
                )

                # All values should be unique
                assert len(set(results)) == 100, (
                    f"Duplicate values found: {sorted(results)}"
                )
                assert min(results) == 0
                assert max(results) == 99

                # Final value should be 0
                final = await backend.get(key)
                assert final is not None
                assert int(final) == 0

    async def test_increment_with_ttl_concurrent(self, backends: BackendGen) -> None:
        """Test increment_with_ttl with 50 concurrent async tasks."""
        for backend in backends(namespace="concurrent_increment_ttl"):
            async with backend(close_on_exit=True):
                key = backend.get_key("ttl_counter")

                # 50 concurrent increments with TTL
                results = await asyncio.gather(
                    *[
                        backend.increment_with_ttl(key, amount=1, ttl=60)
                        for _ in range(50)
                    ]
                )

                # All values should be unique
                assert len(set(results)) == 50, (
                    f"Duplicate values found: {sorted(results)}"
                )
                assert set(results) == set(range(1, 51))

                # Final value should be 50
                final = await backend.get(key)
                assert final == "50"

    async def test_mixed_operations_concurrent(self, backends: BackendGen) -> None:
        """Test mixed increment/decrement operations concurrently."""
        for backend in backends(namespace="concurrent_mixed_ops"):
            async with backend(close_on_exit=True):
                key = backend.get_key("mixed_counter")
                # Start at 100
                await backend.set(key, "100")

                # 50 increments and 30 decrements concurrently
                increment_tasks = [backend.increment(key) for _ in range(50)]
                decrement_tasks = [backend.decrement(key) for _ in range(30)]
                all_tasks = increment_tasks + decrement_tasks

                results = await asyncio.gather(*all_tasks)
                assert len(results) == 80, "Race condition detected!"

                # Final value should be 120 (100 + 50 - 30)
                final = int(await backend.get(key) or "0")
                assert final == 120

    async def test_stress_concurrent_operations(self, backends: BackendGen) -> None:
        """Stress test with 300 concurrent operations."""
        for backend in backends(namespace="stress_concurrent"):
            async with backend(close_on_exit=True):
                key = backend.get_key("stress_counter")

                # 300 concurrent increments
                results = await asyncio.gather(
                    *[backend.increment(key) for _ in range(300)]
                )

                # Verify atomicity
                assert len(set(results)) == 300, "Race condition under stress!"
                assert min(results) == 1
                assert max(results) == 300

                final = await backend.get(key)
                assert final == "300"
