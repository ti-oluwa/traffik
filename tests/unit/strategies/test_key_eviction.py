"""
Tests that every strategy recovers cleanly when its stored state vanishes mid-window.

Redis under `maxmemory` and Memcached under memory pressure evict keys the
library had no say in, and the library can't tell that apart from a TTL
expiring early. Each strategy must treat vanished state as "no history",
start a fresh window or bucket, and not raise or leave a stale deficit.
Exercised against every backend and every built-in and custom strategy.
"""

import pytest

from tests.conftest import CUSTOM_STRATEGIES, STRATEGIES, BackendGen
from traffik.rates import Rate

ALL_STRATEGIES = [*STRATEGIES, *CUSTOM_STRATEGIES]


@pytest.mark.anyio
@pytest.mark.strategy
@pytest.mark.backend
class TestStateEviction:
    @pytest.mark.parametrize("strategy_cls", ALL_STRATEGIES)
    async def test_recovers_after_all_state_is_evicted(
        self, backends: BackendGen, strategy_cls: type
    ) -> None:
        rate = Rate.parse("5/s")
        for backend in backends(namespace="key_eviction"):
            async with backend(persistent=False, close_on_exit=True):
                strategy = strategy_cls()
                key = backend.get_key(f"user:{strategy_cls.__name__}")

                for _ in range(3):
                    await strategy(key, rate, backend, 1)

                await backend.reset()  # Every key gone, as total eviction would

                wait = await strategy(key, rate, backend, 1)
                assert wait == 0, (
                    f"{strategy_cls.__name__} on {type(backend).__name__}: "
                    f"expected a fresh start after eviction, got wait={wait}ms"
                )
