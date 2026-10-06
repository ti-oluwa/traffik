import datetime
import json
import platform
import sys
import typing

from benchmarks.types import AggregatedResult, CompareResult, ScaleResult, SweepResult


def result_to_dict(result: AggregatedResult) -> dict:
    """
    Serialize an AggregatedResult to a plain dict suitable for JSON output.

    :param result: The aggregated result to serialize.
    :return: A dict with all computed properties included.
    """
    return {
        "scenario_name": result.scenario_name,
        "backend_kind": result.backend_kind,
        "strategy_kind": result.strategy_kind,
        "iterations": result.iterations,
        "total_requests": result.total_requests,
        "mean_rps": result.mean_rps,
        "p50_ms": result.p50_ms,
        "p95_ms": result.p95_ms,
        "p99_ms": result.p99_ms,
        "p999_ms": result.p999_ms,
        "sample_count": result.sample_count,
        "mean_ms": result.mean_ms,
        "mean_allowed_rps": result.mean_allowed_rps,
        "mean_throttled_rps": result.mean_throttled_rps,
        "success_rate": result.success_rate,
        "throttle_rate": result.throttle_rate,
        "error_rate": result.error_rate,
        "rps_stddev": result.rps_stddev,
    }


def compare_result_to_dict(result: CompareResult) -> dict:
    """
    Serialize a CompareResult to a plain dict suitable for JSON output.

    :param result: The paired traffik/SlowAPI result to serialize.
    :return: A dict with both sides' full aggregated results plus the
        computed req/s delta.
    """
    slowapi_rps = result.slowapi.mean_rps
    traffik_rps = result.traffik.mean_rps
    rps_delta_pct = (
        ((traffik_rps - slowapi_rps) / slowapi_rps * 100) if slowapi_rps > 0 else None
    )
    return {
        "scenario_key": result.scenario_key,
        "scenario_name": result.scenario_name,
        "traffik": result_to_dict(result.traffik),
        "slowapi": result_to_dict(result.slowapi),
        "rps_delta_pct": rps_delta_pct,
    }


def scale_result_to_dict(result: ScaleResult) -> dict:
    """
    Serialize a ScaleResult to a plain dict suitable for JSON output.

    :param result: The scale run result to serialize.
    :return: A dict with backend/strategy/worker metadata and every checkpoint.
    """
    return {
        "backend_kind": result.backend_kind,
        "strategy_kind": result.strategy_kind,
        "workers": result.workers,
        "checkpoints": [
            {
                "cumulative_keys": checkpoint.cumulative_keys,
                "new_keys_this_checkpoint": checkpoint.new_keys_this_checkpoint,
                "rss_mb": checkpoint.rss_mb,
                "rss_delta_mb": checkpoint.rss_delta_mb,
                "bytes_per_key": checkpoint.bytes_per_key,
                "backend_used_memory_mb": checkpoint.backend_used_memory_mb,
                "mean_rps": checkpoint.mean_rps,
                "p50_ms": checkpoint.p50_ms,
                "p99_ms": checkpoint.p99_ms,
                "successful": checkpoint.successful,
                "errors": checkpoint.errors,
            }
            for checkpoint in result.checkpoints
        ],
    }


def default_meta() -> dict[str, typing.Any]:
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": sys.platform,
        "python_version": platform.python_version(),
    }


def print_aggregate_json(
    results: list[AggregatedResult],
    meta: typing.Optional[dict[str, typing.Any]] = None,
) -> None:
    """
    Print aggregated results as a JSON object to stdout.

    :param results: List of aggregated results to serialize.
    :param meta: Optional metadata dict to include (e.g. backend version, run timestamp).
    """
    meta = {**default_meta(), **(meta or {})}
    output = {
        "meta": meta,
        "results": [result_to_dict(r) for r in results],
    }
    print(json.dumps(output, indent=2))


def print_compare_json(
    results: list[CompareResult],
    meta: typing.Optional[dict[str, typing.Any]] = None,
) -> None:
    """
    Print `compare` results as a JSON object to stdout.

    :param results: List of paired traffik/SlowAPI results to serialize.
    :param meta: Optional metadata dict to include.
    """
    meta = {**default_meta(), **(meta or {})}
    output = {
        "meta": meta,
        "results": [compare_result_to_dict(r) for r in results],
    }
    print(json.dumps(output, indent=2))


def print_scale_json(
    result: ScaleResult,
    meta: typing.Optional[dict[str, typing.Any]] = None,
) -> None:
    """
    Print a `scale` result as a JSON object to stdout.

    :param result: The scale run result to serialize.
    :param meta: Optional metadata dict to include.
    """
    meta = {**default_meta(), **(meta or {})}
    output = {
        "meta": meta,
        "result": scale_result_to_dict(result),
    }
    print(json.dumps(output, indent=2))


def sweep_result_to_dict(result: SweepResult) -> dict:
    """
    Serialize a SweepResult to a plain dict suitable for JSON output.

    :param result: The sweep to serialize.
    :return: A dict with the sweep's settings and every measured point.
    """
    return {
        "backend_kind": result.backend_kind,
        "strategy_kind": result.strategy_kind,
        "workers": result.workers,
        "rate": result.rate,
        "requests_per_iteration": result.requests_per_iteration,
        "points": [
            {
                "series": point.series,
                "distribution": point.distribution,
                "concurrency": point.concurrency,
                **result_to_dict(point.result),
            }
            for point in result.points
        ],
    }


def print_sweep_json(
    result: SweepResult,
    meta: typing.Optional[dict[str, typing.Any]] = None,
) -> None:
    """
    Print `sweep` results as a JSON object to stdout.

    :param result: The sweep to serialize.
    :param meta: Optional metadata dict to include.
    """
    meta = {**default_meta(), **(meta or {})}
    print(json.dumps({"meta": meta, "result": sweep_result_to_dict(result)}, indent=2))
