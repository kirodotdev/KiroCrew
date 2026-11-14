"""Eval scenarios can assert on the tools a turn called, not only on its reply text.

Four checks over the calls a turn made: ``tool_called`` (optionally with
arguments), ``tool_not_called``, and ``tool_order`` (an ordered trajectory). The
pure checks run against ``ToolCall`` values; the runner tests drive the real
``EvalRunner`` with a scripted provider that emits real-shaped tool-call events
(the trusted ``tool_name``, ``mcp_server_name`` and ``raw_tool_params`` fields,
and no ``text``), so the name and arguments a real backend reports are what the
assertions read. A scenario that does not use the new types runs exactly as before.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.cli_commands_eval import _load_user_scenarios
from kiro_crew.eval.runner import EvalRunner, format_results
from kiro_crew.eval.scenario import (
    Assertion,
    AssertionType,
    Scenario,
    Session,
    ToolCall,
    Turn,
    parse_scenario_text,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL

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
    """Emits the tool calls it is given as real-shaped events, then a text reply."""

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
    async def test_arguments_come_from_the_input_text_when_no_params_were_parsed(self) -> None:
        event = _call("read_file")
        event.tool_input = json.dumps({"path": "/w/a.txt"})
        _result, turn = await _run(
            [event], Assertion(T.TOOL_CALLED, "read_file", args={"path": "a.txt"})
        )
        assert turn.assertion_results[0][1] is True

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
