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
        bypass.assert_called_once_with("updates.example")
    else:
        bypass.assert_not_called()


@pytest.mark.parametrize(
    ("no_proxy", "expected"),
    [
        ("updates.example", None),
        (".example", None),
        ("*", None),
        ("other.example", _HTTPS_PROXY),
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


def test_proxy_settings_are_read_per_call(monkeypatch):
    proxies = MagicMock(side_effect=[{"https": _HTTPS_PROXY}, {"http": _HTTP_PROXY}])
    bypass = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(http_proxy.urllib.request, "getproxies", proxies)
    monkeypatch.setattr(http_proxy.urllib.request, "proxy_bypass", bypass)

    assert http_proxy.resolve_proxy("https://updates.example/feed") == _HTTPS_PROXY
    assert http_proxy.resolve_proxy("https://updates.example/feed") is None
    assert proxies.call_count == 2
    assert bypass.call_count == 2
