import typing

import pytest
from _pytest.mark.structures import Markable
from starlette.requests import HTTPConnection, empty_receive, empty_send
from starlette.types import ASGIApp, Receive, Send
from starlette.websockets import WebSocket
from typing_extensions import TypeVar

from tests.client import AsyncTestClient
from traffik.throttles import HTTPThrottle, Throttle, WebSocketThrottle
from traffik.typing import EXEMPTED


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
    """
    Parametrize a test with all supported throttle types.

    Decorator that adds pytest.mark.parametrize to a test function with all
    available throttle types (HTTPThrottle and WebSocketThrottle). Can be used
    as a decorator directly or as a decorator factory.

    :param target: Optional test function or method to decorate. If None, returns
        a decorator factory.
    :param parameter_name: Name of the parameter to pass throttle types to.
        Defaults to "throttle_type".
    :param kwargs: Additional keyword arguments passed to `pytest.mark.parametrize`.
    :return: If target is provided, returns the decorated target. Otherwise, returns
        a decorator function.
    """

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
    app: typing.Optional[ASGIApp] = None,
    send: Send = empty_send,
    receive: Receive = empty_receive,
    method: str = "GET",
    path: str = "/test",
    headers: typing.Optional[typing.Sequence[typing.Tuple[bytes, bytes]]] = None,
    client: typing.Optional[typing.Tuple[str, int]] = ("127.0.0.1", 50000),
    **kwargs: typing.Any,
) -> HTTPConnectionT:
    """
    Create a mocked HTTP or WebSocket connection for testing.

    Factory function that creates properly configured HTTPConnection or
    WebSocket instances with customizable ASGI scope parameters for unit testing.

    :param typ: The connection type class to instantiate (HTTPConnection or WebSocket).
    :param send: ASGI send callable. Defaults to empty_send.
    :param receive: ASGI receive callable. Defaults to empty_receive.
    :param method: HTTP method for the request. Defaults to "GET".
    :param path: Request path. Defaults to "/test".
    :param headers: Optional sequence of (header_name, header_value) tuples as bytes.
        Defaults to None (empty headers).
    :param client: Optional tuple of (host, port) for the client address.
        Defaults to ("127.0.0.1", 50000).
    :param kwargs: Additional ASGI scope parameters to include in the scope dict.
    :return: An instance of the specified connection type with the configured scope.
    """
    kwargs.pop("type", None)
    scope = {
        "type": "http" if not issubclass(typ, WebSocket) else "websocket",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": client,
        "app": app,
        **kwargs,
    }
    if typ is HTTPConnection:
        return typ(scope=scope, receive=receive)
    return typ(scope=scope, receive=receive, send=send)  # type: ignore


def make_client(app: ASGIApp, base_url: str, **kwargs) -> AsyncTestClient:
    return AsyncTestClient(app, base_url=base_url, **kwargs)
