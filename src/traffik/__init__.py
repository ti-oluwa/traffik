"""
*Traffik* - A rate limiting library for `starlette` and `fastapi`.
"""

from .backends.base import *  # noqa
from .backends.inmemory import InMemoryBackend  # noqa
from .throttles import *  # noqa
from ._utils import get_ip_address  # noqa
from .types import UNLIMITED  # noqa

__version__ = "0.1.0"
