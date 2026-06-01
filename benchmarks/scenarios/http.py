import asyncio
import sys
import time
import typing

import httpx2

from benchmarks.backends import create_backend, create_strategy
from benchmarks.base import BenchmarkConfig, ScenarioResult
from benchmarks.scenarios.common import (
    ScenarioFunc,
    make_http_app,
    run_http_scenario,
    send_sequential,
)
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


async def below_limit_steady(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Below-Limit Steady State scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_below_limit_{id(registry)}",
            rate="200/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Below-Limit Steady State",
            app,
            backend,
            n=80,
            config=config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Below-Limit Steady State",
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


async def at_limit_edge(config: BenchmarkConfig, iteration: int = 1) -> ScenarioResult:
    """At-Limit Edge scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_at_limit_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "At-Limit Edge",
            app,
            backend,
            n=100,
            config=config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="At-Limit Edge",
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


async def over_limit_burst(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Over-Limit Burst scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_over_limit_{id(registry)}",
            rate="50/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Over-Limit Burst",
            app,
            backend,
            n=200,
            config=config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Over-Limit Burst",
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


async def concurrent_contention(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Concurrent Contention scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_concurrent_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Concurrent Contention",
            app,
            backend,
            n=500,
            config=config,
            concurrent=True,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Concurrent Contention",
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


async def single_hot_key(config: BenchmarkConfig, iteration: int = 1) -> ScenarioResult:
    """Single Hot Key scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_hot_key_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Single Hot Key",
            app,
            backend,
            n=300,
            config=config,
            concurrent=True,
            iteration=iteration,
            headers={"X-Client-ID": "hot-key-user"},
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Single Hot Key",
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


async def many_unique_keys(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Many Unique Keys scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_many_keys_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)

        # Simulate many unique keys by using concurrent requests with different headers
        async with backend(persistent=False, close_on_exit=False, initialized=True):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
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

                    async def single_request(user_idx: int):
                        try:
                            user_id = f"user-{user_idx % 50}"
                            start = time.perf_counter()
                            response = await client.get(
                                "/test", headers={"X-Client-ID": user_id}
                            )
                            end = time.perf_counter()
                            return end - start, response.status_code
                        except Exception:  # noqa
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
                    scenario_name="Many Unique Keys",
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
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Many Unique Keys",
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


async def window_boundary_burst(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Window Boundary Burst scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_window_boundary_{id(registry)}",
            rate="20/1s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)

        async with backend(persistent=False, close_on_exit=False, initialized=True):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
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
                    scenario_name="Window Boundary Burst",
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
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Window Boundary Burst",
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


async def sustained_high_load(
    config: BenchmarkConfig, iteration: int = 1
) -> ScenarioResult:
    """Sustained High Load scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_sustained_{id(registry)}",
            rate="1000/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Sustained High Load",
            app,
            backend,
            n=800,
            config=config,
            concurrent=True,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Sustained High Load",
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


async def error_recovery(config: BenchmarkConfig, iteration: int = 1) -> ScenarioResult:
    """Error Recovery scenario."""
    backend = create_backend(config)
    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_error_recovery_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
            on_error="allow",
        )
        await backend.initialize()
        app = make_http_app(throttle)
        return await run_http_scenario(
            "Error Recovery (on_error=allow)",
            app,
            backend,
            n=100,
            config=config,
            concurrent=False,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="Error Recovery (on_error=allow)",
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


SCENARIOS: typing.Dict[str, ScenarioFunc] = {
    "below_limit": below_limit_steady,
    "at_limit": at_limit_edge,
    "over_limit": over_limit_burst,
    "concurrent": concurrent_contention,
    "hot_key": single_hot_key,
    "many_keys": many_unique_keys,
    "window_boundary": window_boundary_burst,
    "sustained": sustained_high_load,
    "error_recovery": error_recovery,
}
