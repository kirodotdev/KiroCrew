"""The bell note when a team Crew first shows up in the relay viewer's Crew list.

Announced once per Crew from a successful list read, kept in a state file under
the data home (not browser storage), one summary for two or more at once, never
from a failed read or with the viewer off, and a dot (``unseen``) until the
person opens the Crew or opens the switcher that lists it.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import relay_viewer as rv
from kiro_crew.relay_viewer import cps, crew_notices

A = "a" * 24
B = "b" * 24
C = "c" * 24
TEAM_A = cps.CrewEndpoint(
    endpoint_id=A,
    crew_url=f"https://{A}.relay.example.test",
    name="Build box",
    crew_type="TEAM",
    presence="CONNECTED",
    relay_connection_status="CONNECTED",
)
TEAM_B = dataclasses.replace(TEAM_A, endpoint_id=B, name="Docs box")
PERSONAL_C = dataclasses.replace(TEAM_A, endpoint_id=C, name="My laptop", crew_type="PERSONAL")


class _State:
    def __init__(self) -> None:
        self.notes: list[dict] = []

    def notify(self, kind, title, body, **kw):
        self.notes.append({"kind": kind, "title": title, "body": body, **kw})


@pytest.fixture
def viewer(monkeypatch, tmp_path):
    """The viewer on, CPS faked to answer ``ctl.crews`` or raise ``ctl.refuse``."""
    ctl = SimpleNamespace(enabled=True, crews=(), refuse=None, home=tmp_path)
    tunnel = SimpleNamespace(
        relay_viewer_cps_url="https://cps.example.test",
        relay_viewer_credential="",
        relay_viewer_relay_domain="relay.example.test",
    )

    def _tunnel():
        tunnel.relay_viewer_enabled = ctl.enabled
        return tunnel

    async def _bearer(ref, rejected=""):
        return "bearer", "", ""

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        if ctl.refuse is not None:
            raise ctl.refuse
        return cps.CrewList(ctl.crews)

    async def _owner(request, operation):
        return None

    monkeypatch.setattr(rv, "_load_tunnel", _tunnel)
    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    monkeypatch.setattr(rv, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(crew_notices, "config_dir", lambda: tmp_path)
    return ctl


def _client(state: _State) -> TestClient:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/relay-viewer/crews", rv.api_relay_viewer_crews)
    app.router.add_post("/api/relay-viewer/crews/seen", rv.api_relay_viewer_crews_seen)
    return TestClient(TestServer(app))


async def _read(client) -> tuple[int, dict]:
    resp = await client.get("/api/relay-viewer/crews")
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_new_crew_rings_once_and_repeat_reads_do_not(viewer) -> None:
    state = _State()
    async with _client(state) as client:
        await _read(client)  # first read: nothing granted yet
        viewer.crews = (TEAM_A,)
        status, body = await _read(client)
        for _ in range(3):
            await _read(client)
    assert status == 200
    assert len(state.notes) == 1
    note = state.notes[0]
    assert note["title"] == "You've been added to Build box"
    assert note["body"] == "Open it from Remote Crews in the Crew switcher."
    assert note["channel"] == crew_notices.CHANNEL
    assert note["actions"] == [{"id": "open", "label": "Open", "url": f"/chat?relay_crew={A}"}]
    assert note["i18n"] == {"id": "relay_crew_added", "params": {"name": "Build box"}}
    assert body["crews"][0]["unseen"] is True


@pytest.mark.asyncio
async def test_first_read_with_several_crews_rings_one_summary(viewer) -> None:
    viewer.crews = (TEAM_A, TEAM_B, PERSONAL_C)
    state = _State()
    async with _client(state) as client:
        _status, body = await _read(client)
        await _read(client)
    assert len(state.notes) == 1
    assert state.notes[0]["title"] == "You have access to 2 Shared Crews"
    assert state.notes[0]["body"] == "Open them from Remote Crews in the Crew switcher."
    assert state.notes[0]["i18n"] == {"id": "relay_crews_available", "params": {"count": 2}}
    assert "actions" not in state.notes[0] or state.notes[0]["actions"] is None
    # A personal Crew is the person's own machine: never announced, never dotted.
    assert {c["endpointId"]: c["unseen"] for c in body["crews"]} == {A: True, B: True, C: False}


@pytest.mark.asyncio
async def test_a_failed_read_or_the_viewer_off_never_rings(viewer, monkeypatch) -> None:
    viewer.crews = (TEAM_A,)
    viewer.refuse = cps.CpsRefused(cps.SIGNED_OUT, "signed out")
    state = _State()
    async with _client(state) as client:
        status, _ = await _read(client)
        assert status == 409
        viewer.refuse = None
        monkeypatch.setattr(rv, "local_enable_locked", lambda control: True)
        status, _ = await _read(client)
        assert status == 404
    assert state.notes == []
    assert not (viewer.home / crew_notices.STATE_FILENAME).exists()


@pytest.mark.asyncio
async def test_seen_clears_the_dot_and_never_rings_again(viewer) -> None:
    viewer.crews = (TEAM_A,)
    state = _State()
    async with _client(state) as client:
        await _read(client)
        resp = await client.post("/api/relay-viewer/crews/seen", json={"endpointIds": [A, B]})
        assert resp.status == 200
        _status, body = await _read(client)
        bad = await client.post("/api/relay-viewer/crews/seen", json={"endpointIds": ["../x"]})
        assert bad.status == 400
    assert body["crews"][0]["unseen"] is False
    assert len(state.notes) == 1


@pytest.mark.asyncio
async def test_a_crew_that_leaves_and_returns_is_not_announced_again(viewer) -> None:
    viewer.crews = (TEAM_A,)
    state = _State()
    async with _client(state) as client:
        await _read(client)
        viewer.crews = ()
        await _read(client)
        viewer.crews = (TEAM_A,)
        await _read(client)
    assert len(state.notes) == 1


@pytest.mark.asyncio
async def test_an_unreadable_file_is_rebuilt_without_ringing(viewer) -> None:
    (viewer.home / crew_notices.STATE_FILENAME).write_text("{not json", encoding="utf-8")
    viewer.crews = (TEAM_A, TEAM_B)
    state = _State()
    async with _client(state) as client:
        _status, body = await _read(client)
    assert state.notes == []
    assert all(c["unseen"] is False for c in body["crews"])
    stored = json.loads((viewer.home / crew_notices.STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(stored["crews"]) == {A, B}


@pytest.mark.asyncio
async def test_a_failed_write_sends_nothing(viewer, monkeypatch) -> None:
    def _refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(crew_notices, "atomic_write", _refuse)
    viewer.crews = (TEAM_A,)
    state = _State()
    async with _client(state) as client:
        status, body = await _read(client)
    assert status == 200
    assert state.notes == []
    assert body["crews"][0]["unseen"] is False


@pytest.mark.asyncio
async def test_a_seen_that_cannot_be_saved_says_so_and_keeps_the_dot(viewer, monkeypatch) -> None:
    viewer.crews = (TEAM_A,)
    state = _State()
    real_write = crew_notices.atomic_write
    failing = SimpleNamespace(on=False)

    def _write(*a, **k):
        if failing.on:
            raise OSError("disk full")
        return real_write(*a, **k)

    monkeypatch.setattr(crew_notices, "atomic_write", _write)
    async with _client(state) as client:
        await _read(client)
        failing.on = True
        resp = await client.post("/api/relay-viewer/crews/seen", json={"endpointIds": [A]})
        assert resp.status == 503
        failing.on = False
        _status, body = await _read(client)
    assert body["crews"][0]["unseen"] is True


@pytest.mark.asyncio
async def test_a_failed_write_for_a_new_crew_keeps_the_earlier_dots(viewer, monkeypatch) -> None:
    real_write = crew_notices.atomic_write
    failing = SimpleNamespace(on=False)

    def _write(*a, **k):
        if failing.on:
            raise OSError("disk full")
        return real_write(*a, **k)

    monkeypatch.setattr(crew_notices, "atomic_write", _write)
    viewer.crews = (TEAM_A,)
    state = _State()
    async with _client(state) as client:
        await _read(client)
        failing.on = True
        viewer.crews = (TEAM_A, TEAM_B)
        _status, body = await _read(client)
    assert len(state.notes) == 1
    assert {c["endpointId"]: c["unseen"] for c in body["crews"]} == {A: True, B: False}


@pytest.mark.asyncio
async def test_a_seen_on_an_unreadable_file_is_not_reported_saved(viewer) -> None:
    viewer.crews = (TEAM_A,)
    state = _State()
    async with _client(state) as client:
        await _read(client)
        (viewer.home / crew_notices.STATE_FILENAME).write_text("{not json", encoding="utf-8")
        resp = await client.post("/api/relay-viewer/crews/seen", json={"endpointIds": [A]})
    assert resp.status == 503
