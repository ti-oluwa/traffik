"""Portable FNV-1a hashing extension for traffik."""

def fnv_32bit_hash(data: bytes, /) -> int:
    """
    Compute FNV-1a 32-bit hash of the given bytes. Deterministic across processes and platforms.

    Use this because Python's built-in `hash()` is randomized per-process
    since Python 3.3 and is therefore unusable for cross-process hash table
    probing.

    :param data: The bytes to hash.
    :return: The 32-bit hash value as an unsigned integer (0 to 2^32 - 1).
    """

def fnv_64bit_hash(data: bytes, /) -> int:
    """
    Compute FNV-1a 64-bit hash of the given bytes. Deterministic across processes and platforms.

    Lower collision rate than `fnv_32bit_hash` for the same input space.

    :param data: The bytes to hash.
    :return: The 64-bit hash value as an unsigned integer (0 to 2^64 - 1).
    """
