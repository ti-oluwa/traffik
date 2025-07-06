import asyncio
import time
import typing

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import ConfigurationError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)


class InMemoryBackend(
    ThrottleBackend[
        typing.Optional[
            typing.MutableMapping[
                str,
                typing.MutableMapping[str, float],
            ]
        ],
        HTTPConnectionT,
    ]
):
    """
    In-memory throttle backend for testing or single-process use.
    Not suitable for production or multi-process environments.

    Implements token-bucket algorithm.
    """

    def __init__(
        self,
        prefix: str = "inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__(
            None,
            prefix=prefix,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.connection is None:
            self.connection = {}

    async def get_wait_period(
        self,
        key: str,
        limit: int,
        expires_after: int,
    ) -> int:
        connection = self.connection
        if connection is None:
            raise ConfigurationError("In-memory backend is not initialized")

        now = int(time.monotonic() * 1000)

        async with self._lock:
            record = connection.get(key, {"tokens": float(limit), "last_refill": now})

            # Calculate time elapsed and tokens to add
            time_elapsed = now - record["last_refill"]
            refill_rate = limit / expires_after  # tokens per millisecond
            tokens_to_add = time_elapsed * refill_rate

            # Refill tokens (capped at limit)
            record["tokens"] = min(float(limit), record["tokens"] + tokens_to_add)
            record["last_refill"] = now

            # Check if we can consume a token
            if record["tokens"] >= 1.0:
                record["tokens"] -= 1.0
                connection[key] = record
                return 0

            # Calculate wait time for next token
            tokens_needed = 1.0 - record["tokens"]
            wait_time = int(tokens_needed / refill_rate)
            connection[key] = record
            return wait_time

    async def reset(self) -> None:
        self.connection = None

    async def close(self) -> None:
        pass
