"""`Headers` API for defining and managing headers in throttles."""

import copy
import math
import typing
from collections.abc import Mapping

from typing_extensions import Self

from traffik.types import HTTPConnectionT, StrategyStat

__all__ = ["Header", "Headers", "DEFAULT_HEADERS_ALWAYS", "DEFAULT_HEADERS_THROTTLED"]

HeaderResolver = typing.Callable[
    [
        HTTPConnectionT,
        StrategyStat[typing.Mapping],
        typing.Optional[typing.Mapping[str, typing.Any]],
    ],
    str,
]
"""
Type alias for a callable that resolves to a string based on:

- The HTTP connection
- The strategy statistics
- An optional throttle context
"""

HeaderValue = typing.Union[str, HeaderResolver[HTTPConnectionT]]
"""
Type definition for a header value, which can be either:

- A static string value
- A callable that takes the connection, strategy statistics, and optional context, and returns a string
"""

WhenFunc = typing.Callable[
    [
        HTTPConnectionT,
        StrategyStat[typing.Mapping],
        typing.Optional[typing.Mapping[str, typing.Any]],
    ],
    bool,
]
"""
Type alias for a callable that determines when to include a header based on:

- The HTTP connection
- The strategy statistics
- An optional throttle context
"""

HeaderWhen = typing.Union[
    typing.Literal["always", "throttled"], WhenFunc[HTTPConnectionT]
]
"""
Type definition for the 'when' condition of a header, which can be either:
- A string literal "always" or "throttled"
- A callable that takes the connection, strategy statistics, and optional context, and 
returns a boolean indicating whether to include the header in the response.
"""


class Header(typing.Generic[HTTPConnectionT]):
    """
    A header definition that can be either a raw string or a
    callable that resolves to a string based on the connection and strategy statistics.

    This class allows for flexible header definitions in throttled responses,
    enabling both raw and dynamic header values depending on the context of
    the request and the state of the throttle.

    Example usage:
    ```python
    def custom_header_resolver(connection, stat, context):
        return f"Custom-{stat.rate.limit}"

    def custom_when_func(connection, stat, context):
        return stat.hits_remaining < 5

    headers = {
        # Static/raw header that is always included
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "X-RateLimit-Limit": Header.LIMIT(when="always"),

        # This header is only included when the request is throttled (hits_remaining <= 0)
        "X-RateLimit-Reset": Header.RESET_SECONDS(when="throttled"),

        # Custom header that is included when hits remaining is less than 5
        "X-Custom-Header": Header(custom_header_resolver, when=custom_when_func),
    }
    ```
    """

    # Use string sentinel so we don't have to do any magic to ensure type correctness,
    # and to prevent it from affecting or interferring with the "static" state computation
    # of the `Headers` instance it is used in.
    DISABLE: str = ":___disabled___:"
    """
    Flag to disable a particular header key.

    Example Usage:
    ```python
    from traffik import HTTPThrottle, Headers, Header

    headers = {
        "Header-1": Header("<some_value>"),
        "Header-2": Header(lambda conn, stat, ctx: "<some_result>")
    }
    throttle = HTTPThrottle(..., headers=Headers(headers))

    # Later on in some scope say we want to disable "Header-2" when resolving headers.
    # Pass the override to `get_headers()` to disable it for this specific resolution.
    resolved = await throttle.get_headers(
        connection,
        headers={"Header-2": Header.DISABLE},
    )
    ```
    """

    __slots__ = (
        "_raw",
        "_resolver",
        "_check",
        "_when",
        "_is_static",
        "_hash",
    )

    def __init__(
        self,
        v: HeaderValue[HTTPConnectionT],
        /,
        when: HeaderWhen[HTTPConnectionT] = "throttled",
    ) -> None:
        """
        Initialize the header instance

        :param v: Either a static string value or a resolver callable with
            signature `(connection, stat, context) -> str` that produces the
            header value at request time.

        :param when: Determines when the header should be included. It may be
            the literal strings ``"always"`` or ``"throttled"``, or a
            callable predicate with the same signature as the resolver that
            returns a boolean. When a predicate is provided, it is used to
            decide inclusion instead of the built-in literals.

        Notes:
        - Use `Header.DISABLE` (the identity sentinel) to disable a header
          for a specific request. Example:
              headers = {"X-RateLimit-Reset": Header.DISABLE}
          The resolver that processes headers must check identity (value is `Header.DISABLE`).
        - The `Header` instance may be considered "static" by the `Headers` container
          when a static value is provided; dynamic resolvers are skipped if
          throttling stats are unavailable.
        """
        if isinstance(v, str):
            self._raw = v
            self._resolver = None
        else:
            self._resolver = v
            self._raw = None  # type: ignore[assignment]

        self._check = when if callable(when) else None
        self._when = when if not callable(when) else None
        self._is_static = self._raw is not None and self._when == "always"
        """
        A header is considered static if it has a raw string value and is always included in the response.

        Static headers can be optimized by pre-encoding them and skipping dynamic resolution.
        """
        # Pre-compute the hash of the header here, since the header
        # is not expected to be mutated after initialization.
        if self._is_static:
            self._hash = hash(self._raw)
        elif self._check is not None:
            self._hash = hash((id(self._resolver), id(self._check)))
        else:
            self._hash = hash((id(self._resolver), self._when))

    def check(
        self,
        connection: HTTPConnectionT,
        stat: StrategyStat[typing.Mapping],
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> bool:
        """
        Checks whether this header should be included in the response based on the 'when' condition.

        :param connection: The HTTP connection for the current request.
        :param stat: The strategy statistics for the current request.
        :param context: An optional dictionary containing additional context for the throttle.
        :return: True if the header should be included in the response, False otherwise.
        """
        if self._check is not None:
            return self._check(connection, stat, context)
        elif self._when == "always":
            return True
        elif self._when == "throttled":
            return stat.hits_remaining <= 0
        return False

    def resolve(
        self,
        connection: HTTPConnectionT,
        stat: StrategyStat[typing.Mapping],
        context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    ) -> str:
        """
        Resolves the header value based on the connection, strategy statistics, and optional context.

        :param connection: The HTTP connection for the current request.
        :param stat: The strategy statistics for the current request.
        :param context: An optional dictionary containing additional context for the throttle.
        :return: The resolved header value as a string.
        """
        if self._resolver is None:
            return self._raw  # type: ignore[return-value]
        return self._resolver(connection, stat, context)

    def when(self, when: HeaderWhen[HTTPConnectionT]) -> Self:
        """
        Returns a new `Header` instance with the specified 'when' condition.

        This allows for chaining header definitions with different conditions for when they should be included in the response.

        Example usage:
        ```python
        # Create a header that is always included
        header = Header.REMAINING(when="always")

        # Create a header that is only included when throttled
        header = Header.REMAINING(when="throttled")

        # Create a header with a custom condition
        def hits_less_than_5(connection, stat, context):
            return stat.hits_remaining < 5

        header = Header.REMAINING(when=hits_less_than_5)
        ```
        """
        value = typing.cast(HeaderValue[HTTPConnectionT], self._resolver or self._raw)
        return self.__class__(value, when=when)

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._raw!r})"

    def __setattr__(self, name: str, value: typing.Any):
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError(f"`{self.__class__.__name__}` object is immutable")
        super().__setattr__(name, value)

    @property
    def always(self) -> Self:
        """Returns a new `Header` instance that is always included in the response."""
        return self.when("always")

    @property
    def throttled(self) -> Self:
        """Returns a new `Header` instance that is only included when the request is throttled."""
        return self.when("throttled")

    @classmethod
    def LIMIT(cls, when: HeaderWhen[typing.Any]) -> Self:
        """Resolves to the maximum number of hits allowed in the current period."""
        return cls(lambda _, stat, __: f"{stat.rate.limit}", when=when)

    @classmethod
    def REMAINING(cls, when: HeaderWhen[typing.Any]) -> Self:
        """Resolves to the number of hits remaining in the current period."""
        return cls(lambda _, stat, __: f"{stat.hits_remaining}", when=when)

    @classmethod
    def RESET_MILLISECONDS(cls, when: HeaderWhen[typing.Any]) -> Self:
        """Resolves to the time to wait (in milliseconds) before the next allowed request."""
        return cls(lambda _, stat, __: f"{stat.wait_ms}", when=when)

    @classmethod
    def RESET_SECONDS(cls, when: HeaderWhen[typing.Any]) -> Self:
        """Resolves to the time to wait (in seconds) before the next allowed request."""
        return cls(lambda _, stat, __: f"{math.ceil(stat.wait_ms / 1000)}", when=when)


def _prep_headers(
    headers: typing.Mapping[str, typing.Union[Header, str]],
) -> typing.Dict[str, typing.Union[Header, str]]:
    """
    Prepares the headers, try our best to ensure that the header is static if it can be. i.e, only
    consists of string values and is always included in the response.

    This allows us to optimize for the common case where headers are static
    and do not require dynamic resolution on each request, while still supporting dynamic headers when needed.
    """
    prepared_headers: typing.Dict[str, typing.Union[Header, str]] = {}
    for name, value in headers.items():
        if isinstance(value, str):
            prepared_headers[name] = value
        elif value._is_static:
            # For a dynamic `Header` that is actually static.
            # We can optimize by just using its raw value directly and skipping the resolution process later on.
            prepared_headers[name] = value._raw  # type: ignore[union-attr]
        else:
            prepared_headers[name] = value
    return prepared_headers


def _is_static(headers: typing.Mapping[str, typing.Union[Header, str]]) -> bool:
    """
    Checks if all headers are static, meaning they are all string values and hence, are always included in the response.

    This is used to determine if we can optimize header resolution by pre-encoding them and skipping dynamic resolution on each request.
    """
    return all(isinstance(v, str) for v in headers.values())


class Headers(Mapping[str, typing.Union[str, Header[HTTPConnectionT]]]):
    """
    A mapping of header names to either static string values or `Header` instances.

    This is the recommended way to define and manage headers for throttles.

    Example Usage:

    ```python
    from traffik.headers import Headers, Header

    base = Headers({
        "X-RateLimit-Limit": Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
    })

    # Disable a header for a single hit by using the sentinel identity
    overrides = {"X-RateLimit-Remaining": Header.DISABLE}

    # Merge the resolver uses an identity check to interpret
    # `Header.DISABLE` and will skip that header when resolving.
    merged = base | overrides
    ```

    Note: `Header.DISABLE` is a module-level sentinel; disabling requires
    passing that exact object (checked with ``is``). A plain string with
    the same contents will not disable the header.
    """

    __slots__ = ("_raw", "_is_static")

    def __init__(
        self,
        raw: typing.Optional[
            typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]]
        ] = None,
        /,
        # Indicates whether the provided headers are already prepped, to avoid redundant prepping if they are.
        _prepped: bool = False,
        # Indicates whether all headers are pre-known to be static, to avoid redundant static checks if they are.
        _static: typing.Optional[bool] = None,
    ) -> None:
        """
        Initialize the `Headers` instance with the provided raw headers.
        """
        self._is_static: bool
        """
        A flag indicating whether all headers are static, meaning they are all string values and hence, 
        are to always be included in the response.
        """
        if not raw:
            self._raw = {}
            self._is_static = True
        elif _prepped and raw is not None:
            self._raw = typing.cast(
                typing.Dict[str, typing.Union[str, Header[HTTPConnectionT]]], raw
            )
            self._is_static = _is_static(self._raw) if _static is None else _static
        else:
            self._raw = _prep_headers(raw)
            self._is_static = _is_static(self._raw) if _static is None else _static

    def update(
        self, other: typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]], /
    ) -> None:
        """In-place update the headers. This can be slow, especially for large updates. Use `|` (union) instead."""
        for k, v in other.items():
            self[k] = v

    def copy(self) -> Self:
        """Make a shallow copy of the headers instance"""
        return self.__class__(dict(self._raw), _prepped=True, _static=self._is_static)

    def __getitem__(self, key: str, /) -> Header[HTTPConnectionT]:
        value = self._raw[key]
        if isinstance(value, str):
            return Header(value, when="always")
        return value

    def __setitem__(
        self, key: str, value: typing.Union[str, Header[HTTPConnectionT]], /
    ) -> None:
        # This method has a worst case of O(N-keys).
        # But since headers are usually not that large, it is acceptable.
        self._raw[key] = value
        # Recompute static status after mutation.
        self._is_static = _is_static(self._raw)

    def __contains__(self, key: typing.Hashable, /) -> bool:
        return key in self._raw

    def items(
        self,
    ) -> typing.ItemsView[str, typing.Union[str, Header[HTTPConnectionT]]]:  # type: ignore[override]
        return self._raw.items()

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __bool__(self) -> bool:
        return bool(self._raw)

    def __or__(
        self, other: typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]], /
    ) -> Self:
        # Already prepped since we prep on initialization, so we can skip the prep step for self.
        new = dict(self._raw)
        # Prep the other headers if they are not already prepped,
        # to ensure that we optimize for static headers as much as possible.
        if raw := getattr(other, "_raw", None):
            # If the other is already a Headers instance, we can skip the prep
            # step since it was prepped on initialization.
            new.update(raw)
        elif other:
            new.update(_prep_headers(other))
        return self.__class__(new, _prepped=True, _static=_is_static(new))

    def __ior__(
        self, other: typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]], /
    ) -> Self:
        for k, v in other.items():
            self[k] = v
        return self

    def __reduce__(
        self,
    ) -> typing.Tuple[
        typing.Type[Self],
        typing.Tuple[
            typing.Mapping[str, typing.Union[str, Header[HTTPConnectionT]]],
            typing.Literal[True],
            bool,
        ],
    ]:
        return (self.__class__, (self._raw, True, self._is_static))

    def __copy__(self) -> Self:
        return self.copy()

    def __deepcopy__(self, memo: typing.Dict[int, typing.Any]) -> Self:
        return self.__class__(
            copy.deepcopy(self._raw, memo), _prepped=True, _static=self._is_static
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._raw!r})"


DEFAULT_HEADERS_ALWAYS = Headers[typing.Any](
    {
        "X-RateLimit-Limit": Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "Retry-After": Header.RESET_SECONDS(when="always"),
    }
)
"""
Default headers to include in all responses, regardless of throttling status.

These headers include:
- `X-RateLimit-Limit`: The maximum number of hits allowed in the current period.
- `X-RateLimit-Remaining`: The number of hits remaining in the current period.
- `Retry-After`: The time to wait (in seconds) before the next allowed request (RFC 6585).
"""

DEFAULT_HEADERS_THROTTLED = Headers[typing.Any](
    {
        "X-RateLimit-Limit": Header.LIMIT(when="throttled"),
        "X-RateLimit-Remaining": Header.REMAINING(when="throttled"),
        "Retry-After": Header.RESET_SECONDS(when="throttled"),
    }
)
"""
Default headers to include only in throttled responses.

These headers include:
- `X-RateLimit-Limit`: The maximum number of hits allowed in the current period
- `X-RateLimit-Remaining`: The number of hits remaining in the current period
- `Retry-After`: The time to wait (in seconds) before the next allowed request (RFC 6585)
"""
