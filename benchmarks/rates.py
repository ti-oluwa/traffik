"""
Rate-string conversion between traffik's format and the `limits` library's
format (what SlowAPI's `@limiter.limit(...)` actually parses).
"""

from traffik.rates import Rate


def traffik_rate_to_limits_string(rate: str) -> str:
    """
    Convert a traffik rate string (e.g. `"100/60s"`) into a string
    `limits.parse()` accepts (e.g. `"100/60second"`), for use with
    SlowAPI's `@limiter.limit(...)`.

    Restricted to whole-second windows: every scenario in
    `benchmarks.scenarios` uses one, and `limits`' smallest built-in
    granularity is the second, so there is no equivalent representation
    for a sub-second traffik rate - converting `"10/500ms"` to a coarser
    unit would silently change the limit being compared, defeating the
    point of a controlled comparison.

    :param rate: A traffik rate string, as accepted by `traffik.rates.Rate.parse`.
    :return: An equivalent rate string accepted by `limits.parse`.
    :raises ValueError: If `rate` describes a sub-second window.
    """
    parsed = Rate.parse(rate)
    if parsed.expire % 1000 != 0:
        raise ValueError(
            f"Rate {rate!r} has a sub-second window ({parsed.expire}ms); "
            "SlowAPI/`limits` has no sub-second granularity, so `compare` "
            "cannot represent this rate identically on both sides."
        )
    seconds = parsed.expire // 1000
    return f"{parsed.limit}/{seconds}second"
