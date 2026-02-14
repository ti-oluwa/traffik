import inspect
import re
import threading
import typing

from traffik.exceptions import ConfigurationError
from traffik.types import (
    HTTPConnectionT,
    Matchable,
    ThrottlePredicate,
)

__all__ = ["ThrottleRule", "BypassThrottleRule", "ThrottleRegistry"]


class ThrottleRule(typing.Generic[HTTPConnectionT]):
    """Rule definition determining if a throttle should apply to a HTTP connection"""

    __rank__ = 0
    """Higher value means higher check priority. Mainly for optimizing throttle rule checks"""

    __slots__ = (
        "path",
        "methods",
        "predicate",
        "_predicate_takes_context",
        "_hash",
    )

    def __init__(
        self,
        path: typing.Optional[Matchable] = None,
        methods: typing.Optional[typing.Iterable[str]] = None,
        predicate: typing.Optional[ThrottlePredicate[HTTPConnectionT]] = None,
        _compute_hash: bool = True,
    ):
        """
        Initialize the throttle rule.

        :param path: A matchable path (string or regex) to apply the throttle to.
            If string, it's compiled as a regex pattern.

            Examples:
            - "/api/" matches paths starting with "/api/"
            - r"/api/\\d+" matches "/api/" followed by digits
            - None applies to all paths.

        :param methods: A set of HTTP methods (e.g., 'GET', 'POST') to apply the throttle to.
            If None, the throttle applies to all methods. Ignored for `WebSocket` connections.
        :param predicate: An optional callable that takes an HTTP connection (and an otpioanl context) and returns a boolean.
            If provided, the throttle will only apply if this returns True for the connection.
            This is useful for more complex conditions that cannot be expressed with just path and methods.
            It is run after checking the path and methods, so it should be used for more expensive checks.
        """
        self.path: typing.Optional[re.Pattern[str]]
        if isinstance(path, str):
            self.path = re.compile(path)
        else:
            self.path = path

        if methods is None:
            self.methods = None
        else:
            # Store both lowercase and uppercase versions.
            # Although HTTP scope methods are typically uppercase
            self.methods = frozenset(
                m
                for method in methods
                if isinstance(method, str)
                for m in (method.lower(), method.upper())
            )

        self.predicate = predicate
        if predicate is not None:
            self._predicate_takes_context = (
                len(inspect.signature(predicate).parameters) > 1
            )
        else:
            self._predicate_takes_context = False

        if _compute_hash:
            self._hash = hash(
                (
                    type(
                        self
                    ).__rank__,  # Add to differentiate beteween regular and bypass rule
                    self.path.pattern if self.path is not None else None,
                    self.methods,
                    id(predicate) if predicate is not None else None,
                )
            )

    async def check(
        self,
        connection: HTTPConnectionT,
        *,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        """
        Checks if the throttle rule applies to the connection.

        :param connection: The HTTP connection to check.
        :param context: An optional mapping of context to pass to the rule's predicate.
        :return: True if the throttle applies, else false.
        """
        # Check methods first. Cheapest check is frozenset lookup.
        if self.methods is not None:
            # WebSocket connections don't have a "method" in scope, so skip if absent.
            method = connection.scope.get("method")
            if method is not None and method not in self.methods:
                return False

        if self.path is not None and not self.path.match(connection.scope["path"]):
            return False

        if self.predicate is not None:
            if self._predicate_takes_context:
                if not await self.predicate(connection, context):  # type: ignore[call-arg]
                    return False
            elif not await self.predicate(connection):  # type: ignore[call-arg]
                return False

        return True

    async def __call__(
        self,
        connection: HTTPConnectionT,
        *,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        return await self.check(connection, context=context)

    def __hash__(self) -> int:
        return self._hash

    def __setattr__(self, name: str, value: typing.Any):
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError(f"`{self.__class__.__name__}` object is immutable")
        super().__setattr__(name, value)


class BypassThrottleRule(ThrottleRule[HTTPConnectionT]):
    """
    Rule definition for determining if a throttle should be skipped/bypassed for an HTTP connection.

    This basically the opposite of, or negates the regular `ThrottleRule` behaviour.
    """

    __rank__ = 1

    def __init__(
        self,
        path: typing.Optional[Matchable] = None,
        methods: typing.Optional[typing.Iterable[str]] = None,
        predicate: typing.Optional[ThrottlePredicate[HTTPConnectionT]] = None,
    ):
        super().__init__(
            path=path,
            methods=methods,
            predicate=predicate,
            _compute_hash=False,
        )
        # Compute hash now
        self._hash = hash(
            (
                type(
                    self
                ).__rank__,  # Add to differentiate beteween regular and bypass rule
                self.path.pattern if self.path is not None else None,
                self.methods,
                id(predicate) if predicate is not None else None,
            )
        )

    async def check(
        self,
        connection: HTTPConnectionT,
        *,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        if self.methods is not None:
            method = connection.scope.get("method")
            if method is not None and method not in self.methods:
                return True

        if self.path is not None and not self.path.match(connection.scope["path"]):
            return True

        if self.predicate is not None:
            if self._predicate_takes_context:
                if not await self.predicate(connection, context):  # type: ignore[call-arg]
                    return True
            elif not await self.predicate(connection):  # type: ignore[call-arg]
                return True

        return False


def _prep_rules(
    rules: typing.Iterable[ThrottleRule[typing.Any]],
) -> typing.Tuple[ThrottleRule[typing.Any], ...]:
    """
    Sort rules for optimal short-circuit evaluation order.

    Bypass rules (`BypassThrottleRule`) are placed before regular rules
    because they return `False` on match — the fastest path to skip a throttle.
    Within each tier, rules without predicates come first since path/method
    checks (frozenset lookup + regex) are cheaper than arbitrary async predicates.

    Resulting order:

    1. `BypassThrottleRule` without predicate
    2. `ThrottleRule` without predicate
    3. `BypassThrottleRule` with predicate
    4. `ThrottleRule` with predicate

    :param rules: A sequence of `ThrottleRule` instances to sort.
    :return: A sorted tuple of `ThrottleRule` instances.
    """
    if not rules:
        return ()
    return tuple(
        sorted(
            rules,
            key=lambda r: (r.predicate is not None, -type(r).__rank__),
        )
    )


class ThrottleRegistry:
    """
    Throttle registry.

    Manages throttle registration and rule attachment. Throttles register
    by UID, and any throttle can attach rules to any other registered
    throttle by UID.
    """

    __slots__ = ("_registered", "_lock", "_rules")

    def __init__(self) -> None:
        self._registered: typing.Set[str] = set()
        """Set of registered throttle UIDs"""
        self._lock = threading.RLock()
        """Re-entrant lock for thread-safe registry operations"""
        self._rules: typing.Dict[str, typing.Set[ThrottleRule[typing.Any]]] = {}
        """Mapping of throttle UIDs to rules that must pass for the throttle to be applied"""

    def exist(self, uid: str) -> bool:
        """
        Check if a throttle UID has already been registered in this registry.

        :param uid: The UID to check.
        :return: True if registered, else False.
        """
        return uid in self._registered

    def register(self, uid: str) -> None:
        """
        Register a throttle UID in the registry.

        :param uid: The unique string identifier for the throttle.
        """
        with self._lock:
            self._registered.add(uid)

    def unregister(self, uid: str) -> None:
        """
        Unregister a throttle UID and remove any rules attached to it.

        :param uid: The unique string identifier for the throttle to unregister.
        """
        with self._lock:
            self._registered.discard(uid)
            self._rules.pop(uid, None)

    def add_rules(self, target_uid: str, /, *rules: ThrottleRule[typing.Any]) -> None:
        """
        Add rules that gate a throttle's application.

        Rules are checked conjunctively on the target throttle's `hit(...)` call.
        If any rule returns `False`, the throttle is skipped for that connection.

        :param target_uid: The UID of the throttle to attach rules to.
        :param rules: One or more `ThrottleRule` instances to add.
        :raises ConfigurationError: If `target_uid` is not registered.
        """
        if not rules:
            return None

        if target_uid not in self._registered:
            raise ConfigurationError(f"No throttle registered with UID {target_uid!r}")

        with self._lock:
            if target_uid not in self._rules:
                self._rules[target_uid] = set(rules)
            else:
                self._rules[target_uid].update(rules)

    def get_rules(self, uid: str) -> typing.List[ThrottleRule[typing.Any]]:
        """
        Get all rules associated with a throttle.

        :param uid: The UID of the throttle.
        :return: A list of ``ThrottleRule`` instances, or an empty list if none exist.
        """
        with self._lock:
            return list(self._rules.get(uid, []))

    def clear(self) -> None:
        """Clear the registry"""
        self._registered = set()
        self._rules = {}


GLOBAL_REGISTRY = ThrottleRegistry()
"""Default application-wide throttle registry"""
