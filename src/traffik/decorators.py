"""Throttle decorator. For FastAPI only."""

import functools
import inspect
import typing
from dataclasses import dataclass
from typing import Annotated

from fastapi.params import Depends
from starlette.requests import Request as StarletteRequest
from starlette.websockets import WebSocket as StarletteWebSocket

from traffik._utils import _add_parameter_to_signature
from traffik.throttles import Throttle
from traffik.typing import Dependency, HTTPConnectionT, P, Q, R, S

ThrottleT = typing.TypeVar("ThrottleT", bound=Throttle)

__all__ = ["throttled"]


@typing.final
@dataclass(frozen=True)
class _DecoratorDepends(Depends, typing.Generic[P, R, Q, S]):
    """
    `fastapi.params.Depends` subclass that allows instances to be used as decorators.

    Instances use `dependency_decorator` to apply the dependency to the decorated object,
    while still allowing usage as regular FastAPI dependencies.

    `dependency_decorator` is a callable that takes the decorated object and an optional dependency
    and returns the decorated object with/without the dependency applied.

    Think of the `dependency_decorator` as a chef that mixes the sauce (dependency)
    with the dish (decorated object), making a dish with the sauce or without it.
    """

    dependency_decorator: typing.Optional[
        typing.Callable[
            [
                typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
                Dependency[Q, S],
            ],
            typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
        ]
    ] = None
    dependency: typing.Optional[Dependency[Q, S]] = None
    use_cache: bool = True

    def __post_init__(self) -> None:
        if self.dependency_decorator is None:
            raise ValueError(
                f"`dependency_decorator` is required for {self.__class__.__name__} initialization"
            )

    def __call__(
        self, decorated: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]
    ) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
        if self.dependency is None:
            return decorated
        return self.dependency_decorator(decorated, self.dependency)  # type: ignore[misc,union-attr]


# Is this worth it? I mean! Just because of the `@throttled(...)` decorator?
def _apply_throttle(
    route: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
    throttle: Throttle[HTTPConnectionT],
) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
    """
    Create and returns an wrapper that applies throttling to an route
    by wrapping the route such that the route depends on the throttle.

    :param route: The route to wrap.
    :param throttle: The throttle to apply to the route.
    :return: The wrapper that enforces the throttle on the route.
    """
    # This approach is necessary because FastAPI does not support dependencies
    # that are not in the signature of the route function.

    # Use unique (throttle) dependency parameter name to avoid conflicts
    # with other dependencies that may be applied to the route, or in the case
    # of nested use of this wrapper function.
    throttle_dep_param_name = f"_{id(throttle)}_throttle"

    # We need the throttle dependency to be the first parameter of the route
    # So that the rate limit check is done before any other operations or dependencies
    # are resolved/executed, improving the efficiency of implementation.
    if inspect.iscoroutinefunction(route):
        code = f"""
async def route_wrapper(
    {throttle_dep_param_name}: Annotated[typing.Any, Depends(throttle)],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    return await route(*args, **kwargs)
"""
    else:
        code = f"""
def route_wrapper(
    {throttle_dep_param_name}: Annotated[typing.Any, Depends(throttle)],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    return route(*args, **kwargs)
"""

    local_namespace = {"throttle": throttle, "Annotated": Annotated, "Depends": Depends}
    global_namespace = {**globals(), "route": route}
    exec(  # noqa nosec
        code, globals=global_namespace, locals=local_namespace
    )
    wrapper = local_namespace["route_wrapper"]
    wrapper = functools.wraps(route)(wrapper)  # type: ignore[arg-type]
    # The resulting function from applying `functools.wraps(route)` on `wrapper`
    # would not have the throttle dependency in its signature, although it is present in `wrapper`'s definition,
    # because the result of `functools.wraps` assumes the signature of the original function (route in this case).

    # Since the original/wrapped function does not have the throttle dependency in its signature,
    # the throttle dependency will not be recognized/regarded by FastAPI, as FastAPI
    # uses the signature of the function to determine the params, hence the dependencies of the function.

    # So, we update the signature of the wrapper to include the throttle dependency
    wrapper = _add_parameter_to_signature(
        func=wrapper,
        parameter=inspect.Parameter(
            name=throttle_dep_param_name,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[HTTPConnectionT, Depends(throttle)],  # type: ignore[misc]
        ),
        index=0,  # Since the throttle dependency was added as the first parameter
    )
    return wrapper


@typing.overload
def throttled(
    *throttles: Throttle[HTTPConnectionT],
) -> _DecoratorDepends[typing.Any, typing.Any, typing.Any, HTTPConnectionT]: ...  # type: ignore[misc]


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
    _DecoratorDepends[P, R, Q, HTTPConnectionT],
    typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
]:
    """
    Throttles connections to decorated route using the provided throttle(s).

    **Note! This decorator is designed for FastAPI routes as it depends on FastAPI's dependency injection system to enforce the throttle(s).**

    :param throttles: A single throttle or a sequence of throttles to apply to the route.
    :param route: The route to be throttled. If not provided, returns a decorator that can be used to apply throttling to routes.
    :return: A decorator that applies throttling to the route, or the wrapped route if `route` is provided.

    Example:

    ```python
    import fastapi

    from traffik import HTTPThrottle
    from traffik.decorators import throttled  # FastAPI-specific throttled decorator

    sustained_throttle = HTTPThrottle(uid="sustained", rate="100/min")
    burst_throttle = HTTPThrottle(uid="burst", rate="20/sec")

    router = fastapi.APIRouter()

    @router.get("/throttled2")
    @throttled(burst_throttle, sustained_throttle)
    async def route():
        return {"message": "Limited route 2"}

    ```
    """
    if len(throttles) == 0:
        raise ValueError("At least one throttle must be provided.")

    if len(throttles) > 1:
        connection_type = throttles[0].connection_type
        if not all(
            throttle.connection_type is connection_type for throttle in throttles
        ):
            raise ValueError("All throttles must have the same connection type.")

        async def throttle(connection: HTTPConnectionT) -> HTTPConnectionT:
            nonlocal throttles
            for throttle in throttles:
                await throttle(connection)
            return connection

        # Update the the signature of the throttle function to match the connection type of the throttles
        throttle.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters=[
                inspect.Parameter(
                    name="connection",
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=connection_type,
                )
            ],
            return_annotation=connection_type,
        )

        # Make the type checker happy
        _throttle = typing.cast(Throttle[HTTPConnectionT], throttle)
    else:
        _throttle = throttles[0]  # type: ignore[assignment]
        connection_type = _throttle.connection_type

    if not issubclass(connection_type, (StarletteRequest, StarletteWebSocket)):
        raise TypeError(
            "Throttles must be designed for HTTP connections (`Request` or `WebSocket`)."
        )

    # Make the type checker happy
    decorator = typing.cast(
        typing.Callable[
            [
                typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
                Dependency[Q, HTTPConnectionT],
            ],
            typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
        ],
        _apply_throttle,
    )
    dependency = typing.cast(Dependency[Q, HTTPConnectionT], _throttle)
    decorator_dependency = _DecoratorDepends[P, R, Q, HTTPConnectionT](
        dependency_decorator=decorator,
        dependency=dependency,
        use_cache=False,
        scope="function",
    )
    if route is not None:
        decorated = decorator_dependency(route)
        return decorated
    return decorator_dependency
