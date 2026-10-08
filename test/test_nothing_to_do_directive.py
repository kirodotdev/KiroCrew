"""``nothing_to_do``: the deliberate quiet end of a turn, and the structured
terminal-turn signal it rides on.

The turn-end contract: after its tool calls a turn ends with a closing text or
with ``nothing_to_do`` — never by stopping bare after an ordinary tool. These
tests pin the three layers that make the quiet end real:

* the MCP tool is a stateless directive (marker + optional ``note``);
* the applier returns a structured ``DirectiveOutcome`` whose ``ends_turn`` is
  set only where the effect actually landed — a shown question card, a recorded
  quiet end — and never derived from the outcome prose; a refusal or a dropped
  card leaves it False so the normal empty-reply handling stays armed;
* both consumers honour the signal: the dashboard runner skips the
  empty-response ladder (no notice card, no synthetic continuation) and the
  channel driver owes the thread no empty-turn notice.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard import session_directive_apply as sda
from kiro_crew.dashboard.chat_turn.directives import _DIRECTIVE_NOT_APPLIED_OUTCOMES
from kiro_crew.dashboard.chat_utils import EMPTY_TURN_NOTICE_KIND
from kiro_crew.mcp_tools import control
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.validation import MAX_SHORT_STRING

pytestmark = pytest.mark.xdist_group("nothing_to_do_directive")


# ── the tool ─────────────────────────────────────────────────────────────────


class TestNothingToDoTool:
    def test_is_a_directive_tool_on_the_core_server(self):
        assert "nothing_to_do" in session_directive.DIRECTIVE_TOOLS
        assert "nothing_to_do" in control.HANDLERS
        assert any(t["name"] == "nothing_to_do" for t in control.schemas())

    def test_returns_a_decodable_marker_with_the_note(self):
        out = control.nothing_to_do("nothing_to_do", {"note": "patrol: no new activity"})
        assert session_directive.decode(out, "nothing_to_do") == {"note": "patrol: no new activity"}
        # The human line tells the model the turn is OVER, and says the quiet
        # step is the consumer's to record — never that it already happened.
        human = session_directive.strip_marker(out)
        assert "Write nothing and call nothing" in human
        assert "requested" in human.lower()

    def test_no_note_encodes_an_empty_payload(self):
        out = control.nothing_to_do("nothing_to_do", {})
        assert session_directive.decode(out, "nothing_to_do") == {}

    def test_an_oversized_note_is_clamped_not_refused(self):
        """A long note must not cost the quiet end itself (same rule as
        ``autonudge_stop``'s reason): clamp, keep the directive."""
        out = control.nothing_to_do("nothing_to_do", {"note": "x" * (MAX_SHORT_STRING + 50)})
        args = session_directive.decode(out, "nothing_to_do")
        assert args is not None
        assert 0 < len(args["note"]) <= MAX_SHORT_STRING

    def test_terminal_set_is_a_subset_of_the_directive_tools(self):
        assert sda.TERMINAL_DIRECTIVES <= session_directive.DIRECTIVE_TOOLS
        assert sda.TERMINAL_DIRECTIVES == {"nothing_to_do"}

    def test_every_directive_has_a_not_applied_outcome_line(self):
        """The runner's dropped-effect row names what did not happen for EVERY
        directive, the new one included, or it falls to the generic fallback."""
        assert set(_DIRECTIVE_NOT_APPLIED_OUTCOMES) == set(session_directive.DIRECTIVE_TOOLS)


# ── the frontend mirror ──────────────────────────────────────────────────────


class TestFrontendMirrorStaysInSync:
    def test_quiet_end_identity_constants_match_the_selectors_mirror(self):
        """``is_quiet_end_row`` and ``isQuietEndRow`` must name the same tool on
        the same server, or a rename on one side silently brings the Resume
        button back on every quiet turn. Read from the TypeScript source, so
        the pin fails on the side that drifted."""
        from kiro_crew.dashboard import state as st

        src = (
            Path(__file__).resolve().parents[1]
            / "website"
            / "src"
            / "store"
            / "chat"
            / "selectors.ts"
        ).read_text(encoding="utf-8")
        tool = re.search(r"export const QUIET_END_TOOL = '([^']+)'", src)
        server = re.search(r"export const QUIET_END_SERVER = '([^']+)'", src)
        assert tool and server, "the selectors mirror lost its QUIET_END_* constants"
        assert tool.group(1) == st.QUIET_END_TOOL
        assert server.group(1) == st.QUIET_END_SERVER
        assert "meta?.ends_turn === true" in src, "the mirror no longer requires ends_turn"
        assert st.QUIET_END_SERVER == session_directive.CORE_MCP_SERVER


# ── the applier: structured terminal signal ──────────────────────────────────


def _apply(kind, args, *, slot=None, state=None, session_key="chat-1", **kw):
    return asyncio.run(
        sda.apply_session_directive_outcome(
            state if state is not None else MagicMock(),
            slot,
            session_key,
            kind,
            args,
            **kw,
        )
    )


class TestApplierTerminalSignal:
    def test_nothing_to_do_ends_a_wake_turn_and_records_the_note(self):
        out = _apply(
            "nothing_to_do",
            {"note": "patrol: no new activity"},
            slot=MagicMock(),
            producer_is_self_wake=True,
        )
        assert isinstance(out, sda.DirectiveOutcome)
        assert out.ends_turn is True
        assert out.text.startswith(sda.QUIET_END_OUTCOME_PREFIX)
        assert "patrol: no new activity" in out.text

    def test_nothing_to_do_without_a_note_is_just_the_quiet_line(self):
        out = _apply("nothing_to_do", {}, slot=MagicMock(), producer_is_self_wake=True)
        assert out.ends_turn is True
        assert out.text == sda.QUIET_END_OUTCOME_PREFIX

    def test_nothing_to_do_ends_a_headless_turn(self):
        """A cron, crew or app injection (neither user-facing nor a channel) is
        a turn nobody is waiting on: the quiet end applies with no slot or
        surface gate."""
        out = _apply(
            "nothing_to_do",
            {},
            slot=MagicMock(),
            session_key="cron:job-1",
            producer_is_user_facing=False,
        )
        assert out.ends_turn is True

    def test_a_turn_a_person_opened_is_refused(self):
        """THE gate: a person's message owes a reply. The refusal leaves
        ``ends_turn`` False, so the runner's recovery, the Resume control and
        the channel notice all run as they did before the directive existed."""
        out = _apply(
            "nothing_to_do",
            {"note": "nothing"},
            slot=MagicMock(),
            producer_is_user_facing=True,
        )
        assert out.ends_turn is False
        assert out.text == sda.QUIET_END_REFUSED_USER_TURN
        assert out.text.startswith("Error:")

    def test_a_channel_turn_is_refused_until_it_carries_wake_provenance(self):
        """The channel driver cannot yet tell a human's message from a loop's
        wake, so the conservative answer is the pre-existing notice, never
        silence."""
        out = _apply(
            "nothing_to_do", {}, slot=None, session_key="slack:C1:t1", producer_is_channel=True
        )
        assert out.ends_turn is False
        assert out.text == sda.QUIET_END_REFUSED_USER_TURN

    def test_a_self_wake_on_a_user_facing_slot_is_a_wake(self):
        """A member's loop firing on the member's own slot carries both marks;
        the wake wins, because nobody typed this turn."""
        out = _apply(
            "nothing_to_do",
            {},
            slot=MagicMock(),
            producer_is_user_facing=True,
            producer_is_self_wake=True,
        )
        assert out.ends_turn is True

    def test_ask_question_is_not_a_directive_and_never_ends_the_turn(self):
        """``ask_question`` blocks and returns the answers as its own tool
        result, so it is not a session directive: the turn that asked continues
        with them. An ``ask_question`` marker reaching the applier is unknown and
        cannot end the turn quietly."""
        assert "ask_question" not in session_directive.DIRECTIVE_TOOLS
        out = _apply("ask_question", {"questions": []}, slot=MagicMock())
        assert out.ends_turn is False
        assert out.text.startswith("Error:")

    def test_an_error_text_never_ends_the_turn_whatever_the_applier_claimed(self):
        out = sda._outcome(sda.DirectiveOutcome("Error: boom", ends_turn=True))
        assert out.ends_turn is False
        assert sda._outcome("plain text").ends_turn is False

    def test_string_entry_returns_the_text_only(self):
        text = asyncio.run(
            sda.apply_session_directive(
                MagicMock(),
                MagicMock(),
                "chat-1",
                "nothing_to_do",
                {},
                producer_is_self_wake=True,
            )
        )
        assert text == sda.QUIET_END_OUTCOME_PREFIX

    def test_unknown_kind_is_a_structured_error(self):
        out = _apply("no_such_directive", {}, slot=MagicMock())
        assert out.ends_turn is False
        assert out.text.startswith("Error:")


# ── the channel driver ───────────────────────────────────────────────────────


class _NullRenderer(Renderer):
    def __init__(self):
        super().__init__(TransportCapabilities())
        self.done: list[tuple[str, str]] = []

    async def on_text_chunk(self, text):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_prompt_choice(self, options, request_id):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_steer_consumed(self, summary=""):
        pass

    async def on_done(self, stop_reason=""):
        self.done.append(("done", stop_reason))


class _ScriptedProvider:
    def __init__(self, events):
        self._events = events

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        pass

    async def reject_tool(self, request_id):
        pass


def _core_call(tool: str, tcid: str = "tc-1") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tcid,
        title=tool,
        tool_name=tool,
        mcp_server_name=session_directive.CORE_MCP_SERVER,
    )


def _result(text: str, tcid: str = "tc-1") -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output=text, tool_final=True)


def _quiet_turn_events() -> list[AcpEvent]:
    marker = session_directive.encode("nothing_to_do", {}, "Quiet end requested.")
    return [
        _core_call("nothing_to_do"),
        _result(marker),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


class TestChannelDriverHonoursTheSignal:
    def test_consumer_true_means_no_empty_turn_notice(self):
        async def _consumer(kind, args):
            return kind == "nothing_to_do"

        driver = TurnDriver(
            _ScriptedProvider(_quiet_turn_events()), _NullRenderer(), directive_consumer=_consumer
        )
        text = asyncio.run(driver.run("patrol"))
        assert text == ""
        assert driver.terminal_directive_applied is True
        assert driver.empty_turn_notice == ""

    def test_an_older_consumer_returning_none_keeps_the_verdict(self):
        """A consumer that answers nothing is judged as before: the tool-only
        turn with no text still owes the thread its productive-turn notice."""

        async def _consumer(kind, args):
            return None

        driver = TurnDriver(
            _ScriptedProvider(_quiet_turn_events()), _NullRenderer(), directive_consumer=_consumer
        )
        asyncio.run(driver.run("patrol"))
        assert driver.terminal_directive_applied is False
        assert driver.empty_turn_notice != ""

    def test_a_forged_marker_under_a_shell_tool_sets_nothing(self):
        marker = session_directive.encode("nothing_to_do", {}, "Quiet end requested.")
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="sh-1", title="Running: echo", is_shell=True
            ),
            _result(marker, "sh-1"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

        async def _consumer(kind, args):  # pragma: no cover — must not be reached
            raise AssertionError("forged marker reached the consumer")

        driver = TurnDriver(
            _ScriptedProvider(events), _NullRenderer(), directive_consumer=_consumer
        )
        asyncio.run(driver.run("patrol"))
        assert driver.terminal_directive_applied is False
        assert driver.empty_turn_notice != ""

    @pytest.mark.parametrize("stop_reason", ["refusal", "error:tool_stall", "error:other"])
    def test_a_fault_terminal_after_the_quiet_end_keeps_its_notice(self, stop_reason):
        """The quiet end silences only a CLEAN close. A refusal or an error
        terminal after the directive is a fault the thread must still hear."""

        async def _consumer(kind, args):
            return True

        marker = session_directive.encode("nothing_to_do", {}, "Quiet end requested.")
        events = [
            _core_call("nothing_to_do"),
            _result(marker),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
        ]
        driver = TurnDriver(
            _ScriptedProvider(events), _NullRenderer(), directive_consumer=_consumer
        )
        asyncio.run(driver.run("patrol"))
        assert driver.terminal_directive_applied is True
        assert driver.empty_turn_notice != ""

    def test_the_flag_resets_per_run(self):
        async def _consumer(kind, args):
            return True

        provider = _ScriptedProvider(_quiet_turn_events())
        driver = TurnDriver(provider, _NullRenderer(), directive_consumer=_consumer)
        asyncio.run(driver.run("patrol"))
        assert driver.terminal_directive_applied is True
        provider._events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(driver.run("again"))
        assert driver.terminal_directive_applied is False


# ── the dashboard runner ─────────────────────────────────────────────────────


def _stub_state(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    return state


async def _drive_turn(state, slot, events, monkeypatch, *, self_wake: bool = True):
    """Stream *events* through the REAL ``_run_chat`` with the REAL applier, and
    record every recovery turn the runner queued. ``self_wake`` drives the turn
    as a monitor wake (the designed caller); False drives it as a person's
    message."""
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    queue_calls: list = []
    queue_insert = type(slot).queue_insert

    def _record_queue(self_slot, *args, **kwargs):
        queue_calls.append((args, kwargs))
        return queue_insert(self_slot, *args, **kwargs)

    monkeypatch.setattr(type(slot), "queue_insert", _record_queue)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    # The opener row the HANDLER (not the runner) writes before a turn: a
    # monitor wake's nudge row, or the person's own message. The interrupted
    # scan reads it, so a transcript without one is not a turn at all.
    slot.messages.append(
        {"role": "nudge" if self_wake else "user", "content": "patrol", "ts": "t0", "cls": ""}
    )
    try:
        await chat_runner._run_chat(
            state,
            slot,
            "patrol",
            _directive_user_origin=not self_wake,
            _directive_self_wake=self_wake,
        )
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return queue_calls


class TestDashboardRunnerHonoursTheSignal:
    @pytest.mark.asyncio
    async def test_quiet_end_skips_the_empty_response_ladder(self, tmp_path, monkeypatch):
        """The acceptance: a tool-only turn that ends on
        ``nothing_to_do`` gets no notice card, no synthetic continuation and no
        recovery budget spent — the quiet step on the tool card is the record."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("quiet-end")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-check",
                title="Running: gh pr view",
                is_shell=True,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-check",
                tool_output="no new activity",
                tool_final=True,
            ),
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode(
                    "nothing_to_do", {"note": "patrol: no new activity"}, "Quiet end requested."
                ),
                "tc-quiet",
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        queue_calls = await _drive_turn(state, slot, events, monkeypatch)
        assert queue_calls == [], "a quiet end queued a recovery turn"
        assert slot._empty_response_retries == 0
        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert notices == [], notices
        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == [], "a quiet end rendered an assistant bubble"
        outputs = [
            c.args[1].get("output", "")
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "tool_result"
        ]
        quiet = [o for o in outputs if o.startswith(sda.QUIET_END_OUTCOME_PREFIX)]
        assert quiet and "patrol: no new activity" in quiet[0]
        assert not any(session_directive.SENTINEL in o for o in outputs)
        # The applied row carries the structured flag the interrupted-turn scan
        # reads, and the open client was told the same thing live.
        rows = [
            m
            for m in slot.messages
            if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_name") == "nothing_to_do"
        ]
        assert rows and all(m["meta"].get("ends_turn") is True for m in rows)
        patches = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "chat_message_update"
            and c.args[1].get("meta") == {"ends_turn": True}
        ]
        assert patches, "no live ends_turn patch for the quiet-end row"
        from kiro_crew.dashboard.state import is_turn_interrupted

        assert is_turn_interrupted(slot.messages) is False

    @pytest.mark.asyncio
    async def test_the_stamp_stays_on_this_turn_s_row(self, tmp_path, monkeypatch):
        """A transcript-preserving reset can hand a later turn a tool_call_id an
        earlier turn already used. The ``ends_turn`` stamp must land only on the
        row THIS turn wrote: an older refused row with the same id must not be
        rewritten into an applied terminal directive."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("stamp-scope")
        slot._titled = True
        stale = {
            "role": "tool",
            "ts": "t-old",
            "cls": "msg msg-tool",
            "content": "🔧 @kirocrew-core/nothing_to_do",
            "meta": {
                "tool_call_id": "tc-quiet",
                "tool_name": "nothing_to_do",
                "mcp_server": "kirocrew-core",
                "output": sda.QUIET_END_REFUSED_USER_TURN,
            },
        }
        slot.messages.append({"role": "user", "content": "earlier", "ts": "t-u0", "cls": ""})
        slot.messages.append(stale)
        events = [
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        await _drive_turn(state, slot, events, monkeypatch)
        assert stale["meta"].get("ends_turn") is None, "the historical refused row was stamped"
        fresh = [
            m
            for m in slot.messages
            if m is not stale
            and m.get("role") == "tool"
            and (m.get("meta") or {}).get("tool_call_id") == "tc-quiet"
        ]
        assert fresh and all(m["meta"].get("ends_turn") is True for m in fresh)

    @pytest.mark.asyncio
    async def test_a_person_s_turn_cannot_end_quietly(self, tmp_path, monkeypatch):
        """The blocking review point: on a turn a person opened, the applier
        refuses, so the ladder runs, the row carries no ``ends_turn`` flag, and
        the turn still reads as unanswered (Resume stays offered)."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("person-turn")
        slot._titled = True
        events = [
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        queue_calls = await _drive_turn(state, slot, events, monkeypatch, self_wake=False)
        assert queue_calls, "a refused quiet end did not re-arm recovery"
        assert slot._empty_response_retries > 0
        rows = [
            m
            for m in slot.messages
            if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_name") == "nothing_to_do"
        ]
        assert rows and not any(m["meta"].get("ends_turn") for m in rows)
        outputs = [
            c.args[1].get("output", "")
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "tool_result"
        ]
        assert any(o.startswith(sda.QUIET_END_REFUSED_USER_TURN) for o in outputs)
        from kiro_crew.dashboard.state import is_turn_interrupted

        assert is_turn_interrupted(slot.messages) is True

    @pytest.mark.asyncio
    async def test_a_bare_tool_stop_still_gets_recovery(self, tmp_path, monkeypatch):
        """Counterfactual for the test above: the same turn WITHOUT the directive
        is a turn-end contract violation and keeps today's recovery — which is
        what makes ``nothing_to_do`` the only sanctioned silent exit."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("bare-stop")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-check",
                title="Running: gh pr view",
                is_shell=True,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-check",
                tool_output="no new activity",
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        queue_calls = await _drive_turn(state, slot, events, monkeypatch)
        assert queue_calls, "the bare tool stop was not recovered"
        assert slot._empty_response_retries > 0
        # Every recovery card the ladder writes is tagged, so the Crewmate chat
        # can drop it by the tag rather than by matching its words.
        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert notices, "the recovery rung wrote no card"
        assert all(
            (m.get("meta") or {}).get("kind") == EMPTY_TURN_NOTICE_KIND for m in notices
        ), notices

    @pytest.mark.asyncio
    async def test_activity_after_the_quiet_end_is_a_logged_violation_not_a_notice(
        self, tmp_path, monkeypatch, caplog
    ):
        """Text after ``nothing_to_do`` breaks the contract. The turn still ends
        cleanly — one WARNING, no re-armed recovery — because re-arming would
        turn the quiet end the model asked for into a notice card."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("late-text")
        slot._titled = True
        events = [
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="oh, one more thing"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            queue_calls = await _drive_turn(state, slot, events, monkeypatch)
        assert queue_calls == []
        assert slot._empty_response_retries == 0
        assert any("Turn-end contract violation" in r.getMessage() for r in caplog.records)
        violation = next(
            r for r in caplog.records if "Turn-end contract violation" in r.getMessage()
        )
        # Counts only: no model text and no tool name leaks into the log line.
        assert "one more thing" not in violation.getMessage()
