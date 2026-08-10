"""Throttle for Starlette `Request` connection type."""

import typing

from starlette.requests import Request
from starlette.responses import Response

from traffik.backends.base import ThrottleBackend
from traffik.config import THROTTLE_DEFAULT_SCOPE
from traffik.headers import Header
from traffik.registry import Rule, ThrottleRegistry
from traffik.throttles.base import Throttle, ThrottleExceptionInfo, ThrottleStrategy
from traffik.typing import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    CostType,
    RateType,
    StrategyStat,
    ThrottleErrorHandler,
)

__all__ = ["HTTPThrottle", "RequestThrottle"]


class HTTPThrottle(Throttle[Request]):
    """HTTP connection throttle"""

    __slots__ = ("use_method",)

    connection_type = Request

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
        rules: typing.Optional[typing.Iterable[Rule[Request]]] = None,
        cache_ids: bool = True,
        dynamic_rules: bool = False,
        skip_handler: bool = False,
        use_method: bool = True,
    ) -> None:
        """
        Initialize the throttle.

        :param uid: Unique identifier for the throttle instance. This ensures that
            multiple instances of the same throttle can coexist without conflicts.
            It also allows for persistent storage of throttle state across application
            restarts or deployments.

        :param rate: Rate limit definition. This can be provided as a `Rate` object
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

            Setting this to True is especially useful for long-lived connections
            like WebSockets where the connection does not change after establishment,
            and caching avoids redundant/expensive identifier computations.

        :param dynamic_rules: Whether to re-fetch registry rules on every `hit(...)` call.
            Defaults to False. When False, registry rules are merged and cached on the first
            `hit(...)` call for efficiency. When True, `add_rules(...)` calls made after the
            first hit are picked up on subsequent calls. The overhead is minimal (a dict
            lookup + length comparison per hit), but only enable this if you need to add
            rules after the throttle has already started processing requests.

        :param skip_handler: If `True`, a throttled connection still updates the
            strategy's state and is still marked throttled (`is_throttled(connection)`
            returns `True`), but `handle_throttled` is never invoked - no exception,
            no response, no side effects from the handler. Retry/wait information is
            still computed and available via `get_wait()`, `get_stat()` or the connection's throttle
            headers. Use this when you want to check `get_wait()`/`is_throttled()` yourself and
            decide what happens next, rather than letting the throttle react for you.
            Defaults to `False`.

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
            dynamic_rules=dynamic_rules,
            skip_handler=skip_handler,
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

    async def set_headers(
        self,
        response: Response,
        connection: Request,
        *,
        headers: typing.Optional[
            typing.Mapping[str, typing.Union[Header[Request], str]]
        ] = None,
        stat: typing.Optional[
            StrategyStat[typing.Mapping[typing.Hashable, typing.Any]]
        ] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> None:
        """
        Resolve and apply throttling headers to an HTTP response.

        This helper resolves the configured headers for the current connection,
        merges any per-call overrides, and updates the response header collection.

        :param response: The HTTP response whose headers should be updated.
        :param connection: The current HTTP connection used to resolve dynamic headers.
        :param headers: Optional additional headers for this specific response.
        :param stat: Optional strategy statistics used in dynamic header resolution.
        :param context: Optional request context passed to header resolvers.
        """
        resolved_headers = await self.get_headers(
            connection=connection,
            headers=headers,
            stat=stat,
            context=context,
        )
        if resolved_headers:
            response.headers.update(resolved_headers)


RequestThrottle = HTTPThrottle  # Alias for semantic clarity
