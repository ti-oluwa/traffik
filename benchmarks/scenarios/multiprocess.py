import sys

if sys.platform == "win32" or sys.platform == "cygwin":
    raise SystemExit(
        "Multiprocess inmemory backend benchmarks are not supported on Windows. "
        "This module requires the 'fork' multiprocessing start method."
    )

import asyncio
import time
import typing

import httpx2

from benchmarks.backends import create_strategy
from benchmarks.base import BenchmarkConfig, ScenarioResult
from benchmarks.scenarios.common import (
    ScenarioFunc,
    make_http_app,
    run_http_scenario,
    send_sequential,
)
from traffik.backends.base import ThrottleBackend
from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


def create_multiprocess_backend(config: BenchmarkConfig) -> MultiProcessInMemoryBackend:
    """
    Create a MultiProcessInMemoryBackend with benchmark-appropriate settings.

    :param config: Benchmark configuration containing shard and key counts.
    :return: An uninitialized MultiProcessInMemoryBackend instance.
    """
    return MultiProcessInMemoryBackend.create(
        namespace="bench",
        number_of_shards=config.multiprocess_shards,
        max_keys=config.multiprocess_max_keys,
        cleanup_frequency=30.0,
    )


async def below_limit_steady(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Below-Limit Steady State scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_below_limit_{id(registry)}",
            rate="200/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Below-Limit Steady State",
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
            scenario_name="MP Below-Limit Steady State",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def at_limit_edge(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """At-Limit Edge scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_at_limit_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP At-Limit Edge",
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
            scenario_name="MP At-Limit Edge",
            backend_kind="multiprocess",
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


async def over_limit_burst(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Over-Limit Burst scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_over_limit_{id(registry)}",
            rate="50/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Over-Limit Burst",
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
            scenario_name="MP Over-Limit Burst",
            backend_kind="multiprocess",
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


async def concurrent_contention(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Concurrent Contention scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_concurrent_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Concurrent Contention",
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
            scenario_name="MP Concurrent Contention",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def single_hot_key(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Single Hot Key scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_hot_key_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Single Hot Key",
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
            scenario_name="MP Single Hot Key",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def many_unique_keys(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Many Unique Keys scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_many_keys_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)

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
                    scenario_name="MP Many Unique Keys",
                    backend_kind="multiprocess",
                    strategy_kind=config.strategy_kind,
                    total_requests=300,
                    successful_requests=successful,
                    throttled_requests=throttled,
                    error_requests=errors,
                    total_time_seconds=total_time,
                    latencies_seconds=latencies,
                    iteration=iteration,
                )
    except Exception as exc:  # noqa  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="MP Many Unique Keys",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def window_boundary_burst(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Window Boundary Burst scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_window_boundary_{id(registry)}",
            rate="20/1s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
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
                    scenario_name="MP Window Boundary Burst",
                    backend_kind="multiprocess",
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
            scenario_name="MP Window Boundary Burst",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def sustained_high_load(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Sustained High Load scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_sustained_{id(registry)}",
            rate="1000/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Sustained High Load",
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
            scenario_name="MP Sustained High Load",
            backend_kind="multiprocess",
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
        if owns_backend:
            await backend.close()


async def error_recovery(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """Error Recovery scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_error_recovery_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
            on_error="allow",
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Error Recovery (on_error=allow)",
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
            scenario_name="MP Error Recovery (on_error=allow)",
            backend_kind="multiprocess",
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


async def shared_memory_stress(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """MP Shared Memory Stress scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_shared_memory_{id(registry)}",
            rate="500/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)
        return await run_http_scenario(
            "MP Shared Memory Stress",
            app,
            backend,
            n=2000,
            config=config,
            concurrent=True,
            iteration=iteration,
        )
    except Exception as exc:  # noqa
        print(f"WARN: Scenario failed: {exc}", file=sys.stderr)
        return ScenarioResult(
            scenario_name="MP Shared Memory Stress",
            backend_kind="multiprocess",
            strategy_kind=config.strategy_kind,
            total_requests=2000,
            successful_requests=0,
            throttled_requests=0,
            error_requests=2000,
            total_time_seconds=0.001,
            latencies_seconds=[],
            iteration=iteration,
        )
    finally:
        if owns_backend:
            await backend.close()


async def key_eviction(
    config: BenchmarkConfig,
    iteration: int = 1,
    backend: typing.Optional[ThrottleBackend[typing.Any, typing.Any]] = None,
) -> ScenarioResult:
    """MP Key Eviction scenario."""
    owns_backend = backend is None
    if owns_backend:
        backend = create_multiprocess_backend(config)
        await backend.initialize()

    try:
        strategy = create_strategy(config)
        registry = ThrottleRegistry()
        throttle = HTTPThrottle(
            uid=f"bench_mp_key_eviction_{id(registry)}",
            rate="100/60s",
            backend=backend,
            strategy=strategy,
            registry=registry,
        )
        app = make_http_app(throttle)

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

                # First 250 requests
                for i in range(250):
                    try:
                        user_id = f"user-{i % 1000}"
                        start = time.perf_counter()
                        response = await client.get(
                            "/test", headers={"X-Client-ID": user_id}
                        )
                        end = time.perf_counter()
                        latencies.append(end - start)

                        if response.status_code == 200:
                            successful += 1
                        elif response.status_code == 429:
                            throttled += 1
                        else:
                            errors += 1
                    except Exception:  # noqa
                        errors += 1

                # Sleep to allow cleanup cycle
                await asyncio.sleep(6.0)

                # Remaining 250 requests
                for i in range(250, 500):
                    try:
                        user_id = f"user-{i % 1000}"
                        start = time.perf_counter()
                        response = await client.get(
                            "/test", headers={"X-Client-ID": user_id}
                        )
                        end = time.perf_counter()
                        latencies.append(end - start)

                        if response.status_code == 200:
                            successful += 1
                        elif response.status_code == 429:
                            throttled += 1
                        else:
                            errors += 1
                    except Exception:  # noqa
                        errors += 1

                end_time = time.perf_counter()
                total_time = end_time - start_time

                return ScenarioResult(
                    scenario_name="MP Key Eviction",
                    backend_kind="multiprocess",
                    strategy_kind=config.strategy_kind,
                    total_requests=500,
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
            scenario_name="MP Key Eviction",
            backend_kind="multiprocess",
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
        if owns_backend:
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
    "shared_memory": shared_memory_stress,
    "key_eviction": key_eviction,
}
