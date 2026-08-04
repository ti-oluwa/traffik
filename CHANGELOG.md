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
  - `ThrottleRule` and `BypassThrottleRule` have been renamed to `Rule` and `Bypass`, and the `ThrottlePredicate` type has been renamed to `Predicate`. With `ThrottleRule` and `BypassThrottleRule` kept as compatibility aliases - will be removed in future versions.
  - `throttle_if(...)` and `bypass_if(...)` are now the preferred way to construct `Rule`/`Bypass` instances.
  - Strategy state serialization (used by Redis- and Memcached-backed backends) switched from `msgpack` to a custom struct-based format, for performance and to drop the `msgpack` dependency. State persisted by pre-1.2.0 versions will not deserialize correctly under 1.2.0. Although you may not see errors, the previous stored states are invalidated. Preferably, flush your Redis/Memcached keyspace after upgrading.

- **Enhancements**:
  - Added `MultiProcessInMemoryBackend`: shares rate-limit state across real, forked worker processes (e.g. `gunicorn` running multiple workers) via shared memory, without needing Redis or Memcached. Uses a small C extension (not built on Windows) for fast hashing.
  - Redis and Memcached backends are restructured into per-client submodules. `traffik.backends.redis.aioredis`/`.coredis` and `traffik.backends.memcached.aiomcache`/`.emcache` (new: `emcache` client support for Memcached, generally faster than `aiomcache`). Existing top-level imports (`traffik.backends.redis`/`traffik.backends.memcached`) still work unchanged as long as the relevant client library is installed.
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

## Version 1.2.1 (2026-07-28)

**Security-relevant behavior change** - read this before upgrading if you use `get_remote_address`.

- **Security Fix**:
  - `get_remote_address` no longer trusts proxy headers unconditionally. Previously, any direct client could set `X-Forwarded-For` (or the non-standard `Remote-Addr` header, which was never a real proxy convention) and have it accepted as the client's identity with no verification at all. Therefore easily defeating IP-based rate limiting by spoofing a different address on every request. `get_remote_address` now only consults proxy headers when the immediate connection peer matches an explicitly configured `trusted_proxies` list (exact addresses and/or CIDR networks); otherwise the real socket peer address is returned, unaffected by any header content. **`trusted_proxies` defaults to `None` (trust nothing)**. If you rely on `X-Forwarded-For` or any other proxy header for client identification, you must now pass `trusted_proxies` explicitly.
  
- **Enhancements**:
  - Added `ProxyHeaders`, a flag enum selecting which proxy header conventions `get_remote_address` will consult: RFC 7239 `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, and `CF-Connecting-IP`, independently, or combined via `ProxyHeaders.ALL`.
  - `X-Forwarded-For` chains are walked from the nearest hop backwards, stopping at the first address that isn't itself a trusted proxy. Making this resistant to a client prepending forged entries to the header.
  - RFC 7239 `Forwarded` parsing handles bracketed IPv6, `:port` suffixes, and optional whitespace around `=`.
  - Trusted-proxy matching checks exact addresses via a set lookup first, and then falling back to CIDR containment only when networks are actually configured; the split is cached per distinct `trusted_proxies` value so static configuration isn't reprocessed on every request.
  - Added test coverage for the trust boundary, header precedence, RFC 7239 edge cases, and a regression test asserting the `X-Forwarded-For` walk can't be tricked by forged entries prepended ahead of a real, untrusted hop.
  
- **Removed**:
  - The non-standard `Remote-Addr` header is no longer consulted by `get_remote_address` as it was never a real proxy convention, and honoring it was exactly the same spoofing vector as the unauthenticated `X-Forwarded-For` handling described above.

## Version 1.2.2 (2026-08-04)

- **Enhancements**:
  - Added `skip_handler` support across `Throttle`, `HTTPThrottle`, `WebSocketThrottle`, `MiddlewareThrottle`, `ThrottleMiddleware`, and `QuotaContext`. When enabled (at init, or per-hit/per-entry as an override), a throttled connection still updates the strategy's state and is still marked throttled, but the configured `handle_throttled` is never invoked so you get no exception, no response, no side effects. Useful when you want to check `get_wait()`/`is_throttled()` yourself and decide what happens next instead of letting the throttle react for you.
  - Added `get_wait(connection)`: returns the last active wait period (in ms) if the connection was throttled, `0` otherwise.`THROTTLED_STATE_KEY` now stores this wait value directly instead of a bare `True`/`False`, so `is_throttled(...)` is now just `get_wait(...) != 0.0` under the hood.
  - Added `HTTPThrottle.set_headers(...)`: resolves and applies throttling headers directly onto a `Response` object, for cases where you're not raising/returning a throttled response yourself and just want the headers set.
  - `QuotaContext` gets a new `merge()` method: explicitly folds a nested context's queued entries into its parent without touching the backend. This is now what runs on successful exit of a nested context (previously handled by an internal, undocumented method), so `merge()` and `apply()` now have clearly separate and predictable meanings. `merge()` defers, `apply()` always consumes immediately, regardless of nesting.
  - `ThrottleRegistry` gains `count()`, `__len__`, and `__contains__`. `exists(...)` is now lock-protected for consistency with the rest of the registry's API.
  - `ThrottleRegistry.get_throttle(...)` now automatically de-registers a UID whose throttle has already been garbage-collected, instead of just returning `None` and leaving the stale weakref/UID behind.

- **Bug Fixes**:
  - Fixed `QuotaContext.apply()` leaving itself registered in its parent's internal children tracking when called directly on a nested context, which inflated the parent's `queued_cost` and held a stale reference indefinitely. `apply()` now always detaches and clears its own queue after consuming, regardless of nesting.
  - Fixed `ThrottleRegistry.disable_all()`/`enable_all()` skipping garbage-collected throttles without de-registering them; they now de-register stale UIDs as they go, same as `get_throttle(...)`.
