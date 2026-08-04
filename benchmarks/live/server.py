"""
Spawns the benchmark target app as a real OS process (or a real
multi-process cluster) and waits for it to accept connections.

Two shapes:

- `workers <= 1`: a single `uvicorn` process. This is what "one instance
  of your app" looks like in production - a real event loop, a real
  listening socket, real HTTP parsing.
- `workers > 1`: `gunicorn -k uvicorn.workers.UvicornWorker --preload
  --workers N`. Gunicorn imports the app module once in its master
  process (satisfying `--preload`), then forks `N` worker processes from
  it - the same `fork` start method
  `traffik.backends.multiprocess.MultiProcessInMemoryBackend` is built
  around (see its module docstring).
"""

import asyncio
import contextlib
import os
import platform
import signal
import sys
import typing
from collections import deque
from dataclasses import dataclass, field

import httpx2

from benchmarks.live.ports import get_free_port

HEALTH_PATH = "/__bench__/health"
RESET_PATH = "/__bench__/reset"

# Gunicorn (and its forked workers booting up) can take noticeably longer
# than a single uvicorn process, especially for the first run before
# bytecode is cached.
DEFAULT_READY_TIMEOUT = 20.0
DEFAULT_SHUTDOWN_TIMEOUT = 8.0
STDERR_TAIL_LINES = 40


class ServerStartupError(RuntimeError):
    """Raised when a spawned server process fails to become ready in time."""


@dataclass
class Server:
    """
    A running benchmark server process (or gunicorn cluster), reachable
    over a real TCP socket.

    :param process: The `asyncio` subprocess handle for the master process
        (uvicorn itself, or gunicorn's master which owns the forked workers).
    :param host: Interface the server is bound to.
    :param port: Port the server is bound to.
    :param workers: Number of worker processes (1 = plain uvicorn, no fork).
    :param base_url: `http://{host}:{port}`, ready to hand to an HTTP client.
    :param ws_base_url: `ws://{host}:{port}`, ready to hand to a WS client.
    """

    process: asyncio.subprocess.Process
    host: str
    port: int
    workers: int
    base_url: str = field(init=False)
    ws_base_url: str = field(init=False)
    _stderr_task: typing.Optional[asyncio.Task[None]] = field(default=None, repr=False)
    _stderr_tail: typing.Deque[str] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self.base_url = f"http://{self.host}:{self.port}"
        self.ws_base_url = f"ws://{self.host}:{self.port}"

    def stderr_tail(self) -> str:
        """Last few lines of the process's stderr, for failure diagnostics."""
        return "".join(list(self._stderr_tail)[-STDERR_TAIL_LINES:])

    async def reset(self, client: httpx2.AsyncClient) -> None:
        """
        Ask the app to reset its throttle backend state via its admin route.

        :param client: An `httpx2.AsyncClient` already pointed at this server.
        """
        response = await client.post(RESET_PATH)
        response.raise_for_status()

    async def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """
        Terminate the process (and, for gunicorn, its forked workers)
        gracefully, falling back to a hard kill if it doesn't exit in time.

        :param timeout: Seconds to wait for graceful shutdown before SIGKILL.
        """
        if self.process.returncode is not None:
            await self._stop_stderr_reader()
            return

        pgid: typing.Optional[int] = None
        if platform.system() != "Windows":
            try:
                pgid = os.getpgid(self.process.pid)
            except ProcessLookupError:
                pgid = None

        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                self.process.terminate()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    self.process.kill()
            except ProcessLookupError:
                pass
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=timeout)

        await self._stop_stderr_reader()

    async def _stop_stderr_reader(self) -> None:
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task


async def _read_stderr(
    process: asyncio.subprocess.Process, sink: typing.Deque[str]
) -> None:
    """Continuously drain stderr into `sink` so the pipe never backs up."""
    assert process.stderr is not None
    try:
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            sink.append(line.decode(errors="replace"))
    except asyncio.CancelledError:
        pass


def _build_command(
    app_path: str, host: str, port: int, workers: int
) -> typing.List[str]:
    if workers <= 1:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]

    if platform.system() == "Windows":
        raise ServerStartupError(
            "Multi-worker (gunicorn, fork) benchmark servers require a "
            "POSIX system; Windows does not support the 'fork' start method."
        )

    return [
        sys.executable,
        "-m",
        "gunicorn",
        app_path,
        "--worker-class",
        "uvicorn.workers.UvicornWorker",
        "--workers",
        str(workers),
        "--preload",
        "--bind",
        f"{host}:{port}",
        "--log-level",
        "warning",
        "--timeout",
        "60",
        "--graceful-timeout",
        "5",
    ]


async def _wait_till_ready(server: Server, timeout: float) -> None:
    """Poll the health endpoint until it responds or the deadline passes."""
    deadline = asyncio.get_running_loop().time() + timeout
    delay = 0.05

    async with httpx2.AsyncClient(timeout=1.0) as probe:
        while True:
            if server.process.returncode is not None:
                raise ServerStartupError(
                    f"Server process exited early (code={server.process.returncode}) "
                    f"before becoming ready.\n--- stderr tail ---\n{server.stderr_tail()}"
                )

            try:
                response = await probe.get(f"{server.base_url}{HEALTH_PATH}")
                if response.status_code == 200:
                    return
            except httpx2.TransportError:
                pass

            if asyncio.get_running_loop().time() >= deadline:
                await server.stop(timeout=3.0)
                raise ServerStartupError(
                    f"Server did not become ready within {timeout}s.\n"
                    f"--- stderr tail ---\n{server.stderr_tail()}"
                )

            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 0.5)


async def start_server(
    app_path: str,
    env: typing.Mapping[str, str],
    *,
    workers: int = 1,
    host: str = "127.0.0.1",
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    port_retries: int = 3,
) -> Server:
    """
    Spawn `app_path` (e.g. `"benchmarks.apps.http:app"`) as a
    real server process and block until it answers `GET /__bench__/health`.

    :param app_path: `module:attribute` path to the ASGI app,
        importable by a fresh interpreter (this is what gunicorn/uvicorn
        import - it must not depend on anything from the caller's process).
    :param env: Extra environment variables for the child process (merged
        over a copy of the current process's environment). Used to pass
        `BENCH_*` scenario configuration to `benchmarks.apps.*` modules.
    :param workers: Number of worker processes. `1` spawns plain uvicorn
        (no fork). `>1` spawns gunicorn with `--preload` and the given
        worker count, forking after the app module (and anything it does
        at import time, e.g. `MultiProcessInMemoryBackend.start()`) has run.
    :param host: Loopback interface to bind to.
    :param ready_timeout: Seconds to wait for the health check before
        giving up and raising.
    :param port_retries: How many times to retry with a freshly-allocated
        port if the chosen one turns out to be taken (TOCTOU race in
        `get_free_port`, or a lingering socket in TIME_WAIT).
    :return: A `Server` ready to receive traffic.
    :raises ServerStartupError: If the process exits early or never
        becomes ready within `ready_timeout`.
    """
    last_exc: typing.Optional[BaseException] = None

    for _ in range(max(1, port_retries)):
        port = get_free_port(host)
        command = _build_command(app_path, host, port, workers)
        process_env = {**os.environ, **env}
        process_env.setdefault("PYTHONUNBUFFERED", "1")

        kwargs: typing.Dict[str, typing.Any] = {}
        if platform.system() != "Windows":
            kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            *command,
            env=process_env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        server = Server(process=process, host=host, port=port, workers=workers)
        server._stderr_tail = deque([])
        server._stderr_task = asyncio.ensure_future(
            _read_stderr(process, server._stderr_tail)
        )

        try:
            await _wait_till_ready(server, timeout=ready_timeout)
            return server
        except ServerStartupError as exc:
            last_exc = exc
            await server.stop(timeout=3.0)
            continue

    assert last_exc is not None
    raise last_exc
