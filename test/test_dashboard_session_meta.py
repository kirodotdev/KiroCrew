"""``GET /api/sessions/{key}/meta``: the per-key probe a chat link resolves through.

Pinned here:

* a session on disk answers its list row, found by its transcript stem or by the
  slot key a chat link carries;
* a key with no transcript is a 404, never an empty 200 (``GET
  /api/sessions/{key}`` answers ``[]`` for both, which is why a link could not
  use it to tell them apart);
* the probe reads neither the transcript body nor the directory;
* an app caller sees only a transcript it owns, with the same uniform 404 as a
  missing key -- the probe grants nothing ``GET /api/sessions`` does not.

Real auth middleware and real route table, as in ``test_app_route_trust_gate``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all
from kiro_crew.history import HistoryLockTimeout

_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}
_APP = "meta-app"
_OTHER = "meta-other"


def _manifest(name: str) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "session meta fixture",
            "permissions": {"api": ["/api/sessions", "/api/sessions/*"]},
        }
    )


_MANIFESTS = {_APP: _manifest(_APP), _OTHER: _manifest(_OTHER)}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"session-meta-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", _MANIFESTS.__contains__)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.crons = None
    return st


@contextlib.asynccontextmanager
async def _serve(state):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _q(app_name: str = "") -> dict[str, str]:
    return {"token": token_auth.generate_token("local-app", app=app_name)}


def _transcript(state, key: str, title: str, app_name: str = "") -> None:
    log = state.conversation_log
    log.append(key, "user", "hello")
    log.append(key, "assistant", "noted")
    meta = {"title": title}
    if app_name:
        meta["app"] = app_name
    log.update_metadata(key, meta)


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["chat-7-1784661951", "dashboard_chat-7-1784661951"])
async def test_closed_session_answers_its_row_by_slot_key_or_stem(state, asked):
    _transcript(state, "dashboard:chat-7-1784661951", "Earlier work")
    async with _serve(state) as client:
        resp = await client.get(f"/api/sessions/{asked}/meta", params=_q())
        assert resp.status == 200
        body = await resp.json()
    assert body == {"key": "dashboard_chat-7-1784661951", "title": "Earlier work"}


@pytest.mark.asyncio
async def test_missing_key_is_404_not_an_empty_answer(state):
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-9-1700000000/meta", params=_q())
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_probe_reads_no_transcript_body_and_scans_no_directory(state, monkeypatch):
    _transcript(state, "dashboard:chat-7-1784661951", "Earlier work")
    log = state.conversation_log
    read = MagicMock(side_effect=AssertionError("transcript body read"))
    monkeypatch.setattr(log, "read_messages", read)
    real_list = log.list_sessions
    calls: list[object] = []

    def _list(*, keys=None):
        calls.append(keys)
        assert keys is not None, "full directory scan"
        return real_list(keys=keys)

    monkeypatch.setattr(log, "list_sessions", _list)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-7-1784661951/meta", params=_q())
        assert resp.status == 200
    read.assert_not_called()
    assert calls == [("dashboard:chat-7-1784661951",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", _OTHER])
async def test_app_cannot_probe_a_transcript_it_does_not_own(state, owner):
    _transcript(state, "dashboard:chat-7-1784661951", "Not yours", owner)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-7-1784661951/meta", params=_q(_APP))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_app_probes_its_own_transcript(state):
    _transcript(state, "dashboard:chat-7-1784661951", "Mine", _APP)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-7-1784661951/meta", params=_q(_APP))
        assert resp.status == 200
        assert (await resp.json())["title"] == "Mine"


@pytest.mark.asyncio
async def test_app_ownership_is_rechecked_under_the_transcript_lock(state, monkeypatch):
    """A recreate by another owner between the first check and the read is refused."""
    key = "dashboard:chat-7-1784661951"
    _transcript(state, key, "Mine", _APP)
    log = state.conversation_log
    real_locked = log._locked
    taken_over: list[bool] = []

    @contextlib.contextmanager
    def _locked_after_takeover(k):
        # The transcript changes hands just as the probe takes the lock. The
        # takeover's own write takes the lock too, so it runs on the real one.
        if not taken_over:
            taken_over.append(True)
            monkeypatch.setattr(log, "_locked", real_locked)
            log.update_metadata(key, {"app": _OTHER, "title": "Not yours"})
        with real_locked(k):
            yield

    monkeypatch.setattr(log, "_locked", _locked_after_takeover)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-7-1784661951/meta", params=_q(_APP))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_app_probe_answers_busy_on_a_lock_timeout(state, monkeypatch):
    _transcript(state, "dashboard:chat-7-1784661951", "Mine", _APP)
    monkeypatch.setattr(
        state.conversation_log, "_locked", MagicMock(side_effect=HistoryLockTimeout("busy"))
    )
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/chat-7-1784661951/meta", params=_q(_APP))
        assert resp.status == 503
        assert (await resp.json())["code"] == "session_busy"
