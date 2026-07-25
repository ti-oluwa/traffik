"""
Load generation against a real, running server.

HTTP goes through `httpx2.AsyncClient` with its default (real) transport -
actual TCP connections to `127.0.0.1`, actual HTTP/1.1 framing, actual
connection pooling. This is the same async client family used elsewhere in
the project (`tests/client.py`), just pointed at a socket instead of
`ASGITransport`, since ASGITransport is for calling the app in-process and
that's exactly what we're trying to stop doing.

WebSocket connections use the `websockets` library since httpx/httpx2 don't
implement the client side of a real WebSocket upgrade - only the ASGI
in-process half (which is what `tests/client.py`'s
`AsyncWebSocketTestSession` hand-rolls). `websockets` is fully async and
talks a real WS handshake over a real socket.
"""

import asyncio
import json
import time
import typing

import httpx2
import websockets

Headers = typing.Optional[typing.Dict[str, str]]
SendResult = typing.Tuple[typing.List[float], int, int, int]
# latencies, ok, throttled, error


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
) -> typing.Tuple[float, int]:
    try:
        start = time.perf_counter()
        response = await client.get(path, headers=headers)
        end = time.perf_counter()
        return end - start, response.status_code
    except Exception:  # noqa
        return 0.0, 0


def tally(
    latency: float, status_code: int, latencies: typing.List[float]
) -> typing.Tuple[int, int, int]:
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
    latencies: typing.List[float] = []
    successful = throttled = errors = 0

    for _ in range(n):
        latency, status_code = await make_request(client, path, headers)
        s, t, e = tally(latency, status_code, latencies)
        successful += s
        throttled += t
        errors += e

    return latencies, successful, throttled, errors


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
    Send `n` real requests in batches of up to `concurrency`, gathered
    concurrently within each batch - genuine concurrent sockets, not
    cooperative-scheduling-only concurrency against an in-process callable.

    :param key_header: If given (e.g. `"X-Client-ID"`), each request gets
        a distinct value `f"user-{index % key_mod}"` for this header,
        simulating traffic from many different identities.
    :param key_mod: Number of distinct identities to cycle through when
        `key_header` is set.
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    latencies: typing.List[float] = []
    successful = throttled = errors = 0
    num_batches = (n + concurrency - 1) // concurrency

    for batch_idx in range(num_batches):
        batch_size = min(concurrency, n - batch_idx * concurrency)

        async def _request(index: int) -> typing.Tuple[float, int]:
            request_headers = dict(headers or {})
            if key_header and key_mod:
                request_headers[key_header] = f"user-{index % key_mod}"
            return await make_request(
                client, path=path, headers=request_headers or None
            )

        tasks = [_request(batch_idx * concurrency + i) for i in range(batch_size)]
        results = await asyncio.gather(*tasks)

        for latency, status_code in results:
            s, t, e = tally(latency, status_code, latencies)
            successful += s
            throttled += t
            errors += e

    return latencies, successful, throttled, errors


async def send_waves(
    client: httpx2.AsyncClient,
    waves: typing.Sequence[typing.Tuple[int, float]],
    path: str = "/test",
    headers: Headers = None,
) -> SendResult:
    """
    Send several back-to-back bursts of sequential requests, sleeping
    between bursts - for probing behaviour at window boundaries.

    :param waves: `[(requests_in_wave, seconds_to_sleep_after), ...]`.
    :return: `(latencies_seconds, successful, throttled, errors)`.
    """
    all_latencies: typing.List[float] = []
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
) -> typing.Tuple[typing.List[float], int, int]:
    """
    Open one real WebSocket connection and send `n` JSON messages
    sequentially over it, timing each round trip.

    :param uri: Full `ws://host:port/path` URI.
    :param n: Number of messages to send.
    :return: `(latencies_seconds, successful, throttled)`.
    """
    latencies: typing.List[float] = []
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
            except Exception:  # noqa
                pass

    return latencies, successful, throttled


async def ws_send_waves(
    uri: str,
    waves: typing.Sequence[typing.Tuple[int, float]],
    connect_timeout: float = 10.0,
) -> typing.Tuple[typing.List[float], int, int]:
    """
    Open one real WebSocket connection and send several waves of messages
    over it, sleeping between waves - for window-boundary scenarios.

    :param uri: Full `ws://host:port/path` URI.
    :param waves: `[(messages_in_wave, seconds_to_sleep_after), ...]`.
    :return: `(latencies_seconds, successful, throttled)`.
    """

    all_latencies: typing.List[float] = []
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
                except Exception:  # noqa
                    pass

            if sleep_after and i < len(waves) - 1:
                await asyncio.sleep(sleep_after)

    return all_latencies, total_successful, total_throttled


async def ws_concurrent_connections(
    uri: str,
    connections: int,
    messages_per_connection: int,
    connect_timeout: float = 10.0,
) -> typing.Tuple[typing.List[float], int, int]:
    """
    Open several real, concurrent WebSocket connections, each sending
    `messages_per_connection` sequential messages.

    :param uri: Full `ws://host:port/path` URI.
    :param connections: Number of concurrent connections to open.
    :param messages_per_connection: Messages sent sequentially per connection.
    :return: `(latencies_seconds, successful, throttled)` pooled across
        all connections.
    """

    async def _one_connection() -> typing.Tuple[typing.List[float], int, int]:
        try:
            return await ws_send_messages(
                uri, messages_per_connection, connect_timeout=connect_timeout
            )
        except Exception:  # noqa
            return [], 0, 0

    results = await asyncio.gather(*[_one_connection() for _ in range(connections)])

    all_latencies: typing.List[float] = []
    total_successful = total_throttled = 0
    for latencies, successful, throttled in results:
        all_latencies.extend(latencies)
        total_successful += successful
        total_throttled += throttled

    return all_latencies, total_successful, total_throttled
