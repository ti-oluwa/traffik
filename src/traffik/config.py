import os
import typing

###################
# Backend Configs #
###################
ANONYMOUS_IDENTIFIER = "__anonymous__"
"""Default identifier for anonymous connections."""
BACKEND_APP_CONTEXT_KEY = "__traffik_throttle_backend__"
APP_CONTEXT_ATTR = "state"


####################
# Throttle Configs #
####################
THROTTLED_STATE_KEY = "__traffik_throttled_state__"
CONNECTION_IDS_CONTEXT_KEY = "__traffik_connection_ids__"
THROTTLE_DEFAULT_SCOPE = "default"


###################
# Locking Configs #
###################
DEFAULT_BLOCKING_SETTING_ENV_VAR = "TRAFFIK_DEFAULT_BLOCKING"
DEFAULT_BLOCKING_TIMEOUT_ENV_VAR = "TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"
DEFAULT_LOCK_TTL_ENV_VAR = "TRAFFIK_DEFAULT_LOCK_TTL"


def get_lock_ttl() -> typing.Optional[float]:
    """
    Get the default lock TTL from the environment variable `TRAFFIK_DEFAULT_LOCK_TTL`.

    :return: The default lock TTL in seconds, or None if not set.
    """
    ttl_str = os.getenv(DEFAULT_LOCK_TTL_ENV_VAR)
    if ttl_str is not None:
        try:
            ttl = float(ttl_str)
        except ValueError:
            raise ValueError(
                f"Invalid value for {DEFAULT_LOCK_TTL_ENV_VAR}. Must be a non-negative float."
            )

        if ttl < 0:
            raise ValueError("Lock TTL must be a non-negative float.")
        return ttl
    return None


def get_lock_blocking() -> bool:
    """
    Get the default blocking setting from the environment variable `TRAFFIK_DEFAULT_BLOCKING`.

    :return: The default blocking setting as a boolean. Defaults to True if not set.
    """
    blocking_str = os.getenv(DEFAULT_BLOCKING_SETTING_ENV_VAR)
    if blocking_str is not None:
        return blocking_str.lower() in ("1", "true", "yes", "on")
    return True


def get_lock_blocking_timeout() -> typing.Optional[float]:
    """
    Get the default blocking timeout from the environment variable `TRAFFIK_DEFAULT_BLOCKING_TIMEOUT`.

    :return: The default blocking timeout in seconds, or None if not set.
    """
    timeout_str = os.getenv(DEFAULT_BLOCKING_TIMEOUT_ENV_VAR)
    if timeout_str is not None:
        try:
            timeout = float(timeout_str)
            if timeout < 0:
                raise ValueError
            return timeout
        except ValueError:
            raise ValueError(
                f"Invalid value for {DEFAULT_BLOCKING_TIMEOUT_ENV_VAR}. Must be a non-negative float."
            )
    return None


def set_lock_ttl(ttl: typing.Optional[float]) -> None:
    """
    Set the default global lock TTL in the environment variable `TRAFFIK_DEFAULT_LOCK_TTL`.

    :param ttl: The lock TTL to set, or None to unset.
    """
    if ttl is None:
        if DEFAULT_LOCK_TTL_ENV_VAR in os.environ:
            del os.environ[DEFAULT_LOCK_TTL_ENV_VAR]
    else:
        if ttl < 0:
            raise ValueError("Lock TTL must be a non-negative float.")
        os.environ[DEFAULT_LOCK_TTL_ENV_VAR] = str(ttl)


def set_lock_blocking(blocking: bool) -> None:
    """
    Set the default gloabl blocking setting in the environment variable `TRAFFIK_DEFAULT_BLOCKING`.

    :param blocking: The blocking setting to set.
    """
    os.environ[DEFAULT_BLOCKING_SETTING_ENV_VAR] = "1" if blocking else "0"


def set_lock_blocking_timeout(timeout: typing.Optional[float]) -> None:
    """
    Set the default global blocking timeout in the environment variable `TRAFFIK_DEFAULT_BLOCKING_TIMEOUT`.

    :param timeout: The blocking timeout to set, or None to unset.
    """
    if timeout is None:
        if DEFAULT_BLOCKING_TIMEOUT_ENV_VAR in os.environ:
            del os.environ[DEFAULT_BLOCKING_TIMEOUT_ENV_VAR]
    else:
        if timeout < 0:
            raise ValueError("Blocking timeout must be a non-negative float.")
        os.environ[DEFAULT_BLOCKING_TIMEOUT_ENV_VAR] = str(timeout)
