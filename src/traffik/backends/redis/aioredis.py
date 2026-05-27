"""Redis implementation of the throttle backend using `redis.asyncio`."""

import asyncio
import math
import sys
import typing
from time import monotonic
from types import TracebackType

import redis.asyncio as aioredis
from pottery import AIORedlock
from typing_extensions import TypedDict

from traffik._locks import _GatedNamedLock, _NamedGateRegistry
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import (
    BackendConnectionError,
    LockAcquisitionError,
    LockReleaseError,
)
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)

# Lua script for atomic increment with conditional TTL
# TTL is only set when key is created, but not on subsequent increments
_INCREMENT_WITH_TTL_SCRIPT = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local exists = redis.call('EXISTS', key)

if exists == 0 then
    -- Key doesn't exist: set initial value with TTL
    redis.call('SET', key, amount, 'EX', ttl)
    return amount
else
    -- Key exists: just increment (preserve existing TTL)
    if amount == 1 then
        return redis.call('INCR', key)
    else
        return redis.call('INCRBY', key, amount)
    end
end
"""

# Lua script for atomic clear on single round trip with no race conditions
# Uses SCAN instead of KEYS to avoid blocking on large datasets
_CLEAR_SCRIPT = """
local pattern = ARGV[1]
local cursor = "0"
local deleted = 0

repeat
    local result = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 1000)
    cursor = result[1]
    local keys = result[2]
    
    if #keys > 0 then
        deleted = deleted + redis.call('DEL', unpack(keys))
    end
until cursor == "0"

return deleted
"""


_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_ACQUIRE_SCRIPT = """
-- KEYS[1] = lock key
-- KEYS[2] = fence key
-- ARGV[1] = ttl (seconds, or 0 for no expiry)

-- Try to acquire lock first (most common path when lock is held)
local token = redis.call("INCR", KEYS[2])
local acquired

if ARGV[1] ~= "0" then
    acquired = redis.call(
        "SET",
        KEYS[1],
        token,
        "NX",
        "PX",
        ARGV[1]
    )
else
    acquired = redis.call(
        "SET",
        KEYS[1],
        token,
        "NX"
    )
end

if acquired then
    return token
end
return 0
"""


class _LockScriptSHAs(TypedDict):
    """
    Shared mutable container for lock script SHAs.

    Using a dict allows lock instances to update SHAs after NOSCRIPT errors,
    and have those updates visible to the backend and future lock instances.
    """

    acquire: str
    release: str


@typing.final
class _AsyncRedisLock:
    """
    Name-based, distributed Redis (un-fair) lock implementing the `AsyncLock` protocol.

    Non-reentrant by default but optionally reentrant per task.

    Ownership is tracked via `asyncio.Task` identity rather than `ContextVar`,
    which avoids false-positive `locked()` results in tasks spawned from a
    lock-holding parent (spawned tasks inherit a copy of the parent context,
    which would make a ContextVar-based check incorrectly report the lock as held).

    Reentrancy is process-local only, i.e, the reentry counter is tracked in Python
    and only the outermost acquire/release round-trips to Redis. This means the
    Redis TTL governs the total hold time across all reentrant levels. If the
    key expires while reentrant holds are active, the lock is silently lost.
    Keep critical sections short or set a generous TTL.
    """

    RELEASE_SCRIPT = _RELEASE_SCRIPT
    """Lua script for safe release"""
    ACQUIRE_SCRIPT = _ACQUIRE_SCRIPT
    """Lua script for safe acquire with optional TTL"""

    __slots__ = (
        "_name",
        "_client",
        "_owner",
        "_token",
        "_reentry_count",
        "_ttl",
        "_script_shas",
        "_max_spins_before_backoff",
        "_spin_max_delay_seconds",
        "_reentrant",
    )

    def __init__(
        self,
        name: str,
        client: aioredis.Redis,
        script_shas: _LockScriptSHAs,
        ttl: typing.Optional[float] = None,
        max_spins_before_backoff: int = 4,
        spin_max_delay_seconds: float = 0.01,
        reentrant: bool = False,
    ) -> None:
        """
        Initialize the Redis lock.

        :param name: Unique lock name shared across processes.
        :param client: An `aioredis` client.
        :param script_shas: Shared mutable dict containing pre-loaded script SHAs.
            Updates to this dict propagate to the backend and other lock instances.
        :param ttl: How long the lock should live in seconds (default: None = no expiration) before auto-release.
        :param max_spins_before_backoff: Number of zero-delay yields to the event-loop during acquisition,
            before applying exponential backoff.
        :param spin_max_delay_seconds: Maximum delay in seconds during backoff.
        :param reentrant: Whether the lock should be reentrant per task (default: False).
        """
        self._name = name
        self._client = client
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._token: typing.Optional[int] = None
        self._reentry_count: int = 0
        self._script_shas = script_shas
        self._max_spins_before_backoff = max_spins_before_backoff
        self._spin_max_delay_seconds = spin_max_delay_seconds
        self._reentrant = reentrant
        self._ttl = ttl

    @classmethod
    async def _register_acquire_script(cls, client: aioredis.Redis) -> str:
        """Register and return the acquire script SHA."""
        return await client.script_load(cls.ACQUIRE_SCRIPT)

    @classmethod
    async def _register_release_script(cls, client: aioredis.Redis) -> str:
        """Register and return the release script SHA."""
        return await client.script_load(cls.RELEASE_SCRIPT)

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        """Return True if the current task owns this lock."""
        if task is None:
            task = asyncio.current_task()
        return task is not None and task is self._owner

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the distributed lock, reentrant per task.

        :param blocking: If False, return immediately if locked elsewhere.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Max wait time when blocking (seconds).
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :return: True if lock acquired, False otherwise.
        """
        current = asyncio.current_task()
        if current is None:
            raise LockAcquisitionError(
                f"Lock '{self._name}' must be acquired from within an asyncio Task."
            )

        # Reentrant. Current task already holds the lock
        if self.is_owner(task=current):
            if not self._reentrant:
                raise LockAcquisitionError(
                    f"Lock '{self._name}' is already acquired by the current task "
                    "and was not configured as reentrant."
                )
            self._reentry_count += 1
            return True

        name = self._name
        has_blocking_timeout = blocking_timeout is not None

        start = monotonic()
        attempts = 0
        max_spins = self._max_spins_before_backoff
        spin_max_delay = self._spin_max_delay_seconds
        while True:
            token: typing.Optional[int] = None
            try:
                token = await self._client.evalsha(  # type: ignore
                    self._script_shas["acquire"],  # type: ignore[arg-type]
                    2,  # KEYS
                    name,  # KEYS[1]
                    f"{name}:fence",  # KEYS[2]
                    str(self._ttl or 0),
                )

            except aioredis.ResponseError as exc:
                if "NOSCRIPT" in str(exc):
                    # Script was flushed from Redis cache, re-register and retry
                    # Update shared dict so backend and future locks see the new SHA
                    self._script_shas["acquire"] = await self._client.script_load(  # type: ignore
                        self.ACQUIRE_SCRIPT
                    )
                    token = await self._client.evalsha(  # type: ignore
                        self._script_shas["acquire"],  # type: ignore[arg-type]
                        2,  # KEYS
                        name,  # KEYS[1]
                        f"{name}:fence",  # KEYS[2]
                        str(self._ttl or 0),
                    )
                else:
                    raise LockAcquisitionError(exc) from exc

            if token:
                self._owner = current
                self._token = token
                self._reentry_count = 1
                return True

            if not blocking:
                return False

            if has_blocking_timeout and (monotonic() - start) >= blocking_timeout:  # type: ignore
                return False

            attempts += 1
            if attempts <= max_spins:
                await asyncio.sleep(0)
            else:
                # Exponential backoff
                exponent = min(
                    attempts - max_spins,
                    6,
                )
                delay = min(0.0005 * (1 << exponent), spin_max_delay)
                await asyncio.sleep(delay)

    async def release(self) -> None:
        """
        Release the lock once.

        Only when the reentrancy count reaches zero will the underlying Redis lock actually be released.
        """
        current = asyncio.current_task()
        if not self.is_owner(task=current):
            raise LockReleaseError(
                f"Cannot release lock '{self._name}': "
                f"current task {current!r} does not own the lock "
                f"(owner: {self._owner!r})."
            )

        # Reentrant inner release. Just decrement the counter
        if self._reentry_count > 1:
            self._reentry_count -= 1
            return

        # Outermost release. Attempt Redis release, but clear ownership regardless of Redis outcome
        token = str(self._token)
        name = self._name
        try:
            # Ensure we only release if we own the lock (token matches)
            released = await self._client.evalsha(  # type: ignore
                self._script_shas["release"],  # type: ignore[arg-type]
                1,  # num keys
                name,  # KEYS[1]
                token,  # ARGV[1]
            )
            if not released:
                sys.stderr.write(f"Warning: Lock '{name}' expired or stolen\n")
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" in str(exc):
                # Script was flushed from Redis cache, re-register and retry
                # Update shared dict so backend and future locks see the new SHA
                self._script_shas["release"] = await self._client.script_load(  # type: ignore
                    self.RELEASE_SCRIPT
                )
                await self._client.evalsha(  # type: ignore
                    self._script_shas["release"],  # type: ignore[arg-type]
                    1,  # num keys
                    name,  # KEYS[1]
                    token,  # ARGV[1]
                )
            else:
                raise LockReleaseError(exc) from exc

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nosec
            # Lock might have expired or been released already
            raise LockReleaseError(f"Failed to release lock '{name}'") from exc
        finally:
            # Clear ownership regardless of whether Redis release succeeded.
            # If Redis release failed, the key will expire via TTL.
            self._owner = None
            self._token = None
            self._reentry_count = 0
            sys.stderr.flush()

    async def __aenter__(self):
        if not await self.acquire():
            raise LockAcquisitionError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ):
        await self.release()


@typing.final
class _AsyncRedLock:
    """
    Name-based, distributed Redis (un-fair) redlock implementing the `AsyncLock` protocol.

    Non-reentrant by default but optionally reentrant per task.

    Adapts `pottery.AIORedlock` to the `AsyncLock` protocol. Useful when utilizing
    Redis clusters or multiple Redis instances. There will be noticeable performance
    overhead when compared to `_AsyncRedisLock` due to the multiple Redis connections
    and network roundtrips involved in acquiring and releasing the lock, and is mostly
    overkill for single Redis instance deployments.

    Reentrancy is process-local only, i.e, the reentry counter is tracked in Python
    and only the outermost acquire/release round-trips to Redis. This means the
    Redis TTL governs the total hold time across all reentrant levels. If the
    key expires while reentrant holds are active, the lock is silently lost.
    Keep critical sections short or set a generous TTL.
    """

    __slots__ = (
        "_name",
        "_client",
        "_lock",
        "_owner",
        "_reentry_count",
        "_ttl",
        "_reentrant",
    )

    def __init__(
        self,
        name: str,
        client: aioredis.Redis,
        ttl: typing.Optional[float] = None,
        reentrant: bool = False,
    ) -> None:
        """
        Initialize the Redis redlock.

        :param name: Unique lock name shared across processes.
        :param client: An `aioredis` client.
        :param ttl: How long the lock should live in seconds (default: None) before auto-release.
            If None, the TTL is derived from the blocking_timeout + 1 second buffer. If both
            are None, the lock has no expiration. It is not recommended to have locks without
            expiration in distributed environments to avoid deadlocks.
        :param reentrant: Whether to allow the same task to acquire the lock multiple times.
            Reentrancy is process-local only: the reentry counter is tracked in Python and only
            the outermost acquire/release round-trips to Redis. Defaults to False.
        """
        self._name = name
        self._client = client
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._reentry_count: int = 0
        self._reentrant = reentrant
        self._ttl = math.ceil(ttl) if ttl is not None else 0
        self._lock = AIORedlock(
            key=name,
            masters={client},
            auto_release_time=self._ttl,
        )

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        """Return True if the current task owns this lock."""
        if task is None:
            task = asyncio.current_task()
        return task is not None and task is self._owner

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the distributed lock, reentrant per task.

        :param blocking: If False, return immediately if locked elsewhere.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Max wait time when blocking (seconds). `None` means wait forever.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :return: True if lock acquired, False otherwise.
        """
        current = asyncio.current_task()
        if current is None:
            raise LockAcquisitionError(
                f"Lock '{self._name}' must be acquired from within an asyncio Task."
            )

        # Reentrant. Current task already holds the lock
        if self.is_owner(task=current):
            if not self._reentrant:
                raise LockAcquisitionError(
                    f"Lock '{self._name}' is already acquired by the current task "
                    "and was not configured as reentrant."
                )
            self._reentry_count += 1
            return True

        # Determine effective timeout
        if not blocking:
            # Since `pottery.AIORedlock` does not support a non-blocking mode,
            # we use a very short timeout to approximate it.
            effective_timeout = 1e-6
        else:
            effective_timeout = blocking_timeout or -1

        # Attempt to acquire the Redis lock
        try:
            acquired = await self._lock.acquire(
                blocking=blocking, timeout=effective_timeout
            )
        except Exception as exc:  # nosec
            raise LockAcquisitionError(
                f"Failed to acquire lock '{self._name}'"
            ) from exc

        if acquired:
            self._owner = current
            self._reentry_count = 1
            return True
        return False

    async def release(self) -> None:
        """
        Release the lock once.

        Only when the reentrancy count reaches zero will the underlying Redis lock
        actually be released.
        """
        current = asyncio.current_task()
        if not self.is_owner(task=current):
            raise LockReleaseError(
                f"Cannot release lock '{self._name}': "
                f"current task {current!r} does not own the lock "
                f"(owner: {self._owner!r})."
            )

        # Reentrant inner release. Just decrement the counter
        if self._reentry_count > 1:
            self._reentry_count -= 1
            return

        # Outermost release. Attempt Redis release, but clear ownership regardless of Redis outcome
        name = self._name
        try:
            await self._lock.release()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nosec
            # Lock might have expired or been released already
            raise LockReleaseError(f"Failed to release lock '{name}'") from exc
        finally:
            # Clear ownership regardless of whether Redis release succeeded.
            # If Redis release failed, the key will expire via TTL.
            self._owner = None
            self._reentry_count = 0

    async def __aenter__(self):
        if not await self.acquire():
            raise LockAcquisitionError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


# TODO: Check if these stores would work without any (substantial) change needed:
# - Valkey
# - KeyDB
# - DragonflyDB (probably)
# - Redis Stack


class RedisBackend(ThrottleBackend[aioredis.Redis, HTTPConnectionT]):
    """
    Redis throttle backend.

    Uses `redis.asyncio` for backend operations.
    """

    wrap_methods = ("clear",)

    INCREMENT_WITH_TTL_SCRIPT = _INCREMENT_WITH_TTL_SCRIPT
    """Lua script for atomic increment with conditional TTL (set TTL only on key creation)"""
    CLEAR_SCRIPT = _CLEAR_SCRIPT
    """Lua script for atomic clear of keys matching a pattern"""

    def __init__(
        self,
        connection: typing.Union[
            str, typing.Callable[[], typing.Awaitable[aioredis.Redis]]
        ],
        *,
        namespace: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, typing.Any]
        ] = None,
        persistent: bool = False,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        lock_type: typing.Literal["redis", "redlock"] = "redis",
        lock_blocking: typing.Optional[bool] = None,
        lock_ttl: typing.Optional[float] = None,
        lock_blocking_timeout: typing.Optional[float] = None,
        lock_contention_threshold: int = 4,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the `aioredis` Redis backend.

        :param connection: Redis connection URL or async factory function that returns an `aioredis.Redis` instance.
        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
        :param on_error: Strategy to handle errors during throttling operations.
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
        :param lock_type: The type of Redis lock to use ("redis" or "redlock").
            - "redis": Uses a simple Redis-based lock suitable for single Redis instances.
            - "redlock": Uses the Redlock algorithm for distributed locking, suitable for
              Redis clusters or multiple Redis instances.
        :param lock_contention_threshold: The threshold for the process-local contention serialization gate.
            When the number of waiters for a lock exceeds this threshold, new acquirers will be serialized through
            an `asyncio.Lock` to reduce Redis connection hammering and thundering herd issues.
        :param kwargs: Additional keyword arguments passed to the base `ThrottleBackend`.
        """
        if isinstance(connection, str):
            # Create a redis connection factory with the provided URL
            async def _factory():
                return await aioredis.from_url(connection, decode_responses=True)

            self._get_connection = _factory
        else:
            self._get_connection = connection

        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            **kwargs,
        )
        self._increment_with_ttl_sha: typing.Optional[str] = None
        """SHA hash of the loaded Lua script for increment_with_ttl."""
        self._clear_sha: typing.Optional[str] = None
        """SHA hash of the loaded Lua script for clear."""

        # Why share script SHAs in a mutable dict?
        # Lock script SHAs are stored in a shared mutable dict (`_LockScriptSHAs`) rather than
        # as plain strings because it ensures that when Redis flushes its script cache (e.g., restart,
        # SCRIPT FLUSH, memory pressure), and any lock instance reloads the script, the updated
        # SHA is visible to the backend and all future lock instances. Without this, each new
        # lock would receive a stale SHA from the backend and pay the NOSCRIPT reload cost.
        self._lock_script_shas: typing.Optional[_LockScriptSHAs] = None
        """Shared mutable dict for lock script SHAs (see module docstring)."""
        self._use_redlock = lock_type == "redlock"
        """Whether to use Redlock algorithm for distributed locking."""
        self._lock_contention_threshold = lock_contention_threshold
        """Maximum"""
        self._named_gate_registry: typing.Optional[_NamedGateRegistry] = None
        """Registry for named gates used by `_GatedNamedLock` wrappers around non-reentrant locks."""

    async def initialize(self) -> None:
        """Ensure the Redis connection is ready. Load and register Lua scripts."""
        if self.connection is None:
            self.connection = await self._get_connection()
            try:
                # Pre-load all Lua scripts
                await self._ensure_increment_with_ttl_script()
                await self._ensure_clear_script()
                if not self._use_redlock:
                    await self._ensure_lock_scripts_shas()

            except aioredis.RedisError as exc:
                raise BackendConnectionError(
                    "Failed to initialize Redis connection."
                ) from exc

        if self._named_gate_registry is None or self._named_gate_registry.closed:
            self._named_gate_registry = _NamedGateRegistry(
                contention_threshold=self._lock_contention_threshold
            )

    def set_lock_contention_threshold(self, threshold: int) -> None:
        """
        Adjust the process-local contention serialization gate threshold at runtime.

        Setting threshold to a very large value (e.g. maxsize)
        effectively disables the gate.
        Tasks currently waiting on the gate are unaffected until
        they complete their current acquire cycle.

        :param threshold: The new contention threshold (must be at least 1).
        """
        if threshold < 1:
            raise ValueError("threshold must be at least 1")

        self._lock_contention_threshold = threshold
        if self._named_gate_registry is not None:
            self._named_gate_registry.set_contention_threshold(threshold)

    def disable_lock_contention_gate(self) -> None:
        """
        Effectively disable the process-local lock contention
        serialization gate by setting threshold very high.
        Tasks currently waiting on the gate are unaffected until
        they complete their current acquire cycle.
        """
        self.set_lock_contention_threshold(2**31)

    async def _check_scripts_ready(self) -> bool:
        """Check if all required Lua scripts are loaded and registered."""
        if self.connection is None:
            return False

        scripts_shas = []
        if self._increment_with_ttl_sha is not None:
            scripts_shas.append(self._increment_with_ttl_sha)

        # Check clear script
        if self._clear_sha is not None:
            scripts_shas.append(self._clear_sha)

        # Check lock scripts if using `_AsyncRedisLock`
        if not self._use_redlock and self._lock_script_shas is not None:
            scripts_shas.append(self._lock_script_shas["acquire"])
            scripts_shas.append(self._lock_script_shas["release"])

        exists = await self.connection.script_exists(*scripts_shas)  # type: ignore
        return all(exists)

    async def ready(self) -> bool:
        if self.connection is None:
            return False

        try:
            await self.connection.ping()
            return await self._check_scripts_ready()
        except aioredis.RedisError:
            return False

    async def _ensure_increment_with_ttl_script(self) -> None:
        """Ensure the `increment_with_ttl` Lua script is registered."""
        if self._increment_with_ttl_sha is None:
            self._increment_with_ttl_sha = await self.connection.script_load(  # type: ignore
                self.INCREMENT_WITH_TTL_SCRIPT
            )

    async def _ensure_clear_script(self) -> None:
        """Ensure the `clear` Lua script is registered."""
        if self._clear_sha is None:
            self._clear_sha = await self.connection.script_load(  # type: ignore
                self.CLEAR_SCRIPT
            )

    async def _ensure_lock_scripts_shas(self) -> None:
        """Ensure the lock Lua scripts are registered and stored in the shared dict."""
        if self._lock_script_shas is None and self.connection is not None:
            self._lock_script_shas = dict(  # type: ignore[assignment]
                acquire=await _AsyncRedisLock._register_acquire_script(self.connection),
                release=await _AsyncRedisLock._register_release_script(self.connection),
            )

    def _assert_ready(self) -> None:
        """
        Raise `BackendConnectionError` if the backend has not been initialized.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def get_lock(
        self, name: str, ttl: typing.Optional[float] = None, reentrant: bool = False
    ) -> typing.Union[
        _AsyncRedisLock,
        _AsyncRedLock,
        _GatedNamedLock[typing.Union[_AsyncRedisLock, _AsyncRedLock]],
    ]:
        """Returns a distributed Redis lock for the given name."""
        self._assert_ready()
        if not self._use_redlock:
            lock = _AsyncRedisLock(
                name,
                client=self.connection,  # type: ignore[arg-type]
                script_shas=self._lock_script_shas,  # type: ignore[arg-type]
                ttl=ttl,
                reentrant=reentrant,
            )
        else:
            lock = _AsyncRedLock(
                name,
                client=self.connection,  # type: ignore[arg-type]
                ttl=ttl,
                reentrant=reentrant,
            )
        return _GatedNamedLock(
            lock=lock,
            registry=self._named_gate_registry,  # type: ignore[arg-type]
            name=name,
        )

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Get value by key."""
        self._assert_ready()
        return await self.connection.get(key)  # type: ignore[union-attr]

    async def set(
        self, key: str, value: typing.Any, expire: typing.Optional[int] = None
    ) -> None:
        """Set value by key with optional expiration."""
        self._assert_ready()
        if expire is not None:
            await self.connection.set(key, value, ex=expire)  # type: ignore[union-attr]
        else:
            await self.connection.set(key, value)  # type: ignore[union-attr]

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """Delete key."""
        self._assert_ready()

        deleted_count = await self.connection.delete(key)  # type: ignore[union-attr]
        return deleted_count > 0

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment using Redis INCR/INCRBY."""
        self._assert_ready()
        if amount == 1:
            return await self.connection.incr(key)  # type: ignore[union-attr]
        return await self.connection.incrby(key, amount)  # type: ignore[union-attr]

    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement using Redis DECR/DECRBY."""
        self._assert_ready()
        if amount == 1:
            return await self.connection.decr(key)  # type: ignore[union-attr]
        return await self.connection.decrby(key, amount)  # type: ignore[union-attr]

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration using Redis EXPIRE."""
        self._assert_ready()

        result = await self.connection.expire(key, seconds)  # type: ignore[union-attr]
        return bool(result)

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment with TTL set only on first increment.

        Ensures that:
        1. If key doesn't exist: set counter to `amount` with TTL
        2. If key exists: just increment (preserve existing TTL)

        This ensures TTL is only set once when the key is created,
        and all subsequent increments preserve the original expiration time.

        :param key: Counter key
        :param amount: Amount to increment
        :param ttl: TTL to set if key is new (seconds)
        :return: New value after increment
        """
        self._assert_ready()
        try:
            result = await self.connection.evalsha(  # type: ignore
                self._increment_with_ttl_sha,  # type: ignore[arg-type]
                1,  # number of keys
                key,  # KEYS[1]
                str(amount),  # ARGV[1]
                str(ttl),  # ARGV[2]
            )
            return int(result) if result is not None else 0
        except aioredis.ResponseError as exc:
            # Check if it's a NOSCRIPT error (script flushed from Redis cache)
            if "NOSCRIPT" in str(exc):
                # Re-register the script and retry
                self._increment_with_ttl_sha = await self.connection.script_load(  # type: ignore
                    self.INCREMENT_WITH_TTL_SCRIPT
                )
                result = await self.connection.evalsha(  # type: ignore
                    self._increment_with_ttl_sha,  # type: ignore[arg-type]
                    1,
                    key,
                    str(amount),
                    str(ttl),
                )
                return int(result) if result is not None else 0
            raise

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Get multiple values by keys.

        Uses Redis MGET for batch retrieval.
        MGET is atomic in Redis so all values are retrieved at the same instant.

        :param keys: Keys to retrieve
        :return: List of values (None for missing keys)
        """
        self._assert_ready()
        if not keys:
            return []
        return await self.connection.mget(keys)  # type: ignore[union-attr]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Set multiple values atomically using Redis pipeline.

        Uses Redis pipeline for batch setting in a single round-trip.
        All operations execute atomically on the Redis server.

        :param items: Mapping of keys to values
        :param expire: Optional TTL in seconds for all keys
        """
        self._assert_ready()
        if not items:
            return

        async with self.connection.pipeline(transaction=True) as pipe:  # type: ignore[union-attr]
            for key, value in items.items():
                if expire is not None:
                    pipe.set(key, value, ex=expire)
                else:
                    pipe.set(key, value)
            await pipe.execute()

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        self._assert_ready()
        try:
            await self.connection.evalsha(  # type: ignore
                self._clear_sha,  # type: ignore[arg-type]
                0,  # no KEYS, pattern passed as ARGV
                f"{self.namespace}:*",  # ARGV[1]
            )
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" in str(exc):
                # Script was flushed, re-register and retry
                self._clear_sha = await self.connection.script_load(  # type: ignore
                    self.CLEAR_SCRIPT
                )
                await self.connection.evalsha(  # type: ignore
                    self._clear_sha,  # type: ignore[arg-type]
                    0,
                    f"{self.namespace}:*",
                )
            else:
                raise

    async def reset(self) -> None:
        """Reset all keys in the namespace."""
        await self.clear()

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.connection is not None:
            try:
                await self.connection.aclose()
            except Exception as exc:
                sys.stderr.write(
                    f"Warning: error while closing redis connection: {exc}\n"
                )
                sys.stderr.flush()
            finally:
                self.connection = None
                self._increment_with_ttl_script = None
                self._clear_script = None
                self._lock_script_shas = None

        if self._named_gate_registry is not None:
            self._named_gate_registry.close()
            self._named_gate_registry = None
