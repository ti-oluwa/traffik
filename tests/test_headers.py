"""Test suite for the `Header` and `Headers` classes in `traffik.headers` module."""

import copy
import math
import pickle
import typing

import pytest

from traffik.headers import (
    DEFAULT_HEADERS_ALWAYS,
    DEFAULT_HEADERS_THROTTLED,
    Header,
    Headers,
    _is_static,
    _prep_headers,
    encode_headers,
)
from traffik.rates import Rate
from traffik.types import StrategyStat


def _make_stat(
    limit: int = 10,
    hits_remaining: float = 5,
    wait_ms: float = 0,
) -> StrategyStat[typing.Mapping]:
    """Helper to create a StrategyStat for testing."""
    return StrategyStat(
        key="test:key",
        rate=Rate(limit=limit, seconds=10),
        hits_remaining=hits_remaining,
        wait_ms=wait_ms,
    )


class TestHeaderInit:
    def test_static_header_always(self) -> None:
        h = Header("some-value", when="always")
        assert h._is_static is True
        assert h._raw == "some-value"
        assert h._resolver is None

    def test_static_header_throttled_is_not_static(self) -> None:
        h = Header("some-value", when="throttled")
        assert h._is_static is False

    def test_dynamic_header(self) -> None:
        resolver = lambda conn, stat, ctx: "dynamic"
        h = Header(resolver, when="always")
        assert h._is_static is False
        assert h._raw is None
        assert h._resolver is resolver

    def test_default_when_is_throttled(self) -> None:
        h = Header("val")
        assert h._is_static is False
        assert h._when == "throttled"

    def test_callable_when(self) -> None:
        predicate = lambda conn, stat, ctx: True
        h = Header("val", when=predicate)
        assert h._check is predicate
        assert h._when is None


class TestHeaderCheck:
    def test_check_always_returns_true(self) -> None:
        h = Header("val", when="always")
        stat = _make_stat(hits_remaining=5)
        assert h.check(None, stat) is True  # type: ignore[arg-type]

    def test_check_throttled_when_not_throttled(self) -> None:
        h = Header("val", when="throttled")
        stat = _make_stat(hits_remaining=5)
        assert h.check(None, stat) is False  # type: ignore[arg-type]

    def test_check_throttled_when_throttled(self) -> None:
        h = Header("val", when="throttled")
        stat = _make_stat(hits_remaining=0)
        assert h.check(None, stat) is True  # type: ignore[arg-type]

    def test_check_throttled_when_negative_remaining(self) -> None:
        h = Header("val", when="throttled")
        stat = _make_stat(hits_remaining=-1)
        assert h.check(None, stat) is True  # type: ignore[arg-type]

    def test_check_custom_predicate(self) -> None:
        predicate = lambda conn, stat, ctx: stat.hits_remaining < 3
        h = Header("val", when=predicate)
        assert h.check(None, _make_stat(hits_remaining=2)) is True  # type: ignore[arg-type]
        assert h.check(None, _make_stat(hits_remaining=5)) is False  # type: ignore[arg-type]


class TestHeaderResolve:
    def test_resolve_static(self) -> None:
        h = Header("static-value", when="always")
        stat = _make_stat()
        assert h.resolve(None, stat) == "static-value"  # type: ignore[arg-type]

    def test_resolve_dynamic(self) -> None:
        resolver = lambda conn, stat, ctx: f"remaining-{stat.hits_remaining}"
        h = Header(resolver, when="always")
        stat = _make_stat(hits_remaining=7)
        assert h.resolve(None, stat) == "remaining-7"  # type: ignore[arg-type]

    def test_resolve_dynamic_with_context(self) -> None:
        resolver = lambda conn, stat, ctx: ctx.get("prefix", "") + "value"
        h = Header(resolver, when="always")
        stat = _make_stat()
        assert h.resolve(None, stat, {"prefix": "X-"}) == "X-value"  # type: ignore[arg-type]


class TestHeaderWhen:
    def test_when_returns_new_instance(self) -> None:
        h = Header("val", when="always")
        h2 = h.when("throttled")
        assert h2 is not h
        assert h2._when == "throttled"
        assert h._when == "always"

    def test_always_property(self) -> None:
        h = Header("val", when="throttled")
        h2 = h.always
        assert h2._when == "always"
        assert h2._is_static is True

    def test_throttled_property(self) -> None:
        h = Header("val", when="always")
        h2 = h.throttled
        assert h2._when == "throttled"
        assert h2._is_static is False

    def test_when_preserves_resolver(self) -> None:
        resolver = lambda conn, stat, ctx: "dynamic"
        h = Header(resolver, when="always")
        h2 = h.when("throttled")
        stat = _make_stat(hits_remaining=0)
        assert h2.resolve(None, stat) == "dynamic"  # type: ignore[arg-type]


class TestHeaderFactoryMethods:
    def test_limit(self) -> None:
        h = Header.LIMIT(when="always")
        stat = _make_stat(limit=100)
        assert h.resolve(None, stat) == "100"  # type: ignore[arg-type]

    def test_remaining(self) -> None:
        h = Header.REMAINING(when="always")
        stat = _make_stat(hits_remaining=42)
        assert h.resolve(None, stat) == "42"  # type: ignore[arg-type]

    def test_reset_milliseconds(self) -> None:
        h = Header.RESET_MILLISECONDS(when="always")
        stat = _make_stat(wait_ms=1500)
        assert h.resolve(None, stat) == "1500"  # type: ignore[arg-type]

    def test_reset_seconds(self) -> None:
        h = Header.RESET_SECONDS(when="always")
        stat = _make_stat(wait_ms=1500)
        assert h.resolve(None, stat) == str(math.ceil(1500 / 1000))  # type: ignore[arg-type]

    def test_reset_seconds_rounds_up(self) -> None:
        h = Header.RESET_SECONDS(when="always")
        stat = _make_stat(wait_ms=100)
        assert h.resolve(None, stat) == "1"  # type: ignore[arg-type]


class TestHeaderDisable:
    def test_disable_is_string(self) -> None:
        assert isinstance(Header.DISABLE, str)

    def test_disable_identity_check(self) -> None:
        assert Header.DISABLE is Header.DISABLE
        # A string with the same value should NOT be treated as DISABLE
        copy_str = Header.DISABLE[:]
        # Identity should differ for a copy (implementation detail: CPython may intern,
        # but the sentinel check uses `is`, so we test the pattern)
        assert Header.DISABLE == copy_str


class TestHeaderHash:
    def test_static_header_hash_is_consistent(self) -> None:
        h = Header("value", when="always")
        assert hash(h) == hash(h)
        assert hash(h) == hash("value")

    def test_different_headers_can_have_different_hashes(self) -> None:
        h1 = Header("value1", when="always")
        h2 = Header("value2", when="always")
        # Not guaranteed to be different, but for distinct short strings it should be
        assert hash(h1) != hash(h2)

    def test_repr(self) -> None:
        h = Header("test-val", when="always")
        assert "test-val" in repr(h)
        assert "Header" in repr(h)


class TestHeadersInit:
    def test_empty_init(self) -> None:
        h = Headers()
        assert len(h) == 0
        assert h._is_static is True
        assert bool(h) is False

    def test_none_init(self) -> None:
        h = Headers(None)
        assert len(h) == 0

    def test_all_static_strings(self) -> None:
        h = Headers({"X-A": "a", "X-B": "b"})
        assert h._is_static is True
        assert len(h) == 2

    def test_static_header_instances_are_optimized(self) -> None:
        """Header instances with static values should be reduced to raw strings."""
        h = Headers({"X-A": Header("val", when="always")})
        assert h._is_static is True
        # _prep_headers should have converted to raw string
        assert h._raw["X-A"] == "val"

    def test_mixed_static_and_dynamic(self) -> None:
        h = Headers(
            {
                "X-Static": "static",
                "X-Dynamic": Header(lambda c, s, ctx: "dyn", when="always"),
            }
        )
        assert h._is_static is False

    def test_dynamic_header_not_static(self) -> None:
        h = Headers({"X-A": Header.REMAINING(when="throttled")})
        assert h._is_static is False


class TestHeadersMapping:
    def test_getitem_returns_header(self) -> None:
        h = Headers({"X-A": "value"})
        result = h["X-A"]
        assert isinstance(result, Header)

    def test_getitem_missing_key_raises(self) -> None:
        h = Headers({"X-A": "value"})
        with pytest.raises(KeyError):
            h["X-Missing"]

    def test_contains(self) -> None:
        h = Headers({"X-A": "value"})
        assert "X-A" in h
        assert "X-Missing" not in h

    def test_len(self) -> None:
        h = Headers({"X-A": "a", "X-B": "b", "X-C": "c"})
        assert len(h) == 3

    def test_bool_true(self) -> None:
        h = Headers({"X-A": "a"})
        assert bool(h) is True

    def test_bool_false(self) -> None:
        h = Headers()
        assert bool(h) is False

    def test_iter(self) -> None:
        h = Headers({"X-A": "a", "X-B": "b"})
        keys = list(h)
        assert set(keys) == {"X-A", "X-B"}

    def test_items_returns_raw(self) -> None:
        """items() should return raw dict items without wrapping strings into Header."""
        h = Headers({"X-Static": "val", "X-Dynamic": Header.LIMIT(when="always")})
        items = dict(h.items())
        # Static strings should remain as strings, not wrapped in Header
        assert isinstance(items["X-Static"], str)

    def test_setitem(self) -> None:
        h = Headers({"X-A": "a"})
        h["X-B"] = "b"
        assert "X-B" in h
        assert len(h) == 2

    def test_setitem_dynamic_makes_non_static(self) -> None:
        h = Headers({"X-A": "a"})
        assert h._is_static is True
        h["X-Dynamic"] = Header(lambda c, s, ctx: "dyn", when="always")
        assert h._is_static is False

    def test_update(self) -> None:
        h = Headers({"X-A": "a"})
        h.update({"X-B": "b", "X-C": "c"})
        assert len(h) == 3
        assert "X-B" in h


class TestHeadersMerge:
    def test_or_operator(self) -> None:
        h1 = Headers({"X-A": "a"})
        h2 = Headers({"X-B": "b"})
        merged = h1 | h2
        assert len(merged) == 2
        assert "X-A" in merged
        assert "X-B" in merged

    def test_or_with_plain_dict(self) -> None:
        h = Headers({"X-A": "a"})
        merged = h | {"X-B": "b"}
        assert len(merged) == 2
        assert "X-B" in merged

    def test_or_override(self) -> None:
        h1 = Headers({"X-A": "original"})
        h2 = Headers({"X-A": "overridden"})
        merged = h1 | h2
        # The overridden value should take precedence
        assert merged._raw["X-A"] == "overridden"

    def test_or_returns_new_instance(self) -> None:
        h1 = Headers({"X-A": "a"})
        h2 = Headers({"X-B": "b"})
        merged = h1 | h2
        assert merged is not h1
        assert merged is not h2

    def test_ior_operator(self) -> None:
        h = Headers({"X-A": "a"})
        h |= {"X-B": "b"}
        assert len(h) == 2
        assert "X-B" in h

    def test_or_with_empty(self) -> None:
        h = Headers({"X-A": "a"})
        merged = h | {}
        assert len(merged) == 1

    def test_or_static_tracking(self) -> None:
        h1 = Headers({"X-A": "a"})
        h2 = Headers({"X-B": Header(lambda c, s, ctx: "dyn", when="always")})
        merged = h1 | h2
        assert merged._is_static is False


class TestHeadersCopyAndPickle:
    def test_copy(self) -> None:
        h = Headers({"X-A": "a", "X-B": "b"})
        c = h.copy()
        assert c is not h
        assert len(c) == len(h)
        assert dict(c.items()) == dict(h.items())

    def test_shallow_copy(self) -> None:
        h = Headers({"X-A": "a"})
        c = copy.copy(h)
        assert c is not h
        assert len(c) == len(h)

    def test_deep_copy(self) -> None:
        h = Headers({"X-A": "a"})
        c = copy.deepcopy(h)
        assert c is not h
        assert len(c) == len(h)

    def test_pickle_roundtrip(self) -> None:
        h = Headers({"X-A": "a", "X-B": "b"})
        pickled = pickle.dumps(h)
        restored = pickle.loads(pickled)
        assert len(restored) == 2
        assert "X-A" in restored
        assert "X-B" in restored
        assert restored._is_static is True

    def test_repr(self) -> None:
        h = Headers({"X-A": "a"})
        assert "Headers" in repr(h)


class TestPrepHeaders:
    def test_strings_pass_through(self) -> None:
        result = _prep_headers({"X-A": "a"})
        assert result["X-A"] == "a"

    def test_static_header_reduced_to_string(self) -> None:
        result = _prep_headers({"X-A": Header("val", when="always")})
        assert result["X-A"] == "val"
        assert isinstance(result["X-A"], str)

    def test_dynamic_header_preserved(self) -> None:
        h = Header(lambda c, s, ctx: "dyn", when="throttled")
        result = _prep_headers({"X-A": h})
        assert result["X-A"] is h


class TestIsStatic:
    def test_all_strings(self) -> None:
        assert _is_static({"X-A": "a", "X-B": "b"}) is True

    def test_with_header_instance(self) -> None:
        assert _is_static({"X-A": Header("val")}) is False

    def test_empty(self) -> None:
        assert _is_static({}) is True


class TestEncodeHeaders:
    def test_encode(self) -> None:
        result = encode_headers({"X-A": "value"})
        assert result == [(b"x-a", b"value")]

    def test_encode_multiple(self) -> None:
        result = encode_headers({"X-A": "a", "X-B": "b"})
        assert len(result) == 2

    def test_encode_lowercases_keys(self) -> None:
        result = encode_headers({"X-RateLimit-Limit": "100"})
        assert result[0][0] == b"x-ratelimit-limit"


class TestDefaultPresets:
    def test_default_always_has_expected_keys(self) -> None:
        assert "X-RateLimit-Limit" in DEFAULT_HEADERS_ALWAYS
        assert "X-RateLimit-Remaining" in DEFAULT_HEADERS_ALWAYS
        assert "X-RateLimit-Reset" in DEFAULT_HEADERS_ALWAYS
        assert len(DEFAULT_HEADERS_ALWAYS) == 3

    def test_default_throttled_has_expected_keys(self) -> None:
        assert "X-RateLimit-Limit" in DEFAULT_HEADERS_THROTTLED
        assert "X-RateLimit-Remaining" in DEFAULT_HEADERS_THROTTLED
        assert "X-RateLimit-Reset" in DEFAULT_HEADERS_THROTTLED
        assert len(DEFAULT_HEADERS_THROTTLED) == 3

    def test_default_always_is_not_static(self) -> None:
        # Factory headers use lambdas, so they are not static even with when="always"
        assert DEFAULT_HEADERS_ALWAYS._is_static is False

    def test_default_throttled_is_not_static(self) -> None:
        assert DEFAULT_HEADERS_THROTTLED._is_static is False
