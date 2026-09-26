"""
Tests for `ThrottleBackend.lock(...)`'s TTL enforcement.
`local_ttl_factor` validation, and `LockTimeoutError` surfacing when the TTL watchdog fires.
"""

import asyncio

import pytest

from tests.conftest import BackendGen
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.exceptions import LockTimeoutError


@pytest.mark.anyio
@pytest.mark.backend
class TestLocalTTLFactorValidation:
    """`local_ttl_factor` must be in `(0, 1]` when `enforce_ttl_locally` and
    `ttl` are both set; the validation lives in the shared base class, so a
    single fast backend is enough to exercise it.
    """

    async def test_zero_raises(self) -> None:
        backend = InMemoryBackend()
        with pytest.raises(ValueError, match="local_ttl_factor"):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=True, local_ttl_factor=0.0)

    async def test_negative_raises(self) -> None:
        backend = InMemoryBackend()
        with pytest.raises(ValueError, match="local_ttl_factor"):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=True, local_ttl_factor=-0.1)

    async def test_above_one_raises(self) -> None:
        backend = InMemoryBackend()
        with pytest.raises(ValueError, match="local_ttl_factor"):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=True, local_ttl_factor=1.5)

    async def test_one_is_valid(self) -> None:
        """Regression test: `1.0` (no reduction) must be accepted, not just
        values strictly less than 1 -- see `TestInMemoryAndMultiprocessLockTTLDefaults`
        for why this specific value matters in practice.
        """
        backend = InMemoryBackend()
        async with backend(persistent=False, close_on_exit=True):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=True, local_ttl_factor=1.0)

    async def test_fraction_is_valid(self) -> None:
        backend = InMemoryBackend()
        async with backend(persistent=False, close_on_exit=True):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=True, local_ttl_factor=0.5)

    async def test_not_validated_when_ttl_is_none(self) -> None:
        """No `ttl` means nothing to enforce locally, so an otherwise-invalid
        `local_ttl_factor` is never even inspected.
        """
        backend = InMemoryBackend()
        async with backend(persistent=False, close_on_exit=True):
            backend.lock("l", ttl=None, enforce_ttl_locally=True, local_ttl_factor=0.0)

    async def test_not_validated_when_enforce_ttl_locally_is_false(self) -> None:
        backend = InMemoryBackend()
        async with backend(persistent=False, close_on_exit=True):
            backend.lock("l", ttl=1.0, enforce_ttl_locally=False, local_ttl_factor=0.0)


@pytest.mark.anyio
@pytest.mark.backend
class TestInMemoryAndMultiprocessLockTTLDefaults:
    """Regression tests: `InMemoryBackend.lock(...)`/`MultiProcessInMemoryBackend.lock(...)`
    default to `enforce_ttl_locally=True, local_ttl_factor=1.0` (appropriate, since
    `get_lock(...)` on both ignores `ttl` entirely -- local enforcement is the *only*
    TTL mechanism). The old base-class validation rejected `1.0`, so simply passing
    an explicit `ttl` with no other kwargs -- the most ordinary usage -- always raised.
    """

    @pytest.mark.parametrize(
        "backend_cls", [InMemoryBackend, MultiProcessInMemoryBackend]
    )
    async def test_lock_with_explicit_ttl_and_no_other_kwargs(
        self, backend_cls: type
    ) -> None:
        backend = backend_cls(namespace="lock-ttl-default-test")
        async with backend(persistent=False, close_on_exit=True):
            async with backend.lock("lock:ttl-default", ttl=0.5) as lock_context:
                assert lock_context._acquired
            assert not lock_context._acquired


@pytest.mark.anyio
@pytest.mark.backend
class TestLockTimeoutErrorSurfacing:
    """`LockTimeoutError` must always surface once the TTL watchdog fires,
    regardless of what the protected body does with the `CancelledError` it
    raises -- propagate it untouched, convert it into a different exception,
    or swallow it and return normally. Exercised generically across every
    backend (with `enforce_ttl_locally=True` passed explicitly for backends
    where it isn't the default) since the fix lives in the shared lock-context
    machinery, not in any one backend.
    """

    async def test_propagated_untouched(self, backends: BackendGen) -> None:
        for backend in backends(namespace="lock_timeout_propagate"):
            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(LockTimeoutError):
                    async with backend.lock("l", ttl=0.05, enforce_ttl_locally=True):
                        await asyncio.sleep(2)

    async def test_converted_to_different_exception(self, backends: BackendGen) -> None:
        """A body that catches the cancellation and raises something else
        (e.g. via a broad `except Exception`) must not be able to hide the
        fact that its critical section was forcibly cut short.
        """
        for backend in backends(namespace="lock_timeout_convert"):
            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(LockTimeoutError):
                    async with backend.lock("l", ttl=0.05, enforce_ttl_locally=True):
                        try:
                            await asyncio.sleep(2)
                        except asyncio.CancelledError:
                            raise ValueError("converted") from None

    async def test_swallowed_silently(self, backends: BackendGen) -> None:
        """A body that catches the cancellation and returns normally (no
        exception at all) must still surface the timeout -- otherwise the
        caller has no way to know their critical section didn't finish.
        """
        for backend in backends(namespace="lock_timeout_swallow"):
            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(LockTimeoutError):
                    async with backend.lock("l", ttl=0.05, enforce_ttl_locally=True):
                        try:
                            await asyncio.sleep(2)
                        except asyncio.CancelledError:
                            pass

    async def test_not_raised_when_body_completes_in_time(
        self, backends: BackendGen
    ) -> None:
        for backend in backends(namespace="lock_timeout_no_fire"):
            async with backend(persistent=False, close_on_exit=True):
                async with backend.lock("l", ttl=5.0, enforce_ttl_locally=True):
                    await asyncio.sleep(0.01)

    async def test_ordinary_body_exception_not_masked(
        self, backends: BackendGen
    ) -> None:
        """A body exception unrelated to any timeout (the watchdog never
        fires) must propagate as-is, not be replaced by `LockTimeoutError`.
        """
        for backend in backends(namespace="lock_timeout_ordinary_exc"):
            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(ValueError, match="ordinary failure"):
                    async with backend.lock("l", ttl=5.0, enforce_ttl_locally=True):
                        raise ValueError("ordinary failure")

    async def test_lock_released_and_reusable_after_ttl_fires(
        self, backends: BackendGen
    ) -> None:
        """Mutual exclusion must recover: once the TTL fires and releases the
        lock, a fresh acquisition of the same name must succeed.
        """
        for backend in backends(namespace="lock_timeout_recovery"):
            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(LockTimeoutError):
                    async with backend.lock(
                        "shared", ttl=0.05, enforce_ttl_locally=True
                    ):
                        await asyncio.sleep(2)

                async with backend.lock("shared", ttl=5.0, enforce_ttl_locally=True):
                    pass  # Re-acquiring here proves the earlier lock was released.
