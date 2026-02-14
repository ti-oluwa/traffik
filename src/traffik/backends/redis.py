"""Redis implementation of the throttle backend using `redis.asyncio`."""

import asyncio
import contextvars
import math
import random
import sys
import typing

import redis.asyncio as aioredis
from pottery import AIORedlock
from typing_extensions import TypedDict

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)
from traffik.utils import time


class _LockScriptSHAs(TypedDict):
    """
    Shared mutable container for lock script SHAs.

    Using a dict allows lock instances to update SHAs after NOSCRIPT errors,
    and have those updates visible to the backend and future lock instances.
    """

    acquire: str
    release: str


class _AsyncRedisLock:
    """
    Name-based, task-reentrant, and fenced distributed Redis (un-fair) lock implementing the `AsyncLock` protocol.

    Uses a simple Redis-based locking mechanism with:

    - SET NX EX for acquisition
    - Redis INCR for fencing
    - Task-local reentrancy

    This is suitable for single Redis instance deployments where low-latency locking is required.
    Note: This lock does not implement the full Redlock algorithm and is not suitable
    for distributed Redis clusters.
    """

    _task_locks: contextvars.ContextVar[typing.Dict[str, typing.Tuple[int, int]]] = (
        contextvars.ContextVar("_task_locks")
    )
    """Per-task storage of lock fence tokens and reentrancy counts."""

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
    local acquired
    if ARGV[1] ~= "0" then
        acquired = redis.call("SET", KEYS[1], "pending", "NX", "EX", ARGV[1])
    else
        acquired = redis.call("SET", KEYS[1], "pending", "NX")
    end

    if not acquired then
        return 0  -- Lock already held, skip fence increment
    end

    -- Lock acquired, now get fence token and update lock value
    local token = redis.call("INCR", KEYS[2])
    redis.call("SET", KEYS[1], token, "XX", "KEEPTTL")
    return token
    """

    __slots__ = (
        "_name",
        "_redis",
        "_blocking_timeout",
        "_ttl",
        "_script_shas",
    )

    def __init__(
        self,
        name: str,
        redis: aioredis.Redis,
        script_shas: _LockScriptSHAs,
        ttl: typing.Optional[float] = None,
        blocking_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the Redis lock.

        :param name: Unique lock name shared across processes.
        :param redis: An `aioredis.Redis` connection instance.
        :param script_shas: Shared mutable dict containing pre-loaded script SHAs.
            Updates to this dict propagate to the backend and other lock instances.
        :param blocking_timeout: Max time to wait when acquiring lock (default: None = wait forever).
        :param ttl: How long the lock should live in seconds (default: None = no expiration) before auto-release.
        """
        self._name = name
        self._redis = redis
        self._script_shas = script_shas
        self._blocking_timeout = blocking_timeout

        if ttl is not None:
            self._ttl = math.ceil(ttl)
        elif blocking_timeout is not None:
            # Add 1 second buffer to blocking timeout
            self._ttl = math.ceil(blocking_timeout) + 1
        else:
            self._ttl = None  # type: ignore[assignment]

    def _get_task_locks(self) -> typing.Dict[str, tuple[int, int]]:
        """
        Get task-local lock storage, creating if needed.

        :return: Dictionary mapping lock names to (fence token, reentrancy count).
        """
        try:
            return self._task_locks.get()
        except LookupError:
            m: dict[str, tuple[int, int]] = {}
            self._task_locks.set(m)
            return m

    @classmethod
    async def _load_acquire_script(cls, redis: aioredis.Redis) -> str:
        """Load and return the acquire script SHA."""
        return await redis.script_load(cls._ACQUIRE_SCRIPT)

    @classmethod
    async def _load_release_script(cls, redis: aioredis.Redis) -> str:
        """Load and return the release script SHA."""
        return await redis.script_load(cls._RELEASE_SCRIPT)

    def locked(self) -> bool:
        locks = self._get_task_locks()
        return self._name in locks and locks[self._name][1] > 0

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        locks = self._get_task_locks()
        name = self._name

        # Re-entrant fast-path
        if name in locks:
            token, count = locks[name]
            locks[name] = (token, count + 1)
            return True

        blocking_timeout = (
            self._blocking_timeout if blocking_timeout is None else blocking_timeout
        )

        start = time()
        attempts = 0
        while True:
            try:
                token = await self._redis.evalsha(  # type: ignore
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
                    self._script_shas["acquire"] = await self._redis.script_load(  # type: ignore
                        type(self)._ACQUIRE_SCRIPT
                    )
                    token = await self._redis.evalsha(  # type: ignore
                        self._script_shas["acquire"],  # type: ignore[arg-type]
                        2,  # KEYS
                        name,  # KEYS[1]
                        f"{name}:fence",  # KEYS[2]
                        str(self._ttl or 0),
                    )
                else:
                    raise

            if token:
                locks[name] = (token, 1)  # type: ignore[assignment]
                return True

            if not blocking:
                return False

            if blocking_timeout is not None and (time() - start) >= blocking_timeout:
                return False

            # Exponential backoff with jitter
            # Start at 1ms, cap at 50ms to balance responsiveness vs CPU usage
            attempts += 1
            base_delay = min(
                0.001 * (2 ** min(attempts, 6)), 0.05
            )  # 1ms to 64ms, capped at 50ms
            # Add jitter (±25%) to prevent thundering herd
            jitter = base_delay * 0.25 * (random.random() * 2 - 1)  # nosec
            delay = base_delay + jitter
            await asyncio.sleep(delay)

    async def release(self) -> None:
        locks = self._get_task_locks()
        name = self._name

        if name not in locks:
            raise RuntimeError(f"Cannot release lock '{name}'. Lock not owned by task.")

        token, count = locks[name]
        # If reentrancy count > 1, just decrement
        if count > 1:
            locks[name] = (token, count - 1)
            return

        # If count == 1, fully release the lock
        try:
            await self._redis.evalsha(  # type: ignore
                self._script_shas["release"],  # type: ignore[arg-type]
                1,  # num keys
                name,  # KEYS[1]
                str(token),  # ARGV[1]
            )
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" in str(exc):
                # Script was flushed from Redis cache, re-register and retry
                # Update shared dict so backend and future locks see the new SHA
                self._script_shas["release"] = await self._redis.script_load(  # type: ignore
                    type(self)._RELEASE_SCRIPT
                )
                await self._redis.evalsha(  # type: ignore
                    self._script_shas["release"],  # type: ignore[arg-type]
                    1,  # num keys
                    name,  # KEYS[1]
                    str(token),  # ARGV[1]
                )
            else:
                raise
        finally:
            del locks[name]

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()


class _AsyncRedLock:
    """
    Name-based reentrant distributed Redis (un-fair) lock implementing the `AsyncLock` protocol.

    Uses the `redlock` algorithm for distributed locking via the `pottery.AIORedlock` API.

    This is useful when utilizing redis clusters or multiple Redis instances and is most likely
    overkill for single Redis instance deployments. There will be noticeable performance overhead
    when compared to `_AsyncRedisLock` due to the multiple Redis connections and network roundtrips
    involved in acquiring and releasing the lock.
    """

    _task_locks: contextvars.ContextVar[
        typing.Dict[str, typing.Tuple[AIORedlock, int]]
    ] = contextvars.ContextVar("_task_locks")
    """Per-task storage of lock objects and reentrancy counts."""

    __slots__ = (
        "_name",
        "_redis",
        "_blocking_timeout",
        "_ttl",
    )

    def __init__(
        self,
        name: str,
        redis: aioredis.Redis,
        ttl: typing.Optional[float] = None,
        blocking_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the Redis lock.

        :param name: Unique lock name shared across processes.
        :param redis: An aioredis.Redis connection instance.
        :param blocking_timeout: Max time to wait when acquiring lock (default: None = wait forever).
        :param ttl: How long the lock should live in seconds (default: None) before auto-release.
            If None, the TTL is derived from the blocking_timeout + 1 second buffer. If both
            are None, the lock has no expiration. It is not recommended to have locks without expiration
            in distributed environments to avoid deadlocks.
        """
        self._name = name
        self._redis = redis
        self._blocking_timeout = blocking_timeout
        if ttl is not None:
            self._ttl = math.ceil(ttl)
        elif blocking_timeout is not None:
            # Add 1 second buffer to blocking timeout
            self._ttl = math.ceil(blocking_timeout) + 1
        else:
            self._ttl = 0

    def _get_task_locks(self) -> typing.Dict[str, typing.Tuple[AIORedlock, int]]:
        """Get task-local lock storage, creating if needed."""
        try:
            return self._task_locks.get()
        except LookupError:
            # Create new dict for this task/context
            task_locks: typing.Dict[str, typing.Tuple[AIORedlock, int]] = {}
            self._task_locks.set(task_locks)
            return task_locks

    def locked(self) -> bool:
        """Return True if the current task/context holds this lock."""
        task_locks = self._get_task_locks()
        return self._name in task_locks and task_locks[self._name][1] > 0

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the distributed lock, reentrant per task.

        :param blocking: If False, return immediately if locked elsewhere.
        :param blocking_timeout: Max wait time when blocking (seconds).
        :return: True if lock acquired, False otherwise.
        """
        task_locks = self._get_task_locks()

        # (Reentrancy) If current task already owns lock, just increment counter
        name = self._name
        if name in task_locks:
            redlock, count = task_locks[name]
            task_locks[name] = (redlock, count + 1)
            return True

        # Create new `AIORedlock` instance for this acquisition
        redlock = AIORedlock(
            key=name,
            masters={self._redis},
            auto_release_time=self._ttl,
        )

        # Determine effective timeout
        if not blocking:
            effective_timeout = 1e-6  # 1µs for non-blocking
        elif blocking_timeout is not None:
            effective_timeout = blocking_timeout
        else:
            # If blocking timeout is set to None, then effective timeout is -1 to wait forever
            effective_timeout = (
                self._blocking_timeout if self._blocking_timeout is not None else -1
            )

        # Attempt to acquire the Redis lock
        try:
            acquired = await redlock.acquire(
                blocking=blocking, timeout=effective_timeout
            )
            if acquired:
                # Successfully acquired. Store lock object and count
                task_locks[name] = (redlock, 1)
                return True
            return False

        except (asyncio.TimeoutError, Exception):
            # Any exception during acquisition means we didn't get the lock
            return False

    async def release(self) -> None:
        """
        Release the lock once. Only when the reentrancy count reaches zero
        will the underlying Redis lock actually be released.
        """
        task_locks = self._get_task_locks()
        name = self._name
        if name not in task_locks:
            raise RuntimeError(
                f"Cannot release lock '{name}'. Lock not owned by current task"
            )

        redlock, count = task_locks[name]
        if count > 1:
            # Decrement reentrancy counter
            task_locks[name] = (redlock, count - 1)
            return

        # Fully release the lock
        try:
            await redlock.release()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nosec
            # Lock might have expired or been released already
            # Log and ignore release errors to avoid deadlocks
            sys.stderr.write(f"Warning: Failed to release lock '{name}': {str(exc)}\n")
            sys.stderr.flush()
        finally:
            del task_locks[name]

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class RedisBackend(ThrottleBackend[aioredis.Redis, HTTPConnectionT]):
    """
    Redis throttle backend.

    Uses `redis.asyncio` for backend operations.
    """

    wrap_methods = ("clear",)

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
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the Redis backend.

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
            If None, uses the global default from `traffik.utils.get_lock_blocking()`.
        :param lock_ttl: Default TTL for locks in seconds. If None, locks have
            no expiration unless specified during lock acquisition.
        :param lock_blocking_timeout: Default maximum time to wait for acquiring locks in seconds.
            If None, uses the global default from `traffik.utils.get_lock_blocking_timeout()`.
        :param lock_type: The type of Redis lock to use ("redis" or "redlock").
            - "redis": Uses a simple Redis-based lock suitable for single Redis instances.
            - "redlock": Uses the Redlock algorithm for distributed locking, suitable for
              Redis clusters or multiple Redis instances.
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
        """SHA hash of the registered Lua script for increment_with_ttl."""
        self._clear_sha: typing.Optional[str] = None
        """SHA hash of the registered Lua script for clear."""

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

    async def initialize(self) -> None:
        """Ensure the Redis connection is ready and register Lua scripts."""
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

    async def _scripts_ready(self) -> bool:
        """Check if all required Lua scripts are registered."""
        if self.connection is None:
            return False

        scripts_shas = []
        if self._increment_with_ttl_sha is not None:
            scripts_shas.append(self._increment_with_ttl_sha)

        # Check clear script
        if self._clear_sha is not None:
            scripts_shas.append(self._clear_sha)

        # Check lock scripts if using _AsyncRedisLock
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
            return await self._scripts_ready()
        except aioredis.RedisError:
            return False

    async def _ensure_increment_with_ttl_script(self) -> None:
        """Ensure the `increment_with_ttl` Lua script is registered."""
        if self._increment_with_ttl_sha is None:
            self._increment_with_ttl_sha = await self.connection.script_load(  # type: ignore
                type(self)._INCREMENT_WITH_TTL_SCRIPT
            )

    async def _ensure_clear_script(self) -> None:
        """Ensure the `clear` Lua script is registered."""
        if self._clear_sha is None:
            self._clear_sha = await self.connection.script_load(  # type: ignore
                type(self)._CLEAR_SCRIPT
            )

    async def _ensure_lock_scripts_shas(self) -> None:
        """Ensure the lock Lua scripts are registered into the shared dict."""
        if self._lock_script_shas is None and self.connection is not None:
            self._lock_script_shas = dict(  # type: ignore[assignment]
                acquire=await _AsyncRedisLock._load_acquire_script(self.connection),
                release=await _AsyncRedisLock._load_release_script(self.connection),
            )

    async def get_lock(self, name: str) -> typing.Union[_AsyncRedisLock, _AsyncRedLock]:
        """Returns a distributed Redis lock for the given name."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not self._use_redlock:
            return _AsyncRedisLock(
                name,
                redis=self.connection,
                script_shas=self._lock_script_shas,  # type: ignore[arg-type]
                ttl=self.lock_ttl,
                blocking_timeout=self.lock_blocking_timeout,
            )
        return _AsyncRedLock(
            name,
            redis=self.connection,
            ttl=self.lock_ttl,
            blocking_timeout=self.lock_blocking_timeout,
        )

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Get value by key."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )
        return await self.connection.get(key)

    async def set(
        self, key: str, value: typing.Any, expire: typing.Optional[int] = None
    ) -> None:
        """Set value by key with optional expiration."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if expire is not None:
            await self.connection.set(key, value, ex=expire)
        else:
            await self.connection.set(key, value)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """Delete key."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        deleted_count = await self.connection.delete(key)
        return deleted_count > 0

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment using Redis INCR/INCRBY."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if amount == 1:
            return await self.connection.incr(key)
        return await self.connection.incrby(key, amount)

    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement using Redis DECR/DECRBY."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if amount == 1:
            return await self.connection.decr(key)
        return await self.connection.decrby(key, amount)

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration using Redis EXPIRE."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        result = await self.connection.expire(key, seconds)
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
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

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
                    type(self)._INCREMENT_WITH_TTL_SCRIPT
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
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not keys:
            return []
        return await self.connection.mget(keys)

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
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not items:
            return

        async with self.connection.pipeline(transaction=True) as pipe:
            for key, value in items.items():
                if expire is not None:
                    pipe.set(key, value, ex=expire)
                else:
                    pipe.set(key, value)
            await pipe.execute()

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

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
                    type(self)._CLEAR_SCRIPT
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
            await self.connection.aclose()
            self.connection = None
