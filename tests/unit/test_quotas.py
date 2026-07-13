"""Tests for `QuotaContext` functionality."""

import functools
import typing
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import HTTPConnection

from tests.utils import make_connection, requires_throttle_type
from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import QuotaAppliedError, QuotaCancelledError, QuotaError
from traffik.quotas import QuotaContext
from traffik.registry import ThrottleRegistry
from traffik.throttles import Throttle

new_connection = functools.partial(
    make_connection, path="/", query_string=b"", server=("testserver", 80)
)


@pytest.mark.asyncio
@pytest.mark.quota
@requires_throttle_type
class TestQuotaContext:
    async def test_quota_context_bound_mode(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test `QuotaContext` in bound mode (created via throttle.quota())."""
        throttle = throttle_type(
            "test-bound-quota",
            rate="5/s",
            registry=ThrottleRegistry(),
            backend=backend,
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore

        # Create bound quota context
        async with throttle.quota(connection) as quota:
            assert quota.is_bound is True
            assert quota.owner is throttle
            assert quota.active is True
            assert quota.consumed is False
            assert quota.cancelled is False

            # Queue without specifying throttle (uses owner)
            quota.consume(cost=2)
            quota.consume(cost=3)

            assert quota.queued_cost == 5
            assert quota.applied_cost == 0

        # After exit, should be consumed
        assert quota.consumed is True
        assert quota.applied_cost == 5

    async def test_quota_context_unbound_mode(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test `QuotaContext` in unbound mode."""

        throttle1 = throttle_type(
            "test-unbound-1",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        throttle2 = throttle_type(
            "test-unbound-2",
            rate="3/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore

        # Create unbound quota context
        async with QuotaContext(connection) as quota:
            assert quota.is_bound is False
            assert quota.owner is None

            # Must specify throttle
            quota.consume(throttle1, cost=2)
            quota.consume(throttle2, cost=1)

            assert quota.queued_cost == 3
            assert quota.applied_cost == 0

        assert quota.consumed is True
        assert quota.applied_cost == 3

    async def test_quota_context_unbound_requires_throttle(
        self, throttle_type: typing.Type[Throttle[HTTPConnection]]
    ):
        """Test that unbound context requires throttle argument."""
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with QuotaContext(connection) as quota:
            # Should raise ValueError when no throttle is provided
            with pytest.raises(ValueError, match="No throttle specified"):
                quota.consume()

    async def test_quota_context_cost_aggregation(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test automatic cost aggregation for consecutive calls."""
        throttle = throttle_type(
            "test-aggregation",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Consecutive calls with same config should aggregate
            quota.consume(cost=2)
            quota.consume(cost=3)
            quota.consume(cost=1)

            # Should have 1 entry with aggregated cost
            assert len(quota._queue) == 1
            assert quota._queue[0].cost == 6
            assert quota.queued_cost == 6

    async def test_quota_context_cost_aggregation_different_configs(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that different configs prevent cost aggregation."""
        throttle1 = throttle_type(
            "test-no-agg-1",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        throttle2 = throttle_type(
            "test-no-agg-2",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with QuotaContext(connection, apply_on_exit=False) as quota:
            # Different throttles - no aggregation
            quota.consume(throttle1, cost=2)
            quota.consume(throttle2, cost=3)
            assert len(quota._queue) == 2

            # Same throttle but different retry config - no aggregation
            quota.consume(throttle1, cost=1)
            quota.consume(throttle1, cost=1, retry=1)
            assert len(quota._queue) == 4

    async def test_quota_context_manual_apply(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test manual apply() with apply_on_exit=False."""
        throttle = throttle_type(
            "test-manual-apply",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as quota:
            quota.consume(cost=2)
            quota.consume(cost=3)

            assert quota.consumed is False
            assert quota.applied_cost == 0

            # Manual apply
            await quota.apply()

            assert quota.consumed is True
            assert quota.applied_cost == 5

        # Exit should not apply again (idempotent)
        assert quota.consumed is True

    async def test_quota_context_cancel(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test cancelling quota context."""
        throttle = throttle_type(
            "test-cancel",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as quota:
            quota.consume(cost=2)
            quota.consume(cost=3)

            assert quota.queued_cost == 5

            # Cancel
            await quota.cancel()

            assert quota.cancelled is True
            assert quota.queued_cost == 0
            assert len(quota._queue) == 0

        # Should not apply after cancel
        assert quota.consumed is False
        assert quota.applied_cost == 0

    async def test_quota_context_cancel_idempotent(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that cancel() is idempotent."""
        throttle = throttle_type(
            "test-cancel-idempotent",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            quota.consume(cost=2)

            await quota.cancel()
            assert quota.cancelled is True

            # Cancel again should be idempotent
            await quota.cancel()
            assert quota.cancelled is True

    async def test_quota_context_cannot_enqueue_after_cancel(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that enqueueing after cancel raises error."""
        throttle = throttle_type(
            "test-enqueue-after-cancel",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            quota.consume(cost=2)
            await quota.cancel()

            # Try to enqueue after cancel
            with pytest.raises(QuotaCancelledError, match="Cannot queue quota entries"):
                quota.consume(cost=1)

    async def test_quota_context_cannot_enqueue_after_apply(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that enqueueing after apply raises error."""
        throttle = throttle_type(
            "test-enqueue-after-apply",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            quota.consume(cost=2)
            await quota.apply()

            # Try to enqueue after apply
            with pytest.raises(QuotaAppliedError, match="Cannot queue quota entries"):
                quota.consume(cost=1)

    async def test_quota_context_cannot_cancel_after_apply(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that cancelling after apply raises error."""
        throttle = throttle_type(
            "test-cancel-after-apply",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            quota.consume(cost=2)
            await quota.apply()

            # Try to cancel after apply
            with pytest.raises(QuotaAppliedError, match="Cannot cancel a consumed"):
                await quota.cancel()

    async def test_quota_context_apply_on_error_false(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test apply_on_error=False (default) - don't apply on exception."""
        throttle = throttle_type(
            "test-apply-on-error-false",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        quota = None
        try:
            async with throttle.quota(connection, apply_on_error=False) as quota:
                quota.consume(cost=3)
                raise ValueError("Test error")
        except ValueError:
            pass

        # Should not have applied due to exception
        assert quota is not None
        assert quota.consumed is False
        assert quota.applied_cost == 0

    async def test_quota_context_apply_on_error_true(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test apply_on_error=True - apply on any exception."""
        throttle = throttle_type(
            "test-apply-on-error-true",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        quota = None
        try:
            async with throttle.quota(connection, apply_on_error=True) as quota:
                quota.consume(cost=3)
                raise ValueError("Test error")
        except ValueError:
            pass

        # Should have applied despite exception
        assert quota is not None
        assert quota.consumed is True
        assert quota.applied_cost == 3

    async def test_quota_context_apply_on_error_specific(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test apply_on_error with specific exception types."""

        throttle = throttle_type(
            "test-apply-on-error-specific",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        # Apply only on ValueError
        quota = None
        try:
            async with throttle.quota(
                connection, apply_on_error=(ValueError,)
            ) as quota:
                quota.consume(cost=2)
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
                quota2.consume(cost=1)
                raise TypeError("Test error")
        except TypeError:
            pass

        assert quota2 is not None
        assert quota2.consumed is False
        assert quota2.applied_cost == 0

    async def test_quota_context_nested(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test nested quota contexts."""

        throttle = throttle_type(
            "test-nested",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as parent:
            parent.consume(cost=2)

            # Create nested child
            async with parent.nested() as child:
                assert child.is_nested is True
                assert child.parent is parent
                assert child.depth == 1

                child.consume(cost=3)
                child.consume(cost=1)
                assert child.queued_cost == 4

            # Child should merge into parent on exit
            assert child.consumed is True
            assert parent.queued_cost == 6  # 2 + 3 + 1

            await parent.apply()

        assert parent.consumed is True
        assert parent.applied_cost == 6

    async def test_quota_context_nested_multiple_levels(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test multiple levels of nesting."""

        throttle = throttle_type(
            "test-nested-levels",
            rate="20/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as level0:
            level0.consume(cost=1)
            assert level0.depth == 0

            async with level0.nested() as level1:
                level1.consume(cost=2)
                assert level1.depth == 1
                assert level1.parent is level0
                assert (
                    level1.queued_cost == 2
                )  # Level1 should only include its own cost
                assert (
                    level0.queued_cost == 3
                )  # Level0 should reflect for the queued cost of level1

                async with level1.nested() as level2:
                    level2.consume(cost=3)
                    assert level2.depth == 2
                    assert level2.parent is level1
                    assert (
                        level2.queued_cost == 3
                    )  # Level2 should only include its own cost
                    assert (
                        level1.queued_cost == 5
                    )  # Level1 should now include level2 cost
                    assert (
                        level0.queued_cost == 6
                    )  # Level0 should now include level1 and level2 cost

                assert level2.consumed is True
                assert (
                    level2.applied_cost == 0
                )  # Level2 should have not applied yet since yields its entries to level1
                assert (
                    level1.queued_cost == 5
                )  # Level1 should still account for level2 cost
                assert (
                    level1.consumed is False
                )  # Level1 should not be consumed until it exits
                assert (
                    level0.queued_cost == 6
                )  # Level0 should still account for level1 and level2 cost

            # All should merge into level0
            assert level1.consumed is True
            assert (
                level1.applied_cost == 0
            )  # Level1 should have not applied yet since yields its entries to level0
            assert level0.queued_cost == 6  # Level0 should still reflect total cost
            assert (
                level0.consumed is False
            )  # Level0 should not be consumed until it exits

        # After exiting level0, all should be consumed and applied
        assert level0.applied_cost == 6
        assert level0.consumed is True

    async def test_quota_context_nested_cancel(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test cancelling nested quota context."""

        throttle = throttle_type(
            "test-nested-cancel",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as parent:
            parent.consume(cost=2)

            async with parent.nested(apply_on_exit=False) as child:
                child.consume(cost=3)
                await child.cancel()

            # Cancelled child should not merge into parent
            assert child.consumed is False
            assert child.cancelled is True
            assert parent.queued_cost == 2  # Only parent's cost

        assert parent.applied_cost == 2

    async def test_quota_context_check(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test quota availability checking."""
        throttle = throttle_type(
            "test-check",
            rate="5/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as quota:
            # Should have quota available
            has_quota = await quota.check(cost=3)
            assert has_quota is True

            # Consume some quota
            quota.consume(cost=3)

        # Check after consuming
        async with throttle.quota(connection, apply_on_exit=False) as quota2:
            # Should still have 2 remaining
            has_quota = await quota2.check(cost=2)
            assert has_quota is True

            # Asking for more than remaining should return False
            has_quota = await quota2.check(cost=3)
            assert has_quota is False

    async def test_quota_context_stat(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test getting throttle statistics."""

        throttle = throttle_type(
            "test-stat",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as quota:
            # Get initial stat
            stat = await quota.stat()
            assert stat is not None

            # Consume some quota
            quota.consume(cost=4)

        # Get stat after consuming
        async with throttle.quota(connection, apply_on_exit=False) as quota2:
            stat = await quota2.stat()
            assert stat is not None
            assert stat.hits_remaining == 6  # 10 - 4

    async def test_quota_context_retry_basic(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test basic retry functionality."""

        throttle = throttle_type(
            "test-retry",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )

        attempt_count = 0

        # Mock throttle to fail twice then succeed
        hit = throttle.hit

        async def mock_hit(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError("Simulated failure")
            return await hit(*args, **kwargs)

        mock = AsyncMock(side_effect=mock_hit)
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        with patch.object(throttle_type, "hit", mock):
            async with throttle.quota(connection, apply_on_exit=True) as quota:
                quota.consume(cost=2, retry=3, base_delay=0.01)

            # Should have succeeded after retries
            assert quota.consumed is True
            assert attempt_count == 3

    async def test_quota_context_retry_exhausted(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test retry exhaustion when max attempts reached."""

        throttle = throttle_type(
            "test-retry-exhausted",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )

        attempt_count = 0

        # Mock throttle to always fail
        async def mock_hit(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            raise ValueError("Always fails")

        mock = AsyncMock(side_effect=mock_hit)
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        with patch.object(throttle_type, "hit", mock):
            with pytest.raises(ValueError, match="Always fails"):
                async with throttle.quota(connection, apply_on_exit=True) as quota:
                    quota.consume(cost=2, retry=2, base_delay=0.01)

            # Should have tried 3 times (1 + 2 retries)
            assert attempt_count == 3

    async def test_quota_context_retry_on_specific_exception(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test retry_on with specific exception types."""

        throttle = throttle_type(
            "test-retry-on",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )

        # Mock throttle to raise TypeError
        async def mock_hit(*args, **kwargs):
            raise TypeError("Wrong type")

        mock = AsyncMock(side_effect=mock_hit)
        # Should retry on ValueError but not TypeError
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        with (
            patch.object(throttle_type, "hit", mock),
            pytest.raises(TypeError, match="Wrong type"),
        ):
            async with throttle.quota(connection, apply_on_exit=True) as quota:
                quota.consume(cost=2, retry=3, retry_on=(ValueError,), base_delay=0.01)

            assert mock.call_count == 1  # Should not retry since it's TypeError
            assert quota.consumed is False  # Should not consume since it didn't retry
            assert quota.applied_cost == 0  # Should not apply since it didn't retry

    async def test_quota_context_locking(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test quota context locking."""

        throttle = throttle_type(
            "test-locking",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        # Test with lock=True (uses throttle UID)
        async with throttle.quota(connection, lock=True) as quota:
            assert quota._lock_key == f"quota:{throttle.uid}"
            quota.consume(cost=2)

        # Test with custom lock key
        async with throttle.quota(connection, lock="custom_key") as quota2:
            assert quota2._lock_key == "quota:custom_key"
            quota2.consume(cost=1)

        # Test with lock=False (no locking)
        async with throttle.quota(connection, lock=False) as quota3:
            assert quota3._lock_key is None
            quota3.consume(cost=1)

    async def test_quota_context_nested_lock_deadlock_detection(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that nested contexts detect potential deadlocks."""

        throttle = throttle_type(
            "test-deadlock",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, lock=True) as parent:
            # Child using same lock key should raise error
            with pytest.raises(QuotaError, match="same lock key"):
                async with parent.nested(lock=True):
                    pass

    async def test_quota_context_nested_lock_reentrant(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test nested contexts with reentrant lock flag."""

        throttle = throttle_type(
            "test-reentrant",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with (
            throttle.quota(connection, lock=True) as parent,
            parent.nested(lock=True, lock_config={"reentrant": True}) as child,
        ):  # Should work with reentrant=True
            child.consume(cost=2)

    async def test_quota_context_empty_apply(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test applying empty quota context."""

        throttle = throttle_type(
            "test-empty",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        # Empty context should apply without error
        async with throttle.quota(connection) as quota:
            pass

        assert quota.consumed is True
        assert quota.applied_cost == 0
        assert quota.queued_cost == 0

    async def test_quota_context_properties(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test quota context properties."""

        throttle = throttle_type(
            "test-properties",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Initial state
            assert quota.active is True
            assert quota.consumed is False
            assert quota.cancelled is False
            assert quota.is_bound is True
            assert quota.is_nested is False
            assert quota.depth == 0

            quota.consume(cost=3)
            assert quota.queued_cost == 3
            assert quota.applied_cost == 0

            await quota.apply()
            assert quota.active is False
            assert quota.consumed is True
            assert quota.applied_cost == 3

    async def test_quota_context_aliases(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test quota context method aliases."""

        throttle = throttle_type(
            "test-aliases",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as quota:
            # Test __call__ alias for consume
            quota(cost=2)
            assert quota.queued_cost == 2

            # Test discard alias for cancel
            await quota.discard()
            assert quota.cancelled is True

    async def test_quota_context_nested_alias(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test that quota() is an alias for nested()."""

        throttle = throttle_type(
            "test-nested-alias",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=False) as parent:
            # Use quota() alias
            async with parent.quota() as child:
                assert child.is_nested is True
                assert child.parent is parent
                child.consume(cost=2)

            assert parent.queued_cost == 2

    async def test_quota_context_default_context_merging(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test default context merging."""

        throttle = throttle_type(
            "test-context-merge",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )

        default_ctx = {"key1": "value1", "key2": "value2"}
        override_ctx = {"key2": "override", "key3": "value3"}
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(
            connection, context=default_ctx, apply_on_exit=False
        ) as quota:
            # Enqueue with override context
            quota.consume(cost=1, context=override_ctx)

            # Check that contexts are merged correctly
            entry = quota._queue[0]
            assert entry.context is not None
            assert entry.context["key1"] == "value1"  # From default
            assert entry.context["key2"] == "override"  # Overridden
            assert entry.context["key3"] == "value3"  # From override

    async def test_quota_context_multiple_children(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test parent with multiple child contexts."""

        throttle = throttle_type(
            "test-multi-children",
            rate="20/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection, apply_on_exit=True) as parent:
            parent.consume(cost=1)

            # Create and complete first child
            async with parent.nested() as child1:
                child1.consume(cost=2)

            # Create and complete second child
            async with parent.nested() as child2:
                child2.consume(cost=3)

            # Both children should merge into parent
            assert parent.queued_cost == 6  # 1 + 2 + 3

        assert parent.applied_cost == 6

    async def test_quota_context_zero_cost(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ):
        """Test handling of zero cost entries."""

        throttle = throttle_type(
            "test-zero-cost",
            rate="10/s",
            backend=backend,
            registry=ThrottleRegistry(),
        )
        connection = new_connection(throttle_type.connection_type)  # type: ignore
        async with throttle.quota(connection) as quota:
            quota.consume(cost=0)
            quota.consume(cost=0)

        # Should still work with zero costs
        assert quota.consumed is True
        assert quota.applied_cost == 0
