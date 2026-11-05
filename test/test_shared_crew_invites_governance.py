"""Tailscale invites follow the enterprise ``capabilities.tailnet_origin`` pin.

Invites share this machine over Tailscale, so a ceiling that pins tailnet access
off closes them the way it closes the direct tailnet listener: the invite and
credential routes answer 404, the accept poller never starts, and the
capabilities read reports invites unavailable with ``governance_pinned_off``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.platform import governance_profiles
from kiro_crew.platform.governance import parse_policy

pytestmark = pytest.mark.asyncio

_PIN_OFF = {"version": 1, "boot": {}, "capabilities": {"tailnet_origin": {"enabled": False}}}


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _install_policy(monkeypatch: pytest.MonkeyPatch, doc: dict | None) -> None:
    from kiro_crew.platform import context as pc

    ctx = SimpleNamespace(governance=parse_policy(doc) if doc is not None else None)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(pc, "installed_context", lambda: ctx)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)


_ROUTES = [
    ("GET", "/api/shared-crew/tailscale/invites"),
    ("POST", "/api/shared-crew/tailscale/invites"),
    ("DELETE", "/api/shared-crew/tailscale/invites/inv1"),
    ("POST", "/api/shared-crew/tailscale/invites/inv1/resend"),
    ("POST", "/api/shared-crew/tailscale/credential"),
    ("DELETE", "/api/shared-crew/tailscale/credential"),
]


async def _statuses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from kiro_crew.shared_crew import routes_invites

    async def _not_reached(_request):
        pytest.fail("a pinned route must not reach its owner check")

    monkeypatch.setattr(routes_invites, "_owner_refusal", _not_reached)
    monkeypatch.setattr(routes_invites, "_start_poller", _no_poller)
    app = web.Application()
    routes_invites.register(app)
    out = []
    async with TestClient(TestServer(app)) as client:
        for method, path in _ROUTES:
            resp = await client.request(method, path, json={})
            out.append(resp.status)
    return out


async def _no_poller(_app) -> None:
    return None


async def test_pinned_off_invite_and_credential_routes_answer_404(enterprise, monkeypatch):
    _install_policy(monkeypatch, _PIN_OFF)
    assert await _statuses(monkeypatch) == [404] * len(_ROUTES)


async def test_unpinned_routes_reach_the_owner_check(enterprise, monkeypatch):
    from kiro_crew.shared_crew import routes_invites

    _install_policy(monkeypatch, None)
    reached: list[str] = []

    async def _refuse(request):
        reached.append(request.path)
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(routes_invites, "_owner_refusal", _refuse)
    monkeypatch.setattr(routes_invites, "_start_poller", _no_poller)
    app = web.Application()
    routes_invites.register(app)
    async with TestClient(TestServer(app)) as client:
        for method, path in _ROUTES:
            resp = await client.request(method, path, json={})
            assert resp.status == 403, path
    assert len(reached) == len(_ROUTES)


async def test_pinned_off_poller_does_not_start(enterprise, monkeypatch):
    from kiro_crew.shared_crew import routes_invites
    from kiro_crew.shared_crew import tailscale_invites as invites

    _install_policy(monkeypatch, _PIN_OFF)

    async def _poll(*_a, **_k):
        pytest.fail("the accept poller must not run while tailnet access is pinned off")

    monkeypatch.setattr(invites, "poll_forever", _poll)
    app = web.Application()
    await routes_invites._start_poller(app)
    assert app.get(routes_invites._POLLER_KEY) is None


async def test_unpinned_poller_starts(enterprise, monkeypatch):
    import asyncio

    from kiro_crew.shared_crew import routes_invites
    from kiro_crew.shared_crew import tailscale_invites as invites

    _install_policy(monkeypatch, None)
    started = asyncio.Event()

    async def _poll(*_a, **_k):
        started.set()

    monkeypatch.setattr(invites, "poll_forever", _poll)
    app = web.Application()
    await routes_invites._start_poller(app)
    task = app.get(routes_invites._POLLER_KEY)
    assert task is not None
    await task
    assert started.is_set()


async def test_capabilities_route_runs_the_probe_off_the_event_loop(enterprise, monkeypatch):
    import threading

    from kiro_crew.dashboard import tailnet
    from kiro_crew.shared_crew import routes_capabilities

    _install_policy(monkeypatch, _PIN_OFF)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = tailnet.is_governance_pinned_off

    def _probe(*, audit_tool: str = "") -> bool:
        seen.append(threading.get_ident())
        return real(audit_tool=audit_tool)

    monkeypatch.setattr(tailnet, "is_governance_pinned_off", _probe)
    app = web.Application()
    routes_capabilities.register(app)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get(routes_capabilities.CAPABILITIES_PATH)).json()
    assert body["invites"] == {"state": "unavailable", "reason": "governance_pinned_off"}
    assert seen and loop_thread not in seen


def test_capabilities_report_invites_unavailable_when_pinned(enterprise, monkeypatch):
    from kiro_crew.shared_crew import capabilities

    _install_policy(monkeypatch, _PIN_OFF)
    assert capabilities.invites_capability() == {
        "state": "unavailable",
        "reason": "governance_pinned_off",
    }
    _install_policy(monkeypatch, None)
    assert capabilities.invites_capability() == {"state": "available", "reason": ""}
    assert capabilities.aggregate_capabilities().to_dict()["invites"]["state"] == "available"


async def test_a_running_poller_sync_reads_nothing_once_pinned(enterprise, monkeypatch):
    """A live policy refresh can pin tailnet access off after the poller started."""
    from kiro_crew.shared_crew import tailscale_invites as invites

    _install_policy(monkeypatch, _PIN_OFF)
    monkeypatch.setattr(invites, "_api", lambda: pytest.fail("a pinned sync reads no Tailscale"))
    with pytest.raises(invites.InviteError) as caught:
        await invites.sync()
    assert caught.value.code == invites.CODE_GOVERNANCE_PINNED


async def test_a_pin_that_lands_mid_sync_admits_nobody(enterprise, monkeypatch):
    from kiro_crew.shared_crew import tailscale_invites as invites

    answers = iter([False, True])
    asked: list[str] = []

    def _pinned(*, audit_tool: str = "") -> bool:
        asked.append(audit_tool)
        return next(answers)

    class _Api:
        async def list_invites(self, _device_id):
            return []

    async def _no_commit(*_a, **_k):
        pytest.fail("no admission is committed once the pin lands")

    monkeypatch.setattr(invites, "_governance_pinned", _pinned)
    monkeypatch.setattr(invites, "_api", lambda: (_Api(), "dev1"))
    monkeypatch.setattr(invites, "_commit_accepts", _no_commit)
    with pytest.raises(invites.InviteError) as caught:
        await invites.sync(force=True)
    assert caught.value.code == invites.CODE_GOVERNANCE_PINNED
    # The admission check is the acting one, so it is the audited one.
    assert asked == ["", "shared_crew_invite_admission"]
