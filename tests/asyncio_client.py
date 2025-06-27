import asyncio
import json
import typing
from urllib.parse import unquote, urljoin, urlsplit

import requests
from starlette.testclient import (
    ASGI2App,
    ASGI3App,
    Message,
    Scope,
    WebSocketDisconnect,
    _Upgrade,
)


class AsyncioWebSocketTestSession:
    def __init__(
        self,
        app: ASGI3App,
        scope: Scope,
        event_loop: asyncio.AbstractEventLoop,
        receive_queue: typing.Optional[asyncio.Queue] = None,
        send_queue: typing.Optional[asyncio.Queue] = None,
    ) -> None:
        self.event_loop = event_loop
        self.app = app
        self.scope = scope
        self.accepted_subprotocol = None
        self._receive_queue = receive_queue
        self._send_queue = send_queue

    async def __aenter__(self) -> "AsyncioWebSocketTestSession":
        self.event_loop.create_task(self._run())
        await self.send({"type": "websocket.connect"})
        print("CONNECTING...")
        message = await self.receive()
        assert message["type"] == "websocket.accept"
        print("CONNECTED")

        self.accepted_subprotocol = message.get("subprotocol", None)
        return self

    async def __aexit__(self, *args: typing.Any) -> None:
        await self.close(1000)
        print("CLOSING...")
        while not self._send_queue.empty():
            message = await self._send_queue.get()
            if isinstance(message, BaseException):
                raise message
        print("CLOSED")

    async def _run(self) -> None:
        """
        The sub-thread in which the websocket session runs.
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
        while self._receive_queue.empty():
            await asyncio.sleep(0)
        return await self._receive_queue.get()

    async def _asgi_send(self, message: Message) -> None:
        await self._send_queue.put(message)

    def _raise_on_close(self, message: Message) -> None:
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000))

    async def send(self, message: Message) -> None:
        await self._receive_queue.put(message)

    async def send_text(self, data: str) -> None:
        await self.send({"type": "websocket.receive", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        await self.send({"type": "websocket.receive", "bytes": data})

    async def send_json(self, data: typing.Any, mode: str = "text") -> None:
        assert mode in ["text", "binary"]
        text = json.dumps(data)
        if mode == "text":
            return await self.send({"type": "websocket.receive", "text": text})

        return await self.send(
            {"type": "websocket.receive", "bytes": text.encode("utf-8")}
        )

    async def close(self, code: int = 1000) -> None:
        await self.send({"type": "websocket.disconnect", "code": code})

    async def receive(self) -> Message:
        while True:
            try:
                message = self._send_queue.get_nowait()

                if isinstance(message, BaseException):
                    raise message

                return message
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def receive_text(self) -> str:
        message = await self.receive()
        self._raise_on_close(message)
        return message["text"]

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        self._raise_on_close(message)
        return message["bytes"]

    async def receive_json(self, mode: str = "text") -> typing.Any:
        assert mode in ["text", "binary"]
        message = await self.receive()
        self._raise_on_close(message)

        if mode == "text":
            text = message["text"]
        else:
            text = message["bytes"].decode("utf-8")

        return json.loads(text)


class _AsyncioASGIAdapter(requests.adapters.HTTPAdapter):
    def __init__(
        self,
        app: ASGI3App,
        event_loop: asyncio.AbstractEventLoop,
        raise_server_exceptions: bool = True,
        root_path: str = "",
        receive_queue: asyncio.Queue = None,
        send_queue: asyncio.Queue = None,
    ) -> None:
        self.event_loop = event_loop
        self.app = app
        self.raise_server_exceptions = raise_server_exceptions
        self.root_path = root_path
        self.receive_queue = receive_queue
        self.send_queue = send_queue

    def send(
        self, request: requests.PreparedRequest, *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncioWebSocketTestSession:
        scheme, netloc, path, query, fragment = (
            str(item) for item in urlsplit(request.url)
        )

        default_port = {"http": 80, "ws": 80, "https": 443, "wss": 443}[scheme]

        if ":" in netloc:
            host, port_string = netloc.split(":", 1)
            port = int(port_string)
        else:
            host = netloc
            port = default_port

        # Include the 'host' header.
        if "host" in request.headers:
            headers: typing.List[typing.Tuple[bytes, bytes]] = []
        elif port == default_port:
            headers = [(b"host", host.encode())]
        else:
            headers = [(b"host", (f"{host}:{port}").encode())]

        # Include other request headers.
        headers += [
            (key.lower().encode(), value.encode())
            for key, value in request.headers.items()
        ]

        if scheme not in {"ws", "wss"}:
            raise ValueError("Available only for websockets connection")

        subprotocol = request.headers.get("sec-websocket-protocol", None)

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
        session = AsyncioWebSocketTestSession(
            self.app,
            scope,
            self.event_loop,
            receive_queue=self.receive_queue,
            send_queue=self.send_queue,
        )
        raise _Upgrade(session)


class AsyncioTestClient(requests.Session):
    __test__ = False  # For pytest to not discover this up.

    def __init__(
        self,
        app: typing.Union[ASGI2App, ASGI3App],
        base_url: str = "http://testclient",
        raise_server_exceptions: bool = True,
        root_path: str = "",
        event_loop: asyncio.AbstractEventLoop = None,
    ) -> None:
        super().__init__()

        self.receive_queue = asyncio.Queue()
        self.send_queue = asyncio.Queue()
        self.event_loop = event_loop or asyncio.get_event_loop()

        adapter = _AsyncioASGIAdapter(
            app,
            raise_server_exceptions=raise_server_exceptions,
            root_path=root_path,
            event_loop=event_loop,
            receive_queue=self.receive_queue,
            send_queue=self.send_queue,
        )
        self.mount("http://", adapter)
        self.mount("https://", adapter)
        self.mount("ws://", adapter)
        self.mount("wss://", adapter)
        self.headers.update({"user-agent": "testclient"})
        self.app = app
        self.base_url = base_url

    def request(  # type: ignore
        self,
        method: str,
        url: str,
        params=None,
        data=None,
        headers: typing.MutableMapping[str, str] = None,
        cookies=None,
        files=None,
        auth=None,
        timeout=None,
        allow_redirects: bool = None,
        proxies: typing.MutableMapping[str, str] = None,
        hooks: typing.Any = None,
        stream: bool = None,
        verify: typing.Union[bool, str] = None,
        cert: typing.Union[str, typing.Tuple[str, str]] = None,
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
        self, url: str, subprotocols: typing.Sequence[str] = None, **kwargs: typing.Any
    ) -> AsyncioWebSocketTestSession:
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
        else:
            raise RuntimeError("Expected WebSocket upgrade")  # pragma: no cover

        return session

    async def __app_receive(self) -> Message:
        while True:
            try:
                return self.receive_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0.1)

    async def __app_send(self, message):
        await self.send_queue.put(message)

    async def __receive(self) -> Message:
        while True:
            try:
                return self.send_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                await asyncio.sleep(0)

    async def __send(self, message):
        await self.receive_queue.put(message)

    async def __aenter__(self):
        self.event_loop.create_task(
            self.app({"type": "lifespan"}, self.__app_receive, self.__app_send)
        )

        await self.__send({"type": "lifespan.startup"})
        message = await self.__receive()

        if message["type"] == "lifespan.startup.failed":
            raise ValueError(message["message"])

        assert message["type"] in (
            "lifespan.startup.complete",
            # "lifespan.startup.failed",
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # await self.__send({"type": "lifespan.shutdown"})
        # message = await self.__receive()
        # assert message["type"] in (
        #     "lifespan.shutdown.complete",
        #     "lifespan.shutdown.failed",
        # )
        pass
