import typing

from benchmarks.base import AggregatedResult, BenchmarkConfig
from benchmarks.live.orchestrators import run_websocket_scenarios
from benchmarks.scenarios import WEBSOCKET_SCENARIOS

app_path = "benchmarks.apps.websocket:app"


async def run_scenarios(
    config: BenchmarkConfig,
    scenario_keys: typing.List[str],
    warmup_iterations: int = 1,
) -> typing.List[AggregatedResult]:
    """
    Run each selected WebSocket scenario as a real `uvicorn`/`gunicorn`
    subprocess, driven by real WebSocket connections.

    :param config: Global benchmark configuration.
    :param scenario_keys: List of scenario short names to run.
    :param warmup_iterations: Number of warmup runs to discard.
    :return: List of aggregated results, one per scenario.
    """
    return await run_websocket_scenarios(
        config,
        scenario_keys,
        warmup_iterations,
        WEBSOCKET_SCENARIOS,
        app_path,
    )
