"""
What each benchmark scenario actually measures, and how to read its numbers.

A rate limiter benchmark mixes scenarios that answer different questions, and
a single "req/s" column hides that. Every scenario here is tagged with a
`Kind` and a short explanation, printed under the results table so the numbers
are not read as something they aren't.
"""

import enum
import typing
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from benchmarks.scenarios import (
    HTTP_SCENARIOS,
    MIDDLEWARE_SCENARIOS,
    MULTIPROCESS_SCENARIOS,
    WEBSOCKET_SCENARIOS,
)

Family = typing.Literal["http", "middleware", "multiprocess", "websocket"]


class Kind(enum.Enum):
    """What a scenario's headline number really measures."""

    THROUGHPUT = "T"
    """Many independent keys, no key contention. req/s is the closest thing to capacity."""
    SERIALIZATION = "S"
    """Every request shares one key. req/s is how fast that key's critical section drains."""
    REJECTION = "R"
    """Mostly rejected requests. req/s is how cheap the 429 path is."""
    LATENCY = "L"
    """One request in flight. req/s is just 1 / latency; read the latency columns."""
    PACED = "P"
    """Contains deliberate pauses. Judge the allow/throttle split, not speed."""
    BEHAVIOR = "B"
    """Checks that a behavior holds (routing, cleanup). Not a speed test."""


KIND_LEGEND: dict[Kind, str] = {
    Kind.THROUGHPUT: "throughput: independent keys, req/s approximates capacity",
    Kind.SERIALIZATION: "serialization: one shared key, req/s = single-key drain rate",
    Kind.REJECTION: "rejection path: mostly 429s, req/s = cost of rejecting",
    Kind.LATENCY: "latency: one request in flight, req/s = 1/latency",
    Kind.PACED: "paced: deliberate pauses, read Success%/Throttled%",
    Kind.BEHAVIOR: "behavior: checks correctness, not speed",
}


@dataclass(frozen=True)
class ScenarioDoc:
    """
    Plain-language documentation for one scenario.

    :param kind: What the headline number measures.
    :param tests: What the scenario does and what it is meant to exercise.
    :param read_as: How to interpret the numbers.
    :param caveat: What the numbers cannot tell you.
    :param good: What a healthy result looks like.
    """

    kind: Kind
    tests: str
    read_as: str
    caveat: str
    good: str


_BELOW_LIMIT = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="80 sequential requests against a limit of 200. Nothing is throttled.",
    read_as=(
        "Per-request overhead of the throttle with no contention and no rejection. "
        "One request is in flight at a time, so req/s is simply 1/latency."
    ),
    caveat=(
        "Includes client, ASGI and loopback time, not just the throttle. 80 "
        "samples per iteration makes P99 roughly the single slowest request."
    ),
    good="Throttled 0%, Errors 0%, P99 within a small multiple of P50.",
)

_AT_LIMIT = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="100 sequential requests against a limit of 100: the last one lands exactly on the limit.",
    read_as="Same as Below-Limit; the extra check is that every request is still allowed at the boundary.",
    caveat="A run that straddles a window rollover can legitimately throttle a request or two.",
    good="Success 100%, Throttled 0%.",
)

_OVER_LIMIT = ScenarioDoc(
    kind=Kind.REJECTION,
    tests="200 sequential requests against a limit of 50: 50 allowed, 150 rejected.",
    read_as=(
        "Cost of the rejection path. req/s is dominated by rejected requests, so a "
        "cheaper 429 raises it without doing more useful work. Look at Throttled% and "
        "the allowed rate, not req/s alone."
    ),
    caveat="Not a capacity number. Some strategies do extra work on rejection (e.g. undoing a counter increment).",
    good="Throttled about 75%, Errors 0%, P99 close to P50.",
)

_CONCURRENT = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests=(
        "500 requests in concurrent batches against a limit of 100. No X-Client-ID is "
        "sent, so every request resolves to the same client IP, i.e. one key."
    ),
    read_as=(
        "Single-key contention. req/s is how fast one key's critical section drains, "
        "not parallel capacity. A lower req/s with a lower P99/P99.9 means contention "
        "is being controlled rather than piling up."
    ),
    caveat=(
        "Despite the name this is not a spread-load test, and about 80% of requests "
        "are rejected, so it also exercises the rejection path."
    ),
    good="Errors 0%, Throttled about 80%, P99 not far above P50.",
)

_HOT_KEY = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="300 concurrent requests, all with the same X-Client-ID, against a limit of 100.",
    read_as=(
        "Worst-case single-key contention. Compare it with Many Unique Keys to see what "
        "per-key serialization costs. Tail latency (P99, P99.9) matters more than req/s."
    ),
    caveat=(
        "In the HTTP and multiprocess sets this is effectively the same key pattern as "
        "Concurrent Contention (which shares a key via client IP); only size and limit differ."
    ),
    good="Errors 0%, Throttled about 67%, tail latency rising gradually, not steeply, with --concurrency.",
)

_MANY_KEYS = ScenarioDoc(
    kind=Kind.THROUGHPUT,
    tests="300 requests spread over 50 identities (6 each) with 10 in flight, against a limit of 100.",
    read_as=(
        "Independent keys, so locks do not queue behind each other. This is the best "
        "read on realistic multi-tenant capacity and the baseline for the hot-key cost."
    ),
    caveat="Only 10 requests are in flight (batch size 10), so it cannot find the saturation point.",
    good="Throttled 0%, req/s at or above Single Hot Key, low P99.",
)

_WINDOW_BOUNDARY = ScenarioDoc(
    kind=Kind.PACED,
    tests="Three waves of 20 sequential requests against 20/1s, sleeping 1.1s between waves.",
    read_as=(
        "Does each new window admit a fresh allowance? req/s excludes the sleeps but each "
        "wave is sequential, so it is still a latency figure. Read Success% and Throttled%."
    ),
    caveat=(
        "Strategy-dependent: fixed window should admit about 100%; sliding window "
        "counter weights the previous window and will throttle part of later waves by design. "
        "The 0.1s margin over the window is tight on a slow machine."
    ),
    good="Fixed window: Success about 100%. Sliding window: a consistent, explainable throttle share.",
)

_SUSTAINED = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="800 concurrent requests against a limit of 1000, all from the same client IP (one key).",
    read_as=(
        "The cleanest single-key serialization number: every request is allowed, so req/s "
        "is the drain rate of one key's critical section with no rejection noise."
    ),
    caveat="Still one key, so it says nothing about spread load; 800 requests is only a few batches.",
    good="Success 100%, Errors 0%. Compare req/s with Many Unique Keys to see the cost of one shared key.",
)

_ERROR_RECOVERY = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="100 sequential requests at a limit of 100 with on_error=allow.",
    read_as="No error is injected and the backend is healthy, so expect numbers close to At-Limit Edge.",
    caveat=(
        "Does not exercise the failure path at all. It only shows that on_error=allow adds "
        "no overhead when nothing goes wrong. Differences from At-Limit are noise."
    ),
    good="Close to At-Limit Edge, Errors 0%.",
)

_SHARED_MEMORY = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="2000 concurrent requests from one client IP (one key) against a limit of 500, across forked workers.",
    read_as=(
        "Hammers one shard of the shared-memory table from every worker. req/s reflects "
        "the semaphore and executor cost of the multiprocess backend on a hot key."
    ),
    caveat="Only meaningful with --workers > 1 on the multiprocess backend; about 75% of requests are rejected.",
    good="Errors 0%, no ShardUnavailable errors, tail latency stable across iterations.",
)

_KEY_EVICTION = ScenarioDoc(
    kind=Kind.BEHAVIOR,
    tests=(
        "500 sequential requests over 500 distinct keys, in two halves of 250 with a 6s "
        "pause between them."
    ),
    read_as="Checks the backend stays healthy with many live keys across a pause where background cleanup runs.",
    caveat=(
        "Latency is pooled across both halves. The limit window is 60s, longer than the 6s "
        "pause, so first-half keys generally have not expired by the second half: this does "
        "not test that expired slots are reclaimed."
    ),
    good="Errors 0%, no latency step between halves.",
)

_SELECTIVE = ScenarioDoc(
    kind=Kind.BEHAVIOR,
    tests="100 requests to a throttled path then 100 to an exempt path, against a limit of 50.",
    read_as="Exempt paths should bypass the throttle: only the throttled path's overflow is rejected.",
    caveat="Latency pools both paths, so the average flatters the throttled route.",
    good="Throttled about 25% overall (50 of 200), every exempt request allowed.",
)

_MIDDLEWARE_CONCURRENT = ScenarioDoc(
    kind=Kind.THROUGHPUT,
    tests=(
        "500 requests in batches of --concurrency, each batch spread across --concurrency "
        "distinct identities, against a limit of 100."
    ),
    read_as=(
        "No two in-flight requests share a key, so this is a no-contention scenario and a "
        "capacity read. It differs from the HTTP set's Concurrent Contention, which uses one key."
    ),
    caveat="The shared name with the HTTP scenario invites comparing unlike things.",
    good="Throttled 0% (about 10 requests per key), P99 close to P50.",
)

_WS_BELOW = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="50 messages on one connection against a limit of 100.",
    read_as="Per-message round trip with no rejection; req/s is 1/latency.",
    caveat="One connection, one message in flight.",
    good="Throttled 0%, Errors 0%.",
)

_WS_OVER = ScenarioDoc(
    kind=Kind.REJECTION,
    tests="150 messages on one connection against a limit of 50.",
    read_as="Cost of the rejection path over a WebSocket; 100 of 150 are rejected.",
    caveat="Not a capacity number.",
    good="Throttled about 67%, Errors 0%.",
)

_WS_BURST = ScenarioDoc(
    kind=Kind.REJECTION,
    tests="100 messages on one connection against a limit of 20.",
    read_as="Rejection path at a tighter limit. About 80% are rejected.",
    caveat="Messages are sent sequentially, so despite the name it is not a true burst.",
    good="Throttled about 80%, Errors 0%.",
)

_WS_CONCURRENT = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="10 concurrent connections of 20 messages each against a limit of 100.",
    read_as=(
        "Connection fan-in. All connections come from the same client IP, so they share one "
        "key and about half the messages are rejected."
    ),
    caveat="Mixes connection setup, single-key contention and rejection in one number.",
    good="Throttled about 50%, Errors 0%.",
)

_WS_WINDOW = ScenarioDoc(
    kind=Kind.PACED,
    tests="Three waves of 10 messages against 10/1s, sleeping 1.1s between waves.",
    read_as="Same as the HTTP Window Boundary scenario, over a WebSocket.",
    caveat="Same strategy dependence and tight timing margin as the HTTP version.",
    good="Fixed window: Success about 100%.",
)

_SHARED: dict[str, ScenarioDoc] = {
    "below_limit": _BELOW_LIMIT,
    "at_limit": _AT_LIMIT,
    "over_limit": _OVER_LIMIT,
    "concurrent": _CONCURRENT,
    "hot_key": _HOT_KEY,
    "many_keys": _MANY_KEYS,
    "window_boundary": _WINDOW_BOUNDARY,
    "sustained": _SUSTAINED,
    "error_recovery": _ERROR_RECOVERY,
}

DOCS: dict[Family, dict[str, ScenarioDoc]] = {
    "http": dict(_SHARED),
    "middleware": {
        **_SHARED,
        "concurrent": _MIDDLEWARE_CONCURRENT,
        "selective": _SELECTIVE,
    },
    "multiprocess": {
        **_SHARED,
        "shared_memory": _SHARED_MEMORY,
        "key_eviction": _KEY_EVICTION,
    },
    "websocket": {
        "below_limit": _WS_BELOW,
        "over_limit": _WS_OVER,
        "burst": _WS_BURST,
        "concurrent": _WS_CONCURRENT,
        "window_boundary": _WS_WINDOW,
    },
}

_REGISTRIES: dict[Family, dict[str, typing.Any]] = {
    "http": HTTP_SCENARIOS,
    "middleware": MIDDLEWARE_SCENARIOS,
    "multiprocess": MULTIPROCESS_SCENARIOS,
    "websocket": WEBSOCKET_SCENARIOS,
}


def doc_for(family: Family, scenario_name: str) -> typing.Optional[ScenarioDoc]:
    """
    Find the documentation for a scenario by its human-readable name.

    :param family: Which scenario set the name belongs to.
    :param scenario_name: The `name` shown in the results table.
    :return: The `ScenarioDoc`, or `None` if the scenario is unknown.
    """
    for key, scenario in _REGISTRIES[family].items():
        if scenario.name == scenario_name:
            return DOCS[family].get(key)
    return None


def kind_tag(family: typing.Optional[Family], scenario_name: str) -> str:
    """
    One-letter `Kind` tag for a scenario, or `"-"` if it has none.

    :param family: Scenario set, or `None` if unknown.
    :param scenario_name: The `name` shown in the results table.
    :return: Tag such as `"S"` or `"T"`.
    """
    if family is None:
        return "-"
    doc = doc_for(family, scenario_name)
    return doc.kind.value if doc else "-"


def print_glossary(scenario_names: typing.Iterable[str], family: Family) -> None:
    """
    Print a glossary for the scenarios that ran, under the results table.

    :param scenario_names: Names of the scenarios in the results.
    :param family: Which scenario set they belong to.
    """
    docs = [
        (name, doc)
        for name in dict.fromkeys(scenario_names)
        if (doc := doc_for(family, name)) is not None
    ]
    if not docs:
        return

    console = Console()
    table = Table(
        title="Scenario glossary: what each number means",
        show_lines=True,
        expand=True,
    )
    table.add_column("Scenario", ratio=2)
    table.add_column("Tests", ratio=4)
    table.add_column("Read it as", ratio=5)
    table.add_column("Caveat", ratio=4)
    table.add_column("Good looks like", ratio=3)
    for name, doc in docs:
        table.add_row(
            f"[bold]{doc.kind.value}[/bold]  {name}",
            doc.tests,
            doc.read_as,
            doc.caveat,
            doc.good,
        )
    console.print(table)

    used = {doc.kind for _, doc in docs}
    legend = " | ".join(
        f"[bold]{kind.value}[/bold] {text}"
        for kind, text in KIND_LEGEND.items()
        if kind in used
    )
    console.print(f"\n[dim]Type column: {legend}[/dim]")
    console.print(
        "[dim]req/s excludes intentional pauses. P99.9 is shown only with "
        "1,000+ latency samples; below that it is just the maximum.[/dim]"
    )
