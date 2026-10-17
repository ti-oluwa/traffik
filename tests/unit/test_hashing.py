import hashlib
import os
import subprocess
import sys

from traffik._hash import hash_32, hash_64_hex
from traffik.backends.base import build_key


class TestHash32:
    """
    Tests for `hash_32` (XXH32) against known test vectors.

    The values are pinned on purpose: shard routing and hash-table buckets depend
    on them, so a change in algorithm or seed must fail here loudly.
    """

    def test_empty_input(self):
        """XXH32 of empty bytes with seed 0 is a published test vector."""
        assert hash_32(b"") == 0x02CC5D05

    def test_known_vectors(self):
        assert hash_32(b"a") == 0x550D7456
        assert hash_32(b"foo") == 0xE20F0DD9
        assert hash_32(b"traffik") == 0xBF805102

    def test_deterministic(self):
        """Same input always produces the same output."""
        assert hash_32(b"traffik") == hash_32(b"traffik")

    def test_different_inputs_differ(self):
        assert hash_32(b"key1") != hash_32(b"key2")

    def test_result_fits_32_bits(self):
        assert 0 <= hash_32(b"some longer input string") < 2**32

    def test_hex_suffix_is_8_characters(self):
        """The shared-memory segment name suffix is `format(hash_32(...), "08x")`."""
        assert format(hash_32(b""), "08x") == "02cc5d05"
        assert len(format(hash_32(b"a"), "08x")) == 8

    def test_spreads_similar_keys_across_shards(self):
        """Near-identical keys must not pile onto a few shards."""
        shards = 16
        keys = [f"api:items:10.0.0.{i}".encode() for i in range(1, 255)]
        counts = [0] * shards
        for key in keys:
            counts[hash_32(key) % shards] += 1
        assert all(count > 0 for count in counts)
        assert max(counts) < len(keys) / shards * 2.5


class TestHash64Hex:
    """
    Tests for `hash_64_hex` (XXH3 64-bit, as hex) against known test vectors.

    Pinned for the same reason as `TestHash32`: the digest is part of the keys
    stored in Redis and Memcached.
    """

    def test_empty_input(self):
        """XXH3-64 of empty bytes with seed 0 is a published test vector."""
        assert hash_64_hex(b"") == "2d06800538d394c2"

    def test_known_vectors(self):
        assert hash_64_hex(b"a") == "e6c632b61e964e1f"
        assert hash_64_hex(b"foo") == "ab6e5f64077e7d8a"
        assert hash_64_hex(b"traffik") == "ff3378f4177515d1"

    def test_always_16_lowercase_hex_chars(self):
        for data in (b"", b"x", b"x" * 1000):
            digest = hash_64_hex(data)
            assert len(digest) == 16
            assert digest == digest.lower()
            int(digest, 16)  # doesn't raise - valid hex

    def test_deterministic(self):
        assert hash_64_hex(b"traffik") == hash_64_hex(b"traffik")

    def test_different_inputs_differ(self):
        assert hash_64_hex(b"key1") != hash_64_hex(b"key2")

    def test_is_independent_of_the_32_bit_hash(self):
        """Sanity check that the two helpers aren't aliased."""
        assert int(hash_64_hex(b"traffik"), 16) != hash_32(b"traffik")


class TestLegacyMd5KeysImportTimeResolution:
    """
    `build_key`'s hash function is resolved once, the first time a key is
    built, and cached for the rest of the process (not per-call), so
    exercising `TRAFFIK_LEGACY_MD5_KEYS` honestly requires a fresh interpreter:
    reloading modules in-process wouldn't be representative of how the flag
    is actually meant to be set.
    """

    def test_default_uses_xxh3(self):
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                "from traffik.backends.base import build_key; print(build_key('a'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert output.stdout.strip() == hash_64_hex(b"a")

    def test_env_var_set_before_import_forces_md5(self):
        env = {**os.environ, "TRAFFIK_LEGACY_MD5_KEYS": "1"}
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                "from traffik.backends.base import build_key; print(build_key('a'))",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert output.stdout.strip() == hashlib.md5(b"a").hexdigest()  # nosec


class TestBuildKey:
    """Tests for build_key, which uses `hash_64_hex` to combine args/kwargs."""

    def test_no_args_returns_wildcard(self):
        assert build_key() == "*"

    def test_returns_16_char_hex_digest(self):
        """XXH3 64-bit formatted as hex is always 16 characters."""
        key = build_key("user", "123")
        assert len(key) == 16
        int(key, 16)  # doesn't raise - valid hex

    def test_deterministic(self):
        assert build_key("user", user_id="123") == build_key("user", user_id="123")

    def test_order_independent_kwargs(self):
        """Kwarg order shouldn't affect the resulting key (parts are sorted)."""
        assert build_key(a="1", b="2") == build_key(b="2", a="1")

    def test_different_args_produce_different_keys(self):
        assert build_key("user", "123") != build_key("user", "456")
