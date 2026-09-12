"""
**Traffik** - Distributed Rate Limiting for Starlette Applications.
"""

from .backends import *  # noqa
from .rates import Rate  # noqa
from .throttles import *
from .typing import *
from .config import *
from ._utils import *
from ._locks import *
from .headers import *
from .registry import *


__version__ = "1.3.0"
