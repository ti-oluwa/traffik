import datetime
import json
import platform
import sys
import typing

from benchmarks.base import AggregatedResult


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
        "mean_ms": result.mean_ms,
        "success_rate": result.success_rate,
        "throttle_rate": result.throttle_rate,
        "error_rate": result.error_rate,
        "rps_stddev": result.rps_stddev,
    }


def print_json(
    results: typing.List[AggregatedResult],
    meta: typing.Optional[typing.Dict[str, typing.Any]] = None,
) -> None:
    """
    Print results as a JSON object to stdout.

    :param results: List of aggregated results to serialize.
    :param meta: Optional metadata dict to include (e.g. backend version, run timestamp).
    """
    if meta is None:
        meta = {}

    # Add default metadata
    meta.setdefault(
        "timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    meta.setdefault("platform", sys.platform)
    meta.setdefault("python_version", platform.python_version())

    output = {
        "meta": meta,
        "results": [result_to_dict(r) for r in results],
    }

    print(json.dumps(output, indent=2))
