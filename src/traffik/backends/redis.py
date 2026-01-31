"""Redis implementation of the throttle backend using `redis.asyncio`."""

import asyncio  # noqa: I001
import contextvars
import math
import typing

from pottery import AIORedlock
import redis.asyncio as aioredis
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    ThrottleErrorHandler,
    HTTPConnectionT,
)
from traffik.utils import time


class AsyncRedisLock:
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

    __slots__ = (
        "_name",
        "_redis",
        "_blocking_timeout",
        "_release_timeout",
        "_release_sha",
    )

    def __init__(
        self,
        name: str,
        redis: aioredis.Redis,
        blocking_timeout: typing.Optional[float] = None,
        release_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the Redis lock.

        :param name: Unique lock name shared across processes.
        :param redis: An `aioredis.Redis` connection instance.
        :param blocking_timeout: Max time to wait when acquiring lock (default: None = wait forever).
        :param release_timeout: Lock expiration time in seconds (default 2.0s).
        """
        self._name = name
        self._redis = redis
        self._blocking_timeout = blocking_timeout
        self._release_timeout = math.ceil(release_timeout or 2.0)
        self._release_sha: typing.Optional[str] = None

    def _get_task_locks(self) -> dict[str, tuple[int, int]]:
        """
        Get or create the task-local map:
            lock_name -> (fence_token, reentrancy_count)
        """
        try:
            return self._task_locks.get()
        except LookupError:
            m: dict[str, tuple[int, int]] = {}
            self._task_locks.set(m)
            return m

    async def _ensure_release_script(self) -> None:
        if self._release_sha is None:
            self._release_sha = await self._redis.script_load(
                type(self)._RELEASE_SCRIPT
            )

    def locked(self) -> bool:
        locks = self._get_task_locks()
        return self._name in locks and locks[self._name][1] > 0

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        locks = self._get_task_locks()

        # Re-entrant fast-path (no Redis calls)
        if self._name in locks:
            token, count = locks[self._name]
            locks[self._name] = (token, count + 1)
            return True

        blocking_timeout = (
            self._blocking_timeout if blocking_timeout is None else blocking_timeout
        )

        start = time()
        attempts = 0
        while True:
            pipe = self._redis.pipeline(transaction=False)
            # Generate global fence token
            pipe.incr(f"{self._name}:fence")

            # Try to acquire lock using that token
            # Placeholder value; Redis will already have incremented before SET
            pipe.set(
                self._name,
                "0",  # overwritten logically by token returned from INCR
                nx=True,
                ex=self._release_timeout,
            )

            token, acquired = await pipe.execute()
            if acquired:
                locks[self._name] = (token, 1)
                return True

            if not blocking:
                return False

            if blocking_timeout is not None and (time() - start) >= blocking_timeout:
                return False

            # Exponential backoff with jitter
            attempts += 1
            delay = min(0.00001 * attempts, 0.01)
            await asyncio.sleep(delay)

    async def release(self) -> None:
        locks = self._get_task_locks()

        if self._name not in locks:
            raise RuntimeError(
                f"Cannot release lock '{self._name}'. Lock not owned by task."
            )

        token, count = locks[self._name]

        # Re-entrant. Decrement only
        if count > 1:
            locks[self._name] = (token, count - 1)
            return

        # Final release
        await self._ensure_release_script()
        try:
            await self._redis.evalsha(  # type: ignore
                self._release_sha,  # type: ignore[arg-type]
                1,  # num keys
                self._name,  # KEYS[1]
                str(token),  # ARGV[1]
            )
        except aioredis.ResponseError as exc:
            if "NOSCRIPT" in str(exc):
                # Script was flushed from Redis cache, re-register and retry
                # Reset SHA to force re-registration
                self._release_sha = None
                await self._ensure_release_script()
                await self._redis.evalsha(  # type: ignore
                    self._release_sha,  # type: ignore[arg-type]
                    1,  # num keys
                    self._name,  # KEYS[1]
                    str(token),  # ARGV[1]
                )
            else:
                raise
        finally:
            del locks[self._name]

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()


class AsyncRedLock:
    """
    Name-based reentrant distributed Redis (un-fair) lock implementing the `AsyncLock` protocol.

    Uses the `redlock` algorithm for distributed locking via the `pottery.AIORedlock` API.

    This is useful when utilizing reds cluster or multiple Redis instances and is mostlikely
    overkill for single Redis instance deployments. There will be serious performance overhead
    when compared to `AsyncRedisLock` due to the multiple Redis connections and network roundtrips
    involved in acquiring and releasing the lock.
    """

    _task_locks: contextvars.ContextVar[
        typing.Dict[str, typing.Tuple[AIORedlock, int]]
    ] = contextvars.ContextVar("_task_locks")
    """Per-task storage of lock objects and reentrancy counts."""

    def __init__(
        self,
        name: str,
        redis: aioredis.Redis,
        blocking_timeout: typing.Optional[float] = None,
        release_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the Redis lock.

        :param name: Unique lock name shared across processes.
        :param redis: An aioredis.Redis connection instance.
        :param blocking_timeout: Max time to wait when acquiring lock (default: None = wait forever).
        :param release_timeout: Lock expiration time in seconds (default 1.0s).
        """
        self._name = name
        self._redis = redis
        self._blocking_timeout = blocking_timeout
        self._release_timeout = release_timeout if release_timeout is not None else 1.0

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
        if self._name in task_locks:
            redlock, count = task_locks[self._name]
            task_locks[self._name] = (redlock, count + 1)
            return True

        # Create new `AIORedlock` instance for this acquisition
        redlock = AIORedlock(
            key=self._name,
            masters={self._redis},
            auto_release_time=self._release_timeout,
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
                task_locks[self._name] = (redlock, 1)
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
        if self._name not in task_locks:
            raise RuntimeError(
                f"Cannot release lock '{self._name}'. Lock not owned by current task"
            )

        redlock, count = task_locks[self._name]
        if count > 1:
            # Decrement reentrancy counter
            task_locks[self._name] = (redlock, count - 1)
            return

        # Fully release the lock
        try:
            await redlock.release()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nosec
            # Lock might have expired or been released already
            # Log and ignore release errors to avoid deadlocks
            print(f"Warning: Failed to release lock '{self._name}': {str(exc)}")
            pass
        finally:
            del task_locks[self._name]

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Redis lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


ConnectionGetter: TypeAlias = typing.Callable[[], typing.Awaitable[aioredis.Redis]]


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

    def __init__(
        self,
        connection: typing.Union[str, ConnectionGetter],
        *,
        namespace: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
        lock_type: typing.Literal["redis", "redlock"] = "redis",
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the Redis backend.

        :param connection: Redis connection URL or async getter function.
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
        :param lock_type: The type of Redis lock to use ("redis" or "redlock").
            - "redis": Uses a simple Redis-based lock suitable for single Redis instances.
            - "redlock": Uses the Redlock algorithm for distributed locking, suitable for
              Redis clusters or multiple Redis instances.
        """
        if isinstance(connection, str):

            async def _getter():
                return await aioredis.from_url(connection, decode_responses=False)

            self._get_connection = _getter
        else:
            self._get_connection = connection

        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
            on_error=on_error,
            **kwargs,
        )
        self._increment_with_ttl_sha: typing.Optional[str] = None
        """SHA hash of the registered Lua script for increment_with_ttl."""
        self._lock_cls = AsyncRedisLock if lock_type == "redis" else AsyncRedLock

    async def initialize(self) -> None:
        """Ensure the Redis connection is ready and register Lua scripts."""
        if self.connection is None:
            self.connection = await self._get_connection()
            try:
                await self.connection.ping()
                await self._ensure_increment_with_ttl_script()
            except aioredis.RedisError as exc:
                raise BackendConnectionError(
                    "Failed to initialize Redis connection."
                ) from exc

    async def ready(self) -> bool:
        if self.connection is None:
            return False

        try:
            await self.connection.ping()
            return True
        except aioredis.RedisError:
            return False

    async def _ensure_increment_with_ttl_script(self) -> None:
        """Ensure the `increment_with_ttl` Lua script is registered."""
        if self._increment_with_ttl_sha is None:
            self._increment_with_ttl_sha = await self.connection.script_load(  # type: ignore
                type(self)._INCREMENT_WITH_TTL_SCRIPT
            )

    async def get_lock(self, name: str) -> typing.Union[AsyncRedisLock, AsyncRedLock]:
        """Returns a distributed Redis lock for the given name."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )
        return self._lock_cls(
            name,
            redis=self.connection,
            blocking_timeout=0.1,
            release_timeout=3.0,
        )

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Get value by key."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        value = await self.connection.get(key)
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

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
        Atomic increment with TTL set only on first increment.

        Uses a pre-registered Lua script to ensure:
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

        if self._increment_with_ttl_sha is None:
            raise BackendError("Lua script not registered. Call `initialize()` first.")

        try:
            result = await self.connection.evalsha(  # type: ignore
                self._increment_with_ttl_sha,
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
                self._increment_with_ttl_sha = None
                await self._ensure_increment_with_ttl_script()
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
        Use Redis MGET for atomic batch retrieval.

        MGET is atomic in Redis - all values are retrieved at the same instant.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not keys:
            return []

        values = await self.connection.mget(keys)
        result: typing.List[typing.Optional[str]] = []
        for value in values:
            if value is None:
                result.append(None)
            else:
                result.append(
                    value.decode("utf-8") if isinstance(value, bytes) else str(value)
                )
        return result

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        keys = await self.connection.keys(f"{self.namespace}:*")
        if not keys:
            return
        await self.connection.delete(*keys)

    async def reset(self) -> None:
        """Reset all keys in the namespace."""
        await self.clear()

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.connection is not None:
            await self.connection.aclose()
            self.connection = None
