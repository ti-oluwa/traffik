"""Test suite for the throttle shorthand-spec module (`traffik.throttles.specs`)."""

from unittest.mock import patch

import pytest

from traffik.exceptions import ParseError
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles.http import HTTPThrottle
from traffik.throttles.specs import (
    SPEC_DELIMITER,
    STRATEGY_ALIASES,
    generate_uid,
    parse_spec,
    resolve_specs,
)
from traffik.throttles.websocket import WebSocketThrottle


def test_spec_delimiter_is_pipe() -> None:
    assert SPEC_DELIMITER == "|"


# parse_spec(): rate-only specs
def test_parse_rate_only() -> None:
    parsed = parse_spec("100/min")
    assert parsed.uid is None
    assert parsed.rate == Rate(limit=100, minutes=1)
    assert parsed.strategy is None
    assert parsed.type == "http"
    assert parsed.type_explicit is False
    assert parsed.lookup_only is False


def test_parse_rate_and_strategy() -> None:
    parsed = parse_spec("100/min|token_bucket")
    assert parsed.uid is None
    assert parsed.strategy is STRATEGY_ALIASES["token_bucket"]
    assert parsed.type == "http"
    assert parsed.type_explicit is False


def test_parse_rate_strategy_and_type() -> None:
    parsed = parse_spec("100/min|token_bucket|ws")
    assert parsed.strategy is STRATEGY_ALIASES["token_bucket"]
    assert parsed.type == "ws"
    assert parsed.type_explicit is True


# parse_spec(): uid-only lookup specs
def test_parse_uid_only_is_lookup() -> None:
    parsed = parse_spec("my_uid")
    assert parsed.uid == "my_uid"
    assert parsed.rate is None
    assert parsed.strategy is None
    assert parsed.lookup_only is True


def test_parse_colon_namespaced_uid_only_is_a_single_uid() -> None:
    """A colon-namespaced uid with no `|` is one segment, not two."""
    parsed = parse_spec("api:items")
    assert parsed.uid == "api:items"
    assert parsed.lookup_only is True


# parse_spec(): uid + rate specs
def test_parse_uid_and_rate() -> None:
    parsed = parse_spec("my_uid|100/min")
    assert parsed.uid == "my_uid"
    assert parsed.rate == Rate(limit=100, minutes=1)
    assert parsed.lookup_only is False


def test_parse_uid_and_rate_and_strategy() -> None:
    parsed = parse_spec("my_uid|100/min|token_bucket")
    assert parsed.uid == "my_uid"
    assert parsed.strategy is STRATEGY_ALIASES["token_bucket"]


def test_parse_uid_rate_strategy_and_type() -> None:
    parsed = parse_spec("my_uid|100/min|token_bucket|ws")
    assert parsed.uid == "my_uid"
    assert parsed.type == "ws"
    assert parsed.type_explicit is True


# parse_spec(): colon-namespaced uids
# (regression coverage for the `:` vs `|` segment-delimiter bug)
def test_parse_colon_namespaced_uid_with_rate() -> None:
    parsed = parse_spec("api:items|100/min")
    assert parsed.uid == "api:items"
    assert parsed.rate == Rate(limit=100, minutes=1)
    assert parsed.lookup_only is False


def test_parse_colon_namespaced_uid_full_spec() -> None:
    parsed = parse_spec("login_attempts:ip|20/sec|token_bucket|ws")
    assert parsed.uid == "login_attempts:ip"
    assert parsed.rate == Rate(limit=20, seconds=1)
    assert parsed.strategy is STRATEGY_ALIASES["token_bucket"]
    assert parsed.type == "ws"


def test_parse_uid_with_multiple_colons() -> None:
    parsed = parse_spec("tenant:acme:api|100/min")
    assert parsed.uid == "tenant:acme:api"


# parse_spec(): error handling
def test_parse_empty_string_raises() -> None:
    with pytest.raises(ParseError):
        parse_spec("")


def test_parse_whitespace_only_raises() -> None:
    with pytest.raises(ParseError):
        parse_spec("   ")


def test_parse_leading_delimiter_raises() -> None:
    with pytest.raises(ParseError, match="empty segment"):
        parse_spec("|100/min")


def test_parse_trailing_delimiter_raises() -> None:
    with pytest.raises(ParseError, match="empty segment"):
        parse_spec("100/min|")


def test_parse_doubled_delimiter_raises() -> None:
    with pytest.raises(ParseError, match="empty segment"):
        parse_spec("100/min||token_bucket")


def test_parse_invalid_second_segment_raises() -> None:
    with pytest.raises(ParseError, match="Expected a rate"):
        parse_spec("my_uid|not_a_rate")


def test_parse_unknown_strategy_raises() -> None:
    with pytest.raises(ParseError, match="Unknown strategy"):
        parse_spec("100/min|not_a_real_strategy")


def test_parse_unknown_type_raises() -> None:
    with pytest.raises(ParseError, match="Unknown throttle type"):
        parse_spec("100/min|token_bucket|grpc")


def test_parse_too_many_segments_raises() -> None:
    with pytest.raises(ParseError, match="Too many"):
        parse_spec("my_uid|100/min|token_bucket|ws|extra")


# generate_uid()
def test_generate_uid_returns_unregistered_uid() -> None:
    registry = ThrottleRegistry()
    uid = generate_uid(registry)
    assert isinstance(uid, str)
    assert not registry.exists(uid)


def test_generate_uid_retries_on_collision() -> None:
    registry = ThrottleRegistry()
    with patch.object(ThrottleRegistry, "exists", side_effect=[True, False]):
        uid = generate_uid(registry)
    assert isinstance(uid, str)


def test_generate_uid_raises_after_max_attempts() -> None:
    registry = ThrottleRegistry()
    with patch.object(ThrottleRegistry, "exists", return_value=True):
        with pytest.raises(ParseError, match="Could not generate"):
            generate_uid(registry, max_attempts=3)


# resolve_specs(): pre-built throttles pass through unchanged
def test_resolve_specs_passes_prebuilt_throttle_through() -> None:
    registry = ThrottleRegistry()
    throttle = HTTPThrottle(uid="prebuilt", rate="100/min", registry=registry)
    (resolved,) = resolve_specs([throttle])
    assert resolved is throttle


def test_resolve_specs_mixes_prebuilt_and_string_specs() -> None:
    registry = ThrottleRegistry()
    throttle = HTTPThrottle(uid="prebuilt", rate="100/min", registry=registry)
    resolved = resolve_specs([throttle, "100/min"], registry=ThrottleRegistry())
    assert resolved[0] is throttle
    assert isinstance(resolved[1], HTTPThrottle)


# resolve_specs(): constructing throttles from string specs
def test_resolve_specs_constructs_http_throttle_by_default() -> None:
    resolved = resolve_specs(["100/min"], registry=ThrottleRegistry())
    assert isinstance(resolved[0], HTTPThrottle)


def test_resolve_specs_constructs_websocket_throttle_for_ws_type() -> None:
    resolved = resolve_specs(["100/min|token_bucket|ws"], registry=ThrottleRegistry())
    assert isinstance(resolved[0], WebSocketThrottle)


def test_resolve_specs_uses_embedded_colon_namespaced_uid() -> None:
    """Regression test for the `:` vs `|` segment-delimiter bug."""
    resolved = resolve_specs(["api:items|100/min"], registry=ThrottleRegistry())
    assert resolved[0].uid == "api:items"
    assert resolved[0].rate == Rate(limit=100, minutes=1)


def test_resolve_specs_applies_uid_kwarg_default() -> None:
    resolved = resolve_specs(["100/min"], uid="from_kwarg", registry=ThrottleRegistry())
    assert resolved[0].uid == "from_kwarg"


def test_resolve_specs_embedded_uid_overrides_default() -> None:
    resolved = resolve_specs(["embedded|100/min"], registry=ThrottleRegistry())
    assert resolved[0].uid == "embedded"


def test_resolve_specs_conflicting_uid_kwarg_and_embedded_uid_raises() -> None:
    with pytest.raises(ParseError, match="conflicts with"):
        resolve_specs(
            ["embedded|100/min"], uid="different", registry=ThrottleRegistry()
        )


def test_resolve_specs_uid_kwarg_with_multiple_unnamed_specs_raises() -> None:
    with pytest.raises(ParseError, match="more than one"):
        resolve_specs(["100/min", "200/min"], uid="shared", registry=ThrottleRegistry())


def test_resolve_specs_uid_kwarg_only_fills_specs_without_one() -> None:
    """`uid` fills in for specs that don't embed one; a pre-built throttle
    (which already has its own uid) is unaffected and passes through.
    """
    registry = ThrottleRegistry()
    prebuilt = HTTPThrottle(uid="already_named", rate="200/min", registry=registry)
    resolved = resolve_specs([prebuilt, "100/min"], uid="shared", registry=registry)
    assert resolved[0] is prebuilt
    assert resolved[1].uid == "shared"


def test_resolve_specs_applies_type_kwarg_default() -> None:
    resolved = resolve_specs(["100/min"], type="ws", registry=ThrottleRegistry())
    assert isinstance(resolved[0], WebSocketThrottle)


def test_resolve_specs_embedded_type_matching_kwarg_succeeds() -> None:
    resolved = resolve_specs(
        ["100/min|token_bucket|ws"], type="ws", registry=ThrottleRegistry()
    )
    assert isinstance(resolved[0], WebSocketThrottle)


def test_resolve_specs_conflicting_type_kwarg_and_embedded_type_raises() -> None:
    with pytest.raises(ParseError, match="conflicts with"):
        resolve_specs(
            ["100/min|token_bucket|ws"], type="http", registry=ThrottleRegistry()
        )


def test_resolve_specs_generates_uid_when_none_given() -> None:
    resolved = resolve_specs(["100/min"], registry=ThrottleRegistry())
    assert resolved[0].uid
    assert SPEC_DELIMITER not in resolved[0].uid


def test_resolve_specs_passes_extra_kwargs_to_constructor() -> None:
    resolved = resolve_specs(["100/min"], cost=3, registry=ThrottleRegistry())
    assert resolved[0].cost == 3


# resolve_specs(): uid-only lookup specs
def test_resolve_specs_looks_up_existing_throttle_by_uid() -> None:
    registry = ThrottleRegistry()
    throttle = HTTPThrottle(uid="api:items", rate="100/min", registry=registry)
    (resolved,) = resolve_specs(["api:items"], registry=registry)
    assert resolved is throttle


def test_resolve_specs_lookup_only_allows_registry_kwarg() -> None:
    """Regression test: `registry` alone must not be rejected as a
    construction kwarg for a uid-only lookup, since it's how the caller
    says *where* to look the uid up.
    """
    registry = ThrottleRegistry()
    throttle = HTTPThrottle(uid="lookup_me", rate="100/min", registry=registry)
    (resolved,) = resolve_specs(["lookup_me"], registry=registry)
    assert resolved is throttle


def test_resolve_specs_lookup_only_rejects_other_kwargs() -> None:
    registry = ThrottleRegistry()
    HTTPThrottle(uid="lookup_me", rate="100/min", registry=registry)
    with pytest.raises(ParseError, match="have no effect"):
        resolve_specs(["lookup_me"], registry=registry, cost=2)


def test_resolve_specs_lookup_only_missing_uid_raises() -> None:
    with pytest.raises(ParseError, match="No throttle registered"):
        resolve_specs(["never_registered"], registry=ThrottleRegistry())
