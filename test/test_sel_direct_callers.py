"""Backend-run SEL callers log through ``sel()``, which is the gateway client in a backend.

In a spawned app backend the SEL signing key is masked, so building a
``SecurityEventLog`` directly fails there. Each caller below must reach the log through
``sel()``: the real ``SelClient`` posts the row to the real gateway route, the row is
app-attributed ``api_access``, and a deny exception is still granted (no fail-closed
loss) because its row is taken by the gateway.
"""

# flake8: noqa: F811  -- ``rec`` and ``gateway`` are fixtures imported from the sel route tests
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_sel_client import gateway  # noqa: F401  (the real route and token exchange)
from test_sel_routes import _A, rec  # noqa: F401  (``rec`` is the route tests' fixture)

from kiro_crew.apps import sel_client
from kiro_crew.security import deny_gate, exfil_oauth


class _MaskedKey:
    """The direct constructor, as a backend with the key masked sees it."""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        raise RuntimeError("sel_hmac.key unreadable")


@pytest.fixture
def backend_env(monkeypatch: pytest.MonkeyPatch, rec):
    """Mask the key; return a factory for the backend's client against a gateway.

    The callers read ``sel`` through their own module binding, so it is set there to
    the client and not through the spawn env: the gateway runs in this process, and
    the env would turn its own audit rows into client writes that feed back into it.
    """
    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _MaskedKey)
    for module in (deny_gate, exfil_oauth):
        monkeypatch.setattr(module, "SecurityEventLog", _MaskedKey)

    def point_at(client: TestClient, secret: str = "per-app-secret") -> sel_client.SelClient:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), secret)
        for module in (deny_gate, exfil_oauth):
            monkeypatch.setattr(module, "sel", lambda: backend)
        return backend

    return point_at


def _rows(rec, event_type: str) -> list[dict[str, Any]]:
    return [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.security.{event_type}"]


@pytest.mark.asyncio
async def test_deny_exception_is_granted_with_the_key_masked(gateway, rec, backend_env) -> None:
    async with TestClient(TestServer(gateway)) as client:
        backend_env(client)
        granted = await asyncio.to_thread(deny_gate._emit_deny_exception_event, "Bash", "rm -rf")
    assert granted is True
    rows = _rows(rec, "deny_exception")
    assert [(kw["caller"], kw["outcome"]) for kw in rows] == [(f"app:{_A}", "allowed")]


@pytest.mark.asyncio
async def test_a_grant_row_names_the_command_it_granted(gateway, rec, backend_env) -> None:
    async with TestClient(TestServer(gateway)) as client:
        backend_env(client)
        await asyncio.to_thread(deny_gate._emit_deny_exception_event, "git log --oneline", "p")
    rows = _rows(rec, "deny_exception")
    assert [kw["resources"].split(" ")[0] for kw in rows] == ["operation=git"]
    assert "operation=git log --oneline" in rows[0]["resources"]


@pytest.mark.asyncio
async def test_a_secret_shaped_command_is_redacted_in_the_grant_row(
    gateway, rec, backend_env
) -> None:
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    async with TestClient(TestServer(gateway)) as client:
        backend_env(client)
        await asyncio.to_thread(deny_gate._emit_deny_exception_event, f"echo {secret}", "p")
    rows = _rows(rec, "deny_exception")
    assert len(rows) == 1 and secret not in rows[0]["resources"]
    assert rows[0]["resources"].startswith("operation=echo ")


@pytest.mark.asyncio
async def test_deny_exception_is_refused_when_the_gateway_does_not_take_the_row(
    gateway, rec, backend_env
) -> None:
    async with TestClient(TestServer(gateway)) as client:
        backend_env(client, secret="wrong-secret")
        granted = await asyncio.to_thread(deny_gate._emit_deny_exception_event, "Bash", "rm -rf")
    assert granted is False
    assert _rows(rec, "deny_exception") == []


@pytest.mark.asyncio
async def test_the_other_backend_run_callers_land_as_app_api_access_rows(
    gateway, rec, backend_env
) -> None:
    def emit_all(backend: sel_client.SelClient) -> None:
        deny_gate._emit_deny_event("Bash", "rm -rf", "rm -rf /")
        deny_gate._emit_push_allow_event("git push origin feature")
        deny_gate.audit_injection_dropped(surface="slack_thread_parent")
        exfil_oauth._emit_oauth_discovered_endpoint_event("idp.example", "/authorize", "https://x")
        exfil_oauth._log_operator_list_use("oauth_endpoint_extension_used", "o", "r", lambda: {})
        backend._queue.join()  # the non-critical rows are sent from a thread

    async with TestClient(TestServer(gateway)) as client:
        await asyncio.to_thread(emit_all, backend_env(client))
    expected = [
        "deny_event",
        "push_allowed",
        "prompt_injection_dropped",
        "oauth_endpoint_discovered",
        "oauth_endpoint_extension_used",
    ]
    for event_type in expected:
        assert [kw["caller"] for kw in _rows(rec, event_type)] == [f"app:{_A}"], event_type
    assert {name for name, _kw in rec.rows} == {"log_api_access"}  # no new event kind
