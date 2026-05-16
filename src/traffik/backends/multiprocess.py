"""
Multi-process in-memory throttle backend using shared memory.

Designed for multi-worker single-machine deployments (e.g. gunicorn/uvicorn
with multiple workers) where Redis/Memcached is unavailable or undesirable,
but a single `InMemoryBackend` per process would result in each worker
maintaining independent, incorrect rate limit counters.

All keys share a single unified store backed by `multiprocessing.shared_memory`.
Each slot stores a string value (as UTF-8 bytes up to `max_value_size` bytes),
an expiry timestamp, and an occupied flag. Counter operations (`increment`,
`increment_with_ttl`) read and write numeric string values through the same
slots as `get` and `set`, so a key written by `increment_with_ttl` is always
visible to `get` and vice versa.

Slot layout (fixed size per slot, default `max_value_size=512`):

    [uint16 value_len][bytes value (max_value_size)][float64 expires_at][uint8 occupied]

The slot size is ``2 + max_value_size + 8 + 1`` bytes, padded to an 8-byte
boundary.

Deadlock prevention
-------------------
Deadlocks occur when two threads each hold one lock and wait for the other.
The rules below prevent this by enforcing a **consistent acquisition order**
across all concurrent callers. Many threads can be in-flight simultaneously —
the rules only dictate *which lock to acquire first when more than one is needed*.

The locks involved are:

- ``slot_map_lock`` — guards the key→slot-index mapping and the free-slot list.
- ``shard_locks[i]`` — guards reads and writes to slots within shard *i*.
  There are ``number_of_shards`` of these (default 64).
- ``named_locks[name]`` — per-name locks used by throttle strategies (e.g.
  leaky bucket, GCRA). Entirely independent of the above two.

Rules:

1. **Acquire ``slot_map_lock`` before any ``shard_lock``** when you need both.
   `_delete` is the one exception — it acquires ``shard_lock`` first because it
   never calls `_get_or_allocate_slot` (see its docstring). These two orderings
   cannot deadlock each other because no other path holds ``slot_map_lock`` while
   trying to acquire the same ``shard_lock`` that `_delete` holds.

2. **Never hold two ``shard_locks`` simultaneously.** `_multi_get` and `_multi_set`
   acquire them one at a time in ascending shard-index order. All callers following
   the same order means no two can be waiting on each other.

3. **Never acquire a ``named_lock`` while holding any other lock.** Named locks
   are the outermost lock in strategy code; shard and slot-map locks are internal
   to the backend. Mixing them would create a dependency cycle no ordering rule
   can resolve.
"""

import asyncio
import ctypes
import struct
import typing
from multiprocessing import Lock, Manager
from multiprocessing.managers import SyncManager
from multiprocessing.shared_memory import SharedMemory
from multiprocessing.synchronize import Lock as MPLock
from types import TracebackType

from typing_extensions import Self

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError, LockTimeoutError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)
from traffik.utils import time

__all__ = ["MultiProcessInMemoryBackend"]


class _AsyncMPLock:
    """
    Wraps a `multiprocessing.Lock` to satisfy the `AsyncLock` protocol.

    Blocking acquire is offloaded to the thread-pool executor so it never
    stalls the event loop. Release is synchronous (non-blocking by design in
    `multiprocessing.Lock`).
    """

    __slots__ = ("_lock", "_acquired")

    def __init__(self, lock: MPLock) -> None:
        self._lock = lock
        self._acquired = False

    def locked(self) -> bool:
        return self._acquired

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        loop = asyncio.get_running_loop()

        if not blocking:
            acquired = await loop.run_in_executor(
                None, lambda: self._lock.acquire(block=False)
            )
            self._acquired = bool(acquired)
            return self._acquired

        if blocking_timeout is not None:
            acquired = await loop.run_in_executor(
                None,
                lambda: self._lock.acquire(block=True, timeout=blocking_timeout),
            )
            self._acquired = bool(acquired)
            return self._acquired

        await loop.run_in_executor(None, self._lock.acquire)
        self._acquired = True
        return True

    async def release(self) -> None:
        if not self._acquired:
            raise RuntimeError(
                "Cannot release a lock that is not acquired by this instance."
            )
        self._lock.release()
        self._acquired = False

    async def __aenter__(self) -> Self:
        acquired = await self.acquire()
        if not acquired:
            raise LockTimeoutError("Could not acquire multiprocess lock.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_val: typing.Optional[BaseException],
        exc_tb: typing.Optional[TracebackType],
    ) -> None:
        await self.release()


class MultiProcessInMemoryBackend(ThrottleBackend[None, HTTPConnectionT]):
    """
    Multi-process shared-memory throttle backend.

    Safe for use across multiple OS processes on a single machine.

    Instantiate **once** before forking workers and share the instance. The
    shared memory segment and manager handles are inherited across `fork()`.
    Do **not** instantiate inside a worker after fork.
    """

    # Slot layout (computed from `max_value_size` in `__init__`):
    #   [uint16 value_len][bytes value (max_value_size)][float64 expires_at][uint8 occupied]
    # Structs that don't depend on max_value_size
    _LEN_STRUCT: typing.ClassVar[struct.Struct] = struct.Struct("=H")  # uint16
    _EXPIRY_STRUCT: typing.ClassVar[struct.Struct] = struct.Struct("=d")  # float64
    _OCCUPIED_STRUCT: typing.ClassVar[struct.Struct] = struct.Struct("=?")  # uint8 bool
    _LEN_SIZE: typing.ClassVar[int] = 2
    _EXPIRY_SIZE: typing.ClassVar[int] = 8
    _OCCUPIED_SIZE: typing.ClassVar[int] = 1

    wrap_methods: typing.Tuple[str, ...] = ("clear",)

    def __init__(
        self,
        namespace: str = "mp-inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, typing.Any]
        ] = None,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        lock_blocking: typing.Optional[bool] = None,
        lock_ttl: typing.Optional[float] = None,
        lock_blocking_timeout: typing.Optional[float] = None,
        max_keys: int = 65536,
        number_of_shards: int = 64,
        max_value_size: int = 512,
        cleanup_frequency: typing.Optional[float] = 30.0,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the multi-process in-memory throttle backend.

        :param namespace: Key prefix for all throttle keys.
        :param identifier: Connected client identifier generator.
        :param handle_throttled: Handler called when a connection is throttled.
        :param on_error: Strategy for errors during throttling operations.
            One of `"allow"`, `"throttle"`, `"raise"`, or a custom callable.
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters
                and returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.
        :param lock_blocking: Whether locks should block when acquiring.
            If None, uses the global default from `traffik.config.get_lock_blocking()`.
        :param lock_ttl: Default TTL for locks in seconds. If None, locks have
            no expiration unless specified during lock acquisition.
        :param lock_blocking_timeout: Default maximum time to wait for acquiring locks in seconds.
            If None, uses the global default from `traffik.config.get_lock_blocking_timeout()`.
        :param max_keys: Maximum number of distinct throttle keys. Each key occupies
            one slot in shared memory. Default `65536`.
        :param number_of_shards: Number of lock shards. More shards reduce contention at
            the cost of slightly more memory. Default `64`.
        :param max_value_size: Maximum byte length of a stored string value (UTF-8
            encoded). If using strategies that store large msgpack blobs (e.g. sliding window logs),
            increase this. Default `512`.
        :param cleanup_frequency: Seconds between background expired-slot cleanup
            passes. Set to `None` to rely on lazy expiry only. Default `30.0`.
        :param kwargs: Additional keyword arguments passed to the base `ThrottleBackend`.
        """
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=False,  # Can never be persistent
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            **kwargs,
        )

        if max_keys < 1:
            raise ValueError("`max_keys` must be at least 1.")
        if number_of_shards < 1:
            raise ValueError("`number_of_shards` must be at least 1.")
        if max_value_size < 8:
            raise ValueError("`max_value_size` must be at least 8 bytes.")

        self._max_keys = max_keys
        self._number_of_shards = number_of_shards
        self._max_value_size = max_value_size
        self._cleanup_frequency = cleanup_frequency

        # Slot size: len(uint16) + value(bytes) + expiry(float64) + occupied(uint8)
        # Padded to 8-byte boundary
        raw_slot_size = (
            self._LEN_SIZE + max_value_size + self._EXPIRY_SIZE + self._OCCUPIED_SIZE
        )
        self._slot_size = (raw_slot_size + 7) & ~7  # round up to 8-byte boundary

        # Byte offsets within a slot
        self._off_len = 0
        self._off_value = self._LEN_SIZE
        self._off_expiry = self._LEN_SIZE + max_value_size
        self._off_occupied = self._LEN_SIZE + max_value_size + self._EXPIRY_SIZE

        # All set during `initialize(...)`
        self._shared_memory: typing.Optional[SharedMemory] = None
        self._shared_memory_buffer: typing.Optional[memoryview] = None
        self._manager: typing.Optional[SyncManager] = None
        self._slot_map: typing.Optional[typing.Dict[str, int]] = None
        self._free_slots: typing.Optional[typing.List[int]] = None
        self._shard_locks: typing.Optional[typing.List[MPLock]] = None
        self._named_locks: typing.Optional[typing.Dict[str, MPLock]] = None
        self._slot_map_lock: typing.Optional[MPLock] = None

        self._cleanup_task: typing.Optional[asyncio.Task] = None
        self._initialized = False

    async def initialize(self) -> None:
        """
        Initialize shared memory, the manager process, and shard locks.

        Safe to call multiple times. Subsequent calls are no-ops. The first
        call takes ~100-300 ms due to manager subprocess startup.
        """
        if self._initialized:
            return

        loop = asyncio.get_running_loop()
        manager = await loop.run_in_executor(None, Manager)
        self._manager = manager

        shared_memory_size = self._max_keys * self._slot_size
        self._shared_memory = SharedMemory(create=True, size=shared_memory_size)
        self._shared_memory_buffer = memoryview(self._shared_memory.buf)  # type: ignore[arg-type]
        ctypes.memset(self._shared_memory.buf, 0, shared_memory_size)  # type: ignore[arg-type]

        def _build_shared_state() -> typing.Tuple:
            slot_map = manager.dict()  # type: ignore[attr-defined]
            free_slots = manager.list(range(self._max_keys))  # type: ignore[attr-defined]
            shard_locks = manager.list(  # type: ignore[attr-defined]
                [Lock() for _ in range(self._number_of_shards)]
            )
            named_locks = manager.dict()  # type: ignore[attr-defined]
            slot_map_lock = manager.Lock()  # type: ignore[attr-defined]
            return slot_map, free_slots, shard_locks, named_locks, slot_map_lock

        (
            self._slot_map,
            self._free_slots,
            self._shard_locks,
            self._named_locks,
            self._slot_map_lock,
        ) = await loop.run_in_executor(None, _build_shared_state)

        if self._cleanup_frequency is not None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self._initialized = True

    async def ready(self) -> bool:
        """Return `True` if the backend is initialized and shared memory is open."""
        return self._initialized and self._shared_memory is not None

    async def close(self) -> None:
        """
        Shut down the backend.

        Cancels the cleanup task, releases shared memory, and shuts down the
        manager process.
        """
        self._initialized = False

        if self._cleanup_task is not None and not self._cleanup_task.done():
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
            self._cleanup_task = None

        if self._shared_memory_buffer is not None:
            self._shared_memory_buffer.release()
            self._shared_memory_buffer = None

        if self._shared_memory is not None:
            self._shared_memory.close()
            self._shared_memory.unlink()
            self._shared_memory = None

        if self._manager is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._manager.shutdown
            )
            self._manager = None

    async def reset(self) -> None:
        """Reset the backend by clearing all throttle data."""
        if not self._initialized:
            return
        await self.clear()

    def _assert_ready(self) -> None:
        """
        Raise `BackendConnectionError` if the backend has not been initialized.
        """
        if not self._initialized or self._shared_memory_buffer is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def _slot_offset(self, slot_idx: int) -> int:
        """
        Return the byte offset of *slot_idx* in the shared memory buffer.

        :param slot_idx: Zero-based slot index.
        :return: Byte offset.
        """
        return slot_idx * self._slot_size

    def _read_slot(
        self, buffer: memoryview, slot_idx: int
    ) -> typing.Tuple[typing.Optional[str], float, bool]:
        """
        Read a slot from shared memory.

        :param buffer: Shared memory memoryview.
        :param slot_idx: Zero-based slot index.
        :return: Tuple of `(value, expires_at, occupied)`. Value is `None` if
            the slot is not occupied.
        """
        off = self._slot_offset(slot_idx)
        occupied: bool = self._OCCUPIED_STRUCT.unpack_from(
            buffer, off + self._off_occupied
        )[0]
        if not occupied:
            return None, 0.0, False

        length: int = self._LEN_STRUCT.unpack_from(buffer, off + self._off_len)[0]
        value = bytes(
            buffer[off + self._off_value : off + self._off_value + length]
        ).decode("utf-8")
        expires_at: float = self._EXPIRY_STRUCT.unpack_from(
            buffer, off + self._off_expiry
        )[0]
        return value, expires_at, True

    def _write_slot(
        self,
        buffer: memoryview,
        slot_idx: int,
        value: str,
        expires_at: float,
        occupied: bool,
    ) -> None:
        """
        Write a complete slot to shared memory.

        :param buffer: Shared memory memoryview.
        :param slot_idx: Zero-based slot index.
        :param value: String value to store.
        :param expires_at: Unix timestamp after which the slot expires.
            Pass `0.0` for no expiry.
        :param occupied: `True` if the slot is in use.
        :raises BackendError: If the encoded value exceeds `max_value_size`.
        """
        off = self._slot_offset(slot_idx)
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_size:
            raise BackendError(
                f"Value size {len(encoded)} bytes exceeds `max_value_size` "
                f"({self._max_value_size}). Increase `max_value_size`."
            )

        self._OCCUPIED_STRUCT.pack_into(buffer, off + self._off_occupied, occupied)
        if occupied:
            self._LEN_STRUCT.pack_into(buffer, off + self._off_len, len(encoded))
            buffer[off + self._off_value : off + self._off_value + len(encoded)] = (
                encoded
            )
            self._EXPIRY_STRUCT.pack_into(buffer, off + self._off_expiry, expires_at)

    def _write_value_only(self, buffer: memoryview, slot_idx: int, value: str) -> None:
        """
        Overwrite only the value in a slot, leaving expiry and occupied flag untouched.

        :param buffer: Shared memory memoryview.
        :param slot_idx: Zero-based slot index.
        :param value: New string value.
        :raises BackendError: If the encoded value exceeds `max_value_size`.
        """
        off = self._slot_offset(slot_idx)
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_size:
            raise BackendError(
                f"Value size {len(encoded)} bytes exceeds `max_value_size` "
                f"({self._max_value_size}). Increase `max_value_size`."
            )

        self._LEN_STRUCT.pack_into(buffer, off + self._off_len, len(encoded))
        buffer[off + self._off_value : off + self._off_value + len(encoded)] = encoded

    def _shard_idx_for_key(self, key: str) -> int:
        """
        Return the shard index for *key*.

        :param key: The throttle key.
        :return: Shard index in the range `[0, number_of_shards)`.
        """
        return hash(key) % self._number_of_shards

    def _get_or_allocate_slot(self, key: str) -> int:
        """
        Return the existing slot index for *key*, or allocate a new one.

        Must be called with `_slot_map_lock` held.

        :param key: The throttle key.
        :return: Slot index.
        :raises BackendError: If the key space is exhausted.
        """
        existing = self._slot_map.get(key, None)  # type: ignore[union-attr]
        if existing is not None:
            return existing

        if len(self._free_slots) == 0:  # type: ignore[arg-type]
            raise BackendError(
                f"`max_keys` ({self._max_keys}) reached. "
                "Increase `max_keys` or reduce the number of distinct throttle keys."
            )

        slot_idx: int = self._free_slots[-1]  # type: ignore[index]
        del self._free_slots[-1]  # type: ignore[union-attr]
        self._slot_map[key] = slot_idx  # type: ignore[index]
        return slot_idx

    def _release_slot(self, key: str) -> None:
        """
        Remove *key* from the slot map and return its slot to the free pool.

        Must be called with `_slot_map_lock` held.

        :param key: The throttle key to release.
        """
        slot_idx = self._slot_map.pop(key, None)  # type: ignore[union-attr]
        if slot_idx is not None and self._shared_memory_buffer is not None:
            off = self._slot_offset(slot_idx)
            self._OCCUPIED_STRUCT.pack_into(
                self._shared_memory_buffer, off + self._off_occupied, False
            )
            self._free_slots.append(slot_idx)  # type: ignore[union-attr]

    def _get(
        self, key: str, buffer: memoryview, slot_map: typing.Any, shard_lock: MPLock
    ) -> typing.Optional[str]:
        """
        Synchronous implementation of `get`.

        :param key: The throttle key to retrieve.
        :param buffer: Shared memory buffer.
        :param slot_map: Manager dict mapping keys to slot indices.
        :param shard_lock: Shard lock for this key.
        :return: String value, or `None` if absent or expired.
        """
        now = time()
        with shard_lock:
            slot_idx = slot_map.get(key, None)
            if slot_idx is None:
                return None
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if not occupied:
                return None
            if expires_at != 0.0 and expires_at < now:
                return None
            return value

    def _set(
        self,
        key: str,
        value: str,
        expires_at: float,
        buffer: memoryview,
        slot_map_lock: MPLock,
        shard_lock: MPLock,
    ) -> None:
        """
        Synchronous implementation of `set`.

        Acquires `slot_map_lock` first to allocate the slot, then
        `shard_lock` to write the value (normal ordering).

        :param key: The throttle key.
        :param value: String value to store.
        :param expires_at: Unix timestamp for expiry, or `0.0` for none.
        :param buffer: Shared memory buffer.
        :param slot_map_lock: Lock guarding the slot map.
        :param shard_lock: Shard lock for this key.
        """
        with slot_map_lock:
            slot_idx = self._get_or_allocate_slot(key)
        with shard_lock:
            self._write_slot(buffer, slot_idx, value, expires_at, True)

    def _delete(self, key: str, slot_map_lock: MPLock, shard_lock: MPLock) -> bool:
        """
        Synchronous implementation of `delete`.

        Acquires `shard_lock` **before** `slot_map_lock` — the single
        documented exception to the normal ordering rule. This is safe here
        because `_delete` never calls `_get_or_allocate_slot`, which is
        the only path that needs both locks in the normal order simultaneously.

        :param key: The throttle key to delete.
        :param slot_map_lock: Lock guarding the slot map.
        :param shard_lock: Shard lock for this key.
        :return: `True` if the key existed and was deleted.
        """
        with shard_lock:
            with slot_map_lock:
                if self._slot_map.get(key, None) is None:  # type: ignore[union-attr]
                    return False
                self._release_slot(key)
                return True

    def _increment(
        self,
        key: str,
        amount: int,
        buffer: memoryview,
        slot_map_lock: MPLock,
        shard_lock: MPLock,
    ) -> int:
        """
        Synchronous implementation of `increment`.

        Reads the existing string value as an integer, increments it, and
        writes the new value back as a string. A missing or expired key is
        treated as zero before incrementing.

        :param key: The throttle key.
        :param amount: Amount to increment (negative for decrement).
        :param buffer: Shared memory buffer.
        :param slot_map_lock: Lock guarding the slot map.
        :param shard_lock: Shard lock for this key.
        :return: New counter value.
        """
        now = time()
        with slot_map_lock:
            slot_idx = self._get_or_allocate_slot(key)

        with shard_lock:
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if not occupied or (expires_at != 0.0 and expires_at < now):
                new_value = amount
                self._write_slot(buffer, slot_idx, str(new_value), 0.0, True)
            else:
                try:
                    current = int(value)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    current = 0
                new_value = current + amount
                self._write_value_only(buffer, slot_idx, str(new_value))
        return new_value

    def _expire(
        self,
        key: str,
        seconds: int,
        buffer: memoryview,
        slot_map: typing.Any,
        shard_lock: MPLock,
    ) -> bool:
        """
        Synchronous implementation of `expire`.

        :param key: The throttle key.
        :param seconds: TTL in seconds from now.
        :param buffer: Shared memory buffer.
        :param slot_map: Manager dict mapping keys to slot indices.
        :param shard_lock: Shard lock for this key.
        :return: `True` if expiry was set, `False` if the key was absent.
        """
        now = time()
        with shard_lock:
            slot_idx = slot_map.get(key, None)
            if slot_idx is None:
                return False

            __init__, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if not occupied or (expires_at != 0.0 and expires_at < now):
                return False
            self._EXPIRY_STRUCT.pack_into(
                buffer, self._slot_offset(slot_idx) + self._off_expiry, now + seconds
            )
            return True

    def _increment_with_ttl(
        self,
        key: str,
        amount: int,
        ttl: int,
        buffer: memoryview,
        slot_map_lock: MPLock,
        shard_lock: MPLock,
    ) -> int:
        """
        Synchronous hot-path implementation of `increment_with_ttl`.

        Reads the existing numeric string value, increments it, writes it back.
        TTL is set only on first write. Acquires `slot_map_lock` and `shard_lock`
        sequentially — never simultaneously — for minimal contention.

        :param key: The throttle key.
        :param amount: Amount to increment.
        :param ttl: TTL in seconds, applied only on first write.
        :param buffer: Shared memory buffer.
        :param slot_map_lock: Lock guarding the slot map.
        :param shard_lock: Shard lock for this key.
        :return: New counter value.
        """
        now = time()
        with slot_map_lock:
            slot_idx = self._get_or_allocate_slot(key)

        with shard_lock:
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            is_new = not occupied or (expires_at != 0.0 and expires_at < now)
            if is_new:
                new_value = amount
                new_expiry = now + ttl
                self._write_slot(buffer, slot_idx, str(new_value), new_expiry, True)
            else:
                try:
                    current = int(value)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    current = 0
                new_value = current + amount
                new_expiry = expires_at if expires_at != 0.0 else now + ttl
                self._write_slot(buffer, slot_idx, str(new_value), new_expiry, True)
        return new_value

    def _multi_get(
        self,
        keys: typing.Tuple[str, ...],
        shard_to_keys: typing.Dict[int, typing.List[str]],
        buffer: memoryview,
        slot_map: typing.Any,
    ) -> typing.Dict[str, typing.Optional[str]]:
        """
        Synchronous implementation of `multi_get`.

        Acquires shard locks in ascending shard-index order to prevent
        deadlocks when keys span multiple shards.

        :param keys: All requested keys (used to verify ordering).
        :param shard_to_keys: Pre-computed mapping of shard index → key list.
        :param buffer: Shared memory buffer.
        :param slot_map: Manager dict mapping keys to slot indices.
        :return: Dict mapping each key to its string value or `None`.
        """
        now = time()
        results: typing.Dict[str, typing.Optional[str]] = {}
        for shard_idx in sorted(shard_to_keys):
            shard_lock = self._shard_locks[shard_idx]  # type: ignore[index]
            with shard_lock:
                for key in shard_to_keys[shard_idx]:
                    slot_idx = slot_map.get(key, None)
                    if slot_idx is None:
                        results[key] = None
                        continue
                    value, expires_at, occupied = self._read_slot(buffer, slot_idx)
                    if not occupied or (expires_at != 0.0 and expires_at < now):
                        results[key] = None
                    else:
                        results[key] = value
        return results

    def _multi_set(
        self,
        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]],
        parsed: typing.Mapping[str, str],
        expires_at: float,
        buffer: memoryview,
        slot_map_lock: MPLock,
    ) -> None:
        """
        Synchronous implementation of `multi_set`.

        Allocates all slots under a single `slot_map_lock` acquisition, then
        writes to shards in ascending shard-index order.

        :param shard_to_items: Mapping of shard index → list of `(key, value)`.
        :param parsed: All `key → value` pairs (used for slot allocation).
        :param expires_at: Unix expiry timestamp, or `0.0` for none.
        :param buffer: Shared memory buffer.
        :param slot_map_lock: Lock guarding the slot map.
        """
        slot_assignments: typing.Dict[str, int] = {}
        with slot_map_lock:
            for key in parsed:
                slot_assignments[key] = self._get_or_allocate_slot(key)

        for shard_idx in sorted(shard_to_items):
            shard_lock = self._shard_locks[shard_idx]  # type: ignore[index]
            with shard_lock:
                for key, val in shard_to_items[shard_idx]:
                    self._write_slot(
                        buffer, slot_assignments[key], val, expires_at, True
                    )

    def _clear(self, slot_map_lock: MPLock) -> None:
        """
        Synchronous implementation of `clear`.

        Removes all keys whose names begin with this backend's namespace prefix.

        :param slot_map_lock: Lock guarding the slot map.
        """
        prefix = f"{self.namespace}:"
        with slot_map_lock:
            for key in [k for k in self._slot_map.keys() if k.startswith(prefix)]:  # type: ignore[union-attr]
                self._release_slot(key)

    def _cleanup(self) -> int:
        """
        Synchronous expired-slot reclamation.

        Scans all occupied slots and releases those whose expiry has passed.
        Called periodically by the background cleanup task.

        :return: Number of slots reclaimed.
        """
        buffer = self._shared_memory_buffer
        if buffer is None:
            return 0

        now = time()
        freed = 0
        with self._slot_map_lock:  # type: ignore[union-attr]
            expired = [
                key
                for key, slot_idx in list(self._slot_map.items())  # type: ignore[union-attr]
                if (lambda _, e, o: o and e != 0.0 and e < now)(  # type: ignore[misc]
                    *self._read_slot(buffer, slot_idx)
                )
            ]
            for key in expired:
                self._release_slot(key)
                freed += 1
        return freed

    def _get_or_create_named_lock(
        self,
        name: str,
        slot_map_lock: MPLock,
    ) -> MPLock:
        """
        Return an existing named lock or create and register a new one.

        Uses `slot_map_lock` to serialise creation so that two concurrent
        callers cannot register duplicate `MPLock` objects for the same name.

        :param name: Lock name.
        :param slot_map_lock: Lock used to serialise named-lock creation.
        :return: The `multiprocessing.Lock` registered under *name*.
        """
        existing = self._named_locks.get(name, None)  # type: ignore[union-attr]
        if existing is not None:
            return existing

        with slot_map_lock:
            existing = self._named_locks.get(name, None)  # type: ignore[union-attr]
            if existing is not None:
                return existing
            new_lock: MPLock = Lock()
            self._named_locks[name] = new_lock  # type: ignore[index]
            return new_lock

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """
        Return the string value stored for *key*, or `None` if absent or expired.

        :param key: The throttle key to retrieve.
        :return: String value, or `None`.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._get,
            key,
            self._shared_memory_buffer,
            self._slot_map,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """
        Store a string value for *key* with optional expiry.

        :param key: The throttle key to set.
        :param value: String value to store.
        :param expire: TTL in seconds, or `None` for no expiry.
        :raises BackendError: If the encoded value exceeds `max_value_size`.
        """
        self._assert_ready()
        expires_at = (time() + expire) if expire is not None else 0.0
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._set,
            key,
            value,
            expires_at,
            self._shared_memory_buffer,
            self._slot_map_lock,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """
        Delete *key* if it exists.

        :param key: The throttle key to delete.
        :return: `True` if the key existed and was deleted, `False` otherwise.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._delete,
            key,
            self._slot_map_lock,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment the counter for *key* and return the new value.

        Reads the current string value as an integer, increments it, and writes
        the result back as a string. Creates the key with value *amount* if it
        does not exist or has expired. The updated value is immediately visible
        to `get`.

        :param key: The throttle key.
        :param amount: Amount to increment by. Pass a negative value to decrement.
        :return: New counter value.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._increment,
            key,
            amount,
            self._shared_memory_buffer,
            self._slot_map_lock,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiry on an existing key without modifying its value.

        :param key: The throttle key.
        :param seconds: TTL in seconds from now.
        :return: `True` if expiry was set, `False` if the key did not exist.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._expire,
            key,
            seconds,
            self._shared_memory_buffer,
            self._slot_map,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment the counter for *key* and set its TTL on first write.

        This is the hot path for all fixed-window and sliding-window strategies.
        TTL is only applied when the key is first created or has expired —
        existing keys preserve their original expiry. The updated value is
        immediately visible to `get`.

        :param key: The throttle key.
        :param amount: Amount to increment by.
        :param ttl: TTL in seconds, applied only on first write.
        :return: New counter value.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._increment_with_ttl,
            key,
            amount,
            ttl,
            self._shared_memory_buffer,
            self._slot_map_lock,
            self._shard_locks[self._shard_idx_for_key(key)],  # type: ignore[index]
        )

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Retrieve multiple keys in a single executor call.

        Shard locks are acquired in ascending shard-index order to prevent
        deadlocks when keys span multiple shards.

        :param keys: Keys to retrieve.
        :return: List of values in the same order as *keys*.
            Missing or expired keys produce `None`.
        """
        self._assert_ready()
        if not keys:
            return []
        shard_to_keys: typing.Dict[int, typing.List[str]] = {}
        for key in keys:
            shard_to_keys.setdefault(self._shard_idx_for_key(key), []).append(key)

        result_map = await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._multi_get,
            keys,
            shard_to_keys,
            self._shared_memory_buffer,
            self._slot_map,
        )
        return [result_map[k] for k in keys]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Set multiple string values in a single executor call.

        All slots are allocated under one `slot_map_lock` acquisition, then
        values are written to shards in ascending shard-index order.

        :param items: Mapping of key → string value.
        :param expire: TTL in seconds for all keys, or `None` for no expiry.
        :raises BackendError: If any value exceeds `max_value_size`.
        """
        self._assert_ready()
        if not items:
            return

        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]] = {}
        for key, val in items.items():
            shard_to_items.setdefault(self._shard_idx_for_key(key), []).append(
                (key, val)
            )

        expires_at = (time() + expire) if expire is not None else 0.0
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._multi_set,
            shard_to_items,
            items,
            expires_at,
            self._shared_memory_buffer,
            self._slot_map_lock,
        )

    async def get_lock(self, name: str) -> _AsyncMPLock:
        """
        Return an async-compatible named lock for use by throttle strategies.

        The underlying `multiprocessing.Lock` is created on first use and
        reused on all subsequent calls with the same *name*. Named locks are
        independent of all internal backend locks — never acquire one while
        holding a shard or slot-map lock.

        :param name: Unique lock name.
        :return: `_AsyncMPLock` wrapping a `multiprocessing.Lock`.
        """
        self._assert_ready()
        mp_lock = await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._get_or_create_named_lock,
            name,
            self._slot_map_lock,
        )
        return _AsyncMPLock(lock=mp_lock)

    async def clear(self) -> None:
        """
        Remove all keys belonging to this backend's namespace.

        Keys from other namespaces sharing the same backend instance are left
        untouched.
        """
        self._assert_ready()
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._clear,
            self._slot_map_lock,
        )

    async def _cleanup_loop(self) -> None:
        """Periodically reclaim expired slots. Runs as a background `asyncio.Task`."""
        assert self._cleanup_frequency is not None
        while self._initialized:
            try:
                await asyncio.sleep(self._cleanup_frequency)
                await asyncio.get_running_loop().run_in_executor(None, self._cleanup)
            except asyncio.CancelledError:
                break
            except Exception:
                # Never crash the cleanup loop. Keep the backend alive.
                pass
