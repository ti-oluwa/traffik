# Building Custom Backends

Traffik ships with InMemory, Redis, and Memcached backends. Those cover the vast majority of use cases. But if you have a custom storage layer — DynamoDB, Cassandra, a SQL database, an in-house cache — you can plug it in by subclassing `ThrottleBackend`.

The contract is well-defined, the base class handles a lot of the plumbing for you, and you only need to implement the storage operations themselves.

---

## Inherit from ThrottleBackend

```python
from traffik.backends.base import ThrottleBackend

class CustomBackend(ThrottleBackend[YourConnectionType, HTTPConnection]):
    ...
```

The two type parameters are:
- `YourConnectionType` — the type of your underlying storage connection (e.g., `aiohttp.ClientSession`, `asyncpg.Pool`, your own client class)
- `HTTPConnection` — the Starlette HTTP connection type (`Request`, `WebSocket`, or `HTTPConnection` for both)

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

    async def delete(self, key: str) -> None:
        """Remove key. Should not raise if key doesn't exist."""
        await self.connection.delete(key)

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment counter. Returns new value."""
        return await self.connection.incrby(key, amount)

    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement counter. Returns new value."""
        return await self.connection.decrby(key, amount)

    async def expire(self, key: str, seconds: int) -> None:
        """Set TTL on an existing key."""
        await self.connection.expire(key, seconds)

    async def get_lock(self, key: str, timeout: Optional[float] = None) -> AsyncLock:
        """
        Return a distributed lock object for key.
        The returned object must implement the AsyncLock protocol:
          - locked() -> bool
          - acquire(blocking, blocking_timeout) -> bool
          - release() -> None
        """
        return MyDistributedLock(key, timeout=timeout)

    async def reset(self) -> None:
        """Clear all throttling data in this namespace. Used for testing."""
        await self.connection.flushdb()

    async def close(self) -> None:
        """Close connections and release resources."""
        await self.connection.close()
```

---

## Performance-Critical Overrides

These methods have default implementations in the base class, but overriding them with native operations is strongly recommended for production backends:

### increment_with_ttl()

The most important override. Called on every request for `FixedWindow` and `SlidingWindowCounter` with windows >= 1 second.

```python
async def increment_with_ttl(
    self, key: str, amount: int = 1, ttl: int = 1
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

### multi_get()

Batch read — called by `SlidingWindowCounter` and the stats system:

```python
async def multi_get(self, *keys: str) -> List[Optional[str]]:
    """Get multiple keys in a single round-trip."""
    return await self.connection.mget(*keys)
```

### multi_set()

Batch write — called during window resets in sub-second strategies:

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

Here's a complete, minimal custom backend using a plain Python dict (for illustration purposes — don't use this in production):

```python
import asyncio
import time
import typing
from contextlib import asynccontextmanager
from starlette.requests import HTTPConnection
from traffik.backends.base import ThrottleBackend
from traffik.types import AsyncLock


class _SimpleLock:
    """A minimal AsyncLock implementation using asyncio.Lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        if not blocking:
            return self._lock.acquire.__func__  # non-blocking attempt
        if blocking_timeout is not None:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=blocking_timeout)
                return True
            except asyncio.TimeoutError:
                return False
        await self._lock.acquire()
        return True

    async def release(self) -> None:
        self._lock.release()


class DictBackend(ThrottleBackend[None, HTTPConnection]):
    """Simple dictionary-based backend for demonstration."""

    base_exception_type = Exception

    def __init__(self, namespace: str = "dict") -> None:
        super().__init__(connection=None, namespace=namespace)
        self._store: typing.Dict[str, typing.Tuple[str, typing.Optional[float]]] = {}
        # value, expires_at
        self._locks: typing.Dict[str, _SimpleLock] = {}

    def _is_expired(self, key: str) -> bool:
        if key not in self._store:
            return True
        _, expires_at = self._store[key]
        if expires_at is not None and time.time() > expires_at:
            del self._store[key]
            return True
        return False

    async def initialize(self) -> None:
        pass  # Nothing to initialize for a dict backend

    async def get(self, key: str) -> typing.Optional[str]:
        if self._is_expired(key):
            return None
        return self._store[key][0]

    async def set(
        self, key: str, value: str, expire: typing.Optional[int] = None
    ) -> None:
        expires_at = time.time() + expire if expire else None
        self._store[key] = (str(value), expires_at)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def increment(self, key: str, amount: int = 1) -> int:
        current = await self.get(key)
        new_val = (int(current) if current else 0) + amount
        expires_at = self._store.get(key, (None, None))[1]
        self._store[key] = (str(new_val), expires_at)
        return new_val

    async def decrement(self, key: str, amount: int = 1) -> int:
        return await self.increment(key, -amount)

    async def expire(self, key: str, seconds: int) -> None:
        if key in self._store:
            value, _ = self._store[key]
            self._store[key] = (value, time.time() + seconds)

    async def increment_with_ttl(
        self, key: str, amount: int = 1, ttl: int = 1
    ) -> int:
        current = await self.get(key)
        new_val = (int(current) if current else 0) + amount
        expires_at = time.time() + ttl
        # Only update expiry if key is new (current is None)
        if current is None:
            self._store[key] = (str(new_val), expires_at)
        else:
            existing_expiry = self._store[key][1]
            self._store[key] = (str(new_val), existing_expiry)
        return new_val

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        return [await self.get(k) for k in keys]

    async def multi_set(
        self,
        items: typing.Dict[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        for key, value in items.items():
            await self.set(key, value, expire=expire)

    async def get_lock(
        self, key: str, timeout: typing.Optional[float] = None
    ) -> _SimpleLock:
        if key not in self._locks:
            self._locks[key] = _SimpleLock()
        return self._locks[key]

    async def reset(self) -> None:
        self._store.clear()

    async def close(self) -> None:
        self._store.clear()
        self._locks.clear()
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
        if self.connection:
            await self.connection.close()
```

!!! tip "Use the lifespan pattern"
    Implement a `lifespan` context manager (or use `backend.lifespan`) to ensure `initialize()` and `close()` are called at the right time in your ASGI app lifecycle.

---

## Tips on Lock Implementation

Distributed locks are the hardest part of a custom backend. A few things to keep in mind:

- **TTL is critical**: Locks must expire automatically. If a worker dies while holding a lock, a TTL prevents permanent deadlock.
- **Non-blocking mode**: Implement `acquire(blocking=False)` properly — callers use this to avoid waiting for a lock.
- **Blocking timeout**: `acquire(blocking_timeout=5.0)` should give up after 5 seconds rather than waiting forever.
- **Context manager support**: The base class wraps `get_lock()` in an `asynccontextmanager` — your lock just needs the `acquire/release` protocol.

---

## All Operations Must Be Non-Blocking

Every method in your backend must be a coroutine that does not block the event loop. This means:

- Use `await` for all I/O
- Never call synchronous blocking APIs (`requests`, `time.sleep`, synchronous DB drivers)
- Use thread executor only as a last resort: `asyncio.get_event_loop().run_in_executor(...)`

Blocking the event loop under load will stall *all* requests, not just the throttled ones.
