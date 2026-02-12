"""Quota context manager for deferred throttle consumption."""

import asyncio
import inspect
import typing
from collections import deque
from contextlib import AsyncExitStack
from types import TracebackType

from starlette.exceptions import HTTPException
from typing_extensions import Self

from traffik.backoff import (
    DEFAULT_BACKOFF,
    ConstantBackoff,
    ExponentialBackoff,
    LinearBackoff,
    LogarithmicBackoff,
)
from traffik.exceptions import (
    QuotaAppliedError,
    QuotaCancelledError,
    QuotaError,
)
from traffik.throttles import Throttle
from traffik.types import (
    ApplyOnError,
    BackoffStrategy,
    HTTPConnectionT,
    LockConfig,
    RetryOn,
    StrategyStat,
)

__all__ = ["QuotaContext"]

_NON_RETRYABLE_EXCEPTIONS: typing.Tuple[typing.Type[BaseException], ...] = (
    asyncio.CancelledError,
    HTTPException,
    KeyboardInterrupt,
    SystemExit,
)
"""Tuple of exceptions that should not be retried. They are considered non-retryable signals."""

# Aliases for backwards compatibility
ConstantBackoff = ConstantBackoff
LinearBackoff = LinearBackoff
LogarithmicBackoff = LogarithmicBackoff
ExponentialBackoff = ExponentialBackoff


def _resolve_lock_key(
    lock: typing.Union[bool, str, None],
    owner: typing.Optional["Throttle[typing.Any]"] = None,
) -> typing.Optional[str]:
    """
    Resolve a lock configuration into a lock key string.

    :param lock: Lock configuration. Can be:
        - `None` or `True`: Use owner throttle's UID as key (if bound)
        - `False`: No locking (returns None)
        - `str`: Use the string as lock key

    :param owner: The bound throttle (if any) to use when lock is None/True.
    :return: The resolved lock key string, or None if locking is disabled.
    """
    if lock is None or lock is True:
        return f"quota:{owner.uid}" if owner is not None else None
    elif lock is False:
        return None
    return f"quota:{lock}"


@typing.final
class _QuotaEntry(typing.Generic[HTTPConnectionT]):
    """A quota entry queued for deferred consumption."""

    __slots__ = (
        "throttle",
        "cost",
        "resolved_cost",
        "context",
        "retry",
        "retry_on",
        "backoff",
        "base_delay",
        "max_attempts",
        "_retry_type",
    )

    def __init__(
        self,
        throttle: Throttle[HTTPConnectionT],
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Dict[str, typing.Any]] = None,
        retry: int = 0,
        retry_on: typing.Optional[RetryOn] = None,
        backoff: typing.Optional[BackoffStrategy] = None,
        base_delay: float = 1.0,
    ) -> None:
        self.throttle = throttle
        self.cost = cost
        self.context = context
        self.retry = retry
        self.retry_on = retry_on
        self.backoff = backoff or DEFAULT_BACKOFF
        self.base_delay = base_delay

        # Precompute values for fast apply
        self.max_attempts = retry + 1
        # For cost, if the throttle uses a cost function, we can't determine the actual cost until apply time,
        # so we default to 1 for retry logic purposes.
        # If it doesn't use a cost function, we can use the throttle's defined cost.
        self.resolved_cost: int = (
            cost
            if cost is not None
            else (throttle.cost if not throttle._uses_cost_func else 1)  # type: ignore[assignment]
        )
        # Determine retry_on type for fast checks during retries
        if retry_on is None:
            self._retry_type: typing.Optional[str] = None
        elif isinstance(retry_on, tuple) and all(isinstance(t, type) for t in retry_on):
            self._retry_type = "exception_type"
        elif isinstance(retry_on, type):
            self._retry_type = "exception_type"
        elif callable(retry_on) and inspect.iscoroutinefunction(retry_on):
            self._retry_type = "coroutine"
        elif callable(retry_on):
            self._retry_type = "callable"
        else:
            self._retry_type = None

    async def resolve_cost(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Dict[str, typing.Any]],
    ) -> int:
        """Resolve the actual cost of the throttle, calling cost function if needed."""
        if self.cost is not None:
            return self.cost

        throttle = self.throttle
        if throttle._uses_cost_func:
            return await throttle.cost(connection, context)  # type: ignore[operator]

        return throttle.cost  # type: ignore[return-value]


class QuotaContext(typing.Generic[HTTPConnectionT]):
    """
    A quota context manager for deferred throttle consumption.

    Quota entries are queued within the context and consumed together when
    the context is applied (either explicitly via `apply()` or implicitly
    on successful context exit).

    For strongest guarantees, throttles should be isolated per request or protected by locks.
    When throttles are shared across concurrent contexts, quota enforcement
    is best-effort and may occur after work completes.

    **Quota Context Semantics:**

    - Queued together (deferred consumption on successful exit or manual apply)
    - Consumed in order (predictable)
    - Consumed atomically with respect to locking (isolated from other requests)
    - No roll back on failure (partial consumption is acceptable)
    - Optionally skipped on errors (`apply_on_error`)
    - Automatically aggregates consecutive calls with same throttle/config

    Think of it as: "Consume multiple quota entries together, under a lock, with
    automatic cost aggregation for efficiency, without all-or-nothing guarantees."

    Can be used in two modes:

    1. **Bound mode** (usually created via `throttle.quota()`): Context is tied
       to a specific throttle. Calling `quota()` without arguments uses the owner throttle.

    2. **Unbound mode** (created directly): Context is not tied to any throttle.
       You must always specify a throttle when calling `quota(throttle)`.

    What's supported:

    - Deferred consumption: Quotas consumed only on `apply()`
    - Cost aggregation: Multiple calls with same config aggregate costs automatically
    - Conditional consumption: Skip quotas on errors (configurable)
    - Nested contexts: Parent-child relationships with cascading applies
    - Retry with backoff: Configurable retry logic per quota entry
    - Context-wide locking: Lock acquired on entry, released on exit

    Note:
        When locking is enabled, the lock is held for the entire duration of the
        context. Keep operations within the context fast to avoid blocking other
        requests waiting for the same lock.

    Example (bound mode with cost aggregation):

    ```python

    # Before entering the quota context, you may want to check if the
    # expected quota usage is available. This is a best-effort check.
    if not await throttle.check(conn, cost=5):
        raise ConnectionThrottled()

    async with throttle.quota(conn, lock=True) as quota:
        await quota(cost=2)          # Entry 1: cost=2
        await quota(cost=3)          # Aggregated into Entry 1: cost=5
        await quota(other_throttle)  # Entry 2: different throttle
    ```

    Example (unbound mode):

    ```python

    async with QuotaContext(conn, lock=True) as quota:
        await quota(throttle1, cost=2)
        await quota(throttle2)
    ```
    """

    __slots__ = (
        "connection",
        "_default_context",
        "owner",
        "apply_on_error",
        "apply_on_exit",
        "lock_config",
        "parent",
        "_lock_key",
        "_queue",
        "_children",
        "_consumed",
        "_cancelled",
        "_entered",
        "_bound",
        "_nested",
        "_queued_cost",
        "_applied_cost",
        "_exit_stack",
    )

    def __init__(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        owner: typing.Optional[Throttle[HTTPConnectionT]] = None,
        apply_on_error: ApplyOnError = False,
        apply_on_exit: bool = True,
        lock: typing.Union[bool, str, None] = None,
        lock_config: typing.Optional[LockConfig] = None,
        parent: typing.Optional["QuotaContext[HTTPConnectionT]"] = None,
    ) -> None:
        """
        Initialize a quota context.

        :param connection: The HTTP connection to throttle.
        :param context: Default context for all queued quota entries.
        :param owner: The throttle this context is bound to (if any).
            When set, calling the context without a throttle argument will use the owner.
        :param apply_on_error: Whether to consume quotas even on exceptions.
            - `False` (default): Don't consume on any exception
            - `True`: Consume on all exceptions
            - `tuple[Exception, ...]`: Consume only for these exception types
        :param apply_on_exit: Whether to auto-consume on successful exit.
        :param lock: Controls locking to prevent race conditions.
            - `None` (default): Use throttle UID as lock key if bound, else disable locking
            - `True`: Same as None (use throttle UID if bound, else disable)
            - `False`: Explicitly disable locking
            - `str`: Use the provided string as the lock key

            When enabled, the lock is acquired on context entry and released on exit.
            This ensures the entire quota context (including user code) is atomic with
            respect to other quota contexts using the same lock key.

            **Important:** Keep operations within the context fast since the lock
            is held for the entire duration.
        :param lock_config: Configuration for lock acquisition. See `LockConfig`.
        :param parent: Parent quota context for nesting.
        """
        self.connection = connection
        # Never modify `_default_context` after initialization. It's unsafe.
        self._default_context = dict(context or {})
        self.owner = owner
        self.apply_on_error = apply_on_error
        self.apply_on_exit = apply_on_exit
        self.lock_config = lock_config or {}
        self.parent = parent

        self._lock_key = _resolve_lock_key(lock, owner)
        self._queue: typing.Deque[_QuotaEntry[HTTPConnectionT]] = deque()
        self._children: typing.List["QuotaContext[HTTPConnectionT]"] = []
        self._consumed = False
        self._cancelled = False
        self._entered = False
        self._bound = owner is not None
        self._nested = parent is not None

        self._queued_cost = 0
        self._applied_cost = 0

        # For managing context-wide lock
        self._exit_stack: typing.Optional[AsyncExitStack] = None
        if parent is not None:
            parent._children.append(self)

    @property
    def consumed(self) -> bool:
        """Whether this quota context has been consumed."""
        return self._consumed

    @property
    def cancelled(self) -> bool:
        """Whether this quota context has been cancelled."""
        return self._cancelled

    @property
    def active(self) -> bool:
        """Whether this quota context is still active (not consumed or cancelled)."""
        return not (self._consumed or self._cancelled)

    @property
    def is_bound(self) -> bool:
        """Whether this quota context is bound to an owner throttle."""
        return self._bound

    @property
    def is_nested(self) -> bool:
        """Whether this quota context has a parent. Is this context nested?"""
        return self._nested

    @property
    def queued_cost(self) -> int:
        """
        **Estimated** cost of all queued quota entries (including children).

        Note: This is an estimate since some throttles may use cost functions.
        In those cases, we assume the throttle's defined cost or 1 as a fallback.
        """
        child_cost = sum(child.queued_cost for child in self._children)
        return self._queued_cost + child_cost

    @property
    def applied_cost(self) -> int:
        """Cost that has been consumed."""
        return self._applied_cost

    @property
    def depth(self) -> int:
        """How deep this quota context is nested."""
        depth = 0
        ctx: typing.Optional[QuotaContext[HTTPConnectionT]] = self
        while ctx is not None:
            ctx = ctx.parent
            depth += 1
        return depth - 1

    async def __aenter__(self) -> Self:
        self._entered = True

        # Acquire lock for the entire context if configured
        if self._lock_key and self.owner and not self._nested:
            backend = self.owner.get_backend(self.connection)
            self._exit_stack = AsyncExitStack()
            lock_ctx = await backend.lock(self._lock_key, **self.lock_config)
            await self._exit_stack.enter_async_context(lock_ctx)

        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> typing.Optional[bool]:
        if not self._entered or not self.active:
            return None

        exit_exc: typing.Optional[BaseException] = None
        try:
            should_apply = False
            if exc_type is None:
                # No exception occurred, only apply if configured to do so
                should_apply = self.apply_on_exit
            else:
                # Exception occurred, check `apply_on_error` configuration
                if self.apply_on_error is True:
                    should_apply = True
                elif self.apply_on_error is not False:
                    should_apply = issubclass(exc_type, self.apply_on_error)
                # Else, `apply_on_error=False`, so don't apply

            if should_apply:
                if self.is_nested:
                    self._merge_into_parent(mark_as_consumed=True)
                else:
                    await self.apply()
            return None

        except BaseException as exc:
            # Store exception to raise after lock release
            exit_exc = exc
        finally:
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None

        # Raise stored exception after lock is released. This ensures we don't
        # hold the lock while propagating exceptions.
        if exit_exc is not None:
            raise exit_exc

    def _merge_into_parent(self, mark_as_consumed: bool = True) -> None:
        """
        Merge this batch's queue into the parent's queue.

        :param mark_as_consumed: Whether to mark this batch as applied once merged.
        """
        if self.parent is None:
            return

        parent = self.parent
        parent._queue.extend(self._queue)
        parent._queued_cost += self._queued_cost
        self._consumed = mark_as_consumed

        # Clean up parent reference to prevent memory leaks
        if self in parent._children:
            parent._children.remove(self)

    async def __call__(
        self,
        throttle: typing.Optional["Throttle[HTTPConnectionT]"] = None,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        retry: int = 0,
        retry_on: typing.Optional[RetryOn] = None,
        backoff: typing.Optional[BackoffStrategy] = None,
        base_delay: float = 1.0,
    ) -> Self:
        """
        Queue a quota entry for deferred consumption.

        Automatically aggregates costs for consecutive calls with the same
        throttle and retry configuration for efficiency.

        :param throttle: The throttle to queue. If not provided, uses the owner
            throttle (only valid for bound contexts).
        :param cost: Override cost for this quota entry.
        :param context: Override context for this quota entry.
        :param retry: Number of retry attempts if an error occurs.
        :param retry_on: Conditions for retrying. Can be:
            - A tuple of exception types to retry on
            - A single exception type to retry on
            - A callable that takes an exception info and returns True if it should be retried
            If None, retries on any exception when retry > 0.
        :param backoff: Backoff strategy for retries.
        :param base_delay: Base delay in seconds between retries.
        :return: Self for chaining.

        Example (bound context with automatic cost aggregation):

        ```python
        async with throttle.quota(conn) as quota:
            await quota(cost=2)        # Entry 1: cost=2
            await quota(cost=3)        # Aggregated into Entry 1: cost=5
            await quota(other, cost=1) # Entry 2: different throttle
        ```

        Example (with retry on specific exceptions):

        ```python
        async with QuotaContext(conn) as quota:
            await quota(throttle1, cost=2, retry=3, retry_on=(TimeoutError,))
            await quota(throttle2)
        ```
        """
        return self._enqueue(
            throttle=throttle,
            cost=cost,
            context=context,
            retry=retry,
            retry_on=retry_on,
            backoff=backoff,
            base_delay=base_delay,
        )

    # Aliases for `__call__(...)` method
    consume = __call__

    def _can_aggregate(
        self,
        last_entry: _QuotaEntry[HTTPConnectionT],
        throttle: Throttle[HTTPConnectionT],
        cost: typing.Optional[int],
        context: typing.Optional[typing.Mapping[str, typing.Any]],
        retry: int,
        retry_on: typing.Optional[RetryOn],
        backoff: BackoffStrategy,
        base_delay: float,
    ) -> bool:
        """
        Determine if a new quota entry can be aggregated with the last entry in the queue.

        Aggregation is a cost optimization that combines consecutive calls with identical
        configuration into a single entry with summed costs. This reduces queue size and
        improves performance.

        Rules for aggregation:
        1. Both entries must use the SAME throttle instance (identity check)
        2. Both entries must have the SAME context (exact dict equality)
        3. Both entries must have FIXED costs (no cost functions)
        4. All retry configuration must match exactly:
           - `retry` count
           - `retry_on` condition (identity check for callables)
           - `backoff` strategy (identity check)
           - `base_delay` value

        :param last_entry: The last entry currently in the queue.
        :param throttle: The throttle for the new entry.
        :param cost: The cost for the new entry (must be fixed, not None).
        :param context: The context for the new entry.
        :param retry: Number of retry attempts for the new entry.
        :param retry_on: Retry condition for the new entry.
        :param backoff: Backoff strategy for the new entry.
        :param base_delay: Base delay for the new entry.
        :return: True if aggregation is possible, False otherwise.
        """
        # Both must be the exact same throttle instance
        # Using `is` instead of `==` ensures we're checking identity, not equality
        if last_entry.throttle is not throttle:
            return False

        # Both must have identical context dictionaries
        # Note: This checks exact equality - {"a": 1} != {"a": 1, "b": None}
        if last_entry.context != context:
            return False

        # Both must have fixed costs (no cost functions)
        # We can only aggregate fixed costs, not dynamic costs
        if last_entry.cost is None or cost is None:
            return False

        # Both must have matching retry counts
        if last_entry.retry != retry:
            return False

        # Both must have matching retry conditions
        # For callables, we use identity check (`is`) because:
        # - Different lambda instances are never equal even with same code
        # - We want to ensure the exact same retry logic applies
        if last_entry.retry_on is not retry_on:
            # Special case: both None is OK
            if not (last_entry.retry_on is None and retry_on is None):
                return False

        # Backoff strategy must match
        # Using identity check because backoff objects may not implement __eq__
        if last_entry.backoff is not backoff:
            return False

        # Base delay must match exactly
        if last_entry.base_delay != base_delay:
            return False

        # If all rules passed, then aggregation is safe
        return True

    def _enqueue(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        retry: int = 0,
        retry_on: typing.Optional[RetryOn] = None,
        backoff: typing.Optional[BackoffStrategy] = None,
        base_delay: float = 1.0,
    ) -> Self:
        """
        Enqueue a throttle for deferred application.

        :param throttle: The throttle to queue.
        :param cost: Override cost for this throttle.
        :param context: Override context for this throttle.
        :param retry: Number of retry attempts.
        :param retry_on: Conditions for retrying.
        :param backoff: Backoff strategy for retries.
        :param base_delay: Base delay in seconds between retries.
        :return: Self for chaining.
        """
        if self._cancelled:
            raise QuotaCancelledError(
                "Cannot queue quota entries on a cancelled quota context"
            )
        if self._consumed:
            raise QuotaAppliedError(
                "Cannot queue quota entries on a consumed quota context"
            )

        if cost == 0:
            return self  # No cost, no need to enqueue

        _throttle = throttle if throttle is not None else self.owner
        if _throttle is None:
            raise ValueError(
                "No throttle specified. Either provide a throttle argument or "
                "create the context via `QuotaContext(owner=...)` or `throttle.quota(...)` to bind it to a throttle."
            )

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context

        # Normalize context to None if empty for consistent aggregation checks
        normalized_context = merged_context if merged_context else None
        # Resolve backoff to actual instance (needed for aggregation checks since backoff
        # can be a callable or a strategy instance)
        backoff_to_use = backoff or DEFAULT_BACKOFF

        can_aggregate = False
        if self._queue:
            last_entry = self._queue[-1]
            can_aggregate = self._can_aggregate(
                last_entry=last_entry,
                throttle=_throttle,
                cost=cost,
                context=normalized_context,
                retry=retry,
                retry_on=retry_on,
                backoff=backoff_to_use,
                base_delay=base_delay,
            )

        if can_aggregate:
            # Add cost to existing entry
            last_entry = self._queue[-1]
            last_entry.cost += cost  # type: ignore[operator]
            last_entry.resolved_cost += cost  # type: ignore[operator]
            self._queued_cost += cost  # type: ignore[operator]
        else:
            # Create new entry
            entry = _QuotaEntry[HTTPConnectionT](
                throttle=_throttle,
                cost=cost,
                context=normalized_context,
                retry=retry,
                retry_on=retry_on,
                backoff=backoff_to_use,
                base_delay=base_delay,
            )
            self._queue.append(entry)
            self._queued_cost += entry.resolved_cost

        return self

    async def check(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        cost: typing.Optional[int] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        """
        **Best-effort** check if there's sufficient quota to proceed.

        If the throttle's state cannot be determined, assumes sufficient quota.

        This performs a non-consuming check of the throttle's current state.

        Note:
            This should be used as a best-effort pre-check only. The actual
            quota may change between this check and the eventual consumption
            (classic Time-of-Check to Time-of-Use issue).
            A way to avoid this is to ensure that the throttle checked/used is unique to this context,
            or best, throttle optimistically and handle rejections gracefully.

        :param throttle: Specific throttle to check. If None and context is bound,
            checks the owner throttle. If None and unbound, checks all queued entries.
        :param cost: Override cost to check against.
        :param context: Override context for the check.
        :return: True if sufficient quota is available to proceed, False otherwise.
            If the throttle's state cannot be determined, returns True.
        """
        _throttle = throttle if throttle is not None else self.owner
        if _throttle is not None:
            if context:
                merged_context = self._default_context.copy()
                merged_context.update(context)
            else:
                merged_context = self._default_context

            stat = await _throttle.stat(self.connection, merged_context)
            if stat is None:
                return True  # Can't determine, assume OK

            check_cost = cost if cost is not None else 1
            return stat.hits_remaining >= check_cost

        # No specific throttle so check all queued throttles
        for entry in self._queue:
            # `entry.context` should already contain `self._default_context`
            # since it was merged at enqueue time.
            if entry.context and context:
                merged_context = entry.context.copy()
                merged_context.update(context)
            else:
                merged_context = self._default_context

            stat = await entry.throttle.stat(self.connection, merged_context)
            if stat is None:
                continue

            check_cost = entry.cost if entry.cost is not None else 1
            if stat.hits_remaining < check_cost:
                return False
        return True

    async def stat(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[StrategyStat]:
        """
        Get the current throttle strategy statistics for the connection.

        :param throttle: Specific throttle to get stats for. If None, uses owner throttle.
        :param context: Override context for the stat retrieval.
        """
        _throttle = throttle if throttle is not None else self.owner
        if _throttle is None:
            raise ValueError(
                "No throttle specified. Either provide a throttle argument or "
                "create the context via `throttle.quota()` to bind it to a throttle."
            )

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context
        stat = await _throttle.stat(self.connection, merged_context)
        return stat

    async def apply(self) -> None:
        """
        Consume all quota entries in the context.

        This consumes all queued quota entries in order, including any from
        nested child contexts.

        This method is idempotent and assumes a lock is already held
        if locking is enabled (via context manager).

        Note:
        If called manually (outside of context manager), locking is not applied.
        The lock is only held when using the context manager (`async with`).
        For manual consumption with locking, acquire the lock yourself before calling apply.

        :raises `QuotaCancelledError`: If quota context was cancelled
        """
        if self._cancelled:
            raise QuotaCancelledError("Cannot consume a cancelled quota context")

        if self._consumed:
            return

        # First, merge all children that haven't been applied
        for child in self._children:
            if not child._consumed:
                child._merge_into_parent(mark_as_consumed=True)

        # Apply all queued throttles
        for entry in self._queue:
            await self._hit(entry)

        self._consumed = True

    async def cancel(self) -> None:
        """
        Cancel the quota context, clearing all queued entries without consuming them.

        This clears the queue and marks the context as cancelled. After cancellation,
        no further operations (enqueue or consume) are allowed on this context.

        If the context holds a lock, it will be released.

        For nested contexts, cancellation detaches the child from the parent
        (its quota entries will not be merged into the parent).

        This method is idempotent, i.e., calling it multiple times has no additional effect.

        :raises `QuotaAppliedError`: If the quota context has already been consumed.

        Example:
        ```python
        async with throttle.quota(conn, apply_on_exit=False) as quota:
            await quota(cost=5)
            await quota(throttle2, cost=3)

            if not await validate_operation():
                await quota.cancel()  # Cancel queued quota entries
                return error_response()

            await perform_operation()
            await quota.apply()  # Manually consume
        ```
        """
        if self._consumed:
            raise QuotaAppliedError("Cannot cancel a consumed quota context")

        if self._cancelled:
            return  # Already discarded, idempotent

        # Discard all unapplied children first
        for child in self._children:
            if child.active:
                await child.cancel()

        # Clear the queue
        self._queue.clear()
        self._queued_cost = 0

        # Detach from parent if nested
        if self._nested and self in self.parent._children:  # type: ignore[union-attr]
            self.parent._children.remove(self)  # type: ignore[union-attr]

        # Mark as discarded
        self._cancelled = True

        # Release lock early if we're holding one
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    discard = cancel  # alias

    async def _should_retry(
        self,
        entry: _QuotaEntry[HTTPConnectionT],
        exc: BaseException,
        attempt: int,
    ) -> bool:
        """
        Determine if we should retry based on `retry_on` configuration.

        :param entry: The queued throttle entry.
        :param exc: The exception that occurred.
        :param attempt: Current attempt number.
        :return: True if should retry, False otherwise.
        """
        # If no `retry_on` is specified, we can retry on any other exception
        if entry._retry_type is None:
            return True

        if entry._retry_type == "exception_type":
            return isinstance(exc, entry.retry_on)  # type: ignore[arg-type]

        # Retry type is a callable here. Use precomputed `resolved_cost`
        exc_info = dict(
            connection=self.connection,
            exception=exc,
            attempt=attempt,
            cost=entry.resolved_cost,
            context=entry.context,
        )
        if entry._retry_type == "coroutine":
            return await entry.retry_on(exc_info)  # type: ignore[misc,operator,arg-type]
        return entry.retry_on(exc_info)  # type: ignore[misc,operator,arg-type,return-value]

    async def _hit(self, entry: _QuotaEntry[HTTPConnectionT]) -> None:
        """Consume a queued quota entry with retry logic."""
        attempt = 0
        max_attempts = entry.max_attempts
        # Resolve the actual cost now (in case a cost function is used)
        actual_cost = await entry.resolve_cost(self.connection, entry.context)
        entry.resolved_cost = actual_cost  # Update resolved cost for retry logic
        # Update the cost attribute with the resolved cost, so we don't have to resolve it again
        entry.cost = actual_cost

        while attempt < max_attempts:
            attempt += 1
            try:
                # Hit the throttle
                await entry.throttle.hit(
                    self.connection,
                    cost=actual_cost,
                    context=entry.context,
                )
                # Track applied cost
                self._applied_cost += actual_cost
                return

            except _NON_RETRYABLE_EXCEPTIONS:
                raise
            except BaseException as exc:
                if attempt >= max_attempts or not await self._should_retry(
                    entry, exc, attempt
                ):
                    raise

                # Wait before retrying
                delay = entry.backoff(attempt, entry.base_delay)
                await asyncio.sleep(delay)

    def nested(
        self,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        apply_on_error: typing.Optional[
            typing.Union[bool, typing.Tuple[typing.Type[BaseException], ...]]
        ] = None,
        apply_on_exit: bool = True,
        lock: typing.Union[bool, str, None] = None,
        lock_config: typing.Optional[LockConfig] = None,
        reentrant_lock: bool = True,
    ) -> Self:
        """
        Create a nested child quota context.

        Child contexts merge their queued quota entries into the parent
        when they exit successfully. The parent consumes all quota entries
        (including children's) when it applies or exits successfully.

        :param context: Override default context for the child.
        :param apply_on_error: Override error handling (inherits from parent if None).
        :param apply_on_exit: Whether child auto-applies to parent on exit.
        :param lock: Controls locking for this nested context.
            - `None` (default): No lock (operates under parent's lock context)
            - `False`: Explicitly disable locking
            - `True`: Use child's owner throttle UID as lock key (if bound)
            - `str`: Use the provided string as the lock key

            **Pitfalls of nested quota context locks:**

            1. **Deadlock Risk**: If you use the same lock key as the parent,
                and the owner's (throttle) backend return a non re-entrant lock,
                the child will deadlock waiting for a lock the parent holds.
                An error is raised if this is detected.

            2. **Lock Ordering**: If parent holds lock "A" and child acquires "B",
                while another context holds "B" and wants "A", deadlock occurs.
                Always acquire locks in a consistent order across your application.

            3. **Partial Atomicity**: The child releases its lock on exit, but the
                parent may fail later. This means the child's protected resources
                could be accessed by other requests before the parent applies.

            4. **Use Case**: Nested locks are useful when the child operates on a
                *different* resource than the parent and needs independent protection.

        :param lock_config: Configuration for lock acquisition (ttl, blocking, etc.).
        :param reentrant_lock: If True, allows same lock key as parent (for re-entrant locks).
            Else, raises an error if child lock key is the same as parent to prevent deadlocks.
        :return: A new child quota context.

        Example (no lock - default, safest):

        ```python
        async with throttle_a.quota(conn) as parent:
            await parent(cost=1)

            async with parent.nested() as child:
                # Child operates under parent's lock
                await child(cost=2)
        ```

        Example (child with different lock for different resource):

        ```python
        async with throttle_a.quota(conn) as parent:
            await parent(cost=1)

            # Child protects a different resource
            async with parent.nested(lock="other_resource") as child:
                await child(throttle_b, cost=2)
        ```
        """
        child_lock_key = _resolve_lock_key(lock, self.owner)
        # Raise error about potential deadlock if child uses same lock as parent
        if (
            child_lock_key is not None
            and child_lock_key == self._lock_key
            and not reentrant_lock
        ):
            raise QuotaError(
                f"Nested quota context is using the same lock key as its parent "
                f"({child_lock_key!r}). This will cause a deadlock if the parent lock is not re-entrant, "
                f"because the parent already holds this lock. Use a different lock key or "
                f"set lock=None to operate under the parent's lock context.",
            )

        if context:
            merged_context = self._default_context.copy()
            merged_context.update(context)
        else:
            merged_context = self._default_context

        return self.__class__(
            connection=self.connection,
            context=merged_context,
            owner=self.owner,  # Inherit owner from parent
            apply_on_error=(
                apply_on_error if apply_on_error is not None else self.apply_on_error
            ),
            apply_on_exit=apply_on_exit,
            lock=lock if lock is not None else False,
            lock_config=lock_config,
            parent=self,
        )

    # Alias for `nested(...)` method
    quota = nested

    def __repr__(self) -> str:
        owner_info = f"owner={self.owner.uid!r} " if self.owner else ""
        state = (
            "cancelled"
            if self._cancelled
            else ("consumed" if self._consumed else "active")
        )
        return (
            f"<{self.__class__.__name__} "
            f"{owner_info}"
            f"queued={len(self._queue)} "
            f"state={state} "
            f"nested={self.is_nested} "
            f"depth={self.depth}>"
        )
