"""Tests for the throttle rule and registry module."""

import gc
import re
import typing

import pytest
from starlette.requests import HTTPConnection

from tests.utils import ThrottleT, make_connection, requires_throttle_type
from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import ConfigurationError
from traffik.registry import (
    BypassThrottleRule,
    ThrottleRegistry,
    ThrottleRule,
    _prep_rules,
)
from traffik.throttles import Throttle

RULE_TYPES = [ThrottleRule, BypassThrottleRule]


@pytest.mark.parametrize("rule_type", RULE_TYPES)
class TestThrottleRuleBasic:
    def test_no_args(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type()
        assert rule.path is None
        assert rule.methods is None
        assert rule.predicate is None

    def test_string_path_compiled_to_regex(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type(path="/api/")
        assert isinstance(rule.path, re.Pattern)
        assert rule.path.pattern == "/api/"

    def test_regex_path_stored_as_is(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        pattern = re.compile(r"/api/\d+")
        rule = rule_type(path=pattern)
        assert rule.path is pattern

    def test_methods_stored_as_frozenset_with_both_cases(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type(methods={"GET", "POST"})
        assert isinstance(rule.methods, frozenset)
        assert "GET" in rule.methods
        assert "get" in rule.methods
        assert "POST" in rule.methods
        assert "post" in rule.methods

    def test_none_methods(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type(methods=None)
        assert rule.methods is None

    def test_predicate_stored(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        async def predicate(connection):
            return True

        rule = rule_type(predicate=predicate)
        assert rule.predicate is predicate

    def test_predicate_context_detection(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        async def predicate_no_ctx(connection):
            return True

        async def predicate_with_ctx(connection, ctx):
            return True

        rule1 = rule_type(predicate=predicate_no_ctx)
        assert rule1._predicate_takes_context is False

        rule2 = rule_type(predicate=predicate_with_ctx)
        assert rule2._predicate_takes_context is True

    # TestThrottleRuleImmutability:
    def test_cannot_reassign_path(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type(path="/api/")
        with pytest.raises(AttributeError, match="immutable"):
            rule.path = re.compile("/other/")

    def test_cannot_reassign_methods(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type(methods={"GET"})
        with pytest.raises(AttributeError, match="immutable"):
            rule.methods = frozenset(["POST"])

    def test_cannot_reassign_predicate(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        rule = rule_type()
        with pytest.raises(AttributeError, match="immutable"):
            rule.predicate = lambda c: True  # type: ignore

    def test_same_args_same_hash(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        r1 = rule_type(path="/api/", methods={"GET"})
        r2 = rule_type(path="/api/", methods={"GET"})
        assert hash(r1) == hash(r2)

    def test_different_path_different_hash(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        r1 = rule_type(path="/api/")
        r2 = rule_type(path="/other/")
        assert hash(r1) != hash(r2)

    def test_different_methods_different_hash(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        r1 = rule_type(methods={"GET"})
        r2 = rule_type(methods={"POST"})
        assert hash(r1) != hash(r2)

    def test_same_instance_deduplicates_in_set(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        """Same object instance deduplicates in a set."""
        r = rule_type(path="/api/", methods={"GET"})
        assert len({r, r}) == 1

    def test_distinct_instances_not_deduplicated(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        """Two instances with same args are distinct (identity-based equality)."""
        r1 = rule_type(path="/api/", methods={"GET"})
        r2 = rule_type(path="/api/", methods={"GET"})
        assert len({r1, r2}) == 2


@pytest.mark.anyio
class TestBaseThrottleRuleCheck:
    async def test_no_constraints_matches_everything(self) -> None:
        rule = ThrottleRule()
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="GET", path="/anything")
            )
            is True
        )

    async def test_method_match(self) -> None:
        rule = ThrottleRule(methods={"GET"})
        assert (
            await rule.check(make_connection(HTTPConnection, method="GET", path="/"))
            is True
        )
        assert (
            await rule.check(make_connection(HTTPConnection, method="POST", path="/"))
            is False
        )

    async def test_method_case_insensitive(self) -> None:
        rule = ThrottleRule(methods={"get"})
        assert (
            await rule.check(make_connection(HTTPConnection, method="GET", path="/"))
            is True
        )

    async def test_path_match(self) -> None:
        rule = ThrottleRule(path="/api/")
        assert (
            await rule.check(make_connection(HTTPConnection, path="/api/users")) is True
        )
        assert (
            await rule.check(make_connection(HTTPConnection, path="/public/")) is False
        )

    async def test_path_regex(self) -> None:
        rule = ThrottleRule(path=re.compile(r"/api/v\d+/"))
        assert (
            await rule.check(make_connection(HTTPConnection, path="/api/v1/users"))
            is True
        )
        assert (
            await rule.check(make_connection(HTTPConnection, path="/api/vX/users"))
            is False
        )

    async def test_path_and_method_combined(self) -> None:
        rule = ThrottleRule(path="/api/", methods={"POST"})
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="POST", path="/api/data")
            )
            is True
        )
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="GET", path="/api/data")
            )
            is False
        )
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="POST", path="/public/")
            )
            is False
        )

    async def test_predicate_no_context(self) -> None:
        async def only_admin(connection):
            return connection.scope.get("is_admin", False)

        rule = ThrottleRule(predicate=only_admin)

        admin_connection = make_connection(HTTPConnection, is_admin=True)
        assert await rule.check(admin_connection) is True

        regular_connection = make_connection(HTTPConnection)
        assert await rule.check(regular_connection) is False

    async def test_predicate_with_context(self) -> None:
        async def check_tier(connection, ctx):
            return ctx and ctx.get("tier") == "premium"

        rule = ThrottleRule(predicate=check_tier)
        connection = make_connection(HTTPConnection)
        assert await rule.check(connection, context={"tier": "premium"}) is True
        assert await rule.check(connection, context={"tier": "free"}) is False

    async def test_predicate_runs_after_path_and_method(self) -> None:
        """Predicate should not be called if path/method already fails."""
        call_count = 0

        async def counting_predicate(connection):
            nonlocal call_count
            call_count += 1
            return True

        rule = ThrottleRule(
            path="/api/",
            methods={"GET"},
            predicate=counting_predicate,
        )

        # Method mismatch -> predicate never called
        await rule.check(
            make_connection(HTTPConnection, method="POST", path="/api/data")
        )
        assert call_count == 0

        # Path mismatch -> predicate never called
        await rule.check(make_connection(HTTPConnection, method="GET", path="/public/"))
        assert call_count == 0

        # Both match -> predicate called
        await rule.check(
            make_connection(HTTPConnection, method="GET", path="/api/data")
        )
        assert call_count == 1

    async def test_websocket_no_method_in_scope(self) -> None:
        """WebSocket connections have no 'method' — method filter is skipped."""
        rule = ThrottleRule(methods={"GET"})
        connection = make_connection(HTTPConnection)
        del connection.scope["method"]
        # No method in scope -> method check skipped -> rule passes
        assert await rule.check(connection) is True


@pytest.mark.anyio
class TestBypassThrottleRuleCheck:
    """BypassThrottleRule inverts check: returns False when conditions match (bypass)."""

    async def test_no_constraints_always_bypasses(self) -> None:
        rule = BypassThrottleRule()
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="GET", path="/anything")
            )
            is False
        )

    async def test_method_match_bypasses(self) -> None:
        rule = BypassThrottleRule(methods={"GET"})
        # GET matches -> bypass (False)
        assert (
            await rule.check(make_connection(HTTPConnection, method="GET", path="/"))
            is False
        )
        # POST doesn't match -> don't bypass (True)
        assert (
            await rule.check(make_connection(HTTPConnection, method="POST", path="/"))
            is True
        )

    async def test_path_match_bypasses(self) -> None:
        rule = BypassThrottleRule(path="/api/")
        assert (
            await rule.check(make_connection(HTTPConnection, path="/api/users"))
            is False
        )
        assert (
            await rule.check(make_connection(HTTPConnection, path="/public/")) is True
        )

    async def test_path_and_method_combined(self) -> None:
        rule = BypassThrottleRule(path="/api/", methods={"GET"})
        # Both match -> bypass
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="GET", path="/api/data")
            )
            is False
        )
        # Method doesn't match -> don't bypass
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="POST", path="/api/data")
            )
            is True
        )
        # Path doesn't match -> don't bypass
        assert (
            await rule.check(
                make_connection(HTTPConnection, method="GET", path="/public/")
            )
            is True
        )

    async def test_predicate_match_bypasses(self) -> None:
        async def is_internal(connection):
            return connection.scope.get("internal", False)

        rule = BypassThrottleRule(predicate=is_internal)

        internal = make_connection(HTTPConnection)
        internal.scope["internal"] = True  # type: ignore
        assert await rule.check(internal) is False

        external = make_connection(HTTPConnection)
        assert await rule.check(external) is True

    async def test_predicate_with_context(self) -> None:
        async def check_ctx(connection, ctx):
            return ctx and ctx.get("bypass") is True

        rule = BypassThrottleRule(predicate=check_ctx)
        connection = make_connection(HTTPConnection)
        assert await rule.check(connection, context={"bypass": True}) is False
        assert await rule.check(connection, context={"bypass": False}) is True


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

    def test_no_predicateicate_before_predicate(self) -> None:
        async def predicate(connection):
            return True

        with_pred = ThrottleRule(path="/a/", predicate=predicate)
        without_pred = ThrottleRule(path="/b/")
        result = _prep_rules([with_pred, without_pred])
        assert result == (without_pred, with_pred)

    def test_full_sort_order(self) -> None:
        """Bypass no-predicate -> Regular no-predicate -> Bypass with-predicate -> Regular with-predicate."""

        async def predicate(connection):
            return True

        regular_no_predicate = ThrottleRule(path="/r/")
        regular_with_predicate = ThrottleRule(path="/rp/", predicate=predicate)
        bypass_no_predicate = BypassThrottleRule(path="/b/")
        bypass_with_predicate = BypassThrottleRule(path="/bp/", predicate=predicate)

        # Deliberately shuffled
        result = _prep_rules([
            regular_with_predicate,
            bypass_with_predicate,
            regular_no_predicate,
            bypass_no_predicate,
        ])
        assert result == (
            bypass_no_predicate,
            regular_no_predicate,
            bypass_with_predicate,
            regular_with_predicate,
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


class TestThrottleRegistryBasic:
    def test_register_and_exists(self) -> None:
        registry = ThrottleRegistry()
        assert registry.exists("foo") is False
        registry.register("foo")
        assert registry.exists("foo") is True

    def test_register_idempotent(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.register("foo")
        assert registry.exists("foo") is True

    def test_unregister(self) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.unregister("foo")
        assert registry.exists("foo") is False

    def test_unregister_nonexistent_is_safe(self) -> None:
        registry = ThrottleRegistry()
        registry.unregister("nonexistent")  # Should not raise

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_add_rules_to_registered_uid(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = rule_type(path="/api/")
        registry.add_rules("foo", rule)
        assert rule in registry.get_rules("foo")

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_add_rules_to_unregistered_uid_raises(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        rule = rule_type(path="/api/")
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

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_add_rules_deduplicates(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = rule_type(path="/api/", methods={"GET"})
        registry.add_rules("foo", rule, rule)
        assert len(registry.get_rules("foo")) == 1

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_add_rules_multiple_calls_accumulate(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        r1 = rule_type(path="/a/")
        r2 = rule_type(path="/b/")
        registry.add_rules("foo", r1)
        registry.add_rules("foo", r2)
        rules = registry.get_rules("foo")
        assert len(rules) == 2
        assert r1 in rules
        assert r2 in rules

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_unregister_removes_rules(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        registry.register("foo")
        registry.add_rules("foo", rule_type(path="/api/"))
        registry.unregister("foo")
        assert registry.get_rules("foo") == []

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_rules_isolated_between_uids(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        registry = ThrottleRegistry()
        registry.register("a")
        registry.register("b")
        rule = rule_type(path="/a/")
        registry.add_rules("a", rule)
        assert rule in registry.get_rules("a")
        assert rule not in registry.get_rules("b")

    @pytest.mark.parametrize("rule_type", RULE_TYPES)
    def test_get_rules_returns_list_copy(
        self, rule_type: typing.Type[ThrottleRule[HTTPConnection]]
    ) -> None:
        """Mutating the returned list should not affect internal state."""
        registry = ThrottleRegistry()
        registry.register("foo")
        rule = rule_type(path="/api/")
        registry.add_rules("foo", rule)
        rules = registry.get_rules("foo")
        rules.clear()
        # Internal state should be unaffected
        assert len(registry.get_rules("foo")) == 1


def _make_throttle(
    uid: str,
    throttle_type: typing.Type[ThrottleT],
    registry: ThrottleRegistry,
) -> ThrottleT:
    """Create a `Throttle` bound to the given registry."""
    return throttle_type(
        uid=uid,
        rate="100/min",
        backend=InMemoryBackend(persistent=False),
        registry=registry,
    )


class TestThrottleRegistryGetThrottle:
    @requires_throttle_type
    def test_returns_instance(self, throttle_type: typing.Type[Throttle]) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", throttle_type, registry)
        assert registry.get_throttle("t1") is throttle

    def test_returns_none_for_unknown_uid(self) -> None:
        registry = ThrottleRegistry()
        assert registry.get_throttle("unknown") is None

    @requires_throttle_type
    def test_returns_none_after_gc(self, throttle_type: typing.Type[Throttle]) -> None:
        registry = ThrottleRegistry()
        _make_throttle("t-gc", throttle_type, registry)
        # The throttle is not held by a local variable; collect it.
        gc.collect()
        assert registry.get_throttle("t-gc") is None


@requires_throttle_type
@pytest.mark.asyncio
class TestThrottleRegistryDisableEnable:
    async def test_disable_returns_true_when_found(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", throttle_type, registry)
        result = await registry.disable("t1")
        assert result is True
        del throttle

    async def test_disable_returns_false_when_not_found(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        result = await registry.disable("missing")
        assert result is False

    async def test_disable_sets_throttle_disabled(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", throttle_type, registry)
        await registry.disable("t1")
        assert throttle.is_disabled is True

    async def test_enable_returns_true_when_found(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        throttle = _make_throttle("t1", throttle_type, registry)
        await registry.disable("t1")
        result = await registry.enable("t1")
        assert result is True
        assert throttle.is_disabled is False

    async def test_enable_returns_false_when_not_found(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        result = await registry.enable("missing")
        assert result is False

    async def test_disable_all_disables_every_live_throttle(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        t1 = _make_throttle("ta", throttle_type, registry)
        t2 = _make_throttle("tb", throttle_type, registry)
        await registry.disable_all()
        assert t1.is_disabled is True
        assert t2.is_disabled is True

    async def test_enable_all_re_enables_every_throttle(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        t1 = _make_throttle("ta", throttle_type, registry)
        t2 = _make_throttle("tb", throttle_type, registry)
        await registry.disable_all()
        await registry.enable_all()
        assert t1.is_disabled is False
        assert t2.is_disabled is False

    async def test_disable_all_skips_garbage_collected_throttles(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        """disable_all() must not raise when a throttle has been GC'd."""
        registry = ThrottleRegistry()
        alive = _make_throttle("alive", throttle_type, registry)
        _make_throttle("dead", throttle_type, registry)
        gc.collect()
        # Should not raise even though 'dead' is gone
        await registry.disable_all()
        assert alive.is_disabled is True

    async def test_clear_removes_throttle_refs(
        self, throttle_type: typing.Type[Throttle]
    ) -> None:
        registry = ThrottleRegistry()
        _make_throttle("t1", throttle_type, registry)
        registry.clear()
        assert registry.get_throttle("t1") is None
