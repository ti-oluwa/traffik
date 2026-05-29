import typing

from rich.console import Console
from rich.table import Table
from rich.text import Text

from benchmarks.base import AggregatedResult


def print_results_table(
    results: typing.List[AggregatedResult],
    title: str = "Benchmark Results",
) -> None:
    """
    Print a Rich formatted table of aggregated benchmark results to stdout.

    Columns: Scenario | Backend | Strategy | req/s | P50 (ms) | P95 (ms) | P99 (ms) | Success% | Throttled% | Errors%

    :param results: List of aggregated results to display.
    :param title: Title string shown above the table.
    """
    console = Console()
    table = Table(title=title)
    
    # Add columns
    table.add_column("Scenario", width=35)
    table.add_column("Backend", width=18)
    table.add_column("Strategy", width=22)
    table.add_column("req/s", width=10, justify="right")
    table.add_column("P50 (ms)", width=9, justify="right")
    table.add_column("P95 (ms)", width=9, justify="right")
    table.add_column("P99 (ms)", width=9, justify="right")
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
            backend,
            strategy,
            rps,
            p50,
            p95,
            p99,
            success,
            throttle,
            error,
            style=row_style,
        )
    
    console.print(table)
    
    # Print summary
    total_scenarios = len(results)
    total_requests = sum(r.total_requests for r in results)
    total_time = sum(r.results[0].total_time_seconds if r.results else 0 for r in results)
    
    console.print(f"\nTotal scenarios: {total_scenarios} | Total requests: {total_requests} | Run time: {total_time:.1f}s")


def print_comparison_table(
    baseline: AggregatedResult,
    others: typing.List[AggregatedResult],
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
        
        rps_delta = ((result.mean_rps - baseline_rps) / baseline_rps * 100) if baseline_rps > 0 else 0
        p50_delta = ((result.p50_ms - baseline_p50) / baseline_p50 * 100) if baseline_p50 > 0 else 0
        p95_delta = ((result.p95_ms - baseline_p95) / baseline_p95 * 100) if baseline_p95 > 0 else 0
        
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
