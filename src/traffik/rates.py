import functools
import re
import typing
from typing import Annotated

from annotated_types import Ge


@typing.final
class Rate:
    """Rate limit definition"""

    __slots__ = (
        "limit",
        "expire",
        "is_subsecond",
        "unlimited",
        "rps",
        "rph",
        "rpm",
        "rpd",
    )

    def __init__(
        self,
        *,
        limit: Annotated[int, Ge(0)] = 0,
        milliseconds: Annotated[int, Ge(0)] = 0,
        seconds: Annotated[int, Ge(0)] = 0,
        minutes: Annotated[int, Ge(0)] = 0,
        hours: Annotated[int, Ge(0)] = 0,
    ) -> None:
        """
        Create a `Rate` instance

        :param limit: Maximum number of allowed requests in the time window. 0 means no limit.
        :param milliseconds: Time period in milliseconds.
        :param seconds: Time period in seconds.
        :param minutes: Time period in minutes.
        :param hours: Time period in hours.
        """
        if limit < 0:
            raise ValueError("Limit must be non-negative")

        expire = milliseconds + 1000 * seconds + 60000 * minutes + 3600000 * hours
        if expire < 0:
            raise ValueError("Time period must be non-negative")
        if limit == 0 and expire != 0:
            raise ValueError("Expire must be 0 when limit is 0")
        if limit != 0 and expire == 0:
            raise ValueError("Expire must be greater than 0 when limit is set")

        self.limit = limit
        self.expire = expire
        self.is_subsecond = expire < 1000

        unlimited = limit == 0 and expire == 0
        self.unlimited = unlimited
        self.rps = float("inf") if unlimited else limit / (expire / 1000)
        self.rpm = float("inf") if unlimited else limit / (expire / 60000)
        self.rph = float("inf") if unlimited else limit / (expire / 3600000)
        self.rpd = float("inf") if unlimited else limit / (expire / 86400000)

    def __init_subclass__(cls) -> None:
        raise RuntimeError("`Rate` should not be subclassed.")

    def __eq__(self, other: object, /) -> bool:
        return (
            isinstance(other, Rate)
            and self.limit == other.limit
            and self.expire == other.expire
        )

    def __copy__(self) -> "Rate":
        copy = Rate.__new__(Rate)
        copy.limit = self.limit
        copy.expire = self.expire
        copy.unlimited = self.unlimited
        copy.is_subsecond = self.is_subsecond
        copy.rps = self.rps
        copy.rpm = self.rpm
        copy.rph = self.rph
        copy.rpd = self.rpd
        return copy

    def __setattr__(self, name: str, value: typing.Any):
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("`Rate` object is immutable")
        super().__setattr__(name, value)

    def __repr__(self) -> str:
        if self.unlimited:
            return f"{self.__class__.__name__}(unlimited)"
        return f"{self.__class__.__name__}(limit={self.limit}req, expire={self.expire}ms)"

    @classmethod
    def parse(cls, rate: str) -> "Rate":
        """
        Get a `Rate` object from a string representation.

        Supported formats:
        - `"<limit>/<unit>"`: e.g., "5/m" means 5 requests per minute
        - `"<limit>/<period><unit>"`: e.g., "2/5s" means 2 requests per 5 seconds
        - `"<limit>/<period> <unit>"`: e.g., "10/30 seconds" means 10 requests per 30 seconds
        - `"<limit> per <period> <unit>"`: e.g., "2 per second" means 2 requests per 1 second.
        - `"<limit> per <period><unit>"`: e.g., "2 persecond" means 2 requests per 1 second.

        Where:
        - `"<limit>"`: Maximum number of requests (integer)
        - `"<period>"`: Time period multiplier (integer, optional, defaults to 1)
        - `"<unit>"`: Time unit - supports:
            - Milliseconds: "ms", "millisecond", "milliseconds"
            - Seconds: "s", "sec", "second", "seconds"
            - Minutes: "m", "min", "minute", "minutes"
            - Hours: "h", "hr", "hour", "hours"
            - Days: "d", "day", "days"

        Examples:
        - "5/m" -> 5 requests per minute
        - "100/h" -> 100 requests per hour
        - "10 per second" -> 10 requests per second
        - "2/5s" -> 2 requests per 5 seconds
        - "10/30 seconds" -> 10 requests per 30 seconds
        - "1000/500ms" -> 1000 requests per 500 milliseconds

        :param rate: The string representation of the rate limit.
        :return: A `Rate` object.
        :raises ValueError: If the rate string is invalid or cannot be parsed.
        """
        rate = str(rate).strip()
        if not rate:
            raise ValueError("Rate string cannot be empty")
        # Use `.lower()` for better cache performance
        return _parse_rate_string(rate.lower())


_PERIOD_RE = re.compile(r"^(\d+)?\s*([a-z]+)$")
_SPLIT_RE = re.compile(r"\s*per\s*|\/", re.IGNORECASE)
# Maps unit to Rate constructor parameters
_UNIT_MAPPING = {
    "ms": ("milliseconds", 1),
    "millisecond": ("milliseconds", 1),
    "milliseconds": ("milliseconds", 1),
    "s": ("seconds", 1),
    "sec": ("seconds", 1),
    "second": ("seconds", 1),
    "seconds": ("seconds", 1),
    "m": ("minutes", 1),
    "min": ("minutes", 1),
    "minute": ("minutes", 1),
    "minutes": ("minutes", 1),
    "h": ("hours", 1),
    "hr": ("hours", 1),
    "hour": ("hours", 1),
    "hours": ("hours", 1),
    "d": ("days", 24),  # days are represented as hours
    "day": ("days", 24),
    "days": ("days", 24),
}


@functools.lru_cache(maxsize=512)
def _parse_rate_string(rate: str) -> Rate:
    parts = _SPLIT_RE.split(rate)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid rate format '{rate}'. Expected format: '<limit>/<period><unit>' or '<limit> per <period><unit>'"
            f"(e.g., '5 per m', '2/5s', '10/30 seconds')"
        )

    # Parse limit (left side)
    limit_str = parts[0].strip()
    try:
        limit = int(limit_str)
    except ValueError as exc:
        raise ValueError(
            f"Invalid limit '{limit_str}'. Limit must be a non-negative integer."
        ) from exc

    if limit < 0:
        raise ValueError("Limit must be non-negative")

    # Parse period (right side)
    period_str = parts[1].strip().lower()
    if not period_str:
        raise ValueError("Period cannot be empty")

    # Extract number and unit from period string
    # Regex matches: optional number + optional whitespace + unit
    match = _PERIOD_RE.match(period_str)
    if not match:
        raise ValueError(
            f"Invalid period format '{period_str}'. Expected format: "
            f"'<number><unit>' or '<unit>' (e.g., '5s', 's', '30 seconds')"
        )

    period_num_str, unit = match.groups()
    period_multiplier = int(period_num_str) if period_num_str else 1

    if period_multiplier <= 0:
        raise ValueError(f"Period multiplier must be positive, got {period_multiplier}")

    if unit not in _UNIT_MAPPING:
        valid_units = sorted(set(_UNIT_MAPPING.keys()))
        raise ValueError(
            f"Invalid time unit '{unit}'. Valid units: {', '.join(valid_units)}"
        )

    param_name, base_multiplier = _UNIT_MAPPING[unit]
    # For days, we use hours internally
    if param_name == "days":
        return Rate(limit=limit, hours=period_multiplier * base_multiplier)
    return Rate(limit=limit, **{param_name: period_multiplier * base_multiplier})
