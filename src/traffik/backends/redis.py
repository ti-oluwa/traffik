import typing

from redis.asyncio import Redis

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import ConfigurationError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)


class RedisBackend(ThrottleBackend[Redis, HTTPConnectionT]):
    """
    Redis throttle backend
    """

    def __init__(
        self,
        connection: Redis,
        *,
        namespace: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__(
            connection,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        self._session = self.connection

    async def initialize(self) -> None:
        # Ensure the connection is ready
        await self.connection.ping()

    async def get(self, key: str, *args, **kwargs) -> typing.Optional[typing.Any]:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        value = await self.connection.get(key)
        if value is None:
            return None
        return float(value)

    async def set(
        self,
        key: str,
        value: typing.Any,
        expire: typing.Optional[int] = None,
        *args,
        **kwargs,
    ) -> None:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        if expire is not None:
            await self.connection.set(key, value, ex=expire)
        else:
            await self.connection.set(key, value)

    async def delete(self, key: str, *args, **kwargs) -> None:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        await self.connection.delete(key)

    async def clear(self) -> None:
        keys = await self.connection.keys(f"{self.namespace}:*")
        if not keys:
            return
        await self.connection.delete(*keys)

    async def close(self) -> None:
        await self.connection.aclose()
