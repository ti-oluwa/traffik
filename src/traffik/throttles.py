"""Throttles for Starlette `HTTPConnection` types."""

import asyncio
import functools
import inspect
import math
import sys
import threading
import typing
import weakref

from starlette.requests import HTTPConnection, Request
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketState
from typing_extensions import Self, TypedDict

from traffik.backends.base import (
    ThrottleBackend,
    connection_throttled,
    get_throttle_backend,
)
from traffik.config import (
    CONNECTION_IDS_CONTEXT_KEY,
    THROTTLE_DEFAULT_SCOPE,
    THROTTLED_STATE_KEY,
)
from traffik.exceptions import ConfigurationError
from traffik.headers import Header, Headers
from traffik.rates import Rate
from traffik.registry import (
    GLOBAL_REGISTRY,
    ThrottleRegistry,
    ThrottleRule,
    _prep_rules,
)
from traffik.strategies import DEFAULT_STRATEGY
from traffik.types import (
    EXEMPTED,
    ApplyOnError,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    CostType,
    HTTPConnectionT,
    LockConfig,
    P,
    R,
    RateType,
    StrategyStat,
    Stringable,
    ThrottleErrorHandler,
    WaitPeriod,
)

__all__ = [
    "Throttle",
    "HTTPThrottle",
    "RequestThrottle",
    "WebSocketThrottle",
    "is_throttled",
    "ThrottleExceptionInfo",
    "websocket_throttled",
    "throttled",
]

ThrottleStrategy = typing.Callable[
    [Stringable, Rate, ThrottleBackend[typing.Any, HTTPConnection], int],  # type: ignore[misc]
    typing.Awaitable[WaitPeriod],
]
"""
A callable that implements a throttling strategy.

Takes a key, a Rate object, the throttle backend, and cost, and returns the wait period in milliseconds.
"""


class ThrottleExceptionInfo(TypedDict):
    """TypedDict for exception handler information."""

    exception: Exception
    """The type of exception the handler is for."""
    connection: HTTPConnection
    """The HTTP connection associated with the exception."""
    cost: int
    """The cost associated with the throttling operation."""
    rate: Rate
    """The rate associated with the throttling operation."""
    backend: ThrottleBackend[typing.Any, HTTPConnection]
    """The backend used during the throttling operation."""
    context: typing.Optional[typing.Mapping[str, typing.Any]]
    """Additional context for the throttling operation."""
    throttle: "Throttle[HTTPConnection]"
    """The throttle instance used during the throttling operation."""


ExceptionInfo = ThrottleExceptionInfo  # Alias for backwards compatibility

_conection_type_cache: typing.Dict[
    typing.Type["Throttle"], typing.Type[HTTPConnection]
] = {}
_connection_type_cache_lock = threading.Lock()


def _cache_connection_type(
    cls: typing.Type["Throttle"], connection_type: typing.Type[HTTPConnection]
) -> None:
    """Cache the resolved connection type for a throttle class."""
    with _connection_type_cache_lock:
        _conection_type_cache[cls] = connection_type


class Throttle(typing.Generic[HTTPConnectionT]):
    """Base connection throttle class"""

    __slots__ = (
        "_id",
        "uid",
        "rate",
        "identifier",
        "backend",
        "handle_throttled",
        "strategy",
        "cost",
        "min_wait_period",
        "registry",
        "_rules",
        "_rules_resolved",
        "cache_ids",
        "on_error",
        "use_fixed_backend",
        "_headers",
        "_default_context",
        "_uses_rate_func",
        "_uses_cost_func",
        "_error_callback",
        "_connection_type",
        "__signature__",
        "__weakref__",
    )

    def __init__(
        self,
        uid: str,
        rate: RateType[HTTPConnectionT],
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, Self]  # type: ignore[arg-type]
        ] = None,
        strategy: typing.Optional[ThrottleStrategy] = None,
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]] = None,
        cost: CostType[HTTPConnectionT] = 1,
        dynamic_backend: bool = False,
        min_wait_period: typing.Optional[int] = None,
        headers: typing.Optional[
            typing.Mapping[str, typing.Union[Header[HTTPConnectionT], str]]
        ] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[HTTPConnectionT, ThrottleExceptionInfo],
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        registry: typing.Optional[ThrottleRegistry] = None,
        rules: typing.Optional[typing.Iterable[ThrottleRule[HTTPConnectionT]]] = None,
        cache_ids: bool = True,
    ) -> None:
        """
        Initialize the throttle instance.

        :param uid: Unique identifier for the throttle instance. This ensures that
            multiple instances of the same throttle can coexist without conflicts.
            It also allows for persistent storage of throttle state across application
            restarts or deployments.
            Note! This should always be unique across all throttle instances used
            in the application to avoid conflicts in throttle state management.
            A good convention is to use a namespaced format like
            "throttle_name:purpose" (e.g., "login_attempts:ip") to ensure uniqueness
            and clarity of the throttle's intent.

        :param rate: Rate limit definition. This can be provided as a Rate object
            or as a string in the format "limit/period" (e.g., "100/m" for 100 requests
            per minute).

        :param identifier: Connected client identifier generator.
            If not provided, the throttle backend's identifier will be used.
            This identifier is used to uniquely identify the client connection
            and track its throttling state. Identifiers can be based on various factors,
            such as IP address, user ID, API key, etc.
            Identifiers should be efficient to compute and provide sufficient uniqueness to avoid collisions.
            NOTE: Identifiers can be used to implement any exemption logic (e.g., whitelisting certain clients),
            Just return `EXEMPTED` from the identifier to exempt a connection from throttling.

        :param handle_throttled: Handler to call when the client connection is throttled.
            If provided, it will override the default connection throttled handler
            defined for the throttle backend.
            This handler is responsible for notifying the client about the throttling
            and can implement custom logic, such as sending a specific response or logging.

        :param strategy: Throttling strategy to use. If not provided, the default strategy will be used.
            The strategy defines how the throttling is applied, such as fixed window,
            sliding window, or token bucket.

        :param backend: The throttle backend to use for storing throttling data.
            If not provided, the default backend will be used.
            The backend is responsible for managing the throttling state,
            including checking the current throttling status, updating it, and handling
            throttled connections.
            If `dynamic_backend` is True, the backend will be resolved from the request context
            on each call, allowing for dynamic backend resolution based on the request context.

        :param cost: The cost/weight of each request. This allows for different requests
            to have different impacts on the throttling state. For example, a request that performs a
            resource-intensive operation might have a higher cost than a simple read request.

        :param dynamic_backend: If True, resolves backend from the application/request/local context
            on each request instead of caching it. Designed for multi-tenant applications where the backend
            is determined at runtime from request data (JWT, headers, etc.).

            **Use cases:**
            - Multi-tenant SaaS: Different backends per tenant tier
            - Environment-based routing: Production vs staging backends
            - Testing: Nested context managers with different backends

            **Requirements:**
            - Backend must be set via lifespan or context manager in middleware **before** throttle is called
            - Cannot be combined with explicit `backend` parameter

            **Trade-offs:**
            - Adds ~1-20ms overhead per request (backend resolution)
            - Data fragmentation risk if context switching is inconsistent
            - Use explicit `backend` parameter for simple shared storage

            See documentation on "Context-Aware Backends" section for full examples.

        :param min_wait_period: The minimum allowable wait period (in milliseconds) for a throttled connection.
        :param headers: Optional headers to include in throttling responses. A use case can
            be to include additional throttle/throttling information in the response headers.
            This will be merged with any headers provided in `context`.

        :param on_error: Strategy for handling errors during throttling.
            Can be one of the following:
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters and
                returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.

            If not provided, defaults to behavior defined by the backend or "throttle".

        :param context: Optional default context to use for all throttle calls. This can include any relevant information needed
            for context-aware throttling strategies. The context provided here will be merged with any context
            provided during individual throttle calls, with the call-specific context taking precedence in case of conflicts.

        :param registry: The registry this throttle should belong to and use. Defaults to `GLOBAL_REGISTRY`.
        :param rules: Optional rules that define if, and when this throttle should apply.
        :param cache_ids: Whether to cache connection IDs on the connection state.
            Defaults to True. Disable only for advanced use cases where connection IDs
            may change frequently during the connection's lifetime, or if you want to
            ensure that identifiers are always freshly resolved on each request.

            Setting this to `True` is especially useful for long-lived connections
            like `WebSocket`s where the connection does not change after establishment,
            and caching avoids redundant/expensive identifier computations.
        """
        if not uid or not isinstance(uid, str):
            raise ValueError("uid is required and must be a non-empty string")

        registry = registry or GLOBAL_REGISTRY
        if registry.exist(uid):
            raise ConfigurationError(
                f"Throttle UID must be unique. Throttle with UID {uid!r} already exists."
            )

        if dynamic_backend and backend is not None:
            raise ValueError(
                "Cannot specify an explicit backend with `dynamic_backend=True`"
            )

        self.uid = uid
        self._uses_rate_func = callable(rate)
        self.rate = Rate.parse(rate) if isinstance(rate, str) else rate

        self._uses_cost_func = callable(cost)
        self.cost = cost
        self.use_fixed_backend = not dynamic_backend
        self.strategy = strategy or DEFAULT_STRATEGY
        self.min_wait_period = min_wait_period
        self.registry = registry
        self._rules: typing.Tuple[ThrottleRule[HTTPConnectionT], ...] = (
            _prep_rules(set(rules)) if rules else ()
        )
        self._rules_resolved = False
        self.cache_ids = cache_ids
        self.registry.register(uid)
        # Register a finalization weakref for when the throttle is garbage-collected
        # so its UID can be unregistered.
        weakref.finalize(self, self.registry.unregister, uid)

        # Ensure that we copy the context to avoid potential mutation issues from outside after initialization
        # Never modify `_default_context` after initialization. It's unsafe.
        self._default_context = dict(context or {})
        # Set 'scope' in default context if not already set.
        self._default_context.setdefault("scope", THROTTLE_DEFAULT_SCOPE)

        if headers is None:
            headers = Headers()
        elif not isinstance(headers, Headers):
            headers = Headers(headers)
        self._headers = typing.cast(Headers[HTTPConnectionT], headers)

        # Only set backend for non-dynamic backend throttles
        if not dynamic_backend:
            resolved_backend = backend or get_throttle_backend()
            self.backend = resolved_backend

            self.identifier = identifier or (
                resolved_backend.identifier if resolved_backend is not None else None
            )
            self.handle_throttled = handle_throttled or (
                resolved_backend.handle_throttled
                if resolved_backend is not None
                else None
            )
            on_error_ = on_error or (
                resolved_backend.on_error
                if resolved_backend is not None
                else "throttle"
            )
        else:
            self.backend = backend
            self.identifier = identifier
            self.handle_throttled = handle_throttled
            on_error_ = on_error  # type: ignore[assignment]

        self._error_callback: typing.Optional[
            ThrottleErrorHandler[HTTPConnectionT, ThrottleExceptionInfo]
        ] = None
        if callable(on_error_):
            self._error_callback = on_error_
            # Just set it for reference
            self.on_error = on_error_
        elif isinstance(on_error_, str) and on_error_ in {"allow", "throttle", "raise"}:
            self.on_error = on_error_  # type: ignore[assignment]
        elif on_error_ is None and not self.use_fixed_backend:
            # We'll handle this in `_handle_error(...)` since backend is dynamic
            self.on_error = None
        else:
            raise ValueError(
                f"Invalid `on_error` value: {on_error_!r}. "
                "Must be 'allow', 'throttle', 'raise', or a callable."
            )

        # Set a clean `__signature__` for FastAPI's dependency injection.
        # `__call__` uses *args/**kwargs to support direct calls like
        # `throttle(request, cost=5)`, but FastAPI would interpret those
        # as required query parameters. By setting `__signature__` to only
        # expose `connection`, FastAPI injects the request correctly.
        self.__signature__ = self._make_signature()

    @property
    def is_dynamic(self) -> bool:
        """Returns True if the throttle uses a dynamic backend, False otherwise."""
        return not self.use_fixed_backend

    async def get_headers(
        self,
        connection: HTTPConnectionT,
        headers: typing.Optional[
            typing.Mapping[str, typing.Union[Header[HTTPConnectionT], str]]
        ] = None,
        stat: typing.Optional[StrategyStat[typing.Mapping]] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Dict[str, str]:
        """
        Resolves the headers to include in throttling responses based on the provided connection, strategy statistics, and context.

        :param connection: The HTTP connection for the current request.
        :param headers: Optional additional headers to resolve for this specific call, which will be merged with the throttle's default headers.
            Headers provided here will take precedence over the throttle's default headers in case of conflicts.
        :param stat: Optional strategy statistics for the current request. This may be None if headers are being resolved outside of a throttling operation.
        :param context: An optional dictionary containing additional context for the throttle. This can include any relevant information needed to resolve dynamic headers.
        :return: The resolved headers a dictionary of strings to include in throttling responses.
        """
        if not headers and not self._headers:
            return {}

        if not headers and self._headers._is_static:
            return self._headers._raw.copy()  # type: ignore[return-value]

        if not headers:
            merged_headers = self._headers.copy()
        else:
            merged_headers = self._headers | headers

        if merged_headers._is_static:
            # All headers are static, so we can return them directly without resolution.
            return merged_headers._raw  # type: ignore[return-value]

        return await _resolve_headers(
            headers=merged_headers,
            connection=connection,
            throttle=self,
            stat=stat,
            context=context,
        )

    @property
    def connection_type(self) -> typing.Type[HTTPConnection]:
        """
        Returns the `HTTPConnection` type that this throttle is designed for.

        If `_connection_type` is already set, it returns it. Otherwise,
        it resolves the connection type through a resolution process that checks for type hints
        in the class definition, including generic parameters and method annotations.
        """
        # Notice that we didn't define the `_connection_type` in `__init__`.
        # This is so that when subclasses want to override the connection type resolution logic,
        # they can set `_connection_type` directly before calling `super().__init__()`,
        # so that when `connection_type` is accessed during `__init__`,
        # it returns the connection type they set without going through the resolution process.
        if getattr(self, "_connection_type", None) is not None:
            return self._connection_type  # type: ignore[has-type]

        self._connection_type = self._resolve_connection_type()
        return self._connection_type

    def _resolve_connection_type(self) -> typing.Type[HTTPConnection]:
        """
        Resolve's the `HTTPConnection` type that this throttle is designed for.

        Resolution order:
        1. Check the connection type cache to see if we've already resolved
            the connection type for this throttle class before.
        2. `__orig_class__` (set when instantiated as e.g. `Throttle[Request](...)`)
        3. Walk `__orig_bases__` through the MRO (for subclasses like `HTTPThrottle`)
        4. Inspect the `hit` method's `connection` parameter type hint
        5. If all else fails, default to `HTTPConnection`
        """
        if type(self) in _conection_type_cache:
            return _conection_type_cache[type(self)]  # type: ignore[return-value]

        # Check `__orig_class__` which is available when generic is instantiated directly
        orig_class = getattr(self, "__orig_class__", None)
        if orig_class is not None:
            args = typing.get_args(orig_class)
            if (
                args
                and isinstance(args[0], type)
                and issubclass(args[0], HTTPConnection)
            ):
                _cache_connection_type(type(self), args[0])
                return args[0]  # type: ignore[return-value]

        # If the `__orig_class__` attribute is not set, walk class hierarchy for concrete type args on Throttle bases
        # e.g. `HTTPThrottle(Throttle[Request])` gives `Request` as the connection type
        for cls in type(self).__mro__:
            for base in getattr(cls, "__orig_bases__", ()):
                origin = typing.get_origin(base)
                if origin is None:
                    continue
                try:
                    if not (origin is Throttle or issubclass(origin, Throttle)):
                        continue
                except TypeError:
                    continue
                args = typing.get_args(base)
                if (
                    args
                    and isinstance(args[0], type)
                    and issubclass(args[0], HTTPConnection)
                ):
                    _cache_connection_type(type(self), args[0])
                    return args[0]  # type: ignore[return-value]

        # Lastly, inspect the `hit` method signature for a concrete connection annotation
        try:
            hints = typing.get_type_hints(type(self).hit, include_extras=False)
            # Check for a parameter named "connection" first since that's the conventional name for the connection parameter in `__call__`.
            # If it's not present, the first parameter annotated as a subclass of HTTPConnection is
            # assumed to be the connection type for the throttle
            connection_type = (
                hints.get("connection", None) or hints[list(hints.keys())[0]]
            )
            if (
                connection_type is not None
                and isinstance(connection_type, type)
                and issubclass(connection_type, HTTPConnection)
            ):
                _cache_connection_type(type(self), connection_type)
                return connection_type  # type: ignore[return-value]
        except Exception:  # nosec
            pass

        # Fallback to `HTTPConnection`.
        # Do not cache this fallback result to allow for dynamic resolution later on.
        return HTTPConnection

    def _make_signature(self) -> inspect.Signature:
        """
        Internal method to create a clean signature for the throttle's `__call__` method.

        This is used to ensure that when the throttle is used as a dependency in FastAPI routes,
        it does not interfere with FastAPI's request parsing and OpenAPI schema generation.
        """
        return _make_throttle_signature(
            throttle=self,
            connection_param_name="connection",
            target_method="hit",
            return_annotation=None,
        )

    def get_backend(
        self, connection: typing.Optional[HTTPConnectionT] = None
    ) -> ThrottleBackend[typing.Any, HTTPConnectionT]:
        """
        Get the throttle backend, resolving dynamically if needed.

        :param connection: Optional HTTP connection to use for dynamic backend resolution.
        :return: The resolved throttle backend.
        """
        if self.backend is None and connection is not None:
            app = getattr(connection, "app", None)
            backend = get_throttle_backend(app)
            if backend is None:
                raise ConfigurationError(
                    "No throttle backend configured. "
                    "Provide a backend to the throttle or set a default backend."
                )

            # Only set/cache the backend if the throttle is not dynamic.
            if self.use_fixed_backend:
                self.backend = backend
        else:
            backend = self.backend  # type: ignore[assignment]
        return backend  # type: ignore[return-value]

    async def get_connection_id(
        self,
        connection: HTTPConnectionT,
        backend: ThrottleBackend[typing.Any, HTTPConnectionT],
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> Stringable:
        """
        Get the unique identifier for the connection.

        :param connection: The HTTP connection to get the identifier for.
        :param backend: The throttle backend to use for identifier generation.
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the connection/request for throttling purposes.
        :return: The unique identifier for the connection.
        """
        if not self.cache_ids:
            identifier = self.identifier or backend.identifier
            connection_id = await identifier(connection)
            return connection_id

        # Check the connection state cache first
        cached_connection_ids: typing.Dict[str, Stringable] = getattr(
            connection.state, CONNECTION_IDS_CONTEXT_KEY, {}
        )
        connection_id = cached_connection_ids.get(self.uid, None)
        if connection_id is not None:
            return connection_id

        # If not cached, compute and cache it
        identifier = self.identifier or backend.identifier
        connection_id = await identifier(connection)
        setattr(
            connection.state,
            CONNECTION_IDS_CONTEXT_KEY,
            {**cached_connection_ids, self.uid: connection_id},
        )
        return connection_id

    def get_scoped_key(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        """
        Returns a scoped/unique throttling key for the connection.

        :param connection: The HTTP connection to throttle.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the connection for throttling purposes.

            **Keys to be set in the context:**
            - "scope": A string to differentiate throttling for different contexts within the same connection.

        :return: The scoped throttling key for the connection.
        """
        raise NotImplementedError(
            "`get_scoped_key(...)` must be implemented by subclasses"
        )

    def get_namespaced_key(
        self,
        connection: HTTPConnectionT,
        connection_id: Stringable,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        """
        Returns the namespaced throttling key for the connection.

        :param connection: The HTTP connection to throttle.
        :param connection_id: The unique identifier for the connection.
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the connection for throttling purposes.

            **Keys to be set in the context:**
            - "scope": A string to differentiate throttling for different contexts within the same connection.

        :return: The namespaced throttling key for the connection.
        """
        scoped_key = self.get_scoped_key(connection, context)
        namespaced_key = f"{self.uid}:{str(connection_id)}:{scoped_key}"
        return namespaced_key

    async def _handle_error(
        self,
        connection: HTTPConnectionT,
        exc: Exception,
        cost: int,
        rate: Rate,
        backend: ThrottleBackend[typing.Any, HTTPConnectionT],
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> WaitPeriod:
        """
        Handle errors during throttling based on the configured error-handling strategy.

        :param connection: The HTTP connection being throttled.
        :param exc: The exception that occurred during throttling.
        :param cost: The cost/weight of the request that caused the error.
        :param rate: The rate associated with the throttling operation.
        :param backend: The backend used during the throttling operation.
        :param throttle: The throttle instance.
        :param context: Additional context for the throttling operation.
        :return: The wait period in milliseconds.
        """
        if self._error_callback:
            exc_info = dict(
                exception=exc,
                connection=connection,
                cost=cost,
                rate=rate,
                context=context,
                backend=backend,
                throttle=self,
            )
            return await self._error_callback(connection, exc_info)  # type: ignore[arg-type]
        elif self.on_error == "allow":
            return 0.0
        elif self.on_error == "throttle":
            return self.min_wait_period or 1000.0  # Default to 1000ms
        elif not self.use_fixed_backend and self.on_error is None:
            # For dynamic backend throttles, check backend's on_error
            if backend._error_callback:
                exc_info = dict(
                    exception=exc,
                    connection=connection,
                    cost=cost,
                    rate=rate,
                    context=context,
                    backend=backend,
                    throttle=self,
                )
                return await backend._error_callback(connection, exc_info)  # type: ignore[arg-type]
            elif backend.on_error == "allow":
                return 0.0
            elif backend.on_error == "throttle":
                return self.min_wait_period or 1000.0  # Default to 1000ms
            # Falls through to raise below

        # `on_error` is "raise"
        raise exc

    async def hit(
        self,
        connection: HTTPConnectionT,
        *,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> HTTPConnectionT:
        """
        Throttle the connection based on the limit and time period.

        Records a hit for the connection and applies throttling if necessary.

        :param connection: The HTTP connection to throttle.
        :param cost: The cost/weight of this connection/request (overrides default cost if provided).
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the connection for throttling purposes.

            **Keys to be set in the context:**
            - "scope": A string to differentiate throttling for different contexts within the same connection.

        :return: The throttled HTTP connection.
        """
        if cost == 0:
            return connection  # No cost, no throttling needed

        # Mutation for context downstream is highly prohibited.
        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context

        # Resolve and cache rules on first hit.
        rules = self._rules
        if not self._rules_resolved:
            registry_rules = self.registry.get_rules(self.uid)
            if registry_rules:
                seen = set(rules)
                merged = rules + tuple(r for r in registry_rules if r not in seen)
                # Re-sort since registry rules were added to the pre-sorted constructor rules
                self._rules = rules = _prep_rules(merged)
            self._rules_resolved = True

        if rules:
            # All rules must pass for the throttle to apply
            for rule in rules:
                if not await rule.check(connection, context=merged_context):
                    return connection

        rate = (
            await self.rate(connection, merged_context)  # type: ignore[operator]
            if self._uses_rate_func
            else self.rate
        )
        if rate.unlimited:  # type: ignore[union-attr]
            return connection  # No throttling needed

        actual_cost = (  # type: ignore
            cost
            if cost
            else (
                await self.cost(connection, merged_context)  # type: ignore[operator]
                if self._uses_cost_func
                else self.cost
            )
        )
        if actual_cost < 0:  # type: ignore[operator]
            raise ValueError("cost cannot be negative")
        elif actual_cost == 0:
            return connection  # No cost, no throttling needed

        backend = self.get_backend(connection)
        connection_id = await self.get_connection_id(
            connection, backend, merged_context
        )
        if connection_id is EXEMPTED:
            return connection  # Exempted from throttling

        key = self.get_namespaced_key(connection, connection_id, merged_context)
        try:
            wait_ms = await self.strategy(key, rate, backend, actual_cost)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sys.stderr.write(
                f"Warning: An error occurred while utilizing strategy '{self.strategy!r}': {exc}\n"
            )
            sys.stderr.flush()
            wait_ms = await self._handle_error(
                connection,
                exc=exc,
                cost=actual_cost,  # type: ignore
                rate=rate,  # type: ignore
                backend=backend,
                context=merged_context,
            )

        wait_ms = (
            max(wait_ms, self.min_wait_period) if self.min_wait_period else wait_ms
        )
        if wait_ms:
            handle_throttled = self.handle_throttled or backend.handle_throttled
            # Mark connection as throttled
            setattr(connection.state, THROTTLED_STATE_KEY, True)
            await handle_throttled(connection, wait_ms, self, merged_context)  # type: ignore[arg-type]
            return connection

        # Mark connection as not throttled
        setattr(connection.state, THROTTLED_STATE_KEY, False)
        return connection

    async def __call__(
        self, connection: HTTPConnectionT, *args: typing.Any, **kwargs: typing.Any
    ) -> HTTPConnectionT:
        """
        Alias for `hit(...)` to allow the throttle instance to be called directly.

        Most especially, this enables the throttle to be used as a dependency in FastAPI routes,
        without it showing up in the OpenAPI Schema or route signature, and interferring with how FastAPI
        parses the request body or query parameters.

        :param connection: The HTTP connection to throttle.
        :param args: Positional arguments to pass to `hit(...)`.
        :param kwargs: Keyword arguments to pass to `hit(...)`.
        :return: The throttled HTTP connection.
        """
        return await self.hit(connection, *args, **kwargs)

    async def stat(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        """
        Get the current throttling strategy statistics for the connection.

        :param connection: The HTTP connection to get statistics for.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the connection for throttling purposes.
        :return: A `StrategyStat` object containing the current throttling strategy statistics,
            or None if the connection is not being throttled or the throttle strategy does not support stats
        """
        # We check if the strategy has a `get_stat` method. This is to ensure backward compatibility
        # with the defined `ThrottleStrategy` type which does not include `get_stat(...)`.
        if not hasattr(self.strategy, "get_stat"):
            return None

        backend = self.get_backend(connection)
        identifier = self.identifier or backend.identifier
        if (connection_id := await identifier(connection)) is EXEMPTED:
            return None

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context
        key = self.get_namespaced_key(connection, connection_id, merged_context)
        stat = await self.strategy.get_stat(key, self.rate, backend)  # type: ignore[attr-defined, arg-type]
        return stat

    async def check(
        self,
        connection: HTTPConnectionT,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        """
        **Best-effort** check if there's sufficient quota to proceed without consuming quota.

        This performs a non-consuming check of the throttle's current state.
        Useful for pre-checking before expensive operations.

        Note:
        This should be used as a best-effort pre-check only. The actual
        quota may change between this check and the eventual consumption
        (classic Time-of-Check to Time-of-Use issue).
        A way to avoid this is to ensure that the throttle is only used for one route/operation,
        and no other. Or best, throttle optimistically and handle rejections gracefully.

        :param connection: The HTTP connection to check.
        :param cost: Cost to check against. If None, uses the throttle's default cost.
        :param context: Additional throttle context.
        :return: True if sufficient quota is available to proceed, False otherwise.
            If the throttle's state cannot be determined (e.g., strategy doesn't
            support stats), returns True (optimistic).

        Example:
        ```python
        if await throttle.check(request, cost=5):
            # Sufficient quota, proceed with operation
            result = await expensive_operation()
            await throttle(request, cost=5)  # Actually consume quota
        else:
            raise HTTPException(429, "Rate limit would be exceeded")
        ```
        """
        stat = await self.stat(connection, context)
        if stat is None:
            return True  # Can't determine, assume available (optimistic)

        # Resolve cost
        check_cost: int
        if cost is not None:
            check_cost = cost
        elif self._uses_cost_func:
            if context:
                merged_context = self._default_context.copy()
                merged_context.update(context)
            else:
                merged_context = self._default_context
            check_cost = await self.cost(connection, merged_context)  # type: ignore[operator]
        else:
            check_cost = self.cost  # type: ignore[assignment]

        return stat.hits_remaining >= check_cost

    def set_error_handler(
        self,
        handler: ThrottleErrorHandler[HTTPConnectionT, ThrottleExceptionInfo],
    ) -> None:
        """
        Set a custom error handler for the throttle.

        :param handler: The custom error handler to set.
        """
        self._error_callback = handler

    def quota(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        apply_on_error: ApplyOnError = False,
        apply_on_exit: bool = True,
        lock: typing.Union[bool, str, None] = None,
        lock_config: typing.Optional[LockConfig] = None,
        parent: typing.Optional[typing.Any] = None,
    ):
        """
        Create a quota context bound to this throttle for deferred quota consumption.

        The context is bound to this throttle, meaning calling `quota()` without
        arguments will automatically use this throttle. You can still add other
        throttles explicitly by passing them as arguments.

        Quota entries are queued within the context and only consumed on apply/exit.
        This enables atomic quota consumption of multiple operations and conditional
        consumption based on operation success.

        **Cost Aggregation**: Consecutive calls with the same throttle and retry
        configuration automatically aggregate costs for efficiency.

        :param connection: The HTTP connection to throttle.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the connection for throttling purposes.
        :param apply_on_error: Whether to consume quota even when an exception occurs.
            - `False` (default): Don't consume on any exception
            - `True`: Consume on all exceptions
            - `tuple[Exception, ...]`: Consume only for these exception types
        :param apply_on_exit: Whether to auto-consume on successful context exit.
            Set to `False` for nested contexts that should only consume with parent.
        :param lock: Controls locking to prevent race conditions.
            - `None` (default): Use this throttle's UID as the lock key
            - `True`: Same as None (use this throttle's UID as lock key)
            - `False`: Disable locking (not recommended for concurrent scenarios)
            - `str`: Use the provided string as the lock key

            When enabled, the lock is acquired on context entry and released on exit,
            ensuring the entire quota context is atomic. Keep operations fast.
        :param lock_config: Configuration for lock acquisition (ttl, blocking, blocking_timeout).
            See `LockConfig` TypedDict for options.
        :param parent: Parent quota context for nested contexts.
            Child consumption is deferred to parent's consumption.
        :return: A `QuotaContext` context manager bound to this throttle.

        Example (using owner throttle with cost aggregation):

        ```python
        async with throttle.quota(conn) as quota:
            # Lock is acquired here using throttle.uid as key

            # Uses the owner throttle (no throttle argument needed)
            await quota(cost=2)          # Entry 1: cost=2
            await quota(cost=3)          # Aggregated into Entry 1: cost=5
            await quota()                # Aggregated into Entry 1: cost=6

            # Can still use other throttles
            await quota(other_throttle, cost=1)  # Entry 2: different throttle

            if not await quota.check():
                raise HTTPException(429, "Rate limit exceeded")

            result = await expensive_operation()  # Keep this fast!

        # On successful exit: quota consumed, then lock released
        # On exception: quota NOT consumed (unless apply_on_error=True)
        ```

        Example with custom lock key and config:

        ```python
        async with throttle.quota(
            conn,
            lock="user:123:api_calls",
            lock_config={"ttl": 30, "blocking_timeout": 5}
        ) as quota:
            await quota(cost=2)
            await process()
        ```

        Nested contexts:

        ```python
        async with throttle.quota(conn) as parent:
            # Lock acquired by parent
            await parent(cost=2)

            async with parent.nested() as child:
                # Child doesn't acquire its own lock (under parent's lock)
                await child(cost=1)
            # Child merges into parent's queue

        # Lock released after all quota consumed
        ```

        """
        from traffik.quotas import QuotaContext

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context
        return QuotaContext(
            connection=connection,
            context=merged_context,
            owner=self,
            apply_on_error=apply_on_error,
            apply_on_exit=apply_on_exit,
            lock=lock,
            lock_config=lock_config,
            parent=parent,
        )

    def add_rules(
        self, target_uid: str, /, *rules: ThrottleRule[HTTPConnectionT]
    ) -> None:
        """
        Add rules that gate another throttle's application.

        Rules are checked conjunctively on the target throttle's `hit(...)` call.
        If any rule returns `False`, the target throttle is skipped for that
        connection. This lets one throttle selectively bypass another for
        certain methods, paths, or custom predicates.

        :param target_uid: The UID of the throttle to attach rules to.
        :param rules: One or more `ThrottleRule` instances to add.
        :raises `ConfigurationError`: If `target_uid` is not registered.

        Example Usage - Bypassing the global throttle for GET requests:

        ```python
        from traffik.throttles import HTTPThrottle
        from traffik.registry import ThrottleRule

        # Global throttle: 100 req/min across all methods
        global_throttle = HTTPThrottle(uid="global", rate="100/min")

        # Write throttle: stricter 20 req/min for mutations only
        write_throttle = HTTPThrottle(uid="writes", rate="20/min")

        # Add a rule to the global throttle so it only applies to write methods,
        # effectively bypassing it for reads.
        write_throttle.add_rules(
            "global",
            ThrottleRule(methods={"POST", "PUT", "PATCH", "DELETE"}),
        )

        # Now on a GET request:
        #   - global_throttle.hit(...) checks the rule -> method not in
        #     {"POST","PUT","PATCH","DELETE"} -> rule returns False -> skipped
        #   - write_throttle.hit(...) runs normally
        #
        # On a POST request:
        #   - global_throttle.hit(...) checks the rule -> method in set
        #     -> rule returns True -> throttle applies
        #   - write_throttle.hit(...) also applies
        ```
        """
        self.registry.add_rules(target_uid, *rules)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.uid!s} rate={self.rate!r} cost={self.cost!r}>"


def _make_throttle_signature(
    throttle: Throttle[typing.Any],
    connection_param_name: str,
    target_method: str = "__call__",
    include_response: bool = False,
    response_param_name: str = "response",
    return_annotation: typing.Optional[typing.Type] = None,
) -> inspect.Signature:
    """
    Create a custom signature for the `Throttle` object to improve FastAPI integration.

    This function generates a signature that only includes the connection parameter,
    allowing FastAPI to correctly inject the request without misinterpreting other parameters as query parameters.

    :param throttle: The throttle instance to create the signature for.
    :param connection_param_name: The name of the connection parameter (e.g., "request" or "websocket").
    :param target_method: The method to inspect for the connection parameter. Defaults to "__call__".
    :param include_response: Whether to include a response parameter in the signature. Defaults to False.
    :param response_param_name: The name of the response parameter if included. Defaults to "response".
    :param return_annotation: The return annotation for the signature. If None, defaults to the throttle's connection type.
    :return: An `inspect.Signature` object representing the custom signature.
    """
    method = getattr(throttle, target_method, None)
    if method is None:
        raise ValueError(f"Throttle instance must have a `{target_method}` method")

    method_signature = inspect.signature(method)
    if connection_param_name not in method_signature.parameters:
        raise ValueError(
            f"Connection parameter '{connection_param_name}' not found in throttle's `{target_method}` signature"
        )

    connection_param = method_signature.parameters[connection_param_name]
    param_type = connection_param.annotation
    connection_type = throttle.connection_type
    # If the connection parameter already has a concrete annotation type,
    # but its not compatible with the throttle's connection type, raise an error to avoid confusion.
    is_concrete_param_type = param_type is not inspect.Parameter.empty and isinstance(
        param_type, type
    )
    if is_concrete_param_type and not issubclass(param_type, connection_type):
        raise ValueError(
            f"Connection parameter '{connection_param_name}' has an incompatible annotation type "
            f"{param_type!r} that is not compatible with the throttle's connection type {connection_type!r}. "
            "Please ensure the connection parameter is annotated with a compatible type."
        )
    elif (
        is_concrete_param_type
        and connection_type is HTTPConnection
        and issubclass(param_type, HTTPConnection)
        and param_type is not connection_type
    ):
        # If the connection parameter has a concrete annotation that is a subclass of `HTTPConnection`,
        # but the throttle's connection type is the generic `HTTPConnection`,
        # we can safely use the more specific parameter annotation as the connection type for the signature.
        connection_type = param_type

    parameters = [
        inspect.Parameter(
            name=connection_param_name,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=connection_type,
        )
    ]
    if include_response:
        parameters.append(
            inspect.Parameter(
                name=response_param_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=Response,
            )
        )
    return inspect.Signature(
        parameters=parameters,
        return_annotation=return_annotation
        if return_annotation is not None
        else connection_type,
    )


def is_throttled(connection: HTTPConnection) -> bool:
    """
    Check if the connection has been throttled.

    :param connection: The HTTP connection to check.
    :return: True if the connection has been throttled, False otherwise.
    """
    return getattr(connection.state, THROTTLED_STATE_KEY, False)


class HTTPThrottle(Throttle[Request]):
    """HTTP connection throttle"""

    __slots__ = "use_method"

    def __init__(
        self,
        uid: str,
        rate: RateType[Request],
        identifier: typing.Optional[ConnectionIdentifier[Request]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[Request, "HTTPThrottle"]  # type: ignore[arg-type]
        ] = None,
        strategy: typing.Optional[ThrottleStrategy] = None,
        backend: typing.Optional[ThrottleBackend[typing.Any, Request]] = None,
        cost: CostType[Request] = 1,
        dynamic_backend: bool = False,
        min_wait_period: typing.Optional[int] = None,
        headers: typing.Optional[
            typing.Mapping[str, typing.Union[Header[Request], str]]
        ] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[Request, ThrottleExceptionInfo],
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        registry: typing.Optional[ThrottleRegistry] = None,
        rules: typing.Optional[typing.Iterable[ThrottleRule[Request]]] = None,
        cache_ids: bool = True,
        use_method: bool = True,
    ) -> None:
        """
        Initialize the throttle.

        :param uid: Unique identifier for the throttle instance. This ensures that
            multiple instances of the same throttle can coexist without conflicts.
            It also allows for persistent storage of throttle state across application
            restarts or deployments.

        :param rate: Rate limit definition. This can be provided as a Rate object
            or as a string in the format "limit/period" (e.g., "100/m" for 100 requests
            per minute).

        :param identifier: Connected client identifier generator.
            If not provided, the throttle backend's identifier will be used.
            This identifier is used to uniquely identify the client connection
            and track its throttling state. Identifiers can be based on various factors,
            such as IP address, user ID, API key, etc.
            Identifiers should be efficient to compute and provide sufficient uniqueness to avoid collisions.
            NOTE: Identifiers can be used to implement any exemption logic (e.g., whitelisting certain clients),
            Just return `EXEMPTED` from the identifier to exempt a connection from throttling.

        :param handle_throttled: Handler to call when the client connection is throttled.
            If provided, it will override the default connection throttled handler
            defined for the throttle backend.
            This handler is responsible for notifying the client about the throttling
            and can implement custom logic, such as sending a specific response or logging.

        :param strategy: Throttling strategy to use. If not provided, the default strategy will be used.
            The strategy defines how the throttling is applied, such as fixed window,
            sliding window, or token bucket.

        :param backend: The throttle backend to use for storing throttling data.
            If not provided, the default backend will be used.
            The backend is responsible for managing the throttling state,
            including checking the current throttling status, updating it, and handling
            throttled connections.
            If `dynamic_backend` is True, the backend will be resolved from the request context
            on each call, allowing for dynamic backend resolution based on the request context.

        :param cost: The cost/weight of each request. This allows for different requests
            to have different impacts on the throttling state. For example, a request that performs a
            resource-intensive operation might have a higher cost than a simple read request.

        :param dynamic_backend: If True, resolves backend from the application/request/local context
            on each request instead of caching it. Designed for multi-tenant applications where the backend
            is determined at runtime from request data (JWT, headers, etc.).

            **Use cases:**
            - Multi-tenant SaaS: Different backends per tenant tier
            - Environment-based routing: Production vs staging backends
            - Testing: Nested context managers with different backends

            **Requirements:**
            - Backend must be set via lifespan or context manager in middleware **before** throttle is called
            - Cannot be combined with explicit `backend` parameter

            **Trade-offs:**
            - Adds ~1-20ms overhead per request (backend resolution)
            - Data fragmentation risk if context switching is inconsistent
            - Use explicit `backend` parameter for simple shared storage

            See documentation on "Context-Aware Backends" section for full examples.

        :param min_wait_period: The minimum allowable wait period (in milliseconds) for a throttled connection.
        :param headers: Optional headers to include in throttling responses. A use case can
            be to include additional throttle/throttling information in the response headers.
            This will be merged with any headers provided in `context`.

        :param on_error: Strategy for handling errors during throttling.
            Can be one of the following:
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters and
                returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.

            If not provided, defaults to behavior defined by the backend or "throttle".

        :param context: Optional default context to use for all throttle calls. This can include any relevant information needed
            for context-aware throttling strategies. The context provided here will be merged with any context
            provided during individual throttle calls, with the call-specific context taking precedence in case of conflicts.

        :param registry: The registry this throttle should belong to and use. Defaults to `GLOBAL_REGISTRY`.
        :param rules: Optional rules that define if, and when this throttle should apply.
        :param cache_ids: Whether to cache connection IDs on the connection state.
            Defaults to True. Disable only for advanced use cases where connection IDs
            may change frequently during the connection's lifetime.

            Setting this to `True` is especially useful for long-lived connections
            like WebSockets where the connection does not change after establishment,
            and caching avoids redundant/expensive identifier computations.

        :param use_method: Whether to include the HTTP method in the scoped key for throttling.
            Defaults to True. If set to False, the throttle will ignore the HTTP method and only use the path for throttling.
            This can be useful if you want to apply the same throttling to all methods for a given path (e.g., GET, POST, etc.),
            rather than having separate throttling for each method.
        """
        super().__init__(
            uid=uid,
            rate=rate,
            identifier=identifier,
            handle_throttled=handle_throttled,
            strategy=strategy,
            backend=backend,
            cost=cost,
            dynamic_backend=dynamic_backend,
            min_wait_period=min_wait_period,
            headers=headers,
            on_error=on_error,
            context=context,
            registry=registry,
            rules=rules,
            cache_ids=cache_ids,
        )
        self.use_method = use_method

    def get_scoped_key(
        self,
        connection: Request,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        typ = connection.scope["type"]
        method = connection.scope["method"].upper() if self.use_method else ""
        path = connection.scope["path"]
        scope = context["scope"] if context else THROTTLE_DEFAULT_SCOPE
        return f"{typ}:{method}:{path}:{scope}"


RequestThrottle = HTTPThrottle  # Alias for semantic clarity


async def websocket_throttled(
    connection: WebSocket,
    wait_ms: WaitPeriod,
    throttle: Throttle[WebSocket],
    context: typing.Mapping[str, typing.Any],
) -> None:
    """
    Handler for throttled WebSocket connections.

    If the connection is established, it sends rate limit message to client without closing connection.
    Else, it raises the throttled exception.

    :param connection: The WebSocket connection that is throttled.
    :param wait_ms: The wait period in milliseconds before the client can send messages again.
    :param throttle: The throttle instance that triggered the throttling.
    :param context: Additional context for the throttled handler.
    :return: None
    """
    # If the connection is not yet established, do not attempt to send a message
    # Just raise the throttled exception
    if connection.application_state != WebSocketState.CONNECTED:
        await connection_throttled(connection, wait_ms, throttle, context)

    wait_seconds = math.ceil(wait_ms / 1000)
    try:
        await connection.send_json(
            {
                "type": "rate_limit",
                "error": "Too many messages",
                "retry_after": wait_seconds,
                **context.get("extras", {}),
            }
        )
    except RuntimeError as exc:
        # Connection was closed between check and send
        # Silently ignore since client is already disconnected
        sys.stderr.write(f"An error occurred while sending throttled message: {exc}\n")
        sys.stderr.flush()


class WebSocketThrottle(Throttle[WebSocket]):
    """WebSocket connection throttle"""

    __slots__ = ()

    def __init__(
        self,
        uid: str,
        rate: RateType[WebSocket],
        identifier: typing.Optional[ConnectionIdentifier[WebSocket]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[WebSocket, "WebSocketThrottle"]
        ] = None,
        strategy: typing.Optional[ThrottleStrategy] = None,
        backend: typing.Optional[ThrottleBackend[typing.Any, WebSocket]] = None,
        cost: CostType[WebSocket] = 1,
        dynamic_backend: bool = False,
        min_wait_period: typing.Optional[int] = None,
        headers: typing.Optional[
            typing.Mapping[str, typing.Union[Header[WebSocket], str]]
        ] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[WebSocket, ThrottleExceptionInfo],
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        registry: typing.Optional[ThrottleRegistry] = None,
        rules: typing.Optional[typing.Iterable[ThrottleRule[WebSocket]]] = None,
        cache_ids: bool = True,
    ) -> None:
        if handle_throttled is None:
            handle_throttled = websocket_throttled
        super().__init__(
            uid=uid,
            rate=rate,
            identifier=identifier,
            handle_throttled=handle_throttled,
            strategy=strategy,
            backend=backend,
            cost=cost,
            dynamic_backend=dynamic_backend,
            min_wait_period=min_wait_period,
            headers=headers,
            on_error=on_error,
            context=context,
            registry=registry,
            rules=rules,
            cache_ids=cache_ids,
        )

    def get_scoped_key(
        self,
        connection: WebSocket,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        typ = connection.scope["type"]
        path = connection.scope["path"]
        scope = context["scope"] if context else THROTTLE_DEFAULT_SCOPE
        return f"{typ}:{path}:{scope}"


@typing.overload
def throttled(
    *throttles: Throttle[HTTPConnectionT],
) -> typing.Callable[
    [typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]],
    typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
]: ...


@typing.overload
def throttled(
    *throttles: Throttle[HTTPConnectionT],
    route: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]: ...


def throttled(
    *throttles: Throttle[HTTPConnectionT],
    route: typing.Optional[
        typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]
    ] = None,
) -> typing.Union[
    typing.Callable[
        [typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]],
        typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    ],
    typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
]:
    """
    Throttles connections to decorated route using the provided throttle(s).

    **Note! The decorated route must have an `HTTPConnection` (e.g., `Request`, `WebSocket`) parameter for the throttle(s) to work.**
    For FastAPI routes, use `traffik.decorators.throttled` to bypass this constraint.

    :param throttles: A single throttle or a sequence of throttles to apply to the route.
    :param route: The route to be throttled. If not provided, returns a decorator that can be used to apply throttling to routes.
    :return: A decorator that applies throttling to the route, or the wrapped route if `route` is provided.

    Example:

    ```python
    from starlette import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from traffik import throttled, HTTPThrottle

    sustained_throttle = HTTPThrottle(uid="sustained", rate="100/min")
    burst_throttle = HTTPThrottle(uid="burst", rate="20/sec")

    app = Starlette()

    @app.route("/throttled")
    @throttled(burst_throttle, sustained_throttle)
    async def route(request: Request):
        return JSONResponse({"message": "Limited route 1"})

    ```
    """
    if len(throttles) == 0:
        raise ValueError("At least one throttle must be provided.")

    if len(throttles) > 1:

        async def throttle(connection: HTTPConnectionT) -> HTTPConnectionT:
            nonlocal throttles
            for t in throttles:
                await t(connection)
            return connection
    else:
        throttle = throttles[0]  # type: ignore[assignment]

    def _decorator(
        route: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    ) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
        if asyncio.iscoroutinefunction(route):
            route = typing.cast(typing.Callable[P, typing.Awaitable[R]], route)

            @functools.wraps(route)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                connection = None
                for arg in args:
                    if isinstance(arg, HTTPConnection):
                        connection = arg
                        break
                if connection is None:
                    for kwarg in kwargs.values():
                        if isinstance(kwarg, HTTPConnection):
                            connection = kwarg
                            break

                if connection is None:
                    raise ValueError(
                        "No HTTP connection found in route parameters for throttling."
                    )

                await throttle(connection)  # type: ignore[arg-type]
                return await route(*args, **kwargs)  # type: ignore[misc]

            return async_wrapper

        route = typing.cast(typing.Callable[P, R], route)

        @functools.wraps(route)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            connection = None
            for arg in args:
                if isinstance(arg, HTTPConnection):
                    connection = arg
                    break
            if connection is None:
                for kwarg in kwargs.values():
                    if isinstance(kwarg, HTTPConnection):
                        connection = kwarg
                        break

            if connection is None:
                raise ValueError(
                    "No HTTP connection found in route parameters for throttling."
                )

            loop = asyncio.get_event_loop()
            loop.run_until_complete(throttle(connection))  # type: ignore
            return route(*args, **kwargs)

        return wrapper

    if route is not None:
        return _decorator(route)
    return _decorator


async def _resolve_headers(
    headers: typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]],
    connection: HTTPConnectionT,
    throttle: Throttle[HTTPConnectionT],
    stat: typing.Optional[StrategyStat] = None,
    context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
) -> typing.Dict[str, str]:
    """
    Resolve headers for a throttled response based on the provided header definitions and the throttle's current state.

    This function takes a mapping of header keys to either static string values or `Header` instances
    that dynamically resolves based on the throttle's state.
    It retrieves the current throttling statistics for the connection if not provided, and uses them to resolve any dynamic headers.

    NOTE! If the throttling statistics cannot be gotten and it is not explcitly provided, then `Header`
    instances which need to be resolved are skipped.

    :param headers: A mapping of header keys to either static string values or `Header` instances.
    :param connection: The HTTP connection for which to resolve the headers.
    :param throttle: The throttle instance to retrieve the current throttling statistics.
    :param stat: Optional pre-fetched throttling statistics to use for header resolution. If not provided, the function will fetch the stats from the throttle.
    :param context: Additional context to pass when retrieving the throttle statistics.
        This can include any relevant information needed to uniquely identify the connection for throttling purposes.
    :return: The resolved headers as a dictionary of strings.
    """
    if not headers:
        return {}

    stat = stat or await throttle.stat(connection, context)
    _disable = Header.DISABLE
    if stat is not None:
        out = {}
        for key, value in headers.items():
            # Ensure to use an identity check (`is`) here.
            # has any header hash may collide and match `Header.DISABLE`
            # If we use `==`. Which defeat the purpose of `Header.DISABLE`
            # as a sentinel
            if value is _disable:
                continue
            elif isinstance(value, str):
                out[key] = value
            elif value._is_static:
                out[key] = value._raw  # type: ignore[assignment]
            elif not value.check(connection, stat, context):
                continue
            else:
                out[key] = value.resolve(connection, stat, context)
        return out

    # If stat is None, we cannot resolve dynamic headers, but we can still return static headers
    return {
        k: v for k, v in headers.items() if isinstance(v, str) and v is not _disable
    }
