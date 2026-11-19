"""``kirocrew eval capture KEY`` saves a recorded chat as an eval scenario file.

A real ``ConversationLog`` in ``tmp_path`` holds the chats. The captured file is loaded
with the real scenario loader and run through the real ``_run_eval`` with ``--file``;
only the model is replaced, so no backend starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import cli_parser_admin
from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.eval.capture import (
    MAX_TOOLS_PER_TURN,
    MAX_TURNS,
    MAX_USER_CHARS,
    build_scenario,
    run_capture,
    scenario_name,
)
from kiro_crew.eval.scenario import AssertionType, load_scenario
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.testing.synthetic_credentials import github_classic

_KEY = "dashboard:chat-7-1700000000"


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path: Path):
    _floor_monkeypatch.chdir(tmp_path)
    # The eval runner makes its scratch folder with tempfile; keeping that under
    # tmp_path means anything it leaves is removed with the test's own folder.
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    _floor_monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def log(tmp_path: Path) -> ConversationLog:
    return ConversationLog(base_dir=tmp_path / "sessions")


def _chat(log: ConversationLog, key: str = _KEY) -> None:
    log.append(key, "user", "list my open tickets")
    log.append(
        key, "tool", "🔧 Read", extra_meta={"tool_name": "read_tickets", "mcp_server": "desk"}
    )
    log.append(key, "tool", "🔧 Display only title")
    log.append(key, "tool", "🔧 Write", extra_meta={"tool_name": "write_summary"})
    log.append(key, "assistant", "You have two.")
    log.append(key, "user", "thanks")


def _args(*words: str, **over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"scenarios": ["capture", *words], "out": None, "eval_force": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_a_chat_becomes_a_scenario_the_loader_reads(log, tmp_path, capsys) -> None:
    _chat(log)

    code = run_capture(_args(_KEY), log)

    assert code == 0
    path = tmp_path / "evals" / f"{scenario_name('dashboard_chat-7-1700000000')}.json"
    scenario = load_scenario(path)
    first, second = scenario.sessions[0].turns
    assert (first.user, second.user) == ("list my open tickets", "thanks")
    kinds = [(a.type, a.value or a.tools) for a in first.assertions]
    assert kinds == [
        (AssertionType.TOOL_CALLED, "read_tickets"),
        (AssertionType.TOOL_CALLED, "write_summary"),
        (AssertionType.TOOL_ORDER, ("read_tickets", "write_summary")),
        (AssertionType.JUDGE, kinds[-1][1]),
    ]
    assert [a.type for a in second.assertions] == [AssertionType.JUDGE]
    out = capsys.readouterr().out
    assert "1 tool call(s) recorded no tool name" in out
    assert "kirocrew eval --file" in out


def test_the_rows_one_call_leaves_count_once(log, tmp_path) -> None:
    log.append(_KEY, "user", "run the build")
    for title in ("🔧 Running: make", "🚫 Running: make (rejected)"):
        log.append(_KEY, "tool", title, extra_meta={"tool_call_id": "c1", "tool_name": "shell"})
    log.append(_KEY, "tool", "🔧 Running: make test", extra_meta={"tool_call_id": "c2"})
    log.append(
        _KEY,
        "tool",
        "✅ Running: make test",
        extra_meta={"tool_call_id": "c2", "tool_name": "shell"},
    )

    scenario, omitted = build_scenario(log.read_messages(_KEY), "n")

    (turn,) = scenario["sessions"][0]["turns"]
    assert [(a["type"], a.get("value") or a.get("tools")) for a in turn["assertions"][:-1]] == [
        ("tool_called", "shell"),
        ("tool_order", ["shell", "shell"]),
    ]
    assert omitted.unnamed == 0


@pytest.mark.parametrize("role", ["inject", "nudge", "subagent"])
def test_an_automated_turn_s_tool_calls_stay_out_of_the_user_turn(role, log, capsys) -> None:
    log.append(_KEY, "user", "check the queue")
    log.append(_KEY, "tool", "🔧 Read", extra_meta={"tool_name": "read_queue", "tool_call_id": "a"})
    log.append(_KEY, "assistant", "The queue is empty.")
    opener_meta = {"injectKind": "cron"} if role == "inject" else None
    log.append(_KEY, role, "scheduled report is due", extra_meta=opener_meta)
    for call in ("b", "c"):
        log.append(
            _KEY, "tool", "🔧 Send", extra_meta={"tool_name": "send_report", "tool_call_id": call}
        )
    log.append(
        _KEY, "tool", "✅ Send", extra_meta={"tool_name": "send_report", "tool_call_id": "c"}
    )
    log.append(_KEY, "user", "thanks")
    log.append(_KEY, "tool", "🔧 Note", extra_meta={"tool_name": "write_note"})

    scenario, omitted = build_scenario(log.read_messages(_KEY), "n")

    first, second = scenario["sessions"][0]["turns"]
    assert [a.get("value") for a in first["assertions"][:-1]] == ["read_queue"]
    assert [a.get("value") for a in second["assertions"][:-1]] == ["write_note"]
    assert omitted.automated == 2
    assert run_capture(_args(_KEY), log) == 0
    assert "left out 2 tool call(s) made in automated turns" in capsys.readouterr().out


@pytest.mark.parametrize("meta", [None, {"injectKind": "recovery"}, {"injectKind": "user_replay"}])
def test_an_inject_that_opens_no_new_work_keeps_the_user_turn(meta, log) -> None:
    # A refusal note (no injectKind) or a recovery is written mid-turn; the agent
    # keeps working in the same turn, so the calls after it stay the user's.
    log.append(_KEY, "user", "update the docs")
    log.append(_KEY, "tool", "🔧 Read", extra_meta={"tool_name": "read_file", "tool_call_id": "a"})
    log.append(_KEY, "inject", "that call was refused; continue another way", extra_meta=meta)
    log.append(_KEY, "tool", "🔧 Edit", extra_meta={"tool_name": "edit_file", "tool_call_id": "b"})

    scenario, omitted = build_scenario(log.read_messages(_KEY), "n")

    (turn,) = scenario["sessions"][0]["turns"]
    assert ("tool_order", ["read_file", "edit_file"]) in [
        (a["type"], a.get("tools")) for a in turn["assertions"]
    ]
    assert omitted.automated == 0


def test_a_user_row_that_is_not_text_closes_the_turn_before_it() -> None:
    rows = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "   "},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "orphan"}},
        {"role": "user", "content": "second"},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "kept"}},
    ]

    scenario, omitted = build_scenario(rows, "n")

    first, second = scenario["sessions"][0]["turns"]
    assert [a["type"] for a in first["assertions"]] == ["judge"]
    assert second["assertions"][0]["value"] == "kept"
    assert omitted.automated == 1


def test_a_requeued_steer_is_captured_once_as_the_turn_that_ran_it() -> None:
    # A steer the turn never took went to the queue; its own row is the turn that ran
    # it, and the first row, marked superseded, keeps the same text.
    steer = {"steer": True, "steerState": "superseded"}
    rows = [
        {"role": "user", "content": "first"},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "read_file", "tool_call_id": "a"}},
        {"role": "user", "content": "also fix the tests", "meta": steer},
        {"role": "user", "content": "also fix the tests"},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "run_tests", "tool_call_id": "b"}},
    ]

    scenario, omitted = build_scenario(rows, "n")

    first, second = scenario["sessions"][0]["turns"]
    assert [first["user"], second["user"]] == ["first", "also fix the tests"]
    assert first["assertions"][0]["value"] == "read_file"
    assert second["assertions"][0]["value"] == "run_tests"
    assert omitted.automated == 0


@pytest.mark.parametrize(("state", "text"), [("retracted", ""), ("requeued", "also fix the tests")])
def test_a_steer_the_turn_never_took_keeps_the_turn_it_was_sent_into(state, text) -> None:
    # A taken-back steer stays as an empty row; a requeued one waits for its own turn.
    rows = [
        {"role": "user", "content": "update the docs"},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "read_file", "tool_call_id": "a"}},
        {"role": "user", "content": text, "meta": {"steer": True, "steerState": state}},
        {"role": "tool", "content": "🔧", "meta": {"tool_name": "edit_file", "tool_call_id": "b"}},
    ]

    scenario, omitted = build_scenario(rows, "n")

    (turn,) = scenario["sessions"][0]["turns"]
    assert ("tool_order", ["read_file", "edit_file"]) in [
        (a["type"], a.get("tools")) for a in turn["assertions"]
    ]
    assert omitted.automated == 0


def test_all_scenarios_keeps_main_s_run(log, capsys) -> None:
    ran: list[str] = []

    def fake_capture(args: Any, *_a: Any) -> int:
        ran.append("capture")
        return 0

    with (
        patch("kiro_crew.eval.capture.run_capture", fake_capture),
        patch.object(cc, "_load_eval_scenarios", return_value=None),
    ):
        with pytest.raises(SystemExit) as stopped:
            asyncio.run(cc._run_eval(_args(_KEY, all_scenarios=True)))

    assert (ran, stopped.value.code) == ([], 1)


def test_credentials_in_a_user_turn_are_not_written(log, tmp_path) -> None:
    secret = github_classic()
    log.append(_KEY, "user", f"use token {secret} to push")

    assert run_capture(_args(_KEY), log) == 0

    (written,) = (tmp_path / "evals").glob("*.json")
    assert secret not in written.read_text(encoding="utf-8")


def test_a_long_message_is_cut_after_redaction() -> None:
    rows = [{"role": "user", "content": "x" * (MAX_USER_CHARS + 50)}]

    scenario, omitted = build_scenario(rows, "n")

    assert len(scenario["sessions"][0]["turns"][0]["user"]) == MAX_USER_CHARS
    assert (omitted.turns, omitted.cut) == (0, 1)


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
def test_a_restricted_chat_is_never_written(log, tmp_path, capsys, mode) -> None:
    _chat(log)
    log.update_metadata(_KEY, {"memory_mode": mode})

    assert run_capture(_args(_KEY), log) == 1

    assert not (tmp_path / "evals").exists()
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "nothing captured" in err
    assert "tickets" not in err


def test_an_unknown_chat_and_a_chat_with_no_user_turn_write_nothing(log, tmp_path, capsys) -> None:
    assert run_capture(_args("chat-nope"), log) == 1
    log.append("dashboard:chat-empty", "assistant", "hello")
    assert run_capture(_args("chat-empty"), log) == 1

    assert not (tmp_path / "evals").exists()
    assert "no recorded chat" in capsys.readouterr().err


def test_a_chat_tab_name_without_its_prefix_finds_the_chat(log, tmp_path) -> None:
    _chat(log)

    assert run_capture(_args("chat-7-1700000000"), log) == 0


def test_a_missing_or_extra_key_prints_usage(log, capsys) -> None:
    assert run_capture(_args(), log) == 1
    assert run_capture(_args("a", "b"), log) == 1

    assert capsys.readouterr().err.count("usage: kirocrew eval capture") == 2


def test_an_existing_file_is_kept_unless_forced(log, tmp_path, capsys) -> None:
    _chat(log)
    out = tmp_path / "mine" / "t.json"
    out.parent.mkdir()
    out.write_text("edited by hand", encoding="utf-8")

    assert run_capture(_args(_KEY, out=str(out)), log) == 1
    assert out.read_text(encoding="utf-8") == "edited by hand"
    assert "--force" in capsys.readouterr().err

    assert run_capture(_args(_KEY, out=str(out), eval_force=True), log) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["name"].startswith("captured_")


def test_the_scenario_is_written_by_the_guarded_writers(log, tmp_path) -> None:
    from kiro_crew.eval.bench import safepath

    _chat(log)
    seen: list[str] = []

    def record_replace(path: Any, text: str, *, what: str) -> None:
        seen.append("replace " + what)

    def record_create(path: Any, *, what: str) -> int:
        seen.append("create " + what)
        raise safepath.UnsafePathError("refused for the test")

    with (
        patch.object(safepath, "write_text_atomic_nofollow", record_replace),
        patch.object(safepath, "open_write_nofollow", record_create),
    ):
        assert run_capture(_args(_KEY), log) == 1
        assert run_capture(_args(_KEY, eval_force=True), log) == 0

    assert seen == ["create scenario file", "replace scenario file"]
    assert not list((tmp_path / "evals").glob("*.json"))


@pytest.mark.parametrize("refusal", ["incognito", "busy"])
def test_a_chat_restricted_after_the_read_is_not_written(log, tmp_path, capsys, refusal) -> None:
    from kiro_crew.eval import capture
    from kiro_crew.history import TranscriptBusy

    _chat(log)
    real_build = capture.build_scenario
    real_hold = log.publication_hold

    def build_then_restrict(rows: list[dict], name: str) -> Any:
        log.update_metadata(_KEY, {"memory_mode": "incognito"})
        return real_build(rows, name)

    def busy_hold(key: str, *, expected_keys: Any = None) -> Any:
        assert list(expected_keys) == [_KEY]
        raise TranscriptBusy("held for the test")

    builder = build_then_restrict if refusal == "incognito" else real_build
    with (
        patch.object(capture, "build_scenario", builder),
        patch.object(log, "publication_hold", busy_hold if refusal == "busy" else real_hold),
    ):
        assert run_capture(_args(_KEY), log) == 1

    assert not (tmp_path / "evals").exists()
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "nothing captured" in err


def test_a_capture_that_lands_first_is_not_replaced_without_force(log, tmp_path, capsys) -> None:
    from kiro_crew.eval.bench import safepath

    _chat(log)
    out = tmp_path / "t.json"
    real_guard = safepath.guard_write_path
    first: list[str] = []

    def other_capture_lands_first(path: Any, *, what: str) -> Any:
        # Runs after this capture checked that the path was absent.
        if not first:
            first.append("")
            assert run_capture(_args(_KEY, out=str(out)), log) == 0
            first[0] = out.read_text(encoding="utf-8")
        return real_guard(path, what=what)

    with patch.object(safepath, "guard_write_path", other_capture_lands_first):
        assert run_capture(_args(_KEY, out=str(out)), log) == 1

    assert out.read_text(encoding="utf-8") == first[0]
    assert "--force" in capsys.readouterr().err


def test_a_file_in_a_folder_that_holds_a_protected_tree_is_written(log, tmp_path) -> None:
    # The home folder holds credential stores, yet one scenario file there is fine:
    # only the file's own place is judged, never what else its folder contains.
    _chat(log)
    out = tmp_path / "refund.json"
    with patch("kiro_crew.security.path_contains_sensitive", return_value=True):
        assert run_capture(_args(_KEY, out=str(out)), log) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["sessions"]


def test_the_turns_and_messages_past_the_limits_are_reported(log, capsys) -> None:
    log.append(_KEY, "user", "y" * (MAX_USER_CHARS + 1))
    for n in range(1, MAX_TURNS + 1):
        log.append(_KEY, "tool", "🔧 Read", extra_meta={"tool_name": f"tool_{n - 1}"})
        log.append(_KEY, "user", f"turn {n}")

    assert run_capture(_args(_KEY), log) == 0

    out = capsys.readouterr().out
    assert (
        "The capture limits left out 1 user turn(s) past the first 200 and cut 1 message(s) "
        f"to {MAX_USER_CHARS} characters." in out
    )
    (written,) = Path("evals").glob("*.json")
    turns = json.loads(written.read_text(encoding="utf-8"))["sessions"][0]["turns"]
    assert len(turns) == MAX_TURNS
    assert turns[-1]["assertions"][0]["value"] == f"tool_{MAX_TURNS - 1}"
    assert len(turns[0]["user"]) == MAX_USER_CHARS


def test_one_turn_past_the_limit_reports_one() -> None:
    rows = [{"role": "user", "content": f"t{n}"} for n in range(MAX_TURNS + 1)]
    rows.append({"role": "tool", "content": "🔧", "meta": {"tool_name": "late"}})

    scenario, omitted = build_scenario(rows, "n")

    assert (omitted.turns, omitted.cut) == (1, 0)
    assert "late" not in json.dumps(scenario)


def test_one_turn_keeps_a_bounded_number_of_tool_calls_and_reports_the_rest(log, capsys) -> None:
    extra = 3
    log.append(_KEY, "user", "run the long loop")
    for n in range(MAX_TOOLS_PER_TURN + extra):
        meta = {"tool_name": f"tool_{n}", "tool_call_id": f"c{n}"}
        log.append(_KEY, "tool", "🔧 Run", extra_meta=meta)
        log.append(_KEY, "tool", "✅ Run", extra_meta=meta)
    log.append(_KEY, "user", "thanks")

    scenario, omitted = build_scenario(log.read_messages(_KEY), "n")

    first, second = scenario["sessions"][0]["turns"]
    order = [a for a in first["assertions"] if a["type"] == "tool_order"][0]["tools"]
    assert order == [f"tool_{n}" for n in range(MAX_TOOLS_PER_TURN)]
    assert len(first["assertions"]) == MAX_TOOLS_PER_TURN + 2
    assert [a["type"] for a in second["assertions"]] == ["judge"]
    assert (omitted.tools, omitted.unnamed) == (extra, 0)
    assert run_capture(_args(_KEY), log) == 0
    assert (
        f"The capture limits left out {extra} tool call(s) past the first "
        f"{MAX_TOOLS_PER_TURN} in a turn." in capsys.readouterr().out
    )


def test_the_tool_limit_starts_again_in_the_next_turn() -> None:
    rows: list[dict] = []
    for turn in ("a", "b"):
        rows.append({"role": "user", "content": turn})
        for n in range(MAX_TOOLS_PER_TURN + 1):
            meta = {"tool_name": f"{turn}{n}", "tool_call_id": f"{turn}{n}"}
            rows.append({"role": "tool", "content": "🔧", "meta": meta})

    scenario, omitted = build_scenario(rows, "n")

    kept = [t["assertions"][-2]["tools"] for t in scenario["sessions"][0]["turns"]]
    assert [len(tools) for tools in kept] == [MAX_TOOLS_PER_TURN] * 2
    assert omitted.tools == 2


def test_an_out_file_is_written_exactly_where_it_is_named(log, tmp_path) -> None:
    _chat(log)
    out = tmp_path / "mine.scenario"

    assert run_capture(_args(_KEY, out=str(out)), log) == 0

    assert json.loads(out.read_text(encoding="utf-8"))["sessions"]
    assert not (tmp_path / "mine.scenario.json").exists()


def test_an_out_folder_gets_the_default_name_inside_it(log, tmp_path) -> None:
    _chat(log)
    folder = tmp_path / "mine"
    folder.mkdir()

    assert run_capture(_args(_KEY, out=str(folder)), log) == 0

    assert [p.name for p in folder.iterdir()] == [
        f"{scenario_name('dashboard_chat-7-1700000000')}.json"
    ]
    assert not (tmp_path / "mine.json").exists()


def test_a_hostile_key_is_a_safe_name_and_a_safe_line(log, capsys) -> None:
    key = "x/../\x1b[31m evil"

    assert scenario_name(key) == "captured_x______31m_evil"
    assert run_capture(_args(key), log) == 1
    assert "\x1b" not in capsys.readouterr().err


def test_the_verb_is_one_of_eval_s_positionals() -> None:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    cli_parser_admin.register_eval_parser(sub, argparse.ArgumentParser(add_help=False))

    parsed = root.parse_args(["eval", "capture", "chat-1", "--out", "a.json", "--force"])

    assert (parsed.scenarios, parsed.out, parsed.eval_force) == (
        ["capture", "chat-1"],
        "a.json",
        True,
    )


def test_out_names_the_capture_file_and_stays_refused_on_a_scenario_run() -> None:
    """`--out` is shared with `kirocrew eval run`; the run-only guard lets `capture` use it."""
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    cli_parser_admin.register_eval_parser(sub, argparse.ArgumentParser(add_help=False))

    def refusal(*argv: str) -> str:
        return cc._eval_run_flag_refusal(root.parse_args(["eval", *argv]))

    assert refusal("capture", "chat-1", "--out", "a.json") == ""
    assert refusal("run", "--suite", "tasks", "--out", "results") == ""
    assert refusal("smoke_test", "--out", "a.json") == "--out needs kirocrew eval run"
    assert refusal("capture", "chat-1", "--json") == "--json needs kirocrew eval run"


class _Scripted:
    cwd = ""

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def set_workspace(self, path: Any) -> None:
        pass

    async def stream(self, message: str, **_kwargs: Any):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have two.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: Any) -> None:
        pass

    async def reject_tool(self, request_id: Any) -> None:
        pass

    def context_usage_pct(self) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_a_captured_scenario_runs_with_eval_file(log, tmp_path, capsys) -> None:
    log.append(_KEY, "user", "list my open tickets")
    log.append(_KEY, "assistant", "You have two.")
    with patch("kiro_crew.history.ConversationLog", return_value=log):
        with pytest.raises(SystemExit) as captured:
            await cc._run_eval(_args(_KEY))
    assert captured.value.code == 0
    (path,) = (tmp_path / "evals").glob("*.json")

    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch(
            "kiro_crew.cli_commands.build_provider_factory",
            return_value=lambda session_key, **kwargs: _Scripted(),
        ),
        patch("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict"),
    ):
        await cc._run_eval(
            argparse.Namespace(
                scenarios=[],
                all_scenarios=False,
                judge=False,
                eval_dir=None,
                eval_file=str(path),
                agent=None,
                model=None,
                repeat=1,
            )
        )

    assert "Overall: 1/1 scenarios passed" in capsys.readouterr().out
