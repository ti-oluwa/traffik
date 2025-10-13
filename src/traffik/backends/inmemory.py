import asyncio
from collections import defaultdict
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
    """

    def __init__(
        self,
        namespace: str = "inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.connection is None:
            self.connection = defaultdict(dict)

    async def get(self, key: str, *args, **kwargs) -> typing.Optional[typing.Any]:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        async with self._lock:
            entry = self.connection.get(self.namespace, {}).get(key)
            if entry is None:
                return None

            if not isinstance(entry, tuple) or len(entry) != 2:
                return entry

            value, expiration_time = entry
            if (
                expiration_time is None
                or expiration_time > asyncio.get_event_loop().time()
            ):
                return value

            # Entry has expired
            del self.connection[self.namespace][key]
            return None

    async def set(
        self, key: str, value: float, *, expire: typing.Optional[int] = None, **kwargs
    ) -> None:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        async with self._lock:
            if expire is not None:
                expiration_time = asyncio.get_event_loop().time() + expire
            else:
                expiration_time = None
            self.connection[self.namespace][key] = (value, expiration_time)

    async def delete(self, key: str, *args, **kwargs) -> None:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        async with self._lock:
            if key in self.connection.get(self.namespace, {}):
                del self.connection[self.namespace][key]

    async def clear(self) -> None:
        if self.connection is None:
            raise ConfigurationError("Backend not initialized")

        async with self._lock:
            self.connection[self.namespace].clear()

    async def reset(self) -> None:
        await super().reset()
        self.connection = None
