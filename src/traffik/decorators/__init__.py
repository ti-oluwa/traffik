import typing

from typing_extensions import deprecated

__all__ = ["throttled"]


@deprecated(
    "Importing `throttled` from `traffik.decorators` is deprecated and will "
    "be removed in a future release. Import it from "
    "`traffik.decorators.fastapi` or `traffik.decorators.generic` instead."
)
def throttled(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
    fastapi = kwargs.get("fastapi", None)
    try:
        if fastapi is False:
            from traffik.decorators.generic import throttled as decorator
        else:  # fastapi=True or None
            from traffik.decorators.fastapi import (  # type: ignore[no-redef]
                throttled as decorator,
            )
    except ImportError:
        if fastapi is not None:
            raise
        from traffik.decorators.generic import (  # type: ignore[no-redef]
            throttled as decorator,
        )
    return decorator(*args, **kwargs)
