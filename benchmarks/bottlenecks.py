#!/usr/bin/env python3
"""
Traffik Bottleneck Diagnostic

Identifies specific bottlenecks causing slowness compared to slowapi.
Based on benchmark results showing:
- Traffik: 24-133 req/s
- slowapi: 165-423 req/s
- Traffik is 2-6x slower

This script will pinpoint WHERE the slowdown occurs.
"""
from traffik.backends.memcached import MemcachedBackend
from traffik.backends.redis import RedisBackend

import asyncio
from dataclasses import dataclass
import sys
import time
from typing import Callable, List

from starlette.datastructures import Headers

from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import ConnectionThrottled
from traffik.strategies import FixedWindowStrategy
from traffik.strategies.token_bucket import TokenBucketStrategy
from traffik.throttles import HTTPThrottle

sys.path.insert(0, ".")


class MockRequest:
    def __init__(self):
        self.scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "client": ("127.0.0.1", 12345),
        }
        self.app = type("obj", (object,), {"state": type("obj", (object,), {})()})()

    @property
    def headers(self):
        return Headers(raw=[])

    @property
    def client(self):
        return type("obj", (object,), {"host": "127.0.0.1", "port": 12345})()


@dataclass
class TimingResult:
    """Timing result for a code segment."""

    name: str
    duration_ms: float
    calls: int
    avg_ms: float


class MicroBenchmark:
    """Micro-benchmark individual components."""

    def __init__(self):
        self.results: List[TimingResult] = []

    async def time_async(self, name: str, func: Callable, calls: int = 1000):
        """Time an async function."""
        start = time.perf_counter()

        for _ in range(calls):
            await func()

        duration = (time.perf_counter() - start) * 1000
        avg = duration / calls

        result = TimingResult(
            name=name,
            duration_ms=duration,
            calls=calls,
            avg_ms=avg,
        )
        self.results.append(result)

        print(f"✓ {name:<50} {duration:>8.2f}ms total | {avg:>8.3f}ms avg")

    def time_sync(self, name: str, func: Callable, calls: int = 1000):
        """Time a sync function."""
        start = time.perf_counter()

        for _ in range(calls):
            func()

        duration = (time.perf_counter() - start) * 1000
        avg = duration / calls

        result = TimingResult(
            name=name,
            duration_ms=duration,
            calls=calls,
            avg_ms=avg,
        )
        self.results.append(result)

        print(f"✓ {name:<50} {duration:>8.2f}ms total | {avg:>8.3f}ms avg")

    def print_summary(self):
        """Print summary of all timings."""
        print("\n" + "=" * 80)
        print("MICRO-BENCHMARK SUMMARY (Sorted by Average Time)")
        print("=" * 80)
        print(
            f"{'Component':<50} {'Total (ms)':>12} {'Avg (ms)':>12} {'% of Total':>12}"
        )
        print("-" * 80)

        # Sort by average time
        sorted_results = sorted(self.results, key=lambda x: x.avg_ms, reverse=True)
        total_time = sum(r.duration_ms for r in self.results)

        for result in sorted_results:
            pct = (result.duration_ms / total_time * 100) if total_time > 0 else 0
            print(
                f"{result.name:<50} {result.duration_ms:>12.2f} {result.avg_ms:>12.3f} {pct:>11.1f}%"
            )

        print("=" * 80 + "\n")


async def diagnose_bottlenecks():
    """Diagnose where Traffik is spending time."""

    print("\n" + "=" * 80)
    print("TRAFFIK BOTTLENECK DIAGNOSTIC")
    print("=" * 80)
    print("\nTesting individual components (1000 iterations each)...\n")

    bench = MicroBenchmark()

    # Setup
    backend = RedisBackend(namespace="diag", connection="redis://localhost:6379/0")
    # backend = InMemoryBackend(namespace="diag")
    # backend = MemcachedBackend(namespace="diag")
    throttle = HTTPThrottle(
        uid="diag",
        rate="100/60s",
        strategy=FixedWindowStrategy(),
        backend=backend,
    )
    request = MockRequest()

    async with backend(close_on_exit=True, persistent=False):
        # 1. Test request creation overhead
        await bench.time_async(
            "1. Create MockRequest",
            lambda: asyncio.sleep(0) if MockRequest() else None,
            calls=1000,
        )

        # 2. Test identifier (IP extraction)
        await bench.time_async(
            "2. Get client identifier",
            lambda: backend.identifier(request),  # type: ignore
            calls=1000,
        )

        # 3. Test key generation
        bench.time_sync(
            "3. Generate namespaced key",
            lambda: throttle.get_namespaced_key(request, "127.0.0.1:/test"),  # type: ignore
            calls=1000,
        )

        # 4. Test backend get_key
        bench.time_sync(
            "4. Backend get_key (hashing)",
            lambda: backend.get_key("test_key"),
            calls=1000,
        )

        # 5. Test lock acquisition
        async def test_lock():
            async with await backend.lock("test_lock"):
                pass

        await bench.time_async("5. Lock acquire/release", test_lock, calls=1000)

        # 6. Test backend.get
        await backend.set("test_get", "value")
        await bench.time_async(
            "6. Backend get()", lambda: backend.get("test_get"), calls=1000
        )

        # 7. Test backend.set
        await bench.time_async(
            "7. Backend set()", lambda: backend.set("test_set", "value"), calls=1000
        )

        # 8. Test backend.increment
        await bench.time_async(
            "8. Backend increment()", lambda: backend.increment("test_incr"), calls=1000
        )

        # 9. Test backend.increment_with_ttl
        await bench.time_async(
            "9. Backend increment_with_ttl()",
            lambda: backend.increment_with_ttl("test_incr_ttl", ttl=60),
            calls=1000,
        )

        # 10. Test strategy call (without backend ops)
        strategy = FixedWindowStrategy()
        from traffik.rates import Rate

        rate = Rate.parse("100/60s")

        # Pre-populate some data
        for i in range(10):
            await backend.increment_with_ttl(f"strategy_test:{i}", ttl=60)

        await bench.time_async(
            "10. Strategy __call__ (with backend)",
            lambda: strategy("strategy_test:1", rate, backend, cost=1),
            calls=1000,
        )

        # 11. Test full throttle call (allowed)
        await bench.time_async(
            "11. Full throttle() call (allowed)",
            lambda: throttle(request),  # type: ignore
            calls=100,  # Fewer calls since we'll hit limit
        )

        # 12. Test full throttle call (throttled)
        # Exceed limit first
        for _ in range(100):
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass

        async def throttled_call():
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass

        await bench.time_async(
            "12. Full throttle() call (throttled)", throttled_call, calls=100
        )

    bench.print_summary()

    # Identify bottlenecks
    print("\n" + "=" * 80)
    print("BOTTLENECK ANALYSIS")
    print("=" * 80 + "\n")

    # Find slowest operations
    sorted_results = sorted(bench.results, key=lambda x: x.avg_ms, reverse=True)

    print("🔴 TOP 5 SLOWEST OPERATIONS:\n")
    for i, result in enumerate(sorted_results[:5], 1):
        print(f"  {i}. {result.name}")
        print(f"     Average: {result.avg_ms:.3f}ms per call")
        print(f"     Impact: {result.duration_ms:.2f}ms total\n")

    # Calculate overhead breakdown
    full_call_time = next(
        (r.avg_ms for r in bench.results if "Full throttle()" in r.name), 0
    )

    if full_call_time > 0:
        print("📊 OVERHEAD BREAKDOWN (for full throttle call):\n")
        print(f"  Full throttle() call: {full_call_time:.3f}ms")

        component_times = {
            "Identifier": next(
                (r.avg_ms for r in bench.results if "identifier" in r.name), 0
            ),
            "Key generation": next(
                (r.avg_ms for r in bench.results if "namespaced key" in r.name), 0
            ),
            "Lock": next((r.avg_ms for r in bench.results if "Lock" in r.name), 0),
            "Backend increment": next(
                (r.avg_ms for r in bench.results if "increment_with_ttl" in r.name), 0
            ),
            "Strategy": next(
                (r.avg_ms for r in bench.results if "Strategy __call__" in r.name), 0
            ),
        }

        print()
        for component, time_ms in sorted(
            component_times.items(), key=lambda x: x[1], reverse=True
        ):
            if time_ms > 0:
                pct = (time_ms / full_call_time * 100) if full_call_time > 0 else 0
                print(f"  - {component:<20} {time_ms:>8.3f}ms ({pct:>5.1f}%)")

    print("\n" + "=" * 80 + "\n")


async def compare_with_baseline():
    """Compare Traffik with a minimal baseline."""

    print("\n" + "=" * 80)
    print("COMPARISON WITH MINIMAL BASELINE")
    print("=" * 80 + "\n")

    calls = 1000

    # Baseline: Absolutely minimal rate limiting
    counter = {"count": 0}

    def baseline_check():
        counter["count"] += 1
        return counter["count"] <= 100

    start = time.perf_counter()
    for _ in range(calls):
        baseline_check()
    baseline_time = (time.perf_counter() - start) * 1000

    print(
        f"Baseline (dict increment):        {baseline_time:.2f}ms total | {baseline_time / calls:.3f}ms avg"
    )

    # Traffik
    backend = InMemoryBackend(namespace="compare")
    throttle = HTTPThrottle(
        uid="compare", rate="100/60s", strategy=FixedWindowStrategy()
    )
    request = MockRequest()

    async with backend(close_on_exit=True, persistent=False):
        start = time.perf_counter()
        for _ in range(calls):
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass
        traffik_time = (time.perf_counter() - start) * 1000

    print(
        f"Traffik (full implementation):    {traffik_time:.2f}ms total | {traffik_time / calls:.3f}ms avg"
    )

    overhead = traffik_time - baseline_time
    multiplier = traffik_time / baseline_time if baseline_time > 0 else 0

    print(f"\nOverhead: {overhead:.2f}ms total | {overhead / calls:.3f}ms per call")
    print(f"Multiplier: {multiplier:.1f}x slower than baseline")

    print("\n" + "=" * 80 + "\n")


async def main():
    """Run all diagnostics."""

    print("\n🔍 TRAFFIK PERFORMANCE DIAGNOSTIC TOOL\n")
    print("This will identify exactly where Traffik is spending time.\n")

    await diagnose_bottlenecks()
    await compare_with_baseline()

    print("\n📋 RECOMMENDATIONS:\n")
    print("Based on the results above, focus optimization efforts on:")
    print("1. The top 2-3 slowest operations")
    print("2. Any operation taking >1ms on average")
    print("3. Operations called frequently (check call counts)")
    print("\nLikely culprits:")
    print("  - Lock acquisition overhead (if >0.5ms)")
    print("  - Hash generation for keys (if >0.1ms)")
    print("  - Backend operations (if >0.5ms)")
    print("  - Strategy complexity (if >1ms)")

    print("\n✅ Diagnostic complete!\n")


if __name__ == "__main__":
    asyncio.run(main())
