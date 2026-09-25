"""Generic, framework-agnostic throttle decorator, for any Starlette-based app."""

import asyncio
import functools
import inspect
import typing

from starlette.requests import HTTPConnection
from typing_extensions import Unpack

from traffik.throttles.base import Throttle, ThrottleKwargs
from traffik.throttles.specs import resolve_specs
from traffik.typing import HTTPConnectionT, P, R, ThrottleType

__all__ = ["throttled"]


@typing.overload
def throttled(
    *throttles: typing.Union[Throttle[HTTPConnectionT], str],
    uid: typing.Optional[str] = None,
    type: typing.Optional[ThrottleType] = None,
    **kwargs: Unpack[ThrottleKwargs],
) -> typing.Callable[
    [typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]],
    typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
]: ...
@typing.overload
def throttled(
    *throttles: typing.Union[Throttle[HTTPConnectionT], str],
    route: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    uid: typing.Optional[str] = None,
    type: typing.Optional[ThrottleType] = None,
    **kwargs: Unpack[ThrottleKwargs],
) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]: ...


def throttled(
    *throttles: typing.Union[Throttle[HTTPConnectionT], str],
    route: typing.Optional[
        typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]
    ] = None,
    uid: typing.Optional[str] = None,
    type: typing.Optional[ThrottleType] = None,
    **kwargs: Unpack[ThrottleKwargs],
) -> typing.Union[
    typing.Callable[
        [typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]],
        typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    ],
    typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
]:
    """
    Throttles connections to decorated route using the provided throttle(s).

    **Note! The decorated route must have an `HTTPConnection` (e.g., `Request`, `WebSocket`)
    parameter for the throttle(s) to work.**

    For FastAPI routes, use `traffik.decorators.fastapi.throttled` to bypass this constraint.
    Note that for `WebSocket` endpoints, it only guards the initial connection (befor `accept`)
    not every meesage.

    :param throttles: One or more pre-built `Throttle` instances, or shorthand strings
        (e.g. `"100/min"`, `"100/min|token_bucket|ws"`, `"my_uid"` to look up an
        existing throttle by uid). Can be mixed in the same call.
    :param route: The route to be throttled. If not provided, returns a decorator that
        can be used to apply throttling to routes.
    :param uid: UID to use for any string spec that doesn't already embed one. Only
        valid when at most one such spec is present in this call. Else, each needs its own.
    :param type: `"http"` or `"ws"`, for any string spec that doesn't already embed a
        type. Must match, not conflict with, a type any spec does embed.
    :param kwargs: Extra `Throttle` constructor keyword arguments, applied to every
        string spec that constructs a throttle (not to pre-built `Throttle` instances,
        and not to uid-only lookups).
    :return: A decorator that applies throttling to the route, or the wrapped route if
        `route` is provided.

    Example:

    ```python
    from starlette import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from traffik import throttled, HTTPThrottle

    sustained_throttle = HTTPThrottle(uid="sustained", rate="100/min")

    app = Starlette()


    @app.route("/throttled")
    @throttled(sustained_throttle, "20/sec|token_bucket")
    async def route(request: Request):
        return JSONResponse({"message": "Limited route 1"})
    ```
    """
    resolved_throttles = resolve_specs(throttles, uid=uid, type=type, **kwargs)
    count = len(resolved_throttles)
    if count == 0:
        raise ValueError("At least one throttle must be provided.")

    if count > 1:
        connection_type = resolved_throttles[0].connection_type
        if not all(t.connection_type is connection_type for t in resolved_throttles):
            raise ValueError("All throttles must have the same connection type.")

        async def throttle(connection: HTTPConnectionT) -> HTTPConnectionT:
            nonlocal resolved_throttles
            for t in resolved_throttles:
                await t(connection)
            return connection

    else:
        throttle = resolved_throttles[0]  # type: ignore[assignment]
        connection_type = throttle.connection_type

    if not issubclass(connection_type, HTTPConnection):
        raise TypeError("Throttles must be designed for HTTP connections.")

    def decorator(
        route: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    ) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
        if inspect.iscoroutinefunction(route):
            route = typing.cast(typing.Callable[P, typing.Awaitable[R]], route)

            @functools.wraps(route)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                connection = None
                for arg in args:
                    if isinstance(arg, connection_type):
                        connection = arg
                        break
                if connection is None:
                    for kwarg in kwargs.values():
                        if isinstance(kwarg, connection_type):
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
                if isinstance(arg, connection_type):
                    connection = arg
                    break
            if connection is None:
                for kwarg in kwargs.values():
                    if isinstance(kwarg, connection_type):
                        connection = kwarg
                        break

            if connection is None:
                raise ValueError(
                    "No HTTP connection found in route parameters for throttling."
                )

            loop = asyncio.get_running_loop()
            loop.run_until_complete(throttle(connection))  # type: ignore
            return route(*args, **kwargs)

        return wrapper

    if route is not None:
        return decorator(route)
    return decorator
