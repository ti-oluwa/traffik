"""
Non-cryptographic hashing used for key building, sharding and in-memory hash tables.

Traffik uses [xxHash](https://xxhash.com) (through the `xxhash` package).
"""

import xxhash

__all__ = ["hash_32", "hash_64_hex"]

hash_32 = xxhash.xxh32_intdigest
"""
Compute the XXH32 hash of the given bytes, as an `int` in `[0, 2**32)`.

For shard routing and hash-table buckets. Use `format(hash_32(data), "08x")` for
an 8-character hex string.
"""

hash_64_hex = xxhash.xxh3_64_hexdigest
"""
Compute the XXH3 64-bit hash of the given bytes, as a 16-character lowercase hex string.
"""
