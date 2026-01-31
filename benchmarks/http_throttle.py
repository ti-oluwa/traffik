#!/usr/bin/env env python3
"""
Comprehensive Throttle Profiling Script

Profiles throttle performance, correctness, and behavior under various conditions.
Provides detailed metrics, visualizations, and analysis.

Usage:
    python profile_throttle.py --backend redis --strategy fixed-window --rate "100/m"
    python profile_throttle.py --all  # Run all profiles
    python profile_throttle.py --compare  # Compare strategies
"""

import argparse  # noqa: I001
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import json
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from starlette.datastructures import Headers

from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.memcached import MemcachedBackend
from traffik.backends.redis import RedisBackend
from traffik.exceptions import ConnectionThrottled
from traffik.rates import Rate
from traffik.strategies import (
    FixedWindowStrategy,
    LeakyBucketStrategy,
    SlidingWindowCounterStrategy,
    SlidingWindowLogStrategy,
    TokenBucketStrategy,
)
from traffik.throttles import HTTPThrottle

# Add parent directory to path if running from examples
sys.path.insert(0, ".")


@dataclass
class ThrottleMetrics:
    """Metrics for a single throttle operation."""

    duration_ms: float
    throttled: bool
    wait_time_ms: float
    timestamp: float


@dataclass
class ProfileResult:
    """Results from a profiling run."""

    name: str
    strategy_name: str
    backend_name: str
    rate_limit: str

    # Timing metrics
    total_duration_s: float
    requests_per_second: float
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    max_latency_ms: float
    min_latency_ms: float

    # Throttle metrics
    total_requests: int
    allowed_requests: int
    throttled_requests: int
    throttle_rate: float

    # Correctness metrics
    limit_violations: int
    false_positives: int  # Throttled when should be allowed
    false_negatives: int  # Allowed when should be throttled

    # Memory metrics (if available)
    memory_mb: Optional[float] = None

    # Detailed metrics
    metrics: List[ThrottleMetrics] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "strategy": self.strategy_name,
            "backend": self.backend_name,
            "rate_limit": self.rate_limit,
            "performance": {
                "total_duration_s": round(self.total_duration_s, 3),
                "requests_per_second": round(self.requests_per_second, 1),
                "latency_ms": {
                    "avg": round(self.avg_latency_ms, 3),
                    "p50": round(self.p50_latency_ms, 3),
                    "p95": round(self.p95_latency_ms, 3),
                    "p99": round(self.p99_latency_ms, 3),
                    "max": round(self.max_latency_ms, 3),
                    "min": round(self.min_latency_ms, 3),
                },
            },
            "throttling": {
                "total_requests": self.total_requests,
                "allowed": self.allowed_requests,
                "throttled": self.throttled_requests,
                "throttle_rate": round(self.throttle_rate, 3),
            },
            "correctness": {
                "limit_violations": self.limit_violations,
                "false_positives": self.false_positives,
                "false_negatives": self.false_negatives,
                "accuracy": round(
                    1
                    - (self.false_positives + self.false_negatives)
                    / max(self.total_requests, 1),
                    3,
                ),
            },
            "memory_mb": round(self.memory_mb, 2) if self.memory_mb else None,
        }

    def print_summary(self):
        """Print a formatted summary of results."""
        print(f"\n{'=' * 70}")
        print(f"Profile: {self.name}")
        print(f"Strategy: {self.strategy_name} | Backend: {self.backend_name}")
        print(f"Rate Limit: {self.rate_limit}")
        print(f"{'=' * 70}")

        print("\n📊 PERFORMANCE METRICS:")
        print(f"  Total Duration:      {self.total_duration_s:.3f}s")
        print(f"  Requests/Second:     {self.requests_per_second:.1f} req/s")
        print(f"  Avg Latency:         {self.avg_latency_ms:.3f}ms")
        print(f"  P50 Latency:         {self.p50_latency_ms:.3f}ms")
        print(f"  P95 Latency:         {self.p95_latency_ms:.3f}ms")
        print(f"  P99 Latency:         {self.p99_latency_ms:.3f}ms")
        print(f"  Max Latency:         {self.max_latency_ms:.3f}ms")

        print("\n🚦 THROTTLING METRICS:")
        print(f"  Total Requests:      {self.total_requests}")
        print(
            f"  Allowed:             {self.allowed_requests} ({self.allowed_requests / self.total_requests * 100:.1f}%)"
        )
        print(
            f"  Throttled:           {self.throttled_requests} ({self.throttle_rate * 100:.1f}%)"
        )

        print("\n✅ CORRECTNESS METRICS:")
        accuracy = 1 - (self.false_positives + self.false_negatives) / max(
            self.total_requests, 1
        )
        print(f"  Limit Violations:    {self.limit_violations}")
        print(f"  False Positives:     {self.false_positives}")
        print(f"  False Negatives:     {self.false_negatives}")
        print(f"  Accuracy:            {accuracy * 100:.1f}%")

        if self.memory_mb:
            print("\n💾 MEMORY USAGE:")
            print(f"  Memory:              {self.memory_mb:.2f} MB")

        print(f"\n{'=' * 70}\n")


class MockRequest:
    """Mock Starlette Request for profiling."""

    def __init__(
        self, path: str = "/test", method: str = "GET", client_ip: str = "127.0.0.1"
    ):
        self.scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": (client_ip, 12345),
        }
        self.app = type("obj", (object,), {"state": type("obj", (object,), {})()})()
        self.state = self.app.state

    @property
    def headers(self):
        return Headers(raw=self.scope.get("headers", []))  # type: ignore

    @property
    def client(self):
        if "client" in self.scope:
            return type(
                "obj",
                (object,),
                {"host": self.scope["client"][0], "port": self.scope["client"][1]},
            )()
        return None


class ThrottleProfiler:
    """Comprehensive throttle profiler."""

    def __init__(
        self,
        throttle: HTTPThrottle,
        backend,
        name: str = "Profile",
    ):
        self.throttle = throttle
        self.backend = backend
        self.name = name
        self.metrics: List[ThrottleMetrics] = []

    async def profile_single_request(self, request: MockRequest) -> ThrottleMetrics:
        """Profile a single throttle request."""
        start_time = time.perf_counter()
        timestamp = time.time()
        throttled = False
        wait_time_ms = 0.0

        try:
            await self.throttle(request)  # type: ignore
        except ConnectionThrottled as exc:
            throttled = True
            wait_time_ms = exc.wait_period * 1000 if exc.wait_period else 0.0

        duration_ms = (time.perf_counter() - start_time) * 1000

        return ThrottleMetrics(
            duration_ms=duration_ms,
            throttled=throttled,
            wait_time_ms=wait_time_ms,
            timestamp=timestamp,
        )

    async def profile_burst(
        self,
        num_requests: int,
        delay_between_ms: float = 0,
    ) -> List[ThrottleMetrics]:
        """Profile a burst of requests."""
        metrics = []
        request = MockRequest()

        for i in range(num_requests):
            metric = await self.profile_single_request(request)
            metrics.append(metric)

            if delay_between_ms > 0 and i < num_requests - 1:
                await asyncio.sleep(delay_between_ms / 1000)

        return metrics

    async def profile_sustained_load(
        self,
        duration_s: float,
        requests_per_second: float,
    ) -> List[ThrottleMetrics]:
        """Profile sustained load over time."""
        metrics = []
        request = MockRequest()

        delay_between_requests = 1.0 / requests_per_second
        start_time = time.time()

        while time.time() - start_time < duration_s:
            metric = await self.profile_single_request(request)
            metrics.append(metric)

            # Sleep to maintain target RPS
            await asyncio.sleep(delay_between_requests)

        return metrics

    async def profile_concurrent_requests(
        self,
        num_concurrent: int,
        requests_per_client: int,
    ) -> List[ThrottleMetrics]:
        """Profile concurrent requests from multiple clients."""

        async def client_requests(client_id: int) -> List[ThrottleMetrics]:
            request = MockRequest(client_ip=f"192.168.1.{client_id}")
            metrics = []

            for _ in range(requests_per_client):
                metric = await self.profile_single_request(request)
                metrics.append(metric)
                await asyncio.sleep(0.01)  # Small delay

            return metrics

        # Run all clients concurrently
        tasks = [client_requests(i) for i in range(num_concurrent)]
        results = await asyncio.gather(*tasks)

        # Flatten results
        all_metrics = []
        for client_metrics in results:
            all_metrics.extend(client_metrics)

        return all_metrics

    def analyze_metrics(
        self,
        metrics: List[ThrottleMetrics],
        rate_limit: str,
    ) -> ProfileResult:
        """Analyze collected metrics and produce results."""
        if not metrics:
            raise ValueError("No metrics to analyze")

        # Calculate latencies
        latencies = [m.duration_ms for m in metrics]
        latencies.sort()

        # Calculate percentiles
        def percentile(data: List[float], p: float) -> float:
            k = (len(data) - 1) * p
            f = int(k)
            c = f + 1
            if c >= len(data):
                return data[-1]
            return data[f] + (k - f) * (data[c] - data[f])

        # Timing metrics
        total_duration = metrics[-1].timestamp - metrics[0].timestamp
        if total_duration == 0:
            total_duration = 0.001  # Avoid division by zero

        rps = len(metrics) / total_duration

        # Throttle metrics
        throttled_count = sum(1 for m in metrics if m.throttled)
        allowed_count = len(metrics) - throttled_count

        # Correctness analysis
        limit_violations, false_positives, false_negatives = self._check_correctness(
            metrics, rate_limit
        )

        return ProfileResult(
            name=self.name,
            strategy_name=self.throttle.strategy.__class__.__name__,
            backend_name=self.backend.__class__.__name__,
            rate_limit=rate_limit,
            total_duration_s=total_duration,
            requests_per_second=rps,
            avg_latency_ms=statistics.mean(latencies),
            p50_latency_ms=percentile(latencies, 0.5),
            p95_latency_ms=percentile(latencies, 0.95),
            p99_latency_ms=percentile(latencies, 0.99),
            max_latency_ms=max(latencies),
            min_latency_ms=min(latencies),
            total_requests=len(metrics),
            allowed_requests=allowed_count,
            throttled_requests=throttled_count,
            throttle_rate=throttled_count / len(metrics),
            limit_violations=limit_violations,
            false_positives=false_positives,
            false_negatives=false_negatives,
            metrics=metrics,
        )

    def _check_correctness(
        self,
        metrics: List[ThrottleMetrics],
        rate_limit_str: str,
    ) -> Tuple[int, int, int]:
        """
        Check correctness of throttling decisions.

        Returns: (limit_violations, false_positives, false_negatives)
        """
        rate = Rate.parse(rate_limit_str)
        window_ms = rate.expire
        limit = rate.limit

        # Group requests by time window
        windows: Dict[int, List[ThrottleMetrics]] = defaultdict(list)

        for metric in metrics:
            window_id = int(metric.timestamp * 1000 // window_ms)
            windows[window_id].append(metric)

        limit_violations = 0
        false_positives = 0
        false_negatives = 0

        for window_id, window_metrics in windows.items():
            allowed_in_window = sum(1 for m in window_metrics if not m.throttled)

            # Check for limit violations (more allowed than limit)
            if allowed_in_window > limit:
                limit_violations += allowed_in_window - limit

            # Check each request in window
            for i, metric in enumerate(window_metrics):
                # Count allowed requests before this one
                allowed_before = sum(1 for m in window_metrics[:i] if not m.throttled)

                if metric.throttled and allowed_before < limit:
                    # Should have been allowed but was throttled
                    false_positives += 1
                elif not metric.throttled and allowed_before >= limit:
                    # Should have been throttled but was allowed
                    false_negatives += 1

        return limit_violations, false_positives, false_negatives


async def profile_burst_scenario(
    backend,
    strategy,
    rate_limit: str = "10/10s",
) -> ProfileResult:
    """Profile burst traffic scenario."""
    throttle = HTTPThrottle(
        uid="burst_test",
        rate=rate_limit,
        strategy=strategy,
    )

    profiler = ThrottleProfiler(
        throttle=throttle,
        backend=backend,
        name="Burst Traffic",
    )

    async with backend(close_on_exit=False, persistent=False):
        # Send 50 requests as fast as possible
        metrics = await profiler.profile_burst(num_requests=50, delay_between_ms=0)
        return profiler.analyze_metrics(metrics, rate_limit)


async def profile_sustained_scenario(
    backend,
    strategy,
    rate_limit: str = "10/10s",
) -> ProfileResult:
    """Profile sustained load scenario."""
    throttle = HTTPThrottle(
        uid="sustained_test",
        rate=rate_limit,
        strategy=strategy,
    )

    profiler = ThrottleProfiler(
        throttle=throttle,
        backend=backend,
        name="Sustained Load",
    )

    async with backend(close_on_exit=False, persistent=False):
        # Send requests at 5 req/s for 10 seconds
        metrics = await profiler.profile_sustained_load(
            duration_s=10.0,
            requests_per_second=5.0,
        )
        return profiler.analyze_metrics(metrics, rate_limit)


async def profile_concurrent_scenario(
    backend,
    strategy,
    rate_limit: str = "10/10s",
) -> ProfileResult:
    """Profile concurrent clients scenario."""
    throttle = HTTPThrottle(
        uid="concurrent_test",
        rate=rate_limit,
        strategy=strategy,
    )

    profiler = ThrottleProfiler(
        throttle=throttle,
        backend=backend,
        name="Concurrent Clients",
    )

    async with backend(close_on_exit=False, persistent=False):
        # 10 concurrent clients, 10 requests each
        metrics = await profiler.profile_concurrent_requests(
            num_concurrent=10,
            requests_per_client=10,
        )
        return profiler.analyze_metrics(metrics, rate_limit)


async def profile_boundary_scenario(
    backend,
    strategy,
    rate_limit: str = "10/10s",
) -> ProfileResult:
    """Profile boundary condition scenario (requests at window edges)."""
    throttle = HTTPThrottle(
        uid="boundary_test",
        rate=rate_limit,
        strategy=strategy,
    )

    profiler = ThrottleProfiler(
        throttle=throttle,
        backend=backend,
        name="Window Boundary",
    )

    async with backend(close_on_exit=False, persistent=False):
        # Send 10 requests, wait for window, send 10 more
        metrics1 = await profiler.profile_burst(num_requests=10)
        await asyncio.sleep(11)  # Wait for window to expire
        metrics2 = await profiler.profile_burst(num_requests=10)

        all_metrics = metrics1 + metrics2
        return profiler.analyze_metrics(all_metrics, rate_limit)


async def compare_strategies(
    backend, rate_limit: str = "10/10s"
) -> Dict[str, ProfileResult]:
    """Compare all strategies."""
    strategies = {
        "FixedWindow": FixedWindowStrategy(),
        "SlidingWindowLog": SlidingWindowLogStrategy(),
        "SlidingWindowCounter": SlidingWindowCounterStrategy(),
        "TokenBucket": TokenBucketStrategy(),
        "LeakyBucket": LeakyBucketStrategy(),
    }

    results = {}

    for name, strategy in strategies.items():
        print(f"\n📊 Profiling {name}...")
        result = await profile_burst_scenario(backend, strategy, rate_limit)
        results[name] = result

    return results


def print_comparison_table(results: Dict[str, ProfileResult]):
    """Print comparison table of results."""
    print(f"\n{'=' * 100}")
    print("STRATEGY COMPARISON")
    print(f"{'=' * 100}")

    # Header
    print(
        f"{'Strategy':<20} {'Avg Latency':<15} {'P95 Latency':<15} {'RPS':<12} {'Accuracy':<10}"
    )
    print(f"{'-' * 100}")

    # Sort by average latency
    sorted_results = sorted(results.items(), key=lambda x: x[1].avg_latency_ms)

    for name, result in sorted_results:
        accuracy = 1 - (result.false_positives + result.false_negatives) / max(
            result.total_requests, 1
        )

        print(
            f"{name:<20} "
            f"{result.avg_latency_ms:<15.3f} "
            f"{result.p95_latency_ms:<15.3f} "
            f"{result.requests_per_second:<12.1f} "
            f"{accuracy * 100:<10.1f}%"
        )

    print(f"{'=' * 100}\n")


async def run_profile(args):
    """Run profiling based on CLI arguments."""

    # Create backend
    if args.backend == "inmemory":
        backend = InMemoryBackend(namespace="profile")
    elif args.backend == "redis":
        backend = RedisBackend(
            connection=args.redis_url,
            namespace="profile",
        )
    elif args.backend == "memcached":
        backend = MemcachedBackend(
            host=args.memcached_host,
            port=args.memcached_port,
            namespace="profile",
        )
    else:
        raise ValueError(f"Unknown backend: {args.backend}")

    # Initialize backend
    async with backend(close_on_exit=True, persistent=False):
        # Create strategy
        strategy_map = {
            "fixed-window": FixedWindowStrategy(),
            "sliding-window-log": SlidingWindowLogStrategy(),
            "sliding-window-counter": SlidingWindowCounterStrategy(),
            "token-bucket": TokenBucketStrategy(),
            "leaky-bucket": LeakyBucketStrategy(),
        }

        if args.compare:
            # Compare all strategies
            results = await compare_strategies(backend, args.rate)

            # Print individual results
            for name, result in results.items():
                result.print_summary()

            # Print comparison table
            print_comparison_table(results)

            # Save to file if requested
            if args.output:
                comparison_data = {
                    name: result.to_dict() for name, result in results.items()
                }
                with open(args.output, "w") as f:
                    json.dump(comparison_data, f, indent=2)
                print(f"✅ Results saved to {args.output}")

        else:
            # Single strategy profile
            strategy = strategy_map.get(args.strategy)
            if not strategy:
                raise ValueError(f"Unknown strategy: {args.strategy}")

            # Run all scenarios
            print(f"\n🚀 Profiling {args.strategy} with {args.backend} backend")
            print(f"Rate limit: {args.rate}")

            scenarios = [
                ("Burst Traffic", profile_burst_scenario),
                ("Sustained Load", profile_sustained_scenario),
                ("Concurrent Clients", profile_concurrent_scenario),
                ("Window Boundary", profile_boundary_scenario),
            ]

            results = {}

            for scenario_name, scenario_func in scenarios:
                print(f"\n📊 Running {scenario_name} scenario...")
                result = await scenario_func(backend, strategy, args.rate)
                result.print_summary()
                results[scenario_name] = result

            # Save to file if requested
            if args.output:
                output_data = {
                    name: result.to_dict() for name, result in results.items()
                }
                with open(args.output, "w") as f:
                    json.dump(output_data, f, indent=2)
                print(f"✅ Results saved to {args.output}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Profile Traffik throttle performance and correctness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Profile Fixed Window strategy with Redis
  python profile_throttle.py --backend redis --strategy fixed-window --rate "100/m"
  
  # Profile Sliding Window with in-memory backend
  python profile_throttle.py --backend inmemory --strategy sliding-window-log
  
  # Compare all strategies
  python profile_throttle.py --compare --backend inmemory --rate "50/10s"
  
  # Save results to file
  python profile_throttle.py --compare --output results.json
        """,
    )

    parser.add_argument(
        "--backend",
        choices=["inmemory", "redis", "memcached"],
        default="inmemory",
        help="Backend to use (default: inmemory)",
    )

    parser.add_argument(
        "--strategy",
        choices=[
            "fixed-window",
            "sliding-window-log",
            "sliding-window-counter",
            "token-bucket",
            "leaky-bucket",
        ],
        default="fixed-window",
        help="Strategy to profile (default: fixed-window)",
    )

    parser.add_argument(
        "--rate",
        default="10/10s",
        help="Rate limit (default: 10/10s)",
    )

    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare all strategies",
    )

    parser.add_argument(
        "--output",
        "-o",
        help="Output file for results (JSON)",
    )

    # Redis options
    parser.add_argument(
        "--redis-url",
        default="redis://localhost:6379/0",
        help="Redis connection URL (default: redis://localhost:6379/0)",
    )

    # Memcached options
    parser.add_argument(
        "--memcached-host",
        default="localhost",
        help="Memcached host (default: localhost)",
    )

    parser.add_argument(
        "--memcached-port",
        type=int,
        default=11211,
        help="Memcached port (default: 11211)",
    )

    args = parser.parse_args()

    # Run profiling
    try:
        asyncio.run(run_profile(args))
    except KeyboardInterrupt:
        print("\n\n⚠️  Profiling interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
