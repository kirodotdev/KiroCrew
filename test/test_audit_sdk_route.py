"""``ctx.audit`` from a spawned third-party backend with the SEL key masked.

The App Kit audit seam (``AuditSDK.record``) writes in-process with the real
log when the backend can read the key. With ``trust/sel_hmac.key`` hidden from the backend
that would lose every row,
so the row has to reach the log through the gateway's ``_sel`` route, and a route
failure has to show at warning level.
"""

# flake8: noqa: F811  -- ``rec`` and ``gateway`` are fixtures imported from sibling tests
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_sel_client import gateway, settle  # noqa: F401
from test_sel_routes import _A, rec  # noqa: F401

from kiro_crew.apps.audit_sdk import AuditSDK
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture
def key_holder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces ``SecurityEventLog``; any entry means the process touched the key."""
    calls: list[str] = []

    class _KeyHolder:
        def __init__(self) -> None:
            calls.append("built")

    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _KeyHolder)
    return calls


def _spawned(monkeypatch: pytest.MonkeyPatch, origin: str, rec: Any) -> None:
    """Set the backend's variables; the gateway half of this process writes to ``rec``.

    Gateway and backend share one process here, so the gateway's token audit would
    otherwise resolve ``sel()`` to the backend's own route client and queue a row for
    every request the client makes, without end.
    """
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._sel_fn", lambda: rec)
    for k, v in {
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": _A,
        "KIROCREW_GATEWAY_ORIGIN": origin,
        "KIROCREW_PROXY_SECRET": "per-app-secret",
    }.items():
        monkeypatch.setenv(k, v)
    from kiro_crew.apps import sel_client

    sel_client._clients.clear()


def _settle() -> None:
    from kiro_crew.apps import sel_client

    for client in list(sel_client._clients.values()):
        settle(client)


@pytest.mark.asyncio
async def test_audit_log_from_a_key_masked_backend_lands_as_an_api_access_row(
    gateway, rec, key_holder, monkeypatch
) -> None:
    async with TestClient(TestServer(gateway)) as client:
        _spawned(monkeypatch, str(client.make_url("")).rstrip("/"), rec)
        await asyncio.to_thread(AuditSDK(_A).record, "publish", "ok", resources="doc-1")
        # the client sends from a worker thread
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.publish" for _n, kw in rec.app_rows()),
        )
        await asyncio.to_thread(_settle)
    ours = [(n, kw) for n, kw in rec.app_rows() if kw["operation"] == f"{_A}.publish"]
    assert [(n, kw["outcome"], kw["caller"]) for n, kw in ours] == [
        ("log_api_access", "ok", f"app:{_A}")
    ]
    assert key_holder == []


@pytest.mark.asyncio
async def test_audit_log_with_a_huge_resources_still_lands_clipped(
    gateway, rec, key_holder, monkeypatch
) -> None:
    """20,000 characters is over the route's body cap: the row must be clipped, not lost."""
    async with TestClient(TestServer(gateway)) as client:
        _spawned(monkeypatch, str(client.make_url("")).rstrip("/"), rec)
        await asyncio.to_thread(
            AuditSDK(_A).record, "big", "denied", resources="z" * 20_000, error="e" * 20_000
        )
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.big" for _n, kw in rec.app_rows()),
        )
        await asyncio.to_thread(_settle)
    (row,) = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.big"]
    assert len(row["resources"]) <= 500 and len(row["error"]) <= 500


@pytest.mark.asyncio
async def test_a_route_failure_is_logged_at_warning(rec, key_holder, monkeypatch, caplog) -> None:
    _spawned(monkeypatch, "http://127.0.0.1:1", rec)  # nothing listens: the route is unreachable
    caplog.set_level(logging.DEBUG)
    await asyncio.to_thread(AuditSDK(_A).record, "publish", "ok")
    # two retries at 0.2 s and 1.0 s, then the row is dropped
    await async_wait_until(lambda: any(r.levelno >= logging.WARNING for r in caplog.records))
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned and _A in warned[0].getMessage()


def test_a_write_the_sdk_cannot_make_is_logged_at_warning_without_the_operation(
    monkeypatch, caplog
) -> None:
    def boom() -> Any:
        raise OSError("sel unwritable")

    monkeypatch.setattr("kiro_crew.apps.audit_sdk.sel", boom)
    caplog.set_level(logging.DEBUG)
    AuditSDK(_A).record("secret-verb", "ok")  # never raises
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1 and _A in warned[0].getMessage()
    assert "secret-verb" not in caplog.text
