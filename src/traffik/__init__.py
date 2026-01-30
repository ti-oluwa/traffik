"""
`Traffik` - Rate limiting for `starlette` applications.
"""

from .utils import get_remote_address  # noqa
from .types import *  # noqa
from .rates import Rate  # noqa
from .backends.base import (
    ThrottleBackend as ThrottleBackend,
    default_identifier as default_identifier,
    connection_throttled as connection_throttled,
    get_throttle_backend as get_throttle_backend,
)
from .backends.inmemory import InMemoryBackend as InMemoryBackend
from .throttles import (
    Throttle as Throttle,
    HTTPThrottle as HTTPThrottle,
    WebSocketThrottle as WebSocketThrottle,
)

__version__ = "1.0.0"


# NOTE: All operations by traffik must be fast and non-blocking.
# Hence, avoid using logging or any blocking IO in the main code paths, especially in backends, locks, strategies, and throttles.
# Logging can be used in non-performance critical paths such as startup/shutdown but should be kept to a minimum.
# One other detriment of using logging is that it may cause deadlocks if the logging backend uses the same resources as traffik.
# If logging is absolutely necessary, use `print` statements instead (but keep it brief).
