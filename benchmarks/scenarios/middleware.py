import asyncio
import sys
import time
import typing

import httpx

from benchmarks.backends import create_backend, create_strategy
from benchmarks.base import BenchmarkConfig, ScenarioResult
from benchmarks.scenarios.common import (
    ScenarioFunc,
    make_middleware_app,
    run_http_scenario,
    send_sequential,
)
from traffik.middleware import MiddlewareThrottle
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


async def scenario_below_limit_steady(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Below-Limit Steady State scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_below_limit_{id(registry)}",
            rate="200/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Below-Limit Steady State",
            app,
            backend,
            80,
            config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Below-Limit Steady State",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=80,
            successful_requests=0,
            throttled_requests=0,
            error_requests=80,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_at_limit_edge(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """At-Limit Edge scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_at_limit_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware At-Limit Edge",
            app,
            backend,
            100,
            config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware At-Limit Edge",
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
        await backend.close()


async def scenario_over_limit_burst(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Over-Limit Burst scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_over_limit_{id(registry)}",
            rate="50/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Over-Limit Burst",
            app,
            backend,
            200,
            config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Over-Limit Burst",
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
        await backend.close()


async def scenario_concurrent_contention(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Concurrent Contention scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_concurrent_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Concurrent Contention",
            app,
            backend,
            500,
            config,
            concurrent=True,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Concurrent Contention",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=500,
            successful_requests=0,
            throttled_requests=0,
            error_requests=500,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_single_hot_key(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Single Hot Key scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_hot_key_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Single Hot Key",
            app,
            backend,
            300,
            config,
            concurrent=True,
            iteration=iteration,
            headers={"X-Client-ID": "hot-key-user"},
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Single Hot Key",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=300,
            successful_requests=0,
            throttled_requests=0,
            error_requests=300,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_many_unique_keys(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Many Unique Keys scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_many_keys_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)

        async with backend(persistent=False, close_on_exit=False, initialized=True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                timeout=30.0,
            ) as client:
                start_time = time.perf_counter()

                latencies = []
                successful = 0
                throttled = 0
                errors = 0

                num_batches = (300 + 10 - 1) // 10
                for batch_idx in range(num_batches):
                    batch_size = min(10, 300 - batch_idx * 10)

                    async def single_request(user_idx):
                        try:
                            user_id = f"user-{user_idx % 50}"
                            start = time.perf_counter()
                            response = await client.get(
                                "/test", headers={"X-Client-ID": user_id}
                            )
                            end = time.perf_counter()
                            return end - start, response.status_code
                        except Exception:
                            return 0.0, 0

                    tasks = [
                        single_request(batch_idx * 10 + i) for i in range(batch_size)
                    ]
                    results = await asyncio.gather(*tasks)

                    for latency, status_code in results:
                        if latency > 0:
                            latencies.append(latency)
                        if status_code == 200:
                            successful += 1
                        elif status_code == 429:
                            throttled += 1
                        else:
                            errors += 1

                end_time = time.perf_counter()
                total_time = end_time - start_time

                return ScenarioResult(
                    scenario_name="Middleware Many Unique Keys",
                    backend_kind=config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    total_requests=300,
                    successful_requests=successful,
                    throttled_requests=throttled,
                    error_requests=errors,
                    total_time_seconds=total_time,
                    latencies_seconds=latencies,
                    iteration=iteration,
                )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Many Unique Keys",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=300,
            successful_requests=0,
            throttled_requests=0,
            error_requests=300,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_window_boundary_burst(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Window Boundary Burst scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_window_boundary_{id(registry)}",
            rate="20/1s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)

        async with backend(persistent=False, close_on_exit=False, initialized=True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                timeout=30.0,
            ) as client:
                start_time = time.perf_counter()

                all_latencies = []
                total_successful = 0
                total_throttled = 0
                total_errors = 0

                # Three waves of 20 requests each
                for wave in range(3):
                    latencies, successful, throttled, errors = await send_sequential(
                        client, 20
                    )
                    all_latencies.extend(latencies)
                    total_successful += successful
                    total_throttled += throttled
                    total_errors += errors

                    if wave < 2:
                        await asyncio.sleep(1.1)

                end_time = time.perf_counter()
                total_time = end_time - start_time

                return ScenarioResult(
                    scenario_name="Middleware Window Boundary Burst",
                    backend_kind=config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    total_requests=60,
                    successful_requests=total_successful,
                    throttled_requests=total_throttled,
                    error_requests=total_errors,
                    total_time_seconds=total_time,
                    latencies_seconds=all_latencies,
                    iteration=iteration,
                )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Window Boundary Burst",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=60,
            successful_requests=0,
            throttled_requests=0,
            error_requests=60,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_sustained_high_load(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Sustained High Load scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_sustained_{id(registry)}",
            rate="1000/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Sustained High Load",
            app,
            backend,
            800,
            config,
            concurrent=True,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Sustained High Load",
            backend_kind=config.backend_kind,
            strategy_kind=config.strategy_kind,
            total_requests=800,
            successful_requests=0,
            throttled_requests=0,
            error_requests=800,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        await backend.close()


async def scenario_error_recovery(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Error Recovery scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_error_recovery_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
            on_error="allow",
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)
        return await run_http_scenario(
            "Middleware Error Recovery (on_error=allow)",
            app,
            backend,
            100,
            config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Middleware Error Recovery (on_error=allow)",
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
        await backend.close()


async def scenario_selective_throttling(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Selective Throttling scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mw_selective_{id(registry)}",
            rate="50/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        middleware_throttle = MiddlewareThrottle(throttle)
        await backend.initialize()
        app = make_middleware_app(middleware_throttle, backend)

        async with backend(persistent=False, close_on_exit=False, initialized=True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                timeout=30.0,
            ) as client:
                start_time = time.perf_counter()

                all_latencies = []
                total_successful = 0
                total_throttled = 0
                total_errors = 0

                # 100 requests to /test (throttled)
                latencies, successful, throttled, errors = await send_sequential(
                    client, 100, path="/test"
                )
                all_latencies.extend(latencies)
                total_successful += successful
                total_throttled += throttled
                total_errors += errors

                # 100 requests to /unthrottled (not throttled)
                latencies, successful, throttled, errors = await send_sequential(
                    client, 100, path="/unthrottled"
                )
                all_latencies.extend(latencies)
                total_successful += successful
                total_throttled += throttled
                total_errors += errors

                end_time = time.perf_counter()
                total_time = end_time - start_time

                return ScenarioResult(
                    scenario_name="Selective Throttling",
                    backend_kind=config.backend_kind,
                    strategy_kind=config.strategy_kind,
                    total_requests=200,
                    successful_requests=total_successful,
                    throttled_requests=total_throttled,
                    error_requests=total_errors,
                    total_time_seconds=total_time,
                    latencies_seconds=all_latencies,
                    iteration=iteration,
                )
    except Exception as exc:
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Selective Throttling",
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
        await backend.close()


SCENARIOS: typing.Dict[str, ScenarioFunc] = {
    "below_limit": scenario_below_limit_steady,
    "at_limit": scenario_at_limit_edge,
    "over_limit": scenario_over_limit_burst,
    "concurrent": scenario_concurrent_contention,
    "hot_key": scenario_single_hot_key,
    "many_keys": scenario_many_unique_keys,
    "window_boundary": scenario_window_boundary_burst,
    "sustained": scenario_sustained_high_load,
    "error_recovery": scenario_error_recovery,
    "selective": scenario_selective_throttling,
}
