"""
Charts for benchmark results, written to disk when a command is run with `--plot`.

Requires `matplotlib` (part of the `benchmark` extra). Every function returns
the paths it wrote so the CLI can list them.

Charts are colored by scenario `Kind` wherever scenarios appear, so a
"serialization" scenario is never mistaken for a capacity measurement.
"""

import pathlib
import typing

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from benchmarks.glossary import KIND_LEGEND, Family, Kind, doc_for
from benchmarks.types import (
    AggregatedResult,
    CompareResult,
    ScaleResult,
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
UNKNOWN_COLOR = "#b0b0b0"


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")


def _save(
    fig: "plt.Figure", out_dir: pathlib.Path, name: str, fmt: ImageFormat
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.{fmt}"
    fig.tight_layout()
    fig.savefig(path, format=fmt, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _kind_of(
    family: typing.Optional[Family], scenario_name: str
) -> typing.Optional[Kind]:
    if family is None:
        return None
    doc = doc_for(family, scenario_name)
    return doc.kind if doc else None


def _kind_legend(kinds: typing.Iterable[typing.Optional[Kind]]) -> list[Patch]:
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
    out_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    family: typing.Optional[Family] = None,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write throughput, latency and outcome charts for one set of results.

    :param results: Aggregated results, one per scenario.
    :param out_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix, e.g. `"http-inmemory-fixed_window"`.
    :param family: Scenario set; enables Kind coloring and the legend.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    if not results:
        return []

    names = [r.scenario_name for r in results]
    kinds = [_kind_of(family, name) for name in names]
    colors = [
        KIND_COLORS.get(kind, UNKNOWN_COLOR) if kind else UNKNOWN_COLOR
        for kind in kinds
    ]
    ypos = list(range(len(results)))
    height = max(3.0, 0.55 * len(results) + 1.5)
    paths: list[pathlib.Path] = []

    # 1. Throughput, colored by what it measures.
    fig, ax = plt.subplots(figsize=(9, height))
    ax.barh(
        ypos,
        [r.mean_rps for r in results],
        xerr=[r.rps_stddev for r in results],
        color=colors,
        error_kw={"ecolor": "#333333", "capsize": 3, "lw": 1},
    )
    ax.set_yticks(ypos, names)
    ax.invert_yaxis()
    ax.set_xlabel(
        "requests / second (intentional pauses excluded; bar = mean, whisker = stddev)"
    )
    ax.set_title(f"{title}: throughput")
    ax.grid(axis="x", alpha=0.3)
    if legend := _kind_legend(kinds):
        ax.legend(
            handles=legend,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.12),
            ncol=3,
            fontsize=8,
            title="What req/s measures",
        )
    paths.append(_save(fig, out_dir, f"{prefix}-throughput", fmt))

    # 2. Latency percentiles.
    fig, ax = plt.subplots(figsize=(9, height))
    series: list[tuple[str, list[float], str]] = [
        ("P50", [r.p50_ms for r in results], "#9ecae1"),
        ("P95", [r.p95_ms for r in results], "#4292c6"),
        ("P99", [r.p99_ms for r in results], "#08519c"),
    ]
    if any(r.sample_count >= MIN_SAMPLES_FOR_P999 for r in results):
        series.append(
            (
                "P99.9 (1,000+ samples)",
                [
                    r.p999_ms if r.sample_count >= MIN_SAMPLES_FOR_P999 else 0.0
                    for r in results
                ],
                "#08306b",
            )
        )
    bar_h = 0.8 / len(series)
    for i, (label, values, color) in enumerate(series):
        offsets = [y - 0.4 + bar_h * (i + 0.5) for y in ypos]
        ax.barh(offsets, values, height=bar_h, label=label, color=color)
    ax.set_yticks(ypos, names)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("latency (ms, log scale)")
    ax.set_title(f"{title}: latency")
    ax.grid(axis="x", alpha=0.3, which="both")
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)
    paths.append(_save(fig, out_dir, f"{prefix}-latency", fmt))

    # 3. What happened to the requests.
    fig, ax = plt.subplots(figsize=(9, height))
    success = [r.success_rate for r in results]
    throttled = [r.throttle_rate for r in results]
    errors = [r.error_rate for r in results]
    ax.barh(ypos, success, color="#4daf4a", label="allowed (200)")
    ax.barh(ypos, throttled, left=success, color="#e0a030", label="throttled (429)")
    ax.barh(
        ypos,
        errors,
        left=[s + t for s, t in zip(success, throttled)],
        color="#d62728",
        label="errors",
    )
    ax.set_yticks(ypos, names)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of requests")
    ax.set_title(f"{title}: outcomes")
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)
    paths.append(_save(fig, out_dir, f"{prefix}-outcomes", fmt))

    return paths


def plot_compare(
    results: list[CompareResult],
    out_dir: pathlib.Path,
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
    :param out_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix.
    :param family: `"http"` or `"middleware"`; enables Kind coloring.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    if not results:
        return []

    names = [r.scenario_name for r in results]
    kinds = [_kind_of(family, r.traffik.scenario_name) for r in results]
    ypos = list(range(len(results)))
    height = max(3.0, 0.7 * len(results) + 1.5)
    paths: list[pathlib.Path] = []

    def grouped(
        ax: "plt.Axes",
        traffik: list[float],
        slowapi: list[float],
        xlabel: str,
    ) -> None:
        ax.barh(
            [y - 0.2 for y in ypos],
            traffik,
            height=0.38,
            color=TRAFFIK_COLOR,
            label="traffik",
        )
        ax.barh(
            [y + 0.2 for y in ypos],
            slowapi,
            height=0.38,
            color=SLOWAPI_COLOR,
            label="SlowAPI",
        )
        ax.set_yticks(ypos, names)
        ax.invert_yaxis()
        ax.set_xlabel(xlabel)
        ax.grid(axis="x", alpha=0.3)
        ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4)

    # 1. Throughput.
    fig, ax = plt.subplots(figsize=(9, height))
    grouped(
        ax,
        [r.traffik.mean_rps for r in results],
        [r.slowapi.mean_rps for r in results],
        "requests / second (higher is better on T scenarios; see Type)",
    )
    ax.set_title(f"{title}: throughput")
    paths.append(_save(fig, out_dir, f"{prefix}-throughput", fmt))

    # 2. Tail latency, P50 next to P99.
    fig, (ax50, ax99) = plt.subplots(1, 2, figsize=(13, height), sharey=True)
    grouped(
        ax50,
        [r.traffik.p50_ms for r in results],
        [r.slowapi.p50_ms for r in results],
        "P50 latency (ms)",
    )
    grouped(
        ax99,
        [r.traffik.p99_ms for r in results],
        [r.slowapi.p99_ms for r in results],
        "P99 latency (ms, lower is better)",
    )
    ax99.set_yticklabels([])
    fig.suptitle(f"{title}: latency")
    paths.append(_save(fig, out_dir, f"{prefix}-latency", fmt))

    # 3. The trade-off.
    points = [
        (
            (r.traffik.mean_rps - r.slowapi.mean_rps) / r.slowapi.mean_rps * 100,
            (r.slowapi.p99_ms - r.traffik.p99_ms) / r.slowapi.p99_ms * 100,
            r.scenario_name,
            kind,
        )
        for r, kind in zip(results, kinds)
        if r.slowapi.mean_rps > 0 and r.slowapi.p99_ms > 0
    ]
    if points:
        fig, ax = plt.subplots(figsize=(8, 7))
        ax.axhline(0, color="#555555", lw=1)
        ax.axvline(0, color="#555555", lw=1)
        for dx, dy, name, kind in points:
            ax.scatter(
                dx,
                dy,
                s=70,
                color=KIND_COLORS.get(kind, UNKNOWN_COLOR) if kind else UNKNOWN_COLOR,
                zorder=3,
            )
            ax.annotate(
                name, (dx, dy), textcoords="offset points", xytext=(6, 5), fontsize=7
            )
        ax.set_xlabel("req/s: traffik vs SlowAPI (%)   <- slower | faster ->")
        ax.set_ylabel("P99 improvement over SlowAPI (%, up = better tail)")
        ax.set_title(f"{title}: throughput vs tail trade-off")
        ax.grid(alpha=0.3)
        low, high = ax.get_xlim(), ax.get_ylim()
        ax.text(
            low[0],
            high[1],
            " less throughput, better tail",
            fontsize=8,
            va="top",
            color="#2a7a2a",
        )
        ax.text(
            low[1],
            high[1],
            "more throughput, better tail ",
            fontsize=8,
            va="top",
            ha="right",
            color="#2a7a2a",
        )
        ax.text(
            low[0],
            high[0],
            " less throughput, worse tail",
            fontsize=8,
            va="bottom",
            color="#a02020",
        )
        ax.text(
            low[1],
            high[0],
            "more throughput, worse tail ",
            fontsize=8,
            va="bottom",
            ha="right",
            color="#a02020",
        )
        if legend := _kind_legend(kind for *_, kind in points):
            ax.legend(
                handles=legend, loc="center left", fontsize=8, title="Scenario type"
            )
        paths.append(_save(fig, out_dir, f"{prefix}-tradeoff", fmt))

    return paths


def plot_scale(
    result: ScaleResult,
    out_dir: pathlib.Path,
    *,
    title: str,
    prefix: str,
    fmt: ImageFormat = "svg",
) -> list[pathlib.Path]:
    """
    Write memory and latency charts for a `scale` run.

    :param result: The scale run result.
    :param out_dir: Directory to write into (created if missing).
    :param title: Title shown on every chart.
    :param prefix: File name prefix.
    :param fmt: Image format.
    :return: Paths of the files written.
    """
    checkpoints = [cp for cp in result.checkpoints if cp.cumulative_keys > 0]
    if not checkpoints:
        return []

    keys = [cp.cumulative_keys for cp in checkpoints]
    paths: list[pathlib.Path] = []

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(
        keys,
        [cp.rss_delta_mb for cp in checkpoints],
        marker="o",
        label="server RSS growth (MiB)",
    )
    backend_mem = [cp.backend_used_memory_mb for cp in checkpoints]
    if all(value is not None for value in backend_mem):
        ax.plot(keys, backend_mem, marker="s", label="backend used_memory (MiB)")
    ax.set_xscale("log")
    ax.set_xlabel("distinct keys (log scale)")
    ax.set_ylabel("MiB")
    ax.set_title(f"{title}: memory")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    paths.append(_save(fig, out_dir, f"{prefix}-memory", fmt))

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(keys, [cp.bytes_per_key for cp in checkpoints], marker="o", color="#8e5bb5")
    ax.set_xscale("log")
    ax.set_xlabel("distinct keys (log scale)")
    ax.set_ylabel("bytes per key (cumulative RSS growth / keys)")
    ax.set_title(f"{title}: per-key memory cost")
    ax.grid(alpha=0.3, which="both")
    paths.append(_save(fig, out_dir, f"{prefix}-bytes-per-key", fmt))

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(keys, [cp.p50_ms for cp in checkpoints], marker="o", label="P50")
    ax.plot(keys, [cp.p99_ms for cp in checkpoints], marker="o", label="P99")
    ax.set_xscale("log")
    ax.set_xlabel("distinct keys (log scale)")
    ax.set_ylabel("latency of the batch that grew the backend (ms)")
    ax.set_title(f"{title}: latency as the backend fills")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    paths.append(_save(fig, out_dir, f"{prefix}-latency", fmt))

    return paths


def file_prefix(*parts: str) -> str:
    """
    Build a file name prefix from run parameters, e.g. `("http", "redis", "gcra")`.

    :param parts: Pieces to join; non-alphanumerics become `-`.
    :return: A filesystem-safe prefix.
    """
    return "-".join(_slug(part) for part in parts if part)
