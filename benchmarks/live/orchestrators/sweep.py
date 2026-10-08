"""
Runs the `sweep` command: the same workload at increasing concurrency, against
traffik and (when comparable) SlowAPI, for a hot key and for many keys.

A single fixed-concurrency run answers "how fast at this load?". It cannot say
where an implementation stops scaling, or whether a lower peak buys a better
tail. A sweep can: plot throughput and tail latency against concurrency and
look for the point where throughput stops rising and latency starts to climb.

The rate limit is set far above the offered load so every request is allowed.
That keeps rejection out of the numbers: what is left is the cost of the
throttle's own synchronization, which is what the hot-key and many-key
distributions are there to separate.

The load is a closed loop: `concurrency` requests are kept in flight, and each
worker sends its next request as soon as its previous response arrives. It finds the saturation point, but
it hides how a system behaves under an arrival rate it cannot keep up with
(requests queue up on the client instead). Treat the tail it reports as a lower
bound on what an open-loop load would see.
"""

import dataclasses
import sys
import typing

from benchmarks.live.orchestrators.compare import (
    SLOWAPI_APP_PATHS,
    SUPPORTED_STRATEGIES,
    TRAFFIK_APP_PATHS,
    UNSUPPORTED_COMPARE_BACKENDS,
    run_one_side,
)
from benchmarks.live.orchestrators.core import (
    build_environment_variables,
    warn_unshared_state,
)
from benchmarks.scenarios import HOT_KEY_HEADERS, HttpScenario
from benchmarks.types import (
    AggregatedResult,
    BenchmarkConfig,
    SweepPoint,
    SweepResult,
)

NO_GATE_THRESHOLD = 1_000_000
"""A `lock_contention_threshold` so high the contention gate never engages."""

DEFAULT_LEVELS = (10, 50, 100, 200, 400)
DEFAULT_RATE = "1000000/60s"
DEFAULT_REQUESTS = 3000
DEFAULT_MANY_KEYS = 1000

Distribution = typing.Literal["hot", "many"]


def sweep_scenario(
    distribution: Distribution,
    concurrency: int,
    *,
    rate: str,
    requests: int,
    keys: int,
) -> HttpScenario:
    """
    Build the scenario for one point of the sweep.

    :param distribution: `"hot"` sends every request under one identity;
        `"many"` cycles through `keys` identities, so no two requests in
        flight share a key as long as `keys >= concurrency`.
    :param concurrency: Requests in flight.
    :param rate: Rate limit, far above the offered load.
    :param requests: Requests per iteration.
    :param keys: Identities to cycle through for `"many"`.
    :return: The scenario.
    """
    if distribution == "hot":
        return HttpScenario(
            key=f"sweep_hot_{concurrency}",
            name=f"Hot Key, {concurrency} in flight",
            rate=rate,
            total_requests=requests,
            mode="concurrent",
            headers=HOT_KEY_HEADERS,
        )
    return HttpScenario(
        key=f"sweep_many_{concurrency}",
        name=f"Many Keys, {concurrency} in flight",
        rate=rate,
        total_requests=requests,
        mode="unique_keys_concurrent",
        key_header="X-Client-ID",
        key_mod=max(keys, concurrency),
    )


async def run_sweep(
    config: BenchmarkConfig,
    *,
    levels: typing.Sequence[int] = DEFAULT_LEVELS,
    distributions: typing.Sequence[Distribution] = ("hot", "many"),
    rate: str = DEFAULT_RATE,
    requests: int = DEFAULT_REQUESTS,
    keys: int = DEFAULT_MANY_KEYS,
    warmup_iterations: int = 1,
    include_slowapi: bool = True,
    diagnose_gate: bool = False,
    mode: typing.Literal["http", "middleware"] = "http",
) -> SweepResult:
    """
    Run the sweep.

    :param config: Global configuration. `concurrency` is overridden per point.
    :param levels: Concurrency levels to measure, in order.
    :param distributions: Key distributions to measure.
    :param rate: Rate limit. Keep it far above `requests` so nothing is rejected.
    :param requests: Requests per point per iteration. At least 1,000 over all
        iterations is needed for P99.9 to mean anything.
    :param keys: Identities for the `"many"` distribution.
    :param warmup_iterations: Warmup runs to discard per point.
    :param include_slowapi: Also run SlowAPI at every point. Skipped, with a
        notice, when the backend or strategy has no SlowAPI equivalent.
    :param diagnose_gate: Also run traffik with the process-local contention gate
        disabled, to show what the gate trades for its tail behavior. Only the
        networked backends have a gate.
    :param mode: `"http"` (per-route dependency) or `"middleware"`.
    :return: Every measured point.
    """
    warn_unshared_state(config)
    series: list[tuple[str, str, BenchmarkConfig]] = [
        ("traffik", TRAFFIK_APP_PATHS[mode], config)
    ]

    if include_slowapi:
        if config.backend_kind in UNSUPPORTED_COMPARE_BACKENDS:
            print(
                f"NOTE: SlowAPI skipped: no comparable --backend {config.backend_kind}.",
                file=sys.stderr,
            )
        elif config.strategy_kind not in SUPPORTED_STRATEGIES:
            print(
                f"NOTE: SlowAPI skipped: it has no comparable --strategy "
                f"{config.strategy_kind} (supported: {sorted(SUPPORTED_STRATEGIES)}).",
                file=sys.stderr,
            )
        else:
            series.append(("SlowAPI", SLOWAPI_APP_PATHS[mode], config))

    if diagnose_gate:
        if config.backend_kind in {"inmemory", "multiprocess"}:
            print(
                f"NOTE: --diagnose-gate skipped: --backend {config.backend_kind} "
                "has no contention gate.",
                file=sys.stderr,
            )
        else:
            series.append(
                (
                    "traffik (no gate)",
                    TRAFFIK_APP_PATHS[mode],
                    dataclasses.replace(
                        config, lock_contention_threshold=NO_GATE_THRESHOLD
                    ),
                )
            )

    points: list[SweepPoint] = []
    point_index = 0
    for distribution in distributions:
        for concurrency in levels:
            scenario = sweep_scenario(
                distribution, concurrency, rate=rate, requests=requests, keys=keys
            )
            # Rotate which series runs first from point to point, so no series
            # owns the first slot (cooler CPU, boost clocks) at every level.
            shift = point_index % len(series)
            point_index += 1
            for label, app_path, series_config in series[shift:] + series[:shift]:
                point_config = dataclasses.replace(
                    series_config, concurrency=concurrency
                )
                env = build_environment_variables(
                    point_config,
                    rate=scenario.rate,
                    uid=f"sweep_{distribution}_{concurrency}",
                    on_error=scenario.on_error,
                )
                print(
                    f"Sweep: {label}, {distribution} key(s), "
                    f"{concurrency} in flight...",
                    file=sys.stderr,
                )
                results = await run_one_side(
                    app_path,
                    env,
                    scenario,
                    point_config,
                    "/test",
                    warmup_iterations,
                    label,
                )
                if not results:
                    print(
                        f"WARN: no results for {label}, {distribution}, "
                        f"{concurrency}; skipping the point.",
                        file=sys.stderr,
                    )
                    continue
                points.append(
                    SweepPoint(
                        series=label,
                        distribution=distribution,
                        concurrency=concurrency,
                        result=AggregatedResult(
                            scenario_name=scenario.name,
                            backend_kind=config.backend_kind,
                            strategy_kind=config.strategy_kind,
                            iterations=len(results),
                            results=results,
                        ),
                    )
                )

    # Run order rotates (see above); report in a stable order.
    series_order = {label: i for i, (label, _, _) in enumerate(series)}
    points.sort(
        key=lambda point: (
            list(distributions).index(point.distribution),
            point.concurrency,
            series_order[point.series],
        )
    )

    return SweepResult(
        backend_kind=config.backend_kind,
        strategy_kind=config.strategy_kind,
        workers=config.workers,
        rate=rate,
        requests_per_iteration=requests,
        points=points,
    )
