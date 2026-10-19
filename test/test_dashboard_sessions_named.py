"""``GET /api/sessions?name=chat-N``: every session one short name names.

A ``/chat?sid=chat-7`` link resolves through this filter. Pinned here:

* it keeps only dashboard transcripts minted under that exact name, oldest
  first by the mint time in the key, so ``chat-7`` never claims ``chat-70``;
* a value that is not a short name matches nothing rather than everything;
* an app caller still sees only the transcripts it owns.

Real auth middleware and real route table, as in ``test_dashboard_session_meta``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all

_APP = "named-app"


def _manifest(name: str) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "sessions named fixture",
            "permissions": {"api": ["/api/sessions", "/api/sessions/*"]},
        }
    )


_MANIFESTS = {_APP: _manifest(_APP)}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"sessions-named-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", _MANIFESTS.__contains__)
    from kiro_crew.apps.manager import InstalledApp

    monkeypatch.setattr(
        "kiro_crew.apps.permissions._read_installed",
        lambda name, **_kw: InstalledApp(name=name) if name in _MANIFESTS else None,
    )
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.crons = None
    return st


def _transcript(state, key: str, title: str, app_name: str = "") -> None:
    log = state.conversation_log
    log.append(key, "user", "hello")
    meta = {"title": title}
    if app_name:
        meta["app"] = app_name
    log.update_metadata(key, meta)


async def _seed(state, *transcripts: tuple) -> None:
    """Seed transcripts off the event loop.

    ``_transcript`` ends in ``ConversationLog.update_metadata``, a synchronous
    tmp-file-plus-rename whose Windows sharing-violation retry
    (``atomic_write.replace_with_retry``) is a single attempt on the loop
    thread. Seeding from an ``async def`` test on the loop therefore loses to
    any handle a scanner holds on ``sessions/<key>.jsonl`` for a few
    milliseconds (``[WinError 5]``). Running the writes on a worker thread,
    which has no loop of its own, keeps the retry.
    """

    def _write() -> None:
        for args in transcripts:
            _transcript(state, *args)

    await asyncio.to_thread(_write)


async def _named(state, name: str, app_name: str = "") -> list[str]:
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    token = token_auth.generate_token("local-app", app=app_name)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/sessions", params={"token": token, "name": name})
        assert resp.status == 200
        body = await resp.json()
    assert body["total"] == len(body["sessions"])
    return [row["key"] for row in body["sessions"]]


@pytest.mark.asyncio
async def test_name_keeps_its_own_sessions_oldest_first(state):
    await _seed(
        state,
        ("dashboard:chat-7-1790000000", "Newer"),
        ("dashboard:chat-7-1780000000", "Older"),
        ("dashboard:chat-70-1785000000", "Other number"),
        ("dashboard:chat-8-1785000000", "Other slot"),
        ("subagent:chat-7-1785000000", "Not a dashboard slot"),
    )
    assert await _named(state, "chat-7") == [
        "dashboard_chat-7-1780000000",
        "dashboard_chat-7-1790000000",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "chat-7-1780000000", "chat-", "chat-7*", "dashboard_chat-7"])
async def test_a_value_that_is_not_a_short_name_matches_nothing(state, name):
    await _seed(state, ("dashboard:chat-7-1780000000", "Older"))
    assert await _named(state, name) == []


@pytest.mark.asyncio
async def test_app_sees_only_the_named_sessions_it_owns(state):
    await _seed(
        state,
        ("dashboard:chat-7-1780000000", "Not yours"),
        ("dashboard:chat-7-1790000000", "Mine", _APP),
    )
    assert await _named(state, "chat-7", _APP) == ["dashboard_chat-7-1790000000"]
