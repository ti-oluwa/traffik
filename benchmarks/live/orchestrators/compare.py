"""
Runs the `compare` command. For each selected scenario, it spawns the traffik
app and the SlowAPI app in turn with same rate, backend, identity rule,
worker count, strategy, and traffic pattern, alternating which goes first from
one scenario to the next so neither owns the first slot. It then pairs their
`AggregatedResult`s into a `CompareResult`.
"""

import sys
import typing

from benchmarks.live import client as live_client
from benchmarks.live.orchestrators.core import (
    build_environment_variables,
    warn_unshared_state,
)
from benchmarks.live.runners import run_http_like_scenario
from benchmarks.live.server import ServerStartupError, start_server
from benchmarks.scenarios import HTTP_SCENARIOS, MIDDLEWARE_SCENARIOS, HttpScenario
from benchmarks.types import (
    AggregatedResult,
    BenchmarkConfig,
    CompareResult,
    ScenarioResult,
)

TRAFFIK_APP_PATHS = {
    "http": "benchmarks.apps.traffik.http:app",
    "middleware": "benchmarks.apps.traffik.middleware:app",
}
SLOWAPI_APP_PATHS = {
    "http": "benchmarks.apps.slowapi.http:app",
    "middleware": "benchmarks.apps.slowapi.middleware:app",
}
SCENARIOS_BY_MODE = {"http": HTTP_SCENARIOS, "middleware": MIDDLEWARE_SCENARIOS}

# Strategies both traffik and `limits` (SlowAPI's engine) implement the same way.
SUPPORTED_STRATEGIES = {"fixed_window", "sliding_window_counter"}

UNSUPPORTED_COMPARE_BACKENDS = {"multiprocess"}


async def run_scenario(
    app_path: str,
    env: dict[str, str],
    scenario: HttpScenario,
    config: BenchmarkConfig,
    path: str,
    warmup_iterations: int,
    label: str,
) -> typing.Optional[list[ScenarioResult]]:
    """Spawn one app, run warmup and timed iterations against it, then tear it down."""
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
    mode: typing.Literal["http", "middleware"] = "http",
) -> list[CompareResult]:
    """
    Run each selected scenario against both the traffik app and the
    SlowAPI app, in turn, and pair up their results.

    :param config: Global benchmark configuration. `config.backend_kind`
        must not be `"multiprocess"`; `config.strategy_kind` must be in
        `SUPPORTED_STRATEGIES`.
    :param scenario_keys: Short scenario names, from `HTTP_SCENARIOS` (mode
        `"http"`) or `MIDDLEWARE_SCENARIOS` (mode `"middleware"`).
    :param warmup_iterations: Warmup runs to discard before timing, per side.
    :param endpoint_variant: `"async"` hits `/test`; `"sync"` hits
        `/test-sync`. Ignored for `mode="middleware"` (neither middleware
        app has a sync variant - the middleware itself doesn't touch the
        route handler).
    :param mode: `"http"` (per-route `Depends`/`@limiter.limit`) or
        `"middleware"` (global middleware, `/unthrottled` exempt).
    :return: One `CompareResult` per successfully-run scenario.
    :raises ValueError: If the backend or strategy has no SlowAPI-comparable
        equivalent.
    """
    if config.backend_kind in UNSUPPORTED_COMPARE_BACKENDS:
        raise ValueError(f"`compare` does not support --backend {config.backend_kind}")

    if config.strategy_kind not in SUPPORTED_STRATEGIES:
        raise ValueError(
            f"`compare` does not support --strategy {config.strategy_kind}: "
            f"supported are {sorted(SUPPORTED_STRATEGIES)} (as these are the only ones "
            "both traffik and `limits` implement the same way)."
        )

    warn_unshared_state(config)
    path = (
        "/test" if mode == "middleware" or endpoint_variant == "async" else "/test-sync"
    )
    scenarios = SCENARIOS_BY_MODE[mode]
    results: list[CompareResult] = []

    for index, scenario_key in enumerate(scenario_keys):
        if scenario_key not in scenarios:
            print(f"ERROR: Unknown scenario: {scenario_key}", file=sys.stderr)
            continue

        scenario = scenarios[scenario_key]
        env = build_environment_variables(
            config,
            rate=scenario.rate,
            uid=f"compare_{scenario_key}",
            on_error=scenario.on_error,
        )

        # Alternate which side runs first, scenario by scenario. A fixed order
        # would hand one side every systematic advantage of going first (or
        # last): cooler CPU, boost clocks, whatever else is running.
        sides = [
            ("traffik", TRAFFIK_APP_PATHS[mode]),
            ("SlowAPI", SLOWAPI_APP_PATHS[mode]),
        ]
        if index % 2:
            sides.reverse()

        scenario_results: dict[str, typing.Optional[list[ScenarioResult]]] = {}
        for label, app_path in sides:
            print(f"Running {scenario_key} against {label}...", file=sys.stderr)
            scenario_results[label] = await run_scenario(
                app_path=app_path,
                env=env,
                scenario=scenario,
                config=config,
                path=path,
                warmup_iterations=warmup_iterations,
                label=label,
            )
        traffik_results = scenario_results["traffik"]
        slowapi_results = scenario_results["SlowAPI"]

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
                    strategy_kind=config.strategy_kind,
                    iterations=len(traffik_results),
                    results=traffik_results,
                ),
                slowapi=AggregatedResult(
                    scenario_name=scenario.name,
                    backend_kind=config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    iterations=len(slowapi_results),
                    results=slowapi_results,
                ),
            )
        )

    return results
