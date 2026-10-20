"""Settings > Chat > Models: make the chats that keep a model follow the default.

``GET /api/chat/slots/follow-default`` counts the chats a person started that keep
a model of their own; ``POST`` with the count's digest clears that model, so each
chat inherits the default again. Driven at the route on a real ``DashboardState``
with a real ``ConversationLog`` (closed chats are transcript files on disk, open
ones are slots), and through one real ``_run_chat`` turn for what a cleared chat
runs next.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest.mock
from pathlib import Path

import pytest
import test_llm_helpers_fallback_restore as restore_tests
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_runner_coverage_helpers import _drive, _slot
from chat_test_helpers import _make_state
from test_chat_send_agent_model_default import (
    CREW_PIN,
    GLOBAL_DEFAULT,
    _config,
    _grant_researcher,
    _load_config,
    _requested,
    _session_model,
    _turn_state,
)

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard import chat_follow_default as follow
from kiro_crew.dashboard import chat_handlers, chat_persistence, chat_runner
from kiro_crew.dashboard.channel_slots import note_slot_closed
from kiro_crew.dashboard.chat_api.agent_switch import clear_slot_model_binding
from kiro_crew.dashboard.chat_handlers import _close_slot, resume_slot_from_history
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.llm_helpers import slot_switch_session_lock
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, default_timeout, finished, until_parked

PATH = "/api/chat/slots/follow-default"
#: Fake AWS access-key shapes, built by concatenation as the repo's other redaction
#: tests build them, so no secret scanner reads a key literal here.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_OTHER_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLF"

OLD = "claude-fable-5"
PICK = "claude-sonnet-5"


def _app(state) -> web.Application:
    """The route behind an identity the gate reads: the local owner unless a header says not."""

    @web.middleware
    async def _identity(request, handler):
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = state
    app.router.add_get(PATH, follow.api_chat_slots_follow_default)
    app.router.add_post(PATH, follow.api_chat_slots_follow_default)
    return app


async def _count(client, **headers) -> dict:
    resp = await client.get(PATH, headers=headers)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _confirm(client, digest: str) -> dict:
    resp = await client.post(PATH, json={"digest": digest})
    assert resp.status == 200, await resp.text()
    return await resp.json()


def _line(base: Path, name: str, **meta) -> Path:
    """A closed chat: its transcript file, metadata line first."""
    path = base / f"dashboard_{name}.jsonl"
    head = {"_type": "metadata", "created_at": "2026-08-01T00:00:00", **meta}
    rows = [head, {"role": "user", "content": "hi", "ts": "2026-08-01T00:00:01"}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _head(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _open_chat(state, name: str, model: str, **attrs):
    """An open chat a person started, keeping *model*."""
    slot = state.get_or_create_slot(name, origin=SlotOrigin.USER)
    slot.model = model
    for attr, value in attrs.items():
        setattr(slot, attr, value)
    return slot


#: Each shape a chat's line can take, the slot attributes it restores to, and
#: whether the chat is a person's whose model the action clears.
_SHAPES = [
    ("person", {"origin": "user"}, True),
    ("no-origin-line", {}, True),
    ("crewmate-agent", {"origin": "user", "agent": "researcher", "agent_kind": "member"}, True),
    ("session-create-child", {"origin": "user", "created_by": "dashboard:chat-1-1"}, False),
    ("cron-child", {"origin": "user", "created_by": "cron:job-1"}, False),
    ("app-owned", {"origin": "user", "app": "auto-research"}, False),
    ("cron-origin", {"origin": "cron"}, False),
    ("app-origin", {"origin": "app"}, False),
    ("system-origin", {"origin": "system"}, False),
    ("crewmate-thread", {"origin": "user", "mode": "member"}, False),
    ("channel-linked", {"origin": "user", "linked_session_key": "slack:1.2"}, False),
    ("channel-origin", {"origin": "user", "channel_origin": True}, False),
    ("remote", {"origin": "user", "executor": "remote"}, False),
]

_SLOT_ATTR = {
    "origin": "_origin",
    "created_by": "_created_by",
    "app": "_app",
    "mode": "mode",
    "linked_session_key": "linked_session_key",
    "channel_origin": "channel_origin",
    "executor": "executor",
    "agent": "agent",
    "agent_kind": "agent_kind",
}


class TestWhichChatsCount:
    @pytest.mark.parametrize(("shape", "meta", "counted"), _SHAPES, ids=[s[0] for s in _SHAPES])
    def test_the_line_rule_and_the_slot_rule_agree(self, shape, meta, counted, tmp_path):
        """A closed chat and an open one are judged alike, on every shape."""
        expected = OLD if counted else ""
        assert follow._line_model({"model": OLD, **meta}) == expected
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-shape-1")
        slot.model = OLD
        for key, value in meta.items():
            setattr(slot, _SLOT_ATTR[key], value)
        assert follow._slot_model(slot) == expected

    @pytest.mark.parametrize("model", ["", None, 7])
    def test_a_chat_that_inherits_is_not_counted(self, model):
        assert follow._line_model({"model": model, "origin": "user"}) == ""

    @pytest.mark.parametrize(
        ("agent_kind", "counted"), [("", False), ("member", False), ("template", True)]
    )
    def test_a_chat_on_a_crewmate_that_pins_a_model_is_not_counted(
        self, agent_kind, counted, tmp_path
    ):
        """The resolver's own rule: a template that shares a crewmate's name is not it."""
        pinned = frozenset({"researcher"})
        expected = OLD if counted else ""
        meta = {"model": OLD, "origin": "user", "agent": "researcher", "agent_kind": agent_kind}
        assert follow._line_model(meta, pinned) == expected
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-pin-1")
        slot.model, slot.agent, slot.agent_kind = OLD, "researcher", agent_kind
        assert follow._slot_model(slot, pinned) == expected

    def test_a_chat_on_the_explicit_auto_pick_is_counted(self):
        """``auto`` is a pick of its own: such a chat ignores a concrete default."""
        assert follow._line_model({"model": "auto", "origin": "user"}) == "auto"

    def test_every_origin_but_a_persons_is_left_alone(self, tmp_path):
        """A slot kind added later with its own origin is covered without a new row here."""
        origins = {
            value
            for name, value in vars(SlotOrigin).items()
            if name.isupper() and not name.startswith("_")
        }
        assert SlotOrigin.USER in origins and len(origins) > 1
        state = _make_state(tmp_path)
        for origin in sorted(origins - {SlotOrigin.USER}):
            assert follow._persons_line({"origin": origin}) is False, origin
            slot = state.get_or_create_slot(f"chat-origin-{origin}")
            slot.model, slot._origin = OLD, origin
            assert follow._slot_model(slot) == "", origin


class TestCountAndConfirm:
    @pytest.mark.asyncio
    async def test_the_count_shown_is_the_number_of_chats_changed(self, tmp_path):
        state = _make_state(tmp_path)
        closed = [
            _line(tmp_path, "chat-1-100", origin="user", model=OLD),
            _line(tmp_path, "chat-2-100", model="auto"),
        ]
        kept_elsewhere = [
            _line(
                tmp_path, "chat-3-100", origin="user", model=OLD, created_by="dashboard:chat-1-100"
            ),
            _line(tmp_path, "chat-4-100", origin="cron", model=OLD),
            _line(tmp_path, "member-researcher", origin="user", model=OLD, mode="member"),
            _line(tmp_path, "chat-5-100", origin="user", model=""),
        ]
        before = {path: path.read_bytes() for path in kept_elsewhere}
        open_line = _line(tmp_path, "chat-6-100", origin="user", model=OLD)
        open_chat = _open_chat(state, "chat-6-100", OLD, jev_route=True)
        pick_gen = open_chat._model_pick_gen
        state.push_slots_update = unittest.mock.MagicMock()
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 3
            done = await _confirm(client, shown["digest"])
            assert done == {"ok": True, "changed": shown["count"]}
            assert (await _count(client))["count"] == 0
        assert [_head(path)["model"] for path in closed] == ["", ""]
        assert {path: path.read_bytes() for path in kept_elsewhere} == before
        state.push_slots_update.assert_called_once()
        # Cleared like an explicit pick; later flushes keep the cleared model.
        assert (open_chat.model, open_chat.jev_route) == ("", False)
        assert open_chat._model_pick_gen == pick_gen + 1
        state.flush_slot_now(open_chat)
        assert _head(open_line)["model"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_messages", [False, True], ids=["empty-window", "full-window"])
    async def test_an_open_chats_clear_is_saved_before_the_confirm_returns(
        self, tmp_path, with_messages
    ):
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD)
        if with_messages:
            chat.messages = [json.loads(path.read_text(encoding="utf-8").splitlines()[1])]
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            assert _head(path)["model"] == OLD
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 1}
            assert _head(path)["model"] == ""
        assert chat.model == ""

    @pytest.mark.asyncio
    async def test_an_open_chat_whose_save_is_skipped_is_not_counted_as_changed(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD)
        save = unittest.mock.MagicMock(return_value=False)
        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", save)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 0}
        save.assert_called_once()
        assert chat.model == ""
        assert _head(path)["model"] == OLD

    @pytest.mark.asyncio
    async def test_a_lazy_chat_reopened_during_the_clear_save_keeps_its_new_pick(
        self, tmp_path, monkeypatch
    ):
        """A close drains the pending clear before History can publish another pick."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        clock = ManualClock()
        clock.install(monkeypatch, channel_slots)
        clock.install(monkeypatch, chat_handlers)
        monkeypatch.setattr(chat_handlers, "_GUARDED_WRITE_WAIT_SECS", default_timeout())
        state = _make_state(tmp_path)
        name = "chat-1-100"
        history_key = f"dashboard:{name}"
        path = _line(tmp_path, name, origin="user", model=OLD, tab_id="tab-lazy-clear")
        restored = chat_persistence._apply_restored_open_slot(
            state,
            name,
            meta=_head(path),
            readable=True,
            messages=None,
            model_map={},
            unrestored=set(),
            tail=[json.loads(path.read_text(encoding="utf-8").splitlines()[1])],
        )
        assert restored == 1
        chat = state._slots[name]
        assert chat.window_pending and chat.model == OLD
        entered, release = threading.Event(), threading.Event()
        merge = chat_persistence._metadata_line.merge_empty_window

        def _held_merge(log, saved_slot, *args, **kwargs):
            if saved_slot is chat and not kwargs.get("closed"):
                entered.set()
                assert release.wait(default_timeout()), "release the pending-window clear"
            return merge(log, saved_slot, *args, **kwargs)

        monkeypatch.setattr(chat_persistence._metadata_line, "merge_empty_window", _held_merge)

        async def _close_reopen_pick():
            await _close_slot(state, chat, name)
            assert name not in state._slots
            clock.advance(1)
            outcome = await resume_slot_from_history(state, name=name, history_key=history_key)
            assert outcome.refusal is None and outcome.slot is not None
            reopened = outcome.slot
            assert reopened is not chat and state._slots[name] is reopened
            async with slot_switch_session_lock(effective_session_key(reopened)):
                async with reopened._model_pick_lock:
                    reopened.model = PICK
                    reopened._model_pick_gen += 1
                    reopened._dirty = True
                    pick_history_key = follow.slot_history_key(reopened)
            assert await chat_persistence.save_slot_off_loop(
                state,
                reopened,
                force=True,
                expected_history_key=pick_history_key,
                expected_slot_name=name,
                best_effort=False,
            )
            assert _head(path)["model"] == PICK
            return reopened

        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            confirming = asyncio.create_task(client.post(PATH, json={"digest": shown["digest"]}))
            picking = None
            try:
                await async_wait_until(
                    entered.is_set, describe=lambda: "the pending-window clear has not entered"
                )
                assert chat.window_pending and chat.model == ""
                assert _head(path)["model"] == OLD
                picking = asyncio.create_task(_close_reopen_pick())
                # Either close is waiting on the real write registry, or the new
                # pick has already landed and the held clear must not overwrite it.
                await async_wait_until(
                    lambda: picking.done() or (chat.is_closing and chat._guarded_history_writes),
                    describe=lambda: "close has neither reached its drain nor saved the new pick",
                )
            finally:
                release.set()
                tasks = [confirming] if picking is None else [confirming, picking]
                settled = await finished(asyncio.gather(*tasks), "settle clear, close and new pick")
            resp, reopened = settled
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "changed": 1}
        assert reopened.model == PICK
        assert _head(path)["model"] == PICK

    @pytest.mark.asyncio
    async def test_a_pick_during_a_failed_open_chat_save_is_kept(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD)
        entered, release = threading.Event(), threading.Event()

        def _held_save(*args, **kwargs):
            entered.set()
            assert release.wait(default_timeout()), "release the failing open-chat save"
            raise OSError("No space left on device")

        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _held_save)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            confirming = asyncio.create_task(client.post(PATH, json={"digest": shown["digest"]}))
            try:
                await async_wait_until(
                    entered.is_set, describe=lambda: "the open-chat save has not started"
                )
                assert chat.model == ""
                async with chat._model_pick_lock:
                    chat.model = PICK
                    chat._model_pick_gen += 1
                pick_gen = chat._model_pick_gen
            finally:
                release.set()
                resp = await finished(confirming, "confirm after the failing save and new pick")
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "changed": 0}
        assert (chat.model, chat._model_pick_gen) == (PICK, pick_gen)
        assert _head(path)["model"] == OLD

    @pytest.mark.asyncio
    async def test_counting_alone_changes_nothing(self, tmp_path):
        """Cancel in the confirm sends nothing more than the count."""
        state = _make_state(tmp_path)
        path = _line(tmp_path, "chat-1-100", origin="user", model=OLD)
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        open_chat = _open_chat(state, "chat-2-100", OLD)
        async with TestClient(TestServer(_app(state))) as client:
            assert (await _count(client))["count"] == 2
            assert (await _count(client))["count"] == 2
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime
        assert open_chat.model == OLD

    @pytest.mark.asyncio
    async def test_a_closed_chat_keeps_its_place_in_older_sessions(self, tmp_path):
        """The line is rewritten in place and its file keeps its modified time."""
        state = _make_state(tmp_path)
        path = _line(tmp_path, "chat-1-100", origin="user", model=OLD, title="Old chat")
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        async with TestClient(TestServer(_app(state))) as client:
            await _confirm(client, (await _count(client))["digest"])
        assert path.stat().st_mtime_ns == 1_700_000_000_000_000_000
        head = _head(path)
        assert head["model"] == "" and head["title"] == "Old chat"
        assert path.read_text(encoding="utf-8").splitlines()[1:] == [
            json.dumps({"role": "user", "content": "hi", "ts": "2026-08-01T00:00:01"})
        ]

    @pytest.mark.asyncio
    async def test_a_worker_and_a_crewmate_thread_keep_their_models(self, tmp_path):
        state = _make_state(tmp_path)
        worker = _open_chat(state, "chat-1-100", OLD, _created_by="dashboard:chat-9-100")
        thread = _open_chat(state, "chat-3-100", OLD, mode="member")
        closed_worker = _line(
            tmp_path, "chat-2-100", origin="user", model=OLD, created_by="dashboard:chat-9-100"
        )
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 0
            assert (await _confirm(client, shown["digest"]))["changed"] == 0
        assert (worker.model, thread.model) == (OLD, OLD)
        assert _head(closed_worker)["model"] == OLD

    @pytest.mark.asyncio
    async def test_a_running_chat_is_cleared_without_touching_its_session(self, tmp_path):
        """A turn in flight keeps the model it started on; nothing is reset or stopped."""
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD)
        chat._turn_admission_reserved = True
        async with TestClient(TestServer(_app(state))) as client:
            assert (await _confirm(client, (await _count(client))["digest"]))["changed"] == 1
        assert chat.model == ""
        state.sessions.reset.assert_not_called()
        state.sessions.remove.assert_not_awaited()
        state.sessions.discard_conversation.assert_not_awaited()


class TestAnOpenChatIsJudgedOnItsLineToo:
    """A History resume restores neither the creator nor the app; the line still names them."""

    @staticmethod
    async def _reopen(state, name: str):
        outcome = await resume_slot_from_history(state, name=name, history_key=f"dashboard:{name}")
        assert outcome.refusal is None and outcome.slot is not None
        return outcome.slot

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provenance", "slot_attr"),
        [
            ({"created_by": "dashboard:chat-9-100"}, "_created_by"),
            ({"app": "auto-research"}, "_app"),
        ],
        ids=["session-create-worker", "app-owned"],
    )
    async def test_a_chat_reopened_from_history_keeps_the_model_it_was_given(
        self, tmp_path, monkeypatch, provenance, slot_attr
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        name = "chat-1-100"
        # An archived line carries the close instant its close save stamped. Without
        # it the reopen compares the file's mtime with its own start, and a file
        # written a moment ago can read as closed after the reopen began.
        path = _line(
            tmp_path,
            name,
            origin="user",
            model=OLD,
            closed=True,
            closed_at=1_700_000_000.0,
            **provenance,
        )
        reopened = await self._reopen(state, name)
        assert state._slots[name] is reopened
        assert (getattr(reopened, slot_attr), reopened.model) == ("", OLD)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 0
            assert (await _confirm(client, shown["digest"]))["changed"] == 0
        assert reopened.model == OLD
        head = _head(path)
        assert head["model"] == OLD
        assert {key: head.get(key) for key in provenance} == provenance

    @pytest.mark.asyncio
    async def test_an_open_chat_whose_line_cannot_be_read_is_not_counted(self, tmp_path):
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = tmp_path / f"dashboard_{name}.jsonl"
        path.write_text("{not json\n", encoding="utf-8")
        chat = _open_chat(state, name, OLD)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 0
            assert (await _confirm(client, shown["digest"]))["changed"] == 0
        assert chat.model == OLD

    @pytest.mark.asyncio
    async def test_an_open_chat_with_no_transcript_yet_is_judged_on_its_slot(self, tmp_path):
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD)
        assert not (tmp_path / "dashboard_chat-1-100.jsonl").exists()
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            assert (await _confirm(client, shown["digest"]))["changed"] == 1
        assert chat.model == ""


class TestNothingChangesBehindTheCount:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "archive",
        [{}, {"closed": True}, {"closed": True, "closed_at": 1_700_000_000.0}],
        ids=["open-line", "legacy-archive", "older-archive"],
    )
    async def test_a_pending_archive_refuses_the_confirm(self, tmp_path, monkeypatch, archive):
        """A detached slot's archival save must finish before its model can be cleared."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD, **archive)
        before = path.read_bytes()
        chat = _open_chat(state, name, OLD)
        archiving, release = asyncio.Event(), asyncio.Event()
        save = chat_handlers.save_slot_off_loop

        async def _held_archive(*args, **kwargs):
            archiving.set()
            await finished(release.wait(), "release the archive")
            return await save(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _held_archive)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            closing = asyncio.create_task(_close_slot(state, chat, name))
            try:
                await finished(archiving.wait(), "reach the archival save")
                assert name not in state._slots
                resp = await client.post(PATH, json={"digest": shown["digest"]})
                assert resp.status == 409
                body = await resp.json()
                assert (body["code"], body["count"]) == ("chats_changed", 0)
                assert body["digest"] != shown["digest"]
                assert path.read_bytes() == before
                assert chat.model == OLD
            finally:
                release.set()
                await finished(closing, "finish archiving")
            assert _head(path)["closed"] is True
            assert _head(path)["model"] == OLD
            remaining = await _count(client)
            assert remaining["count"] == 1
            assert await _confirm(client, remaining["digest"]) == {"ok": True, "changed": 1}
        assert _head(path)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_counted_chat_with_a_pending_archive_is_skipped(self, tmp_path, monkeypatch):
        """Closing after the count does not let the detached slot overwrite a clear."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        before = path.read_bytes()
        chat = _open_chat(state, name, OLD)
        kept = await follow._kept_models(state)
        archiving, release = asyncio.Event(), asyncio.Event()
        save = chat_handlers.save_slot_off_loop

        async def _held_archive(*args, **kwargs):
            archiving.set()
            await finished(release.wait(), "release the archive")
            return await save(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _held_archive)
        closing = asyncio.create_task(_close_slot(state, chat, name))
        try:
            await finished(archiving.wait(), "reach the archival save")
            assert name not in state._slots
            assert await follow._clear(state, name, kept[name]) == follow.SKIPPED
            assert path.read_bytes() == before
            assert chat.model == OLD
        finally:
            release.set()
            await finished(closing, "finish archiving")
        assert _head(path)["closed"] is True
        assert _head(path)["model"] == OLD
        remaining = await follow._kept_models(state)
        assert set(remaining) == {name}
        assert remaining[name].model == OLD
        assert await follow._clear(state, name, remaining[name]) == follow.CHANGED
        assert _head(path)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_close_begun_before_the_count_is_left_for_the_next_press(self, tmp_path):
        """A close is recorded before its slot leaves the table, and saves after that."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        before = path.read_bytes()
        chat = _open_chat(state, name, OLD)
        note_slot_closed(state, name)
        kept = await follow._kept_models(state)
        assert set(kept) == {name}
        assert state._slots.pop(name) is chat
        assert await follow._clear(state, name, kept[name]) == follow.SKIPPED
        assert path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_closed_chat_is_not_counted_while_the_tab_restore_runs(self, tmp_path):
        """A tab the boot restore has not rebuilt yet would apply its stale line after a clear."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        state.restoring_open_slots = True
        assert await follow._kept_models(state) == {}
        state.restoring_open_slots = False
        kept = await follow._kept_models(state)
        assert set(kept) == {name}
        assert await follow._clear(state, name, kept[name]) == follow.CHANGED
        assert _head(path)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_tab_the_restore_could_not_read_is_left_alone(self, tmp_path):
        """An unreadable tab is still open; the next restore would rebuild the old model."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        kept = await follow._kept_models(state)
        assert set(kept) == {name}
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        state.unrestored_slot_keys = {name}
        assert await follow._kept_models(state) == {}
        assert await follow._clear(state, name, kept[name]) == follow.SKIPPED
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)

    @pytest.mark.asyncio
    async def test_a_close_in_the_same_clock_tick_does_not_stop_the_next_press(
        self, tmp_path, monkeypatch
    ):
        """A coarse clock can stamp the close and the next press alike; values decide."""
        ManualClock(start=1_800_000_000.0).install(monkeypatch, channel_slots)
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD)
        await finished(_close_slot(state, chat, name), "archive the chat")
        assert (_head(path)["closed_at"], _head(path)["model"]) == (1_800_000_000.0, OLD)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 1}
        assert _head(path)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_close_after_the_clock_stepped_back_is_still_pending(
        self, tmp_path, monkeypatch
    ):
        """A clock step can stamp a later close below the archived one; its save is still due."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(
            tmp_path, name, origin="user", model=OLD, closed=True, closed_at=1_800_000_000.0
        )
        before = path.read_bytes()
        kept = await follow._kept_models(state)
        assert set(kept) == {name}
        ManualClock(start=1_799_999_940.0).install(monkeypatch, channel_slots)
        note_slot_closed(state, name)
        assert await follow._kept_models(state) == {}
        assert await follow._clear(state, name, kept[name]) == follow.SKIPPED
        assert path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_close_during_the_rewrite_is_not_counted_as_changed(
        self, tmp_path, monkeypatch
    ):
        """A close during the off-loop write can still archive the model it held."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        kept = await follow._kept_models(state)
        update = state.conversation_log.update_metadata_if

        def _update_then_close(*args, **kwargs):
            written = update(*args, **kwargs)
            assert written
            note_slot_closed(state, name)
            return written

        monkeypatch.setattr(state.conversation_log, "update_metadata_if", _update_then_close)
        assert await follow._clear(state, name, kept[name]) == follow.SKIPPED
        assert _head(path)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_pick_between_count_and_confirm_refuses_the_confirm(self, tmp_path):
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD)
        other = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            chat.model = PICK
            resp = await client.post(PATH, json={"digest": shown["digest"]})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "chats_changed"
            assert body["count"] == 2 and body["digest"] != shown["digest"]
        assert chat.model == PICK
        assert _head(other)["model"] == OLD

    @pytest.mark.asyncio
    async def test_a_chat_picked_while_the_clear_runs_keeps_its_pick(self, tmp_path):
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD)
        path = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        kept = await follow._kept_models(state)
        chat.model = PICK
        path.write_text(path.read_text().replace(f'"model": "{OLD}"', f'"model": "{PICK}"'))
        assert await follow._clear(state, "chat-1-100", kept["chat-1-100"]) == follow.SKIPPED
        assert await follow._clear(state, "chat-2-100", kept["chat-2-100"]) == follow.SKIPPED
        assert chat.model == PICK
        assert _head(path)["model"] == PICK

    @pytest.mark.asyncio
    async def test_a_chat_closed_while_the_clear_waits_is_left_for_the_next_press(self, tmp_path):
        """Closing archives the kept model; a detached slot is not a successful clear."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD, jev_route=True)
        kept = await follow._kept_models(state)
        pick_gen = chat._model_pick_gen
        await chat._model_pick_lock.acquire()
        clearing = asyncio.create_task(follow._clear(state, name, kept[name]))
        try:
            await until_parked(chat._model_pick_lock)
            await finished(_close_slot(state, chat, name), "archive the chat")
            assert name not in state._slots
        finally:
            chat._model_pick_lock.release()
            outcome = await finished(clearing, "clear the archived chat")

        assert outcome == follow.SKIPPED
        assert (chat.model, chat.jev_route, chat._model_pick_gen) == (OLD, True, pick_gen)
        assert _head(path)["closed"] is True
        assert _head(path)["model"] == OLD
        remaining = await follow._kept_models(state)
        assert set(remaining) == set(kept)
        assert (remaining[name].model, remaining[name].stem) == (kept[name].model, kept[name].stem)
        assert follow._digest(remaining) == follow._digest(kept)
        assert await follow._clear(state, name, remaining[name]) == follow.CHANGED
        assert _head(path)["model"] == ""
        assert await follow._kept_models(state) == {}

    @pytest.mark.asyncio
    async def test_a_chat_reopened_while_the_clear_waits_keeps_its_model(self, tmp_path):
        """A new slot under the same name does not make the detached slot current."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        _line(tmp_path, name, origin="user", model=OLD)
        chat = _open_chat(state, name, OLD)
        kept = await follow._kept_models(state)
        pick_gen = chat._model_pick_gen
        await chat._model_pick_lock.acquire()
        clearing = asyncio.create_task(follow._clear(state, name, kept[name]))
        try:
            await until_parked(chat._model_pick_lock)
            await finished(_close_slot(state, chat, name), "archive before reopening")
            reopened = _open_chat(state, name, PICK)
            assert reopened is not chat
        finally:
            chat._model_pick_lock.release()
            outcome = await finished(clearing, "clear the replaced chat")

        assert outcome == follow.SKIPPED
        assert (chat.model, chat._model_pick_gen) == (OLD, pick_gen)
        assert state._slots[name] is reopened
        assert reopened.model == PICK

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [OLD, ""], ids=["kept-model", "already-follows"])
    async def test_a_chat_closed_after_the_rewrite_is_not_counted_as_changed(self, tmp_path, model):
        """A rewritten line cannot acknowledge a loaded slot that closed during the wait."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        kept = await follow._kept_models(state)
        opened = _open_chat(state, name, model)
        state._slots.pop(name)
        pick_gen = opened._model_pick_gen
        update = state.conversation_log.update_metadata_if

        def _update_then_open(*args, **kwargs):
            written = update(*args, **kwargs)
            state._slots[name] = opened
            return written

        with unittest.mock.patch.object(
            state.conversation_log, "update_metadata_if", side_effect=_update_then_open
        ) as rewrite:
            await opened._model_pick_lock.acquire()
            clearing = asyncio.create_task(follow._clear(state, name, kept[name]))
            try:
                await until_parked(opened._model_pick_lock)
                rewrite.side_effect = update
                assert _head(path)["model"] == ""
                await finished(_close_slot(state, opened, name), "archive the opened chat")
                assert name not in state._slots
            finally:
                opened._model_pick_lock.release()
                outcome = await finished(clearing, "clear after the rewrite and close")

        assert outcome == follow.SKIPPED
        assert (opened.model, opened._model_pick_gen) == (model, pick_gen)
        assert _head(path)["closed"] is True
        assert _head(path)["model"] == model

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "outcome"),
        [("", follow.CHANGED), (PICK, follow.SKIPPED)],
        ids=["already-follows", "new-pick"],
    )
    async def test_a_chat_opened_with_another_model_keeps_it_after_the_rewrite(
        self, tmp_path, model, outcome
    ):
        """The loaded slot decides whether the rewritten chat now follows the default."""
        state = _make_state(tmp_path)
        name = "chat-1-100"
        _line(tmp_path, name, origin="user", model=OLD)
        kept = await follow._kept_models(state)
        opened = _open_chat(state, name, model)
        state._slots.pop(name)
        pick_gen = opened._model_pick_gen
        update = state.conversation_log.update_metadata_if

        def _update_then_open(*args, **kwargs):
            written = update(*args, **kwargs)
            state._slots[name] = opened
            return written

        with unittest.mock.patch.object(
            state.conversation_log, "update_metadata_if", side_effect=_update_then_open
        ):
            assert await follow._clear(state, name, kept[name]) == outcome
        assert (opened.model, opened._model_pick_gen) == (model, pick_gen)

    @pytest.mark.asyncio
    async def test_a_chat_opened_while_its_line_is_rewritten_is_cleared_in_memory(self, tmp_path):
        """A resume that read the line first holds the old model; its save would write it back."""
        state = _make_state(tmp_path)
        _line(tmp_path, "chat-1-100", origin="user", model=OLD)
        kept = await follow._kept_models(state)
        opened = _open_chat(state, "chat-1-100", OLD)
        state._slots.pop("chat-1-100")
        update = state.conversation_log.update_metadata_if

        def _update_then_open(*args, **kwargs):
            written = update(*args, **kwargs)
            state._slots["chat-1-100"] = opened
            return written

        with unittest.mock.patch.object(
            state.conversation_log, "update_metadata_if", side_effect=_update_then_open
        ):
            assert await follow._clear(state, "chat-1-100", kept["chat-1-100"]) == follow.CHANGED
        assert opened.model == ""

    @pytest.mark.asyncio
    async def test_a_chat_being_opened_is_left_for_the_next_press(self, tmp_path):
        """A resume still building its slot may hold the old model it read; never count it."""
        state = _make_state(tmp_path)
        before = _line(tmp_path, "chat-1-100", origin="user", model=OLD)
        during = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        kept = await follow._kept_models(state)
        state._slots_under_construction.add("chat-1-100")
        assert await follow._clear(state, "chat-1-100", kept["chat-1-100"]) == follow.SKIPPED
        assert _head(before)["model"] == OLD
        update = state.conversation_log.update_metadata_if

        def _update_then_start_opening(*args, **kwargs):
            written = update(*args, **kwargs)
            state._slots_under_construction.add("chat-2-100")
            return written

        with unittest.mock.patch.object(
            state.conversation_log, "update_metadata_if", side_effect=_update_then_start_opening
        ):
            assert await follow._clear(state, "chat-2-100", kept["chat-2-100"]) == follow.SKIPPED
        # The line was rewritten, but the slot being built may hold what it read before:
        # not counted, and the next press counts it again if that slot saves the old model.
        assert _head(during)["model"] == ""

    @pytest.mark.asyncio
    async def test_a_line_that_cannot_be_saved_fails_the_confirm(self, tmp_path):
        """The answer never says done for a chat that still keeps its model."""
        state = _make_state(tmp_path)
        saved = _line(tmp_path, "chat-1-100", origin="user", model=OLD)
        stuck = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        chat = _open_chat(state, "chat-3-100", OLD)
        update = state.conversation_log.update_metadata_if

        def _disk_full_for_one(key, *args, **kwargs):
            if key == "dashboard_chat-2-100":
                raise OSError(28, "No space left on device")
            return update(key, *args, **kwargs)

        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            with unittest.mock.patch.object(
                state.conversation_log, "update_metadata_if", side_effect=_disk_full_for_one
            ):
                resp = await client.post(PATH, json={"digest": shown["digest"]})
            assert resp.status == 500
            body = await resp.json()
            assert (body["code"], body["changed"], body["failed"]) == ("chats_not_saved", 2, 1)
            assert (await _count(client))["count"] == 1
        assert (_head(saved)["model"], _head(stuck)["model"], chat.model) == ("", OLD, "")

    @pytest.mark.asyncio
    async def test_a_deferred_rewrite_fails_the_confirm_and_a_deleted_chat_is_skipped(
        self, tmp_path
    ):
        """A rewrite that never ran is not a skip; a chat deleted since the count is."""
        state = _make_state(tmp_path)
        deferred = _line(tmp_path, "chat-1-100", origin="user", model=OLD)
        deleted = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        saved = _line(tmp_path, "chat-3-100", origin="user", model=OLD)
        update = state.conversation_log.update_metadata_if

        def _defer_one_delete_one(key, *args, **kwargs):
            if key == "dashboard_chat-1-100":
                # What a read that fails under the write lock answers: no guard, no write.
                return False
            if key == "dashboard_chat-2-100":
                deleted.unlink()
            return update(key, *args, **kwargs)

        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 3
            with unittest.mock.patch.object(
                state.conversation_log, "update_metadata_if", side_effect=_defer_one_delete_one
            ):
                resp = await client.post(PATH, json={"digest": shown["digest"]})
            assert resp.status == 500
            body = await resp.json()
            assert (body["code"], body["changed"], body["failed"]) == ("chats_not_saved", 1, 1)
            assert (await _count(client))["count"] == 1
        assert (_head(deferred)["model"], _head(saved)["model"]) == (OLD, "")
        assert not deleted.exists()

    @pytest.mark.asyncio
    async def test_a_malformed_digest_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD)
        async with TestClient(TestServer(_app(state))) as client:
            for body in ({}, {"digest": "nope"}, {"digest": 7}, {"digest": "A" * 64}):
                resp = await client.post(PATH, json=body)
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_digest"
            resp = await client.post(PATH, data="{}", headers={"Content-Type": "text/plain"})
            assert resp.status == 415
        assert chat.model == OLD


def _throttle_fallback(slot, serving: str) -> None:
    """The sticky record a throttle swap leaves: *serving* stands in for the kept model."""
    slot._active_fallback_model = serving
    slot._fallback_primary_model = slot.model
    slot._fallback_slot_model = slot.model
    slot._fallback_pick_gen = slot._model_pick_gen
    slot._fallback_client_pick_epoch = 0


def _refusal_fallback(slot, serving: str) -> None:
    """The record a refusal retry leaves until the next turn moves the session back."""
    slot._refusal_fallback_primary = slot.model
    slot._refusal_fallback_candidate = serving
    slot._refusal_pick_gen = slot._model_pick_gen


_FALLBACKS = pytest.mark.parametrize(
    "record", [_throttle_fallback, _refusal_fallback], ids=["throttle", "refusal"]
)


class TestAChatAFallbackServesIsLeftAlone:
    """A clear bumps the pick generation, which a fallback's restore reads as a pick."""

    @pytest.mark.asyncio
    @_FALLBACKS
    async def test_it_is_neither_counted_nor_cleared(self, record, tmp_path):
        state = _make_state(tmp_path)
        path = _line(tmp_path, "chat-1-100", origin="user", model=GLOBAL_DEFAULT)
        chat = _open_chat(state, "chat-1-100", GLOBAL_DEFAULT)
        record(chat, PICK)
        pick_gen = chat._model_pick_gen
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 0
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 0}
        assert (chat.model, chat._model_pick_gen) == (GLOBAL_DEFAULT, pick_gen)
        assert _head(path)["model"] == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_its_session_returns_to_the_primary_and_a_later_press_clears_it(self, tmp_path):
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", GLOBAL_DEFAULT)
        _throttle_fallback(chat, PICK)
        provider = restore_tests.TestProbeFallbackRestoreSlotSeams._provider(served=PICK)
        async with TestClient(TestServer(_app(state))) as client:
            await _confirm(client, (await _count(client))["digest"])
            await chat_runner._probe_fallback_restore_for_slot(chat, provider)
            provider.set_model.assert_awaited_once_with(GLOBAL_DEFAULT)
            assert provider.served_model == GLOBAL_DEFAULT
            assert (chat._active_fallback_model, chat._fallback_primary_model) == ("", "")
            assert chat.model == GLOBAL_DEFAULT
            shown = await _count(client)
            assert shown["count"] == 1
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 1}
        assert chat.model == ""

    @pytest.mark.asyncio
    async def test_an_agent_switch_rolled_back_during_the_confirm_leaves_it_alone(self, tmp_path):
        """A switch hides the fallback record while it holds the session lock; the clear waits."""
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", GLOBAL_DEFAULT)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            _throttle_fallback(chat, PICK)
            record = (
                chat._active_fallback_model,
                chat._fallback_primary_model,
                chat._fallback_slot_model,
                chat._fallback_pick_gen,
            )
            pick_gen = chat._model_pick_gen
            switching = slot_switch_session_lock(effective_session_key(chat))
            async with switching:
                clear_slot_model_binding(chat)
                confirming = asyncio.create_task(
                    client.post(PATH, json={"digest": shown["digest"]})
                )
                await until_parked(switching)
                (
                    chat._active_fallback_model,
                    chat._fallback_primary_model,
                    chat._fallback_slot_model,
                    chat._fallback_pick_gen,
                ) = record
            resp = await finished(confirming, "confirm after the switch rolled back")
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "changed": 0}
        assert (chat.model, chat._model_pick_gen) == (GLOBAL_DEFAULT, pick_gen)

    @pytest.mark.asyncio
    @_FALLBACKS
    async def test_one_that_starts_after_the_count_refuses_the_confirm(self, record, tmp_path):
        state = _make_state(tmp_path)
        path = _line(tmp_path, "chat-1-100", origin="user", model=GLOBAL_DEFAULT)
        chat = _open_chat(state, "chat-1-100", GLOBAL_DEFAULT)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert shown["count"] == 1
            record(chat, PICK)
            pick_gen = chat._model_pick_gen
            resp = await client.post(PATH, json={"digest": shown["digest"]})
            assert resp.status == 409
            body = await resp.json()
        assert (body["code"], body["count"]) == ("chats_changed", 0)
        assert (chat.model, chat._model_pick_gen) == (GLOBAL_DEFAULT, pick_gen)
        assert _head(path)["model"] == GLOBAL_DEFAULT


class TestOwnerOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [{"X-Test-App": "some-app"}, {"X-Test-User": "someone-else"}],
        ids=["app-token", "not-the-owner"],
    )
    async def test_only_the_owner_counts_or_clears(self, headers, tmp_path):
        state = _make_state(tmp_path)
        state.owner_id = "owner-1"
        chat = _open_chat(state, "chat-1-100", OLD)
        path = _line(tmp_path, "chat-2-100", origin="user", model=OLD)
        owner = {"X-Test-User": "owner-1"}
        async with TestClient(TestServer(_app(state))) as client:
            digest = (await _count(client, **owner))["digest"]
            assert (await client.get(PATH, headers=headers)).status == 403
            resp = await client.post(PATH, json={"digest": digest}, headers=headers)
            assert resp.status == 403
        assert chat.model == OLD
        assert _head(path)["model"] == OLD

    @pytest.mark.asyncio
    async def test_the_real_table_answers_the_path_with_this_handler(self):
        """Ahead of ``/api/chat/slots/{slot}``, which would read it as a slot named so."""
        from kiro_crew.dashboard import routes

        app = web.Application()
        routes.register_all(app)
        for method in ("GET", "POST"):
            match = await app.router.resolve(make_mocked_request(method, PATH, app=app))
            assert match.handler is follow.api_chat_slots_follow_default


@pytest.fixture
def runner_config():
    """Serve one real config object to every ``KiroCrewConfig.load()`` in the turn."""
    patchers: list = []

    def _use(cfg: KiroCrewConfig) -> None:
        patcher = unittest.mock.patch.object(
            chat_runner.KiroCrewConfig, "load", unittest.mock.MagicMock(return_value=cfg)
        )
        patcher.start()
        patchers.append(patcher)

    yield _use
    for patcher in patchers:
        patcher.stop()


class TestTheNextTurn:
    """What a cleared chat runs next, through one real turn."""

    @staticmethod
    async def _clear_all(state) -> int:
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            return (await _confirm(client, shown["digest"]))["changed"]

    @staticmethod
    def _pinned(state, model: str):
        slot = _slot()
        slot.model = model
        state._slots[slot.key] = slot
        return slot

    @staticmethod
    def _live(state, client, stamped: str):
        """A live session already allocated on *stamped*, idle."""
        client.has_active_turn = unittest.mock.MagicMock(return_value=False)
        state.sessions.get_provider = unittest.mock.MagicMock(return_value=client)
        state.sessions.allocation_requested_model = unittest.mock.MagicMock(return_value=stamped)
        state.sessions.reset = unittest.mock.AsyncMock(return_value=True)

    @staticmethod
    def _resets(state) -> list:
        return [
            c for c in state.sessions.reset.await_args_list if c.args[0] == "dashboard:chat-cov-1"
        ]

    @pytest.mark.asyncio
    async def test_a_failed_open_chat_save_restores_the_model_and_is_retryable(
        self, tmp_path, monkeypatch, runner_config
    ):
        cfg = _load_config(
            tmp_path, {"agent": {"model": "auto", "provider": "acp"}, "default_agent": "kirocrew"}
        )
        runner_config(cfg)
        state = _make_state(tmp_path)
        self._live(state, unittest.mock.MagicMock(), OLD)
        name = "chat-1-100"
        path = _line(tmp_path, name, origin="user", model=OLD)
        slot = _open_chat(state, name, OLD, jev_route=True)
        pick_gen = slot._model_pick_gen
        assert not slot._pending_reset_history_key
        save = chat_persistence._save_slot_to_history

        def _disk_full(*args, **kwargs):
            assert slot._pending_reset_history_key == effective_session_key(slot)
            raise OSError("No space left on device")

        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _disk_full)
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            assert (shown["count"], shown["default_model"]) == (1, "")
            resp = await client.post(PATH, json={"digest": shown["digest"]})
            assert resp.status == 500
            body = await resp.json()
            assert (body["code"], body["changed"], body["failed"]) == ("chats_not_saved", 0, 1)
            assert (slot.model, slot.jev_route, slot._model_pick_gen) == (OLD, True, pick_gen + 1)
            assert slot._pending_reset_history_key == effective_session_key(slot)
            assert slot._dirty
            assert _head(path)["model"] == OLD
            remaining = await _count(client)
            assert remaining["count"] == 1
            assert set(await follow._kept_models(state)) == {name}
            monkeypatch.setattr(chat_persistence, "_save_slot_to_history", save)
            assert await _confirm(client, remaining["digest"]) == {"ok": True, "changed": 1}
            assert _head(path)["model"] == ""
        assert (slot.model, slot.jev_route, slot._model_pick_gen) == ("", False, pick_gen + 2)
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cleared_chat_runs_the_default_and_its_chip_reads_inherited(
        self, tmp_path, runner_config
    ):
        runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = self._pinned(state, OLD)

        assert await self._clear_all(state) == 1
        payload = next(row for row in state.serialize_slots() if row["key"] == slot.key)
        assert payload["model"] == ""
        await _drive(state, slot)

        assert _requested(state) is None
        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_a_live_session_on_the_old_model_restarts_on_the_default_next_turn(
        self, tmp_path, runner_config
    ):
        runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        self._live(state, client, OLD)
        slot = self._pinned(state, OLD)

        await self._clear_all(state)
        state.sessions.reset.assert_not_awaited()
        await _drive(state, slot)

        resets = self._resets(state)
        assert resets and all(c.kwargs.get("skip_if_busy") is True for c in resets)
        assert _session_model(state) == GLOBAL_DEFAULT

    @pytest.mark.asyncio
    async def test_a_live_in_place_pick_restarts_on_the_default_after_clear(
        self, tmp_path, runner_config
    ):
        runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        self._live(state, client, GLOBAL_DEFAULT)
        slot = self._pinned(state, OLD)

        assert await self._clear_all(state) == 1
        state.sessions.reset.assert_not_awaited()
        await _drive(state, slot)

        resets = self._resets(state)
        assert resets and all(c.kwargs.get("skip_if_busy") is True for c in resets)
        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_a_kept_model_equal_to_the_default_is_cleared_and_its_session_kept(
        self, tmp_path, runner_config
    ):
        runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        self._live(state, client, GLOBAL_DEFAULT)
        slot = self._pinned(state, GLOBAL_DEFAULT)

        assert await self._clear_all(state) == 1
        await _drive(state, slot)

        assert self._resets(state) == []
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_on_an_auto_default_a_cleared_chat_asks_for_no_model(
        self, tmp_path, runner_config
    ):
        cfg = _load_config(
            tmp_path, {"agent": {"model": "auto", "provider": "acp"}, "default_agent": "kirocrew"}
        )
        runner_config(cfg)
        state, _client = _turn_state(tmp_path)
        slot = self._pinned(state, OLD)

        await self._clear_all(state)
        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        ):
            await _drive(state, slot)

        assert _requested(state) is None
        assert _session_model(state) is None
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_on_an_auto_default_a_live_session_on_the_old_model_restarts_next_turn(
        self, tmp_path, runner_config
    ):
        """Every tier defers on Auto, so the turn's stamp check has no default to compare."""
        cfg = _load_config(
            tmp_path, {"agent": {"model": "auto", "provider": "acp"}, "default_agent": "kirocrew"}
        )
        runner_config(cfg)
        state, client = _turn_state(tmp_path)
        self._live(state, client, OLD)
        slot = self._pinned(state, OLD)

        assert await self._clear_all(state) == 1
        state.sessions.reset.assert_not_awaited()
        with unittest.mock.patch.object(
            KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")
        ):
            await _drive(state, slot)

        resets = self._resets(state)
        assert resets and all(c.kwargs.get("skip_if_busy") is True for c in resets)
        assert _requested(state) is None
        assert _session_model(state) is None
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_a_chat_on_a_crewmate_that_pins_a_model_keeps_its_model(
        self, tmp_path, runner_config
    ):
        """The crewmate's pin outranks the default, and its next turn would write it back."""
        cfg = _config(tmp_path, crew_model=CREW_PIN)
        runner_config(cfg)
        _grant_researcher(cfg)
        state, _client = _turn_state(tmp_path)
        slot = self._pinned(state, OLD)
        slot.agent = "researcher"
        closed = _line(tmp_path, "chat-2-100", origin="user", model=OLD, agent="researcher")

        assert await self._clear_all(state) == 0

        assert (slot.model, _head(closed)["model"]) == (OLD, OLD)
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["agents"]["researcher"]["model"] == CREW_PIN

    @pytest.mark.asyncio
    async def test_a_chat_on_a_crewmate_with_no_pin_follows_the_default(
        self, tmp_path, runner_config
    ):
        cfg = _config(tmp_path)
        runner_config(cfg)
        _grant_researcher(cfg)
        state, _client = _turn_state(tmp_path)
        slot = self._pinned(state, OLD)
        slot.agent = "researcher"

        assert await self._clear_all(state) == 1
        await _drive(state, slot)

        assert _session_model(state) == GLOBAL_DEFAULT
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_a_model_picked_after_the_action_is_kept(self, tmp_path, runner_config):
        runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = self._pinned(state, OLD)

        await self._clear_all(state)
        slot.model = PICK
        slot._model_pick_gen += 1
        await _drive(state, slot)

        assert _session_model(state) == PICK
        assert slot.model == PICK


def _closed_at(base: Path, name: str, mtime_s: int, **meta) -> Path:
    """A closed chat whose file was last written at *mtime_s*: Older Sessions order."""
    path = _line(base, name, **meta)
    os.utime(path, ns=(mtime_s * 1_000_000_000, mtime_s * 1_000_000_000))
    return path


class TestWhatTheCountShows:
    """The confirm names where the chats land, which models they leave, and which chats."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("saved", "shown"),
        [("auto", ""), ("", ""), (GLOBAL_DEFAULT, GLOBAL_DEFAULT)],
        ids=["auto", "unset", "a-model"],
    )
    async def test_it_names_the_saved_default_model(self, tmp_path, runner_config, saved, shown):
        runner_config(_config(tmp_path, default=saved))
        state = _make_state(tmp_path)
        _open_chat(state, "chat-1-100", OLD)
        async with TestClient(TestServer(_app(state))) as client:
            body = await _count(client)
        assert (body["count"], body["default_model"]) == (1, shown)

    @pytest.mark.asyncio
    async def test_it_breaks_the_count_down_by_kept_model_most_first(self, tmp_path):
        state = _make_state(tmp_path)
        _open_chat(state, "chat-1-100", PICK)
        _open_chat(state, "chat-2-100", "auto")
        _line(tmp_path, "chat-3-100", origin="user", model=PICK)
        _line(tmp_path, "chat-4-100", origin="user", model=OLD)
        _line(tmp_path, "chat-5-100", origin="user", model=OLD)
        _line(tmp_path, "chat-6-100", origin="cron", model="auto")
        async with TestClient(TestServer(_app(state))) as client:
            body = await _count(client)
        assert body["count"] == 5
        assert body["models"] == [
            {"model": OLD, "count": 2},
            {"model": PICK, "count": 2},
            {"model": "auto", "count": 1},
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("other_model", "label_count"),
        [(OLD, 2), (_OTHER_FAKE_KEY, 1)],
        ids=["normal-model", "same-redacted-label"],
    )
    async def test_kept_model_labels_are_redacted_but_raw_models_are_cleared(
        self, tmp_path, other_model, label_count
    ):
        secret = _FAKE_KEY
        state = _make_state(tmp_path)
        paths = [
            _line(tmp_path, "chat-1-100", origin="user", model=secret),
            _line(tmp_path, "chat-2-100", origin="user", model=other_model),
        ]
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            labels = [row["model"] for row in shown["models"]]
            assert shown["count"] == 2
            assert all(secret[:8] not in label for label in labels)
            assert len(labels) == len(set(labels)) == label_count
            assert sum(row["count"] for row in shown["models"]) == shown["count"]
            if other_model == OLD:
                assert OLD in labels
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 2}
        assert [_head(path)["model"] for path in paths] == ["", ""]

    @pytest.mark.asyncio
    async def test_it_shows_the_first_five_counted_chats_open_ones_first(self, tmp_path):
        state = _make_state(tmp_path)
        _open_chat(state, "chat-1-100", OLD, title="Open one")
        _open_chat(
            state, "chat-2-100", OLD, title="Worker chat", _created_by="dashboard:chat-1-100"
        )
        _open_chat(state, "chat-3-100", OLD)
        _closed_at(tmp_path, "chat-4-100", 1_700_000_005, origin="user", model=OLD, title="Newest")
        _closed_at(
            tmp_path,
            "chat-5-100",
            1_700_000_004,
            origin="user",
            model=OLD,
            title="A",
            mode="member",
        )
        _closed_at(tmp_path, "chat-6-100", 1_700_000_003, origin="user", model=OLD)
        _closed_at(
            tmp_path, "chat-7-100", 1_700_000_002, origin="user", model=OLD, title="chat-7-100"
        )
        _closed_at(tmp_path, "chat-8-100", 1_700_000_001, origin="user", model=OLD, title="Oldest")
        _closed_at(
            tmp_path, "chat-9-100", 1_700_000_000, origin="app", model=OLD, title="App session"
        )
        async with TestClient(TestServer(_app(state))) as client:
            body = await _count(client)
        assert body["count"] == 6
        assert body["titles"] == ["Open one", "", "Newest", "", ""]

    @pytest.mark.asyncio
    async def test_no_title_of_a_chat_left_alone_is_shown(self, tmp_path):
        state = _make_state(tmp_path)
        _open_chat(state, "chat-1-100", OLD, title="Worker", _created_by="dashboard:chat-9-100")
        _open_chat(state, "chat-2-100", OLD, title="App", _app="auto-research")
        _open_chat(state, "chat-3-100", OLD, title="Thread", mode="member")
        _line(
            tmp_path, "chat-4-100", origin="user", model=OLD, title="Closed worker", created_by="x"
        )
        _line(tmp_path, "chat-5-100", origin="user", model=OLD, title="Mine")
        async with TestClient(TestServer(_app(state))) as client:
            body = await _count(client)
        assert (body["count"], body["titles"]) == (1, ["Mine"])

    @pytest.mark.asyncio
    async def test_a_credential_in_a_title_is_redacted_before_the_cap(self, tmp_path):
        """Cut first, and a key straddling the cap would leak as a stub the redactor misses."""
        secret = _FAKE_KEY
        state = _make_state(tmp_path)
        _open_chat(state, "chat-1-100", OLD, title=f"key {secret} here")
        # Older Sessions order is newest first; fixed times keep it off the clock's tick.
        _closed_at(
            tmp_path,
            "chat-2-100",
            1_700_000_001,
            origin="user",
            model=OLD,
            title="x" * 190 + f" {secret}",
        )
        _closed_at(tmp_path, "chat-3-100", 1_700_000_000, origin="user", model=OLD, title="y" * 300)
        async with TestClient(TestServer(_app(state))) as client:
            titles = (await _count(client))["titles"]
        assert len(titles) == 3
        assert all(secret[:8] not in title for title in titles)
        assert titles[0].startswith("key ") and titles[0].endswith(" here")
        assert titles[1].startswith("x" * 190)
        assert titles[2] == "y" * 200

    @pytest.mark.asyncio
    async def test_the_titles_and_breakdown_stay_out_of_the_digest(self, tmp_path):
        """A rename between the count and the confirm is not a change to the counted set."""
        state = _make_state(tmp_path)
        chat = _open_chat(state, "chat-1-100", OLD, title="Before")
        async with TestClient(TestServer(_app(state))) as client:
            shown = await _count(client)
            chat.title = "After"
            again = await _count(client)
            assert again["titles"] == ["After"]
            assert again["digest"] == shown["digest"]
            assert await _confirm(client, shown["digest"]) == {"ok": True, "changed": 1}
