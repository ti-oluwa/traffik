"""
Declarative scenario definitions.

`mode` determines which traffic pattern `runner.run_http_like_scenario`
uses:

- `"sequential"`         one request after another, same connection.
- `"concurrent"`         batches of `config.concurrency` requests via
                         `asyncio.gather`.
- `"waves"`              bursts from `waves`, sleeping between them -
                         for probing window-boundary behaviour.
- `"unique_keys_batched"` like `"concurrent"`, but each request carries a
                         distinct `X-Client-ID` cycling through
                         `key_mod` identities.
- `"unique_keys_split"`  two sequential halves of unique-keyed requests
                         with a pause between them - for cleanup/eviction
                         behaviour.
- `"mixed_paths"`        sequential batches against different paths (e.g.
                         a throttled route and an exempt one).
"""

import typing
from dataclasses import dataclass


@dataclass(frozen=True)
class HttpScenario:
    """
    A declarative HTTP/middleware/multiprocess scenario.

    :param key: Short CLI-facing scenario name (e.g. `"below_limit"`).
    :param name: Human-readable scenario name shown in output.
    :param rate: Throttle rate string, e.g. `"100/60s"`.
    :param total_requests: Total requests sent across the whole scenario.
    :param mode: Traffic pattern - see module docstring.
    :param on_error: `Throttle(on_error=...)` value.
    :param headers: Fixed extra headers sent with every request.
    :param waves: For `mode="waves"`: `[(count, sleep_after_seconds), ...]`.
    :param key_header: For `unique_keys_*`/`concurrent` modes: header name
        carrying a per-request identity.
    :param key_mod: Number of distinct identities to cycle through for
        `key_header`. Ignored if `key_mod_is_concurrency` is set.
    :param key_mod_is_concurrency: If set, use the run's `--concurrency`
        value as `key_mod` instead of a fixed number (some scenarios tie
        key cardinality to the configured concurrency).
    :param batch_size: Override `config.concurrency` for batch sizing in
        `unique_keys_batched` mode (independent of key cardinality).
    :param mixed_paths: For `mode="mixed_paths"`: `[(path, count), ...]`
        sent sequentially, in order.
    :param extra_sleep_seconds: For `unique_keys_split` mode: seconds to
        sleep between the two halves.
    """

    key: str
    name: str
    rate: str
    total_requests: int
    mode: str = "sequential"
    on_error: str = "raise"
    headers: typing.Optional[typing.Dict[str, str]] = None
    waves: typing.Optional[typing.Tuple[typing.Tuple[int, float], ...]] = None
    key_header: typing.Optional[str] = None
    key_mod: typing.Optional[int] = None
    key_mod_is_concurrency: bool = False
    batch_size: typing.Optional[int] = None
    mixed_paths: typing.Optional[typing.Tuple[typing.Tuple[str, int], ...]] = None
    extra_sleep_seconds: float = 0.0


@dataclass(frozen=True)
class WebSocketScenario:
    """
    A declarative WebSocket scenario.

    :param key: Short CLI-facing scenario name.
    :param name: Human-readable scenario name shown in output.
    :param rate: Throttle rate string.
    :param mode: `"sequential"` (one connection, N messages),
        `"waves"` (one connection, bursts with pauses), or
        `"concurrent_connections"` (multiple concurrent connections).
    :param total_messages: Messages sent, for `"sequential"` mode.
    :param waves: For `"waves"` mode: `[(count, sleep_after_seconds), ...]`.
    :param connections: For `"concurrent_connections"` mode: number of
        concurrent connections to open.
    :param messages_per_connection: For `"concurrent_connections"` mode.
    """

    key: str
    name: str
    rate: str
    mode: str = "sequential"
    total_messages: int = 0
    waves: typing.Optional[typing.Tuple[typing.Tuple[int, float], ...]] = None
    connections: int = 0
    messages_per_connection: int = 0


# --------------------------------------------------------------------------
# HTTP (Depends-based) scenarios - also reused, with BENCH_BACKEND=multiprocess
# and workers > 1, as the "multiprocess" command's base scenario set.
# --------------------------------------------------------------------------

HTTP_SCENARIOS: typing.Dict[str, HttpScenario] = {
    "below_limit": HttpScenario(
        key="below_limit",
        name="Below-Limit Steady State",
        rate="200/60s",
        total_requests=80,
        mode="sequential",
    ),
    "at_limit": HttpScenario(
        key="at_limit",
        name="At-Limit Edge",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
    ),
    "over_limit": HttpScenario(
        key="over_limit",
        name="Over-Limit Burst",
        rate="50/60s",
        total_requests=200,
        mode="sequential",
    ),
    "concurrent": HttpScenario(
        key="concurrent",
        name="Concurrent Contention",
        rate="100/60s",
        total_requests=500,
        mode="concurrent",
    ),
    "hot_key": HttpScenario(
        key="hot_key",
        name="Single Hot Key",
        rate="100/60s",
        total_requests=300,
        mode="concurrent",
        headers={"X-Client-ID": "hot-key-user"},
    ),
    "many_keys": HttpScenario(
        key="many_keys",
        name="Many Unique Keys",
        rate="100/60s",
        total_requests=300,
        mode="unique_keys_batched",
        key_header="X-Client-ID",
        key_mod=50,
        batch_size=10,
    ),
    "window_boundary": HttpScenario(
        key="window_boundary",
        name="Window Boundary Burst",
        rate="20/1s",
        total_requests=60,
        mode="waves",
        waves=((20, 1.1), (20, 1.1), (20, 0.0)),
    ),
    "sustained": HttpScenario(
        key="sustained",
        name="Sustained High Load",
        rate="1000/60s",
        total_requests=800,
        mode="concurrent",
    ),
    "error_recovery": HttpScenario(
        key="error_recovery",
        name="Error Recovery (on_error=allow)",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
        on_error="allow",
    ),
}


# --------------------------------------------------------------------------
# Middleware scenarios - same shape, plus "selective" (mixed throttled /
# exempt paths). Note "concurrent" here differs deliberately from the HTTP
# version: it round-robins across `--concurrency` distinct identities
# rather than hammering a single shared one.
# --------------------------------------------------------------------------

MIDDLEWARE_SCENARIOS: typing.Dict[str, HttpScenario] = {
    "below_limit": HttpScenario(
        key="below_limit",
        name="Middleware Below-Limit Steady State",
        rate="200/60s",
        total_requests=80,
        mode="sequential",
    ),
    "at_limit": HttpScenario(
        key="at_limit",
        name="Middleware At-Limit Edge",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
    ),
    "over_limit": HttpScenario(
        key="over_limit",
        name="Middleware Over-Limit Burst",
        rate="50/60s",
        total_requests=200,
        mode="sequential",
    ),
    "concurrent": HttpScenario(
        key="concurrent",
        name="Middleware Concurrent Contention",
        rate="100/60s",
        total_requests=500,
        mode="unique_keys_batched",
        key_header="X-Client-ID",
        key_mod_is_concurrency=True,
    ),
    "hot_key": HttpScenario(
        key="hot_key",
        name="Middleware Single Hot Key",
        rate="100/60s",
        total_requests=300,
        mode="concurrent",
        headers={"X-Client-ID": "hot-key-user"},
    ),
    "many_keys": HttpScenario(
        key="many_keys",
        name="Middleware Many Unique Keys",
        rate="100/60s",
        total_requests=300,
        mode="unique_keys_batched",
        key_header="X-Client-ID",
        key_mod=50,
        batch_size=10,
    ),
    "window_boundary": HttpScenario(
        key="window_boundary",
        name="Middleware Window Boundary Burst",
        rate="20/1s",
        total_requests=60,
        mode="waves",
        waves=((20, 1.1), (20, 1.1), (20, 0.0)),
    ),
    "sustained": HttpScenario(
        key="sustained",
        name="Middleware Sustained High Load",
        rate="1000/60s",
        total_requests=800,
        mode="concurrent",
    ),
    "error_recovery": HttpScenario(
        key="error_recovery",
        name="Middleware Error Recovery (on_error=allow)",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
        on_error="allow",
    ),
    "selective": HttpScenario(
        key="selective",
        name="Selective Throttling",
        rate="50/60s",
        total_requests=200,
        mode="mixed_paths",
        mixed_paths=(("/test", 100), ("/unthrottled", 100)),
    ),
}


# --------------------------------------------------------------------------
# Multiprocess scenarios - the HTTP set again (forced onto
# BENCH_BACKEND=multiprocess, run under gunicorn's forked workers), plus two
# scenarios scenarioific to the shared-memory backend's own characteristics.
# --------------------------------------------------------------------------

MULTIPROCESS_SCENARIOS: typing.Dict[str, HttpScenario] = {
    "below_limit": HttpScenario(
        key="below_limit",
        name="MP Below-Limit Steady State",
        rate="200/60s",
        total_requests=80,
        mode="sequential",
    ),
    "at_limit": HttpScenario(
        key="at_limit",
        name="MP At-Limit Edge",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
    ),
    "over_limit": HttpScenario(
        key="over_limit",
        name="MP Over-Limit Burst",
        rate="50/60s",
        total_requests=200,
        mode="sequential",
    ),
    "concurrent": HttpScenario(
        key="concurrent",
        name="MP Concurrent Contention",
        rate="100/60s",
        total_requests=500,
        mode="concurrent",
    ),
    "hot_key": HttpScenario(
        key="hot_key",
        name="MP Single Hot Key",
        rate="100/60s",
        total_requests=300,
        mode="concurrent",
        headers={"X-Client-ID": "hot-key-user"},
    ),
    "many_keys": HttpScenario(
        key="many_keys",
        name="MP Many Unique Keys",
        rate="100/60s",
        total_requests=300,
        mode="unique_keys_batched",
        key_header="X-Client-ID",
        key_mod=50,
        batch_size=10,
    ),
    "window_boundary": HttpScenario(
        key="window_boundary",
        name="MP Window Boundary Burst",
        rate="20/1s",
        total_requests=60,
        mode="waves",
        waves=((20, 1.1), (20, 1.1), (20, 0.0)),
    ),
    "sustained": HttpScenario(
        key="sustained",
        name="MP Sustained High Load",
        rate="1000/60s",
        total_requests=800,
        mode="concurrent",
    ),
    "error_recovery": HttpScenario(
        key="error_recovery",
        name="MP Error Recovery (on_error=allow)",
        rate="100/60s",
        total_requests=100,
        mode="sequential",
        on_error="allow",
    ),
    "shared_memory": HttpScenario(
        key="shared_memory",
        name="MP Shared Memory Stress",
        rate="500/60s",
        total_requests=2000,
        mode="concurrent",
    ),
    "key_eviction": HttpScenario(
        key="key_eviction",
        name="MP Key Eviction",
        rate="100/60s",
        total_requests=500,
        mode="unique_keys_split",
        key_header="X-Client-ID",
        key_mod=1000,
        extra_sleep_seconds=6.0,
    ),
}


# --------------------------------------------------------------------------
# WebSocket scenarios
# --------------------------------------------------------------------------

WEBSOCKET_SCENARIOS: typing.Dict[str, WebSocketScenario] = {
    "below_limit": WebSocketScenario(
        key="below_limit",
        name="WS Below-Limit",
        rate="100/60s",
        mode="sequential",
        total_messages=50,
    ),
    "over_limit": WebSocketScenario(
        key="over_limit",
        name="WS Over-Limit",
        rate="50/60s",
        mode="sequential",
        total_messages=150,
    ),
    "burst": WebSocketScenario(
        key="burst",
        name="WS Burst",
        rate="20/60s",
        mode="sequential",
        total_messages=100,
    ),
    "concurrent": WebSocketScenario(
        key="concurrent",
        name="WS Concurrent Connections",
        rate="100/60s",
        mode="concurrent_connections",
        connections=10,
        messages_per_connection=20,
    ),
    "window_boundary": WebSocketScenario(
        key="window_boundary",
        name="WS Window Boundary",
        rate="10/1s",
        mode="waves",
        waves=((10, 1.1), (10, 1.1), (10, 0.0)),
    ),
}
