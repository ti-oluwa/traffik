"""
Multi-process in-memory throttle backend using shared memory.

Designed for multi-worker single-machine deployments (e.g. gunicorn/uvicorn
with multiple workers) where Redis/Memcached is unavailable or undesirable,
but a single `InMemoryBackend` per process would result in each worker
maintaining independent, incorrect rate limit counters.

All state lives in a single `multiprocessing.shared_memory` segment divided
into *N* independent shards. Each shard owns its own hash table, free-slot
stack, and slot data, protected by its own pair of semaphores.

Slots can store either a raw int64 counter or a variable-length UTF-8 string.
The hot `increment_with_ttl` path reads and writes the int64 field directly,
avoiding all string encoding and parsing overhead.

Locking uses `multiprocessing.Semaphore` objects that must be created
**before** fork and inherited by all worker processes through the normal Unix
fork mechanism.

**Platform requirement**

This backend requires the `fork` or `forkserver` multiprocessing start method.
It will raise `RuntimeError` on Windows or when the start method is `spawn`
(the macOS default since Python 3.8). Linux with the default `fork` start
method is the primary supported platform.

**Shared memory naming**

Every instance owns (or attaches to) a named POSIX shared memory segment.
The name is either supplied explicitly via `shared_memory_name` or derived
automatically from the namespace. Names must:

- Contain only `[A-Za-z0-9_-]`
- Be at most 30 characters (the OS prepends `/`, and macOS caps at 31)
- Be non-empty

**Create / attach pattern**

Use the `create` and `attach` class methods (or the `create` bool constructor
argument) to be explicit about ownership:

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
import math
import multiprocessing
import re
import struct
import sys
import threading
import typing
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.shared_memory import SharedMemory
from multiprocessing.synchronize import Semaphore
from time import monotonic
from types import TracebackType

from typing_extensions import Self

from traffik._locks import _NamedLockHandle, _NamedLockPool
from traffik.backends._ext import (  # type: ignore[import]
    clear_byte,
    fnv_32bit_hash,
    test_and_set_byte,
)
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import (
    BackendConnectionError,
    BackendError,
    LockAcquisitionError,
    LockReleaseError,
)
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)
from traffik.utils import time

__all__ = ["MultiProcessInMemoryBackend"]


_HASH_TABLE_LOAD_FACTOR: float = 0.65
_HASH_TABLE_EMPTY_STATE: int = 0
_HASH_TABLE_OCCUPIED_STATE: int = 1
_HASH_TABLE_TOMBSTONE_STATE: int = 2
_KEY_MAX_BYTES: int = 200

_HASH_TABLE_STATE_SIZE: int = 1
_HASH_TABLE_KEY_VALUE_LENGTH_SIZE: int = 1
_HASH_TABLE_SLOT_IDX_SIZE: int = 4
_HASH_TABLE_ENTRY_SIZE: int = (
    _HASH_TABLE_STATE_SIZE
    + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE
    + _KEY_MAX_BYTES
    + _HASH_TABLE_SLOT_IDX_SIZE
)

_HASH_TABLE_STATE_OFFSET: int = 0
_HASH_TABLE_KEY_LENGTH_OFFSET: int = _HASH_TABLE_STATE_SIZE
_HASH_TABLE_KEY_OFFSET: int = _HASH_TABLE_STATE_SIZE + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE
_HASH_TABLE_SLOT_IDX_OFFSET: int = (
    _HASH_TABLE_STATE_SIZE + _HASH_TABLE_KEY_VALUE_LENGTH_SIZE + _KEY_MAX_BYTES
)

_HEADER_FREE_COUNT_SIZE: int = 4
_HEADER_FREE_COUNT_OFFSET: int = 0

_SHARED_MEMORY_NAME_MAX_LENGTH: int = 30
_SHARED_MEMORY_NAME_PREFIX: str = "traffik_"
_SHARED_MEMORY_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_STRING_SLOT_KIND: int = 0
_INT_SLOT_KIND: int = 1

_UINT8_STRUCT = struct.Struct("=B")
_UINT16_STRUCT = struct.Struct("=H")
_UINT32_STRUCT = struct.Struct("=I")
_INT64_STRUCT = struct.Struct("=q")
_FLOAT64_STRUCT = struct.Struct("=d")
_BOOL_STRUCT = struct.Struct("=?")


def _derive_shared_memory_name(namespace: str) -> str:
    """
    Derive a valid shared memory segment name from *namespace*.

    Replaces characters outside `[A-Za-z0-9_-]` with underscores, appends
    an 8-character FNV-1a hex suffix for collision-resistance, and prefixes
    with `_SHARED_MEMORY_NAME_PREFIX`. The result is truncated to
    `_SHARED_MEMORY_NAME_MAX_LENGTH` characters.

    :param namespace: The backend namespace string.
    :return: A valid POSIX shared memory segment name.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", namespace)
    hex_suffix = format(fnv_32bit_hash(namespace.encode("utf-8")), "08x")
    middle_max = (
        _SHARED_MEMORY_NAME_MAX_LENGTH
        - len(_SHARED_MEMORY_NAME_PREFIX)
        - 1
        - len(hex_suffix)
    )
    middle_max = max(middle_max, 1)
    sanitized = sanitized[:middle_max]
    return f"{_SHARED_MEMORY_NAME_PREFIX}{sanitized}_{hex_suffix}"


def _validate_shared_memory_name(name: str) -> None:
    """
    Raise `ValueError` if *name* is not a legal shared memory segment name.

    :param name: The candidate shared memory name.
    :raises ValueError: If the name is empty, too long, or contains invalid characters.
    """
    if not name:
        raise ValueError("`shared_memory_name` must not be empty.")
    if len(name) > _SHARED_MEMORY_NAME_MAX_LENGTH:
        raise ValueError(
            f"`shared_memory_name` must be at most {_SHARED_MEMORY_NAME_MAX_LENGTH} "
            f"characters (got {len(name)} chars). "
            "macOS caps POSIX shared memory names at 31 characters including the "
            "leading '/' the OS prepends."
        )
    if not _SHARED_MEMORY_NAME_ALLOWED_RE.match(name):
        raise ValueError(
            f"`shared_memory_name` {name!r} contains invalid characters. "
            "Only [A-Za-z0-9_-] are allowed."
        )


class _SharedMemoryLockBytePool:
    """
    Thread-safe allocator that hands out byte offsets within a region of
    shared memory for use as lock flags.

    The pool is backed by a simple integer free-stack protected by a
    `threading.Lock`. It lives entirely in the parent process's Python
    heap; worker processes inherit a private copy after fork and therefore
    each have their own independent allocator state because each worker independently
    creates its own `_AsyncSharedMemoryLock` instances via `_NamedLockPool`.
    """

    __slots__ = ("_base", "_size", "_free", "_lock")

    def __init__(self, base_offset: int, size: int) -> None:
        """
        Initialize the byte pool.

        :param base_offset: Byte offset within the shared memory buffer where the lock-byte
            region begins. The caller is responsible for reserving
            `size` bytes at this offset in the shared memory layout.
        :param size: Total number of lock bytes available. Must be >= the peak
            number of simultaneously live `_AsyncSharedMemoryLock` instances
            (i.e. >= `lock_pool_size` plus headroom for over-limit
            instances created under traffic spikes).
        """
        if size <= 0:
            raise ValueError("`size` must be positive")

        self._base = base_offset
        self._size = size
        # Pre-populate free stack with every index
        self._free: typing.List[int] = list(range(base_offset, base_offset + size))
        self._lock = threading.Lock()

    def acquire_index(self) -> int:
        """
        Pop a free byte index from the pool.

        :raises RuntimeError: If the pool is exhausted. Size the pool with enough headroom
            to avoid this. See class docstring.
        """
        with self._lock:
            if not self._free:
                raise RuntimeError(
                    f"{self.__class__.__name__} exhausted. Increase `lock_pool_size` "
                    "or the byte pool headroom multiplier."
                )
            return self._free.pop()

    def release_index(self, index: int) -> None:
        """Return a byte index to the pool."""
        with self._lock:
            self._free.append(index)

    @property
    def available(self) -> int:
        """Number of byte indices currently available."""
        with self._lock:
            return len(self._free)


class _AsyncSharedMemoryLock:
    """
    Cross-process, asyncio-cooperative lock backed by a single byte in a `multiprocessing.SharedMemory` segment.

    Non-reentrant by default but optionally reentrant per task.

    Reentrancy is process-local only, i.e, the reentry counter is tracked
    in Python and only the outermost acquire/release touches the shared
    memory byte. This means if the byte is cleared externally (e.g. by
    `close()` zeroing the lock region) while reentrant holds are active,
    the lock is silently lost. Keep critical sections short.

    **Acquisition algorithm**:

    1. If the current task already owns the lock and reentrancy is enabled,
       increment the counter and return immediately.
    2. Call `test_and_set_byte` (atomic XCHG).
    3. If old value was 0, we acquired the lock; record task ownership and return `True`.
    4. Otherwise yield to the event loop via `asyncio.sleep(0)`
       (first `max_spins_before_backoff` attempts) or with an
       exponentially increasing delay up to `spin_max_delay_seconds`.
    5. Repeat until acquired, non-blocking return, or timeout.

    The yield in step 4 is a pure cooperative handoff.
    The lock holder (running in the same or a different process)
    will complete its critical section and call `clear_byte`, making
    the byte 0 again so a subsequent `test_and_set_byte` by a waiter succeeds.
    """

    __slots__ = (
        "_buffer",
        "_byte_index",
        "_byte_pool",
        "_owner_task",
        "_reentry_count",
        "_max_spins_before_backoff",
        "_spin_max_delay_seconds",
        "_reentrant",
    )

    def __init__(
        self,
        buffer: memoryview,
        byte_pool: _SharedMemoryLockBytePool,
        max_spins_before_backoff: int = 8,
        spin_max_delay_seconds: float = 0.005,
        reentrant: bool = False,
    ) -> None:
        """
        Initialize the lock.

        :param buffer: Writable `memoryview` over the shared memory segment.
            Must remain valid for the lifetime of this instance.
        :param byte_pool: The `_SharedMemoryLockBytePool` to acquire a byte index from.
            The index is returned to the pool when `discard()` is called.
        :param max_spins_before_backoff: Number of zero-delay yields to the event-loop during acquisition,
            before applying exponential backoff.
        :param spin_max_delay_seconds: Maximum delay in seconds during backoff.
        :param reentrant: Whether to allow the same task to acquire the lock multiple times.
            Reentrancy is process-local only, i.e, the reentry counter is tracked in Python and only
            the outermost acquire/release touches the shared memory byte. Defaults to False.
        """
        self._buffer = buffer
        self._byte_index = byte_pool.acquire_index()
        self._byte_pool = byte_pool
        self._owner_task: typing.Optional[asyncio.Task[typing.Any]] = None
        self._reentry_count: int = 0
        self._reentrant = reentrant
        self._max_spins_before_backoff = max_spins_before_backoff
        self._spin_max_delay_seconds = spin_max_delay_seconds

    def _is_owner(self, task: typing.Optional[asyncio.Task] = None) -> bool:
        """Return True if the current task owns this lock."""
        if task is None:
            task = asyncio.current_task()
        return task is not None and task is self._owner_task

    def byte_state(self) -> int:
        """Return the raw byte state of this lock's flag byte."""
        return self._buffer[self._byte_index]

    def locked(self) -> bool:
        """Return True if the lock is held by any task/process"""
        return self.byte_state() != 0

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the lock.

        :param blocking: If `False`, attempt once and return immediately.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Maximum seconds to wait. `None` means wait indefinitely.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :return: `True` if the lock was acquired, `False` otherwise.
        """
        current = asyncio.current_task()
        if current is None:
            raise LockAcquisitionError(
                "Lock must be acquired from within an asyncio Task."
            )

        # Reentrant. Current task already holds the lock
        if self._is_owner(task=current):
            if not self._reentrant:
                raise LockAcquisitionError(
                    "Lock is already acquired by the current task "
                    "and was not configured as reentrant."
                )
            self._reentry_count += 1
            return True

        start = monotonic()
        attempts = 0
        max_spins = self._max_spins_before_backoff
        spin_max_delay = self._spin_max_delay_seconds
        has_blocking_timeout = blocking_timeout is not None
        while True:
            if test_and_set_byte(self._buffer, self._byte_index) == 0:
                # Old value was 0. We already atomically set it to 1 and own the lock
                self._owner_task = current
                self._reentry_count = 1
                return True

            if not blocking:
                return False

            if has_blocking_timeout and (monotonic() - start) >= blocking_timeout:  # type: ignore
                return False

            if attempts < max_spins:
                # Yield back to event loop to give other tasks a chance to run
                await asyncio.sleep(0)
            else:
                # Backoff to avoid starving the event loop under sustained contention.
                # Capped at `spin_max_delay`.
                delay = min(0.0001 * (1 << min(attempts, 10)), spin_max_delay)
                await asyncio.sleep(delay)

            attempts += 1

    async def release(self) -> None:
        """
        Release the lock once.

        Only when the reentrancy count reaches zero will the underlying shared
        memory byte actually be cleared.

        :raises RuntimeError: If the current task does not own the lock.
        """
        if not self._is_owner():
            current = asyncio.current_task()
            raise LockReleaseError(
                f"Cannot release lock: current task {current!r} does not own "
                f"the lock (owner: {self._owner_task!r})."
            )

        # Reentrant inner release. Just decrement the counter
        if self._reentry_count > 1:
            self._reentry_count -= 1
            return

        # Outermost release. Clear the shared memory byte and ownership together
        try:
            clear_byte(self._buffer, self._byte_index)
        finally:
            # Clear ownership regardless of whether clear_byte succeeded.
            # `clear_byte` is a C extension writing a single byte, so failure here
            # would indicate a severe memory error, but we still clean up state.
            self._owner_task = None
            self._reentry_count = 0

    def discard(self) -> None:
        """
        Return the byte index to `_SharedMemoryLockBytePool`.

        Called by `_NamedLockPool` when an over-limit lock instance is
        discarded rather than returned to the free list. Must not be
        called while the lock is held.
        """
        if self._owner_task is not None:
            # For safety, clear the byte so we don't permanently poison
            # the shared memory flag for future lock instances that
            # receive the same byte index.
            clear_byte(self._buffer, self._byte_index)
            self._owner_task = None
            self._reentry_count = 0
        self._byte_pool.release_index(self._byte_index)

    async def __aenter__(self):
        if not await self.acquire():
            raise LockAcquisitionError("Could not acquire multiprocess lock.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ):
        await self.release()


class MultiProcessInMemoryBackend(ThrottleBackend[None, HTTPConnectionT]):
    """
    Multi-process shared-memory throttle backend with sharded layout and
    native int64 counter storage.

    All state (hash tables, slot data, free-slot stacks) lives in a single
    `multiprocessing.shared_memory` segment divided into *N* independent
    shards. Each shard has its own slot-map semaphore and slot-write semaphore,
    so single-key operations never contend with operations on different shards.

    The slot format stores both a variable-length UTF-8 string value and a
    raw int64 counter in every slot, with a `slot_kind` byte indicating
    which field is active. The `increment` and `increment_with_ttl` hot
    paths read and write the int64 field directly, eliminating all string
    encoding and parsing overhead on the critical path.

    **Platform requirement**

    Requires `fork` or `forkserver` multiprocessing start method.
    Raises `RuntimeError` on Windows or when the start method is `spawn`.

    **Shared memory layout (per shard)**:

    ```
    [shard header]
        uint32  free_count
        uint32  free_stack[max_keys_per_shard]

    [shard hash table]
        shard_hash_table_capacity * ENTRY_SIZE bytes
        entry: uint8 state | uint8 key_length | bytes key[200] | uint32 slot_idx

    [shard slot data]
        max_keys_per_shard * slot_size bytes
        slot: uint32 generation | uint8 slot_kind | uint8 _pad | uint16 value_length
                | bytes value[max_value_size] | int64 int_value
                | float64 expires_at | uint8 occupied
                (padded to 8-byte boundary)
    ```

    **Lock ordering** (must always be respected to avoid deadlock within a shard)

    - `slot_map_semaphores[shard_idx]` before `shard_semaphores[shard_idx]`
    - Never hold two different shards' semaphores simultaneously

    **ABA mitigation**

    Each slot carries a `uint32 generation` counter incremented every time
    the slot is allocated. Operations that release `slot_map_semaphores[shard_idx]`
    before acquiring `shard_semaphores[shard_idx]` capture the generation at lookup
    time and verify it under `shard_semaphores[shard_idx]` before reading or writing.
    A mismatch triggers a retry up to `self._max_aba_retries` times.
    """

    _GENERATION_STRUCT: typing.ClassVar[struct.Struct] = _UINT32_STRUCT
    _SLOT_KIND_STRUCT: typing.ClassVar[struct.Struct] = _UINT8_STRUCT
    _VALUE_LENGTH_STRUCT: typing.ClassVar[struct.Struct] = _UINT16_STRUCT
    _INT_VALUE_STRUCT: typing.ClassVar[struct.Struct] = _INT64_STRUCT
    _EXPIRY_STRUCT: typing.ClassVar[struct.Struct] = _FLOAT64_STRUCT
    _OCCUPIED_FLAG_STRUCT: typing.ClassVar[struct.Struct] = _BOOL_STRUCT

    _GENERATION_SIZE: typing.ClassVar[int] = 4
    _SLOT_KIND_SIZE: typing.ClassVar[int] = 1
    _PAD1_SIZE: typing.ClassVar[int] = 1
    _VALUE_LENGTH_SIZE: typing.ClassVar[int] = 2
    _INT_VALUE_SIZE: typing.ClassVar[int] = 8
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
        lock_pool_size: int = 128,
        lock_pool_headroom: int = 4,
        max_keys: int = 65536,
        number_of_shards: int = 64,
        max_value_size: int = 512,
        cleanup_frequency: typing.Optional[float] = None,
        shared_memory_name: typing.Optional[str] = None,
        create: bool = True,
        max_aba_retries: int = 3,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the backend.

        :param namespace: Key prefix for all throttle keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection
            is throttled.
        :param on_error: Strategy to handle errors during throttling operations.
            One of `"allow"`, `"throttle"`, `"raise"`, or a custom
            callable that takes the connection and exception and returns the
            wait period in milliseconds.
        :param lock_blocking: Whether named locks should block when acquiring.
            `None` uses the global default.
        :param lock_ttl: Default TTL for named locks in seconds.
        :param lock_blocking_timeout: Default maximum wait time for named locks
            in seconds. `None` uses the global default.
        :param lock_pool_size: Maximum number of idle named-lock semaphores
            kept in the free pool.
        :param max_keys: Maximum number of distinct live keys across all shards
            at any one time.
        :param number_of_shards: Number of independent shards. Each shard gets
            `ceil(max_keys / number_of_shards)` key slots, its own hash
            table, free stack, and a pair of semaphores. Higher values reduce
            contention at the cost of more semaphores and memory.
        :param max_value_size: Maximum UTF-8 byte length of a stored string
            value. Counter (int64) slots ignore this limit.
        :param cleanup_frequency: Seconds between background expired-slot
            reclamation passes. `None` disables the background task.
        :param shared_memory_name: Explicit POSIX shared memory segment name.
            Must match `[A-Za-z0-9_-]`, max 30 characters. Derived from
            *namespace* automatically when `None`.
        :param create: When `True` (default), `initialize()` creates and
            owns the shared-memory segment. When `False` it attaches to an existing segment
            created by another instance. Prefer the `create` / `attach` class methods for clarity.
        """
        if sys.platform == "win32" or sys.platform == "cygwin":
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
                f"{start_method!r}. On macOS with Python >= 3.8 the default changed "
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
        if max_aba_retries < 1:
            raise ValueError("`max_aba_retries` must be atleast 1.")

        self._max_keys = max_keys
        self._number_of_shards = number_of_shards
        self._max_value_size = max_value_size
        self._cleanup_frequency = cleanup_frequency
        self._max_keys_per_shard = math.ceil(max_keys / number_of_shards)
        self._max_aba_retries = max_aba_retries

        # Per-shard hash table capacity: next power of two above
        # (max_keys_per_shard / load_factor).
        min_buckets = int(self._max_keys_per_shard / _HASH_TABLE_LOAD_FACTOR) + 1
        shard_hash_table_capacity = 1
        while shard_hash_table_capacity < min_buckets:
            shard_hash_table_capacity <<= 1

        self._shard_hash_table_capacity = shard_hash_table_capacity
        self._shard_hash_table_mask = shard_hash_table_capacity - 1

        # Slot layout (all offsets are relative to the start of a slot):
        #   generation(4) | slot_kind(1) | _pad(1) | value_length(2)
        #   | value[max_value_size] | int_value(8) | expires_at(8) | occupied(1)
        #   + padding to 8-byte boundary
        raw_slot_size = (
            self._GENERATION_SIZE
            + self._SLOT_KIND_SIZE
            + self._PAD1_SIZE
            + self._VALUE_LENGTH_SIZE
            + max_value_size
            + self._INT_VALUE_SIZE
            + self._EXPIRY_SIZE
            + self._OCCUPIED_FLAG_SIZE
        )
        self._slot_size = (raw_slot_size + 7) & ~7

        self._generation_offset = 0
        self._slot_kind_offset = self._GENERATION_SIZE
        # _pad1 at _GENERATION_SIZE + 1, implicit
        self._value_length_offset = (
            self._GENERATION_SIZE + self._SLOT_KIND_SIZE + self._PAD1_SIZE
        )
        self._value_offset = self._value_length_offset + self._VALUE_LENGTH_SIZE
        self._int_value_offset = self._value_offset + max_value_size
        self._expiry_offset = self._int_value_offset + self._INT_VALUE_SIZE
        self._occupied_flag_offset = self._expiry_offset + self._EXPIRY_SIZE

        # Per-shard region sizes:
        #   header: free_count(4) + free_stack(4 * max_keys_per_shard)
        #   hash table: shard_hash_table_capacity * ENTRY_SIZE
        #   slot data: max_keys_per_shard * slot_size
        self._shard_header_size = _HEADER_FREE_COUNT_SIZE + 4 * self._max_keys_per_shard
        self._shard_free_stack_offset = _HEADER_FREE_COUNT_SIZE
        self._shard_hash_table_size = shard_hash_table_capacity * _HASH_TABLE_ENTRY_SIZE
        self._shard_slot_data_size = self._max_keys_per_shard * self._slot_size
        self._shard_size = (
            self._shard_header_size
            + self._shard_hash_table_size
            + self._shard_slot_data_size
        )

        # Offsets within a shard (relative to shard base)
        self._shard_hash_table_base_offset = self._shard_header_size
        self._shard_slot_data_base_offset = (
            self._shard_header_size + self._shard_hash_table_size
        )

        self._shared_memory: typing.Optional[SharedMemory] = None
        self._buffer: typing.Optional[memoryview] = None

        # One slot-map semaphore and one shard (slot-write) semaphore per shard.
        self._slot_map_semaphores: typing.Optional[typing.List[Semaphore]] = None
        self._shard_semaphores: typing.Optional[typing.List[Semaphore]] = None

        self._executor = ThreadPoolExecutor(
            max_workers=max(number_of_shards, 32), thread_name_prefix=namespace
        )
        self._lock_pool_size = lock_pool_size
        self._lock_pool_headroom = lock_pool_headroom
        # Size the byte pool with headroom
        self._lock_byte_pool_size = lock_pool_size * lock_pool_headroom
        self._lock_bytes_offset = self._shard_size * number_of_shards

        self._shared_memory_size = (
            self._shard_size * number_of_shards + self._lock_byte_pool_size
        )

        # Byte pool is created now but the actual `_SharedMemoryLockBytePool` is
        # created in initialize() once we know the shared memory layout
        self._lock_byte_pool: typing.Optional[_SharedMemoryLockBytePool] = None
        self._named_lock_pool: typing.Optional[
            _NamedLockPool[_AsyncSharedMemoryLock]
        ] = None

        self._cleanup_task: typing.Optional[asyncio.Task[None]] = None
        self._initialized: bool = False

    @classmethod
    def create(
        cls,
        *,
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
        **kwargs: typing.Any,
    ) -> Self:
        """
        Create a new backend instance that **owns** the shared memory segment.

        Call `await instance.initialize()` after construction. Only one
        process should call `create()` for a given *shared_memory_name* /
        *namespace* combination. The segment is unlinked when `close()` is
        called on the creator.

        :param namespace: Key prefix for all throttle keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection
            is throttled.
        :param on_error: Strategy to handle errors during throttling. One of
            `"allow"`, `"throttle"`, `"raise"`, or a custom callable.
        :param lock_blocking: Whether named locks block on acquisition.
        :param lock_ttl: Default TTL for named locks in seconds.
        :param lock_blocking_timeout: Default maximum wait time for named locks.
        :param lock_pool_size: Maximum idle named-lock semaphores in the pool.
        :param max_keys: Maximum distinct live keys across all shards.
        :param number_of_shards: Number of independent shards.
        :param max_value_size: Maximum UTF-8 byte length of a stored string.
        :param cleanup_frequency: Seconds between background cleanup passes.
        :param shared_memory_name: Explicit POSIX shared memory segment name.
        :return: A new `MultiProcessInMemoryBackend` configured as creator.
        """
        return cls(
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            lock_pool_size=lock_pool_size,
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
        *,
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
        **kwargs: typing.Any,
    ) -> Self:
        """
        Attach to an **existing** shared memory segment created by another instance.

        The *namespace*, *shared_memory_name*, *max_keys*, *number_of_shards*,
        and *max_value_size* must exactly match those used by the creator, as
        they determine the segment layout. The segment is **not** unlinked when
        `close()` is called on an attaching instance.

        :param namespace: Key prefix — must match the creator'shard_idx value.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection
            is throttled.
        :param on_error: Strategy to handle errors during throttling. One of
            `"allow"`, `"throttle"`, `"raise"`, or a custom callable.
        :param lock_blocking: Whether named locks block on acquisition.
        :param lock_ttl: Default TTL for named locks in seconds.
        :param lock_blocking_timeout: Default maximum wait time for named locks.
        :param lock_pool_size: Maximum idle named-lock semaphores in the pool.
        :param max_keys: Must match the creator'shard_idx value.
        :param number_of_shards: Must match the creator'shard_idx value.
        :param max_value_size: Must match the creator'shard_idx value.
        :param cleanup_frequency: Seconds between background cleanup passes.
        :param shared_memory_name: Must match the creator'shard_idx value.
        :return: A new `MultiProcessInMemoryBackend` configured as attacher.
        """
        return cls(
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            lock_pool_size=lock_pool_size,
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

        Safe to call multiple times; subsequent calls are no-ops.

        When `create=True` (the default), allocates a new named segment,
        zeroes it, and writes each shard's initial free-stack header.

        When `create=False`, opens an existing segment by name. The segment
        must already have been initialised by a creator instance; no zeroing
        or header writing is performed.

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
            buffer[:] = bytes(self._shared_memory_size)
        else:
            shared_memory = SharedMemory(
                create=False,
                name=self._shared_memory_name,
            )
            buffer = memoryview(shared_memory.buf)  # type: ignore[arg-type]

        try:
            self._slot_map_semaphores = [
                multiprocessing.Semaphore(1) for _ in range(self._number_of_shards)
            ]
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
            for shard_idx in range(self._number_of_shards):
                shard_base = shard_idx * self._shard_size
                _UINT32_STRUCT.pack_into(
                    buffer,
                    shard_base + _HEADER_FREE_COUNT_OFFSET,
                    self._max_keys_per_shard,
                )
                for i in range(self._max_keys_per_shard):
                    _UINT32_STRUCT.pack_into(
                        buffer,
                        shard_base + self._shard_free_stack_offset + i * 4,
                        self._max_keys_per_shard - 1 - i,
                    )

        buffer[
            self._lock_bytes_offset : self._lock_bytes_offset
            + self._lock_byte_pool_size
        ] = bytes(self._lock_byte_pool_size)

        # Create the byte pool
        byte_pool = _SharedMemoryLockBytePool(
            base_offset=self._lock_bytes_offset,
            size=self._lock_byte_pool_size,
        )
        self._lock_byte_pool = byte_pool

        if self._named_lock_pool is None or self._named_lock_pool.closed:
            # Create the named lock pool wired to `_AsyncSharedMemoryLock`
            def _make_lock() -> _AsyncSharedMemoryLock:
                nonlocal buffer, byte_pool
                return _AsyncSharedMemoryLock(
                    buffer=buffer,
                    byte_pool=byte_pool,
                    max_spins_before_backoff=10,
                    spin_max_delay_seconds=0.005,
                )

            self._named_lock_pool = _NamedLockPool(
                factory=_make_lock,
                max_size=self._lock_pool_size,
                headroom=self._lock_pool_headroom,
            )

        # Pre-populate named lock pool
        self._named_lock_pool.populate()

        if self._cleanup_frequency is not None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self._initialized = True

    async def ready(self) -> bool:
        """
        Return `True` if the backend has been initialised and the shared
        memory segment is open.
        """
        return (
            self._initialized
            and self._shared_memory is not None
            and self._buffer is not None
        )

    def _assert_ready(self) -> None:
        """
        Raise `BackendConnectionError` if the backend has not been
        initialised.
        """
        if not self._initialized or self._buffer is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def _shard_idx_for_key(self, key: str) -> int:
        """
        Return the shard index for *key*.

        Uses FNV-1a so the result is deterministic across processes and does
        not rely on Python's randomised `hash()`.

        :param key: The throttle key string.
        :return: Shard index in `[0, number_of_shards)`.
        """
        return fnv_32bit_hash(key.encode("utf-8")) % self._number_of_shards

    def _shard_base(self, shard_idx: int) -> int:
        """
        Return the byte offset in the shared memory buffer at which shard
        *shard_idx* begins.

        :param shard_idx: The shard index.
        :return: Byte offset of the shard's start within the buffer.
        """
        return shard_idx * self._shard_size

    def _hash_table_lookup(
        self, buffer: memoryview, shard_base: int, key_bytes: bytes
    ) -> int:
        """
        Return the bucket index within shard *shard_base*'shard_idx hash table for
        *key_bytes*.

        Returns the index of the occupied bucket containing this key, **or**
        the index of the first tombstone / empty bucket where it could be
        inserted. The caller must inspect the bucket state to distinguish.

        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param key_bytes: UTF-8 encoded key bytes.
        :return: Bucket index within the shard's hash table.
        :raises BackendError: If the hash table is completely full (should
            never happen when `max_keys_per_shard` is respected).
        """
        hash_table_base = shard_base + self._shard_hash_table_base_offset
        capacity = self._shard_hash_table_capacity
        mask = self._shard_hash_table_mask
        start = fnv_32bit_hash(key_bytes) & mask
        first_tombstone = -1

        for i in range(capacity):
            idx = (start + i) & mask
            entry_offset = hash_table_base + idx * _HASH_TABLE_ENTRY_SIZE
            state = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
            )[0]

            if state == _HASH_TABLE_EMPTY_STATE:
                return first_tombstone if first_tombstone != -1 else idx

            if state == _HASH_TABLE_TOMBSTONE_STATE:
                if first_tombstone == -1:
                    first_tombstone = idx
                continue

            key_length = _UINT8_STRUCT.unpack_from(
                buffer, entry_offset + _HASH_TABLE_KEY_LENGTH_OFFSET
            )[0]
            if key_length == len(key_bytes):
                key_start = entry_offset + _HASH_TABLE_KEY_OFFSET
                stored = bytes(buffer[key_start : key_start + key_length])
                if stored == key_bytes:
                    return idx

        raise BackendError("Hash table is full. This should never happen.")

    def _hash_table_get_slot(
        self, buffer: memoryview, shard_base: int, key_bytes: bytes
    ) -> typing.Optional[int]:
        """
        Return the slot index for *key_bytes* within the shard, or `None`
        if the key is not present.

        Must be called with the shard's `slot_map_semaphore` held.
        Use `_hash_table_get_slot_with_generation` when ABA protection is needed.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param key_bytes: UTF-8 encoded key bytes.
        :return: Slot index within the shard, or `None`.
        """
        hash_table_base = shard_base + self._shard_hash_table_base_offset
        idx = self._hash_table_lookup(buffer, shard_base, key_bytes)
        entry_offset = hash_table_base + idx * _HASH_TABLE_ENTRY_SIZE
        state = _UINT8_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
        )[0]
        if state != _HASH_TABLE_OCCUPIED_STATE:
            return None

        return _UINT32_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET
        )[0]

    def _hash_table_get_slot_with_generation(
        self, buffer: memoryview, shard_base: int, key_bytes: bytes
    ) -> typing.Optional[typing.Tuple[int, int]]:
        """
        Return `(slot_idx, generation)` for *key_bytes* within the shard,
        or `None` if the key is not present.

        The generation is read from the slot data region so it reflects the
        current allocation epoch. Callers save this value and compare it under
        `shard_semaphore` to detect ABA slot recycling.

        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param key_bytes: UTF-8 encoded key bytes.
        :return: `(slot_idx, generation)` tuple, or `None`.
        """
        hash_table_base = shard_base + self._shard_hash_table_base_offset
        idx = self._hash_table_lookup(buffer, shard_base, key_bytes)
        entry_offset = hash_table_base + idx * _HASH_TABLE_ENTRY_SIZE
        state = _UINT8_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_STATE_OFFSET
        )[0]
        if state != _HASH_TABLE_OCCUPIED_STATE:
            return None

        slot_idx = _UINT32_STRUCT.unpack_from(
            buffer, entry_offset + _HASH_TABLE_SLOT_IDX_OFFSET
        )[0]
        generation = self._read_slot_generation(buffer, shard_base, slot_idx)
        return slot_idx, generation

    def _hash_table_upsert(
        self,
        buffer: memoryview,
        shard_base: int,
        key_bytes: bytes,
        slot_idx: int,
    ) -> None:
        """
        Insert or update *key_bytes* -> *slot_idx* in the shard's hash table.

        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param key_bytes: UTF-8 encoded key bytes.
        :param slot_idx: Slot index to associate with the key.
        :raises BackendError: If the key exceeds `_KEY_MAX_BYTES`.
        """
        if len(key_bytes) > _KEY_MAX_BYTES:
            raise BackendError(
                f"Key length {len(key_bytes)} exceeds maximum {_KEY_MAX_BYTES} bytes."
            )

        hash_table_base = shard_base + self._shard_hash_table_base_offset
        idx = self._hash_table_lookup(buffer, shard_base, key_bytes)
        entry_offset = hash_table_base + idx * _HASH_TABLE_ENTRY_SIZE
        key_length = len(key_bytes)
        _UINT8_STRUCT.pack_into(
            buffer,
            entry_offset + _HASH_TABLE_STATE_OFFSET,
            _HASH_TABLE_OCCUPIED_STATE,
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
        self, buffer: memoryview, shard_base: int, key_bytes: bytes
    ) -> typing.Optional[int]:
        """
        Remove *key_bytes* from the shard's hash table by marking its bucket
        as a tombstone.

        Returns the freed slot index, or `None` if the key was not present.
        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param key_bytes: UTF-8 encoded key bytes.
        :return: The freed slot index, or `None`.
        """
        hash_table_base = shard_base + self._shard_hash_table_base_offset
        idx = self._hash_table_lookup(buffer, shard_base, key_bytes)
        entry_offset = hash_table_base + idx * _HASH_TABLE_ENTRY_SIZE
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
        self, buffer: memoryview, shard_base: int
    ) -> typing.Iterator[typing.Tuple[str, int]]:
        """
        Iterate over all occupied `(key_str, slot_idx)` pairs in the shard.

        The caller is responsible for holding the shard's
        `slot_map_semaphore` if mutations must not interleave.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :return: Iterator of `(key_str, slot_idx)` pairs.
        """
        hash_table_base = shard_base + self._shard_hash_table_base_offset
        for i in range(self._shard_hash_table_capacity):
            entry_offset = hash_table_base + i * _HASH_TABLE_ENTRY_SIZE
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

    def _free_stack_pop(self, buffer: memoryview, shard_base: int) -> int:
        """
        Pop and return a free slot index from the shard's free stack,
        incrementing the slot'shard_idx generation counter.

        The generation increment is the core of ABA-problem mitigation. Every
        re-allocation changes the slot'shard_idx generation, making stale references
        detectable.

        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :return: A free slot index within the shard.
        :raises BackendError: If the shard's free stack is empty.
        """
        count = _UINT32_STRUCT.unpack_from(
            buffer, shard_base + _HEADER_FREE_COUNT_OFFSET
        )[0]
        if count == 0:
            raise BackendError(
                f"`max_keys_per_shard` ({self._max_keys_per_shard}) reached for this "
                "shard. Increase `max_keys` or `number_of_shards`."
            )

        count -= 1
        slot_idx = _UINT32_STRUCT.unpack_from(
            buffer,
            shard_base + self._shard_free_stack_offset + count * 4,
        )[0]
        _UINT32_STRUCT.pack_into(buffer, shard_base + _HEADER_FREE_COUNT_OFFSET, count)
        self._bump_slot_generation(buffer, shard_base, slot_idx)
        return slot_idx

    def _free_stack_push(
        self, buffer: memoryview, shard_base: int, slot_idx: int
    ) -> None:
        """
        Return *slot_idx* to the shard's free pool.

        The generation is **not** bumped here; that happens on the next pop
        (allocation), which is the moment a new owner takes the slot.

        Must be called with the shard's `slot_map_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: The slot index to return to the free pool.
        """
        count = _UINT32_STRUCT.unpack_from(
            buffer, shard_base + _HEADER_FREE_COUNT_OFFSET
        )[0]
        _UINT32_STRUCT.pack_into(
            buffer,
            shard_base + self._shard_free_stack_offset + count * 4,
            slot_idx,
        )
        _UINT32_STRUCT.pack_into(
            buffer, shard_base + _HEADER_FREE_COUNT_OFFSET, count + 1
        )

    def _slot_offset(self, shard_base: int, slot_idx: int) -> int:
        """
        Return the absolute byte offset of slot *slot_idx* within the shard.

        :param shard_base: Byte offset of the shard's start in the buffer.
        :param slot_idx: Slot index within the shard.
        :return: Absolute byte offset of the slot.
        """
        return (
            shard_base + self._shard_slot_data_base_offset + slot_idx * self._slot_size
        )

    def _read_slot_generation(
        self, buffer: memoryview, shard_base: int, slot_idx: int
    ) -> int:
        """
        Return the current generation counter for *slot_idx* in the shard.

        May be called with either the shard's `slot_map_semaphore` or its
        `shard_semaphore` held; both are valid read contexts.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        :return: The slot'shard_idx current generation counter value.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        return _UINT32_STRUCT.unpack_from(buffer, offset + self._generation_offset)[0]

    def _bump_slot_generation(
        self, buffer: memoryview, shard_base: int, slot_idx: int
    ) -> None:
        """
        Increment the generation counter for *slot_idx*, wrapping at 2³².

        Must be called with the shard's `slot_map_semaphore` held. Called
        exclusively from `_free_stack_pop`.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        current = _UINT32_STRUCT.unpack_from(buffer, offset + self._generation_offset)[
            0
        ]
        _UINT32_STRUCT.pack_into(
            buffer,
            offset + self._generation_offset,
            (current + 1) & 0xFFFFFFFF,
        )

    def _read_slot(
        self,
        buffer: memoryview,
        shard_base: int,
        slot_idx: int,
    ) -> typing.Tuple[typing.Optional[str], typing.Optional[int], float, bool]:
        """
        Read a slot and return `(str_value, int_value, expires_at, occupied)`.

        Exactly one of *str_value* and *int_value* is non-`None` when the
        slot is occupied, determined by the `slot_kind` field.

        Must be called with the shard's `shard_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        :return: `(str_value, int_value, expires_at, occupied)` where
            *str_value* is set when `slot_kind == _STRING_SLOT_KIND` and
            *int_value* is set when `slot_kind == _INT_SLOT_KIND`.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        occupied: bool = self._OCCUPIED_FLAG_STRUCT.unpack_from(
            buffer, offset + self._occupied_flag_offset
        )[0]
        if not occupied:
            return None, None, 0.0, False

        expires_at: float = self._EXPIRY_STRUCT.unpack_from(
            buffer, offset + self._expiry_offset
        )[0]
        slot_kind = _UINT8_STRUCT.unpack_from(buffer, offset + self._slot_kind_offset)[
            0
        ]

        if slot_kind == _INT_SLOT_KIND:
            int_value = _INT64_STRUCT.unpack_from(
                buffer, offset + self._int_value_offset
            )[0]
            return None, int_value, expires_at, True

        value_length = self._VALUE_LENGTH_STRUCT.unpack_from(
            buffer, offset + self._value_length_offset
        )[0]
        str_value = bytes(
            buffer[
                offset + self._value_offset : offset + self._value_offset + value_length
            ]
        ).decode("utf-8")
        return str_value, None, expires_at, True

    def _write_string_slot(
        self,
        buffer: memoryview,
        shard_base: int,
        slot_idx: int,
        value: str,
        expires_at: float,
    ) -> None:
        """
        Write a string value into *slot_idx*, setting `slot_kind` to
        `_STRING_SLOT_KIND` and `occupied` to `True`.

        Does **not** touch the generation counter.
        Must be called with the shard's `shard_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        :param value: The string value to store.
        :param expires_at: Expiry timestamp (seconds since epoch). `0.0`
            means no expiry.
        :raises BackendError: If the encoded value exceeds `max_value_size`.
        """
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_size:
            raise BackendError(
                f"Value size {len(encoded)} bytes exceeds `max_value_size` "
                f"({self._max_value_size}). Increase `max_value_size`."
            )

        offset = self._slot_offset(shard_base, slot_idx)
        _UINT8_STRUCT.pack_into(
            buffer, offset + self._slot_kind_offset, _STRING_SLOT_KIND
        )
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

    def _write_int_slot(
        self,
        buffer: memoryview,
        shard_base: int,
        slot_idx: int,
        int_value: int,
        expires_at: float,
    ) -> None:
        """
        Write an int64 counter into *slot_idx*, setting `slot_kind` to
        `_INT_SLOT_KIND` and `occupied` to `True`.

        Does **not** touch the generation counter.
        Must be called with the shard's `shard_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        :param int_value: The integer counter value to store.
        :param expires_at: Expiry timestamp (seconds since epoch). `0.0`
            means no expiry.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        _UINT8_STRUCT.pack_into(buffer, offset + self._slot_kind_offset, _INT_SLOT_KIND)
        _INT64_STRUCT.pack_into(buffer, offset + self._int_value_offset, int_value)
        self._EXPIRY_STRUCT.pack_into(buffer, offset + self._expiry_offset, expires_at)
        self._OCCUPIED_FLAG_STRUCT.pack_into(
            buffer, offset + self._occupied_flag_offset, True
        )

    def _write_int_value_only(
        self,
        buffer: memoryview,
        shard_base: int,
        slot_idx: int,
        int_value: int,
    ) -> None:
        """
        Overwrite only the int64 counter field, preserving generation, expiry,
        and occupied flag. Sets `slot_kind` to `_INT_SLOT_KIND`.

        Use this on the fast path when the slot already exists and only the
        counter value changes.

        Must be called with the shard's `shard_semaphore` held.

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        :param int_value: The new integer counter value.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        _UINT8_STRUCT.pack_into(buffer, offset + self._slot_kind_offset, _INT_SLOT_KIND)
        _INT64_STRUCT.pack_into(buffer, offset + self._int_value_offset, int_value)

    def _clear_slot(self, buffer: memoryview, shard_base: int, slot_idx: int) -> None:
        """
        Mark *slot_idx* as unoccupied.

        Does not zero value bytes or reset the generation; that happens on
        the next allocation.

        Must be called with the shard's `shard_semaphore` held (or under
        the shard's `slot_map_semaphore` in `_cleanup` where the ordering
        exception is documented).

        :param buffer: The shared memory buffer view.
        :param shard_base: Byte offset of the shard's start in *buffer*.
        :param slot_idx: Slot index within the shard.
        """
        offset = self._slot_offset(shard_base, slot_idx)
        self._OCCUPIED_FLAG_STRUCT.pack_into(
            buffer, offset + self._occupied_flag_offset, False
        )

    def _get(self, key: str) -> typing.Optional[str]:
        """
        Synchronous get.

        Lock order: `slot_map_semaphores[shard_idx]` then `shard_semaphores[shard_idx]`
        (sequential, never overlapping).

        ABA protection: the generation captured at hash-table lookup time is
        verified under `shard_semaphores[shard_idx]`. A mismatch means the slot was
        recycled between the two lock acquisitions; the key is considered gone
        and `None` is returned.

        For int-kind slots the stored int64 is returned as its decimal string
        representation to satisfy the `Optional[str]` contract.

        :param key: The throttle key.
        :return: The stored value as a string, or `None` if absent or expired.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
        try:
            result = self._hash_table_get_slot_with_generation(
                buffer, shard_base, key_bytes
            )
        finally:
            self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

        if result is None:
            return None

        slot_idx, generation = result

        self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
        try:
            if self._read_slot_generation(buffer, shard_base, slot_idx) != generation:
                return None

            str_val, int_val, expires_at, occupied = self._read_slot(
                buffer, shard_base, slot_idx
            )
            if not occupied:
                return None
            if expires_at != 0.0 and expires_at < now:
                return None

            return str(int_val) if int_val is not None else str_val
        finally:
            self._shard_semaphores[shard_idx].release()  # type: ignore[index]

    def _set(self, key: str, value: str, expire: typing.Optional[float]) -> None:
        """
        Synchronous set.

        Always stores the value as a string slot (`slot_kind = STRING`),
        overwriting any existing int slot for the same key.

        Lock order: `slot_map_semaphores[shard_idx]` then `shard_semaphores[shard_idx]`
        (sequential). ABA retry up to `self._max_aba_retries`.

        :param key: The throttle key.
        :param value: The string value to store.
        :param expire: TTL in seconds from now, or `None` for no expiry.
        :raises BackendError: If the ABA retry limit is exceeded or the value
            is too large.
        """
        buffer = self._buffer
        assert buffer is not None
        key_bytes = key.encode("utf-8")
        expires_at = (time() + expire) if expire is not None else 0.0
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        for _ in range(self._max_aba_retries):
            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                hash_table_result = self._hash_table_get_slot_with_generation(
                    buffer, shard_base, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer, shard_base)
                    self._hash_table_upsert(buffer, shard_base, key_bytes, slot_idx)
                    generation = self._read_slot_generation(
                        buffer, shard_base, slot_idx
                    )
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

            self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                if (
                    self._read_slot_generation(buffer, shard_base, slot_idx)
                    != generation
                ):
                    continue

                self._write_string_slot(buffer, shard_base, slot_idx, value, expires_at)
                return
            finally:
                self._shard_semaphores[shard_idx].release()  # type: ignore[index]

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {self._max_aba_retries} retries."
        )

    def _delete(self, key: str) -> bool:
        """
        Synchronous delete.

        Lock order: `shard_semaphores[shard_idx]` first, then
        `slot_map_semaphores[shard_idx]`. This is the documented exception to the
        normal ordering rule and is safe here because `_delete` never calls
        `_free_stack_pop`. Holding the shard semaphore while clearing the
        slot prevents a concurrent reader from seeing the slot as occupied
        after it has been returned to the free pool.

        No ABA retry needed: both locks are held simultaneously for the entire
        mutation.

        :param key: The throttle key to delete.
        :return: `True` if the key existed and was deleted, `False` otherwise.
        """
        buffer = self._buffer
        assert buffer is not None
        key_bytes = key.encode("utf-8")
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
        try:
            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                slot_idx = self._hash_table_delete(buffer, shard_base, key_bytes)
                if slot_idx is None:
                    return False

                self._clear_slot(buffer, shard_base, slot_idx)
                self._free_stack_push(buffer, shard_base, slot_idx)
                return True
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]
        finally:
            self._shard_semaphores[shard_idx].release()  # type: ignore[index]

    def _increment(self, key: str, amount: int) -> int:
        """
        Synchronous increment using native int64 storage.

        Lock order: `slot_map_semaphores[shard_idx]` -> `shard_semaphores[shard_idx]`
        (sequential). ABA retry up to `self._max_aba_retries`.

        If the existing slot is a string kind, it is parsed as an integer.
        The result is stored back as an int kind slot, converting the slot
        type transparently.

        :param key: The throttle key.
        :param amount: The increment amount (may be negative for decrement).
        :return: The new counter value after incrementing.
        :raises BackendError: If the ABA retry limit is exceeded.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        for _ in range(self._max_aba_retries):
            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                hash_table_result = self._hash_table_get_slot_with_generation(
                    buffer, shard_base, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer, shard_base)
                    self._hash_table_upsert(buffer, shard_base, key_bytes, slot_idx)
                    generation = self._read_slot_generation(
                        buffer, shard_base, slot_idx
                    )
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

            self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                if (
                    self._read_slot_generation(buffer, shard_base, slot_idx)
                    != generation
                ):
                    continue

                str_val, int_val, expires_at, occupied = self._read_slot(
                    buffer, shard_base, slot_idx
                )
                if not occupied or (expires_at != 0.0 and expires_at < now):
                    new_value = amount
                    self._write_int_slot(buffer, shard_base, slot_idx, new_value, 0.0)

                else:
                    if int_val is not None:
                        current = int_val
                    else:
                        try:
                            current = int(str_val)  # type: ignore[arg-type]
                        except (ValueError, TypeError):
                            current = 0

                    new_value = current + amount
                    self._write_int_value_only(buffer, shard_base, slot_idx, new_value)
                return new_value
            finally:
                self._shard_semaphores[shard_idx].release()  # type: ignore[index]

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {self._max_aba_retries} retries."
        )

    def _expire(self, key: str, seconds: int) -> bool:
        """
        Synchronous expire.

        Lock order: `slot_map_semaphores[shard_idx]` -> `shard_semaphores[shard_idx]`
        (sequential). ABA protection: a generation mismatch under the shard
        semaphore means the key is gone; `False` is returned.

        :param key: The throttle key.
        :param seconds: TTL to set in seconds from now.
        :return: `True` if the expiry was set, `False` if the key does
            not exist or has already expired.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
        try:
            result = self._hash_table_get_slot_with_generation(
                buffer, shard_base, key_bytes
            )
        finally:
            self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

        if result is None:
            return False

        slot_idx, generation = result

        self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
        try:
            if self._read_slot_generation(buffer, shard_base, slot_idx) != generation:
                return False

            _, _, expires_at, occupied = self._read_slot(buffer, shard_base, slot_idx)
            if not occupied or (expires_at != 0.0 and expires_at < now):
                return False

            self._EXPIRY_STRUCT.pack_into(
                buffer,
                self._slot_offset(shard_base, slot_idx) + self._expiry_offset,
                now + seconds,
            )
            return True
        finally:
            self._shard_semaphores[shard_idx].release()  # type: ignore[index]

    def _increment_with_ttl(self, key: str, amount: int, ttl: int) -> int:
        """
        Synchronous increment_with_ttl using native int64 storage.

        Hot path for all fixed-window and sliding-window throttle strategies.
        TTL is applied only on the first write or after expiry; subsequent
        increments within the window preserve the existing expiry.

        Lock order: `slot_map_semaphores[shard_idx]` -> `shard_semaphores[shard_idx]`
        (sequential). ABA retry up to `self._max_aba_retries`.

        :param key: The throttle key.
        :param amount: The increment amount.
        :param ttl: Window TTL in seconds; applied when the key is new or has expired.
        :return: The new counter value after incrementing.
        :raises BackendError: If the ABA retry limit is exceeded.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        key_bytes = key.encode("utf-8")
        shard_idx = self._shard_idx_for_key(key)
        shard_base = self._shard_base(shard_idx)

        for _ in range(self._max_aba_retries):
            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                hash_table_result = self._hash_table_get_slot_with_generation(
                    buffer, shard_base, key_bytes
                )
                if hash_table_result is None:
                    slot_idx = self._free_stack_pop(buffer, shard_base)
                    self._hash_table_upsert(buffer, shard_base, key_bytes, slot_idx)
                    generation = self._read_slot_generation(
                        buffer, shard_base, slot_idx
                    )
                else:
                    slot_idx, generation = hash_table_result
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

            self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                if (
                    self._read_slot_generation(buffer, shard_base, slot_idx)
                    != generation
                ):
                    continue

                str_val, int_val, expires_at, occupied = self._read_slot(
                    buffer, shard_base, slot_idx
                )
                is_new = not occupied or (expires_at != 0.0 and expires_at < now)
                if is_new:
                    new_value = amount
                    self._write_int_slot(
                        buffer, shard_base, slot_idx, new_value, now + ttl
                    )
                else:
                    if int_val is not None:
                        current = int_val
                    else:
                        try:
                            current = int(str_val)  # type: ignore[arg-type]
                        except (ValueError, TypeError):
                            current = 0

                    new_value = current + amount
                    effective_expiry = expires_at if expires_at != 0.0 else now + ttl
                    self._write_int_slot(
                        buffer, shard_base, slot_idx, new_value, effective_expiry
                    )
                return new_value
            finally:
                self._shard_semaphores[shard_idx].release()  # type: ignore[index]

        raise BackendError(
            f"ABA race on key {key!r} not resolved after {self._max_aba_retries} retries."
        )

    def _multi_get(
        self,
        keys: typing.Tuple[str, ...],
        shard_to_keys: typing.Dict[int, typing.List[str]],
    ) -> typing.Dict[str, typing.Optional[str]]:
        """
        Synchronous multi_get.

        Groups keys by shard. For each shard: one `slot_map_semaphores[shard_idx]`
        acquisition for all hash-table lookups within that shard, then one
        `shard_semaphores[shard_idx]` acquisition for all slot reads. Shards are
        processed in ascending index order to avoid deadlock.

        ABA protection: per-key generation captured at lookup time, verified
        under `shard_semaphores[shard_idx]`. A mismatch yields `None` for that
        key (no retry on reads — best-effort snapshot).

        :param keys: All keys to retrieve.
        :param shard_to_keys: Pre-computed mapping of shard index to the list
            of keys hashing to that shard.
        :return: Mapping of key to value string (or `None` for absent /
            expired keys), in no guaranteed order. The public `multi_get`
            re-orders by the original *keys* sequence.
        """
        buffer = self._buffer
        assert buffer is not None
        now = time()
        results: typing.Dict[str, typing.Optional[str]] = {}

        for shard_idx in sorted(shard_to_keys):
            shard_base = self._shard_base(shard_idx)
            shard_keys = shard_to_keys[shard_idx]

            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                slot_info: typing.Dict[str, typing.Optional[typing.Tuple[int, int]]] = {
                    k: self._hash_table_get_slot_with_generation(
                        buffer, shard_base, k.encode("utf-8")
                    )
                    for k in shard_keys
                }
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

            self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                for k in shard_keys:
                    info = slot_info[k]
                    if info is None:
                        results[k] = None
                        continue

                    slot_idx, generation = info
                    if (
                        self._read_slot_generation(buffer, shard_base, slot_idx)
                        != generation
                    ):
                        results[k] = None
                        continue

                    str_val, int_val, expires_at, occupied = self._read_slot(
                        buffer, shard_base, slot_idx
                    )
                    if not occupied or (expires_at != 0.0 and expires_at < now):
                        results[k] = None
                    elif int_val is not None:
                        results[k] = str(int_val)
                    else:
                        results[k] = str_val
            finally:
                self._shard_semaphores[shard_idx].release()  # type: ignore[index]

        return results

    def _multi_set(
        self,
        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]],
        all_keys: typing.List[str],
        expire: typing.Optional[float],
    ) -> None:
        """
        Synchronous multi_set.

        For each shard: allocates all slots in one `slot_map_semaphores[shard_idx]`
        hold, then writes values in one `shard_semaphores[shard_idx]` hold. Shards
        are processed in ascending index order.

        ABA protection: per-key generation verified under
        `shard_semaphores[shard_idx]`. ABA-affected keys are re-allocated in a
        follow-up `slot_map_semaphores[shard_idx]` pass, retrying up to
        `self._max_aba_retries` times before raising `BackendError`.

        All values are stored as string slots.

        :param shard_to_items: Pre-computed mapping of shard index to
            `(key, value)` pairs hashing to that shard.
        :param all_keys: All keys being set (used for slot pre-allocation).
        :param expire: TTL in seconds from now applied to all keys, or
            `None` for no expiry.
        :raises BackendError: If the ABA retry limit is exceeded for any key.
        """
        buffer = self._buffer
        assert buffer is not None
        expires_at = (time() + expire) if expire is not None else 0.0

        slot_assignments: typing.Dict[str, typing.Tuple[int, int]] = {}

        for shard_idx in sorted(shard_to_items):
            shard_base = self._shard_base(shard_idx)
            shard_items = shard_to_items[shard_idx]

            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                for key, _ in shard_items:
                    key_bytes = key.encode("utf-8")
                    hash_table_result = self._hash_table_get_slot_with_generation(
                        buffer, shard_base, key_bytes
                    )
                    if hash_table_result is None:
                        slot_idx = self._free_stack_pop(buffer, shard_base)
                        self._hash_table_upsert(buffer, shard_base, key_bytes, slot_idx)
                        gen = self._read_slot_generation(buffer, shard_base, slot_idx)
                    else:
                        slot_idx, gen = hash_table_result
                    slot_assignments[key] = (slot_idx, gen)
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

        aba_keys: typing.List[str] = []
        for attempt in range(self._max_aba_retries):
            aba_keys = []

            for shard_idx in sorted(shard_to_items):
                shard_base = self._shard_base(shard_idx)
                self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
                try:
                    for key, val in shard_to_items[shard_idx]:
                        slot_idx, gen = slot_assignments[key]
                        if (
                            self._read_slot_generation(buffer, shard_base, slot_idx)
                            != gen
                        ):
                            aba_keys.append(key)
                            continue

                        self._write_string_slot(
                            buffer, shard_base, slot_idx, val, expires_at
                        )
                finally:
                    self._shard_semaphores[shard_idx].release()  # type: ignore[index]

            if not aba_keys:
                return

            if attempt == self._max_aba_retries - 1:
                break

            aba_by_shard: typing.Dict[int, typing.List[str]] = {}
            for key in aba_keys:
                aba_by_shard.setdefault(self._shard_idx_for_key(key), []).append(key)

            for shard_idx in sorted(aba_by_shard):
                shard_base = self._shard_base(shard_idx)
                self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
                try:
                    for key in aba_by_shard[shard_idx]:
                        key_bytes = key.encode("utf-8")
                        hash_table_result = self._hash_table_get_slot_with_generation(
                            buffer, shard_base, key_bytes
                        )
                        if hash_table_result is None:
                            slot_idx = self._free_stack_pop(buffer, shard_base)
                            self._hash_table_upsert(
                                buffer, shard_base, key_bytes, slot_idx
                            )
                            gen = self._read_slot_generation(
                                buffer, shard_base, slot_idx
                            )
                        else:
                            slot_idx, gen = hash_table_result
                        slot_assignments[key] = (slot_idx, gen)
                finally:
                    self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

        raise BackendError(
            f"ABA race on keys {aba_keys!r} not resolved after {self._max_aba_retries} retries."
        )

    def _clear(self) -> None:
        """
        Remove all keys whose name starts with this backend'shard_idx namespace prefix.

        For each shard: a `slot_map_semaphores[shard_idx]` acquisition scans the
        hash table, deletes matching entries, and pushes their slots back to
        the free pool. A subsequent `shard_semaphores[shard_idx]` acquisition clears
        the occupied flags. Shards are processed independently, in ascending
        index order.

        Holding both semaphores for the same shard simultaneously is avoided
        to respect the lock-ordering rule; `_delete` holds them in the
        opposite order (shard first, then slot-map), which is safe only
        because `_delete` never calls `_free_stack_pop`. Here we separate
        the phases instead.
        """
        buffer = self._buffer
        assert buffer is not None
        prefix = f"{self.namespace}:".encode("utf-8")

        for shard_idx in range(self._number_of_shards):
            shard_base = self._shard_base(shard_idx)
            candidates: typing.List[int] = []

            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                for key_str, slot_idx in list(
                    self._hash_table_iter_occupied(buffer, shard_base)
                ):
                    if not key_str.encode("utf-8").startswith(prefix):
                        continue

                    self._hash_table_delete(buffer, shard_base, key_str.encode("utf-8"))
                    self._free_stack_push(buffer, shard_base, slot_idx)
                    candidates.append(slot_idx)
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

            if not candidates:
                continue

            self._shard_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                for slot_idx in candidates:
                    self._clear_slot(buffer, shard_base, slot_idx)
            finally:
                self._shard_semaphores[shard_idx].release()  # type: ignore[index]

    def _cleanup(self) -> int:
        """
        Reclaim expired slots across all shards.

        Pass 1 (no lock): unsynchronised snapshot of occupied entries and
        their expiry times; used only as a candidate hint.

        Pass 2 (`slot_map_semaphores[shard_idx]` only, per shard): re-verify each
        candidate. The shard semaphore is intentionally **not** held here to
        avoid violating the lock-ordering rule. The float64 expiry write is
        not guaranteed atomic on all architectures; the worst outcome is a
        spurious skip (missed cleanup cycle), never a spurious free.

        :return: The total number of slots freed across all shards.
        """
        buffer = self._buffer
        if buffer is None:
            return 0

        now = time()
        total_freed = 0

        for shard_idx in range(self._number_of_shards):
            shard_base = self._shard_base(shard_idx)

            candidates: typing.List[typing.Tuple[bytes, int]] = []
            for key_str, slot_idx in self._hash_table_iter_occupied(buffer, shard_base):
                _, _, expires_at, occupied = self._read_slot(
                    buffer, shard_base, slot_idx
                )
                if occupied and expires_at != 0.0 and expires_at < now:
                    candidates.append((key_str.encode("utf-8"), slot_idx))

            if not candidates:
                continue

            self._slot_map_semaphores[shard_idx].acquire()  # type: ignore[index]
            try:
                for key_bytes, expected_slot_idx in candidates:
                    current_slot_idx = self._hash_table_get_slot(
                        buffer, shard_base, key_bytes
                    )
                    if (
                        current_slot_idx is None
                        or current_slot_idx != expected_slot_idx
                    ):
                        continue

                    _, _, expires_at, occupied = self._read_slot(
                        buffer, shard_base, current_slot_idx
                    )
                    if not occupied or expires_at == 0.0 or expires_at >= now:
                        continue

                    self._hash_table_delete(buffer, shard_base, key_bytes)
                    self._free_stack_push(buffer, shard_base, current_slot_idx)
                    self._clear_slot(buffer, shard_base, current_slot_idx)
                    total_freed += 1
            finally:
                self._slot_map_semaphores[shard_idx].release()  # type: ignore[index]

        return total_freed

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """
        Get the value for the given key.

        :param key: The throttle key to retrieve.
        :return: The stored value as a string, or `None` if the key does
            not exist or has expired.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._get, key
        )

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """
        Set the value for the given key with optional expiration.

        :param key: The throttle key to set.
        :param value: The string value to store.
        :param expire: Optional TTL in seconds from now.
        """
        self._assert_ready()
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._set, key, value, expire
        )

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """
        Delete the given key.

        :param key: The throttle key to delete.
        :return: `True` if the key was deleted, `False` if it did not exist.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._delete, key
        )

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment a counter and return the new value.

        Uses native int64 storage; no string parsing on the hot path.
        If the key does not exist it is initialised to *amount*. If it
        exists as a string kind it is parsed and converted to int kind.

        :param key: The counter key.
        :param amount: Amount to increment by (default `1`; use a negative
            value to decrement).
        :return: The new counter value after incrementing.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._increment, key, amount
        )

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration time on an existing key.

        :param key: The key to set expiration on.
        :param seconds: TTL in seconds from now.
        :return: `True` if the expiration was set, `False` if the key
            does not exist or has already expired.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._expire, key, seconds
        )

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment a counter and set a TTL if the key is new.

        Uses native int64 storage; the critical path performs no string
        encoding or decoding. TTL is applied only on the first write within
        a window or after expiry; subsequent increments within the window
        preserve the existing expiry.

        :param key: The counter key.
        :param amount: Amount to increment by (default `1`).
        :param ttl: Window TTL in seconds applied when the key is new or expired (default `60`).
        :return: The new counter value after incrementing.
        """
        self._assert_ready()
        return await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._increment_with_ttl, key, amount, ttl
        )

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Retrieve multiple keys in a single operation.

        Keys are grouped by shard and each shard is read atomically within
        its own semaphore pair. The overall result is a best-effort snapshot
        across shards; no cross-shard atomicity is guaranteed.

        :param keys: Keys to retrieve.
        :return: List of values (`None` for absent or expired keys) in the same order as *keys*.
        """
        self._assert_ready()
        if not keys:
            return []

        shard_to_keys: typing.Dict[int, typing.List[str]] = {}
        for key in keys:
            shard_to_keys.setdefault(self._shard_idx_for_key(key), []).append(key)

        result_map = await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._multi_get, keys, shard_to_keys
        )
        return [result_map[k] for k in keys]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Set multiple keys in a single operation.

        Keys are grouped by shard. Each shard's slot allocation and write
        are performed atomically within that shard's semaphore pair. No
        cross-shard atomicity is guaranteed.

        :param items: Mapping of keys to string values.
        :param expire: Optional TTL in seconds applied to all keys.
        """
        self._assert_ready()
        if not items:
            return

        shard_to_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]] = {}
        for key, val in items.items():
            shard_to_items.setdefault(self._shard_idx_for_key(key), []).append(
                (key, val)
            )

        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor,
            self._multi_set,
            shard_to_items,
            list(items.keys()),
            expire,
        )

    def get_lock(
        self, name: str, ttl: typing.Optional[float] = None, reentrant: bool = False
    ) -> _NamedLockHandle[_AsyncSharedMemoryLock]:
        """
        Return a named cross-process lock backed by a byte in the
        shared memory segment.

        Named locks are process-local (not shared across processes). Each
        forked worker builds its own pool independently; cross-process named
        locking is unnecessary because named locks are used only by throttle
        strategies within a single asyncio event loop.

        :param name: The logical lock name (should be a namespaced key).
        :return: A `_NamedLockHandle`.
        """
        self._assert_ready()
        return self._named_lock_pool.get(name)  # type: ignore[union-attr]

    async def clear(self) -> None:
        """
        Remove all keys belonging to this backend'shard_idx namespace.

        Other namespaces sharing the same shared memory segment are
        unaffected.
        """
        self._assert_ready()
        await asyncio.get_running_loop().run_in_executor(  # type: ignore[arg-type]
            self._executor, self._clear
        )

    async def _cleanup_loop(self) -> None:
        """
        Background task that periodically reclaims expired slots.

        Runs until the backend is closed or the task is cancelled.
        Exceptions other than `CancelledError` are swallowed to keep the
        backend alive.
        """
        assert self._cleanup_frequency is not None
        while self._initialized:
            try:
                await asyncio.sleep(self._cleanup_frequency)
                await asyncio.get_running_loop().run_in_executor(None, self._cleanup)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def reset(self) -> None:
        """Reset the backend by clearing all throttling data."""
        # Just clear all data since this is an in-memory backend.
        await self.clear()

    def closed(self) -> bool:
        """Return `True` if the backend has been closed."""
        return not self._initialized

    async def close(self) -> None:
        """
        Shut down this backend instance.

        Cancels the cleanup task, releases the buffer view, and closes the
        shared memory handle. If this instance is the creator (`create=True`),
        the segment is also unlinked (destroyed). Attaching instances only
        close their handle; they do not unlink.

        Closes the named lock pool, and shuts down the thread pool executor.
        """
        await self.clear()
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

        if self._buffer is not None and self._lock_byte_pool is not None:
            try:
                self._buffer[
                    self._lock_bytes_offset : self._lock_bytes_offset
                    + self._lock_byte_pool_size
                ] = bytes(self._lock_byte_pool_size)
            except Exception:
                pass

        if self._buffer is not None:
            self._buffer.release()
            self._buffer = None

        if self._shared_memory is not None:
            self._shared_memory.close()
            if self._create:
                self._shared_memory.unlink()
            self._shared_memory = None

        if self._named_lock_pool is not None:
            self._named_lock_pool.close()
            self._named_lock_pool = None

        # Close threadpool executor
        self._executor.shutdown(wait=False)
