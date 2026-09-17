"""
Runs the `scale` command: starts one traffik app server and keeps it
running for the whole benchmark, growing the backend's distinct-key count
in checkpoints, sampling the target process's memory and the batch's
latency/throughput after each one.

Unlike every other command in this suite, this deliberately does NOT
restart the server between measurements - the whole point is to watch
memory accumulate and latency shift as the *same* backend instance holds
more and more live keys, which a fresh-process-per-iteration design would
hide entirely.

Each checkpoint's batch is sent as genuinely concurrent traffic (real
sockets, via `httpx2`, gathered concurrently - see
`benchmarks.live.client.send_concurrent_unique_keys`), every request
carrying a brand new identity, so both the memory-growth curve and the
latency-at-scale numbers reflect real concurrent access to a backend
that's already holding however many keys the previous checkpoints put
there - not a sequential loop that never exercises the backend's locking
under load.
"""

import sys
import time
import typing

from benchmarks.live import client as live_client
from benchmarks.live.orchestrators import (
    build_environment_variables,
    warn_unshared_state,
)
from benchmarks.live.server import start_server
from benchmarks.types import BenchmarkConfig, ScaleCheckpoint, ScaleResult

try:
    import psutil
except ImportError:  # pragma: no cover - benchmark extra, not a hard runtime dep
    psutil = None  # type: ignore[assignment]

TRAFFIK_APP_PATH = "benchmarks.apps.http:app"

# Generous enough that a single request against a brand-new key is never
# throttled - this command measures memory/latency vs. key count, not
# throttling behaviour, and every checkpoint's requests each touch a key
# that has never been seen before.
GENEROUS_RATE = "1000000000/3600s"


def bytes_to_mb(n: float) -> float:
    return n / (1024 * 1024)


async def get_redis_used_memory(config: BenchmarkConfig) -> typing.Optional[float]:
    """
    Best-effort `INFO memory` query against the configured Redis, for
    backends where the *server's* own reported memory is more meaningful
    than the app process's RSS (the app process mostly just holds
    connections for these backends, not the actual key data).

    :return: `used_memory` in MiB, or `None` if not applicable, `redis`
        isn't installed, or the query fails for any reason - this is a
        nice-to-have, not something worth failing the whole run over.
    """
    if config.backend_kind not in ("aioredis", "coredis"):
        return None
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return None

    try:
        redis_client = redis_asyncio.Redis.from_url(config.redis_url)
        try:
            info = await redis_client.info("memory")
            return bytes_to_mb(float(info["used_memory"]))
        finally:
            await redis_client.aclose()
    except Exception:
        return None


async def run_scale(
    config: BenchmarkConfig,
    checkpoints: list[int],
    concurrency: int,
) -> ScaleResult:
    """
    Grow one continuously-running traffik server's backend to each of
    `checkpoints` (cumulative, ascending distinct-key counts), measuring
    process RSS and per-batch latency/throughput after each one.

    :param config: Global benchmark configuration.
    :param checkpoints: Cumulative distinct-key counts to measure at
        (e.g. `[1_000, 10_000, 100_000, 1_000_000]`). Deduplicated and
        sorted ascending; non-positive values are dropped.
    :param concurrency: How many requests to have in flight at once
        within each checkpoint's batch.
    :return: A `ScaleResult` with one entry per checkpoint, plus a
        leading zero-key baseline entry.
    :raises RuntimeError: If `psutil` (a benchmark-only dependency) isn't
        installed.
    """
    if psutil is None:
        raise RuntimeError(
            "`scale` requires psutil. Install the benchmark extras: "
            "`uv sync --group benchmark` (or `pip install psutil` directly)."
        )

    warn_unshared_state(config)
    sorted_checkpoints = sorted({c for c in checkpoints if c > 0})
    if not sorted_checkpoints:
        raise ValueError("`checkpoints` must contain at least one positive value.")

    env = build_environment_variables(
        config, rate=GENEROUS_RATE, uid="scale", on_error="raise"
    )
    server = await start_server(TRAFFIK_APP_PATH, env=env, workers=config.workers)

    results: list[ScaleCheckpoint] = []
    try:
        process = psutil.Process(server.process.pid)
        # A couple of no-op samples first: the very first sample after
        # process start can be an undershoot before the interpreter and
        # its imports have fully settled.
        process.memory_info()
        baseline_rss_mb = bytes_to_mb(process.memory_info().rss)
        baseline_backend_mb = await get_redis_used_memory(config)

        print(
            f"Baseline: 0 keys, {baseline_rss_mb:.1f} MiB RSS",
            file=sys.stderr,
        )
        results.append(
            ScaleCheckpoint(
                cumulative_keys=0,
                new_keys_this_checkpoint=0,
                rss_mb=baseline_rss_mb,
                rss_delta_mb=0.0,
                bytes_per_key=0.0,
                backend_used_memory_mb=baseline_backend_mb,
                mean_rps=0.0,
                p50_ms=0.0,
                p99_ms=0.0,
                successful=0,
                errors=0,
            )
        )

        async with live_client.make_http_client(
            server.base_url, concurrency=concurrency
        ) as http_client:
            previous_cumulative = 0
            for target in sorted_checkpoints:
                new_keys = target - previous_cumulative
                print(
                    f"Growing to {target:,} keys (+{new_keys:,})...",
                    file=sys.stderr,
                )

                start_time = time.perf_counter()
                (
                    latencies,
                    successful,
                    _throttled,
                    errors,
                ) = await live_client.send_concurrent_unique_keys(
                    http_client,
                    start_index=previous_cumulative,
                    count=new_keys,
                    concurrency=concurrency,
                    key_header="X-Client-ID",
                )
                elapsed = time.perf_counter() - start_time

                rss_mb = bytes_to_mb(process.memory_info().rss)
                rss_delta_mb = rss_mb - baseline_rss_mb
                bytes_per_key = (rss_delta_mb * 1024 * 1024) / target if target else 0.0
                backend_mb = await get_redis_used_memory(config)

                sorted_latencies = sorted(latencies)
                p50_ms = (
                    sorted_latencies[len(sorted_latencies) // 2] * 1000
                    if sorted_latencies
                    else 0.0
                )
                p99_ms = (
                    sorted_latencies[int(len(sorted_latencies) * 0.99)] * 1000
                    if sorted_latencies
                    else 0.0
                )
                mean_rps = len(latencies) / elapsed if elapsed > 0 else 0.0

                results.append(
                    ScaleCheckpoint(
                        cumulative_keys=target,
                        new_keys_this_checkpoint=new_keys,
                        rss_mb=rss_mb,
                        rss_delta_mb=rss_delta_mb,
                        bytes_per_key=bytes_per_key,
                        backend_used_memory_mb=backend_mb,
                        mean_rps=mean_rps,
                        p50_ms=p50_ms,
                        p99_ms=p99_ms,
                        successful=successful,
                        errors=errors,
                    )
                )
                previous_cumulative = target
    finally:
        await server.stop()

    return ScaleResult(
        backend_kind=config.backend_kind,
        strategy_kind=config.strategy_kind,
        workers=config.workers,
        checkpoints=results,
    )
