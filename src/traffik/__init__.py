"""
*Traffik* - A rate limiting library for `starlette` and `fastapi`.
"""

from traffik._utils import get_ip_address  # noqa
from traffik.types import UNLIMITED  # noqa
from traffik.backends.base import (
    ThrottleBackend as ThrottleBackend,
    connection_identifier as connection_identifier,
    connection_throttled as connection_throttled,
    get_throttle_backend as get_throttle_backend,
)
from traffik.backends.inmemory import InMemoryBackend as InMemoryBackend
from traffik.throttles import (
    BaseThrottle as BaseThrottle,
    HTTPThrottle as HTTPThrottle,
    WebSocketThrottle as WebSocketThrottle,
)

__version__ = "0.1.0"
