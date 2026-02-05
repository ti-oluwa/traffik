"""
WebSocket rate limiting benchmarks for Traffik.

Usage:
    python benchmarks/websockets.py --help
    python benchmarks/websockets.py --backend redis --strategy sliding-window-counter
    python benchmarks/websockets.py --scenarios burst,sustained --iterations 5
"""

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from asynctestclient import AsyncTestClient  # type: ignore[import]
from base import BenchmarkMemcachedBackend, custom_identifier  # type: ignore[import]
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from traffik import WebSocketThrottle, is_throttled
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.strategies.custom import GCRAStrategy
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.strategies.leaky_bucket import LeakyBucketStrategy
from traffik.strategies.sliding_window import (
    SlidingWindowCounterStrategy,
    SlidingWindowLogStrategy,
)
from traffik.strategies.token_bucket import (
    TokenBucketStrategy,
    TokenBucketWithDebtStrategy,
)


@dataclass
class BenchmarkConfig:
    """Configuration for benchmark execution."""

    scenarios: List[str] = field(
        default_factory=lambda: ["low", "high", "sustained", "burst", "concurrent"]
    )
    iterations: int = 3
    backend: str = "inmemory"
    strategy: str = "fixed-window"
    redis_url: Optional[str] = None
    memcached_url: Optional[str] = None
    memcached_track_keys: bool = False


@dataclass
class ScenarioResult:
    """Results from a single benchmark scenario."""

    name: str
    total_messages: int
    successful_messages: int
    throttled_messages: int
    total_time: float
    latencies: List[float]

    @property
    def messages_per_second(self) -> float:
        return self.total_messages / self.total_time if self.total_time > 0 else 0

    @property
    def success_rate(self) -> float:
        return (
            (self.successful_messages / self.total_messages * 100)
            if self.total_messages > 0
            else 0
        )

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) * 1000 if self.latencies else 0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[idx] * 1000

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_latencies = sorted(self.latencies)
        idx = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[idx] * 1000


STRATEGIES = {
    "fixed-window": FixedWindowStrategy,
    "sliding-window-counter": SlidingWindowCounterStrategy,
    "sliding-window-log": SlidingWindowLogStrategy,
    "token-bucket": TokenBucketStrategy,
    "token-bucket-debt": TokenBucketWithDebtStrategy,
    "leaky-bucket": LeakyBucketStrategy,
    "gcra": GCRAStrategy,
}


def create_backend(config: BenchmarkConfig):
    """Create backend based on configuration."""
    if config.backend == "redis":
        if not config.redis_url:
            raise ValueError("Redis URL required for redis backend")
        return RedisBackend(
            connection=config.redis_url,
            namespace="bench",
            identifier=custom_identifier,
            persistent=False,
        )
    elif config.backend == "memcached":
        return BenchmarkMemcachedBackend(
            url=config.memcached_url or "memcached://localhost:11211",
            namespace="bench",
            identifier=custom_identifier,
            persistent=False,
            track_keys=config.memcached_track_keys,
        )
    return InMemoryBackend(
        namespace="bench",
        identifier=custom_identifier,
        persistent=False,
        number_of_shards=5,
    )


def create_strategy(config: BenchmarkConfig):
    """Create strategy based on configuration."""
    strategy_cls = STRATEGIES.get(config.strategy.replace("_", "-"))
    if strategy_cls:
        return strategy_cls()
    return FixedWindowStrategy()


def create_app(
    limit: int = 100, window: int = 60, config: Optional[BenchmarkConfig] = None
):
    """Create FastAPI app with WebSocket throttling."""
    if config is None:
        config = BenchmarkConfig()

    backend = create_backend(config)
    strategy = create_strategy(config)

    app = FastAPI(lifespan=backend.lifespan)

    throttle = WebSocketThrottle(
        uid="ws_bench",
        rate=f"{limit}/{window}s",
        backend=backend,  # type: ignore
        strategy=strategy,
    )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_json()
                await throttle(websocket)
                if is_throttled(websocket):
                    continue

                await websocket.send_json({"echo": data, "status": "ok"})
        except WebSocketDisconnect:
            pass

    return app


async def run_scenario_low_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 1: Low load - messages well within limit."""
    num_messages = 50

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()
    base_url = "http://test"
    running_loop = asyncio.get_running_loop()
    async with (
        AsyncTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client,
        client.websocket_connect(url="/ws", timeout=30.0) as websocket,
    ):
        for i in range(num_messages):
            msg_start = time.perf_counter()

            await websocket.send_json({"message": f"test_{i}"})
            response = await websocket.receive_json()

            msg_end = time.perf_counter()
            latencies.append(msg_end - msg_start)

            if response.get("type") == "rate_limit":
                throttled += 1
            else:
                successful += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Low Load",
        total_messages=num_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_high_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 2: High load - messages exceeding limit."""
    num_messages = 200

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    base_url = "http://test"
    running_loop = asyncio.get_running_loop()
    async with (
        AsyncTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client,
        client.websocket_connect(url="/ws", timeout=30.0) as websocket,
    ):
        for i in range(num_messages):
            msg_start = time.perf_counter()

            await websocket.send_json({"message": f"test_{i}"})
            response = await websocket.receive_json()

            msg_end = time.perf_counter()
            latencies.append(msg_end - msg_start)

            if response.get("type") == "rate_limit":
                throttled += 1
            else:
                successful += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="High Load",
        total_messages=num_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_sustained_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 3: Sustained load under higher limit."""
    app = create_app(limit=1000, window=60, config=config)
    num_messages = 500

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    base_url = "http://test"
    running_loop = asyncio.get_running_loop()
    async with (
        AsyncTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client,
        client.websocket_connect(url="/ws", timeout=30.0) as websocket,
    ):
        for i in range(num_messages):
            msg_start = time.perf_counter()

            await websocket.send_json({"message": f"test_{i}"})
            response = await websocket.receive_json()

            msg_end = time.perf_counter()
            latencies.append(msg_end - msg_start)

            if response.get("type") == "rate_limit":
                throttled += 1
            else:
                successful += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Sustained Load",
        total_messages=num_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_burst_traffic(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 4: Burst traffic pattern."""
    app = create_app(limit=50, window=60, config=config)
    num_messages = 100

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    base_url = "http://test"
    running_loop = asyncio.get_running_loop()
    async with (
        AsyncTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client,
        client.websocket_connect(url="/ws", timeout=30.0) as websocket,
    ):
        for i in range(num_messages):
            msg_start = time.perf_counter()

            await websocket.send_json({"message": f"test_{i}"})
            response = await websocket.receive_json()

            msg_end = time.perf_counter()
            latencies.append(msg_end - msg_start)

            if response.get("type") == "rate_limit":
                throttled += 1
            else:
                successful += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Burst Traffic",
        total_messages=num_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_concurrent_connections(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 5: Multiple concurrent WebSocket connections."""
    app = create_app(limit=100, window=60, config=config)
    num_connections = 10
    messages_per_connection = 15

    latencies = []
    successful = 0
    throttled = 0
    total_messages = 0

    start_time = time.perf_counter()

    async def send_messages(client_id: int):
        local_latencies = []
        local_successful = 0
        local_throttled = 0

        base_url = "http://test"
        running_loop = asyncio.get_running_loop()
        async with (
            AsyncTestClient(
                app=app,
                base_url=base_url,
                event_loop=running_loop,
            ) as client,
            client.websocket_connect(url="/ws", timeout=30.0) as websocket,
        ):
            for i in range(messages_per_connection):
                msg_start = time.perf_counter()

                await websocket.send_json({"client": client_id, "message": i})
                response = await websocket.receive_json()

                msg_end = time.perf_counter()
                local_latencies.append(msg_end - msg_start)

                if response.get("type") == "rate_limit":
                    local_throttled += 1
                else:
                    local_successful += 1

        return local_latencies, local_successful, local_throttled

    tasks = [send_messages(i) for i in range(num_connections)]
    results = await asyncio.gather(*tasks)

    for result_latencies, result_successful, result_throttled in results:
        latencies.extend(result_latencies)
        successful += result_successful
        throttled += result_throttled
        total_messages += len(result_latencies)

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Concurrent Connections",
        total_messages=total_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_benchmark_suite(
    config: BenchmarkConfig,
) -> Dict[str, List[ScenarioResult]]:
    """Run all benchmark scenarios."""
    print(f"\n{'=' * 60}")
    print("Running WebSocket Benchmarks")
    print(f"{'=' * 60}\n")
    print(f"Backend: {config.backend}")
    print(f"Strategy: {config.strategy}")
    print(f"Iterations: {config.iterations}\n")

    results = {}

    scenario_runners = {
        "low": ("Low Load", run_scenario_low_load),
        "high": ("High Load", run_scenario_high_load),
        "sustained": ("Sustained Load", run_scenario_sustained_load),
        "burst": ("Burst Traffic", run_scenario_burst_traffic),
        "concurrent": ("Concurrent Connections", run_scenario_concurrent_connections),
    }

    for scenario_key in config.scenarios:
        if scenario_key not in scenario_runners:
            print(f"Warning: Unknown scenario '{scenario_key}', skipping...")
            continue

        scenario_name, runner = scenario_runners[scenario_key]
        print(f"Scenario: {scenario_name}...")

        scenario_results = []
        for i in range(config.iterations):
            app = create_app(limit=100, window=60, config=config)

            result = await runner(app, config)
            scenario_results.append(result)

            if i < config.iterations - 1:
                await asyncio.sleep(0.5)

        results[scenario_name] = scenario_results
        await asyncio.sleep(1.0)

    return results


def aggregate_results(results: List[ScenarioResult]) -> ScenarioResult:
    """Aggregate multiple scenario results."""
    if not results:
        raise ValueError("No results to aggregate")

    total_messages = sum(r.total_messages for r in results)
    successful = sum(r.successful_messages for r in results)
    throttled = sum(r.throttled_messages for r in results)
    total_time = sum(r.total_time for r in results)
    all_latencies = []
    for r in results:
        all_latencies.extend(r.latencies)

    return ScenarioResult(
        name=results[0].name,
        total_messages=total_messages,
        successful_messages=successful,
        throttled_messages=throttled,
        total_time=total_time,
        latencies=all_latencies,
    )


def print_results(results: Dict[str, List[ScenarioResult]]):
    """Print benchmark results."""
    print("\n" + "=" * 80)
    print("WEBSOCKET BENCHMARK RESULTS")
    print("=" * 80)

    print("\n" + "-" * 80)
    print(f"{'Scenario':<25} {'Metric':<25} {'Value':<15}")
    print("-" * 80)

    for scenario_name, scenario_results in results.items():
        agg = aggregate_results(scenario_results)

        print(
            f"{scenario_name:<25} {'Messages/sec':<25} {agg.messages_per_second:<15.2f}"
        )
        print(f"{'':<25} {'Success Rate (%)':<25} {agg.success_rate:<15.1f}")
        print(f"{'':<25} {'P50 Latency (ms)':<25} {agg.p50_latency:<15.2f}")
        print(f"{'':<25} {'P95 Latency (ms)':<25} {agg.p95_latency:<15.2f}")
        print(f"{'':<25} {'P99 Latency (ms)':<25} {agg.p99_latency:<15.2f}")
        print(f"{'':<25} {'Total Messages':<25} {agg.total_messages:<15}")
        print(f"{'':<25} {'Successful':<25} {agg.successful_messages:<15}")
        print(f"{'':<25} {'Throttled':<25} {agg.throttled_messages:<15}")
        print()

    print("=" * 80 + "\n")


async def main():
    parser = argparse.ArgumentParser(
        description="WebSocket rate limiting benchmarks for Traffik",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--scenarios",
        type=str,
        default="low,high,sustained,burst,concurrent",
        help="Comma-separated list of scenarios to run (default: all)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per scenario (default: 3)",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["inmemory", "redis", "memcached"],
        default="inmemory",
        help="Backend to use (default: inmemory)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        choices=list(STRATEGIES.keys()),
        default="fixed-window",
        help="Strategy to use (default: fixed-window)",
    )
    parser.add_argument(
        "--redis-url",
        type=str,
        default="redis://localhost:6379/0",
        help="Redis URL (required if backend=redis)",
    )
    parser.add_argument(
        "--memcached-url",
        type=str,
        default="memcached://localhost:11211",
        help="Memcached URL (required if backend=memcached)",
    )
    parser.add_argument(
        "--memcached-track-keys",
        action="store_true",
        help="Enable key tracking for Memcached backend (default: disabled)",
    )

    args = parser.parse_args()
    config = BenchmarkConfig(
        scenarios=args.scenarios.split(","),
        iterations=args.iterations,
        backend=args.backend,
        strategy=args.strategy,
        redis_url=args.redis_url,
        memcached_url=args.memcached_url,
        memcached_track_keys=args.memcached_track_keys,
    )

    print("Starting WebSocket benchmarks...")
    print(f"Scenarios: {', '.join(config.scenarios)}")
    print(f"Iterations per scenario: {config.iterations}\n")

    results = await run_benchmark_suite(config)
    print_results(results)

    print("Benchmark complete!")


if __name__ == "__main__":
    asyncio.run(main())
