"""Head-to-head comparison benchmark against SlowAPI."""

import asyncio
import os
import typing

from benchmarks.live.orchestrators import run_http_scenarios
from benchmarks.output._json import print_json
from benchmarks.output.table import print_comparison_table
from benchmarks.scenarios import HTTP_SCENARIOS
from benchmarks.types import AggregatedResult, BenchmarkConfig

SLOWAPI_APP_PATH = "benchmarks.apps.slowapi:app"


def _scenario_keys(value: str) -> list[str]:
    if value == "all":
        return list(HTTP_SCENARIOS)
    return [item.strip() for item in value.split(",") if item.strip()]


async def run_comparison(
    config: BenchmarkConfig,
    scenario_keys: list[str],
    warmup_iterations: int,
) -> tuple[list[AggregatedResult], list[AggregatedResult]]:
    """Run identical HTTP scenarios against Traffik and SlowAPI."""
    traffik_results = await run_http_scenarios(
        config,
        scenario_keys,
        warmup_iterations,
        HTTP_SCENARIOS,
        "benchmarks.apps.http:app",
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
        slowapi_config,
        scenario_keys,
        warmup_iterations,
        HTTP_SCENARIOS,
        SLOWAPI_APP_PATH,
    )
    return traffik_results, slowapi_results


def _index(results: list[AggregatedResult]) -> dict[str, AggregatedResult]:
    return {result.scenario_name: result for result in results}


def print_comparison(
    traffik_results: list[AggregatedResult],
    slowapi_results: list[AggregatedResult],
) -> None:
    """Print matched Traffik/SlowAPI results with Traffik as the baseline."""
    traffik = _index(traffik_results)
    slowapi = _index(slowapi_results)
    matched = [name for name in traffik if name in slowapi]

    if not matched:
        raise RuntimeError("No scenarios produced results for both implementations")

    baseline = traffik[matched[0]]
    others: list[AggregatedResult] = []
    for name in matched:
        result = slowapi[name]
        others.append(result)

    print("\nTraffik vs SlowAPI (SlowAPI delta is relative to Traffik)\n")
    for name in matched:
        left = traffik[name]
        right = slowapi[name]
        rps_delta = ((right.mean_rps - left.mean_rps) / left.mean_rps * 100) if left.mean_rps else 0
        p50_delta = ((right.p50_ms - left.p50_ms) / left.p50_ms * 100) if left.p50_ms else 0
        p95_delta = ((right.p95_ms - left.p95_ms) / left.p95_ms * 100) if left.p95_ms else 0
        print(
            f"{name}: Traffik={left.mean_rps:.1f} req/s, SlowAPI={right.mean_rps:.1f} req/s "
            f"(RPS {rps_delta:+.1f}%, P50 {p50_delta:+.1f}%, P95 {p95_delta:+.1f}%)"
        )


async def _run_and_print(
    config: BenchmarkConfig,
    scenario_keys: list[str],
    warmup_iterations: int,
) -> None:
    traffik_results, slowapi_results = await run_comparison(
        config, scenario_keys, warmup_iterations
    )

    if config.output_format == "json":
        print_json(
            traffik_results + slowapi_results,
            {
                "benchmark": "traffik_vs_slowapi",
                "traffik_backend": config.backend_kind,
                "traffik_strategy": config.strategy_kind,
                "slowapi": True,
                "workers": config.workers,
                "iterations": config.iterations,
                "warmup_iterations": warmup_iterations,
            },
        )
        return

    print_comparison(traffik_results, slowapi_results)


def run_from_cli(
    *,
    backend: str,
    strategy: str,
    iterations: int,
    warmup: int,
    concurrency: int,
    workers: int,
    output: str,
    scenarios: str,
) -> None:
    """Synchronous entry point used by the Click command."""
    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
        workers=workers,
    )
    os.environ["BENCH_SLOWAPI"] = "1"
    asyncio.run(_run_and_print(config, _scenario_keys(scenarios), warmup))
