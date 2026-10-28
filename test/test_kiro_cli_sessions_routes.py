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

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import kiro_cli_sessions as kcs
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.tool_search import resume_takes_tool_search_replay
from kiro_crew.config import paths
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.chat_utils import effective_session_key
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
    # Chats run on the kiro-cli backend unless a test picks another one.
    _use_backend(monkeypatch, ACP_BACKEND_KIRO)
    return s


def _use_backend(
    monkeypatch, backend: str, *, tool_search: bool = False, offers_kas: bool = False
) -> None:
    from kiro_crew.dashboard import kiro_cli_continue as kcc

    config = SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, tool_search=tool_search))
    # Whether this host's kiro-cli serves its v3 engine (the gateway's probe).
    monkeypatch.setattr(kcc, "kiro_cli_offers_kas", lambda: offers_kas)
    monkeypatch.setattr(kcc.KiroCrewConfig, "load", staticmethod(lambda *a, **k: config))


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
async def test_the_list_pages_after_the_last_row_shown(state, store, project):
    other = "22222222-2222-4333-8444-555555555555"
    _write(store, SID, str(project), title="older")
    _write(store, other, str(project), title="newer")
    envelope = json.loads((store / f"{other}.json").read_text())
    envelope["updated_at"] = "2026-10-03T10:00:00Z"
    (store / f"{other}.json").write_text(json.dumps(envelope))
    async with _serve(state) as client:
        first = await (
            await client.get("/api/kiro-cli/sessions", params={**_owner(), "limit": "1"})
        ).json()
        last = first["sessions"][-1]
        cursor = {"after_updated": last["updated_at"], "after_id": last["id"]}
        second = await (
            await client.get("/api/kiro-cli/sessions", params={**_owner(), "limit": "1", **cursor})
        ).json()
        # A cursor that is not a session id is ignored, not compared.
        bogus = {"after_updated": "9999", "after_id": "../x"}
        ignored = await (
            await client.get("/api/kiro-cli/sessions", params={**_owner(), "limit": "1", **bogus})
        ).json()
    assert [r["title"] for r in first["sessions"]] == ["newer"] and first["has_more"] is True
    assert [r["title"] for r in second["sessions"]] == ["older"] and second["has_more"] is False
    assert [r["title"] for r in ignored["sessions"]] == ["newer"]


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
async def test_a_credential_in_a_title_is_redacted_in_the_list_and_preview(state, store, project):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    _write(store, SID, str(project), title=f"deploy with {key} please")
    async with _serve(state) as client:
        listed = await client.get("/api/kiro-cli/sessions", params=_owner())
        listed_text = await listed.text()
        read = await client.get(f"/api/kiro-cli/sessions/{SID}", params=_owner())
        read_text = await read.text()
    assert (listed.status, read.status) == (200, 200)
    assert key not in listed_text and key not in read_text
    assert "deploy with" in json.loads(listed_text)["sessions"][0]["title"]
    # The terminal's own file keeps what the person typed.
    assert key in (store / f"{SID}.json").read_text()


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


async def _folders(state) -> list[dict]:
    return await state.read_folders(lambda folders: [dict(f) for f in folders])


@pytest.mark.asyncio
async def test_continue_files_the_chat_in_a_top_level_from_kiro_cli_folder(state, store, project):
    # Not "Imported" / "from kiro-cli": the session was copied from this
    # machine's own kiro-cli store, it did not arrive from somewhere else. The
    # same folder holds every copy.
    other = "11111111-2222-4333-8444-555555555555"
    _write(store, SID, str(project))
    _write(store, other, str(project), title="another session")
    async with _serve(state) as client:
        first = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
        second = await (
            await client.post(f"/api/kiro-cli/sessions/{other}/continue", params=_owner())
        ).json()
    folders = await _folders(state)
    assert [(f["name"], f["parent_id"]) for f in folders] == [("from kiro-cli", "")]
    assert first["folder_id"] == second["folder_id"] == folders[0]["id"]
    assert state._slots[first["key"]].folder_id == folders[0]["id"]
    assert state._slots[first["key"]].title.endswith("(from kiro-cli)")


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
    assert body["project_skipped"] == "Project directory must be an existing directory"
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

    def busy(_sid, _max_bytes=None):
        raise kcs.SnapshotRefused("kiro_cli_session_busy", "still being written")

    monkeypatch.setattr(kcs, "snapshot_session", busy)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 409
    assert body["code"] == "kiro_cli_session_busy"


@pytest.mark.parametrize(
    ("text", "raw", "factor"),
    [
        # ASCII parses at the floor factor 3; the snapshot admits 2 * 3 + 1.
        ("A missing dependency.", False, 7),
        # An escaped non-BMP character builds four-byte strings: 1 + 2 * 4 = 9.
        ("A missing dependency \U0001f9e9.", False, 19),
        # Written raw, as kiro-cli writes it, it widens the decoded text too: 12.
        ("A missing dependency \U0001f9e9.", True, 25),
    ],
)
@pytest.mark.asyncio
async def test_continue_reserves_memory_for_the_pair_as_measured(
    state, store, project, monkeypatch, text, raw, factor
):
    from kiro_crew.dashboard import kiro_cli_continue as kcc

    _write(store, SID, str(project))
    log = store / f"{SID}.jsonl"
    line = _event("AssistantMessage", text)
    line = json.dumps(json.loads(line), ensure_ascii=False) if raw else line
    log.write_text(log.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    size = sum((store / f"{SID}{sfx}").stat().st_size for sfx in (".json", ".jsonl"))
    seen: list[tuple[int, int, int, int]] = []
    sizes: list[int | None] = []
    real_admission = kcc._memory_admission
    real_snapshot = kcs.snapshot_session

    def admission(doc_bytes, messages=0, values=0, factor=3):
        seen.append((doc_bytes, messages, values, factor))
        return real_admission(doc_bytes, messages, values, factor)

    def snapshot(sid, max_bytes=None):
        sizes.append(max_bytes)
        return real_snapshot(sid, max_bytes)

    monkeypatch.setattr(kcc, "_memory_admission", admission)
    monkeypatch.setattr(kcs, "snapshot_session", snapshot)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        assert resp.status == 200, await resp.text()
    ((doc_bytes, records, values, admitted),) = seen
    assert doc_bytes == size
    assert records >= 3 and values > records
    assert admitted == factor
    # The snapshot reads no more than the size the reservation was made for.
    assert sizes == [size]


@pytest.mark.asyncio
async def test_continue_refuses_a_session_this_machine_cannot_hold(
    state, store, project, monkeypatch
):
    from kiro_crew.dashboard import session_transfer_intake as intake

    _write(store, SID, str(project))
    original = _digest(store, SID)
    # A host whose memory, past the fixed headroom, is smaller than the pair's
    # measured cost: waiting cannot help, so it is refused, not attempted.
    budget = intake._MEMORY_HEADROOM_BYTES + 64
    monkeypatch.setattr(intake, "_gateway_memory", lambda: (budget, budget))
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 413
    assert body["code"] == "kiro_cli_session_too_large"
    assert state._slots == {}
    assert _digest(store, SID) == original


@pytest.mark.asyncio
async def test_continue_answers_at_once_for_a_planted_event_log(state, store, project):
    # The agent's shell can write kiro-cli's sessions dir. A log that is a FIFO
    # (or, on a host without FIFOs, a directory) must be refused, not opened:
    # opening a FIFO waits for a writer forever and would pin a worker thread.
    # The ceiling is a lost-run bound (a regression hangs), not a speed bet.
    _write(store, SID, str(project))
    planted = store / f"{SID}.jsonl"
    planted.unlink()
    fifo = hasattr(os, "mkfifo")
    if fifo:
        os.mkfifo(planted)
    else:
        planted.mkdir()
    try:
        async with _serve(state) as client:
            resp = await asyncio.wait_for(
                client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner()), 60
            )
            body = await resp.json()
    finally:
        if fifo:
            try:
                os.close(os.open(planted, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
    assert resp.status == 422, body
    assert body["code"] == "kiro_cli_session_no_transcript"
    assert state._slots == {}


@pytest.mark.asyncio
async def test_a_folder_with_a_nul_neither_breaks_the_list_nor_reaches_continue(
    state, store, project
):
    # kiro-cli's sessions dir is writable by the agent's shell. One envelope
    # whose cwd holds a NUL must not take the whole list down with it.
    bad = "22222222-2222-4333-8444-555555555555"
    _write(store, SID, str(project))
    _write(store, bad, str(project) + "\x00x")
    async with _serve(state) as client:
        listed = await client.get("/api/kiro-cli/sessions", params=_owner())
        body = await listed.json()
        preview = await client.get(f"/api/kiro-cli/sessions/{bad}", params=_owner())
        cont = await client.post(f"/api/kiro-cli/sessions/{bad}/continue", params=_owner())
    assert listed.status == 200, body
    assert [r["id"] for r in body["sessions"]] == [SID]
    assert (preview.status, cont.status) == (404, 404)
    assert state._slots == {}


@pytest.mark.asyncio
async def test_continue_on_another_backend_copies_the_transcript_and_says_so(
    state, store, project, monkeypatch
):
    # KAS (the default) labels its turns with its own provider, so a native
    # join would be dropped on the first turn and the chat would quietly replay
    # the transcript while Continue claimed full context.
    _use_backend(monkeypatch, ACP_BACKEND_KAS)
    _write(store, SID, str(project))
    original = _digest(store, SID)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 200, body
    assert body["resume_mode"] == "transcript"
    assert body["context_skipped"] == "kiro_cli_backend_required"
    # No native join, no copied pair, and the configured value is never echoed.
    state.sessions.seed_conversation.assert_not_called()
    assert ACP_BACKEND_KAS not in json.dumps(body)
    assert "transcript only" in state._slots[body["key"]].title
    assert sorted(p.name for p in store.iterdir()) == [f"{SID}.json", f"{SID}.jsonl"]
    assert _digest(store, SID) == original


@pytest.mark.asyncio
async def test_continue_on_kiro_cli_keeps_the_native_join(state, store, project):
    # The fixture runs kiro-cli with MCP Tool Search off: the one configuration
    # in which the chat's first turn loads the copy.
    _write(store, SID, str(project))
    async with _serve(state) as client:
        body = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
    assert body["resume_mode"] == "session_load"
    assert "context_skipped" not in body
    state.sessions.seed_conversation.assert_called_once()
    # The provider's own resume rule, asked for this very chat, keeps the load:
    # a "session_load" answer is never one the first turn turns into a replay.
    assert not resume_takes_tool_search_replay(
        tool_search=False,
        backend=ACP_BACKEND_KIRO,
        channel_id=None,
        session_key=effective_session_key(state._slots[body["key"]]),
    )


@pytest.mark.asyncio
async def test_continue_with_tool_search_on_copies_the_transcript_and_says_so(
    state, store, project, monkeypatch
):
    # MCP Tool Search (on by default) makes every resumed dashboard chat on
    # kiro-cli start a fresh session and replay its transcript, because a loaded
    # session comes back without Tool Search's activated tool schemas. A native
    # join would never be loaded, so Continue must not claim one.
    _use_backend(monkeypatch, ACP_BACKEND_KIRO, tool_search=True)
    _write(store, SID, str(project))
    original = _digest(store, SID)
    async with _serve(state) as client:
        resp = await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        body = await resp.json()
    assert resp.status == 200, body
    assert body["resume_mode"] == "transcript"
    assert body["context_skipped"] == "tool_search_replays_transcript"
    # The same rule the provider applies on the chat's first turn.
    assert resume_takes_tool_search_replay(
        tool_search=True,
        backend=ACP_BACKEND_KIRO,
        channel_id=None,
        session_key=effective_session_key(state._slots[body["key"]]),
    )
    state.sessions.seed_conversation.assert_not_called()
    assert "transcript only" in state._slots[body["key"]].title
    assert sorted(p.name for p in store.iterdir()) == [f"{SID}.json", f"{SID}.jsonl"]
    assert _digest(store, SID) == original


@pytest.mark.asyncio
async def test_continue_where_kiro_cli_runs_chats_on_kas_says_no_setting_helps(
    state, store, project, monkeypatch
):
    # This host's kiro-cli serves its v3 engine, so a stored Kiro CLI choice
    # itself loads as KAS (the a-kiro-cli-choice-runs-on-kas entry in docs/decisions).
    # Telling the person to pick Kiro CLI would send them to a setting that
    # changes nothing, so the code is a distinct one.
    _use_backend(monkeypatch, ACP_BACKEND_KAS, offers_kas=True)
    _write(store, SID, str(project))
    async with _serve(state) as client:
        preview = await (await client.get(f"/api/kiro-cli/sessions/{SID}", params=_owner())).json()
        body = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
    assert preview["context_skipped"] == "kiro_cli_state_unavailable"
    assert body["resume_mode"] == "transcript"
    assert body["context_skipped"] == "kiro_cli_state_unavailable"
    state.sessions.seed_conversation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend, tool_search, code",
    [
        (ACP_BACKEND_KIRO, False, None),
        (ACP_BACKEND_KIRO, True, "tool_search_replays_transcript"),
        (ACP_BACKEND_KAS, False, "kiro_cli_backend_required"),
    ],
)
async def test_the_preview_says_what_continue_would_keep(
    state, store, project, monkeypatch, backend, tool_search, code
):
    _use_backend(monkeypatch, backend, tool_search=tool_search)
    _write(store, SID, str(project))
    async with _serve(state) as client:
        preview = await (await client.get(f"/api/kiro-cli/sessions/{SID}", params=_owner())).json()
    assert preview.get("context_skipped") == code


@pytest.mark.asyncio
async def test_continue_with_an_unreadable_config_copies_the_transcript(
    state, store, project, monkeypatch
):
    from kiro_crew.dashboard import kiro_cli_continue as kcc

    def broken(*_a, **_k):
        raise ValueError("corrupt config")

    monkeypatch.setattr(kcc.KiroCrewConfig, "load", staticmethod(broken))
    _write(store, SID, str(project))
    async with _serve(state) as client:
        body = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
    assert body["resume_mode"] == "transcript"
    assert body["context_skipped"] == "kiro_cli_backend_required"


@pytest.mark.asyncio
async def test_continue_keeps_a_folder_whose_name_ends_in_a_space(state, store, tmp_path):
    # A sibling named without the space also exists: trimming the recorded path
    # would quietly make the wrong folder the chat's project.
    spaced = tmp_path / "widget "
    spaced.mkdir()
    (tmp_path / "widget").mkdir()
    _write(store, SID, str(spaced))
    async with _serve(state) as client:
        body = await (
            await client.post(f"/api/kiro-cli/sessions/{SID}/continue", params=_owner())
        ).json()
    assert body["project"] == str(spaced.resolve())
    assert state._slots[body["key"]].project == str(spaced.resolve())
