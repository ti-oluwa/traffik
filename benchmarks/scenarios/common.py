import asyncio
import time
import typing

import httpx
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect

from benchmarks.base import BenchmarkConfig, ScenarioResult
from traffik.backends.base import ThrottleBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.throttles import HTTPThrottle, WebSocketThrottle, is_throttled


class ScenarioFunc(typing.Protocol):
    async def __call__(
        self, config: BenchmarkConfig, iteration: int
    ) -> ScenarioResult: ...


def make_http_app(
    throttle: HTTPThrottle, backend: ThrottleBackend[typing.Any, typing.Any]
) -> FastAPI:
    """
    Create a minimal FastAPI app with a single throttled GET /test endpoint.

    :param throttle: An HTTPThrottle instance.
    :param backend: The backend the throttle is bound to.
    :return: A FastAPI application instance.
    """
    app = FastAPI(lifespan=backend.lifespan)

    @app.get("/test")
    async def test_endpoint(request: Request = Depends(throttle)):
        return {"status": "ok"}

    return app


def make_ws_app(
    throttle: WebSocketThrottle, backend: ThrottleBackend[typing.Any, typing.Any]
) -> FastAPI:
    """
    Create a minimal FastAPI app with a throttled WebSocket at /ws.

    :param throttle: A WebSocketThrottle instance.
    :param backend: The backend instance.
    :return: A FastAPI application using backend.lifespan.
    """
    app = FastAPI(lifespan=backend.lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_json()
                await throttle(websocket)

                if is_throttled(websocket):
                    await websocket.send_json({"type": "rate_limit"})
                else:
                    await websocket.send_json({"echo": data, "status": "ok"})
        except WebSocketDisconnect:
            pass

    return app


def make_middleware_app(
    throttle: typing.Union[MiddlewareThrottle, HTTPThrottle],
    backend: ThrottleBackend[typing.Any, typing.Any],
) -> FastAPI:
    """
    Create a FastAPI app with ThrottleMiddleware applied to /test.

    Also has an /unthrottled endpoint for selective throttling tests.

    :param throttle: An HTTPThrottle instance.
    :param backend: The backend instance.
    :param middleware_throttle: A MiddlewareThrottle wrapping the throttle.
    :return: A FastAPI application instance with ThrottleMiddleware.
    """
    app = FastAPI(lifespan=backend.lifespan)

    @app.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    @app.get("/unthrottled")
    async def unthrottled_endpoint():
        return {"status": "ok"}

    app.add_middleware(  # type: ignore
        ThrottleMiddleware,
        throttle=throttle,  # type: ignore[arg-type]
        path="/test",  # type: ignore[arg-type]
        methods={"GET"},  # type: ignore[arg-type]
    )

    return app


async def send_sequential(
    client: httpx.AsyncClient,
    n: int,
    path: str = "/test",
    headers: typing.Optional[typing.Dict[str, str]] = None,
) -> typing.Tuple[typing.List[float], int, int, int]:
    """
    Send `n` sequential GET requests and collect timing and status counts.

    :param client: An initialized httpx AsyncClient.
    :param n: Number of requests to send.
    :param path: URL path to request.
    :param headers: Optional headers to send with each request.
    :return: Tuple of (latencies_seconds, successful, throttled, errors).
    """
    latencies = []
    successful = 0
    throttled = 0
    errors = 0

    for _ in range(n):
        try:
            start = time.perf_counter()
            response = await client.get(path, headers=headers)
            end = time.perf_counter()
            latencies.append(end - start)

            if response.status_code == 200:
                successful += 1
            elif response.status_code == 429:
                throttled += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    return latencies, successful, throttled, errors


async def send_concurrent(
    client: httpx.AsyncClient,
    n: int,
    concurrency: int,
    path: str = "/test",
    headers: typing.Optional[typing.Dict[str, str]] = None,
) -> typing.Tuple[typing.List[float], int, int, int]:
    """
    Send `n` requests in batches of `concurrency` using asyncio.gather.

    :param client: An initialized httpx AsyncClient.
    :param n: Total number of requests to send.
    :param concurrency: Max requests per gather batch.
    :param path: URL path to request.
    :param headers: Optional headers to send with each request.
    :return: Tuple of (latencies_seconds, successful, throttled, errors).
    """
    latencies = []
    successful = 0
    throttled = 0
    errors = 0

    num_batches = (n + concurrency - 1) // concurrency

    for batch_idx in range(num_batches):
        batch_size = min(concurrency, n - batch_idx * concurrency)

        async def single_request(idx: int):
            try:
                start = time.perf_counter()
                response = await client.get(path, headers=headers)
                end = time.perf_counter()
                return end - start, response.status_code
            except Exception:
                return 0.0, 0

        tasks = [single_request(batch_idx * concurrency + i) for i in range(batch_size)]
        results = await asyncio.gather(*tasks)

        for latency, status_code in results:
            if latency > 0:
                latencies.append(latency)

            if status_code == 200:
                successful += 1
            elif status_code == 429:
                throttled += 1
            elif status_code != 0:
                errors += 1
            else:
                errors += 1

    return latencies, successful, throttled, errors


async def run_http_scenario(
    scenario_name: str,
    app: FastAPI,
    backend,
    n: int,
    config: BenchmarkConfig,
    concurrent: bool = False,
    iteration: int = 1,
    path: str = "/test",
    headers: typing.Optional[typing.Dict[str, str]] = None,
) -> ScenarioResult:
    """
    Run a single HTTP scenario iteration.

    Enters the backend context, creates an httpx ASGI client, runs requests,
    and returns a ScenarioResult.

    :param scenario_name: Human-readable name for this scenario.
    :param app: The FastAPI application to test.
    :param backend: The initialized backend instance.
    :param n: Number of requests to send.
    :param config: Benchmark configuration.
    :param concurrent: If True, send requests concurrently in batches.
    :param iteration: Iteration number for this run.
    :param path: URL path to request.
    :param headers: Optional headers to send with each request.
    :return: A populated ScenarioResult.
    """
    async with backend(persistent=False, close_on_exit=False, initialized=True):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=30.0,
        ) as client:
            start_time = time.perf_counter()

            if concurrent:
                latencies, successful, throttled, errors = await send_concurrent(
                    client, n, config.concurrency, path, headers
                )
            else:
                latencies, successful, throttled, errors = await send_sequential(
                    client, n, path, headers
                )

            end_time = time.perf_counter()
            total_time = end_time - start_time

            return ScenarioResult(
                scenario_name=scenario_name,
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                total_requests=n,
                successful_requests=successful,
                throttled_requests=throttled,
                error_requests=errors,
                total_time_seconds=total_time,
                latencies_seconds=latencies,
                iteration=iteration,
            )
