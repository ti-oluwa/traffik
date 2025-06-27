"""
Utilities for API routes throttling in FastAPI.

Uses Redis as the backend for storing throttling data.

Adapted from `fastapi-limiter` package.

Usage:

In a FastAPI application setup:
```python

import fastapi

from helpers.fastapi.requests.throttling import configure

def lifespan(app: fastapi.FastAPI):
    try:
        async configure(
            persistent=app.debug is False,
            # Disables persistent rate limiting in debug mode,
            # Useful in development environments.
            redis="redis://localhost/0"
        ):
            yield
    finally:
        pass

app = fastapi.FastAPI(lifespan=lifespan)
```

In a FastAPI route/endpoint:
```python
from helpers.fastapi.requests.throttling import throttle

router = fastapi.APIRouter()

@router.get("some-route")
@throttled(limit=20, seconds=10) # Burst limit of 20 requests in 10 seconds
@throttled(limit=10000, hours=4) # Sustained limit of 10000 requests in 4 hours
async def some_route():
    pass
"""

from .backends.base import *  # noqa
from .backends.inmemory import InMemoryBackend # noqa
from .throttles import *  # noqa
from .decorators import throttled  # noqa
from ._utils import get_ip_address  # noqa
