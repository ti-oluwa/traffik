import time
import typing

from redis.asyncio import Redis
from redis.exceptions import NoScriptError

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import ConfigurationError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)


class RedisBackend(ThrottleBackend[Redis, HTTPConnectionT]):
    """
    Redis backend for API throttling

    Implements a token-bucket algorithm using Redis Lua scripts.
    """

    lua_script: typing.ClassVar[str] = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local expire_time = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])

    -- Get current token data
    local data = redis.call('HMGET', key, 'tokens', 'last_refill')
    local tokens = tonumber(data[1]) or limit
    local last_refill = tonumber(data[2]) or now

    -- Calculate tokens to add
    local time_passed = now - last_refill
    local refill_rate = limit / expire_time
    local tokens_to_add = time_passed * refill_rate

    -- Refill tokens (capped at limit)
    tokens = math.min(limit, tokens + tokens_to_add)

    -- Check if we can consume a token
    if tokens >= 1 then
        tokens = tokens - 1
        redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
        redis.call('PEXPIRE', key, expire_time * 2)  -- Keep some buffer
        return 0
    else
        -- Calculate wait time
        local tokens_needed = 1 - tokens
        local wait_time = math.ceil(tokens_needed / refill_rate)
        redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
        redis.call('PEXPIRE', key, expire_time * 2)
        return wait_time
    end
    """

    def __init__(
        self,
        connection: Redis,
        *,
        prefix: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__(
            connection,
            prefix=prefix,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        self._lua_sha: typing.Optional[str] = None  # SHA1 hash of the Lua script
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

        now = int(time.monotonic() * 1000)
        try:
            wait_period = await self.connection.evalsha(  # type: ignore
                self._lua_sha,
                1,
                key,
                str(limit),
                str(expires_after),
                str(now),
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
