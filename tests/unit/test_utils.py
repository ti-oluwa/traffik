import asyncio
import functools
import inspect
import ipaddress

import anyio
import pytest
from starlette.requests import HTTPConnection

from tests.utils import make_connection
from traffik._utils import (
    CircuitBreaker,
    CircuitState,
    ProxyHeaders,
    _add_parameter_to_signature,
    _as_cache_key,
    _is_ip,
    _is_trusted_proxy,
    _split_trusted_proxies,
    _TaskTimer,
    get_remote_address,
    is_async_callable,
    time,
)


class TestGetRemoteAddress:
    """Tests for get_remote_address utility."""

    def test_no_trusted_proxies_ignores_x_forwarded_for(self):
        """With no trusted_proxies (the default), XFF is never consulted."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"172.16.0.1")],
            client=("203.0.113.42", 50000),
        )
        address = get_remote_address(connection)
        assert address == "203.0.113.42"

    def test_explicit_empty_trusted_proxies_ignores_headers(self):
        """Explicit `trusted_proxies=()` behaves identically to the default `None`."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"172.16.0.1")],
            client=("203.0.113.42", 50000),
        )
        address = get_remote_address(connection, trusted_proxies=())
        assert address == "203.0.113.42"

    def test_get_remote_from_client_tuple(self):
        """Falls back to the socket peer when there's nothing else to use."""
        connection = make_connection(
            HTTPConnection,
            client=("203.0.113.42", 50000),
        )
        address = get_remote_address(connection)
        assert address == "203.0.113.42"

    def test_get_remote_returns_none_when_unavailable(self):
        """Returns `None` when there's no client at all."""
        connection = make_connection(HTTPConnection, client=None)
        address = get_remote_address(connection)
        assert address is None

    def test_untrusted_peer_headers_ignored(self):
        """A peer not in `trusted_proxies` can't spoof the client IP via headers."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"1.2.3.4")],
            client=("8.8.8.8", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
        )
        assert address == "8.8.8.8"

    def test_trusted_peer_exact_match_honors_headers(self):
        """A peer matching an exact trusted address has its headers honored."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
        )
        assert address == "203.0.113.7"

    def test_trusted_peer_network_match_honors_headers(self):
        """A peer matching a trusted CIDR network has its headers honored."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
            client=("10.0.5.5", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_network("10.0.0.0/16"),),
        )
        assert address == "203.0.113.7"

    def test_ipv6_exact_trusted_peer(self):
        """Trusted-proxy matching also works for IPv6 peers."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"2001:db8::42")],
            client=("::1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("::1"),),
        )
        assert address == "2001:db8::42"

    def test_xff_single_hop(self):
        """Single-hop XFF from a trusted proxy resolves to the client IP."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
        )
        assert address == "203.0.113.7"

    def test_xff_walks_past_multiple_trusted_hops(self):
        """A chain of several trusted proxies is walked to the real client."""
        trusted = (
            ipaddress.ip_address("10.0.0.1"),
            ipaddress.ip_address("10.0.0.2"),
        )
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"203.0.113.7, 10.0.0.2")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(connection, trusted_proxies=trusted)
        assert address == "203.0.113.7"

    def test_xff_stops_at_first_untrusted_hop_from_the_right(self):
        """
        Security-critical: an attacker who prepends fake entries to XFF can't
        make it past a real, untrusted hop that a trusted proxy correctly
        appended. Walking must stop at the first non-trusted entry from the
        right, not read further left.
        """
        # An attacker connects directly to our trusted proxy and sends a
        # forged XFF; the trusted proxy appends the attacker's real IP.
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"9.9.9.9, 8.8.8.8, 203.0.113.99")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection, trusted_proxies=(ipaddress.ip_address("10.0.0.1"),)
        )
        # The attacker's real, untrusted IP - not their forged entries.
        assert address == "203.0.113.99"

    def test_xff_skips_malformed_entries(self):
        """Non-IP / empty entries in the chain (e.g. trailing commas) are skipped."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"203.0.113.7, , not-an-ip, 10.0.0.1")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection, trusted_proxies=(ipaddress.ip_address("10.0.0.1"),)
        )
        assert address == "203.0.113.7"

    def test_xff_all_hops_trusted_falls_back_to_peer(self):
        """If every entry in the chain is itself a trusted proxy, fall through."""
        trusted = (
            ipaddress.ip_address("10.0.0.1"),
            ipaddress.ip_address("10.0.0.2"),
        )
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-forwarded-for", b"10.0.0.2")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(connection, trusted_proxies=trusted)
        assert address == "10.0.0.1"

    def test_forwarded_basic(self):
        """Basic `for=` extraction from the Forwarded header."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'for="203.0.113.7"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "203.0.113.7"

    def test_forwarded_bracketed_ipv6(self):
        """Bracketed IPv6 addresses have their brackets stripped."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'for="[2001:db8::1]:4711"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "2001:db8::1"

    def test_forwarded_ipv4_with_port(self):
        """IPv4 addresses with a trailing `:port` have the port stripped."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'for="192.0.2.1:4711"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "192.0.2.1"

    def test_forwarded_whitespace_around_equals(self):
        """Optional whitespace around `'='` (RFC 7239 OWS) is tolerated."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'for = "203.0.113.7"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "203.0.113.7"

    def test_forwarded_case_insensitive_key(self):
        """The `'for'` parameter key is matched case-insensitively."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'For="203.0.113.7"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "203.0.113.7"

    def test_forwarded_multiple_elements_uses_first_valid(self):
        """Multiple comma-separated Forwarded elements: first valid `for=` wins."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"forwarded", b'for="203.0.113.7";proto=https, for="10.0.0.1"')],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "203.0.113.7"

    @pytest.mark.parametrize(
        ("header", "flag"),
        [
            (b"cf-connecting-ip", ProxyHeaders.CF_CONNECTING_IP),
            (b"true-client-ip", ProxyHeaders.TRUE_CLIENT_IP),
            (b"x-real-ip", ProxyHeaders.X_REAL_IP),
        ],
    )
    def test_single_ip_header_honored_when_enabled(self, header, flag):
        """Each single-IP header is honored when its flag is enabled."""
        connection = make_connection(
            HTTPConnection,
            headers=[(header, b"203.0.113.7")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=flag,
        )
        assert address == "203.0.113.7"

    @pytest.mark.parametrize(
        "header",
        [b"cf-connecting-ip", b"true-client-ip", b"x-real-ip"],
    )
    def test_single_ip_header_ignored_when_not_enabled(self, header):
        """A single-IP header is ignored unless its flag is set."""
        connection = make_connection(
            HTTPConnection,
            headers=[(header, b"203.0.113.7")],
            client=("10.0.0.1", 50000),
        )
        # Only FORWARDED enabled - none of the single-IP headers apply.
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.FORWARDED,
        )
        assert address == "10.0.0.1"

    def test_single_ip_header_precedence(self):
        """When several are present with ALL enabled, CF > True-Client > X-Real-IP."""
        connection = make_connection(
            HTTPConnection,
            headers=[
                (b"cf-connecting-ip", b"1.1.1.1"),
                (b"true-client-ip", b"2.2.2.2"),
                (b"x-real-ip", b"3.3.3.3"),
            ],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.ALL,
        )
        assert address == "1.1.1.1"

    def test_malformed_single_ip_header_is_skipped(self):
        """A malformed single-IP header value is ignored, falling back to peer."""
        connection = make_connection(
            HTTPConnection,
            headers=[(b"x-real-ip", b"not-an-ip")],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.X_REAL_IP,
        )
        assert address == "10.0.0.1"

    def test_proxy_headers_none_ignores_everything(self):
        """`ProxyHeaders.NONE` means no header source is consulted at all."""
        connection = make_connection(
            HTTPConnection,
            headers=[
                (b"forwarded", b'for="1.1.1.1"'),
                (b"x-forwarded-for", b"2.2.2.2"),
                (b"x-real-ip", b"3.3.3.3"),
            ],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.NONE,
        )
        assert address == "10.0.0.1"

    def test_forwarded_takes_precedence_over_xff(self):
        """With ALL enabled, Forwarded is checked before X-Forwarded-For."""
        connection = make_connection(
            HTTPConnection,
            headers=[
                (b"forwarded", b'for="1.1.1.1"'),
                (b"x-forwarded-for", b"2.2.2.2"),
            ],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.ALL,
        )
        assert address == "1.1.1.1"

    def test_xff_takes_precedence_over_single_ip_headers(self):
        """With ALL enabled, X-Forwarded-For is checked before single-IP headers."""
        connection = make_connection(
            HTTPConnection,
            headers=[
                (b"x-forwarded-for", b"2.2.2.2"),
                (b"x-real-ip", b"3.3.3.3"),
            ],
            client=("10.0.0.1", 50000),
        )
        address = get_remote_address(
            connection,
            trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),
            proxy_headers=ProxyHeaders.ALL,
        )
        assert address == "2.2.2.2"


class TestIsIp:
    """Tests for the `_is_ip` format validator."""

    @pytest.mark.parametrize(
        "value",
        ["0.0.0.0", "127.0.0.1", "255.255.255.255", "203.0.113.42"],
    )
    def test_valid_ipv4(self, value):
        assert _is_ip(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "::1",
            "::",
            "2001:db8::1",
            "fe80::1",
            "2001:0db8:0000:0000:0000:0000:0000:0001",
        ],
    )
    def test_valid_ipv6(self, value):
        assert _is_ip(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not-an-ip",
            "1.2.3",
            "1.2.3.4.5",
            "999.1.1.1",
            "1.2.3.4:8080",  # port not stripped here - not a bare IP
            "  1.2.3.4  ",  # whitespace not stripped
            "1.2.3.4/24",  # a network, not a bare address
        ],
    )
    def test_invalid(self, value):
        assert _is_ip(value) is False

    def test_ipv6_zone_id_rejected(self):
        """
        Documented trade-off: unlike ipaddress.IPv6Address, `inet_pton` doesn't
        accept zone IDs. Scoped link-local addresses in a proxy header aren't
        a real-world case worth validating for on every request.
        """
        assert _is_ip("fe80::1%eth0") is False


class TestSplitTrustedProxies:
    """Tests for `_split_trusted_proxies` and its cache."""

    def test_splits_addresses_and_networks(self):
        proxies = (
            ipaddress.ip_address("10.0.0.1"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        exact, networks = _split_trusted_proxies(_as_cache_key(proxies))
        assert exact == frozenset({"10.0.0.1"})
        assert networks == (ipaddress.ip_network("192.168.0.0/16"),)

    def test_all_addresses_no_networks(self):
        proxies = (ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("10.0.0.2"))
        exact, networks = _split_trusted_proxies(_as_cache_key(proxies))
        assert exact == frozenset({"10.0.0.1", "10.0.0.2"})
        assert networks == ()

    def test_result_is_cached_for_same_tuple(self):
        proxies = (ipaddress.ip_address("10.0.0.1"),)
        key = _as_cache_key(proxies)

        _split_trusted_proxies.cache_clear()
        _split_trusted_proxies(key)
        info_after_first = _split_trusted_proxies.cache_info()

        _split_trusted_proxies(key)
        info_after_second = _split_trusted_proxies.cache_info()

        assert info_after_first.misses == 1
        assert info_after_second.hits == info_after_first.hits + 1
        assert info_after_second.misses == info_after_first.misses

    def test_distinct_configurations_do_not_interfere(self):
        proxies_a = (ipaddress.ip_address("10.0.0.1"),)
        proxies_b = (ipaddress.ip_address("10.0.0.2"),)

        exact_a, _ = _split_trusted_proxies(_as_cache_key(proxies_a))
        exact_b, _ = _split_trusted_proxies(_as_cache_key(proxies_b))

        assert exact_a == frozenset({"10.0.0.1"})
        assert exact_b == frozenset({"10.0.0.2"})

    def test_list_and_tuple_inputs_are_equivalent(self):
        as_list = [ipaddress.ip_address("10.0.0.1")]
        as_tuple = (ipaddress.ip_address("10.0.0.1"),)

        result_from_list = _split_trusted_proxies(_as_cache_key(as_list))
        result_from_tuple = _split_trusted_proxies(_as_cache_key(as_tuple))

        assert result_from_list == result_from_tuple


class TestIsTrustedProxy:
    """Tests for `_is_trusted_proxy`."""

    def test_exact_match(self):
        exact, networks = _split_trusted_proxies(
            _as_cache_key((ipaddress.ip_address("10.0.0.1"),))
        )
        assert _is_trusted_proxy("10.0.0.1", exact, networks) is True

    def test_network_match(self):
        exact, networks = _split_trusted_proxies(
            _as_cache_key((ipaddress.ip_network("192.168.0.0/16"),))
        )
        assert _is_trusted_proxy("192.168.5.5", exact, networks) is True

    def test_no_match(self):
        exact, networks = _split_trusted_proxies(
            _as_cache_key((ipaddress.ip_address("10.0.0.1"),))
        )
        assert _is_trusted_proxy("8.8.8.8", exact, networks) is False

    def test_garbage_address_returns_false(self):
        """A malformed address string against a network config fails closed."""
        exact, networks = _split_trusted_proxies(
            _as_cache_key((ipaddress.ip_network("192.168.0.0/16"),))
        )
        assert _is_trusted_proxy("not-an-ip", exact, networks) is False

    def test_empty_configuration_never_trusts(self):
        assert _is_trusted_proxy("10.0.0.1", frozenset(), ()) is False


class TestIsAsyncCallable:
    """Tests for is_async_callable utility."""

    def test_async_function(self):
        """Test with async function."""

        async def async_func():
            pass

        assert is_async_callable(async_func) is True

    def test_sync_function(self):
        """Test with regular function."""

        def sync_func():
            pass

        assert is_async_callable(sync_func) is False

    def test_async_method(self):
        """Test with async method."""

        class MyClass:
            async def async_method(self):
                pass

        obj = MyClass()
        assert is_async_callable(obj.async_method) is True

    def test_sync_method(self):
        """Test with regular method."""

        class MyClass:
            def sync_method(self):
                pass

        obj = MyClass()
        assert is_async_callable(obj.sync_method) is False

    def test_async_callable_object(self):
        """Test with object having async __call__."""

        class AsyncCallable:
            async def __call__(self):
                pass

        obj = AsyncCallable()
        assert is_async_callable(obj) is True

    def test_sync_callable_object(self):
        """Test with object having sync __call__."""

        class SyncCallable:
            def __call__(self):
                pass

        obj = SyncCallable()
        assert is_async_callable(obj) is False

    def test_partial_async_function(self):
        """Test with functools.partial wrapping async function."""

        async def async_func(a, b):
            return a + b

        partial = functools.partial(async_func, 1)
        assert is_async_callable(partial) is True

    def test_partial_sync_function(self):
        """Test with functools.partial wrapping sync function."""

        def sync_func(a, b):
            return a + b

        partial = functools.partial(sync_func, 1)
        assert is_async_callable(partial) is False

    def test_lambda(self):
        """Test with lambda function."""
        assert is_async_callable(lambda: None) is False

    def test_builtin(self):
        """Test with builtin function."""
        assert is_async_callable(len) is False


class TestTimeFunction:
    """Tests for time utility."""

    def test_time_returns_float(self):
        """Test that time() returns a float."""
        t = time()
        assert isinstance(t, float)

    def test_time_is_positive(self):
        """Test that time() returns positive value."""
        t = time()
        assert t > 0

    def test_time_is_monotonic_increasing(self):
        """Test that time() returns increasing values."""
        t1 = time()
        anyio.run(anyio.sleep, 0.01)
        t2 = time()
        assert t2 >= t1

    def test_time_is_unix_timestamp(self):
        """Test that time() returns unix timestamp (reasonable range)."""
        t = time()
        # Should be within reasonable unix timestamp range (after 2020, before 2100)
        assert 1577836800 < t < 4102444800  # 2020-01-01 to 2100-01-01


class TestTaskTimer:
    """Tests for TaskTimer context manager."""

    async def test_task_timer_without_timeout(self):
        """Test TaskTimer with None timeout allows execution."""
        loop = asyncio.get_event_loop()
        async with _TaskTimer(timeout=None, loop=loop) as timer:
            # Should complete without timing out
            await anyio.sleep(0.01)
        assert timer.done()
        assert not timer.timed_out()

    async def test_task_timer_allows_fast_execution(self):
        """Test TaskTimer allows execution within timeout."""
        loop = asyncio.get_event_loop()
        async with _TaskTimer(timeout=0.1, loop=loop) as timer:
            await anyio.sleep(0.01)
        assert timer.done()
        assert not timer.timed_out()

    async def test_task_timer_times_out(self):
        """Test TaskTimer times out on slow execution."""
        loop = asyncio.get_event_loop()
        with pytest.raises(asyncio.TimeoutError):
            async with _TaskTimer(timeout=0.05, loop=loop):
                await anyio.sleep(0.2)

    async def test_task_timer_sets_timed_out_flag(self):
        """Test TaskTimer sets timed_out flag on timeout."""
        loop = asyncio.get_event_loop()
        timer = None
        try:
            async with _TaskTimer(timeout=0.05, loop=loop) as timer:
                await anyio.sleep(0.2)
        except asyncio.TimeoutError:
            assert timer is not None
            assert timer.timed_out()

    async def test_task_timer_cancelled_state(self):
        """Test TaskTimer cancelled() returns True when stopped normally."""
        loop = asyncio.get_event_loop()
        async with _TaskTimer(timeout=None, loop=loop) as timer:
            pass
        assert timer.done()
        assert timer.cancelled()

    async def test_task_timer_custom_error(self):
        """Test TaskTimer with custom error."""
        loop = asyncio.get_event_loop()
        custom_error = RuntimeError("Custom timeout error")
        with pytest.raises(RuntimeError, match="Custom timeout error"):
            async with _TaskTimer(timeout=0.05, loop=loop, error=custom_error):
                await anyio.sleep(0.2)

    async def test_task_timer_cannot_restart(self):
        """Test TaskTimer cannot be restarted after completion."""
        loop = asyncio.get_event_loop()
        timer = _TaskTimer(timeout=None, loop=loop)
        timer.start()
        timer.stop()
        with pytest.raises(RuntimeError, match="already cancelled or timed out"):
            timer.start()


class TestAddParameterToSignature:
    """Tests for _add_parameter_to_signature utility."""

    def test_add_parameter_at_beginning(self):
        """Test adding parameter at beginning of signature."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=0)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params[0] == "new_param"
        assert params == ["new_param", "a", "b"]

    def test_add_parameter_at_end(self):
        """Test adding parameter at end of signature."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=-1)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params[-1] == "new_param"
        assert params == ["a", "b", "new_param"]

    def test_add_parameter_in_middle(self):
        """Test adding parameter in middle of signature."""

        def func(a: int, b: str, c: bool):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=1)
        sig = inspect.signature(updated)
        params = list(sig.parameters.keys())
        assert params == ["a", "new_param", "b", "c"]

    def test_add_parameter_with_default(self):
        """Test adding parameter with default value."""

        def func(a: int = 3):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str,
            default="default_value",
        )
        updated = _add_parameter_to_signature(func, param, index=0)
        sig = inspect.signature(updated)
        assert sig.parameters["new_param"].default == "default_value"

    def test_add_parameter_index_out_of_bounds(self):
        """Test adding parameter with invalid index raises error."""

        def func(a: int, b: str):
            pass

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        with pytest.raises(ValueError, match="out of bounds"):
            _add_parameter_to_signature(func, param, index=10)

    def test_add_parameter_preserves_annotations(self):
        """Test that adding parameter preserves existing annotations."""

        def func(a: int, b: str) -> bool:
            return bool(a and b)

        param = inspect.Parameter(
            "new_param",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=float,
        )
        updated = _add_parameter_to_signature(func, param, index=1)
        sig = inspect.signature(updated)
        assert sig.return_annotation is bool
        assert sig.parameters["a"].annotation is int
        assert sig.parameters["b"].annotation is str


@pytest.mark.anyio
class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    async def test_initial_state(self):
        """Test circuit breaker starts in closed state."""
        breaker = CircuitBreaker()
        assert breaker.is_open is False
        assert breaker._state == CircuitState.CLOSED
        assert breaker._failure_count == 0

    async def test_open_after_threshold(self):
        """Test circuit opens after reaching failure threshold."""
        breaker = CircuitBreaker(failure_threshold=3)

        # Record failures
        await breaker.record_failure()
        assert breaker.is_open is False

        await breaker.record_failure()
        assert breaker.is_open is False

        await breaker.record_failure()
        # Should open after 3rd failure
        assert breaker.is_open is True
        assert breaker._state == CircuitState.OPEN

    async def test_half_open_after_timeout(self):
        """Test circuit enters half-open state after recovery timeout."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,  # 100ms
        )

        # Open the circuit
        await breaker.record_failure()
        assert breaker.is_open is True

        # Should still be open immediately
        assert breaker.is_open is True

        # Wait for recovery timeout
        await anyio.sleep(0.15)

        # Call allow_execution to trigger state transition to half-open
        allowed = await breaker.allow_execution()
        assert allowed is True
        assert breaker._state == CircuitState.HALF_OPEN

    async def test_close_from_half_open(self):
        """Test circuit closes after successful operations in half-open."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
            success_threshold=2,
        )

        # Open the circuit
        await breaker.record_failure()
        assert breaker.is_open is True

        # Wait for half-open
        await anyio.sleep(0.02)
        await breaker.allow_execution()  # Trigger transition to half-open

        # Record successes
        await breaker.record_success()
        assert breaker._state == CircuitState.HALF_OPEN

        await breaker.record_success()
        # Should close after 2nd success
        assert breaker._state == CircuitState.CLOSED

    async def test_reopen_from_half_open_on_failure(self):
        """Test circuit reopens if failure occurs in half-open state."""
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        # Open the circuit
        await breaker.record_failure()

        # Wait for half-open
        await anyio.sleep(0.02)
        await breaker.allow_execution()
        assert breaker._state == CircuitState.HALF_OPEN

        # Record a failure in half-open
        await breaker.record_failure()
        assert breaker._state == CircuitState.OPEN

    async def test_reset_failures_on_success_in_closed(self):
        """Test failures reset on success when circuit is closed."""
        breaker = CircuitBreaker(failure_threshold=3)

        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker._failure_count == 2

        # Success resets counter
        await breaker.record_success()
        assert breaker._failure_count == 0
        assert breaker._state == CircuitState.CLOSED

    async def test_info(self):
        """Test info() returns current state."""
        breaker = CircuitBreaker()
        info = await breaker.info()

        assert "state" in info
        assert "failure_count" in info
        assert "success_count" in info
        assert "opened_at" in info
        assert info["state"] == CircuitState.CLOSED.value
