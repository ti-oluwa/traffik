"""Memcached implementation of a throttle backend using `emcache`."""

import asyncio
import math
import sys
import typing
from time import monotonic
from types import TracebackType
from urllib.parse import urlparse

import emcache

from traffik._locks import fence_token_generator
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)


def _parse_memcached_url(url: str) -> typing.Dict[str, typing.Any]:
    """
    Parse Memcached URL into connection parameters.

    :param url: Memcached URL (e.g. memcached://host:port).
    :return: Dictionary of connection parameters.
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith("memcached"):
        raise ValueError("Invalid Memcached URL scheme")

    host = parsed.hostname or "localhost"
    port = parsed.port or 11211
    return {"host": host, "port": port}


def _parse_memcached_nodes(
    nodes: typing.Sequence[typing.Union[str, typing.Tuple[str, int]]],
) -> typing.List[emcache.MemcachedHostAddress]:
    """
    Parse a sequence of node specifiers into `emcache.MemcachedHostAddress` objects.

    Each element may be:

    - A `(host, port)` tuple.
    - A URL string such as `"memcached://host:port"`.
    - A bare `"host:port"` string.

    :param nodes: Sequence of node specifiers.
    :return: List of `emcache.MemcachedHostAddress` instances.
    :raises ValueError: If a specifier cannot be parsed.
    """
    result: typing.List[emcache.MemcachedHostAddress] = []
    for node in nodes:
        if isinstance(node, tuple):
            host, port = node
            result.append(emcache.MemcachedHostAddress(host, port))
        elif isinstance(node, str):
            if node.startswith("memcached://"):
                parsed = _parse_memcached_url(node)
                result.append(
                    emcache.MemcachedHostAddress(parsed["host"], parsed["port"])
                )
            elif ":" in node:
                host, port_str = node.rsplit(":", 1)
                result.append(emcache.MemcachedHostAddress(host, int(port_str)))
            else:
                result.append(emcache.MemcachedHostAddress(node, 11211))
        else:
            raise ValueError(
                f"Cannot parse Memcached node specifier: {node!r}. "
                "Expected a (host, port) tuple, a 'memcached://host:port' URL, "
                "or a 'host:port' string."
            )
    return result


class _AsyncMemcachedLock:
    """
    Name-based, best-effort, and "instance-reentrant" distributed (un-fair) lock
    implementation using Memcached's `add` operation via `emcache`.

    Uses Memcached's atomic `add()` which only succeeds if the key does not
    exist, providing a lightweight distributed locking mechanism.

    Suitable for Memcached deployments where low-latency locking is required.
    """

    __slots__ = (
        "_name",
        "_client",
        "_ttl",
        "_acquired",
        "_token",
        "_max_spins_before_backoff",
        "_spin_max_delay_seconds",
    )

    def __init__(
        self,
        name: str,
        client: emcache.Client,
        ttl: typing.Optional[float] = None,
        max_spins_before_backoff: int = 4,
        spin_max_delay_seconds: float = 0.01,
    ) -> None:
        """
        Initialize the lock.

        :param name: Unique lock name.
        :param client: `emcache.Client` instance.
        :param ttl: How long to hold the lock in seconds before auto-release.
            If None, lock has no expiration. This is bad practice and may lead
            to deadlocks.
        :param max_spins_before_backoff: Number of zero-delay yields to the
            event-loop during acquisition before applying exponential backoff.
        :param spin_max_delay_seconds: Maximum delay in seconds during backoff.
        """
        self._name = name
        self._client = client
        self._ttl = math.ceil(ttl) if ttl is not None else 0
        self._acquired = False
        self._token: typing.Optional[str] = None
        self._max_spins_before_backoff = max_spins_before_backoff
        self._spin_max_delay_seconds = spin_max_delay_seconds

    def locked(self) -> bool:
        """Return True if this instance currently holds the lock."""
        return self._acquired

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the distributed lock.

        :param blocking: If False, return immediately if locked elsewhere.
        :param blocking_timeout: Max wait time when blocking.
        :return: True if lock acquired, False otherwise.
        """
        if self._acquired:
            # Already acquired by this instance (reentrancy guard).
            return True

        # We generate our own fencing token per acquisition attempt.
        # This helps prevent the "stale lock" problem per-process.
        token = str(fence_token_generator.next())
        name_bytes = self._name.encode()
        token_bytes = token.encode()
        start = monotonic()
        attempts = 0
        max_spins = self._max_spins_before_backoff
        spin_max_delay = self._spin_max_delay_seconds

        while True:
            try:
                # `add` succeeds only when the key does not yet exist,
                # providing the atomic "set-if-not-exists" semantic we need.
                await self._client.add(
                    name_bytes,
                    token_bytes,
                    exptime=self._ttl,
                    noreply=False,
                )
                # No exception means `add` succeeded — we own the lock.
                self._acquired = True
                self._token = token
                return True
            except emcache.NotStoredStorageCommandError:
                # Key already exists; lock is held by someone else.
                pass
            except (emcache.CommandError, Exception) as exc:
                # Any other Memcached error during lock acquisition.
                sys.stderr.write(
                    f"Warning: Error during lock acquisition for '{self._name}': {exc}\n"
                )
                sys.stderr.flush()

            if not blocking:
                return False

            if (
                blocking_timeout is not None
                and (monotonic() - start) >= blocking_timeout
            ):
                return False

            attempts += 1
            if attempts <= max_spins:
                await asyncio.sleep(0)
            else:
                exponent = min(attempts - max_spins, 6)
                delay = min(0.0005 * (1 << exponent), spin_max_delay)
                await asyncio.sleep(delay)

    async def release(self) -> None:
        """Release the lock."""
        if not self._acquired:
            raise RuntimeError(
                f"Cannot release lock '{self._name}'. Lock not owned by this instance"
            )

        name_bytes = self._name.encode()
        try:
            # Only delete the key if the stored token still matches ours,
            # preventing accidental release of a lock acquired by another
            # instance after ours expired.
            item = await self._client.get(name_bytes)
            if item is not None and item.value.decode() == self._token:
                await self._client.delete(name_bytes, noreply=False)
            else:
                sys.stderr.write(f"Warning: Lock '{self._name}' expired or stolen\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nosec
            # Lock might have expired or been released already.
            sys.stderr.write(
                f"Warning: Failed to release lock '{self._name}': {str(exc)}\n"
            )
        finally:
            self._acquired = False
            self._token = None
            sys.stderr.flush()

    async def __aenter__(self) -> "_AsyncMemcachedLock":
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Memcached lock '{self._name}'")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        await self.release()


class MemcachedBackend(ThrottleBackend[emcache.Client, HTTPConnectionT]):
    """
    Memcached-based throttle backend with distributed locking support.

    Uses `emcache` for high-performance async Memcached operations with
    native support for multiple Memcached nodes via Rendezvous hashing
    and an adaptive connection pool.

    Note: Memcached has a key size limit of 250 bytes.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        url: typing.Optional[str] = None,
        host: str = "localhost",
        port: int = 11211,
        *,
        nodes: typing.Optional[
            typing.Sequence[typing.Union[str, typing.Tuple[str, int]]]
        ] = None,
        max_connections: int = 2,
        min_connections: int = 1,
        purge_unused_connections_after: typing.Optional[float] = None,
        connection_timeout: typing.Optional[float] = None,
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
        track_keys: bool = False,
        autobatching: bool = False,
        ssl: bool = False,
        ssl_verify: bool = True,
        ssl_extra_ca: typing.Optional[str] = None,
        username: typing.Optional[str] = None,
        password: typing.Optional[str] = None,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize Memcached backend.


        :param url: Optional Memcached URL (e.g. `"memcached://host:port"`).
            Mutually exclusive with explicit `host`/`port` values.
        :param host: Memcached server host (single-node shorthand).
        :param port: Memcached server port (single-node shorthand).
        :param nodes: Optional sequence of node specifiers for multi-node
            deployments. Each element may be a `(host, port)` tuple, a
            `"memcached://host:port"` URL, or a `"host:port"` string.
            When provided, `url`, `host`, and `port` are ignored.
            Traffic is distributed across nodes using Rendezvous hashing.
        :param max_connections: Maximum number of connections per node in
            the adaptive connection pool.
        :param min_connections: Minimum number of connections per node kept
            alive in the pool.
        :param purge_unused_connections_after: Seconds of inactivity after
            which idle connections above `min_connections` are closed.
            If None, unused connections are never purged.
        :param connection_timeout: Seconds to wait when opening a new
            connection to a Memcached node. If None, waits indefinitely.
        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client
            connection is throttled.
        :param persistent: Whether to persist throttling data across
            application restarts.
        :param on_error: Strategy to handle errors during throttling
            operations.
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate
              limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception
              as parameters and returns an integer representing the wait
              period in milliseconds. Ensure this function executes quickly
              to avoid additional latency.
        :param lock_blocking: Whether locks should block when acquiring.
            If None, uses the global default from
            `traffik.config.get_lock_blocking()`.
        :param lock_ttl: Default TTL for locks in seconds. If None, locks
            have no expiration unless specified during lock acquisition.
        :param lock_blocking_timeout: Default maximum time to wait for
            acquiring locks in seconds. If None, uses the global default
            from `traffik.config.get_lock_blocking_timeout()`.
        :param track_keys: Whether to track all keys in the namespace for
            clearing. Since Memcached does not support key listing natively,
            this enables a best-effort tracking mechanism using a special
            tracking key to store all keys set by this backend. This allows
            the `clear()` method to function.
            Note: This adds overhead to cache operations and is not 100%
            reliable. Only use if you absolutely need the `clear()`
            functionality. `clear()` will be a no-op if this is False.
        :param autobatching: Whether to enable emcache's autobatching
            feature. When True, multiple concurrent `get` operations are
            transparently batched into a single Memcached `get_many`
            command, potentially doubling throughput at the cost of a tiny
            extra latency per individual get.
        :param ssl: Whether to use SSL/TLS for Memcached connections.
        :param ssl_verify: Whether to verify the server certificate when
            using SSL/TLS.
        :param ssl_extra_ca: Path to an extra CA certificate bundle to use
            when verifying the server certificate.
        :param username: SASL authentication username.  Requires a
            Memcached server compiled with SASL support.
        :param password: SASL authentication password.
        :param kwargs: Additional keyword arguments.
        """
        if nodes is not None:
            self._host_addresses = _parse_memcached_nodes(nodes)
        else:
            if url and (host != "localhost" or port != 11211):
                raise ValueError("Specify either 'url' or 'host'/'port', not both.")
            if url is not None:
                parsed = _parse_memcached_url(url)
                host = parsed["host"]
                port = parsed["port"]
            self._host_addresses = [emcache.MemcachedHostAddress(host, port)]

        # Keep single-node attributes for backwards-compatible introspection.
        if len(self._host_addresses) == 1:
            self.host: str = self._host_addresses[0].address
            self.port: int = self._host_addresses[0].port
        else:
            self.host = self._host_addresses[0].address
            self.port = self._host_addresses[0].port

        self.max_connections = max_connections
        self.min_connections = min_connections
        self.purge_unused_connections_after = purge_unused_connections_after
        self.connection_timeout = connection_timeout
        self.track_keys = track_keys
        self.autobatching = autobatching
        self.ssl = ssl
        self.ssl_verify = ssl_verify
        self.ssl_extra_ca = ssl_extra_ca
        self.username = username
        self.password = password
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

    async def _track_key(self, key: str) -> None:
        """Best-effort add key to tracking set."""
        if self.connection is None:
            return

        if "||" in key:
            sys.stderr.write(
                f"Warning: Key '{key}' contains '||' which is used as a separator "
                "in key tracking. Ensure keys do not contain this sequence.\n"
            )
            sys.stderr.flush()
            return

        tracking_key = self._tracking_key.encode()
        try:
            item = await self.connection.get(tracking_key)
            if item is None:
                await self.connection.set(
                    tracking_key,
                    key.encode(),
                    exptime=0,
                    noreply=False,
                )
            else:
                keys_set = set(item.value.decode().split("||"))
                if key not in keys_set:
                    keys_set.add(key)
                    await self.connection.set(
                        tracking_key,
                        "||".join(sorted(keys_set)).encode(),
                        exptime=0,
                        noreply=False,
                    )
        except Exception as exc:
            sys.stderr.write(f"Warning: Failed to track key '{key}': {exc}\n")
            sys.stderr.flush()

    async def _untrack_key(self, key: str) -> None:
        """Best-effort remove key from tracking set."""
        if self.connection is None:
            return

        tracking_key = self._tracking_key.encode()
        try:
            item = await self.connection.get(tracking_key)
            if item is None:
                return

            keys_set = set(item.value.decode().split("||"))
            if key in keys_set:
                keys_set.remove(key)
                if keys_set:
                    await self.connection.set(
                        tracking_key,
                        "||".join(sorted(keys_set)).encode(),
                        exptime=0,
                        noreply=False,
                    )
                else:
                    await self.connection.delete(tracking_key, noreply=False)
        except Exception as exc:
            sys.stderr.write(f"Warning: Failed to untrack key '{key}': {exc}\n")
            sys.stderr.flush()

    async def initialize(self) -> None:
        """Initialize the Memcached connection pool via emcache."""
        if self.connection is not None:
            return

        # Build optional kwargs for `create_client` only when values are set,
        # so we don't pass None where `emcache` expects an absent argument.
        create_kwargs: typing.Dict[str, typing.Any] = {
            "max_connections": self.max_connections,
            "min_connections": self.min_connections,
            "autobatching": self.autobatching,
            "ssl": self.ssl,
            "ssl_verify": self.ssl_verify,
        }
        if self.purge_unused_connections_after is not None:
            create_kwargs["purge_unused_connections_after"] = (
                self.purge_unused_connections_after
            )
        if self.connection_timeout is not None:
            create_kwargs["connection_timeout"] = self.connection_timeout
        if self.ssl_extra_ca is not None:
            create_kwargs["ssl_extra_ca"] = self.ssl_extra_ca
        if self.username is not None:
            create_kwargs["username"] = self.username
        if self.password is not None:
            create_kwargs["password"] = self.password

        self.connection = await emcache.create_client(
            self._host_addresses,
            **create_kwargs,
        )

    async def ready(self) -> bool:
        """Return True if the client has been created and can serve traffic."""
        return self.connection is not None

    def _assert_ready(self) -> None:
        """
        Raise `BackendConnectionError` if the backend has not been initialized.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def get_lock(self, name: str) -> _AsyncMemcachedLock:
        """
        Get a distributed lock for the given name.

        :param name: Lock name.
        :return: `_AsyncMemcachedLock` instance.
        """
        self._assert_ready()
        return _AsyncMemcachedLock(name, client=self.connection, ttl=self.lock_ttl)  # type: ignore[arg-type]

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """
        Get value by key.

        :param key: Key to retrieve.
        :return: Value as string, or None if not found.
        """
        self._assert_ready()

        item = await self.connection.get(key.encode())  # type: ignore[union-attr]
        if item is None:
            return None
        return item.value.decode()

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
            noreply=False,
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

        try:
            await self.connection.delete(key.encode(), noreply=False)  # type: ignore[union-attr]
            if self.track_keys:
                await self._untrack_key(key)
            return True
        except emcache.NotFoundCommandError:
            return False

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        Memcached's `incr`/`decr` commands are atomic and thread-safe
        across all clients. If the key does not exist it is initialised to
        `amount` using an atomic `add`.

        Note: Memcached counters are unsigned and cannot go below 0 via
        `decr`; negative amounts are handled by initialising to
        `amount` (which may be negative) via `add` when the key is absent.

        :param key: Counter key.
        :param amount: Amount to increment by (may be negative to decrement).
        :return: New value after increment.
        """
        self._assert_ready()

        encoded_key = key.encode()

        if amount >= 0:
            # Try native INCR first (fast path for existing keys).
            try:
                new_value = await self.connection.increment(encoded_key, amount)  # type: ignore[union-attr]
                if new_value is not None:
                    return new_value
            except (emcache.NotFoundCommandError, emcache.CommandError):
                pass

            # Key does not exist; initialise atomically.
            try:
                await self.connection.add(  # type: ignore[union-attr]
                    encoded_key,
                    str(amount).encode(),
                    exptime=0,
                    noreply=False,
                )
                if self.track_keys:
                    await self._track_key(key)
                return amount
            except emcache.NotStoredStorageCommandError:
                # Race occured. Another client created the key first; retry INCR.
                new_value = await self.connection.increment(encoded_key, amount)  # type: ignore[union-attr]
                return new_value  # type: ignore[return-value]
        else:
            # Decrement path.
            decrement_amount = -amount
            try:
                new_value = await self.connection.decrement(  # type: ignore[union-attr]
                    encoded_key, decrement_amount
                )
                if new_value is not None:
                    return new_value
            except (emcache.NotFoundCommandError, emcache.CommandError):
                pass

            # Key does not exist; initialise to a negative value via set
            # (Memcached counters can't go negative, so we store as a plain string).
            try:
                await self.connection.add(  # type: ignore[union-attr]
                    encoded_key,
                    str(amount).encode(),  # amount is negative here
                    exptime=0,
                    noreply=False,
                )
                if self.track_keys:
                    await self._track_key(key)
                return amount
            except emcache.NotStoredStorageCommandError:
                # Race occurred. Another client created the key first; retry DECR.
                new_value = await self.connection.decrement(  # type: ignore[union-attr]
                    encoded_key, decrement_amount
                )
                return new_value  # type: ignore[return-value]

    async def decrement(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement counter.

        Delegates to `increment` with a negated amount so the semantics
        remain consistent with the base class contract.

        Note: Memcached counters cannot go below 0 via the native `decr`
        command. When decrement produces a value that would be negative on a
        fresh key, the key is initialised to `-amount` as a plain string
        so that subsequent reads return the correct (negative) value.

        :param key: Counter key.
        :param amount: Amount to decrement by.
        :return: New value after decrement.
        """
        return await self.increment(key, -amount)

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        Uses Memcached's native `touch` command, which updates the TTL
        without fetching or re-storing the value.

        :param key: Key to set expiration on.
        :param seconds: TTL in seconds.
        :return: True if expiration was set, False if key does not exist.
        """
        self._assert_ready()

        try:
            await self.connection.touch(key.encode(), seconds)  # type: ignore[union-attr]
            return True
        except emcache.NotFoundCommandError:
            return False

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment and set a TTL only when the key is new.

        Uses `add` (atomic set-if-not-exists) when the key is absent so
        the TTL is applied in a single round-trip. Subsequent increments
        within the same window use the native `incr` command and preserve
        the existing expiry.

        :param key: Counter key.
        :param amount: Amount to increment.
        :param ttl: TTL in seconds (applied only on first creation).
        :return: New value after increment.
        """
        self._assert_ready()

        encoded_key = key.encode()

        # Key exists, just increment (preserves existing TTL).
        try:
            new_value = await self.connection.increment(encoded_key, amount)  # type: ignore[union-attr]
            if new_value is not None:
                return new_value
        except (emcache.NotFoundCommandError, emcache.CommandError):
            pass

        # Key does not exist, create atomically with TTL.
        try:
            await self.connection.add(  # type: ignore[union-attr]
                encoded_key,
                str(amount).encode(),
                exptime=ttl,
                noreply=False,
            )
            if self.track_keys:
                await self._track_key(key)
            return amount
        except emcache.NotStoredStorageCommandError:
            # Race occurred. Another client created the key first; increment it.
            new_value = await self.connection.increment(encoded_key, amount)  # type: ignore[union-attr]
            return new_value  # type: ignore[return-value]

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get multiple keys in a single Memcached command.

        :param keys: Keys to retrieve.
        :return: List of values (None for missing keys) in the same order as *keys*.
        """
        self._assert_ready()
        if not keys:
            return []

        encoded_keys = [k.encode() for k in keys]
        items: typing.Dict[bytes, emcache.Item] = await self.connection.get_many(  # type: ignore[union-attr]
            encoded_keys
        )
        return [
            items[k.encode()].value.decode() if k.encode() in items else None
            for k in keys
        ]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Batch set multiple keys using concurrent `set` operations.

        Memcached does not have a native multi-set command, so operations
        are issued concurrently via `asyncio.gather`.

        :param items: Mapping of keys to values.
        :param expire: Optional TTL in seconds for all keys.
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
                noreply=False,
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

        Note: This only works if `track_keys` was enabled at construction
        time. If not enabled, this is a no-op. If the Memcached server is
        dedicated to this backend, consider overriding `clear()` to call
        `flush_all()` instead:

        ```python

        async def clear(self) -> None:
            if self.connection is not None and not self.track_keys:
                await self.connection.flush_all()
                return
            await super().clear()
        ```
        """
        if not self.track_keys:
            return

        self._assert_ready()

        tracking_key = self._tracking_key.encode()
        item = await self.connection.get(tracking_key)  # type: ignore[union-attr]
        if item is None:
            return

        raw_keys = item.value.decode().split("||")
        tasks = [
            asyncio.create_task(self.connection.delete(k.encode(), noreply=False))  # type: ignore[union-attr]
            for k in raw_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key_str, result in zip(raw_keys, results):
            if isinstance(result, Exception) and not isinstance(
                result, emcache.NotFoundCommandError
            ):
                raise BackendError(
                    f"Failed to clear key '{key_str}': {str(result)}"
                ) from result

        # Remove the tracking key itself.
        try:
            await self.connection.delete(tracking_key, noreply=False)  # type: ignore[union-attr]
        except emcache.NotFoundCommandError:
            pass

    async def reset(self) -> None:
        """Reset the backend by clearing all tracked namespace data."""
        await self.clear()

    async def close(self) -> None:
        """Close the Memcached connection pool."""
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
