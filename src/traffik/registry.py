import inspect
import re
import threading
import typing

from traffik.exceptions import ConfigurationError
from traffik.types import HTTPConnectionT, Matchable, ThrottlePredicate

__all__ = ["ThrottleRule", "BypassThrottleRule", "ThrottleRegistry"]


class ThrottleRule(typing.Generic[HTTPConnectionT]):
    """Rule definition determining if a throttle should apply to a HTTP connection"""

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
                    0,  # Add to differentiate beteween regular and bypass rule
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
                if not await self.predicate(connection, context):  # type: ignore[arg-type]
                    return False
            elif not await self.predicate(connection):  # type: ignore[arg-type]
                return False

        return True

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
                1,  # Add to differentiate beteween regular and bypass rule
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
                if not await self.predicate(connection, context):  # type: ignore[arg-type]
                    return True
            elif not await self.predicate(connection):  # type: ignore[arg-type]
                return True

        return False


@typing.final
class _ReusableIdGenerator:
    """Thread-safe incrementing integer ID generator"""

    __slots__ = ("_counter", "_free", "_lock")

    def __init__(self):
        self._counter = 0
        self._free = []
        self._lock = threading.Lock()

    def next(self) -> int:
        """
        Get the next available ID, reusing a released one if available.

        :return: A unique integer ID.
        """
        with self._lock:
            if self._free:
                return self._free.pop()
            value = self._counter
            self._counter += 1
            return value

    def release(self, value: int):
        """
        Release an ID back to the pool for reuse.

        :param value: The integer ID to release.
        """
        with self._lock:
            self._free.append(value)


@typing.final
class _Node:
    """A Directed Acyclic Graph (DAG) node"""

    __slots__ = (
        "id",
        "parents",
        "children",
        "ancestor_mask",
    )

    def __init__(self, throttle_id: int):
        """
        Initial the node.

        :param throttle_id: The unique integer ID for this node.
        """
        self.id = throttle_id
        self.parents: set["_Node"] = set()
        self.children: set["_Node"] = set()
        self.ancestor_mask: int = 0

    def recompute_mask(self) -> None:
        """Recompute the ancestor bitmask from the current parent set."""
        new_mask = 0
        for parent in self.parents:
            new_mask |= 1 << parent.id
            new_mask |= parent.ancestor_mask
        self.ancestor_mask = new_mask

    def add_parent(self, parent: "_Node") -> None:
        """
        Add a parent node, establishing an upstream relationship.

        :param parent: The node to add as a parent.
        :raises ConfigurationError: If the parent is self or would create a cycle.
        """
        if parent is self:
            raise ConfigurationError("Throttle cannot be parent of itself.")

        # Check if we are in a cycle
        # if parent is self or self.is_parent_of(parent): # This may be safer but slower
        if parent.ancestor_mask & (1 << self.id):
            raise ConfigurationError("Cycle detected in throttle graph.")

        if parent in self.parents:
            return

        self.parents.add(parent)
        parent.children.add(self)

        self._inherit_from(parent)

    def _inherit_from(self, parent: "_Node") -> None:
        """
        Inherit ancestor bits from a parent and propagate downward to children.

        :param parent: The parent node to inherit from.
        """
        parent_bit = 1 << parent.id
        inherited_mask = parent_bit | parent.ancestor_mask

        if (self.ancestor_mask & inherited_mask) == inherited_mask:
            return  # nothing new

        self.ancestor_mask |= inherited_mask

        # Propagate downward
        for child in self.children:
            child._inherit_from(self)

    def add_child(self, child: "_Node") -> None:
        """
        Add a child node, establishing a downstream relationship.

        :param child: The node to add as a child.
        """
        child.add_parent(self)

    def is_parent_of(self, other: "_Node") -> bool:
        """
        Check if this node is an ancestor of the other node.

        :param other: The node to check against.
        :return: True if this node is an ancestor of the other.
        """
        return bool(other.ancestor_mask & (1 << self.id))

    def is_child_of(self, other: "_Node") -> bool:
        """
        Check if this node is a descendant of the other node.

        :param other: The node to check against.
        :return: True if this node is a descendant of the other.
        """
        return bool(self.ancestor_mask & (1 << other.id))

    def has_ancestor(self) -> bool:
        """
        :return: True if this node has any ancestors.
        """
        return bool(self.ancestor_mask)

    def has_descendant(self) -> bool:
        """
        :return: True if this node has any descendants.
        """
        return bool(self.children)


class ThrottleRegistry:
    """
    Throttle registry

    Manages throttle registration, throttle rules, and maintains a
    Directed Acyclic Graph (DAG) structure to describe throttle relationships.
    """

    __slots__ = ("_ids", "_id_gen", "_nodes", "_lock", "_rules")

    def __init__(self) -> None:
        self._ids: typing.Dict[str, int] = {}
        """Mapping of throttle UID to its integer ID"""
        self._id_gen = _ReusableIdGenerator()
        """Thread-safe unique integer ID generator"""
        self._nodes: typing.Dict[int, _Node] = {}
        """Mapping of throttle IDs to the DAG nodes"""
        self._lock = threading.RLock()
        """Re-entrant lock for thread-safe registry operations"""
        self._rules: typing.Dict[int, typing.Set[ThrottleRule[typing.Any]]] = {}
        """Mapping of throttle IDs to rules that must pass for the throttle to be applied"""

    def _recompute_subgraph(self, starting_nodes: typing.Iterable[_Node]) -> None:
        """
        Recompute ancestor masks for a subgraph starting from the given nodes,
        propagating downward through children when masks change.

        :param starting_nodes: The nodes to begin recomputation from.
        """
        stack = list(starting_nodes)
        visited = set()

        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)

            old_mask = node.ancestor_mask
            node.recompute_mask()

            # If mask changed, children must update
            if node.ancestor_mask != old_mask:
                stack.extend(node.children)

    def exist(self, uid: str) -> bool:
        """
        Check if a throttle UID has already been registered in this registry.

        :param uid: The UID to check.
        :return: True, If registered, else False.
        """
        return uid in self._ids

    def get_id(self, uid: str) -> int:
        """
        Get or create a unique integer ID for the given throttle UID.

        :param uid: The unique string identifier for the throttle.
        :return: The integer ID assigned to this UID.
        """
        with self._lock:
            if uid in self._ids:
                return self._ids[uid]

            throttle_id = self._id_gen.next()
            self._ids[uid] = throttle_id
            self._nodes[throttle_id] = _Node(throttle_id)
        return throttle_id

    def release_id(self, uid: str) -> None:
        """
        Release a throttle's ID, disconnecting it from the DAG and
        returning the ID to the pool for reuse.

        :param uid: The unique string identifier for the throttle to release.
        """
        with self._lock:
            if uid not in self._ids:
                return None

            throttle_id = self._ids.pop(uid)
            node = self._nodes.pop(throttle_id)

            # Disconnect from parents
            for parent in list(node.parents):
                parent.children.remove(node)

            # Disconnect from children
            affected = list(node.children)
            for child in affected:
                child.parents.remove(node)

            # Recompute masks for affected subtree
            self._recompute_subgraph(affected)

            # Release ID
            self._id_gen.release(throttle_id)

    def register_upstream(self, uid: str, upstream_uid: str) -> None:
        """
        Register an upstream (parent) relationship between two throttles.

        :param uid: The UID of the downstream (child) throttle.
        :param upstream_uid: The UID of the upstream (parent) throttle.
        :raises ConfigurationError: If the relationship would create a cycle.
        """
        with self._lock:
            node = self._nodes[self._ids[uid]]
            parent = self._nodes[self._ids[upstream_uid]]
            node.add_parent(parent)

    def register_downstream(self, uid: str, downstream_uid: str) -> None:
        """
        Register a downstream (child) relationship between two throttles.

        :param uid: The UID of the upstream (parent) throttle.
        :param downstream_uid: The UID of the downstream (child) throttle.
        :raises ConfigurationError: If the relationship would create a cycle.
        """
        with self._lock:
            parent = self._nodes[self._ids[uid]]
            child = self._nodes[self._ids[downstream_uid]]
            parent.add_child(child)

    def is_downstream_of(self, uid: str, upstream_uid: str) -> bool:
        """
        Check if a throttle is a descendant of another in the DAG.

        :param uid: The UID of the potential downstream throttle.
        :param upstream_uid: The UID of the potential upstream throttle.
        :return: True if `uid` is downstream of `upstream_uid`.
        """
        node = self._nodes[self._ids[uid]]
        other = self._nodes[self._ids[upstream_uid]]
        return node.is_child_of(other)

    def is_upstream_of(self, uid: str, downstream_uid: str) -> bool:
        """
        Check if a throttle is an ancestor of another in the DAG.

        :param uid: The UID of the potential upstream throttle.
        :param downstream_uid: The UID of the potential downstream throttle.
        :return: True if `uid` is upstream of `downstream_uid`.
        """
        node = self._nodes[self._ids[uid]]
        other = self._nodes[self._ids[downstream_uid]]
        return node.is_parent_of(other)

    def has_upstream(self, uid: str) -> bool:
        """
        Check if a throttle has any upstream (ancestor) throttles.

        :param uid: The UID of the throttle to check.
        :return: True if the throttle has at least one ancestor.
        """
        return self._nodes[self._ids[uid]].has_ancestor()

    def has_downstream(self, uid: str) -> bool:
        """
        Check if a throttle has any downstream (descendant) throttles.

        :param uid: The UID of the throttle to check.
        :return: True if the throttle has at least one descendant.
        """
        return self._nodes[self._ids[uid]].has_descendant()

    def add_upstream_rules(
        self, uid: str, upstream_uid: str, /, *rules: ThrottleRule[typing.Any]
    ) -> None:
        """
        Add rules that gate an upstream throttle's application.

        :param uid: The UID of the downstream throttle asserting the relationship.
        :param upstream_uid: The UID of the upstream throttle to attach rules to.
        :param rules: One or more `ThrottleRule` instances to add.
        :raises ConfigurationError: If `uid` is not downstream of `upstream_uid`.
        """
        if not rules:
            return None

        if not self.is_downstream_of(uid, upstream_uid):
            raise ConfigurationError(
                f"Throttle with UID {uid!r} does not have an upstream throttle with UID {upstream_uid!r}"
            )

        with self._lock:
            upstream_id = self._ids[upstream_uid]
            if upstream_id not in self._rules:
                self._rules[upstream_id] = set(rules)
                return None
            self._rules[upstream_id].update(rules)
        return None

    def get_rules(self, uid: str) -> typing.List[ThrottleRule[typing.Any]]:
        """
        Get all rules associated with a throttle.

        :param uid: The UID of the throttle.
        :return: A list of `ThrottleRule` instances, or an empty list if none exist.
        """
        if uid not in self._ids:
            return []

        with self._lock:
            return list(self._rules.get(self._ids[uid], []))


GLOBAL_REGISTRY = ThrottleRegistry()
"""Default application0wide throttle registry"""
