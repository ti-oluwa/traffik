"""
**Traffik** - Asynchronous Distributed Rate Limiting for Starlette and FastAPI Applications.
"""

from .backends.base import *  # noqa
from .backends.inmemory import *  # noqa
from .rates import Rate  # noqa
from .throttles import *  # noqa
from .types import *  # noqa
from .utils import (  # noqa
    get_lock_blocking,
    get_lock_blocking_timeout,
    get_lock_ttl,
    get_remote_address,
    set_lock_blocking,
    set_lock_blocking_timeout,
    set_lock_ttl,
)

__version__ = "1.0.2"


# NOTE: All operations by traffik must be fast, efficient and non-blocking.
# Hence, avoid or reduce the use of logging or any blocking IO in the main code paths, especially in backends, locks, strategies, and throttles.
# Logging can be used in non-performance critical paths such as startup/shutdown but should be kept to a minimum.
# One other detriment of using logging is that it may cause deadlocks if the logging backend uses the same resources as traffik.
# If logging is absolutely necessary, use `sys.stderr.write` & `sys.stderr.flush` statements instead (and keep it brief).
