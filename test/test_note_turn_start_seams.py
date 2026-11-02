"""A held ``/note`` reaches the next turn whichever path starts it.

A note posted during a running turn is held and written at a seam: the queue
drain, the queue cycle's end, or -- on a slot no turn owns -- the next live
``user`` row, which flushes the hold above itself. The last seam is what
delivers a hold a gateway restart restored onto an idle slot; without it the
next send ran without the note's context and the next immediate note landed
above the older, already-acknowledged one.

The turn-start scan at the bottom enumerates every function that starts a turn
(calls ``spawn_guarded_turn``) and requires each to be accounted for, so a new
dispatch path that would skip the hold turns this file red.
"""

from __future__ import annotations

import ast
import copy
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway_appkit_endpoints_helpers import _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_runner import drain_pending_context
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _restored_hold(slot: _ChatSlot, content: str, context: str | None = None) -> None:
    """Seat a hold the way the restore paths do: on a slot no turn owns."""
    slot._deferred_notes = [
        {
            "id": "a1b2c3d4e5f6",
            "content": content,
            "cls": "reconcile-note",
            "context": (
                None
                if context is None
                else {"content": context, "source": "board-sync", "ephemeral": True}
            ),
            "source": "",
            "session": effective_session_key(slot),
        }
    ]


async def _noop_title(*_args, **_kwargs) -> None:
    return None


@asynccontextmanager
async def _note_client(state: DashboardState):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as client:
        yield client


class TestRestoredHoldOnAnIdleSlot:
    @pytest.mark.asyncio
    async def test_an_immediate_note_lands_below_the_older_held_one(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot
        _restored_hold(slot, "older held note", context="older context")
        assert not slot.running

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "newer note", "source": "board-sync"},
            )
            assert resp.status == 200
            assert (await resp.json())["appended"] is True

        assert [m["content"] for m in slot.messages] == ["older held note", "newer note"]
        assert slot._deferred_notes == []
        assert [e["content"] for e in slot._pending_context] == ["older context", "newer note"]

    @pytest.mark.asyncio
    async def test_an_sse_send_streams_the_note_it_delivered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The send's stale-frame drain runs before the row that delivers the note."""
        from chat_test_helpers import _make_app
        from chat_test_helpers import _make_state as _make_chat_state

        async def _turn(_state, s, _message, **_kwargs):
            s.append("assistant", "the reply")
            s.append("done", "", "done")

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", _turn)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", _noop_title)
        state = _make_chat_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _restored_hold(slot, "held note")
        slot.append("assistant", "a stale frame from an earlier turn")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi"})
            stream = await resp.text()

        assert resp.status == 200
        assert "held note" in stream and "the reply" in stream
        assert stream.index("held note") < stream.index("the reply")
        assert "stale frame" not in stream

    def test_the_next_user_row_delivers_the_hold_above_itself(self):
        slot = _ChatSlot("s1")
        _restored_hold(slot, "held note", context="held context")

        slot.append("user", "what changed?", "msg msg-u")

        assert [(m["role"], m["content"]) for m in slot.messages] == [
            ("inject", "held note"),
            ("user", "what changed?"),
        ]
        assert slot._deferred_notes == []
        # The context half reaches the turn this row starts, not the one after.
        assert "held context" in drain_pending_context(slot)

    def test_a_user_row_on_a_running_slot_keeps_the_hold(self):
        """A steer is a user row too; it must not pull the note into the turn."""
        slot = _ChatSlot("s1")
        _restored_hold(slot, "held note")
        slot._turn_admission_reserved = True
        assert slot.running

        slot.append("user", "steer", "msg msg-u")

        assert [m["content"] for m in slot.messages] == ["steer"]
        assert len(slot._deferred_notes) == 1

    def test_the_queue_drain_opt_out_and_replay_appends_keep_the_hold(self):
        slot = _ChatSlot("s1")
        _restored_hold(slot, "held note")

        slot.append("user", "drained", "msg msg-u", flush_held_notes=False)
        slot.append("user", "replayed", "msg msg-u", broadcast=False)
        slot.append("nudge", "loop wake", "msg msg-nudge")

        assert [m["content"] for m in slot.messages] == ["drained", "replayed", "loop wake"]
        assert len(slot._deferred_notes) == 1

    def test_a_failed_flush_does_not_cost_the_user_row(self, monkeypatch: pytest.MonkeyPatch):
        slot = _ChatSlot("s1")
        _restored_hold(slot, "held note")

        def _boom(self_slot: _ChatSlot) -> int:
            raise RuntimeError("flush failed")

        monkeypatch.setattr(_ChatSlot, "flush_deferred_notes", _boom)
        slot.append("user", "still sent", "msg msg-u")

        assert [m["content"] for m in slot.messages] == ["still sent"]


class TestScratchSlotHold:
    """Rewind and edit-resend deliver the hold on a ``copy.copy`` scratch slot."""

    def test_a_refused_scratch_delivery_leaves_the_live_hold_whole(self):
        from kiro_crew.dashboard.slot_buffers import sever_note_hold

        live = _ChatSlot("s1")
        _restored_hold(live, "held note", context="held context")
        scratch = copy.copy(live)
        scratch.messages = []

        sever_note_hold(live, scratch)
        scratch.append("user", "edited", "msg msg-u")

        assert [m["content"] for m in scratch.messages] == ["held note", "edited"]
        # Nothing reached the live slot: not the hold, its context, or the note dict.
        assert [n["content"] for n in live._deferred_notes] == ["held note"]
        assert live._deferred_notes[0]["context"]["content"] == "held context"
        assert live._pending_context == []

    def test_adopt_retires_only_what_the_scratch_delivered(self):
        from kiro_crew.dashboard.slot_buffers import adopt_note_hold, sever_note_hold

        live = _ChatSlot("s1")
        _restored_hold(live, "held note", context="held context")
        scratch = copy.copy(live)
        scratch.messages = []
        hold = sever_note_hold(live, scratch)
        scratch.append("user", "edited", "msg msg-u")
        # A note posted to the live slot while the edit's boundaries were pending.
        arrived = {**live._deferred_notes[0], "id": "f6e5d4c3b2a1", "content": "later note"}
        arrived["context"] = None
        live._deferred_notes.append(arrived)

        assert adopt_note_hold(live, scratch, hold, logger=logging.getLogger(__name__)) == 1

        assert live._deferred_notes == [arrived]
        assert "held context" in drain_pending_context(live)


# ---------------------------------------------------------------------------
# Turn-start seams
# ---------------------------------------------------------------------------

_TURN_START = "spawn_guarded_turn"

# Turn starts that deliberately run WITHOUT a person's held note: a note is owed
# to the next user turn, and each of these is the gateway's own. The hold stays
# for the queue cycle that follows them.
_AUTOMATIC_TURN_STARTS = {
    ("queue_cycle.py", "_run_pending_synthesis"): "sub-agent synthesis",
    ("mate_welcome.py", "dispatch_hidden_turn"): "gateway-authored welcome / check-in",
    ("nudge_dashboard_fire.py", "_fire_dashboard_nudge"): "monitor loop wake",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _is_user_row_append(node: ast.Call) -> bool:
    """``<slot>.append("user", ...)`` -- the row whose append flushes the hold."""
    if _call_name(node) != "append" or not isinstance(node.func, ast.Attribute):
        return False
    first = node.args[0] if node.args else None
    if first is None:
        first = next((kw.value for kw in node.keywords if kw.arg == "role"), None)
    return isinstance(first, ast.Constant) and first.value == "user"


def _turn_start_seams(source: str) -> list[tuple[str, str]]:
    """Classify each top-level function (or method) that starts a turn.

    ``flushes``: it calls ``flush_deferred_notes`` above its first turn start.
    ``user_row``: it appends a live ``user`` row above it, which flushes.
    ``unaccounted``: neither -- a held note would miss this turn.
    """
    tree = ast.parse(source)
    roots: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            roots.extend(
                n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots.append(node)
    seams: list[tuple[str, str]] = []
    for fn in roots:
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        starts = [n.lineno for n in calls if _call_name(n) == _TURN_START]
        if not starts:
            continue
        first = min(starts)
        if any(_call_name(n) == "flush_deferred_notes" and n.lineno < first for n in calls):
            kind = "flushes"
        elif any(_is_user_row_append(n) and n.lineno < first for n in calls):
            kind = "user_row"
        else:
            kind = "unaccounted"
        seams.append((fn.name, kind))
    return seams


def _package_turn_start_seams() -> list[tuple[str, str, str]]:
    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    found: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "turn_dispatch.py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"{_TURN_START}(" not in source:
            continue
        for name, kind in _turn_start_seams(source):
            found.append((path.name, name, kind))
    return found


class TestTurnStartSeams:
    def test_every_turn_start_delivers_or_is_a_listed_automatic_turn(self):
        """A NEW turn-start path that neither flushes nor writes a user row turns red."""
        found = _package_turn_start_seams()
        # Positive control: an empty scan would pass vacuously.
        assert found, "scan found no turn start; the scan itself is broken"
        offenders = [
            (mod, fn)
            for mod, fn, kind in found
            if kind == "unaccounted" and (mod, fn) not in _AUTOMATIC_TURN_STARTS
        ]
        assert not offenders, f"turn starts that skip the held-note hold: {offenders}"

    def test_the_automatic_allowlist_is_not_stale(self):
        unaccounted = {
            (mod, fn) for mod, fn, kind in _package_turn_start_seams() if kind == "unaccounted"
        }
        stale = set(_AUTOMATIC_TURN_STARTS) - unaccounted
        assert not stale, f"allowlisted turn starts that no longer exist: {stale}"

    def test_the_person_send_path_is_a_user_row_seam(self):
        kinds = {(mod, fn): kind for mod, fn, kind in _package_turn_start_seams()}
        assert kinds[("chat_send.py", "_api_chat_send")] == "user_row"

    def test_the_scan_can_actually_fail(self):
        """Negative controls, one per way a turn start breaks."""
        missing = (
            "def _start(state, slot):\n"
            "    slot.append('inject', 'x')\n"
            "    spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
        )
        assert _turn_start_seams(missing) == [("_start", "unaccounted")]

        late = (
            "def _start(state, slot):\n"
            "    spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
            "    slot.flush_deferred_notes()\n"
            "    slot.append('user', 'x')\n"
        )
        assert _turn_start_seams(late) == [("_start", "unaccounted")]

        flushes = (
            "class S:\n"
            "    def start(self, state, slot):\n"
            "        slot.flush_deferred_notes()\n"
            "        spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
        )
        assert _turn_start_seams(flushes) == [("start", "flushes")]

        user_row = (
            "async def _start(state, slot):\n"
            "    slot.append(role='user', content='x')\n"
            "    spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
        )
        assert _turn_start_seams(user_row) == [("_start", "user_row")]
