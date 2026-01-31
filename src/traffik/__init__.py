"""
**Traffik** - Asynchronous Distributed Rate Limiting for Starlette and FastAPI Applications.
"""

from .backends.base import (
    ThrottleBackend as ThrottleBackend,
)
from .backends.base import (
    connection_throttled as connection_throttled,
)
from .backends.base import (
    default_identifier as default_identifier,
)
from .backends.base import (
    get_throttle_backend as get_throttle_backend,
)
from .backends.inmemory import InMemoryBackend as InMemoryBackend
from .rates import Rate  # noqa
from .throttles import (
    HTTPThrottle as HTTPThrottle,
)
from .throttles import (
    Throttle as Throttle,
)
from .throttles import (
    WebSocketThrottle as WebSocketThrottle,
)
from .throttles import (
    is_throttled as is_throttled,
)
from .types import *  # noqa
from .utils import (  # noqa
    get_blocking_setting,
    get_blocking_timeout,
    get_remote_address,
    set_blocking_setting,
    set_blocking_timeout,
)

__version__ = "1.0.0"


# NOTE: All operations by traffik must be fast and non-blocking.
# Hence, avoid using logging or any blocking IO in the main code paths, especially in backends, locks, strategies, and throttles.
# Logging can be used in non-performance critical paths such as startup/shutdown but should be kept to a minimum.
# One other detriment of using logging is that it may cause deadlocks if the logging backend uses the same resources as traffik.
# If logging is absolutely necessary, use `print` statements instead (but keep it brief).
