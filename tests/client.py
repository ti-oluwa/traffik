"""
Fully async test client for ASGI applications.

HTTP is handled by an internal `httpx2.AsyncClient` over `ASGITransport`with
async `get`/`post`/`put`/`patch`/`delete`/`head`/`options`, all going through
`request()` underneath, same as `httpx` itself.

WebSocket connections use a hand-rolled queue-driven session (`AsyncWebSocketTestSession`)
since `httpx` doesn't implement the WebSocket half of ASGI. Both HTTP and WebSocket calls run
against the same app instance and share the same ASGI *lifespan* (startup on
`__aenter__`, shutdown on `__aexit__`).
"""

import asyncio
import json
import logging
import typing
from urllib.parse import unquote, urljoin, urlsplit

from httpx2 import ASGITransport, AsyncClient, Response
from starlette.testclient import ASGI3App
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect
from typing_extensions import Self

logger = logging.getLogger(__name__)


def _ensure_bytes(
    val: typing.Union[str, bytes], /, *, encoding: str = "utf-8"
) -> bytes:
    return val if isinstance(val, bytes) else str(val).encode(encoding=encoding)


class AsyncWebSocketTestSession:
    """
    Asynchronous test session for WebSocket connections to ASGI applications.

    This class provides methods to send and receive messages over a `WebSocket`
    connection in an asynchronous context.
    """

    def __init__(
        self,
        app: ASGI3App,
        scope: Scope,
        loop: asyncio.AbstractEventLoop,
        receive_queue: asyncio.Queue[Message],
        send_queue: asyncio.Queue[typing.Union[Message, BaseException]],
    ) -> None:
        """
        Initialize the WebSocket test session.

        :param app: The ASGI application to connect to.
        :param scope: The ASGI scope for the WebSocket connection.
        :param loop: The asyncio event loop to use.
        :param receive_queue: The queue to receive messages from the ASGI app.
        :param send_queue: The queue to send messages to the ASGI app.
        """
        self.loop = loop
        self.app = app
        self.scope = scope
        self.accepted_subprotocol = None
        self._receive_queue = receive_queue
        self._send_queue = send_queue
        self._task: typing.Optional[asyncio.Task[None]] = None

    async def __aenter__(self) -> Self:
        self._task = self.loop.create_task(self._run())
        await asyncio.sleep(0)  # Allow task to start
        await self.send({"type": "websocket.connect"})

        message = await self.receive()
        if message["type"] not in ("websocket.accept", "websocket.close"):
            raise RuntimeError(
                f"Expected 'websocket.accept' or 'websocket.close', got {message['type']!r}"
            )

        self._raise_on_close(message)
        self.accepted_subprotocol = message.get("subprotocol", None)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            await self.close(1000)
        elif issubclass(exc_type, WebSocketDisconnect):
            # Acknowledge close frame by sending disconnect
            await self.close(exc_val.code)
        else:
            await self.close(1011)

        # Drain remaining messages
        while not self._send_queue.empty():
            message = await self._send_queue.get()
            if isinstance(message, BaseException):
                raise message

        # Wait for task to complete
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

    async def _run(self) -> None:
        """
        Run the ASGI application in a separate task.
        """
        scope = self.scope
        receive = self._asgi_receive
        send = self._asgi_send
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            await self._send_queue.put(exc)
            raise

    async def _asgi_receive(self) -> Message:
        """Receive a message from the ASGI app receive queue."""
        while self._receive_queue.empty():
            await asyncio.sleep(0)
        return await self._receive_queue.get()

    async def _asgi_send(self, message: Message) -> None:
        """
        Send a message to the ASGI app send queue.

        :param message: The message to send.
        """
        await self._send_queue.put(message)

    def _raise_on_close(self, message: Message) -> None:
        """
        Raise `WebSocketDisconnect` if the message indicates a close.

        :param message: The message to check.
        """
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000))

    async def send(self, message: Message) -> None:
        """
        Send a message to the ASGI application.

        :param message: The message to send.
        """
        await self._receive_queue.put(message)

    async def receive(self) -> Message:
        """Receive a message from the ASGI application."""
        while True:
            try:
                message = self._send_queue.get_nowait()
                if isinstance(message, BaseException):
                    raise message
                self._raise_on_close(message)

                return message
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def close(self, code: int = 1000) -> None:
        """
        Close the WebSocket connection.

        :param code: The close code to send.
        """
        await self.send({"type": "websocket.disconnect", "code": code})

    async def send_text(self, data: str) -> None:
        """
        Send a text message to the ASGI application.

        :param data: The text data to send.
        """
        await self.send({"type": "websocket.receive", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        """
        Send a binary message to the ASGI application.

        :param data: The binary data to send.
        """
        await self.send({"type": "websocket.receive", "bytes": data})

    async def send_json(self, data: typing.Any, mode: str = "text") -> None:
        """
        Send a JSON message to the ASGI application.

        :param data: The data to send as JSON.
        :param mode: The mode to send the JSON data, either "text" or "binary".
        """
        assert mode in ["text", "binary"]
        text = json.dumps(data)
        if mode == "text":
            return await self.send({"type": "websocket.receive", "text": text})

        return await self.send({
            "type": "websocket.receive",
            "bytes": text.encode("utf-8"),
        })

    async def receive_text(self) -> str:
        """
        Receive a text message from the ASGI application.

        :return: The text data received.
        """
        message = await self.receive()
        return message["text"]

    async def receive_bytes(self) -> bytes:
        """
        Receive a binary message from the ASGI application.

        :return: The binary data received.
        """
        message = await self.receive()
        return message["bytes"]

    async def receive_json(self, mode: str = "text") -> typing.Any:
        """
        Receive a JSON message from the ASGI application.

        :param mode: The mode to receive the JSON data, either "text" or "binary".
        :return: The data received as JSON.
        """
        assert mode in ["text", "binary"]
        message = await self.receive()

        if mode == "text":
            text = message["text"]
        else:
            text = message["bytes"].decode("utf-8")
        return json.loads(text)


class LifespanError(RuntimeError):
    """Exception raised when lifespan event fails."""


class LifespanStartupError(LifespanError):
    """Exception raised when lifespan startup fails."""

    pass


class LifespanShutdownError(LifespanError):
    """Exception raised when lifespan startup fails."""

    pass


class AsyncTestClient:
    """
    Asynchronous test client for ASGI applications.

    HTTP requests (`get`/`post`/`put`/`patch`/`delete`/`head`/`options`, are
    routed through `request()`), backed by `httpx2.AsyncClient`
    over `ASGITransport`.

    WebSocket connections use `websocket_connect()` backed by `AsyncWebSocketTestSession`.
    Both share the same app and the same lifespan.
    """

    __test__ = False  # For pytest to not discover this up.

    def __init__(
        self,
        app: ASGI3App,
        base_url: str = "http://testclient",
        raise_server_exceptions: bool = True,
        root_path: str = "",
        loop: typing.Optional[asyncio.AbstractEventLoop] = None,
        headers: typing.Optional[typing.Mapping[str, str]] = None,
        **http_kwargs: typing.Any,
    ) -> None:
        """
        Initialize the asynchronous test client.

        :param app: The ASGI application to test.
        :param base_url: The base URL for the client.
        :param raise_server_exceptions: Whether to raise server exceptions.
        :param root_path: The root path for the application.
        :param loop: The asyncio event loop to use.
        :param headers: Extra default headers to send with every HTTP request.
        """
        self.app = app
        self.base_url = base_url
        self.root_path = root_path
        self.raise_server_exceptions = raise_server_exceptions
        self.loop = loop or asyncio.get_running_loop()

        self._http_client = AsyncClient(
            transport=ASGITransport(
                app=app,
                raise_app_exceptions=raise_server_exceptions,
                root_path=root_path,
            ),
            base_url=base_url,
            headers={"user-agent": "testclient", **(headers or {})},
            **http_kwargs,
        )

        # Separate queues for lifespan
        self.lifespan_receive_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.lifespan_send_queue: asyncio.Queue[Message] = asyncio.Queue()
        self._lifespan_task: typing.Optional[asyncio.Task] = None

    async def request(self, method: str, url: str, **kwargs: typing.Any) -> Response:
        """
        Send an HTTP request to the ASGI application.

        :param method: The HTTP method (e.g. "GET", "POST").
        :param url: The URL to request. May be relative to `base_url`.
        :param kwargs: Any other keyword arguments `httpx2.AsyncClient.request`
            accepts (`params`, `json`, `data`, `content`, `files`, `headers`,
            `cookies`, `auth`, `follow_redirects`, `timeout`, ...).
        :return: The `httpx2.Response`.
        """
        return await self._http_client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs: typing.Any) -> Response:
        return await self.request("OPTIONS", url, **kwargs)

    def websocket_connect(
        self,
        url: str,
        subprotocols: typing.Optional[typing.Sequence[str]] = None,
        headers: typing.Optional[typing.MutableMapping[str, str]] = None,
        **kwargs: typing.Any,
    ) -> AsyncWebSocketTestSession:
        """
        Establish a WebSocket connection to the ASGI application.

        :param url: The WebSocket URL to connect to (`ws://`/`wss://`, or a
            path/`http(s)://` URL, the scheme is normalized to `ws`/`wss`).
        :param subprotocols: Optional list of subprotocols to use.
        :param headers: Optional extra headers for the upgrade request.
        :param kwargs: Extra ASGI scope fields (merged in as-is).
        :return: An `AsyncWebSocketTestSession` for the connection, used as an
            async context manager: `async with client.websocket_connect(url) as ws:`.
        """
        host = self.base_url.split("://")[1]
        url = urljoin(f"ws://{host}", url)

        scheme, netloc, path, query, _fragment = (str(item) for item in urlsplit(url))
        if scheme not in {"ws", "wss"}:
            raise ValueError(
                f"`websocket_connect(...)` requires a ws/wss URL, got scheme {scheme!r}"
            )

        default_port = {"ws": 80, "wss": 443}[scheme]
        if ":" in netloc:
            connection_host, port_string = netloc.split(":", 1)
            port = int(port_string)
        else:
            connection_host = netloc
            port = default_port

        request_headers = dict(headers or {})
        header_pairs: typing.List[typing.Tuple[bytes, bytes]] = []
        if "host" not in {k.lower() for k in request_headers}:
            if port == default_port:
                header_pairs.append((b"host", connection_host.encode()))
            else:
                header_pairs.append((b"host", f"{connection_host}:{port}".encode()))

        header_pairs.append((b"connection", b"upgrade"))
        header_pairs.append((b"sec-websocket-key", b"testserver=="))
        header_pairs.append((b"sec-websocket-version", b"13"))
        if subprotocols:
            header_pairs.append((
                b"sec-websocket-protocol",
                ", ".join(subprotocols).encode(),
            ))
        header_pairs += [
            (
                key.lower().encode(encoding="utf-8"),
                _ensure_bytes(value, encoding="utf-8"),
            )
            for key, value in request_headers.items()
            if key.lower() != "host"
        ]

        scope: Scope = {
            "type": "websocket",
            "path": unquote(path),
            "root_path": self.root_path,
            "scheme": scheme,
            "query_string": query.encode(),
            "headers": header_pairs,
            "client": ["testclient", 50000],
            "server": [connection_host, port],
            "subprotocols": list(subprotocols) if subprotocols else [],
            **kwargs,
        }

        receive_queue = asyncio.Queue()
        send_queue = asyncio.Queue()
        return AsyncWebSocketTestSession(
            app=self.app,
            scope=scope,
            loop=self.loop,
            receive_queue=receive_queue,
            send_queue=send_queue,
        )

    async def _lifespan_receive(self) -> Message:
        """Receive a message from the lifespan receive queue."""
        while True:
            try:
                return self.lifespan_receive_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def _lifespan_send(self, message: Message) -> None:
        """
        Send a message to the lifespan send queue.

        :param message: The message to send.
        """
        await self.lifespan_send_queue.put(message)

    async def _receive_from_lifespan(self) -> Message:
        """
        Receive a message from the lifespan send queue.

        :return: The received message.
        """
        while True:
            try:
                return self.lifespan_send_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def _send_to_lifespan(self, message: Message) -> None:
        """
        Send a message to the lifespan receive queue.

        :param message: The message to send.
        """
        await self.lifespan_receive_queue.put(message)

    async def __aenter__(self) -> Self:
        await self._http_client.__aenter__()

        # Create lifespan task
        self._lifespan_task = self.loop.create_task(
            self.app(  # type: ignore[arg-type]
                {"type": "lifespan"}, self._lifespan_receive, self._lifespan_send
            )
        )

        # Send startup event
        await self._send_to_lifespan({"type": "lifespan.startup"})
        # Confirm startup complete
        message = await self._receive_from_lifespan()

        # If startup failed, raise the error. The lifespan task also raised
        # internally (that's how the ASGI app reports the failure). Retrieve
        # it so asyncio doesn't complain about an exception nobody looked at.
        cause: typing.Optional[BaseException] = None
        if message["type"] == "lifespan.startup.failed":
            if self._lifespan_task is not None:
                try:
                    await asyncio.wait_for(self._lifespan_task, timeout=1.0)
                except asyncio.TimeoutError:
                    self._lifespan_task.cancel()
                    try:
                        await self._lifespan_task
                    except asyncio.CancelledError:
                        pass
                except BaseException as exc:
                    cause = exc
                    logger.exception(
                        "An error occurred when waiting on lifespan task after startup failure"
                    )
            raise LifespanStartupError(
                message.get("message", "Lifespan startup failed")
            ) from cause

        # Ensure startup complete
        if message["type"] != "lifespan.startup.complete":
            raise LifespanStartupError(
                f"Expected startup complete, got {message['type']}"
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> typing.Optional[bool]:
        try:
            # Only send shutdown if startup was successful
            if exc_type is None or not issubclass(exc_type, LifespanError):
                await self._send_to_lifespan({"type": "lifespan.shutdown"})
                message = await self._receive_from_lifespan()
                if message["type"] not in (
                    "lifespan.shutdown.complete",
                    "lifespan.shutdown.failed",
                ):
                    raise LifespanShutdownError(
                        f"Expected shutdown complete or shutdown failed, got {message['type']}"
                    )

            # Wait for lifespan task to complete
            if self._lifespan_task and not self._lifespan_task.done():
                try:
                    await asyncio.wait_for(self._lifespan_task, timeout=1.0)
                except asyncio.TimeoutError:
                    self._lifespan_task.cancel()
                    try:
                        await self._lifespan_task
                    except asyncio.CancelledError:
                        pass
                except BaseException as exc:
                    raise LifespanShutdownError(
                        "An error occurred when waiting on lifespan task after shutdown failure"
                    ) from exc
        finally:
            await self._http_client.__aexit__(exc_type, exc_val, exc_tb)

        if exc_type is not None:
            return False  # Propagate exception
        return None
