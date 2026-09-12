"""The front's backend client must not read ambient proxy or trust-store env.

Every request it makes goes to ``http://127.0.0.1:<backend_port>``. Ambient
environment is wrong for that in both directions httpx would read it:

* An inherited ``HTTP_PROXY`` / ``ALL_PROXY`` would route the front's internal
  turn traffic -- whole conversations -- through a proxy instead of to the
  process next door.
* httpx eagerly builds an SSL context from ``SSL_CERT_FILE`` even for a client
  that never speaks TLS, so a stale value crashes ``AsyncClient(...)``
  construction with ``FileNotFoundError`` before any request is made.

The second one is how this was found: CI's environment carried an
``SSL_CERT_FILE`` pointing at a file that was not there, and twelve front tests
died inside client construction while every local run passed. So the assertion
below sets that exact condition rather than trusting the flag's presence.
"""

from __future__ import annotations

import os

import httpx
import pytest
from container.front import app as front_app


def _client_for(monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    """Build the client the way the app does, under a hostile environment."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca-bundle.crt")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")

    class _State:
        pass

    class _App:
        state = _State()

    return front_app._get_client(_App())  # type: ignore[arg-type]


def test_construction_survives_a_stale_ssl_cert_file(monkeypatch):
    """A missing SSL_CERT_FILE must not stop a plain-HTTP loopback client."""
    assert os.environ.get("SSL_CERT_FILE") != "/nonexistent/ca-bundle.crt"
    client = _client_for(monkeypatch)
    assert isinstance(client, httpx.AsyncClient)


def test_no_ambient_proxy_is_mounted(monkeypatch):
    """An inherited proxy must not carry the front's internal turn traffic.

    Asserted through the client's own mounts rather than by reading the flag
    back: ``trust_env`` is the mechanism, and a proxy reaching a loopback call
    is the consequence that actually matters.
    """
    client = _client_for(monkeypatch)
    proxied = [pattern for pattern, transport in client._mounts.items() if transport is not None]
    assert proxied == [], f"the backend client mounted ambient proxies: {proxied}"


def test_verification_is_still_on(monkeypatch):
    """``trust_env=False`` must not be confused with ``verify=False``.

    Turning verification off would matter the day the backend address becomes an
    https one, so pin that this fix did not quietly do that.
    """
    client = _client_for(monkeypatch)
    transport = client._transport
    ssl_context = getattr(getattr(transport, "_pool", None), "_ssl_context", None)
    assert ssl_context is not None, "no SSL context on the transport to check"
    assert (
        ssl_context.verify_mode is not __import__("ssl").CERT_NONE
    ), "certificate verification is off on the front's backend client"
