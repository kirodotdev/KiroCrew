"""Shared explicit proxy selection, independent of host proxy settings."""

from unittest.mock import MagicMock

import pytest

from kiro_crew import http_proxy

_HTTPS_PROXY = "http://https-proxy.example:3128"
_HTTP_PROXY = "http://http-proxy.example:3128"


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({"https": _HTTPS_PROXY, "http": _HTTP_PROXY}, _HTTPS_PROXY),
        ({"https": _HTTPS_PROXY}, _HTTPS_PROXY),
        ({"http": _HTTP_PROXY}, _HTTP_PROXY),
        ({"https": "", "http": _HTTP_PROXY}, _HTTP_PROXY),
        ({}, None),
    ],
)
@pytest.mark.parametrize("scheme", ["https", "http"])
def test_proxy_priority(monkeypatch, proxies, expected, scheme):
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies", lambda: proxies)
    bypass = MagicMock(return_value=False)
    monkeypatch.setattr(http_proxy.urllib.request, "proxy_bypass", bypass)

    assert http_proxy.resolve_proxy(f"{scheme}://updates.example:8443/feed") == expected

    if expected:
        assert [c.args for c in bypass.call_args_list] == [
            ("updates.example",),
            ("updates.example:8443",),
        ]
    else:
        bypass.assert_not_called()


@pytest.mark.parametrize(
    ("no_proxy", "expected"),
    [
        ("updates.example", None),
        (".example", None),
        ("*", None),
        ("other.example", _HTTPS_PROXY),
        ("updates.example:443", None),
        ("updates.example:8443", _HTTPS_PROXY),
    ],
)
def test_environment_bypass(monkeypatch, no_proxy, expected):
    # Pin the readers to environment-only variants on every OS.
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _HTTPS_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    monkeypatch.setattr(
        http_proxy.urllib.request, "getproxies", http_proxy.urllib.request.getproxies_environment
    )
    monkeypatch.setattr(
        http_proxy.urllib.request,
        "proxy_bypass",
        http_proxy.urllib.request.proxy_bypass_environment,
    )

    assert http_proxy.resolve_proxy("https://updates.example/feed") == expected


def _env_proxy(monkeypatch, no_proxy):
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("NO_PROXY", no_proxy)


@pytest.mark.parametrize(
    ("no_proxy", "url", "expected"),
    [
        ("discord.com", "https://discord.com/api/v10/x", None),
        (".discord.gg", "wss://gateway.discord.gg/?v=10", None),
        ("*", "https://webexapis.com/v1/people/me", None),
        ("webexapis.com:443", "https://webexapis.com/v1/x", None),
        ("webexapis.com:8443", "https://webexapis.com/v1/x", _HTTPS_PROXY),
        ("example.org:80", "ws://example.org/ws", None),
        ("other.example", "https://discord.com/api", _HTTPS_PROXY),
        ("discord.com", "not a url", _HTTPS_PROXY),
        ("discord.com", "https://discord.com:bad/", _HTTPS_PROXY),
    ],
)
def test_proxy_for_url_honours_no_proxy(monkeypatch, no_proxy, url, expected):
    _env_proxy(monkeypatch, no_proxy)

    assert http_proxy.proxy_for_url(_HTTPS_PROXY, url) == expected


def test_proxy_for_url_without_proxy_is_direct(monkeypatch):
    _env_proxy(monkeypatch, "")

    assert http_proxy.proxy_for_url(None, "https://discord.com/api") is None
    assert http_proxy.proxy_for_url(_HTTPS_PROXY, "https://discord.com/api") == _HTTPS_PROXY


def test_proxy_settings_are_read_per_call(monkeypatch):
    proxies = MagicMock(side_effect=[{"https": _HTTPS_PROXY}, {"http": _HTTP_PROXY}])
    bypass = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies", proxies)
    monkeypatch.setattr(http_proxy.urllib.request, "proxy_bypass", bypass)

    assert http_proxy.resolve_proxy("https://updates.example/feed") == _HTTPS_PROXY
    assert http_proxy.resolve_proxy("https://updates.example/feed") is None
    assert proxies.call_count == 2
    assert bypass.call_count == 3


def test_bypass_check_does_not_raise_on_an_out_of_range_port(monkeypatch):
    # A base URL such as TELEGRAM_API_BASE_URL ending in :99999 must not make
    # the shared check raise ValueError, which Telegram's call paths do not catch.
    url = "https://tg.example:99999/botT/getMe"
    # Pin the OS matcher to the environment-only variant, as the test above
    # does, so the os case never reads this host's own registry or settings.
    monkeypatch.setattr(
        http_proxy.urllib.request,
        "proxy_bypass",
        http_proxy.urllib.request.proxy_bypass_environment,
    )
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert http_proxy.is_bypassed(url) is False
    assert http_proxy.proxy_for_url(_HTTPS_PROXY, url) == _HTTPS_PROXY
    monkeypatch.setenv("NO_PROXY", "tg.example")
    assert http_proxy.is_bypassed(url) is True


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        # What the stdlib returns for a Windows `ProxyServer` of `127.0.0.1:3128`.
        (
            {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"},
            "http://127.0.0.1:3128",
        ),
        # `http=127.0.0.1:3128;https=127.0.0.1:3128`: an https= entry is still http://.
        (
            {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"},
            "http://127.0.0.1:3128",
        ),
        # `socks=127.0.0.1:1080`: the stdlib copies a socks4:// URL into http and https.
        (
            {
                "socks": "socks://127.0.0.1:1080",
                "http": "socks4://127.0.0.1:1080",
                "https": "socks4://127.0.0.1:1080",
            },
            None,
        ),
        ({"https": "socks5://127.0.0.1:1080"}, None),
        # `http=127.0.0.1:3128;socks=127.0.0.1:1080`: the stdlib fills https with
        # socks4://, and the http entry is still a usable HTTP CONNECT proxy.
        (
            {
                "http": "http://127.0.0.1:3128",
                "socks": "socks://127.0.0.1:1080",
                "https": "socks4://127.0.0.1:1080",
            },
            "http://127.0.0.1:3128",
        ),
    ],
    ids=["single-server", "per-protocol", "windows-socks", "socks5", "http-plus-socks"],
)
def test_only_a_proxy_the_clients_can_use_is_returned(monkeypatch, proxies, expected):
    # These come from the OS settings: no proxy variable is set.
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(http_proxy.urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(http_proxy, "_warned_schemes", set())

    assert http_proxy.resolve_proxy("https://slack.com/api/") == expected


_UNUSABLE_ENV_VALUES = ["socks5://127.0.0.1:1080", "10.0.0.1:3128"]


@pytest.mark.parametrize("value", _UNUSABLE_ENV_VALUES, ids=["socks5", "schemeless"])
def test_an_environment_proxy_passes_through_unchanged(monkeypatch, value):
    # The operator's own HTTPS_PROXY reaches the client as written, whatever its
    # scheme, so a misconfigured value fails closed there as on main.
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", value)
    monkeypatch.setattr(
        http_proxy.urllib.request, "getproxies", http_proxy.urllib.request.getproxies_environment
    )
    monkeypatch.setattr(
        http_proxy.urllib.request,
        "proxy_bypass",
        http_proxy.urllib.request.proxy_bypass_environment,
    )

    assert http_proxy.resolve_proxy("https://slack.com/api/") == value


@pytest.mark.parametrize("value", _UNUSABLE_ENV_VALUES, ids=["socks5", "schemeless"])
def test_an_environment_proxy_wins_over_the_os_and_passes_through(monkeypatch, value):
    # On Windows getproxies() returns the environment when it names a proxy,
    # and the registry only when it does not.
    env = {"https": value}
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies_environment", lambda: env)
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies", lambda: env)
    monkeypatch.setattr(http_proxy.urllib.request, "proxy_bypass", lambda host: False)

    assert http_proxy.resolve_proxy("https://slack.com/api/") == value
