"""Eval scenarios can assert on the tools a turn called, not only on its reply text.

Three checks over the calls a turn made: ``tool_called`` (optionally with
arguments), ``tool_not_called``, and ``tool_order`` (an ordered trajectory). The
pure checks run against ``ToolCall`` values. The runner tests drive the real
``EvalRunner`` with a scripted provider, either with events built as the backend
parsers build them or with the events the real parsers make of a recorded
fixture, so the name and arguments a backend states are what the assertions read.
A call no frame names, or whose arguments no frame states, makes an assertion
fail with a reason rather than pass on what the turn could not see. A scenario
that does not use the new types runs exactly as before.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture

from kiro_crew.acp._dispatch import (
    build_permission_event,
    classify_notification,
    parse_session_update,
)
from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.cli_commands_eval import _load_user_scenarios
from kiro_crew.eval.runner import UNNAMED_TOOL_LABEL, EvalRunner, format_results
from kiro_crew.eval.scenario import (
    Assertion,
    AssertionType,
    Scenario,
    Session,
    ToolCall,
    Turn,
    parse_scenario_text,
)
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_crew.testing.synthetic_credentials import github_classic

T = AssertionType


def _calls(*names: str) -> list[ToolCall]:
    return [ToolCall(name=n) for n in names]


class TestToolCalled:
    def test_passes_when_the_tool_was_called(self) -> None:
        a = Assertion(T.TOOL_CALLED, "read_file")
        assert a.check("", _calls("grep", "read_file")) is True

    def test_fails_when_it_was_not(self) -> None:
        a = Assertion(T.TOOL_CALLED, "read_file")
        assert a.check("", _calls("grep")) is False
        assert a.check("", []) is False

    def test_the_name_must_match_whole_not_as_a_fragment(self) -> None:
        a = Assertion(T.TOOL_CALLED, "read")
        assert a.check("", _calls("read_file")) is False

    def test_case_insensitive_by_default_and_exact_on_request(self) -> None:
        assert Assertion(T.TOOL_CALLED, "READ_FILE").check("", _calls("read_file")) is True
        strict = Assertion(T.TOOL_CALLED, "READ_FILE", case_sensitive=True)
        assert strict.check("", _calls("read_file")) is False

    def test_an_mcp_tool_answers_to_its_bare_and_server_qualified_names(self) -> None:
        call = [ToolCall(name="memory_recall", server="kirocrew-core")]
        assert Assertion(T.TOOL_CALLED, "memory_recall").check("", call) is True
        assert Assertion(T.TOOL_CALLED, "kirocrew-core/memory_recall").check("", call) is True
        assert Assertion(T.TOOL_CALLED, "@kirocrew-core/memory_recall").check("", call) is True
        assert Assertion(T.TOOL_CALLED, "other/memory_recall").check("", call) is False

    def test_the_reply_text_plays_no_part(self) -> None:
        a = Assertion(T.TOOL_CALLED, "read_file")
        assert a.check("I called read_file for you", []) is False


class TestArgumentMatch:
    def test_every_named_argument_must_match(self) -> None:
        call = [ToolCall(name="read_file", args={"path": "/w/README.md", "limit": 50})]
        ok = Assertion(T.TOOL_CALLED, "read_file", args={"path": "README.md", "limit": 50})
        assert ok.check("", call) is True
        wrong = Assertion(T.TOOL_CALLED, "read_file", args={"path": "README.md", "limit": 51})
        assert wrong.check("", call) is False

    def test_a_missing_argument_does_not_match(self) -> None:
        call = [ToolCall(name="read_file", args={"path": "/w/a"})]
        a = Assertion(T.TOOL_CALLED, "read_file", args={"offset": 1})
        assert a.check("", call) is False

    def test_a_string_matches_as_a_fragment_of_a_string_argument_only(self) -> None:
        call = [ToolCall(name="run", args={"command": "git status --short", "count": 12})]
        assert Assertion(T.TOOL_CALLED, "run", args={"command": "GIT STATUS"}).check("", call)
        strict = Assertion(T.TOOL_CALLED, "run", args={"command": "GIT"}, case_sensitive=True)
        assert strict.check("", call) is False
        assert Assertion(T.TOOL_CALLED, "run", args={"count": "12"}).check("", call) is False

    def test_the_arguments_must_belong_to_one_call_of_that_name(self) -> None:
        calls = [
            ToolCall(name="read_file", args={"path": "a.txt"}),
            ToolCall(name="read_file", args={"path": "b.txt", "limit": 5}),
        ]
        both = Assertion(T.TOOL_CALLED, "read_file", args={"path": "a.txt", "limit": 5})
        assert both.check("", calls) is False
        assert Assertion(T.TOOL_CALLED, "read_file", args={"path": "b.txt"}).check("", calls)

    def test_tool_not_called_with_arguments_forbids_only_that_call(self) -> None:
        calls = [ToolCall(name="write_file", args={"path": "notes.md"})]
        a = Assertion(T.TOOL_NOT_CALLED, "write_file", args={"path": ".env"})
        assert a.check("", calls) is True
        b = Assertion(T.TOOL_NOT_CALLED, "write_file", args={"path": "notes"})
        assert b.check("", calls) is False


class TestToolNotCalled:
    def test_passes_when_absent_and_fails_when_present(self) -> None:
        a = Assertion(T.TOOL_NOT_CALLED, "web_fetch")
        assert a.check("", _calls("read_file")) is True
        assert a.check("", []) is True
        assert a.check("", _calls("read_file", "web_fetch")) is False


class TestToolOrder:
    def test_an_in_order_subsequence_passes_with_other_calls_between(self) -> None:
        a = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"))
        assert a.check("", _calls("grep", "ls", "read_file")) is True

    def test_the_wrong_order_fails(self) -> None:
        a = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"))
        assert a.check("", _calls("read_file", "grep")) is False

    def test_a_missing_step_fails(self) -> None:
        a = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"))
        assert a.check("", _calls("grep")) is False

    def test_a_repeated_step_needs_the_tool_to_be_called_that_many_times(self) -> None:
        a = Assertion(T.TOOL_ORDER, "", tools=("read_file", "read_file"))
        assert a.check("", _calls("read_file")) is False
        assert a.check("", _calls("read_file", "grep", "read_file")) is True

    def test_exact_allows_no_other_call(self) -> None:
        a = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"), exact=True)
        assert a.check("", _calls("grep", "read_file")) is True
        assert a.check("", _calls("grep", "ls", "read_file")) is False
        assert a.check("", _calls("grep")) is False
        assert a.check("", _calls("grep", "read_file", "ls")) is False


class TestCallsTheTurnCannotSee:
    """A call no frame named, or one whose arguments no frame stated, never lets an
    assertion pass on a guess: it fails, and the verdict says why."""

    def test_tool_not_called_fails_when_a_call_named_no_tool(self) -> None:
        a = Assertion(T.TOOL_NOT_CALLED, "write_file")
        ok, reason = a.verdict("", [ToolCall(name="grep"), ToolCall(name=None)])
        assert ok is False
        assert reason == "unknown: 1 of the turn's calls named no tool"
        assert a.verdict("", _calls("grep")) == (True, "")

    def test_a_named_match_still_decides_beside_an_unnamed_call(self) -> None:
        calls = [ToolCall(name=None), ToolCall(name="write_file")]
        assert Assertion(T.TOOL_NOT_CALLED, "write_file").verdict("", calls) == (False, "")
        assert Assertion(T.TOOL_CALLED, "write_file").verdict("", calls) == (True, "")

    def test_tool_called_says_why_when_only_an_unnamed_call_could_be_it(self) -> None:
        a = Assertion(T.TOOL_CALLED, "read_file")
        assert a.verdict("", [ToolCall(name=None)]) == (
            False,
            "unknown: 1 of the turn's calls named no tool",
        )
        assert a.verdict("", _calls("grep")) == (False, "")

    def test_an_order_cannot_be_exact_around_an_unnamed_call(self) -> None:
        calls = [ToolCall(name="grep"), ToolCall(name=None), ToolCall(name="read_file")]
        exact = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"), exact=True)
        assert exact.verdict("", calls)[0] is False
        loose = Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file"))
        assert loose.verdict("", calls) == (True, "")
        backwards = Assertion(T.TOOL_ORDER, "", tools=("read_file", "grep"))
        assert backwards.verdict("", calls) == (
            False,
            "unknown: 1 of the turn's calls named no tool",
        )

    def test_unstated_arguments_neither_match_nor_clear_a_forbidden_call(self) -> None:
        calls = [ToolCall(name="write_file", args=None)]
        forbid = Assertion(T.TOOL_NOT_CALLED, "write_file", args={"path": ".env"})
        assert forbid.verdict("", calls) == (
            False,
            "unknown: a call to this tool stated no arguments",
        )
        need = Assertion(T.TOOL_CALLED, "write_file", args={"path": ".env"})
        assert need.verdict("", calls) == (
            False,
            "unknown: a call to this tool stated no arguments",
        )
        assert Assertion(T.TOOL_CALLED, "write_file").verdict("", calls) == (True, "")

    def test_a_text_assertion_has_no_reason(self) -> None:
        assert Assertion(T.CONTAINS, "ok").verdict("ok", [ToolCall(name=None)]) == (True, "")


class TestTextAssertionsUnchanged:
    def test_the_text_types_still_check_the_reply_and_ignore_calls(self) -> None:
        assert Assertion(T.CONTAINS, "rust").check("I love Rust") is True
        assert Assertion(T.CONTAINS, "rust").check("I love Rust", _calls("x")) is True
        assert Assertion(T.NOT_CONTAINS, "error").check("fine", _calls("error")) is True
        assert Assertion(T.EQUALS, "yes").check(" yes ") is True
        assert Assertion(T.REGEX, "go|Go").check("We use Go") is True

    def test_a_judge_assertion_still_defers(self) -> None:
        assert Assertion(T.JUDGE, "was it helpful").check("anything") is True


class TestParsing:
    def test_a_scenario_with_the_new_types_loads(self) -> None:
        doc = {
            "name": "tools",
            "sessions": [
                {
                    "name": "s",
                    "turns": [
                        {
                            "user": "read the config",
                            "assertions": [
                                {
                                    "type": "tool_called",
                                    "value": "read_file",
                                    "args": {"path": "config"},
                                },
                                {
                                    "type": "tool_not_called",
                                    "value": "web_fetch",
                                    "case_sensitive": True,
                                },
                                {
                                    "type": "tool_order",
                                    "tools": ["grep", "read_file"],
                                    "exact": True,
                                },
                                {"type": "contains", "value": "ok"},
                            ],
                        }
                    ],
                }
            ],
        }
        scenario = parse_scenario_text(json.dumps(doc), ".json")
        called, not_called, order, text = scenario.sessions[0].turns[0].assertions
        assert (called.type, called.value, called.args) == (
            T.TOOL_CALLED,
            "read_file",
            {"path": "config"},
        )
        assert (not_called.type, not_called.case_sensitive) == (T.TOOL_NOT_CALLED, True)
        assert (order.type, order.tools, order.exact) == (T.TOOL_ORDER, ("grep", "read_file"), True)
        assert (text.type, text.args, text.tools, text.exact) == (T.CONTAINS, {}, (), False)

    def test_yaml_scenarios_load_the_same_way(self) -> None:
        text = (
            "name: tools\n"
            "sessions:\n"
            "  - name: s\n"
            "    turns:\n"
            "      - user: go\n"
            "        assertions:\n"
            "          - {type: tool_order, tools: [grep, read_file]}\n"
        )
        scenario = parse_scenario_text(text, ".yaml")
        assert scenario.sessions[0].turns[0].assertions[0].tools == ("grep", "read_file")

    def test_an_unknown_type_is_still_refused_as_before(self) -> None:
        doc = {
            "name": "x",
            "sessions": [
                {
                    "name": "s",
                    "turns": [
                        {"user": "u", "assertions": [{"type": "tool_was_called", "value": "a"}]}
                    ],
                }
            ],
        }
        with pytest.raises(ValueError, match="tool_was_called"):
            parse_scenario_text(json.dumps(doc), ".json")

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "tool_called"},
            {"type": "tool_not_called", "value": ""},
            {"type": "tool_called", "value": 7},
            {"type": "tool_called", "value": "a", "args": ["path"]},
            {"type": "tool_order"},
            {"type": "tool_order", "tools": []},
            {"type": "tool_order", "tools": "grep"},
            {"type": "tool_order", "tools": ["grep", ""]},
            {"type": "tool_order", "tools": ["grep"], "exact": "false"},
        ],
    )
    def test_a_tool_assertion_that_cannot_be_checked_does_not_load(
        self, bad: dict[str, Any]
    ) -> None:
        doc = {
            "name": "x",
            "sessions": [{"name": "s", "turns": [{"user": "u", "assertions": [bad]}]}],
        }
        with pytest.raises(ValueError, match=rf"{bad['type']} (needs|'\w+' must be)"):
            parse_scenario_text(json.dumps(doc), ".json")

    def test_the_extra_keys_mean_nothing_on_a_text_assertion(self) -> None:
        doc = {
            "name": "x",
            "sessions": [
                {
                    "name": "s",
                    "turns": [
                        {
                            "user": "u",
                            "assertions": [
                                {
                                    "type": "contains",
                                    "value": "a",
                                    "args": 3,
                                    "tools": 4,
                                    "exact": "no",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        a = parse_scenario_text(json.dumps(doc), ".json").sessions[0].turns[0].assertions[0]
        assert (a.type, a.value) == (T.CONTAINS, "a")

    def test_a_user_scenario_file_with_the_new_types_loads_through_dir(
        self, tmp_path: Path
    ) -> None:
        doc = {
            "name": "tools",
            "sessions": [
                {
                    "name": "s",
                    "turns": [
                        {"user": "u", "assertions": [{"type": "tool_called", "value": "read_file"}]}
                    ],
                }
            ],
        }
        (tmp_path / "tools.json").write_text(json.dumps(doc), encoding="utf-8")
        args = argparse.Namespace(
            scenarios=[], all_scenarios=False, eval_dir=str(tmp_path), eval_file=None
        )
        loaded = _load_user_scenarios(args)
        assert loaded is not None
        assert loaded[0].sessions[0].turns[0].assertions[0].type == T.TOOL_CALLED


class _ToolProvider:
    """Emits the events it is given, then a text reply."""

    cwd = ""

    def __init__(self, events: list[LLMEvent]) -> None:
        self._events = events

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def set_workspace(self, path: Any) -> None:
        pass

    async def stream(self, message: str, **_kwargs: Any):
        for event in self._events:
            yield event
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: Any) -> None:
        pass

    async def reject_tool(self, request_id: Any) -> None:
        pass

    def context_usage_pct(self) -> float:
        return 0.0


def _call(name: str, params: dict[str, Any] | None = None, server: str = "") -> LLMEvent:
    """A tool-call event as the backend parser builds it: no ``text``, a trusted name."""
    return LLMEvent(
        kind=EVENT_TOOL_CALL,
        title=f"Running {name}",
        tool_name=name,
        mcp_server_name=server,
        tool_input=json.dumps(params) if params else "",
        raw_tool_params=params,
        tool_call_id=f"id-{name}",
    )


async def _run(events: list[LLMEvent], *assertions: Assertion):
    scenario = Scenario(
        name="tools",
        sessions=[Session(name="s", turns=[Turn(user="go", assertions=list(assertions))])],
    )
    runner = EvalRunner(provider_factory=lambda key, **kw: _ToolProvider(events))
    result = await runner.run_scenario(scenario)
    return result, result.sessions[0].turns[0]


async def _turn(events: list[LLMEvent], *assertions: Assertion):
    """One turn through ``EvalRunner._run_turn``, the loop that reads the events."""
    provider = _ToolProvider(events)
    runner = EvalRunner(provider_factory=lambda key, **kw: provider)
    return await runner._run_turn(provider, Turn(user="go", assertions=list(assertions)), "eval-s")


def _replayed(fixture: str, backend: str, frames: slice = slice(None)) -> list[LLMEvent]:
    """The events the real parsers make of a recorded fixture, given the caches and the
    backend a live session's handle gives them, so each call is named and argued as a
    real turn on that backend sees it."""
    _meta, recorded = read_fixture(CORPUS / fixture)
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "tool_input_redacted_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "diff_path_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
        "cache_scope": "eval",
    }
    events: list[LLMEvent] = []
    for raw in recorded[frames]:
        msg = JsonRpcMessage.from_dict(raw)
        if msg.method is None:
            continue
        action = classify_notification(msg)
        if action == "permission":
            event, _options = build_permission_event(
                msg, harness_backend=backend, kas_consent_meta=backend == ACP_BACKEND_KAS, **caches
            )
            if event is not None:
                events.append(event)
        elif action == "update":
            update = (msg.params or {}).get("update") or {}
            events.extend(parse_session_update(update, harness_backend=backend, **caches))
    return events


class TestRunner:
    @pytest.mark.asyncio
    async def test_a_turn_is_scored_on_the_calls_it_made(self) -> None:
        events = [
            _call("grep", {"pattern": "TODO"}),
            _call("read_file", {"path": "/w/src/app.py"}),
        ]
        result, turn = await _run(
            events,
            Assertion(T.TOOL_CALLED, "read_file", args={"path": "app.py"}),
            Assertion(T.TOOL_NOT_CALLED, "write_file"),
            Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file")),
        )
        assert [ok for _, ok in turn.assertion_results] == [True, True, True]
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_a_wrong_trajectory_fails_the_turn(self) -> None:
        events = [_call("read_file", {"path": "a"}), _call("grep", {"pattern": "x"})]
        result, turn = await _run(
            events,
            Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file")),
            Assertion(T.TOOL_CALLED, "read_file", args={"path": "b"}),
        )
        assert [ok for _, ok in turn.assertion_results] == [False, False]
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_an_mcp_call_is_matched_by_server_and_tool(self) -> None:
        events = [_call("memory_recall", {"query": "x"}, server="kirocrew-core")]
        _result, turn = await _run(events, Assertion(T.TOOL_CALLED, "kirocrew-core/memory_recall"))
        assert turn.assertion_results[0][1] is True

    @pytest.mark.asyncio
    async def test_the_display_input_is_not_read_as_the_arguments(self) -> None:
        event = _call("read_file", {})
        event.tool_input = json.dumps({"path": "/w/a.txt"})
        turn = await _turn(
            [event], Assertion(T.TOOL_NOT_CALLED, "read_file", args={"path": "a.txt"})
        )
        assert turn.assertion_results[0][1] is False
        assert turn.assertion_notes == ["unknown: a call to this tool stated no arguments"]

    @pytest.mark.asyncio
    async def test_no_calls_at_all(self) -> None:
        _result, turn = await _run(
            [],
            Assertion(T.TOOL_CALLED, "read_file"),
            Assertion(T.TOOL_NOT_CALLED, "read_file"),
        )
        assert [ok for _, ok in turn.assertion_results] == [False, True]

    @pytest.mark.asyncio
    async def test_tool_calls_keeps_one_name_per_call(self) -> None:
        events = [_call("grep"), LLMEvent(kind=EVENT_TOOL_CALL, text="read_file")]
        _result, turn = await _run(events)
        assert turn.tool_calls == ["grep", "read_file"]

    @pytest.mark.asyncio
    async def test_text_assertions_score_as_before_beside_the_new_ones(self) -> None:
        _result, turn = await _run(
            [_call("grep")],
            Assertion(T.CONTAINS, "done"),
            Assertion(T.NOT_CONTAINS, "grep"),
            Assertion(T.TOOL_CALLED, "grep"),
        )
        assert [ok for _, ok in turn.assertion_results] == [True, True, True]

    @pytest.mark.asyncio
    async def test_a_harness_id_names_a_call_unless_it_names_no_single_tool(self) -> None:
        stated = {
            "a": "opencode#bash",
            "b": "opencode#@kirocrew-core/monitor_start",
            "c": "opencode#@kirocrew-core/monitor_start|@kirocrew/core_monitor_start",
            "d": "claude#mcp__kirocrew-core__monitor_start",
        }
        events: list[LLMEvent] = []
        for call_id, tool_id in stated.items():
            events.append(LLMEvent(kind=EVENT_TOOL_CALL, title=tool_id, tool_call_id=call_id))
            events.append(
                LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title=tool_id,
                    tool_call_id=call_id,
                    harness_tool_id=tool_id,
                    request_id=call_id,
                )
            )
        turn = await _turn(events, Assertion(T.TOOL_CALLED, "kirocrew-core/monitor_start"))
        assert turn.tool_calls == ["bash", "monitor_start", "", ""]
        assert turn.tool_call_records[1].server == "kirocrew-core"
        assert turn.assertion_results[0][1] is True


class TestRecordedFrames:
    """The calls a real turn makes, from frames recorded off each backend's wire."""

    @pytest.mark.asyncio
    async def test_claude_calls_are_named_by_the_harness_and_argued_by_their_updates(
        self,
    ) -> None:
        # Read .env, Glob, Grep: each opens with ``rawInput: {}`` under a display title
        # ("Read File", "Find", "grep"), streams its input in updates, then asks.
        events = _replayed("claude/side-read-ask-live.jsonl", ACP_BACKEND_CLAUDE, slice(0, 17))
        turn = await _turn(
            events,
            Assertion(T.TOOL_CALLED, "Read", args={"file_path": ".env"}),
            Assertion(T.TOOL_NOT_CALLED, "Read", args={"file_path": ".env"}),
            Assertion(T.TOOL_CALLED, "Glob", args={"pattern": ".env", "path": "/work"}),
            Assertion(T.TOOL_ORDER, "", tools=("Read", "Glob", "Grep"), exact=True),
        )
        assert turn.tool_calls == ["Read", "Glob", "Grep"]
        assert [ok for _, ok in turn.assertion_results] == [True, False, True, True]

    @pytest.mark.asyncio
    async def test_claude_calls_that_never_asked_are_unnamed_and_block_a_clean_pass(
        self,
    ) -> None:
        # The last three calls in this capture failed without a permission request, so
        # no frame names them.
        events = _replayed("claude/side-read-ask-live.jsonl", ACP_BACKEND_CLAUDE)
        turn = await _turn(events, Assertion(T.TOOL_NOT_CALLED, "Bash"))
        assert turn.tool_calls[-3:] == ["", "", ""]
        assert turn.assertion_results[0][1] is False
        assert turn.assertion_notes == ["unknown: 3 of the turn's calls named no tool"]

    @pytest.mark.asyncio
    async def test_kas_built_ins_are_named_by_their_permission_requests(self) -> None:
        events = _replayed("kas/side-read-only-live.jsonl", ACP_BACKEND_KAS)
        turn = await _turn(
            events,
            Assertion(T.TOOL_CALLED, "read_file", args={"path": "note.txt"}),
            Assertion(T.TOOL_ORDER, "", tools=("read_file", "grep_search", "fs_write")),
            Assertion(T.TOOL_NOT_CALLED, "fs_write", args={"path": "created.txt"}),
        )
        assert turn.tool_calls == ["", "read_file", "grep_search", "run_command", "fs_write"]
        assert [ok for _, ok in turn.assertion_results] == [True, True, False]

    @pytest.mark.asyncio
    async def test_a_kas_mcp_call_is_named_bare_and_its_arguments_unwrapped(self) -> None:
        events = _replayed("kas/mcp-live-apply-live.jsonl", ACP_BACKEND_KAS, slice(0, 10))
        turn = await _turn(
            events,
            Assertion(T.TOOL_CALLED, "probe_a_echo", args={"text": "one"}),
            Assertion(T.TOOL_CALLED, "probe-a/probe_a_echo"),
            Assertion(T.TOOL_NOT_CALLED, "probe_a_echo", args={"text": "one"}),
        )
        assert [ok for _, ok in turn.assertion_results] == [True, True, False]

    @pytest.mark.asyncio
    async def test_a_codex_mcp_call_is_matched_on_its_own_arguments(self) -> None:
        events = _replayed("codex/crew-oauth-bridge-args-live.jsonl", ACP_BACKEND_CODEX)
        turn = await _turn(
            events,
            Assertion(T.TOOL_CALLED, "github/get_issue", args={"issue_number": 7}),
            Assertion(T.TOOL_NOT_CALLED, "get_issue", args={"issue_number": 7}),
        )
        assert [ok for _, ok in turn.assertion_results] == [True, False]


#: A request header a shell command can carry. Built at run time, so the file holds no
#: credential-shaped literal.
_CANARY = "opaque-eval-" + "canary-0123456789"
_BEARER = "Authorization: " + "Bearer " + _CANARY
_TOKEN = github_classic(seed=174)


def _codex_shell_call(command: str) -> LLMEvent:
    """A codex shell ``tool_call`` frame (its title is the command) through the real parser."""
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_0",
        "title": command,
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"command": command, "cwd": "/work"},
    }
    events = parse_session_update(update, harness_backend=ACP_BACKEND_CODEX)
    assert events and events[0].wire_title == command
    return events[0]


class TestReport:
    @pytest.mark.asyncio
    async def test_the_report_names_each_tool_assertion(self) -> None:
        result, _turn = await _run(
            [_call("grep")],
            Assertion(T.TOOL_CALLED, "grep"),
            Assertion(T.TOOL_ORDER, "", tools=("grep", "read_file")),
        )
        report = format_results([result])
        assert "tool_called: `grep`" in report
        assert "tool_order: `grep > read_file`" in report
        assert "Tools: grep" in report

    @pytest.mark.asyncio
    async def test_a_tool_name_with_terminal_controls_is_not_printed_raw(self) -> None:
        result, _turn = await _run([_call("grep\x1b[31m\x07")])
        report = format_results([result])
        assert "Tools: grep" in report
        assert "\x1b" not in report
        assert "\x07" not in report

    @pytest.mark.asyncio
    async def test_a_credential_in_a_wire_title_never_reaches_the_report(self) -> None:
        event = _codex_shell_call(f"curl -H '{_BEARER}' https://example.invalid/issues")
        result, turn = await _run([event], Assertion(T.TOOL_NOT_CALLED, "write_file"))
        report = format_results([result])
        assert _CANARY not in report
        assert "Tools: exec_command" in report
        assert turn.tool_calls == ["exec_command"]

    @pytest.mark.asyncio
    async def test_a_credential_shaped_tool_name_is_redacted_when_printed(self) -> None:
        name = "lookup_" + _TOKEN
        result, turn = await _run([_call(name)], Assertion(T.TOOL_CALLED, name))
        assert turn.assertion_results[0][1] is True
        report = format_results([result])
        assert _TOKEN not in report
        assert "Tools: lookup_" in report

    @pytest.mark.asyncio
    async def test_an_unnamed_call_prints_the_placeholder_and_the_reason(self) -> None:
        unnamed = LLMEvent(
            kind=EVENT_TOOL_CALL, title="Read File", wire_title="Read File", tool_call_id="t1"
        )
        result, _turn = await _run([unnamed], Assertion(T.TOOL_NOT_CALLED, "read_file"))
        report = format_results([result])
        assert f"Tools: {UNNAMED_TOOL_LABEL}" in report
        assert "Read File" not in report
        assert (
            "tool_not_called: `read_file` (unknown: 1 of the turn's calls named no tool)" in report
        )


class TestAuditRow:
    @pytest.mark.asyncio
    async def test_the_invoked_row_records_the_name_the_assertions_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.eval.runner.sel", lambda: audit)
        await _turn([_call("read_file", {"path": "a"}), _call("lookup_" + _TOKEN)])
        rows = [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs["outcome"] == "invoked"
        ]
        assert rows[0]["tool_name"] == "read_file"
        assert rows[1]["tool_name"].startswith("lookup_")
        assert _TOKEN not in rows[1]["tool_name"]
