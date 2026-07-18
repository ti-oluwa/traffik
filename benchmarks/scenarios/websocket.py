import asyncio
import sys
import time
import typing

from benchmarks.backends import create_backend, create_strategy
from benchmarks.base import BenchmarkConfig, ScenarioResult
from benchmarks.client import AsyncTestClient, AsyncWebSocketTestSession
from benchmarks.scenarios.common import ScenarioFunc, make_ws_app
from traffik import WebSocketThrottle
from traffik.backends.base import ThrottleBackend
from traffik.registry import ThrottleRegistry


async def send_messages(
    ws: AsyncWebSocketTestSession, n: int
) -> typing.Tuple[typing.List[float], int, int]:
    """
    Send n JSON messages over an open WebSocket session and collect timing.

    :param ws: An open `AsyncWebSocketTestSession`.
    :param n: Number of messages to send.
    :return: Tuple of (latencies_seconds, successful, throttled).
    """
    latencies = []
    successful = 0
    throttled = 0

    for i in range(n):
        try:
            start = time.perf_counter()
            await ws.send_json({"message": f"test_{i}"})
            response = await ws.receive_json()
            end = time.perf_counter()
            latencies.append(end - start)

            if response.get("type") == "rate_limit":
                throttled += 1
            else:
                successful += 1
        except Exception:  # noqa
            pass

    return latencies, successful, throttled


async def below_limit(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """WS Below-Limit scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = WebSocketThrottle(
            uid=f"bench_ws_below_limit_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_ws_app(throttle, backend)

        async with (
            AsyncTestClient(app) as client,
            client.websocket_connect("/ws") as ws,
        ):
            start = time.perf_counter()
            latencies, successful, throttled = await send_messages(ws, 50)
            end = time.perf_counter()
            total_time = end - start

            return ScenarioResult(
                scenario_name="WS Below-Limit",
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                total_requests=50,
                successful_requests=successful,
                throttled_requests=throttled,
                error_requests=0,
                total_time_seconds=total_time,
                latencies_seconds=latencies,
                iteration=iteration,
            )
    except Exception as exc:  # noqa
        print(f"WARN: WS scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="WS Below-Limit",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=50,
            successful_requests=0,
            throttled_requests=0,
            error_requests=50,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


async def over_limit(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """WS Over-Limit scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = WebSocketThrottle(
            uid=f"bench_ws_over_limit_{id(registry)}",
            rate="50/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_ws_app(throttle, backend)

        async with (
            AsyncTestClient(app) as client,
            client.websocket_connect("/ws") as ws,
        ):
            start = time.perf_counter()
            latencies, successful, throttled = await send_messages(ws, 150)
            end = time.perf_counter()
            total_time = end - start

            return ScenarioResult(
                scenario_name="WS Over-Limit",
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                total_requests=150,
                successful_requests=successful,
                throttled_requests=throttled,
                error_requests=0,
                total_time_seconds=total_time,
                latencies_seconds=latencies,
                iteration=iteration,
            )
    except Exception as exc:  # noqa
        print(f"WARN: WS scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="WS Over-Limit",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=150,
            successful_requests=0,
            throttled_requests=0,
            error_requests=150,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


async def burst(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """WS Burst scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = WebSocketThrottle(
            uid=f"bench_ws_burst_{id(registry)}",
            rate="20/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_ws_app(throttle, backend)

        async with (
            AsyncTestClient(app) as client,
            client.websocket_connect("/ws") as ws,
        ):
            start = time.perf_counter()
            latencies, successful, throttled = await send_messages(ws, 100)
            end = time.perf_counter()
            total_time = end - start

            return ScenarioResult(
                scenario_name="WS Burst",
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                total_requests=100,
                successful_requests=successful,
                throttled_requests=throttled,
                error_requests=0,
                total_time_seconds=total_time,
                latencies_seconds=latencies,
                iteration=iteration,
            )
    except Exception as exc:  # noqa
        print(f"WARN: WS scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="WS Burst",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=100,
            successful_requests=0,
            throttled_requests=0,
            error_requests=100,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


async def concurrent_connections(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """WS Concurrent Connections scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = WebSocketThrottle(
            uid=f"bench_ws_concurrent_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_ws_app(throttle, backend)

        all_latencies = []
        total_successful = 0
        total_throttled = 0

        async def single_connection():
            try:
                async with (
                    AsyncTestClient(app) as client,
                    client.websocket_connect("/ws") as ws,
                ):
                    latencies, successful, throttled = await send_messages(ws, 20)
                    return latencies, successful, throttled
            except Exception:  # noqa
                return [], 0, 0

        start = time.perf_counter()
        tasks = [single_connection() for _ in range(10)]
        results = await asyncio.gather(*tasks)
        end = time.perf_counter()
        total_time = end - start

        for latencies, successful, throttled in results:
            all_latencies.extend(latencies)
            total_successful += successful
            total_throttled += throttled

        return ScenarioResult(
            scenario_name="WS Concurrent Connections",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=200,
            successful_requests=total_successful,
            throttled_requests=total_throttled,
            error_requests=0,
            total_time_seconds=total_time,
            latencies_seconds=all_latencies,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: WS scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="WS Concurrent Connections",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=200,
            successful_requests=0,
            throttled_requests=0,
            error_requests=200,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


async def window_boundary(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """WS Window Boundary scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = WebSocketThrottle(
            uid=f"bench_ws_window_boundary_{id(registry)}",
            rate="10/1s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_ws_app(throttle, backend)

        async with (
            AsyncTestClient(app) as client,
            client.websocket_connect("/ws") as ws,
        ):
            start = time.perf_counter()

            all_latencies = []
            total_successful = 0
            total_throttled = 0

            # Three waves of 10 messages each
            for wave in range(3):
                latencies, successful, throttled = await send_messages(ws, 10)
                all_latencies.extend(latencies)
                total_successful += successful
                total_throttled += throttled

                if wave < 2:
                    await asyncio.sleep(1.1)

            end = time.perf_counter()
            total_time = end - start

            return ScenarioResult(
                scenario_name="WS Window Boundary",
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                total_requests=30,
                successful_requests=total_successful,
                throttled_requests=total_throttled,
                error_requests=0,
                total_time_seconds=total_time,
                latencies_seconds=all_latencies,
                iteration=iteration,
            )
    except Exception as exc:  # noqa
        print(f"WARN: WS scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="WS Window Boundary",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=30,
            successful_requests=0,
            throttled_requests=0,
            error_requests=30,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


SCENARIOS: typing.Dict[str, ScenarioFunc] = {
    "below_limit": below_limit,
    "over_limit": over_limit,
    "burst": burst,
    "concurrent": concurrent_connections,
    "window_boundary": window_boundary,
}
