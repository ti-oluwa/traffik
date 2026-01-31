#!/usr/bin/env python3
"""
Performance Profiling for Traffik Throttles

Uses cProfile, line_profiler, and memory_profiler to identify bottlenecks.
Helps diagnose why Traffik is slower than slowapi in benchmarks.

Usage:
    python perf_profile.py --profile cpu --backend inmemory --strategy fixed-window
    python perf_profile.py --profile memory --backend redis
    python perf_profile.py --profile line --function "increment_with_ttl"
    python perf_profile.py --profile all  # Run all profiling
"""

import argparse  # noqa: I001
import asyncio
import cProfile
from contextlib import contextmanager
import io
import pstats
import sys
import time
import tracemalloc

from line_profiler import LineProfiler
from starlette.datastructures import Headers

from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.exceptions import ConnectionThrottled
from traffik.strategies import FixedWindowStrategy
from traffik.throttles import HTTPThrottle

sys.path.insert(0, ".")


class MockRequest:
    """Lightweight mock request for profiling."""

    def __init__(self, path: str = "/test", client_ip: str = "127.0.0.1"):
        self.scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": (client_ip, 12345),
        }
        self.app = type("obj", (object,), {"state": type("obj", (object,), {})()})()

    @property
    def state(self):
        return self.app.state  # type: ignore

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


class CPUProfiler:
    """CPU profiling using cProfile."""

    def __init__(self, sort_by: str = "cumulative"):
        self.sort_by = sort_by
        self.profiler = cProfile.Profile()

    @contextmanager
    def profile(self):
        """Context manager for profiling."""
        self.profiler.enable()
        try:
            yield
        finally:
            self.profiler.disable()

    def print_stats(self, top_n: int = 30):
        """Print profiling statistics."""
        s = io.StringIO()
        ps = pstats.Stats(self.profiler, stream=s)
        ps.strip_dirs()
        ps.sort_stats(self.sort_by)

        print("\n" + "=" * 80)
        print("CPU PROFILING RESULTS (Top Functions by Cumulative Time)")
        print("=" * 80 + "\n")

        ps.print_stats(top_n)
        print(s.getvalue())

        # Also print callers for top functions
        print("\n" + "=" * 80)
        print("CALLERS (Who called these functions)")
        print("=" * 80 + "\n")

        ps.print_callers(10)
        print(s.getvalue())

    def save_stats(self, filename: str):
        """Save stats to file for later analysis."""
        self.profiler.dump_stats(filename)
        print(f"\n✅ CPU profile saved to {filename}")
        print(f"   Analyze with: python -m pstats {filename}")


async def run_cpu_profile(backend, strategy, num_requests: int = 100):
    """Run CPU profiling on throttle operations."""

    throttle = HTTPThrottle(
        uid="cpu_profile",
        rate="50/10s",
        strategy=strategy,
    )
    request = MockRequest()
    profiler = CPUProfiler()

    async with backend(close_on_exit=True, persistent=False):
        print(f"\n🔍 CPU Profiling {num_requests} throttle requests...")

        with profiler.profile():
            for i in range(num_requests):
                try:
                    await throttle(request)  # type: ignore
                except ConnectionThrottled:
                    pass

        profiler.print_stats()
        profiler.save_stats("cpu_profile.stats")


class MemoryProfiler:
    """Memory profiling using tracemalloc."""

    def __init__(self):
        self.snapshots = []

    def start(self):
        """Start memory tracking."""
        tracemalloc.start()
        self.snapshots = []

    def snapshot(self, label: str = ""):
        """Take a memory snapshot."""
        snapshot = tracemalloc.take_snapshot()
        self.snapshots.append((label, snapshot))

    def stop(self):
        """Stop memory tracking."""
        tracemalloc.stop()

    def print_stats(self, top_n: int = 20):
        """Print memory statistics."""
        if len(self.snapshots) < 2:
            print("Need at least 2 snapshots to compare")
            return

        print("\n" + "=" * 80)
        print("MEMORY PROFILING RESULTS")
        print("=" * 80 + "\n")

        # Compare first and last snapshot
        first_label, first_snap = self.snapshots[0]
        last_label, last_snap = self.snapshots[-1]

        print(f"Comparing: '{first_label}' → '{last_label}'\n")

        # Top memory allocations
        stats = last_snap.compare_to(first_snap, "lineno")

        print(f"Top {top_n} Memory Allocations:\n")
        print(f"{'File:Line':<50} {'Size Diff':<15} {'Count Diff':<12}")
        print("-" * 80)

        for stat in stats[:top_n]:
            print(
                f"{str(stat.traceback):<50} {self._format_size(stat.size_diff):<15} {stat.count_diff:>12}"
            )

        # Total memory
        total_before = sum(stat.size for stat in first_snap.statistics("filename"))
        total_after = sum(stat.size for stat in last_snap.statistics("filename"))

        print("\n" + "-" * 80)
        print(f"Total Memory Before: {self._format_size(total_before)}")
        print(f"Total Memory After:  {self._format_size(total_after)}")
        print(f"Memory Increase:     {self._format_size(total_after - total_before)}")
        print("=" * 80 + "\n")

    @staticmethod
    def _format_size(size: float) -> str:
        """Format size in human-readable format."""
        for unit in ["B", "KB", "MB", "GB"]:
            if abs(size) < 1024.0:
                return f"{size:>6.1f} {unit}"
            size /= 1024.0
        return f"{size:>6.1f} TB"


async def run_memory_profile(backend, strategy, num_requests: int = 100):
    """Run memory profiling on throttle operations."""
    throttle = HTTPThrottle(
        uid="memory_profile",
        rate="50/10s",
        strategy=strategy,
    )
    request = MockRequest()
    profiler = MemoryProfiler()

    async with backend(close_on_exit=True, persistent=False):
        print(f"\n🔍 Memory Profiling {num_requests} throttle requests...")

        profiler.start()
        profiler.snapshot("Start")

        for i in range(num_requests):
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass

            # Take snapshots at intervals
            if i in [10, 50, 100]:
                profiler.snapshot(f"After {i} requests")

        profiler.snapshot("End")
        profiler.stop()
        profiler.print_stats()


async def run_line_profile(backend, strategy, num_requests: int = 100):
    """Run line-by-line profiling."""

    throttle = HTTPThrottle(
        uid="line_profile",
        rate="50/10s",
        strategy=strategy,
    )
    request = MockRequest()

    # Create line profiler
    lp = LineProfiler()

    # Add functions to profile
    lp.add_function(throttle.__call__)
    lp.add_function(throttle.get_backend)
    lp.add_function(throttle.get_scoped_key)
    lp.add_function(throttle.get_namespaced_key)
    lp.add_function(throttle._handle_error)

    # Add backend methods
    if hasattr(backend, "increment"):
        lp.add_function(backend.increment)
    if hasattr(backend, "decrement"):
        lp.add_function(backend.decrement)
    if hasattr(backend, "expire"):
        lp.add_function(backend.expire)
    if hasattr(backend, "increment_with_ttl"):
        lp.add_function(backend.increment_with_ttl)
    if hasattr(backend, "get"):
        lp.add_function(backend.get)
    if hasattr(backend, "set"):
        lp.add_function(backend.set)

    # Add strategy
    if hasattr(strategy, "__call__"):
        lp.add_function(strategy.__call__)

    print(f"\n🔍 Line-by-Line Profiling {num_requests} throttle requests...")

    async with backend(close_on_exit=True, persistent=False):
        # Wrap the async function
        async def profile_work():
            for i in range(num_requests):
                try:
                    await throttle(request)  # type: ignore
                except ConnectionThrottled:
                    pass

        # Run with profiling
        lp_wrapper = lp(profile_work)
        await lp_wrapper()

    # Print stats
    print("\n" + "=" * 80)
    print("LINE-BY-LINE PROFILING RESULTS")
    print("=" * 80 + "\n")
    lp.print_stats()


async def run_comparative_profile(num_requests: int = 100):
    """Profile Traffik and compare with what slowapi would do."""

    print("\n" + "=" * 80)
    print("COMPARATIVE PROFILING: Traffik vs Expected slowapi Performance")
    print("=" * 80 + "\n")

    # Profile Traffik
    backend = InMemoryBackend(namespace="profile")
    throttle = HTTPThrottle(
        uid="compare",
        rate="100/60s",
        strategy=FixedWindowStrategy(),
    )
    request = MockRequest()

    async with backend(close_on_exit=True, persistent=False):
        # Warm up
        for _ in range(10):
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass

        # Time Traffik
        start = time.perf_counter()
        for i in range(num_requests):
            try:
                await throttle(request)  # type: ignore
            except ConnectionThrottled:
                pass
        traffik_duration = time.perf_counter() - start

    # Simulate what slowapi does (simplified)
    # slowapi uses limits library which does basic INCR + EXPIRE
    from collections import defaultdict

    cache = defaultdict(int)

    def slowapi_check():
        """Simplified slowapi-style rate limiting."""
        key = "test_key"
        cache[key] += 1
        return cache[key] <= 100

    # Time slowapi simulation
    start = time.perf_counter()
    for i in range(num_requests):
        slowapi_check()
    slowapi_duration = time.perf_counter() - start

    # Results
    print("Traffik Performance:")
    print(f"  Total Time:      {traffik_duration * 1000:.2f}ms")
    print(f"  Per Request:     {traffik_duration * 1000 / num_requests:.3f}ms")
    print(f"  Requests/sec:    {num_requests / traffik_duration:.1f}")

    print("\nslowapi Simulation:")
    print(f"  Total Time:      {slowapi_duration * 1000:.2f}ms")
    print(f"  Per Request:     {slowapi_duration * 1000 / num_requests:.3f}ms")
    print(f"  Requests/sec:    {num_requests / slowapi_duration:.1f}")

    print("\nDifference:")
    print(f"  Traffik is {traffik_duration / slowapi_duration:.1f}x slower")
    print(
        f"  Overhead:        {(traffik_duration - slowapi_duration) * 1000:.2f}ms total"
    )
    print(
        f"  Overhead/req:    {(traffik_duration - slowapi_duration) * 1000 / num_requests:.3f}ms"
    )

    print("\n" + "=" * 80 + "\n")


async def analyze_hot_path(backend, strategy):
    """Analyze the hot path through a single throttle request."""

    call_stack = []

    # Create a tracer
    def trace_calls(frame, event, arg):
        if event == "call":
            code = frame.f_code
            filename = code.co_filename

            # Only trace traffik code
            if "traffik" in filename:
                func_name = code.co_name
                file_short = filename.split("/")[-1]
                call_stack.append(f"{file_short}:{func_name}")

        return trace_calls

    throttle = HTTPThrottle(
        uid="hotpath",
        rate="10/10s",
        strategy=strategy,
    )
    request = MockRequest()

    print("\n" + "=" * 80)
    print("HOT PATH ANALYSIS (Call Stack for Single Request)")
    print("=" * 80 + "\n")

    async with backend(close_on_exit=True, persistent=False):
        # Set the tracer
        sys.settrace(trace_calls)

        try:
            await throttle(request)  # type: ignore
        except ConnectionThrottled:
            pass

        # Stop tracing
        sys.settrace(None)

    # Print call stack
    print("Call Stack (traffik functions only):\n")
    for i, call in enumerate(call_stack[:50], 1):  # Limit to first 50
        print(f"  {i}. {call}")

    if len(call_stack) > 50:
        print(f"  ... ({len(call_stack) - 50} more calls)")

    print(f"\nTotal traffik function calls: {len(call_stack)}")
    print("=" * 80 + "\n")


async def main(args):
    """Main profiling entry point."""

    # Create backend
    if args.backend == "inmemory":
        backend = InMemoryBackend(namespace="profile")
    elif args.backend == "redis":
        backend = RedisBackend(
            connection=args.redis_url,
            namespace="profile",
        )
    else:
        raise ValueError(f"Unknown backend: {args.backend}")

    # Create strategy
    strategy = FixedWindowStrategy()

    # Run requested profiling
    if args.profile == "cpu" or args.profile == "all":
        await run_cpu_profile(backend, strategy, args.requests)

    if args.profile == "memory" or args.profile == "all":
        await run_memory_profile(backend, strategy, args.requests)

    if args.profile == "line" or args.profile == "all":
        await run_line_profile(backend, strategy, args.requests)

    if args.profile == "compare" or args.profile == "all":
        await run_comparative_profile(args.requests)

    if args.profile == "hotpath" or args.profile == "all":
        await analyze_hot_path(backend, strategy)


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Performance profiling for Traffik throttles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # CPU profiling
  python perf_profile.py --profile cpu --requests 1000
  
  # Memory profiling
  python perf_profile.py --profile memory --requests 500
  
  # Line-by-line profiling (requires line_profiler)
  python perf_profile.py --profile line --requests 100
  
  # Compare with slowapi
  python perf_profile.py --profile compare --requests 1000
  
  # Analyze hot path
  python perf_profile.py --profile hotpath
  
  # Run all profiling
  python perf_profile.py --profile all --requests 500
        """,
    )

    parser.add_argument(
        "--profile",
        choices=["cpu", "memory", "line", "compare", "hotpath", "all"],
        default="cpu",
        help="Type of profiling to run",
    )

    parser.add_argument(
        "--backend",
        choices=["inmemory", "redis"],
        default="inmemory",
        help="Backend to profile",
    )

    parser.add_argument(
        "--requests",
        type=int,
        default=100,
        help="Number of requests to profile",
    )

    parser.add_argument(
        "--redis-url",
        default="redis://localhost:6379/0",
        help="Redis URL",
    )

    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\n⚠️  Profiling interrupted")
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    cli()
