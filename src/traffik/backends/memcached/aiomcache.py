"""Memcached implementation of a throttle backend using `aiomcache`."""

import asyncio
import functools
import math
import sys
import typing
from time import monotonic
from types import TracebackType

import aiomcache
from aiomcache.exceptions import ClientException

from traffik._locks import _GatedNamedLock, _NamedGateRegistry, get_token
from traffik.backends.base import ThrottleBackend
from traffik.backends.memcached._utils import _parse_memcached_url
from traffik.exceptions import (
    BackendConnectionError,
    BackendError,
    LockAcquisitionError,
    LockReleaseError,
)
from traffik.typing import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    P,
    R,
    T,
    ThrottleErrorHandler,
)


def _on_error_return(
    func: typing.Callable[P, typing.Awaitable[R]],
    return_value: typing.Optional[T] = None,
    predicate: typing.Optional[typing.Callable[[Exception], bool]] = None,
) -> typing.Callable[P, typing.Awaitable[typing.Union[R, T, None]]]:
    """
    Decorator to catch `aiomcache.ClientException` and return a specified value.
    Used for handling transient Memcached errors gracefully.

    :param func: Async function to wrap.
    :param return_value: Value to return on exception (default None).
    :param predicate: Optional callable to check if the exception should be suppressed.
    It takes the exception as input and returns a boolean.
        If provided, the exception is only suppressed and `return_value` is returned if `predicate(exc)`
        returns True. Otherwise, the exception is re-raised. This allows for more fine-grained control
        over which exceptions to suppress, such as only suppressing certain error codes or messages.
        e.g. `predicate(exc) -> bool`. Else, the exception is re-raised.
    :param log: Whether to log the exception (default False).
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> typing.Union[R, T, None]:
        try:
            return await func(*args, **kwargs)
        except ClientException as exc:
            if predicate and not predicate(exc):
                raise
            return return_value

    return wrapper


def _wrap_methods_with_on_error_return(
    obj: typing.Any,
    methods: typing.Iterable[str],
    return_value: typing.Optional[T] = None,
    predicate: typing.Optional[typing.Callable[[Exception], bool]] = None,
) -> None:
    """
    Wrap specified methods of the object with `_on_error_return` decorator.

    :param obj: Object whose methods to wrap.
    :param methods: Iterable of method names to wrap.
    :param return_value: Value to return on exception (default None).
    :param predicate: Optional callable to check if the exception should be suppressed.
        See `_on_error_return` for details.
    """
    for method_name in methods:
        if hasattr(obj, method_name):
            original_method = getattr(obj, method_name)
            wrapped_method = _on_error_return(
                original_method,
                return_value=return_value,
                predicate=predicate,
            )
            setattr(obj, method_name, wrapped_method)


@typing.final
class _AsyncMemcachedLock:
    """
    Name-based, best-effort, distributed (un-fair) lock implementing the `AsyncLock` protocol.

    Non-reentrant by default but optionally reentrant per task.

    Uses Memcached's atomic `add()` which only succeeds if the key does not exist,
    providing a lightweight distributed locking mechanism.

    Reentrancy is process-local only, i.e, the reentry counter is tracked in Python
    and only the outermost acquire/release round-trips to Memcached. This means the
    TTL governs the total hold time across all reentrant levels. If the key expires
    while reentrant holds are active, the lock is silently lost.
    Keep critical sections short or set a generous TTL.
    """

    __slots__ = (
        "_client",
        "_max_spins_before_backoff",
        "_name",
        "_name_bytes",
        "_owner",
        "_reentrant",
        "_reentry_count",
        "_spin_max_delay_seconds",
        "_token",
        "_ttl",
    )

    def __init__(
        self,
        name: str,
        client: aiomcache.Client,
        ttl: typing.Optional[float] = None,
        max_spins_before_backoff: int = 4,
        spin_max_delay_seconds: float = 0.01,
        reentrant: bool = False,
    ) -> None:
        """
        Initialize the lock.

        :param name: Unique lock name.
        :param client: `aiomcache.Client` instance.
        :param ttl: How long to hold the lock in seconds before auto-release.
            If None, lock has no expiration. This is bad practice and may lead to deadlocks.
        :param max_spins_before_backoff: Number of zero-delay yields to the event-loop during acquisition,
            before applying exponential backoff.
        :param spin_max_delay_seconds: Maximum delay in seconds during backoff.
        :param reentrant: Whether to allow the same task to acquire the lock multiple times.
            Reentrancy is process-local only, i.e, the reentry counter is tracked in Python and only
            the outermost acquire/release round-trips to Memcached. Defaults to False.
        """
        self._name = name
        self._name_bytes = name.encode("utf-8")
        self._client = client
        self._ttl = math.ceil(ttl) if ttl is not None else 0
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._token: typing.Optional[str] = None
        self._reentry_count = 0
        self._reentrant = reentrant
        self._max_spins_before_backoff = max_spins_before_backoff
        self._spin_max_delay_seconds = spin_max_delay_seconds

    def is_owner(self, task: typing.Optional[asyncio.Task] = None) -> bool:
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
        Acquire the distributed lock.

        :param blocking: If False, return immediately if locked elsewhere.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Max wait time when blocking. `None` means wait forever.
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

        # Memcached has no way to generate fencing tokens natively,
        # we generate our own unique token per acquisition attempt.
        # This helps prevent the "stale lock" problem but only
        # per process, not cross-process if clocks are skewed across processes.
        token = get_token()
        start = monotonic()
        attempts = 0
        max_spins = self._max_spins_before_backoff
        spin_max_delay = self._spin_max_delay_seconds
        has_blocking_timeout = blocking_timeout is not None
        while True:
            # `add()` is atomic, as only succeeds if key doesn't exist
            # Returns True if added, False if key already exists
            try:
                added = await self._client.add(
                    self._name_bytes,
                    token.encode("utf-8"),
                    exptime=self._ttl,
                )
            except ClientException as exc:
                raise LockAcquisitionError(
                    f"Failed to acquire lock '{self._name}'"
                ) from exc

            if added:
                self._owner = current
                self._token = token
                self._reentry_count = 0
                return True

            # Lock is held by someone else
            if not blocking:
                return False

            # Check timeout
            if has_blocking_timeout and (monotonic() - start) >= blocking_timeout:  # type: ignore
                return False

            attempts += 1
            if attempts <= max_spins:
                await asyncio.sleep(0)
            else:
                # Exponential backoff
                exponent = min(attempts - max_spins, 6)
                delay = min(0.0005 * (1 << exponent), spin_max_delay)
                await asyncio.sleep(delay)

    async def release(self) -> None:
        """
        Release the lock once.

        Only when the reentrancy count reaches zero will the underlying Memcached key
        actually be deleted.
        """
        current = asyncio.current_task()
        if not self.is_owner(task=current):
            raise LockReleaseError(
                f"Cannot release lock '{self._name}': "
                f"current task {current!r} does not own the lock "
                f"(owner: {self._owner!r})."
            )

        # Reentrant inner release. Just decrement the counter
        if self._reentry_count > 0:
            self._reentry_count -= 1
            return

        # Outermost release. Attempt Memcached release, but clear ownership regardless of outcome
        name_bytes = self._name_bytes
        token = self._token
        try:
            # Ensure we only release/delete if we own the lock (token matches)
            current = await self._client.get(name_bytes)
            if current and current.decode("utf-8") == token:
                await self._client.delete(name_bytes)
            else:
                sys.stderr.write(f"Warning: Lock '{self._name}' expired or stolen\n")
        except ClientException as exc:  # nosec
            # Lock might have expired or been released already
            raise LockReleaseError(f"Failed to release lock '{self._name}'") from exc
        finally:
            # Clear ownership regardless of whether Memcached release succeeded.
            # If release failed, the key will expire via TTL.
            self._owner = None
            self._token = None
            self._reentry_count = 0
            sys.stderr.flush()

    async def __aenter__(self):
        if not await self.acquire():
            raise LockAcquisitionError(
                f"Could not acquire Memcached lock '{self._name}'"
            )
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ):
        await self.release()


class MemcachedBackend(ThrottleBackend[aiomcache.Client, HTTPConnectionT]):
    """
    Memcached-based throttle backend.

    Uses `aiomcache` for async Memcached operations. Does not support multi-node setup.

    Note: Memcached has a key size limit of 250 bytes.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        url: typing.Optional[str] = None,
        host: str = "localhost",
        port: int = 11211,
        *,
        pool_size: int = 2,
        pool_minsize: int = 1,
        connection_args: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        namespace: str = ":memcached:",
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
        lock_contention_threshold: int = 1,
        track_keys: bool = False,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the `aiomcache` Memcached backend.

        :param host: Memcached server host.
        :param port: Memcached server port.
        :param namespace: Key prefix namespace.
        :param pool_size: Connection pool size.
        :param pool_minsize: Minimum pool size.
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
            :param lock_contention_threshold: The threshold for the process-local contention serialization gate.
            When the number of waiters for a lock exceeds this threshold, new acquirers will be serialized through
            an `asyncio.Lock` to reduce Memcached connection hammering and thundering herd issues.
        :param track_keys: Whether to track all keys in the namespace for clearing.
            Since Memcached doesn't support key listing like Redis, this enables
            a best-effort tracking mechanism using a special tracking key to store
            all keys set by this backend. This allows the `clear()` method to function.
            Note: This adds overhead to cache operations and is not 100% reliable.
            Only use if you absolutely need the `clear()` functionality.
            `clear()` will be no-op if this is False.
        :param kwargs: Additional keyword arguments.
        """
        if url and (host != "localhost" or port != 11211):
            raise ValueError("Specify either 'url' or 'host'/'port', not both.")

        if url is not None:
            parsed = _parse_memcached_url(url)
            host = parsed["host"]
            port = parsed["port"]

        self.host = host
        self.port = port
        self.pool_size = pool_size
        self.pool_minsize = pool_minsize
        self.connection_args = connection_args
        self.track_keys = track_keys
        self._tracking_key = f"{namespace}:__tracked_keys__"
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
        self._lock_contention_threshold = lock_contention_threshold
        self._named_gate_registry: typing.Optional[_NamedGateRegistry] = None

    async def _track_key(self, key: str) -> None:
        """Best-effort add key to tracking set."""
        if self.connection is None:
            return

        if "||" in key:
            sys.stderr.write(
                f"Warning: Key '{key}' contains '||' character which is used as separator in tracking."
                " Ensure keys do not contain this sequence.\n"
            )
            sys.stderr.flush()
            # There's no use tracking this key as it will break the tracking mechanism
            return

        tracking_key = self._tracking_key
        try:
            tracked = await self.connection.get(tracking_key.encode())
            if tracked is None:
                await self.connection.set(
                    tracking_key.encode(),
                    key.encode(),
                    exptime=0,
                )
            else:
                keys_set = set(tracked.decode().split("||"))
                if key not in keys_set:
                    keys_set.add(key)
                    new_tracked = "||".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
        except Exception as exc:
            sys.stderr.write(f"Warning: Failed to track key '{key}': {exc}\n")
            sys.stderr.flush()

    async def _untrack_key(self, key: str) -> None:
        """Best-effort remove key from tracking set."""
        if self.connection is None:
            return

        tracking_key = self._tracking_key
        try:
            tracked = await self.connection.get(tracking_key.encode())
            if tracked is None:
                return

            keys_set = set(tracked.decode().split("||"))
            if key in keys_set:
                keys_set.remove(key)
                if keys_set:
                    new_tracked = "||".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
                else:
                    await self.connection.delete(tracking_key.encode())
        except Exception as exc:
            sys.stderr.write(f"Warning: Failed to untrack key '{key}': {exc}\n")
            sys.stderr.flush()

    async def initialize(self) -> None:
        """Initialize the Memcached connection."""
        if self.connection is None:
            self.connection = aiomcache.Client(
                self.host,
                self.port,
                pool_size=self.pool_size,
                pool_minsize=self.pool_minsize,
                conn_args=self.connection_args,
            )
            # If key doesn't exist, `incr` & `decr` raises a specific
            # `ClientException`. We catch that and return None instead
            _wrap_methods_with_on_error_return(
                self.connection,
                methods=["incr", "decr"],
                predicate=lambda exc: b"NOT_FOUND" in str(exc).encode(),
                return_value=None,
            )

        if self._named_gate_registry is None or self._named_gate_registry.closed:
            self._named_gate_registry = _NamedGateRegistry(
                contention_threshold=self._lock_contention_threshold,
            )

    async def ready(self) -> bool:
        if self.connection is None:
            return False

        try:
            await self.connection.version()
            return True
        except ClientException:
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
    ) -> _GatedNamedLock[_AsyncMemcachedLock]:
        """
        Get a distributed lock for the given name.

        :param name: Lock name.
        :return: `_AsyncMemcachedLock` instance.
        """
        self._assert_ready()
        lock = _AsyncMemcachedLock(
            name,
            client=self.connection,  # type: ignore[arg-type]
            ttl=ttl,
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
        """
        Get value by key.

        :param key: Key to retrieve.
        :return: Value as string, or None if not found.
        """
        self._assert_ready()

        value = await self.connection.get(key.encode())  # type: ignore[union-attr]
        if value is None:
            return None
        return value.decode()

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """
        Set value by key.

        :param key: Key to set.
        :param value: Value to store.
        :param expire: Optional TTL in seconds.
        """
        self._assert_ready()

        exptime = int(expire) if expire is not None else 0
        await self.connection.set(  # type: ignore[union-attr]
            key.encode(),
            str(value).encode(),
            exptime=exptime,
        )
        if self.track_keys:
            await self._track_key(key)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """
        Delete key if exists.

        :param key: Key to delete.
        :return: True if deleted, False if not found.
        """
        self._assert_ready()

        deleted = await self.connection.delete(key.encode())  # type: ignore[union-attr]
        if deleted and self.track_keys:
            await self._untrack_key(key)
        return deleted

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        Memcached's `incr` is atomic and thread-safe across all clients.

        :param key: Counter key.
        :param amount: Amount to increment by.
        :return: New value after increment.
        """
        self._assert_ready()

        # Try to increment existing counter
        encoded_key = key.encode()
        new_value = await self.connection.incr(encoded_key, amount)  # type: ignore[union-attr]
        if new_value is not None:
            return new_value

        # Key doesn't exist, initialize it
        # Use add() to atomically create if not exists
        added = await self.connection.add(  # type: ignore[union-attr]
            encoded_key,
            str(amount).encode(),
            exptime=0,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return amount

        # Someone else created it, try increment again
        new_value = await self.connection.incr(encoded_key, amount)  # type: ignore[union-attr]
        return new_value  # type: ignore[return-value]

    async def decrement(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement counter.

        Memcached's `decr` is atomic and thread-safe across all clients.
        Note: Memcached counters cannot go below 0.

        :param key: Counter key.
        :param amount: Amount to decrement by.
        :return: New value after decrement.
        """
        self._assert_ready()

        # Try to decrement existing counter
        encoded_key = key.encode()
        new_value = await self.connection.decr(encoded_key, amount)  # type: ignore[union-attr]
        if new_value is not None:
            return new_value

        # Key doesn't exist, initialize it to `0 - amount`
        # Use `add()` to atomically create if not exists
        added = await self.connection.add(  # type: ignore[union-attr]
            encoded_key,
            str(-amount).encode(),
            exptime=0,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return -amount

        # Someone else created it, try decrement again
        new_value = await self.connection.decr(encoded_key, amount)  # type: ignore[union-attr]
        return new_value  # type: ignore[return-value]

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        Note: Memcached doesn't have a native "expire" command.
        We need to get the value and set it again with new TTL.

        :param key: Key to set expiration on.
        :param seconds: TTL in seconds.
        :return: True if expiration was set, False if key doesn't exist.
        """
        self._assert_ready()

        # Get current value
        encoded_key = key.encode()
        value = await self.connection.get(encoded_key)  # type: ignore[union-attr]
        if value is None:
            return False

        # Set with new expiration
        is_set = await self.connection.set(  # type: ignore[union-attr]
            encoded_key,
            value,
            exptime=seconds,
        )
        return is_set

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomic increment with TTL.

        For Memcached, we need to handle this specially since `incr` doesn't
        update expiration time.

        :param key: Counter key.
        :param amount: Amount to increment.
        :param ttl: TTL in seconds (only applied on first set).
        :return: New value after increment.
        """
        self._assert_ready()

        # Try to increment existing counter
        encoded_key = key.encode()
        new_value = await self.connection.incr(encoded_key, amount)  # type: ignore[union-attr]
        if new_value is not None:
            return new_value

        # Key doesn't exist, create with TTL
        # Atomically create with TTL
        added = await self.connection.add(  # type: ignore[union-attr]
            encoded_key,
            str(amount).encode(),
            exptime=ttl,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return amount

        # Someone else created it, increment
        new_value = await self.connection.incr(encoded_key, amount)  # type: ignore[union-attr]
        return new_value  # type: ignore[return-value]

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get multiple keys.

        :param keys: Keys to retrieve.
        :return: List of values (None for missing keys), same order as keys.
        """
        self._assert_ready()
        if not keys:
            return []

        encoded_keys = [k.encode() for k in keys]
        values = await self.connection.multi_get(*encoded_keys)  # type: ignore[union-attr]
        results: typing.List[typing.Optional[str]] = []
        for value in values:
            if value is not None:
                results.append(value.decode())
            else:
                results.append(None)
        return results

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Batch set multiple keys using concurrent set operations.

        Memcached doesn't have native multi-set, so we use `asyncio.gather(...)`
        to execute sets concurrently over the connection pool.

        :param items: Mapping of keys to values
        :param expire: Optional TTL in seconds for all keys
        """
        self._assert_ready()
        if not items:
            return

        exptime = int(expire) if expire is not None else 0

        async def _set_one(key: str, value: str) -> None:
            await self.connection.set(  # type: ignore[union-attr]
                key.encode(),
                value.encode(),
                exptime=exptime,
            )
            if self.track_keys:
                await self._track_key(key)

        tasks = [asyncio.create_task(_set_one(k, v)) for k, v in items.items()]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            # Any exception should cancel any ongoing task.
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

    async def clear(self) -> None:
        """
        Clear all tracked keys in the namespace.

        Note: This only works if `track_keys` was enabled.
        If not enabled, this is a no-op. If the Memcached server
        is only used for this backend, consider flushing the entire cache instead.
        Override this method as so.

        ```python
        ...
        async def clear(self) -> None:
            # Flush entire Memcached cache, if not tracking keys
            if self.connection is not None and not self.track_keys:
                await self.connection.flush_all()
                return
            await super().clear()
        ```
        """
        if not self.track_keys:
            # No-op if not tracking keys
            return

        self._assert_ready()

        tracking_key = self._tracking_key
        tracked = await self.connection.get(tracking_key.encode())  # type: ignore[union-attr]
        if tracked is None:
            return

        keys = tracked.decode().split("||")
        # Delete the tracking key itself finally (added as last key)
        keys.append(tracking_key)
        tasks = [
            asyncio.create_task(self.connection.delete(key.encode()))  # type: ignore[union-attr]
            for key in keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                raise BackendError(
                    f"Failed to clear key '{key}': {str(result)}"
                ) from result

    async def reset(self) -> None:
        """Reset the backend."""
        await self.clear()

    async def close(self) -> None:
        """Close the Memcached connection."""
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

        if self._named_gate_registry is not None:
            self._named_gate_registry.close()
            self._named_gate_registry = None
