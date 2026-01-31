import asyncio
import json
import typing
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.sessions import Session
from starlette.testclient import ASGI3App, _Upgrade
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect


class AsyncWebSocketTestSession:
    """
    Asynchronous test session for WebSocket connections to ASGI applications.

    This class provides methods to send and receive messages over a WebSocket
    connection in an asynchronous context.
    """

    def __init__(
        self,
        app: ASGI3App,
        scope: Scope,
        event_loop: asyncio.AbstractEventLoop,
        receive_queue: asyncio.Queue,
        send_queue: asyncio.Queue,
    ) -> None:
        """
        Initialize the WebSocket test session.

        :param app: The ASGI application to connect to.
        :param scope: The ASGI scope for the WebSocket connection.
        :param event_loop: The asyncio event loop to use.
        :param receive_queue: The queue to receive messages from the ASGI app.
        :param send_queue: The queue to send messages to the ASGI app.
        """
        self.event_loop = event_loop
        self.app = app
        self.scope = scope
        self.accepted_subprotocol = None
        self._receive_queue = receive_queue
        self._send_queue = send_queue
        self._task = None

    async def __aenter__(self) -> "AsyncWebSocketTestSession":
        self._task = self.event_loop.create_task(self._run())
        await asyncio.sleep(0)  # Allow task to start
        await self.send({"type": "websocket.connect"})

        message = await self.receive()
        if message["type"] not in ("websocket.accept", "websocket.close"):
            raise RuntimeError(
                f"Expected 'websocket.accept' or 'websocket.close', got {message['type']}"
            )

        self._raise_on_close(message)
        self.accepted_subprotocol = message.get("subprotocol", None)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            await self.close(1000)
        elif issubclass(exc_type, WebSocketDisconnect):
            await self.close(exc_val.code)
        else:
            await self.close(1011)

        # Drain remaining messages
        while not self._send_queue.empty():
            message = await self._send_queue.get()
            if isinstance(message, BaseException):
                raise message

        # Wait for task to complete
        if self._task and not self._task.done():
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

        return await self.send(
            {"type": "websocket.receive", "bytes": text.encode("utf-8")}
        )

    async def receive_text(self) -> str:
        """
        Receive a text message from the ASGI application.

        :return: The text data received.
        """
        message = await self.receive()
        self._raise_on_close(message)
        return message["text"]

    async def receive_bytes(self) -> bytes:
        """
        Receive a binary message from the ASGI application.

        :return: The binary data received.
        """
        message = await self.receive()
        self._raise_on_close(message)
        return message["bytes"]

    async def receive_json(self, mode: str = "text") -> typing.Any:
        """
        Receive a JSON message from the ASGI application.

        :param mode: The mode to receive the JSON data, either "text" or "binary".
        :return: The data received as JSON.
        """
        assert mode in ["text", "binary"]
        message = await self.receive()
        self._raise_on_close(message)

        if mode == "text":
            text = message["text"]
        else:
            text = message["bytes"].decode("utf-8")

        return json.loads(text)


class _ASGIAdapter(HTTPAdapter):
    """
    Adapter for `requests` to talk to ASGI applications over asyncio.

    Only supports WebSocket connections.
    """

    def __init__(
        self,
        app: ASGI3App,
        event_loop: asyncio.AbstractEventLoop,
        root_path: str = "",
        raise_server_exceptions: bool = True,
    ) -> None:
        """
        Initialize the ASGI adapter.

        :param app: The ASGI application to connect to.
        :param event_loop: The asyncio event loop to use.
        :param root_path: The root path for the application.
        :param raise_server_exceptions: Whether to raise server exceptions.
        """
        self.event_loop = event_loop
        self.app = app
        self.raise_server_exceptions = raise_server_exceptions
        self.root_path = root_path

    def send(  # type: ignore[override]
        self, request: requests.PreparedRequest, *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncWebSocketTestSession:
        """
        Send a request to upgrade to a WebSocket session.

        :param request: The prepared request.
        :return: An `AsyncWebSocketTestSession` for the WebSocket connection.
        """
        scheme, netloc, path, query, fragment = (
            str(item) for item in urlsplit(request.url)
        )
        if scheme not in {"ws", "wss"}:
            raise ValueError("Available only for websockets connection")

        default_port = {"http": 80, "ws": 80, "https": 443, "wss": 443}[scheme]

        if ":" in netloc:
            host, port_string = netloc.split(":", 1)
            port = int(port_string)
        else:
            host = netloc
            port = default_port

        # Include the 'host' header.
        request_headers = request.headers or {}
        if "host" in request_headers:
            headers: typing.List[typing.Tuple[bytes, bytes]] = []
        elif port == default_port:
            headers = [(b"host", host.encode())]
        else:
            headers = [(b"host", (f"{host}:{port}").encode())]

        # Include other request headers.
        headers += [
            (key.lower().encode(), value.encode())
            for key, value in request_headers.items()
        ]

        subprotocol = request_headers.get("sec-websocket-protocol", None)
        if subprotocol is None:
            subprotocols: typing.Sequence[str] = []
        else:
            subprotocols = [value.strip() for value in subprotocol.split(",")]

        scope = {
            "type": "websocket",
            "path": unquote(path),
            "root_path": self.root_path,
            "scheme": scheme,
            "query_string": query.encode(),
            "headers": headers,
            "client": ["testclient", 50000],
            "server": [host, port],
            "subprotocols": subprotocols,
        }

        # Create new queues for each WebSocket connection
        receive_queue = asyncio.Queue()
        send_queue = asyncio.Queue()
        session = AsyncWebSocketTestSession(
            app=self.app,
            scope=scope,
            event_loop=self.event_loop,
            receive_queue=receive_queue,
            send_queue=send_queue,
        )
        raise _Upgrade(session)  # type: ignore[arg-type]


class LifespanStartupError(Exception):
    """Exception raised when lifespan startup fails."""

    pass


class AsyncTestClient(Session):
    """
    Asynchronous test client for ASGI applications.

    Async version of Starlette's `TestClient` supporting WebSocket connections.
    """

    __test__ = False  # For pytest to not discover this up.

    def __init__(
        self,
        app: ASGI3App,
        base_url: str = "http://testclient",
        raise_server_exceptions: bool = True,
        root_path: str = "",
        event_loop: typing.Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """
        Initialize the asynchronous test client.

        :param app: The ASGI application to test.
        :param base_url: The base URL for the client.
        :param raise_server_exceptions: Whether to raise server exceptions.
        :param root_path: The root path for the application.
        :param event_loop: The asyncio event loop to use.
        """
        super().__init__()

        # Separate queues for lifespan
        self.event_loop = event_loop or asyncio.get_event_loop()
        self.lifespan_receive_queue = asyncio.Queue()
        self.lifespan_send_queue = asyncio.Queue()

        adapter = _ASGIAdapter(
            app,
            raise_server_exceptions=raise_server_exceptions,
            root_path=root_path,
            event_loop=self.event_loop,
        )
        self.mount("http://", adapter)
        self.mount("https://", adapter)
        self.mount("ws://", adapter)
        self.mount("wss://", adapter)
        self.headers.update({"user-agent": "testclient"})
        self.app = app
        self.base_url = base_url
        self._lifespan_task = None

    def request(  # type: ignore
        self,
        method: str,
        url: str,
        params=None,
        data=None,
        headers: typing.Optional[typing.MutableMapping[str, str]] = None,
        cookies=None,
        files=None,
        auth=None,
        timeout=None,
        allow_redirects: bool = True,
        proxies: typing.Optional[typing.MutableMapping[str, str]] = None,
        hooks: typing.Any = None,
        stream: typing.Optional[bool] = None,
        verify: typing.Optional[typing.Union[bool, str]] = None,
        cert: typing.Optional[typing.Union[str, typing.Tuple[str, str]]] = None,
        json: typing.Any = None,
    ) -> requests.Response:
        url = urljoin(self.base_url, url)
        return super().request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            allow_redirects=allow_redirects,
            proxies=proxies,
            hooks=hooks,
            stream=stream,
            verify=verify,
            cert=cert,
            json=json,
        )

    def websocket_connect(
        self,
        url: str,
        subprotocols: typing.Optional[typing.Sequence[str]] = None,
        **kwargs: typing.Any,
    ) -> AsyncWebSocketTestSession:
        """
        Establish a WebSocket connection to the ASGI application.

        :param url: The WebSocket URL to connect to.
        :param subprotocols: Optional list of subprotocols to use.
        :param kwargs: Additional arguments to pass to the request.
        :return: An `AsyncWebSocketTestSession` for the connection.
        """
        host = self.base_url.split("://")[1]
        url = urljoin(f"ws://{host}", url)

        headers = kwargs.get("headers", {})
        headers.setdefault("connection", "upgrade")
        headers.setdefault("sec-websocket-key", "testserver==")
        headers.setdefault("sec-websocket-version", "13")

        if subprotocols is not None:
            headers.setdefault("sec-websocket-protocol", ", ".join(subprotocols))

        kwargs["headers"] = headers
        try:
            super().request("GET", url, **kwargs)
        except _Upgrade as exc:
            session = exc.session
            if not isinstance(session, AsyncWebSocketTestSession):
                raise RuntimeError(
                    f"Expected `AsyncWebSocketTestSession`, got {type(session)}"
                ) from exc
        else:
            raise RuntimeError("Expected WebSocket upgrade")  # pragma: no cover
        return session

    async def __lifespan_receive(self) -> Message:
        """
        Receive a message from the lifespan receive queue.
        """
        while True:
            try:
                return self.lifespan_receive_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def __lifespan_send(self, message: Message) -> None:
        """
        Send a message to the lifespan send queue.

        :param message: The message to send.
        """
        await self.lifespan_send_queue.put(message)

    async def __receive_from_lifespan(self) -> Message:
        """
        Receive a message from the lifespan send queue.

        :return: The received message.
        """
        while True:
            try:
                return self.lifespan_send_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def __send_to_lifespan(self, message: Message) -> None:
        """
        Send a message to the lifespan receive queue.

        :param message: The message to send.
        """
        await self.lifespan_receive_queue.put(message)

    async def __aenter__(self):
        # Create lifespan task
        self._lifespan_task = self.event_loop.create_task(
            self.app(
                {"type": "lifespan"}, self.__lifespan_receive, self.__lifespan_send
            )  # type: ignore[arg-type]
        )

        # Send startup event
        await self.__send_to_lifespan({"type": "lifespan.startup"})
        # Confirm startup complete
        message = await self.__receive_from_lifespan()

        # If startup failed, raise the error
        if message["type"] == "lifespan.startup.failed":
            raise LifespanStartupError(
                message.get("message", "Lifespan startup failed")
            )

        # Ensure startup complete
        assert message["type"] == "lifespan.startup.complete", (
            f"Expected startup complete, got {message['type']}"
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Only send shutdown if startup was successful
        if exc_type is None or not issubclass(exc_type, LifespanStartupError):
            await self.__send_to_lifespan({"type": "lifespan.shutdown"})
            message = await self.__receive_from_lifespan()
            if message["type"] not in (
                "lifespan.shutdown.complete",
                "lifespan.shutdown.failed",
            ):
                raise RuntimeError(
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

        if exc_type is not None:
            return False  # Propagate exception
