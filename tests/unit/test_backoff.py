from traffik.backoff import (
    ConstantBackoff,
    ExponentialBackoff,
    JitterType,
    LinearBackoff,
    LogarithmicBackoff,
)


def test_constant_backoff():
    """Test ConstantBackoff strategy."""
    base_delay = 0.5

    # Test that delay remains constant
    delay1 = ConstantBackoff(1, base_delay)
    delay2 = ConstantBackoff(2, base_delay)
    delay3 = ConstantBackoff(3, base_delay)

    assert delay1 == base_delay
    assert delay2 == base_delay
    assert delay3 == base_delay


def test_linear_backoff():
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


def test_exponential_backoff():
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


def test_exponential_backoff_with_max():
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


def test_logarithmic_backoff():
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


def test_linear_backoff_with_no_jitter():
    """Test LinearBackoff with no jitter."""
    backoff = LinearBackoff(increment=1.0, jitter=JitterType.NONE)
    base_delay = 1.0

    # Without jitter, delays should be exact
    delay1 = backoff(1, base_delay)  # 1.0
    delay2 = backoff(2, base_delay)  # 2.0
    delay3 = backoff(3, base_delay)  # 3.0

    assert delay1 == 1.0
    assert delay2 == 2.0
    assert delay3 == 3.0


def test_linear_backoff_with_full_jitter():
    """Test LinearBackoff with full jitter."""
    backoff = LinearBackoff(increment=1.0, jitter=JitterType.FULL)
    base_delay = 1.0

    # With full jitter, delays should be between 0 and calculated delay
    for attempt in range(1, 4):
        delay = backoff(attempt, base_delay)
        expected_delay = base_delay + (attempt - 1) * 1.0
        assert 0 <= delay <= expected_delay


def test_linear_backoff_with_equal_jitter():
    """Test LinearBackoff with equal jitter."""
    backoff = LinearBackoff(increment=1.0, jitter=JitterType.EQUAL)
    base_delay = 1.0

    # With equal jitter, delays should be between delay/2 and delay
    for attempt in range(1, 4):
        delay = backoff(attempt, base_delay)
        expected_delay = base_delay + (attempt - 1) * 1.0
        assert expected_delay / 2 <= delay <= expected_delay


def test_exponential_backoff_with_full_jitter():
    """Test ExponentialBackoff with full jitter."""
    backoff = ExponentialBackoff(multiplier=2.0, jitter=JitterType.FULL)
    base_delay = 1.0

    # With full jitter, delays should be between 0 and calculated delay
    for attempt in range(1, 4):
        delay = backoff(attempt, base_delay)
        expected_delay = base_delay * (2.0 ** (attempt - 1))
        assert 0 <= delay <= expected_delay


def test_exponential_backoff_with_equal_jitter():
    """Test ExponentialBackoff with equal jitter."""
    backoff = ExponentialBackoff(multiplier=2.0, jitter=JitterType.EQUAL)
    base_delay = 1.0

    # With equal jitter, delays should be between delay/2 and delay
    for attempt in range(1, 4):
        delay = backoff(attempt, base_delay)
        expected_delay = base_delay * (2.0 ** (attempt - 1))
        assert expected_delay / 2 <= delay <= expected_delay


def test_exponential_backoff_with_jitter_and_max():
    """Test ExponentialBackoff with jitter and max_delay cap."""
    backoff = ExponentialBackoff(multiplier=2.0, max_delay=3.0, jitter=JitterType.FULL)
    base_delay = 1.0

    # Delays should respect max_delay even with jitter
    for attempt in range(1, 6):
        delay = backoff(attempt, base_delay)
        assert 0 <= delay <= 3.0


def test_logarithmic_backoff_with_full_jitter():
    """Test LogarithmicBackoff with full jitter."""
    backoff = LogarithmicBackoff(base=2.0, jitter=JitterType.FULL)
    base_delay = 1.0

    # With full jitter, delays should be between 0 and calculated delay
    for attempt in range(1, 4):
        delay = backoff(attempt, base_delay)
        # Expected delay without jitter: base_delay * log2(attempt + 1)
        expected_delay = backoff.__class__(base=2.0, jitter=JitterType.NONE)(
            attempt, base_delay
        )
        assert 0 <= delay <= expected_delay


def test_linear_backoff_jitter_as_bool():
    """Test LinearBackoff with jitter as boolean (True defaults to EQUAL)."""
    backoff_true = LinearBackoff(increment=1.0, jitter=True)
    backoff_false = LinearBackoff(increment=1.0, jitter=False)
    backoff_equal = LinearBackoff(increment=1.0, jitter=JitterType.EQUAL)
    backoff_none = LinearBackoff(increment=1.0, jitter=JitterType.NONE)
    base_delay = 1.0

    # jitter=False should behave like JitterType.NONE
    for attempt in range(1, 4):
        delay_false = backoff_false(attempt, base_delay)
        delay_none = backoff_none(attempt, base_delay)
        assert delay_false == delay_none

    # jitter=True should behave like JitterType.EQUAL (mostly)
    # Just verify they both produce jittered results in expected range
    for attempt in range(1, 4):
        delay_true = backoff_true(attempt, base_delay)
        delay_equal = backoff_equal(attempt, base_delay)
        expected_delay = base_delay + (attempt - 1) * 1.0
        assert expected_delay / 2 <= delay_true <= expected_delay
        assert expected_delay / 2 <= delay_equal <= expected_delay
