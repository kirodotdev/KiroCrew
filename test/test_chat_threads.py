"""Reply threads on crewmate chat messages (``dashboard/chat_threads.py``).

Uses ``async with _client()`` inside each test rather than an async-gen fixture:
the CI-pinned ``pytest-asyncio`` is incompatible with the pinned ``pytest`` for
async fixtures (see test_denied_commands_api.py docstring).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import pathlib
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import history_projection, platform_compat
from kiro_crew.acp_backends import ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_threads
from kiro_crew.dashboard.chat_threads import (
    THREAD_BOUNDARY_PROMPT,
    THREAD_BOUNDARY_PROMPT_NO_TOOLS,
    THREAD_INSTRUCTIONS,
    _run_thread_turn,
    api_chat_thread_detail,
    api_chat_thread_reply,
    api_chat_threads_summary,
    build_thread_message,
    summarize,
    thread_session_key,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.side_readonly_spec import PublishedSpec, ReadOnlySpecError
from kiro_crew.history import ThreadStoreUnreadable
from kiro_crew.members import DM_SLOT_MODE

_MEMBER_SLOT = "member-radar"
_ANSWER = "Five are covered by open PRs, three are queued."


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/threads", api_chat_threads_summary)
    app.router.add_get("/api/chat/threads/{mid}", api_chat_thread_detail)
    app.router.add_post("/api/chat/threads/{mid}/reply", api_chat_thread_reply)
    return app


def _client(state, *, app_name: str = "") -> TestClient:
    app = _make_app(state)
    if app_name:

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_as_app)
    return TestClient(TestServer(app))


def _member_slot(state, key: str = _MEMBER_SLOT):
    """A crewmate's chat with one exchange in it; returns ``(slot, parent_mid)``.

    The transcript is written to disk because the thread store only writes
    beside a transcript that exists (a chat younger than its first flush answers
    ``transcript_missing``).
    """
    slot = state.get_or_create_slot(key, agent="Radar", mode=DM_SLOT_MODE)
    slot.append("user", "Anything overnight?", broadcast=False)
    row = slot.append(
        "assistant", "Overnight triage: 9 new issues, one needs you.", broadcast=False
    )
    # Both rows flushed, with the window's mids: a thread hangs off the parent
    # row ON DISK, so a parent the slot has not flushed yet is refused.
    hk = slot_history_key(slot)
    state.conversation_log.append(
        hk, "user", "Anything overnight?", mid=slot.messages[0]["meta"]["mid"]
    )
    state.conversation_log.append(hk, "assistant", row["content"], mid=row["meta"]["mid"])
    return slot, row["meta"]["mid"]


def M(tag: str) -> str:
    """A canonical row id (``m-`` + 16 hex) that is stable per *tag*, for tests
    that name mids by hand: the store keeps only keys in the minted shape."""
    import hashlib

    return "m-" + hashlib.sha256(tag.encode()).hexdigest()[:16]


def _store_path(state, slot):
    return state.conversation_log.threads_sidecar_path(slot_history_key(slot))


def _threads(state, slot):
    return state.conversation_log.read_threads(slot_history_key(slot))


def _identity(state, slot):
    """What the route captures at admission: the transcript's ``created_at``."""
    return state.conversation_log.thread_transcript_identity(slot_history_key(slot))


def _seed_reply(state, slot, mid, role, content, *, max_total: int = 5000):
    """Store a reply the way the route does -- against the transcript's identity."""
    return state.conversation_log.append_thread_reply(
        slot_history_key(slot),
        mid,
        chat_threads._new_reply(role, content),
        max_replies=500,
        max_total=max_total,
        expected_created_at=_identity(state, slot),
    )


def _capture_broadcasts(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []

    def _record(msg_type, data):
        events.append((msg_type, data))

    state.broadcast_ws = _record
    state.broadcast_ws_owners = _record
    return events


def _finals(events):
    return [d for t, d in events if t == "chat.thread_reply" and d.get("final")]


@pytest.fixture(autouse=True)
def _no_carry_over():
    """The in-flight set is module state; a test that leaves a turn marked in
    flight would refuse the next test's reply."""
    chat_threads._in_flight.clear()
    yield
    chat_threads._in_flight.clear()


def _stub_turn(monkeypatch):
    """Replace the background turn with a no-op that only clears the in-flight
    mark, as the real turn's ``finally`` does."""
    calls: list[dict[str, Any]] = []

    async def _fake(state, slot, mid, run_id, text, parent, context_before, flight_key, identity):
        calls.append({"mid": mid, "text": text, "parent": parent, "context_before": context_before})
        chat_threads._in_flight.discard(flight_key)

    monkeypatch.setattr(chat_threads, "_run_thread_turn", _fake)
    return calls


def _parent() -> dict[str, Any]:
    return {"role": "assistant", "content": "Overnight triage: 9 new issues."}


def _arm_turn(state, monkeypatch, *, answer: str, backend: str = ACP_BACKEND_KIRO):
    """A fake cold session that answers *answer*; returns the recorded calls."""
    calls: list[dict[str, Any]] = []
    provider = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        calls.append({"key": key, **kwargs})
        return provider, True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, **kwargs):
        calls.append({"message": message, **kwargs})
        if answer and on_chunk is not None:
            on_chunk(answer)
        return answer

    monkeypatch.setattr(chat_threads, "stream_and_collect", _fake_stream)
    monkeypatch.setattr(
        chat_threads,
        "publish_readonly_spec",
        lambda base, project=None: PublishedSpec(name=f"{base}--readonly", digest="d" * 64),
    )
    monkeypatch.setattr(
        chat_threads.KiroCrewConfig,
        "load",
        classmethod(lambda cls: MagicMock(agent=MagicMock(acp_backend=backend))),
    )
    monkeypatch.setattr(chat_threads, "warm_project_agent_names", AsyncMock())

    def _resolve(cfg, agent, project, **kwargs):
        # Recorded so a test can pin that the turn never asks the resolver to
        # validate memory files (a synchronous SQLite open on the loop).
        calls.append({"resolve": agent, **kwargs})
        return MagicMock(kiro_agent="kirocrew", requested_resolved=True)

    monkeypatch.setattr(chat_threads, "resolve_agent_bindings", _resolve)
    return calls


# ── Store ──


def test_summarize_reports_count_last_ts_and_participants_in_first_appearance_order():
    threads = {
        "m1": [
            {"role": "user", "content": "a", "ts": "2026-01-01T00:00:00+00:00"},
            {"role": "assistant", "content": "b", "ts": "2026-01-01T00:01:00+00:00"},
            {"role": "user", "content": "c", "ts": "2026-01-01T00:02:00+00:00"},
        ],
        "m2": [],
    }
    assert summarize(threads) == {
        "m1": {
            "count": 3,
            "last_reply_ts": "2026-01-01T00:02:00+00:00",
            "participants": ["user", "assistant"],
        }
    }


def test_sidecar_lives_beside_the_transcript_and_is_removed_with_it(tmp_path):
    state = _make_state(tmp_path)
    key = "dashboard:member-radar"
    log = state.conversation_log
    path = log.threads_sidecar_path(key)
    assert path.parent == tmp_path / ".threads"
    log.append(key, "user", "hi", mid=M("1"))
    assert (
        log.append_thread_reply(
            key, M("1"), chat_threads._new_reply("user", "x"), max_replies=5, max_total=50
        )
        == "ok"
    )
    assert path.exists()
    assert log.delete_session(key)
    assert not path.exists()


def test_store_refuses_to_write_beside_a_missing_transcript(tmp_path):
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:member-nobody"
    reply = chat_threads._new_reply("user", "x")
    assert log.append_thread_reply(key, M("1"), reply, max_replies=5, max_total=50) == "missing"
    assert not log.threads_sidecar_path(key).exists()


def test_a_reply_admitted_against_one_transcript_never_lands_in_its_replacement(tmp_path):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    log.update_metadata(key, {"created_at": "2026-09-22T07:00:00+00:00"})
    identity = log.thread_transcript_identity(key)
    assert identity == "2026-09-22T07:00:00+00:00"
    # The chat is deleted and recreated under the same key while a turn is in flight.
    assert log.delete_session(key)
    log.append(key, "user", "a new chat")
    log.update_metadata(key, {"created_at": "2026-09-22T08:00:00+00:00"})
    reply = chat_threads._new_reply("assistant", "late")
    assert (
        log.append_thread_reply(
            key, mid, reply, max_replies=5, max_total=50, expected_created_at=identity
        )
        == "replaced"
    )
    assert log.read_threads(key) == {}
    # A caller with no identity (a transcript whose metadata predates the
    # field) is held to the parent instead: the old chat's message id is not in
    # the replacement, so the reply is refused; a row the replacement holds is
    # answered.
    assert log.append_thread_reply(key, mid, reply, max_replies=5, max_total=50) == "replaced"
    log.append(key, "assistant", "a row of the new chat", mid=M("new"))
    assert log.append_thread_reply(key, M("new"), reply, max_replies=5, max_total=50) == "ok"
    assert list(log.read_threads(key)) == [M("new")]


@pytest.mark.asyncio
async def test_a_chat_replaced_between_lookup_and_write_is_refused(tmp_path, monkeypatch):
    """The identity is captured before the parent lookup; a delete-and-recreate
    landing anywhere after it is caught at the store write, and the in-flight
    mark is released."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    log = state.conversation_log
    key = slot_history_key(slot)
    real_transcript = chat_threads._transcript

    async def _replace_then_lookup(state_, slot_):
        rows = await real_transcript(state_, slot_)
        assert log.delete_session(key)
        log.append(key, "user", "a new chat under the same key")
        log.update_metadata(key, {"created_at": "2026-09-22T08:00:00+00:00"})
        return rows

    monkeypatch.setattr(chat_threads, "_transcript", _replace_then_lookup)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "late"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "transcript_replaced"
    assert log.read_threads(key) == {}
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


def test_delete_takes_the_thread_sidecar_with_the_transcript_or_neither(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    transcript = log._path(key)
    # The transcript refuses to go: the sidecar is put back where it was.
    real_unlink = pathlib.Path.unlink

    def _refuse(self, missing_ok=False):
        if self == transcript:
            raise OSError("busy")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", _refuse)
    assert log.delete_session(key) is False
    assert transcript.exists() and path.exists()
    assert log.read_threads(key)[mid][0]["content"] == "kept"
    assert not list(path.parent.glob("*.deleting-*"))
    monkeypatch.undo()
    assert log.delete_session(key)
    assert not path.exists() and not transcript.exists()
    assert not list(path.parent.glob("*.deleting-*"))


def test_a_sidecar_that_cannot_be_looked_at_stops_the_reclaim_scan(tmp_path, monkeypatch):
    """Session Storage lists a session's files through one scan; a sidecar
    that is absent is fine, one that cannot be stat'ed RAISES rather than being
    left out, or the transcript would move and the replies stay behind."""
    from kiro_crew import session_storage

    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    sidecar = _store_path(state, slot)
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: sidecar.parent.parent)
    from kiro_crew.history import transcript_stem

    stem = transcript_stem(slot_history_key(slot))
    listed = session_storage._unit_paths("", (stem,), archives={}, cli_files={})
    assert sidecar in {p for p, _rel in listed}
    real_lstat = os.lstat

    def _refuse(path, *a, **kw):
        if pathlib.Path(path) == sidecar:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", _refuse)
    with pytest.raises(PermissionError):
        session_storage._unit_paths("", (stem,), archives={}, cli_files={})
    # Absent is not an error: a session with no threads lists no sidecar.
    monkeypatch.undo()
    sidecar.unlink()
    assert sidecar not in {
        p for p, _rel in session_storage._unit_paths("", (stem,), archives={}, cli_files={})
    }
    # A link where `.threads` should be refuses the scan, so no reclaim can be
    # fed a path outside the session store.
    sidecar.parent.rmdir()
    sidecar.parent.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: sidecar.parent.parent)
    with pytest.raises(NotADirectoryError):
        session_storage._unit_paths("", (stem,), archives={}, cli_files={})


def test_unreadable_sidecar_is_refused_never_overwritten(tmp_path):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    path = _store_path(state, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ThreadStoreUnreadable):
        log.read_threads(key)
    with pytest.raises(ThreadStoreUnreadable):
        log.append_thread_reply(
            key,
            mid,
            chat_threads._new_reply("user", "x"),
            max_replies=5,
            max_total=50,
            expected_created_at=_identity(state, slot),
        )
    assert path.read_text(encoding="utf-8") == "{not json"
    # Rows of the wrong shape drop one by one; a whole map of the wrong shape
    # refuses; a retained row is reduced to the reply schema -- a field the store
    # never writes (an agent editing the file) does not survive the read.
    path.write_text(
        json.dumps(
            {
                "threads": {
                    M("m"): "not-a-list",
                    # A key that is not a minted row id is not a thread: the
                    # summary route hands keys to the dashboard as they are.
                    "AKIAIOSFODNN7EXAMPLE": [
                        {"id": "3" * 32, "role": "user", "content": "x", "ts": ""}
                    ],
                    M("n"): [
                        {"role": "user"},
                        3,
                        {
                            "id": "0" * 32,
                            "role": "user",
                            "content": "ok",
                            "ts": "2026-09-22T07:41:00+00:00",
                            "token": "AKIA...",
                        },
                        # Metadata not in the writer's shape is not a reply the
                        # store wrote: an id or ts that could carry prose (a
                        # credential an agent put there) never reaches the
                        # dashboard, since only ``content`` is redacted on the way out.
                        {"id": "AKIAIOSFODNN7EXAMPLE", "role": "user", "content": "x", "ts": ""},
                        {"id": "1" * 32, "role": "user", "content": "x", "ts": "ghp_secret"},
                        {"id": "2" * 32, "role": "system", "content": "x", "ts": ""},
                    ],
                }
            }
        )
    )
    assert log.read_threads(key) == {
        M("n"): [
            {"id": "0" * 32, "role": "user", "content": "ok", "ts": "2026-09-22T07:41:00+00:00"}
        ]
    }
    path.write_text(json.dumps({"threads": []}))
    with pytest.raises(ThreadStoreUnreadable):
        log.read_threads(key)


def test_read_threads_bounds_every_retained_field_and_count(tmp_path):
    """The reader cuts a sidecar to the writer's own shape: a string longer than
    the writer ever stores is clipped, a thread keeps its newest rows only, the
    map stops at the whole-file cap, and an oversize mid is not a thread."""
    from kiro_crew.history import (
        THREAD_MID_RE,
        THREAD_REPLY_CONTENT_MAX_CHARS,
        THREADS_MAX_REPLIES_PER_SIDECAR,
        THREADS_MAX_REPLIES_PER_THREAD,
    )

    state = _make_state(tmp_path)
    slot, _mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    path = _store_path(state, slot)
    path.parent.mkdir(parents=True, exist_ok=True)

    def row(i, content="c"):
        return {"id": f"{i:032x}", "role": "user", "content": content, "ts": "2026-09-22T07:41:00Z"}

    per_thread = THREADS_MAX_REPLIES_PER_THREAD
    doc = {
        "threads": {
            M("big"): [row(i) for i in range(per_thread + 7)],
            M("long"): [row(0, "x" * (THREAD_REPLY_CONTENT_MAX_CHARS + 500))],
            "m-" + "f" * 17: [row(0)],
        }
    }
    # Enough further threads to cross the whole-file cap.
    rest = THREADS_MAX_REPLIES_PER_SIDECAR - per_thread - 1
    for n in range(rest + 25):
        doc["threads"][M(f"t{n}")] = [row(n)]
    path.write_text(json.dumps(doc), encoding="utf-8")
    out = log.read_threads(key)
    big = out[M("big")]
    assert (
        len(big) == per_thread
        and big[0]["id"] == f"{7:032x}"
        and big[-1]["id"] == f"{per_thread + 6:032x}"
    )
    assert len(out[M("long")][0]["content"]) == THREAD_REPLY_CONTENT_MAX_CHARS
    assert all(THREAD_MID_RE.match(m) for m in out)
    assert sum(len(v) for v in out.values()) == THREADS_MAX_REPLIES_PER_SIDECAR
    # A key mapped to no rows is not a thread: a map of empty lists cannot grow
    # the retained structure by keys alone.
    path.write_text(
        json.dumps({"threads": {M(f"e{n}"): [] for n in range(20_000)} | {M("one"): [row(0)]}}),
        encoding="utf-8",
    )
    assert list(log.read_threads(key)) == [M("one")]
    # The write path's caps are the same constants, so a sidecar it wrote reads back whole.
    assert chat_threads._MAX_REPLIES_PER_THREAD == per_thread
    assert (
        chat_threads._MAX_STORED_REPLY_CHARS + len(chat_threads._INTERRUPTED_MARKER) + 2
        <= THREAD_REPLY_CONTENT_MAX_CHARS
    )


def test_an_oversized_or_irregular_sidecar_is_refused_before_it_is_read(tmp_path, monkeypatch):
    """The file is sized before a byte of it is read: over the ceiling, or not a
    regular file, it is unreadable -- the row bounds are never reached through
    a parse that already blew the memory they exist to protect. The writer
    holds the same line: a document that would pass the ceiling is refused as
    ``sidecar_full`` and the file is left as it was."""
    from kiro_crew import history

    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    before = path.read_bytes()
    monkeypatch.setattr(history, "THREADS_SIDECAR_MAX_BYTES", len(before) - 1)
    reads = []
    real_read_text = pathlib.Path.read_text

    def _counting(self, *a, **kw):
        if self == path:
            reads.append(self)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", _counting)
    with pytest.raises(ThreadStoreUnreadable):
        log.read_threads(key)
    assert reads == []
    monkeypatch.setattr(history, "THREADS_SIDECAR_MAX_BYTES", len(before) + 8)
    assert log.read_threads(key)[mid][0]["content"] == "kept"
    # The next reply would pass the ceiling: refused as the sidecar being full, file untouched.
    assert _seed_reply(state, slot, mid, "user", "one more") == "sidecar_full"
    assert path.read_bytes() == before
    # A link where the file should be is not followed.
    monkeypatch.setattr(history, "THREADS_SIDECAR_MAX_BYTES", 64 * 1024 * 1024)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(before)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ThreadStoreUnreadable):
        log.read_threads(key)
    # Without O_NOFOLLOW (Windows) the link is refused by the pre-check instead.
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(ThreadStoreUnreadable):
        log.read_threads(key)


def test_a_link_where_the_threads_directory_should_be_gets_no_write(tmp_path):
    """The writer refuses a linked ``.threads`` directory before creating anything
    under it, and a link at the sidecar's own name is refused as unreadable (the
    read that precedes every write never follows it): nothing this store writes
    can land outside the session directory."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    path = _store_path(state, slot)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ThreadStoreUnreadable):
        _seed_reply(state, slot, mid, "user", "x")
    assert list(elsewhere.iterdir()) == []
    path.parent.unlink()
    # A link at the leaf: refused, never followed, never replaced.
    path.parent.mkdir()
    target = elsewhere / "victim.json"
    target.write_text("untouched", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(ThreadStoreUnreadable):
        _seed_reply(state, slot, mid, "user", "x")
    assert path.is_symlink() and target.read_text(encoding="utf-8") == "untouched"
    assert log is state.conversation_log


def test_delete_refuses_when_the_threads_directory_is_a_link(tmp_path):
    """A link where ``.threads`` should be would carry the delete's sidecar moves
    outside the session store: the delete refuses (transcript kept) and nothing
    under the link's target is touched."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / path.name).write_text("victim", encoding="utf-8")
    real = path.read_bytes()
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(elsewhere, target_is_directory=True)
    assert log.delete_session(key) is False
    assert log._path(key).exists()
    assert (elsewhere / path.name).read_text(encoding="utf-8") == "victim"
    assert sorted(p.name for p in elsewhere.iterdir()) == [path.name]
    path.parent.unlink()
    path.parent.mkdir()
    path.write_bytes(real)
    assert log.delete_session(key)
    assert not path.exists()


def test_a_deeply_nested_sidecar_is_unreadable_not_a_crash(tmp_path):
    state = _make_state(tmp_path)
    slot, _mid = _member_slot(state)
    path = _store_path(state, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"threads": ' + "[" * 200_000 + "]" * 200_000 + "}", encoding="utf-8")
    with pytest.raises(ThreadStoreUnreadable):
        state.conversation_log.read_threads(slot_history_key(slot))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc to count descriptors")
def test_a_leftover_staged_sidecar_is_never_written_over(tmp_path, monkeypatch):
    """A staged sidecar an earlier delete left behind (its rollback failed, or the
    process died) is the only copy of those replies. The move aside takes a fresh
    name each time and refuses a taken one, so a later delete cannot clobber it."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    monkeypatch.setattr(history_projection.secrets, "token_hex", lambda n: "feedface")
    leftover = path.with_name(f"{path.name}.deleting-{os.getpid()}-feedface")
    leftover.write_text('{"older": "replies"}', encoding="utf-8")
    assert log.delete_session(key) is False
    assert leftover.read_text(encoding="utf-8") == '{"older": "replies"}'
    assert path.exists() and log._path(key).exists()
    assert log.read_threads(key)[mid][0]["content"] == "kept"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="descriptor pinning is POSIX-only")
def test_a_failed_sidecar_staging_leaks_no_descriptor(tmp_path):
    """The delete pins the `.threads` directory before moving the sidecar aside;
    a move that fails (an unwritable directory) must close that descriptor on
    the way out, or repeated deletes walk the gateway toward EMFILE."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    os.chmod(path.parent, 0o500)
    try:
        # One refused delete first: the session index opens its SQLite handles
        # lazily on the first call, and those are not the descriptor under test.
        assert log.delete_session(key) is False
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            assert log.delete_session(key) is False
        after = len(os.listdir("/proc/self/fd"))
    finally:
        os.chmod(path.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- restores the fixture's own owner-only mode after the 0o500 lockout above so tmp_path can be cleaned; nothing published.  # noqa: E501  # fmt: skip
    assert after == before
    assert path.exists() and log._path(key).exists()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="needs a mode the stat honours")
def test_an_unreadable_sidecar_stops_the_delete_instead_of_orphaning_it(tmp_path):
    """`Path.exists` reads every OSError as "absent"; a sidecar the process cannot
    stat must NOT be read as "no sidecar" -- the transcript would be deleted while
    the replies stayed behind. The delete refuses and leaves both in place."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    assert _seed_reply(state, slot, mid, "user", "kept") == "ok"
    path = _store_path(state, slot)
    os.chmod(path.parent, 0)
    try:
        assert log.delete_session(key) is False
    finally:
        os.chmod(path.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- restores the fixture's own owner-only mode after the lockout above; nothing published.  # noqa: E501  # fmt: skip
    assert path.exists() and log._path(key).exists()


# ── Summary + detail ──


@pytest.mark.asyncio
async def test_summary_requires_slot_and_refuses_a_plain_chat(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-1")
    async with _client(state) as client:
        resp = await client.get("/api/chat/threads")
        assert resp.status == 400
        resp = await client.get("/api/chat/threads", params={"slot": "nope"})
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"
        resp = await client.get("/api/chat/threads", params={"slot": "chat-1"})
        assert resp.status == 409
        assert (await resp.json())["code"] == "not_crewmate_chat"


@pytest.mark.asyncio
async def test_summary_is_empty_before_any_reply(tmp_path):
    state = _make_state(tmp_path)
    _member_slot(state)
    async with _client(state) as client:
        resp = await client.get("/api/chat/threads", params={"slot": _MEMBER_SLOT})
        assert resp.status == 200
        assert await resp.json() == {"threads": {}}


@pytest.mark.asyncio
async def test_app_caller_gets_an_indistinguishable_404(tmp_path):
    state = _make_state(tmp_path)
    _, mid = _member_slot(state)
    async with _client(state, app_name="some-app") as client:
        resp = await client.get("/api/chat/threads", params={"slot": _MEMBER_SLOT})
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"
        resp = await client.get(f"/api/chat/threads/{mid}", params={"slot": _MEMBER_SLOT})
        assert resp.status == 404
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "x"}
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_detail_quotes_the_parent_and_404s_an_unknown_mid(tmp_path):
    state = _make_state(tmp_path)
    _, mid = _member_slot(state)
    async with _client(state) as client:
        resp = await client.get(f"/api/chat/threads/{mid}", params={"slot": _MEMBER_SLOT})
        assert resp.status == 200
        body = await resp.json()
        assert body["parent"]["mid"] == mid
        assert body["parent"]["role"] == "assistant"
        assert body["parent"]["content"].startswith("Overnight triage")
        assert body["replies"] == []
        assert body["in_flight"] is False
        resp = await client.get(f"/api/chat/threads/{M('missing')}", params={"slot": _MEMBER_SLOT})
        assert resp.status == 404
        assert (await resp.json())["code"] == "parent_not_found"


@pytest.mark.asyncio
async def test_unreadable_sidecar_answers_503_on_every_route(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    path = _store_path(state, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    async with _client(state) as client:
        responses = [
            await client.get("/api/chat/threads", params={"slot": _MEMBER_SLOT}),
            await client.get(f"/api/chat/threads/{mid}", params={"slot": _MEMBER_SLOT}),
            await client.post(
                f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "hi"}
            ),
        ]
        for resp in responses:
            assert resp.status == 503
            assert (await resp.json())["code"] == "threads_unavailable"
    assert path.read_text(encoding="utf-8") == "{not json"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


# ── Reply ──


@pytest.mark.asyncio
async def test_reply_is_stored_broadcast_and_starts_the_turn(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    calls = _stub_turn(monkeypatch)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "  What about the other 8?  "},
        )
        assert resp.status == 202
        body = await resp.json()
        assert body["reply"]["role"] == "user"
        assert body["reply"]["content"] == "What about the other 8?"
        assert body["run_id"]
        for task in list(state._background_tasks):
            await task
        # The store holds the reply; the summary reports it; the main chat did not grow.
        resp = await client.get("/api/chat/threads", params={"slot": _MEMBER_SLOT})
        assert (await resp.json())["threads"] == {
            mid: {"count": 1, "last_reply_ts": body["reply"]["ts"], "participants": ["user"]}
        }
        assert len(slot.messages) == 2
        # The user's frame went out on the thread channel, not the main transcript.
        thread_frames = [d for t, d in events if t == "chat.thread_reply"]
        assert [f["role"] for f in thread_frames] == ["user"]
        assert thread_frames[0]["mid"] == mid
        assert thread_frames[0]["slot"] == _MEMBER_SLOT
        assert not [t for t, _ in events if t in ("chat_message", "chat_done")]
        # The turn got the parent and the chat before it.
        assert calls and calls[0]["mid"] == mid
        assert calls[0]["parent"]["content"].startswith("Overnight triage")
        assert [r["content"] for r in calls[0]["context_before"]] == ["Anything overnight?"]


@pytest.mark.asyncio
async def test_a_re_sent_reply_id_returns_the_stored_reply_and_runs_no_second_turn(
    tmp_path, monkeypatch
):
    """The panel mints a ``reply_id`` per send. A client that never saw the 202
    (response lost on the wire) re-sends the same id: the store already holds
    the row, so it is handed back and never stored twice. While its turn runs,
    or once it is answered, nothing else happens; stored unanswered with no turn
    active (the turn failed or a restart took it), the re-send runs the turn for
    the stored reply. A malformed id is a 400, never a fresh id."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    calls = _stub_turn(monkeypatch)
    rid = "ab" * 16
    async with _client(state) as client:
        first = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "Is #4198 among them?", "reply_id": rid},
        )
        assert first.status == 202
        stored = (await first.json())["reply"]
        assert stored["id"] == rid
        # Re-sent while the crewmate is still replying: the stored row, not a 409.
        chat_threads._in_flight.add(f"{slot.key}:{mid}")
        again = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "Is #4198 among them?", "reply_id": rid},
        )
        chat_threads._in_flight.discard(f"{slot.key}:{mid}")
        assert again.status == 202
        body = await again.json()
        assert body["reply"] == stored and body["duplicate"] is True and body["run_id"] == ""
        for task in list(state._background_tasks):
            await task
        # The stub turn stored no answer: the reply is the last row and no turn
        # is active, so the re-send runs the turn again for the STORED reply
        # (its text, not the re-sent one) -- one row, two turns.
        again2 = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "edited later", "reply_id": rid},
        )
        assert again2.status == 202
        body2 = await again2.json()
        assert body2["reply"] == stored and body2["duplicate"] is True and body2["run_id"]
        for task in list(state._background_tasks):
            await task
        assert [r["id"] for r in _threads(state, slot)[mid]] == [rid]
        assert len(calls) == 2 and calls[1]["text"] == "Is #4198 among them?"
        # A stopped turn leaves the "stored but not answered ... Send it again"
        # row behind the reply. That row is the ask it invites, not an answer:
        # the same-id re-send it tells the user to make runs the turn.
        assert _seed_reply(state, slot, mid, "assistant", chat_threads._UNANSWERED_FALLBACK) == "ok"
        again3 = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "Is #4198 among them?", "reply_id": rid},
        )
        assert again3.status == 202 and (await again3.json())["run_id"]
        for task in list(state._background_tasks):
            await task
        assert len(calls) == 3 and calls[2]["text"] == "Is #4198 among them?"
        # Answered: the re-send is a plain duplicate, no turn.
        assert _seed_reply(state, slot, mid, "assistant", "5 covered.") == "ok"
        later = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "edited later", "reply_id": rid},
        )
        assert later.status == 202 and (await later.json())["run_id"] == ""
        assert len(calls) == 3
        bad = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "x", "reply_id": "not-hex"},
        )
        assert bad.status == 400 and (await bad.json())["code"] == "invalid_reply_id"
    # The store's own guard, under its lock: a duplicate id writes nothing.
    assert _seed_reply(state, slot, mid, "user", "again") == "ok"
    dup = dict(chat_threads._new_reply("user", "twice", rid))
    assert (
        state.conversation_log.append_thread_reply(
            slot_history_key(slot), mid, dup, max_replies=500, max_total=5000
        )
        == "duplicate"
    )
    assert [r["content"] for r in _threads(state, slot)[mid]] == [
        "Is #4198 among them?",
        chat_threads._UNANSWERED_FALLBACK,
        "5 covered.",
        "again",
    ]


@pytest.mark.asyncio
async def test_reply_validation(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    _, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    async with _client(state) as client:
        resp = await client.post(f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT})
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_required_fields"
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "   "}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "empty_reply"
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "x" * (chat_threads._MAX_REPLY_BYTES + 1)},
        )
        assert resp.status == 413
        # A lone surrogate is a str JSON admits and no encoding carries: 400, not 500.
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply",
            data=b'{"slot_key": "%s", "text": "\\ud800"}' % _MEMBER_SLOT.encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_text"
        resp = await client.post(
            f"/api/chat/threads/{M('missing')}/reply", json={"slot_key": _MEMBER_SLOT, "text": "hi"}
        )
        assert resp.status == 404
        assert (await resp.json())["code"] == "parent_not_found"
        resp = await client.post(
            f"/api/chat/threads/{'m' * 129}/reply", json={"slot_key": _MEMBER_SLOT, "text": "hi"}
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_second_reply_while_the_crewmate_is_replying_is_refused(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    _, mid = _member_slot(state)

    async def _hold(state, slot, mid, run_id, text, parent, context_before, flight_key, identity):
        pass  # never clears the mark: the turn is still running

    monkeypatch.setattr(chat_threads, "_run_thread_turn", _hold)
    async with _client(state) as client:
        first = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "one"}
        )
        assert first.status == 202
        second = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert second.status == 409
        assert (await second.json())["code"] == "thread_turn_in_flight"
        resp = await client.get(f"/api/chat/threads/{mid}", params={"slot": _MEMBER_SLOT})
        assert (await resp.json())["in_flight"] is True


@pytest.mark.asyncio
async def test_two_replies_racing_through_the_store_write_run_one_turn(tmp_path, monkeypatch):
    """The reservation is taken before the store write suspends, so a
    double-click cannot start two turns on one thread."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    calls = _stub_turn(monkeypatch)
    async with _client(state) as client:
        a, b = await asyncio.gather(
            client.post(
                f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "one"}
            ),
            client.post(
                f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
            ),
        )
        for task in list(state._background_tasks):
            await task
    assert sorted([a.status, b.status]) == [202, 409]
    assert len(calls) == 1
    assert len(_threads(state, slot)[mid]) == 1


@pytest.mark.asyncio
async def test_thread_full_is_refused_and_releases_the_mark(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 1)
    assert _seed_reply(state, slot, mid, "user", "one") == "ok"
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "thread_full"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_store_error_nothing_names_still_releases_the_mark(tmp_path, monkeypatch):
    """An ``OSError`` out of the sidecar write is not one of the store's
    outcomes; it answers the documented 503, and the reservation must not
    outlive it, or the thread refuses every reply until restart."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)

    def _disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state.conversation_log, "append_thread_reply", _disk_full)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert resp.status == 503
        assert (await resp.json())["code"] == "threads_unavailable"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_user_reply_leaves_the_last_seat_for_the_answer(tmp_path, monkeypatch):
    """With one seat left under the cap the user's reply is refused, not stored:
    admitting it would run a whole turn whose answer then finds the thread
    full and is dropped. The answer itself writes under the full cap."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 2)
    assert _seed_reply(state, slot, mid, "user", "one") == "ok"
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "thread_full"
    assert [r["content"] for r in _threads(state, slot)[mid]] == ["one"]
    # Same seat rule for the whole-sidecar bound.
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 500)
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_SIDECAR", 2)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "threads_full"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_cancelled_handler_drains_the_commit_and_leaves_no_unanswered_reply(
    tmp_path, monkeypatch
):
    """Cancelled while the store write is in flight (gateway shutdown): the write
    still commits, so the drain writes the terminal "not answered" row beside
    it, the cancellation propagates, and the reservation is released."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    real_append = log.append_thread_reply
    started = threading.Event()
    release = threading.Event()

    def _slow_append(*args, **kwargs):
        if kwargs.get("max_replies") == chat_threads._MAX_REPLIES_PER_THREAD - 1:
            started.set()
            release.wait(5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(log, "append_thread_reply", _slow_append)
    reply = chat_threads._new_reply("user", "answer me")
    task = asyncio.create_task(
        chat_threads._append_user_reply(log, key, mid, reply, _identity(state, slot))
    )
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = _threads(state, slot)[mid]
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == chat_threads._UNANSWERED_FALLBACK


def test_the_sidecar_as_a_whole_is_bounded_across_threads(tmp_path):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    key = slot_history_key(slot)
    state.conversation_log.append(key, "assistant", "a", mid=M("a"))
    state.conversation_log.append(key, "assistant", "b", mid=M("b"))
    assert _seed_reply(state, slot, M("a"), "user", "1", max_total=2) == "ok"
    assert _seed_reply(state, slot, M("b"), "user", "2", max_total=2) == "ok"
    assert _seed_reply(state, slot, mid, "user", "3", max_total=2) == "sidecar_full"
    assert sum(len(r) for r in _threads(state, slot).values()) == 2


@pytest.mark.asyncio
async def test_a_full_sidecar_is_refused_with_its_own_code(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _stub_turn(monkeypatch)
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_SIDECAR", 1)
    state.conversation_log.append(slot_history_key(slot), "assistant", "other", mid=M("other"))
    assert _seed_reply(state, slot, M("other"), "user", "one") == "ok"
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "two"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "threads_full"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_reply_before_the_first_flush_is_refused_and_releases_the_mark(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(_MEMBER_SLOT, agent="Radar", mode=DM_SLOT_MODE)
    mid = slot.append("assistant", "hello", broadcast=False)["meta"]["mid"]
    _stub_turn(monkeypatch)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "hi"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "transcript_missing"
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_parent_the_slot_has_not_flushed_is_refused_and_releases_the_mark(
    tmp_path, monkeypatch
):
    """The transcript exists, but the parent row is only in the memory window:
    a thread on it would be unreachable if the process died before the flush,
    so the reply is refused with the same "try again in a moment" answer."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(_MEMBER_SLOT, agent="Radar", mode=DM_SLOT_MODE)
    state.conversation_log.append(slot_history_key(slot), "user", "hi", mid=M("flushed"))
    mid = slot.append("assistant", "not flushed yet", broadcast=False)["meta"]["mid"]
    _stub_turn(monkeypatch)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply", json={"slot_key": _MEMBER_SLOT, "text": "hi"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "transcript_missing"
    assert state.conversation_log.read_threads(slot_history_key(slot)) == {}
    assert f"{slot.key}:{mid}" not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_parent_that_exists_only_on_disk_is_found_when_the_window_is_idle(tmp_path):
    """The memory window claims the whole chat (_disk_older_count == 0) yet the
    disk transcript is longer: an idle window reconciles against disk, so a
    parent only disk holds is found rather than answered 404."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(_MEMBER_SLOT, agent="Radar", mode=DM_SLOT_MODE)
    log = state.conversation_log
    key = slot_history_key(slot)
    log.append(key, "user", "Anything overnight?", mid=M("user"))
    log.append(key, "assistant", "a row only disk holds", mid=M("disk-only"))
    assert slot.messages == [] and slot._disk_older_count == 0
    async with _client(state) as client:
        resp = await client.get(
            f"/api/chat/threads/{M('disk-only')}", params={"slot": _MEMBER_SLOT}
        )
        assert resp.status == 200
        assert (await resp.json())["parent"]["content"] == "a row only disk holds"


# ── Envelope ──


def test_first_turn_envelope_carries_context_parent_thread_and_boundary():
    msg = build_thread_message(
        _parent(),
        [{"role": "user", "content": "Anything overnight?"}],
        [
            {"role": "user", "content": "What about the other 8?"},
            {"role": "assistant", "content": "5 covered, 3 queued."},
            {"role": "user", "content": "Is #4198 among them?"},
        ],
        "Is #4198 among them?",
        tools_available=True,
    )
    assert msg.startswith(THREAD_INSTRUCTIONS)
    assert "User: Anything overnight?" in msg
    assert "You: Overnight triage: 9 new issues." in msg
    assert "You: 5 covered, 3 queued." in msg
    # The reply being answered appears once, at the end, not also in the thread block.
    assert msg.count("Is #4198 among them?") == 1
    assert msg.rstrip().endswith("User: Is #4198 among them?")
    assert THREAD_BOUNDARY_PROMPT in msg
    assert THREAD_BOUNDARY_PROMPT_NO_TOOLS not in msg


def test_no_tools_boundary():
    msg = build_thread_message(_parent(), [], [], " why? ", tools_available=False)
    assert THREAD_BOUNDARY_PROMPT_NO_TOOLS in msg
    assert THREAD_BOUNDARY_PROMPT not in msg
    assert msg.rstrip().endswith("User: why?")


def test_a_long_thread_travels_as_its_tail_plus_a_count():
    """Every reply re-sends the envelope from a cold session, so the thread
    block is the newest replies and a count of the rest, never the whole thread."""
    tail = chat_threads._THREAD_TAIL_REPLIES
    replies = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"r{i}"} for i in range(tail + 3)
    ]
    replies.append({"role": "user", "content": "latest"})
    msg = build_thread_message(_parent(), [], replies, "latest", tools_available=True)
    assert "[3 earlier replies not shown]" in msg
    # r0..r2 are elided; r3 (the crewmate's) opens the block, the newest closes it.
    assert "\nUser: r2\n" not in msg and "\nYou: r3\n" in msg and f"\nUser: r{tail + 2}\n" in msg
    assert msg.count("latest") == 1
    short = build_thread_message(_parent(), [], replies[-2:], "latest", tools_available=True)
    assert "not shown" not in short


def test_a_credential_straddling_the_line_cap_is_scrubbed_before_the_cut():
    """The envelope leaves the dashboard's storage, so a crewmate row is redacted on
    the way out -- and BEFORE the per-line cap, or a secret straddling the cap would
    be cut to a fragment the redactor cannot recognise and leave as plain text."""
    secret = "AKIA" + "Q" * 16
    prefix = "x" * 30
    cap = len(prefix) + 12  # the cut lands inside the key
    line = chat_threads._line(chat_threads.ROLE_ASSISTANT, prefix + secret, cap)
    assert secret not in line and secret[:12] not in line
    assert len(line) <= len("You: ") + cap
    # A user's own line is not redacted (it is their text); the cap still applies.
    assert chat_threads._line(chat_threads.ROLE_USER, "a" * 20, 5) == "User: aaaaa"


def test_thread_session_key_is_its_own_stateless_dashboard_surface():
    from kiro_crew import session as session_module
    from kiro_crew.messaging.link import telemetry_channel_of
    from kiro_crew.sel import _infer_source

    key = thread_session_key("member-radar", M("1"))
    assert key == f"thread:member-radar:{M('1')}"
    assert _infer_source(key) == "dashboard"
    assert any(key.startswith(p) for p in session_module._STATELESS_PREFIXES)
    assert telemetry_channel_of(key) == "thread"


# ── The turn ──


@pytest.mark.asyncio
async def test_turn_runs_read_only_in_its_own_session_and_lands_in_the_thread(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    calls = _arm_turn(state, monkeypatch, answer=_ANSWER)
    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/threads/{mid}/reply",
            json={"slot_key": _MEMBER_SLOT, "text": "the other 8?"},
        )
        assert resp.status == 202
        for task in list(state._background_tasks):
            await task
        detail = await (
            await client.get(f"/api/chat/threads/{mid}", params={"slot": _MEMBER_SLOT})
        ).json()
    assert [r["role"] for r in detail["replies"]] == ["user", "assistant"]
    assert detail["replies"][1]["content"] == _ANSWER
    assert detail["in_flight"] is False
    # The main chat is untouched.
    assert len(slot.messages) == 2
    # Own isolated session, derived read-only spec, READ_ONLY policy.
    resolved = next(c for c in calls if "resolve" in c)
    assert resolved["validate_memory_files"] is False
    acquired = next(c for c in calls if "key" in c)
    assert acquired["key"] == thread_session_key(_MEMBER_SLOT, mid)
    assert acquired["agent"] == "kirocrew--readonly"
    streamed = next(c for c in calls if "message" in c)
    assert streamed["approval_policy"] == chat_threads.ToolApprovalPolicy.READ_ONLY
    assert streamed["session_key"] == thread_session_key(_MEMBER_SLOT, mid)
    assert streamed["message"].startswith(THREAD_INSTRUCTIONS)
    assert streamed["message"].rstrip().endswith("User: the other 8?")
    state.sessions.release.assert_called_once_with(thread_session_key(_MEMBER_SLOT, mid))
    # Every reply cold-starts: the session is destroyed after the turn.
    state.sessions.destroy.assert_awaited_once_with(thread_session_key(_MEMBER_SLOT, mid))
    # Streamed delta(s), then the terminal frame carrying the stored row. The
    # live stream goes through the rolling redactor, which may hold back the
    # trailing word until the stream ends, so the deltas are a prefix of the
    # answer and the final frame -- the one the panel keeps -- carries it whole.
    frames = [d for t, d in events if t == "chat.thread_reply" and d["role"] == "assistant"]
    deltas = "".join(d["content"] for d in frames[:-1])
    assert deltas and _ANSWER.startswith(deltas)
    assert all("final" not in d for d in frames[:-1])
    assert frames[-1]["final"] is True and frames[-1]["content"] == _ANSWER
    assert frames[-1]["reply"]["id"] == detail["replies"][1]["id"]
    assert not [t for t, _ in events if t in ("chat_message", "chat_done")]


@pytest.mark.asyncio
async def test_turn_without_tools_uses_reject_all_and_the_no_tools_boundary(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    calls = _arm_turn(state, monkeypatch, answer="ok", backend="other-harness")
    assert _seed_reply(state, slot, mid, "user", "hi") == "ok"
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    acquired = next(c for c in calls if "key" in c)
    streamed = next(c for c in calls if "message" in c)
    assert acquired["agent"] == "kirocrew"
    assert streamed["approval_policy"] == chat_threads.ToolApprovalPolicy.REJECT_ALL
    assert THREAD_BOUNDARY_PROMPT_NO_TOOLS in streamed["message"]


@pytest.mark.asyncio
async def test_empty_answer_becomes_the_visible_boundary_line(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="")
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    final = _finals(events)[-1]
    assert "read-only" in final["content"]
    assert "main chat" in final["content"]
    assert "is_error" not in final
    stored = _threads(state, slot)[mid]
    assert stored[-1]["role"] == "assistant" and stored[-1]["content"] == final["content"]


@pytest.mark.asyncio
async def test_a_long_answer_is_clipped_before_it_is_stored(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="x" * (chat_threads._MAX_STORED_REPLY_CHARS + 500))
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    stored = _threads(state, slot)[mid][-1]["content"]
    assert len(stored) == chat_threads._MAX_STORED_REPLY_CHARS + len(chat_threads._CLIPPED_MARKER)
    assert stored.endswith(chat_threads._CLIPPED_MARKER)


@pytest.mark.asyncio
async def test_a_reply_the_store_refuses_is_published_as_a_failure(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="a fine answer")
    # The thread fills up between the user's reply and the crewmate's.
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 1)
    assert _seed_reply(state, slot, mid, "user", "hi") == "ok"
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    final = _finals(events)[-1]
    assert final["is_error"] is True
    assert "reply" not in final
    assert "full" in final["content"]
    assert [r["role"] for r in _threads(state, slot)[mid]] == ["user"]


@pytest.mark.asyncio
async def test_turn_failure_sends_a_plain_final_error_and_persists_nothing(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="never")

    def _refuse(base, project=None):
        raise ReadOnlySpecError("base_spec_missing", "no spec")

    monkeypatch.setattr(chat_threads, "publish_readonly_spec", _refuse)
    sel_mock = MagicMock()
    monkeypatch.setattr(chat_threads, "sel", lambda: sel_mock)
    flight = f"{slot.key}:{mid}"
    chat_threads._in_flight.add(flight)
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], flight, _identity(state, slot)
    )
    final = _finals(events)
    assert len(final) == 1
    assert final[0]["is_error"] is True
    assert "server" not in final[0]["content"].lower()
    # A refused spec is not a transient: the sentence points at the main chat, not at a retry.
    assert "main chat" in final[0]["content"]
    assert "Try again" not in final[0]["content"]
    assert _threads(state, slot) == {}
    # The permission refusal is on the SEL audit trail, like the app-isolation one.
    denied = [
        c.kwargs
        for c in sel_mock.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]
    assert denied and denied[0]["operation"] == "thread_reply"
    assert denied[0]["source"] == "read_only_spec"
    assert "base_spec_missing" in denied[0]["error"]
    assert flight not in chat_threads._in_flight
    # No session was created, so nothing was released.
    state.sessions.release.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_spec_still_sends_its_final_frame_when_the_audit_fails(
    tmp_path, monkeypatch
):
    """The audit of the refusal is best-effort: an audit subsystem that raises
    must not leave the panel waiting for a terminal frame that never comes."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="never")

    def _refuse(base, project=None):
        raise ReadOnlySpecError("base_spec_missing", "no spec")

    def _no_audit():
        raise RuntimeError("SEL never warmed")

    monkeypatch.setattr(chat_threads, "publish_readonly_spec", _refuse)
    monkeypatch.setattr(chat_threads, "sel", _no_audit)
    flight = f"{slot.key}:{mid}"
    chat_threads._in_flight.add(flight)
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], flight, _identity(state, slot)
    )
    final = _finals(events)
    assert len(final) == 1 and final[0]["is_error"] is True
    assert "main chat" in final[0]["content"]
    assert flight not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_turn_cancelled_mid_stream_keeps_the_partial_answer(tmp_path, monkeypatch):
    """A gateway restart cancels the turn after a delta streamed: the redacted
    partial answer is stored under the interruption marker, so the reopened
    thread does not show the user's question with nothing under it."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="Five are covered")
    streamed = asyncio.Event()

    async def _hang(provider, message, *, on_chunk=None, **kwargs):
        on_chunk("Five are covered")
        streamed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_threads, "stream_and_collect", _hang)
    flight = f"{slot.key}:{mid}"
    chat_threads._in_flight.add(flight)
    task = asyncio.create_task(
        _run_thread_turn(
            state, slot, mid, "run-1", "hi", _parent(), [], flight, _identity(state, slot)
        )
    )
    await streamed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = _threads(state, slot)[mid]
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"].startswith("Five are covered")
    assert chat_threads._INTERRUPTED_MARKER in rows[-1]["content"]
    assert flight not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_cancel_landing_on_the_committed_answer_writes_no_interruption_row(
    tmp_path, monkeypatch
):
    """The final store write runs on a worker thread a cancellation does not
    stop. A cancel that lands while the whole answer is committing is drained:
    the thread holds the answer once, and no row claims it was interrupted."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer=_ANSWER)
    log = state.conversation_log
    real_append = log.append_thread_reply
    loop = asyncio.get_running_loop()
    holder: dict[str, asyncio.Task] = {}

    def _cancel_then_commit(*args, **kwargs):
        # From the worker thread: cancel the awaiting task, then commit anyway.
        loop.call_soon_threadsafe(holder["task"].cancel)
        time.sleep(0.05)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(log, "append_thread_reply", _cancel_then_commit)
    flight = f"{slot.key}:{mid}"
    chat_threads._in_flight.add(flight)
    holder["task"] = asyncio.create_task(
        _run_thread_turn(
            state, slot, mid, "run-1", "hi", _parent(), [], flight, _identity(state, slot)
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await holder["task"]
    rows = [r for r in _threads(state, slot)[mid] if r["role"] == "assistant"]
    assert [r["content"] for r in rows] == [_ANSWER]
    assert flight not in chat_threads._in_flight


@pytest.mark.asyncio
async def test_a_crewmate_that_no_longer_resolves_gets_no_substitute_answer(tmp_path, monkeypatch):
    """Deleting a crewmate leaves its chat open. The resolver then falls back to
    the default agent; a thread turn that ran with it would store a stranger's
    words under the crewmate's name, with no marker. The turn refuses instead, as
    the main chat does, and says what to do."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    calls = _arm_turn(state, monkeypatch, answer="never")
    monkeypatch.setattr(
        chat_threads,
        "resolve_agent_bindings",
        lambda cfg, agent, project, **kw: MagicMock(
            kiro_agent="kirocrew", requested_resolved=False
        ),
    )
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    final = _finals(events)[-1]
    assert final["is_error"] is True
    assert final["content"] == chat_threads._CREWMATE_GONE_FALLBACK
    assert not any("message" in c for c in calls), "no turn ran under the substituted agent"
    assert _threads(state, slot) == {}


@pytest.mark.asyncio
async def test_a_signed_out_harness_says_so(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot, mid = _member_slot(state)
    _arm_turn(state, monkeypatch, answer="never")

    class AcpAuthRequired(Exception):
        """Stands in for the harness's own signed-out error, matched by name."""

    async def _signed_out(*args, **kwargs):
        raise AcpAuthRequired("signed out")

    monkeypatch.setattr(chat_threads, "stream_and_collect", _signed_out)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._mark_kiro_signed_out", lambda state: None)
    await _run_thread_turn(
        state, slot, mid, "run-1", "hi", _parent(), [], f"{slot.key}:{mid}", _identity(state, slot)
    )
    final = _finals(events)[-1]
    assert final["is_error"] is True
    assert final["content"]
    assert _threads(state, slot) == {}
