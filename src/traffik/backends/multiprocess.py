"""
Multi-process in-memory throttle backend using shared memory.

Designed for multi-worker single-machine deployments (e.g. gunicorn/uvicorn
with multiple workers) where Redis/Memcached is unavailable or undesirable,
but a single `InMemoryBackend` per process would result in each worker
maintaining independent, incorrect rate limit counters.

All state lives in a single `multiprocessing.shared_memory` segment.
Locking uses `multiprocessing.Semaphore` objects created before fork and
inherited by all workers.
"""

import asyncio
import multiprocessing
import struct
import typing
from multiprocessing.shared_memory import SharedMemory
from multiprocessing.synchronize import Semaphore
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


# Hash table
_HASH_TABLE_LOAD_FACTOR: float = 0.65
_HASH_TABLE_EMPTY_STATE: int = 0
_HASH_TABLE_OCCUPIED_STATE: int = 1
_HASH_TABLE_TOMBSTONE_STATE: int = 2
_KEY_MAX_BYTES: int = 200  # max UTF-8 byte length of a key

# Hash table entry layout: state(1) + key_length(1) + key(200) + slot_idx(4) = 206
_HASH_TABLE_STATE_SIZE: int = 1
_HASH_TABLE_KEY_VALUE_LENGTH_SIZE: int = 1
_HASH_TABLE_SLOT_IDX_SIZE: int = 4
_HASH_TABLE_ENTRY_SIZE: int = (
    _HASH_TABLE_STATE_SIZE
    + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE
    + _KEY_MAX_BYTES
    + _HASH_TABLE_SLOT_IDX_SIZE
)

# Within a hash table entry:
_HASH_TABLE_STATE_OFFSET: int = 0
_HASH_TABLE_KEY_LENGTH_OFFSET: int = _HASH_TABLE_STATE_SIZE
_HASH_TABLE_KEY_OFFSET: int = _HASH_TABLE_STATE_SIZE + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE
_HASH_TABLE_SLOT_IDX_OFFSET: int = (
    _HASH_TABLE_STATE_SIZE + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE + _KEY_MAX_BYTES
)

# Header region layout: free_count(4) + free_stack(4 * max_keys)
_HEADER_FREE_COUNT_SIZE: int = 4
_HEADER_FREE_COUNT_OFFSET: int = 0


# Structs (pre-compiled, class-level)
_UINT8_STRUCT = struct.Struct("=B")  # 1 byte unsigned
_UINT16_STRUCT = struct.Struct("=H")  # 2 bytes unsigned
_UINT32_STRUCT = struct.Struct("=I")  # 4 bytes unsigned
_FLOAT64_STRUCT = struct.Struct("=d")  # 8 bytes double
_BOOL_STRUCT = struct.Struct("=?")  # 1 byte bool


def _fnv1a_32(data: bytes) -> int:
    """
    FNV-1a 32-bit hash. Deterministic across processes and platforms.

    We use this because Python's built-in `hash()` is randomized per-process since python3.3
    and is therefore unusable for cross-process hash table probing.
    """
    h = 0x811C9DC5
    for byte in data:
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class _AsyncMPSemaphoreLock:
    """
    `AsyncLock`-compatible wrapper around a `multiprocessing.Semaphore(1)`.

    Blocking acquire is dispatched to the thread-pool executor so the
    event loop is never stalled. The semaphore itself is a POSIX futex
    (Linux) or equivalent, hence acquire/release cost ~1-5µs with no IPC.

    One instance wraps one underlying semaphore. Instances are not
    reentrant; strategy code must not acquire the same named lock twice
    on the same task.
    """

    __slots__ = ("_semaphore", "_acquired")

    def __init__(self, semaphore: Semaphore) -> None:  # type: ignore[type-arg]
        self._semaphore = semaphore
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
            # Non-blocking: timeout=0 on Semaphore.acquire returns False immediately
            acquired = await loop.run_in_executor(
                None, lambda: self._semaphore.acquire(block=False)
            )
            self._acquired = bool(acquired)
            return self._acquired

        if blocking_timeout is not None:
            acquired = await loop.run_in_executor(
                None,
                lambda: self._semaphore.acquire(block=True, timeout=blocking_timeout),
            )
            self._acquired = bool(acquired)
            return self._acquired

        # Blocking with no timeout
        await loop.run_in_executor(None, self._semaphore.acquire)
        self._acquired = True
        return True

    async def release(self) -> None:
        if not self._acquired:
            raise RuntimeError(
                "Cannot release a semaphore that is not acquired by this instance."
            )
        self._semaphore.release()
        self._acquired = False

    async def __aenter__(self) -> Self:
        acquired = await self.acquire()
        if not acquired:
            raise LockTimeoutError("Could not acquire multiprocess semaphore.")
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

    All state (key -> slot index mapping, slot data, free-slot stack) lives
    in a single `multiprocessing.shared_memory` segment.

    Locking uses `multiprocessing.Semaphore` objects that must be created **before** fork
    and inherited by all worker processes through the normal Unix fork mechanism.

    **Usage contract**

    Create one instance in your application factory *before* the server
    forks workers, then pass it (or let it be inherited) into each worker.
    Do **not** call `initialize()` inside a worker after fork.
    It should be called once in the parent process.

    ```
    # pre-fork (application factory / lifespan startup)
    backend = MultiProcessInMemoryBackend(namespace="myapp")
    await backend.initialize()   # creates shared memory + semaphores

    # gunicorn/uvicorn then forks; each worker inherits the backend
    # and can use it immediately with no further initialization.
    ```

    **Shared memory layout**

    ```
    [header]
        uint32 free_count
        uint32 free_stack[max_keys]

    [hash table]
        HT_BUCKETS x HT_ENTRY_SIZE bytes
        entry: uint8 state | uint8 key_length | bytes key[200] | uint32 slot_idx

    [slot data]
        max_keys x slot_size bytes
        slot: uint16 value_length | bytes value[max_value_size] |
            float64 expires_at | uint8 occupied
        (padded to 8-byte boundary)
    ```

    **Lock ordering** (must always be respected to avoid deadlock)

    - `slot_map_semaphore` before any `shard_semaphores[i]`
    - Never hold two `shard_semaphores` simultaneously
    - Named semaphores always outermost — never acquired while holding
       slot_map_semaphore or shard_semaphores
    """

    # Pre-compiled structs shared across all instances
    _VALUE_LENGTH_STRUCT: typing.ClassVar[struct.Struct] = _UINT16_STRUCT
    _EXPIRY_STRUCT: typing.ClassVar[struct.Struct] = _FLOAT64_STRUCT
    _OCCUPIED_FLAG_STRUCT: typing.ClassVar[struct.Struct] = _BOOL_STRUCT
    _VALUE_LENGTH_SIZE: typing.ClassVar[int] = 2
    _EXPIRY_SIZE: typing.ClassVar[int] = 8
    _OCCUPIED_FLAG_SIZE: typing.ClassVar[int] = 1

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
        cleanup_frequency: typing.Optional[float] = None,
        **kwargs: typing.Any,
    ) -> None:
        kwargs.pop("persistent", None)
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=False,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            **kwargs,
        )

        if max_keys < 1:
            raise ValueError("`max_keys` must be at least")
        if number_of_shards < 1:
            raise ValueError("`number_of_shards` must be at least")
        if max_value_size < 8:
            raise ValueError("`max_value_size` must be at least 8 bytes.")

        self._max_keys = max_keys
        self._number_of_shards = number_of_shards
        self._max_value_size = max_value_size
        self._cleanup_frequency = cleanup_frequency

        # Hash table capacity (next power of two above max_keys / load)
        min_buckets = int(max_keys / _HASH_TABLE_LOAD_FACTOR) + 1
        hash_table_capacity = 1
        while hash_table_capacity < min_buckets:
            hash_table_capacity <<= 1
        self._hash_table_capacity: int = hash_table_capacity
        self._hash_table_mask: int = hash_table_capacity - 1  # for fast modulo via &

        # Slot data layout
        raw_slot_size = (
            self._VALUE_LENGTH_SIZE
            + max_value_size
            + self._EXPIRY_SIZE
            + self._OCCUPIED_FLAG_SIZE
        )
        self._slot_size: int = (raw_slot_size + 7) & ~7  # 8-byte aligned

        # Byte offsets within a slot
        self._value_length_offset: int = 0
        self._value_offset: int = self._VALUE_LENGTH_SIZE
        self._expiry_offset: int = self._VALUE_LENGTH_SIZE + max_value_size
        self._occupied_flag_offset: int = (
            self._VALUE_LENGTH_SIZE + max_value_size + self._EXPIRY_SIZE
        )

        # Shared memory region offsets
        # Header: free_count (4) + free_stack (4 * max_keys)
        self._header_size: int = _HEADER_FREE_COUNT_SIZE + 4 * max_keys
        self._free_stack_offset: int = _HEADER_FREE_COUNT_SIZE

        # Hash table
        self._hash_table_offset: int = self._header_size
        self._hash_table_size: int = hash_table_capacity * _HASH_TABLE_ENTRY_SIZE

        # Slot data
        self._slot_data_offset: int = self._hash_table_offset + self._hash_table_size
        self._slot_data_size: int = max_keys * self._slot_size

        # Total shared memory size
        self._shared_memory_size: int = (
            self._header_size + self._hash_table_size + self._slot_data_size
        )

        # Set during initialize()
        self._shared_memory: typing.Optional[SharedMemory] = None
        self._buffer: typing.Optional[memoryview] = None

        # Semaphores (created before fork, inherited by workers)
        self._slot_map_semaphore: typing.Optional[Semaphore] = None  # type: ignore[type-arg]
        self._shard_semaphores: typing.Optional[typing.List[Semaphore]] = None  # type: ignore[type-arg]

        # Named semaphores — process-local, not in shared memory.
        # Each process builds its own dict; cross-process named locking is
        # not needed because named locks are only used by strategies within
        # a single asyncio event loop.
        self._named_semaphores: typing.Dict[str, Semaphore] = {}  # type: ignore[type-arg]
        self._named_semaphores_lock = multiprocessing.Semaphore(
            1
        )  # protects _named_semaphores dict creation

        self._cleanup_task: typing.Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._initialized: bool = False

    async def initialize(self) -> None:
        """
        Allocate shared memory and create semaphores.

        Safe to call multiple times (subsequent calls are no-ops).
        Must be called **before** forking worker processes.
        """
        if self._initialized:
            return

        loop = asyncio.get_running_loop()

        # Create shared memory and zero it
        shared_memory = SharedMemory(create=True, size=self._shared_memory_size)
        buffer = memoryview(shared_memory.buf)  # type: ignore[arg-type]
        buffer[:] = bytes(self._shared_memory_size)  # zero-initialise

        # Create semaphores synchronously (cheap, no subprocess)
        def _create_semaphores() -> typing.Tuple[Semaphore, typing.List[Semaphore]]:  # type: ignore[type-arg]
            slot_map_semaphore = multiprocessing.Semaphore(1)
            shard_semaphores = [
                multiprocessing.Semaphore(1) for _ in range(self._number_of_shards)
            ]
            return slot_map_semaphore, shard_semaphores

        try:
            slot_map_semaphore, shard_semaphores = await loop.run_in_executor(
                None, _create_semaphores
            )
        except Exception:
            buffer.release()
            shared_memory.close()
            shared_memory.unlink()
            raise

        self._shared_memory = shared_memory
        self._buffer = buffer
        self._slot_map_semaphore = slot_map_semaphore
        self._shard_semaphores = shard_semaphores

        # Initialise the free-slot stack in shared memory:
        # free_count = max_keys, free_stack = [max_keys-1, max_keys-2, ..., 0]
        # (top of stack = index 0, popped first)
        _UINT32_STRUCT.pack_into(buffer, _HEADER_FREE_COUNT_OFFSET, self._max_keys)
        for i in range(self._max_keys):
            # Stack entry i holds slot index (max_keys - 1 - i) so slot 0 is
            # popped first (stack grows downward from max_keys-1)
            _UINT32_STRUCT.pack_into(
                buffer,
                self._free_stack_offset + i * 4,
                self._max_keys - 1 - i,
            )

        if self._cleanup_frequency is not None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self._initialized = True

    async def ready(self) -> bool:
        return self._initialized and self._shared_memory is not None

    async def close(self) -> None:
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

        if self._buffer is not None:
            self._buffer.release()
            self._buffer = None

        if self._shared_memory is not None:
            self._shared_memory.close()
            self._shared_memory.unlink()
            self._shared_memory = None

    async def reset(self) -> None:
        if not self._initialized:
            return
        await self.clear()

    def _assert_ready(self) -> None:
        if not self._initialized or self._buffer is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def _hash_table_lookup(self, buffer: memoryview, key_bytes: bytes) -> int:
        """
        Find the bucket index for *key_bytes*.

        Returns the index of the occupied bucket containing this key, OR
        the index of the first empty/tombstone bucket where it could be
        inserted.  The caller must check the bucket state to distinguish.

        Caller must hold `_slot_map_semaphore`.
        """
        capacity = self._hash_table_capacity
        mask = self._hash_table_mask
        hash_table_offset = self._hash_table_offset
        start = _fnv1a_32(key_bytes) & mask
        first_tombstone = -1

        for i in range(capacity):
            idx = (start + i) & mask
            entry_offset = hash_table_offset + idx * _HASH_TABLE_ENTRY_SIZE
            state = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
            )[0]

            if state == _HASH_TABLE_EMPTY_STATE:
                # Key not present; return tombstone if we passed one, else this empty bucket
                return first_tombstone if first_tombstone != -1 else idx

            if state == _HASH_TABLE_TOMBSTONE_STATE:
                if first_tombstone == -1:
                    first_tombstone = idx
                continue

            # state == OCCUPIED — check key match
            key_length = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_KEY_LENGTH_OFFSET
            )[0]
            if key_length == len(key_bytes):
                key_start = entry_offset + _HASH_TABLE_KEY_OFFSET
                stored = bytes(buffer[key_start : key_start + key_length])
                if stored == key_bytes:
                    return idx  # found

        # Table is full (should never happen if load factor is respected)
        raise BackendError("Hash table is full. This should never happen.")

    def _get_hash_table_slot(
        self, buffer: memoryview, key_bytes: bytes
    ) -> typing.Optional[int]:
        """
        Return the slot_idx for *key_bytes*, or None if not present.
        Caller must hold `_slot_map_semaphore`.
        """
        idx = self._hash_table_lookup(buffer, key_bytes)
        entry_offset = self._hash_table_offset + idx * _HASH_TABLE_ENTRY_SIZE
        state = _UINT8_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
        )[0]
        if state != _HASH_TABLE_OCCUPIED_STATE:
            return None
        return _UINT32_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET
        )[0]

    def _hash_table_upsert(
        self, buffer: memoryview, key_bytes: bytes, slot_idx: int
    ) -> None:
        """
        Insert or update *key_bytes* -> *slot_idx* in the hash table.
        Caller must hold `_slot_map_semaphore`.
        Raises `BackendError` if the key is too long or table is full.
        """
        if len(key_bytes) > _KEY_MAX_BYTES:
            raise BackendError(
                f"Key length {len(key_bytes)} exceeds maximum {_KEY_MAX_BYTES} bytes."
            )
        idx = self._hash_table_lookup(buffer, key_bytes)
        entry_offset = self._hash_table_offset + idx * _HASH_TABLE_ENTRY_SIZE
        key_length = len(key_bytes)
        _UINT8_STRUCT.pack_into(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET, _HASH_TABLE_OCCUPIED_STATE
        )
        _UINT8_STRUCT.pack_into(
            buffer, entry_offset + _HASH_TABLE_KEY_LENGTH_OFFSET, key_length
        )
        buffer[
            entry_offset + _HASH_TABLE_KEY_OFFSET : entry_offset
            + _HASH_TABLE_KEY_OFFSET
            + key_length
        ] = key_bytes
        _UINT32_STRUCT.pack_into(
            buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET, slot_idx
        )

    def _hash_table_delete(
        self, buffer: memoryview, key_bytes: bytes
    ) -> typing.Optional[int]:
        """
        Remove *key_bytes* from the hash table.
        Returns the freed slot_idx, or None if the key was not present.
        Caller must hold `_slot_map_semaphore`.
        """
        idx = self._hash_table_lookup(buffer, key_bytes)
        entry_offset = self._hash_table_offset + idx * _HASH_TABLE_ENTRY_SIZE
        state = _UINT8_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
        )[0]
        if state != _HASH_TABLE_OCCUPIED_STATE:
            return None
        slot_idx = _UINT32_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET
        )[0]
        # Mark as tombstone to preserve probe chains for other keys
        _UINT8_STRUCT.pack_into(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET, _HASH_TABLE_TOMBSTONE_STATE
        )
        return slot_idx

    def _hash_table_iter_occupied(
        self, buffer: memoryview
    ) -> typing.Iterator[typing.Tuple[str, int]]:
        """
        Iterate over all occupied (key_str, slot_idx) pairs.
        Caller is responsible for holding `_slot_map_semaphore` if needed.
        """
        hash_table_offset = self._hash_table_offset
        for i in range(self._hash_table_capacity):
            entry_offset = hash_table_offset + i * _HASH_TABLE_ENTRY_SIZE
            state = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
            )[0]
            if state != _HASH_TABLE_OCCUPIED_STATE:
                continue
            
            key_length = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_KEY_LENGTH_OFFSET
            )[0]
            key_str = bytes(
                buffer[
                    entry_offset + _HASH_TABLE_KEY_OFFSET : entry_offset
                    + _HASH_TABLE_KEY_OFFSET
                    + key_length
                ]
            ).decode("utf-8")
            slot_idx = _UINT32_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET
            )[0]
            yield key_str, slot_idx

    def _free_stack_pop(self, buffer: memoryview) -> int:
        """
        Pop and return a free slot index.
        Raises `BackendError` if the stack is empty (max_keys reached).
        Caller must hold `_slot_map_semaphore`.
        """
        count = _UINT32_STRUCT.unpack_from(buffer, _HEADER_FREE_COUNT_OFFSET)[0]
        if count == 0:
            raise BackendError(
                f"`max_keys` ({self._max_keys}) reached. "
                "Increase `max_keys` or reduce the number of distinct throttle keys."
            )
        count -= 1
        slot_idx = _UINT32_STRUCT.unpack_from(
            buffer, self._free_stack_offset + count * 4
        )[0]
        _UINT32_STRUCT.pack_into(buffer, _HEADER_FREE_COUNT_OFFSET, count)
        return slot_idx

    def _free_stack_push(self, buffer: memoryview, slot_idx: int) -> None:
        """
        Return *slot_idx* to the free pool. Caller must hold `_slot_map_semaphore`.
        """
        count = _UINT32_STRUCT.unpack_from(buffer, _HEADER_FREE_COUNT_OFFSET)[0]
        _UINT32_STRUCT.pack_into(buffer, self._free_stack_offset + count * 4, slot_idx)
        _UINT32_STRUCT.pack_into(buffer, _HEADER_FREE_COUNT_OFFSET, count + 1)

    def _slot_offset(self, slot_idx: int) -> int:
        return self._slot_data_offset + slot_idx * self._slot_size

    def _read_slot(
        self, buffer: memoryview, slot_idx: int
    ) -> typing.Tuple[typing.Optional[str], float, bool]:
        """Return (value, expires_at, occupied). Value is None if not occupied."""
        offset = self._slot_offset(slot_idx)
        occupied: bool = self._OCCUPIED_FLAG_STRUCT.unpack_from(
            buffer, offset + self._occupied_flag_offset
        )[0]
        if not occupied:
            return None, 0.0, False

        value_length: int = self._VALUE_LENGTH_STRUCT.unpack_from(
            buffer, offset + self._value_length_offset
        )[0]
        value = bytes(
            buffer[
                offset + self._value_offset : offset + self._value_offset + value_length
            ]
        ).decode("utf-8")
        expires_at: float = self._EXPIRY_STRUCT.unpack_from(
            buffer, offset + self._expiry_offset
        )[0]
        return value, expires_at, True

    def _write_slot(
        self,
        buffer: memoryview,
        slot_idx: int,
        value: str,
        expires_at: float,
    ) -> None:
        """
        Write value + expires_at + occupied=True into *slot_idx*.
        Raises `BackendError` if value exceeds max_value_size.
        """
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_size:
            raise BackendError(
                f"Value size {len(encoded)} bytes exceeds `max_value_size` "
                f"({self._max_value_size}). Increase `max_value_size`."
            )
        offset = self._slot_offset(slot_idx)
        self._VALUE_LENGTH_STRUCT.pack_into(
            buffer, offset + self._value_length_offset, len(encoded)
        )
        buffer[
            offset + self._value_offset : offset + self._value_offset + len(encoded)
        ] = encoded
        self._EXPIRY_STRUCT.pack_into(buffer, offset + self._expiry_offset, expires_at)
        self._OCCUPIED_FLAG_STRUCT.pack_into(
            buffer, offset + self._occupied_flag_offset, True
        )

    def _write_value_only(self, buffer: memoryview, slot_idx: int, value: str) -> None:
        """Overwrite only the value bytes, preserving expiry and occupied flag."""
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_size:
            raise BackendError(
                f"Value size {len(encoded)} bytes exceeds `max_value_size` "
                f"({self._max_value_size}). Increase `max_value_size`."
            )

        offset = self._slot_offset(slot_idx)
        self._VALUE_LENGTH_STRUCT.pack_into(
            buffer, offset + self._value_length_offset, len(encoded)
        )
        buffer[
            offset + self._value_offset : offset + self._value_offset + len(encoded)
        ] = encoded

    def _clear_slot(self, buffer: memoryview, slot_idx: int) -> None:
        """Mark slot as unoccupied. No need to zero the value bytes."""
        offset = self._slot_offset(slot_idx)
        self._OCCUPIED_FLAG_STRUCT.pack_into(
            buffer, offset + self._occupied_flag_offset, False
        )

    def _shard_idx(self, key: str) -> int:
        """Deterministic shard index for *key*. Uses FNV-1a, not hash()."""
        return _fnv1a_32(key.encode("utf-8")) % self._number_of_shards

    def _get(self, key: str) -> typing.Optional[str]:
        """
        Synchronous get.

        Lock order: slot_map_semaphore -> shard_semaphore.
        We hold slot_map_semaphore only for the hash table lookup (minimal time),
        then release it before acquiring the shard_semaphore for the slot read.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        # Look up slot index under slot_map_semaphore
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_idx = self._get_hash_table_slot(buffer, key_bytes)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if slot_idx is None:
            return None

        # Read slot data under shard_semaphore
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if not occupied:
                return None
            if expires_at != 0.0 and expires_at < now:
                return None
            return value
        finally:
            shard_semaphore.release()

    def _set(self, key: str, value: str, expire: typing.Optional[float]) -> None:
        """
        Synchronous set.

        Lock order: slot_map_semaphore (for allocation) -> shard_semaphore (for write).
        Both locks released before the next is acquired.
        """
        buffer = self._buffer
        assert buffer is not None
        key_bytes = key.encode("utf-8")
        expires_at = (time() + expire) if expire is not None else 0.0

        # Allocate or find slot under slot_map_semaphore
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_idx = self._get_hash_table_slot(buffer, key_bytes)
            if slot_idx is None:
                slot_idx = self._free_stack_pop(buffer)
                self._hash_table_upsert(buffer, key_bytes, slot_idx)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        # Write slot data under shard_semaphore
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            self._write_slot(buffer, slot_idx, value, expires_at)
        finally:
            shard_semaphore.release()

    def _delete(self, key: str) -> bool:
        """
        Synchronous delete.

        Lock order: shard_semaphore first (to guard the slot clear), then
        slot_map_semaphore (to remove from hash table and push to free stack).

        This is the documented exception to the normal ordering rule.
        It is safe because _delete never calls _free_stack_pop
        (which is the only path that strictly requires slot_map_semaphore first
        to avoid ABA on slot reuse).

        We must hold shard_semaphore while clearing the slot to prevent a
        concurrent reader seeing the slot as occupied after we've
        returned it to the free pool.
        """
        buffer = self._buffer
        assert buffer is not None
        key_bytes = key.encode("utf-8")

        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
            try:
                slot_idx = self._hash_table_delete(buffer, key_bytes)
                if slot_idx is None:
                    return False
                self._clear_slot(buffer, slot_idx)
                self._free_stack_push(buffer, slot_idx)
                return True
            finally:
                self._slot_map_semaphore.release()  # type: ignore[union-attr]
        finally:
            shard_semaphore.release()

    def _increment(self, key: str, amount: int) -> int:
        """
        Synchronous increment.

        Lock order: slot_map_semaphore -> shard_semaphore (sequentially, not nested).

        Pitfall: between releasing slot_map_semaphore and acquiring shard_semaphore,
        another process could delete the slot and reallocate it to a
        different key.  We detect this by re-checking the occupied flag
        under the shard_semaphore.  If the slot was recycled, we treat it as
        a fresh key (value = 0 before increment).
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        # Allocate/find slot
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_idx = self._get_hash_table_slot(buffer, key_bytes)
            is_new = slot_idx is None
            if is_new:
                slot_idx = self._free_stack_pop(buffer)
                self._hash_table_upsert(buffer, key_bytes, slot_idx)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        assert slot_idx is not None
        # Read-modify-write under shard_semaphore
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            # Treat as zero if: newly allocated, unoccupied, or expired
            if not occupied or (expires_at != 0.0 and expires_at < now):
                new_val = amount
                self._write_slot(buffer, slot_idx, str(new_val), 0.0)
            else:
                try:
                    current = int(value)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    current = 0
                new_val = current + amount
                self._write_value_only(buffer, slot_idx, str(new_val))
            return new_val
        finally:
            shard_semaphore.release()

    def _expire(self, key: str, seconds: int) -> bool:
        """
        Synchronous expire.

        Lock order: slot_map_semaphore -> shard_semaphore (sequentially).
        Same recycled-slot caveat as _increment — we re-check
        occupied under shard_semaphore.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_idx = self._get_hash_table_slot(buffer, key_bytes)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if slot_idx is None:
            return False

        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            _, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if not occupied or (expires_at != 0.0 and expires_at < now):
                return False
            self._EXPIRY_STRUCT.pack_into(
                buffer,
                self._slot_offset(slot_idx) + self._expiry_offset,
                now + seconds,
            )
            return True
        finally:
            shard_semaphore.release()

    def _increment_with_ttl(self, key: str, amount: int, ttl: int) -> int:
        """
        Synchronous increment_with_ttl.

        Hot path for all fixed-window and sliding-window strategies.
        TTL is applied only on first write or after expiry.

        Lock order: slot_map_semaphore -> shard_semaphore (sequentially).
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        # Allocate/find slot
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_idx = self._get_hash_table_slot(buffer, key_bytes)
            if slot_idx is None:
                slot_idx = self._free_stack_pop(buffer)
                self._hash_table_upsert(buffer, key_bytes, slot_idx)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        # Read-modify-write under shard_semaphore
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            value, expires_at, occupied = self._read_slot(buffer, slot_idx)
            is_new = not occupied or (expires_at != 0.0 and expires_at < now)
            if is_new:
                new_val = amount
                self._write_slot(buffer, slot_idx, str(new_val), now + ttl)
            else:
                try:
                    current = int(value)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    current = 0
                new_val = current + amount
                # Preserve existing TTL; only apply ttl if there was none
                effective_expiry = expires_at if expires_at != 0.0 else now + ttl
                self._write_slot(buffer, slot_idx, str(new_val), effective_expiry)
            return new_val
        finally:
            shard_semaphore.release()

    def _multi_get(
        self,
        keys: typing.Tuple[str, ...],
        shard_to_keys: typing.Dict[int, typing.List[str]],
    ) -> typing.Dict[str, typing.Optional[str]]:
        """
        Synchronous multi_get.

        Lock order: one slot_map_semaphore acquisition for all lookups, then
        shard_semaphores in ascending shard-index order.

        We do all hash table lookups in one slot_map_semaphore hold to avoid
        repeated acquire/release overhead.  The slot index snapshot is
        safe because we re-check the occupied flag under each shard_semaphore.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()

        # Look up all slot indices in one slot_map_semaphore hold
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_indices: typing.Dict[str, typing.Optional[int]] = {
                key: self._get_hash_table_slot(buffer, key.encode("utf-8"))
                for key in keys
            }
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        # Read shard by shard in ascending order
        results: typing.Dict[str, typing.Optional[str]] = {}
        for shard_idx in sorted(shard_to_keys):
            shard_semaphore = self._shard_semaphores[shard_idx]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                for key in shard_to_keys[shard_idx]:
                    si = slot_indices[key]
                    if si is None:
                        results[key] = None
                        continue
                    value, expires_at, occupied = self._read_slot(buffer, si)
                    if not occupied or (expires_at != 0.0 and expires_at < now):
                        results[key] = None
                    else:
                        results[key] = value
            finally:
                shard_semaphore.release()

        return results

    def _multi_set(
        self,
        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]],
        all_keys: typing.List[str],
        expire: typing.Optional[float],
    ) -> None:
        """
        Synchronous multi_set.

        Allocate all slots in one slot_map_semaphore hold, then write to shards
        in ascending shard-index order.
        """
        buffer = self._buffer
        assert buffer is not None
        expires_at = (time() + expire) if expire is not None else 0.0

        # Allocate all slots at once
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_assignments: typing.Dict[str, int] = {}
            for key in all_keys:
                key_bytes = key.encode("utf-8")
                si = self._get_hash_table_slot(buffer, key_bytes)
                if si is None:
                    si = self._free_stack_pop(buffer)
                    self._hash_table_upsert(buffer, key_bytes, si)
                slot_assignments[key] = si
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        # Write in shard order
        for shard_idx in sorted(shard_to_items):
            shard_semaphore = self._shard_semaphores[shard_idx]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                for key, val in shard_to_items[shard_idx]:
                    self._write_slot(buffer, slot_assignments[key], val, expires_at)
            finally:
                shard_semaphore.release()

    def _clear(self) -> None:
        """
        Remove all keys whose name starts with this backend's namespace prefix.

        Acquires slot_map_semaphore for the full scan + delete pass. Then takes
        each shard_semaphore briefly to clear the slot's occupied flag.

        We collect (slot_idx, shard_idx) pairs under slot_map_semaphore, release
        it, then clear the slots shard by shard.  This avoids holding
        slot_map_semaphore + shard_semaphore simultaneously (which would violate ordering
        if any other code path exists that holds shard_semaphore then slot_map_semaphore,
        as _delete does).  It's safe here because we own the namespace
        exclusively.
        """
        buffer = self._buffer
        assert buffer is not None
        prefix = f"{self.namespace}:".encode("utf-8")

        # Collect and remove from hash table under slot_map_semaphore
        to_clear: typing.List[typing.Tuple[int, int]] = []  # (slot_idx, shard_idx)
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            for key_str, slot_idx in list(self._hash_table_iter_occupied(buffer)):
                key_bytes = key_str.encode("utf-8")
                if not key_bytes.startswith(prefix):
                    continue
                # Remove from hash table
                self._hash_table_delete(buffer, key_bytes)
                # Return slot to free pool immediately
                self._free_stack_push(buffer, slot_idx)
                to_clear.append((slot_idx, self._shard_idx(key_str)))
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if not to_clear:
            return

        # Clear occupied flags in shard order (ascending to avoid deadlock)
        by_shard: typing.Dict[int, typing.List[int]] = {}
        for slot_idx, shard_idx in to_clear:
            by_shard.setdefault(shard_idx, []).append(slot_idx)

        for shard_idx in sorted(by_shard):
            shard_semaphore = self._shard_semaphores[shard_idx]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                for slot_idx in by_shard[shard_idx]:
                    self._clear_slot(buffer, slot_idx)
            finally:
                shard_semaphore.release()

    def _cleanup(self) -> int:
        """
        Reclaim expired slots.

        Pass 1 (no lock): snapshot occupied entries, read expiry from slot
        data to build a candidate list.  Racy but harmless — it's a hint.

        Pass 2 (slot_map_semaphore only): for each candidate, re-verify the key
        still maps to the same slot and the slot is still expired before
        freeing.  We do NOT hold shard_semaphore here to avoid the ordering
        violation (shard_semaphore before slot_map_semaphore is only legal in
        _delete).  The float64 expiry write is not atomic on all
        architectures, but the worst outcome is a spurious skip (missed
        cleanup), not a spurious free.
        """
        buffer = self._buffer
        if buffer is None:
            return 0

        now = time()

        # Pass 1: unsynchronised snapshot
        candidates: typing.List[typing.Tuple[bytes, int]] = []
        for key_str, slot_idx in self._hash_table_iter_occupied(buffer):
            _, expires_at, occupied = self._read_slot(buffer, slot_idx)
            if occupied and expires_at != 0.0 and expires_at < now:
                candidates.append((key_str.encode("utf-8"), slot_idx))

        if not candidates:
            return 0

        freed = 0
        # Pass 2: authoritative under slot_map_semaphore
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            for key_bytes, expected_slot_idx in candidates:
                current_slot_idx = self._get_hash_table_slot(buffer, key_bytes)
                if current_slot_idx is None or current_slot_idx != expected_slot_idx:
                    continue  # already deleted or reallocated
                # Re-read without shard_semaphore (see docstring)
                _, expires_at, occupied = self._read_slot(buffer, current_slot_idx)
                if not occupied or expires_at == 0.0 or expires_at >= now:
                    continue  # refreshed since pass 1
                self._hash_table_delete(buffer, key_bytes)
                self._free_stack_push(buffer, current_slot_idx)
                self._clear_slot(buffer, current_slot_idx)
                freed += 1
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        return freed

    def _get_or_create_named_semaphore(self, name: str) -> Semaphore:  # type: ignore[type-arg]
        """
        Return an existing named semaphore or create a new one.

        Named semaphores are process-local (not in shared memory). They are
        used by throttle strategies within a single event loop and do not
        need to be visible to other processes.  Each process builds its own
        dict independently.

        Double-checked locking under _named_semaphores_lock.
        """
        existing = self._named_semaphores.get(name)
        if existing is not None:
            return existing

        self._named_semaphores_lock.acquire()
        try:
            existing = self._named_semaphores.get(name)
            if existing is not None:
                return existing

            semaphore = multiprocessing.Semaphore(1)
            self._named_semaphores[name] = semaphore
            return semaphore
        finally:
            self._named_semaphores_lock.release()

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._get, key
        )

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        self._assert_ready()
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._set, key, value, expire
        )

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._delete, key
        )

    async def increment(self, key: str, amount: int = 1) -> int:
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._increment, key, amount
        )

    async def expire(self, key: str, seconds: int) -> bool:
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._expire, key, seconds
        )

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._increment_with_ttl, key, amount, ttl
        )

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        self._assert_ready()
        if not keys:
            return []

        shard_to_keys: typing.Dict[int, typing.List[str]] = {}
        for key in keys:
            shard_to_keys.setdefault(self._shard_idx(key), []).append(key)

        result_map = await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._multi_get, keys, shard_to_keys
        )
        return [result_map[k] for k in keys]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        self._assert_ready()
        if not items:
            return

        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]] = {}
        for key, val in items.items():
            shard_to_items.setdefault(self._shard_idx(key), []).append((key, val))

        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None,
            self._multi_set,
            shard_to_items,
            list(items.keys()),
            expire,
        )

    async def get_lock(self, name: str) -> _AsyncMPSemaphoreLock:
        self._assert_ready()
        sem = await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._get_or_create_named_semaphore, name
        )
        return _AsyncMPSemaphoreLock(sem)

    async def clear(self) -> None:
        self._assert_ready()
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            None, self._clear
        )

    async def _cleanup_loop(self) -> None:
        assert self._cleanup_frequency is not None
        while self._initialized:
            try:
                await asyncio.sleep(self._cleanup_frequency)
                await asyncio.get_running_loop().run_in_executor(None, self._cleanup)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
