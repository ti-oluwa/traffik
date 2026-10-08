import asyncio
import json
import time
import typing

import httpx2
import websockets

Headers = typing.Optional[dict[str, str]]
SendResult = tuple[list[float], int, int, int]  # latencies, ok, throttled, error


def make_http_client(
    base_url: str, concurrency: int = 50, timeout: float = 30.0
) -> httpx2.AsyncClient:
    """
    Build a real, connection-pooled async HTTP client for a live server.

    :param base_url: e.g. `"http://127.0.0.1:8000"`.
    :param concurrency: Sizes the connection pool so concurrent scenarios
        aren't artificially bottlenecked on pool limits rather than the
        server itself.
    :param timeout: Per-request timeout in seconds.
    :return: An unopened `httpx2.AsyncClient` (use as `async with`).
    """
    limits = httpx2.Limits(
        max_connections=max(concurrency * 2, 20),
        max_keepalive_connections=max(concurrency, 20),
    )
    return httpx2.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        limits=limits,
        headers={"user-agent": "traffik-benchmarks"},
    )


async def make_request(
    client: httpx2.AsyncClient, path: str, headers: Headers
) -> tuple[float, int]:
    """
    Send one request and time it.

    A request that fails (timeout, reset connection) keeps the time it took and
    reports status `0`. Dropping failures from the latency data would make an
    implementation that times out look faster: its slowest requests would simply
    vanish from the percentiles.

    :return: `(latency_seconds, status_code)`; status `0` means the request failed.
    """
    start = time.perf_counter()
    try:
        response = await client.get(path, headers=headers)
    except Exception:
        return time.perf_counter() - start, 0
    return time.perf_counter() - start, response.status_code


def tally(
    latency: float, status_code: int, latencies: list[float]
) -> tuple[int, int, int]:
    if latency > 0:
        latencies.append(latency)
    if status_code == 200:
        return 1, 0, 0
    elif status_code == 429:
        return 0, 1, 0
    return 0, 0, 1


async def send_sequential(
    client: httpx2.AsyncClient,
    n: int,
    path: str = "/test",
    headers: Headers = None,
) -> SendResult:
    """
    Send `n` real requests one after another over the same client (so
    connections get reused, same as a real keep-alive client would).

    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    latencies: list[float] = []
    successful = throttled = errors = 0

    for _ in range(n):
        latency, status_code = await make_request(client, path, headers)
        s, t, e = tally(latency, status_code, latencies)
        successful += s
        throttled += t
        errors += e

    return latencies, successful, throttled, errors


Sender = typing.Callable[[int, int, int], typing.Awaitable[tuple[float, int]]]


async def run_closed_loop(n: int, concurrency: int, send: Sender) -> SendResult:
    """
    Send `n` requests keeping exactly `concurrency` in flight (a closed loop).

    Each of `concurrency` workers sends its next request the moment its previous
    response arrives, so the load stays constant. Sending in barrier-synchronized
    batches instead (gather a batch, wait for all of it, repeat) ties throughput to
    the slowest request of every batch, which rewards the implementation with the
    better tail and lets in-flight load drop as each batch drains.

    :param n: Total requests to send.
    :param concurrency: Workers, i.e. requests in flight.
    :param send: `send(worker, worker_request_number, sequence)` sends one
        request and returns `(latency_seconds, status_code)`. `sequence` is a
        global counter, unique per request.
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    latencies: list[float] = []
    totals = [0, 0, 0]  # successful, throttled, errors
    next_sequence = 0

    async def worker(worker_id: int) -> None:
        nonlocal next_sequence
        sent = 0
        while next_sequence < n:
            sequence = next_sequence
            next_sequence += 1
            latency, status_code = await send(worker_id, sent, sequence)
            sent += 1
            s, t, e = tally(latency, status_code, latencies)
            totals[0] += s
            totals[1] += t
            totals[2] += e

    await asyncio.gather(*(worker(w) for w in range(min(concurrency, n))))
    return latencies, totals[0], totals[1], totals[2]


async def send_concurrent(
    client: httpx2.AsyncClient,
    n: int,
    concurrency: int,
    path: str = "/test",
    headers: Headers = None,
    key_header: typing.Optional[str] = None,
    key_mod: typing.Optional[int] = None,
) -> SendResult:
    """
    Send `n` real requests with `concurrency` in flight at all times, over
    genuine concurrent sockets.

    :param key_header: If given (e.g. `"X-Client-ID"`), requests carry distinct
        identities in this header, simulating traffic from many clients. Each
        worker owns its own identities, so no two requests in flight ever share
        one, however slowly any of them completes.
    :param key_mod: Approximate number of distinct identities to cycle through
        when `key_header` is set, rounded down to a multiple of `concurrency`
        and never below it (one identity per worker).
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    keys_per_worker = max((key_mod or concurrency) // concurrency, 1)

    async def send(worker: int, sent: int, _sequence: int) -> tuple[float, int]:
        request_headers = dict(headers or {})
        if key_header and key_mod:
            identity = worker + concurrency * (sent % keys_per_worker)
            request_headers[key_header] = f"user-{identity}"
        return await make_request(client, path, request_headers or None)

    return await run_closed_loop(n, concurrency, send)


async def send_concurrent_unique_keys(
    client: httpx2.AsyncClient,
    start_index: int,
    count: int,
    concurrency: int,
    key_header: str,
    path: str = "/test",
    headers: Headers = None,
    key_prefix: str = "user",
) -> SendResult:
    """
    Send `count` real requests with `concurrency` in flight, each carrying a
    distinct, never-repeated `key_header` value `f"{key_prefix}-{start_index + i}"`.

    Used by the `scale` command to grow a backend's key count by a precise
    amount while sending genuinely concurrent traffic, so both the
    memory-growth measurement and the latency measurement reflect real
    concurrent access, not a sequential loop.

    :param start_index: First key index to use; keys run
        `start_index .. start_index + count - 1`.
    :param count: Number of requests (and therefore new keys) to send.
    :param key_header: Header identifying the caller (e.g. `"X-Client-ID"`).
    :param key_prefix: Prefix of the generated identities. Use a different one
        for warmup traffic so it can never collide with measured keys.
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """

    async def send(_worker: int, _sent: int, sequence: int) -> tuple[float, int]:
        request_headers = dict(headers or {})
        request_headers[key_header] = f"{key_prefix}-{start_index + sequence}"
        return await make_request(client, path, request_headers)

    return await run_closed_loop(count, concurrency, send)


async def send_waves(
    client: httpx2.AsyncClient,
    waves: typing.Sequence[tuple[int, float]],
    path: str = "/test",
    headers: Headers = None,
) -> SendResult:
    """
    Send several back-to-back bursts of sequential requests, sleeping
    between bursts - for probing behaviour at window boundaries.

    :param waves: `[(requests_in_wave, seconds_to_sleep_after), ...]`.
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    all_latencies: list[float] = []
    total_successful = total_throttled = total_errors = 0

    for i, (count, sleep_after) in enumerate(waves):
        latencies, successful, throttled, errors = await send_sequential(
            client, count, path=path, headers=headers
        )
        all_latencies.extend(latencies)
        total_successful += successful
        total_throttled += throttled
        total_errors += errors

        if sleep_after and i < len(waves) - 1:
            await asyncio.sleep(sleep_after)

    return all_latencies, total_successful, total_throttled, total_errors


# WebSocket (real connections, via the `websockets` library)


async def ws_send_messages(
    uri: str, n: int, connect_timeout: float = 10.0
) -> tuple[list[float], int, int]:
    """
    Open one real WebSocket connection and send `n` JSON messages
    sequentially over it, timing each round trip.

    :param uri: Full `ws://host:port/path` URI.
    :param n: Number of messages to send.
    :return: `(latencies_seconds, successful, throttled)`.
    """
    latencies: list[float] = []
    successful = throttled = 0

    async with websockets.connect(uri, open_timeout=connect_timeout) as ws:
        for i in range(n):
            try:
                start = time.perf_counter()
                await ws.send(json.dumps({"message": f"test_{i}"}))
                raw = await ws.recv()
                end = time.perf_counter()
                latencies.append(end - start)

                data = json.loads(raw)
                if data.get("type") == "rate_limit":
                    throttled += 1
                else:
                    successful += 1
            except Exception:
                pass

    return latencies, successful, throttled


async def ws_send_waves(
    uri: str,
    waves: typing.Sequence[tuple[int, float]],
    connect_timeout: float = 10.0,
) -> tuple[list[float], int, int]:
    """
    Open one real WebSocket connection and send several waves of messages
    over it, sleeping between waves - for window-boundary scenarios.

    :param uri: Full `ws://host:port/path` URI.
    :param waves: `[(messages_in_wave, seconds_to_sleep_after), ...]`.
    :return: `(latencies_seconds, successful, throttled)`.
    """

    all_latencies: list[float] = []
    total_successful = total_throttled = 0

    async with websockets.connect(uri, open_timeout=connect_timeout) as ws:
        for i, (count, sleep_after) in enumerate(waves):
            for j in range(count):
                try:
                    start = time.perf_counter()
                    await ws.send(json.dumps({"message": f"test_{i}_{j}"}))
                    raw = await ws.recv()
                    end = time.perf_counter()
                    all_latencies.append(end - start)

                    data = json.loads(raw)
                    if data.get("type") == "rate_limit":
                        total_throttled += 1
                    else:
                        total_successful += 1
                except Exception:
                    pass

            if sleep_after and i < len(waves) - 1:
                await asyncio.sleep(sleep_after)

    return all_latencies, total_successful, total_throttled


async def ws_concurrent_connections(
    uri: str,
    connections: int,
    messages_per_connection: int,
    connect_timeout: float = 10.0,
) -> tuple[list[float], int, int]:
    """
    Open several real, concurrent WebSocket connections, each sending
    `messages_per_connection` sequential messages.

    :param uri: Full `ws://host:port/path` URI.
    :param connections: Number of concurrent connections to open.
    :param messages_per_connection: Messages sent sequentially per connection.
    :return: `(latencies_seconds, successful, throttled)` pooled across
        all connections.
    """

    async def connection() -> tuple[list[float], int, int]:
        try:
            return await ws_send_messages(
                uri, messages_per_connection, connect_timeout=connect_timeout
            )
        except Exception:
            return [], 0, 0

    results = await asyncio.gather(*[connection() for _ in range(connections)])

    all_latencies: list[float] = []
    total_successful = total_throttled = 0
    for latencies, successful, throttled in results:
        all_latencies.extend(latencies)
        total_successful += successful
        total_throttled += throttled

    return all_latencies, total_successful, total_throttled
