"""Coverage for the auto-improvement spine's agent runner.

``spine/agent_runner.py`` is the ONE place the autonomous loop invokes a model, and it
carries the loop's whole safety perimeter: the shell denylist that stops an injected PR
comment from reaching ``gh pr comment``/``curl``, the governance chokepoint, the
audit-or-deny SEL writes, and the two ``run`` paths (subprocess ``claude -p`` and the
in-process provider session) whose result contract is "never raise into the spine".

The module's own suite lives under the app tree, which the reduced-scope CI selector
deselects — so none of that perimeter was exercised on a pull request. These tests live
under ``test/`` (never deselected) and drive the perimeter with INJECTED fakes at the
agent-process boundary: no agent binary, no provider, no network, no real ``git`` and no
real ``subprocess``. Writes are confined to ``tmp_path`` (and ``KIRO_HOME`` is
redirected there for the self-registration test).

One product defect is characterised rather than fixed — see
``test_run_spawn_failure_result_or_known_defect``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
)
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as R

# ── shared fakes ────────────────────────────────────────────────────────────


class _FakeSel:
    """Stand-in for the Security Event Log singleton: records, optionally fails."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def log_tool_invocation(self, **kw) -> None:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("SEL unwritable")


@pytest.fixture
def fake_sel(monkeypatch):
    sel = _FakeSel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
    return sel


@pytest.fixture
def broken_sel(monkeypatch):
    sel = _FakeSel(fail=True)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
    return sel


class _OneShotPopen:
    """A ``claude -p --output-format json`` child: ``communicate`` returns one envelope."""

    def __init__(self, *, out: str = "{}", err: str = "", returncode: int = 0, timeouts: int = 0):
        self.pid = 424242
        self.returncode = returncode
        self._out = out
        self._err = err
        self._timeouts = timeouts
        self.killed = 0
        self.waited: list[float | None] = []

    def communicate(self, timeout=None):
        if self._timeouts > 0:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="fake-agent", timeout=timeout or 0)
        return self._out, self._err

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return self.returncode


class _StreamPopen:
    """A ``--output-format stream-json`` child: stdout/stderr are plain iterators."""

    def __init__(self, lines, *, stderr_lines=(), returncode: int = 0):
        self.pid = 424243
        self.returncode = returncode
        self.stdout = iter(list(lines))
        self.stderr = iter(list(stderr_lines))
        self.waited: list[float | None] = []
        self.killed = 0

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed += 1


class _AsyncEvents:
    """A true async iterator over canned provider events."""

    def __init__(self, events, *, stall_s: float = 0.0):
        self._events = list(events)
        self._stall_s = stall_s

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stall_s:
            await asyncio.sleep(self._stall_s)
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeProvider:
    def __init__(self, events=(), *, stall_s: float = 0.0, plain_stream: bool = False):
        self._events = list(events)
        self._stall_s = stall_s
        self._plain_stream = plain_stream
        self.started = 0
        self.shutdowns = 0
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.prompt = ""
        self.shutdown_error: Exception | None = None
        self.start_error: Exception | None = None

    async def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def stream(self, prompt: str):
        self.prompt = prompt
        if self._plain_stream:
            return object()  # no __aiter__ -> the watchdog degrades gracefully
        return _AsyncEvents(self._events, stall_s=self._stall_s)

    async def approve_tool(self, rid) -> None:
        self.approved.append(rid)

    async def reject_tool(self, rid) -> None:
        self.rejected.append(rid)

    async def shutdown(self) -> None:
        self.shutdowns += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


class _FakeAgentRunner:
    """Duck-typed runner for ``author_bug_fix`` / ``author_perf_fix``."""

    def __init__(self, result: R.AgentResult):
        self._result = result
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    def run(self, prompt, **kw) -> R.AgentResult:
        self.prompts.append(prompt)
        self.kwargs.append(kw)
        return self._result


def _ev(**kw):
    """A permission/tool event: the product reads everything with ``getattr``."""
    kw.setdefault("kind", "")
    return SimpleNamespace(**kw)


# ── _LineBuffer ─────────────────────────────────────────────────────────────


def test_line_buffer_flushes_whole_newline_terminated_lines():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    buf.feed("first line\nsecond")
    assert seen == ["first line"]
    buf.feed(" line\n")
    assert seen == ["first line", "second line"]


def test_line_buffer_drops_blank_lines_and_strips():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    buf.feed("\n   \n  kept  \n")
    assert seen == ["kept"]


def test_line_buffer_truncates_an_emitted_line_to_the_flush_length():
    seen: list[str] = []
    R._LineBuffer(seen.append).feed("x" * 250 + "\n")
    assert seen == ["x" * R._LINE_FLUSH_LEN]


def test_line_buffer_splits_a_long_paragraph_at_a_sentence_boundary():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    head = "a" * 50
    buf.feed(head + ". " + "b" * 160)
    assert seen == [head + "."]
    buf.flush()
    assert seen[-1] == "b" * 160


def test_line_buffer_emits_an_overlong_buffer_with_no_usable_boundary():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    buf.feed("c" * 205)
    assert seen == ["c" * R._LINE_FLUSH_LEN]
    buf.flush()
    assert seen == ["c" * R._LINE_FLUSH_LEN]  # buffer was consumed, nothing trailing


def test_line_buffer_ignores_a_boundary_that_yields_a_tiny_line():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    # The only ". " sits at index 2, below the 40-char minimum, so the whole buffer goes.
    buf.feed("ab. " + "d" * 210)
    assert len(seen) == 1
    assert seen[0].startswith("ab. ")


def test_line_buffer_flush_of_whitespace_only_emits_nothing():
    seen: list[str] = []
    buf = R._LineBuffer(seen.append)
    buf.feed("   ")
    buf.flush()
    assert seen == []


# ── _tool_detail / _detail_is_richer ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,title,tool_input,expected",
    [
        ({"path": "src/a.py"}, "Read File", "", "Read File · src/a.py"),
        ({"file_path": "src/b.py"}, "", "", "src/b.py"),
        ({"command": "pytest -q"}, "Bash", "", "Bash · pytest -q"),
        ({"path": "src/c.py"}, "Read src/c.py", "", "Read src/c.py"),
        ({"path": "   "}, "Grep", "", "Grep"),
        (None, "Write File", "", "Write File"),
        (None, "", "raw tool input", "raw tool input"),
        (None, "", "", ""),
        ({"query": "needle"}, "", "", "needle"),
    ],
)
def test_tool_detail_prefers_the_target_over_a_generic_label(raw, title, tool_input, expected):
    ev = _ev(raw_tool_params=raw, title=title, tool_input=tool_input)
    assert R._tool_detail(ev) == expected


def test_tool_detail_truncates_a_long_combined_label():
    ev = _ev(raw_tool_params={"path": "p" * 400}, title="Read File", tool_input="")
    assert len(R._tool_detail(ev)) == 160


def test_tool_detail_truncates_a_raw_tool_input_fallback():
    ev = _ev(raw_tool_params=None, title="", tool_input="i" * 400)
    assert len(R._tool_detail(ev)) == 80


@pytest.mark.parametrize(
    "refined,prior,expected",
    [
        ("", "anything", False),
        ("Read File", "", True),
        ("Read File · src/a.py", "Read File", True),
        ("Read File", "Read File", False),
        ("Read Fileeeee", "Read File", True),
        ("Read Fil", "Read File", False),
    ],
)
def test_detail_is_richer(refined, prior, expected):
    assert R._detail_is_richer(refined, prior) is expected


# ── _unlink_quietly ─────────────────────────────────────────────────────────


def test_unlink_quietly_removes_the_launcher_and_tolerates_absence(tmp_path):
    target = tmp_path / "launcher.sh"
    target.write_text("#!/bin/sh\n", newline="\n")
    R._unlink_quietly(target)
    assert not target.exists()
    R._unlink_quietly(target)  # missing_ok
    R._unlink_quietly(None)
    R._unlink_quietly("")


def test_unlink_quietly_swallows_an_os_error(monkeypatch, tmp_path):
    def _boom(self, missing_ok=False):
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", _boom)
    R._unlink_quietly(tmp_path / "whatever")  # must not raise


# ── _shell_words ────────────────────────────────────────────────────────────


def test_shell_words_tokenizes_quoted_arguments():
    assert R._shell_words("gh pr view '#1 title'") == ["gh", "pr", "view", "#1 title"]


def test_shell_words_falls_back_to_a_naive_split_on_unbalanced_quotes():
    assert R._shell_words("echo 'unbalanced") == ["echo", "'unbalanced"]


# ── shell_command_refusal ───────────────────────────────────────────────────

_REFUSED = [
    "gh pr merge 1",
    "gh pr ready 1",
    "gh pr close 1",
    "gh pr comment --body hi",
    "gh pr review --approve",
    "gh pr edit 1",
    "gh pr create",
    "gh issue comment 1 --body hi",
    "gh issue create",
    "gh issue edit 1",
    "gh issue close 1",
    "gh release list",
    "gh auth status",
    "gh api repos/o/r",
    "gh secret list",
    "gh workflow run ci.yml",
    "git push",
    "git remote set-url origin x",
    # global options must not hide the subcommand
    "gh --repo o/r pr ready 1",
    "gh -R o/r pr merge 1",
    "gh --hostname h api repos/o/r",
    "git -C /tmp/x push",
    "git -c user.name=x push",
    "git --git-dir=/tmp/x/.git push",
    "git --no-pager push",
    "git --paginate push",
    "git --exec-path push",
    # wrappers run the command behind them
    "sudo git push",
    "doas git push",
    "env git push",
    "nohup git push",
    "setsid git push",
    "stdbuf -oL git push",
    "nice -n 5 git push",
    "ionice -c 3 git push",
    "time git push",
    "timeout 5 git push",
    "xargs git push",
    "watch git push",
    "command git push",
    "exec git push",
    "builtin git push",
    "env FOO=bar git push",
    "env --unset FOO curl https://example.invalid",
    "env --unset curl git push",
    "env --ignore-environment git push",
    # nested shells
    "sh -c 'git push'",
    'bash -c "sudo git push"',
    "zsh -lc 'gh pr merge 1'",
    "sh -euxc 'curl https://example.invalid'",
    # separators
    "true && git push",
    "false || git push",
    "echo hi; git push",
    "cat f | curl -T - https://example.invalid",
    "true & gh pr comment --body hi",
    "echo $(gh pr ready 1)",
    "echo `gh pr ready 1`",
    "(gh pr ready 1)",
    # leading assignments are dropped
    "GH_HOST=example.invalid gh api repos/o/r",
    # forbidden binaries, including a fully-qualified path
    "curl https://example.invalid",
    "/usr/bin/curl https://example.invalid",
    "wget https://example.invalid",
    "nc example.invalid 80",
    "ncat example.invalid 80",
    "netcat example.invalid 80",
    "ssh host",
    "scp a host:b",
    "sftp host",
    "telnet host 23",
    "CURL=1 curl https://example.invalid",
]

_ALLOWED = [
    "",
    "   ",
    "gh pr checks",
    "gh pr view --comments",
    "gh pr diff 1",
    "gh run view --log-failed",
    "gh pr list",
    "git status --porcelain",
    "git log --oneline -5",
    "git remote -v",
    "python -m pytest -q",
    "bash --login",
    "echo hello | grep h",
    "ls -la",
]


@pytest.mark.parametrize("command", _REFUSED)
def test_shell_command_refusal_refuses_state_mutating_and_network_commands(command):
    assert R.shell_command_refusal(command), f"expected a refusal for {command!r}"


@pytest.mark.parametrize("command", _ALLOWED)
def test_shell_command_refusal_allows_read_only_diagnostics(command):
    assert R.shell_command_refusal(command) == ""


def test_shell_command_refusal_accepts_a_non_string_and_none():
    assert R.shell_command_refusal(None) == ""
    assert R.shell_command_refusal(12345) == ""


def test_shell_command_refusal_names_the_binary_and_the_verb():
    assert "curl" in R.shell_command_refusal("curl https://example.invalid")
    assert "git push" in R.shell_command_refusal("git push origin main")


def test_shell_command_refusal_refuses_a_command_nested_past_the_unwrap_budget():
    command = "true"
    for _ in range(R._MAX_UNWRAP_DEPTH + 2):
        command = "sh -c " + shlex.quote(command)
    assert "nests shells too deeply" in R.shell_command_refusal(command)


def test_shell_command_refusal_reaches_a_wrapped_command_inside_a_nested_shell():
    assert R.shell_command_refusal("sh -c 'timeout 5 env sudo git push'")


def test_glab_is_known_to_the_option_table_but_has_no_denylist_yet():
    """PRODUCT GAP (reported, not fixed here): ``glab`` is unguarded end to end.

    ``_VALUE_TAKING_OPTIONS`` lists ``glab``'s global options, which reads as denylist
    coverage for the GitLab CLI — but ``_FORBIDDEN_SUBCOMMANDS`` has no ``glab`` key, so
    the option-skipping code never runs and EVERY ``glab`` verb is allowed, including
    ``glab mr merge`` / ``glab mr note`` / ``glab api``. On a GitLab-hosted repo the
    watcher's outsider-writable prompt therefore faces no publish denylist at all. Pinned
    permissively so this test also passes once the missing key is added.
    """
    assert "glab" in R._VALUE_TAKING_OPTIONS
    if "glab" not in R._FORBIDDEN_SUBCOMMANDS:
        assert R.shell_command_refusal("glab mr merge 1") == ""
        assert R.shell_command_refusal("glab --repo o/r mr note 1 --message hi") == ""
    else:
        assert R.shell_command_refusal("glab mr merge 1")


# ── _requested_command ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"command": "pytest -q"}, "pytest -q"),
        ({"cmd": "ls"}, "ls"),
        ({"command": "  ", "cmd": "ls"}, "ls"),
        ({"command": 12}, ""),
        ({}, ""),
        (None, ""),
        ("not-a-dict", ""),
    ],
)
def test_requested_command(raw, expected):
    assert R._requested_command(_ev(raw_tool_params=raw)) == expected


# ── _governance_denial ──────────────────────────────────────────────────────


def _wire_hooks(monkeypatch, *, action: str, reason: str = ""):
    """Point the module's governance chokepoint at a fake HookManager."""
    seen: dict = {}

    class _FakeManager:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        def on_tool_call(self, name, **kw):
            seen["name"] = name
            seen.update(kw)
            return SimpleNamespace(action=action, reason=reason)

    monkeypatch.setattr(
        R, "KiroCrewConfig", SimpleNamespace(load=lambda: SimpleNamespace(hooks={"x": 1}))
    )
    monkeypatch.setattr(R, "hooks_config_from_config_dict", lambda d: {"from": d})
    monkeypatch.setattr(R, "HookManager", _FakeManager)
    return seen


def test_governance_denial_allows_when_the_hook_layer_allows(monkeypatch):
    seen = _wire_hooks(monkeypatch, action="allow")
    ev = _ev(title="Bash", tool_kind="bash", raw_tool_params={"command": "pytest -q"})
    assert R._governance_denial(ev, session_key="s", agent="a") == ""
    assert seen["command"] == "pytest -q"
    assert seen["app"] == "auto-improvement"
    assert seen["session_key"] == "s"


def test_governance_denial_returns_the_hook_reason(monkeypatch):
    _wire_hooks(monkeypatch, action=R.TOOL_DENY, reason="  reads ~/.aws  ")
    ev = _ev(title="Read", tool_kind="fsRead", raw_tool_params={"path": "~/.aws/config"})
    assert R._governance_denial(ev, session_key="s", agent="a") == "reads ~/.aws"


def test_governance_denial_supplies_a_default_reason(monkeypatch):
    _wire_hooks(monkeypatch, action=R.TOOL_DENY, reason="")
    assert R._governance_denial(_ev(title="x"), session_key="s", agent="a") == (
        "denied by governance policy"
    )


def test_governance_denial_marks_a_command_bearing_request_as_shell(monkeypatch):
    seen = _wire_hooks(monkeypatch, action="allow")
    ev = _ev(title="", tool_kind="", is_shell=False, raw_tool_params={"command": "ls"})
    R._governance_denial(ev, session_key="s", agent="a")
    assert seen["is_shell"] is True


def test_governance_denial_fails_closed_when_the_hook_layer_breaks(monkeypatch):
    monkeypatch.setattr(
        R, "KiroCrewConfig", SimpleNamespace(load=lambda: SimpleNamespace(hooks={}))
    )
    monkeypatch.setattr(R, "hooks_config_from_config_dict", lambda d: d)

    def _boom(cfg):
        raise RuntimeError("hooks store corrupt")

    monkeypatch.setattr(R, "HookManager", _boom)
    reason = R._governance_denial(_ev(title="Bash"), session_key="s", agent="a")
    assert reason.startswith("governance hook unavailable")
    assert "hooks store corrupt" in reason


# ── _tool_permitted ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool,allowed,expected",
    [
        ("anything", None, True),
        ("anything", [], False),
        ("anything", ["  ", ""], False),
        ("", ["Bash"], False),
        (None, ["Bash"], False),
        ("bash", ["Bash"], True),
        ("execute_bash", ["execute"], True),
        ("exec", ["execute_bash"], True),
        ("my_bash_tool", ["bash"], True),
        ("write", ["Bash", "Read"], False),
    ],
)
def test_tool_permitted(tool, allowed, expected):
    assert R._tool_permitted(tool, allowed) is expected


# ── the SEL audit helpers ───────────────────────────────────────────────────


def test_audit_unattended_agent_records_critically_and_allows_the_launch(fake_sel):
    assert R._audit_unattended_agent(cwd="/tmp/wt", model="opus", max_turns=7) is True
    (call,) = fake_sel.calls
    assert call["critical"] is True
    assert call["outcome"] == "auto_approved"
    assert call["tool_name"] == "claude-cli"
    assert call["metadata"]["unattended"] is True
    assert call["metadata"]["max_turns"] == 7


def test_audit_unattended_agent_denies_the_launch_when_the_audit_fails(broken_sel):
    assert R._audit_unattended_agent(cwd=None, model=None, max_turns=1) is False


def test_audit_fallback_tool_redacts_and_truncates_the_target(monkeypatch, fake_sel):
    monkeypatch.setattr("kiro_crew.security.redact", lambda t: "R:" + t)
    R._audit_fallback_tool(tool="Bash", detail="d" * 400, cwd="/tmp/wt")
    (call,) = fake_sel.calls
    assert call["outcome"] == "invoked"
    assert "critical" not in call
    assert call["metadata"]["target"].startswith("R:")
    assert len(call["metadata"]["target"]) == 200


def test_audit_fallback_tool_never_emits_raw_text_when_redaction_breaks(monkeypatch, fake_sel):
    def _boom(text):
        raise RuntimeError("redactor down")

    monkeypatch.setattr("kiro_crew.security.redact", _boom)
    R._audit_fallback_tool(tool="Bash", detail="secret-value", cwd=None)
    (call,) = fake_sel.calls
    assert call["metadata"]["target"] == "[redaction unavailable]"


def test_audit_fallback_tool_survives_an_unwritable_log(broken_sel, monkeypatch):
    monkeypatch.setattr("kiro_crew.security.redact", lambda t: t)
    R._audit_fallback_tool(tool="Bash", detail="x", cwd=None)  # must not raise


# ── _summarize_stream_event ─────────────────────────────────────────────────


def test_summarize_stream_event_surfaces_a_tool_use_with_a_target_hint():
    obj = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a"}}]},
    }
    assert R._summarize_stream_event(obj) == {"kind": "tool", "tool": "Edit", "detail": "a"}


def test_summarize_stream_event_truncates_a_long_hint():
    obj = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "z" * 200}}]
        },
    }
    detail = R._summarize_stream_event(obj)["detail"]
    assert len(detail) == 78 and detail.endswith("…")


def test_summarize_stream_event_falls_back_through_the_hint_keys():
    obj = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "input": {"description": "look around"}}],
        },
    }
    assert R._summarize_stream_event(obj) == {
        "kind": "tool",
        "tool": "tool",
        "detail": "look around",
    }


def test_summarize_stream_event_surfaces_assistant_text_truncated():
    obj = {"type": "assistant", "message": {"content": [{"type": "text", "text": "  " + "t" * 300}]}}
    act = R._summarize_stream_event(obj)
    assert act["kind"] == "text" and len(act["detail"]) == 200


@pytest.mark.parametrize(
    "obj",
    [
        {"type": "assistant", "message": {"content": "not-a-list"}},
        {"type": "assistant", "message": {"content": ["not-a-dict"]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "   "}]}},
        {"type": "assistant"},
        {"type": "system"},
        {},
    ],
)
def test_summarize_stream_event_skips_uninteresting_lines(obj):
    assert R._summarize_stream_event(obj) is None


@pytest.mark.parametrize(
    "is_error,detail", [(False, "done"), (True, "error")]
)
def test_summarize_stream_event_reports_the_result_line(is_error, detail):
    obj = {"type": "result", "is_error": is_error}
    assert R._summarize_stream_event(obj) == {"kind": "result", "detail": detail}


# ── AgentRunner: construction, cost, availability, teardown ─────────────────


def test_agent_runner_starts_with_zero_cost_and_ignores_a_non_callable_sink():
    runner = R.AgentRunner(on_activity="not callable")
    assert runner.total_cost_usd() == 0.0
    assert runner._on_activity is None


def test_agent_runner_available_reflects_the_binary_on_path(monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda name: "/usr/bin/claude")
    assert R.AgentRunner.available() is True
    monkeypatch.setattr(R.shutil, "which", lambda name: None)
    assert R.AgentRunner.available() is False


def test_agent_runner_emit_activity_swallows_a_sink_error():
    def _boom(ev):
        raise RuntimeError("sink down")

    R.AgentRunner(on_activity=_boom)._emit_activity({"kind": "text"})  # must not raise


def test_terminate_group_signals_the_whole_process_group(monkeypatch):
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: signalled.append((pid, sig)))
    popen = _OneShotPopen()
    R.AgentRunner._terminate_group(popen)
    assert signalled == [(popen.pid, R.signal.SIGTERM)]
    assert popen.killed == 0


def test_terminate_group_falls_back_to_kill_when_the_group_signal_fails(monkeypatch):
    def _boom(pid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(R, "kill_process_tree", _boom)
    popen = _OneShotPopen()
    R.AgentRunner._terminate_group(popen)
    assert popen.killed == 1


def test_terminate_group_escalates_to_sigkill_when_the_child_will_not_exit(monkeypatch):
    signalled: list[int] = []

    def _record(pid, sig):
        signalled.append(sig)

    monkeypatch.setattr(R, "kill_process_tree", _record)

    class _Stubborn(_OneShotPopen):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake-agent", timeout=timeout or 0)

    R.AgentRunner._terminate_group(_Stubborn())
    assert signalled == [R.signal.SIGTERM, R.SIGKILL]


def test_terminate_group_tolerates_a_child_that_cannot_be_killed_at_all(monkeypatch):
    def _boom(pid, sig):
        raise OSError("no perms")

    monkeypatch.setattr(R, "kill_process_tree", _boom)

    class _Unkillable(_OneShotPopen):
        def kill(self):
            raise RuntimeError("nope")

    R.AgentRunner._terminate_group(_Unkillable())  # must not raise


# ── AgentRunner.run: the one-shot json path ─────────────────────────────────


def _wire_spawn(monkeypatch, runner, popen, *, cleanup=None):
    """Replace the sandboxed spawn with a canned child; record the argv it was given."""
    captured: dict = {}

    def _fake(cmd, cwd):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return popen, cleanup

    monkeypatch.setattr(runner, "_spawn_sandboxed_agent", _fake)
    return captured


def test_run_returns_the_parsed_envelope_and_accumulates_cost(monkeypatch, fake_sel):
    runner = R.AgentRunner(model="opus")
    popen = _OneShotPopen(out='{"result": "fixed it", "total_cost_usd": 0.25}')
    captured = _wire_spawn(monkeypatch, runner, popen)

    res = runner.run("prompt", cwd="/tmp/wt", max_turns=9, add_dirs=["/tmp/extra"])

    assert res.ok is True
    assert res.text == "fixed it"
    assert res.cost_usd == 0.25
    assert runner.total_cost_usd() == 0.25
    assert res.duration_s >= 0.0
    cmd = captured["cmd"]
    assert cmd[0] == R.CLAUDE_BIN
    assert "--output-format" in cmd and "json" in cmd
    assert "stream-json" not in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "9"
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp/extra"


def test_run_json_encodes_a_non_string_result():
    runner = R.AgentRunner()
    popen = _OneShotPopen(out='{"result": {"a": 1}}')
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("kiro_crew.sel.sel", lambda: _FakeSel())
        _wire_spawn(mp, runner, popen)
        res = runner.run("prompt")
    assert res.ok is True
    assert res.text == '{"a": 1}'


def test_run_denies_every_tool_with_a_sentinel_when_the_allowlist_is_empty(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    captured = _wire_spawn(monkeypatch, runner, _OneShotPopen(out='{"result": ""}'))
    runner.run("prompt", allowed_tools=[])
    cmd = captured["cmd"]
    assert cmd[cmd.index("--allowed-tools") + 1] == "__none__"


def test_run_forwards_a_non_empty_allowlist_and_the_system_prompt(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    captured = _wire_spawn(monkeypatch, runner, _OneShotPopen(out='{"result": ""}'))
    runner.run("prompt", allowed_tools=["Bash", "Read"], append_system="be careful")
    cmd = captured["cmd"]
    idx = cmd.index("--allowed-tools")
    assert cmd[idx + 1 : idx + 3] == ["Bash", "Read"]
    assert cmd[cmd.index("--append-system-prompt") + 1] == "be careful"


def test_run_omits_the_allowlist_flag_when_no_restriction_was_imposed(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    captured = _wire_spawn(monkeypatch, runner, _OneShotPopen(out='{"result": ""}'))
    runner.run("prompt")
    assert "--allowed-tools" not in captured["cmd"]


def test_run_reports_a_non_zero_exit_with_the_stderr_tail(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    _wire_spawn(monkeypatch, runner, _OneShotPopen(out="", err="boom", returncode=3))
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "exit 3: boom"


def test_run_reports_an_unparseable_envelope(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    _wire_spawn(monkeypatch, runner, _OneShotPopen(out="not json"))
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "unparseable claude json envelope"
    assert res.raw == {"stdout": "not json"}


def test_run_surfaces_a_model_side_is_error_without_raising(monkeypatch, fake_sel):
    runner = R.AgentRunner()
    _wire_spawn(
        monkeypatch,
        runner,
        _OneShotPopen(out='{"result": "partial", "is_error": true, "total_cost_usd": 0.5}'),
    )
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "claude reported is_error"
    assert res.text == "partial"
    assert runner.total_cost_usd() == 0.5


def test_run_aborts_the_child_on_a_stop_request(monkeypatch, fake_sel):
    killed: list[int] = []
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: killed.append(pid))
    runner = R.AgentRunner(stop_check=lambda: True)
    popen = _OneShotPopen(timeouts=10, out="")
    _wire_spawn(monkeypatch, runner, popen)
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "stopped by request"
    assert killed == [popen.pid]


def test_run_aborts_the_child_when_the_deadline_passes(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)
    runner = R.AgentRunner()
    _wire_spawn(monkeypatch, runner, _OneShotPopen(timeouts=10))
    res = runner.run("prompt", timeout_s=-1.0)
    assert res.ok is False
    assert res.error == "timeout after -1.0s"


def test_run_wraps_an_unexpected_communicate_failure(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)

    class _Broken(_OneShotPopen):
        def communicate(self, timeout=None):
            raise ValueError("pipe closed")

    runner = R.AgentRunner()
    _wire_spawn(monkeypatch, runner, _Broken())
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "ValueError: pipe closed"


def test_run_refuses_to_launch_an_unattended_agent_it_cannot_audit(monkeypatch, broken_sel):
    runner = R.AgentRunner()

    def _must_not_spawn(cmd, cwd):  # pragma: no cover - asserts the refusal is pre-spawn
        raise AssertionError("the agent was spawned despite a failed audit")

    monkeypatch.setattr(runner, "_spawn_sandboxed_agent", _must_not_spawn)
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "refusing to launch an unattended agent that cannot be audited"


def test_run_spawn_failure_result_or_known_defect(monkeypatch, fake_sel, tmp_path):
    """A missing agent binary must come back as a result, never as an exception.

    PRODUCT DEFECT (reported, not fixed here): both spawn handlers in ``AgentRunner.run``
    call ``_unlink_quietly(cleanup)`` while ``cleanup`` is only ever bound by the tuple
    unpacking that just failed, so the handler raises ``UnboundLocalError`` and the
    documented "never raises on a model-side failure" contract is broken on exactly the
    path written to honour it. Asserted permissively so the test also passes once the
    defect is fixed.
    """
    runner = R.AgentRunner()

    def _missing(cmd, cwd):
        raise FileNotFoundError(2, "No such file or directory", R.CLAUDE_BIN)

    monkeypatch.setattr(runner, "_spawn_sandboxed_agent", _missing)
    try:
        res = runner.run("prompt")
    except (UnboundLocalError, NameError) as exc:
        assert "cleanup" in str(exc)
    else:
        assert res.ok is False
        assert res.error == f"agent binary not found: {R.CLAUDE_BIN}"


def test_run_generic_spawn_failure_result_or_known_defect(monkeypatch, fake_sel):
    """Same defect on the generic-exception handler; see the test above."""
    runner = R.AgentRunner()

    def _broken(cmd, cwd):
        raise PermissionError("sandbox refused")

    monkeypatch.setattr(runner, "_spawn_sandboxed_agent", _broken)
    try:
        res = runner.run("prompt")
    except (UnboundLocalError, NameError) as exc:
        assert "cleanup" in str(exc)
    else:
        assert res.ok is False
        assert res.error == "PermissionError: sandbox refused"


# ── AgentRunner.run: the streaming path ─────────────────────────────────────


def test_streaming_run_emits_activity_audits_tools_and_returns_the_result(monkeypatch, fake_sel):
    seen: list[dict] = []
    runner = R.AgentRunner(on_activity=seen.append)
    lines = [
        "\n",
        "not json\n",
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}}\n',
        '{"type": "assistant", "message": {"content": ['
        '{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}]}}\n',
        '{"type": "result", "result": "done here", "total_cost_usd": 0.75}\n',
    ]
    popen = _StreamPopen(lines, stderr_lines=["warn\n"])
    captured = _wire_spawn(monkeypatch, runner, popen)

    res = runner.run("prompt", cwd="/tmp/wt")

    assert res.ok is True
    assert res.text == "done here"
    assert res.cost_usd == 0.75
    assert runner.total_cost_usd() == 0.75
    assert [e["kind"] for e in seen] == ["text", "tool", "result"]
    cmd = captured["cmd"]
    assert "stream-json" in cmd and "--verbose" in cmd
    # The tool the agent used is audited on top of the blanket launch event.
    assert [c["tool_name"] for c in fake_sel.calls] == ["claude-cli", "Edit"]


def test_streaming_run_unlinks_the_launcher_temp_file(monkeypatch, fake_sel, tmp_path):
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\n", newline="\n")
    runner = R.AgentRunner(on_activity=lambda ev: None)
    popen = _StreamPopen(['{"type": "result", "result": "ok"}\n'])
    _wire_spawn(monkeypatch, runner, popen, cleanup=str(launcher))
    assert runner.run("prompt").ok is True
    assert not launcher.exists()


def test_streaming_run_reports_a_non_zero_exit_with_the_drained_stderr(monkeypatch, fake_sel):
    runner = R.AgentRunner(on_activity=lambda ev: None)
    popen = _StreamPopen(
        ['{"type": "result", "result": "x", "total_cost_usd": 0.1}\n'],
        stderr_lines=["fatal: bad\n"],
        returncode=2,
    )
    _wire_spawn(monkeypatch, runner, popen)
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error.startswith("exit 2:")
    assert "fatal: bad" in res.error
    assert res.cost_usd == 0.1


def test_streaming_run_surfaces_is_error(monkeypatch, fake_sel):
    runner = R.AgentRunner(on_activity=lambda ev: None)
    popen = _StreamPopen(['{"type": "result", "result": "half", "is_error": true}\n'])
    _wire_spawn(monkeypatch, runner, popen)
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "claude reported is_error"
    assert res.text == "half"


def test_streaming_run_stops_on_request(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)
    runner = R.AgentRunner(on_activity=lambda ev: None, stop_check=lambda: True)
    _wire_spawn(monkeypatch, runner, _StreamPopen(['{"type": "result"}\n']))
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "stopped by request"


def test_streaming_run_stops_at_the_deadline(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)
    runner = R.AgentRunner(on_activity=lambda ev: None)
    _wire_spawn(monkeypatch, runner, _StreamPopen(['{"type": "result"}\n']))
    res = runner.run("prompt", timeout_s=-1.0)
    assert res.ok is False
    assert res.error == "timeout after -1.0s"


def test_streaming_run_wraps_a_stdout_read_failure(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)

    def _exploding_lines():
        yield '{"type": "result", "result": "x"}\n'
        raise OSError("pipe died")

    runner = R.AgentRunner(on_activity=lambda ev: None)
    popen = _StreamPopen([])
    popen.stdout = _exploding_lines()
    _wire_spawn(monkeypatch, runner, popen)
    res = runner.run("prompt")
    assert res.ok is False
    assert res.error == "OSError: pipe died"


def test_streaming_run_tolerates_a_missing_stderr_pipe(monkeypatch, fake_sel):
    runner = R.AgentRunner(on_activity=lambda ev: None)
    popen = _StreamPopen(['{"type": "result", "result": "ok"}\n'])
    popen.stderr = None
    _wire_spawn(monkeypatch, runner, popen)
    assert runner.run("prompt").ok is True


def test_streaming_run_terminates_a_child_that_will_not_reap(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "kill_process_tree", lambda pid, sig: None)

    class _NoReap(_StreamPopen):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake-agent", timeout=timeout or 0)

    runner = R.AgentRunner(on_activity=lambda ev: None)
    _wire_spawn(monkeypatch, runner, _NoReap(['{"type": "result", "result": "ok"}\n']))
    assert runner.run("prompt").ok is True


# ── SessionAgentRunner: availability, factory, registration ─────────────────


def test_session_runner_available_when_a_provider_factory_can_be_built(monkeypatch):
    monkeypatch.setattr(
        R,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: SimpleNamespace(create_provider_factory=lambda: object())),
    )
    assert R.SessionAgentRunner.available() is True


def test_session_runner_unavailable_without_a_configured_backend(monkeypatch):
    monkeypatch.setattr(
        R,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: SimpleNamespace(create_provider_factory=lambda: None)),
    )
    assert R.SessionAgentRunner.available() is False


def test_session_runner_unavailable_when_config_load_raises(monkeypatch):
    def _boom():
        raise RuntimeError("config corrupt")

    monkeypatch.setattr(R, "KiroCrewConfig", SimpleNamespace(load=_boom))
    assert R.SessionAgentRunner.available() is False


def test_resolve_factory_prefers_the_injected_one():
    sentinel = object()
    runner = R.SessionAgentRunner(provider_factory=sentinel)
    assert runner._resolve_factory() is sentinel


def test_resolve_factory_falls_back_to_the_configured_provider(monkeypatch):
    made = object()
    monkeypatch.setattr(
        R,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: SimpleNamespace(create_provider_factory=lambda: made)),
    )
    runner = R.SessionAgentRunner()
    assert runner._resolve_factory() is made
    assert runner._resolve_factory() is made  # cached on the instance


def test_session_runner_emit_activity_is_a_noop_without_a_sink():
    R.SessionAgentRunner()._emit_activity({"kind": "text"})  # must not raise


def test_session_runner_emit_activity_swallows_a_sink_error():
    def _boom(ev):
        raise RuntimeError("sink down")

    R.SessionAgentRunner(on_activity=_boom)._emit_activity({"kind": "text"})


def test_ensure_agent_registered_writes_the_apps_own_spec(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    runner = R.SessionAgentRunner()
    assert runner.ensure_agent_registered() is True
    dest = tmp_path / "kiro" / "agents" / f"{runner.agent_name}.json"
    assert dest.is_file()
    src = (
        Path(R.__file__).resolve().parent.parent / "agents" / "discovery.json"
    )
    assert dest.read_bytes() == src.read_bytes()
    assert runner.ensure_agent_registered() is True  # idempotent re-register


def test_ensure_agent_registered_never_clobbers_the_users_own_file(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    runner = R.SessionAgentRunner()
    dest_dir = tmp_path / "kiro" / "agents"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / f"{runner.agent_name}.json"
    dest.write_text('{"name": "mine"}\n', newline="\n")
    assert runner.ensure_agent_registered() is False
    assert dest.read_text() == '{"name": "mine"}\n'


def test_ensure_agent_registered_tolerates_an_unreadable_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    runner = R.SessionAgentRunner()
    dest_dir = tmp_path / "kiro" / "agents"
    dest_dir.mkdir(parents=True)
    # A DIRECTORY at the destination path exists but cannot be read as bytes.
    (dest_dir / f"{runner.agent_name}.json").mkdir()
    assert runner.ensure_agent_registered() is False


def test_ensure_agent_registered_only_self_registers_its_own_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    runner = R.SessionAgentRunner(agent_name="somebody-elses-agent")
    assert runner.ensure_agent_registered() is False
    assert not (tmp_path / "kiro" / "agents").exists()


def test_ensure_agent_registered_never_blocks_a_run_on_a_path_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))

    def _boom():
        raise RuntimeError("no agents dir")

    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", _boom)
    assert R.SessionAgentRunner().ensure_agent_registered() is False


# ── SessionAgentRunner.run (sync bridge) ────────────────────────────────────


def test_session_run_reports_an_unavailable_factory(monkeypatch):
    def _boom():
        raise RuntimeError("config corrupt")

    monkeypatch.setattr(R, "KiroCrewConfig", SimpleNamespace(load=_boom))
    res = R.SessionAgentRunner().run("prompt")
    assert res.ok is False
    assert res.error.startswith("provider factory unavailable")


def test_session_run_reports_no_configured_provider(monkeypatch):
    monkeypatch.setattr(
        R,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: SimpleNamespace(create_provider_factory=lambda: None)),
    )
    res = R.SessionAgentRunner().run("prompt")
    assert res.ok is False
    assert res.error == "no Kiro Crew provider configured"


def test_session_run_drives_the_provider_and_returns_the_assembled_text():
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_TEXT_CHUNK, text="half "),
            _ev(kind=EVENT_TEXT_CHUNK, text="a thought\n"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    runner = R.SessionAgentRunner(
        provider_factory=lambda key, **kw: provider, on_activity=seen.append
    )
    res = runner.run("do the thing", cwd="/tmp/wt", append_system="system rules")
    assert res.ok is True
    assert res.text == "half a thought\n"
    assert provider.started == 1 and provider.shutdowns == 1
    assert provider.prompt.startswith("system rules")
    assert "do the thing" in provider.prompt
    assert seen == [{"kind": "text", "detail": "half a thought"}]


def test_session_run_falls_back_through_the_factory_signatures():
    provider = _FakeProvider([_ev(kind=EVENT_COMPLETE)])
    attempts: list[tuple] = []

    def _picky_factory(session_key, **kw):
        attempts.append(tuple(sorted(kw)))
        if kw:
            raise TypeError("unexpected keyword")
        return provider

    res = R.SessionAgentRunner(provider_factory=_picky_factory).run("p", cwd="/tmp/wt")
    assert res.ok is True
    assert attempts == [("agent", "cwd"), ("agent",), ()]


def test_session_run_never_raises_when_the_provider_dies():
    provider = _FakeProvider([])
    provider.start_error = RuntimeError("provider died")
    res = R.SessionAgentRunner(provider_factory=lambda key, **kw: provider).run("p")
    assert res.ok is False
    assert res.error == "RuntimeError: provider died"


def test_session_run_swallows_a_shutdown_failure():
    provider = _FakeProvider([_ev(kind=EVENT_COMPLETE)])
    provider.shutdown_error = RuntimeError("teardown failed")
    res = R.SessionAgentRunner(provider_factory=lambda key, **kw: provider).run("p")
    assert res.ok is True


def test_session_run_uses_a_stable_session_key_derived_from_the_worktree():
    keys: list[str] = []

    def _factory(session_key, **kw):
        keys.append(session_key)
        return _FakeProvider([_ev(kind=EVENT_COMPLETE)])

    for _ in range(2):
        R.SessionAgentRunner(provider_factory=_factory).run("p", cwd="/tmp/wt-a")
    R.SessionAgentRunner(provider_factory=_factory).run("p", cwd="/tmp/wt-b")
    assert keys[0] == keys[1] != keys[2]
    assert all(k.startswith("auto-improvement-") for k in keys)


# ── SessionAgentRunner._run_async: the permission + watchdog paths ──────────


@pytest.fixture
def allow_governance(monkeypatch):
    """Neutralise the platform governance gate so the app-local checks are isolated."""
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "")


async def _drive(runner, provider, **kw):
    kw.setdefault("cwd", "/tmp/wt")
    kw.setdefault("append_system", None)
    kw.setdefault("timeout_s", 30.0)
    kw.setdefault("t0", time.monotonic())
    return await runner._run_async("prompt", factory=lambda key, **k: provider, **kw)


@pytest.mark.asyncio
async def test_run_async_auto_approves_an_unrestricted_tool(fake_sel, allow_governance):
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_PERMISSION_REQUEST, tool_kind="fsWrite", request_id="r1"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    runner = R.SessionAgentRunner(on_activity=seen.append)
    res = await _drive(runner, provider)
    assert res.ok is True
    assert provider.approved == ["r1"]
    assert {"kind": "tool", "tool": "fsWrite", "detail": "approved"} in seen
    assert fake_sel.calls[0]["critical"] is True


@pytest.mark.asyncio
async def test_run_async_rejects_a_governance_denied_tool(monkeypatch, fake_sel):
    monkeypatch.setattr(R, "_governance_denial", lambda ev, **kw: "reads ~/.ssh")
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_PERMISSION_REQUEST, tool_kind="fsRead", request_id="r1"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    runner = R.SessionAgentRunner(on_activity=seen.append)
    res = await _drive(runner, provider)
    assert res.ok is True
    assert provider.rejected == ["r1"] and provider.approved == []
    assert {"kind": "tool", "tool": "fsRead", "detail": "refused: reads ~/.ssh"} in seen


@pytest.mark.asyncio
async def test_run_async_rejects_a_state_mutating_shell_command(fake_sel, allow_governance):
    provider = _FakeProvider(
        [
            _ev(
                kind=EVENT_PERMISSION_REQUEST,
                tool_kind="bash",
                request_id="r1",
                raw_tool_params={"command": "gh pr comment --body hi"},
            ),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    res = await _drive(R.SessionAgentRunner(on_activity=seen.append), provider)
    assert res.ok is True
    assert provider.rejected == ["r1"]
    assert any(e.get("detail", "").startswith("refused: gh pr comment") for e in seen)


@pytest.mark.asyncio
async def test_run_async_rejects_a_tool_outside_the_callers_allowlist(fake_sel, allow_governance):
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_PERMISSION_REQUEST, tool_kind="bash", request_id="r1"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    res = await _drive(
        R.SessionAgentRunner(on_activity=seen.append), provider, allowed_tools=["Read"]
    )
    assert res.ok is True
    assert provider.rejected == ["r1"]
    assert {"kind": "tool", "tool": "bash", "detail": "refused"} in seen


@pytest.mark.asyncio
async def test_run_async_announces_a_tool_call_and_upgrades_it_with_the_real_target(fake_sel):
    provider = _FakeProvider(
        [
            _ev(
                kind=EVENT_TOOL_CALL,
                tool_kind="read",
                tool_call_id="t1",
                title="Read File",
                raw_tool_params={},
            ),
            _ev(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_kind="read",
                tool_call_id="t1",
                title="Read File",
                raw_tool_params={"path": "src/a.py"},
            ),
            _ev(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_kind="read",
                tool_call_id="t1",
                title="Read File",
                raw_tool_params={"path": "src/a.py"},
            ),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    seen: list[dict] = []
    res = await _drive(R.SessionAgentRunner(on_activity=seen.append), provider, max_turns=0)
    assert res.ok is True
    details = [e["detail"] for e in seen]
    assert details == ["Read File", "Read File · src/a.py"]


@pytest.mark.asyncio
async def test_run_async_enforces_max_turns_on_the_session_path(fake_sel):
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_TOOL_CALL, tool_kind="read", tool_call_id="t1", title="Read File"),
            _ev(kind=EVENT_TOOL_CALL, tool_kind="read", tool_call_id="t2", title="Read File"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    res = await _drive(R.SessionAgentRunner(), provider, max_turns=1)
    assert res.ok is False
    assert res.error == "max_turns (1) reached"


@pytest.mark.asyncio
async def test_run_async_keeps_partial_text_and_bills_cost_on_a_stop_request(fake_sel):
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_TEXT_CHUNK, text="partial answer", cost_usd=0.4),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    stop = {"now": False}
    runner = R.SessionAgentRunner(stop_check=lambda: stop["now"])

    calls = {"n": 0}

    def _stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    runner._stop_check = _stop_after_first
    res = await _drive(runner, provider)
    assert res.ok is False
    assert res.error == "stopped by request"
    assert res.text == "partial answer"
    assert res.cost_usd == 0.4
    assert runner.total_cost_usd() == 0.4


@pytest.mark.asyncio
async def test_run_async_returns_the_accumulated_text_when_the_wall_clock_expired(fake_sel):
    provider = _FakeProvider([_ev(kind=EVENT_COMPLETE)])
    runner = R.SessionAgentRunner()
    res = await _drive(runner, provider, timeout_s=1.0, t0=time.monotonic() - 100.0)
    assert res.ok is False
    assert res.error == "timeout after 1.0s"


@pytest.mark.asyncio
async def test_run_async_force_cancels_an_in_turn_stall(fake_sel):
    provider = _FakeProvider([_ev(kind=EVENT_COMPLETE)], stall_s=5.0)
    res = await _drive(R.SessionAgentRunner(), provider, timeout_s=0.05)
    assert res.ok is False
    assert res.error == "timeout after 0.05s"


@pytest.mark.asyncio
async def test_run_async_degrades_gracefully_for_a_non_iterator_stream(fake_sel):
    provider = _FakeProvider([], plain_stream=True)
    res = await _drive(R.SessionAgentRunner(), provider)
    assert res.ok is True
    assert res.text == ""


@pytest.mark.asyncio
async def test_run_async_treats_an_exhausted_stream_as_success(fake_sel):
    provider = _FakeProvider([_ev(kind=EVENT_TEXT_CHUNK, text="all done")])
    res = await _drive(R.SessionAgentRunner(), provider)
    assert res.ok is True
    assert res.text == "all done"


# ── _reject / _approve ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_audits_the_refusal_and_tells_the_provider(fake_sel):
    provider = _FakeProvider()
    await R.SessionAgentRunner._reject(provider, "r9", tool="bash", session_key="s")
    assert provider.rejected == ["r9"]
    (call,) = fake_sel.calls
    assert call["outcome"] == "denied"
    assert call["error"] == "not_in_allowed_tools"


@pytest.mark.asyncio
async def test_reject_still_refuses_when_the_audit_cannot_be_written(broken_sel):
    provider = _FakeProvider()
    await R.SessionAgentRunner._reject(provider, "r9", tool="bash")
    assert provider.rejected == ["r9"]


@pytest.mark.asyncio
async def test_reject_tolerates_a_provider_that_cannot_be_told(fake_sel):
    class _Deaf(_FakeProvider):
        async def reject_tool(self, rid):
            raise RuntimeError("stdin closed")

    await R.SessionAgentRunner._reject(_Deaf(), "r9", tool="bash")  # must not raise


@pytest.mark.asyncio
async def test_approve_records_a_one_shot_approval_before_granting_it(fake_sel):
    provider = _FakeProvider()
    await R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite", session_key="s")
    assert provider.approved == ["r1"]
    (call,) = fake_sel.calls
    assert call["critical"] is True
    assert call["outcome"] == "auto_approved"


@pytest.mark.asyncio
async def test_approve_rejects_instead_of_approving_when_the_audit_fails(broken_sel):
    provider = _FakeProvider()
    await R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite")
    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_approve_survives_a_failed_reject_after_a_failed_audit(broken_sel):
    class _Deaf(_FakeProvider):
        async def reject_tool(self, rid):
            raise RuntimeError("stdin closed")

    provider = _Deaf()
    await R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite")
    assert provider.approved == []


@pytest.mark.asyncio
async def test_approve_tolerates_a_provider_that_cannot_be_told(fake_sel):
    class _Deaf(_FakeProvider):
        async def approve_tool(self, rid):
            raise RuntimeError("stdin closed")

    await R.SessionAgentRunner._approve(_Deaf(), "r1", tool="fsWrite")  # must not raise


# ── _repro_test_dir ─────────────────────────────────────────────────────────


def test_repro_test_dir_defaults_to_test_when_the_repo_has_neither(tmp_path):
    assert R._repro_test_dir(tmp_path) == "test"


def test_repro_test_dir_picks_the_directory_the_repo_actually_uses(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_a.py").write_text("", newline="\n")
    (tmp_path / "tests").mkdir()
    for name in ("test_b.py", "test_c.py"):
        (tmp_path / "tests" / name).write_text("", newline="\n")
    assert R._repro_test_dir(tmp_path) == "tests"


def test_repro_test_dir_prefers_the_singular_directory_when_it_is_the_suite(tmp_path):
    (tmp_path / "test").mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / "test" / name).write_text("", newline="\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_stray.py").write_text("", newline="\n")
    assert R._repro_test_dir(tmp_path) == "test"


def test_repro_test_dir_breaks_a_tie_deterministically(tmp_path):
    for name in ("test", "tests"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "test_a.py").write_text("", newline="\n")
    assert R._repro_test_dir(tmp_path) == "tests"


# ── author_bug_fix / author_perf_fix ────────────────────────────────────────


@dataclasses.dataclass
class _ReproTest:
    test_path: str = "test/test_bug_invented.py"
    test_id: str = "test/test_bug_invented.py"


@dataclasses.dataclass(frozen=True)
class _FrozenReproTest:
    test_path: str = "test/test_bug_invented.py"
    test_id: str = "test/test_bug_invented.py"


def _candidate(repro=None):
    return SimpleNamespace(
        target="src/pkg/core.py::add",
        signature="add(1, 2) == 4",
        hypothesis="off-by-one in the accumulator",
        reproducing_test=repro,
    )


@pytest.fixture
def fake_git(monkeypatch):
    """Replace the host-side ``git status --porcelain`` probe; never runs real git."""
    state = {"stdout": "", "argv": None}

    def _run(argv, **kw):
        state["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout=state["stdout"], stderr="")

    monkeypatch.setattr(R, "require_pinned", lambda cwd: None)
    monkeypatch.setattr(R.subprocess, "run", _run)
    return state


def test_author_bug_fix_bails_on_a_genuine_runner_failure(tmp_path, fake_git):
    runner = _FakeAgentRunner(R.AgentResult(ok=False, error="provider died"))
    assert R.author_bug_fix(runner, candidate=_candidate(), worktree=tmp_path) is False
    assert fake_git["argv"] is None  # never even probed the tree


@pytest.mark.parametrize("error", ["timeout after 600s", "max_turns (40) reached"])
def test_author_bug_fix_harvests_a_bounded_exit(tmp_path, fake_git, error):
    fake_git["stdout"] = " M src/pkg/core.py\n"
    runner = _FakeAgentRunner(R.AgentResult(ok=False, error=error))
    assert R.author_bug_fix(runner, candidate=_candidate(), worktree=tmp_path) is True
    assert "--porcelain" in fake_git["argv"]
    assert "core.fsmonitor=false" in fake_git["argv"]


def test_author_bug_fix_treats_a_clean_tree_as_no_defect_found(tmp_path, fake_git):
    fake_git["stdout"] = "   \n"
    runner = _FakeAgentRunner(R.AgentResult(ok=True, text="NO DEFECT FOUND — already handled"))
    assert R.author_bug_fix(runner, candidate=_candidate(), worktree=tmp_path) is False


def test_author_bug_fix_adopts_the_test_the_agent_actually_wrote(tmp_path, fake_git):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_bug_real.py").write_text("def test_x():\n    pass\n", newline="\n")
    fake_git["stdout"] = "?? test/test_bug_real.py\n M src/pkg/core.py\n"
    repro = _ReproTest()
    runner = _FakeAgentRunner(R.AgentResult(ok=True, text="fixed the accumulator"))
    assert R.author_bug_fix(runner, candidate=_candidate(repro), worktree=tmp_path) is True
    assert repro.test_path == "test/test_bug_real.py"
    assert repro.test_id == "test/test_bug_real.py"


def test_author_bug_fix_prompt_carries_the_gates_own_test_command(tmp_path, fake_git):
    fake_git["stdout"] = " M src/pkg/core.py\n"
    runner = _FakeAgentRunner(R.AgentResult(ok=True))
    R.author_bug_fix(
        runner,
        candidate=_candidate(_ReproTest(test_id="test/test_bug_x.py::test_x")),
        worktree=tmp_path,
        test_cmd_hint="/venv/bin/python -m pytest <test_path>",
    )
    prompt = runner.prompts[0]
    assert "/venv/bin/python -m pytest <test_path>" in prompt
    assert "suggested reproducing test id: test/test_bug_x.py::test_x" in prompt
    assert "src/pkg/core.py::add" in prompt
    kwargs = runner.kwargs[0]
    assert kwargs["allowed_tools"] == ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]
    assert kwargs["timeout_s"] == 600
    assert kwargs["cwd"] == str(tmp_path)


def test_author_bug_fix_prompt_omits_the_hints_it_was_not_given(tmp_path, fake_git):
    fake_git["stdout"] = " M src/pkg/core.py\n"
    runner = _FakeAgentRunner(R.AgentResult(ok=True))
    R.author_bug_fix(runner, candidate=_candidate(), worktree=tmp_path)
    prompt = runner.prompts[0]
    assert "suggested reproducing test id" not in prompt
    assert "HOW TO RUN TESTS" not in prompt


def test_author_perf_fix_requires_a_real_diff(tmp_path, fake_git):
    fake_git["stdout"] = ""
    runner = _FakeAgentRunner(R.AgentResult(ok=True, text="NO WIN FOUND — already linear"))
    assert R.author_perf_fix(runner, candidate=_candidate(), worktree=tmp_path) is False


def test_author_perf_fix_returns_true_for_a_bounded_exit_that_left_a_diff(tmp_path, fake_git):
    fake_git["stdout"] = " M src/pkg/core.py\n"
    runner = _FakeAgentRunner(R.AgentResult(ok=False, error="timeout after 600s"))
    assert R.author_perf_fix(runner, candidate=_candidate(), worktree=tmp_path) is True


def test_author_perf_fix_bails_on_a_genuine_runner_failure(tmp_path, fake_git):
    runner = _FakeAgentRunner(R.AgentResult(ok=False, error="stopped by request"))
    assert R.author_perf_fix(runner, candidate=_candidate(), worktree=tmp_path) is False
    assert fake_git["argv"] is None


def test_author_perf_fix_prompt_forbids_editing_the_ruler(tmp_path, fake_git):
    fake_git["stdout"] = " M src/pkg/core.py\n"
    runner = _FakeAgentRunner(R.AgentResult(ok=True))
    R.author_perf_fix(
        runner,
        candidate=_candidate(),
        worktree=tmp_path,
        test_cmd_hint="/venv/bin/python -m pytest",
    )
    prompt = runner.prompts[0]
    assert "behavior-preserving" in prompt
    assert "HOW TO RUN TESTS" in prompt
    assert "Do NOT touch any test file" in prompt


# ── _adopt_authored_test ────────────────────────────────────────────────────


def test_adopt_authored_test_is_a_noop_without_a_reproducing_test(tmp_path):
    candidate = _candidate(None)
    R._adopt_authored_test(candidate, tmp_path, "?? test/test_bug_x.py\n")
    assert candidate.reproducing_test is None


def test_adopt_authored_test_leaves_the_candidate_alone_when_nothing_was_added(tmp_path):
    repro = _ReproTest()
    R._adopt_authored_test(_candidate(repro), tmp_path, " M src/pkg/core.py\n?? notes.md\n")
    assert repro.test_path == "test/test_bug_invented.py"


def test_adopt_authored_test_takes_the_destination_of_a_rename(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_bug_moved.py").write_text("", newline="\n")
    repro = _ReproTest()
    R._adopt_authored_test(
        _candidate(repro), tmp_path, "R  test/old.py -> test/test_bug_moved.py\n"
    )
    assert repro.test_path == "test/test_bug_moved.py"


def test_adopt_authored_test_unquotes_a_porcelain_path(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_bug_quoted.py").write_text("", newline="\n")
    repro = _ReproTest()
    R._adopt_authored_test(_candidate(repro), tmp_path, '?? "test/test_bug_quoted.py"\n')
    assert repro.test_path == "test/test_bug_quoted.py"


def test_adopt_authored_test_picks_the_shortest_path_then_lexicographically(tmp_path):
    (tmp_path / "test").mkdir()
    for name in ("test_bug_b.py", "test_bug_a.py", "test_bug_longer.py"):
        (tmp_path / "test" / name).write_text("", newline="\n")
    repro = _ReproTest()
    porcelain = (
        "?? test/test_bug_longer.py\n?? test/test_bug_b.py\n?? test/test_bug_a.py\nxx\n"
    )
    R._adopt_authored_test(_candidate(repro), tmp_path, porcelain)
    assert repro.test_path == "test/test_bug_a.py"


def test_adopt_authored_test_ignores_a_path_that_is_not_on_disk(tmp_path):
    repro = _ReproTest()
    R._adopt_authored_test(_candidate(repro), tmp_path, "?? test/test_bug_ghost.py\n")
    assert repro.test_path == "test/test_bug_invented.py"


def test_adopt_authored_test_returns_early_when_already_pointed_at_the_file(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_bug_same.py").write_text("", newline="\n")
    repro = _ReproTest(test_path="test/test_bug_same.py", test_id="test/test_bug_same.py::case")
    R._adopt_authored_test(_candidate(repro), tmp_path, "?? test/test_bug_same.py\n")
    assert repro.test_id == "test/test_bug_same.py::case"  # untouched


def test_adopt_authored_test_gives_up_on_an_immutable_candidate(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_bug_frozen.py").write_text("", newline="\n")
    repro = _FrozenReproTest()
    R._adopt_authored_test(_candidate(repro), tmp_path, "?? test/test_bug_frozen.py\n")
    assert repro.test_path == "test/test_bug_invented.py"


def test_adopt_authored_test_never_raises_on_malformed_porcelain(tmp_path):
    repro = _ReproTest()
    R._adopt_authored_test(_candidate(repro), tmp_path, None)  # splitlines would raise
    assert repro.test_path == "test/test_bug_invented.py"


# ── module constants the safety perimeter depends on ────────────────────────


def test_git_safe_config_disables_hooks_and_the_fsmonitor():
    assert R._GIT_SAFE_CONFIG is R.GIT_SAFE_CONFIG
    assert "core.fsmonitor=false" in R._GIT_SAFE_CONFIG
    assert any(v.startswith("core.hooksPath=") for v in R._GIT_SAFE_CONFIG)


def test_agent_result_defaults_are_a_failure_shaped_empty_record():
    res = R.AgentResult(ok=False)
    assert (res.text, res.error, res.cost_usd, res.duration_s, res.raw) == ("", "", 0.0, 0.0, {})


def test_claude_bin_is_overridable_from_the_environment():
    assert isinstance(R.CLAUDE_BIN, str) and R.CLAUDE_BIN


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal names")
def test_sigkill_is_available_for_the_escalation_path():
    assert int(R.SIGKILL) > 0
