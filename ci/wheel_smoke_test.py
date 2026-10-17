"""
Run by cibuildwheel against every wheel it builds, on every target
platform, before any of them are allowed into a release.

This exists specifically so a platform-specific problem in the compiled
C extension (and in the `xxhash` wheel the hashing depends on) gets caught here,
on the actual target platform, instead of by a user after publishing.
"""

import platform
import sys

import traffik  # noqa: F401

# Hashing comes from the `xxhash` package and is expected to work everywhere,
# Windows included. We check known XXH32/XXH3 test vectors rather than just
# importing: the hashes decide shard routing and are part of stored keys, so a
# platform that computed different values would silently split throttle state.
from traffik._hash import hash_32, hash_64_hex
from traffik.backends.inmemory import InMemoryBackend  # noqa: F401
from traffik.throttles import HTTPThrottle  # noqa: F401

print(
    f"[cibw-smoke] base import OK "
    f"({sys.platform}, py{sys.version_info.major}.{sys.version_info.minor})"
)


assert hash_32(b"") == 0x02CC5D05
assert hash_32(b"a") == 0x550D7456

assert hash_64_hex(b"") == "2d06800538d394c2"
assert hash_64_hex(b"a") == "e6c632b61e964e1f"
print("[cibw-smoke] `traffik._hash` OK. Known XXH32/XXH3 vectors match")

if platform.system() != "Windows":
    # `_atomic` (atomic byte-lock primitives) needs GCC/Clang atomic builtins
    # and isn't built on Windows (see setup.py), so
    # `MultiProcessInMemoryBackend` isn't expected to be usable there.
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(namespace="cibw-smoke-test")
    backend.start()
    print(
        "[cibw-smoke] `MultiProcessInMemoryBackend.start()` OK. C extension loads and runs"
    )
else:
    print(
        "[cibw-smoke] Skipping `MultiProcessInMemoryBackend` check on Windows (not built there)"
    )
