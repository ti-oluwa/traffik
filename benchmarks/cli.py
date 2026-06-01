import asyncio
import functools
import sys
import typing

import click
from typing_extensions import ParamSpec, TypeVar

from benchmarks.base import BackendKind, BenchmarkConfig, StrategyKind
from benchmarks.bench.http import run_scenarios as run_http_scenarios
from benchmarks.bench.middleware import run_scenarios as run_middleware_scenarios

if sys.platform not in ("win32", "cygwin"):
    from benchmarks.bench.multiprocess import (
        run_scenarios as run_multiprocess_scenarios,
    )
    from benchmarks.scenarios.multiprocess import SCENARIOS as MULTIPROCESS_SCENARIOS
else:
    run_multiprocess_scenarios = None
    MULTIPROCESS_SCENARIOS = {}
from benchmarks.bench.websocket import run_scenarios as run_websocket_scenarios
from benchmarks.output._json import print_json
from benchmarks.output.table import print_results_table
from benchmarks.scenarios.http import SCENARIOS as HTTP_SCENARIOS
from benchmarks.scenarios.middleware import SCENARIOS as MIDDLEWARE_SCENARIOS
from benchmarks.scenarios.websocket import SCENARIOS as WEBSOCKET_SCENARIOS

P = ParamSpec("P")
R = TypeVar("R")


def options(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
    """Common click options for all benchmark commands."""

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


@click.group()
def cli() -> None:
    """
    Traffik benchmark suite.

    Run HTTP, middleware, WebSocket, or multiprocess benchmarks
    against any supported backend and strategy combination.
    """
    pass


@cli.command("http")
@options
def http_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark HTTP throttles using Depends-based injection.

    Available scenarios: below_limit, at_limit, over_limit, concurrent,
    hot_key, many_keys, window_boundary, sustained, error_recovery.
    """
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
    )

    # Resolve scenario keys
    if scenarios == "all":
        scenario_keys = list(HTTP_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    # Run scenarios
    results = asyncio.run(run_http_scenarios(config, scenario_keys, warmup))

    # Output
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="HTTP Benchmark Results")


@cli.command("middleware")
@options
def middleware_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark middleware-mounted throttles.

    Available scenarios: below_limit, at_limit, over_limit, concurrent,
    hot_key, many_keys, window_boundary, sustained, error_recovery, selective.
    """
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
    )

    # Resolve scenario keys
    if scenarios == "all":
        scenario_keys = list(MIDDLEWARE_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    # Run scenarios
    results = asyncio.run(run_middleware_scenarios(config, scenario_keys, warmup))

    # Output
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="Middleware Benchmark Results")


@cli.command("websocket")
@options
def websocket_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark WebSocket throttles.

    Available scenarios: below_limit, over_limit, burst, concurrent, window_boundary.
    """
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
    )

    # Resolve scenario keys
    if scenarios == "all":
        scenario_keys = list(WEBSOCKET_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    # Run scenarios
    results = asyncio.run(run_websocket_scenarios(config, scenario_keys, warmup))

    # Output
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="WebSocket Benchmark Results")


@cli.command("multiprocess")
@options
def multiprocess_command(
    backend,
    strategy,
    iterations,
    warmup,
    concurrency,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
) -> None:
    """
    Benchmark MultiProcessInMemoryBackend scenarios (POSIX only).

    Available scenarios: below_limit, at_limit, over_limit, concurrent,
    hot_key, many_keys, window_boundary, sustained, error_recovery,
    shared_memory, key_eviction.
    """
    if sys.platform == "win32" or sys.platform == "cygwin":
        click.echo("ERROR: MultiProcess benchmarks require a POSIX system.", err=True)
        sys.exit(1)

    config = BenchmarkConfig(
        backend_kind="multiprocess",
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        concurrency=concurrency,
        output_format=output,
    )
    if run_multiprocess_scenarios is None:
        click.echo("ERROR: MultiProcess benchmarks require a POSIX system.", err=True)
        sys.exit(1)
        return

    # Resolve scenario keys
    if scenarios == "all":
        scenario_keys = list(MULTIPROCESS_SCENARIOS.keys())
    else:
        scenario_keys = [s.strip() for s in scenarios.split(",")]

    # Run scenarios
    results = asyncio.run(run_multiprocess_scenarios(config, scenario_keys, warmup))

    # Output
    if output == "json":
        meta = {
            "backend": "multiprocess",
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
        }
        print_json(results, meta)
    else:
        print_results_table(results, title="MultiProcess Benchmark Results")
