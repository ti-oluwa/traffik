"""Env-var readers shared by the traffik and SlowAPI benchmark apps."""

import os


def get_env(name: str, default: str) -> str:
    """Read an environment variable, falling back to `default`."""
    return os.getenv(name, default)


def int_env(name: str, default: int) -> int:
    """Read an environment variable as an int, falling back to `default`."""
    raw = os.getenv(name)
    return int(raw) if raw is not None else default
