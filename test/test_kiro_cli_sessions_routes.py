"""The History pane's Kiro CLI routes, served through the real route table and auth middleware.

``GET /api/kiro-cli/sessions``, ``GET /api/kiro-cli/sessions/{sid}`` and
``POST /api/kiro-cli/sessions/{sid}/continue``: owner-only (an app token and a
non-owner dashboard session are refused), a malformed id never reaches the
filesystem, a session Crew owns is not found, and Continue copies the terminal's
pair into a NEW chat under a fresh sid -- leaving the original byte-identical,
setting the chat's project to the session's folder when it can, and making a
second independent chat when the same session is continued again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import kiro_cli_sessions as kcs
from kiro_crew.config import paths
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all

OWNER = "U0OWNER"
SID = "11111111-2222-4333-8444-555555555555"
OWNED_SID = "99999999-2222-4333-8444-555555555555"


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "kiro" / "sessions" / "cli"
    d.mkdir(parents=True)
    monkeypatch.setattr(paths, "_sessions_dir_override", lambda: d)
    kcs._META_CACHE.clear()
    return d


@pytest.fixture
def project(tmp_path) -> Path:
    p = tmp_path / "terminal-project"
    p.mkdir()
    return p


@pytest.fixture
def state(tmp_path, monkeypatch, store):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"kiro-cli-routes-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_import_tmp_dir", lambda: tmp_path / "imp")
    monkeypatch.setattr(st, "_egress_tmp_dir", lambda: tmp_path / "egr")
    (tmp_path / "imp").mkdir()
    (tmp_path / "egr").mkdir()
    s = _make_state(tmp_path)
    s.broadcast_ws = MagicMock()
    s.push_slots_update = MagicMock()
    s.owner_id = OWNER
    s.crons = None
    s.sessions._session_map = SimpleNamespace(
        mapped_sids_by_key=lambda: {"dashboard:chat-1": OWNED_SID}
    )
    return s


@contextlib.asynccontextmanager
async def _serve(state):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _owner() -> dict[str, str]:
    return {"token": token_auth.generate_token(OWNER)}


def _event(kind, text):
    return json.dumps(
        {"version": "v1", "kind": kind, "data": {"content": [{"kind": "text", "data": text}]}}
    )


def _write(store: Path, sid: str, cwd: str, title="debug the build") -> None:
    envelope = {
        "session_id": sid,
        "cwd": cwd,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "title": title,
        "session_created_reason": "subagent",
        "session_state": {
            "version": "v1",
            "conversation_metadata": {"user_turn_metadatas": []},
            "permissions": {"filesystem": {"allowed_read_paths": [cwd]}},
            "agent_name": "fable",
        },
    }
    (store / f"{sid}.json").write_text(json.dumps(envelope))
    (store / f"{sid}.jsonl").write_text(
        _event("Prompt", "why does the build fail?")
        + "\n"
        + _event("AssistantMessage", "A missing dependency.")
        + "\n"
    )


def _digest(store: Path, sid: str) -> dict[str, str]:
    return {
        suffix: hashlib.sha256((store / f"{sid}{suffix}").read_bytes()).hexdigest()
        for suffix in (".json", ".jsonl")
    }


ROUTES = [
    ("get", "/api/kiro-cli/sessions"),
    ("get", f"/api/kiro-cli/sessions/{SID}"),
    ("post", f"/api/kiro-cli/sessions/{SID}/continue"),
]


# ── owner only ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: token_auth.generate_token("U0NONOWNER"), id="non-owner-dashboard"),
        pytest.param(lambda: token_auth.generate_token("local-app", app="some-app"), id="app"),
    ],
)
async def test_every_route_refuses_anyone_but_the_owner(state, store, project, method, path, token):
    _write(store, SID, str(project))
    async with _serve(state) as client:
        resp = await getattr(client, method)(path, params={"token": token()})
        text = await resp.text()
    assert resp.status in (401, 403), text
    assert "debug the build" not in text
    assert not any(p.name not in (f"{SID}.json", f"{SID}.jsonl") for p in store.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_needs_a_token(state, store, project, method, path):
    _write(store, SID, str(project))
    async with _serve(state) as client:
        resp = await getattr(client, method)(path)
    assert resp.status in (401, 403)


# ── the list and the preview ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owner_lists_terminal_sessions_but_not_crews(
    state, store, project, tmp_path, monkeypatch
):
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
    _write(store, SID, str(project))
    _write(store, OWNED_SID, str(project), title="a crew chat")
    background_sid = "33333333-2222-4333-8444-555555555555"
    _write(store, background_sid, str(tmp_path / "ws" / "_bg"), title="background run")
    async with _serve(state) as client:
        resp = await client.get("/api/kiro-cli/sessions", params=_owner())
        body = await resp.json()
    assert resp.status == 200, body
    assert [r["id"] for r in body["sessions"]] == [SID]
    assert body["sessions"][0]["cwd"] == str(project)
    assert (body["total"], body["has_more"]) == (1, False)


@pytest.mark.asyncio
async def test_the_owner_reads_a_preview(state, store, project):
    _write(store, SID, str(project))
    async with _serve(state) as client:
        resp = await client.get(f"/api/kiro-cli/sessions/{SID}", params={**_owner(), "limit": "1"})
        body = await resp.json()
    assert resp.status == 200, body
    assert [m["content"] for m in body["messages"]] == ["A missing dependency."]
    assert (body["total"], body["start"], body["title"]) == (2, 1, "debug the build")


@pytest.mark.asyncio
async def test_a_preview_redacts_assistant_text(state, store, project):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    _write(store, SID, str(project))
    with open(store / f"{SID}.jsonl", "a") as f:
        f.write(_event("AssistantMessage", f"use {key} here") + "\n")
    async with _serve(state) as client:
        resp = await client.get(f"/api/kiro-cli/sessions/{SID}", params=_owner())
        text = await resp.text()
    assert resp.status == 200
    assert key not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [OWNED_SID, "22222222-2222-4333-8444-555555555555"])
async def test_a_crew_owned_or_missing_session_is_not_found(state, store, project, sid):
    _write(store, OWNED_SID, str(project))
    async with _serve(state) as client:
        read = await client.get(f"/api/kiro-cli/sessions/{sid}", params=_owner())
        cont = await client.post(f"/api/kiro-cli/sessions/{sid}/continue", params=_owner())
        assert (read.status, cont.status) == (404, 404)
        assert (await cont.json())["code"] == "kiro_cli_session_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    ["..%2F..%2Fetc%2Fpasswd", "%2E%2E", SID.replace("-", ""), "not-a-uuid", SID + ".json"],
)
async def test_a_malformed_id_never_reaches_the_filesystem(state, store, project, raw, monkeypatch):
    _write(store, SID, str(project))
    touched: list[object] = []
    real = kcs.session_paths
    monkeypatch.setattr(kcs, "session_paths", lambda s: touched.append(s) or real(s))
    async with _serve(state) as client:
        read = await client.get(f"/api/kiro-cli/sessions/{raw}", params=_owner())
        cont = await client.post(f"/api/kiro-cli/sessions/{raw}/continue", params=_owner())
    assert read.status == 404 and cont.status == 404
    assert touched == []


# ── Continue in Crew ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_continue_copies_the_session_into_a_new_chat(state, store, project):
    _write(store, SID, str(project))
    original = _digest(store, SID)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 200, body
    assert body["resume_mode"] == "session_load"
    assert body["project"] == str(project.resolve())
    assert "project_skipped" not in body
    slot = state._slots[body["key"]]
    assert slot.project == str(project.resolve())
    assert "debug the build" in slot.title and "kiro-cli" in slot.title
    # The original pair is untouched, and the copy lives under a fresh sid.
    assert _digest(store, SID) == original
    joined = state.sessions.seed_conversation.call_args
    new_sid = joined.args[1]
    assert new_sid != SID and kcs.valid_session_id(new_sid)
    assert (store / f"{new_sid}.jsonl").read_bytes() == (store / f"{SID}.jsonl").read_bytes()
    copied = json.loads((store / f"{new_sid}.json").read_text())
    assert copied["session_id"] == new_sid
    assert copied["session_state"]["conversation_metadata"] == {"user_turn_metadatas": []}


@pytest.mark.asyncio
async def test_a_copy_stays_off_the_list_after_its_join_is_dropped(state, store, project):
    _write(store, SID, str(project))
    joined: dict[str, str] = {}
    state.sessions.seed_conversation.side_effect = lambda key, sid, **_k: joined.update({key: sid})
    state.sessions.resumable_sid.side_effect = joined.get
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        assert resp.status == 200, await resp.text()
        (copy_sid,) = joined.values()
        # kiro-cli titles the copy from its first prompt, the terminal one, and
        # gives it the folder it was loaded in: it looks like a terminal session.
        _write(store, copy_sid, str(project))
        # A reset or failed start drops the join; the record still names the copy.
        joined.clear()
        listed = await (await client.get("/api/kiro-cli/sessions", params=_owner())).json()
    assert [r["id"] for r in listed["sessions"]] == [SID]
    assert copy_sid in kcs.continued_sids()


@pytest.mark.asyncio
async def test_continuing_twice_makes_two_independent_chats(state, store, project):
    _write(store, SID, str(project))
    async with _serve(state) as client:
        first = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
        second = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
    assert first["key"] != second["key"]
    sids = [c.args[1] for c in state.sessions.seed_conversation.call_args_list]
    assert len(set(sids)) == 2 and SID not in sids
    for sid in sids:
        assert (store / f"{sid}.json").exists()


@pytest.mark.asyncio
async def test_continue_without_a_usable_folder_opens_without_a_project(state, store, tmp_path):
    _write(store, SID, str(tmp_path / "deleted-since"))
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 200, body
    assert body["project"] == ""
    assert body["project_skipped"] == "the session's folder no longer exists"
    assert state._slots[body["key"]].project == ""


@pytest.mark.asyncio
async def test_continue_refuses_a_session_that_cannot_resume(state, store, project):
    _write(store, SID, str(project))
    (store / f"{SID}.jsonl").write_text("{corrupt\n" + _event("Prompt", "x") + "\n")
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 422
    assert body["code"] == "kiro_cli_session_unreadable"
    assert state._slots == {} or all("debug" not in s.title for s in state._slots.values())


@pytest.mark.asyncio
async def test_continue_answers_busy_for_a_session_still_being_written(
    state, store, project, monkeypatch
):
    _write(store, SID, str(project))

    def busy(_sid):
        raise kcs.SnapshotRefused("kiro_cli_session_busy", "still being written")

    monkeypatch.setattr(kcs, "snapshot_session", busy)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 409
    assert body["code"] == "kiro_cli_session_busy"
