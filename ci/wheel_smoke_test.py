"""
Run by cibuildwheel against every wheel it builds, on every target
platform, before any of them are allowed into a release.

This exists specifically so a platform-specific problem in the compiled
C extension gets caught here, on the actual target platform, instead of by a user after publishing.
"""

import platform
import sys

import traffik  # noqa: F401

# `_hashing` has no platform-specific build restriction and is expected to
# be present everywhere, Windows included. Check known FNV-1a test vectors
# rather than just importing, so a bad build (e.g. a struct-width mismatch
# on an unusual platform) is caught here instead of downstream.
from traffik._hashing import fnv_32bit_hash, fnv_64bit_hash
from traffik.backends.inmemory import InMemoryBackend  # noqa: F401
from traffik.throttles import HTTPThrottle  # noqa: F401

print(
    f"[cibw-smoke] base import OK "
    f"({sys.platform}, py{sys.version_info.major}.{sys.version_info.minor})"
)


assert fnv_32bit_hash(b"") == 0x811C9DC5
assert fnv_32bit_hash(b"a") == 0xE40C292C
assert fnv_64bit_hash(b"") == 0xCBF29CE484222325
assert fnv_64bit_hash(b"a") == 0xAF63DC4C8601EC8C
print("[cibw-smoke] `traffik._hashing` OK - known FNV-1a vectors match")

if platform.system() != "Windows":
    # `_ext` (atomic byte-lock primitives) needs GCC/Clang atomic builtins
    # and isn't built on Windows (see setup.py), so
    # `MultiProcessInMemoryBackend` isn't expected to be usable there.
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(namespace="cibw-smoke-test")
    backend.start()
    print(
        "[cibw-smoke] `MultiProcessInMemoryBackend.start()` OK - C extension loads and runs"
    )
else:
    print(
        "[cibw-smoke] Skipping `MultiProcessInMemoryBackend` check on Windows (not built there)"
    )
