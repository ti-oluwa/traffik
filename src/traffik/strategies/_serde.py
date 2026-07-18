"""Stratgies' serialization and deserialization utils"""

import struct
import typing

_FLOAT = struct.Struct("=d")
_TWO_FLOATS = struct.Struct("=dd")
_THREE_FLOATS = struct.Struct("=ddd")
_UINT32 = struct.Struct("=I")


def _encode_two_float_records(
    entries: typing.Sequence[typing.Tuple[float, float]],
) -> str:
    """
    Serialize a sequence of two-float pairs into a compact Latin-1
    encoded binary string.

    The binary layout is:

        uint32 record_count
        record_count x (double a, double b)

    :param entries: The two-float records to serialize.
    :returns: A Latin-1 encoded binary string representing the serialized
        records.
    """
    out = bytearray(_UINT32.size + len(entries) * _TWO_FLOATS.size)
    _UINT32.pack_into(out, 0, len(entries))

    offset = _UINT32.size
    for a, b in entries:
        _TWO_FLOATS.pack_into(out, offset, a, b)
        offset += _TWO_FLOATS.size
    return out.decode("latin-1")


def _decode_two_float_records(raw: str) -> typing.List[typing.Tuple[float, float]]:
    """
    Deserialize a Latin-1 encoded binary string into a list of float pairs.

    The encoded format consists of a 32-bit unsigned integer record count
    followed by consecutive two-float records, each stored as two
    double-precision floating-point values.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_two_float_records`.
    :returns: A list of two-float tuples.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _TWO_FLOATS.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} records")

    records: typing.List[typing.Tuple[float, float]] = [None] * count  # type: ignore[list-item]
    offset = _UINT32.size
    for index in range(count):
        records[index] = _TWO_FLOATS.unpack_from(view, offset)
        offset += _TWO_FLOATS.size
    return records


def _iter_two_float_records(raw: str) -> typing.Iterator[tuple[float, float]]:
    """
    Lazily iterate over float pairs stored in a Latin-1 encoded binary string.

    The encoded format consists of a 32-bit unsigned integer record count
    followed by consecutive two-float records. Records are unpacked
    on demand without first allocating a list.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_two_float_records`.
    :returns: An iterator yielding two-float tuples.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _TWO_FLOATS.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} records")

    offset = _UINT32.size
    for _ in range(count):
        yield _TWO_FLOATS.unpack_from(view, offset)
        offset += _TWO_FLOATS.size


def _encode_three_float_records(
    entries: typing.Sequence[typing.Tuple[float, float, float]],
) -> str:
    """
    Serialize a sequence of three-float records into a compact Latin-1 encoded
    binary string.

    The binary layout is:

        uint32 record_count
        record_count x (double a, double b, double c)

    :param entries: The records to serialize.
    :returns: A Latin-1 encoded binary string representing the serialized
        records.
    """
    out = bytearray(_UINT32.size + len(entries) * _THREE_FLOATS.size)
    _UINT32.pack_into(out, 0, len(entries))

    offset = _UINT32.size
    for a, b, c in entries:
        _THREE_FLOATS.pack_into(out, offset, a, b, c)
        offset += _THREE_FLOATS.size
    return out.decode("latin-1")


def _decode_three_float_records(
    raw: str,
) -> typing.List[typing.Tuple[float, float, float]]:
    """
    Deserialize a Latin-1 encoded binary string into a list of three-float
    records.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_three_float_records`.
    :returns: A list of decoded records.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _THREE_FLOATS.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} records")

    records: typing.List[typing.Tuple[float, float, float]] = [None] * count  # type: ignore[list-item]
    offset = _UINT32.size
    for index in range(count):
        records[index] = _THREE_FLOATS.unpack_from(view, offset)
        offset += _THREE_FLOATS.size
    return records


def _iter_three_float_records(
    raw: str,
) -> typing.Iterator[typing.Tuple[float, float, float]]:
    """
    Lazily iterate over three-float records stored in a Latin-1 encoded binary
    string.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_three_float_records`.
    :returns: An iterator yielding decoded records.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _THREE_FLOATS.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} records")

    offset = _UINT32.size
    for _ in range(count):
        yield _THREE_FLOATS.unpack_from(view, offset)
        offset += _THREE_FLOATS.size


def _encode_two_float_records_and_float(
    entries: typing.Sequence[typing.Tuple[float, float]],
    last_float: float,
) -> str:
    """
    Serialize a sequence of two-float pairs followed by a single
    floating-point value into a compact Latin-1 encoded binary string.

    The binary layout is:

        uint32 record_count
        record_count x (double a, double b)
        double last_float

    :param entries: The two-float records to serialize.
    :param last_float: The trailing floating-point value to serialize.
    :returns: A Latin-1 encoded binary string representing the serialized
        state.
    """
    out = bytearray(_UINT32.size + len(entries) * _TWO_FLOATS.size + _FLOAT.size)
    _UINT32.pack_into(out, 0, len(entries))

    offset = _UINT32.size
    for a, b in entries:
        _TWO_FLOATS.pack_into(out, offset, a, b)
        offset += _TWO_FLOATS.size

    _FLOAT.pack_into(out, offset, last_float)
    return out.decode("latin-1")


def _decode_two_float_records_and_float(
    raw: str,
) -> typing.Tuple[
    typing.List[typing.Tuple[float, float]],
    float,
]:
    """
    Deserialize a Latin-1 encoded binary string into a list of
    two-float pairs and a trailing floating-point value.

    :param raw: Latin-1 encoded binary string produced by the corresponding
        encode function.
    :returns: A tuple containing the decoded two-float records and
        the trailing floating-point value.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)

    expected_size = _UINT32.size + count * _TWO_FLOATS.size + _FLOAT.size
    if len(view) < expected_size:
        raise struct.error(
            f"buffer too small for {count} records: need {expected_size} bytes, got {len(view)}"
        )

    entries: typing.List[typing.Tuple[float, float]] = [None] * count  # type: ignore[list-item]
    offset = _UINT32.size
    for index in range(count):
        entries[index] = _TWO_FLOATS.unpack_from(view, offset)
        offset += _TWO_FLOATS.size

    (last_float,) = _FLOAT.unpack_from(view, offset)
    return entries, last_float


def _encode_float_list(values: typing.Sequence[float]) -> str:
    """
    Serialize a sequence of floating-point values into a compact Latin-1 encoded
    binary string.

    The binary layout is:

        uint32 value_count
        value_count x double

    :param values: The floating-point values to serialize.
    :returns: A Latin-1 encoded binary string representing the serialized
        values.
    """
    out = bytearray(_UINT32.size + len(values) * _FLOAT.size)
    _UINT32.pack_into(out, 0, len(values))

    offset = _UINT32.size
    for value in values:
        _FLOAT.pack_into(out, offset, value)
        offset += _FLOAT.size
    return out.decode("latin-1")


def _decode_float_list(raw: str) -> typing.List[float]:
    """
    Deserialize a Latin-1 encoded binary string into a list of floating-point
    values.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_float_list`.
    :returns: A list of decoded floating-point values.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _FLOAT.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} values")

    values: typing.List[float] = [0.0] * count
    offset = _UINT32.size
    for index in range(count):
        (values[index],) = _FLOAT.unpack_from(view, offset)
        offset += _FLOAT.size
    return values


def _iter_float_list(raw: str) -> typing.Iterator[float]:
    """
    Lazily iterate over floating-point values stored in a Latin-1 encoded
    binary string.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_float_list`.
    :returns: An iterator yielding floating-point values.
    """
    view = memoryview(raw.encode("latin-1"))
    (count,) = _UINT32.unpack_from(view, 0)
    expected_size = _UINT32.size + count * _FLOAT.size
    if len(view) < expected_size:
        raise struct.error(f"buffer too small for {count} values")

    offset = _UINT32.size
    for _ in range(count):
        yield _FLOAT.unpack_from(view, offset)[0]
        offset += _FLOAT.size


def _encode_two_floats(a: float, b: float) -> str:
    """
    Serialize two double-precision floating-point values into a compact
    Latin-1 encoded binary string.

    :param a: The first floating-point value.
    :param b: The second floating-point value.
    :returns: A Latin-1 encoded binary string containing both values.
    """
    return _TWO_FLOATS.pack(a, b).decode("latin-1")


def _decode_two_floats(raw: str) -> typing.Tuple[float, float]:
    """
    Deserialize two double-precision floating-point values from a Latin-1
    encoded binary string.

    :param raw: Latin-1 encoded binary string produced by
        `_encode_two_floats`.
    :returns: A tuple containing the two decoded floating-point values.
    """
    view = memoryview(raw.encode("latin-1"))
    expected_size = _TWO_FLOATS.size
    if len(view) < expected_size:
        raise struct.error("buffer too small for values")
    return _TWO_FLOATS.unpack(view)


SERDE_ERRORS = (ValueError, TypeError, MemoryError, struct.error, AttributeError)
"""Errors that may be raised by any of the struct-based se/deserializers in `traffik.strategies._serde`"""
