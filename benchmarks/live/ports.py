import contextlib
import socket


def get_free_port(host: str = "127.0.0.1") -> int:
    """
    Ask the OS for a free TCP port on `host`.

    Binds a throwaway socket to port 0 (OS picks an ephemeral free port),
    reads the assigned port back, then closes it. There's a small,
    unavoidable TOCTOU race between closing this socket and the spawned
    server binding the same port, but it's the standard approach and
    collisions are rare enough in practice that `process.py` treats a
    bind failure as a retryable condition rather than ignoring it.

    :param host: Interface to bind the probe socket to.
    :return: A currently-free TCP port number on `host`.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
