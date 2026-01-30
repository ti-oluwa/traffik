"""Throttles for HTTP and WebSocket connections."""

import math  # noqa: I001
import typing

from typing_extensions import TypedDict

from starlette.requests import Request, HTTPConnection
from starlette.websockets import WebSocket

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import BackendError, ConfigurationError
from traffik.rates import Rate
from traffik.strategies import default_strategy
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    ThrottleErrorHandler,
    HTTPConnectionT,
    StrategyStat,
    Stringable,
    EXEMPTED,
    WaitPeriod,
    RateType,
    CostType,
)

__all__ = ["BaseThrottle", "HTTPThrottle", "WebSocketThrottle"]

ThrottleStrategy = typing.Callable[
    [Stringable, Rate, ThrottleBackend[typing.Any, HTTPConnectionT], int],  # type: ignore[misc]
    typing.Awaitable[WaitPeriod],
]
"""
A callable that implements a throttling strategy.

Takes a key, a Rate object, the throttle backend, and cost, and returns the wait period in seconds.
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
    throttle: "BaseThrottle[HTTPConnection]"
    """The throttle instance used during the throttling operation."""


class BaseThrottle(typing.Generic[HTTPConnectionT]):
    """Base throttle class"""

    def __init__(
        self,
        uid: str,
        rate: RateType[HTTPConnectionT],
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        strategy: typing.Optional[ThrottleStrategy] = None,
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]] = None,
        cost: CostType[HTTPConnectionT] = 1,
        context_backend: bool = False,
        min_wait_period: typing.Optional[int] = None,
        headers: typing.Optional[typing.Mapping[str, str]] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo],
            ]
        ] = None,
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
            If `context_backend` is True, the backend will be resolved from the request context
            on each call, allowing for dynamic backend resolution based on the request context.
        :param cost: The cost/weight of each request. This allows for different requests
            to have different impacts on the throttling state. For example, a request that performs a
            resource-intensive operation might have a higher cost than a simple read request.
        :param context_backend: If True, resolves backend from (request) context on each call instead of caching.
            Use only when backend choice must be determined dynamically at runtime.

            This feature is designed for advanced use cases where the same throttle instance
            needs to use different backends based on runtime conditions.

            Valid use cases:
            - Multi-tenant applications where tenant is determined from JWT/headers at runtime
            - Request-based backend selection (e.g., different storage for different request types)
            - Advanced testing scenarios with nested backend context managers

        Example (Multi-tenant):

        ```python
        # Tenant determined at runtime from request headers
        tenant_throttle = HTTPThrottle(
            uid="api_quota",
            rate="1000/h",
            context_backend=True
        )

        async def tenant_middleware(request, call_next):
            tenant = extract_tenant_from_auth_header(request.headers["Authorization"])

            if tenant == "premium":
                backend = RedisBackend("redis://premium-redis:6379/0")
            elif tenant == "enterprise":
                backend = RedisBackend("redis://enterprise-redis:6379/0")
            else:
                backend = InMemoryBackend()  # Free tier

            async with backend():
                return await call_next(request)
        ```

        Example (Testing with nested contexts):

        ```python
        throttle = HTTPThrottle(uid="test", limit=3, seconds=5, context_backend=True)

        async with backend_a():
            await throttle.hit(request)  # Uses backend_a

            async with backend_b():
                await throttle.hit(request)  # Switches to backend_b

            await throttle.hit(request)  # Back to backend_a
        ```

        IMPORTANT: Do not use for simple shared storage across services.
        For shared backends, use explicit backend configuration instead:

        ```python
        # GOOD: Explicit shared backend
        shared_backend = RedisBackend("redis://shared-redis:6379/0")
        user_quota = HTTPThrottle(uid="user_quota", rate="1000/h", backend=shared_backend)

        # BAD: Unnecessary dynamic resolution
        user_quota = HTTPThrottle(uid="user_quota", rate="1000/h", context_backend=True)
        ```

            **WARNING:** This feature adds complexity and slight performance overhead.
            - Cannot be used with explicit backend parameter
            - May cause data fragmentation if context switching is inconsistent
            - Harder to debug due to dynamic backend resolution
            - Only use when you absolutely need runtime backend switching

            For most use cases, explicit backend configuration is simpler and more efficient.

        :param min_wait_period: The minimum allowable wait period (in milliseconds) for a throttled connection.
        :param headers: Optional headers to include in throttling responses. A use case can
            be to include additional throttle/throttling information in the response headers.
        :param on_error: Strategy for handling errors during throttling.
            Can be one of the following:
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters and
                returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.

            If not provided, defaults to behavior defined by the backend or "throttle".
        """
        if not uid or not isinstance(uid, str):
            raise ValueError("uid is required and must be a non-empty string")

        if context_backend and backend is not None:
            raise ValueError(
                "Cannot specify an explicit backend with `context_backend=True`"
            )

        self.uid = uid
        self._uses_rate_func = callable(rate)
        self.rate = Rate.parse(rate) if isinstance(rate, str) else rate

        self._uses_cost_func = callable(cost)
        self.cost = cost
        self.uses_fixed_backend = not context_backend
        self.strategy = strategy or default_strategy
        self.min_wait_period = min_wait_period
        self.headers = dict(headers or {})

        # Only set backend for non-context backend throttles
        if not context_backend:
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
            on_error_ = on_error

        self._error_callback: typing.Optional[
            ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo]
        ] = None
        if callable(on_error_):
            self._error_callback = on_error_
            # Just set it for reference
            self.on_error = on_error_
        elif isinstance(on_error_, str) and on_error_ in {"allow", "throttle", "raise"}:
            self.on_error = on_error_
        elif on_error_ is None and not self.uses_fixed_backend:
            # We'll handle this in `_handle_error(...)` since backend is dynamic
            self.on_error = None
        else:
            raise ValueError(
                f"Invalid `on_error` value: {on_error_!r}. "
                "Must be 'allow', 'throttle', 'raise', or a callable."
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
            if self.uses_fixed_backend:
                self.backend = backend
        else:
            backend = self.backend  # type: ignore[assignment]
        return backend  # type: ignore[return-value]

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
            needed to uniquely identify the request for throttling purposes.
        :return: The namespaced throttling key for the connection.
        """
        throttle_key = self.get_key(connection, context)
        namespaced_key = f"{self.uid}:{str(connection_id)}:{throttle_key}"
        return namespaced_key

    async def _handle_error(
        self,
        connection: HTTPConnectionT,
        exc: Exception,
        cost: int,
        rate: Rate,
        backend: ThrottleBackend[typing.Any, HTTPConnectionT],
    ) -> WaitPeriod:
        """
        Handle errors during throttling based on the configured strategy.

        :param connection: The HTTP connection being throttled.
        :param exc: The exception that occurred during throttling.
        :param cost: The cost/weight of the request that caused the error.
        :param rate: The rate associated with the throttling operation.
        :param backend: The backend used during the throttling operation.
        :param throttle: The throttle instance.
        :return: The wait period in milliseconds.
        """
        if self._error_callback:
            exc_info = ExceptionInfo(
                exception=exc,
                connection=connection,
                cost=cost,
                rate=rate,
                backend=backend,  # type: ignore
                throttle=self,  # type: ignore
            )
            return await self._error_callback(connection, exc_info)
        elif self.on_error == "allow":
            return 0.0
        elif self.on_error == "throttle":
            return self.min_wait_period or 1000.0  # Default to 1000ms
        elif not self.uses_fixed_backend and self.on_error is None:
            # For dynamic backend throttles, check backend's on_error
            backend_on_error = backend.on_error
            if callable(backend_on_error):
                exc_info = ExceptionInfo(
                    exception=exc,
                    connection=connection,
                    cost=cost,
                    rate=rate,
                    backend=backend,  # type: ignore
                    throttle=self,  # type: ignore
                )
                return await backend_on_error(connection, exc_info)
            elif backend_on_error == "allow":
                return 0.0
            elif backend_on_error == "throttle":
                return self.min_wait_period or 1000.0  # Default to 1000ms
            # Falls through to raise below

        # `on_error` is "raise"
        raise exc

    async def __call__(
        self,
        connection: HTTPConnectionT,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> HTTPConnectionT:
        """
        Throttle the connection based on the limit and time period.

        Records a hit for the connection and applies throttling if necessary.

        :param connection: The HTTP connection to throttle.
        :param cost: The cost/weight of this request (overrides default cost if provided).
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the request for throttling purposes.
        :return: The throttled HTTP connection.
        """
        rate = (
            await self.rate(connection, context) if self._uses_rate_func else self.rate  # type: ignore
        )
        if rate.unlimited:  # type: ignore[attr-defined]
            return connection  # No throttling applied

        cost_ = (  # type: ignore
            cost
            if cost
            else (
                await self.cost(connection, context)  # type: ignore
                if self._uses_cost_func
                else self.cost
            )
        )
        if cost_ <= 0:  # type: ignore
            raise ValueError("cost must be a positive integer")

        backend = self.get_backend(connection)
        identifier = self.identifier or backend.identifier
        if (connection_id := await identifier(connection)) is EXEMPTED:
            return connection  # Exempted from throttling

        namespaced_key = self.get_namespaced_key(connection, connection_id, context)
        try:
            wait_ms = await self.strategy(namespaced_key, rate, backend, cost_)  # type: ignore
        except (BackendError, TimeoutError) as exc:
            print(
                f"Warning: An error occurred while utilizing strategy '{self.strategy!r}': {exc}",
            )
            wait_ms = await self._handle_error(
                connection,
                exc=exc,
                cost=cost_,  # type: ignore
                rate=rate,  # type: ignore
                backend=backend,
            )

        wait_ms = (
            max(wait_ms, self.min_wait_period) if self.min_wait_period else wait_ms
        )
        if wait_ms:
            handle_throttled = self.handle_throttled or backend.handle_throttled
            context = dict(context or {})
            context.setdefault("headers", self.headers)
            await handle_throttled(connection, wait_ms, context)
        return connection

    hit = __call__  # Useful for explicitly recording a hit
    """Alias for the `__call__` method to record a throttle hit."""

    async def stat(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        """
        Get the current throttling statistics for the connection.

        :param connection: The HTTP connection to get statistics for.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the request for throttling purposes.
        :return: A `ThrottleStat` object containing the current throttling statistics,
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

        namespaced_key = self.get_namespaced_key(connection, connection_id, context)
        stat = await self.strategy.get_stat(namespaced_key, self.rate, backend)  # type: ignore[attr-defined]
        return stat

    def get_key(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        """
        Returns a unique throttling key for the connection.

        :param connection: The HTTP connection to throttle.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the request for throttling purposes.
        :return: The unique throttling key for the connection.
        """
        raise NotImplementedError

    def set_error_handler(
        self,
        handler: ThrottleErrorHandler[HTTPConnectionT, ExceptionInfo],
    ) -> None:
        """
        Set a custom error handler for the throttle.

        :param handler: The custom error handler to set.
        """
        self._error_callback = handler


class HTTPThrottle(BaseThrottle[Request]):
    """HTTP connection throttle"""

    def get_key(
        self,
        connection: Request,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        method = connection.scope["method"].upper()
        path = connection.scope["path"]
        return f"http:{method}:{path}"

    async def __call__(
        self,
        connection: Request,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> Request:
        return await super().__call__(connection, cost=cost, context=context)


async def websocket_throttled(
    connection: WebSocket,
    wait_ms: WaitPeriod,
    context: typing.Mapping[str, typing.Any],
) -> None:
    """
    Handler for throttled WebSocket connections.

    Sends rate limit message to client without closing connection.

    :param connection: The WebSocket connection that is throttled.
    :param wait_ms: The wait period in milliseconds before the client can send messages again.
    :param context: Additional context for the throttled handler.
    """
    wait_seconds = math.ceil(wait_ms / 1000)
    await connection.send_json(
        {
            "type": "rate_limit",
            "error": "Too many messages",
            "retry_after": wait_seconds,
            **context.get("extras", {}),
        }
    )


class WebSocketThrottle(BaseThrottle[WebSocket]):
    """WebSocket connection throttle"""

    def __init__(
        self,
        uid: str,
        rate: RateType[WebSocket],
        identifier: typing.Optional[ConnectionIdentifier[WebSocket]] = None,
        handle_throttled: typing.Optional[ConnectionThrottledHandler[WebSocket]] = None,
        strategy: typing.Optional[ThrottleStrategy] = None,
        backend: typing.Optional[ThrottleBackend[typing.Any, WebSocket]] = None,
        cost: CostType[WebSocket] = 1,
        context_backend: bool = False,
        min_wait_period: typing.Optional[int] = None,
        headers: typing.Optional[typing.Mapping[str, str]] = None,
        on_error: typing.Optional[
            typing.Union[
                typing.Literal["allow", "throttle", "raise"],
                ThrottleErrorHandler[WebSocket, ExceptionInfo],
            ]
        ] = None,
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
            context_backend=context_backend,
            min_wait_period=min_wait_period,
            headers=headers,
            on_error=on_error,
        )

    def get_key(
        self,
        connection: WebSocket,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        """
        Returns a unique throttling key for the `WebSocket` connection.

        :param connection: The `WebSocket` connection to throttle.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the request for throttling purposes.
            Provide an identifier called "key" in the context to differentiate throttling
            for different contexts within the same `WebSocket` connection.
        :return: The unique throttling key for the `WebSocket` connection.
        """
        path = connection.scope["path"]
        context_key = context.get("key", "default") if context else "default"
        return f"ws:{path}:{context_key}"

    async def __call__(
        self,
        connection: WebSocket,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> WebSocket:
        """
        Calls the throttle for a `WebSocket` connection.

        :param connection: The `WebSocket` connection to throttle.
        :param context_key: Optional context key to differentiate throttling
            for different contexts within the same `WebSocket` connection.
        :param cost: The cost/weight of this request (overrides default cost if provided).
        :param context: Additional throttle context. The context can include any relevant information
            needed to uniquely identify the request for throttling purposes.
            Provide an identifier called "key" in the context to differentiate throttling
            for different contexts within the same `WebSocket` connection.
        :return: The throttled `WebSocket` connection.
        """
        return await super().__call__(connection, cost=cost, context=context)

    async def stat(  # type: ignore[override]
        self,
        connection: WebSocket,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        """
        Get the current throttling statistics for the `WebSocket` connection.

        :param connection: The `WebSocket` connection to get statistics for.
        :param context: Additional throttle context. Can contain any relevant information needed
            to uniquely identify the request for throttling purposes.
            Provide an identifier called "key" in the context to differentiate throttling
            for different contexts within the same `WebSocket` connection.
        :return: A `ThrottleStat` object containing the current throttling statistics,
            or None if the connection is not being throttled or the throttle strategy does not support stats.
        """
        return await super().stat(connection, context=context)
