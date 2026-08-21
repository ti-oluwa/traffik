"""Throttle for Starlette `WebSocket` connection type."""

import logging
import math
import typing

from starlette.websockets import WebSocket, WebSocketState

from traffik.backends.base import ThrottleBackend, connection_throttled
from traffik.config import THROTTLE_DEFAULT_SCOPE
from traffik.headers import Header
from traffik.registry import Rule, ThrottleRegistry
from traffik.throttles.base import Throttle, ThrottleExceptionInfo, ThrottleStrategy
from traffik.typing import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    CostType,
    RateType,
    ThrottleErrorHandler,
    WaitPeriod,
)

__all__ = ["WebSocketThrottle", "websocket_throttled"]

logger = logging.getLogger(__name__)


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
    except RuntimeError:
        # Connection was closed (by client) between check and send
        # Silently ignore since client is already disconnected
        logger.exception("An error occurred while sending throttled message\n")


class WebSocketThrottle(Throttle[WebSocket]):
    """WebSocket connection throttle"""

    __slots__ = ()

    connection_type = WebSocket

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
        rules: typing.Optional[typing.Iterable[Rule[WebSocket]]] = None,
        cache_ids: bool = True,
        dynamic_rules: bool = False,
        skip_handler: bool = False,
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
            dynamic_rules=dynamic_rules,
            skip_handler=skip_handler,
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
