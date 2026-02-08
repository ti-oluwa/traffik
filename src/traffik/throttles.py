"""Throttles for Starlette `HTTPConnection` types."""

import asyncio
import functools
import inspect
import math
import sys
import typing

from starlette.requests import HTTPConnection, Request
from starlette.websockets import WebSocket, WebSocketState
from typing_extensions import Self, TypedDict

from traffik.backends.base import (
    ThrottleBackend,
    connection_throttled,
    get_throttle_backend,
)
from traffik.exceptions import ConfigurationError
from traffik.rates import Rate
from traffik.strategies import default_strategy
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
    "WebSocketThrottle",
    "is_throttled",
    "ExceptionInfo",
    "websocket_throttled",
    "throttled",
]

THROTTLED_STATE_KEY = "__traffik_throttled_state__"
CONNECTION_IDS_CONTEXT_KEY = "__traffik_connection_ids__"
DEFAULT_SCOPE = "default"

ThrottleStrategy = typing.Callable[
    [Stringable, Rate, ThrottleBackend[typing.Any, HTTPConnection], int],  # type: ignore[misc]
    typing.Awaitable[WaitPeriod],
]
"""
A callable that implements a throttling strategy.

Takes a key, a Rate object, the throttle backend, and cost, and returns the wait period in milliseconds.
"""


class ExceptionInfo(TypedDict):
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


class Throttle(typing.Generic[HTTPConnectionT]):
    """Base connection throttle class"""

    __slots__ = (
        "uid",
        "rate",
        "identifier",
        "backend",
        "handle_throttled",
        "strategy",
        "cost",
        "min_wait_period",
        "_default_context",
        "cache_ids",
        "on_error",
        "uses_fixed_backend",
        "_uses_rate_func",
        "_uses_cost_func",
        "_error_callback",
        "__signature__",
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
        headers: typing.Optional[typing.Mapping[str, str]] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo],
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        cache_ids: bool = True,
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
        :param cache_ids: Whether to cache connection IDs on the connection state.
            Defaults to True. Disable only for advanced use cases where connection IDs
            may change frequently during the connection's lifetime.

            Setting this to `True` is especially useful for long-lived connections
            like WebSockets where the connection does not change after establishment,
            and caching avoids redundant/expensive identifier computations.
        """
        if not uid or not isinstance(uid, str):
            raise ValueError("uid is required and must be a non-empty string")

        if dynamic_backend and backend is not None:
            raise ValueError(
                "Cannot specify an explicit backend with `dynamic_backend=True`"
            )

        self.uid = uid
        self._uses_rate_func = callable(rate)
        self.rate = Rate.parse(rate) if isinstance(rate, str) else rate

        self._uses_cost_func = callable(cost)
        self.cost = cost
        self.uses_fixed_backend = not dynamic_backend
        self.strategy = strategy or default_strategy
        self.min_wait_period = min_wait_period

        # Ensure that we copy the context to avoid potential mutation issues from outside after initialization
        # Never modify `_default_context` after initialization. It's unsafe.
        self._default_context = dict(context or {})
        # Set 'scope' in default context if not already set.
        self._default_context.setdefault("scope", DEFAULT_SCOPE)
        if "headers" not in self._default_context:
            self._default_context["headers"] = dict(headers or {})
        else:
            # Merge headers into context if headers are provided both in `headers` and `context`
            self._default_context["headers"] = {
                **self._default_context["headers"],
                **(headers or {}),
            }

        self.cache_ids = cache_ids

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
            ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo]
        ] = None
        if callable(on_error_):
            self._error_callback = on_error_
            # Just set it for reference
            self.on_error = on_error_
        elif isinstance(on_error_, str) and on_error_ in {"allow", "throttle", "raise"}:
            self.on_error = on_error_  # type: ignore[assignment]
        elif on_error_ is None and not self.uses_fixed_backend:
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
        call_signature = inspect.signature(self.__call__)
        self.__signature__ = call_signature.replace(
            parameters=[
                param
                for param in call_signature.parameters.values()
                if param.kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            ]
        )

    @property
    def is_dynamic(self) -> bool:
        """Returns True if the throttle uses a dynamic backend, False otherwise."""
        return not self.uses_fixed_backend

    @property
    def headers(self) -> typing.Mapping[str, str]:
        """
        Returns the default headers used by the throttle for throttling responses.

        These headers can be overridden or extended by providing a 'headers' key
        in the context during throttle calls.
        """
        return self._default_context["headers"]  # type: ignore[return-value]

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
            if self.uses_fixed_backend:
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
        elif not self.uses_fixed_backend and self.on_error is None:
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

        # We have to try as much as possible to avoid copying the context in downstream calls.
        # So that mutations to the context in those calls reflect back here. That's the whole point
        # of having a context in the first place. Hence we only copy it once here. Although, there
        # may be some edge cases where downstream mutations should not affect this (global) context.
        # Then in that case its justifiable.
        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context

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
        # with the defined `ThrottleStrategy` type which does not include `get_stat`.
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
        handler: ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo],
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

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.uid!r} rate={self.rate!r} cost={self.cost!r}>"


def is_throttled(connection: HTTPConnection) -> bool:
    """
    Check if the connection has been throttled.

    :param connection: The HTTP connection to check.
    :return: True if the connection has been throttled, False otherwise.
    """
    return getattr(connection.state, THROTTLED_STATE_KEY, False)


class HTTPThrottle(Throttle[Request]):
    """HTTP connection throttle"""

    __slots__ = ()

    def get_scoped_key(
        self,
        connection: Request,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        method = connection.scope["method"].upper()
        path = connection.scope["path"]
        scope = context["scope"] if context else DEFAULT_SCOPE
        return f"http:{method}:{path}:{scope}"

    # Redefine signatures with concrete types so that they can
    # resolved by the FastAPI dependency injection systems
    async def hit(
        self,
        connection: Request,
        *,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> Request:
        return await super().hit(connection, cost=cost, context=context)

    async def __call__(
        self, connection: Request, *args: typing.Any, **kwargs: typing.Any
    ) -> Request:
        return await super().__call__(connection, *args, **kwargs)

    async def stat(
        self,
        connection: Request,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        return await super().stat(connection, context=context)

    async def check(
        self,
        connection: Request,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        return await super().check(connection, cost=cost, context=context)


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
        headers: typing.Optional[typing.Mapping[str, str]] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[WebSocket, ExceptionInfo],
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
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
            cache_ids=cache_ids,
        )

    def get_scoped_key(
        self,
        connection: WebSocket,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        path = connection.scope["path"]
        scope = context["scope"] if context else DEFAULT_SCOPE
        return f"ws:{path}:{scope}"

    async def hit(
        self,
        connection: WebSocket,
        *,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> WebSocket:
        """
        Throttle the `WebSocket` connection based on the limit and time period.

        :param connection: The `WebSocket` connection to throttle.
        :param cost: The cost/weight of this connection/message (overrides default cost if provided).
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the connection for throttling purposes.

        **Keys that can be set in the context:**
        - `"scope"`: A string to differentiate throttling for different contexts within the same `WebSocket` connection.
        - `"extras"`: A dictionary of extra data to include in the throttling message sent to the client.

        :return: The throttled `WebSocket` connection.
        """
        return await super().hit(connection, cost=cost, context=context)

    async def __call__(
        self, connection: WebSocket, *args: typing.Any, **kwargs: typing.Any
    ) -> WebSocket:
        return await super().__call__(connection, *args, **kwargs)

    async def stat(
        self,
        connection: WebSocket,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        return await super().stat(connection, context=context)

    async def check(
        self,
        connection: WebSocket,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        return await super().check(connection, cost=cost, context=context)


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
