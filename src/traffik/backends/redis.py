"""Redis implementation of the throttle backend using `redis.asyncio`."""

import asyncio
import contextvars
import typing

import redis.asyncio as aioredis
from pottery import AIORedlock
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)


class AsyncRedisLock:
    """
    Name-based reentrant distributed Redis lock implementing the `AsyncLock` protocol.

    Uses the `redlock` algorithm for distributed locking via the `pottery.AIORedlock` API.
    """

    # Per-task storage of lock objects and counts
    _task_locks: contextvars.ContextVar[
        typing.Dict[str, typing.Tuple[AIORedlock, int]]
    ] = contextvars.ContextVar("_task_locks")

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
        :param release_timeout: Lock expiration time in seconds (default 10s).
        """
        self._name = name
        self._redis = redis
        self._blocking_timeout = blocking_timeout
        self._release_timeout = release_timeout if release_timeout is not None else 10.0

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

        # Reentrancy: if current task already owns lock, just increment counter
        if self._name in task_locks:
            lock_obj, count = task_locks[self._name]
            task_locks[self._name] = (lock_obj, count + 1)
            return True

        # Create new AIORedlock instance for this acquisition
        lock_obj = AIORedlock(
            key=self._name,
            masters={self._redis},
            auto_release_time=self._release_timeout,
        )

        # Determine effective timeout
        if not blocking:
            effective_timeout = 0.001  # 1ms for non-blocking
        elif blocking_timeout is not None:
            effective_timeout = blocking_timeout
        else:
            # If blocking timeout is set to None, then effective timeout is -1 to wait forever
            effective_timeout = (
                self._blocking_timeout if self._blocking_timeout is not None else -1
            )

        # Attempt to acquire the Redis lock
        try:
            acquired = await lock_obj.acquire(timeout=effective_timeout)
            if acquired:
                # Successfully acquired - store lock object and count
                task_locks[self._name] = (lock_obj, 1)
                return True
            else:
                return False

        except asyncio.CancelledError:
            raise
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

        lock_obj, count = task_locks[self._name]
        if count > 1:
            # Decrement reentrancy counter
            task_locks[self._name] = (lock_obj, count - 1)
            return

        # Fully release the lock
        try:
            await lock_obj.release()
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
    ) -> None:
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
        )
        self._increment_with_ttl_sha: typing.Optional[str] = None
        """SHA hash of the registered Lua script for increment_with_ttl."""

    async def initialize(self) -> None:
        """Ensure the Redis connection is ready and register Lua scripts."""
        if self.connection is None:
            self.connection = await self._get_connection()
            await self.connection.ping()
            # Register the increment_with_ttl Lua script
            self._increment_with_ttl_sha = await self.connection.script_load(
                self._INCREMENT_WITH_TTL_SCRIPT
            )

    async def get_lock(self, name: str) -> AsyncRedisLock:
        """Returns a distributed Redis lock for the given name."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )
        return AsyncRedisLock(name, redis=self.connection, blocking_timeout=10.0)

    async def get(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Optional[str]:
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
            raise BackendError("Lua script not registered. Call initialize() first.")

        try:
            result = await self.connection.execute_command(
                "EVALSHA",
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
                self._increment_with_ttl_sha = await self.connection.script_load(
                    self._INCREMENT_WITH_TTL_SCRIPT
                )
                result = await self.connection.execute_command(
                    "EVALSHA",
                    self._increment_with_ttl_sha,
                    1,
                    key,
                    str(amount),
                    str(ttl),
                )
                return int(result) if result is not None else 0

            # Re-raise other Redis errors
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
