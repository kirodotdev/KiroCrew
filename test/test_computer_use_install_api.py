"""agent-desktop install API: status contract, owner gate, job lifecycle."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.computer_use import agent_desktop_install as adi
from kiro_crew.dashboard.handlers import computer_use_install as api
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.testing.wait import async_wait_until

_READY = {
    "installed": False,
    "version": None,
    "pinned": "1.0.2",
    "path": None,
    "permissions": {"accessibility": "unknown", "screen_recording": "unknown"},
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    api._reset_for_tests()
    monkeypatch.setattr(adi, "readiness", lambda: dict(_READY))
    monkeypatch.setattr(api, "_driver_active", lambda: "native")
    sel = MagicMock()
    monkeypatch.setattr(api, "_sel", lambda: sel)
    yield sel
    api._reset_for_tests()


def _owner(monkeypatch: pytest.MonkeyPatch, is_owner: bool) -> None:
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda req: is_owner)


def _request(path: str) -> Any:
    req = MagicMock()
    req.path = path
    claims = {"app": "", "user": "the-owner"}
    req.get = lambda key, default=None: claims.get(key, default)
    return req


def _payload(resp: Any) -> dict[str, Any]:
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_status_has_exactly_the_contract_keys(monkeypatch):
    _owner(monkeypatch, True)
    body = _payload(await api.api_agent_desktop_status(_request("/s")))
    assert set(body) == {
        "installed",
        "version",
        "pinned",
        "path",
        "driver_active",
        "permissions",
        "job",
    }
    assert set(body["permissions"]) == {"accessibility", "screen_recording"}
    assert body["job"] == {"state": "idle", "log_tail": "", "error": None}
    assert body["driver_active"] == "native"


@pytest.mark.parametrize(
    "handler",
    [
        api.api_agent_desktop_status,
        api.api_agent_desktop_install,
        api.api_agent_desktop_permissions_guide,
    ],
)
@pytest.mark.asyncio
async def test_every_route_refuses_a_non_owner_and_audits_it(monkeypatch, _isolate, handler):
    _owner(monkeypatch, False)
    monkeypatch.setattr(adi, "install", lambda *a: pytest.fail("installed for a non-owner"))
    monkeypatch.setattr(adi, "open_permissions_guide", lambda: pytest.fail("guide for a non-owner"))
    monkeypatch.setattr(adi, "readiness", lambda: pytest.fail("status for a non-owner"))
    resp = await handler(_request("/m"))
    assert resp.status == 403
    assert _isolate.log_api_access.call_args.kwargs["outcome"] == "denied"
    assert api._job.state == "idle"


@pytest.mark.asyncio
async def test_install_runs_in_the_background_and_a_second_click_joins(monkeypatch, _isolate):
    _owner(monkeypatch, True)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fake_install(on_log=None):
        calls.append(1)
        on_log("$ npm install ...")
        entered.set()
        assert release.wait(10)
        return {"ok": True, "version": "1.0.2", "path": "/p"}

    monkeypatch.setattr(adi, "install", fake_install)
    first = await api.api_agent_desktop_install(_request("/i"))
    assert first.status == 202 and _payload(first)["job"]["state"] == "running"
    await async_wait_until(entered.is_set, timeout=10)
    second = await api.api_agent_desktop_install(_request("/i"))
    assert _payload(second)["job"]["state"] == "running"
    release.set()
    await async_wait_until(lambda: api._job.state == "done", timeout=10)
    assert calls == [1]
    body = _payload(await api.api_agent_desktop_status(_request("/s")))
    assert body["job"]["state"] == "done" and "npm install" in body["job"]["log_tail"]
    assert _isolate.log_api_access.call_args.kwargs["outcome"] == "allowed"


@pytest.mark.asyncio
async def test_a_failed_install_reports_step_and_error(monkeypatch):
    _owner(monkeypatch, True)
    monkeypatch.setattr(
        adi, "install", lambda on_log=None: {"ok": False, "step": "verify", "error": "mismatch"}
    )
    await api.api_agent_desktop_install(_request("/i"))
    await async_wait_until(lambda: api._job.state == "failed", timeout=10)
    assert api._job.error == "verify: mismatch"


@pytest.mark.asyncio
async def test_an_exception_is_a_redacted_failure(monkeypatch):
    _owner(monkeypatch, True)

    def boom(on_log=None):
        raise RuntimeError("npm said _authToken=abcdef123")

    monkeypatch.setattr(adi, "install", boom)
    await api.api_agent_desktop_install(_request("/i"))
    await async_wait_until(lambda: api._job.state == "failed", timeout=10)
    assert "abcdef123" not in (api._job.error or "")


@pytest.mark.asyncio
async def test_stop_interrupts_a_running_install(monkeypatch):
    _owner(monkeypatch, True)
    entered, release = threading.Event(), threading.Event()

    def parked(on_log=None):
        entered.set()
        assert release.wait(10)
        return {"ok": True}

    monkeypatch.setattr(adi, "install", parked)
    await api.api_agent_desktop_install(_request("/i"))
    await async_wait_until(entered.is_set, timeout=10)
    stopper = asyncio.create_task(api.stop_agent_desktop_install())
    await async_wait_until(lambda: api._job.scope.terminated, timeout=10)
    release.set()
    await stopper
    assert api._job.state == "failed" and "interrupted" in (api._job.error or "")
    assert api._job.scope is not None and api._job.scope.terminated


@pytest.mark.asyncio
async def test_stop_with_no_job_is_a_no_op():
    await api.stop_agent_desktop_install()


@pytest.mark.asyncio
async def test_guide_success_and_refusal(monkeypatch):
    _owner(monkeypatch, True)
    monkeypatch.setattr(adi, "open_permissions_guide", lambda: {"ok": True, "error": None})
    ok = await api.api_agent_desktop_permissions_guide(_request("/g"))
    assert ok.status == 200 and _payload(ok) == {"ok": True, "error": None}
    monkeypatch.setattr(adi, "open_permissions_guide", lambda: {"ok": False, "error": "nope"})
    bad = await api.api_agent_desktop_permissions_guide(_request("/g"))
    assert bad.status == 409 and _payload(bad)["error"] == "nope"


@pytest.mark.parametrize(
    ("cls_name", "expected"),
    [
        ("AgentDesktopBackend", "agent-desktop"),
        ("UnsupportedBackend", "unsupported"),
        ("FakeComputerUseBackend", "unsupported"),
    ],
)
def test_driver_active_reads_the_class_name(monkeypatch, cls_name, expected):
    from kiro_crew.computer_use import backend

    monkeypatch.undo()  # drop the autouse _driver_active stub for this test
    fake = type(cls_name, (), {})()
    monkeypatch.setattr(backend, "get_shared_backend", lambda: fake)
    assert api._driver_active() == expected


def test_driver_active_survives_a_backend_that_will_not_build(monkeypatch):
    from kiro_crew.computer_use import backend

    monkeypatch.undo()

    def broken():
        raise RuntimeError("no framework")

    monkeypatch.setattr(backend, "get_shared_backend", broken)
    assert api._driver_active() == "unsupported"


def test_routes_register_with_a_cleanup_hook():
    from aiohttp import web

    app = web.Application()
    before = len(app.on_cleanup)
    api.register_routes(app)
    paths = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/computer-use/agent-desktop/status") in paths
    assert ("POST", "/api/computer-use/agent-desktop/install") in paths
    assert ("POST", "/api/computer-use/agent-desktop/permissions-guide") in paths
    assert len(app.on_cleanup) == before + 1


def test_one_huge_log_entry_is_cut_to_the_bound():
    job = api._Job()
    cap = 4 * adi.LOG_TAIL_CAP
    job.append("x" * (cap * 50) + "TAIL")
    assert len(job.log) == 1 and len(job.log[0]) == cap
    assert job.log_tail().endswith("TAIL")
