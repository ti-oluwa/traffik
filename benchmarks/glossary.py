"""What each benchmark scenario actually measures, and how to read its numbers."""

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


UNDER_LIMIT = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="80 sequential requests against a limit of 200. Nothing is rejected.",
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

AT_LIMIT = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="101 sequential requests against a limit of 100: the 100th is the last allowed, the 101st is rejected.",
    read_as=(
        "Latency as in Sequential, Under Limit, plus a boundary check: the throttle must "
        "allow exactly `limit` requests and reject the next one, no more and no fewer."
    ),
    caveat="A run that straddles a window rollover can legitimately allow one extra request.",
    good="Success 99.0%, Throttled 1.0%, Errors 0%.",
)

OVER_LIMIT = ScenarioDoc(
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

HOT_KEY_UNDER = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="800 requests at --concurrency in flight, all sharing one X-Client-ID, against a limit of 1000. Nothing is rejected.",
    read_as=(
        "The cleanest single-key serialization number: req/s is how fast one key's "
        "critical section drains, with no rejection noise. Compare it with Many Keys, "
        "Under Limit, which sends the same requests at the same concurrency; the gap is "
        "the cost of sharing a key."
    ),
    caveat=(
        "Says nothing about spread load. A lower req/s with a lower P99/P99.9 than "
        "another implementation means contention is being controlled, not that it is slower."
    ),
    good="Success 100%, Errors 0%, tail latency rising gradually with --concurrency.",
)

HOT_KEY_OVER = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="300 requests at --concurrency in flight, all sharing one X-Client-ID, against a limit of 100: 100 allowed, 200 rejected.",
    read_as=(
        "Single-key contention plus the rejection path, the shape of one abusive client. "
        "Read the tail (P99, P99.9) and Throttled%, not req/s."
    ),
    caveat="Mixes serialization with rejection cost; use Hot Key, Under Limit to separate them.",
    good="Errors 0%, Throttled about 67%.",
)

MANY_KEYS_UNDER = ScenarioDoc(
    kind=Kind.THROUGHPUT,
    tests=(
        "800 requests with --concurrency in flight, each in-flight request using its own "
        "distinct X-Client-IDs (so no two in-flight requests share a key), against a limit of 1000."
    ),
    read_as=(
        "The no-contention baseline and the closest thing to capacity. Same requests, "
        "concurrency and limit as Hot Key, Under Limit, so the difference is purely key contention."
    ),
    caveat=(
        "Only --concurrency distinct keys, so it does not stress large key populations "
        "(see `scale`, or MP Many Keys Across Shards). It cannot find the saturation point "
        "by itself; use `sweep`."
    ),
    good=(
        "Success 100%, Errors 0%. On a networked backend req/s should be at or above "
        "Hot Key, Under Limit; in-process backends are CPU-bound, so the two can be close."
    ),
)

WINDOW_ROLLOVER = ScenarioDoc(
    kind=Kind.PACED,
    tests="Three waves of 20 sequential requests against 20/1s, sleeping 1.1s between waves.",
    read_as=(
        "Does each new window grant a fresh allowance? req/s excludes the sleeps but each "
        "wave is sequential, so it is a latency figure anyway. Read Success% and Throttled%."
    ),
    caveat=(
        "Strategy-dependent: a sliding window counter still weighs the previous window, "
        "so later waves are partly throttled by design. The 0.1s margin over the window "
        "is tight on a slow machine."
    ),
    good=(
        "Fixed window: Success 100%, every wave lands in a new window. Sliding window "
        "counter: a consistent, explainable throttle share."
    ),
)

MANY_KEYS_ACROSS_SHARDS = ScenarioDoc(
    kind=Kind.THROUGHPUT,
    tests="2000 requests over 1000 distinct X-Client-IDs at --concurrency in flight, against a limit of 100 per key.",
    read_as=(
        "Shard parallelism of the shared-memory backend: many keys should spread over "
        "shards and workers instead of queuing. Compare with MP Hot Key, Under Limit."
    ),
    caveat="Only meaningful with --workers > 1; with one worker there is nothing to spread across.",
    good="Success 100%, req/s clearly above the hot-key scenarios, no ShardUnavailable errors.",
)

KEY_EXPIRY_REUSE = ScenarioDoc(
    kind=Kind.BEHAVIOR,
    tests=(
        "500 sequential requests over 500 distinct keys against 100/2s, in two halves of "
        "250 with a 6s pause between, long enough for every first-half key to expire."
    ),
    read_as=(
        "Checks the backend reclaims expired slots instead of filling up. Run it with "
        "`--mp-max-keys 375`, about 1.5x the 250 keys of one half: one half fits with "
        "room for uneven hashing, both together (500) do not, so the second half only "
        "succeeds if the first half's expired slots are reclaimed."
    ),
    caveat=(
        "At the default capacity (65,536 keys) nothing is ever full, so it passes "
        "trivially. Do not size the table to exactly one half: keys hash unevenly "
        "across shards, so some would overflow even with perfect reclamation. "
        "Latency is pooled across both halves."
    ),
    good="Errors 0%, no latency step between halves, even at a tight --mp-max-keys.",
)

EXEMPT_PATH = ScenarioDoc(
    kind=Kind.BEHAVIOR,
    tests="100 requests to a throttled path then 100 to an exempt path, against a limit of 50.",
    read_as="Exempt paths must bypass the throttle: only the throttled path's overflow is rejected.",
    caveat="Latency pools both paths, so the average flatters the throttled route.",
    good="Throttled 25% (50 of 200), every exempt request allowed.",
)

WS_UNDER = ScenarioDoc(
    kind=Kind.LATENCY,
    tests="50 messages on one connection against a limit of 100.",
    read_as="Per-message round trip with no rejection; req/s is 1/latency.",
    caveat="One connection, one message in flight.",
    good="Throttled 0%, Errors 0%.",
)

WS_OVER = ScenarioDoc(
    kind=Kind.REJECTION,
    tests="150 messages on one connection against a limit of 50: 100 rejected.",
    read_as="Cost of the rejection path over a WebSocket.",
    caveat="Not a capacity number.",
    good="Throttled about 67%, Errors 0%.",
)

WS_SHARED_KEY = ScenarioDoc(
    kind=Kind.SERIALIZATION,
    tests="10 concurrent connections of 20 messages each against a limit of 1000. Every connection comes from the same client IP, so they share one key.",
    read_as="Connection fan-in onto one key, with nothing rejected: connection setup plus single-key contention.",
    caveat="Mixes connection setup with contention in one number; there is no per-connection-key variant.",
    good="Throttled 0%, Errors 0%.",
)

WS_WINDOW_ROLLOVER = ScenarioDoc(
    kind=Kind.PACED,
    tests="Three waves of 10 messages against 10/1s, sleeping 1.1s between waves.",
    read_as="Same as the HTTP Window Rollover scenario, over a WebSocket.",
    caveat="Same strategy dependence and tight timing margin as the HTTP version.",
    good="Fixed window: Success 100%.",
)

SHARED: dict[str, ScenarioDoc] = {
    "under_limit": UNDER_LIMIT,
    "at_limit": AT_LIMIT,
    "over_limit": OVER_LIMIT,
    "hot_key_under_limit": HOT_KEY_UNDER,
    "hot_key_over_limit": HOT_KEY_OVER,
    "many_keys_under_limit": MANY_KEYS_UNDER,
    "window_rollover": WINDOW_ROLLOVER,
}

DOCS: dict[Family, dict[str, ScenarioDoc]] = {
    "http": dict(SHARED),
    "middleware": {**SHARED, "exempt_path": EXEMPT_PATH},
    "multiprocess": {
        **SHARED,
        "many_keys_across_shards": MANY_KEYS_ACROSS_SHARDS,
        "key_expiry_reuse": KEY_EXPIRY_REUSE,
    },
    "websocket": {
        "under_limit": WS_UNDER,
        "over_limit": WS_OVER,
        "shared_key_connections": WS_SHARED_KEY,
        "window_rollover": WS_WINDOW_ROLLOVER,
    },
}

REGISTRIES: dict[Family, dict[str, typing.Any]] = {
    "http": HTTP_SCENARIOS,
    "middleware": MIDDLEWARE_SCENARIOS,
    "multiprocess": MULTIPROCESS_SCENARIOS,
    "websocket": WEBSOCKET_SCENARIOS,
}


def get_doc_for(family: Family, scenario_name: str) -> typing.Optional[ScenarioDoc]:
    """
    Find the documentation for a scenario by its human-readable name.

    :param family: Which scenario set the name belongs to.
    :param scenario_name: The `name` shown in the results table.
    :return: The `ScenarioDoc`, or `None` if the scenario is unknown.
    """
    for key, scenario in REGISTRIES[family].items():
        if scenario.name == scenario_name:
            return DOCS[family].get(key)
    return None


def get_kind_tag(family: typing.Optional[Family], scenario_name: str) -> str:
    """
    One-letter `Kind` tag for a scenario, or `"-"` if it has none.

    :param family: Scenario set, or `None` if unknown.
    :param scenario_name: The `name` shown in the results table.
    :return: Tag such as `"S"` or `"T"`.
    """
    if family is None:
        return "-"
    doc = get_doc_for(family, scenario_name)
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
        if (doc := get_doc_for(family, name)) is not None
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
    console.print(
        "[dim]Fixed and sliding windows are clock-aligned: a run that straddles a "
        "window boundary can allow extra requests. If Success% looks off, rerun.[/dim]"
    )
