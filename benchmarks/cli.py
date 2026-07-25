import asyncio
import functools
import platform
import sys
import typing

import click
import uvloop
from typing_extensions import ParamSpec, TypeVar

from benchmarks.base import BackendKind, BenchmarkConfig, StrategyKind
from benchmarks.bench.http import run_scenarios as run_http_scenarios
from benchmarks.bench.middleware import run_scenarios as run_middleware_scenarios

if IS_WINDOWS := (platform.system() == "Windows"):
    from benchmarks.bench.multiprocess import (
        run_scenarios as run_multiprocess_scenarios,
    )
else:
    run_multiprocess_scenarios = None
from benchmarks.bench.websocket import run_scenarios as run_websocket_scenarios
from benchmarks.output._json import print_json
from benchmarks.output.table import print_results_table
from benchmarks.scenarios import (
    HTTP_SCENARIOS,
    MIDDLEWARE_SCENARIOS,
    MULTIPROCESS_SCENARIOS,
    WEBSOCKET_SCENARIOS,
)

P = ParamSpec("P")
R = TypeVar("R")


def options(
    workers_default: int = 1,
) -> typing.Callable[[typing.Callable[P, R]], typing.Callable[P, R]]:
    """
    Common click options for all benchmark commands.

    :param workers_default: Default for `--workers`. The `multiprocess`
        command wants this >1 by default (that's the whole point of it);
        the others default to a single real process.
    """

    def decorator(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
        @click.option(
            "--backend",
            "-b",
            type=click.Choice(BackendKind.choices()),
            default="inmemory",
            help="Backend to benchmark.",
        )
        @click.option(
            "--strategy",
            "-s",
            type=click.Choice(StrategyKind.choices()),
            default="fixed_window",
            help="Strategy to benchmark.",
        )
        @click.option(
            "--iterations",
            "-n",
            type=int,
            default=3,
            help="Number of timed iterations per scenario.",
        )
        @click.option(
            "--warmup",
            "-w",
            type=int,
            default=1,
            help="Number of warmup iterations to discard.",
        )
        @click.option(
            "--concurrency",
            "-c",
            type=int,
            default=50,
            help="Concurrent requests per batch in concurrent scenarios.",
        )
        @click.option(
            "--workers",
            "-W",
            type=int,
            default=workers_default,
            help=(
                "Real worker processes serving the benchmark app. 1 = a single "
                "uvicorn process. >1 = gunicorn with --preload, forking that "
                "many workers (POSIX only)."
            ),
        )
        @click.option(
            "--output",
            "-o",
            type=click.Choice(["table", "json"]),
            default="table",
            help="Output format.",
        )
        @click.option(
            "--redis-url",
            default="redis://localhost:6379/0",
            help="Redis connection URL.",
        )
        @click.option(
            "--memcached-host",
            default="localhost",
            help="Memcached host.",
        )
        @click.option(
            "--memcached-port",
            type=int,
            default=11211,
            help="Memcached port.",
        )
        @click.option(
            "--scenarios",
            default="all",
            help="Comma-separated scenario names or 'all'.",
        )
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> R:
            return func(*args, **kwargs)

        return wrapper

    return decorator


def _check_workers_platform(workers: int) -> None:
    if workers > 1 and platform.system() == "Windows":
        click.echo(
            "ERROR: --workers > 1 requires a POSIX system (gunicorn's "
            "worker model relies on the 'fork' start method, which "
            "Windows does not support).",
            err=True,
        )
        sys.exit(1)


@click.group()
def cli() -> None:
    """
    Traffik benchmark suite.

    Every command spawns the target app as a real `uvicorn` (single
    worker) or `gunicorn` (multiple forked workers, via --workers)
    process, listening on a real loopback socket, and drives it with a
    real async HTTP or WebSocket client - not an in-process ASGI call.
    """
    pass


@cli.command("http")
@options()
def http_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark HTTP throttles using Depends-based injection.

    Available scenarios: `below_limit`, `at_limit`, `over_limit`, `concurrent`,
    `hot_key`, `many_keys`, `window_boundary`, `sustained`, `error_recovery`.
    """
    _check_workers_platform(workers)
    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        workers=workers,
    )

    if scenarios == "all":
        scenario_keys = list(HTTP_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    results = asyncio.run(run_http_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="HTTP Benchmark Results")


@cli.command("middleware")
@options()
def middleware_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark middleware-mounted throttles.

    Available scenarios: `below_limit`, `at_limit`, `over_limit`, `concurrent`,
    `hot_key`, `many_keys`, `window_boundary`, `sustained`, `error_recovery`, `selective`.
    """
    _check_workers_platform(workers)
    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        workers=workers,
    )

    if scenarios == "all":
        scenario_keys = list(MIDDLEWARE_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    results = asyncio.run(run_middleware_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="Middleware Benchmark Results")


@cli.command("websocket")
@options()
def websocket_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark WebSocket throttles.

    Available scenarios: `below_limit`, `over_limit`, `burst`, `concurrent`, `window_boundary`.
    """
    _check_workers_platform(workers)
    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        workers=workers,
    )

    if scenarios == "all":
        scenario_keys = list(WEBSOCKET_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    results = asyncio.run(run_websocket_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="WebSocket Benchmark Results")


@cli.command("multiprocess")
@options(workers_default=4)
def multiprocess_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark MultiProcessInMemoryBackend across real forked gunicorn
    workers (POSIX only).

    Available scenarios: `below_limit`, `at_limit`, `over_limit`, `concurrent`,
    `hot_key`, `many_keys`, `window_boundary`, `sustained`, `error_recovery`,
    `shared_memory`, `key_eviction`.
    """
    if IS_WINDOWS or run_multiprocess_scenarios is None:
        click.echo("ERROR: MultiProcess benchmarks require a POSIX system.", err=True)
        sys.exit(1)
        return
    if workers < 2:
        click.echo(
            "WARN: --workers < 2 means gunicorn won't actually fork multiple "
            "workers, so this won't exercise cross-process state sharing.",
            err=True,
        )

    config = BenchmarkConfig(
        backend_kind="multiprocess",
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        workers=workers,
    )

    if scenarios == "all":
        scenario_keys = list(MULTIPROCESS_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    results = asyncio.run(run_multiprocess_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": "multiprocess",
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="MultiProcess Benchmark Results")


if not IS_WINDOWS:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
