from starlette.requests import HTTPConnection

from traffik.types import UNLIMITED


async def default_client_identifier(connection: HTTPConnection) -> str:
    return "testclient"


async def unlimited_identifier(connection: HTTPConnection) -> object:
    return UNLIMITED
