"""
Run by cibuildwheel against every wheel it builds, on every target
platform, before any of them are allowed into a release.

This exists specifically so a platform-specific problem in the compiled
`traffik.backends._ext` C extension gets caught here, on the actual target platform,
instead of by a user after publishing.
"""

import platform
import sys

import traffik  # noqa: F401
from traffik.backends.inmemory import InMemoryBackend  # noqa: F401
from traffik.throttles import HTTPThrottle  # noqa: F401

print(
    f"[cibw-smoke] base import OK "
    f"({sys.platform}, py{sys.version_info.major}.{sys.version_info.minor})"
)

if platform.system() != "Windows":
    # The C extension isn't built on Windows at all (see setup.py), so
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
