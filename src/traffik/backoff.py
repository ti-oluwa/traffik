"""Backoff stretegies"""

import math
import random
import typing

__all__ = [
    "ConstantBackoff",
    "LinearBackoff",
    "ExponentialBackoff",
    "LogarithmicBackoff",
]


def ConstantBackoff(
    attempt: int,
    base_delay: float,
) -> float:
    """Delay remains the same for each attempt."""
    return base_delay


class LinearBackoff:
    """Delay increases linearly with each attempt."""

    __slots__ = ("increment",)

    def __init__(self, increment: float = 1.0) -> None:
        """
        :param increment: Amount to increase delay per attempt (in seconds).
        """
        self.increment = increment

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay + (attempt - 1) * self.increment


class ExponentialBackoff:
    """Delay doubles (or multiplies) with each attempt."""

    __slots__ = ("multiplier", "max_delay", "jitter")

    def __init__(
        self,
        multiplier: float = 2.0,
        max_delay: typing.Optional[float] = None,
        jitter: bool = False,
    ) -> None:
        """
        :param multiplier: Factor to multiply delay by each attempt.
        :param max_delay: Maximum delay cap (in seconds).
        :param jitter: Whether to add random jitter to prevent thundering herd.
        """
        self.multiplier = multiplier
        self.max_delay = max_delay
        self.jitter = jitter

    def __call__(self, attempt: int, base_delay: float) -> float:
        delay = base_delay * (self.multiplier ** (attempt - 1))
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random())  # nosec
        return delay


class LogarithmicBackoff:
    """Delay increases logarithmically with each attempt."""

    __slots__ = ("base",)

    def __init__(self, base: float = 2.0) -> None:
        """
        :param base: Logarithm base.
        """
        self.base = base

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay * math.log(attempt + 1, self.base)


DEFAULT_BACKOFF = ExponentialBackoff(multiplier=2.0, max_delay=60.0, jitter=True)
