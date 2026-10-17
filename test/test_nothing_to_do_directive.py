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
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
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
        assert (
            "absentOr(meta.tool_name, QUIET_END_TOOL)" in src
        ), "the mirror no longer tolerates a backend that names no tool (KAS)"
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


class _RecordingRenderer(_NullRenderer):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []

    async def on_text_chunk(self, text):
        self.text.append(text)


#: Placed in a scripted stream where a person's steer reaches the backend: the
#: provider stamps the write the way the ACP session does.
_STAMP = object()


class _StampingProvider(_ScriptedProvider):
    last_steer_monotonic = 0.0

    async def stream(self, message):
        for ev in self._events:
            if ev is _STAMP:
                # Strictly after the turn's own start stamp, whatever the
                # platform clock's resolution.
                self.last_steer_monotonic = time.monotonic() + 1.0
                continue
            yield ev


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

    def test_text_after_the_quiet_end_is_dropped_not_rendered(self, caplog):
        """The backend's alternation filler echoed after an applied
        ``nothing_to_do`` reaches the channel as a plain text chunk. Keyed on
        the consumer's structured answer, the driver drops it: nothing reaches
        the renderer, ``run()`` returns no text for the transcript, the quiet
        end still owes no notice, and the violation is one counted WARNING."""

        async def _consumer(kind, args):
            return True

        marker = session_directive.encode("nothing_to_do", {}, "Quiet end requested.")
        events = [
            _core_call("nothing_to_do"),
            _result(marker),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="(No assistant response was recorded "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="for the preceding turn.)"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        renderer = _RecordingRenderer()
        driver = TurnDriver(_ScriptedProvider(events), renderer, directive_consumer=_consumer)
        with caplog.at_level("WARNING", logger="kiro_crew.messaging.driver"):
            text = asyncio.run(driver.run("patrol"))
        assert text == ""
        assert driver.partial_text == ""
        assert renderer.text == []
        assert driver.terminal_directive_applied is True
        assert driver.empty_turn_notice == ""
        violations = [r for r in caplog.records if "Turn-end contract violation" in r.getMessage()]
        assert len(violations) == 1
        assert "2 text frame(s)" in violations[0].getMessage()
        assert "preceding turn" not in violations[0].getMessage()

    @pytest.mark.parametrize("order", ["echo_before_reply", "stamp_only", "echo_before_quiet_end"])
    def test_a_steer_in_the_turn_keeps_the_reply_through_a_quiet_end(self, caplog, order):
        """A person can steer into a channel turn that ends (or has ended) on
        ``nothing_to_do`` while its stream is still open. Their answer must not
        be dropped with the filler, in every shape the evidence takes: kiro-cli
        echoes consumption before the reply streams; codex echoes it only at
        the terminal, so the provider's steer stamp is the only early evidence;
        and a steer the model read BEFORE the directive still owns the text
        that follows it."""

        async def _consumer(kind, args):
            return True

        marker = session_directive.encode("nothing_to_do", {}, "Quiet end requested.")
        quiet_end = [_core_call("nothing_to_do"), _result(marker)]
        filler = AcpEvent(kind=EVENT_TEXT_CHUNK, text="(No assistant response was recorded.)")
        reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text="Yes, still here.")
        consumed = AcpEvent(kind=EVENT_STEER_CONSUMED, text="are you still there?")
        if order == "echo_before_reply":
            events = quiet_end + [filler, _STAMP, consumed, reply]
            dropped = 1
        elif order == "stamp_only":
            events = quiet_end + [filler, _STAMP, reply]
            dropped = 1
        else:
            events = [_STAMP, consumed] + quiet_end + [reply]
            dropped = 0
        events.append(AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"))
        renderer = _RecordingRenderer()
        driver = TurnDriver(_StampingProvider(events), renderer, directive_consumer=_consumer)
        with caplog.at_level("WARNING", logger="kiro_crew.messaging.driver"):
            text = asyncio.run(driver.run("patrol"))
        assert text == "Yes, still here."
        assert "".join(renderer.text) == "Yes, still here."
        assert driver.terminal_directive_applied is True
        violations = [r for r in caplog.records if "Turn-end contract violation" in r.getMessage()]
        if dropped:
            assert len(violations) == 1 and "1 text frame(s)" in violations[0].getMessage()
        else:
            assert violations == []

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

    @pytest.mark.asyncio
    async def test_text_after_the_quiet_end_is_dropped_not_rendered(
        self, tmp_path, monkeypatch, caplog
    ):
        """A quiet end renders no assistant bubble, whatever the model says
        after it. The field case is the backend's alternation placeholder: a
        quiet end leaves the backend's history on a tool result, its repair
        fabricates a self-describing assistant filler for the next request, and
        the model reads it back as its own prior reply and repeats it after
        each later quiet end -- a grey "(No assistant response was recorded...)"
        bubble per patrol cycle. The drop keys on the structured ``ends_turn``
        signal, not on the placeholder's wording, so any late text is dropped:
        not streamed as a chunk, not persisted as an assistant row. The one
        trace is the turn-end WARNING with a count."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("late-placeholder")
        slot._titled = True
        placeholder = (
            "(No assistant response was recorded for the preceding turn. This "
            "placeholder was inserted automatically to preserve message "
            "alternation -- it is not a reply the assistant produced.)"
        )
        events = [
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
            # Token-by-token, as the model streams it.
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=placeholder[:40]),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=placeholder[40:]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            queue_calls = await _drive_turn(state, slot, events, monkeypatch)
        assert queue_calls == []
        assert slot._empty_response_retries == 0
        # Nothing persisted as the agent's words: no assistant row, no chunk row.
        roles = [m.get("role") for m in slot.messages]
        assert "assistant" not in roles and "chunk" not in roles, roles
        assert not any("No assistant response" in str(m.get("content", "")) for m in slot.messages)
        # Nothing streamed to the wire either.
        streamed = [
            c.args[0]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_chunk"
        ]
        assert streamed == []
        # The applied quiet end is still the turn's recorded end.
        rows = [
            m
            for m in slot.messages
            if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_name") == "nothing_to_do"
        ]
        assert rows and rows[-1]["meta"].get("ends_turn") is True
        violations = [r for r in caplog.records if "Turn-end contract violation" in r.getMessage()]
        assert len(violations) == 1
        assert "2 model event(s)" in violations[0].getMessage()
        assert "No assistant response" not in violations[0].getMessage()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "order", ["echo_before_reply", "echo_at_terminal", "echo_before_quiet_end"]
    )
    async def test_a_person_s_steer_keeps_the_reply_through_a_quiet_end(
        self, tmp_path, monkeypatch, caplog, order
    ):
        """A person can steer into a turn that ends (or has ended) on
        ``nothing_to_do`` while its stream is still open. Their answer must not
        be dropped with the filler, in every order a backend produces: kiro-cli
        echoes consumption before the reply streams; codex echoes it only at the
        terminal, so the steer is still pending when the text arrives; and a
        steer the model read BEFORE calling the directive still owns the text
        that follows it."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("steer-" + order)
        slot._titled = True
        steer = "are you still there?"
        consumed = AcpEvent(kind=EVENT_STEER_CONSUMED, text=steer)
        quiet_end = [
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
        ]
        filler = AcpEvent(kind=EVENT_TEXT_CHUNK, text="(No assistant response was recorded.)")
        reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text="Yes, still here.")
        # The admission ``steer_into_running_turn`` records for a live steer,
        # placed in the stream where the person pressed send.
        admit = object()
        if order == "echo_before_reply":
            # The filler echo lands before any person spoke: still dropped.
            events = quiet_end + [filler, admit, consumed, reply]
            dropped = 1
        elif order == "echo_at_terminal":
            events = quiet_end + [filler, admit, reply, consumed]
            dropped = 1
        else:
            # Once a person's steer was read, nothing after the quiet end can be
            # told from their answer, so nothing is dropped.
            events = [admit, consumed] + quiet_end + [reply]
            dropped = 0
        events.append(AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"))

        def _stream():
            for ev in events:
                if ev is admit:
                    slot._pending_steers.append(steer)
                    continue
                yield ev

        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            await _drive_turn(state, slot, _stream(), monkeypatch)
        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert [m["content"] for m in assistant] == ["Yes, still here."], slot.messages
        assert not any("No assistant response" in str(m.get("content", "")) for m in slot.messages)
        wire = [
            c.args[1]["content"]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_chunk"
        ]
        assert "".join(wire) == "Yes, still here."
        assert slot._pending_steers == []
        violations = [r for r in caplog.records if "Turn-end contract violation" in r.getMessage()]
        if dropped:
            assert len(violations) == 1 and "1 model event(s)" in violations[0].getMessage()
        else:
            assert violations == []

    @pytest.mark.asyncio
    async def test_a_compacted_quiet_end_queues_no_continuation(self, tmp_path, monkeypatch):
        """An automatic compaction completed mid-turn, then the turn ended on
        ``nothing_to_do`` and the model echoed the filler. The dropped text
        leaves the answer segment empty, which is exactly what the
        post-compaction continuation reads as an abandoned request — but a
        quiet end is answered by its directive, so no notice and no synthetic
        continuation."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("compacted-quiet-end")
        slot._titled = True
        events = [
            AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
            AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"),
            _core_call("nothing_to_do", "tc-quiet"),
            _result(
                session_directive.encode("nothing_to_do", {}, "Quiet end requested."), "tc-quiet"
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="(No assistant response was recorded.)"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        queue_calls = await _drive_turn(state, slot, events, monkeypatch)
        assert queue_calls == [], "a compacted quiet end queued a continuation"
        assert slot._compaction_continue_retries == 0
        recovery = [
            m
            for m in slot.messages
            if m.get("role") == "notice"
            and (m.get("meta") or {}).get("kind") == EMPTY_TURN_NOTICE_KIND
        ]
        assert recovery == [], recovery
        # The compaction notice is the only assistant row; the filler is gone.
        assistant = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert all(c.startswith("✅ Conversation compacted") for c in assistant), assistant
        assert not any("No assistant response" in c for c in assistant)


# ── a quiet end on a backend that names no tool (KAS) ───────────────────────


def _kas_tool_call_frame(tcid: str, tool: str) -> dict:
    """The ``tool_call`` frame KAS (kiro-agent) sends for a Crew MCP tool.

    KAS names the SERVER on the frame (``_meta.kiro.serverName``) and NO tool
    name; the tool is named only by its wrapper's ``@<server>/<tool>`` wire
    title. Same shape as ``test/fixtures/acp_frames/kas/session.jsonl`` and as
    the field transcript rows that showed Resume after every patrol quiet end
    (``meta`` carried ``mcp_server`` and ``ends_turn``, never ``tool_name``).
    """
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tcid,
        "title": f"@{session_directive.CORE_MCP_SERVER}/{tool}",
        "kind": "other",
        "status": "pending",
        "rawInput": {"note": "patrol: nothing new"},
        "_meta": {
            "kiro": {"serverName": session_directive.CORE_MCP_SERVER, "toolOrigin": "client"}
        },
    }


def _parked_kas_stream(slot, tool: str, events: list[AcpEvent]):
    """Yield *events* after parking the record the KAS-side tool parks.

    KAS states no tool name, so the runner reaches the directive through the
    out-of-band record the MCP tool posts to ``/api/session-directive``, keyed
    by the call's input digest. Parked mid-turn, as the tool does, so the
    claim's turn bound admits it.
    """
    from kiro_crew.dashboard import directive_queue
    from kiro_crew.dashboard.chat_utils import effective_session_key

    args = _kas_tool_call_frame("", tool)["rawInput"]
    directive_queue.publish(
        effective_session_key(slot), tool, args, session_directive.call_input_digest(tool, args)
    )
    yield from events


def _kas_call(tool: str, tcid: str) -> AcpEvent:
    """Parse the KAS frame through the REAL ``parse_session_update``."""
    from kiro_crew.acp._dispatch import parse_session_update

    [event] = parse_session_update(_kas_tool_call_frame(tcid, tool))
    assert event.kind == EVENT_TOOL_CALL
    # The precondition this class exists for: KAS states no tool name.
    assert event.tool_name == ""
    assert event.mcp_server_name == session_directive.CORE_MCP_SERVER
    return event


class TestQuietEndWithoutABackendToolName:
    """The applied quiet end is a FINISHED turn on every surface that classifies
    it, even when the backend never stated the tool's name -- and a genuinely
    unfinished turn on the same backend still reads as interrupted."""

    @pytest.fixture(autouse=True)
    def _clean_queue(self):
        from kiro_crew.dashboard import directive_queue

        directive_queue.reset()
        yield
        directive_queue.reset()

    @pytest.mark.asyncio
    async def test_a_kas_quiet_end_is_a_finished_turn(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import _is_interrupted
        from kiro_crew.dashboard.state import is_turn_interrupted

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("kas-quiet-end")
        slot._titled = True
        events = [
            _kas_call("nothing_to_do", "tc-quiet"),
            _result("Quiet end requested for this turn.", "tc-quiet"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        queue_calls = await _drive_turn(
            state, slot, _parked_kas_stream(slot, "nothing_to_do", events), monkeypatch
        )
        assert queue_calls == [], "the applied quiet end re-armed recovery"
        rows = [
            m
            for m in slot.messages
            if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_call_id") == "tc-quiet"
        ]
        assert rows and all(m["meta"].get("ends_turn") is True for m in rows)
        # Every backend consumer reads the one scan: the Continue endpoint's
        # wording, the composer's Resume gate and the sidebar's flag.
        assert is_turn_interrupted(slot.messages) is False
        assert _is_interrupted(slot) is False
        assert slot.to_dict().get("interrupted") is not True

    @pytest.mark.asyncio
    async def test_a_kas_turn_that_died_after_a_tool_is_still_interrupted(
        self, tmp_path, monkeypatch
    ):
        """Counterfactual: the same backend, an ordinary Crew tool, no reply and
        no quiet end. That is the shape a dropped connection leaves."""
        from kiro_crew.dashboard.state import is_turn_interrupted

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("kas-died")
        slot._titled = True
        events = [
            _kas_call("resource_status", "tc-tool"),
            _result("ok", "tc-tool"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        await _drive_turn(state, slot, events, monkeypatch)
        assert is_turn_interrupted(slot.messages) is True

    @pytest.mark.asyncio
    async def test_a_refused_kas_quiet_end_is_still_interrupted(self, tmp_path, monkeypatch):
        """A person opened the turn, so the applier refuses: no ``ends_turn``
        stamp, and the row must not close the turn."""
        from kiro_crew.dashboard.state import is_turn_interrupted

        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("kas-refused")
        slot._titled = True
        events = [
            _kas_call("nothing_to_do", "tc-quiet"),
            _result("Quiet end requested for this turn.", "tc-quiet"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        await _drive_turn(
            state,
            slot,
            _parked_kas_stream(slot, "nothing_to_do", events),
            monkeypatch,
            self_wake=False,
        )
        rows = [m for m in slot.messages if m.get("role") == "tool"]
        assert rows and not any((m.get("meta") or {}).get("ends_turn") for m in rows)
        assert is_turn_interrupted(slot.messages) is True


class TestQuietEndRowPredicate:
    """``is_quiet_end_row`` keys on the runner's own ``ends_turn`` stamp; the
    backend's identity halves may be absent but never contradict it."""

    @staticmethod
    def _row(**meta) -> dict:
        return {"role": "tool", "content": "🔧 @kirocrew-core/nothing_to_do", "meta": meta}

    def test_a_stamped_row_without_a_backend_tool_name_closes_the_turn(self):
        from kiro_crew.dashboard.state import is_quiet_end_row

        assert is_quiet_end_row(self._row(mcp_server="kirocrew-core", ends_turn=True))
        assert is_quiet_end_row(self._row(ends_turn=True))

    def test_an_unstamped_row_never_closes_the_turn(self):
        from kiro_crew.dashboard.state import is_quiet_end_row

        assert not is_quiet_end_row(
            self._row(tool_name="nothing_to_do", mcp_server="kirocrew-core")
        )
        assert not is_quiet_end_row(self._row(mcp_server="kirocrew-core", ends_turn="true"))

    def test_a_contradicting_identity_never_closes_the_turn(self):
        from kiro_crew.dashboard.state import is_quiet_end_row

        assert not is_quiet_end_row(
            self._row(tool_name="execute_bash", mcp_server="kirocrew-core", ends_turn=True)
        )
        assert not is_quiet_end_row(
            self._row(tool_name="nothing_to_do", mcp_server="other-server", ends_turn=True)
        )

    def test_the_field_transcript_tail_reads_as_finished(self):
        """The persisted rows of the reported session, reduced to the keys the
        scan reads: a nudge, two tool calls, then the stamped quiet end."""
        from kiro_crew.dashboard.state import is_turn_interrupted

        rows = [
            {"role": "nudge", "content": "[auto-nudge cycle 25] patrol"},
            {
                "role": "tool",
                "content": "🔧 @kirocrew-work/work_ledger_read",
                "meta": {"mcp_server": "kirocrew-work", "done": True},
            },
            {
                "role": "tool",
                "content": "🔧 @kirocrew-core/nothing_to_do",
                "meta": {"mcp_server": "kirocrew-core", "done": True, "ends_turn": True},
            },
        ]
        assert is_turn_interrupted(rows) is False
        # A later tool after it means the model kept working: not closed.
        rows.append({"role": "tool", "content": "🔧 Run Command", "meta": {"done": True}})
        assert is_turn_interrupted(rows) is True
