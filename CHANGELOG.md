# Traffik Changelogs

## Version 1.0.0b2 (2025-27-11)

- **Enhancements**:
  - Add support for cost parameter in rate limiting strategies.
  
- **Bug Fixes**:
  - Small code fixes and optimizations.

## Version 1.0.2 (2026-02-08)

- **Enhancements**:
  - Throttles, middleware throttles can now accept a default `context` on initialization, which will be merged with any context provided during throttle checks and applications. This allows for more flexible and reusable throttle configurations, as common context values can be set at initialization and overridden or extended as needed during individual checks and applications.
  - Refactor context merging logic for better clarity, maintainability, and predictability.
  - Ensure consistent handling of contexts across all throttle operations.
  - Performance optimizations to middlewares, throttles, backends, and strategies.
  - Added new method `check(...)` to `Throttle` for zero-cost 'best-effort' checking of quota availability, allowing for more flexible usage patterns where you may want to check if quota is sufficient before actually performing an action.
  - Most internal class now all use `__slots__` for memory efficiency.
  - Added new module `quotas` which provides a context for deferring and aggregating throttle checks and application, allowing for more flexible and efficient quota management. This features is still in early stages and may receive significant changes in future releases.
  - Added `throttled(...)` decorator support for Starlette routes.
  - Better corruption recovery in throttling strategies to prevent issues with corrupted state in backends, and ensure continued operation even in the face of backend issues.
  - `ThrottleMiddleware` now supports websocket connections, allowing for throttling of WebSocket routes in addition to regular HTTP routes.
  - `traffik.decorators.throttled(...)` now supports WebSocket routes in addition to regular request/HTTP routes.

- **Bug Fixes**:
  - Minor bug fixes and code cleanups.
  - Fix bug where `Throttle.__call__` and `MiddlewareThrottle.__call__` `*args`/`**kwargs` parameters leaked into FastAPI's dependency injection, causing `args` and `kwargs` to appear as required query parameters in the OpenAPI schema and forcing `Body(embed=True)` behavior on Pydantic model body parameters when the throttle was used as a dependency via `Depends(throttle)`. Throttle instances now set a clean `__signature__` that only exposes the `connection` parameter to FastAPI, while still supporting direct calls like `throttle(request, cost=5)`.
  - Fix potential issue where `backend.close(...)` might not be called on backent context exit if an exception is raised when `backend.reset(...)` is called. Now ensures that `backend.close(...)` is always called on context exit, even if an exception occurs during `backend.reset(...)`. This prevents potential resource leaks and ensures proper cleanup of backend resources.
  - Small fixes to docstrings and type hints for better clarity and accuracy.
  
## Version 1.1.0 (2026-02-15)

- **Enhancements**:
  - `HTTPThrottle` now supports a `use_method` arg, when allows disabling using the request method in the throttling key. This enables applying the same throttle a connection, for the same route/path and scope but different methods. Hence, duplicate throttle don't have to be made to achieve this.
  - Removed `headers` property from `Throttle` class as headers are now treated as an entity separate from the throttle `context`, which must be passed and defined explictly. A new method `get_headers(...)` was added in its stead.
  - Add new `Headers` API, that allows the definition of runtime/throttling-time resolution of headers. It provides a subjectively better way of defining headers for throttles with optized datastructures.
  - Removed `include_headers` initialization argument from `ThrottleMiddleware` since `Throttle`s and `MiddlewareThrottle`s can now define their own headers.
  - Removed redundant shard locking from the `get(..)` method of the `InMemoryBackend` and moved lock acquisition in `set(..)` to exactly when shards needs to be modified. This slight improved high-concurrency performance for same key access for strategies that use `get` and `set` alot. Switching from `_AsyncRLock` to `asyncio.Lock` for shard locks saw a drastic peformance boost for the `InMemoryBackend` especially in high-load scenarios.
  - Added new `ThrottleRule` and `ThrottleRegistry` classes for defining gates and bypasses for throttles. Enables better DX for applying and bypassing throttles conditionally.
  - Many micro optimizations, that may or may not reflect in high concurrency situations.
  - Moved a lot of code around to better structure the library. Public APIs that were moved have aliases in their previous locations for backwards compatibility.
  - Official documentation added.

- **Bug Fixes**:
  - Minor bug fixes and code cleanups.
  - Small fixes to docstrings and type hints and names for better clarity and accuracy.

## Version 1.2.0 (2026-07-25)

**This release contains breaking changes** - read the section below before upgrading.

- **Breaking Changes**:
  - `ThrottleRule` and `BypassThrottleRule` have been renamed to `Rule` and `Bypass`, and the `ThrottlePredicate` type has been renamed to `Predicate`. There are no compatibility aliases for these - update any direct imports or usages.
  - `throttle_if(...)` and `bypass_if(...)` are now the preferred way to construct `Rule`/`Bypass` instances.
  - Strategy state serialization (used by Redis- and Memcached-backed backends) switched from `msgpack` to a custom struct-based format, for performance and to drop the `msgpack` dependency. State persisted by pre-1.2.0 versions will not deserialize correctly under 1.2.0. Flush your Redis/Memcached keyspace after upgrading.

- **Enhancements**:
  - Added `MultiProcessInMemoryBackend`: shares rate-limit state across real, forked worker processes (e.g. `gunicorn` running multiple workers) via shared memory, without needing Redis or Memcached. Uses a small C extension (not built on Windows) for fast hashing.
  - Redis and Memcached backends restructured into per-client submodules. `traffik.backends.redis.aioredis`/`.coredis` and `traffik.backends.memcached.aiomcache`/`.emcache` (new: `emcache` client support for Memcached, generally faster than `aiomcache`). Existing top-level imports (`traffik.backends.redis`/`traffik.backends.memcached`) still work unchanged as long as the relevant client library is installed.
  - Added `Throttle.as_middleware(...)`, returning a ready-to-use Starlette `Middleware` entry built directly from a throttle instance.
  - Expanded lock-related exceptions: new `LockError`, `LockAcquisitionError`, `LockReleaseError`, and `LockPoolError`, all still catchable via the existing `BackendError`/`TimeoutError` handlers.
  - Internal locking overhaul across all backends: pooled, reference-counted named locks with optional reentrancy, reducing lock-related memory growth and improving throughput under contention on shared keys.
  - Rewrote the benchmark suite (`benchmarks/`) to run against real, separately spawned server processes. A single `uvicorn` instance, or `gunicorn` with real forked workers via a new `--workers` option, driven by real HTTP/WebSocket clients, instead of calling the ASGI app in-process. This is the only way the suite can meaningfully exercise `MultiProcessInMemoryBackend`.
  - Documentation migrated to Zensical, reorganized, and expanded.
  - Test suite reorganized into `tests/unit/` and `tests/integration/` for clarity.
  - Maximum supported Python version bumped to 3.14.

- **Bug Fixes**:
  - Fixed `get_stat(...).wait_ms` across all strategies to correctly report the time to wait before the next request is allowed, rather than a stale, misleading value.
  - Fixed a data-loss issue in Memcached key-tracking: replaced a get-update-set cycle with an atomic append, and sharded tracking keys to avoid Memcached's 1MB value size ceiling.
  - Fixed lock release and error propagation issues under contention in Redis- and Memcached-backed locks.
  - Minor bug fixes, docstring corrections, and code cleanups throughout.
