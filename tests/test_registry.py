"""Tests for the throttle rule and registry module."""

import gc
import re

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import ConfigurationError
from traffik.registry import (
    BypassThrottleRule,
    ThrottleRegistry,
    ThrottleRule,
    _prep_rules,
)
from traffik.throttles import HTTPThrottle


class _MockConnection:
    """Minimal mock connection with a scope dict for rule testing."""

    __slots__ = ("scope",)

    def __init__(self, *, method: str = "GET", path: str = "/"):
        self.scope = {
            "type": "http",
            "method": method,
            "path": path,
        }


class TestThrottleRuleInit:
    def test_no_args(self) -> None:
        rule = ThrottleRule()
        assert rule.path is None
        assert rule.methods is None
        assert rule.predicate is None

    def test_string_path_compiled_to_regex(self) -> None:
        rule = ThrottleRule(path="/api/")
        assert isinstance(rule.path, re.Pattern)
        assert rule.path.pattern == "/api/"

    def test_regex_path_stored_as_is(self) -> None:
        pattern = re.compile(r"/api/\d+")
        rule = ThrottleRule(path=pattern)
        assert rule.path is pattern

    def test_methods_stored_as_frozenset_with_both_cases(self) -> None:
        rule = ThrottleRule(methods={"GET", "POST"})
        assert isinstance(rule.methods, frozenset)
        assert "GET" in rule.methods
        assert "get" in rule.methods
        assert "POST" in rule.methods
        assert "post" in rule.methods

    def test_none_methods(self) -> None:
        rule = ThrottleRule(methods=None)
        assert rule.methods is None

    def test_predicate_stored(self) -> None:
        async def pred(conn):
            return True

        rule = ThrottleRule(predicate=pred)
        assert rule.predicate is pred

    def test_predicate_context_detection(self) -> None:
        async def pred_no_ctx(conn):
            return True

        async def pred_with_ctx(conn, ctx):
            return True

        rule1 = ThrottleRule(predicate=pred_no_ctx)
        assert rule1._predicate_takes_context is False

        rule2 = ThrottleRule(predicate=pred_with_ctx)
        assert rule2._predicate_takes_context is True

    def test_rank_is_zero(self) -> None:
        assert ThrottleRule.__rank__ == 0


class TestThrottleRuleImmutability:
    def test_cannot_reassign_path(self) -> None:
        rule = ThrottleRule(path="/api/")
        with pytest.raises(AttributeError, match="immutable"):
            rule.path = re.compile("/other/")

    def test_cannot_reassign_methods(self) -> None:
        rule = ThrottleRule(methods={"GET"})
        with pytest.raises(AttributeError, match="immutable"):
            rule.methods = frozenset(["POST"])

    def test_cannot_reassign_predicate(self) -> None:
        rule = ThrottleRule()
        with pytest.raises(AttributeError, match="immutable"):
            rule.predicate = lambda c: True  # type: ignore


class TestThrottleRuleHash:
    def test_same_args_same_hash(self) -> None:
        r1 = ThrottleRule(path="/api/", methods={"GET"})
        r2 = ThrottleRule(path="/api/", methods={"GET"})
        assert hash(r1) == hash(r2)

    def test_different_path_different_hash(self) -> None:
        r1 = ThrottleRule(path="/api/")
        r2 = ThrottleRule(path="/other/")
        assert hash(r1) != hash(r2)

    def test_different_methods_different_hash(self) -> None:
        r1 = ThrottleRule(methods={"GET"})
        r2 = ThrottleRule(methods={"POST"})
        assert hash(r1) != hash(r2)

    def test_throttle_vs_bypass_different_hash(self) -> None:
        r1 = ThrottleRule(path="/api/", methods={"GET"})
        r2 = BypassThrottleRule(path="/api/", methods={"GET"})
        assert hash(r1) != hash(r2)

    def test_same_instance_deduplicates_in_set(self) -> None:
        """Same object instance deduplicates in a set."""
        r = ThrottleRule(path="/api/", methods={"GET"})
        assert len({r, r}) == 1

    def test_distinct_instances_not_deduplicated(self) -> None:
        """Two instances with same args are distinct (identity-based equality)."""
        r1 = ThrottleRule(path="/api/", methods={"GET"})
        r2 = ThrottleRule(path="/api/", methods={"GET"})
        assert len({r1, r2}) == 2


class TestThrottleRuleCheck:
    @pytest.mark.asyncio
    async def test_no_constraints_matches_everything(self) -> None:
        rule = ThrottleRule()
        assert await rule.check(_MockConnection(method="GET", path="/anything")) is True

    @pytest.mark.asyncio
    async def test_method_match(self) -> None:
        rule = ThrottleRule(methods={"GET"})
        assert await rule.check(_MockConnection(method="GET", path="/")) is True
        assert await rule.check(_MockConnection(method="POST", path="/")) is False

    @pytest.mark.asyncio
    async def test_method_case_insensitive(self) -> None:
        rule = ThrottleRule(methods={"get"})
        assert await rule.check(_MockConnection(method="GET", path="/")) is True

    @pytest.mark.asyncio
    async def test_path_match(self) -> None:
        rule = ThrottleRule(path="/api/")
        assert await rule.check(_MockConnection(path="/api/users")) is True
        assert await rule.check(_MockConnection(path="/public/")) is False

    @pytest.mark.asyncio
    async def test_path_regex(self) -> None:
        rule = ThrottleRule(path=re.compile(r"/api/v\d+/"))
        assert await rule.check(_MockConnection(path="/api/v1/users")) is True
        assert await rule.check(_MockConnection(path="/api/vX/users")) is False

    @pytest.mark.asyncio
    async def test_path_and_method_combined(self) -> None:
        rule = ThrottleRule(path="/api/", methods={"POST"})
        assert (
            await rule.check(_MockConnection(method="POST", path="/api/data")) is True
        )
        assert (
            await rule.check(_MockConnection(method="GET", path="/api/data")) is False
        )
        assert (
            await rule.check(_MockConnection(method="POST", path="/public/")) is False
        )

    @pytest.mark.asyncio
    async def test_predicate_no_context(self) -> None:
        async def only_admin(conn):
            return conn.scope.get("is_admin", False)

        rule = ThrottleRule(predicate=only_admin)

        conn_admin = _MockConnection()
        conn_admin.scope["is_admin"] = True  # type: ignore
        assert await rule.check(conn_admin) is True

        conn_regular = _MockConnection()
        assert await rule.check(conn_regular) is False

    @pytest.mark.asyncio
    async def test_predicate_with_context(self) -> None:
        async def check_tier(conn, ctx):
            return ctx and ctx.get("tier") == "premium"

        rule = ThrottleRule(predicate=check_tier)
        conn = _MockConnection()
        assert await rule.check(conn, context={"tier": "premium"}) is True
        assert await rule.check(conn, context={"tier": "free"}) is False

    @pytest.mark.asyncio
    async def test_predicate_runs_after_path_and_method(self) -> None:
        """Predicate should not be called if path/method already fails."""
        call_count = 0

        async def counting_pred(conn):
            nonlocal call_count
            call_count += 1
            return True

        rule = ThrottleRule(path="/api/", methods={"GET"}, predicate=counting_pred)

        # Method mismatch → predicate never called
        await rule.check(_MockConnection(method="POST", path="/api/data"))
        assert call_count == 0

        # Path mismatch → predicate never called
        await rule.check(_MockConnection(method="GET", path="/public/"))
        assert call_count == 0

        # Both match → predicate called
        await rule.check(_MockConnection(method="GET", path="/api/data"))
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_websocket_no_method_in_scope(self) -> None:
        """WebSocket connections have no 'method' — method filter is skipped."""
        rule = ThrottleRule(methods={"GET"})
        conn = _MockConnection()
        del conn.scope["method"]
        # No method in scope → method check skipped → rule passes
        assert await rule.check(conn) is True


class TestBypassThrottleRuleInit:
    def test_rank_is_one(self) -> None:
        assert BypassThrottleRule.__rank__ == 1

    def test_is_subclass_of_throttle_rule(self) -> None:
        assert issubclass(BypassThrottleRule, ThrottleRule)


class TestBypassThrottleRuleCheck:
    """BypassThrottleRule inverts check: returns False when conditions match (bypass)."""

    @pytest.mark.asyncio
    async def test_no_constraints_always_bypasses(self) -> None:
        rule = BypassThrottleRule()
        assert (
            await rule.check(_MockConnection(method="GET", path="/anything")) is False
        )

    @pytest.mark.asyncio
    async def test_method_match_bypasses(self) -> None:
        rule = BypassThrottleRule(methods={"GET"})
        # GET matches → bypass (False)
        assert await rule.check(_MockConnection(method="GET", path="/")) is False
        # POST doesn't match → don't bypass (True)
        assert await rule.check(_MockConnection(method="POST", path="/")) is True

    @pytest.mark.asyncio
    async def test_path_match_bypasses(self) -> None:
        rule = BypassThrottleRule(path="/api/")
        assert await rule.check(_MockConnection(path="/api/users")) is False
        assert await rule.check(_MockConnection(path="/public/")) is True

    @pytest.mark.asyncio
    async def test_path_and_method_combined(self) -> None:
        rule = BypassThrottleRule(path="/api/", methods={"GET"})
        # Both match → bypass
        assert (
            await rule.check(_MockConnection(method="GET", path="/api/data")) is False
        )
        # Method doesn't match → don't bypass
        assert (
            await rule.check(_MockConnection(method="POST", path="/api/data")) is True
        )
        # Path doesn't match → don't bypass
        assert await rule.check(_MockConnection(method="GET", path="/public/")) is True

    @pytest.mark.asyncio
    async def test_predicate_match_bypasses(self) -> None:
        async def is_internal(conn):
            return conn.scope.get("internal", False)

        rule = BypassThrottleRule(predicate=is_internal)

        internal = _MockConnection()
        internal.scope["internal"] = True  # type: ignore
        assert await rule.check(internal) is False

        external = _MockConnection()
        assert await rule.check(external) is True

    @pytest.mark.asyncio
    async def test_predicate_with_context(self) -> None:
        async def check_ctx(conn, ctx):
            return ctx and ctx.get("bypass") is True

        rule = BypassThrottleRule(predicate=check_ctx)
        conn = _MockConnection()
        assert await rule.check(conn, context={"bypass": True}) is False
        assert await rule.check(conn, context={"bypass": False}) is True


class TestPrepRules:
    def test_empty_returns_empty_tuple(self) -> None:
        assert _prep_rules(()) == ()
        assert _prep_rules([]) == ()
        assert _prep_rules(set()) == ()

    def test_bypass_before_regular(self) -> None:
        regular = ThrottleRule(path="/a/")
        bypass = BypassThrottleRule(path="/b/")
        result = _prep_rules([regular, bypass])
        assert result == (bypass, regular)

    def test_no_predicate_before_predicate(self) -> None:
        async def pred(conn):
            return True

        with_pred = ThrottleRule(path="/a/", predicate=pred)
        without_pred = ThrottleRule(path="/b/")
        result = _prep_rules([with_pred, without_pred])
        assert result == (without_pred, with_pred)

    def test_full_sort_order(self) -> None:
        """Bypass no-pred → Regular no-pred → Bypass with-pred → Regular with-pred."""

        async def pred(conn):
            return True

        regular_no_pred = ThrottleRule(path="/r/")
        regular_with_pred = ThrottleRule(path="/rp/", predicate=pred)
        bypass_no_pred = BypassThrottleRule(path="/b/")
        bypass_with_pred = BypassThrottleRule(path="/bp/", predicate=pred)

        # Deliberately shuffled
        result = _prep_rules(
            [regular_with_pred, bypass_with_pred, regular_no_pred, bypass_no_pred]
        )
        assert result == (
            bypass_no_pred,
            regular_no_pred,
            bypass_with_pred,
            regular_with_pred,
        )

    def test_returns_tuple(self) -> None:
        result = _prep_rules([ThrottleRule()])
        assert isinstance(result, tuple)

    def test_accepts_set(self) -> None:
        r = ThrottleRule(path="/a/")
        result = _prep_rules({r})
        assert result == (r,)

    def test_single_rule_unchanged(self) -> None:
        rule = ThrottleRule(path="/x/")
        result = _prep_rules([rule])
        assert result == (rule,)


class TestThrottleRegistry:
    def test_register_and_exist(self) -> None:
        registry = ThrottleRegistry()
        assert registry.exist("foo") is False
        registry.register("foo")
        assert registry.exist("foo") is True

    def test_register_idempotent(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.register("foo")
        assert registry.exist("foo") is True

    def test_unregister(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.unregister("foo")
        assert registry.exist("foo") is False

    def test_unregister_nonexistent_is_safe(self) -> None:
        registry = ThrottleRegistry()
        registry.unregister("nonexistent")  # Should not raise

    def test_add_rules_to_registered_uid(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = ThrottleRule(path="/api/")
        registry.add_rules("foo", rule)
        assert rule in registry.get_rules("foo")

    def test_add_rules_to_unregistered_uid_raises(self) -> None:
        registry = ThrottleRegistry()
        rule = ThrottleRule(path="/api/")
        with pytest.raises(ConfigurationError, match="No throttle registered"):
            registry.add_rules("nonexistent", rule)

    def test_add_rules_empty_is_noop(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        result = registry.add_rules("foo")
        assert result is None
        assert registry.get_rules("foo") == []

    def test_get_rules_empty(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        assert registry.get_rules("foo") == []

    def test_get_rules_for_unknown_uid(self) -> None:
        registry = ThrottleRegistry()
        assert registry.get_rules("unknown") == []

    def test_add_rules_deduplicates(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = ThrottleRule(path="/api/", methods={"GET"})
        registry.add_rules("foo", rule, rule)
        assert len(registry.get_rules("foo")) == 1

    def test_add_rules_multiple_calls_accumulate(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        r1 = ThrottleRule(path="/a/")
        r2 = ThrottleRule(path="/b/")
        registry.add_rules("foo", r1)
        registry.add_rules("foo", r2)
        rules = registry.get_rules("foo")
        assert len(rules) == 2
        assert r1 in rules
        assert r2 in rules

    def test_unregister_removes_rules(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.add_rules("foo", ThrottleRule(path="/api/"))
        registry.unregister("foo")
        assert registry.get_rules("foo") == []

    def test_rules_isolated_between_uids(self) -> None:
        registry = ThrottleRegistry()
        registry.register("a")
        registry.register("b")
        rule = ThrottleRule(path="/a/")
        registry.add_rules("a", rule)
        assert rule in registry.get_rules("a")
        assert rule not in registry.get_rules("b")

    def test_get_rules_returns_list_copy(self) -> None:
        """Mutating the returned list should not affect internal state."""
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = ThrottleRule(path="/api/")
        registry.add_rules("foo", rule)
        rules = registry.get_rules("foo")
        rules.clear()
        # Internal state should be unaffected
        assert len(registry.get_rules("foo")) == 1


def _make_throttle(uid: str, registry: ThrottleRegistry) -> HTTPThrottle:
    """Create a minimal HTTPThrottle bound to the given registry."""
    return HTTPThrottle(
        uid=uid,
        rate="100/min",
        backend=InMemoryBackend(persistent=False),
        registry=registry,
    )


class TestThrottleRegistryGetThrottle:
    def test_returns_instance(self) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", registry)
        assert registry.get_throttle("t1") is throttle

    def test_returns_none_for_unknown_uid(self) -> None:
        registry = ThrottleRegistry()
        assert registry.get_throttle("unknown") is None

    def test_returns_none_after_gc(self) -> None:
        registry = ThrottleRegistry()
        _make_throttle("t-gc", registry)
        # The throttle is not held by a local variable; collect it.
        gc.collect()
        assert registry.get_throttle("t-gc") is None


class TestThrottleRegistryDisableEnable:
    @pytest.mark.asyncio
    async def test_disable_returns_true_when_found(self) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", registry)
        result = await registry.disable("t1")
        assert result is True
        del throttle

    @pytest.mark.asyncio
    async def test_disable_returns_false_when_not_found(self) -> None:
        registry = ThrottleRegistry()
        result = await registry.disable("missing")
        assert result is False

    @pytest.mark.asyncio
    async def test_disable_sets_throttle_disabled(self) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", registry)
        await registry.disable("t1")
        assert throttle.is_disabled is True

    @pytest.mark.asyncio
    async def test_enable_returns_true_when_found(self) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", registry)
        await registry.disable("t1")
        result = await registry.enable("t1")
        assert result is True
        assert throttle.is_disabled is False

    @pytest.mark.asyncio
    async def test_enable_returns_false_when_not_found(self) -> None:
        registry = ThrottleRegistry()
        result = await registry.enable("missing")
        assert result is False

    @pytest.mark.asyncio
    async def test_disable_all_disables_every_live_throttle(self) -> None:
        registry = ThrottleRegistry()
        t1 = _make_throttle("ta", registry)
        t2 = _make_throttle("tb", registry)
        await registry.disable_all()
        assert t1.is_disabled is True
        assert t2.is_disabled is True

    @pytest.mark.asyncio
    async def test_enable_all_re_enables_every_throttle(self) -> None:
        registry = ThrottleRegistry()
        t1 = _make_throttle("ta", registry)
        t2 = _make_throttle("tb", registry)
        await registry.disable_all()
        await registry.enable_all()
        assert t1.is_disabled is False
        assert t2.is_disabled is False

    @pytest.mark.asyncio
    async def test_disable_all_skips_garbage_collected_throttles(self) -> None:
        """disable_all() must not raise when a throttle has been GC'd."""
        registry = ThrottleRegistry()
        alive = _make_throttle("alive", registry)
        _make_throttle("dead", registry)
        gc.collect()
        # Should not raise even though 'dead' is gone
        await registry.disable_all()
        assert alive.is_disabled is True

    @pytest.mark.asyncio
    async def test_clear_removes_throttle_refs(self) -> None:
        registry = ThrottleRegistry()
        _make_throttle("t1", registry)
        registry.clear()
        assert registry.get_throttle("t1") is None
