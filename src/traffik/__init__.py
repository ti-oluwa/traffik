"""
**Traffik** - Distributed Rate Limiting for Starlette Applications.
"""

from .backends import *  # noqa
from .rates import Rate  # noqa
from .throttles import *  # noqa
from .typing import *  # noqa
from .config import *  # noqa
from ._utils import *  # noqa
from .headers import *  # noqa
from .registry import *  # noqa


__version__ = "1.2.0"
