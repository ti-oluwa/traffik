import asyncio
import functools
import pathlib
import platform
import sys
import typing

import click
import uvloop
from typing_extensions import ParamSpec, TypeVar

from benchmarks.bench.http import run_scenarios as run_http_scenarios
from benchmarks.bench.middleware import run_scenarios as run_middleware_scenarios
from benchmarks.live.orchestrators import run_compare_scenarios, run_scale
from benchmarks.live.orchestrators.sweep import (
    DEFAULT_LEVELS,
    DEFAULT_MANY_KEYS,
    DEFAULT_RATE,
    DEFAULT_REQUESTS,
    NO_GATE_THRESHOLD,
    run_sweep,
)
from benchmarks.types import BackendKind, BenchmarkConfig, StrategyKind

if IS_WINDOWS := (platform.system() == "Windows"):
    run_multiprocess_scenarios = None
else:
    from benchmarks.bench.multiprocess import (
        run_scenarios as run_multiprocess_scenarios,
    )
from benchmarks.bench.websocket import run_scenarios as run_websocket_scenarios
from benchmarks.output._json import (
    print_aggregate_json,
    print_compare_json,
    print_scale_json,
    print_sweep_json,
)
from benchmarks.output.table import (
    print_aggregate_table,
    print_compare_table,
    print_scale_table,
    print_sweep_table,
)
from benchmarks.scenarios import (
    HTTP_SCENARIOS,
    MIDDLEWARE_SCENARIOS,
    MULTIPROCESS_SCENARIOS,
    WEBSOCKET_SCENARIOS,
    resolve_scenario_keys,
)

P = ParamSpec("P")
R = TypeVar("R")


def plot_options(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
    """`--plot` / `--plot-format`, shared by every command that can draw charts."""

    @click.option(
        "--plot",
        type=click.Path(file_okay=False, path_type=pathlib.Path),
        default=None,
        help=(
            "Write charts for this run into DIRECTORY (created if missing). "
            "Needs matplotlib: pip install 'traffik[benchmark]'."
        ),
    )
    @click.option(
        "--plot-format",
        type=click.Choice(["svg", "png"]),
        default="svg",
        help="Image format for --plot.",
    )
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> R:
        return func(*args, **kwargs)

    return wrapper


def gate_options(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
    """`--no-gate` / `--lock-contention-threshold`, for commands that run traffik."""

    @click.option(
        "--lock-contention-threshold",
        type=click.IntRange(min=1),
        default=None,
        help=(
            "Waiters on one lock name before the process-local contention gate "
            "starts serializing them (networked backends only; backend default if unset)."
        ),
    )
    @click.option(
        "--no-gate",
        is_flag=True,
        help=(
            "Disable the process-local contention gate, to measure what it buys. "
            "A diagnostic, not a recommended setting. Networked backends only."
        ),
    )
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> R:
        return func(*args, **kwargs)

    return wrapper


def resolve_gate_threshold(
    no_gate: bool, threshold: typing.Optional[int]
) -> typing.Optional[int]:
    """Resolve `--no-gate` / `--lock-contention-threshold` to one threshold."""
    return NO_GATE_THRESHOLD if no_gate else threshold


def check_plotting_available(plot: typing.Optional[pathlib.Path]) -> None:
    """Fail fast, before a long benchmark runs, if `--plot` can't work."""
    if plot is None:
        return
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        click.echo(
            "ERROR: --plot needs matplotlib. Install it with: "
            "pip install 'traffik[benchmark]'",
            err=True,
        )
        sys.exit(1)


def report_plots(paths: typing.Iterable[pathlib.Path]) -> None:
    """List written chart files on stderr, so `--output json` stdout stays clean."""
    for path in paths:
        click.echo(f"wrote {path}", err=True)


def draw_aggregate(
    results,
    plot: typing.Optional[pathlib.Path],
    plot_format: str,
    *,
    title: str,
    family: str,
    backend: str,
    strategy: str,
    workers: int,
) -> None:
    """Write the aggregate charts for a run if `--plot` was given."""
    if plot is None:
        return
    from benchmarks.output.plots import file_prefix, plot_aggregate

    report_plots(
        plot_aggregate(
            results,
            plot,
            title=f"{title} ({backend}, {strategy}, {workers} worker(s))",
            prefix=file_prefix(family, backend, strategy, f"w{workers}"),
            family=family,  # type: ignore[arg-type]
            fmt=plot_format,  # type: ignore[arg-type]
        )
    )


def options(
    default_workers: int = 1,
) -> typing.Callable[[typing.Callable[P, R]], typing.Callable[P, R]]:
    """Common Click options for benchmark commands."""

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
    """Traffik benchmark suite."""


@cli.command("http")
@options()
@plot_options
@gate_options
def http(
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
    plot,
    plot_format,
    lock_contention_threshold,
    no_gate,
) -> None:
    """Benchmark HTTP throttles using Depends-based injection."""
    check_plotting_available(plot)
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
        lock_contention_threshold=resolve_gate_threshold(
            no_gate, lock_contention_threshold
        ),
    )
    scenario_keys_ = resolve_scenario_keys(scenarios, HTTP_SCENARIOS)
    results = asyncio.run(run_http_scenarios(config, scenario_keys_, warmup))
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
        print_aggregate_table(results, title="HTTP Benchmark Results", family="http")
    draw_aggregate(
        results,
        plot,
        plot_format,
        title="HTTP Benchmark",
        family="http",
        backend=backend,
        strategy=strategy,
        workers=workers,
    )


@cli.command("middleware")
@options()
@plot_options
@gate_options
def middleware(
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
    plot,
    plot_format,
    lock_contention_threshold,
    no_gate,
) -> None:
    """Benchmark middleware-mounted throttles."""
    check_plotting_available(plot)
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
        lock_contention_threshold=resolve_gate_threshold(
            no_gate, lock_contention_threshold
        ),
    )
    scenario_keys_ = resolve_scenario_keys(scenarios, MIDDLEWARE_SCENARIOS)
    results = asyncio.run(run_middleware_scenarios(config, scenario_keys_, warmup))
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
        print_aggregate_table(
            results, title="Middleware Benchmark Results", family="middleware"
        )
    draw_aggregate(
        results,
        plot,
        plot_format,
        title="Middleware Benchmark",
        family="middleware",
        backend=backend,
        strategy=strategy,
        workers=workers,
    )


@cli.command("websocket")
@options()
@plot_options
def websocket(
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
    plot,
    plot_format,
) -> None:
    """Benchmark WebSocket throttles."""
    check_plotting_available(plot)
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
    scenario_keys_ = resolve_scenario_keys(scenarios, WEBSOCKET_SCENARIOS)
    results = asyncio.run(run_websocket_scenarios(config, scenario_keys_, warmup))
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
        print_aggregate_table(
            results, title="WebSocket Benchmark Results", family="websocket"
        )
    draw_aggregate(
        results,
        plot,
        plot_format,
        title="WebSocket Benchmark",
        family="websocket",
        backend=backend,
        strategy=strategy,
        workers=workers,
    )


@cli.command("multiprocess")
@options(default_workers=4)
@plot_options
def multiprocess(
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
    plot,
    plot_format,
) -> None:
    """
    Benchmark `MultiProcessInMemoryBackend` across real forked gunicorn workers (POSIX only).

    Available scenarios: `under_limit`, `at_limit`, `over_limit`,
    `hot_key_under_limit`, `hot_key_over_limit`, `many_keys_under_limit`,
    `window_rollover`, `many_keys_across_shards`, `key_expiry_reuse`.
    """
    if IS_WINDOWS or run_multiprocess_scenarios is None:
        click.echo("ERROR: MultiProcess benchmarks require a POSIX system.", err=True)
        sys.exit(1)
    check_plotting_available(plot)
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
    scenario_keys_ = resolve_scenario_keys(scenarios, MULTIPROCESS_SCENARIOS)
    results = asyncio.run(run_multiprocess_scenarios(config, scenario_keys_, warmup))
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
        print_aggregate_table(
            results, title="MultiProcess Benchmark Results", family="multiprocess"
        )
    draw_aggregate(
        results,
        plot,
        plot_format,
        title="MultiProcess Benchmark",
        family="multiprocess",
        backend="multiprocess",
        strategy=strategy,
        workers=workers,
    )


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
@plot_options
@gate_options
def compare(
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
    plot,
    plot_format,
    lock_contention_threshold,
    no_gate,
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
    check_plotting_available(plot)
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
        lock_contention_threshold=resolve_gate_threshold(
            no_gate, lock_contention_threshold
        ),
    )

    scenario_source = HTTP_SCENARIOS if mode == "http" else MIDDLEWARE_SCENARIOS
    selected_keys = resolve_scenario_keys(scenarios, scenario_source)

    try:
        results = asyncio.run(
            run_compare_scenarios(
                config, selected_keys, warmup, endpoint_variant=endpoint, mode=mode
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
            results,
            title=f"traffik vs SlowAPI ({backend}, {strategy}, {mode})",
            family=mode,
        )

    if plot is not None:
        from benchmarks.output.plots import file_prefix, plot_compare

        report_plots(
            plot_compare(
                results,
                plot,
                title=f"traffik vs SlowAPI ({backend}, {strategy}, {mode}, {workers} worker(s))",
                prefix=file_prefix("compare", mode, backend, strategy, f"w{workers}"),
                family=mode,
                fmt=plot_format,
            )
        )


@cli.command("sweep")
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
    default="sliding_window_counter",
    help=(
        "Strategy. SlowAPI is only run for fixed_window and sliding_window_counter. "
        "Run the sweep twice (e.g. fixed_window, then sliding_window_counter) to see "
        "how much of a gap is the strategy's locking."
    ),
)
@click.option(
    "--mode",
    type=click.Choice(["http", "middleware"]),
    default="http",
    help="Per-route (Depends/@limiter.limit) or global middleware.",
)
@click.option(
    "--levels",
    default=",".join(str(level) for level in DEFAULT_LEVELS),
    help="Comma-separated requests-in-flight levels to measure.",
)
@click.option(
    "--distribution",
    type=click.Choice(["hot", "many", "both"]),
    default="both",
    help="One shared key, many keys (none shared in flight), or both.",
)
@click.option(
    "--requests",
    type=click.IntRange(min=1),
    default=DEFAULT_REQUESTS,
    help="Requests per point per iteration. P99.9 needs 1,000+ across iterations.",
)
@click.option(
    "--keys",
    type=click.IntRange(min=1),
    default=DEFAULT_MANY_KEYS,
    help="Distinct keys for the many-key distribution (raised to the level if lower).",
)
@click.option(
    "--rate",
    default=DEFAULT_RATE,
    help="Rate limit. Keep it far above --requests so nothing is rejected.",
)
@click.option(
    "--iterations", "-n", type=int, default=3, help="Timed iterations per point."
)
@click.option(
    "--warmup", "-w", type=int, default=1, help="Warmup iterations per point."
)
@click.option("--workers", "-W", type=int, default=1, help="Real worker processes.")
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
@click.option("--redis-url", default="redis://localhost:6379/0")
@click.option("--memcached-host", default="localhost")
@click.option("--memcached-port", type=int, default=11211)
@click.option(
    "--no-slowapi",
    is_flag=True,
    help="Only run traffik, skipping the SlowAPI series.",
)
@click.option(
    "--diagnose-gate",
    is_flag=True,
    help=(
        "Also run traffik with the contention gate disabled, as a third series. "
        "Shows what the gate trades for its tail behavior. Networked backends only."
    ),
)
@plot_options
def sweep(
    backend,
    strategy,
    mode,
    levels,
    distribution,
    requests,
    keys,
    rate,
    iterations,
    warmup,
    workers,
    output,
    redis_url,
    memcached_host,
    memcached_port,
    no_slowapi,
    diagnose_gate,
    plot,
    plot_format,
) -> None:
    """
    Run one workload at increasing load to find where each implementation saturates.

    A single fixed-concurrency run cannot say where throughput stops rising or
    whether a lower peak buys a better tail. The sweep measures throughput and
    P50-P99.9 at each level for a hot key and for many keys, against traffik and
    (when comparable) SlowAPI, and with `--plot` draws the curves.
    """
    check_plotting_available(plot)
    check_workers_platform(workers)
    try:
        level_list = [int(item) for item in levels.split(",") if item.strip()]
    except ValueError:
        click.echo("ERROR: --levels must be comma-separated integers.", err=True)
        sys.exit(1)
        return
    if not level_list or min(level_list) < 1:
        click.echo("ERROR: --levels needs at least one level of 1 or more.", err=True)
        sys.exit(1)
        return

    config = BenchmarkConfig(
        backend_kind=backend,
        strategy_kind=strategy,
        iterations=iterations,
        warmup_iterations=warmup,
        output_format=output,
        redis_url=redis_url,
        memcached_host=memcached_host,
        memcached_port=memcached_port,
        workers=workers,
    )
    distributions = ("hot", "many") if distribution == "both" else (distribution,)
    result = asyncio.run(
        run_sweep(
            config,
            levels=level_list,
            distributions=distributions,  # type: ignore[arg-type]
            rate=rate,
            requests=requests,
            keys=keys,
            warmup_iterations=warmup,
            include_slowapi=not no_slowapi,
            diagnose_gate=diagnose_gate,
            mode=mode,
        )
    )
    if not result.points:
        click.echo("The sweep produced no results.", err=True)
        sys.exit(1)
        return

    if output == "json":
        print_sweep_json(result)
    else:
        print_sweep_table(result)

    if plot is not None:
        from benchmarks.output.plots import file_prefix, plot_sweep

        report_plots(
            plot_sweep(
                result,
                plot,
                title=f"Sweep ({backend}, {strategy}, {mode}, {workers} worker(s))",
                prefix=file_prefix("sweep", mode, backend, strategy, f"w{workers}"),
                fmt=plot_format,
            )
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
@plot_options
def scale(
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
    plot,
    plot_format,
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
    check_plotting_available(plot)
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

    if plot is not None:
        from benchmarks.output.plots import file_prefix, plot_scale

        report_plots(
            plot_scale(
                result,
                plot,
                title=f"Scale ({backend}, {strategy}, {workers} worker(s))",
                prefix=file_prefix("scale", backend, strategy, f"w{workers}"),
                fmt=plot_format,
            )
        )


if not IS_WINDOWS:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
