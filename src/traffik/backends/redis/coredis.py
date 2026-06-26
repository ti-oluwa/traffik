"""
Redis implementation of the throttle backend using `coredis`.

Supports all Redis deployment topologies:

- **Single node** - pass a `coredis.Redis` instance or a Redis URL string.
- **Redis Cluster** - pass a `coredis.RedisCluster` instance or a list of
  startup-node dicts / `TCPLocation` objects.
- **Sentinel** - pass a `coredis.Sentinel` instance together with a
  *service_name* so the backend can obtain a primary client.
"""

import asyncio
import math
import sys
import typing
from contextlib import AsyncExitStack
from types import TracebackType

if sys.version_info < (3, 10):
    raise ImportError(
        "`traffik.redis.coredis` is not supported for python<3.10. Upgrade or use a different backend"
    )

import coredis.exceptions
from coredis.client import Redis, RedisCluster
from coredis.commands import Script
from coredis.connection import TCPLocation
from coredis.patterns.lock import Lock as _CoredisLock
from coredis.sentinel import Sentinel
from coredis.typing import Node
from typing_extensions import Self

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

__all__ = ["RedisBackend"]


_AnyRedis = typing.Union[Redis[str], RedisCluster[str]]
"""Union of the two coredis client types that share a compatible command API."""


_INCREMENT_WITH_TTL_SCRIPT = """
-- Atomically increment a counter and set a TTL only on first creation.
--
-- KEYS[1]  counter key
-- ARGV[1]  increment amount (int string)
-- ARGV[2]  TTL in seconds   (int string)
--
-- Returns the new counter value.

local key    = KEYS[1]
local amount = tonumber(ARGV[1])
local ttl    = tonumber(ARGV[2])

local exists = redis.call('EXISTS', key)

if exists == 0 then
    -- New key: set value with TTL atomically.
    redis.call('SET', key, amount, 'EX', ttl)
    return amount
else
    -- Existing key: just increment; preserve existing TTL.
    if amount == 1 then
        return redis.call('INCR', key)
    else
        return redis.call('INCRBY', key, amount)
    end
end
"""

_CLEAR_SCRIPT = """
-- Scan and delete all keys matching a pattern.
-- Uses SCAN in a loop to avoid blocking Redis on large datasets.
--
-- ARGV[1]  glob pattern (e.g. "namespace:*")
--
-- Returns the total number of keys deleted.

local pattern = ARGV[1]
local cursor  = '0'
local deleted = 0

repeat
    local result = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 1000)
    cursor = result[1]
    local keys = result[2]
    if #keys > 0 then
        deleted = deleted + redis.call('DEL', unpack(keys))
    end
until cursor == '0'

return deleted
"""


@typing.final
class _AsyncCoredisLock:
    """
    Name-based, distributed Redis (un-fair) lock implementing the `AsyncLock` protocol.

    Support redis cluster (eventual replication), but still does not implement the redlock algorithm.

    Non-reentrant by default but optionally reentrant per task.

    Adapts `coredis.patterns.lock.Lock` to the `AsyncLock` protocol.

    Reentrancy is process-local only, i.e, the reentry counter is tracked in Python
    and only the outermost acquire/release round-trips to Redis. This means the
    Redis TTL governs the total hold time across all reentrant levels. If the
    key expires while reentrant holds are active, the lock is silently lost.
    Keep critical sections short or set a generous TTL.
    """

    __slots__ = (
        "_client",
        "_lock",
        "_name",
        "_owner",
        "_reentrant",
        "_reentry_count",
        "_sleep",
        "_ttl",
    )

    def __init__(
        self,
        name: str,
        client: _AnyRedis,
        ttl: typing.Optional[float] = None,
        sleep: float = 0.05,
        reentrant: bool = False,
    ) -> None:
        """
        Initialize the coredis lock adapter.

        :param name: Name of the lock. Different names correspond to different locks.
        :param client: An instance of `coredis.Redis` or `coredis.RedisCluster` to use for lock operations.
        :param ttl: Maximum lifetime of the lock in seconds. Setting this is **strongly recommended**
            in production to prevent deadlocks after process crashes.
            If `None`, the lock will persist until explicitly released.
        :param sleep: Seconds to sleep between acquisition attempts when the lock is held.
        :param reentrant: Whether to allow the same task to acquire the lock multiple times.
            Reentrancy is process-local only: the reentry counter is tracked in Python and only
            the outermost acquire/release round-trips to Redis. Defaults to False.
        """
        self._name = name
        self._client = client
        self._lock: typing.Optional[_CoredisLock] = None
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._reentry_count: int = 0
        self._reentrant = reentrant
        self._sleep = sleep
        self._ttl = math.ceil(ttl) if ttl is not None else 0

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
        Acquire the lock.

        :param blocking: If `False`, attempt once and return immediately.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Maximum seconds to wait when blocking. `None` means wait forever.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :return: `True` if acquired, `False` otherwise.
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

        lock = _CoredisLock(
            client=self._client,
            name=self._name,
            timeout=self._ttl,
            sleep=self._sleep,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )
        try:
            acquired = await lock.acquire()
        except coredis.exceptions.LockError as exc:
            raise LockAcquisitionError(
                f"Failed to acquire lock '{self._name}'"
            ) from exc

        if acquired:
            self._lock = lock
            self._owner = current
            self._reentry_count = 1
        return acquired

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
        lock = self._lock
        try:
            await lock.release()  # type: ignore[union-attr]
        except coredis.exceptions.LockError as exc:
            # Lock might have expired or been released already
            raise LockReleaseError(f"Failed to release lock '{self._name}'") from exc
        finally:
            # Clear ownership regardless of whether Redis release succeeded.
            # If Redis release failed, the key will expire via TTL.
            self._lock = None
            self._owner = None
            self._reentry_count = 0

    async def __aenter__(self) -> Self:
        if not await self.acquire():
            raise LockAcquisitionError(f"Could not acquire Redis lock '{self._name}'.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        await self.release()


class RedisBackend(ThrottleBackend[_AnyRedis, HTTPConnectionT]):
    """
    Redis throttle backend using `coredis` client.

    Supports three topologies via the *connection* argument:

    **Single node (default)**:

    ```python
    backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
    ```

    **Redis Cluster**:

    ```python
    from coredis import RedisCluster
    from coredis.connection import TCPLocation

    client = RedisCluster(
        startup_nodes=[TCPLocation("127.0.0.1", 7000)],
        decode_responses=True,
    )
    backend = RedisBackend(client, namespace="myapp")
    ```

    Or pass a list of startup-node dicts / `TCPLocation` objects and let the
    backend build the cluster client for you:

    ```python
    backend = RedisBackend(
        [TCPLocation("127.0.0.1", 7000), TCPLocation("127.0.0.1", 7001)],
        namespace="myapp",
    )
    ```

    **Sentinel**:
    ```python
    from coredis import Sentinel

    sentinel = Sentinel([("sentinel-host", 26379)], stream_timeout=0.1)
    backend = RedisBackend(
        sentinel,
        sentinel_service_name="myredis",
        namespace="myapp",
    )
    ```
    """

    wrap_methods: typing.Tuple[str, ...] = ("clear",)

    INCREMENT_WITH_TTL_SCRIPT: typing.ClassVar[str] = _INCREMENT_WITH_TTL_SCRIPT
    """Lua script for atomic increment with TTL on first creation."""
    CLEAR_SCRIPT: typing.ClassVar[str] = _CLEAR_SCRIPT
    """Lua script for non-blocking deletion of all keys matching a pattern."""

    def __init__(
        self,
        connection: typing.Union[
            str,
            _AnyRedis,
            Sentinel,
            typing.Sequence[
                typing.Union[TCPLocation, typing.Dict[str, typing.Any], Node]
            ],
            typing.Callable[[], typing.Awaitable[_AnyRedis]],
        ],
        *,
        namespace: str,
        sentinel_service_name: typing.Optional[str] = None,
        sentinel_stream_timeout: typing.Optional[float] = None,
        cluster_kwargs: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        url_kwargs: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, typing.Any]
        ] = None,
        persistent: bool = False,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        lock_blocking: typing.Optional[bool] = None,
        lock_ttl: typing.Optional[float] = None,
        lock_blocking_timeout: typing.Optional[float] = None,
        lock_sleep: float = 0.0025,
        lock_contention_threshold: int = 1,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the `coredis` Redis backend.

        :param connection: How to connect. One of:

            - A Redis URL string (`"redis://host:port/db"` or `"rediss://..."` for TLS).
            - A pre-built `coredis.Redis` or `coredis.RedisCluster` instance.
                Must have `decode_responses=True`.
            - A `coredis.Sentinel` instance (also supply `sentinel_service_name`).
            - A list of `TCPLocation` objects or dicts with `host`/`port`
                keys to build a `RedisCluster` automatically.
            - An async factory callable `() -> typing.Union[Redis, RedisCluster]`.

        :param namespace: Key prefix for all throttle keys.
        :param sentinel_service_name: Required when *connection* is a `Sentinel` instance.
            Names the Redis service to monitor.
        :param sentinel_stream_timeout: Optional socket read timeout for Sentinel-managed connections.
        :param cluster_kwargs: Extra keyword arguments forwarded to the
            `RedisCluster` constructor when *connection* is a list of nodes.
        :param url_kwargs: Extra keyword arguments forwarded to `coredis.Redis.from_url()`
            when *connection* is a URL string.
        :param identifier: Connected client identifier generator.
        :param handle_throttled: Handler called when a connection is throttled.
        :param persistent: Whether to preserve throttle data on context exit.
        :param on_error: Error strategy - `"allow"`, `"throttle"`, `"raise"`, or a custom async callable.
        :param lock_blocking: Default blocking mode for all distributed locks.
        :param lock_ttl: Maximum lifetime of each distributed lock in seconds.
            Setting this is **strongly recommended** in production to prevent
            deadlocks after process crashes.
        :param lock_blocking_timeout: Maximum seconds to wait when acquiring a lock.
            `None` means block forever.
        :param lock_sleep: Seconds to sleep between acquisition attempts when the lock is held.
            Smaller values reduce latency but increase Redis load. Default `0.05` (50 ms).
        :param lock_contention_threshold: The threshold for the process-local contention serialization gate.
            When the number of waiters for a lock exceeds this threshold, new acquirers will be serialized through
            an `asyncio.Lock` to reduce Redis connection hammering and thundering herd issues.
        :param kwargs: Additional keyword arguments forwarded to the base `ThrottleBackend`.
        """
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

        self._raw_connection = connection
        self._sentinel_service_name = sentinel_service_name
        self._sentinel_stream_timeout = sentinel_stream_timeout
        self._cluster_kwargs: typing.Dict[str, typing.Any] = dict(cluster_kwargs or {})
        self._url_kwargs: typing.Dict[str, typing.Any] = dict(url_kwargs or {})
        self._lock_sleep = lock_sleep
        self._is_cluster: bool = False

        # `True` when the backend is built the client itself and is therefore
        # responsible for closing it. `False` when a pre-built client was
        # handed in (Redis/RedisCluster instance, Sentinel primary, or async
        # factory result). In that case, close() skips aclose() so the caller
        # retains full control over the client lifetime.
        self._owns_connection: bool = False

        # Lua Script objects will be set during initialize()
        self._increment_with_ttl_script: typing.Optional[Script] = None
        self._clear_script: typing.Optional[Script] = None
        self._lock_contention_threshold = lock_contention_threshold
        self._named_gate_registry: typing.Optional[_NamedGateRegistry] = None
        self._exit_stack: typing.Optional[AsyncExitStack] = None

    def _assert_ready(self) -> None:
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized before use."
            )

    async def _build_client(self) -> _AnyRedis:
        """Construct and return a ready-to-use coredis client."""
        raw = self._raw_connection

        if callable(raw) and not isinstance(raw, (Redis, RedisCluster, Sentinel)):
            self._owns_connection = True
            return await raw()

        if isinstance(raw, (Redis, RedisCluster)):
            if not getattr(raw, "decode_responses", True):
                raise ValueError(
                    "The coredis client must be created with decode_responses=True."
                )
            self._owns_connection = False
            return typing.cast(_AnyRedis, raw)

        if isinstance(raw, Sentinel):
            if not self._sentinel_service_name:
                raise ValueError(
                    "`sentinel_service_name` is required when using a `Sentinel` connection."
                )

            kwargs: typing.Dict[str, typing.Any] = {"decode_responses": True}
            if self._sentinel_stream_timeout is not None:
                kwargs["stream_timeout"] = self._sentinel_stream_timeout
            self._owns_connection = False
            return typing.cast(
                _AnyRedis,
                raw.primary_for(service_name=self._sentinel_service_name, **kwargs),  # type: ignore
            )

        if isinstance(raw, (list, tuple)):
            nodes = []
            for item in raw:
                if isinstance(item, TCPLocation):
                    nodes.append(item)
                elif isinstance(item, dict):
                    nodes.append(TCPLocation(item["host"], int(item.get("port", 6379))))
                else:
                    raise TypeError(
                        f"Unsupported node specifier type: {type(item)!r}. "
                        "Expected `TCPLocation` or dict with 'host'/'port' keys."
                    )

            self._owns_connection = True
            return RedisCluster(
                startup_nodes=nodes,
                decode_responses=True,
                **self._cluster_kwargs,
            )

        if isinstance(raw, str):
            self._owns_connection = True
            client = Redis.from_url(
                raw,
                decode_responses=True,
                **self._url_kwargs,
            )
            return client

        raise TypeError(f"Unsupported connection argument type: {type(raw)!r}.")

    async def initialize(self) -> None:
        """
        Create the Redis client (if not already done) and register Lua scripts.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self.connection is None:
            client = await self._build_client()
            self._is_cluster = isinstance(client, RedisCluster)
            self._exit_stack = AsyncExitStack()
            self.connection = await self._exit_stack.enter_async_context(client)

            # Script object that transparently handles NOSCRIPT errors.
            self._increment_with_ttl_script = client.register_script(
                self.INCREMENT_WITH_TTL_SCRIPT
            )
            self._clear_script = client.register_script(self.CLEAR_SCRIPT)

        if self._named_gate_registry is None or self._named_gate_registry.closed:
            self._named_gate_registry = _NamedGateRegistry(
                contention_threshold=self._lock_contention_threshold,
            )

    async def ready(self) -> bool:
        if self.connection is None:
            return False
        try:
            await self.connection.ping()
            return True
        except Exception:
            return False

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

    def get_lock(
        self, name: str, ttl: typing.Optional[float] = None, reentrant: bool = False
    ) -> _GatedNamedLock[_AsyncCoredisLock]:
        """
        Return a distributed lock for the given name backed by `coredis.patterns.lock.Lock`.
        """
        self._assert_ready()
        lock = _AsyncCoredisLock(
            client=self.connection,  # type: ignore[arg-type]
            name=name,
            ttl=ttl,
            sleep=self._lock_sleep,  # polling interval
            reentrant=reentrant,
        )
        return _GatedNamedLock(
            lock=lock,
            name=name,
            registry=self._named_gate_registry,  # type: ignore[arg-type]
        )

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Return the string value stored at *key*, or `None` if absent."""
        self._assert_ready()
        value = await self.connection.get(key)  # type: ignore[union-attr]
        return value  # type: ignore[return-value]

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """Set *key* to *value* with an optional expiry in seconds."""
        self._assert_ready()
        if expire is not None:
            await self.connection.set(key, value, px=expire * 1000)  # type: ignore[union-attr]
        else:
            await self.connection.set(key, value)  # type: ignore[union-attr]

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """Delete *key*. Returns `True` if the key existed."""
        self._assert_ready()
        deleted_count = await self.connection.delete([key])  # type: ignore[union-attr]
        return bool(deleted_count)

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment *key* by *amount* and return the new value."""
        self._assert_ready()
        if amount == 1:
            return await self.connection.incr(key)  # type: ignore[union-attr]
        if amount == -1:
            return await self.connection.decr(key)  # type: ignore[union-attr]
        if amount > 0:
            return await self.connection.incrby(key, amount)  # type: ignore[union-attr]
        # If amount < 0, we use DECRBY with the absolute value
        return await self.connection.decrby(key, -amount)  # type: ignore[union-attr]

    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement *key* by *amount*."""
        self._assert_ready()
        if amount == 1:
            return await self.connection.decr(key)  # type: ignore[union-attr]
        return await self.connection.decrby(key, amount)  # type: ignore[union-attr]

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set a TTL on *key*. Returns `True` if the key exists and TTL was set.
        """
        self._assert_ready()
        result = await self.connection.expire(key, seconds)  # type: ignore[union-attr]
        return bool(result)

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment *key* and set *ttl* **only when the key is new**.

        Uses a Lua script so the check-and-set is performed in a single
        server-side round-trip with no race conditions. Subsequent increments
        within the same window preserve the original expiry.
        """
        self._assert_ready()
        assert self._increment_with_ttl_script is not None
        result = await self._increment_with_ttl_script(
            keys=[key],
            args=[str(amount), str(ttl)],
        )
        return int(result)  # type: ignore[arg-type]

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Retrieve multiple keys in a single `MGET` command.

        For cluster deployments, this is best-effort as keys may reside on
        different shards served at slightly different times.
        """
        self._assert_ready()
        if not keys:
            return []

        values = await self.connection.mget(list(keys))  # type: ignore[union-attr]
        # `coredis` mget returns a list containing None for missing keys
        return list(values)  # type: ignore[arg-type]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Set multiple keys in one operation.

        **For single node**, we use `MULTI`/`EXEC` for an atomic transaction.
        **For redis cluster**, we use a plain pipeline (no `MULTI`) because
        cross-slot transactions are not supported in cluster mode.
        Each individual `SET` is atomic; the batch as a whole is not.
        """
        self._assert_ready()
        if not items:
            return

        if self._is_cluster:
            # Non-transactional pipeline. `coredis` routes each command to the appropriate shard.
            async with self.connection.pipeline(transaction=False) as pipe:  # type: ignore[union-attr]
                pending = []
                for key, value in items.items():
                    if expire is not None:
                        pending.append(pipe.set(key, value, ex=expire))
                    else:
                        pending.append(pipe.set(key, value))
                # Execute the pipeline; results are awaitable after exit.
            # We don't need the return values here; errors would propagate.
        else:
            # Transactional pipeline for single node / sentinel primaries.
            async with self.connection.pipeline(transaction=True) as pipe:  # type: ignore[union-attr]
                for key, value in items.items():
                    if expire is not None:
                        pipe.set(key, value, ex=expire)
                    else:
                        pipe.set(key, value)
                # Pipeline executes on context exit and exceptions propagate normally.

    async def clear(self) -> None:
        """
        Delete all keys whose name starts with `namespace:`.

        Uses the `_CLEAR_SCRIPT` Lua `SCAN` loop so Redis is never blocked,
        even for very large datasets.

        On Redis Cluster, `SCAN` is routed to **each primary** automatically
        by `coredis` and keys are deleted node-by-node.
        """
        self._assert_ready()
        assert self._clear_script is not None
        pattern = f"{self.namespace}:*"
        await self._clear_script(keys=[], args=[pattern])

    async def reset(self) -> None:
        """Reset throttle state by clearing all namespace keys."""
        await self.clear()

    async def close(self) -> None:
        """
        Release resources and clean up.

        If the backend built the client itself (from a URL string, a list of
        startup nodes, or an async factory), `aclose()` is called on the
        connection pool so sockets are returned to the OS.

        If a pre-built `Redis` or `RedisCluster` instance was passed in, or
        if the connection was obtained from a `Sentinel`, the caller retains
        ownership and `aclose()` is **not** called. The client remains
        usable by any other code that holds a reference to it.

        After this method returns, `connection` is set to `None` and the
        backend must be re-initialized before further use.
        """
        if (
            self.connection is not None
            and self._owns_connection
            and self._exit_stack is not None
        ):
            try:
                # Close the connection pool and all underlying sockets. Safe to call multiple times.
                await self._exit_stack.aclose()
            except coredis.exceptions.RedisError as exc:
                sys.stderr.write(
                    f"Warning: error while closing coredis connection: {exc}\n"
                )
                sys.stderr.flush()
            finally:
                self._exit_stack = None

        self.connection = None
        self._increment_with_ttl_script = None
        self._clear_script = None

        if self._named_gate_registry is not None:
            self._named_gate_registry.close()
            self._named_gate_registry = None
