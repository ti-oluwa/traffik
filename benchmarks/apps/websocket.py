"""
WebSocket benchmark target: a single throttled `/ws` endpoint that echoes
each received JSON message, or replies `{"type": "rate_limit"}` once
throttled - checked over a real WebSocket upgrade, not the ASGI in-process
WebSocket session `tests/client.py` uses for unit tests.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.websocket:app --port 8000
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from benchmarks.apps.config import backend_from_env, env, strategy_from_env
from traffik.registry import ThrottleRegistry
from traffik.throttles import WebSocketThrottle, is_throttled

backend = backend_from_env()
strategy = strategy_from_env()
registry = ThrottleRegistry()

throttle = WebSocketThrottle(
    uid=env("BENCH_UID", "bench_ws"),
    rate=env("BENCH_RATE", "100/60s"),
    backend=backend,
    strategy=strategy,
    registry=registry,
    on_error=env("BENCH_ON_ERROR", "raise"),  # type: ignore[arg-type]
)

app = FastAPI(lifespan=backend.lifespan)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            await throttle(websocket)

            if is_throttled(websocket):
                await websocket.send_json({"type": "rate_limit"})
            else:
                await websocket.send_json({"echo": data, "status": "ok"})
    except WebSocketDisconnect:
        pass


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    await backend.reset()
    return {"status": "reset"}
