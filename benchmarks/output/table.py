import typing

from rich.console import Console
from rich.table import Table
from rich.text import Text

from benchmarks.glossary import Family, get_kind_tag, print_glossary
from benchmarks.types import AggregatedResult, CompareResult, ScaleResult, SweepResult

MIN_SAMPLES_FOR_P999 = 1000
"""Below this many latency samples, P99.9 is effectively just the maximum."""


def print_aggregate_table(
    results: list[AggregatedResult],
    title: str = "Benchmark Results",
    family: typing.Optional[Family] = None,
) -> None:
    """
    Print a Rich formatted table of aggregated benchmark results to stdout.

    Columns: Scenario | Type | Backend | Strategy | req/s | P50 (ms) | P95 (ms) | P99 (ms) | P99.9 (ms) | Success% | Throttled% | Errors%

    When `family` is given, a glossary explaining what each scenario measures
    is printed under the table.

    :param results: List of aggregated results to display.
    :param title: Title string shown above the table.
    :param family: Scenario set the results come from (`"http"`, `"middleware"`,
        `"multiprocess"` or `"websocket"`). Enables the Type column tags and the glossary.
    """
    console = Console()
    table = Table(title=title)

    # Add columns
    table.add_column("Scenario", width=32)
    table.add_column("Type", width=4, justify="center")
    table.add_column("Backend", width=12)
    table.add_column("Strategy", width=22)
    table.add_column("req/s", width=10, justify="right")
    table.add_column("P50 (ms)", width=9, justify="right")
    table.add_column("P95 (ms)", width=9, justify="right")
    table.add_column("P99 (ms)", width=9, justify="right")
    table.add_column("P99.9 (ms)", width=10, justify="right")
    table.add_column("Success %", width=10, justify="right")
    table.add_column("Throttled %", width=11, justify="right")
    table.add_column("Errors %", width=9, justify="right")

    # Add rows
    for result in results:
        scenario = result.scenario_name
        backend = result.backend_kind
        strategy = result.strategy_kind
        rps = f"{result.mean_rps:.1f}"
        p50 = f"{result.p50_ms:.2f}"
        p95 = f"{result.p95_ms:.2f}"
        p99 = f"{result.p99_ms:.2f}"
        p999 = (
            f"{result.p999_ms:.2f}"
            if result.sample_count >= MIN_SAMPLES_FOR_P999
            else "-"
        )
        success = f"{result.success_rate:.1f}"
        throttle = f"{result.throttle_rate:.1f}"
        error = f"{result.error_rate:.1f}"

        # Check for highlighting
        row_style = None
        if result.error_rate > 1.0:
            row_style = "yellow"
        elif result.p99_ms > 100.0:
            row_style = "red"

        table.add_row(
            scenario,
            get_kind_tag(family, scenario),
            backend,
            strategy,
            rps,
            p50,
            p95,
            p99,
            p999,
            success,
            throttle,
            error,
            style=row_style,
        )

    console.print(table)

    # Print summary
    total_scenarios = len(results)
    total_requests = sum(result.total_requests for result in results)
    total_time = sum(
        result.results[0].total_time_seconds if result.results else 0
        for result in results
    )

    console.print(
        f"\nTotal scenarios: {total_scenarios} | Total requests: {total_requests} | Run time: {total_time:.1f}s"
    )
    if family is not None:
        console.print()
        print_glossary((result.scenario_name for result in results), family)


def print_compare_table(
    results: list[CompareResult],
    title: str = "traffik vs SlowAPI",
    family: typing.Optional[Family] = None,
) -> None:
    """
    Print a side-by-side traffik-vs-SlowAPI table, one row per scenario.

    RPS delta is `(traffik - slowapi) / slowapi * 100`: positive means
    traffik was faster in this run, negative means SlowAPI was.

    When `family` is given, a glossary explaining what each scenario measures
    is printed under the table. Read the Type tag before the req/s delta: a
    lower req/s on an `S` (single-key serialization) scenario is expected.

    :param results: List of `CompareResult`.
    :param title: Title string shown above the table.
    :param family: `"http"` or `"middleware"`. Enables the Type tags and glossary.
    """
    console = Console()
    table = Table(title=title)

    table.add_column("Scenario", width=28)
    table.add_column("Type", width=4, justify="center")
    table.add_column("SlowAPI req/s", width=13, justify="right")
    table.add_column("traffik req/s", width=13, justify="right")
    table.add_column("req/s (Δ%)", width=11, justify="right")
    table.add_column("SlowAPI P50", width=11, justify="right")
    table.add_column("traffik P50", width=11, justify="right")
    table.add_column("SlowAPI P99", width=11, justify="right")
    table.add_column("traffik P99", width=11, justify="right")

    for result in results:
        slowapi_rps = result.slowapi.mean_rps
        traffik_rps = result.traffik.mean_rps
        delta = (
            ((traffik_rps - slowapi_rps) / slowapi_rps * 100) if slowapi_rps > 0 else 0
        )
        delta_style = "green" if delta > 0 else "red"

        table.add_row(
            result.scenario_name,
            get_kind_tag(family, result.traffik.scenario_name),
            f"{slowapi_rps:.1f}",
            f"{traffik_rps:.1f}",
            Text(f"{delta:+.1f}%", style=delta_style),
            f"{result.slowapi.p50_ms:.2f}ms",
            f"{result.traffik.p50_ms:.2f}ms",
            f"{result.slowapi.p99_ms:.2f}ms",
            f"{result.traffik.p99_ms:.2f}ms",
        )

    console.print(table)
    console.print(
        "\n[dim]req/s (Δ%): positive = traffik faster, negative = SlowAPI "
        "faster, in this run.[/dim]"
    )

    if family is not None:
        console.print()
        print_glossary((r.traffik.scenario_name for r in results), family)


def print_scale_table(result: ScaleResult, title: str = "Scale Results") -> None:
    """
    Print a `scale` run's checkpoints: memory and latency as key count grows.

    :param result: A `ScaleResult`.
    :param title: Title string shown above the table.
    """
    console = Console()
    table = Table(title=f"{title} ({result.backend_kind}, {result.strategy_kind})")

    table.add_column("Keys", width=12, justify="right")
    table.add_column("New keys", width=10, justify="right")
    table.add_column("RSS (MiB)", width=10, justify="right")
    table.add_column("Δ RSS (MiB)", width=12, justify="right")
    table.add_column("Bytes/key", width=10, justify="right")
    table.add_column("Backend mem", width=12, justify="right")
    table.add_column("req/s", width=9, justify="right")
    table.add_column("P50 (ms)", width=9, justify="right")
    table.add_column("P99 (ms)", width=9, justify="right")

    for checkpoint in result.checkpoints:
        backend_mem = (
            f"{checkpoint.backend_used_memory_mb:.1f}"
            if checkpoint.backend_used_memory_mb is not None
            else "-"
        )
        table.add_row(
            f"{checkpoint.cumulative_keys:,}",
            f"{checkpoint.new_keys_this_checkpoint:,}",
            f"{checkpoint.rss_mb:.1f}",
            f"{checkpoint.rss_delta_mb:.1f}",
            f"{checkpoint.bytes_per_key:.1f}" if checkpoint.cumulative_keys else "-",
            backend_mem,
            f"{checkpoint.mean_rps:.1f}" if checkpoint.mean_rps else "-",
            f"{checkpoint.p50_ms:.2f}" if checkpoint.p50_ms else "-",
            f"{checkpoint.p99_ms:.2f}" if checkpoint.p99_ms else "-",
        )

    console.print(table)
    console.print(
        "\n[dim]RSS is the target server process's resident memory. Bytes/key "
        "is cumulative Δ RSS divided by cumulative keys which is a rough per-key "
        "cost estimate, not a precise allocator accounting.[/dim]"
    )


def print_comparison_table(
    baseline: AggregatedResult,
    others: list[AggregatedResult],
) -> None:
    """
    Print a comparison table showing delta vs a baseline result.

    :param baseline: The reference result to compare against.
    :param others: Results to compare. Each row shows absolute value and % diff vs baseline.
    """
    console = Console()
    table = Table(title="Benchmark Comparison (vs Baseline)")

    table.add_column("Scenario", width=35)
    table.add_column("Backend", width=18)
    table.add_column("req/s (Δ%)", width=15, justify="right")
    table.add_column("P50 (Δ%)", width=13, justify="right")
    table.add_column("P95 (Δ%)", width=13, justify="right")

    baseline_rps = baseline.mean_rps
    baseline_p50 = baseline.p50_ms
    baseline_p95 = baseline.p95_ms

    for result in others:
        scenario = result.scenario_name
        backend = result.backend_kind

        rps_delta = (
            ((result.mean_rps - baseline_rps) / baseline_rps * 100)
            if baseline_rps > 0
            else 0
        )
        p50_delta = (
            ((result.p50_ms - baseline_p50) / baseline_p50 * 100)
            if baseline_p50 > 0
            else 0
        )
        p95_delta = (
            ((result.p95_ms - baseline_p95) / baseline_p95 * 100)
            if baseline_p95 > 0
            else 0
        )

        rps_style = "green" if rps_delta > 0 else "red"
        rps_str = f"{result.mean_rps:.1f} ({rps_delta:+.1f}%)"

        p50_str = f"{result.p50_ms:.2f} ({p50_delta:+.1f}%)"
        p95_str = f"{result.p95_ms:.2f} ({p95_delta:+.1f}%)"

        table.add_row(
            scenario,
            backend,
            Text(rps_str, style=rps_style),
            p50_str,
            p95_str,
        )
    console.print(table)


def print_sweep_table(result: SweepResult) -> None:
    """
    Print a load sweep: one table per key distribution, one row per
    concurrency level and series, then each series' peak throughput.

    :param result: The sweep to display.
    """
    console = Console()
    distributions = list(dict.fromkeys(point.distribution for point in result.points))
    labels = {"hot": "one hot key", "many": "many keys (none shared in flight)"}

    for distribution in distributions:
        table = Table(
            title=(
                f"Sweep, {labels.get(distribution, distribution)} "
                f"({result.backend_kind}, {result.strategy_kind}, "
                f"{result.workers} worker(s), {result.rate})"
            )
        )
        table.add_column("In flight", justify="right", width=9)
        table.add_column("Series", width=18)
        table.add_column("req/s", justify="right", width=10)
        table.add_column("P50 (ms)", justify="right", width=9)
        table.add_column("P95 (ms)", justify="right", width=9)
        table.add_column("P99 (ms)", justify="right", width=9)
        table.add_column("P99.9 (ms)", justify="right", width=10)
        table.add_column("Errors%", justify="right", width=8)

        for point in (
            point for point in result.points if point.distribution == distribution
        ):
            r = point.result
            p999 = f"{r.p999_ms:.2f}" if r.sample_count >= MIN_SAMPLES_FOR_P999 else "-"
            table.add_row(
                str(point.concurrency),
                point.series,
                f"{r.mean_rps:.1f}",
                f"{r.p50_ms:.2f}",
                f"{r.p95_ms:.2f}",
                f"{r.p99_ms:.2f}",
                p999,
                f"{r.error_rate:.1f}",
            )
        console.print(table)

    peaks = Table(title="Peak throughput per series")
    peaks.add_column("Key distribution")
    peaks.add_column("Series")
    peaks.add_column("Peak req/s", justify="right")
    peaks.add_column("At in-flight", justify="right")
    peaks.add_column("P99 there (ms)", justify="right")
    for distribution in distributions:
        for series in dict.fromkeys(point.series for point in result.points):
            candidates = [
                point
                for point in result.points
                if point.distribution == distribution and point.series == series
            ]
            if not candidates:
                continue
            best = max(candidates, key=lambda point: point.result.mean_rps)
            peaks.add_row(
                labels.get(distribution, distribution),
                series,
                f"{best.result.mean_rps:.1f}",
                str(best.concurrency),
                f"{best.result.p99_ms:.2f}",
            )
    console.print(peaks)
    console.print(
        "\n[dim]Closed loop: a fixed number of requests are kept in flight, so this finds "
        "where each implementation stops scaling but understates the tail an "
        "arrival rate it cannot keep up with would produce. The rate limit is far above "
        "the load, so nothing is rejected: what you see is the cost of the throttle "
        "itself. Read the shape (where req/s flattens and P99 starts climbing), not one "
        "point. A lower peak with a flatter tail is a trade, not a loss. The load generator "
        "shares this machine with the server: if req/s falls as in-flight rises even "
        "with many keys, the client is part of the bottleneck.[/dim]"
    )
