"""Throttle and ASGI middleware for throttling HTTP connections."""

import inspect
import re
import typing
import warnings

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import ConfigurationError, _build_exception_handler_getter
from traffik.throttles import Throttle
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

    This throttle applies to HTTP connections based on the specified path, methods, and an optional hook.
    These criteria are checked before applying the throttle to the connection, from least expensive to most expensive.
    That is, it first checks the HTTP method, then the path, and finally the hook if provided.

    If the connection does not match the criteria, it is returned unchanged.

    Usage:
    ```python
    from starletter.applications import Starlette

    from traffik.middleware import MiddlewareThrottle
    from traffik.throttles import HTTPThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.middleware import ThrottleMiddleware

    # Use a custom hook to use throttle for only premium users.
    async def is_premium_user(connection: HTTPConnection) -> bool:
        # Check if the user is a premium user
        return connection.headers.get("X-User-Tier") == "premium"

    middleware_throttle = MiddlewareThrottle(
        HTTPThrottle(uid="...", rate="10/min"),
        path="/api/",
        methods={"GET", "POST"},
        predicate=is_premium_user,
        context={"scope": "premium_api"}
    )

    # Use the middleware throttle in your application
    throttle_backend = InMemoryBackend()
    app = Starlette(lifespan=throttle_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
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
            If None, the throttle applies to all methods.
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
        # Check methods first. Cheapest check is frozenset lookup
        if self.methods is not None:
            if connection.scope["method"] not in self.methods:
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
        return await self.throttle(
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


class ThrottleMiddleware(typing.Generic[HTTPConnectionT]):
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
    )

    def __init__(
        self,
        app: ASGIApp,
        middleware_throttles: typing.Sequence[MiddlewareThrottle[HTTPConnectionT]],
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]] = None,
        exception_handler_getter: typing.Optional[
            typing.Callable[
                [Exception], typing.Optional[ExceptionHandler[HTTPConnectionT]]
            ]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
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
        """
        self.app = app
        self.middleware_throttles = middleware_throttles
        self.backend = backend or get_throttle_backend(app)
        self.get_exception_handler = exception_handler_getter
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The ASGI application callable that applies throttles to the connection.

        :param scope: The ASGI scope.
        :param receive: The receive function for incoming messages.
        :param send: The send function for outgoing messages.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope, receive)
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
            for throttle in self.middleware_throttles:
                try:
                    connection = await throttle.hit(connection, context=context)  # type: ignore[arg-type]
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
                            await response(scope, receive, send)  # type: ignore[call-arg]
                            return

                    raise exc

        # Ensure that the next middleware or application call is not nested
        # within this middleware's backend context. Else, it would cause
        # the next call to use the same backend context as this middleware,
        # even when it supposed to use the backend context set on lifespan.
        await self.app(scope, receive, send)
