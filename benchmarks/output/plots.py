"""
Charts for benchmark results, written to disk when a command is run with `--plot`.

Requires `matplotlib` (part of the `benchmark` extra). Every function returns
the paths it wrote so the CLI can list them.
"""

import pathlib
import typing

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from benchmarks.glossary import KIND_LEGEND, Family, Kind, get_doc_for
from benchmarks.types import (
    AggregatedResult,
    CompareResult,
    ScaleResult,
    SweepResult,
)

ImageFormat = typing.Literal["svg", "png"]

MIN_SAMPLES_FOR_P999 = 1000

KIND_COLORS: dict[Kind, str] = {
    Kind.THROUGHPUT: "#2a78c2",
    Kind.SERIALIZATION: "#e08a1e",
    Kind.REJECTION: "#8c8c8c",
    Kind.LATENCY: "#1f9d8b",
    Kind.PACED: "#8e5bb5",
    Kind.BEHAVIOR: "#a0644a",
}
TRAFFIK_COLOR = "#2a78c2"
SLOWAPI_COLOR = "#d6603a"
NO_GATE_COLOR = "#2f9e5b"
UNKNOWN_COLOR = "#b0b0b0"


def get_slug(text: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in text.lower()
    ).strip("-")


def save_figure(
    figure: Figure, output_dir: pathlib.Path, name: str, fmt: ImageFormat
) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.{fmt}"
    figure.tight_layout()
    figure.savefig(path, format=fmt, dpi=150, bbox_inches="tight")
    plt.close(figure)
    return path


def get_kind_of(
    family: typing.Optional[Family], scenario_name: str
) -> typing.Optional[Kind]:
    if family is None:
        return None
    doc = get_doc_for(family, scenario_name)
    return doc.kind if doc else None


def get_kind_legend(kinds: typing.Iterable[typing.Optional[Kind]]) -> list[Patch]:
    seen = [kind for kind in dict.fromkeys(kinds) if kind is not None]
    return [
        Patch(
            facecolor=KIND_COLORS[kind],
            label=f"{kind.value}: {KIND_LEGEND[kind].split(':')[0]}",
        )
        for kind in seen
    ]


def plot_aggregate(
    results: list[AggregatedResult],
    output_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    family: typing.Optional[Family] = None,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write throughput, latency and outcome charts for one set of results.

    :param results: Aggregated results, one per scenario.
    :param output_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix, e.g. `"http-inmemory-fixed_window"`.
    :param family: Scenario set; enables Kind coloring and the legend.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    if not results:
        return []

    names = [result.scenario_name for result in results]
    kinds = [get_kind_of(family, name) for name in names]
    colors = [
        KIND_COLORS.get(kind, UNKNOWN_COLOR) if kind else UNKNOWN_COLOR
        for kind in kinds
    ]
    ypos = list(range(len(results)))
    height = max(3.0, 0.55 * len(results) + 1.5)
    paths: list[pathlib.Path] = []

    # 1. Throughput, colored by what it measures.
    figure, axes = plt.subplots(figsize=(9, height))
    axes.barh(
        ypos,
        [result.mean_rps for result in results],
        xerr=[result.rps_stddev for result in results],
        color=colors,
        error_kw={"ecolor": "#333333", "capsize": 3, "lw": 1},
    )
    axes.set_yticks(ypos, names)
    axes.invert_yaxis()
    axes.set_xlabel(
        "requests / second (intentional pauses excluded; bar = mean, whisker = stddev)"
    )
    axes.set_title(f"{title}: throughput")
    axes.grid(axis="x", alpha=0.3)
    if legend := get_kind_legend(kinds):
        axes.legend(
            handles=legend,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.12),
            ncol=3,
            fontsize=8,
            title="What req/s measures",
        )
    paths.append(save_figure(figure, output_dir, f"{prefix}-throughput", fmt))

    # 2. Latency percentiles.
    figure, axes = plt.subplots(figsize=(9, height))
    series: list[tuple[str, list[float], str]] = [
        ("P50", [result.p50_ms for result in results], "#9ecae1"),
        ("P95", [result.p95_ms for result in results], "#4292c6"),
        ("P99", [result.p99_ms for result in results], "#08519c"),
    ]
    if any(result.sample_count >= MIN_SAMPLES_FOR_P999 for result in results):
        series.append((
            "P99.9 (1,000+ samples)",
            [
                result.p999_ms if result.sample_count >= MIN_SAMPLES_FOR_P999 else 0.0
                for result in results
            ],
            "#08306b",
        ))
    bar_h = 0.8 / len(series)
    for i, (label, values, color) in enumerate(series):
        offsets = [y - 0.4 + bar_h * (i + 0.5) for y in ypos]
        axes.barh(offsets, values, height=bar_h, label=label, color=color)
    axes.set_yticks(ypos, names)
    axes.invert_yaxis()
    axes.set_xscale("log")
    axes.set_xlabel("latency (ms, log scale)")
    axes.set_title(f"{title}: latency")
    axes.grid(axis="x", alpha=0.3, which="both")
    axes.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)
    paths.append(save_figure(figure, output_dir, f"{prefix}-latency", fmt))

    # 3. What happened to the requests.
    figure, axes = plt.subplots(figsize=(9, height))
    success = [result.success_rate for result in results]
    throttled = [result.throttle_rate for result in results]
    errors = [result.error_rate for result in results]
    axes.barh(ypos, success, color="#4daf4a", label="allowed (200)")
    axes.barh(ypos, throttled, left=success, color="#e0a030", label="throttled (429)")
    axes.barh(
        ypos,
        errors,
        left=[s + t for s, t in zip(success, throttled)],
        color="#d62728",
        label="errors",
    )
    axes.set_yticks(ypos, names)
    axes.invert_yaxis()
    axes.set_xlim(0, 100)
    axes.set_xlabel("% of requests")
    axes.set_title(f"{title}: outcomes")
    axes.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)
    paths.append(save_figure(figure, output_dir, f"{prefix}-outcomes", fmt))

    return paths


def plot_compare(
    results: list[CompareResult],
    output_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    family: typing.Optional[Family] = None,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write traffik-vs-SlowAPI charts: throughput, tail latency and the trade-off.

    The trade-off chart plots each scenario's req/s change against its P99
    improvement (both traffik relative to SlowAPI). The upper-left quadrant
    is "less throughput, better tail".

    :param results: Paired results, one per scenario.
    :param output_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix.
    :param family: `"http"` or `"middleware"`; enables Kind coloring.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    if not results:
        return []

    names = [result.scenario_name for result in results]
    kinds = [get_kind_of(family, result.traffik.scenario_name) for result in results]
    ypos = list(range(len(results)))
    height = max(3.0, 0.7 * len(results) + 1.5)
    paths: list[pathlib.Path] = []

    def grouped(
        axes: Axes,
        traffik: list[float],
        slowapi: list[float],
        xlabel: str,
    ) -> None:
        axes.barh(
            [y - 0.2 for y in ypos],
            traffik,
            height=0.38,
            color=TRAFFIK_COLOR,
            label="traffik",
        )
        axes.barh(
            [y + 0.2 for y in ypos],
            slowapi,
            height=0.38,
            color=SLOWAPI_COLOR,
            label="SlowAPI",
        )
        axes.set_yticks(ypos, names)
        axes.invert_yaxis()
        axes.set_xlabel(xlabel)
        axes.grid(axis="x", alpha=0.3)
        axes.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)

    # 1. Throughput.
    figure, axes = plt.subplots(figsize=(9, height))
    grouped(
        axes,
        [result.traffik.mean_rps for result in results],
        [result.slowapi.mean_rps for result in results],
        "requests / second (higher is better on T scenarios; see Type)",
    )
    axes.set_title(f"{title}: throughput")
    paths.append(save_figure(figure, output_dir, f"{prefix}-throughput", fmt))

    # 2. Tail latency, P50 next to P99.
    figure, (ax50, ax99) = plt.subplots(1, 2, figsize=(13, height), sharey=True)
    grouped(
        ax50,
        [result.traffik.p50_ms for result in results],
        [result.slowapi.p50_ms for result in results],
        "P50 latency (ms)",
    )
    grouped(
        ax99,
        [result.traffik.p99_ms for result in results],
        [result.slowapi.p99_ms for result in results],
        "P99 latency (ms, lower is better)",
    )
    ax99.set_yticklabels([])
    figure.suptitle(f"{title}: latency")
    paths.append(save_figure(figure, output_dir, f"{prefix}-latency", fmt))

    # 3. The trade-off.
    points = [
        (
            (result.traffik.mean_rps - result.slowapi.mean_rps)
            / result.slowapi.mean_rps
            * 100,
            (result.slowapi.p99_ms - result.traffik.p99_ms)
            / result.slowapi.p99_ms
            * 100,
            result.scenario_name,
            kind,
        )
        for result, kind in zip(results, kinds)
        if result.slowapi.mean_rps > 0 and result.slowapi.p99_ms > 0
    ]
    if points:
        figure, axes = plt.subplots(figsize=(8, 7))
        axes.axhline(0, color="#555555", lw=1)
        axes.axvline(0, color="#555555", lw=1)
        for dx, dy, name, kind in points:
            axes.scatter(
                dx,
                dy,
                s=70,
                color=KIND_COLORS.get(kind, UNKNOWN_COLOR) if kind else UNKNOWN_COLOR,
                zorder=3,
            )
            axes.annotate(
                name, (dx, dy), textcoords="offset points", xytext=(6, 5), fontsize=7
            )
        axes.set_xlabel("req/s: traffik vs SlowAPI (%)   <- slower | faster ->")
        axes.set_ylabel("P99 improvement over SlowAPI (%, up = better tail)")
        axes.set_title(f"{title}: throughput vs tail trade-off")
        axes.grid(alpha=0.3)
        low, high = axes.get_xlim(), axes.get_ylim()
        axes.text(
            low[0],
            high[1],
            " less throughput, better tail",
            fontsize=8,
            va="top",
            color="#2a7a2a",
        )
        axes.text(
            low[1],
            high[1],
            "more throughput, better tail ",
            fontsize=8,
            va="top",
            ha="right",
            color="#2a7a2a",
        )
        axes.text(
            low[0],
            high[0],
            " less throughput, worse tail",
            fontsize=8,
            va="bottom",
            color="#a02020",
        )
        axes.text(
            low[1],
            high[0],
            "more throughput, worse tail ",
            fontsize=8,
            va="bottom",
            ha="right",
            color="#a02020",
        )
        if legend := get_kind_legend(kind for *_, kind in points):
            axes.legend(
                handles=legend, loc="center left", fontsize=8, title="Scenario type"
            )
        paths.append(save_figure(figure, output_dir, f"{prefix}-tradeoff", fmt))

    return paths


def plot_scale(
    result: ScaleResult,
    output_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write memory and latency charts for a `scale` run.

    :param result: The scale run result.
    :param output_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    checkpoints = [
        checkpoint
        for checkpoint in result.checkpoints
        if checkpoint.cumulative_keys > 0
    ]
    if not checkpoints:
        return []

    keys = [checkpoint.cumulative_keys for checkpoint in checkpoints]
    paths: list[pathlib.Path] = []

    figure, axes = plt.subplots(figsize=(8, 5))
    axes.plot(
        keys,
        [checkpoint.rss_delta_mb for checkpoint in checkpoints],
        marker="o",
        label="server RSS growth (MiB)",
    )
    backend_used_memory = [
        checkpoint.backend_used_memory_mb for checkpoint in checkpoints
    ]
    if all(value is not None for value in backend_used_memory):
        axes.plot(
            keys, backend_used_memory, marker="s", label="backend used_memory (MiB)"
        )

    axes.set_xscale("log")
    axes.set_xlabel("distinct keys (log scale)")
    axes.set_ylabel("MiB")
    axes.set_title(f"{title}: memory")
    axes.grid(alpha=0.3, which="both")
    axes.legend(fontsize=8)
    paths.append(save_figure(figure, output_dir, f"{prefix}-memory", fmt))

    figure, axes = plt.subplots(figsize=(8, 5))
    axes.plot(
        keys,
        [checkpoint.bytes_per_key for checkpoint in checkpoints],
        marker="o",
        color="#8e5bb5",
    )
    axes.set_xscale("log")
    axes.set_xlabel("distinct keys (log scale)")
    axes.set_ylabel("bytes per key (cumulative RSS growth / keys)")
    axes.set_title(f"{title}: per-key memory cost")
    axes.grid(alpha=0.3, which="both")
    paths.append(save_figure(figure, output_dir, f"{prefix}-bytes-per-key", fmt))

    figure, axes = plt.subplots(figsize=(8, 5))
    axes.plot(
        keys, [checkpoint.p50_ms for checkpoint in checkpoints], marker="o", label="P50"
    )
    axes.plot(
        keys, [checkpoint.p99_ms for checkpoint in checkpoints], marker="o", label="P99"
    )
    axes.set_xscale("log")
    axes.set_xlabel("distinct keys (log scale)")
    axes.set_ylabel("latency of the batch that grew the backend (ms)")
    axes.set_title(f"{title}: latency as the backend fills")
    axes.grid(alpha=0.3, which="both")
    axes.legend(fontsize=8)
    paths.append(save_figure(figure, output_dir, f"{prefix}-latency", fmt))
    return paths


def file_prefix(*parts: str) -> str:
    """
    Build a file name prefix from run parameters, e.g. `("http", "redis", "gcra")`.

    :param parts: Pieces to join; non-alphanumerics become `-`.
    :return: A filesystem-safe prefix.
    """
    return "-".join(get_slug(part) for part in parts if part)


SERIES_STYLE: dict[str, tuple[str, str]] = {
    "traffik": (TRAFFIK_COLOR, "o"),
    "SlowAPI": (SLOWAPI_COLOR, "s"),
    "traffik (no gate)": (NO_GATE_COLOR, "^"),
}
DISTRIBUTION_TITLES = {"hot": "one hot key", "many": "many keys"}


def plot_sweep(
    result: SweepResult,
    output_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write the load-sweep charts: throughput, P99 and P99.9 against concurrency
    (one panel per key distribution), and the hot-key cost.

    Look for where each throughput curve flattens and where each tail curve
    starts to climb: that is the saturation point, and it matters more than
    the peak.

    :param result: The sweep to draw.
    :param output_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    if not result.points:
        return []

    distributions = list(dict.fromkeys(point.distribution for point in result.points))
    series_names = list(dict.fromkeys(point.series for point in result.points))
    levels = sorted({point.concurrency for point in result.points})
    paths: list[pathlib.Path] = []

    def get_points_for(distribution: str, series: str) -> list:
        return sorted(
            (
                point
                for point in result.points
                if point.distribution == distribution and point.series == series
            ),
            key=lambda point: point.concurrency,
        )

    def style(series: str) -> tuple[str, str]:
        return SERIES_STYLE.get(series, (UNKNOWN_COLOR, "d"))

    def panels(
        metric: typing.Callable[[AggregatedResult], typing.Optional[float]],
        ylabel: str,
        suffix: str,
        heading: str,
        *,
        log_y: bool = False,
        error: typing.Optional[typing.Callable[[AggregatedResult], float]] = None,
    ) -> None:
        figure, axes = plt.subplots(
            1,
            len(distributions),
            figsize=(6.2 * len(distributions), 4.8),
            sharey=True,
            squeeze=False,
        )
        for axis, distribution in zip(axes[0], distributions):
            for series in series_names:
                pts = [
                    point
                    for point in get_points_for(distribution, series)
                    if metric(point.result) is not None
                ]
                if not pts:
                    continue
                color, marker = style(series)
                xs = [point.concurrency for point in pts]
                ys = [metric(point.result) for point in pts]
                if error is not None:
                    axis.errorbar(
                        xs,
                        ys,
                        yerr=[error(point.result) for point in pts],
                        color=color,
                        marker=marker,
                        capsize=3,
                        label=series,
                    )
                else:
                    axis.plot(xs, ys, color=color, marker=marker, label=series)
            axis.set_xscale("log")
            axis.set_xticks(levels, [str(level) for level in levels])
            axis.minorticks_off()
            if log_y:
                axis.set_yscale("log")
            axis.set_xlabel("requests in flight (log scale)")
            axis.set_title(DISTRIBUTION_TITLES.get(distribution, distribution))
            axis.grid(alpha=0.3, which="both")

        axes[0][0].set_ylabel(ylabel)
        axes[0][0].legend(fontsize=8)
        figure.suptitle(f"{title}: {heading}")
        paths.append(save_figure(figure, output_dir, f"{prefix}-{suffix}", fmt))

    panels(
        lambda result: result.mean_rps,
        "requests / second",
        "throughput",
        "throughput vs load",
        error=lambda result: result.rps_stddev,
    )
    panels(
        lambda result: result.p99_ms,
        "P99 latency (ms, log)",
        "p99",
        "P99 vs load",
        log_y=True,
    )
    panels(
        lambda result: (
            result.p999_ms if result.sample_count >= MIN_SAMPLES_FOR_P999 else None
        ),
        "P99.9 latency (ms, log)",
        "p999",
        "P99.9 vs load (1,000+ samples)",
        log_y=True,
    )

    if {"hot", "many"} <= set(distributions):
        figure, axes = plt.subplots(figsize=(7, 4.8))
        for series in series_names:
            hot = {
                point.concurrency: point.result.mean_rps
                for point in get_points_for("hot", series)
            }
            many = {
                point.concurrency: point.result.mean_rps
                for point in get_points_for("many", series)
            }
            xs = [c for c in levels if c in hot and c in many and many[c] > 0]
            if not xs:
                continue
            color, marker = style(series)
            axes.plot(
                xs,
                [hot[c] / many[c] for c in xs],
                color=color,
                marker=marker,
                label=series,
            )
        axes.axhline(1.0, color="#555555", lw=1)
        axes.set_xscale("log")
        axes.set_xticks(levels, [str(level) for level in levels])
        axes.minorticks_off()
        axes.set_xlabel("requests in flight (log scale)")
        axes.set_ylabel(
            "hot-key req/s / many-key req/s   (1.0 = key sharing costs nothing)"
        )
        axes.set_title(f"{title}: what sharing one key costs")
        axes.grid(alpha=0.3)
        axes.legend(fontsize=8)
        paths.append(save_figure(figure, output_dir, f"{prefix}-hot-key-cost", fmt))

    return paths
