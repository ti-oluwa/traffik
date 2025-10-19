import re
from dataclasses import dataclass, field
from typing import Annotated

from annotated_types import Ge


@dataclass(frozen=True, eq=False)
class Rate:
    """Rate limit definition"""

    limit: Annotated[int, Ge(0)] = 0
    """Maximum number of allowed requests in the time window. 0 means no limit."""
    milliseconds: Annotated[int, Ge(0)] = 0
    """Time period in milliseconds"""
    seconds: Annotated[int, Ge(0)] = 0
    """Time period in seconds"""
    minutes: Annotated[int, Ge(0)] = 0
    """Time period in minutes"""
    hours: Annotated[int, Ge(0)] = 0
    """Time period in hours"""
    _expire: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("Limit must be non-negative")
        expire = (
            self.milliseconds
            + 1000 * self.seconds
            + 60000 * self.minutes
            + 3600000 * self.hours
        )
        if expire < 0:
            raise ValueError("Time period must be non-negative")
        if self.limit == 0 and expire != 0:
            raise ValueError("Expire must be 0 when limit is 0")
        if self.limit != 0 and expire == 0:
            raise ValueError("Expire must be greater than 0 when limit is set")

        object.__setattr__(self, "_expire", expire)

    @property
    def expire(self) -> float:
        """Total time period in milliseconds, per limit"""
        return self._expire

    @property
    def unlimited(self) -> bool:
        """Whether the rate limit is unlimited"""
        return self.limit == 0 and self.expire == 0

    @property
    def rps(self) -> float:
        """Requests per second"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 1000)

    @property
    def rpm(self) -> float:
        """Requests per minute"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 60000)

    @property
    def rph(self) -> float:
        """Requests per hour"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 3600000)

    @property
    def rpd(self) -> float:
        """Requests per day"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 86400000)

    def __eq__(self, other: object, /) -> bool:
        return isinstance(other, Rate) and self.rps == other.rps

    @classmethod
    def parse(cls, rate: str) -> "Rate":
        """
        Construct a `Rate` object from a string representation.

        Supported formats:
        - "<limit>/<unit>": e.g., "5/m" means 5 requests per minute
        - "<limit>/<period><unit>": e.g., "2/5s" means 2 requests per 5 seconds
        - "<limit>/<period> <unit>": e.g., "10/30 seconds" means 10 requests per 30 seconds

        Where:
        - <limit>: Maximum number of requests (integer)
        - <period>: Time period multiplier (integer, optional, defaults to 1)
        - <unit>: Time unit - supports:
            - Milliseconds: "ms", "millisecond", "milliseconds"
            - Seconds: "s", "sec", "second", "seconds"
            - Minutes: "m", "min", "minute", "minutes"
            - Hours: "h", "hr", "hour", "hours"
            - Days: "d", "day", "days"

        Examples:
            - "5/m" -> 5 requests per minute
            - "100/h" -> 100 requests per hour
            - "2/5s" -> 2 requests per 5 seconds
            - "10/30 seconds" -> 10 requests per 30 seconds
            - "1000/500ms" -> 1000 requests per 500 milliseconds

        :param rate: The string representation of the rate limit.
        :return: A `Rate` object.
        :raises ValueError: If the rate string is invalid or cannot be parsed.
        """
        if not isinstance(rate, str):
            raise ValueError(f"Rate must be a string, got {type(rate).__name__}")

        rate = rate.strip()
        if not rate:
            raise ValueError("Rate string cannot be empty")

        # Split on forward slash
        parts = rate.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid rate format '{rate}'. Expected format: '<limit>/<period>' "
                f"(e.g., '5/m', '2/5s', '10/30 seconds')"
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
        match = re.match(r"^(\d+)?\s*([a-z]+)$", period_str)
        if not match:
            raise ValueError(
                f"Invalid period format '{period_str}'. Expected format: "
                f"'<number><unit>' or '<unit>' (e.g., '5s', 's', '30 seconds')"
            )

        period_num_str, unit = match.groups()
        period_multiplier = int(period_num_str) if period_num_str else 1

        if period_multiplier <= 0:
            raise ValueError(
                f"Period multiplier must be positive, got {period_multiplier}"
            )

        # Map unit to Rate constructor parameters
        unit_mapping = {
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

        if unit not in unit_mapping:
            valid_units = sorted(set(unit_mapping.keys()))
            raise ValueError(
                f"Invalid time unit '{unit}'. Valid units: {', '.join(valid_units)}"
            )

        param_name, base_multiplier = unit_mapping[unit]
        # For days, we use hours internally
        if param_name == "days":
            return cls(limit=limit, hours=period_multiplier * base_multiplier)
        return cls(limit=limit, **{param_name: period_multiplier * base_multiplier})
