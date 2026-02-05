"""Tests for `QuotaContext` functionality."""

import pytest
from starlette.requests import Request

from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import (
    QuotaAppliedError,
    QuotaCancelledError,
    QuotaError,
)
from traffik.quotas import (
    ConstantBackoff,
    ExponentialBackoff,
    LinearBackoff,
    LogarithmicBackoff,
    QuotaContext,
)
from traffik.throttles import HTTPThrottle


@pytest.fixture(scope="function")
def connection() -> Request:
    """Create a test connection for quota context tests."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_bound_mode(backend: InMemoryBackend, connection: Request):
    """Test `QuotaContext` in bound mode (created via throttle.quota())."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-bound-quota",
            rate="5/s",
            backend=backend,
        )

        # Create bound quota context
        async with throttle.quota(connection) as quota:
            assert quota.is_bound is True
            assert quota.owner is throttle
            assert quota.active is True
            assert quota.consumed is False
            assert quota.cancelled is False

            # Queue without specifying throttle (uses owner)
            await quota(cost=2)
            await quota(cost=3)

            assert quota.queued_cost == 5
            assert quota.applied_cost == 0

        # After exit, should be consumed
        assert quota.consumed is True
        assert quota.applied_cost == 5


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_unbound_mode(
    backend: InMemoryBackend, connection: Request
):
    """Test `QuotaContext` in unbound mode."""
    async with backend(close_on_exit=True):
        throttle1 = HTTPThrottle(
            "test-unbound-1",
            rate="5/s",
            backend=backend,
        )
        throttle2 = HTTPThrottle(
            "test-unbound-2",
            rate="3/s",
            backend=backend,
        )

        # Create unbound quota context
        async with QuotaContext(connection) as quota:
            assert quota.is_bound is False
            assert quota.owner is None

            # Must specify throttle
            await quota(throttle1, cost=2)
            await quota(throttle2, cost=1)

            assert quota.queued_cost == 3


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_unbound_requires_throttle(
    backend: InMemoryBackend, connection: Request
):
    """Test that unbound context requires throttle argument."""
    async with backend(close_on_exit=True):
        async with QuotaContext(connection) as quota:
            # Should raise ValueError when no throttle is provided
            with pytest.raises(ValueError, match="No throttle specified"):
                await quota()


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cost_aggregation(
    backend: InMemoryBackend, connection: Request
):
    """Test automatic cost aggregation for consecutive calls."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-aggregation",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Consecutive calls with same config should aggregate
            await quota(cost=2)
            await quota(cost=3)
            await quota(cost=1)

            # Should have 1 entry with aggregated cost
            assert len(quota._queue) == 1
            assert quota._queue[0].cost == 6
            assert quota.queued_cost == 6


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cost_aggregation_different_configs(
    backend: InMemoryBackend, connection: Request
):
    """Test that different configs prevent cost aggregation."""
    async with backend(close_on_exit=True):
        throttle1 = HTTPThrottle(
            "test-no-agg-1",
            rate="10/s",
            backend=backend,
        )
        throttle2 = HTTPThrottle(
            "test-no-agg-2",
            rate="5/s",
            backend=backend,
        )

        async with QuotaContext(connection, apply_on_exit=False) as quota:
            # Different throttles - no aggregation
            await quota(throttle1, cost=2)
            await quota(throttle2, cost=3)
            assert len(quota._queue) == 2

            # Same throttle but different retry config - no aggregation
            await quota(throttle1, cost=1)
            await quota(throttle1, cost=1, retry=1)
            assert len(quota._queue) == 4


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_manual_apply(
    backend: InMemoryBackend, connection: Request
):
    """Test manual apply() with apply_on_exit=False."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-manual-apply",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)
            await quota(cost=3)

            assert quota.consumed is False
            assert quota.applied_cost == 0

            # Manual apply
            await quota.apply()

            assert quota.consumed is True
            assert quota.applied_cost == 5

        # Exit should not apply again (idempotent)
        assert quota.consumed is True


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cancel(backend: InMemoryBackend, connection: Request):
    """Test cancelling quota context."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-cancel",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)
            await quota(cost=3)

            assert quota.queued_cost == 5

            # Cancel
            await quota.cancel()

            assert quota.cancelled is True
            assert quota.queued_cost == 0
            assert len(quota._queue) == 0

        # Should not apply after cancel
        assert quota.consumed is False
        assert quota.applied_cost == 0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cancel_idempotent(
    backend: InMemoryBackend, connection: Request
):
    """Test that cancel() is idempotent."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-cancel-idempotent",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)

            await quota.cancel()
            assert quota.cancelled is True

            # Cancel again - should be idempotent
            await quota.cancel()
            assert quota.cancelled is True


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cannot_enqueue_after_cancel(
    backend: InMemoryBackend, connection: Request
):
    """Test that enqueueing after cancel raises error."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-enqueue-after-cancel",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)
            await quota.cancel()

            # Try to enqueue after cancel
            with pytest.raises(QuotaCancelledError, match="Cannot queue quota entries"):
                await quota(cost=1)


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cannot_enqueue_after_apply(
    backend: InMemoryBackend, connection: Request
):
    """Test that enqueueing after apply raises error."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-enqueue-after-apply",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)
            await quota.apply()

            # Try to enqueue after apply
            with pytest.raises(QuotaAppliedError, match="Cannot queue quota entries"):
                await quota(cost=1)


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_cannot_cancel_after_apply(
    backend: InMemoryBackend, connection: Request
):
    """Test that cancelling after apply raises error."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-cancel-after-apply",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            await quota(cost=2)
            await quota.apply()

            # Try to cancel after apply
            with pytest.raises(QuotaAppliedError, match="Cannot cancel a consumed"):
                await quota.cancel()


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_apply_on_error_false(
    backend: InMemoryBackend, connection: Request
):
    """Test apply_on_error=False (default) - don't apply on exception."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-apply-on-error-false",
            rate="5/s",
            backend=backend,
        )

        quota = None
        try:
            async with throttle.quota(connection, apply_on_error=False) as quota:
                await quota(cost=3)
                raise ValueError("Test error")
        except ValueError:
            pass

        # Should not have applied due to exception
        assert quota is not None
        assert quota.consumed is False
        assert quota.applied_cost == 0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_apply_on_error_true(
    backend: InMemoryBackend, connection: Request
):
    """Test apply_on_error=True - apply on any exception."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-apply-on-error-true",
            rate="5/s",
            backend=backend,
        )

        quota = None
        try:
            async with throttle.quota(connection, apply_on_error=True) as quota:
                await quota(cost=3)
                raise ValueError("Test error")
        except ValueError:
            pass

        # Should have applied despite exception
        assert quota is not None
        assert quota.consumed is True
        assert quota.applied_cost == 3


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_apply_on_error_specific(
    backend: InMemoryBackend, connection: Request
):
    """Test apply_on_error with specific exception types."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-apply-on-error-specific",
            rate="5/s",
            backend=backend,
        )

        # Apply only on ValueError
        quota = None
        try:
            async with throttle.quota(
                connection, apply_on_error=(ValueError,)
            ) as quota:
                await quota(cost=2)
                raise ValueError("Test error")
        except ValueError:
            pass

        assert quota is not None
        assert quota.consumed is True
        assert quota.applied_cost == 2

        # Don't apply on TypeError
        quota2 = None
        try:
            async with throttle.quota(
                connection, apply_on_error=(ValueError,)
            ) as quota2:
                await quota2(cost=1)
                raise TypeError("Test error")
        except TypeError:
            pass

        assert quota2 is not None
        assert quota2.consumed is False
        assert quota2.applied_cost == 0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested(backend: InMemoryBackend, connection: Request):
    """Test nested quota contexts."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-nested",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as parent:
            await parent(cost=2)

            # Create nested child
            async with parent.nested() as child:
                assert child.is_nested is True
                assert child.parent is parent
                assert child.depth == 1

                await child(cost=3)
                await child(cost=1)

            # Child should merge into parent on exit
            assert child.consumed is True
            assert parent.queued_cost == 6  # 2 + 3 + 1

            await parent.apply()

        assert parent.consumed is True
        assert parent.applied_cost == 6


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested_multiple_levels(
    backend: InMemoryBackend, connection: Request
):
    """Test multiple levels of nesting."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-nested-levels",
            rate="20/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as level0:
            await level0(cost=1)
            assert level0.depth == 0

            async with level0.nested() as level1:
                await level1(cost=2)
                assert level1.depth == 1

                async with level1.nested() as level2:
                    await level2(cost=3)
                    assert level2.depth == 2

            # All should merge into level0
            assert level0.queued_cost == 6
            await level0.apply()

        assert level0.applied_cost == 6


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested_cancel(
    backend: InMemoryBackend, connection: Request
):
    """Test cancelling nested quota context."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-nested-cancel",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as parent:
            await parent(cost=2)

            async with parent.nested(apply_on_exit=False) as child:
                await child(cost=3)
                await child.cancel()

            # Cancelled child should not merge into parent
            assert child.cancelled is True
            assert parent.queued_cost == 2  # Only parent's cost

            await parent.apply()

        assert parent.applied_cost == 2


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_check(backend: InMemoryBackend, connection: Request):
    """Test quota availability checking."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-check",
            rate="5/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Should have quota available
            has_quota = await quota.check(cost=3)
            assert has_quota is True

            # Consume some quota
            await quota(cost=3)
            await quota.apply()

        # Check after consuming
        async with throttle.quota(connection, apply_on_exit=False) as quota2:
            # Should still have 2 remaining
            has_quota = await quota2.check(cost=2)
            assert has_quota is True

            # Asking for more than remaining should return False
            has_quota = await quota2.check(cost=3)
            assert has_quota is False


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_stat(backend: InMemoryBackend, connection: Request):
    """Test getting throttle statistics."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Get initial stat
            stat = await quota.stat()
            assert stat is not None

            # Consume some quota
            await quota(cost=4)
            await quota.apply()

        # Get stat after consuming
        async with throttle.quota(connection) as quota2:
            stat = await quota2.stat()
            assert stat is not None
            assert stat.hits_remaining == 6  # 10 - 4


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_retry_basic(backend: InMemoryBackend, connection: Request):
    """Test basic retry functionality."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-retry",
            rate="10/s",
            backend=backend,
        )

        attempt_count = 0

        # Mock throttle to fail twice then succeed
        original_call = throttle.__call__

        async def mock_throttle_call(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError("Simulated failure")
            return await original_call(*args, **kwargs)

        throttle.__call__ = mock_throttle_call  # type: ignore[assignment]

        try:
            async with throttle.quota(connection) as quota:
                await quota(cost=2, retry=3, base_delay=0.01)

            # Should have succeeded after retries
            assert quota.consumed is True
            assert attempt_count == 3
        finally:
            throttle.__call__ = original_call


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_retry_exhausted(
    backend: InMemoryBackend, connection: Request
):
    """Test retry exhaustion when max attempts reached."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-retry-exhausted",
            rate="10/s",
            backend=backend,
        )

        attempt_count = 0

        # Mock throttle to always fail
        original_call = throttle.__call__

        async def mock_throttle_call(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            raise ValueError("Always fails")

        throttle.__call__ = mock_throttle_call  # type: ignore[assignment]

        try:
            with pytest.raises(ValueError, match="Always fails"):
                async with throttle.quota(connection) as quota:
                    await quota(cost=2, retry=2, base_delay=0.01)

            # Should have tried 3 times (1 + 2 retries)
            assert attempt_count == 3
        finally:
            throttle.__call__ = original_call


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_retry_on_specific_exception(
    backend: InMemoryBackend, connection: Request
):
    """Test retry_on with specific exception types."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-retry-on",
            rate="10/s",
            backend=backend,
        )

        # Mock throttle to raise TypeError
        original_call = throttle.__call__

        async def mock_throttle_call(*args, **kwargs):
            raise TypeError("Wrong type")

        throttle.__call__ = mock_throttle_call  # type: ignore[assignment]

        try:
            # Should retry on ValueError but not TypeError
            with pytest.raises(TypeError, match="Wrong type"):
                async with throttle.quota(connection) as quota:
                    await quota(
                        cost=2, retry=3, retry_on=(ValueError,), base_delay=0.01
                    )
        finally:
            throttle.__call__ = original_call  # type: ignore[assignment]


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_constant_backoff(
    backend: InMemoryBackend, connection: Request
):
    """Test ConstantBackoff strategy."""
    base_delay = 0.5

    # Test that delay remains constant
    delay1 = ConstantBackoff(1, base_delay)
    delay2 = ConstantBackoff(2, base_delay)
    delay3 = ConstantBackoff(3, base_delay)

    assert delay1 == base_delay
    assert delay2 == base_delay
    assert delay3 == base_delay


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_linear_backoff(
    backend: InMemoryBackend, connection: Request
):
    """Test LinearBackoff strategy."""
    backoff = LinearBackoff(increment=1.0)
    base_delay = 0.5

    # Delay should increase linearly
    delay1 = backoff(1, base_delay)  # 0.5 + 0*1 = 0.5
    delay2 = backoff(2, base_delay)  # 0.5 + 1*1 = 1.5
    delay3 = backoff(3, base_delay)  # 0.5 + 2*1 = 2.5

    assert delay1 == 0.5
    assert delay2 == 1.5
    assert delay3 == 2.5


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_exponential_backoff(
    backend: InMemoryBackend, connection: Request
):
    """Test ExponentialBackoff strategy."""
    backoff = ExponentialBackoff(multiplier=2.0)
    base_delay = 1.0

    # Delay should double each time
    delay1 = backoff(1, base_delay)  # 1 * 2^0 = 1
    delay2 = backoff(2, base_delay)  # 1 * 2^1 = 2
    delay3 = backoff(3, base_delay)  # 1 * 2^2 = 4

    assert delay1 == 1.0
    assert delay2 == 2.0
    assert delay3 == 4.0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_exponential_backoff_with_max(
    backend: InMemoryBackend, connection: Request
):
    """Test ExponentialBackoff with max_delay cap."""
    backoff = ExponentialBackoff(multiplier=2.0, max_delay=3.0)
    base_delay = 1.0

    delay1 = backoff(1, base_delay)  # 1
    delay2 = backoff(2, base_delay)  # 2
    delay3 = backoff(3, base_delay)  # Should be capped at 3.0
    delay4 = backoff(4, base_delay)  # Should be capped at 3.0

    assert delay1 == 1.0
    assert delay2 == 2.0
    assert delay3 == 3.0
    assert delay4 == 3.0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_logarithmic_backoff(
    backend: InMemoryBackend, connection: Request
):
    """Test LogarithmicBackoff strategy."""
    backoff = LogarithmicBackoff(base=2.0)
    base_delay = 1.0

    # Delay should increase logarithmically
    delay1 = backoff(1, base_delay)  # 1 * log2(2) = 1.0
    delay2 = backoff(2, base_delay)  # 1 * log2(3) ≈ 1.585
    delay3 = backoff(3, base_delay)  # 1 * log2(4) = 2.0

    assert delay1 == 1.0
    assert 1.5 < delay2 < 1.6
    assert delay3 == 2.0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_locking(backend: InMemoryBackend, connection: Request):
    """Test quota context locking."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-locking",
            rate="10/s",
            backend=backend,
        )

        # Test with lock=True (uses throttle UID)
        async with throttle.quota(connection, lock=True) as quota:
            assert quota._lock_key == f"quota:{throttle.uid}"
            await quota(cost=2)

        # Test with custom lock key
        async with throttle.quota(connection, lock="custom_key") as quota2:
            assert quota2._lock_key == "quota:custom_key"
            await quota2(cost=1)

        # Test with lock=False (no locking)
        async with throttle.quota(connection, lock=False) as quota3:
            assert quota3._lock_key is None
            await quota3(cost=1)


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested_lock_deadlock_detection(
    backend: InMemoryBackend, connection: Request
):
    """Test that nested contexts detect potential deadlocks."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-deadlock",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, lock=True) as parent:
            # Child using same lock key should raise error
            with pytest.raises(QuotaError, match="same lock key"):
                async with parent.nested(lock=True):
                    pass


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested_lock_reentrant(
    backend: InMemoryBackend, connection: Request
):
    """Test nested contexts with reentrant lock flag."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-reentrant",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, lock=True) as parent:
            # Should work with reentrant_lock=True
            async with parent.nested(lock=True, reentrant_lock=True) as child:
                await child(cost=2)


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_empty_apply(backend: InMemoryBackend, connection: Request):
    """Test applying empty quota context."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-empty",
            rate="10/s",
            backend=backend,
        )

        # Empty context should apply without error
        async with throttle.quota(connection) as quota:
            pass

        assert quota.consumed is True
        assert quota.applied_cost == 0
        assert quota.queued_cost == 0


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_properties(backend: InMemoryBackend, connection: Request):
    """Test quota context properties."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-properties",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Initial state
            assert quota.active is True
            assert quota.consumed is False
            assert quota.cancelled is False
            assert quota.is_bound is True
            assert quota.is_nested is False
            assert quota.depth == 0

            await quota(cost=3)
            assert quota.queued_cost == 3
            assert quota.applied_cost == 0

            await quota.apply()
            assert quota.active is False
            assert quota.consumed is True
            assert quota.applied_cost == 3


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_aliases(backend: InMemoryBackend, connection: Request):
    """Test quota context method aliases."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-aliases",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Test consume alias for __call__
            await quota.consume(cost=2)
            assert quota.queued_cost == 2

            # Test discard alias for cancel
            await quota.discard()
            assert quota.cancelled is True


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_nested_alias(
    backend: InMemoryBackend, connection: Request
):
    """Test that quota() is an alias for nested()."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-nested-alias",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as parent:
            # Use quota() alias
            async with parent.quota() as child:
                assert child.is_nested is True
                assert child.parent is parent
                await child(cost=2)

            assert parent.queued_cost == 2


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_default_context_merging(
    backend: InMemoryBackend, connection: Request
):
    """Test default context merging."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-context-merge",
            rate="10/s",
            backend=backend,
        )

        default_ctx = {"key1": "value1", "key2": "value2"}
        override_ctx = {"key2": "override", "key3": "value3"}

        async with throttle.quota(
            connection, context=default_ctx, apply_on_exit=False
        ) as quota:
            # Enqueue with override context
            await quota(cost=1, context=override_ctx)

            # Check that contexts are merged correctly
            entry = quota._queue[0]
            assert entry.context is not None
            assert entry.context["key1"] == "value1"  # From default
            assert entry.context["key2"] == "override"  # Overridden
            assert entry.context["key3"] == "value3"  # From override


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_multiple_children(
    backend: InMemoryBackend, connection: Request
):
    """Test parent with multiple child contexts."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-multi-children",
            rate="20/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as parent:
            await parent(cost=1)

            # Create and complete first child
            async with parent.nested() as child1:
                await child1(cost=2)

            # Create and complete second child
            async with parent.nested() as child2:
                await child2(cost=3)

            # Both children should merge into parent
            assert parent.queued_cost == 6  # 1 + 2 + 3
            await parent.apply()

        assert parent.applied_cost == 6


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_repr(backend: InMemoryBackend, connection: Request):
    """Test quota context __repr__."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-repr",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection, apply_on_exit=False) as quota:
            repr_str = repr(quota)
            assert "QuotaContext" in repr_str
            assert "test-repr" in repr_str
            assert "state=active" in repr_str
            assert "nested=False" in repr_str

            await quota(cost=2)
            repr_str = repr(quota)
            assert "queued=1" in repr_str

            await quota.apply()
            repr_str = repr(quota)
            assert "state=consumed" in repr_str


@pytest.mark.asyncio
@pytest.mark.quota
async def test_quota_context_zero_cost(backend: InMemoryBackend, connection: Request):
    """Test handling of zero cost entries."""
    async with backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-zero-cost",
            rate="10/s",
            backend=backend,
        )

        async with throttle.quota(connection) as quota:
            await quota(cost=0)
            await quota(cost=0)

        # Should still work with zero costs
        assert quota.consumed is True
        assert quota.applied_cost == 0
