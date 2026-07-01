import sys
import typing

from benchmarks.backends import create_backend
from benchmarks.base import AggregatedResult, BenchmarkConfig
from benchmarks.scenarios.middleware import SCENARIOS


async def run_scenarios(
    config: BenchmarkConfig,
    scenario_keys: typing.List[str],
    warmup_iterations: int = 1,
) -> typing.List[AggregatedResult]:
    """
    Run each selected scenario through warmup and timed iterations.

    :param config: Global benchmark configuration.
    :param scenario_keys: List of scenario short names to run.
    :param warmup_iterations: Number of warmup runs to discard.
    :return: List of aggregated results, one per scenario.
    """
    results: typing.List[AggregatedResult] = []

    for scenario_key in scenario_keys:
        if scenario_key not in SCENARIOS:
            print(f"ERROR: Unknown scenario: {scenario_key}", file=sys.stderr)
            continue

        scenario = SCENARIOS[scenario_key]
        backend = create_backend(config)
        await backend.initialize()

        try:
            # Warmup
            print(f"Running warmup for {scenario_key}...", file=sys.stderr)
            for _ in range(warmup_iterations):
                try:
                    await scenario(config, iteration=0, backend=backend)
                except Exception as exc:  # noqa
                    print(
                        f"WARN: Warmup failed for {scenario_key}: {exc}",
                        file=sys.stderr,
                    )

            # Timed iterations
            scenario_results = []
            for i in range(1, config.iterations + 1):
                print(
                    f"Running {scenario_key} (iteration {i}/{config.iterations})...",
                    file=sys.stderr,
                )
                try:
                    result = await scenario(config, iteration=i, backend=backend)
                    scenario_results.append(result)
                except Exception as exc:  # noqa
                    print(
                        f"WARN: Iteration {i} failed for {scenario_key}: {exc}",
                        file=sys.stderr,
                    )
        finally:
            await backend.close()

        if scenario_results:
            aggregated = AggregatedResult(
                scenario_name=scenario_results[0].scenario_name,
                backend_kind=config.backend_kind,
                strategy_kind=config.strategy_kind,
                iterations=len(scenario_results),
                results=scenario_results,
            )
            results.append(aggregated)

    return results
