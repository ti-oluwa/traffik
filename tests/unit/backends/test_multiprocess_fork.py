"""
Tests for `MultiProcessInMemoryBackend`'s fork behavior.

The shared-memory mapping and semaphores are inherited correctly by `fork()`,
but thread- and event-loop-bound state (the executor, the cleanup task, the
cached pid) is not, so `_reinitialize_after_fork` rebuilds those in the child.
"""

import asyncio
import multiprocessing
import platform
import typing
from unittest.mock import patch

import pytest

from traffik.backends.multiprocess import MultiProcessInMemoryBackend

SUPPORTS_FORK = (
    platform.system() != "Windows" and "fork" in multiprocessing.get_all_start_methods()
)


class TestStartMethodGuard:
    def test_rejects_spawn_start_method(self) -> None:
        with patch(
            "traffik.backends.multiprocess.multiprocessing.get_start_method",
            return_value="spawn",
        ):
            with pytest.raises(RuntimeError, match="'fork' or 'forkserver'"):
                MultiProcessInMemoryBackend(namespace="fork_guard_spawn")

    @pytest.mark.parametrize("method", ["fork", "forkserver"])
    def test_accepts_fork_and_forkserver(self, method: str) -> None:
        with patch(
            "traffik.backends.multiprocess.multiprocessing.get_start_method",
            return_value=method,
        ):
            MultiProcessInMemoryBackend(namespace=f"fork_guard_{method}")

    def test_rejects_windows(self) -> None:
        with patch("traffik.backends.multiprocess.ON_WINDOWS", True):
            with pytest.raises(RuntimeError, match="not supported on Windows"):
                MultiProcessInMemoryBackend(namespace="fork_guard_windows")


def child_reads_and_writes(
    backend: "MultiProcessInMemoryBackend", results: dict[str, typing.Any]
) -> None:
    """
    Runs its own event loop, since the parent's does not
    survive a fork, and exercises the rebuilt executor end to end.
    """

    async def run() -> None:
        async with backend(persistent=True, close_on_exit=False):
            key = backend.get_key("from_parent")
            results["saw_parent_value"] = await backend.get(key)
            await backend.set(backend.get_key("from_child"), "child-value")
            results["child_pid_cached"] = backend._pid

    asyncio.run(run())


@pytest.mark.anyio
@pytest.mark.backend
@pytest.mark.skipif(not SUPPORTS_FORK, reason="requires the 'fork' start method")
class TestForkInheritance:
    async def test_data_is_shared_in_both_directions(self) -> None:
        backend = MultiProcessInMemoryBackend(
            namespace="fork_inheritance", number_of_shards=2, semaphore_timeout=2.0
        )
        backend.start()
        try:
            async with backend(persistent=True, close_on_exit=False):
                await backend.set(backend.get_key("from_parent"), "parent-value")

                with multiprocessing.Manager() as manager:
                    results = manager.dict()
                    proc = multiprocessing.Process(
                        target=child_reads_and_writes, args=(backend, results)
                    )
                    proc.start()
                    proc.join(timeout=10)
                    assert proc.exitcode == 0
                    results = dict(results)

                # Parent -> child: written before the fork, visible after it.
                assert results["saw_parent_value"] == "parent-value"
                # Child -> parent: written after the fork, visible here.
                assert await backend.get(backend.get_key("from_child")) == "child-value"
                # The child refreshed its cached pid for owner records.
                assert results["child_pid_cached"] != backend._pid
        finally:
            await backend.close()
