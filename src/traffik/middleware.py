"""Throttle and ASGI middleware for throttling HTTP connections."""

import inspect
import re
import typing
import warnings

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import ConfigurationError, _build_exception_handler_getter
from traffik.throttles import Throttle, prepare_throttled_headers
from traffik.types import ExceptionHandler, HTTPConnectionT, Matchable
from traffik.utils import is_async_callable

ThrottlePredicate: TypeAlias = typing.Callable[
    [HTTPConnectionT], typing.Awaitable[bool]
]
"""
A type alias for a callable that takes an HTTP connection and returns a boolean.

This is used as a predicate to determine if the throttle should apply.
"""


class MiddlewareThrottle(typing.Generic[HTTPConnectionT]):
    """
    Middleware throttle.

    This throttle applies to HTTP or WebSocket connections based on the specified path,
    methods, and an optional predicate. These criteria are checked before applying the
    throttle to the connection, from least expensive to most expensive. That is, it first
    checks the HTTP method (skipped for WebSocket), then the path, and finally the
    predicate if provided.

    If the connection does not match the criteria, it is returned unchanged.

    Usage:
    ```python
    from starlette.applications import Starlette

    from traffik.middleware import MiddlewareThrottle
    from traffik.throttles import HTTPThrottle, WebSocketThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.middleware import ThrottleMiddleware

    # Use a custom hook to use throttle for only premium users.
    async def is_premium_user(connection: HTTPConnection) -> bool:
        # Check if the user is a premium user
        return connection.headers.get("X-User-Tier") == "premium"

    http_throttle = MiddlewareThrottle(
        HTTPThrottle(uid="http-limit", rate="10/min"),
        path="/api/",
        methods={"GET", "POST"},
        predicate=is_premium_user,
        context={"scope": "premium_api"},
    )

    ws_throttle = MiddlewareThrottle(
        WebSocketThrottle(uid="ws-limit", rate="30/min"),
        path="/ws/",
    )

    # Use the middleware throttle in your application
    throttle_backend = InMemoryBackend()
    app = Starlette(lifespan=throttle_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[http_throttle, ws_throttle],
        backend=throttle_backend,
    )
    ```
    """

    __slots__ = (
        "throttle",
        "path",
        "methods",
        "predicate",
        "_default_context",
        "cost",
        "__signature__",
    )

    def __init__(
        self,
        throttle: Throttle[HTTPConnectionT],
        path: typing.Optional[Matchable] = None,
        methods: typing.Optional[typing.Iterable[str]] = None,
        hook: typing.Optional[ThrottlePredicate[HTTPConnectionT]] = None,
        predicate: typing.Optional[ThrottlePredicate[HTTPConnectionT]] = None,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> None:
        """
        Initialize the middleware throttle.

        :param throttle: The throttle to apply to the connection.
        :param path: A matchable path (string or regex) to apply the throttle to.
            If string, it's compiled as a regex pattern.

            Examples:

                - "/api/" matches paths starting with "/api/"
                - r"/api/\\d+" matches "/api/" followed by digits
                - None applies to all paths.

        :param methods: A set of HTTP methods (e.g., 'GET', 'POST') to apply the throttle to.
            If None, the throttle applies to all methods. Ignored for `WebSocket` connections.
        :param predicate: An optional callable that takes an HTTP connection and returns a boolean.
            If provided, the throttle will only apply if this hook returns True for the connection.
            This is useful for more complex conditions that cannot be expressed with just path and methods.
            It is run after checking the path and methods, so it should be used for more expensive checks.
        :param hook: Deprecated alias for `predicate`. Use `predicate` instead.
        :param context: An optional mapping of context to pass to the throttle.
            This is merged with the default context provided at initialization,
            with the provided context taking precedence.
        """
        if hook is not None:
            warnings.warn(
                "`hook` parameter is deprecated and will be removed in future versions. "
                "Use `predicate` instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        if predicate is not None and hook is not None:
            raise ConfigurationError(
                "Cannot specify both `predicate` and `hook`. Only use `predicate`."
            )

        self._default_context = dict(context or {})
        self.throttle = throttle
        self.cost = cost

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
        self.predicate = predicate or hook

        # Set a clean `__signature__` so FastAPI's dependency injection only
        # sees `connection` and doesn't treat *args/**kwargs as query params.

        # The middleware throttle' signature should basically mirror the throttle's signature.
        # If the throttle has a custom signature, use it. Else,
        # create a signature that only includes the `connection` parameter frm the `hit` method,
        # and excludes *args and **kwargs.
        if throttle_signature := getattr(throttle, "__signature__", None) is not None:
            self.__signature__ = throttle_signature
        else:
            signature = inspect.signature(self.hit)
            self.__signature__ = signature.replace(
                parameters=[
                    param
                    for param in signature.parameters.values()
                    if param.kind
                    not in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    )
                ]
            )

    async def hit(
        self,
        connection: HTTPConnectionT,
        *,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> HTTPConnectionT:
        """
        Checks if the throttle applies to the connection and applies it if so.

        :param connection: The HTTP connection to check.
        :param cost: An optional cost to pass to the throttle. If not provided, the throttle's default cost is used.
        :param context: An optional mapping of context to pass to the throttle.
            This is merged with the default context provided at initialization,
            with the provided context taking precedence.
        :return: The connection, possibly modified by the throttle. If throttling criteria
            are not met, returns the original connection unchanged. If throttled, may return
            a modified connection or raise a throttling exception.
        :raises: `HTTPException` if the connection exceeds rate limits.
        """
        # Check methods first. Cheapest check is frozenset lookup.
        # WebSocket connections don't have a "method" in scope, so skip if absent.
        if self.methods is not None:
            method = connection.scope.get("method")
            if method is not None and method not in self.methods:
                return connection
        if self.path is not None and not self.path.match(connection.scope["path"]):
            return connection
        if self.predicate is not None and not await self.predicate(connection):
            return connection

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context
        return await self.throttle.hit(
            connection, cost=cost or self.cost, context=merged_context
        )

    async def __call__(
        self, connection: HTTPConnectionT, *args: typing.Any, **kwargs: typing.Any
    ) -> HTTPConnectionT:
        """
        Apply the throttle to the connection if criteria are met.

        This is a wrapper around the `hit` method that allows the throttle to be used
        as a callable in the middleware.

        :param connection: The HTTP connection to check and possibly throttle.
        :return: The connection, possibly modified by the throttle. If throttling criteria
            are not met, returns the original connection unchanged. If throttled, may return
            a modified connection or raise a throttling exception.
        :raises: `HTTPException` if the connection exceeds rate limits.
        """
        return await self.hit(connection, *args, **kwargs)


_SortThrottles = typing.Union[
    typing.Literal["cheap_first", "cheap_last", False, None],
    typing.Callable[[MiddlewareThrottle[HTTPConnectionT]], typing.Any],
]


def _prepare_middleware_throttles(
    middleware_throttles: typing.Sequence[MiddlewareThrottle[HTTPConnectionT]],
    *,
    sort: _SortThrottles = "cheap_first",
) -> typing.Mapping[
    typing.Literal["http", "websocket"],
    typing.List[MiddlewareThrottle[HTTPConnectionT]],
]:
    """
    Prepare middleware throttles by sorting them based on their cost and categorizing by connection type.

    Throttles with lower cost are sorted before those with higher cost.
    Throttles without a specified cost are treated as having infinite cost and are sorted last.

    :param middleware_throttles: A sequence of `MiddlewareThrottle` instances to sort.
    :param sort: Determines the sorting order of throttles based on their cost.
        - "cheap_first": Sorts throttles with lower cost before those with higher cost (default).
        - "cheap_last": Sorts throttles with higher cost before those with lower cost.
        - False or None: No sorting is applied, and throttles are categorized in the order they are provided.
        - A custom callable that takes a `MiddlewareThrottle` and returns a value to sort by.
            Ensure to return `float("inf")` for throttles without a specified cost if you want them to be sorted last.
    :return: A mapping categorizing the throttles by connection type ('http' or 'websocket'), with each category containing a list of throttles sorted by cost.
    """
    sorted_throttles: typing.Sequence[MiddlewareThrottle[HTTPConnectionT]]
    if sort == "cheap_first":
        sorted_throttles = sorted(
            middleware_throttles,
            key=lambda t: t.cost if t.cost is not None else float("inf"),
        )
    elif sort == "cheap_last":
        sorted_throttles = sorted(
            middleware_throttles,
            key=lambda t: -(t.cost if t.cost is not None else float("inf")),
        )
    elif sort in (False, None):
        sorted_throttles = middleware_throttles
    elif callable(sort):
        sorted_throttles = sorted(middleware_throttles, key=sort)
    else:
        raise ValueError(
            f"Invalid value for `sort`: {sort}. Must be 'cheap_first', 'cheap_last', False, None, or a callable."
        )

    categorized: typing.Dict[
        typing.Literal["http", "websocket"],
        typing.List[MiddlewareThrottle[HTTPConnectionT]],
    ] = {
        "http": [],
        "websocket": [],
    }
    for throttle in sorted_throttles:
        connection_type = throttle.throttle.connection_type
        if issubclass(connection_type, WebSocket):
            categorized["websocket"].append(throttle)
        elif issubclass(connection_type, HTTPConnection):
            categorized["http"].append(throttle)
        else:
            raise ConfigurationError(
                f"Unsupported connection type '{connection_type}' for throttle '{throttle}'."
            )
    return categorized


class ThrottleMiddleware:
    """
    Traffik ASGI middleware.

    This middleware processes incoming HTTP connections and applies throttles based on
    the provided `MiddlewareThrottle` instances. It integrates with throttle backends
    to manage throttling state across connections.

    Usage:
    ```python
    from starletter.applications import Starlette

    from traffik.backends.inmemory import InMemoryBackend
    from traffik.middleware import ThrottleMiddleware
    from traffik.throttles import HTTPThrottle

    backend = InMemoryBackend()
    app = Starlette(lifespan=backend.lifespan)

    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[
            MiddlewareThrottle(
                HTTPThrottle(rate="5/min", uid="..."),
                path="/api/",
                methods={"GET", "POST"},
            )
        ],
        backend=backend, # Optional, can be omitted to use the context(lifespan) backend
    )
    ... # Other routes and/or middleware
    ```

    """

    __slots__ = (
        "app",
        "middleware_throttles",
        "backend",
        "get_exception_handler",
        "context",
        "include_headers",
    )

    def __init__(
        self,
        app: ASGIApp,
        middleware_throttles: typing.Sequence[MiddlewareThrottle[typing.Any]],
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnection]] = None,
        exception_handler_getter: typing.Optional[
            typing.Callable[
                [Exception], typing.Optional[ExceptionHandler[HTTPConnection]]
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        sort: _SortThrottles[HTTPConnection] = "cheap_first",
        include_headers: bool = False,
    ) -> None:
        """
        Initialize the middleware with the application and throttles.

        :param app: The ASGI application to wrap.
        :param middleware_throttles: A sequence of `MiddlewareThrottle` instances to apply.
        :param backend: An optional throttle backend to use.
        :param exception_handler_getter: An optional callable that takes an exception and returns an ASGI exception handler.
        :param context: An optional mapping of context to pass to the throttles.
            This is merged with any context provided by individual throttles, with the
            throttle-specific context taking precedence.
        :param sort: Determines the sorting order of throttles based on their cost. This can be used to optimize the order in which throttles are applied.
            If you want ot preseve the order of throttles as provided, set this to False or None.

            - "cheap_first": Sorts throttles with lower cost before those with higher cost (default).
            - "cheap_last": Sorts throttles with higher cost before those with lower cost
            - False or None: No sorting is applied, and throttles are categorized in the order they are provided.
            - A custom callable that takes a `MiddlewareThrottle` and returns a value to sort by.
                Ensure to return `float("inf")` for throttles without a specified cost if you want them to be sorted last.

        :param include_headers: Whether to include throttling headers in the response when a connection is throttled.
        """
        self.app = app
        self.middleware_throttles = _prepare_middleware_throttles(
            middleware_throttles, sort=sort
        )
        self.backend = backend or get_throttle_backend(app)
        self.get_exception_handler = exception_handler_getter
        self.context = context
        self.include_headers = include_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The ASGI application callable that applies throttles to the connection.

        :param scope: The ASGI scope.
        :param receive: The receive function for incoming messages.
        :param send: The send function for outgoing messages.
        """
        typ = scope["type"]
        if typ == "http":
            connection = HTTPConnection(scope)
        elif typ == "websocket":
            connection = WebSocket(scope, receive, send)
        else:
            # Ignore unsupported connection types and pass through to the next middleware or application.
            await self.app(scope, receive, send)
            return

        # Resolve backend once and cache it
        backend = self.backend
        if backend is None:
            backend = get_throttle_backend(connection.app)
            if backend is None:
                raise ConfigurationError("No throttle backend configured.")
            self.backend = backend

        # The backend context must be not closed on context exit.
        # It must also be persistent to ensure throttles can maintain
        # state across multiple connections.
        async with backend(close_on_exit=False, persistent=True):
            context = self.context
            for middleware_throttle in self.middleware_throttles[typ]:
                try:
                    connection = await middleware_throttle.hit(
                        connection,  # type: ignore[arg-type]
                        context=context,
                    )
                except Exception as exc:
                    # This approach allows custom throttles to raise custom exceptions
                    # that will be handled if they register an exception handler with
                    # the application. If not, the exception will propagate and the
                    # `ServerErrorMiddleware` will properly handle it.
                    get_exc_handler = self.get_exception_handler
                    if get_exc_handler is None:
                        get_exc_handler = _build_exception_handler_getter(
                            connection.app
                        )
                        # Cache the exception handler getter for future use
                        self.get_exception_handler = get_exc_handler

                    handler = get_exc_handler(exc)
                    if handler is not None:
                        if is_async_callable(handler):
                            response = await handler(connection, exc)  # type: ignore
                        else:
                            response = await run_in_threadpool(handler, connection, exc)  # type: ignore[arg-type]

                        if response is not None:
                            if self.include_headers:
                                headers = await prepare_throttled_headers(
                                    connection,
                                    throttle=middleware_throttle.throttle,  # type: ignore
                                    context=context,
                                    encode=True,
                                )
                                scope["headers"].extend(headers)

                            await response(scope, receive, send)  # type: ignore[call-arg]
                            return

                    raise exc

        # Ensure that the next middleware or application call is not nested
        # within this middleware's backend context. Else, it would cause
        # the next call to use the same backend context as this middleware,
        # even when it supposed to use the backend context set on lifespan.
        await self.app(scope, receive, send)
