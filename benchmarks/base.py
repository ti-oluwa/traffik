from starlette.requests import HTTPConnection

from traffik import get_remote_address
from traffik.backends.memcached import MemcachedBackend


async def custom_identifier(connection: HTTPConnection) -> str:
    """Use X-Client-ID header if present, otherwise fall back to IP."""
    client_id = connection.headers.get("X-Client-ID")
    if client_id:
        return f"client:{client_id}"

    # Fallback to IP
    client_ip = get_remote_address(connection)
    if not client_ip:
        return "anonymous"
    return f"ip:{client_ip}"


class BenchmarkMemcachedBackend(MemcachedBackend):
    """
    Memcached backend variant for benchmarks.

    NOTE: This backend skips key tracking to avoid performance overhead during benchmarks.
    But caution: Without key tracking, the `clear` method will flush the entire Memcached cache,
    which may affect other applications using the same Memcached instance.
    Ensure that the Memcached instance is dedicated for benchmarking purposes only.
    """

    async def clear(self) -> None:
        # Flush entire Memcached cache, if not tracking keys
        if self.connection is not None and not self.track_keys:
            await self.connection.flush_all()
            return
        await super().clear()
