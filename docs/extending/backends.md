# Building Custom Backends

Traffik ships with In-Memory, Redis, and Memcached backends. Those cover the vast majority of use cases. But if you have a custom storage layer - DynamoDB, Cassandra, a SQL database, an in-house cache - you can plug it in by subclassing `ThrottleBackend`.

The contract is well-defined, the base class handles a lot of the plumbing for you, and you only need to implement the storage operations themselves.

---

## Inherit from `ThrottleBackend`

```python
from traffik.backends.base import ThrottleBackend

class CustomBackend(ThrottleBackend[YourConnectionType, HTTPConnection]):
    ...
```

The two type parameters are:

- `YourConnectionType` - the type of your underlying storage connection (e.g., `aiohttp.ClientSession`, `asyncpg.Pool`, your own client class)
- `HTTPConnection` - the Starlette HTTP connection type (`Request`, `WebSocket`, or `HTTPConnection` for both)

---

## Required Methods

You must implement all of these. The base class will automatically wrap them to re-raise exceptions as `BackendError` (using the `base_exception_type` class variable).

```python
class CustomBackend(ThrottleBackend):
    base_exception_type = YourLibraryBaseException  # Exceptions to wrap as BackendError

    async def initialize(self) -> None:
        """Setup connections, create tables, etc. Called before first use."""
        self.connection = await create_connection(...)

    async def get(self, key: str) -> Optional[str]:
        """Get value for key. Returns None if not found."""
        return await self.connection.get(key)

    async def set(self, key: str, value: str, expire: Optional[int] = None) -> None:
        """Set value for key with optional TTL in seconds."""
        await self.connection.set(key, value, ex=expire)

    async def delete(self, key: str) -> bool:
        """Remove key. Returns True if the key existed, False otherwise. Should not raise if key doesn't exist."""
        return bool(await self.connection.delete(key))

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment counter. Returns new value."""
        return await self.connection.incrby(key, amount)

    async def expire(self, key: str, seconds: int) -> bool:
        """Set TTL on an existing key. Returns True if the key existed, False otherwise."""
        return bool(await self.connection.expire(key, seconds))

    def get_lock(
        self, name: str, ttl: Optional[float] = None, reentrant: bool = False
    ) -> AsyncLock:
        """
        Return a lock object for `name`, implementing the `AsyncLock` protocol:
          - is_owner(task=None) -> bool
          - async acquire(blocking, blocking_timeout) -> bool
          - async release() -> None

        This is a plain method, not a coroutine - it constructs/looks up a
        lock object without acquiring it. `backend.lock(name)` (which callers
        actually use) wraps whatever this returns in a context manager, so
        you don't need to implement `__aenter__`/`__aexit__` yourself. See
        "Tips on Lock Implementation" below for ready-made building blocks.
        """
        return MyDistributedLock(name, ttl=ttl, reentrant=reentrant)

    async def reset(self) -> None:
        """Clear all throttling data in this namespace. Used for testing."""
        await self.connection.flushdb()

    async def close(self) -> None:
        """
        Close connections and release resources. Should NOT clear stored
        data - that's reset()'s job, so a persistent backend survives a
        reconnect.
        """
        await self.connection.close()
```

`decrement()` also exists but isn't required - the base class already provides a working default (`increment(key, -amount)`). Override it if your storage has a native, more efficient decrement operation.

If your backend has no real connection object (e.g. it's backed by a plain in-process structure), also override `closed()` - the base implementation just checks `self.connection is None`, which would always be `True` for a connectionless backend, causing spurious warnings on every non-persistent context exit. See the full example below.

---

## Performance-Critical Overrides

These methods have default implementations in the base class, but overriding them with native operations is strongly recommended for production backends:

### `increment_with_ttl()`

The most important override. Called on every request for `FixedWindow` and `SlidingWindowCounter` with windows >= 1 second.

```python
async def increment_with_ttl(
    self, key: str, amount: int = 1, ttl: int = 60
) -> int:
    """
    Atomically increment counter AND set TTL if key is new.

    This must be atomic. In Redis: MULTI/EXEC or a Lua script.
    The TTL should only be set when the key is newly created.
    """
    # Redis example using a pipeline:
    async with self.connection.pipeline(transaction=True) as pipe:
        await pipe.incrby(key, amount)
        await pipe.expire(key, ttl)  # or use SET ... EX ... for atomic set+expire
        results = await pipe.execute()
    return results[0]
```

### `multi_get()`

Batch read. Called by `SlidingWindowCounter` and the stats system:

```python
async def multi_get(self, *keys: str) -> List[Optional[str]]:
    """Get multiple keys in a single round-trip."""
    return await self.connection.mget(*keys)
```

### `multi_set()`

Batch write. Called during window resets in sub-second strategies:

```python
async def multi_set(
    self, items: Dict[str, str], expire: Optional[int] = None
) -> None:
    """Set multiple key-value pairs, optionally all with the same TTL."""
    async with self.connection.pipeline() as pipe:
        for key, value in items.items():
            if expire:
                await pipe.setex(key, expire, value)
            else:
                await pipe.set(key, value)
        await pipe.execute()
```

---

## Full Example: Simple In-Process Dictionary Backend

Here's a complete, minimal custom backend using a plain Python dict (for illustration purposes - don't use this in production, `InMemoryBackend` already does this properly with sharding and bounded active expiration):

```python
import time
import typing

from traffik.backends.base import ThrottleBackend
from traffik import AsyncLockAdapter, AsyncRLock, NamedLockPool
from traffik.typing import AsyncLock


class DictBackend(ThrottleBackend[None, HTTPConnection]):
    """Simple dictionary-based backend for demonstration. Don't use in production."""

    base_exception_type = Exception

    def __init__(self, namespace: str = "dict", **kwargs: typing.Any) -> None:
        super().__init__(connection=None, namespace=namespace, **kwargs)
        self._store: typing.Dict[str, typing.Tuple[str, typing.Optional[float]]] = {}
        self._closed = True
        # AsyncRLock/NamedLockPool are traffik's own building blocks (the
        # same ones the in-memory backends use) - see "Tips on Lock
        # Implementation" below.
        self._lock_pool = NamedLockPool(
            factory=lambda: AsyncLockAdapter(lock=AsyncRLock(), reentrant=True)
        )

    def _is_expired(self, key: str) -> bool:
        if key not in self._store:
            return True
        _, expires_at = self._store[key]
        if expires_at is not None and time.time() > expires_at:
            del self._store[key]
            return True
        return False

    async def initialize(self) -> None:
        self._closed = False  # Nothing to actually connect to for a dict backend

    async def ready(self) -> bool:
        return True  # No connection to check; always ready once initialized

    def closed(self) -> bool:
        # Base closed() just checks `self.connection is None` - always true
        # here, since a dict backend has no real connection object.
        return self._closed

    async def get(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Optional[str]:
        if self._is_expired(key):
            return None
        return self._store[key][0]

    async def set(self, key: str, value: str, expire: typing.Optional[int] = None) -> None:
        expires_at = time.time() + expire if expire else None
        self._store[key] = (str(value), expires_at)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        return self._store.pop(key, None) is not None

    async def increment(self, key: str, amount: int = 1) -> int:
        current = await self.get(key)
        new_val = (int(current) if current else 0) + amount
        expires_at = self._store.get(key, (None, None))[1]
        self._store[key] = (str(new_val), expires_at)
        return new_val

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self._store:
            return False
        value, _ = self._store[key]
        self._store[key] = (value, time.time() + seconds)
        return True

    def get_lock(
        self, name: str, ttl: typing.Optional[float] = None, reentrant: bool = False
    ) -> AsyncLock:
        return self._lock_pool.get(name)

    async def reset(self) -> None:
        self._store.clear()

    async def close(self) -> None:
        # Don't clear self._store here - that's reset()'s job, not close()'s.
        self._closed = True
        self._lock_pool.close()
```

Usage - `backend.lock()` and `increment_with_ttl()`/`multi_get()`/`multi_set()` all work out of the box via the base class's defaults, built on top of the required methods above:

```python
async with DictBackend(namespace="test")(close_on_exit=True) as backend:
    await backend.set("key", "value", expire=60)
    await backend.increment_with_ttl("counter", ttl=60)
    async with backend.lock("my-resource"):
        ...
    values = await backend.multi_get("key", "counter")
```

---

## Tips on Connection Pooling

For backends backed by network connections (Redis, Cassandra, HTTP APIs), always use connection pooling:

```python
class PooledBackend(ThrottleBackend):
    def __init__(self, dsn: str, pool_size: int = 10, **kwargs):
        super().__init__(connection=None, **kwargs)
        self._dsn = dsn
        self._pool_size = pool_size

    async def initialize(self) -> None:
        self.connection = await create_pool(self._dsn, max_size=self._pool_size)

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
```

!!! tip "Use the lifespan pattern"
    Implement a `lifespan` context manager (or use `backend.lifespan`) to ensure `initialize()` and `close()` are called at the right time in your ASGI app lifecycle.

---

## Tips on Lock Implementation

Distributed locks are the hardest part of a custom backend. `traffik` exports its own lock building blocks (the same ones `InMemoryBackend` uses) so you don't have to write one from scratch:

- **`AsyncRLock`/`FairAsyncRLock`** - simple, in-process reentrant locks (`FairAsyncRLock` guarantees FIFO wakeup order; `AsyncRLock` is a bit cheaper without that guarantee). Neither alone satisfies the full `AsyncLock` protocol.
- **`AsyncLockAdapter`** - wraps one of the above to add `blocking`/`blocking_timeout` semantics and optional reentrancy control, producing something that *does* satisfy `AsyncLock`. This is what `get_lock()` should typically return.
- **`NamedLockPool`/`NamedLockHandle`** - a refcounted pool keyed by lock name, so concurrent callers locking the same `name` share one underlying lock instance instead of creating a new one per call. `pool.get(name)` returns a handle usable directly as an async context manager.

For a genuinely *distributed* lock (Redis/etcd/similar), you still need to implement the coordination yourself, but the same principles apply:

- **TTL is critical**: Locks must expire automatically. If a worker dies while holding a lock, a TTL prevents permanent deadlock.
- **Non-blocking mode**: `acquire(blocking=False)` should return `False` immediately rather than waiting - callers use this to avoid waiting for a lock.
- **Blocking timeout**: `acquire(blocking_timeout=5.0)` should give up after 5 seconds rather than waiting forever.
- **You only implement `get_lock()`**: `backend.lock(name)` (what callers actually use) wraps whatever `get_lock()` returns in a context manager - your lock just needs to satisfy the `AsyncLock` protocol, not implement `__aenter__`/`__aexit__` itself.

---

## All Operations Must Be Non-Blocking

Every method in your backend must be a coroutine that does not block the event loop. This means:

- Use `await` for all I/O
- Never call synchronous blocking APIs (`requests`, `time.sleep`, synchronous DB drivers)
- Use thread executor only as a last resort: `asyncio.get_running_loop().run_in_executor(...)`

Blocking the event loop under load will stall *all* requests, not just the throttled ones.
