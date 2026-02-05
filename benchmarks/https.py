"""
HTTP benchmarks comparing Traffik and SlowAPI rate limiting.

Usage:
    python benchmarks/https.py --help
    python benchmarks/https.py --traffik-backend redis --traffik-strategy sliding-window-counter
    python benchmarks/https.py --scenarios low,high,burst --iterations 3
"""

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from base import BenchmarkMemcachedBackend, custom_identifier  # type: ignore[import]
from fastapi import FastAPI, Request
from slowapi import Limiter as SlowAPILimiter
from starlette.testclient import TestClient

from traffik import HTTPThrottle, get_remote_address
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.strategies.custom import GCRAStrategy
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.strategies.leaky_bucket import (
    LeakyBucketStrategy,
    LeakyBucketWithQueueStrategy,
)
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

    # General settings
    scenarios: List[str] = field(
        default_factory=lambda: [
            "low",
            "high",
            "sustained",
            "burst",
            "race",
            "distributed",
        ]
    )
    iterations: int = 3
    cooldown_seconds: float = 2.0

    # Traffik configuration
    traffik_backend: str = "inmemory"  # inmemory, redis, memcached
    traffik_strategy: str = "fixed-window"  # fixed-window, sliding-window-counter, sliding-window-log, token-bucket, token-bucket-debt, leaky-bucket, leaky-bucket-queue
    traffik_redis_url: Optional[str] = None
    traffik_memcached_url: Optional[str] = None
    # We will not track keys for memcached backend by default to avoid performance overhead
    traffik_memcached_track_keys: bool = False

    # SlowAPI configuration
    slowapi_backend: str = "inmemory"  # inmemory, redis, memcached
    slowapi_strategy: str = "fixed-window"  # fixed-window, sliding-window-counter, fixed-window-elastic-expiry, moving-window
    slowapi_redis_url: Optional[str] = None
    slowapi_memcached_url: Optional[str] = None


@dataclass
class ScenarioResult:
    """Results from a single benchmark scenario."""

    name: str
    library: str
    total_requests: int
    successful_requests: int
    throttled_requests: int
    total_time: float
    latencies: List[float]

    @property
    def requests_per_second(self) -> float:
        return self.total_requests / self.total_time if self.total_time > 0 else 0

    @property
    def success_rate(self) -> float:
        return (
            (self.successful_requests / self.total_requests * 100)
            if self.total_requests > 0
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


def create_traffik_backend(config: BenchmarkConfig):
    """Create Traffik backend based on configuration."""
    if config.traffik_backend == "redis":
        if not config.traffik_redis_url:
            raise ValueError("Redis URL required for redis backend")
        return RedisBackend(
            connection=config.traffik_redis_url,
            namespace="traffik:bench",
            identifier=custom_identifier,
            persistent=False,
        )
    elif config.traffik_backend == "memcached":
        return BenchmarkMemcachedBackend(
            url=config.traffik_memcached_url or "memcached://localhost:11211",
            namespace="traffik:bench",
            identifier=custom_identifier,
            persistent=False,
            track_keys=config.traffik_memcached_track_keys,
        )
    # inmemory
    return InMemoryBackend(
        namespace="traffik:bench",
        identifier=custom_identifier,
        persistent=False,
        number_of_shards=5,
    )


TRAFFIK_STRATEGIES = {
    "fixed-window": FixedWindowStrategy,
    "sliding-window-counter": SlidingWindowCounterStrategy,
    "sliding-window-log": SlidingWindowLogStrategy,
    "token-bucket": TokenBucketStrategy,
    "token-bucket-debt": TokenBucketWithDebtStrategy,
    "leaky-bucket": LeakyBucketStrategy,
    "leaky-bucket-queue": LeakyBucketWithQueueStrategy,
    "gcra": GCRAStrategy,
}


def create_traffik_strategy(config: BenchmarkConfig):
    """Create Traffik strategy based on configuration."""
    strategy_cls = TRAFFIK_STRATEGIES.get(config.traffik_strategy.replace("_", "-"))
    if strategy_cls:
        return strategy_cls()
    return FixedWindowStrategy()


def create_traffik_app(
    limit: int = 100, window: int = 60, config: Optional[BenchmarkConfig] = None
):
    """Create FastAPI app with Traffik rate limiting."""
    if config is None:
        config = BenchmarkConfig()

    backend = create_traffik_backend(config)
    strategy = create_traffik_strategy(config)

    app = FastAPI(lifespan=backend.lifespan)

    throttle = HTTPThrottle(
        uid="bench",
        rate=f"{limit}/{window}s",
        backend=backend,  # type: ignore
        strategy=strategy,
    )

    @app.get("/test")
    # @throttled(throttle)
    async def test_endpoint(request: Request):
        await throttle(request)
        return {"status": "ok"}

    return app


def create_slowapi_backend(config: BenchmarkConfig):
    """Create SlowAPI backend based on configuration."""
    if config.slowapi_backend == "redis":
        if not config.slowapi_redis_url:
            raise ValueError("Redis URL required for redis backend")
        return config.slowapi_redis_url
    elif config.slowapi_backend == "memcached":
        if not config.slowapi_memcached_url:
            raise ValueError("Memcached URL required for memcached backend")
        return config.slowapi_memcached_url
    # inmemory
    return "memory://"


def create_slowapi_app(
    limit: int = 100, window: int = 60, config: Optional[BenchmarkConfig] = None
):
    """Create FastAPI app with SlowAPI rate limiting."""
    if config is None:
        config = BenchmarkConfig()

    app = FastAPI()
    storage_uri = create_slowapi_backend(config)

    limiter = SlowAPILimiter(
        key_func=lambda request: request.headers.get("X-Client-ID")
        or get_remote_address(request)
        or "anonymous",
        storage_uri=storage_uri,
        strategy=config.slowapi_strategy.replace("_", "-"),
    )

    @app.get("/test")
    @limiter.limit(f"{limit}/{window}second")
    async def test_endpoint(request: Request):
        return {"status": "ok"}

    app.state.limiter = limiter
    return app


async def run_scenario_low_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 1: Low load - requests well within limit."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"
    num_requests = 50

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    with TestClient(app) as client:
        for _ in range(num_requests):
            req_start = time.perf_counter()
            response = client.get("/test")
            req_end = time.perf_counter()

            latencies.append(req_end - req_start)

            if response.status_code == 200:
                successful += 1
            elif response.status_code == 429:
                throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Low Load",
        library=library,
        total_requests=num_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_high_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 2: High load - requests exceeding limit."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"
    num_requests = 200

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    with TestClient(app) as client:
        for _ in range(num_requests):
            req_start = time.perf_counter()
            response = client.get("/test")
            req_end = time.perf_counter()

            latencies.append(req_end - req_start)

            if response.status_code == 200:
                successful += 1
            elif response.status_code == 429:
                throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="High Load",
        library=library,
        total_requests=num_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_sustained_load(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 3: Sustained load under higher limit."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"

    # Create new app with higher limit
    if library == "Traffik":
        app = create_traffik_app(limit=1000, window=60, config=config)
    else:
        app = create_slowapi_app(limit=1000, window=60, config=config)

    num_requests = 500
    concurrency = 50  # 50 concurrent requests at a time

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def make_request():
            req_start = time.perf_counter()
            response = await client.get("/test")
            req_end = time.perf_counter()
            return req_end - req_start, response.status_code

        # Create batches of concurrent requests
        for batch_start in range(0, num_requests, concurrency):
            batch_size = min(concurrency, num_requests - batch_start)
            tasks = [make_request() for _ in range(batch_size)]

            results = await asyncio.gather(*tasks)

            for req_time, status in results:
                latencies.append(req_time)
                if status == 200:
                    successful += 1
                elif status == 429:
                    throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Sustained Load",
        library=library,
        total_requests=num_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_burst_traffic(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 4: Burst traffic pattern."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"

    # Create new app with 50 req/min limit
    if library == "Traffik":
        app = create_traffik_app(limit=50, window=60, config=config)
    else:
        app = create_slowapi_app(limit=50, window=60, config=config)

    num_requests = 100

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    with TestClient(app) as client:
        # Burst all requests quickly
        for _ in range(num_requests):
            req_start = time.perf_counter()
            response = client.get("/test")
            req_end = time.perf_counter()

            latencies.append(req_end - req_start)

            if response.status_code == 200:
                successful += 1
            elif response.status_code == 429:
                throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Burst Load",
        library=library,
        total_requests=num_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_race_conditions(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 5: Test concurrent requests for race conditions."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"

    # Create new app with 100 req/min limit
    if library == "Traffik":
        app = create_traffik_app(limit=100, window=60, config=config)
    else:
        app = create_slowapi_app(limit=100, window=60, config=config)

    num_concurrent = 150

    latencies = []
    successful = 0
    throttled = 0

    start_time = time.perf_counter()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def make_request():
            req_start = time.perf_counter()
            response = await client.get("/test")
            req_end = time.perf_counter()
            return response, req_end - req_start

        # Fire all requests concurrently
        tasks = [make_request() for _ in range(num_concurrent)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                continue
            response, latency = result
            latencies.append(latency)

            if response.status_code == 200:
                successful += 1
            elif response.status_code == 429:
                throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Race Condition Test",
        library=library,
        total_requests=num_concurrent,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_scenario_distributed(
    app: FastAPI, config: BenchmarkConfig
) -> ScenarioResult:
    """Scenario 6: Test distributed correctness with multiple clients."""
    library = "Traffik" if hasattr(app.state, "backend") else "SlowAPI"

    # Create new app with 100 req/min limit per client
    if library == "Traffik":
        app = create_traffik_app(limit=100, window=60, config=config)
    else:
        app = create_slowapi_app(limit=100, window=60, config=config)

    num_clients = 10
    requests_per_client = 120

    latencies = []
    successful = 0
    throttled = 0
    total_requests = 0

    start_time = time.perf_counter()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def make_client_requests(client_id: int):
            """Make all requests for a single client."""
            results_list = []
            for _ in range(requests_per_client):
                req_start = time.perf_counter()
                response = await client.get(
                    "/test", headers={"X-Client-ID": f"client-{client_id}"}
                )
                req_end = time.perf_counter()
                results_list.append((response, req_end - req_start))
            return results_list

        # Run all clients concurrently
        client_tasks = [make_client_requests(i) for i in range(num_clients)]
        all_client_results = await asyncio.gather(*client_tasks)

        # Aggregate results
        for client_results in all_client_results:
            for response, latency in client_results:
                total_requests += 1
                latencies.append(latency)

                if response.status_code == 200:
                    successful += 1
                elif response.status_code == 429:
                    throttled += 1

    end_time = time.perf_counter()
    return ScenarioResult(
        name="Distributed Correctness",
        library=library,
        total_requests=total_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=end_time - start_time,
        latencies=latencies,
    )


async def run_benchmark_suite(
    library: str, config: BenchmarkConfig
) -> Dict[str, List[ScenarioResult]]:
    """Run all benchmark scenarios for a library."""
    print(f"\n{'=' * 60}")
    print(f"Running benchmarks for: {library}")
    print(f"{'=' * 60}\n")

    if library == "Traffik":
        print(f"Backend: {config.traffik_backend}")
        print(f"Strategy: {config.traffik_strategy}\n")
    else:
        print(f"Backend: {config.slowapi_backend}\n")
        print(f"Strategy: {config.slowapi_strategy}\n")

    results = {}

    scenario_runners = {
        "low": ("Low Load", run_scenario_low_load),
        "high": ("High Load", run_scenario_high_load),
        "sustained": ("Sustained Load", run_scenario_sustained_load),
        "burst": ("Burst Load", run_scenario_burst_traffic),
        "race": ("Race Condition Test", run_scenario_race_conditions),
        "distributed": ("Distributed Correctness", run_scenario_distributed),
    }

    for scenario_key in config.scenarios:
        if scenario_key not in scenario_runners:
            print(f"Warning: Unknown scenario '{scenario_key}', skipping...")
            continue

        scenario_name, runner = scenario_runners[scenario_key]
        print(f"Scenario: {scenario_name}...")

        scenario_results = []
        for i in range(config.iterations):
            # Create fresh app for each iteration
            if library == "Traffik":
                app = create_traffik_app(limit=100, window=60, config=config)
            else:
                app = create_slowapi_app(limit=100, window=60, config=config)

            result = await runner(app, config)
            scenario_results.append(result)

            # Cleanup
            if hasattr(app.state, "backend"):
                await app.state.backend.close()

            if i < config.iterations - 1:
                await asyncio.sleep(0.5)  # Brief pause between iterations

        results[scenario_name] = scenario_results
        await asyncio.sleep(1.0)  # Pause between scenarios

    return results


def aggregate_results(results: List[ScenarioResult]) -> ScenarioResult:
    """Aggregate multiple scenario results."""
    if not results:
        raise ValueError("No results to aggregate")

    total_requests = sum(r.total_requests for r in results)
    successful = sum(r.successful_requests for r in results)
    throttled = sum(r.throttled_requests for r in results)
    total_time = sum(r.total_time for r in results)
    all_latencies = []
    for r in results:
        all_latencies.extend(r.latencies)

    return ScenarioResult(
        name=results[0].name,
        library=results[0].library,
        total_requests=total_requests,
        successful_requests=successful,
        throttled_requests=throttled,
        total_time=total_time,
        latencies=all_latencies,
    )


def print_comparison(
    traffik_results: Dict[str, List[ScenarioResult]],
    slowapi_results: Dict[str, List[ScenarioResult]],
):
    """Print comparison table of results."""
    print("\n" + "=" * 80)
    print("BENCHMARK RESULTS COMPARISON")
    print("=" * 80)

    print("\nPERFORMANCE BENCHMARKS")
    print("-" * 80)
    print(
        f"{'Scenario':<20} {'Metric':<25} {'Traffik':<15} {'SlowAPI':<15} {'Winner':<10}"
    )
    print("-" * 80)

    performance_scenarios = ["Low Load", "High Load", "Sustained Load", "Burst Load"]

    traffik_wins = 0
    slowapi_wins = 0
    ties = 0
    scenarios_compared = 0

    for scenario in performance_scenarios:
        if scenario not in traffik_results or scenario not in slowapi_results:
            continue

        scenarios_compared += 1
        t_agg = aggregate_results(traffik_results[scenario])
        s_agg = aggregate_results(slowapi_results[scenario])

        # Requests/sec comparison
        t_rps = t_agg.requests_per_second
        s_rps = s_agg.requests_per_second
        rps_winner = (
            "Traffik" if t_rps > s_rps else "SlowAPI" if s_rps > t_rps else "Tie"
        )
        if rps_winner == "Traffik":
            traffik_wins += 1
        elif rps_winner == "SlowAPI":
            slowapi_wins += 1
        else:  # Tie
            ties += 1

        diff_pct = ((t_rps - s_rps) / s_rps * 100) if s_rps > 0 else 0

        print(
            f"{scenario:<20} {'Requests/sec':<25} {t_rps:<15.2f} {s_rps:<15.2f} {rps_winner:<10}"
        )
        print(f"{'':<20} {'  Difference':<25} {diff_pct:+.1f}%")

        # Latency comparison
        p50_winner = "Traffik" if t_agg.p50_latency < s_agg.p50_latency else "SlowAPI"
        print(
            f"{'':<20} {'P50 Latency (ms)':<25} {t_agg.p50_latency:<15.2f} {s_agg.p50_latency:<15.2f} {p50_winner:<10}"
        )

        p95_winner = "Traffik" if t_agg.p95_latency < s_agg.p95_latency else "SlowAPI"
        print(
            f"{'':<20} {'P95 Latency (ms)':<25} {t_agg.p95_latency:<15.2f} {s_agg.p95_latency:<15.2f} {p95_winner:<10}"
        )

        p99_winner = "Traffik" if t_agg.p99_latency < s_agg.p99_latency else "SlowAPI"
        print(
            f"{'':<20} {'P99 Latency (ms)':<25} {t_agg.p99_latency:<15.2f} {s_agg.p99_latency:<15.2f} {p99_winner:<10}"
        )

        print(
            f"{'':<20} {'Success Rate (%)':<25} {t_agg.success_rate:<15.1f} {s_agg.success_rate:<15.1f}"
        )
        print()

    # Correctness tests
    print("\nCORRECTNESS TESTS")
    print("-" * 80)
    print(f"{'Test':<30} {'Traffik':<20} {'SlowAPI':<20}")
    print("-" * 70)

    if (
        "Race Condition Test" in traffik_results
        and "Race Condition Test" in slowapi_results
    ):
        t_race = aggregate_results(traffik_results["Race Condition Test"])
        s_race = aggregate_results(slowapi_results["Race Condition Test"])

        # Number of iterations determines expected values
        num_iterations = len(traffik_results["Race Condition Test"])
        expected_success = 100 * num_iterations
        expected_success_min = 95 * num_iterations
        expected_success_max = 105 * num_iterations

        print("Race Condition Test")
        print(
            f"  {'Successful':<28} {t_race.successful_requests:<20} {s_race.successful_requests:<20}"
        )
        print(
            f"  {'Throttled':<28} {t_race.throttled_requests:<20} {s_race.throttled_requests:<20}"
        )
        print(
            f"  {'Expected Success':<28} {'~' + str(expected_success):<20} {'~' + str(expected_success):<20}"
        )

        t_race_ok = (
            expected_success_min <= t_race.successful_requests <= expected_success_max
        )
        s_race_ok = (
            expected_success_min <= s_race.successful_requests <= expected_success_max
        )
        print(
            f"  {'Within Expected Range':<28} {'Yes' if t_race_ok else 'No':<20} {'Yes' if s_race_ok else 'No':<20}"
        )
        print()

    if (
        "Distributed Correctness" in traffik_results
        and "Distributed Correctness" in slowapi_results
    ):
        t_dist = aggregate_results(traffik_results["Distributed Correctness"])
        s_dist = aggregate_results(slowapi_results["Distributed Correctness"])

        # Number of iterations determines expected values
        num_iterations = len(traffik_results["Distributed Correctness"])
        expected_success = 1000 * num_iterations
        expected_throttled = 200 * num_iterations
        expected_success_min = 950 * num_iterations
        expected_success_max = 1050 * num_iterations

        print("Distributed Correctness")
        print(
            f"  {'Successful':<28} {t_dist.successful_requests:<20} {s_dist.successful_requests:<20}"
        )
        print(
            f"  {'Throttled':<28} {t_dist.throttled_requests:<20} {s_dist.throttled_requests:<20}"
        )
        print(
            f"  {'Expected Success':<28} {'~' + str(expected_success):<20} {'~' + str(expected_success):<20}"
        )
        print(
            f"  {'Expected Throttled':<28} {'~' + str(expected_throttled):<20} {'~' + str(expected_throttled):<20}"
        )

        t_dist_ok = (
            expected_success_min <= t_dist.successful_requests <= expected_success_max
        )
        s_dist_ok = (
            expected_success_min <= s_dist.successful_requests <= expected_success_max
        )
        print(
            f"  {'Within Expected Range':<28} {'Yes' if t_dist_ok else 'No':<20} {'Yes' if s_dist_ok else 'No':<20}"
        )

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    if traffik_wins > slowapi_wins:
        winner = "Traffik"
    elif slowapi_wins > traffik_wins:
        winner = "SlowAPI"
    else:
        winner = "Tie"

    print(f"\nPerformance Winner: {winner}")
    print(f"  Traffik wins: {traffik_wins}/{scenarios_compared} scenarios")
    print(f"  SlowAPI wins: {slowapi_wins}/{scenarios_compared} scenarios")
    if ties > 0:
        print(f"  Ties: {ties}/{scenarios_compared} scenarios")
    print("\n" + "=" * 80 + "\n")


async def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Traffik vs SlowAPI rate limiting libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # General options
    parser.add_argument(
        "--scenarios",
        type=str,
        default="low,high,sustained,burst,race,distributed",
        help="Comma-separated list of scenarios to run (default: all)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per scenario (default: 3)",
    )
    parser.add_argument(
        "--libraries",
        type=str,
        default="traffik,slowapi",
        help="Comma-separated list of libraries to test (default: both)",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=2.0,
        help="Cooldown seconds between library tests (default: 2.0)",
    )

    # Traffik options
    parser.add_argument(
        "--traffik-backend",
        type=str,
        choices=["inmemory", "redis", "memcached"],
        default="inmemory",
        help="Backend for Traffik (default: inmemory)",
    )
    parser.add_argument(
        "--traffik-strategy",
        type=str,
        choices=list(TRAFFIK_STRATEGIES.keys()),
        default="fixed-window",
        help="Strategy for Traffik (default: fixed-window)",
    )
    parser.add_argument(
        "--traffik-redis-url",
        type=str,
        default="redis://localhost:6379/0",
        help="Redis URL for Traffik (required if backend=redis)",
    )
    parser.add_argument(
        "--traffik-memcached-url",
        type=str,
        default="memcached://localhost:11211",
        help="Memcached URL for Traffik (required if backend=memcached)",
    )
    parser.add_argument(
        "--traffik-memcached-track-keys",
        action="store_true",
        help="Enable key tracking for Traffik Memcached backend (default: disabled)",
    )

    # SlowAPI options
    parser.add_argument(
        "--slowapi-backend",
        type=str,
        choices=["inmemory", "redis", "memcached"],
        default="inmemory",
        help="Backend for SlowAPI (default: inmemory)",
    )
    parser.add_argument(
        "--slowapi-strategy",
        type=str,
        choices=[
            "fixed-window",
            "sliding-window-counter",
            "fixed-window-elastic-expiry",
            "moving-window",
        ],
        default="fixed-window",
        help="Strategy for SlowAPI (default: fixed-window)",
    )
    parser.add_argument(
        "--slowapi-redis-url",
        type=str,
        default="redis://localhost:6379/0",
        help="Redis URL for SlowAPI (required if backend=redis)",
    )
    parser.add_argument(
        "--slowapi-memcached-url",
        type=str,
        default="memcached://localhost:11211",
        help="Memcached URL for SlowAPI (required if backend=memcached)",
    )

    args = parser.parse_args()
    config = BenchmarkConfig(
        scenarios=args.scenarios.split(","),
        iterations=args.iterations,
        cooldown_seconds=args.cooldown_seconds,
        traffik_backend=args.traffik_backend,
        traffik_strategy=args.traffik_strategy,
        traffik_redis_url=args.traffik_redis_url,
        traffik_memcached_url=args.traffik_memcached_url,
        traffik_memcached_track_keys=args.traffik_memcached_track_keys,
        slowapi_backend=args.slowapi_backend,
        slowapi_strategy=args.slowapi_strategy,
        slowapi_redis_url=args.slowapi_redis_url,
        slowapi_memcached_url=args.slowapi_memcached_url,
    )

    libraries = args.libraries.split(",")

    print("Starting benchmarks...")
    print(f"Scenarios: {', '.join(config.scenarios)}")
    print(f"Iterations per scenario: {config.iterations}")
    print(f"Libraries to test: {', '.join(libraries)}\n")
    print(f"Cooldown seconds between tests: {config.cooldown_seconds}\n")

    results = {}

    if "traffik" in libraries:
        results["Traffik"] = await run_benchmark_suite("Traffik", config)
        await asyncio.sleep(config.cooldown_seconds)

    if "slowapi" in libraries:
        results["SlowAPI"] = await run_benchmark_suite("SlowAPI", config)

    # Print comparison if both were tested
    if "Traffik" in results and "SlowAPI" in results:
        print_comparison(results["Traffik"], results["SlowAPI"])

    print("Benchmark complete!")


if __name__ == "__main__":
    asyncio.run(main())
