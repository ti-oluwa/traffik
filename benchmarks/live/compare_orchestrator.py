"""
Runs the `compare` command: for each selected scenario, spawn the traffik
app and the SlowAPI app in turn - same rate (converted via
`benchmarks.rates`), same backend, same identity rule, same worker count,
same endpoint-variant path, same warmup/timed iteration counts - and pair
up their `AggregatedResult`s into a `CompareResult`.

Deliberately sequential (traffik server up/down, then SlowAPI server
up/down) rather than running both concurrently: running them
side-by-side on the same machine at the same time would have each
app's traffic competing for the same CPU cores and, for external
backends, the same Redis/Memcached connection, which would make
whichever one happened to get scheduled first look artificially faster.
One at a time is slower to run but keeps the two measurements
independent of each other.
"""

import sys
import typing

from benchmarks.live import client as live_client
from benchmarks.live.orchestrators import (
    build_environment_variables,
    warn_unshared_state,
)
from benchmarks.live.runners import run_http_like_scenario
from benchmarks.live.server import ServerStartupError, start_server
from benchmarks.scenarios import HTTP_SCENARIOS, HttpScenario
from benchmarks.types import (
    AggregatedResult,
    BenchmarkConfig,
    CompareResult,
    ScenarioResult,
)

TRAFFIK_APP_PATH = "benchmarks.apps.http:app"
SLOWAPI_APP_PATH = "benchmarks.apps.slowapi_http:app"

UNSUPPORTED_COMPARE_BACKENDS = {"multiprocess"}


async def run_one_side(
    app_path: str,
    env: dict[str, str],
    scenario: HttpScenario,
    config: BenchmarkConfig,
    path: str,
    warmup_iterations: int,
    label: str,
) -> typing.Optional[list[ScenarioResult]]:
    """Spawn one app, run warmup + timed iterations against it, tear it down."""
    try:
        server = await start_server(app_path, env=env, workers=config.workers)
    except ServerStartupError as exc:
        print(f"ERROR: Could not start {label} server: {exc}", file=sys.stderr)
        return None

    results: list[ScenarioResult] = []
    try:
        async with live_client.make_http_client(
            server.base_url, concurrency=config.concurrency
        ) as http_client:
            for _ in range(warmup_iterations):
                try:
                    await run_http_like_scenario(
                        scenario, config, http_client, iteration=0, path=path
                    )
                    await server.reset(http_client)
                except Exception as exc:
                    print(f"WARN: {label} warmup failed: {exc}", file=sys.stderr)

            for i in range(1, config.iterations + 1):
                try:
                    result = await run_http_like_scenario(
                        scenario, config, http_client, iteration=i, path=path
                    )
                    results.append(result)
                    await server.reset(http_client)
                except Exception as exc:
                    print(f"WARN: {label} iteration {i} failed: {exc}", file=sys.stderr)
    finally:
        await server.stop()

    return results


async def run_compare_scenarios(
    config: BenchmarkConfig,
    scenario_keys: list[str],
    warmup_iterations: int,
    endpoint_variant: typing.Literal["async", "sync"] = "async",
) -> list[CompareResult]:
    """
    Run each selected `HTTP_SCENARIOS` entry against both the traffik app
    and the SlowAPI app, in turn, and pair up their results.

    Strategy is always forced to `fixed_window` on the traffik side
    (SlowAPI's `strategy="fixed-window"` is likewise hardcoded in
    `benchmarks.apps.slowapi_http`) - `fixed_window` is the only algorithm
    both sides can run identically, so honoring `--strategy` here would
    silently invalidate the "identical algorithm" comparison.

    :param config: Global benchmark configuration. `config.backend_kind`
        must not be `"multiprocess"` (see `benchmarks.apps.slowapi_http`'s
        docstring for why there's no fair comparison for it).
    :param scenario_keys: Short scenario names to run, drawn from `HTTP_SCENARIOS`.
    :param warmup_iterations: Warmup runs to discard before timing, per side.
    :param endpoint_variant: `"async"` hits `/test` (async def) on both
        apps; `"sync"` hits `/test-sync` (plain def) on both.
    :return: One `CompareResult` per successfully-run scenario.
    :raises ValueError: If `config.backend_kind` has no SlowAPI-comparable
        storage.
    """
    if config.backend_kind in UNSUPPORTED_COMPARE_BACKENDS:
        raise ValueError(
            f"`compare` does not support --backend {config.backend_kind}: "
            "see benchmarks/apps/slowapi_http.py's docstring for why there "
            "is no fair, identical-backend comparison for it. Use aioredis "
            "or coredis (same Redis on both sides) if you want a "
            "multi-worker-safe comparison instead."
        )

    warn_unshared_state(config)
    path = "/test" if endpoint_variant == "async" else "/test-sync"
    results: list[CompareResult] = []

    for scenario_key in scenario_keys:
        if scenario_key not in HTTP_SCENARIOS:
            print(f"ERROR: Unknown scenario: {scenario_key}", file=sys.stderr)
            continue

        scenario = HTTP_SCENARIOS[scenario_key]
        env = build_environment_variables(
            config,
            rate=scenario.rate,
            uid=f"compare_{scenario_key}",
            on_error=scenario.on_error,
        )
        # Always fixed_window: see this function's docstring.
        env["BENCH_STRATEGY"] = "fixed_window"

        print(f"Running {scenario_key} against traffik...", file=sys.stderr)
        traffik_results = await run_one_side(
            TRAFFIK_APP_PATH, env, scenario, config, path, warmup_iterations, "traffik"
        )

        print(f"Running {scenario_key} against SlowAPI...", file=sys.stderr)
        slowapi_results = await run_one_side(
            SLOWAPI_APP_PATH, env, scenario, config, path, warmup_iterations, "SlowAPI"
        )

        if not traffik_results or not slowapi_results:
            print(
                f"WARN: Skipping {scenario_key} - one or both sides produced "
                "no results.",
                file=sys.stderr,
            )
            continue

        results.append(
            CompareResult(
                scenario_key=scenario_key,
                scenario_name=scenario.name,
                traffik=AggregatedResult(
                    scenario_name=scenario.name,
                    backend_kind=config.backend_kind,
                    strategy_kind="fixed_window",
                    iterations=len(traffik_results),
                    results=traffik_results,
                ),
                slowapi=AggregatedResult(
                    scenario_name=scenario.name,
                    backend_kind=config.backend_kind,
                    strategy_kind="fixed_window",
                    iterations=len(slowapi_results),
                    results=slowapi_results,
                ),
            )
        )

    return results
