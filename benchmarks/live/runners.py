"""
Turns one `HttpScenario`/`WebSocketScenario` plus a running
`Server` into a single `ScenarioResult` - the one place that actually
knows how to drive each traffic pattern, shared by every benchmark
category instead of being reimplemented per scenario.
"""

import asyncio
import time
import typing

import httpx2

from benchmarks.base import BenchmarkConfig, ScenarioResult
from benchmarks.live import client as live_client
from benchmarks.scenarios import HttpScenario, WebSocketScenario


async def run_http_like_scenario(
    scenario: HttpScenario,
    config: BenchmarkConfig,
    client: httpx2.AsyncClient,
    iteration: int,
) -> ScenarioResult:
    """
    Execute one iteration of an HTTP/middleware/multiprocess scenario
    against an already-running server, over `client`.

    :param scenario: What traffic pattern to send.
    :param config: The active benchmark run configuration.
    :param client: A real, connected `httpx2.AsyncClient` for the target server.
    :param iteration: 1-based iteration number (0 for warmup).
    :return: The resulting `ScenarioResult`.
    """
    start_time = time.perf_counter()

    if scenario.mode == "sequential":
        latencies, successful, throttled, errors = await live_client.send_sequential(
            client, n=scenario.total_requests, headers=scenario.headers
        )
    elif scenario.mode == "concurrent":
        latencies, successful, throttled, errors = await live_client.send_concurrent(
            client,
            n=scenario.total_requests,
            concurrency=config.concurrency,
            headers=scenario.headers,
        )
    elif scenario.mode == "waves":
        assert scenario.waves is not None
        latencies, successful, throttled, errors = await live_client.send_waves(
            client, waves=scenario.waves, headers=scenario.headers
        )
    elif scenario.mode == "unique_keys_batched":
        key_mod = (
            config.concurrency if scenario.key_mod_is_concurrency else scenario.key_mod
        )
        batch_size = scenario.batch_size or config.concurrency
        latencies, successful, throttled, errors = await live_client.send_concurrent(
            client,
            n=scenario.total_requests,
            concurrency=batch_size,
            headers=scenario.headers,
            key_header=scenario.key_header,
            key_mod=key_mod,
        )
    elif scenario.mode == "unique_keys_split":
        assert scenario.key_header is not None and scenario.key_mod is not None
        half = scenario.total_requests // 2
        latencies, successful, throttled, errors = [], 0, 0, 0

        for start_idx in (0, half):
            count = half if start_idx == 0 else scenario.total_requests - half
            (
                batch_latencies,
                batch_ok,
                batch_throttled,
                batch_errors,
            ) = await _send_sequential_with_keys(
                client,
                start_index=start_idx,
                count=count,
                key_header=scenario.key_header,
                key_mod=scenario.key_mod,
            )
            latencies.extend(batch_latencies)
            successful += batch_ok
            throttled += batch_throttled
            errors += batch_errors

            if start_idx == 0 and scenario.extra_sleep_seconds:
                await asyncio.sleep(scenario.extra_sleep_seconds)
    elif scenario.mode == "mixed_paths":
        assert scenario.mixed_paths is not None
        latencies, successful, throttled, errors = [], 0, 0, 0
        for path, count in scenario.mixed_paths:
            (
                batch_latencies,
                batch_ok,
                batch_throttled,
                batch_errors,
            ) = await live_client.send_sequential(client, n=count, path=path)
            latencies.extend(batch_latencies)
            successful += batch_ok
            throttled += batch_throttled
            errors += batch_errors
    else:
        raise ValueError(f"Unknown scenario mode: {scenario.mode!r}")

    total_time = time.perf_counter() - start_time

    return ScenarioResult(
        scenario_name=scenario.name,
        backend_kind=config.backend_kind,
        strategy_kind=config.strategy_kind,
        total_requests=scenario.total_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        error_requests=errors,
        total_time_seconds=total_time,
        latencies_seconds=latencies,
        iteration=iteration,
    )


async def _send_sequential_with_keys(
    client: httpx2.AsyncClient,
    start_index: int,
    count: int,
    key_header: str,
    key_mod: int,
) -> typing.Tuple[typing.List[float], int, int, int]:
    """Sequential requests indexed from `start_index`, each with a distinct key."""
    latencies: typing.List[float] = []
    successful = throttled = errors = 0

    for offset in range(count):
        index = start_index + offset
        headers = {key_header: f"user-{index % key_mod}"}
        latency, status_code = await live_client.make_request(client, "/test", headers)
        s, t, e = live_client.tally(latency, status_code, latencies)
        successful += s
        throttled += t
        errors += e

    return latencies, successful, throttled, errors


async def run_websocket_scenario(
    scenario: WebSocketScenario,
    config: BenchmarkConfig,
    ws_url: str,
    iteration: int,
) -> ScenarioResult:
    """
    Execute one iteration of a WebSocket scenario against `ws_url`
    (a real `ws://host:port/ws` on an already-running server).

    :param scenario: What traffic pattern to send.
    :param config: The active benchmark run configuration.
    :param ws_url: Full WebSocket URI to connect to.
    :param iteration: 1-based iteration number (0 for warmup).
    :return: The resulting `ScenarioResult`.
    """
    start_time = time.perf_counter()

    if scenario.mode == "sequential":
        latencies, successful, throttled = await live_client.ws_send_messages(
            ws_url, n=scenario.total_messages
        )
        total_requests = scenario.total_messages
    elif scenario.mode == "waves":
        assert scenario.waves is not None
        latencies, successful, throttled = await live_client.ws_send_waves(
            ws_url, waves=scenario.waves
        )
        total_requests = sum(count for count, _ in scenario.waves)
    elif scenario.mode == "concurrent_connections":
        latencies, successful, throttled = await live_client.ws_concurrent_connections(
            ws_url,
            connections=scenario.connections,
            messages_per_connection=scenario.messages_per_connection,
        )
        total_requests = scenario.connections * scenario.messages_per_connection
    else:
        raise ValueError(f"Unknown WebSocket scenario mode: {scenario.mode!r}")

    total_time = time.perf_counter() - start_time

    return ScenarioResult(
        scenario_name=scenario.name,
        backend_kind=config.backend_kind,
        strategy_kind=config.strategy_kind,
        total_requests=total_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        error_requests=max(total_requests - successful - throttled, 0),
        total_time_seconds=total_time,
        latencies_seconds=latencies,
        iteration=iteration,
    )
