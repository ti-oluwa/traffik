import platform
import typing

if platform.system() == "Windows":
    raise SystemExit(
        "Multiprocess inmemory backend benchmarks are not supported on Windows. "
        "This module requires the 'fork' multiprocessing start method."
    )

from benchmarks.base import AggregatedResult, BenchmarkConfig
from benchmarks.live.orchestrators import run_http_scenarios
from benchmarks.scenarios import MULTIPROCESS_SCENARIOS

# Reuses the plain HTTP (Depends-based) app. What makes this "multiprocess"
# is BENCH_BACKEND=multiprocess (forced below) plus real gunicorn workers
# forked from a master that already ran `MultiProcessInMemoryBackend.start()`
# at import time (see benchmarks.apps.config.backend_from_env).
app_path = "benchmarks.apps.http:app"


async def run_scenarios(
    config: BenchmarkConfig, scenario_keys: typing.List[str], warmup_iterations: int = 1
) -> typing.List[AggregatedResult]:
    """
    Run each selected scenario against real, forked gunicorn worker
    processes sharing one `MultiProcessInMemoryBackend`.

    :param config: Global benchmark configuration (backend is forced to
        `"multiprocess"` regardless of `config.backend_kind`).
    :param scenario_keys: List of scenario short names to run.
    :param warmup_iterations: Number of warmup runs to discard.
    :return: List of aggregated results, one per scenario.
    """
    return await run_http_scenarios(
        config,
        scenario_keys,
        warmup_iterations,
        MULTIPROCESS_SCENARIOS,
        app_path,
        forced_backend_kind="multiprocess",
    )
