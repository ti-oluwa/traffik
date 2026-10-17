"""
Rate-string conversion between traffik's format and the `limits` library's
format (what SlowAPI's `@limiter.limit(...)` actually parses).
"""

from traffik.rates import Rate


def to_limits_rate(rate: str) -> str:
    """
    Convert a traffik rate string (e.g. `"100/60s"`) into a string
    `limits.parse()` accepts (e.g. `"100/60second"`), for use with
    SlowAPI's `@limiter.limit(...)`.

    Restricted to whole-second windows as `limits`' smallest built-in
    granularity is the second.

    :param rate: A traffik rate string, as accepted by `traffik.rates.Rate.parse`.
    :return: An equivalent rate string accepted by `limits.parse`.
    :raises ValueError: If `rate` describes a sub-second window.
    """
    parsed = Rate.parse(rate)
    if parsed.expire % 1000 != 0:
        raise ValueError(
            f"Rate {rate!r} has a sub-second window ({parsed.expire}ms); "
            "SlowAPI/`limits` has no sub-second granularity. "
            "Cannot represent this rate identically on both sides."
        )
    seconds = parsed.expire // 1000
    return f"{parsed.limit}/{seconds}second"
