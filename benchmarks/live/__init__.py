"""
Real-process, real-network benchmark harness.

Everything under `benchmarks.live` is about actually running the
application the way it runs in production - as its own OS process,
listening on a real TCP socket, driven by a real async HTTP/WebSocket
client - instead of calling the ASGI callable directly in-process.

`process.py`   spawns/tears down `uvicorn` (single worker) or `gunicorn`
               (multiple workers, fork start method) subprocesses hosting
               a `benchmarks.apps.*` module.
`ports.py`     picks free loopback TCP ports for those subprocesses.
`client.py`    drives real HTTP requests (`httpx2.AsyncClient`, real
               transport) and real WebSocket connections (`websockets`)
               against a running server.
"""
