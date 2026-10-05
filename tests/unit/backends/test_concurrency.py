"""Concurrency Tests for Backend Operations"""

import asyncio
import random

import pytest

from tests.conftest import BackendGen


@pytest.mark.anyio
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


@pytest.mark.anyio
@pytest.mark.backend
@pytest.mark.concurrent
class TestBackendCancellation:
    """
    Cancellation (a client disconnect, a request timeout, a server shutdown) is
    ordinary under an HTTP server, and lands wherever a task happens to be,
    including between a request being sent and its reply being read. It must
    never leave a backend's connection able to hand a later caller someone
    else's reply.

    Before the fix under test, aiomcache returned such a connection to its pool
    with the reply unread, and almost every later read returned a wrong answer.
    """

    async def test_cancelled_reads_do_not_corrupt_later_replies(
        self, backends: BackendGen
    ) -> None:
        keys = 20
        for backend in backends(namespace="cancelled_reads"):
            async with backend(close_on_exit=True):
                for i in range(keys):
                    await backend.set(backend.get_key(f"k{i}"), f"v{i}")

                wrong = []

                async def read(i: int) -> None:
                    value = await backend.get(backend.get_key(f"k{i}"))
                    if value != f"v{i}":
                        wrong.append((i, value))

                rng = random.Random(0)
                for _ in range(6):
                    tasks = [
                        asyncio.create_task(read(rng.randrange(keys)))
                        for _ in range(100)
                    ]
                    await asyncio.sleep(rng.random() * 0.004)
                    for task in rng.sample(tasks, 70):
                        task.cancel()
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=15
                    )

                # Then, once nothing is in flight, every key must still read back
                # its own value.
                for i in range(keys):
                    value = await backend.get(backend.get_key(f"k{i}"))
                    if value != f"v{i}":
                        wrong.append((i, value))

                assert not wrong, (
                    f"{type(backend).__name__} returned wrong replies after "
                    f"cancellation: {wrong[:3]}"
                )

    async def test_lock_survives_cancelled_waiters_and_holders(
        self, backends: BackendGen
    ) -> None:
        for backend in backends(namespace="cancelled_lock"):
            async with backend(close_on_exit=True):
                inside = 0

                async def critical_section() -> None:
                    nonlocal inside
                    async with backend.lock("L", ttl=5.0, blocking_timeout=10.0):
                        inside += 1
                        try:
                            assert inside == 1, "mutual exclusion violated"
                            await asyncio.sleep(0.01)
                        finally:
                            inside -= 1

                rng = random.Random(0)
                tasks = [asyncio.create_task(critical_section()) for _ in range(40)]
                await asyncio.sleep(0.03)
                for task in rng.sample(tasks, 30):
                    task.cancel()
                    await asyncio.sleep(rng.random() * 0.004)

                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=30
                )
                failures = [
                    result
                    for result in results
                    if isinstance(result, Exception)
                    and not isinstance(result, asyncio.CancelledError)
                ]
                assert not failures, (
                    f"{type(backend).__name__}: unexpected errors {failures[:3]}"
                )

                # Whoever was cancelled, the lock must not be left held.
                async with backend.lock("L", ttl=5.0, blocking_timeout=3.0):
                    pass
