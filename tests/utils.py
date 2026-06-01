import typing

import pytest
from _pytest.mark.structures import Markable
from starlette.requests import HTTPConnection, empty_receive, empty_send
from starlette.types import Receive, Send
from starlette.websockets import WebSocket
from typing_extensions import TypeVar

from traffik.throttles import HTTPThrottle, Throttle, WebSocketThrottle
from traffik.types import EXEMPTED


async def default_client_identifier(connection: HTTPConnection) -> str:
    return "testclient"


async def unlimited_identifier(connection: HTTPConnection) -> object:
    return EXEMPTED


ThrottleT = TypeVar("ThrottleT", bound=Throttle)

THROTTLE_TYPES = [
    pytest.param(HTTPThrottle, id="http_throttle"),
    pytest.param(WebSocketThrottle, id="ws_throttle"),
]


def requires_throttle_type(
    target: typing.Optional[Markable] = None,
    /,
    parameter_name: str = "throttle_type",
    **kwargs: typing.Any,
) -> typing.Union[Markable, typing.Callable[[Markable], Markable]]:
    def decorator(target: Markable) -> Markable:
        return pytest.mark.parametrize(
            parameter_name,
            THROTTLE_TYPES,
            **kwargs,
        )(target)  # type: ignore[return-value]

    if target is None:
        return decorator
    return decorator(target)


HTTPConnectionT = TypeVar("HTTPConnectionT", bound=HTTPConnection)


def make_connection(
    typ: typing.Type[HTTPConnectionT],
    /,
    send: Send = empty_send,
    receive: Receive = empty_receive,
    method: str = "GET",
    path: str = "/test",
    headers: typing.Optional[typing.Sequence[typing.Tuple[bytes, bytes]]] = None,
    client: typing.Optional[typing.Tuple[str, int]] = ("127.0.0.1", 50000),
    **kwargs: typing.Any,
) -> HTTPConnectionT:
    """Create a mocked `HTTPConnection` for testing."""
    kwargs.pop("type", None)
    scope = {
        "type": "http" if not issubclass(typ, WebSocket) else "websocket",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": client,
        **kwargs,
    }
    if typ is HTTPConnection:
        return typ(scope=scope, receive=receive)
    return typ(scope=scope, receive=receive, send=send)  # type: ignore
