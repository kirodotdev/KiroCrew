"""Tests for the mediated-request SSRF guard."""

from __future__ import annotations

import socket

import pytest

from kiro_crew.secrets_mediation import ssrf
from kiro_crew.secrets_mediation.ssrf import SsrfError, check_url


def _fake_getaddrinfo(ip: str, family: int = socket.AF_INET):
    def _inner(host, port, *a, **kw):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _inner


def test_rejects_non_https():
    with pytest.raises(SsrfError):
        check_url("http://example.com/x")


def test_rejects_missing_host():
    with pytest.raises(SsrfError):
        check_url("https://")


@pytest.mark.parametrize(
    "host",
    ["localhost", "metadata.google.internal", "metadata", "169.254.169.254"],
)
def test_rejects_blocked_hostnames_before_resolution(host):
    # These are refused by name — no resolver is consulted.
    with pytest.raises(SsrfError):
        check_url(f"https://{host}/path")


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "172.16.0.9",  # private
        "169.254.169.254",  # link-local / metadata
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
    ],
)
def test_rejects_hosts_resolving_to_blocked_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(SsrfError):
        check_url("https://evil.example.com/path")


def test_rejects_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("::1", socket.AF_INET6))
    with pytest.raises(SsrfError):
        check_url("https://evil.example.com/")


def test_allows_public_address_and_pins_it(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    target = check_url("https://example.com/api?q=1")
    assert target.host == "example.com"
    assert target.ip == "93.184.216.34"
    assert target.port == 443


def test_explicit_port_preserved(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    target = check_url("https://example.com:8443/api")
    assert target.port == 8443


def test_mixed_public_and_private_records_fail_closed(monkeypatch):
    # A resolver returning BOTH a public and a private A record is the rebinding
    # shape; the whole target is refused rather than cherry-picking the public one.
    def _mixed(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)
    with pytest.raises(SsrfError):
        check_url("https://example.com/")


def test_resolution_failure_fails_closed(monkeypatch):
    def _boom(host, port, *a, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(SsrfError):
        check_url("https://nx.example.com/")


def test_too_many_addresses_fail_closed(monkeypatch):
    def _many(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{i}", port))
            for i in range(ssrf._MAX_RESOLVED_ADDRS + 5)
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _many)
    with pytest.raises(SsrfError):
        check_url("https://example.com/")
