"""
ThrottleRule and registry overhead benchmarks for Traffik.

Measures the throughput and latency impact of `ThrottleRule` / `BypassThrottleRule`
registry evaluation across different rule configurations: no rules, single exact-path
rule, wildcard rules, bypass rules, and many mixed rules.

Usage:
    python benchmarks/rules.py --help
    python benchmarks/rules.py --scenarios no-rules,single-rule,many-rules
    python benchmarks/rules.py --concurrency 100 --num-requests 1000 --iterations 5
"""

import argparse
import asyncio
import contextlib
import re
import statistics
import time
import typing
from dataclasses import dataclass, field

import httpx
from base import custom_identifier  # type: ignore[import]
from fastapi import Depends, FastAPI

from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import BypassThrottleRule, ThrottleRegistry, ThrottleRule
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
            "no-rules",
            "single-rule",
            "wildcard-single",
            "wildcard-deep",
            "bypass-rule",
            "many-rules",
            "regex-rule",
        ]
    )
    iterations: int = 3
    concurrency: int = 50
    num_requests: int = 500


# All tests hit /api/v1/test. Rules are designed so throttling still applies
# for that path — the overhead measured is purely registry evaluation cost.
BENCH_PATH = "/api/v1/test"


def create_app(
    rules: typing.Optional[typing.List[ThrottleRule[typing.Any]]],
    limit: int = 1000,
    window: int = 60,
):
    """Create a FastAPI app with a specific registry configuration."""
    backend = InMemoryBackend(
        namespace="bench:rules",
        identifier=custom_identifier,
        persistent=False,
        number_of_shards=5,
    )
    registry = ThrottleRegistry()
    throttle = HTTPThrottle(
        uid="bench_rules",
        rate=f"{limit}/{window}s",
        backend=backend,  # type: ignore[arg-type]
        strategy=FixedWindowStrategy(),
        registry=registry,
    )
    if rules:
        registry.add_rules("bench_rules", *rules)

    app = FastAPI()

    @app.get(BENCH_PATH, dependencies=[Depends(throttle)])
    async def test_endpoint():
        return {"status": "ok"}

    return app, backend


async def _run_concurrent(
    client: httpx.AsyncClient,
    num_requests: int,
    concurrency: int,
    path: str = BENCH_PATH,
) -> tuple:
    """Run concurrent HTTP requests in batches and collect results."""
    latencies: typing.List[float] = []
    successful = 0
    throttled = 0

    async def make_request():
        req_start = time.perf_counter()
        response = await client.get(path)
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
    rules: typing.Optional[typing.List[ThrottleRule[typing.Any]]],
    config: BenchmarkConfig,
) -> ScenarioResult:
    """Run a single rules scenario."""
    app, backend = create_app(rules)

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


NO_RULES = None

# Exact path match — fastest rule type
SINGLE_RULE = [ThrottleRule(path="/api/v1/test", methods=["GET"])]

# * matches a single non-slash segment: /api/<segment>/test
WILDCARD_SINGLE = [ThrottleRule(path="/api/*/test", methods=["GET"])]

# ** matches any number of segments: /api/<anything>
WILDCARD_DEEP = [ThrottleRule(path="/api/**", methods=["GET"])]

# BypassThrottleRule for an unrelated path; our bench path still gets throttled
BYPASS_RULE = [
    BypassThrottleRule(path="/health"),
    BypassThrottleRule(path="/metrics"),
    ThrottleRule(path="/api/v1/test", methods=["GET"]),
]

# 10 mixed rules, similar to a realistic production registry
MANY_RULES = [
    BypassThrottleRule(path="/health"),
    BypassThrottleRule(path="/metrics"),
    BypassThrottleRule(path="/readyz"),
    ThrottleRule(path="/api/v1/users", methods=["GET", "POST"]),
    ThrottleRule(path="/api/v1/items", methods=["GET", "POST"]),
    ThrottleRule(path="/api/v1/orders", methods=["GET", "POST", "DELETE"]),
    ThrottleRule(path="/api/v1/search", methods=["GET"]),
    ThrottleRule(path="/api/v1/test", methods=["GET"]),
    ThrottleRule(path="/api/*/export", methods=["GET"]),
    ThrottleRule(path="/api/**"),
]

# Compiled regex — the most expressive but potentially slower matching
REGEX_RULE = [ThrottleRule(path=re.compile(r"^/api/v\d+/.*$"), methods=["GET"])]


SCENARIOS: typing.Dict[
    str, typing.Tuple[str, typing.Optional[typing.List[ThrottleRule[typing.Any]]]]
] = {
    "no-rules": ("No Rules (baseline)", NO_RULES),
    "single-rule": ("Single ThrottleRule (exact path)", SINGLE_RULE),
    "wildcard-single": ("ThrottleRule (* single segment)", WILDCARD_SINGLE),
    "wildcard-deep": ("ThrottleRule (** deep wildcard)", WILDCARD_DEEP),
    "bypass-rule": ("2x BypassThrottleRule + ThrottleRule", BYPASS_RULE),
    "many-rules": ("10 Mixed Rules (realistic registry)", MANY_RULES),
    "regex-rule": ("ThrottleRule (compiled re.Pattern)", REGEX_RULE),
}


async def run_benchmark(
    config: BenchmarkConfig,
) -> typing.Dict[str, typing.List[ScenarioResult]]:
    results: typing.Dict[str, typing.List[ScenarioResult]] = {}

    for key in config.scenarios:
        if key not in SCENARIOS:
            print(f"Warning: Unknown scenario '{key}', skipping...")
            continue

        name, rules = SCENARIOS[key]
        print(f"Scenario: {name}...")

        scenario_results = []
        for i in range(config.iterations):
            result = await run_scenario(name, rules, config)
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
    print("\n" + "=" * 92)
    print("THROTTLE RULES REGISTRY OVERHEAD BENCHMARK RESULTS")
    print("=" * 92)
    print(
        f"\n{'Scenario':<42} {'req/s':<12} {'P50 (ms)':<12} {'P95 (ms)':<12} {'P99 (ms)'}"
    )
    print("-" * 92)

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
            f"{name:<42} {agg.requests_per_second:<12.1f}"
            f"{agg.p50_latency:<12.2f} {agg.p95_latency:<12.2f} {agg.p99_latency:<12.2f}"
            f"{overhead}"
        )

    print("\n" + "=" * 92 + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Traffik ThrottleRule registry overhead",
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

    print("Starting ThrottleRule registry overhead benchmarks...")
    print(f"Concurrency: {config.concurrency}, Requests/run: {config.num_requests}")
    print(f"Iterations: {config.iterations}")
    print(f"Scenarios: {', '.join(config.scenarios)}\n")

    results = await run_benchmark(config)
    print_results(results)
    print("Benchmark complete!")


if __name__ == "__main__":
    asyncio.run(main())
