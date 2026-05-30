"""Backoff stretegies"""

import enum
import math
import random
import typing

__all__ = [
    "ConstantBackoff",
    "ExponentialBackoff",
    "LinearBackoff",
    "LogarithmicBackoff",
]


class JitterType(enum.Enum):
    NONE = "none"
    FULL = "full"
    EQUAL = "equal"


def _get_jitter(jitter: typing.Union[JitterType, bool]) -> JitterType:
    if isinstance(jitter, JitterType):
        return jitter
    return JitterType.EQUAL if jitter else JitterType.NONE


def ConstantBackoff(
    attempt: int,
    base_delay: float,
) -> float:
    """Delay remains the same for each attempt."""
    return base_delay


class LinearBackoff:
    """Delay increases linearly with each attempt."""

    __slots__ = ("increment", "jitter")

    def __init__(
        self,
        increment: float = 1.0,
        jitter: typing.Union[JitterType, bool] = JitterType.NONE,
    ) -> None:
        """
        :param increment: Amount to increase delay per attempt (in seconds).
        :param jitter: Random jitter type to add to delay.
        """
        self.increment = increment
        self.jitter = _get_jitter(jitter)

    def __call__(self, attempt: int, base_delay: float) -> float:
        delay = base_delay + (attempt - 1) * self.increment
        if self.jitter is JitterType.FULL:
            delay = random.uniform(0.0, delay)  # nosec

        elif self.jitter is JitterType.EQUAL:
            delay = delay / 2 + random.uniform(0.0, delay / 2)  # nosec
        return delay


class ExponentialBackoff:
    """Delay increases exponentially with each attempt."""

    __slots__ = ("jitter", "max_delay", "multiplier")

    def __init__(
        self,
        multiplier: float = 2.0,
        max_delay: typing.Optional[float] = None,
        jitter: typing.Union[JitterType, bool] = JitterType.NONE,
    ) -> None:
        """
        :param multiplier: Factor to multiply delay by each attempt.
        :param max_delay: Maximum delay cap (in seconds).
        :param jitter: Random jitter type to add to delay.
        """
        self.multiplier = multiplier
        self.max_delay = max_delay
        self.jitter = _get_jitter(jitter)

    def __call__(self, attempt: int, base_delay: float) -> float:
        delay = base_delay * (self.multiplier ** (attempt - 1))
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)

        if self.jitter is JitterType.FULL:
            delay = random.uniform(0.0, delay)  # nosec

        elif self.jitter is JitterType.EQUAL:
            delay = delay / 2 + random.uniform(0.0, delay / 2)  # nosec
        return delay


class LogarithmicBackoff:
    """Delay increases logarithmically with each attempt."""

    __slots__ = ("base", "jitter")

    def __init__(
        self,
        base: float = 2.0,
        jitter: typing.Union[JitterType, bool] = JitterType.NONE,
    ) -> None:
        """
        :param base: Logarithm base.
        :param jitter: Random jitter type to add to delay.
        """
        self.base = base
        self.jitter = _get_jitter(jitter)

    def __call__(self, attempt: int, base_delay: float) -> float:
        delay = base_delay * math.log(attempt + 1, self.base)
        if self.jitter is JitterType.FULL:
            delay = random.uniform(0.0, delay)  # nosec

        elif self.jitter is JitterType.EQUAL:
            delay = delay / 2 + random.uniform(0.0, delay / 2)  # nosec
        return delay


DEFAULT_BACKOFF = ExponentialBackoff(
    multiplier=2.0, max_delay=60.0, jitter=JitterType.FULL
)
