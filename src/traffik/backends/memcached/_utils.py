import typing
from urllib.parse import urlparse


def _parse_memcached_url(url: str) -> typing.Dict[str, typing.Any]:
    """
    Parse Memcached URL into connection parameters.

    :param url: Memcached URL (e.g. memcached://host:port).
    :return: Dictionary of connection parameters.
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith("memcached"):
        raise ValueError("Invalid Memcached URL scheme")

    host = parsed.hostname or "localhost"
    port = parsed.port or 11211
    return {"host": host, "port": port}
