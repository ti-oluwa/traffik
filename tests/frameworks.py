"""ASGI framework adapters for integration tests."""

import typing
from dataclasses import dataclass

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Lifespan

HTTPEndpoint = typing.Callable[..., typing.Awaitable[typing.Any]]
WebSocketEndpoint = typing.Callable[..., typing.Awaitable[None]]


@dataclass(frozen=True)
class HTTPRoute:
    """A framework-agnostic HTTP route spec."""

    path: str
    endpoint: HTTPEndpoint
    methods: typing.Sequence[str] = ("GET",)


@dataclass(frozen=True)
class WSRoute:
    """A framework-agnostic WebSocket route spec."""

    path: str
    endpoint: WebSocketEndpoint


class ASGIFramework(typing.Protocol):
    """
    ASGI web framework adapter

    Implments logic for assembling an app of one specific framework from route specs.
    """

    name: str

    def build_app(
        self,
        *,
        http_routes: typing.Optional[typing.Sequence[HTTPRoute]] = None,
        ws_routes: typing.Optional[typing.Sequence[WSRoute]] = None,
        lifespan: typing.Optional[Lifespan[typing.Any]] = None,
        middleware: typing.Optional[typing.Sequence[Middleware]] = None,
    ) -> ASGIApp: ...


class FastAPIAdapter:
    name = "fastapi"

    def build_app(
        self,
        *,
        http_routes: typing.Optional[typing.Sequence[HTTPRoute]] = None,
        ws_routes: typing.Optional[typing.Sequence[WSRoute]] = None,
        lifespan: typing.Optional[Lifespan[typing.Any]] = None,
        middleware: typing.Optional[typing.Sequence[Middleware]] = None,
    ) -> ASGIApp:
        from fastapi import FastAPI

        app = FastAPI(lifespan=lifespan, middleware=middleware, debug=True)
        if http_routes:
            for route in http_routes:
                app.add_api_route(
                    route.path, route.endpoint, methods=list(route.methods)
                )

        if ws_routes:
            for ws_route in ws_routes:
                app.add_api_websocket_route(ws_route.path, ws_route.endpoint)
        return app


class StarletteAdapter:
    name = "starlette"

    def build_app(
        self,
        *,
        http_routes: typing.Optional[typing.Sequence[HTTPRoute]] = None,
        ws_routes: typing.Optional[typing.Sequence[WSRoute]] = None,
        lifespan: typing.Optional[Lifespan[typing.Any]] = None,
        middleware: typing.Optional[typing.Sequence[Middleware]] = None,
    ) -> ASGIApp:
        from starlette.applications import Starlette
        from starlette.routing import Route, WebSocketRoute

        routes = []
        if http_routes:
            routes += [
                Route(route.path, route.endpoint, methods=list(route.methods))
                for route in http_routes
            ]
        if ws_routes:
            routes += [
                WebSocketRoute(ws_route.path, ws_route.endpoint)
                for ws_route in ws_routes
            ]
        return Starlette(
            routes=routes, lifespan=lifespan, middleware=middleware, debug=True
        )
