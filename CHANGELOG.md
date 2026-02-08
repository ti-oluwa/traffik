# Traffik Changelogs

## Version 1.0.0b2 (2025-27-11)

- **Enhancements**:
  - Add support for cost parameter in rate limiting strategies.
  
- **Bug Fixes**:
  - Small code fixes and optimizations.

## Version 1.0.2 (2026-02-06)

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

- **Bug Fixes**:
  - Minor bug fixes and code cleanups.
  - Fix bug where `Throttle.__call__` and `MiddlewareThrottle.__call__` `*args`/`**kwargs` parameters leaked into FastAPI's dependency injection, causing `args` and `kwargs` to appear as required query parameters in the OpenAPI schema and forcing `Body(embed=True)` behavior on Pydantic model body parameters when the throttle was used as a dependency via `Depends(throttle)`. Throttle instances now set a clean `__signature__` that only exposes the `connection` parameter to FastAPI, while still supporting direct calls like `throttle(request, cost=5)`.
  - Fix potential issue where `backend.close(...)` might not be called on backent context exit if an exception is raised when `backend.reset(...)` is called. Now ensures that `backend.close(...)` is always called on context exit, even if an exception occurs during `backend.reset(...)`. This prevents potential resource leaks and ensures proper cleanup of backend resources.
  - Small fixes to docstrings and type hints for better clarity and accuracy.
  