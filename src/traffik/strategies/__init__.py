from .token_bucket import *  # noqa
from .leaky_bucket import *  # noqa
from .fixed_window import *  # noqa
from .sliding_window import *  # noqa
from .fixed_window import FixedWindowStrategy  # noqa
from .custom import *  # noqa

default_strategy = FixedWindowStrategy()
