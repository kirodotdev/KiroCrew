"""Shared fixtures and helpers of the merge-back tests.

``test_chat_merge_back*.py`` import from here; this module collects no tests.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers, chat_merge_back, chat_persistence
from kiro_crew.dashboard.chat_merge_back import (
    MERGE_NOTE_SOURCE,
    api_chat_slot_merge_back,
    api_chat_slot_merge_back_draft,
    covered_digest,
    message_key,
)
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    slot_history_key,
    transcripts_share_file,
)
from kiro_crew.dashboard.handlers.sessions import api_session_delete
from kiro_crew.dashboard.slot_buffers import MERGED_FROM_META_KEY, sanitize_merged_from
from kiro_crew.testing.wait import async_wait_until, await_until


def _row(role: str, content: str, mid: str | None) -> dict:
    row: dict = {"role": role, "content": content}
    if mid is not None:
        row["meta"] = {"mid": mid}
    return row


def _card(
    after: str, through: str, covered: list[dict], mid: str, fork_session: str = "dashboard:fork"
) -> dict:
    """A merge card in the parent: the fork messages after *after* through *through*."""
    block = {
        "session": fork_session,
        "slot": "fork",
        "title": "↳ Fork of Parent",
        "after": after,
        "through": through,
        "digest": covered_digest(covered),
        "messages": len(covered),
    }
    return {
        "role": "inject",
        "content": "summary",
        "meta": {"mid": mid, "noteId": f"note-{mid}", MERGED_FROM_META_KEY: block},
    }


def _seed(slot, *messages: tuple[str, str]) -> None:
    for role, content in messages:
        slot.append(role, content, "msg msg-u" if role == "user" else "msg msg-a", broadcast=False)


def _fork_of(state, parent, name: str = "fork", *, upto: int | None = None):
    """A fork made the way the fork route makes one: the parent's visible rows
    copied with their meta, so they keep the parent's message ids."""
    fork = state.get_or_create_slot(name)
    visible = [m for m in parent.messages if m.get("role") in ("user", "assistant")]
    for m in visible[:upto]:
        fork.append(
            m["role"],
            m["content"],
            m.get("cls", ""),
            ts=m.get("ts", ""),
            meta=dict(m["meta"]),
            broadcast=False,
        )
    fork.forked_from = effective_session_key(parent)
    fork.title = "↳ Fork of Parent"
    fork._titled = True
    state.flush_slot_now(fork)
    return fork


_CHANNEL_STEM = "slack_1700000000.000001"


_CHANNEL_KEY = "slack:1700000000.000001"


def _channel_parent_and_fork(state, stem: str, *, linked: bool):
    """A channel-born parent named by its transcript stem, and a fork of it.

    *linked* binds it to the channel's own session key, as the dashboard does
    once the session map answers for the stem; unbound, it writes the same file
    under the stem alone.
    """
    parent = state.get_or_create_slot(stem, channel_origin=True)
    if linked:
        parent.linked_session_key = _CHANNEL_KEY
    parent.title = "Thread"
    parent._titled = True
    _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
    state.flush_slot_now(parent)
    fork = _fork_of(state, parent, "channel-fork")
    _seed(fork, ("user", "try redis"), ("assistant", "redis works, TTL 300s"))
    return parent, fork


@pytest.fixture(name="model")
def model_fixture(monkeypatch):
    """The background model call, recorded and answered with a fixed note."""
    calls: list[dict] = []
    replies = ["Tried Redis as the cache. It works; the TTL is 300s."]

    async def fake(sessions, prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        reply = replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: f"model-for-{role}"))
    monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", fake)
    monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.fixture(name="audit")
def audit_fixture(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        chat_merge_back,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: calls.append(kwargs)),
    )
    return calls


def _assert_privacy_denial(
    calls: list[dict], operation: str, *, resources: str, transcript: str
) -> None:
    assert calls == [
        {
            "caller": "dashboard",
            "operation": operation,
            "outcome": "denied",
            "source": "dashboard",
            "resources": resources,
            "error": f"{transcript} transcript is restricted",
        }
    ]


@pytest.fixture(name="chats")
def chats_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent.title = "Parent"
    parent._titled = True
    _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
    state.flush_slot_now(parent)
    fork = _fork_of(state, parent)
    _seed(fork, ("user", "try redis"), ("assistant", "redis works, TTL 300s"))
    return SimpleNamespace(state=state, parent=parent, fork=fork)


def _app(state, *, request_app: str = "") -> web.Application:
    app = _make_app(state)

    @web.middleware
    async def _as_caller(request, handler):
        request["is_dashboard_user"] = not bool(request_app)
        if request_app:
            request["app"] = request_app
        return await handler(request)

    app.middlewares.insert(0, _as_caller)
    app.router.add_post("/api/chat/slots/{slot}/merge-back/draft", api_chat_slot_merge_back_draft)
    app.router.add_post("/api/chat/slots/{slot}/merge-back", api_chat_slot_merge_back)
    app.router.add_delete("/api/chat/slots/{slot}", chat_handlers.api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", chat_handlers.api_chat_slot_resume)
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_delete("/api/sessions/{key}", api_session_delete)
    return app


@asynccontextmanager
async def _client(state, **kwargs):
    async with TestClient(TestServer(_app(state, **kwargs))) as client:
        yield client


async def _draft(client, slot: str = "fork"):
    resp = await client.post(f"/api/chat/slots/{slot}/merge-back/draft", json={})
    return resp.status, await resp.json()


async def _merge(client, summary: str, draft: dict, slot: str = "fork"):
    """Merge *summary* against *draft*: its ``through`` and ``digest``."""
    resp = await client.post(
        f"/api/chat/slots/{slot}/merge-back",
        json={"summary": summary, "through": draft["through"], "digest": draft["digest"]},
    )
    return resp.status, await resp.json()


async def _close_route(client, slot: str = "parent") -> tuple[int, dict]:
    response = await client.delete(f"/api/chat/slots/{slot}")
    return response.status, await response.json()


async def _resume_route(client, history_key: str, slot: str = "parent") -> tuple[int, dict]:
    response = await client.post(f"/api/chat/slots/{slot}/resume", json={"key": history_key})
    return response.status, await response.json()


def _workflow_result_for(session: str, run_id: str) -> dict:
    """A finished run's terminal snapshot, as the registry hands it to ``on_done``."""
    return {
        "run_id": run_id,
        "name": "nightly",
        "status": "finished",
        "result": {"answer": 42},
        "session_key": session,
        "memory_mode": "persistent",
    }


def _slots_on_session(state, session: str) -> list:
    """Every live slot whose turns run on *session*, in registry order."""
    return [slot for slot in state._slots.values() if effective_session_key(slot) == session]


def _ends_at(through: str) -> dict:
    """A merge point with no draft behind it, for refusals decided before the digest."""
    return {"through": through, "digest": "0" * 64}


async def _run_turn(
    state,
    slot,
    message: str,
    *,
    outcome: str = "complete",
    started: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
    events=None,
    queued_events=None,
    cancel_when: asyncio.Event | None = None,
    yolo: bool = False,
    synthetic_payload: bool = False,
) -> list[str]:
    """Run one real turn on *slot* through ``_run_chat``; the prompts the model got.

    *events*, when given, is an async-generator factory whose events replace the
    default reply (one text chunk, then ``end_turn``); it may block or signal
    *cancel_when*. When *cancel_when* is set the turn is cancelled as soon as
    it fires, wherever the turn is at that moment. When *queued_events* is given,
    the recovery entry queued by the first turn is dispatched with those events.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_runner import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    prompts: list[str] = []
    stream_started = asyncio.Event()
    stream_calls = 0

    async def stream(prompt):
        nonlocal stream_calls
        prompts.append(prompt)
        stream_calls += 1
        stream_started.set()
        if started is not None:
            started.set()
        if release is not None:
            await await_until(release, what="the test's release")
        if outcome == "error":
            raise RuntimeError("provider failed after accepting the prompt")
        if outcome == "cancel":
            await await_until(asyncio.Future(), what="the cancel that ends this stub")
        active_events = events if stream_calls == 1 or queued_events is None else queued_events
        if active_events is not None:
            async for event in active_events():
                yield event
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="noted")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client = MagicMock(is_kiro_backend=True, stream=stream, stream_command=stream, exit_code=None)
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=yolo)
    state.context_builder = MagicMock()
    state.context_builder.conversation_log = None
    state.context_builder.build_message.side_effect = lambda text, *_a, **_k: (text, None)
    state._background_tasks = set()
    slot.append("user", message, "msg msg-u")
    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new=AsyncMock(return_value=False),
    ):
        turn = asyncio.create_task(
            _run_chat(state, slot, message, _synthetic_payload=synthetic_payload)
        )
        if outcome == "cancel":
            await await_until(stream_started, what="the stream's start")
            turn.cancel()
        elif cancel_when is not None:
            await await_until(cancel_when, what="the cancel signal")
            turn.cancel()
        await await_until(
            asyncio.gather(turn, return_exceptions=True), what="the chat turn cleanup"
        )
    if queued_events is not None:
        assert await chat_runner._start_next_queued_turn(state, slot) is True
        await await_until(
            asyncio.gather(slot.task, return_exceptions=True),
            what="the queued recovery turn cleanup",
        )
        await async_wait_until(
            lambda: not slot.running and not slot._queue,
            describe=lambda: {
                "running": slot.running,
                "queued": len(slot._queue),
                "prompts": len(prompts),
            },
        )
    tasks = list(state._background_tasks)
    for task in tasks:
        task.cancel()
    await await_until(
        asyncio.gather(*tasks, return_exceptions=True),
        what="the cancelled background task cleanup",
    )
    return prompts


def _drop_ids_on_disk(state, slot) -> None:
    """Rewrite *slot*'s transcript as it was before rows carried ids."""
    log = state.conversation_log
    key = slot_history_key(slot)
    path = log._path(key)
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() else None
        if isinstance(row, dict) and isinstance(row.get("meta"), dict):
            row["meta"].pop("mid", None)
        lines.append(line if row is None else json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _cards(parent) -> list[dict]:
    return [
        m
        for m in parent.messages
        if isinstance(m.get("meta"), dict) and MERGED_FROM_META_KEY in m["meta"]
    ]


def _change_at_source_check(chats, monkeypatch, change) -> list[bool]:
    """Apply *change* at the merge source check and prove the hold encloses it."""
    log = chats.state.conversation_log
    fork_key = slot_history_key(chats.fork)
    real_hold = log.publication_hold
    real_check = chat_merge_back._source_range_matches
    source_hold_active = False
    changed = False
    observations: list[bool] = []

    @contextmanager
    def tracked_hold(key, **kwargs):
        nonlocal source_hold_active
        with real_hold(key, **kwargs):
            was_active = source_hold_active
            if key == fork_key:
                source_hold_active = True
            try:
                yield
            finally:
                source_hold_active = was_active

    def change_then_check(*args, **kwargs):
        nonlocal changed
        observations.append(source_hold_active)
        if not changed:
            change()
            changed = True
        return real_check(*args, **kwargs)

    monkeypatch.setattr(log, "publication_hold", tracked_hold)
    monkeypatch.setattr(chat_merge_back, "_source_range_matches", change_then_check)
    return observations


async def _place_merge_card(state, parent, fork, content: str):
    visible = [row for row in fork.messages if row.get("role") in ("user", "assistant")]
    covered = visible[-2:]
    merged_from = sanitize_merged_from(
        {
            "session": effective_session_key(fork),
            "slot": fork.key,
            "title": fork.title,
            "after": message_key(visible[-3]) or "",
            "through": message_key(covered[-1]),
            "digest": covered_digest(covered),
            "messages": len(covered),
        }
    )
    assert merged_from is not None
    delivery = await chat_handlers.deliver_note(
        state,
        parent,
        content=content,
        source=MERGE_NOTE_SOURCE,
        max_age=None,
        merged_from=merged_from,
        durable=True,
    )
    assert not isinstance(delivery, web.Response)
    return delivery


def _queued_merge_context(index: int) -> dict:
    return {
        "content": f"queued merge {index}",
        "source": MERGE_NOTE_SOURCE,
        "ephemeral": True,
        "injectedAt": 1e12,
        "noteId": f"queued-note-{index}",
        "noteTranscript": "dashboard:parent",
    }


def _restrict_parent_chain(chats, mode: str = "temporary") -> str:
    """Restrict the parent's requested member of its real transcript chain."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    parent_key = slot_history_key(chats.parent)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(parent_key, {"memory_mode": mode})
    assert log.get_metadata(parent_key)["memory_mode"] == mode
    return parent_key


async def _move_older_fork_rows_to_rotated_archive(chats) -> None:
    """Leave the fork's copied rows and first fork-only turn only in its archive head."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    key = slot_history_key(chats.fork)
    await asyncio.to_thread(chats.state.flush_slot_now, chats.fork)
    path = log._path(key)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    messages = [row for row in rows if "_type" not in row]
    controls = [row for row in rows if "_type" in row]
    assert len(messages) >= 6
    archived_messages, live_messages = messages[:-2], messages[-2:]
    archive_dir = history_mod._archive_dir(log._dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / (
        history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER + "20261001-000000.jsonl"
    )
    archived = [{"_type": "archive", "reason": "rotate"}, *archived_messages]
    archive.write_text("\n".join(json.dumps(row) for row in archived) + "\n", encoding="utf-8")
    live = [*controls, *live_messages]
    path.write_text("\n".join(json.dumps(row) for row in live) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _move_parent_rows_to_rotated_archive(chats) -> None:
    """Leave the persistent parent's copied rows only in its archive head."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    key = slot_history_key(chats.parent)
    path = log._path(key)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    messages = [row for row in rows if "_type" not in row]
    controls = [row for row in rows if "_type" in row]
    assert messages
    archive_dir = history_mod._archive_dir(log._dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / (
        history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER + "20261001-000000.jsonl"
    )
    archived = [{"_type": "archive", "reason": "rotate"}, *messages]
    archive.write_text("\n".join(json.dumps(row) for row in archived) + "\n", encoding="utf-8")
    path.write_text("\n".join(json.dumps(row) for row in controls) + "\n", encoding="utf-8")
    log._invalidate_cache(key)
    chats.parent.messages.clear()


async def _restart_as_sidebar_rows(chats, tmp_path, monkeypatch):
    """Both chats after a restart that builds every open tab as a sidebar row."""
    chats.state.flush_slot_now(chats.parent)
    chats.state.flush_slot_now(chats.fork)
    home = tmp_path / "home"
    home.mkdir()
    (home / "open_slots.json").write_text(json.dumps({"keys": ["fork", "parent"], "ts": 0.0}))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(chat_persistence, "EAGER_WINDOW_RESTORES", 0)
    state = _make_state(tmp_path)
    await chat_persistence.restore_open_slots_async(state)
    return state


def _window_loads_on_the_loop(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "on the event loop" in r.getMessage()]


class _RecordingWindowLoader:
    """A ``_PendingWindow`` stand-in: counts its disk reads and window loads."""

    def __init__(self, *, read_raises: bool) -> None:
        self.read_raises = read_raises
        self.reads = 0
        self.loads = 0

    def read(self, slot) -> tuple[list[dict], list[set[str]]]:
        self.reads += 1
        if self.read_raises:
            raise OSError("transcript unreadable")
        return [], []

    def __call__(self, slot, messages=None, claims=None) -> None:
        if messages is None or claims is None:
            messages, claims = self.read(slot)
        self.loads += 1
        slot._messages = list(messages)


def _pending_twin_of(state, parent, *, read_raises: bool):
    """A dashboard twin of *parent*, built as a sidebar row whose window is still pending.

    The restore never defers a linked tab today, so the row is built by hand
    with a loader shaped like ``chat_persistence._PendingWindow``.
    """
    twin = state.get_or_create_slot("twin")
    twin.linked_session_key = effective_session_key(parent)
    twin._disk_meta_created_at = parent._disk_meta_created_at
    loader = _RecordingWindowLoader(read_raises=read_raises)
    twin._lazy_window = loader
    assert twin.window_pending
    assert transcripts_share_file(slot_history_key(parent), slot_history_key(twin))
    return twin, loader
