import asyncio
import inspect
import math
import random
import typing
import warnings
from collections import deque
from contextlib import AsyncExitStack
from types import TracebackType

from typing_extensions import Self

from traffik.exceptions import (
    QuotaExhaustedError,
    TransactionCommittedError,
    TransactionRolledBackError,
)
from traffik.throttles import Throttle
from traffik.types import (
    ApplyOnError,
    BackoffStrategy,
    HTTPConnectionT,
    LockConfig,
    OnQuotaExhausted,
    RetryOn,
)

__all__ = [
    "ThrottleTransaction",
    "ConstantBackoff",
    "LinearBackoff",
    "ExponentialBackoff",
    "LogarithmicBackoff",
]


def ConstantBackoff(
    attempt: int,
    base_delay: float,
) -> float:
    """Constant backoff - delay remains the same for each attempt."""
    return base_delay


class LinearBackoff:
    """Linear backoff - delay increases linearly with each attempt."""

    def __init__(self, increment: float = 1.0) -> None:
        """
        :param increment: Amount to increase delay per attempt (in seconds).
        """
        self.increment = increment

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay + (attempt - 1) * self.increment


class ExponentialBackoff:
    """Exponential backoff - delay doubles (or multiplies) with each attempt."""

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
    """Logarithmic backoff - delay increases logarithmically."""

    def __init__(self, base: float = 2.0) -> None:
        """
        :param base: Logarithm base.
        """
        self.base = base

    def __call__(self, attempt: int, base_delay: float) -> float:
        return base_delay * math.log(attempt + 1, self.base)


@typing.final
class _QueuedThrottle(typing.Generic[HTTPConnectionT]):
    """Represents a throttle queued for deferred application."""

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

        # Precompute values for fast commit
        self.max_attempts = retry + 1
        # For cost, if the throttle uses a cost function, we can't determine the actual cost until commit time,
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


class ThrottleTransaction(typing.Generic[HTTPConnectionT]):
    """
    A transaction context manager for deferred throttle application.

    Throttles are queued within the transaction and only applied when the
    transaction is committed (either explicitly via `commit()` or implicitly
    on successful context exit).

    Can be used in two modes:

    1. **Bound mode** (usually created via `throttle.transaction()`): Transaction is tied
       to a specific throttle. Calling `tx()` without arguments uses the owner throttle.

    2. **Unbound mode** (created directly): Transaction is not tied to any throttle.
       You must always specify a throttle when calling `tx(throttle)`..

    Features:
    - Deferred throttling: Throttles applied only on commit
    - Conditional throttling: Skip throttles on errors (configurable)
    - Nested transactions: Parent-child relationships with cascading commits
    - Retry with backoff: Configurable retry logic per throttle
    - Quota checking: Pre-check if quota is available before operations
    - Context-wide locking: Lock acquired on entry, released on exit

    Note:
        When locking is enabled, the lock is held for the entire duration of the
        context. Keep operations within the context fast to avoid blocking other
        requests waiting for the same lock.

    Example (bound mode):

    ```python
    async with throttle.transaction(conn) as tx:
        await tx(cost=2)          # Uses owner throttle with cost=2
        await tx()                 # Uses owner throttle with default cost
        await tx(other_throttle)   # Can still use other throttles
    ```

    Example (unbound mode):

    ```python
    async with ThrottleTransaction(conn) as tx:
        await tx(throttle1, cost=2)
        await tx(throttle2)
    ```
    """

    __slots__ = (
        "connection",
        "default_context",
        "owner",
        "apply_on_error",
        "on_quota_exhausted",
        "commit_on_exit",
        "lock_config",
        "parent",
        "_lock_key",
        "_queue",
        "_children",
        "_committed",
        "_rolled_back",
        "_entered",
        "_bound",
        "_nested",
        "_quota_exhausted_mode",
        "_delay_config",
        "_total_queued_cost",
        "_total_applied_cost",
        "_exit_stack",
    )

    def __init__(
        self,
        connection: HTTPConnectionT,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        owner: typing.Optional[Throttle[HTTPConnectionT]] = None,
        apply_on_error: ApplyOnError = False,
        on_quota_exhausted: OnQuotaExhausted = "raise",
        commit_on_exit: bool = True,
        lock: typing.Union[bool, str, None] = None,
        lock_config: typing.Optional[LockConfig] = None,
        parent: typing.Optional["ThrottleTransaction[HTTPConnectionT]"] = None,
    ) -> None:
        """
        Initialize a throttle transaction.

        :param connection: The HTTP connection to throttle.
        :param context: Default context for all queued throttles.
        :param owner: The throttle this transaction is bound to (if any).
            When set, calling `tx()` without a throttle argument will use the owner.
        :param apply_on_error: Whether to apply throttles even on exceptions.
            - `False` (default): Don't apply on any exception
            - `True`: Apply on all exceptions
            - `tuple[Exception, ...]`: Apply only for these exception types
        :param on_quota_exhausted: Behavior when quota is exhausted at commit.
            - "raise": Raise `QuotaExhaustedError` (default)
            - "force": Apply throttle anyway (may go over limit)
            - "skip": Silently skip throttling
            - ("delay", seconds_or_backoff): Wait and retry until quota available
        :param commit_on_exit: Whether to auto-commit on successful exit.
        :param lock: Controls locking to prevent race conditions.
            - `None` (default): Use throttle UID as lock key if bound, else disable locking
            - `True`: Same as None (use throttle UID if bound, else disable)
            - `False`: Explicitly disable locking
            - `str`: Use the provided string as the lock key

            When enabled, the lock is acquired on context entry and released on exit.
            This ensures the entire transaction (including user code) is atomic with
            respect to other transactions using the same lock key.

            **Important:** Keep operations within the transaction fast since the lock
            is held for the entire duration.
        :param lock_config: Configuration for lock acquisition. See `LockConfig`.
        :param parent: Parent transaction for nesting.
        """
        self.connection = connection
        self.default_context = context
        self.owner = owner
        self.apply_on_error = apply_on_error
        self.on_quota_exhausted = on_quota_exhausted
        self.commit_on_exit = commit_on_exit
        self.lock_config = lock_config or {}
        self.parent = parent

        # Locking configuration: derive lock key
        # - None/True with owner: use owner.uid as lock key
        # - None/True without owner: no locking (random key is useless)
        # - False: explicitly disabled
        # - str: use provided key
        if lock is None or lock is True:
            self._lock_key: typing.Optional[str] = (
                f"tx:{owner.uid}" if owner is not None else None
            )
        elif lock is False:
            self._lock_key = None
        else:
            self._lock_key = f"tx:{lock}"

        # Use deque for thread-safe append operations
        self._queue: typing.Deque[_QueuedThrottle[HTTPConnectionT]] = deque()
        self._children: typing.List["ThrottleTransaction[HTTPConnectionT]"] = []
        self._committed = False
        self._rolled_back = False
        self._entered = False
        self._bound = owner is not None
        self._nested = parent is not None

        # Precompute quota exhausted mode for fast access
        self._quota_exhausted_mode: str = (
            on_quota_exhausted[0]
            if isinstance(on_quota_exhausted, tuple)
            else on_quota_exhausted
        )
        # Precompute delay config if using delay mode
        self._delay_config: typing.Optional[typing.Union[float, BackoffStrategy]] = (
            on_quota_exhausted[1] if isinstance(on_quota_exhausted, tuple) else None
        )

        self._total_queued_cost = 0
        self._total_applied_cost = 0

        # For managing context-wide lock
        self._exit_stack: typing.Optional[AsyncExitStack] = None

        if parent is not None:
            parent._children.append(self)

    @property
    def committed(self) -> bool:
        """Whether this transaction has been committed."""
        return self._committed

    @property
    def rolled_back(self) -> bool:
        """Whether this transaction has been rolled back."""
        return self._rolled_back

    @property
    def active(self) -> bool:
        """Whether this transaction is still active (not committed or rolled back)."""
        return not self._committed and not self._rolled_back

    @property
    def is_bound(self) -> bool:
        """Whether this transaction is bound to an owner throttle."""
        return self._bound

    @property
    def is_nested(self) -> bool:
        """Whether this transaction has a parent. Is this transaction nested?"""
        return self._nested

    @property
    def queued_cost(self) -> int:
        """
        **Estimated** cost of all queued throttles (including children).

        notE: This is an estimate since some throttles may use cost functions.
        In those cases, we assume the throttle's defined cost or 1 as a fallback for retry logic purposes.
        """
        child_cost = sum(child.queued_cost for child in self._children)
        return self._total_queued_cost + child_cost

    @property
    def applied_cost(self) -> int:
        """
        **Estimated** cost that has been applied.

        notE: This is an estimate since some throttles may use cost functions.
        In those cases, we track the resolved cost at enqueue time for retry logic purposes, but the actual applied cost may differ.
        """
        return self._total_applied_cost

    @property
    def nesting_depth(self) -> int:
        """How deep this transaction is nested."""
        depth = 0
        tx: typing.Optional[ThrottleTransaction[HTTPConnectionT]] = self
        while tx is not None:
            tx = tx.parent
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
        try:
            # If already committed or rolled back, do nothing
            if self._committed or self._rolled_back:
                return None

            should_commit = False
            if exc_type is None:
                # No exception occured, only commit if configured to do so
                should_commit = self.commit_on_exit
            else:
                # Exception occurred, check `apply_on_error` configuration
                if self.apply_on_error is True:
                    should_commit = True
                elif isinstance(self.apply_on_error, tuple):
                    should_commit = issubclass(exc_type, self.apply_on_error)
                # else: apply_on_error is False, don't commit

            if should_commit:
                if self._nested:
                    # For nested transactions, merge queue into parent
                    self._merge_into_parent(mark_as_committed=True)
                else:
                    await self._commit()

            return None
        finally:
            # Release lock if we acquired one
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None

    def _merge_into_parent(self, mark_as_committed: bool = True) -> None:
        """
        Merge this transaction's queue into the parent's queue if bounded.

        Marks this transaction as committed.
        """
        if self.parent is None:
            return

        self.parent._queue.extend(self._queue)
        self.parent._total_queued_cost += self._total_queued_cost
        self._committed = mark_as_committed

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
            throttle (only valid for bound transactions).
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

        Example (bound transaction):

        ```python
        async with throttle.transaction(conn) as tx:
            await tx(cost=2)        # Uses owner throttle
            await tx()              # Uses owner throttle with default cost
            await tx(other, cost=1) # Uses different throttle
        ```

        Example (with retry on specific exceptions):

        ```python
        async with ThrottleTransaction(conn) as tx:
            await tx(throttle1, cost=2, retry=3, retry_on=(TimeoutError,))
            await tx(throttle2)
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

    # Alias for `__call__` method. Makes it compatible with the `Throttle` class' API.
    hit = __call__

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

        This method handles the actual queueing logic and is thread-safe
        due to the use of `collections.deque`.

        :param throttle: The throttle to queue.
        :param cost: Override cost for this throttle.
        :param context: Override context for this throttle.
        :param retry: Number of retry attempts.
        :param retry_on: Conditions for retrying.
        :param backoff: Backoff strategy for retries.
        :param base_delay: Base delay in seconds between retries.
        :return: Self for chaining.
        """
        if self._rolled_back:
            raise TransactionRolledBackError(
                "Cannot queue throttles on a rolled-back transaction"
            )
        if self._committed:
            raise TransactionCommittedError(
                "Cannot queue throttles on a committed transaction"
            )

        resolved_throttle = throttle if throttle is not None else self.owner
        if resolved_throttle is None:
            raise ValueError(
                "No throttle specified. Either provide a throttle argument or "
                "create the transaction via `ThrottleTransaction(owner=...)` or `throttle.transaction(...)` to bind it to a throttle."
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
        # deque.append is thread-safe
        self._queue.append(entry)

        # Track queued cost (`resolved_cost` is precomputed in `_QueuedThrottle`)
        self._total_queued_cost += entry.resolved_cost
        return self

    async def check(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        cost: typing.Optional[int] = None,
    ) -> bool:
        """
        Check if there's sufficient quota to proceed.

        This performs a non-consuming check of the throttle's current state.

        :param throttle: Specific throttle to check. If None and transaction is bound,
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

    async def get_available_quota(
        self,
        throttle: typing.Optional[Throttle[HTTPConnectionT]] = None,
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> typing.Optional[float]:
        """
        Get the available quota for a specific throttle.

        :param throttle: The throttle to check. If None and transaction is bound to an owner,
            uses the owner throttle.
        :param context: Context for the throttle stat lookup.
        :return: Available quota, or None if unavailable.
        :raises ValueError: If no throttle specified and transaction is unbound.
        """
        _throttle = throttle if throttle is not None else self.owner
        if _throttle is None:
            raise ValueError(
                "No throttle specified. Either provide a throttle argument or "
                "create the transaction via `throttle.transaction()` to bind it to a throttle."
            )

        merged_context = dict(self.default_context or {})
        if context:
            merged_context.update(context)

        stat = await _throttle.stat(
            self.connection, merged_context if merged_context else None
        )
        return stat.hits_remaining if stat else None

    async def commit(self) -> None:
        """
        Apply all queued throttles.

        This commits all queued throttles in order, including any from
        nested child transactions.

        Note:
            If called manually (outside of context manager), locking is not applied.
            The lock is only held when using the context manager (`async with`).
            For manual commits with locking, acquire the lock yourself before calling commit.

        :raises `TransactionCommittedError`: If already committed.
        :raises `QuotaExhaustedError`: If quota exhausted and on_quota_exhausted="raise".
        """
        await self._commit()

    async def rollback(self) -> None:
        """
        Rollback the transaction, discarding all queued throttles.

        This clears the queue without applying any throttles and marks the
        transaction as rolled back. After rollback, no further operations
        (enqueue or commit) are allowed on this transaction.

        If the transaction holds a lock, it will be released.

        For nested transactions, rollback detaches the child from the parent
        (its throttles will not be merged into the parent).

        This method is idempotent, i.e., calling it multiple times has no additional effect.

        :raises `TransactionCommittedError`: If the transaction has already been committed.

        Example:
        ```python
        async with throttle.transaction(conn, commit_on_exit=False) as tx:
            await tx(cost=5)
            await tx(throttle2, cost=3)

            if not await validate_operation():
                await tx.rollback()  # Discard queued throttles
                return error_response()

            await perform_operation()
            await tx.commit()  # Manually commit
        ```
        """
        if self._committed:
            raise TransactionCommittedError("Cannot rollback a committed transaction")

        if self._rolled_back:
            return  # Already rolled back, idempotent

        # Rollback all uncommitted children first
        for child in self._children:
            if not child._committed and not child._rolled_back:
                await child.rollback()

        # Clear the queue
        self._queue.clear()
        self._total_queued_cost = 0

        # Detach from parent if nested
        if self.parent is not None and self in self.parent._children:
            self.parent._children.remove(self)

        # Mark as rolled back
        self._rolled_back = True

        # Release lock early if we're holding one
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    async def _commit(self) -> None:
        """
        Private commit implementation.

        Assumes lock is already held if locking is enabled (via context manager).
        """
        if self._rolled_back:
            raise TransactionRolledBackError("Cannot commit a rolled-back transaction")
        if self._committed:
            raise TransactionCommittedError("Transaction has already been committed")

        # First, commit all children that haven't been committed
        for child in self._children:
            if not child._committed:
                child._merge_into_parent(mark_as_committed=True)

        # Apply all queued throttles
        # Note: Lock is already held if using context manager
        for entry in self._queue:
            await self._apply_throttle(entry)

        self._committed = True

    async def _should_retry(
        self,
        entry: _QueuedThrottle[HTTPConnectionT],
        exc: BaseException,
        attempt: int,
    ) -> bool:
        """
        Determine if we should retry based on retry_on configuration.

        :param entry: The queued throttle entry.
        :param exc: The exception that occurred.
        :param attempt: Current attempt number.
        :return: True if should retry, False otherwise.
        """
        # If no `retry_on` is specified, we can retry on any exception
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

    async def _apply_throttle(self, entry: _QueuedThrottle[HTTPConnectionT]) -> None:
        """Apply a single throttle entry with retry logic."""
        attempt = 0
        delay_attempt = 0  # For quota exhausted delay backoff
        # Use precomputed values
        max_attempts = entry.max_attempts
        required_cost = entry.resolved_cost
        quota_exhausted_mode = self._quota_exhausted_mode
        delay_config = self._delay_config

        while attempt < max_attempts:
            attempt += 1
            try:
                # Check quota before applying
                if quota_exhausted_mode != "force":
                    stat = await entry.throttle.stat(self.connection, entry.context)
                    if stat is not None and stat.hits_remaining < required_cost:
                        if quota_exhausted_mode == "raise":
                            raise QuotaExhaustedError(
                                f"Insufficient quota for throttle '{entry.throttle.uid}'",
                                throttle=entry.throttle,
                                required_cost=required_cost,
                                available_quota=stat.hits_remaining,
                            )
                        elif quota_exhausted_mode == "skip":
                            return  # Skip this throttle
                        elif quota_exhausted_mode == "delay":
                            # Wait and retry
                            delay_attempt += 1
                            if callable(delay_config):
                                delay = delay_config(delay_attempt, 1.0)
                            else:
                                delay = typing.cast(float, delay_config)
                            await asyncio.sleep(delay)
                            continue  # Retry quota check

                # Apply the throttle
                await entry.throttle(
                    self.connection,
                    cost=entry.cost,
                    context=entry.context,
                )
                # Track applied cost using precomputed value
                self._total_applied_cost += required_cost
                return

            except QuotaExhaustedError:
                raise

            except Exception as exc:
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
        on_quota_exhausted: typing.Optional[
            typing.Union[
                typing.Literal["raise", "force", "skip"],
                typing.Tuple[
                    typing.Literal["delay"], typing.Union[float, "BackoffStrategy"]
                ],
            ]
        ] = None,
        commit_on_exit: bool = True,
        lock: typing.Union[bool, str, None] = None,
        lock_config: typing.Optional[LockConfig] = None,
    ) -> "ThrottleTransaction[HTTPConnectionT]":
        """
        Create a nested child transaction.

        Child transactions merge their queued throttles into the parent
        when they exit successfully. The parent applies all throttles
        (including children's) when it commits.

        :param context: Override default context for the child.
        :param apply_on_error: Override error handling (inherits from parent if None).
        :param on_quota_exhausted: Override quota handling (inherits from parent if None).
        :param commit_on_exit: Whether child auto-commits to parent on exit.
        :param lock: Controls locking for this nested transaction.
            - `None` (default): No lock (operates under parent's lock context)
            - `False`: Explicitly disable locking
            - `True`: Use child's owner throttle UID as lock key (if bound)
            - `str`: Use the provided string as the lock key

            .. warning:: **Pitfalls of nested transaction locks:**

                1. **Deadlock Risk**: If you use the same lock key as the parent,
                   the child will deadlock waiting for a lock the parent holds.
                   A warning is emitted if this is detected.

                2. **Lock Ordering**: If parent holds lock "A" and child acquires "B",
                   while another transaction holds "B" and wants "A", deadlock occurs.
                   Always acquire locks in a consistent order across your application.

                3. **Partial Atomicity**: The child releases its lock on exit, but the
                   parent may fail later. This means the child's protected resources
                   could be accessed by other requests before the parent commits.

                4. **Use Case**: Nested locks are useful when the child operates on a
                   *different* resource than the parent and needs independent protection.

        :param lock_config: Configuration for lock acquisition (ttl, blocking, etc.).
        :return: A new child transaction.

        Example (no lock - default, safest):

        ```python
        async with throttle_a.transaction(conn) as parent:
            await parent(cost=1)

            async with parent.nested() as child:
                # Child operates under parent's lock
                await child(cost=2)
        ```

        Example (child with different lock for different resource):

        ```python
        async with throttle_a.transaction(conn) as parent:
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
            child_lock_key = f"tx:{self.owner.uid}" if self.owner else None
        else:
            child_lock_key = f"tx:{lock}"

        # Warn about potential deadlock if child uses same lock as parent
        if child_lock_key is not None and child_lock_key == self._lock_key:
            warnings.warn(
                f"Nested transaction is using the same lock key as its parent "
                f"({child_lock_key!r}). This will cause a deadlock if the parent lock is not re-entrant, "
                f"because the parent already holds this lock. Use a different lock key or "
                f"set lock=None to operate under the parent's lock context.",
                UserWarning,
                stacklevel=2,
            )

        return self.__class__(
            connection=self.connection,
            context=merged_context if merged_context else None,
            owner=self.owner,  # Inherit owner from parent
            apply_on_error=(
                apply_on_error if apply_on_error is not None else self.apply_on_error
            ),
            on_quota_exhausted=(
                on_quota_exhausted
                if on_quota_exhausted is not None
                else self.on_quota_exhausted  # type: ignore[arg-type]
            ),
            commit_on_exit=commit_on_exit,
            lock=lock if lock is not None else False,
            lock_config=lock_config,
            parent=self,
        )

    transaction = nested  # Alias for `nested()` method.

    def __repr__(self) -> str:
        owner_info = f"owner={self.owner.uid!r} " if self.owner else ""
        state = (
            "rolled_back"
            if self._rolled_back
            else ("committed" if self._committed else "active")
        )
        return (
            f"<{self.__class__.__name__} "
            f"{owner_info}"
            f"queued={len(self._queue)} "
            f"state={state} "
            f"nested={self.is_nested} "
            f"depth={self.nesting_depth}>"
        )
