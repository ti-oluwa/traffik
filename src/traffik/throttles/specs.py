"""
Throttle shorthand-string/specification parsing/resolution.

Grammar (`|`-separated segments, evaluated left to right):

    "<rate>"
    "<rate>|<strategy>"
    "<rate>|<strategy>|<type>"
    "<uid>"
    "<uid>|<rate>"
    "<uid>|<rate>|<strategy>"
    "<uid>|<rate>|<strategy>|<type>"

The first segment is tried as a `Rate` first. If that succeeds, there is no uid
in the string. If it fails, the first segment is a uid, and the second segment
(if present) must be a rate. `<type>` is `"http"` or `"ws"`, defaulting to `"http"`.
Segments are separated by `SPEC_DELIMITER` (`|`) rather than `:`, so a uid may
freely use `:` for namespacing (e.g. `"api:items"`), a common convention elsewhere
in this library. A uid must not itself contain `SPEC_DELIMITER`.

A uid-only string (no `|`) does not construct a throttle. It looks
the uid up in the registry and uses the existing throttle as-is, raising
if it isn't there.

Anything not expressible this way (a custom strategy, HTTP-only `use_method`, etc.)
needs the `Throttle` built directly.
"""

import typing
import uuid

from typing_extensions import Unpack

from traffik.exceptions import ParseError
from traffik.rates import Rate
from traffik.registry import GLOBAL_REGISTRY, ThrottleRegistry
from traffik.strategies import (
    GCRA,
    FixedWindow,
    LeakyBucket,
    LeakyBucketWithQueue,
    SlidingWindowCounter,
    SlidingWindowLog,
    TokenBucket,
)
from traffik.throttles.base import Throttle, ThrottleKwargs, ThrottleStrategy
from traffik.throttles.http import HTTPThrottle
from traffik.throttles.websocket import WebSocketThrottle
from traffik.typing import ThrottleType

__all__ = ["SPEC_DELIMITER", "resolve_specs"]


SPEC_DELIMITER = "|"
"""Character separating segments in a shorthand throttle spec string."""

STRATEGY_ALIASES: dict[str, ThrottleStrategy[typing.Any]] = {
    "fixed_window": FixedWindow(),
    "sliding_window_log": SlidingWindowLog(),
    "sliding_window_counter": SlidingWindowCounter(),
    "token_bucket": TokenBucket(),
    "leaky_bucket": LeakyBucket(),
    "leaky_bucket_with_queue": LeakyBucketWithQueue(),
    "gcra": GCRA(),
}

THROTTLE_TYPES: dict[str, type[Throttle[typing.Any]]] = {
    "http": HTTPThrottle,
    "ws": WebSocketThrottle,
}


class ParsedSpec(typing.NamedTuple):
    uid: typing.Optional[str]
    """Embedded uid from the shorthand spec, if provided."""

    rate: typing.Optional[Rate]
    """Parsed rate limit definition for the shorthand spec."""

    strategy: typing.Optional[ThrottleStrategy[typing.Any]]
    """Resolved throttling strategy for the shorthand spec."""

    type: str
    """Resolved throttle type, such as `"http"` or `"ws"`."""

    type_explicit: bool
    """Whether the type was explicitly provided in the shorthand spec."""

    lookup_only: bool
    """Whether the spec is a uid-only lookup instead of a new throttle definition."""


def parse_spec(spec: str) -> ParsedSpec:
    """
    Parse a shorthand throttle specification into a structured descriptor.

    The shorthand grammar accepts forms such as `"100/min"`,
    `"api:items|100/min"`, `"100/min|token_bucket"`, and
    `"api:items|100/min|token_bucket|ws"`. The parser attempts to interpret the
    first segment as a rate; if that fails, it treats the first segment as a
    uid and requires a rate in the second segment.

    Supported strategy aliases are resolved from `STRATEGY_ALIASES`, and
    the optional trailing segment selects the throttle type. The default type is
    `"http"` when no explicit type is supplied.

    :param spec: Shorthand throttle description to parse.
    :returns: A `ParsedSpec` describing the uid, parsed rate, strategy,
        resolved type, and whether the spec is a uid-only lookup.
    :raises ParseError: If the spec is empty, malformed, contains invalid
        strategy or type segments, or includes too many `SPEC_DELIMITER`-separated
        parts.
    """
    if not spec or not spec.strip():
        raise ParseError("Throttle spec must not be empty.")

    parts = spec.split(SPEC_DELIMITER)
    if any(not part for part in parts):
        raise ParseError(
            f"Throttle spec {spec!r} has an empty segment. Check for a "
            f"stray or doubled {SPEC_DELIMITER!r}."
        )

    uid: typing.Optional[str] = None
    try:
        rate = Rate.parse(parts[0])
        rest = parts[1:]
    except ValueError:
        uid = parts[0]
        if len(parts) == 1:
            return ParsedSpec(
                uid=uid,
                rate=None,
                strategy=None,
                type="http",
                type_explicit=False,
                lookup_only=True,
            )
        try:
            rate = Rate.parse(parts[1])
        except ValueError as exc:
            raise ParseError(
                f"Expected a rate as the second segment of {spec!r} (after "
                f"uid {uid!r}), got {parts[1]!r}."
            ) from exc
        rest = parts[2:]

    strategy: typing.Optional[ThrottleStrategy[typing.Any]] = None
    if rest:
        strategy_name = rest[0]
        try:
            strategy = STRATEGY_ALIASES[strategy_name]
        except KeyError:
            raise ParseError(
                f"Unknown strategy {strategy_name!r} in throttle spec "
                f"{spec!r}. Supported: {sorted(STRATEGY_ALIASES)}. For any "
                "other strategy, pass a pre-built `Throttle` instance "
                "instead."
            ) from None
        rest = rest[1:]

    type_ = "http"
    type_explicit = False
    if rest:
        type_candidate = rest[0]
        if type_candidate not in THROTTLE_TYPES:
            raise ParseError(
                f"Unknown throttle type {type_candidate!r} in throttle spec "
                f"{spec!r}. Supported: {sorted(THROTTLE_TYPES)}."
            )
        type_ = type_candidate
        type_explicit = True
        rest = rest[1:]

    if rest:
        raise ParseError(
            f"Too many {SPEC_DELIMITER!r}-separated segments in throttle spec "
            f"{spec!r} (expected at most 4: uid, rate, strategy, type)."
        )
    return ParsedSpec(
        uid=uid,
        rate=rate,
        strategy=strategy,
        type=type_,
        type_explicit=type_explicit,
        lookup_only=False,
    )


def generate_uid(registry: ThrottleRegistry, max_attempts: int = 10) -> str:
    """
    Generate a unique throttle uid that does not already exist in a registry.

    A fresh `uuid.uuid4` hex value is created and checked against the
    supplied registry. If a collision is found, a new candidate is generated up
    to `max_attempts` times before raising `ParseError`.

    :param registry: Registry used to verify whether the generated uid is already
        in use.
    :param max_attempts: Number of uid generation attempts before failing.
    :returns: A unique uid string that is not currently registered.
    :raises ParseError: If a unique uid cannot be generated after the allowed
        number of attempts.
    """
    for _ in range(max_attempts):
        candidate = uuid.uuid4().hex
        if not registry.exists(candidate):
            return candidate
    raise ParseError(
        f"Could not generate a unique throttle uid after {max_attempts} attempts."
    )


def resolve_specs(
    specs: typing.Sequence[typing.Union[Throttle[typing.Any], str]],
    *,
    uid: typing.Optional[str] = None,
    type: typing.Optional[ThrottleType] = None,
    **kwargs: Unpack[ThrottleKwargs],
) -> list[Throttle[typing.Any]]:
    """
    Resolve shorthand throttle specs and pre-built throttles into concrete instances.

    Each item in `specs` may be either a ready-made `Throttle` object or a
    shorthand string such as `"100/min"` or `"my_uid|100/min|token_bucket"`.
    Built throttles are returned unchanged. String specs are parsed, validated, and
    converted into an `HTTPThrottle` or `WebSocketThrottle` using the
    provided keyword arguments.

    `uid` and `type` may be supplied as defaults for shorthand specs that do not
    already embed them. If a string explicitly includes a uid or type, those values are
    checked for compatibility with the corresponding keyword arguments and cannot
    conflict with them.

    :param specs: Sequence of pre-built throttles and/or shorthand strings to resolve.
    :param uid: Optional uid to apply to shorthand specs that construct a new throttle.
    :param type: Optional explicit throttle type for shorthand specs that do not set one.
    :param kwargs: Common throttle constructor arguments shared by all throttle types,
        such as backend, identifier, headers, registry, and rules.
    :returns: A list of concrete throttle instances, preserving the order of the input.
    :raises ParseError: If a shorthand spec is malformed, has conflicting uid/type
        information, tries to use keyword arguments with a uid-only lookup, or generates
        an ambiguous uid configuration.
    """
    parsed_specs = [
        (spec, None if isinstance(spec, Throttle) else parse_spec(spec))
        for spec in specs
    ]

    needs_generated_uid = [
        spec
        for spec, parsed in parsed_specs
        if parsed is not None and parsed.uid is None and not parsed.lookup_only
    ]
    if uid is not None and len(needs_generated_uid) > 1:
        raise ParseError(
            "`uid` was given, but more than one throttle spec in this call "
            "would need it. Embed a uid in each string instead (e.g. "
            '"my_uid|100/min").'
        )

    registry = kwargs.get("registry") or GLOBAL_REGISTRY
    resolved: list[Throttle[typing.Any]] = []

    for spec, parsed in parsed_specs:
        if parsed is None:
            resolved.append(typing.cast(Throttle[typing.Any], spec))
            continue

        if parsed.lookup_only:
            # `registry` says *where* to look the uid up, so it's meaningful
            # here and exempt; every other kwarg only affects construction of
            # a new throttle, which a lookup-only spec never does.
            disallowed_kwargs = {k: v for k, v in kwargs.items() if k != "registry"}
            if disallowed_kwargs:
                raise ParseError(
                    f"Throttle spec {spec!r} looks up an existing throttle "
                    "by uid rather than constructing one, so keyword "
                    f"arguments ({sorted(disallowed_kwargs)}) have no effect "
                    "here and are rejected rather than silently ignored."
                )
            assert parsed.uid is not None
            existing = registry.get_throttle(parsed.uid)
            if existing is None:
                raise ParseError(
                    f"No throttle registered under uid {parsed.uid!r}, and "
                    f"the spec {spec!r} gave no rate to construct one."
                )
            resolved.append(existing)
            continue

        if parsed.uid is not None and uid is not None and parsed.uid != uid:
            raise ParseError(
                f"Throttle spec {spec!r} embeds uid {parsed.uid!r}, which "
                f"conflicts with the `uid={uid!r}` keyword argument."
            )
        resolved_uid = parsed.uid or uid or generate_uid(registry)

        if parsed.type_explicit and type is not None and parsed.type != type:
            raise ParseError(
                f"Throttle spec {spec!r} embeds type {parsed.type!r}, which "
                f"conflicts with the `type={type!r}` keyword argument."
            )
        resolved_type = parsed.type if parsed.type_explicit else (type or "http")

        init_kwargs: dict[str, typing.Any] = dict(kwargs)
        if parsed.strategy is not None:
            init_kwargs["strategy"] = parsed.strategy

        throttle_cls = THROTTLE_TYPES[resolved_type]
        assert parsed.rate is not None
        resolved.append(throttle_cls(uid=resolved_uid, rate=parsed.rate, **init_kwargs))
    return resolved
