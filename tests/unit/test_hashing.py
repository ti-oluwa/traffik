import subprocess
import sys

from traffik._hashing import fnv_32bit_hash, fnv_64bit_hash, fnv_64bit_hash_hex
from traffik.backends.base import build_key


class TestFnv32BitHash:
    """Tests for fnv_32bit_hash against known FNV-1a test vectors."""

    def test_empty_input(self):
        """Hash of empty bytes is the offset basis itself."""
        assert fnv_32bit_hash(b"") == 0x811C9DC5

    def test_known_vectors(self):
        """Standard FNV-1a 32-bit vectors."""
        assert fnv_32bit_hash(b"a") == 0xE40C292C
        assert fnv_32bit_hash(b"foo") == 0xA9F37ED7

    def test_deterministic(self):
        """Same input always produces the same output."""
        assert fnv_32bit_hash(b"traffik") == fnv_32bit_hash(b"traffik")

    def test_different_inputs_differ(self):
        assert fnv_32bit_hash(b"key1") != fnv_32bit_hash(b"key2")

    def test_result_fits_32_bits(self):
        assert 0 <= fnv_32bit_hash(b"some longer input string") < 2**32


class TestFnv64BitHash:
    """Tests for fnv_64bit_hash against known FNV-1a test vectors."""

    def test_empty_input(self):
        """Hash of empty bytes is the offset basis itself."""
        assert fnv_64bit_hash(b"") == 0xCBF29CE484222325

    def test_known_vectors(self):
        """Standard FNV-1a 64-bit vectors."""
        assert fnv_64bit_hash(b"a") == 0xAF63DC4C8601EC8C
        assert fnv_64bit_hash(b"foo") == 0xDCB27518FED9D577

    def test_deterministic(self):
        """Same input always produces the same output."""
        assert fnv_64bit_hash(b"traffik") == fnv_64bit_hash(b"traffik")

    def test_different_inputs_differ(self):
        assert fnv_64bit_hash(b"key1") != fnv_64bit_hash(b"key2")

    def test_result_fits_64_bits(self):
        assert 0 <= fnv_64bit_hash(b"some longer input string") < 2**64

    def test_32_and_64_bit_hashes_of_same_input_differ(self):
        """Sanity check that the two extension functions aren't aliased."""
        assert fnv_64bit_hash(b"traffik") != fnv_32bit_hash(b"traffik")


class TestFnv64BitHashHex:
    """Tests for fnv_64bit_hash_hex - same hash as fnv_64bit_hash, as hex."""

    def test_matches_int_variant_formatted_as_hex(self):
        for data in (b"", b"a", b"foo", b"traffik", b"hello world"):
            assert fnv_64bit_hash_hex(data) == format(fnv_64bit_hash(data), "016x")

    def test_known_vectors(self):
        assert fnv_64bit_hash_hex(b"") == "cbf29ce484222325"
        assert fnv_64bit_hash_hex(b"a") == "af63dc4c8601ec8c"

    def test_always_16_chars(self):
        assert len(fnv_64bit_hash_hex(b"")) == 16
        assert len(fnv_64bit_hash_hex(b"x" * 1000)) == 16

    def test_deterministic(self):
        assert fnv_64bit_hash_hex(b"traffik") == fnv_64bit_hash_hex(b"traffik")


class TestLegacyMd5KeysImportTimeResolution:
    """
    `build_key`'s hash function is resolved once at import time (not
    per-call), so exercising `TRAFFIK_LEGACY_MD5_KEYS` honestly requires a
    fresh interpreter - reload()-ing the module in-process wouldn't be
    representative of how the flag is actually meant to be set.
    """

    def test_default_uses_fnv(self):
        output = subprocess.run(
            [sys.executable, "-c", "from traffik.backends.base import build_key; print(build_key('a'))"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert output.stdout.strip() == fnv_64bit_hash_hex(b"a")

    def test_env_var_set_before_import_forces_md5(self):
        import hashlib
        import os

        env = {**os.environ, "TRAFFIK_LEGACY_MD5_KEYS": "1"}
        output = subprocess.run(
            [sys.executable, "-c", "from traffik.backends.base import build_key; print(build_key('a'))"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert output.stdout.strip() == hashlib.md5(b"a").hexdigest()  # nosec


class TestBuildKey:
    """Tests for build_key, which uses fnv_64bit_hash to combine args/kwargs."""

    def test_no_args_returns_wildcard(self):
        assert build_key() == "*"

    def test_returns_16_char_hex_digest(self):
        """FNV-1a 64-bit formatted as hex is always 16 characters."""
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
