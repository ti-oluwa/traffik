import statistics
import sys
import typing
from dataclasses import dataclass
from enum import Enum, auto


class BackendKind(Enum):
    """Supported backend variants."""

    INMEMORY = auto()
    MULTIPROCESS = auto()
    REDIS_AIOREDIS = auto()
    REDIS_COREDIS = auto()
    MEMCACHED_AIOMCACHE = auto()
    MEMCACHED_EMCACHE = auto()

    @classmethod
    def choices(cls) -> typing.List[str]:
        """
        Return lowercase names of all available backends on this platform.

        :return: List of available backend choice strings.
        """
        choices = [
            "inmemory",
            "multiprocess",
            "redis_aioredis",
            "redis_coredis",
            "memcached_aiomcache",
        ]
        if sys.platform != "win32":
            choices.append("memcached_emcache")
        return choices


class StrategyKind(Enum):
    """Supported throttling strategies."""

    FIXED_WINDOW = auto()
    SLIDING_WINDOW_COUNTER = auto()
    SLIDING_WINDOW_LOG = auto()
    TOKEN_BUCKET = auto()
    TOKEN_BUCKET_DEBT = auto()
    LEAKY_BUCKET = auto()
    LEAKY_BUCKET_QUEUE = auto()
    GCRA = auto()

    @classmethod
    def choices(cls) -> typing.List[str]:
        """
        Return lowercase names of all strategy members.

        :return: List of available strategy choice strings.
        """
        return [
            "fixed_window",
            "sliding_window_counter",
            "sliding_window_log",
            "token_bucket",
            "token_bucket_debt",
            "leaky_bucket",
            "leaky_bucket_queue",
            "gcra",
        ]


class OutputFormat(Enum):
    """Output format options."""

    TABLE = auto()
    JSON = auto()


@dataclass
class ScenarioResult:
    """
    Results from a single scenario run.

    :param scenario_name: Human-readable scenario name.
    :param backend_kind: Which backend was used.
    :param strategy_kind: Which strategy was used.
    :param total_requests: Total requests (or messages for WebSocket) attempted.
    :param successful_requests: Requests that received HTTP 200 / WS ok response.
    :param throttled_requests: Requests that received HTTP 429 / WS rate_limit response.
    :param error_requests: Requests that received any other status code or raised an exception.
    :param total_time_seconds: Wall-clock seconds for the entire scenario run.
    :param latencies_seconds: Per-request latency in seconds, same length as total_requests.
    :param iteration: Which iteration number this result belongs to (1-based).
    """

    scenario_name: str
    backend_kind: str
    strategy_kind: str
    total_requests: int
    successful_requests: int
    throttled_requests: int
    error_requests: int
    total_time_seconds: float
    latencies_seconds: typing.List[float]
    iteration: int

    @property
    def requests_per_second(self) -> float:
        """
        Requests per second throughput.

        :return: Requests per second or 0.0 if total_time_seconds is zero.
        """
        if self.total_time_seconds == 0:
            return 0.0
        return self.total_requests / self.total_time_seconds

    @property
    def success_rate(self) -> float:
        """
        Percentage of successful requests.

        :return: Success rate as percentage or 0.0.
        """
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests * 100

    @property
    def throttle_rate(self) -> float:
        """
        Percentage of throttled requests.

        :return: Throttle rate as percentage or 0.0.
        """
        if self.total_requests == 0:
            return 0.0
        return self.throttled_requests / self.total_requests * 100

    @property
    def error_rate(self) -> float:
        """
        Percentage of error requests.

        :return: Error rate as percentage or 0.0.
        """
        if self.total_requests == 0:
            return 0.0
        return self.error_requests / self.total_requests * 100

    @property
    def p50_ms(self) -> float:
        """
        50th percentile (median) latency in milliseconds.

        :return: P50 latency in ms or 0.0 if empty.
        """
        if not self.latencies_seconds:
            return 0.0
        sorted_latencies = sorted(self.latencies_seconds)
        median = statistics.median(sorted_latencies)
        return median * 1000

    @property
    def p95_ms(self) -> float:
        """
        95th percentile latency in milliseconds.

        :return: P95 latency in ms or 0.0 if empty.
        """
        if not self.latencies_seconds:
            return 0.0
        sorted_latencies = sorted(self.latencies_seconds)
        index = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[index] * 1000

    @property
    def p99_ms(self) -> float:
        """
        99th percentile latency in milliseconds.

        :return: P99 latency in ms or 0.0 if empty.
        """
        if not self.latencies_seconds:
            return 0.0
        sorted_latencies = sorted(self.latencies_seconds)
        index = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[index] * 1000

    @property
    def mean_ms(self) -> float:
        """
        Mean latency in milliseconds.

        :return: Mean latency in ms or 0.0 if empty.
        """
        if not self.latencies_seconds:
            return 0.0
        mean = statistics.mean(self.latencies_seconds)
        return mean * 1000

    @property
    def stddev_ms(self) -> float:
        """
        Standard deviation of latency in milliseconds.

        :return: Stddev in ms or 0.0 if fewer than 2 samples.
        """
        if len(self.latencies_seconds) < 2:
            return 0.0
        stddev = statistics.stdev(self.latencies_seconds)
        return stddev * 1000


@dataclass
class AggregatedResult:
    """
    Aggregated statistics across multiple iterations of the same scenario.

    :param scenario_name: Human-readable scenario name.
    :param backend_kind: Which backend was used.
    :param strategy_kind: Which strategy was used.
    :param iterations: Number of iterations aggregated.
    :param results: Individual per-iteration results.
    """

    scenario_name: str
    backend_kind: str
    strategy_kind: str
    iterations: int
    results: typing.List[ScenarioResult]

    @property
    def total_requests(self) -> int:
        """
        Total requests across all iterations.

        :return: Sum of requests across all results.
        """
        return sum(r.total_requests for r in self.results)

    @property
    def mean_rps(self) -> float:
        """
        Mean requests per second across all iterations.

        :return: Mean RPS or 0.0.
        """
        if not self.results:
            return 0.0
        return statistics.mean(r.requests_per_second for r in self.results)

    @property
    def p50_ms(self) -> float:
        """
        50th percentile latency in milliseconds across all combined latencies.

        :return: P50 in ms or 0.0 if no latencies.
        """
        all_latencies = []
        for r in self.results:
            all_latencies.extend(r.latencies_seconds)
        if not all_latencies:
            return 0.0
        return statistics.median(all_latencies) * 1000

    @property
    def p95_ms(self) -> float:
        """
        95th percentile latency in milliseconds across all combined latencies.

        :return: P95 in ms or 0.0 if no latencies.
        """
        all_latencies = []
        for r in self.results:
            all_latencies.extend(r.latencies_seconds)
        if not all_latencies:
            return 0.0
        sorted_latencies = sorted(all_latencies)
        index = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[index] * 1000

    @property
    def p99_ms(self) -> float:
        """
        99th percentile latency in milliseconds across all combined latencies.

        :return: P99 in ms or 0.0 if no latencies.
        """
        all_latencies = []
        for r in self.results:
            all_latencies.extend(r.latencies_seconds)
        if not all_latencies:
            return 0.0
        sorted_latencies = sorted(all_latencies)
        index = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[index] * 1000

    @property
    def mean_ms(self) -> float:
        """
        Mean latency in milliseconds across all combined latencies.

        :return: Mean in ms or 0.0 if no latencies.
        """
        all_latencies = []
        for r in self.results:
            all_latencies.extend(r.latencies_seconds)
        if not all_latencies:
            return 0.0
        return statistics.mean(all_latencies) * 1000

    @property
    def success_rate(self) -> float:
        """
        Percentage of successful requests across all iterations.

        :return: Success rate as percentage or 0.0.
        """
        total_successful = sum(r.successful_requests for r in self.results)
        total = self.total_requests
        if total == 0:
            return 0.0
        return total_successful / total * 100

    @property
    def throttle_rate(self) -> float:
        """
        Percentage of throttled requests across all iterations.

        :return: Throttle rate as percentage or 0.0.
        """
        total_throttled = sum(r.throttled_requests for r in self.results)
        total = self.total_requests
        if total == 0:
            return 0.0
        return total_throttled / total * 100

    @property
    def error_rate(self) -> float:
        """
        Percentage of error requests across all iterations.

        :return: Error rate as percentage or 0.0.
        """
        total_errors = sum(r.error_requests for r in self.results)
        total = self.total_requests
        if total == 0:
            return 0.0
        return total_errors / total * 100

    @property
    def rps_stddev(self) -> float:
        """
        Standard deviation of requests per second across iterations.

        :return: Stddev of RPS or 0.0 if fewer than 2 iterations.
        """
        if len(self.results) < 2:
            return 0.0
        rps_values = [r.requests_per_second for r in self.results]
        return statistics.stdev(rps_values)


@dataclass
class BenchmarkConfig:
    """
    Global configuration for a benchmark run.

    :param backend_kind: Which backend variant to benchmark.
    :param strategy_kind: Which throttling strategy to use.
    :param iterations: Number of timed iterations per scenario (warmup not counted).
    :param warmup_iterations: Number of warmup iterations to run and discard before timing.
    :param concurrency: Number of concurrent requests per batch in concurrent scenarios.
    :param output_format: How to display results.
    :param redis_url: Redis connection URL for redis-backed backends.
    :param memcached_host: Memcached host for memcached-backed backends.
    :param memcached_port: Memcached port for memcached-backed backends.
    :param multiprocess_shards: Number of shards for MultiProcessInMemoryBackend.
    :param multiprocess_max_keys: Maximum keys for MultiProcessInMemoryBackend.
    """

    backend_kind: str = "inmemory"
    strategy_kind: str = "fixed_window"
    iterations: int = 3
    warmup_iterations: int = 1
    concurrency: int = 50
    output_format: str = "table"
    redis_url: str = "redis://localhost:6379/0"
    memcached_host: str = "localhost"
    memcached_port: int = 11211
    multiprocess_shards: int = 32
    multiprocess_max_keys: int = 65536
