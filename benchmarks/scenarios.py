"""
Benchmark scenario definitions.

`mode` determines which traffic pattern is used:

- `"sequential"`         one request after another, same connection.
- `"concurrent"`         batches of `config.concurrency` requests via
                         `asyncio.gather`.
- `"waves"`              bursts from `waves`, sleeping between them -
                         for probing window rollover. The sleeps are not
                         counted in the scenario's req/s.
- `"unique_keys_batched"` like `"concurrent"`, but each request carries a
                         distinct `X-Client-ID` cycling through
                         `key_mod` identities.
- `"unique_keys_split"`  two sequential halves of unique-keyed requests
                         with a pause between them - for key expiry and
                         slot reuse. The pause is not counted in req/s.
- `"mixed_paths"`        sequential batches against different paths (e.g.
                         a throttled route and an exempt one).
"""

import typing
from dataclasses import dataclass


@dataclass(frozen=True)
class HttpScenario:
    """
    A declarative HTTP/middleware/multiprocess scenario.

    :param key: Short CLI-facing scenario name (e.g. `"under_limit"`).
    :param name: Human-readable scenario name shown in output.
    :param rate: Throttle rate string, e.g. `"100/60s"`.
    :param total_requests: Total requests sent across the whole scenario.
    :param mode: Traffic pattern.
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
    mode: typing.Literal[
        "sequential",
        "concurrent",
        "waves",
        "unique_keys_batched",
        "unique_keys_split",
        "mixed_paths",
    ] = "sequential"
    on_error: str = "raise"
    headers: typing.Optional[dict[str, str]] = None
    waves: typing.Optional[tuple[tuple[int, float], ...]] = None
    key_header: typing.Optional[str] = None
    key_mod: typing.Optional[int] = None
    key_mod_is_concurrency: bool = False
    batch_size: typing.Optional[int] = None
    mixed_paths: typing.Optional[tuple[tuple[str, int], ...]] = None
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
    waves: typing.Optional[tuple[tuple[int, float], ...]] = None
    connections: int = 0
    messages_per_connection: int = 0


# ---------------------------------------------------------------------------
# Scenario names say what the traffic does, not how it is expected to turn out.
# Every concurrent scenario runs at `--concurrency` in-flight requests, so the
# hot-key and many-key ones differ in key distribution and nothing else:
#
#                      1 key (every request shares it)   many keys (none shared in flight)
#   under the limit    hot_key_under_limit               many_keys_under_limit
#   over the limit     hot_key_over_limit                -
#
# Comparing the two "under the limit" scenarios isolates the cost of key
# contention: same requests, same concurrency, nothing rejected in either.
# ---------------------------------------------------------------------------

HOT_KEY_HEADERS = {"X-Client-ID": "hot-key-user"}


def make_shared_scenarios(prefix: str = "") -> dict[str, HttpScenario]:
    """
    The scenarios every HTTP-shaped benchmark set (HTTP, middleware, multiprocess)
    runs.

    :param prefix: Prepended to every display name (e.g. `"Middleware "`).
    :return: The scenarios, keyed by their CLI name.
    """
    scenarios = (
        HttpScenario(
            key="under_limit",
            name=f"{prefix}Sequential, Under Limit",
            rate="200/60s",
            total_requests=80,
            mode="sequential",
        ),
        HttpScenario(
            key="at_limit",
            name=f"{prefix}Sequential, Exactly At Limit",
            rate="100/60s",
            # One past the limit: exactly 100 allowed, then exactly 1 rejected.
            total_requests=101,
            mode="sequential",
        ),
        HttpScenario(
            key="over_limit",
            name=f"{prefix}Sequential, Over Limit",
            rate="50/60s",
            total_requests=200,
            mode="sequential",
        ),
        HttpScenario(
            key="hot_key_under_limit",
            name=f"{prefix}Hot Key, Under Limit",
            rate="1000/60s",
            total_requests=800,
            mode="concurrent",
            headers=HOT_KEY_HEADERS,
        ),
        HttpScenario(
            key="hot_key_over_limit",
            name=f"{prefix}Hot Key, Over Limit",
            rate="100/60s",
            total_requests=300,
            mode="concurrent",
            headers=HOT_KEY_HEADERS,
        ),
        HttpScenario(
            key="many_keys_under_limit",
            name=f"{prefix}Many Keys, Under Limit",
            rate="1000/60s",
            total_requests=800,
            mode="unique_keys_batched",
            key_header="X-Client-ID",
            # As many keys as requests in flight, so no two concurrent requests
            # share a key.
            key_mod_is_concurrency=True,
        ),
        HttpScenario(
            key="window_rollover",
            name=f"{prefix}Window Rollover, Three Waves",
            rate="20/1s",
            total_requests=60,
            mode="waves",
            waves=((20, 1.1), (20, 1.1), (20, 0.0)),
        ),
    )
    return {scenario.key: scenario for scenario in scenarios}


HTTP_SCENARIOS: dict[str, HttpScenario] = make_shared_scenarios()

MIDDLEWARE_SCENARIOS: dict[str, HttpScenario] = {
    **make_shared_scenarios("Middleware "),
    "exempt_path": HttpScenario(
        key="exempt_path",
        name="Middleware Throttled Path vs Exempt Path",
        rate="50/60s",
        total_requests=200,
        mode="mixed_paths",
        mixed_paths=(("/test", 100), ("/unthrottled", 100)),
    ),
}

# The HTTP set again, forced onto BENCH_BACKEND=multiprocess and run under
# gunicorn's forked workers, plus two scenarios for the shared-memory backend.
MULTIPROCESS_SCENARIOS: dict[str, HttpScenario] = {
    **make_shared_scenarios("MP "),
    "many_keys_across_shards": HttpScenario(
        key="many_keys_across_shards",
        name="MP Many Keys Across Shards",
        rate="100/60s",
        total_requests=2000,
        mode="unique_keys_batched",
        key_header="X-Client-ID",
        key_mod=1000,
    ),
    "key_expiry_reuse": HttpScenario(
        key="key_expiry_reuse",
        name="MP Key Expiry and Reuse",
        # A 2s window, so every first-half key has expired during the pause.
        rate="100/2s",
        total_requests=500,
        mode="unique_keys_split",
        key_header="X-Client-ID",
        key_mod=1000,
        extra_sleep_seconds=6.0,
    ),
}

WEBSOCKET_SCENARIOS: dict[str, WebSocketScenario] = {
    "under_limit": WebSocketScenario(
        key="under_limit",
        name="WS Sequential, Under Limit",
        rate="100/60s",
        mode="sequential",
        total_messages=50,
    ),
    "over_limit": WebSocketScenario(
        key="over_limit",
        name="WS Sequential, Over Limit",
        rate="50/60s",
        mode="sequential",
        total_messages=150,
    ),
    "shared_key_connections": WebSocketScenario(
        key="shared_key_connections",
        name="WS Parallel Connections, One Shared Key",
        rate="1000/60s",
        mode="concurrent_connections",
        connections=10,
        messages_per_connection=20,
    ),
    "window_rollover": WebSocketScenario(
        key="window_rollover",
        name="WS Window Rollover, Three Waves",
        rate="10/1s",
        mode="waves",
        waves=((10, 1.1), (10, 1.1), (10, 0.0)),
    ),
}


def resolve_scenario_keys(value: str, registry: dict[str, typing.Any]) -> list[str]:
    """
    Resolve the CLI `--scenarios` selector against a scenario registry.

    :param value: `"all"` or a comma-separated list of scenario keys.
    :param registry: One of the scenario dicts in this module.
    :return: The scenario keys to run, in the order given.
    """
    if value == "all":
        return list(registry)
    return [item.strip() for item in value.split(",") if item.strip()]
