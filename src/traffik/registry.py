import inspect
import re
import threading
import typing
import weakref

from traffik.exceptions import ConfigurationError
from traffik.types import (
    HTTPConnectionT,
    Matchable,
    ThrottlePredicate,
)

__all__ = ["ThrottleRule", "BypassThrottleRule", "ThrottleRegistry"]


def _glob_to_regex(pattern: str) -> str:
    """
    Helper to convert a simple glob wildcards in a path pattern to regex equivalents.

    - `**` is replaced with `.*` (matches anything, including `/`)
    - A standalone `*` (not part of `**`) is replaced with `[^/]+`
      (matches a single non-empty path segment)

    Bare `*` is not valid regex (it's a quantifier without a preceding token),
    so any path string containing `*` is assumed to be a glob, not regex.
    Strings without `*` pass through unchanged and are compiled as regex directly.
    """
    if "*" not in pattern:
        return pattern
    # Replace ** first to avoid double-conversion
    pattern = pattern.replace("**", "\x00")
    pattern = pattern.replace("*", "[^/]+")
    pattern = pattern.replace("\x00", ".*")
    return pattern


class ThrottleRule(typing.Generic[HTTPConnectionT]):
    """
    A Throttle "Gate" Rule.

    Determines when a throttle should apply to an HTTP connection.

    When used with a throttle, all rules are checked conjunctively on each `hit(...)` call.
    If **any** rule returns False, the throttle is skipped for that connection.
    A `ThrottleRule` returns True when the connection matches its criteria (path,
    methods, predicate), meaning "yes, apply the throttle". Non-matching connections
    pass through without consuming quota.

    See also: `BypassThrottleRule` for the inverse behavior.
    """

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

        :param path: A matchable path (string, glob, or compiled regex) to apply the throttle to.
            If a string is provided, simple wildcards are supported and converted to regex:

            - `*` matches a single path segment (everything except `/`)
            - `**` matches any number of segments (including `/`)

            Wildcard examples:

            - `"/api/*/users"` matches `/api/v1/users`, `/api/v2/users`
            - `"/api/**"` matches `/api/`, `/api/v1/data`, `/api/v1/v2/nested`
            - `"/api/"` matches paths starting with `/api/` (plain regex, no wildcards)
            - `r"/api/\\d+"` matches `/api/` followed by digits (plain regex)
            - None applies to all paths.

        :param methods: A set of HTTP methods (e.g., 'GET', 'POST') to apply the throttle to.
            If None, the throttle applies to all methods. Ignored for `WebSocket` connections.
        :param predicate: An optional callable that takes an HTTP connection (and an optional context) and returns a boolean.
            If provided, the throttle will only apply if this returns True for the connection.
            This is useful for more complex conditions that cannot be expressed with just path and methods.
            It is run after checking the path and methods, so it should be used for more expensive checks.
        """
        self.path: typing.Optional[re.Pattern[str]]
        if isinstance(path, str):
            self.path = re.compile(_glob_to_regex(path))
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
    A Throttle "Bypass" Rule.

    Skip/bypass the throttle when this rule matches an HTTP connection.

    The inverse of `ThrottleRule`. When a connection matches the bypass criteria
    (path, methods, predicate), the throttle is **not** applied and the connection
    passes through without consuming quota.

    Semantics of `check(...)`:

    - Returns False when the connection **matches** the bypass criteria,
      causing the conjunctive rule check in `hit(...)` to short-circuit and skip
      the throttle.
    - Returns True when the connection does **not** match, allowing other
      rules (and the throttle itself) to proceed normally.
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
    because they return False on match, which is the fastest path to skip a throttle.
    Within each tier, rules without predicates come first since path/method
    checks (frozenset lookup + regex) are cheaper than arbitrary async predicates.

    The resulting order becomes:

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

    __slots__ = ("_registered", "_lock", "_rules", "_throttle_refs")

    def __init__(self) -> None:
        self._registered: typing.Set[str] = set()
        """Set of registered throttle UIDs"""
        self._lock = threading.RLock()
        """Re-entrant lock for thread-safe registry operations"""
        self._rules: typing.Dict[str, typing.Set[ThrottleRule[typing.Any]]] = {}
        """Mapping of throttle UIDs to rules that must pass for the throttle to be applied"""
        self._throttle_refs: typing.Dict[str, weakref.ref] = {}  # type: ignore[type-arg]
        """Weak references to registered throttle instances, keyed by UID"""

    def exist(self, uid: str) -> bool:
        """
        Check if a throttle UID has already been registered in this registry.

        :param uid: The UID to check.
        :return: True if registered, else False.
        """
        return uid in self._registered

    def register(self, uid: str, throttle: typing.Any = None) -> None:
        """
        Register a throttle UID in the registry.

        :param uid: The unique string identifier for the throttle.
        :param throttle: Optional throttle instance to store a weak reference to.
            When provided, enables registry-level disable/enable via `disable()`
            and `disable_all()`.
        """
        with self._lock:
            self._registered.add(uid)
            if throttle is not None:
                self._throttle_refs[uid] = weakref.ref(throttle)

    def unregister(self, uid: str) -> None:
        """
        Unregister a throttle UID and remove any rules attached to it.

        :param uid: The unique string identifier for the throttle to unregister.
        """
        with self._lock:
            self._registered.discard(uid)
            self._rules.pop(uid, None)
            self._throttle_refs.pop(uid, None)

    def add_rules(self, target_uid: str, /, *rules: ThrottleRule[typing.Any]) -> None:
        """
        Add rules that gate a throttle's application.

        Rules are checked conjunctively on the target throttle's `hit(...)` call.
        If any rule returns False, the throttle is skipped for that connection.

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
        :return: A list of `ThrottleRule` instances, or an empty list if none exist.
        """
        with self._lock:
            return list(self._rules.get(uid, []))

    def get_throttle(self, uid: str) -> typing.Any:
        """
        Return the live throttle instance for the given UID, or None if
        the throttle has been garbage-collected or was never registered with
        an instance reference.

        :param uid: The UID of the throttle to look up.
        :return: The throttle instance, or None.
        """
        with self._lock:
            ref = self._throttle_refs.get(uid)
        return ref() if ref is not None else None

    async def disable(self, uid: str) -> bool:
        """
        Disable the throttle registered under *uid*.

        Calls `disable` on the live throttle instance so that subsequent
        `hit` calls return immediately
        without consuming quota.

        :param uid: The UID of the throttle to disable.
        :return: True if the throttle was found and disabled, False
            if the UID is unknown or the throttle has been garbage-collected.
        """
        throttle = self.get_throttle(uid)
        if throttle is not None:
            await throttle.disable()
            return True
        return False

    async def enable(self, uid: str) -> bool:
        """
        Re-enable the throttle registered under *uid*.

        :param uid: The UID of the throttle to enable.
        :return: True if the throttle was found and enabled, False
            if the UID is unknown or the throttle has been garbage-collected.
        """
        throttle = self.get_throttle(uid)
        if throttle is not None:
            await throttle.enable()
            return True
        return False

    async def disable_all(self) -> None:
        """
        Disable every live throttle currently registered in this registry.

        Throttles that have already been garbage-collected are silently skipped.
        """
        with self._lock:
            refs = list(self._throttle_refs.values())
        for ref in refs:
            throttle = ref()
            if throttle is not None:
                await throttle.disable()

    async def enable_all(self) -> None:
        """
        Re-enable every live throttle currently registered in this registry.

        Throttles that have already been garbage-collected are silently skipped.
        """
        with self._lock:
            refs = list(self._throttle_refs.values())
        for ref in refs:
            throttle = ref()
            if throttle is not None:
                await throttle.enable()

    def clear(self) -> None:
        """Clear the registry"""
        with self._lock:
            self._registered = set()
            self._rules = {}
            self._throttle_refs = {}


GLOBAL_REGISTRY = ThrottleRegistry()
"""Default application-wide throttle registry"""
