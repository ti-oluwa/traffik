"""Tests for traffik configuration utilities."""

import os

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.types import LockConfig
from traffik.utils import (
    get_lock_blocking,
    get_lock_blocking_timeout,
    set_lock_blocking,
    set_lock_blocking_timeout,
)


class TestBlockingConfiguration:
    """Tests for blocking configuration utilities."""

    def test_get_lock_blocking_default(self):
        """Test get_lock_blocking returns True by default."""
        # Clear env var if set
        if "TRAFFIK_DEFAULT_BLOCKING" in os.environ:
            del os.environ["TRAFFIK_DEFAULT_BLOCKING"]

        result = get_lock_blocking()
        assert result is True, "Default blocking should be True"

    def test_get_lock_blocking_truthy_values(self):
        """Test get_lock_blocking recognizes truthy string values."""
        truthy_values = ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "ON"]

        for value in truthy_values:
            os.environ["TRAFFIK_DEFAULT_BLOCKING"] = value
            result = get_lock_blocking()
            assert result is True, f"'{value}' should be recognized as True"

    def test_get_lock_blocking_falsy_values(self):
        """Test get_lock_blocking recognizes falsy string values."""
        falsy_values = ["0", "false", "False", "FALSE", "no", "off", ""]

        for value in falsy_values:
            os.environ["TRAFFIK_DEFAULT_BLOCKING"] = value
            result = get_lock_blocking()
            assert result is False, f"'{value}' should be recognized as False"

    def test_set_lock_blocking_true(self):
        """Test set_lock_blocking correctly sets True."""
        set_lock_blocking(True)
        assert os.environ["TRAFFIK_DEFAULT_BLOCKING"] == "1"
        assert get_lock_blocking() is True

    def test_set_lock_blocking_false(self):
        """Test set_lock_blocking correctly sets False."""
        set_lock_blocking(False)
        assert os.environ["TRAFFIK_DEFAULT_BLOCKING"] == "0"
        assert get_lock_blocking() is False

    def test_blocking_setting_roundtrip(self):
        """Test setting and getting blocking setting works consistently."""
        original = get_lock_blocking()

        # Toggle it
        set_lock_blocking(not original)
        assert get_lock_blocking() == (not original)

        # Toggle back
        set_lock_blocking(original)
        assert get_lock_blocking() == original


class TestBlockingTimeout:
    """Tests for blocking timeout configuration utilities."""

    def test_get_lock_blocking_timeout_default(self):
        """Test get_lock_blocking_timeout returns None by default."""
        if "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT" in os.environ:
            del os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"]

        result = get_lock_blocking_timeout()
        assert result is None, "Default timeout should be None"

    def test_get_lock_blocking_timeout_valid_values(self):
        """Test get_lock_blocking_timeout parses valid float values."""
        test_cases = [
            ("0", 0.0),
            ("1.5", 1.5),
            ("10", 10.0),
            ("0.001", 0.001),
            ("99.999", 99.999),
        ]

        for env_value, expected in test_cases:
            os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] = env_value
            result = get_lock_blocking_timeout()
            assert result == expected, f"'{env_value}' should parse to {expected}"

    def test_get_lock_blocking_timeout_invalid_negative(self):
        """Test get_lock_blocking_timeout raises ValueError for negative values."""
        os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] = "-1.0"

        with pytest.raises(ValueError, match="non-negative"):
            get_lock_blocking_timeout()

    def test_get_lock_blocking_timeout_invalid_non_numeric(self):
        """Test get_lock_blocking_timeout raises ValueError for non-numeric values."""
        invalid_values = ["abc", "1.2.3", "true", ""]

        for value in invalid_values:
            os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] = value
            with pytest.raises(ValueError, match="Invalid value"):
                get_lock_blocking_timeout()

    def test_set_lock_blocking_timeout_valid(self):
        """Test set_lock_blocking_timeout correctly sets valid timeout."""
        set_lock_blocking_timeout(5.0)
        assert os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] == "5.0"
        assert get_lock_blocking_timeout() == 5.0

    def test_set_lock_blocking_timeout_none_unsets(self):
        """Test set_lock_blocking_timeout(None) removes the environment variable."""
        # First set a value
        set_lock_blocking_timeout(3.0)
        assert "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT" in os.environ

        # Then unset it
        set_lock_blocking_timeout(None)
        assert "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT" not in os.environ
        assert get_lock_blocking_timeout() is None

    def test_set_lock_blocking_timeout_zero(self):
        """Test set_lock_blocking_timeout accepts zero."""
        set_lock_blocking_timeout(0.0)
        assert get_lock_blocking_timeout() == 0.0

    def test_set_lock_blocking_timeout_negative_raises(self):
        """Test set_lock_blocking_timeout raises ValueError for negative values."""
        with pytest.raises(ValueError, match="non-negative"):
            set_lock_blocking_timeout(-1.0)

    def test_blocking_timeout_roundtrip(self):
        """Test setting and getting timeout works consistently."""
        test_timeouts = [None, 0.0, 1.5, 10.0, 99.99]

        for timeout in test_timeouts:
            set_lock_blocking_timeout(timeout)
            result = get_lock_blocking_timeout()
            assert result == timeout, f"Roundtrip failed for {timeout}"


class TestConfigurationIntegration:
    """Test configuration utilities work together."""

    def test_independent_configuration(self):
        """Test blocking setting and timeout are independent."""
        set_lock_blocking(True)
        set_lock_blocking_timeout(5.0)

        assert get_lock_blocking() is True
        assert get_lock_blocking_timeout() == 5.0

        set_lock_blocking(False)
        assert get_lock_blocking() is False
        assert get_lock_blocking_timeout() == 5.0  # Should not change

        set_lock_blocking_timeout(None)
        assert get_lock_blocking() is False  # Should not change
        assert get_lock_blocking_timeout() is None

    def test_environment_variable_names(self):
        """Test correct environment variable names are used."""
        from traffik.utils import (
            DEFAUL_BLOCKING_SETTING_ENV_VAR,
            DEFAULT_BLOCKING_TIMEOUT_ENV_VAR,
        )

        assert DEFAUL_BLOCKING_SETTING_ENV_VAR == "TRAFFIK_DEFAULT_BLOCKING"
        assert DEFAULT_BLOCKING_TIMEOUT_ENV_VAR == "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"

        # Test they're actually used
        os.environ["TRAFFIK_DEFAULT_BLOCKING"] = "0"
        os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] = "2.5"

        assert get_lock_blocking() is False
        assert get_lock_blocking_timeout() == 2.5


@pytest.mark.anyio
async def test_configuration_with_strategy():
    """Test configuration utilities integrate with strategy LockConfig."""
    # Set global defaults
    set_lock_blocking(False)
    set_lock_blocking_timeout(1.5)

    # Create strategy that uses defaults
    strategy = FixedWindowStrategy(
        lock_config=LockConfig(
            blocking=get_lock_blocking(),
            blocking_timeout=get_lock_blocking_timeout(),
        )
    )

    assert strategy.lock_config["blocking"] is False  # type: ignore[typeddict-item]
    assert strategy.lock_config["blocking_timeout"] == 1.5  # type: ignore[typeddict-item]

    # Test it works
    backend = InMemoryBackend()
    async with backend(close_on_exit=True):
        rate = Rate.parse("10/s")
        wait = await strategy("test:key", rate, backend)
        assert wait == 0.0


@pytest.mark.anyio
async def test_configuration_strategy_override():
    """Test strategy can override global configuration."""
    # Set global defaults
    set_lock_blocking(False)
    set_lock_blocking_timeout(1.0)

    # Create strategy that overrides defaults
    strategy = FixedWindowStrategy(
        lock_config=LockConfig(
            blocking=True,  # Override global False
            blocking_timeout=5.0,  # Override global 1.0
        )
    )

    assert strategy.lock_config["blocking"] is True  # type: ignore[typeddict-item]
    assert strategy.lock_config["blocking_timeout"] == 5.0  # type: ignore[typeddict-item]

    # Verify global settings unchanged
    assert get_lock_blocking() is False
    assert get_lock_blocking_timeout() == 1.0


def test_configuration_cleanup():
    """Clean up environment variables after tests."""
    # This test runs last to clean up
    if "TRAFFIK_DEFAULT_BLOCKING" in os.environ:
        del os.environ["TRAFFIK_DEFAULT_BLOCKING"]
    if "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT" in os.environ:
        del os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"]

    assert get_lock_blocking() is True  # Back to default
    assert get_lock_blocking_timeout() is None  # Back to default
