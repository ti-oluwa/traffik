"""
Response headers overhead benchmarks for Traffik.

Measures the throughput and latency impact of the `Headers` API across different
header configurations: no headers, default presets, custom resolvers, and many headers.

Usage:
    python benchmarks/headers.py --help
    python benchmarks/headers.py --scenarios no-headers,default-always,many-headers
    python benchmarks/headers.py --concurrency 100 --num-requests 1000 --iterations 5
"""

import argparse
import asyncio
import contextlib
import statistics
import time
import typing
from dataclasses import dataclass, field

import httpx
from base import custom_identifier  # type: ignore[import]
from fastapi import Depends, FastAPI, Request

from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import (
    DEFAULT_HEADERS_ALWAYS,
    DEFAULT_HEADERS_THROTTLED,
    Header,
    Headers,
)
from traffik.registry import ThrottleRegistry
from traffik.strategies.fixed_window import FixedWindowStrategy


@dataclass
class ScenarioResult:
    """Results from a single benchmark scenario."""

    name: str
    total_requests: int
    successful_requests: int
    throttled_requests: int
    total_time: float
    latencies: typing.List[float]

    @property
    def requests_per_second(self) -> float:
        return self.total_requests / self.total_time if self.total_time > 0 else 0

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) * 1000 if self.latencies else 0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0
        sl = sorted(self.latencies)
        return sl[min(int(len(sl) * 0.95), len(sl) - 1)] * 1000

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0
        sl = sorted(self.latencies)
        return sl[min(int(len(sl) * 0.99), len(sl) - 1)] * 1000


@dataclass
class BenchmarkConfig:
    scenarios: typing.List[str] = field(
        default_factory=lambda: [
            "no-headers",
            "default-always",
            "default-throttled",
            "custom-headers",
            "many-headers",
        ]
    )
    iterations: int = 3
    concurrency: int = 50
    num_requests: int = 500


def create_app(
    headers: typing.Mapping[str, typing.Union[Header[Request], str]],
    limit: int = 1000,
    window: int = 60,
):
    """Create a FastAPI app with the given headers configuration."""
    backend = InMemoryBackend(
        namespace="bench:headers",
        identifier=custom_identifier,
        persistent=False,
        number_of_shards=5,
    )
    throttle = HTTPThrottle(
        uid="bench_headers",
        rate=f"{limit}/{window}s",
        backend=backend,  # type: ignore[arg-type]
        strategy=FixedWindowStrategy(),
        registry=ThrottleRegistry(),
        headers=headers,
    )

    app = FastAPI()

    @app.get("/test", dependencies=[Depends(throttle)])
    async def test_endpoint():
        return {"status": "ok"}

    return app, backend


async def _run_concurrent(
    client: httpx.AsyncClient, num_requests: int, concurrency: int
) -> tuple:
    """Run concurrent HTTP requests in batches and collect results."""
    latencies: typing.List[float] = []
    successful = 0
    throttled = 0

    async def make_request():
        req_start = time.perf_counter()
        response = await client.get("/test")
        req_end = time.perf_counter()
        return req_end - req_start, response.status_code

    for batch_start in range(0, num_requests, concurrency):
        batch_size = min(concurrency, num_requests - batch_start)
        results = await asyncio.gather(*[make_request() for _ in range(batch_size)])
        for req_time, status in results:
            latencies.append(req_time)
            if status == 200:
                successful += 1
            elif status == 429:
                throttled += 1

    return latencies, successful, throttled


async def run_scenario(
    name: str,
    headers: typing.Mapping[str, typing.Union[Header[Request], str]],
    config: BenchmarkConfig,
) -> ScenarioResult:
    """Run a single headers scenario."""
    app, backend = create_app(headers)

    start_time = time.perf_counter()
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(backend())
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )
        )
        latencies, successful, throttled = await _run_concurrent(
            client, config.num_requests, config.concurrency
        )

    end_time = time.perf_counter()
    return ScenarioResult(
        name=name,
        total_requests=config.num_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


NO_HEADERS: typing.Optional[Headers] = None

DEFAULT_ALWAYS = DEFAULT_HEADERS_ALWAYS

DEFAULT_THROTTLED_ONLY = DEFAULT_HEADERS_THROTTLED

CUSTOM_HEADERS = Headers(
    {
        "X-RateLimit-Limit": Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "X-Custom-Info": Header("traffik-bench", when="always"),
    }
)

MANY_HEADERS = Headers(
    {
        "X-RateLimit-Limit": Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "Retry-After": Header.RESET_SECONDS(when="always"),
        "X-RateLimit-Policy": Header(
            lambda conn, stat, ctx: f"{stat.rate.limit};w={stat.rate.expire}",
            when="always",
        ),
        "X-Custom-1": Header("bench-value-1", when="always"),
        "X-Custom-2": Header("bench-value-2", when="always"),
        "X-Custom-3": Header("bench-value-3", when="always"),
        "X-Throttled-At": Header.RESET_SECONDS(when="throttled"),
    }
)

SCENARIOS: typing.Dict[str, tuple] = {
    "no-headers": ("No Headers (baseline)", NO_HEADERS),
    "default-always": ("Default Headers (always)", DEFAULT_ALWAYS),
    "default-throttled": ("Default Headers (throttled only)", DEFAULT_THROTTLED_ONLY),
    "custom-headers": ("3 Custom Headers", CUSTOM_HEADERS),
    "many-headers": ("8 Headers (4 dynamic resolvers)", MANY_HEADERS),
}


async def run_benchmark(
    config: BenchmarkConfig,
) -> typing.Dict[str, typing.List[ScenarioResult]]:
    results: typing.Dict[str, typing.List[ScenarioResult]] = {}

    for key in config.scenarios:
        if key not in SCENARIOS:
            print(f"Warning: Unknown scenario '{key}', skipping...")
            continue

        name, headers = SCENARIOS[key]
        print(f"Scenario: {name}...")

        scenario_results = []
        for i in range(config.iterations):
            result = await run_scenario(name, headers, config)
            scenario_results.append(result)
            if i < config.iterations - 1:
                await asyncio.sleep(0.3)

        results[name] = scenario_results
        await asyncio.sleep(0.5)

    return results


def aggregate_results(results: typing.List[ScenarioResult]) -> ScenarioResult:
    return ScenarioResult(
        name=results[0].name,
        total_requests=sum(r.total_requests for r in results),
        successful_requests=sum(r.successful_requests for r in results),
        throttled_requests=sum(r.throttled_requests for r in results),
        total_time=sum(r.total_time for r in results),
        latencies=[lat for r in results for lat in r.latencies],
    )


def print_results(results: typing.Dict[str, typing.List[ScenarioResult]]) -> None:
    print("\n" + "=" * 88)
    print("HEADERS OVERHEAD BENCHMARK RESULTS")
    print("=" * 88)
    print(
        f"\n{'Scenario':<38} {'req/s':<12} {'P50 (ms)':<12} {'P95 (ms)':<12} {'P99 (ms)'}"
    )
    print("-" * 88)

    baseline_rps: typing.Optional[float] = None
    for name, scenario_results in results.items():
        agg = aggregate_results(scenario_results)
        if baseline_rps is None:
            baseline_rps = agg.requests_per_second

        overhead = ""
        if baseline_rps and baseline_rps > 0:
            diff = ((agg.requests_per_second - baseline_rps) / baseline_rps) * 100
            overhead = f"  ({diff:+.1f}%)"

        print(
            f"{name:<38} {agg.requests_per_second:<12.1f}"
            f"{agg.p50_latency:<12.2f} {agg.p95_latency:<12.2f} {agg.p99_latency:<12.2f}"
            f"{overhead}"
        )

    print("\n" + "=" * 88 + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Traffik response headers overhead",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available scenarios: {', '.join(SCENARIOS.keys())}",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default=",".join(SCENARIOS.keys()),
        help="Comma-separated list of scenarios to run (default: all)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per scenario (default: 3)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of concurrent requests per batch (default: 50)",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=500,
        help="Total requests per scenario iteration (default: 500)",
    )

    args = parser.parse_args()
    config = BenchmarkConfig(
        scenarios=args.scenarios.split(","),
        iterations=args.iterations,
        concurrency=args.concurrency,
        num_requests=args.num_requests,
    )

    print("Starting headers overhead benchmarks...")
    print(f"Concurrency: {config.concurrency}, Requests/run: {config.num_requests}")
    print(f"Iterations: {config.iterations}")
    print(f"Scenarios: {', '.join(config.scenarios)}\n")

    results = await run_benchmark(config)
    print_results(results)
    print("Benchmark complete!")


if __name__ == "__main__":
    asyncio.run(main())
