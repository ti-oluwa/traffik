import hashlib
import typing
from dataclasses import dataclass, field

from annotated_types import Ge
from starlette.requests import Request
from starlette.websockets import WebSocket
from typing_extensions import Annotated

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import ConfigurationError, TraffikException
from traffik.types import (
    UNLIMITED,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)

__all__ = [
    "BaseThrottle",
    "HTTPThrottle",
    "WebSocketThrottle",
]


@dataclass(frozen=True)
class BaseThrottle(typing.Generic[HTTPConnectionT]):
    """Base throttle class"""

    uid: str
    """
    Unique identifier for the throttle instance. This ensures that
    multiple instances of the same throttle can coexist without conflicts.
    It also allows for persistent storage of throttle state across application
    restarts or deployments.
    """
    limit: Annotated[int, Ge(0)] = 0
    """Maximum number of times the route can be accessed within specified time period"""
    milliseconds: Annotated[int, Ge(0)] = 0
    """Time period in milliseconds"""
    seconds: Annotated[int, Ge(0)] = 0
    """Time period in seconds"""
    minutes: Annotated[int, Ge(0)] = 0
    """Time period in minutes"""
    hours: Annotated[int, Ge(0)] = 0
    """Time period in hours"""
    identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None
    """
    Connected client identifier generator.
    If not provided, the throttle backend's identifier will be used.
    This identifier is used to uniquely identify the client connection
    and track its throttling state.
    """
    handle_throttled: typing.Optional[ConnectionThrottledHandler[HTTPConnectionT]] = (
        None
    )
    """
    Handler to call when the client connection is throttled.
    If provided, it will override the default connection throttled handler
    defined for the throttle backend.
    This handler is responsible for notifying the client about the throttling
    and can implement custom logic, such as sending a specific response or logging.
    """
    backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]] = None
    """
    The throttle backend to use for storing throttling data.
    If not provided, the default backend will be used.
    The backend is responsible for managing the throttling state,
    including checking the current throttling status, updating it, and handling
    throttled connections.
    If `dynamic_backend` is True, the backend will be resolved from the request context
    on each call, allowing for dynamic backend resolution based on the request context.
    """
    dynamic_backend: bool = False
    """
    If True, resolves backend from (request) context on each call instead of caching.
    Use ONLY when backend choice must be determined dynamically at runtime.

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
        limit=100, 
        minutes=1, 
        dynamic_backend=True
    )

    async def tenant_middleware(request, call_next):
        tenant = extract_tenant_from_auth_header(request.headers["Authorization"])
        
        if tenant == "premium":
            backend = RedisBackend("redis://premium-redis:6379/0")
        elif tenant == "enterprise":
            backend = RedisBackend("redis://enterprise-redis:6379/0")
        else:
            backend = InMemoryBackend()  # Free tier
        
        async with backend:
            return await call_next(request)
    ```

    Example (Testing with nested contexts):
    ```python
    throttle = HTTPThrottle(uid="test", limit=3, seconds=5, dynamic_backend=True)

    async with backend_a:
        await throttle(request)  # Uses backend_a
        
        async with backend_b:
            await throttle(request)  # Switches to backend_b
            
        await throttle(request)  # Back to backend_a
    ```

    IMPORTANT: Do NOT use for simple shared storage across services.
    For shared backends, use explicit backend configuration instead:

    ```python
    # GOOD - Explicit shared backend
    shared_backend = RedisBackend("redis://shared-redis:6379/0")
    user_quota = HTTPThrottle(uid="user_quota", limit=1000, hours=1, backend=shared_backend)

    # BAD - Unnecessary dynamic resolution
    user_quota = HTTPThrottle(uid="user_quota", limit=1000, hours=1, dynamic_backend=True)
    ```

    WARNING: This feature adds complexity and slight performance overhead.
    - Cannot be used with explicit backend parameter
    - May cause data fragmentation if context switching is inconsistent
    - Harder to debug due to dynamic backend resolution
    - Only use when you absolutely need runtime backend switching

    For most use cases, explicit backend configuration is simpler and more efficient.
    """
    expires_after: Annotated[int, Ge(0)] = field(default=0, init=False)
    """Time period in milliseconds after which the throttle expires"""

    def __post_init__(self) -> None:
        """
        Post-initialization checks for the throttle.
        Ensures that the uid is a non-empty string and that the limit and time period are non-negative.
        """
        if not self.uid or not isinstance(self.uid, str):
            raise ValueError("uid is required and must be a non-empty string")

        if self.limit < 0:
            raise ValueError("Limit must be non-negative")

        if self.dynamic_backend and self.backend is not None:
            raise ValueError(
                "Cannot specify explicit backend with dynamic_backend=True"
            )

        expires_after = (
            self.milliseconds
            + 1000 * self.seconds
            + 60000 * self.minutes
            + 3600000 * self.hours
        )
        object.__setattr__(self, "expires_after", expires_after)
        if self.expires_after < 0:
            raise ValueError("Time period must be non-negative")

        # Only set backend for non-context-aware throttles
        if not self.dynamic_backend:
            resolved_backend = self.backend or get_throttle_backend()
            object.__setattr__(self, "backend", resolved_backend)

            identifier = self.identifier or (
                resolved_backend.identifier if resolved_backend is not None else None
            )
            handle_throttled = self.handle_throttled or (
                resolved_backend.handle_throttled
                if resolved_backend is not None
                else None
            )
            object.__setattr__(self, "identifier", identifier)
            object.__setattr__(self, "handle_throttled", handle_throttled)

    async def __call__(
        self, connection: HTTPConnectionT, *args: typing.Any, **kwargs: typing.Any
    ) -> HTTPConnectionT:
        """
        Throttle the connection based on the limit and time period.

        :param connection: The HTTP connection to throttle.
        :param args: Additional positional arguments to pass to the throttled handler.
        :param kwargs: Additional keyword arguments to pass to the throttled handler.
        :return: The throttled HTTP connection.
        """
        if self.limit == 0 or self.expires_after == 0:
            return connection  # No throttling applied if limit is 0

        if self.backend is None:
            backend = get_throttle_backend(connection)
            if backend is None:
                raise ConfigurationError(
                    "No throttle backend configured. "
                    "Provide a backend to the throttle or set a default backend."
                )
            # Only set the backend if the throttle is not context-aware.
            if not self.dynamic_backend:
                object.__setattr__(self, "backend", backend)
        else:
            backend = self.backend

        identifier = self.identifier or backend.identifier
        if (connection_id := await identifier(connection)) is UNLIMITED:
            return connection

        throttle_key = await self.get_key(connection, *args, **kwargs)
        backend_key = f"{backend.prefix}:{self.uid}:{connection_id}:{throttle_key}"
        if not await backend.check_key_pattern(backend_key):
            raise TraffikException(
                "Invalid throttling key pattern. "
                f"Key must be in the format: {backend.get_key_pattern()}"
            )

        wait_period = await backend.get_wait_period(
            backend_key, self.limit, self.expires_after
        )
        if wait_period != 0:
            handle_throttled = self.handle_throttled or backend.handle_throttled
            await handle_throttled(connection, wait_period, *args, **kwargs)
        return connection

    async def get_key(
        self, connection: HTTPConnectionT, *args: typing.Any, **kwargs: typing.Any
    ) -> str:
        """
        Returns the unique throttling key for the connection.

        :param connection: The HTTP connection to throttle.
        :param args: Additional positional arguments to pass to the throttled handler.
        :param kwargs: Additional keyword arguments to pass to the throttled handler.
        :return: The unique throttling key for the connection.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class HTTPThrottle(BaseThrottle[Request]):
    """HTTP connection throttle"""

    async def get_key(self, connection: Request) -> str:
        """
        Returns the unique throttling key for the HTTP connection.

        :param connection: The HTTP connection to throttle.
        :param args: Additional positional arguments to pass to the throttled handler.
        :param kwargs: Additional keyword arguments to pass to the throttled handler.
        """
        method = connection.scope["method"].upper()
        path = connection.scope["path"]
        connection_key = f"{method}:{path}"
        # Hash for some layer of security
        hashed_connection_key = hashlib.md5(connection_key.encode()).hexdigest()  # nosec
        throttle_key = f"http:{hashed_connection_key}"
        return throttle_key

    async def __call__(self, connection: Request) -> Request:
        """
        Calls the throttle for an HTTP connection.

        :param connection: The HTTP connection to throttle.
        :return: The throttled HTTP connection.
        """
        return await super().__call__(connection)


@dataclass(frozen=True)
class WebSocketThrottle(BaseThrottle[WebSocket]):
    """WebSocket connection throttle"""

    async def get_key(
        self, connection: WebSocket, context_key: typing.Optional[str] = None
    ) -> str:
        """
        Returns the unique throttling key for the WebSocket connection.

        :param connection: The WebSocket connection to throttle.
        :param context_key: Optional context key to differentiate throttling
            for different contexts within the same WebSocket connection.
        :return: The unique throttling key for the WebSocket connection.
        """
        path = connection.scope["path"]
        context = context_key or "default"
        connection_key = f"{path}:{context}"
        # Hash for some layer of security
        hashed_connection_key = hashlib.md5(connection_key.encode()).hexdigest()  # nosec
        throttle_key = f"ws:{hashed_connection_key}"
        return throttle_key

    async def __call__(
        self, connection: WebSocket, context_key: typing.Optional[str] = None
    ) -> WebSocket:
        """
        Calls the throttle for a WebSocket connection.

        :param connection: The WebSocket connection to throttle.
        :param context_key: Optional context key to differentiate throttling
            for different contexts within the same WebSocket connection.
        :return: The throttled WebSocket connection.
        """
        return await super().__call__(connection, context_key=context_key)
