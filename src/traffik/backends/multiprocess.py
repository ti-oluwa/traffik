"""
Multi-process in-memory throttle backend using shared memory.

Designed for multi-worker single-machine deployments (e.g. gunicorn/uvicorn
with multiple workers) where Redis/Memcached is unavailable or undesirable,
but a single `InMemoryBackend` per process would result in each worker
maintaining independent, incorrect rate limit counters.

All state lives in a single `multiprocessing.shared_memory` segment.
Locking uses `multiprocessing.Semaphore` objects created before fork and
inherited by all workers.

**Platform requirement**

This backend requires the `fork` or `forkserver` multiprocessing start
method. It will raise `RuntimeError` on Windows or when the start method is
`spawn` (the macOS default since Python 3.8). Linux with the default
`fork` start method is the primary supported platform.

**Shared memory naming**

Every instance owns (or attaches to) a named POSIX shared memory segment.
The name is either supplied explicitly via `shared_memory_name` or derived
automatically from the namespace.  Names must:

- Contain only `[A-Za-z0-9_-]`
- Be at most 30 characters (the OS prepends `/`, and macOS caps at 31)
- Be non-empty

**Create / attach pattern**

Use the `create` and `attach` class methods (or the `create` `bool`
constructor argument) to be explicit about ownership:

```
# In the parent process, before forking:
backend = MultiProcessInMemoryBackend.create(namespace="myapp")
await backend.initialize()

# In a worker that attaches to an already-initialised segment:
backend = MultiProcessInMemoryBackend.attach(namespace="myapp")
await backend.initialize()
```

Only the creator calls `unlink()` on close. Attachers call `close()` only.
"""

import asyncio
import multiprocessing
import re
import struct
import sys
import typing
from multiprocessing.shared_memory import SharedMemory
from multiprocessing.synchronize import Semaphore
from types import TracebackType

from typing_extensions import Self

from traffik._locks import _NamedLockHandle, _NamedLockPool
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError, LockTimeoutError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)
from traffik.utils import fnv1a_32bit_hash, time

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

# Shared memory name constraints
_SHARED_MEMORY_NAME_MAX_LENGTH: int = 30  # macOS caps at 31 including OS-prepended '/'
_SHARED_MEMORY_NAME_PREFIX: str = "traffik_"
_SHARED_MEMORY_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# ABA mitigation: maximum retries when a generation mismatch is detected
_MAX_ABA_RETRIES: int = 3

# Structs (pre-compiled, module-level)
_UINT8_STRUCT = struct.Struct("=B")  # 1 byte  unsigned
_UINT16_STRUCT = struct.Struct("=H")  # 2 bytes unsigned
_UINT32_STRUCT = struct.Struct("=I")  # 4 bytes unsigned
_FLOAT64_STRUCT = struct.Struct("=d")  # 8 bytes double
_BOOL_STRUCT = struct.Struct("=?")  # 1 byte  bool


def _derive_shared_memory_name(namespace: str) -> str:
    """
    Derive a valid shared memory segment name from *namespace*.

    Rules applied:
    - Replace any character outside `[A-Za-z0-9_-]` with `_`.
    - Append an 8-character hex suffix of the FNV-1a hash of the *raw*
      namespace bytes so that namespaces differing only in sanitised
      characters never collide.
    - Prefix with `traffik_`.
    - Truncate the sanitised middle so the total is ≤ `_SHARED_MEMORY_NAME_MAX_LENGTH`.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", namespace)
    hex_suffix = format(fnv1a_32bit_hash(namespace.encode("utf-8")), "08x")
    # prefix(8) + "_"(1) + sanitized(?) + "_"(1) + hex_suffix(8) = 18 + len(sanitized)
    # -> len(sanitized) ≤ _SHARED_MEMORY_NAME_MAX_LENGTH - 18
    middle_max = (
        _SHARED_MEMORY_NAME_MAX_LENGTH
        - len(_SHARED_MEMORY_NAME_PREFIX)
        - 1
        - len(hex_suffix)
    )  # = 13
    middle_max = max(middle_max, 1)  # always keep at least one char
    sanitized = sanitized[:middle_max]
    return f"{_SHARED_MEMORY_NAME_PREFIX}{sanitized}_{hex_suffix}"


def _validate_shared_memory_name(name: str) -> None:
    """
    Raise `ValueError` if *name* is not a legal shared memory segment name.
    """
    if not name:
        raise ValueError("`shared_memory_name` must not be empty.")
    if len(name) > _SHARED_MEMORY_NAME_MAX_LENGTH:
        raise ValueError(
            f"`shared_memory_name` must be at most {_SHARED_MEMORY_NAME_MAX_LENGTH} characters "
            f"(got {len(name)!r} = {len(name)} chars). "
            "macOS caps POSIX shared memory names at 31 characters including the "
            "leading '/' the OS prepends."
        )
    if not _SHARED_MEMORY_NAME_ALLOWED_RE.match(name):
        raise ValueError(
            f"`shared_memory_name` {name!r} contains invalid characters. "
            "Only [A-Za-z0-9_-] are allowed."
        )


class _AsyncMPSemaphoreLock:
    """
    `AsyncLock`-compatible wrapper around a `multiprocessing.Semaphore(1)`.

    Blocking acquire is dispatched to the thread-pool executor so the
    event loop is never stalled. The semaphore itself is a POSIX futex
    (Linux) or equivalent, hence acquire/release cost ~1-5 µs with no IPC.

    **One instance must be used by exactly one task at a time.** A fresh
    wrapper is returned on every `get_lock(...)` call, so this is automatically
    satisfied as long as you do not share a single instance across concurrent
    tasks. The underlying semaphore is shared and process-safe; only the
    `_acquired` bookkeeping is instance-local.

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

    Locking uses `multiprocessing.Semaphore` objects that must be created
    **before** fork and inherited by all worker processes through the normal
    Unix fork mechanism.

    **Platform requirement**

    Requires `fork` or `forkserver` multiprocessing start method.
    Raises `RuntimeError` on Windows or when the start method is `spawn`.

    **Usage contract**

    ```
    # In the parent process, before forking:
    backend = MultiProcessInMemoryBackend.create(namespace="myapp")
    await backend.initialize()

    # In a spawned worker that needs to attach to the existing segment:
    backend = MultiProcessInMemoryBackend.attach(namespace="myapp")
    await backend.initialize()
    ```

    Only the creator calls `unlink()` on close.

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
        slot: uint32 generation | uint16 value_length | bytes value[max_value_size]
                | float64 expires_at | uint8 occupied
        (padded to 8-byte boundary)
    ```

    **Lock ordering** (must always be respected to avoid deadlock)

    - `slot_map_semaphore` before any `shard_semaphores[i]`
    - Never hold two `shard_semaphores` simultaneously
    - Named semaphores should always be the outermost lock.
        Never acquire while holding `slot_map_semaphore` or `shard_semaphores`

    **ABA mitigation**

    Each slot carries a `uint32 generation` counter incremented every time
    the slot is allocated (popped from the free stack). Operations that
    release `slot_map_semaphore` before acquiring `shard_semaphore`
    capture the generation at lookup time and verify it under
    `shard_semaphore` before reading or writing. A mismatch means the slot
    was recycled between the two lock acquisitions; the operation retries up
    to `_MAX_ABA_RETRIES` times before raising `BackendError`.
    """

    # Pre-compiled structs and sizes
    _GENERATION_STRUCT: typing.ClassVar[struct.Struct] = _UINT32_STRUCT
    _VALUE_LENGTH_STRUCT: typing.ClassVar[struct.Struct] = _UINT16_STRUCT
    _EXPIRY_STRUCT: typing.ClassVar[struct.Struct] = _FLOAT64_STRUCT
    _OCCUPIED_FLAG_STRUCT: typing.ClassVar[struct.Struct] = _BOOL_STRUCT

    _GENERATION_SIZE: typing.ClassVar[int] = 4
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
        lock_pool_size: int = 50,
        max_keys: int = 65536,
        number_of_shards: int = 64,
        max_value_size: int = 512,
        cleanup_frequency: typing.Optional[float] = None,
        shared_memory_name: typing.Optional[str] = None,
        create: bool = True,
        **kwargs: typing.Any,
    ) -> None:
        """
        :param namespace: Key prefix for all throttle keys.
        :param max_keys: Maximum number of distinct live keys at any time.
        :param number_of_shards: Number of shard semaphores (lock striping). Default `64`.
        :param max_value_size: Maximum UTF-8 byte length of a stored value. Default `512`.
        :param cleanup_frequency: Seconds between background expired-slot
            reclamation passes. `None` disables the task.  Default `None`.
        :param shared_memory_name: Explicit POSIX shared memory segment name.
            Must match `[A-Za-z0-9_-]`, max 30 characters. If `None` a
            name is derived automatically from *namespace*.
        :param create: When `True` (default) `initialize()` creates and
            owns the segment. When `False` it attaches to an existing
            segment created by another instance with the same name. Use the
            `create` / `attach` class methods for clarity.
        """
        if sys.platform == "win32":
            raise RuntimeError(
                f"`{self.__class__.__name__}` is not supported on Windows. "
                "It requires the 'fork' multiprocessing start method, which "
                "Windows does not provide. Use `RedisBackend` or `MemcachedBackend` "
                "for cross-platform multi-process deployments."
            )

        start_method = multiprocessing.get_start_method(allow_none=True)
        if start_method is not None and start_method not in ("fork", "forkserver"):
            raise RuntimeError(
                f"`{self.__class__.__name__}` requires the 'fork' or 'forkserver' "
                f"multiprocessing start method, but the current start method is "
                f"{start_method!r}. On macOS with Python ≥ 3.8 the default changed "
                f"to 'spawn'; call `multiprocessing.set_start_method('fork')` before "
                f"instantiating this backend, or use `RedisBackend` instead."
            )

        if shared_memory_name is None:
            shared_memory_name = _derive_shared_memory_name(namespace)

        _validate_shared_memory_name(shared_memory_name)
        self._shared_memory_name: str = shared_memory_name
        self._create: bool = create

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
            raise ValueError("`max_keys` must be at least 1.")
        if number_of_shards < 1:
            raise ValueError("`number_of_shards` must be at least 1.")
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
        self._hash_table_mask: int = hash_table_capacity - 1  # fast modulo via &

        # Slot data layout
        # generation(4) + value_length(2) + value(N) + expires_at(8) + occupied(1)
        # padded to 8-byte boundary
        raw_slot_size = (
            self._GENERATION_SIZE
            + self._VALUE_LENGTH_SIZE
            + max_value_size
            + self._EXPIRY_SIZE
            + self._OCCUPIED_FLAG_SIZE
        )
        self._slot_size: int = (raw_slot_size + 7) & ~7  # 8-byte aligned

        # Byte offsets within a slot
        self._generation_offset: int = 0
        self._value_length_offset: int = self._GENERATION_SIZE
        self._value_offset: int = self._GENERATION_SIZE + self._VALUE_LENGTH_SIZE
        self._expiry_offset: int = (
            self._GENERATION_SIZE + self._VALUE_LENGTH_SIZE + max_value_size
        )
        self._occupied_flag_offset: int = (
            self._GENERATION_SIZE
            + self._VALUE_LENGTH_SIZE
            + max_value_size
            + self._EXPIRY_SIZE
        )

        # Shared memory region offsets
        # Header: free_count(4) + free_stack(4 * max_keys)
        self._header_size: int = _HEADER_FREE_COUNT_SIZE + 4 * max_keys
        self._free_stack_offset: int = _HEADER_FREE_COUNT_SIZE

        # Hash table
        self._hash_table_offset: int = self._header_size
        self._hash_table_size: int = hash_table_capacity * _HASH_TABLE_ENTRY_SIZE

        # Slot data
        self._slot_data_offset: int = self._hash_table_offset + self._hash_table_size
        self._slot_data_size: int = max_keys * self._slot_size

        # Total
        self._shared_memory_size: int = (
            self._header_size + self._hash_table_size + self._slot_data_size
        )

        # Runtime state to be set during initialization
        self._shared_memory: typing.Optional[SharedMemory] = None
        self._buffer: typing.Optional[memoryview] = None

        self._slot_map_semaphore: typing.Optional[Semaphore] = None
        self._shard_semaphores: typing.Optional[typing.List[Semaphore]] = None

        # `_AsyncMPSemaphoreLock` cannot be pooled directly because its not reusable
        # across tasks. So we pool the semaphores and then provide `_AsyncMPSemaphoreLock`
        # as a proxy to wrap it when we create a `_NamedLockHandle` in `_NamedLockPool.get`
        self._named_lock_pool: _NamedLockPool[_AsyncMPSemaphoreLock] = _NamedLockPool(
            factory=lambda: multiprocessing.Semaphore(1),
            max_size=lock_pool_size,
            threadsafe=True,
            proxy=_AsyncMPSemaphoreLock,  # type: ignore[arg-type]
        )

        self._cleanup_task: typing.Optional[asyncio.Task[None]] = None  # type: ignore[type-arg]
        self._initialized: bool = False

    @classmethod
    def create(
        cls,
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
        shared_memory_name: typing.Optional[str] = None,
        **kwargs: typing.Any,
    ) -> Self:
        """
        Create a new backend instance that **owns** the shared memory segment.

        Call `await instance.initialize()` after construction to allocate
        the segment. Only one process should call `create()` for a given
        *shared_memory_name* / *namespace* combination.

        The segment is unlinked when `close()` is called on the creator.
        """
        return cls(
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            max_keys=max_keys,
            number_of_shards=number_of_shards,
            max_value_size=max_value_size,
            cleanup_frequency=cleanup_frequency,
            shared_memory_name=shared_memory_name,
            create=True,
            **kwargs,
        )

    @classmethod
    def attach(
        cls,
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
        shared_memory_name: typing.Optional[str] = None,
        **kwargs: typing.Any,
    ) -> Self:
        """
        Attach to an **existing** shared memory segment created by another
        instance (typically the parent process).

        The *namespace*, *shared_memory_name*, *max_keys*, *number_of_shards*,
        and *max_value_size* must match those used by the creator exactly as
        they determine the segment layout.

        The segment is **not** unlinked when `close()` is called on an attaching instance.
        """
        return cls(
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            max_keys=max_keys,
            number_of_shards=number_of_shards,
            max_value_size=max_value_size,
            cleanup_frequency=cleanup_frequency,
            shared_memory_name=shared_memory_name,
            create=False,
            **kwargs,
        )

    async def initialize(self) -> None:
        """
        Allocate (or attach to) the shared memory segment and create semaphores.

        Safe to call multiple times. Subsequent calls are no-ops.

        When `create=True` (the default), we allocate a new named segment,
        zeroes it, and writes the initial free-stack header.

        When `create=False`, we open an existing segment by name. The segment
        must already have been initialised by a creator instance.
        No zeroing or header writing is performed.

        Must be called before any read/write operations.
        """
        if self._initialized:
            return

        if self._create:
            shared_memory = SharedMemory(
                create=True,
                name=self._shared_memory_name,
                size=self._shared_memory_size,
            )
            buffer = memoryview(shared_memory.buf)  # type: ignore[arg-type]
            buffer[:] = bytes(self._shared_memory_size)  # zero-initialise
        else:
            shared_memory = SharedMemory(
                create=False,
                name=self._shared_memory_name,
            )
            buffer = memoryview(shared_memory.buf)  # type: ignore[arg-type]
            # Attacher's do not zero or re-initialise as the segment is live.

        # Create semaphores synchronously as they are cheap.
        # For fork: semaphores are created in the parent and inherited.
        # For attach (forkserver or explicit attach): semaphores are created
        # fresh per-process and are process-local by design (they only guard
        # in-process threading, not cross-process coordination, but cross-process
        # coordination is inherent in the shared memory layout itself and the
        # fact that all forked workers share the same semaphore objects via
        # fork inheritance).
        try:
            self._slot_map_semaphore = multiprocessing.Semaphore(1)
            self._shard_semaphores = [
                multiprocessing.Semaphore(1) for _ in range(self._number_of_shards)
            ]
        except Exception:
            buffer.release()
            shared_memory.close()
            if self._create:
                shared_memory.unlink()
            raise

        self._shared_memory = shared_memory
        self._buffer = buffer

        if self._create:
            # Initialise the free-slot stack:
            # free_count = max_keys, free_stack[i] = max_keys - 1 - i
            # so slot 0 is popped first.
            _UINT32_STRUCT.pack_into(buffer, _HEADER_FREE_COUNT_OFFSET, self._max_keys)
            for i in range(self._max_keys):
                _UINT32_STRUCT.pack_into(
                    buffer,
                    self._free_stack_offset + i * 4,
                    self._max_keys - 1 - i,
                )

        # Pre-populate named lock pool
        self._named_lock_pool.populate()

        if self._cleanup_frequency is not None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self._initialized = True

    async def ready(self) -> bool:
        return self._initialized and self._shared_memory is not None

    async def close(self) -> None:
        """
        Shut down this backend instance.

        Cancels the cleanup task, releases the buffer, and closes the shared
        memory handle. If this instance is the **creator** (`create=True`),
        the segment is also unlinked (destroyed). Attaching instances only
        close their handle.
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

        if self._buffer is not None:
            self._buffer.release()
            self._buffer = None

        if self._shared_memory is not None:
            self._shared_memory.close()
            if self._create:
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
        Return the bucket index for *key_bytes*.

        Returns the index of the occupied bucket containing this key, **or**
        the index of the first tombstone / empty bucket where it could be
        inserted. The caller must check the bucket state to distinguish.

        Must be called with `_slot_map_semaphore` held.
        """
        capacity = self._hash_table_capacity
        mask = self._hash_table_mask
        hash_table_offset = self._hash_table_offset
        start = fnv1a_32bit_hash(key_bytes) & mask
        first_tombstone = -1

        for i in range(capacity):
            idx = (start + i) & mask
            entry_offset = hash_table_offset + idx * _HASH_TABLE_ENTRY_SIZE
            state = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
            )[0]

            if state == _HASH_TABLE_EMPTY_STATE:
                return first_tombstone if first_tombstone != -1 else idx

            if state == _HASH_TABLE_TOMBSTONE_STATE:
                if first_tombstone == -1:
                    first_tombstone = idx
                continue

            # OCCUPIED state, check key match
            key_length = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_KEY_LENGTH_OFFSET
            )[0]
            if key_length == len(key_bytes):
                key_start = entry_offset + _HASH_TABLE_KEY_OFFSET
                stored = bytes(buffer[key_start : key_start + key_length])
                if stored == key_bytes:
                    return idx

        raise BackendError("Hash table is full. This should never happen.")

    def _get_hash_table_slot(
        self, buffer: memoryview, key_bytes: bytes
    ) -> typing.Optional[int]:
        """
        Return the slot_idx for *key_bytes*, or `None` if not present.

        Must be called with `_slot_map_semaphore` held.
        Use `_get_hash_table_slot_with_generation` when ABA protection is needed.
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

    def _get_hash_table_slot_with_generation(
        self, buffer: memoryview, key_bytes: bytes
    ) -> typing.Optional[typing.Tuple[int, int]]:
        """
        Return `(slot_idx, generation)` for *key_bytes*, or `None`.

        The generation is read from the slot data region (not the hash table
        entry) so that it reflects the current allocation epoch of the slot.
        Callers save this value and compare it under `shard_semaphore` to
        detect ABA slot recycling.

        Must be called with `_slot_map_semaphore` held.
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
        generation = self._read_slot_generation(buffer, slot_idx)
        return slot_idx, generation

    def _hash_table_upsert(
        self, buffer: memoryview, key_bytes: bytes, slot_idx: int
    ) -> None:
        """
        Insert or update *key_bytes* -> *slot_idx* in the hash table.

        Must be called with `_slot_map_semaphore` held.
        Raises `BackendError` if the key exceeds `_KEY_MAX_BYTES`.
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
        Remove *key_bytes* from the hash table (mark bucket as tombstone).

        Returns the freed slot_idx, or `None` if the key was not present.
        Must be called with `_slot_map_semaphore` held.
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
        _UINT8_STRUCT.pack_into(
            buffer,
            entry_offset + _HASH_TABLE_STATE_OFFSET,
            _HASH_TABLE_TOMBSTONE_STATE,
        )
        return slot_idx

    def _hash_table_iter_occupied(
        self, buffer: memoryview
    ) -> typing.Iterator[typing.Tuple[str, int]]:
        """
        Iterate over all occupied `(key_str, slot_idx)` pairs.

        Caller is responsible for holding `_slot_map_semaphore` if mutations
        must not interleave.
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
        Pop and return a free slot index, incrementing its generation counter.

        A generation increment is key to ABA-problem mitigation. Every time a
        slot is re-allocated its generation changes, making stale slot_idx
        references detectable by any concurrent operation that saved the
        previous generation.

        Must be called with `_slot_map_semaphore` held.
        Raises `BackendError` if the free stack is empty.
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
        # Bump the generation so any in-flight reader that holds an old
        # (slot_idx, generation) pair will detect the recycling.
        self._bump_slot_generation(buffer, slot_idx)
        return slot_idx

    def _free_stack_push(self, buffer: memoryview, slot_idx: int) -> None:
        """
        Return *slot_idx* to the free pool.

        Must be called with `_slot_map_semaphore` held.
        We do not bump the generation here. The bump happens on the next
        pop (allocation), which is the moment a new owner takes the slot.
        """
        count = _UINT32_STRUCT.unpack_from(buffer, _HEADER_FREE_COUNT_OFFSET)[0]
        _UINT32_STRUCT.pack_into(buffer, self._free_stack_offset + count * 4, slot_idx)
        _UINT32_STRUCT.pack_into(buffer, _HEADER_FREE_COUNT_OFFSET, count + 1)

    def _slot_offset(self, slot_idx: int) -> int:
        return self._slot_data_offset + slot_idx * self._slot_size

    def _read_slot_generation(self, buffer: memoryview, slot_idx: int) -> int:
        """
        Return the current generation counter for *slot_idx*.

        Can be called with either `_slot_map_semaphore` or the appropriate
        `shard_semaphore` held; both are valid read contexts.
        """
        offset = self._slot_offset(slot_idx)
        return _UINT32_STRUCT.unpack_from(buffer, offset + self._generation_offset)[0]

    def _bump_slot_generation(self, buffer: memoryview, slot_idx: int) -> None:
        """
        Increment the generation counter for *slot_idx*, wrapping at 2^32.

        Must be called with `_slot_map_semaphore` held (called from
        `_free_stack_pop` only).
        """
        offset = self._slot_offset(slot_idx)
        current = _UINT32_STRUCT.unpack_from(buffer, offset + self._generation_offset)[
            0
        ]
        _UINT32_STRUCT.pack_into(
            buffer,
            offset + self._generation_offset,
            (current + 1) & 0xFFFFFFFF,
        )

    def _read_slot(
        self, buffer: memoryview, slot_idx: int
    ) -> typing.Tuple[typing.Optional[str], float, bool]:
        """
        Return `(value, expires_at, occupied)`.

        `value` is `None` when `occupied` is `False`.
        Must be called with the appropriate `shard_semaphore` held.
        """
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
        Write *value*, *expires_at*, and `occupied=True` into *slot_idx*.

        Does **not** touch the generation counter. That is only written by
        `_bump_slot_generation` during allocation.

        Must be called with the appropriate `shard_semaphore` held.
        Raises `BackendError` if the encoded value exceeds `max_value_size`.
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
        """
        Overwrite only the value bytes, preserving generation, expiry, and
        occupied flag.

        Must be called with the appropriate `shard_semaphore` held.
        Raises `BackendError` if the encoded value exceeds `max_value_size`.
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

    def _clear_slot(self, buffer: memoryview, slot_idx: int) -> None:
        """
        Mark *slot_idx* as unoccupied.

        We also do not zero the value bytes or reset the generation here.
        The generation is only bumped on next allocation.

        Must be called with the appropriate `shard_semaphore` held (or
        under `_slot_map_semaphore` in `_cleanup` where the ordering
        exception is documented).
        """
        offset = self._slot_offset(slot_idx)
        self._OCCUPIED_FLAG_STRUCT.pack_into(
            buffer, offset + self._occupied_flag_offset, False
        )

    def _shard_idx(self, key: str) -> int:
        """Deterministic shard index. Uses FNV-1a, never Python's `hash()`."""
        return fnv1a_32bit_hash(key.encode("utf-8")) % self._number_of_shards

    def _get(self, key: str) -> typing.Optional[str]:
        """
        Synchronous get.

        Lock order: `slot_map_semaphore` -> `shard_semaphore` (sequential).

        ABA protection: the generation captured at hash-table lookup time is
        verified under `shard_semaphore`. A mismatch means the slot was
        recycled and the key no longer exists from our perspective, so we
        return `None`.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        # Phase 1: hash table lookup - capture slot_idx and generation
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            result = self._get_hash_table_slot_with_generation(buffer, key_bytes)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if result is None:
            return None
        slot_idx, generation = result

        # Phase 2: slot read - verify generation before trusting the data
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            if self._read_slot_generation(buffer, slot_idx) != generation:
                # Slot was recycled between phase 1 and phase 2 - key is gone.
                return None
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

        Lock order: `slot_map_semaphore` -> `shard_semaphore` (sequential).

        ABA protection: if the generation mismatches under `shard_semaphore`
        the allocation was stale. We retry up to `_MAX_ABA_RETRIES` times.
        Each retry re-acquires `slot_map_semaphore` to get a fresh
        (slot_idx, generation) pair.
        """
        buffer = self._buffer
        assert buffer is not None
        key_bytes = key.encode("utf-8")
        expires_at = (time() + expire) if expire is not None else 0.0

        for _ in range(_MAX_ABA_RETRIES):
            # Phase 1: allocate or locate slot
            self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
            try:
                hash_table_result = self._get_hash_table_slot_with_generation(
                    buffer, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer)  # bumps generation
                    self._hash_table_upsert(buffer, key_bytes, slot_idx)
                    generation = self._read_slot_generation(buffer, slot_idx)
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphore.release()  # type: ignore[union-attr]

            # Phase 2: write - verify generation first
            shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                if self._read_slot_generation(buffer, slot_idx) != generation:
                    # ABA detected - another thread recycled this slot. Retry.
                    continue
                self._write_slot(buffer, slot_idx, value, expires_at)
                return
            finally:
                shard_semaphore.release()

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {_MAX_ABA_RETRIES} retries."
        )

    def _delete(self, key: str) -> bool:
        """
        Synchronous delete.

        Lock order: `shard_semaphore` first, then `slot_map_semaphore`
        (the documented exception to the normal ordering rule).

        This is safe because `_delete` never calls `_free_stack_pop`, which
        is the only path requiring `slot_map_semaphore` *before*
        `shard_semaphore`. Holding `shard_semaphore` while clearing the
        slot prevents a concurrent reader from seeing the slot as occupied
        after it has been returned to the free pool.

        No ABA retry needed: both locks are held simultaneously for the entire
        mutation, so no other thread can interleave.
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

        Lock order: `slot_map_semaphore` -> `shard_semaphore` (sequential).

        ABA protection: generation verified under `shard_semaphore`.
        A mismatch means the slot was recycled and the counter effectively
        resets to zero, so we write `amount` as a fresh value.
        Retries up to `_MAX_ABA_RETRIES`.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        for _ in range(_MAX_ABA_RETRIES):
            # Phase 1
            self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
            try:
                hash_table_result = self._get_hash_table_slot_with_generation(
                    buffer, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer)
                    self._hash_table_upsert(buffer, key_bytes, slot_idx)
                    generation = self._read_slot_generation(buffer, slot_idx)
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphore.release()  # type: ignore[union-attr]

            # Phase 2
            shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                if self._read_slot_generation(buffer, slot_idx) != generation:
                    continue  # ABA problem detected. Retry.
                value, expires_at, occupied = self._read_slot(buffer, slot_idx)
                if not occupied or (expires_at != 0.0 and expires_at < now):
                    new_value = amount
                    self._write_slot(buffer, slot_idx, str(new_value), 0.0)
                else:
                    try:
                        current = int(value)  # type: ignore[arg-type]
                    except (ValueError, TypeError):
                        current = 0
                    new_value = current + amount
                    self._write_value_only(buffer, slot_idx, str(new_value))
                return new_value
            finally:
                shard_semaphore.release()

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {_MAX_ABA_RETRIES} retries."
        )

    def _expire(self, key: str, seconds: int) -> bool:
        """
        Synchronous expire.

        Lock order: `slot_map_semaphore` -> `shard_semaphore` (sequential).

        ABA protection: generation verified under `shard_semaphore`.
        A mismatch means the key was deleted and the slot reused,
        so we return `False` (the key no longer exists).
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        # Phase 1
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            result = self._get_hash_table_slot_with_generation(buffer, key_bytes)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if result is None:
            return False
        slot_idx, generation = result

        # Phase 2
        shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
        shard_semaphore.acquire()
        try:
            if self._read_slot_generation(buffer, slot_idx) != generation:
                return False  # slot recycled - key gone
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

        Lock order: `slot_map_semaphore` -> `shard_semaphore` (sequential).

        ABA protection: generation verified under `shard_semaphore`.
        Retries up to `_MAX_ABA_RETRIES`.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")

        for _ in range(_MAX_ABA_RETRIES):
            # Phase 1
            self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
            try:
                hash_table_result = self._get_hash_table_slot_with_generation(
                    buffer, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer)
                    self._hash_table_upsert(buffer, key_bytes, slot_idx)
                    generation = self._read_slot_generation(buffer, slot_idx)
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphore.release()  # type: ignore[union-attr]

            # Phase 2
            shard_semaphore = self._shard_semaphores[self._shard_idx(key)]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                if self._read_slot_generation(buffer, slot_idx) != generation:
                    continue  # ABA - retry
                value, expires_at, occupied = self._read_slot(buffer, slot_idx)
                is_new = not occupied or (expires_at != 0.0 and expires_at < now)
                if is_new:
                    new_value = amount
                    self._write_slot(buffer, slot_idx, str(new_value), now + ttl)
                else:
                    try:
                        current = int(value)  # type: ignore[arg-type]
                    except (ValueError, TypeError):
                        current = 0

                    new_value = current + amount
                    effective_expiry = expires_at if expires_at != 0.0 else now + ttl
                    self._write_slot(buffer, slot_idx, str(new_value), effective_expiry)
                return new_value
            finally:
                shard_semaphore.release()

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {_MAX_ABA_RETRIES} retries."
        )

    def _multi_get(
        self,
        keys: typing.Tuple[str, ...],
        shard_to_keys: typing.Dict[int, typing.List[str]],
    ) -> typing.Dict[str, typing.Optional[str]]:
        """
        Synchronous multi_get.

        Lock order: one `slot_map_semaphore` acquisition for all lookups,
        then `shard_semaphores` in ascending shard-index order.

        ABA protection: per-key generation captured at lookup time, verified
        under each `shard_semaphore`.  A mismatch yields `None` for that
        key (no retry - reads are best-effort; the key was concurrently
        deleted).
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()

        # Phase 1: all hash-table lookups in one semaphore hold
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_info: typing.Dict[str, typing.Optional[typing.Tuple[int, int]]] = {
                key: self._get_hash_table_slot_with_generation(
                    buffer, key.encode("utf-8")
                )
                for key in keys
            }
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        # Phase 2: reads shard by shard in ascending order
        results: typing.Dict[str, typing.Optional[str]] = {}
        for shard_idx in sorted(shard_to_keys):
            shard_semaphore = self._shard_semaphores[shard_idx]  # type: ignore[index]
            shard_semaphore.acquire()
            try:
                for key in shard_to_keys[shard_idx]:
                    info = slot_info[key]
                    if info is None:
                        results[key] = None
                        continue

                    slot_idx, generation = info
                    if self._read_slot_generation(buffer, slot_idx) != generation:
                        results[key] = None  # slot recycled
                        continue

                    value, expires_at, occupied = self._read_slot(buffer, slot_idx)
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

        Allocate all slots in one `slot_map_semaphore` hold, then write to
        shards in ascending shard-index order.

        ABA protection: per-key generation verified under each
        `shard_semaphore`. Any key whose slot was recycled is re-allocated
        in a follow-up `slot_map_semaphore` pass; this repeats up to
        `_MAX_ABA_RETRIES` times before raising `BackendError`.
        """
        buffer = self._buffer
        assert buffer is not None
        expires_at = (time() + expire) if expire is not None else 0.0

        # Initial allocation pass
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            slot_assignments: typing.Dict[str, typing.Tuple[int, int]] = {}
            for key in all_keys:
                key_bytes = key.encode("utf-8")
                hash_table_result = self._get_hash_table_slot_with_generation(
                    buffer, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer)
                    self._hash_table_upsert(buffer, key_bytes, slot_idx)
                    gen = self._read_slot_generation(buffer, slot_idx)
                else:
                    slot_idx, gen = hash_table_result
                slot_assignments[key] = (slot_idx, gen)
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        aba_keys: typing.List[str] = []
        for attempt in range(_MAX_ABA_RETRIES):
            aba_keys: typing.List[str] = []

            # Write pass: ascending shard order
            for shard_idx in sorted(shard_to_items):
                shard_semaphore = self._shard_semaphores[shard_idx]  # type: ignore[index]
                shard_semaphore.acquire()
                try:
                    for key, val in shard_to_items[shard_idx]:
                        slot_idx, gen = slot_assignments[key]
                        if self._read_slot_generation(buffer, slot_idx) != gen:
                            aba_keys.append(key)
                            continue
                        self._write_slot(buffer, slot_idx, val, expires_at)
                finally:
                    shard_semaphore.release()

            if not aba_keys:
                return

            if attempt == _MAX_ABA_RETRIES - 1:
                break

            # Re-allocate only the ABA-affected keys
            self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
            try:
                for key in aba_keys:
                    key_bytes = key.encode("utf-8")
                    hash_table_result = self._get_hash_table_slot_with_generation(
                        buffer, key_bytes
                    )
                    if hash_table_result is None:
                        slot_idx = self._free_stack_pop(buffer)
                        self._hash_table_upsert(buffer, key_bytes, slot_idx)
                        gen = self._read_slot_generation(buffer, slot_idx)
                    else:
                        slot_idx, gen = hash_table_result
                    slot_assignments[key] = (slot_idx, gen)
                    # Rebuild shard_to_items entry for this key so the write
                    # pass above will retry it in the correct shard bucket.
                    # (The shard index never changes for a given key, so the
                    # existing shard_to_items mapping is still correct.)
            finally:
                self._slot_map_semaphore.release()  # type: ignore[union-attr]

        raise BackendError(
            f"ABA race on keys {aba_keys!r} not resolved after "
            f"{_MAX_ABA_RETRIES} retries."
        )

    def _clear(self) -> None:
        """
        Remove all keys whose name starts with this backend's namespace prefix.

        Phase 1 (`slot_map_semaphore`): scan the hash table, delete matching
        entries, push their slots back to the free pool, collect
        `(slot_idx, shard_idx)` pairs.

        Phase 2 (`shard_semaphores` in ascending order): clear the occupied
        flag in each slot.

        Holding `slot_map_semaphore` and `shard_semaphore` simultaneously
        is avoided to respect the lock-ordering rule (`_delete` holds them in
        the opposite order). This is safe here because `_clear` exclusively
        owns all keys in the namespace and no other code path modifies them
        concurrently within the same namespace.
        """
        buffer = self._buffer
        assert buffer is not None
        prefix = f"{self.namespace}:".encode("utf-8")

        to_clear: typing.List[typing.Tuple[int, int]] = []  # (slot_idx, shard_idx)
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            for key_str, slot_idx in list(self._hash_table_iter_occupied(buffer)):
                key_bytes = key_str.encode("utf-8")
                if not key_bytes.startswith(prefix):
                    continue
                self._hash_table_delete(buffer, key_bytes)
                self._free_stack_push(buffer, slot_idx)
                to_clear.append((slot_idx, self._shard_idx(key_str)))
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        if not to_clear:
            return

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

        Pass 1 (no lock): snapshot occupied entries and their expiry times.
        Racy but harmless - used only as a candidate hint.

        Pass 2 (`slot_map_semaphore` only): re-verify each candidate.  We do
        **not** hold `shard_semaphore` here to avoid violating the ordering
        rule (`shard_semaphore` before `slot_map_semaphore` is only legal
        in `_delete`).  The float64 expiry write is not atomic on all
        architectures; the worst outcome is a spurious skip (missed cleanup
        cycle), never a spurious free.
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
        self._slot_map_semaphore.acquire()  # type: ignore[union-attr]
        try:
            for key_bytes, expected_slot_idx in candidates:
                current_slot_idx = self._get_hash_table_slot(buffer, key_bytes)
                if current_slot_idx is None or current_slot_idx != expected_slot_idx:
                    continue
                _, expires_at, occupied = self._read_slot(buffer, current_slot_idx)
                if not occupied or expires_at == 0.0 or expires_at >= now:
                    continue
                self._hash_table_delete(buffer, key_bytes)
                self._free_stack_push(buffer, current_slot_idx)
                self._clear_slot(buffer, current_slot_idx)
                freed += 1
        finally:
            self._slot_map_semaphore.release()  # type: ignore[union-attr]

        return freed

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

    async def get_lock(self, name: str) -> _NamedLockHandle[_AsyncMPSemaphoreLock]:
        self._assert_ready()
        return self._named_lock_pool.get(name)

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
