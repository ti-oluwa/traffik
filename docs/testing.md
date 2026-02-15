# Testing

Testing rate-limited endpoints should be fast, isolated, and deterministic. Traffik makes this easy with `InMemoryBackend` — no Redis, no Memcached, no Docker required for unit tests.

---

## The Golden Rule

Use `InMemoryBackend` with `persistent=False` for tests. This gives you a backend that:

- Starts instantly
- Has zero external dependencies
- Resets cleanly between tests
- Is fast enough to run in tight loops

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="test", persistent=False)
```

!!! tip "Always reset between tests"
    Use a fresh backend per test (via fixture scope) or call `await backend.reset()` in a teardown. Without this, rate limit state leaks between tests and you'll get mysterious failures. A `pytest` fixture with `scope="function"` handles this automatically.

---

## Basic Test Pattern

Here's the standard setup with `pytest`, `anyio`, and `httpx.AsyncClient`:

```python
# tests/test_my_endpoints.py

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend


@pytest.fixture
def backend():
    """Fresh backend for each test."""
    return InMemoryBackend(namespace="test", persistent=False)


@pytest.fixture
def app(backend):
    """FastAPI app with a rate-limited endpoint."""
    throttle = HTTPThrottle("test:api", rate="3/min", backend=backend)
    app = FastAPI(lifespan=backend.lifespan)

    @app.get("/items")
    async def get_items(request: Request = Depends(throttle)):
        return {"items": [1, 2, 3]}

    return app


@pytest.mark.anyio
async def test_allows_requests_under_limit(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(3):
            response = await client.get("/items")
            assert response.status_code == 200


@pytest.mark.anyio
async def test_rejects_requests_over_limit(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Use up all 3 allowed requests
        for _ in range(3):
            await client.get("/items")

        # The 4th should be rejected
        response = await client.get("/items")
        assert response.status_code == 429
```

Add `anyio_mode = "auto"` to your `pytest.ini` or `pyproject.toml`:

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

---

## Strategy Parametrize Pattern

Test that your endpoint behaves correctly regardless of which strategy is used:

```python
import pytest
from traffik.strategies import (
    FixedWindow,
    TokenBucket,
    SlidingWindowCounter,
)

STRATEGIES = [
    FixedWindow(),
    TokenBucket(),
    SlidingWindowCounter(),
]


@pytest.mark.parametrize("strategy", STRATEGIES, ids=["fixed", "token", "sliding"])
@pytest.mark.anyio
async def test_rate_limit_with_strategy(strategy):
    backend = InMemoryBackend(namespace="test", persistent=False)
    throttle = HTTPThrottle(
        f"test:strategy:{type(strategy).__name__}",
        rate="2/min",
        backend=backend,
        strategy=strategy,
    )

    app = FastAPI(lifespan=backend.lifespan)

    @app.get("/data")
    async def get_data(request: Request = Depends(throttle)):
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/data")).status_code == 200
        assert (await client.get("/data")).status_code == 200
        assert (await client.get("/data")).status_code == 429
```

---

## Error Handler Test

Test that your `on_error` fallback behaves correctly when the backend fails:

```python
import pytest
from unittest.mock import AsyncMock, patch
from traffik.exceptions import BackendError


@pytest.mark.anyio
async def test_error_handler_fallback():
    """When the primary backend fails, the fallback should kick in."""
    primary = InMemoryBackend(namespace="primary", persistent=False)
    fallback = InMemoryBackend(namespace="fallback", persistent=False)

    from traffik.error_handlers import backend_fallback

    throttle = HTTPThrottle(
        "test:fallback",
        rate="10/min",
        backend=primary,
        on_error=backend_fallback(backend=fallback, fallback_on=(BackendError,)),
    )

    app = FastAPI(lifespan=primary.lifespan)

    @app.get("/api")
    async def api_endpoint(request: Request = Depends(throttle)):
        return {"ok": True}

    # Patch primary backend to raise BackendError
    with patch.object(primary, "increment_with_ttl", side_effect=BackendError("Redis down")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Should succeed via fallback backend
            response = await client.get("/api")
            assert response.status_code == 200


@pytest.mark.anyio
async def test_on_error_allow():
    """on_error='allow' should let requests through when backend fails."""
    backend = InMemoryBackend(namespace="test", persistent=False)
    throttle = HTTPThrottle("test:allow", rate="1/min", backend=backend, on_error="allow")

    app = FastAPI(lifespan=backend.lifespan)

    @app.get("/api")
    async def api_endpoint(request: Request = Depends(throttle)):
        return {"ok": True}

    with patch.object(backend, "increment_with_ttl", side_effect=Exception("DB error")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api")
            assert response.status_code == 200  # Allowed through despite error
```

---

## WebSocket Test Pattern

```python
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocket
from traffik import WebSocketThrottle
from traffik.backends.inmemory import InMemoryBackend
from fastapi import FastAPI


@pytest.mark.anyio
async def test_websocket_rate_limiting():
    backend = InMemoryBackend(namespace="test", persistent=False)
    ws_throttle = WebSocketThrottle("test:ws", rate="2/min", backend=backend)

    app = FastAPI(lifespan=backend.lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        while True:
            data = await websocket.receive_text()
            await ws_throttle.hit(websocket)  # Throttle each message
            await websocket.send_text(f"echo: {data}")

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        response = ws.receive_text()
        assert "echo" in response

        ws.send_text("world")
        response = ws.receive_text()
        assert "echo" in response

        # Third message should be rate-limited
        ws.send_text("third")
        # WebSocket throttle sends a rate_limit JSON message, not an exception
        response = ws.receive_json()
        assert response["type"] == "rate_limit"
```

---

## Running Tests with Docker

If you want to test against real Redis or Memcached, use the Docker testing script:

```bash
# Run the full test suite (InMemory + Redis + Memcached)
./docker-test.sh test

# Run fast tests (InMemory only, skips external backends)
./docker-test.sh test-fast

# Test across multiple Python versions (3.9, 3.10, 3.11, 3.12)
./docker-test.sh test-matrix

# Start a development environment with Redis running
./docker-test.sh dev
```

The `test-fast` command skips Redis and Memcached tests by setting `SKIP_REDIS_TESTS=true` and `SKIP_MEMCACHED_TESTS=true`. Great for quick iteration during development.

!!! tip "CI strategy"
    For your CI pipeline, run `test-fast` on every PR (fast, catches logic bugs) and `test` on merge to main (comprehensive, catches distributed coordination issues). The full test suite including Redis and Memcached is where race condition correctness tests run.
