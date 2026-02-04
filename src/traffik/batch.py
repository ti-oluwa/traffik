import asyncio
import inspect
import math
import random
import typing
from collections import deque
from contextlib import AsyncExitStack
from types import TracebackType

from starlette.exceptions import HTTPException
from typing_extensions import Self

from traffik.exceptions import (
    BatchAppliedError,
    BatchDiscardedError,
    BatchError,
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

__all__ = [
    "ThrottleBatch",
    "ConstantBackoff",
    "LinearBackoff",
    "ExponentialBackoff",
    "LogarithmicBackoff",
]

_NON_RETRYABLE_EXCEPTIONS: typing.Tuple[typing.Type[BaseException], ...] = (
    asyncio.CancelledError,
    HTTPException,
    KeyboardInterrupt,
    SystemExit,
)
"""Tuple of exceptions that should not be retried. They are considered non-retryable signals."""


def ConstantBackoff(
    attempt: int,
    base_delay: float,
) -> float:
    """Delay remains the same for each attempt."""
    return base_delay


class LinearBackoff:
    """Delay increases linearly with each attempt."""

    def __init__(self, increment: float = 1.0) -> None:
        """
        :param increment: Amount to increase delay per attempt (in seconds).
        """
        self.increment = increment

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay + (attempt - 1) * self.increment


class ExponentialBackoff:
    """Delay doubles (or multiplies) with each attempt."""

    def __init__(
        self,
        multiplier: float = 2.0,
        max_delay: typing.Optional[float] = None,
        jitter: bool = False,
    ) -> None:
        """
        :param multiplier: Factor to multiply delay by each attempt.
        :param max_delay: Maximum delay cap (in seconds).
        :param jitter: Whether to add random jitter to prevent thundering herd.
        """
        self.multiplier = multiplier
        self.max_delay = max_delay
        self.jitter = jitter

    def __call__(self, attempt: int, base_delay: float) -> float:
        delay = base_delay * (self.multiplier ** (attempt - 1))
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random())
        return delay


class LogarithmicBackoff:
    """Delay increases logarithmically with each attempt."""

    def __init__(self, base: float = 2.0) -> None:
        """
        :param base: Logarithm base.
        """
        self.base = base

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay * math.log(attempt + 1, self.base)


@typing.final
class _QueuedThrottle(typing.Generic[HTTPConnectionT]):
    """A throttle queued for deferred application."""

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
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
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
        self.backoff = backoff or ExponentialBackoff()
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
        elif isinstance(retry_on, tuple) and all(inspect.isclass(t) for t in retry_on):
            self._retry_type = "exception_type"
        elif isinstance(retry_on, type) and inspect.isclass(retry_on):
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
        context: typing.Optional[typing.Mapping[str, typing.Any]],
    ) -> int:
        """Resolve the actual cost of the throttle, calling cost function if needed."""
        if self.cost is not None:
            return self.cost

        throttle = self.throttle
        if throttle._uses_cost_func:
            return await throttle.cost(connection, context)  # type: ignore[call-arg]

        return throttle.cost  # type: ignore[return-value]


class ThrottleBatch(typing.Generic[HTTPConnectionT]):
    """
    A batch context manager for deferred throttle application.

    Throttles are queued within the batch and applied together when the
    batch is applied (either explicitly via `apply()` or implicitly
    on successful context exit).

    **Batch Semantics (not Transaction!):**

    - ✅ Queued together (deferred application on successful exit or manual apply)
    - ✅ Applied in order (predictable)
    - ✅ Applied atomically with respect to locking (isolated from other requests)
    - ❌ NOT rolled back on failure (partial application is acceptable)
    - ✅ Optionally skipped on errors (`apply_on_error`)

    Think of it as: "Apply multiple throttles together, under a lock, but
    without all-or-nothing guarantees."

    Can be used in two modes:

    1. **Bound mode** (usually created via `throttle.batch()`): Batch is tied
       to a specific throttle. Calling `batch()` without arguments uses the owner throttle.

    2. **Unbound mode** (created directly): Batch is not tied to any throttle.
       You must always specify a throttle when calling `batch(throttle)`.

    Features:

    - Deferred throttling: Throttles applied only on `apply()`
    - Conditional throttling: Skip throttles on errors (configurable)
    - Nested batches: Parent-child relationships with cascading applies
    - Retry with backoff: Configurable retry logic per throttle
    - Context-wide locking: Lock acquired on entry, released on exit

    Note:
        When locking is enabled, the lock is held for the entire duration of the
        context. Keep operations within the context fast to avoid blocking other
        requests waiting for the same lock.

    Example (bound mode):

    ```python
    async with ThrottleBatch(conn, owner=throttle) as batch:
        await batch(cost=2)          # Uses owner throttle with cost=2
        await batch()                 # Uses owner throttle with default cost
        await batch(other_throttle)   # Can still use other throttles
    ```

    Example (unbound mode):

    ```python
    async with ThrottleBatch(conn) as batch:
        await batch(throttle1, cost=2)
        await batch(throttle2)
    ```
    """

    __slots__ = (
        "connection",
        "default_context",
        "owner",
        "apply_on_error",
        "apply_on_exit",
        "lock_config",
        "parent",
        "_lock_key",
        "_queue",
        "_children",
        "_applied",
        "_discarded",
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
        parent: typing.Optional["ThrottleBatch[HTTPConnectionT]"] = None,
    ) -> None:
        """
        Initialize a throttle batch.

        :param connection: The HTTP connection to throttle.
        :param context: Default context for all queued throttles.
        :param owner: The throttle this batch is bound to (if any).
            When set, calling the batch without a throttle argument will use the owner.
        :param apply_on_error: Whether to apply throttles even on exceptions.
            - `False` (default): Don't apply on any exception
            - `True`: Apply on all exceptions
            - `tuple[Exception, ...]`: Apply only for these exception types
        :param apply_on_exit: Whether to auto-apply on successful exit.
        :param lock: Controls locking to prevent race conditions.
            - `None` (default): Use throttle UID as lock key if bound, else disable locking
            - `True`: Same as None (use throttle UID if bound, else disable)
            - `False`: Explicitly disable locking
            - `str`: Use the provided string as the lock key

            When enabled, the lock is acquired on context entry and released on exit.
            This ensures the entire batch (including user code) is atomic with
            respect to other batches using the same lock key.

            **Important:** Keep operations within the batch fast since the lock
            is held for the entire duration.
        :param lock_config: Configuration for lock acquisition. See `LockConfig`.
        :param parent: Parent batch for nesting.
        """
        self.connection = connection
        self.default_context = context
        self.owner = owner
        self.apply_on_error = apply_on_error
        self.apply_on_exit = apply_on_exit
        self.lock_config = lock_config or {}
        self.parent = parent

        if lock is None or lock is True:
            self._lock_key: typing.Optional[str] = (
                f"batch:{owner.uid}" if owner is not None else None
            )
        elif lock is False:
            self._lock_key = None
        else:
            self._lock_key = f"batch:{lock}"

        # We use deque for thread-safe append operations
        self._queue: typing.Deque[_QueuedThrottle[HTTPConnectionT]] = deque()
        self._children: typing.List["ThrottleBatch[HTTPConnectionT]"] = []
        self._applied = False
        self._discarded = False
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
    def applied(self) -> bool:
        """Whether this batch has been applied."""
        return self._applied

    @property
    def discarded(self) -> bool:
        """Whether this batch has been discarded."""
        return self._discarded

    @property
    def active(self) -> bool:
        """Whether this batch is still active (not applied or discarded)."""
        return not (self._applied or self._discarded)

    @property
    def is_bound(self) -> bool:
        """Whether this batch is bound to an owner throttle."""
        return self._bound

    @property
    def is_nested(self) -> bool:
        """Whether this batch has a parent. Is this batch nested?"""
        return self._nested

    @property
    def queued_cost(self) -> int:
        """
        **Estimated** cost of all queued throttles (including children).

        Note: This is an estimate since some throttles may use cost functions.
        In those cases, we assume the throttle's defined cost or 1 as a fallback.
        """
        child_cost = sum(child.queued_cost for child in self._children)
        return self._queued_cost + child_cost

    @property
    def applied_cost(self) -> int:
        """Cost that has been applied."""
        return self._applied_cost

    @property
    def nesting_depth(self) -> int:
        """How deep this batch is nested."""
        depth = 0
        batch: typing.Optional[ThrottleBatch[HTTPConnectionT]] = self
        while batch is not None:
            batch = batch.parent
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
            return

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
                    self._merge_into_parent(mark_as_applied=True)
                else:
                    await self.apply()
            return

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

    def _merge_into_parent(self, mark_as_applied: bool = True) -> None:
        """
        Merge this batch's queue into the parent's queue.

        :param mark_as_applied: Whether to mark this batch as applied once merged.
        """
        if self.parent is None:
            return

        parent = self.parent
        parent._queue.extend(self._queue)
        parent._queued_cost += self._queued_cost
        self._applied = mark_as_applied

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
        Queue a throttle for deferred application.

        :param throttle: The throttle to queue. If not provided, uses the owner
            throttle (only valid for bound batches).
        :param cost: Override cost for this throttle.
        :param context: Override context for this throttle.
        :param retry: Number of retry attempts if an error occurs.
        :param retry_on: Conditions for retrying. Can be:
            - A tuple of exception types to retry on
            - A callable(connection, exception, cost, context, attempt) -> bool
            If None, retries on any exception when retry > 0.
        :param backoff: Backoff strategy for retries.
        :param base_delay: Base delay in seconds between retries.
        :return: Self for chaining.

        Example (bound batch):

        ```python
        async with throttle.batch(conn) as batch:
            await batch(cost=2)        # Uses owner throttle
            await batch()              # Uses owner throttle with default cost
            await batch(other, cost=1) # Uses different throttle
        ```

        Example (with retry on specific exceptions):

        ```python
        async with ThrottleBatch(conn) as batch:
            await batch(throttle1, cost=2, retry=3, retry_on=(TimeoutError,))
            await batch(throttle2)
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
    add = __call__

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
        if self._discarded:
            raise BatchDiscardedError("Cannot queue throttles on a discarded batch")
        if self._applied:
            raise BatchAppliedError("Cannot queue throttles on an applied batch")

        resolved_throttle = throttle if throttle is not None else self.owner
        if resolved_throttle is None:
            raise ValueError(
                "No throttle specified. Either provide a throttle argument or "
                "create the batch via `ThrottleBatch(owner=...)` or `throttle.batch(...)` to bind it to a throttle."
            )

        merged_context = dict(self.default_context or {})
        if context:
            merged_context.update(context)

        entry = _QueuedThrottle[HTTPConnectionT](
            throttle=resolved_throttle,
            cost=cost,
            context=merged_context if merged_context else None,
            retry=retry,
            retry_on=retry_on,
            backoff=backoff or ExponentialBackoff(),
            base_delay=base_delay,
        )
        self._queue.append(entry)

        # Track queued cost (`resolved_cost` is precomputed in `_QueuedThrottle`)
        # This still may be an estimate if cost function is used.
        # But will be resolved properly at apply time.
        self._queued_cost += entry.resolved_cost
        return self

    async def check(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        cost: typing.Optional[int] = None,
    ) -> bool:
        """
        **Best-effort** check if there's sufficient quota to proceed.

        If the throttle's state cannot be determined, assumes sufficient quota.

        This performs a non-consuming check of the throttle's current state.

        Note:
            This should be used as a best-effort pre-check only. The actual
            quota may change between this check and the eventual apply
            (classic Time-of-Check to Time-of-Use issue).
            A way to avoid this is to ensure that the throttle checked/used is unique to this batch,
            or best, throttle optimistically and handle rejections gracefully.

        :param throttle: Specific throttle to check. If None and batch is bound,
            checks the owner throttle. If None and unbound, checks all queued throttles.
        :param cost: Override cost to check against.
        :return: True if sufficient quota is available to proceed, False otherwise.
            If the throttle's state cannot be determined, returns True.
        """
        _throttle = throttle if throttle is not None else self.owner
        if _throttle is not None:
            # Check specific throttle (either provided or owner)
            stat = await _throttle.stat(self.connection, self.default_context)
            if stat is None:
                return True  # Can't determine, assume OK

            check_cost = cost if cost is not None else 1
            return stat.hits_remaining >= check_cost

        # No specific throttle so check all queued throttles
        for entry in self._queue:
            stat = await entry.throttle.stat(self.connection, entry.context)
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
                "create the batch via `throttle.batch()` to bind it to a throttle."
            )

        merged_context = dict(self.default_context or {})
        if context:
            merged_context.update(context)

        stat = await _throttle.stat(
            self.connection, merged_context if merged_context else None
        )
        return stat

    async def apply(self) -> None:
        """
        Apply all throttles in the batch.

        This applies all queued throttles in order, including any from
        nested child batches.

        This method is idempotent and assumes a lock is already held 
        if locking is enabled (via context manager).

        Note:
        If called manually (outside of context manager), locking is not applied.
        The lock is only held when using the context manager (`async with`).
        For manual applies with locking, acquire the lock yourself before calling apply.

        :raises `BatchDiscardedError`: If batch was discarded
        """
        if self._discarded:
            raise BatchDiscardedError("Cannot apply a discarded batch")

        if self._applied:
            return

        # First, merge all children that haven't been applied
        for child in self._children:
            if not child._applied:
                child._merge_into_parent(mark_as_applied=True)

        # Apply all queued throttles
        for entry in self._queue:
            # Resolve the actual cost now (in case cost function is used)
            actual_cost = await entry.resolve_cost(self.connection, entry.context)
            entry.resolved_cost = actual_cost  # Update resolved cost for retry logic
            # Update the cost attribute with the resolved cost, so the throttle doesn't have to resolve it again
            entry.cost = actual_cost
            await self._hit_entry(entry)

        self._applied = True

    async def discard(self) -> None:
        """
        Discard the batch, clearing all queued throttles without applying them.

        This clears the queue and marks the batch as discarded. After discard,
        no further operations (enqueue or apply) are allowed on this batch.

        If the batch holds a lock, it will be released.

        For nested batches, discard detaches the child from the parent
        (its throttles will not be merged into the parent).

        This method is idempotent, i.e., calling it multiple times has no additional effect.

        :raises `BatchAppliedError`: If the batch has already been applied.

        Example:
        ```python
        async with throttle.batch(conn, apply_on_exit=False) as batch:
            await batch(cost=5)
            await batch(throttle2, cost=3)

            if not await validate_operation():
                await batch.discard()  # Discard queued throttles
                return error_response()

            await perform_operation()
            await batch.apply()  # Manually apply
        ```
        """
        if self._applied:
            raise BatchAppliedError("Cannot discard an applied batch")

        if self._discarded:
            return  # Already discarded, idempotent

        # Discard all unapplied children first
        for child in self._children:
            if child.active:
                await child.discard()

        # Clear the queue
        self._queue.clear()
        self._queued_cost = 0

        # Detach from parent if nested
        if self._nested and self in self.parent._children:  # type: ignore[union-attr]
            self.parent._children.remove(self)  # type: ignore[union-attr]

        # Mark as discarded
        self._discarded = True

        # Release lock early if we're holding one
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    async def _should_retry(
        self,
        entry: _QueuedThrottle[HTTPConnectionT],
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

        # Retry type is a callable - use precomputed resolved_cost
        exc_info = dict(
            connection=self.connection,
            exception=exc,
            attempt=attempt,
            cost=entry.resolved_cost,
            context=entry.context,
        )
        if entry._retry_type == "coroutine":
            return await entry.retry_on(exc_info)  # type: ignore[call-arg,arg-type]
        return entry.retry_on(exc_info)  # type: ignore[call-arg,arg-type]

    async def _hit_entry(self, entry: _QueuedThrottle[HTTPConnectionT]) -> None:
        """Hit/apply a queued throttle with retry logic."""
        attempt = 0
        max_attempts = entry.max_attempts
        cost = entry.resolved_cost

        while attempt < max_attempts:
            attempt += 1
            try:
                # Hit the throttle
                await entry.throttle(
                    self.connection,
                    cost=entry.cost,
                    context=entry.context,
                )
                # Track applied cost
                self._applied_cost += cost
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
        reentrant_lock: bool = False,
    ) -> "ThrottleBatch[HTTPConnectionT]":
        """
        Create a nested child batch.

        Child batches merge their queued throttles into the parent
        when they exit successfully. The parent applies all throttles
        (including children's) when it applies.

        :param context: Override default context for the child.
        :param apply_on_error: Override error handling (inherits from parent if None).
        :param apply_on_exit: Whether child auto-applies to parent on exit.
        :param lock: Controls locking for this nested batch.
            - `None` (default): No lock (operates under parent's lock context)
            - `False`: Explicitly disable locking
            - `True`: Use child's owner throttle UID as lock key (if bound)
            - `str`: Use the provided string as the lock key

            **Pitfalls of nested batch locks:**

            1. **Deadlock Risk**: If you use the same lock key as the parent,
                the child will deadlock waiting for a lock the parent holds.
                An error is raised if this is detected.

            2. **Lock Ordering**: If parent holds lock "A" and child acquires "B",
                while another batch holds "B" and wants "A", deadlock occurs.
                Always acquire locks in a consistent order across your application.

            3. **Partial Atomicity**: The child releases its lock on exit, but the
                parent may fail later. This means the child's protected resources
                could be accessed by other requests before the parent applies.

            4. **Use Case**: Nested locks are useful when the child operates on a
                *different* resource than the parent and needs independent protection.

        :param lock_config: Configuration for lock acquisition (ttl, blocking, etc.).
        :param reentrant_lock: If True, allows same lock key as parent (for re-entrant locks).
        :return: A new child batch.

        Example (no lock - default, safest):

        ```python
        async with throttle_a.batch(conn) as parent:
            await parent(cost=1)

            async with parent.nested() as child:
                # Child operates under parent's lock
                await child(cost=2)
        ```

        Example (child with different lock for different resource):

        ```python
        async with throttle_a.batch(conn) as parent:
            await parent(cost=1)

            # Child protects a different resource
            async with parent.nested(lock="other_resource") as child:
                await child(throttle_b, cost=2)
        ```
        """
        merged_context = dict(self.default_context or {})
        if context:
            merged_context.update(context)

        # Determine child's lock key
        child_lock_key: typing.Optional[str] = None
        if lock is None or lock is False:
            child_lock_key = None
        elif lock is True:
            # Use owner's UID if bound
            child_lock_key = f"batch:{self.owner.uid}" if self.owner else None
        else:
            child_lock_key = f"batch:{lock}"

        # Raise error about potential deadlock if child uses same lock as parent
        if (
            child_lock_key is not None
            and child_lock_key == self._lock_key
            and not reentrant_lock
        ):
            raise BatchError(
                f"Nested batch is using the same lock key as its parent "
                f"({child_lock_key!r}). This will cause a deadlock if the parent lock is not re-entrant, "
                f"because the parent already holds this lock. Use a different lock key or "
                f"set lock=None to operate under the parent's lock context.",
            )

        return self.__class__(
            connection=self.connection,
            context=merged_context if merged_context else None,
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
    batch = nested

    def __repr__(self) -> str:
        owner_info = f"owner={self.owner.uid!r} " if self.owner else ""
        state = (
            "discarded"
            if self._discarded
            else ("applied" if self._applied else "active")
        )
        return (
            f"<{self.__class__.__name__} "
            f"{owner_info}"
            f"queued={len(self._queue)} "
            f"state={state} "
            f"nested={self.is_nested} "
            f"depth={self.nesting_depth}>"
        )
