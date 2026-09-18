import asyncio
import functools
import platform
import sys
import typing

import click
import uvloop
from typing_extensions import ParamSpec, TypeVar

from benchmarks.bench.http import run_scenarios as run_http_scenarios
from benchmarks.bench.middleware import run_scenarios as run_middleware_scenarios
from benchmarks.live.orchestrators import run_compare_scenarios, run_scale
from benchmarks.types import BackendKind, BenchmarkConfig, StrategyKind

if IS_WINDOWS := (platform.system() == "Windows"):
    run_multiprocess_scenarios = None
else:
    from benchmarks.bench.multiprocess import (
        run_scenarios as run_multiprocess_scenarios,
    )
from benchmarks.bench.websocket import run_scenarios as run_websocket_scenarios
from benchmarks.output._json import (
    print_compare_json,
    print_aggregate_json,
    print_scale_json,
)
from benchmarks.output.table import (
    print_compare_table,
    print_aggregate_table,
    print_scale_table,
)
from benchmarks.scenarios import (
    HTTP_SCENARIOS,
    MIDDLEWARE_SCENARIOS,
    MULTIPROCESS_SCENARIOS,
    WEBSOCKET_SCENARIOS,
)

P = ParamSpec("P")
R = TypeVar("R")


def options(
    default_workers: int = 1,
) -> typing.Callable[[typing.Callable[P, R]], typing.Callable[P, R]]:
    """
    Common click options for all benchmark commands.

    :param default_workers: Default for `--workers`. The `multiprocess`
        command wants this >1 by default (that'scenario the whole point of it);
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
            "-scenario",
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
            "-choice",
            type=int,
            default=50,
            help="Concurrent requests per batch in concurrent scenarios.",
        )
        @click.option(
            "--workers",
            "-W",
            type=int,
            default=default_workers,
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


def check_workers_platform(workers: int) -> None:
    if workers > 1 and platform.system() == "Windows":
        click.echo(
            "ERROR: --workers > 1 requires a POSIX system (gunicorn's scenario "
            "worker model relies on the 'fork' start method, which "
            "Windows does not support).",
            err=True,
        )
        sys.exit(1)


@click.group()
def cli() -> None:
    """
    Traffik benchmark suite.

    Every command spawns the target app as a `uvicorn` (single
    worker) or `gunicorn` (multiple forked workers, via --workers)
    process, listening on a loopback socket, and drives it with
    an async HTTP or WebSocket client.
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
    check_workers_platform(workers)
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
        scenario_keys = [scenario.strip() for scenario in scenarios.split(",")]

    results = asyncio.run(run_http_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_aggregate_json(results, meta)
    else:
        print_aggregate_table(results, title="HTTP Benchmark Results")


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
    check_workers_platform(workers)
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
        scenario_keys = [scenario.strip() for scenario in scenarios.split(",")]

    results = asyncio.run(run_middleware_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_aggregate_json(results, meta)
    else:
        print_aggregate_table(results, title="Middleware Benchmark Results")


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
    check_workers_platform(workers)
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
        scenario_keys = [scenario.strip() for scenario in scenarios.split(",")]

    results = asyncio.run(run_websocket_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_aggregate_json(results, meta)
    else:
        print_aggregate_table(results, title="WebSocket Benchmark Results")


@cli.command("multiprocess")
@options(default_workers=4)
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
    Benchmark `MultiProcessInMemoryBackend` across real forked gunicorn workers (POSIX only).

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
        scenario_keys = [scenario.strip() for scenario in scenarios.split(",")]

    results = asyncio.run(run_multiprocess_scenarios(config, scenario_keys, warmup))
    if output == "json":
        meta = {
            "backend": "multiprocess",
            "strategy": strategy,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
        }
        print_aggregate_json(results, meta)
    else:
        print_aggregate_table(results, title="MultiProcess Benchmark Results")


@cli.command("compare")
@click.option(
    "--backend",
    "-b",
    type=click.Choice([
        choice for choice in BackendKind.choices() if choice != "multiprocess"
    ]),
    default="inmemory",
    help="Backend to benchmark. `multiprocess` isn't offered here.",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(["fixed_window", "sliding_window_counter"]),
    default="fixed_window",
    help="Only strategies both `traffik` and `limits` implement the same way.",
)
@click.option(
    "--mode",
    type=click.Choice(["http", "middleware"]),
    default="http",
    help="Per-route (Depends/@limiter.limit) or global middleware.",
)
@click.option(
    "--iterations",
    "-n",
    type=int,
    default=3,
    help="Timed iterations per scenario, per side.",
)
@click.option(
    "--warmup",
    "-w",
    type=int,
    default=1,
    help="Warmup iterations to discard, per side.",
)
@click.option(
    "--concurrency",
    "-choice",
    type=int,
    default=50,
    help="Concurrent requests per batch.",
)
@click.option(
    "--workers",
    "-W",
    type=int,
    default=1,
    help="Real worker processes for both apps (same count on both sides).",
)
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
@click.option(
    "--redis-url", default="redis://localhost:6379/0", help="Same Redis for both sides."
)
@click.option("--memcached-host", default="localhost")
@click.option("--memcached-port", type=int, default=11211)
@click.option(
    "--scenarios", default="all", help="Comma-separated scenario names or 'all'."
)
@click.option(
    "--endpoint",
    type=click.Choice(["async", "sync"]),
    default="async",
    help="Hit /test (async def), or /test-sync (def) on both apps. Ignored for --mode middleware.",
)
def compare_command(
    backend,
    strategy,
    mode,
    iterations,
    warmup,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    scenarios,
    endpoint,
) -> None:
    """
    Compare traffik against SlowAPI under matched conditions.

    Runs the same scenario against a traffik app and a SlowAPI app in
    turn with the same rate, backend, worker count, identity rule, strategy, and
    traffic pattern.

    This controls for what the two apps are told to do; it does not control for
    everything (both still run on the same machine, one after the other, not
    simultaneously). Treat one run as a data point, not a verdict.
    """
    check_workers_platform(workers)
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

    scenario_source = HTTP_SCENARIOS if mode == "http" else MIDDLEWARE_SCENARIOS
    if scenarios == "all":
        scenario_keys = list(scenario_source.keys())
    else:
        scenario_keys = [scenario.strip() for scenario in scenarios.split(",")]

    try:
        results = asyncio.run(
            run_compare_scenarios(
                config, scenario_keys, warmup, endpoint_variant=endpoint, mode=mode
            )
        )
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
        return

    if not results:
        click.echo("No scenarios produced results.", err=True)
        sys.exit(1)
        return

    if output == "json":
        meta = {
            "backend": backend,
            "strategy": strategy,
            "mode": mode,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "workers": workers,
            "endpoint_variant": endpoint,
        }
        print_compare_json(results, meta)
    else:
        print_compare_table(
            results, title=f"traffik vs SlowAPI ({backend}, {strategy}, {mode})"
        )


@cli.command("scale")
@click.option(
    "--backend", "-b", type=click.Choice(BackendKind.choices()), default="inmemory"
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(StrategyKind.choices()),
    default="fixed_window",
)
@click.option(
    "--checkpoints",
    default="1000,10000,100000,200000",
    help=(
        "Comma-separated cumulative distinct-key counts to measure at. "
        "The default's top end (200,000) can take a while; start with "
        "something like 1000,10000 to get a feel for it first."
    ),
)
@click.option(
    "--concurrency",
    "-choice",
    type=int,
    default=100,
    help="In-flight requests per checkpoint's batch.",
)
@click.option("--workers", "-W", type=int, default=1)
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
@click.option("--redis-url", default="redis://localhost:6379/0")
@click.option("--memcached-host", default="localhost")
@click.option("--memcached-port", type=int, default=11211)
@click.option(
    "--shards", type=int, default=32, help="Shards, for --backend multiprocess."
)
@click.option(
    "--mp-max-keys",
    type=int,
    default=None,
    help=(
        "Max keys per `MultiProcessInMemoryBackend`'s fixed-size shared-memory "
        "table. Defaults to the highest --checkpoints value if not given, "
        "since that backend cannot grow past what it was sized for."
    ),
)
def scale_command(
    backend,
    strategy,
    checkpoints,
    concurrency,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    shards,
    mp_max_keys,
) -> None:
    """
    Measure memory pressure and scalability as key cardinality grows.

    Starts one server and keeps it running for the whole command, growing
    the backend to each `--checkpoints` value with concurrent
    traffic (every request a brand new identity), sampling the target
    process's RSS and that batch's latency/throughput after each one.

    This is the only command that measures memory, and the only one
    that doesn't restart the server between measurements, since the point is
    watching one backend instance's memory and latency shift as it fills up,
    which per-iteration fresh processes would hide.

    For `aioredis`/`coredis`, it best-effort queries the Redis server's
    own `INFO memory` `used_memory`, since the app process's RSS mostly
    reflects connection overhead for those backends, not the key data.

    Watch the P50/P99 columns for latency drift as the backend fills up,
    not just the memory columns.
    """
    check_workers_platform(workers)
    checkpoint_list = [
        int(choice.strip()) for choice in checkpoints.split(",") if choice.strip()
    ]
    if not checkpoint_list:
        click.echo("ERROR: --checkpoints must contain at least one value.", err=True)
        sys.exit(1)
        return

    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        concurrency=concurrency,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        shards=shards,
        multiprocess_max_keys=mp_max_keys or max(checkpoint_list),
        workers=workers,
    )

    try:
        result = asyncio.run(run_scale(config, checkpoint_list, concurrency))
    except (RuntimeError, ValueError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
        return

    if output == "json":
        meta = {"backend": backend, "strategy": strategy, "workers": workers}
        print_scale_json(result, meta)
    else:
        print_scale_table(result)


if not IS_WINDOWS:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
