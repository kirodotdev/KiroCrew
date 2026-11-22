"""The command line each ``kirocrew eval run`` arm builds, and how it reads what the arm printed.

No harness runs here: the argv is asserted as data, and the parsers read
canned output in the shapes the RFC names (rfc-builtin-eval.md §3.2, §12.4).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from kiro_crew.eval import arms
from kiro_crew.eval.arms import ARMS, Caps

CAPS = Caps(
    timeout_s=1200, credits=30, usd=3
)  # flake-ok: a cap rendered into argv, never waited on
TOOLS = ["Read", "Edit", "Bash"]


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_every_rfc_arm_is_registered():
    assert list(ARMS) == ["crew-chat", "crew-conductor", "kiro-cli-v3", "claude-code", "codex"]


def test_crew_chat_drives_the_headless_cli_with_trust_on_its_own_chat():
    argv = ARMS["crew-chat"].argv("fix it", "/runs/r/repo", CAPS, "", TOOLS)
    from kiro_crew.platform_compat import isolated_python_argv

    head = isolated_python_argv("-P", "-m", "kiro_crew", "chat", "--headless", "-m")
    assert argv[: len(head)] == head and "-P" in head
    assert argv[len(head)] == "fix it"
    assert _flag(argv, "--approval-mode") == "trust"
    assert _flag(argv, "--cwd") == "/runs/r/repo"
    assert _flag(argv, "--timeout") == "1200"
    assert _flag(argv, "--budget") == "30"
    assert "--json" in argv
    assert "--mode" not in argv and "--until-done" not in argv and "--model" not in argv


def test_crew_conductor_asks_for_conductor_mode_and_never_names_an_agent():
    argv = ARMS["crew-conductor"].argv("fix it", "/repo", CAPS, "m1", TOOLS)
    assert _flag(argv, "--mode") == "conductor"
    assert "--until-done" in argv
    assert "--agent" not in argv
    assert _flag(argv, "--model") == "m1"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_claude_code_runs_dont_ask_with_only_the_case_tools_and_no_mcp(monkeypatch, tmp_path):
    real = tmp_path / "real-claude"
    real.write_text("")
    shim = tmp_path / "claude"
    shim.symlink_to(real)
    monkeypatch.setattr(arms.shutil, "which", lambda name: str(shim))
    argv = ARMS["claude-code"].argv("fix it", "/repo", CAPS, "", TOOLS)
    assert argv[:3] == [str(real.resolve()), "-p", "fix it"]
    assert _flag(argv, "--output-format") == "stream-json" and "--verbose" in argv
    assert _flag(argv, "--permission-mode") == "dontAsk"
    assert _flag(argv, "--permission-prompts") == "none"
    assert _flag(argv, "--allowedTools") == "Read,Edit,Bash"
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert _flag(argv, "--max-budget-usd") == "3"
    assert "bypassPermissions" not in argv and "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize("name", ["kiro-cli-v3", "codex"])
def test_an_arm_not_built_yet_says_so(name):
    assert "not available" in ARMS[name].unavailable()


def test_claude_code_without_a_binary_is_unavailable(monkeypatch):
    monkeypatch.setattr(arms.shutil, "which", lambda name: None)
    assert ARMS["claude-code"].unavailable() == "claude is not on PATH"


def test_each_arm_names_the_eval_sign_in_it_misses():
    assert "KIRO_API_KEY" in ARMS["crew-chat"].missing_credential({})
    assert ARMS["crew-chat"].missing_credential({"KIRO_API_KEY": "k"}) == ""
    assert ARMS["claude-code"].missing_credential({}) != ""
    assert ARMS["claude-code"].missing_credential({"ANTHROPIC_API_KEY": "k"}) == ""
    bedrock = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "t"}
    assert ARMS["claude-code"].missing_credential(bedrock) == ""


def _crew_json(**over):
    payload = {
        "status": "done",
        "asks": 0,
        "ask_events": [],
        "credits": 4.5,
        "agent": {"name": "conductor-x", "source": "default", "configured": ""},
        "transcript": "/t.jsonl",
        "error": "",
    }
    payload.update(over)
    return "progress line\n" + json.dumps(payload) + "\n"


def test_crew_result_carries_credits_asks_and_the_resolved_agent():
    out = ARMS["crew-conductor"].parse(0, _crew_json())
    assert (out.status, out.asks, out.cost, out.cost_unit) == ("done", 0, 4.5, "credits")
    assert out.agent == {"name": "conductor-x", "source": "default", "configured": ""}


def test_a_crew_ask_is_counted_with_its_events():
    events = [{"kind": "approval", "slot": "s", "text": "shell", "t_s": "3"}]
    out = ARMS["crew-chat"].parse(3, _crew_json(status="asked", asks=1, ask_events=events))
    assert (out.status, out.asks, out.ask_events) == ("asked", 1, events)


@pytest.mark.parametrize(
    "status", ["not_signed_in", "sandbox_refused", "no_gateway", "interrupted"]
)
def test_a_crew_run_that_never_started_is_void(status):
    assert ARMS["crew-chat"].parse(6, _crew_json(status=status)).status == "void"


@pytest.mark.parametrize("status", ["timeout", "budget", "incomplete", "error"])
def test_other_crew_endings_are_scored(status):
    assert ARMS["crew-chat"].parse(4, _crew_json(status=status)).status == status


def test_crew_output_without_a_json_result_is_an_error():
    out = ARMS["crew-chat"].parse(1, "Traceback ...\n")
    assert out.status == "error" and "exit 1" in out.note


def _stream(*objs) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def test_claude_result_line_gives_cost_and_done():
    text = _stream(
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.42,
            "permission_denials": [],
        },
    )
    out = ARMS["claude-code"].parse(0, text)
    assert (out.status, out.asks, out.cost, out.cost_unit) == ("done", 0, 0.42, "usd")


def test_claude_question_tools_are_asks_and_a_denial_is_not():
    text = _stream(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.1,
            "permission_denials": [{"tool_name": "WebFetch"}],
        },
    )
    out = ARMS["claude-code"].parse(0, text)
    # dontAsk refuses WebFetch on the spot and nobody is waited on: not an ask.
    assert out.asks == 1
    assert [e["kind"] for e in out.ask_events] == ["question"]
    assert out.denials == [{"tool": "WebFetch"}]


def test_a_run_of_only_denials_has_no_asks():
    text = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.6,
            "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Bash"}],
        }
    )
    out = ARMS["claude-code"].parse(0, text)
    assert (out.status, out.asks, out.ask_events) == ("done", 0, [])
    assert out.denials == [{"tool": "Bash"}, {"tool": "Bash"}]


def test_a_crew_run_that_left_chats_open_has_no_known_cost():
    # Cut mid-turn and unable to close its chats: the counted credits are a floor.
    cut = ARMS["crew-conductor"].parse(
        1, _crew_json(status="error", credits=0.0, unclosed=["chat-2", "chat-1"])
    )
    assert (cut.cost, cut.cost_floor, cut.status) == (None, 0.0, "error")
    assert ARMS["crew-chat"].parse(0, _crew_json(unclosed=[])).cost == 4.5


def test_claude_budget_stop_and_missing_result():
    budget = _stream(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "total_cost_usd": 3.0,
        }
    )
    assert ARMS["claude-code"].parse(1, budget).status == "budget"
    assert ARMS["claude-code"].parse(1, "not json\n").status == "error"


# ---------- --use-operator-credentials ----------
def _script(path, code, line="no login"):
    path.write_text(f"#!/bin/sh\necho '{line}' >&2\nexit {code}\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX scripts")
def test_operator_checks_run_whoami_and_say_ok(monkeypatch, tmp_path):
    from kiro_crew import kiro_cli

    seen = tmp_path / "kiro_home"
    good = tmp_path / "good-kiro-cli"
    good.write_text(f'#!/bin/sh\nprintf %s "$KIRO_HOME" > {seen}\nexit 0\n')
    good.chmod(0o755)
    bad = _script(tmp_path / "bad-kiro-cli", 1, "Not logged in")
    crew = ARMS["crew-chat"]
    found = {"bin": str(good)}
    monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda environ=None: found["bin"])
    assert crew.operator_check({"PATH": "/bin", "KIRO_HOME": "/real"}) == ""
    assert seen.read_text() not in ("", "/real")  # a fresh KIRO_HOME, as each run gets
    found["bin"] = str(bad)
    assert crew.operator_check({"PATH": "/bin"}) == (
        "`bad-kiro-cli whoami` failed (exit 1): Not logged in"
    )
    found["bin"] = None
    assert crew.operator_check({"PATH": "/bin"}) == "kiro-cli is not installed"
    cc = arms.ClaudeCodeArm()
    claude = {"bin": str(_script(tmp_path / "claude-ok", 0))}
    monkeypatch.setattr(cc, "binary", lambda: claude["bin"])
    assert cc.operator_check({"PATH": "/bin"}) == ""
    claude["bin"] = str(_script(tmp_path / "claude-bad", 2, "auth"))
    assert cc.operator_check({"PATH": "/bin"}) == '`claude-bad -p "say ok"` failed (exit 2): auth'


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_under_operator_credentials_a_claude_shim_is_kept_as_found(monkeypatch, tmp_path):
    real = tmp_path / "real-claude"
    real.write_text("")
    shim = tmp_path / "claude"
    shim.symlink_to(real)
    monkeypatch.setattr(arms.shutil, "which", lambda name: str(shim))
    cc = arms.ClaudeCodeArm()
    assert cc.binary() == str(real.resolve())
    cc.operator_credentials = True
    assert cc.binary() == str(shim)
    # A relative PATH entry (./node_modules/.bin) still names an absolute file: the
    # arm starts in the task repo.
    monkeypatch.setattr(arms.shutil, "which", lambda name: "node_modules/.bin/claude")
    assert cc.binary() == os.path.abspath("node_modules/.bin/claude")


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX here")
def test_a_probe_that_hangs_is_killed_with_everything_it_started(monkeypatch, tmp_path):
    import os
    import time

    pid_file = tmp_path / "child.pid"
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(arms, "_PROBE_TIMEOUT_S", 1.0)  # flake-ok: the cap under test
    assert arms._probe([str(wrapper)], ["-p", "say ok"], {"PATH": "/bin:/usr/bin"}) == (
        '`claude -p "say ok"` did not answer within 1 s'
    )
    child = int(pid_file.read_text())

    def running(pid: int) -> bool:
        """Alive and not a zombie: a killed child no init has reaped yet counts as gone."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = f"/proc/{pid}/stat"
        try:
            with open(stat, encoding="utf-8") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return not os.path.isdir("/proc")  # no /proc (macOS): kill(0) is the answer

    deadline = time.monotonic() + 5  # flake-ok: bounded wait for the kill to land
    while running(child) and time.monotonic() < deadline:
        time.sleep(0.05)  # flake-ok: polls a pid until it stops, bounded above
    assert not running(child), "the forked child outlived the probe"
