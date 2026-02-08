"""Throttle decorator. For FastAPI only."""

import asyncio
import functools
import inspect
import typing
from typing import Annotated

from fastapi.params import Depends

from traffik.throttles import Throttle
from traffik.types import Dependency, HTTPConnectionT, P, Q, R, S
from traffik.utils import _add_parameter_to_signature

ThrottleT = typing.TypeVar("ThrottleT", bound=Throttle)

__all__ = ["throttled"]


class _DecoratorDepends(typing.Generic[P, R, Q, S], Depends):
    """
    `fastapi.params.Depends` subclass that allows instances to be used as decorators.

    Instances use `dependency_decorator` to apply the dependency to the decorated object,
    while still allowing usage as regular FastAPI dependencies.

    `dependency_decorator` is a callable that takes the decorated object and an optional dependency
    and returns the decorated object with/without the dependency applied.

    Think of the `dependency_decorator` as a chef that mixes the sauce (dependency)
    with the dish (decorated object), making a dish with the sauce or without it.
    """

    def __init__(
        self,
        dependency_decorator: typing.Callable[
            [
                typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
                Dependency[Q, S],
            ],
            typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
        ],
        dependency: typing.Optional[Dependency[Q, S]] = None,
        *,
        use_cache: bool = True,
    ) -> None:
        self.dependency_decorator = dependency_decorator
        super().__init__(dependency, use_cache=use_cache)

    def __call__(
        self, decorated: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]
    ) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
        if self.dependency is None:
            return decorated
        return self.dependency_decorator(decorated, self.dependency)


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
    if asyncio.iscoroutinefunction(route):
        wrapper_code = f"""
async def route_wrapper(
    {throttle_dep_param_name}: Annotated[typing.Any, Depends(throttle)],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    return await route(*args, **kwargs)
"""
    else:
        wrapper_code = f"""
def route_wrapper(
    {throttle_dep_param_name}: Annotated[typing.Any, Depends(throttle)],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    return route(*args, **kwargs)
"""

    local_namespace = {
        "throttle": throttle,
        "Annotated": Annotated,
        "Depends": Depends,
    }
    global_namespace = {
        **globals(),
        "route": route,
    }
    exec(  # nosec
        wrapper_code,
        global_namespace,
        local_namespace,
    )
    route_wrapper = local_namespace["route_wrapper"]
    route_wrapper = functools.wraps(route)(route_wrapper)  # type: ignore[arg-type]
    # The resulting function from applying `functools.wraps(route)` on `route_wrapper`
    # would not have the throttle dependency in its signature, although it is present in `route_wrapper`'s definition,
    # because the result of `functools.wraps` assumes the signature of the original function (route in this case).

    # Since the original/wrapped function does not have the throttle dependency in its signature,
    # the throttle dependency will not be recognized/regarded by FastAPI, as FastAPI
    # uses the signature of the function to determine the params, hence the dependencies of the function.

    # So, we update the signature of the wrapper to include the throttle dependency
    route_wrapper = _add_parameter_to_signature(
        func=route_wrapper,
        parameter=inspect.Parameter(
            name=throttle_dep_param_name,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[HTTPConnectionT, Depends(throttle)],  # type: ignore[misc]
        ),
        index=0,  # Since the throttle dependency was added as the first parameter
    )
    return route_wrapper


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

        async def throttle(connection: HTTPConnectionT) -> HTTPConnectionT:
            nonlocal throttles
            for t in throttles:
                await t(connection)
            return connection
    else:
        throttle = throttles[0]  # type: ignore[assignment]

    # Just to make the type checker happy
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
    dependency = typing.cast(Dependency[Q, HTTPConnectionT], throttle)
    decorator_dependency = _DecoratorDepends[P, R, Q, HTTPConnectionT](
        dependency_decorator=decorator,
        dependency=dependency,
    )
    if route is not None:
        decorated = decorator_dependency(route)
        return decorated
    return decorator_dependency
