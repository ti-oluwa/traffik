"""Test suite for the `Rate` class in `traffik.rates` module."""

import pytest

from traffik.rates import Rate


def test_rate_init_with_seconds() -> None:
    rate = Rate(limit=5, seconds=10)
    assert rate.limit == 5
    assert rate.seconds == 10
    assert rate.expire == 10000  # milliseconds


def test_rate_init_with_minutes() -> None:
    rate = Rate(limit=100, minutes=1)
    assert rate.limit == 100
    assert rate.minutes == 1
    assert rate.expire == 60000


def test_rate_init_with_hours() -> None:
    rate = Rate(limit=1000, hours=1)
    assert rate.limit == 1000
    assert rate.hours == 1
    assert rate.expire == 3600000


def test_rate_init_with_milliseconds() -> None:
    rate = Rate(limit=10, milliseconds=500)
    assert rate.limit == 10
    assert rate.milliseconds == 500
    assert rate.expire == 500


def test_rate_init_with_mixed_units() -> None:
    rate = Rate(limit=5, hours=1, minutes=30, seconds=45)
    expected_expire = 3600000 + 1800000 + 45000  # 5445000 ms
    assert rate.expire == expected_expire


def test_unlimited_rate() -> None:
    rate = Rate(limit=0, seconds=0)
    assert rate.unlimited is True
    assert rate.rps == float("inf")


def test_rate_validation_negative_limit() -> None:
    with pytest.raises(ValueError):
        Rate(limit=-1, seconds=10)


def test_rate_validation_expire_with_zero_limit() -> None:
    with pytest.raises(ValueError):
        Rate(limit=0, seconds=10)


def test_rate_validation_zero_expire_with_limit() -> None:
    with pytest.raises(ValueError):
        Rate(limit=10, seconds=0)


# Rate metric properties tests
def test_rps_calculation() -> None:
    rate = Rate(limit=10, seconds=1)
    assert rate.rps == 10.0


def test_rpm_calculation() -> None:
    rate = Rate(limit=60, minutes=1)
    assert rate.rpm == 60.0


def test_rph_calculation() -> None:
    rate = Rate(limit=3600, hours=1)
    assert rate.rph == 3600.0


def test_rpd_calculation() -> None:
    rate = Rate(limit=86400, hours=24)
    assert rate.rpd == 86400.0


# Rate.parse() with simple format: <limit>/<unit>
def test_parse_seconds_short() -> None:
    rate = Rate.parse("5/s")
    assert rate.limit == 5
    assert rate.seconds == 1
    assert rate.expire == 1000


def test_parse_seconds_full() -> None:
    rate = Rate.parse("10/seconds")
    assert rate.limit == 10
    assert rate.seconds == 1


def test_parse_minutes_short() -> None:
    rate = Rate.parse("100/m")
    assert rate.limit == 100
    assert rate.minutes == 1
    assert rate.expire == 60000


def test_parse_minutes_full() -> None:
    rate = Rate.parse("50/minutes")
    assert rate.limit == 50
    assert rate.minutes == 1


def test_parse_hours_short() -> None:
    rate = Rate.parse("1000/h")
    assert rate.limit == 1000
    assert rate.hours == 1
    assert rate.expire == 3600000


def test_parse_hours_full() -> None:
    rate = Rate.parse("500/hours")
    assert rate.limit == 500
    assert rate.hours == 1


def test_parse_days_short() -> None:
    rate = Rate.parse("10000/d")
    assert rate.limit == 10000
    assert rate.hours == 24
    assert rate.expire == 86400000


def test_parse_days_full() -> None:
    rate = Rate.parse("5000/days")
    assert rate.limit == 5000
    assert rate.hours == 24


# Rate.parse() with advanced format: <limit>/<period><unit>
def test_parse_with_period_seconds() -> None:
    rate = Rate.parse("2/5s")
    assert rate.limit == 2
    assert rate.seconds == 5
    assert rate.expire == 5000


def test_parse_with_period_minutes() -> None:
    rate = Rate.parse("10/30m")
    assert rate.limit == 10
    assert rate.minutes == 30
    assert rate.expire == 1800000


def test_parse_with_period_hours() -> None:
    rate = Rate.parse("100/12h")
    assert rate.limit == 100
    assert rate.hours == 12
    assert rate.expire == 43200000


def test_parse_with_period_milliseconds() -> None:
    rate = Rate.parse("1000/500ms")
    assert rate.limit == 1000
    assert rate.milliseconds == 500
    assert rate.expire == 500


def test_parse_with_period_and_space() -> None:
    rate = Rate.parse("10/30 seconds")
    assert rate.limit == 10
    assert rate.seconds == 30
    assert rate.expire == 30000


def test_parse_with_full_words() -> None:
    rate = Rate.parse("5/15 minutes")
    assert rate.limit == 5
    assert rate.minutes == 15
    assert rate.expire == 900000


# Rate.parse() edge cases and variations
def test_parse_case_insensitive() -> None:
    rate1 = Rate.parse("5/S")
    rate2 = Rate.parse("5/s")
    assert rate1.limit == rate2.limit
    assert rate1.expire == rate2.expire


def test_parse_with_whitespace() -> None:
    rate = Rate.parse("  10  /  5s  ")
    assert rate.limit == 10
    assert rate.seconds == 5


def test_parse_alternative_units() -> None:
    """Test all unit variations"""
    assert Rate.parse("1/sec").expire == 1000
    assert Rate.parse("1/second").expire == 1000
    assert Rate.parse("1/min").expire == 60000
    assert Rate.parse("1/minute").expire == 60000
    assert Rate.parse("1/hr").expire == 3600000
    assert Rate.parse("1/hour").expire == 3600000
    assert Rate.parse("1/day").expire == 86400000
    assert Rate.parse("1/ms").expire == 1
    assert Rate.parse("1/millisecond").expire == 1


def test_parse_large_numbers() -> None:
    rate = Rate.parse("1000000/100000s")
    assert rate.limit == 1000000
    assert rate.seconds == 100000


# Rate.parse() error handling
def test_parse_invalid_format_no_slash() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5s")


def test_parse_invalid_format_multiple_slashes() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/10/s")


def test_parse_empty_string() -> None:
    with pytest.raises(ValueError):
        Rate.parse("")


def test_parse_whitespace_only() -> None:
    with pytest.raises(ValueError):
        Rate.parse("   ")


def test_parse_invalid_limit_not_number() -> None:
    with pytest.raises(ValueError):
        Rate.parse("abc/s")


def test_parse_invalid_limit_negative() -> None:
    with pytest.raises(ValueError):
        Rate.parse("-5/s")


def test_parse_invalid_limit_float() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5.5/s")


def test_parse_empty_period() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/")


def test_parse_invalid_unit() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/x")


def test_parse_invalid_period_format() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/5.5s")


def test_parse_zero_period_multiplier() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/0s")


def test_parse_negative_period_multiplier() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/-5s")


def test_parse_non_string_input() -> None:
    with pytest.raises(ValueError):
        Rate.parse(123)  # type: ignore


def test_parse_invalid_characters() -> None:
    with pytest.raises(ValueError):
        Rate.parse("5/5s@")


# Rate comparisons and conversions
def test_equivalent_rates() -> None:
    """Test that different representations of same rate are equivalent"""
    rate1 = Rate.parse("60/m")  # 60 per minute
    rate2 = Rate.parse("1/s")  # 1 per second
    # Both should be 1 request per second
    assert rate1 == rate2


def test_rate_conversion() -> None:
    """Test rate conversions between units"""
    rate = Rate.parse("120/h")  # 120 per hour
    assert rate.rpm == 2.0  # 2 per minute
    assert rate.rps == pytest.approx(0.0333, abs=0.001)  # ~0.033 per second


def test_complex_rate_rps() -> None:
    """Test RPS calculation for complex rates"""
    rate = Rate.parse("5/30s")
    assert rate.rps == pytest.approx(0.1667, abs=0.001)
