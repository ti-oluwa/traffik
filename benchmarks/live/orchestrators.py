"""
The loop every `benchmarks.bench.*` module runs: for each selected
scenario, spawn one real server process configured for that scenario,
run warmup iterations (discarded) and timed iterations (kept) against it,
resetting throttle state between every single iteration, then tear the
process down before moving to the next scenario.

One server process is reused across all iterations of the *same*
scenario, but a fresh process per scenario since each scenario
configures its own rate/uid/on_error via environment variables the app
module reads once, at import time.
"""

import sys
import typing
import uuid

from benchmarks.base import AggregatedResult, BenchmarkConfig, ScenarioResult
from benchmarks.live import client as live_client
from benchmarks.live.runners import run_http_like_scenario, run_websocket_scenario
from benchmarks.live.server import ServerStartupError, start_server
from benchmarks.scenarios import HttpScenario, WebSocketScenario


def _build_env(
    config: BenchmarkConfig,
    *,
    rate: str,
    uid: str,
    on_error: str,
    backend_kind: typing.Optional[str] = None,
) -> typing.Dict[str, str]:
    """Environment variables a `benchmarks.apps.*` module reads at import time."""
    run_id = uuid.uuid4().hex[:8]
    return {
        "BENCH_BACKEND": backend_kind or config.backend_kind,
        "BENCH_STRATEGY": config.strategy_kind,
        "BENCH_RATE": rate,
        "BENCH_UID": f"{uid}_{run_id}",
        "BENCH_ON_ERROR": on_error,
        "BENCH_NAMESPACE": f"bench{run_id}",
        "BENCH_REDIS_URL": config.redis_url,
        "BENCH_MEMCACHED_HOST": config.memcached_host,
        "BENCH_MEMCACHED_PORT": str(config.memcached_port),
        "BENCH_SHARDS": str(config.shards),
        "BENCH_MP_MAX_KEYS": str(config.multiprocess_max_keys),
    }


def _maybe_warn_unshared_state(config: BenchmarkConfig) -> None:
    if config.workers > 1 and config.backend_kind == "inmemory":
        print(
            "WARN: --workers > 1 with --backend inmemory: each forked worker "
            "gets its own copy of in-memory state (no sharing across "
            "processes), so throttling will look inconsistent across "
            "requests routed to different workers. Use --backend "
            "multiprocess (or an external backend like aioredis/coredis) "
            "to see real cross-worker throttling.",
            file=sys.stderr,
        )


async def run_http_scenarios(
    config: BenchmarkConfig,
    scenario_keys: typing.List[str],
    warmup_iterations: int,
    scenarios: typing.Dict[str, HttpScenario],
    app_path: str,
    *,
    forced_backend_kind: typing.Optional[str] = None,
) -> typing.List[AggregatedResult]:
    """
    Run each selected HTTP/middleware/multiprocess scenario as a real
    subprocess-backed benchmark.

    :param config: Global benchmark configuration.
    :param scenario_keys: Short scenario names to run.
    :param warmup_iterations: Warmup runs to discard before timing.
    :param scenarios: The scenario registry to resolve `scenario_keys` against.
    :param app_path: `module:app` path uvicorn/gunicorn will import.
    :param forced_backend_kind: If given, overrides `config.backend_kind`
        for the spawned server (the `multiprocess` command forces
        `"multiprocess"` regardless of what `--backend` was given).
    :return: One `AggregatedResult` per successfully-run scenario.
    """
    _maybe_warn_unshared_state(config)
    results: typing.List[AggregatedResult] = []

    for scenario_key in scenario_keys:
        if scenario_key not in scenarios:
            print(f"ERROR: Unknown scenario: {scenario_key}", file=sys.stderr)
            continue

        scenario = scenarios[scenario_key]
        env = _build_env(
            config,
            rate=scenario.rate,
            uid=f"bench_{scenario_key}",
            on_error=scenario.on_error,
            backend_kind=forced_backend_kind,
        )

        try:
            server = await start_server(app_path, env=env, workers=config.workers)
        except ServerStartupError as exc:
            print(
                f"ERROR: Could not start server for {scenario_key}: {exc}",
                file=sys.stderr,
            )
            continue

        scenario_results: typing.List[ScenarioResult] = []
        try:
            async with live_client.make_http_client(
                server.base_url, concurrency=config.concurrency
            ) as http_client:
                print(f"Running warmup for {scenario_key}...", file=sys.stderr)
                for _ in range(warmup_iterations):
                    try:
                        await run_http_like_scenario(
                            scenario, config, http_client, iteration=0
                        )
                        await server.reset(http_client)
                    except Exception as exc:  # noqa
                        print(
                            f"WARN: Warmup failed for {scenario_key}: {exc}",
                            file=sys.stderr,
                        )

                for i in range(1, config.iterations + 1):
                    print(
                        f"Running {scenario_key} (iteration {i}/{config.iterations})...",
                        file=sys.stderr,
                    )
                    try:
                        result = await run_http_like_scenario(
                            scenario, config, client=http_client, iteration=i
                        )
                        scenario_results.append(result)
                        await server.reset(http_client)
                    except Exception as exc:  # noqa
                        print(
                            f"WARN: Iteration {i} failed for {scenario_key}: {exc}",
                            file=sys.stderr,
                        )
        finally:
            await server.stop()

        if scenario_results:
            results.append(
                AggregatedResult(
                    scenario_name=scenario_results[0].scenario_name,
                    backend_kind=forced_backend_kind or config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    iterations=len(scenario_results),
                    results=scenario_results,
                )
            )
    return results


async def run_websocket_scenarios(
    config: BenchmarkConfig,
    scenario_keys: typing.List[str],
    warmup_iterations: int,
    scenarios: typing.Dict[str, WebSocketScenario],
    app_path: str,
) -> typing.List[AggregatedResult]:
    """
    Run each selected WebSocket scenario as a real subprocess-backed
    benchmark, driven by real WebSocket connections.

    :param config: Global benchmark configuration.
    :param scenario_keys: Short scenario names to run.
    :param warmup_iterations: Warmup runs to discard before timing.
    :param scenarios: The scenario registry to resolve `scenario_keys` against.
    :param app_path: `module:app` path uvicorn/gunicorn will import.
    :return: One `AggregatedResult` per successfully-run scenario.
    """
    _maybe_warn_unshared_state(config)
    results: typing.List[AggregatedResult] = []

    for scenario_key in scenario_keys:
        if scenario_key not in scenarios:
            print(f"ERROR: Unknown scenario: {scenario_key}", file=sys.stderr)
            continue

        scenario = scenarios[scenario_key]
        env = _build_env(
            config,
            rate=scenario.rate,
            uid=f"bench_ws_{scenario_key}",
            on_error="raise",
        )

        try:
            server = await start_server(app_path, env=env, workers=config.workers)
        except ServerStartupError as exc:
            print(
                f"ERROR: Could not start server for {scenario_key}: {exc}",
                file=sys.stderr,
            )
            continue

        ws_url = f"{server.ws_base_url}/ws"
        scenario_results: typing.List[ScenarioResult] = []
        try:
            async with live_client.make_http_client(server.base_url) as http_client:
                print(f"Running warmup for {scenario_key}...", file=sys.stderr)
                for _ in range(warmup_iterations):
                    try:
                        await run_websocket_scenario(
                            scenario,
                            config=config,
                            ws_url=ws_url,
                            iteration=0,
                        )
                        await server.reset(http_client)
                    except Exception as exc:  # noqa
                        print(
                            f"WARN: Warmup failed for {scenario_key}: {exc}",
                            file=sys.stderr,
                        )

                for i in range(1, config.iterations + 1):
                    print(
                        f"Running {scenario_key} (iteration {i}/{config.iterations})...",
                        file=sys.stderr,
                    )
                    try:
                        result = await run_websocket_scenario(
                            scenario,
                            config=config,
                            ws_url=ws_url,
                            iteration=i,
                        )
                        scenario_results.append(result)
                        await server.reset(http_client)
                    except Exception as exc:  # noqa
                        print(
                            f"WARN: Iteration {i} failed for {scenario_key}: {exc}",
                            file=sys.stderr,
                        )
        finally:
            await server.stop()

        if scenario_results:
            results.append(
                AggregatedResult(
                    scenario_name=scenario_results[0].scenario_name,
                    backend_kind=config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    iterations=len(scenario_results),
                    results=scenario_results,
                )
            )
    return results
