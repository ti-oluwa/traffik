*[HTTPThrottle]: Rate limiter for HTTP requests. Attach to FastAPI routes via Depends() or call hit() directly. Import: from traffik import HTTPThrottle
*[WebSocketThrottle]: Rate limiter for WebSocket connections. Throttles per-connection or per-message. Import: from traffik import WebSocketThrottle
*[MiddlewareThrottle]: Throttle applied at the ASGI middleware layer, before route handlers execute. Import: from traffik.throttles import MiddlewareThrottle
*[InMemoryBackend]: In-process storage backend. Fast, zero-dependency, not shared across processes. Import: from traffik.backends.inmemory import InMemoryBackend
*[RedisBackend]: Distributed backend backed by Redis. Shares counters across all app instances. Import: from traffik.backends.redis import RedisBackend
*[MemcachedBackend]: Distributed backend backed by Memcached. Import: from traffik.backends.memcached import MemcachedBackend
*[ThrottleRegistry]: Manages throttle registration, rule attachment, and group disable/enable. Import: from traffik.registry import ThrottleRegistry
*[GLOBAL_REGISTRY]: The default ThrottleRegistry used when no registry is specified on a throttle. Import: from traffik.registry import GLOBAL_REGISTRY
*[ThrottleRule]: A gating rule — throttle only fires when all rules pass. Conditions on path, method, or a predicate callable. Import: from traffik.registry import ThrottleRule
*[BypassThrottleRule]: Bypasses throttling entirely when matched (inverted logic vs ThrottleRule). Import: from traffik.registry import BypassThrottleRule
*[Rate]: A rate specification parsed from a string like "100/min". Holds the request limit and window duration. Import: from traffik.rates import Rate
*[EXEMPTED]: Sentinel returned from an identifier function to skip throttling for that connection entirely. Import: from traffik import EXEMPTED
*[Headers]: A collection of rate limit response headers, resolved per request. Import: from traffik.headers import Headers
*[Header]: A single rate limit response header with a name, value resolver, and inclusion condition. Import: from traffik.headers import Header
*[StrategyStat]: Statistics snapshot returned by a throttling strategy: hits remaining, wait time, rate, and metadata. Import: from traffik.types import StrategyStat
