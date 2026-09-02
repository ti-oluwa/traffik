"""
Importable ASGI apps used as real benchmark targets.

Each module here defines a module-level `app` (and the throttle/backend it
depends on) built entirely from `BENCH_*` environment variables, and is
importable by a fresh interpreter with no dependency on the process that
launched it.

Every app exposes two admin routes the live harness relies on:

- `GET  /__bench__/health`: liveness probe, polled while waiting for the
  process to come up.
- `POST /__bench__/reset`: clears throttle backend state between
  iterations, since the harness no longer holds a Python reference to the
  backend object living inside the server process.
"""
