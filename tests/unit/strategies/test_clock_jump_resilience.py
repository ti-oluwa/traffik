"""
Regression tests for backward system-clock-jump resilience.

`TokenBucketStrategy`/`TokenBucketWithDebtStrategy`, `LeakyBucketStrategy`/
`LeakyBucketWithQueueStrategy`, and `CostBasedTokenBucketStrategy` all refill
or leak based on `now - last_<event>_time`, using wall-clock time (needed so
the calculation agrees across processes and survives restarts). Without a
floor at zero, a backward clock jump (NTP correction, a manual date change,
a leap-second adjustment) produced a *negative* elapsed time, which was then
used directly: token bucket's token count went negative and got persisted,
so every later request computed its wait time from that deficit -- a wait
proportional to the jump size, long after the jump itself had already been
corrected. Leaky bucket's level was pushed up rather than down, causing the
same kind of incorrect, escalating throttling. The fix floors every such
elapsed-time calculation at zero: a clock moving backward is treated as no
time having passed, not negative time.
"""

from unittest.mock import patch

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.custom import CostBasedTokenBucketStrategy
from traffik.strategies.leaky_bucket import (
    LeakyBucketStrategy,
    LeakyBucketWithQueueStrategy,
)
from traffik.strategies.token_bucket import (
    TokenBucketStrategy,
    TokenBucketWithDebtStrategy,
)


@pytest.mark.anyio
@pytest.mark.strategy
class TestTokenBucketClockJumpResilience:
    @pytest.mark.parametrize(
        "strategy_cls", [TokenBucketStrategy, TokenBucketWithDebtStrategy]
    )
    async def test_backward_jump_does_not_cause_persistent_wait(
        self, backend: InMemoryBackend, strategy_cls: type
    ) -> None:
        strategy = strategy_cls()
        rate = Rate.parse("10/s")
        fake_now = [1_000_000.0]

        with patch(
            "traffik.strategies.token_bucket.time", side_effect=lambda: fake_now[0]
        ):
            wait = await strategy("user", rate, backend, cost=1)
            assert wait == 0, "first request should be allowed (bucket starts full)"

            fake_now[0] -= 3600.0  # 1 hour backward
            wait = await strategy("user", rate, backend, cost=1)
            assert wait == 0, (
                "a backward clock jump must not produce a spurious, "
                f"proportional wait (got {wait}ms)"
            )

    async def test_backward_jump_does_not_inflate_wait_when_throttled(
        self, backend: InMemoryBackend
    ) -> None:
        """Even while genuinely throttled (bucket empty), the wait time must
        reflect the configured refill rate, not a clock-jump-inflated deficit.
        """
        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")  # 100ms per token
        fake_now = [1_000_000.0]

        with patch(
            "traffik.strategies.token_bucket.time", side_effect=lambda: fake_now[0]
        ):
            for _ in range(10):
                await strategy("user", rate, backend, cost=1)  # drain the bucket

            fake_now[0] -= 3600.0
            wait = await strategy("user", rate, backend, cost=1)
            # Should be a normal single-token wait (~100ms), not ~1hr+.
            assert wait < 1000.0, (
                f"expected an ordinary refill wait, got {wait}ms "
                "(clock jump leaked into the deficit)"
            )


@pytest.mark.anyio
@pytest.mark.strategy
class TestLeakyBucketClockJumpResilience:
    @pytest.mark.parametrize(
        "strategy_cls", [LeakyBucketStrategy, LeakyBucketWithQueueStrategy]
    )
    async def test_backward_jump_does_not_cause_persistent_throttling(
        self, backend: InMemoryBackend, strategy_cls: type
    ) -> None:
        strategy = strategy_cls()
        rate = Rate.parse("10/s")
        fake_now = [1_000_000.0]

        with patch(
            "traffik.strategies.leaky_bucket.time", side_effect=lambda: fake_now[0]
        ):
            # Fill most of the bucket first, leaving little headroom -- a
            # clock jump inflating the level would tip this into throttling.
            for _ in range(9):
                await strategy("user", rate, backend, cost=1)

            fake_now[0] -= 3600.0
            wait = await strategy("user", rate, backend, cost=1)
            assert wait == 0, (
                "a backward clock jump must not inflate the bucket level "
                f"and cause a spurious wait (got {wait}ms)"
            )


@pytest.mark.anyio
@pytest.mark.strategy
class TestCostBasedTokenBucketClockJumpResilience:
    async def test_backward_jump_does_not_cause_persistent_wait(
        self, backend: InMemoryBackend
    ) -> None:
        strategy = CostBasedTokenBucketStrategy()
        rate = Rate.parse("10/s")
        fake_now = [1_000_000.0]

        with patch("traffik.strategies.custom.time", side_effect=lambda: fake_now[0]):
            wait = await strategy("user", rate, backend, cost=1)
            assert wait == 0

            fake_now[0] -= 3600.0
            wait = await strategy("user", rate, backend, cost=1)
            assert wait == 0, (
                "a backward clock jump must not produce a spurious, "
                f"proportional wait (got {wait}ms)"
            )
