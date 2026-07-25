"""
Importable ASGI apps used as real benchmark targets.

Each module here defines a module-level `app` (and the throttle/backend it
depends on) built entirely from `BENCH_*` environment variables, and is
importable by a *fresh* interpreter with no dependency on the process that
launched it - because that's exactly what `uvicorn`/`gunicorn` do: import
`"benchmarks.apps.http:app"` by string in their own process.

This is also why configuration travels via environment variables rather
than, say, a pickled config object: gunicorn's `--preload` imports the
module once in its master *before* forking, and everything that module
does at import time (including `MultiProcessInMemoryBackend.start()` in
`multiprocess_app.py`) needs to already be sitting in plain process state
that `fork()` can duplicate - not something reconstructed from an argument
that was only ever passed to this process's `__main__`.

Every app exposes two admin routes the live harness relies on:

- `GET  /__bench__/health` - liveness probe, polled while waiting for the
  process to come up.
- `POST /__bench__/reset`  - clears throttle backend state between
  iterations, since the harness no longer holds a Python reference to the
  backend object living inside the server process.
"""
