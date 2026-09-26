"""Head-to-head comparison benchmark against SlowAPI."""

import asyncio

from benchmarks.live.orchestrators import run_http_scenarios
from benchmarks.output._json import print_json
from benchmarks.scenarios import HTTP_SCENARIOS
from benchmarks.types import AggregatedResult, BenchmarkConfig

SLOWAPI_APP_PATH = "benchmarks.apps.slowapi:app"
TRAFFIK_APP_PATH = "benchmarks.apps.http:app"


def scenario_keys(value: str) -> list[str]:
    """Resolve the CLI scenario selector."""
    if value == "all":
        return list(HTTP_SCENARIOS)
    return [item.strip() for item in value.split(",") if item.strip()]


async def run_comparison(
    config: BenchmarkConfig,
    selected_scenarios: list[str],
    warmup_iterations: int,
) -> tuple[list[AggregatedResult], list[AggregatedResult]]:
    """Run identical HTTP scenarios against Traffik and SlowAPI."""
    traffik_results = await run_http_scenarios(
        config, selected_scenarios, warmup_iterations, HTTP_SCENARIOS, TRAFFIK_APP_PATH
    )
    slowapi_config = BenchmarkConfig(
        backend_kind="slowapi",
        strategy_kind="fixed_window",
        iterations=config.iterations,
        warmup_iterations=config.warmup_iterations,
        concurrency=config.concurrency,
        output_format=config.output_format,
        workers=config.workers,
    )
    slowapi_results = await run_http_scenarios(
        slowapi_config, selected_scenarios, warmup_iterations, HTTP_SCENARIOS, SLOWAPI_APP_PATH
    )
    return traffik_results, slowapi_results


def _by_scenario(results: list[AggregatedResult]) -> dict[str, AggregatedResult]:
    return {result.scenario_name: result for result in results}


def print_comparison(traffik_results: list[AggregatedResult], slowapi_results: list[AggregatedResult]) -> None:
    """Print SlowAPI deltas relative to the corresponding Traffik result."""
    traffik = _by_scenario(traffik_results)
    slowapi = _by_scenario(slowapi_results)
    names = [name for name in traffik if name in slowapi]
    if not names:
        raise RuntimeError("No scenarios produced results for both implementations")

    print("\nTraffik vs SlowAPI (SlowAPI delta relative to Traffik)\n")
    for name in names:
        left = traffik[name]
        right = slowapi[name]
        rps_delta = (right.mean_rps - left.mean_rps) / left.mean_rps * 100 if left.mean_rps else 0
        p50_delta = (right.p50_ms - left.p50_ms) / left.p50_ms * 100 if left.p50_ms else 0
        p95_delta = (right.p95_ms - left.p95_ms) / left.p95_ms * 100 if left.p95_ms else 0
        print(
            f"{name}: Traffik={left.mean_rps:.1f} req/s, SlowAPI={right.mean_rps:.1f} req/s "
            f"(RPS {rps_delta:+.1f}%, P50 {p50_delta:+.1f}%, P95 {p95_delta:+.1f}%)"
        )


def print_json_comparison(
    traffik_results: list[AggregatedResult],
    slowapi_results: list[AggregatedResult],
    config: BenchmarkConfig,
    warmup_iterations: int,
) -> None:
    """Emit both implementations' results in machine-readable form."""
    print_json(
        traffik_results + slowapi_results,
        {
            "benchmark": "traffik_vs_slowapi",
            "traffik_backend": config.backend_kind,
            "traffik_strategy": config.strategy_kind,
            "workers": config.workers,
            "iterations": config.iterations,
            "warmup_iterations": warmup_iterations,
        },
    )
