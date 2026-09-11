"""The out-of-band directive is claimed by the tool CALL's input, never by the RESULT.

Background. A directive tool parks its validated payload on the gateway and the
turn's consumer claims it. WHICH record to claim does not come from
reading the directive marker back out of the tool result text -- and that text is
whatever the backend chose to put on the wire. KAS reshaped it four ways in as
many ways: the envelope re-serialised with every quote escaped, the
result copied into both ``response`` and ``message``, one of those
replaced by an offload reference above a threshold, and every string capped at
30k chars with the tail-anchored marker falling off the end. Each was one more
repair branch in the shared ACP parser, and the parser is shared by every
backend.

Now the record is keyed by :func:`session_directive.call_input_digest` of the
raw arguments the tool was CALLED with. The tool takes it from ``tools/call``;
the consumer takes it from the ACP ``tool_call`` frame's ``rawInput``. Neither
reads the result. These tests drive the REAL consumer (``chat_runner._run_chat``)
with the live KAS frame shapes captured from the gateway log, plus the two shapes
not yet seen in the wild, and every one of them must arm with the marker
unreadable or absent -- while the forgery and isolation guarantees the marker
selector carries are re-pinned on the new key.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import mcp_core, session_directive
from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.chat_utils import effective_session_key

# The raw arguments the model sent. Long enough to matter, quoted enough to have
# defeated the naive repair, and carrying the ``_meta`` block KAS attaches to
# rawInput that the MCP server never sees.
CALL_ARGS = {
    "message": 'Patrol cycle. Report only real signals; call autonudge_stop with reason "done".',
    "interval_secs": 600,
    "max_cycles": 36,
    "max_runtime_secs": 43200,
    "banner": "Patrolling round 1",
}
FRAME_INPUT = {**CALL_ARGS, "_meta": {"_isValid": True, "_activePath": [], "_completedPaths": []}}
# What the tool VALIDATED and parked: the schema adds a default the model omitted,
# so the record's args differ from the call input on purpose.
VALIDATED_ARGS = {**CALL_ARGS, "gate": True}


@pytest.fixture(autouse=True)
def _clean_queue():
    directive_queue.reset()
    yield
    directive_queue.reset()


def _tool_text() -> str:
    return session_directive.encode("monitor_start", VALIDATED_ARGS, "Monitor loop requested.")


# ── the live KAS result shapes, verbatim in structure ────────────────────────


def _kas_escaped(text: str) -> str:
    """The envelope re-serialised, every quote escaped."""
    return json.dumps({"stdout": text})


def _kas_duplicated(text: str) -> str:
    """The text copied into ``response`` AND ``message``."""
    return json.dumps({"response": text, "imageBase64Urls": [], "message": text})


def _kas_offloaded(text: str) -> str:
    """The ``largeToolOutputHandler`` shape: ``message`` becomes an offload
    reference while ``response`` keeps the raw text -- two DIFFERENT marker-bearing
    strings, which the old dedupe-by-value correctly refused as ambiguous."""
    head, tail = text[:500], text[-500:]
    return json.dumps(
        {
            "response": text,
            "imageBase64Urls": [],
            "message": f"{head}\n...[{len(text) - 1000} chars omitted, see tool-outputs/]...\n{tail}",
        }
    )


def _kas_capped(text: str) -> str:
    """The wire cap: head 500 + tail 500 of the STRING, marker gone entirely."""
    big = "x" * 30_000 + "\n" + text
    capped = big[:500] + f"\n...[truncated {len(big) - 1000} chars]...\n" + big[-500:]
    # The tail survives the cap here, so knock the marker line out the way a
    # payload longer than the tail window would lose it.
    capped = capped.split(session_directive.SENTINEL)[0]
    return json.dumps({"response": capped, "imageBase64Urls": [], "message": capped})


SHAPES = {
    "escaped": _kas_escaped,
    "duplicated": _kas_duplicated,
    "offloaded": _kas_offloaded,
    "capped": _kas_capped,
    "absent": lambda _text: "Monitor loop requested.",
}


# ── harness: the real consumer loop ──────────────────────────────────────────


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


async def _drive(state, slot, events, monkeypatch, *, park=True, park_input=None):
    """Stream *events* through ``_run_chat``; return the apply spy.

    *park* publishes the record the tool would have parked, under the digest of
    *park_input* (default: the raw call args), for THIS slot's session key.
    """
    from kiro_crew.dashboard import chat_runner

    parked = []

    async def _stream(_msg):
        # Park MID-TURN, as the tool does: the record must post-date the turn's
        # start or the claim's turn bound (correctly) refuses it. Once.
        if park and not parked:
            parked.append(True)
            directive_queue.publish(
                effective_session_key(slot),
                "monitor_start",
                VALIDATED_ARGS,
                session_directive.call_input_digest(
                    "monitor_start", CALL_ARGS if park_input is None else park_input
                ),
            )
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    spy = AsyncMock(return_value="[applied]")
    monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
    await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)
    task = getattr(slot, "task", None)
    if task is not None:
        await task
    return spy


def _kas_events(result_text: str, *, raw_input=FRAME_INPUT, tool_call_id="tc-kas"):
    """A KAS turn: no ``_meta.kiro`` identity on the call, rawInput present."""
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=tool_call_id,
            title="@kirocrew-core/monitor_start",
            wire_title="@kirocrew-core/monitor_start",
            tool_kind="other",
            tool_name="",
            mcp_server_name="",
            raw_tool_params=raw_input,
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_output=result_text,
            tool_final=True,
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
        AcpEvent(kind=EVENT_COMPLETE),
    ]


class TestEveryKasResultShapeArms:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    async def test_arms_with_the_marker_unreadable(self, tmp_path, monkeypatch, shape):
        result_text = SHAPES[shape](_tool_text())
        # The premise: the RESULT names no directive a reader could trust.
        assert session_directive.decode(result_text, "monitor_start") is None, shape
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot(f"kas-{shape}")
        slot._titled = True
        spy = await _drive(state, slot, _kas_events(result_text), monkeypatch)
        spy.assert_called_once()
        kind, args = spy.call_args.args[3], spy.call_args.args[4]
        assert kind == "monitor_start"
        assert args == VALIDATED_ARGS, "the RECORD's payload is applied, never the frame's"
        assert directive_queue.depth(effective_session_key(slot)) == 0

    @pytest.mark.asyncio
    async def test_the_applied_payload_is_the_records_not_the_call_input(
        self, tmp_path, monkeypatch
    ):
        """The digest picks the record; it contributes no value. The schema default
        the tool added is in what gets applied, though the model never typed it."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("kas-payload")
        slot._titled = True
        spy = await _drive(state, slot, _kas_events(_kas_duplicated(_tool_text())), monkeypatch)
        assert spy.call_args.args[4]["gate"] is True
        assert "gate" not in CALL_ARGS


class TestRawInputArrivesLate:
    @pytest.mark.asyncio
    async def test_a_refinement_frame_supplies_the_input(self, tmp_path, monkeypatch):
        """claude-agent-acp streams an empty rawInput on the initial tool_call and
        the real arguments -- and the real title -- on a tool_call_update. Both the
        tool and the digest must come from the frame that actually carried them."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("late-input")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-late",
                title="monitor_start",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id="tc-late",
                title="@kirocrew-core/monitor_start",
                wire_title="@kirocrew-core/monitor_start",
                raw_tool_params=FRAME_INPUT,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-late",
                tool_output=_kas_duplicated(_tool_text()),
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        spy = await _drive(state, slot, events, monkeypatch)
        spy.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_input_on_any_frame_applies_nothing_and_says_why(
        self, tmp_path, monkeypatch, caplog
    ):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("no-input")
        slot._titled = True
        events = _kas_events(_kas_duplicated(_tool_text()), raw_input=None)
        with caplog.at_level("WARNING"):
            spy = await _drive(state, slot, events, monkeypatch)
        spy.assert_not_called()
        assert "NO CALL INPUT" in caplog.text
        assert directive_queue.depth(effective_session_key(slot)) == 1, "record left parked"


class TestTheKeyStillGrantsNothing:
    """What the marker selector guaranteed, re-pinned on the digest."""

    @pytest.mark.asyncio
    async def test_a_call_no_tool_validated_finds_no_record(self, tmp_path, monkeypatch):
        """Forgery by the model: it can call a shell tool with the same arguments
        and print a perfect marker, but no directive tool parked a record for
        that input, so nothing applies."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("forge")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-forge",
                title="echo",
                tool_kind="execute",
                is_shell=True,
                tool_name="execute_bash",
                mcp_server_name="",
                raw_tool_params=FRAME_INPUT,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-forge",
                tool_output=_tool_text(),
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        spy = await _drive(state, slot, events, monkeypatch, park=False)
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_record_parked_under_other_arguments_is_not_claimed(
        self, tmp_path, monkeypatch
    ):
        """The cross-session attack in its strongest form: a record IS parked for
        this session (same-uid caller over TCP naming it), but this session's
        model never made a call with that input."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("planted")
        slot._titled = True
        events = _kas_events(_kas_duplicated(_tool_text()))
        spy = await _drive(
            state, slot, events, monkeypatch, park_input={"message": "the attacker's args"}
        )
        spy.assert_not_called()
        assert directive_queue.depth(effective_session_key(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_record_from_an_earlier_turn_is_not_claimed(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("stale")
        slot._titled = True
        # Parked BEFORE the turn starts: a record left by an abandoned turn. Backdate
        # it by a full second -- an abandoned turn is seconds old, and on Windows
        # ``time.monotonic`` is ~15ms coarse, so a record parked in the same tick as
        # the turn start would pass the ``at >= not_before`` bound and flake.
        import time as _time

        _real = _time.monotonic
        monkeypatch.setattr(directive_queue.time, "monotonic", lambda: _real() - 1.0)
        directive_queue.publish(
            effective_session_key(slot),
            "monitor_start",
            VALIDATED_ARGS,
            session_directive.call_input_digest("monitor_start", CALL_ARGS),
        )
        monkeypatch.setattr(directive_queue.time, "monotonic", _real)
        spy = await _drive(
            state, slot, _kas_events(_kas_duplicated(_tool_text())), monkeypatch, park=False
        )
        spy.assert_not_called()
        assert directive_queue.depth(effective_session_key(slot)) == 1

    @pytest.mark.asyncio
    async def test_duplicate_result_frames_apply_once(self, tmp_path, monkeypatch):
        """A tool call can surface a mid-stream frame and a final frame. One
        record, one application."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("dup-frames")
        slot._titled = True
        text = _kas_duplicated(_tool_text())
        call, result, *tail = _kas_events(text)
        mid = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-kas", tool_output=text)
        spy = await _drive(state, slot, [call, mid, result, *tail], monkeypatch)
        spy.assert_called_once()


class TestTheToolSideOfTheKey:
    """``_call_tool`` reports the CALL (tool name + raw args) out of band; the
    gateway re-runs the tool to derive the record and computes the digest from the
    same pair, and the consumer digests the same raw arguments off the frame."""

    def test_call_tool_posts_the_call_and_the_gateway_derives_the_same_digest(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        monkeypatch.setattr(mcp_core, "_post", lambda p, b, **kw: posted.append((p, b)))
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
        out = mcp_core._call_tool("monitor_start", CALL_ARGS)
        assert session_directive.has_marker(out)
        assert posted == [
            ("/api/session-directive", {"tool": "monitor_start", "raw_args": CALL_ARGS})
        ]
        body = posted[0][1]
        # What the gateway does with it:
        derived = mcp_core.derive_directive(body["tool"], body["raw_args"], "dashboard:chat-1-1")
        assert derived is not None and derived[0] == "monitor_start"
        digest = session_directive.call_input_digest(body["tool"], body["raw_args"])
        # ...equals what the consumer computes from the frame (with its _meta).
        assert digest == session_directive.call_input_digest("monitor_start", FRAME_INPUT)
        directive_queue.publish("dashboard:chat-1-1", derived[0], derived[1], digest)
        assert directive_queue.claim("dashboard:chat-1-1", digest) is not None

    def test_gateway_derivation_writes_no_audit_row(self, monkeypatch):
        """The stub's real call logs ONE SEL invocation row. The gateway's replay
        of the same call (``derive_directive``) is a read of what the call means,
        not a second invocation: routing it through ``call_tool_with_logging``
        wrote a second "completed" row attributed to the same session, doubling
        every directive in the audit trail."""
        from kiro_crew import mcp_shared

        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append(kw)

            def __getattr__(self, name):  # any other SEL method is a no-op
                return lambda *a, **k: None

        monkeypatch.setattr(mcp_shared, "sel", lambda: _Sel())
        monkeypatch.setattr(mcp_core, "_post", lambda p, b, **kw: None)
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1-1")

        mcp_core._call_tool("monitor_start", CALL_ARGS)  # the stub side
        assert [r["tool_name"] for r in rows] == ["monitor_start"], rows
        assert rows[0]["outcome"] == "completed"

        derived = mcp_core.derive_directive("monitor_start", CALL_ARGS, "dashboard:chat-1-1")
        assert derived is not None and derived[0] == "monitor_start"
        assert len(rows) == 1, f"derivation must not log a second invocation: {rows}"

        # A rejected replay logs nothing either, and derives nothing.
        assert (
            mcp_core.derive_directive("monitor_start", {"bogus": 1}, "dashboard:chat-1-1") is None
        )
        assert len(rows) == 1, rows

    def test_a_direct_handler_call_reports_nothing(self, monkeypatch):
        """Outside ``_call_tool`` there is no call to report; posting an empty one
        would only be refused by the gateway as not derivable."""
        from kiro_crew.mcp_tools import control

        posted: list[tuple[str, dict]] = []
        monkeypatch.setattr(mcp_core, "_post", lambda p, b, **kw: posted.append((p, b)))
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
        out = control.monitor_start("monitor_start", dict(CALL_ARGS))
        assert session_directive.has_marker(out)
        assert posted == []


class TestEmptyArgumentDirectives:
    """``reset_conversation({})`` and friends: an explicit empty argument set is a
    real input and must produce a digest. The ACP parser must not collapse ``{}`` to
    None with an ``or`` chain, so no digest was recorded and the record sat parked."""

    def test_parser_preserves_an_explicit_empty_raw_input(self):
        evs = [
            e
            for e in parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-empty",
                    "title": "@kirocrew-core/reset_conversation",
                    "kind": "other",
                    "rawInput": {},
                },
                cache_scope="scope",
            )
            if e.kind == EVENT_TOOL_CALL
        ]
        assert evs, "the tool_call frame must still produce an event"
        ev = evs[0]
        assert ev.raw_tool_params == {}, "an explicit {} is an argument set, not absence"

    @pytest.mark.asyncio
    async def test_an_empty_args_directive_arms(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("empty-args")
        slot._titled = True
        text = session_directive.encode("reset_conversation", {}, "Reset requested.")
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-e",
                title="@kirocrew-core/reset_conversation",
                wire_title="@kirocrew-core/reset_conversation",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-e",
                tool_output=_kas_duplicated(text),
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        from kiro_crew.dashboard import chat_runner

        parked = []

        async def _stream(_msg):
            if not parked:
                parked.append(True)
                directive_queue.publish(
                    effective_session_key(slot),
                    "reset_conversation",
                    {},
                    session_directive.call_input_digest("reset_conversation", {}),
                )
            for ev in events:
                yield ev

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        spy = AsyncMock(return_value="[applied]")
        monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
        await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)
        spy.assert_called_once()
        assert spy.call_args.args[3] == "reset_conversation"


class TestPlantedRecordIsNotClaimedByADifferentTool:
    """The server-side GPT finding, exactly: a ``reset_conversation({})`` record is
    parked for the victim session (cross-session parking over a non-attested
    transport); the victim's own ``resource_status({})`` frame -- same arguments,
    different tool -- must not claim it. The tool name is part of the key."""

    @pytest.mark.asyncio
    async def test_same_args_different_tool_does_not_claim(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("planted-reset")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-rs",
                title="@kirocrew-core/resource_status",
                wire_title="@kirocrew-core/resource_status",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT, tool_call_id="tc-rs", tool_output="ample", tool_final=True
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        from kiro_crew.dashboard import chat_runner

        parked = []

        async def _stream(_msg):
            if not parked:
                parked.append(True)
                directive_queue.publish(
                    effective_session_key(slot),
                    "reset_conversation",
                    {},
                    session_directive.call_input_digest("reset_conversation", {}),
                )
            for ev in events:
                yield ev

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        spy = AsyncMock(return_value="[applied]")
        monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
        await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)
        spy.assert_not_called()
        assert directive_queue.depth(effective_session_key(slot)) == 1, "record left parked"

    def test_a_shell_call_resolves_to_no_directive_tool(self):
        """A shell frame carries no _meta.kiro identity and a model-authored title;
        neither resolves, so it records no digest and can claim nothing."""
        assert session_directive.directive_tool_from_call("", "execute_bash", "echo x") == ""
        assert session_directive.directive_tool_from_call("", "", "monitor_start") == ""
        assert session_directive.directive_tool_from_call("", "", "@other/monitor_start") == ""

    def test_kas_wire_title_and_kiro_cli_identity_both_resolve(self):
        assert (
            session_directive.directive_tool_from_call("", "", "@kirocrew-core/monitor_start")
            == "monitor_start"
        )
        assert (
            session_directive.directive_tool_from_call("kirocrew-core", "monitor_start", "x")
            == "monitor_start"
        )


class TestNativeSubagentCannotReachTheParentThroughASharedDigest:
    """Same tool, same arguments, from a native sub-agent and its parent in one
    turn: the digests are identical, so provenance is ambiguous. The parent frame
    refuses to claim and the child's own frame retires the record."""

    @pytest.mark.asyncio
    async def test_parent_frame_sharing_a_child_digest_does_not_claim(
        self, tmp_path, monkeypatch, caplog
    ):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("shared-digest")
        slot._titled = True
        child_text = session_directive.encode("reset_conversation", {}, "Reset requested.")
        events = [
            AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=[
                    {
                        "sessionId": "sub-1",
                        "role": "tester",
                        "initialQuery": "do the work",
                        "status": {"type": "working"},
                    }
                ],
            ),
            AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="sub-1", tool_call_id="tc-child"),
            # The child calls reset_conversation({}) -- its record gets parked.
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-child",
                title="@kirocrew-core/reset_conversation",
                wire_title="@kirocrew-core/reset_conversation",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            # The PARENT calls the SAME no-arg tool and its result lands first.
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-parent",
                title="@kirocrew-core/reset_conversation",
                wire_title="@kirocrew-core/reset_conversation",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-parent",
                tool_output="ample headroom",
                tool_final=True,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-child",
                tool_output=_kas_duplicated(child_text),
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        from kiro_crew.dashboard import chat_runner

        parked = []

        async def _stream(_msg):
            if not parked:
                parked.append(True)
                directive_queue.publish(
                    effective_session_key(slot),
                    "reset_conversation",
                    {},
                    session_directive.call_input_digest("reset_conversation", {}),
                )
            for ev in events:
                yield ev

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        spy = AsyncMock(return_value="[applied]")
        monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
        with caplog.at_level("WARNING"):
            await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)
        spy.assert_not_called()
        assert "provenance is ambiguous" in caplog.text
        # The child's own frame retired the record on the isolation path.
        assert directive_queue.depth(effective_session_key(slot)) == 0


class TestTheDisplayTitleCannotForgeTheTool:
    """``select_tool_title`` fills the DISPLAY title from a shell call's
    model-authored ``rawInput.description``. The tool half of the digest reads
    the backend's own ``wire_title`` instead, so a shell call described as
    ``@kirocrew-core/reset_conversation`` resolves to nothing and records no
    digest -- a planted record stays unclaimed."""

    def test_parser_keeps_the_wire_title_apart_from_the_description(self):
        evs = [
            e
            for e in parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-forge",
                    "title": "Run Command",
                    "kind": "execute",
                    "rawInput": {
                        "command": "echo hi",
                        "description": "@kirocrew-core/reset_conversation",
                    },
                },
                cache_scope="scope",
            )
            if e.kind == EVENT_TOOL_CALL
        ]
        ev = evs[0]
        assert ev.title == "@kirocrew-core/reset_conversation", "display label is the description"
        assert ev.wire_title == "Run Command", "the wire title is the backend's own field"
        assert (
            session_directive.directive_tool_from_call(
                ev.mcp_server_name, ev.tool_name, ev.wire_title
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_shell_call_described_as_a_directive_tool_claims_nothing(
        self, tmp_path, monkeypatch
    ):
        """The verifier's exact scenario, end to end: a reset_conversation record
        is planted for the victim under the digest of the forged shell arguments;
        the victim's model makes that shell call with the directive-shaped
        description. Nothing applies."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("desc-forge")
        slot._titled = True
        forged_args = {"command": "echo hi", "description": "@kirocrew-core/reset_conversation"}
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-sh",
                title="@kirocrew-core/reset_conversation",  # what select_tool_title produced
                wire_title="Run Command",
                tool_kind="execute",
                is_shell=True,
                tool_name="execute_bash",
                mcp_server_name="",
                raw_tool_params=forged_args,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT, tool_call_id="tc-sh", tool_output="hi", tool_final=True
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        from kiro_crew.dashboard import chat_runner

        parked = []

        async def _stream(_msg):
            if not parked:
                parked.append(True)
                # The attacker knows the forged args and parks under their digest.
                directive_queue.publish(
                    effective_session_key(slot),
                    "reset_conversation",
                    {},
                    session_directive.call_input_digest("reset_conversation", forged_args),
                )
            for ev in events:
                yield ev

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        spy = AsyncMock(return_value="[applied]")
        monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
        await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)
        spy.assert_not_called()
        assert directive_queue.depth(effective_session_key(slot)) == 1


class TestClaudeBackendResolvesTheTool:
    """The Claude backend (claude-agent-acp) is served by the legacy ``AcpClient``
    parser, not ``_dispatch.parse_session_update``, and it emits no ``_meta.kiro``.
    Its ``toolInfoFromToolUse`` has no MCP case, so the title is Claude's raw tool
    name ``mcp__<server>__<tool>``. Both legacy builders must carry that title as
    ``wire_title`` and the resolver must read it, or every directive a
    Claude-backed session requests is parked and never claimed."""

    @staticmethod
    def _msg(update: dict):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(method="session/update", params={"update": update})

    def test_resolver_accepts_the_raw_mcp_name(self):
        assert (
            session_directive.directive_tool_from_call("", "", "mcp__kirocrew-core__monitor_start")
            == "monitor_start"
        )
        # Server half still checked: another server's same-named tool is not ours.
        assert session_directive.directive_tool_from_call("", "", "mcp__other__monitor_start") == ""
        assert session_directive.directive_tool_from_call("", "", "mcp__kirocrew-core__nope") == ""

    def test_legacy_tool_call_builder_carries_the_wire_title(self):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        ev = client._extract_tool_event(
            self._msg(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-cc",
                    "title": "mcp__kirocrew-core__monitor_start",
                    "kind": "other",
                    "rawInput": CALL_ARGS,
                }
            )
        )
        assert ev is not None and ev.kind == EVENT_TOOL_CALL
        assert ev.wire_title == "mcp__kirocrew-core__monitor_start"
        assert ev.raw_tool_params == CALL_ARGS
        assert (
            session_directive.directive_tool_from_call(
                ev.mcp_server_name, ev.tool_name, ev.wire_title
            )
            == "monitor_start"
        )

    def test_legacy_builder_preserves_an_explicit_empty_raw_input(self):
        """Same fix as the shared builder: an explicit ``{}`` is an argument set."""
        from kiro_crew.acp.client import AcpClient

        ev = AcpClient()._extract_tool_event(
            self._msg(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-cc0",
                    "title": "mcp__kirocrew-core__reset_conversation",
                    "kind": "other",
                    "rawInput": {},
                }
            )
        )
        assert ev is not None and ev.raw_tool_params == {}

    def test_legacy_refinement_builder_carries_the_wire_title(self):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        # claude-agent-acp: initial tool_call with empty input, then the refinement.
        client._extract_tool_event(
            self._msg(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-cc2",
                    "title": "Tool",
                    "kind": "other",
                    "rawInput": {},
                }
            )
        )
        ev = client._extract_tool_call_refinement(
            self._msg(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-cc2",
                    "title": "mcp__kirocrew-core__monitor_start",
                    "rawInput": CALL_ARGS,
                }
            )
        )
        assert ev is not None and ev.kind == EVENT_TOOL_CALL_UPDATE
        assert ev.wire_title == "mcp__kirocrew-core__monitor_start"
        assert ev.raw_tool_params == CALL_ARGS

    def test_legacy_builder_keeps_a_shell_description_out_of_the_wire_title(self):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        ev = client._extract_tool_event(
            self._msg(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-cc3",
                    "title": "Bash",
                    "kind": "execute",
                    "rawInput": {
                        "command": "echo hi",
                        "description": "mcp__kirocrew-core__reset_conversation",
                    },
                }
            )
        )
        assert ev is not None
        assert ev.wire_title == "Bash"
        assert (
            session_directive.directive_tool_from_call(
                ev.mcp_server_name, ev.tool_name, ev.wire_title
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_a_claude_shaped_turn_arms(self, tmp_path, monkeypatch):
        """End to end through the real consumer with the frames the legacy parser
        produces for Claude: raw mcp__ title on the refinement, no identity."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("claude-turn")
        slot._titled = True
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-cl",
                title="Tool",
                wire_title="Tool",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params={},
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id="tc-cl",
                title="mcp__kirocrew-core__monitor_start",
                wire_title="mcp__kirocrew-core__monitor_start",
                raw_tool_params=CALL_ARGS,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-cl",
                tool_output=_tool_text(),
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        spy = await _drive(state, slot, events, monkeypatch)
        spy.assert_called_once()
        assert spy.call_args.args[3] == "monitor_start"


class TestKasWireTitleCarriesTheBackendPrefix:
    """A recorded KAS ``tool_call`` frame titled the MCP call
    ``Running: @kirocrew-core/ask_question`` -- the backend's own ``Running: ``
    prefix in front of the wrapper's ``@<server>/<tool>``. Matching the bare
    spelling only left that frame with no digest, so on a KAS session that emits
    no ``_meta.kiro`` the record parked and nothing claimed it: the tool answered
    "requested" and no card appeared."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Running: @kirocrew-core/ask_question", "ask_question"),
            ("Running: @kirocrew-core/monitor_start", "monitor_start"),
            ("@kirocrew-core/monitor_start", "monitor_start"),
            ("Running: @other-server/monitor_start", ""),
            ("Running: Running: @kirocrew-core/monitor_start", ""),  # one prefix, not a loop
            ("Loading tool: kirocrew-core::ask_question", ""),  # tool_search, not the call
            ("Running: echo @kirocrew-core/monitor_start", ""),
        ],
    )
    def test_prefixed_wire_title_resolves(self, title, expected):
        assert session_directive.directive_tool_from_call("", "", title) == expected

    @pytest.mark.asyncio
    async def test_a_recorded_kas_frame_without_meta_arms(self, tmp_path, monkeypatch):
        """End to end through the consumer: the frame as KAS sent it (prefixed wire
        title, no ``_meta.kiro``), the result body unreadable, the record parked
        under the input digest -- and the loop arms."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("kas-prefixed-title")
        events = _kas_events(_kas_offloaded(_tool_text()))
        events[0].title = "Running: @kirocrew-core/monitor_start"
        events[0].wire_title = "Running: @kirocrew-core/monitor_start"
        spy = await _drive(state, slot, events, monkeypatch)
        spy.assert_called_once()


class TestDisplayStripKeepsAnEnvelopeReadable:
    """``strip_marker`` is display-only, but cutting from the first sentinel
    to the END of the text. That is right for the tool's own shape (marker on the
    last line) and wrong for a KAS envelope, where the marker sits inside a JSON
    string and the cut left ``{"response":"Monitor loop requested...`` in the
    transcript with the rest of the envelope gone."""

    def test_tail_anchored_marker_is_cut_to_the_end(self):
        text = _tool_text()
        assert session_directive.strip_marker(text) == "Monitor loop requested."

    @pytest.mark.parametrize("shape", ["escaped", "duplicated", "offloaded"])
    def test_embedded_marker_is_replaced_in_place_and_the_json_still_parses(self, shape):
        wire = SHAPES[shape](_tool_text())
        shown = session_directive.strip_marker(wire)
        assert session_directive.SENTINEL not in shown
        assert "monitor_start" not in shown or "kind" not in shown, "payload removed"
        parsed = json.loads(shown)  # the envelope survives as valid JSON
        # Every envelope field is still there, and the human line is intact.
        assert "Monitor loop requested." in json.dumps(parsed)
        assert session_directive._DEFANGED in shown

    @pytest.mark.parametrize(
        "kind,args",
        [
            ("monitor_update", {"patch": {"message": 'say "hi" {twice}', "banner": "b"}}),
            (
                "ask_question",
                {"questions": [{"question": "pick {one}", "options": [{"label": "a"}]}]},
            ),
            (
                "suggest_followup",
                {"items": [{"title": "t", "description": "d", "prompt": "p {x}"}]},
            ),
        ],
    )
    @pytest.mark.parametrize("shape", ["duplicated", "escaped"])
    def test_deeply_nested_payloads_are_stripped_whole(self, kind, args, shape):
        """A depth-limited regex left ``{"kind":...,"args":{"patch":{...}}}`` in the
        transcript. Brace-counting removes the whole object, braces-in-strings and
        escaped quotes included, in both the plain and the re-serialised spelling."""
        text = session_directive.encode(kind, args, "Requested.")
        wire = SHAPES[shape](text)
        shown = session_directive.strip_marker(wire)
        assert session_directive.SENTINEL not in shown
        assert '"kind"' not in shown and '\\"kind\\"' not in shown, shown
        parsed = json.loads(shown)
        assert "Requested." in json.dumps(parsed)
        # And the plain (unenveloped, tail-anchored) form still cuts clean.
        assert session_directive.strip_marker(text) == "Requested."

    def test_embedded_plain_payload_mid_text_is_stripped_whole(self):
        # Marker not at line start and NOT inside a JSON envelope: plain spelling.
        text = (
            "prefix "
            + session_directive.encode(
                "monitor_update", {"patch": {"message": "x {y}"}}, "Requested."
            ).replace("\n", " ")
            + " suffix"
        )
        shown = session_directive.strip_marker(text)
        assert shown == f"prefix Requested. {session_directive._DEFANGED} suffix"

    @pytest.mark.parametrize(
        "message",
        [
            'say " }}SECRET_TAIL',  # a quote before the braces (GPT's reaching input)
            'a \\\\" }} b',  # backslash, then quote: the parity case
            'tab\\there " }} nl\\n end',  # other envelope escapes in between
            'unicode \u00e9 " }} \u4e2d end',
        ],
    )
    @pytest.mark.parametrize("shape", ["duplicated", "escaped", "offloaded"])
    @pytest.mark.parametrize(
        "kind,args_of",
        [
            ("monitor_start", lambda m: {"message": m}),  # message at depth 2: ``}}`` closes it
            ("monitor_update", lambda m: {"patch": {"message": m}}),  # depth 3
        ],
    )
    def test_escaped_quote_before_braces_does_not_leak_the_payload_tail(
        self, message, shape, kind, args_of
    ):
        """A message ``" }}X`` becomes ``\\\\\\" }}X`` on the KAS wire. Reading that
        ``\\"`` as a string delimiter (the ``\\\\`` before it makes it an escaped
        quote instead) closed the string early, ``}}`` then ended the object, and
        ``X`` plus the rest of the payload stayed in the transcript."""
        text = session_directive.encode(kind, args_of(message), "Ok.")
        wire = SHAPES[shape](text)
        shown = session_directive.strip_marker(wire)
        assert session_directive.SENTINEL not in shown
        assert "SECRET_TAIL" not in shown and "}} b" not in shown and "}} end" not in shown, shown
        assert '"kind"' not in shown and '\\"kind\\"' not in shown, shown
        parsed = json.loads(shown)  # the envelope is still valid JSON
        assert "Ok." in json.dumps(parsed)
        assert session_directive.strip_marker(text) == "Ok."

    def test_refusal_tag_embedded_is_replaced_too(self):
        wire = json.dumps(
            {"response": session_directive.tag_refusal("Error: nope"), "message": "x"}
        )
        shown = session_directive.strip_marker(wire)
        assert json.loads(shown)["response"].startswith("Error: nope")
        assert "REFUSED" not in shown


class TestRawArgsSnapshotIsDeep:
    """``_call_tool`` snapshots the raw arguments for the out-of-band report.
    ``suggest_followup``'s validator mutates nested items in place (pops an empty
    ``branch``), so a SHALLOW snapshot would report mutated args, the gateway would
    digest them, and the consumer -- digesting the frame's untouched rawInput --
    would never match. Silent card loss on a documented, ordinary input."""

    def test_reported_raw_args_are_what_the_model_sent(self, monkeypatch):
        posted: list[tuple[str, dict]] = []
        monkeypatch.setattr(mcp_core, "_post", lambda p, b, **kw: posted.append((p, b)))
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1-1")
        sent = {"items": [{"title": "t", "description": "d", "prompt": "p", "branch": ""}]}
        frame_copy = json.loads(json.dumps(sent))  # what the ACP frame carries
        mcp_core._call_tool("suggest_followup", sent)
        assert posted and posted[0][1]["raw_args"] == frame_copy
        digest_gateway = session_directive.call_input_digest(
            "suggest_followup", posted[0][1]["raw_args"]
        )
        digest_consumer = session_directive.call_input_digest("suggest_followup", frame_copy)
        assert digest_gateway == digest_consumer

    def test_gateway_derivation_does_not_mutate_the_args_it_digests(self):
        raw = {"items": [{"title": "t", "description": "d", "prompt": "p", "branch": ""}]}
        before = json.dumps(raw, sort_keys=True)
        derived = mcp_core.derive_directive("suggest_followup", raw, "dashboard:chat-1-1")
        assert derived is not None
        assert json.dumps(raw, sort_keys=True) == before


class TestLegacyParamsCacheIsTruthyGated:
    """Claude streams ``{}`` on the initial tool_call and the real arguments on the
    refinement. The permission event's trusted-params cache must not hold the ``{}``
    (a ``.get`` hit suppresses the inline-frame fallback, so governance would derive
    no sensitive-path scope), and the refinement must refresh it."""

    def test_initial_empty_input_is_not_cached_and_refinement_refreshes(self):
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage

        def msg(update):
            return JsonRpcMessage(method="session/update", params={"update": update})

        client = AcpClient()
        ev = client._extract_tool_event(
            msg(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-g",
                    "title": "Write",
                    "kind": "edit",
                    "rawInput": {},
                }
            )
        )
        assert ev is not None and ev.raw_tool_params == {}, "the event keeps the {}"
        assert "tc-g" not in client._tool_call_params, "the trusted cache does not"
        client._extract_tool_call_refinement(
            msg(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-g",
                    "title": "Write",
                    "rawInput": {"path": "/home/user/.ssh/id_rsa", "content": "x"},
                }
            )
        )
        assert client._tool_call_params.get("tc-g") == {
            "path": "/home/user/.ssh/id_rsa",
            "content": "x",
        }


class TestConcurrentDirectivesInOneSession:
    """The user's real concern: our OWN sessions all use the out-of-band queue.
    Several directives in one turn, frames interleaved, plus a native sub-agent
    issuing a DIFFERENT directive under the parent's key -- each frame must claim
    exactly its own record, nothing else, and the child's record must not leak to
    the parent."""

    @pytest.mark.asyncio
    async def test_interleaved_directives_each_claim_their_own(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("concurrent")
        slot._titled = True
        a_args = {"message": "loop A", "interval_secs": 300}
        b_args = {"message": "loop B", "interval_secs": 600}
        q_args = {"questions": [{"question": "which?", "options": [{"label": "x"}]}]}
        child_args: dict = {}

        def call(tcid, tool, args):
            return AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=tcid,
                title=f"@kirocrew-core/{tool}",
                wire_title=f"@kirocrew-core/{tool}",
                tool_kind="other",
                tool_name="",
                mcp_server_name="",
                raw_tool_params=args,
            )

        def result(tcid, tool, args):
            return AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=tcid,
                tool_final=True,
                tool_output=_kas_duplicated(session_directive.encode(tool, args, "Requested.")),
            )

        events = [
            AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=[
                    {
                        "sessionId": "sub-1",
                        "role": "r",
                        "initialQuery": "q",
                        "status": {"type": "working"},
                    }
                ],
            ),
            AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="sub-1", tool_call_id="tc-child"),
            call("tc-a", "monitor_start", a_args),
            call("tc-q", "ask_question", q_args),
            call("tc-child", "reset_conversation", child_args),
            call("tc-b", "monitor_start", b_args),
            # Results arrive out of call order.
            result("tc-b", "monitor_start", b_args),
            result("tc-child", "reset_conversation", child_args),
            result("tc-a", "monitor_start", a_args),
            result("tc-q", "ask_question", q_args),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE),
        ]
        from kiro_crew.dashboard import chat_runner

        parked = []

        async def _stream(_msg):
            if not parked:
                parked.append(True)
                sk = effective_session_key(slot)
                for tool, args in (
                    ("monitor_start", a_args),
                    ("ask_question", q_args),
                    ("reset_conversation", child_args),  # the child's, under the parent key
                    ("monitor_start", b_args),
                ):
                    kind, validated = mcp_core.derive_directive(tool, args, sk)
                    directive_queue.publish(
                        sk, kind, validated, session_directive.call_input_digest(tool, args)
                    )
            for ev in events:
                yield ev

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        spy = AsyncMock(return_value="[applied]")
        monkeypatch.setattr(chat_runner, "apply_session_directive", spy)
        await chat_runner._run_chat(state, slot, "go", _directive_user_origin=True)

        applied = [
            (c.args[3], c.args[4].get("message") or c.args[4].get("questions"))
            for c in spy.call_args_list
        ]
        assert ("monitor_start", "loop A") in applied
        assert ("monitor_start", "loop B") in applied
        assert any(k == "ask_question" for k, _ in applied)
        assert all(k != "reset_conversation" for k, _ in applied), "child never reaches parent"
        assert len(applied) == 3
        assert directive_queue.depth(effective_session_key(slot)) == 0, "child record retired"
