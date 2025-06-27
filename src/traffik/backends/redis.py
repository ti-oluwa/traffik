import typing

from redis.asyncio import Redis
from redis.exceptions import NoScriptError

from traffik._typing import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import ConfigurationError


class RedisBackend(ThrottleBackend[Redis, HTTPConnectionT]):
    """Redis backend for API throttling"""

    lua_script: typing.ClassVar[str] = """local key = KEYS[1]
local limit = tonumber(ARGV[1])
local expire_time = ARGV[2]

local current = tonumber(redis.call('get', key) or "0")
if current > 0 then
 if current + 1 > limit then
 return redis.call("PTTL",key)
 else
        redis.call("INCR", key)
 return 0
 end
else
    redis.call("SET", key, 1,"px",expire_time)
 return 0
end"""

    def __init__(
        self,
        connection: typing.Union[Redis, str],
        *,
        prefix: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        if isinstance(connection, str):
            connection = Redis.from_url(url=connection)

        elif not isinstance(self.connection, Redis):
            raise TypeError(
                f"Connection must be an instance of `{Redis!r}`, got {type(self.connection)!r}"
            )

        super().__init__(
            connection,
            prefix=prefix,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        self._lua_sha = None  # SHA1 hash of the Lua script
        self._session = self.connection

    async def initialize(self) -> None:
        # Ensure the connection is ready
        await self.connection.ping()
        self._lua_sha = await self.connection.script_load(self.lua_script)  # type: ignore
        self._lua_sha = typing.cast(str, self._lua_sha)

    async def get_wait_period(
        self,
        key: str,
        limit: int,
        expires_after: int,
    ) -> int:
        """
        Get the wait period for the given key.

        :param key: The throttling key.
        :param limit: The maximum number of requests allowed within the time period.
        :param expires_after: The time period in milliseconds for which the key is valid.
        :return: The wait period in milliseconds.
        """
        if not self._lua_sha:
            raise ConfigurationError(
                "Lua script SHA is not initialized. Call `initialize` first."
            )

        try:
            wait_period = await self.connection.evalsha(  # type: ignore
                self._lua_sha,
                1,
                key,
                str(limit),
                str(expires_after),
            )
        except NoScriptError:
            self._lua_sha = await self.connection.script_load(self.lua_script)
            wait_period = await self.get_wait_period(key, limit, expires_after)
        return int(wait_period)

    async def reset(self) -> None:
        """
        Reset the throttling keys.

        :param keys: The throttling keys to reset.
        """
        keys = await self.connection.keys(f"{self.prefix}:*")
        if not keys:
            return
        await self.connection.delete(*keys)

    async def close(self) -> None:
        await self.connection.aclose()
