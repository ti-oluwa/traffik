"""Tests for `traffik.strategies._serde`'s struct-based codecs."""

import struct

import pytest

from traffik.strategies._serde import (
    _decode_float_list,
    _decode_three_float_records,
    _decode_two_float_records,
    _decode_two_float_records_and_float,
    _decode_two_floats,
    _encode_float_list,
    _encode_three_float_records,
    _encode_two_float_records,
    _encode_two_float_records_and_float,
    _encode_two_floats,
    _iter_float_list,
    _iter_three_float_records,
    _iter_two_float_records,
)


class TestTwoFloatRecords:
    """Tests for `_encode_two_float_records`/`_decode_two_float_records`."""

    def test_round_trip(self):
        records = [(1.5, 2.5), (3.0, 4.0), (-1.1, 0.0)]
        raw = _encode_two_float_records(records)
        assert _decode_two_float_records(raw) == records

    def test_empty_sequence(self):
        raw = _encode_two_float_records([])
        assert _decode_two_float_records(raw) == []

    def test_iterator_matches_decode(self):
        records = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
        raw = _encode_two_float_records(records)
        assert list(_iter_two_float_records(raw)) == _decode_two_float_records(raw)


class TestThreeFloatRecords:
    """Tests for `_encode_three_float_records`/`_decode_three_float_records`."""

    def test_round_trip(self):
        records = [(1.0, 2.0, 3.0), (4.5, 5.5, 6.5)]
        raw = _encode_three_float_records(records)
        assert _decode_three_float_records(raw) == records

    def test_empty_sequence(self):
        raw = _encode_three_float_records([])
        assert _decode_three_float_records(raw) == []

    def test_iterator_matches_decode(self):
        records = [(1.0, 2.0, 3.0), (7.0, 8.0, 9.0)]
        raw = _encode_three_float_records(records)
        assert list(_iter_three_float_records(raw)) == _decode_three_float_records(raw)


class TestTwoFloatRecordsAndFloat:
    """Tests for `_encode_two_float_records_and_float`/its decoder."""

    def test_round_trip(self):
        records = [(1.0, 2.0), (3.0, 4.0)]
        raw = _encode_two_float_records_and_float(records, 42.5)
        decoded_records, last_float = _decode_two_float_records_and_float(raw)
        assert decoded_records == records
        assert last_float == 42.5

    def test_empty_records_with_trailing_float(self):
        raw = _encode_two_float_records_and_float([], 7.0)
        decoded_records, last_float = _decode_two_float_records_and_float(raw)
        assert decoded_records == []
        assert last_float == 7.0


class TestFloatList:
    """Tests for `_encode_float_list`/`_decode_float_list`."""

    def test_round_trip(self):
        values = [1.0, 2.5, -3.75, 0.0]
        raw = _encode_float_list(values)
        assert _decode_float_list(raw) == values

    def test_empty_list(self):
        raw = _encode_float_list([])
        assert _decode_float_list(raw) == []

    def test_iterator_matches_decode(self):
        values = [1.0, 2.0, 3.0]
        raw = _encode_float_list(values)
        assert list(_iter_float_list(raw)) == _decode_float_list(raw)


class TestTwoFloats:
    """Tests for `_encode_two_floats`/`_decode_two_floats`."""

    def test_round_trip(self):
        raw = _encode_two_floats(1.23, 4.56)
        assert _decode_two_floats(raw) == (1.23, 4.56)

    def test_negative_and_zero_values(self):
        raw = _encode_two_floats(-1.0, 0.0)
        assert _decode_two_floats(raw) == (-1.0, 0.0)


class TestCorruptData:
    """
    Documents current behavior on malformed input.

    None of these codecs validate the encoded record count against the
    actual buffer length before allocating -- a truncated or bit-flipped
    payload gets its leading bytes reinterpreted as a record count, which
    can be an arbitrarily large number. `_decode_two_floats`/`_decode_two_float_records_and_float`
    (fixed-size, no count prefix) fail with `struct.error` on short input, but
    the count-prefixed decoders (`_decode_float_list`, `_decode_two_float_records`,
    `_decode_three_float_records`) can raise `MemoryError` instead when garbage
    bytes decode to a huge count, since they eagerly pre-allocate a list of
    that size. Worth hardening with a sanity bound on the decoded count relative
    to the remaining buffer length before allocating.
    """

    def test_short_fixed_size_payload_raises_struct_error(self):
        with pytest.raises(struct.error):
            _decode_two_floats("short")

    def test_garbage_count_prefixed_payload_raises(self):
        with pytest.raises((struct.error, MemoryError)):
            _decode_float_list("junkdata_not_valid")
