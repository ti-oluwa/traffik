import platform
import statistics
import typing
from dataclasses import dataclass
from enum import Enum, auto


class BackendKind(Enum):
    """Supported backend variants."""

    INMEMORY = auto()
    MULTIPROCESS = auto()
    AIOREDIS = auto()
    COREDIS = auto()
    AIOMCACHE = auto()
    EMCACHE = auto()

    @classmethod
    def choices(cls) -> list[str]:
        """
        Return lowercase names of all available backends on this platform.

        :return: List of available backend choice strings.
        """
        choices = [
            "inmemory",
            "aioredis",
            "coredis",
            "aiomcache",
        ]
        if platform.system() != "Windows":
            choices.append("emcache")
            choices.append("multiprocess")
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
    def choices(cls) -> list[str]:
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


@dataclass(slots=True)
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
    latencies_seconds: list[float]
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


@dataclass(slots=True)
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
    results: list[ScenarioResult]

    @property
    def total_requests(self) -> int:
        """
        Total requests across all iterations.

        :return: Sum of requests across all results.
        """
        return sum(result.total_requests for result in self.results)

    @property
    def mean_rps(self) -> float:
        """
        Mean requests per second across all iterations.

        :return: Mean RPS or 0.0.
        """
        if not self.results:
            return 0.0
        return statistics.mean(result.requests_per_second for result in self.results)

    @property
    def p50_ms(self) -> float:
        """
        50th percentile latency in milliseconds across all combined latencies.

        :return: P50 in ms or 0.0 if no latencies.
        """
        all_latencies = []
        for result in self.results:
            all_latencies.extend(result.latencies_seconds)
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
        for result in self.results:
            all_latencies.extend(result.latencies_seconds)
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
        for result in self.results:
            all_latencies.extend(result.latencies_seconds)
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
        for result in self.results:
            all_latencies.extend(result.latencies_seconds)
        if not all_latencies:
            return 0.0
        return statistics.mean(all_latencies) * 1000

    @property
    def success_rate(self) -> float:
        """
        Percentage of successful requests across all iterations.

        :return: Success rate as percentage or 0.0.
        """
        total_successful = sum(result.successful_requests for result in self.results)
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
        total_throttled = sum(result.throttled_requests for result in self.results)
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
        total_errors = sum(result.error_requests for result in self.results)
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
        rps_values = [result.requests_per_second for result in self.results]
        return statistics.stdev(rps_values)


@dataclass(slots=True)
class CompareResult:
    """
    Paired traffik/SlowAPI results for the same scenario, run against
    identical rate, backend, worker count, and traffic pattern.

    :param scenario_key: Short scenario name (matches `HTTP_SCENARIOS` keys).
    :param scenario_name: Human-readable scenario name.
    :param traffik: Aggregated result from the traffik-backed app.
    :param slowapi: Aggregated result from the SlowAPI-backed app, run
        with the same rate (converted via `benchmarks.rates`), backend,
        identity rule, worker count, and endpoint variant.
    """

    scenario_key: str
    scenario_name: str
    traffik: AggregatedResult
    slowapi: AggregatedResult


@dataclass(slots=True)
class ScaleCheckpoint:
    """
    One measurement point in a `scale` run. The state of the world after
    growing the backend to `cumulative_keys` distinct keys.

    :param cumulative_keys: Total distinct keys created by this point.
    :param new_keys_this_checkpoint: Keys created since the previous checkpoint.
    :param rss_mb: Target server process RSS, in MiB, sampled right after
        this checkpoint's traffic finished.
    :param rss_delta_mb: `rss_mb` minus the very first (baseline, zero-key)
        sample.
    :param bytes_per_key: `rss_delta_mb` (in bytes) divided by
        `cumulative_keys`, i.e. incremental memory cost per stored key so
        far. `0.0` at the baseline checkpoint (no keys yet).
    :param backend_used_memory_mb: The backend's own self-reported memory
        usage in MiB, where available (currently: Redis `used_memory` via
        `INFO memory`). `None` when not applicable or not reachable.
    :param mean_rps: Mean requests/sec for the batch of new-key requests
        that grew the backend to this checkpoint.
    :param p50_ms: Median latency for that batch, in ms.
    :param p99_ms: P99 latency for that batch, in ms.
    :param successful: Successful (200) requests in that batch.
    :param errors: Non-200, non-429 requests in that batch.
    """

    cumulative_keys: int
    new_keys_this_checkpoint: int
    rss_mb: float
    rss_delta_mb: float
    bytes_per_key: float
    backend_used_memory_mb: typing.Optional[float]
    mean_rps: float
    p50_ms: float
    p99_ms: float
    successful: int
    errors: int


@dataclass(slots=True)
class ScaleResult:
    """
    Full output of a `scale` run. Contains memory and latency measured as the
    backend's key count grows, against one continuously-running server.

    :param backend_kind: Which backend was used.
    :param strategy_kind: Which strategy was used.
    :param workers: Worker count the target server was run with.
    :param checkpoints: One entry per configured checkpoint, in ascending
        order of `cumulative_keys`. The first entry is the zero-key
        baseline (`cumulative_keys == 0`).
    """

    backend_kind: str
    strategy_kind: str
    workers: int
    checkpoints: list[ScaleCheckpoint]


@dataclass(slots=True)
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
    :param workers: Number of real worker processes to serve the benchmark
        target app. `1` spawns a single `uvicorn` process. `>1` spawns
        `gunicorn` with `--preload` and the `fork` start method, actually
        forking that many worker processes rather than simulating them.
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
    shards: int = 32
    multiprocess_max_keys: int = 65536
    workers: int = 1
