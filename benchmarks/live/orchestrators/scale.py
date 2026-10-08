"""
Runs the `scale` command. Starts one traffik app server and keeps it
running for the whole benchmark, growing the backend's distinct-key count
in checkpoints, sampling the target process's memory and the batch's
latency/throughput after each one.

Unlike every other command in this suite, this deliberately does not
restart the server between measurements as the whole point is to watch
memory accumulate and latency shift as the same backend instance holds
more and more live keys, which a fresh process per iteration design would
hide entirely.

Each checkpoint's batch is sent as concurrent traffic (real
sockets, via `httpx2`, gathered concurrently), every request
carries a brand new identity, so both the memory-growth curve and the
latency-at-scale numbers reflect real concurrent access to a backend
that's already holding however many keys the previous checkpoints put
there.
"""

import sys
import time
import typing

from benchmarks.live import client as live_client
from benchmarks.live.orchestrators.core import (
    build_environment_variables,
    warn_unshared_state,
)
from benchmarks.live.server import start_server
from benchmarks.types import (
    BenchmarkConfig,
    ScaleCheckpoint,
    ScaleResult,
    percentile,
)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

TRAFFIK_APP_PATH = "benchmarks.apps.traffik.http:app"

# Generous enough that a single request against a brand-new key is never
# throttled as this command measures memory/latency against key count, not
# throttling behaviour, and every checkpoint's requests each touch a key
# that has never been seen before.
GENEROUS_RATE = "1000000000/3600s"

WARMUP_REQUESTS = 300
"""Requests (on throwaway keys) sent before the baseline memory sample."""


CAPACITY_HEADROOM = 2
"""
Multiplier on the largest checkpoint when sizing the multiprocess backend.

Keys hash unevenly across shards and each shard's capacity is fixed, so a table
sized to exactly the number of keys sent overflows in some shards (which then
answer `ShardFullError` 500s) long before it is full overall.
"""


def default_multiprocess_capacity(checkpoints: typing.Sequence[int]) -> int:
    """
    Capacity for the multiprocess backend when `--mp-max-keys` isn't given.

    :param checkpoints: The checkpoint key counts being measured.
    :return: Enough room for the largest checkpoint, the warmup keys, and uneven
        hashing across shards.
    """
    return max(checkpoints) * CAPACITY_HEADROOM + WARMUP_REQUESTS


def bytes_to_mb(n: float) -> float:
    return n / (1024 * 1024)


def server_memory_mb(process: "psutil.Process") -> float:
    """
    Memory used by the server: its process plus every worker it forked.

    With `--workers` above 1 the process we spawned is only gunicorn's master;
    the workers (which hold the keys) are its children, so measuring the master
    alone would miss almost everything. Proportional set size is used where the
    platform has it, so pages shared between workers (the shared-memory
    backend's segment) are counted once in total rather than once per worker;
    elsewhere it falls back to resident set size.

    :param process: The server's master process.
    :return: Total memory in MiB.
    """
    processes = [process, *process.children(recursive=True)]
    total = 0.0
    for proc in processes:
        try:
            info = proc.memory_full_info()
            total += getattr(info, "pss", None) or info.rss
        except (psutil.Error, AttributeError):
            try:
                total += proc.memory_info().rss
            except psutil.Error:
                continue
    return bytes_to_mb(total)


async def get_redis_used_memory(config: BenchmarkConfig) -> typing.Optional[float]:
    """
    Best-effort `INFO memory` query against the configured Redis, for
    backends where the server's own reported memory is more meaningful
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

        async with live_client.make_http_client(
            server.base_url, concurrency=concurrency
        ) as http_client:
            # Warm the server up before taking the baseline. The first requests
            # allocate one-time state (route and validator caches, connections,
            # a worker's first event-loop iterations) that would otherwise land
            # in the first checkpoint and make the per-key cost look enormous.
            # Warmup keys use their own prefix and are part of the baseline.
            await live_client.send_concurrent_unique_keys(
                http_client,
                start_index=0,
                count=WARMUP_REQUESTS,
                concurrency=concurrency,
                key_header="X-Client-ID",
                key_prefix="warmup",
            )
            # Do a couple of no-op samples first as the very first sample after
            # process start can undershoot before the interpreter and its imports
            # have fully settled.
            server_memory_mb(process)
            baseline_rss_mb = server_memory_mb(process)
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

                rss_mb = server_memory_mb(process)
                rss_delta_mb = rss_mb - baseline_rss_mb
                bytes_per_key = (rss_delta_mb * 1024 * 1024) / target if target else 0.0
                backend_mb = await get_redis_used_memory(config)

                sorted_latencies = sorted(latencies)
                p50_ms = percentile(sorted_latencies, 0.5) * 1000
                p99_ms = percentile(sorted_latencies, 0.99) * 1000
                answered = successful + _throttled
                mean_rps = answered / elapsed if elapsed > 0 else 0.0

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
