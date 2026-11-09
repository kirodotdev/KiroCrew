"""``kirocrew sandbox explain -- CMD``: run a command under the agent sandbox and report.

The real backend is replaced at the module's seams (tier resolution, backend probe,
tier plan, deny gate, spawn), so these tests run on any host and never launch a
namespace or Seatbelt child.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from kiro_crew import cli_parser_gateway, cli_sandbox_explain, cli_server, sandbox
from kiro_crew.agent_sdk.tool_gate import (
    adapter_expose_files,
    adapter_hidden_credential_dirs,
    label_for,
)
from kiro_crew.platform.security_authority import PolicyAuthority
from kiro_crew.testing import wait

# The fixture replaces the module's deny check and environment base; the tests that
# exercise the real ones put them back.
_REAL_DENY_REASON = cli_sandbox_explain._deny_reason
_REAL_AGENT_BASE_ENV = cli_sandbox_explain._agent_base_env


def _plan(
    *,
    tier_dirs: tuple[str, ...] = ("/users/alex/.gnupg", "/users/alex/.docker"),
    harness_dirs: tuple[str, ...] = (),
    **overrides: object,
) -> SimpleNamespace:
    masks = tuple(
        SimpleNamespace(path=path, origin=origin, cancelled=False, read_only_when_cancelled=False)
        for origin, paths in (("tier", tier_dirs), ("caller", harness_dirs))
        for path in paths
    )
    fields: dict[str, object] = {
        "tier": "standard",
        "masks": masks,
        "files": ("/users/alex/.netrc",),
        "expose": (),
        "readonly": ("/users/alex/.kiro/crew/bin",),
        "runtime_parents": (),
        "hide_ssh": False,
        "ssh_dir": "/users/alex/.ssh",
        "denied_socket_patterns": (),
        "refusals": (),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"spawn": [], "spawn_kwargs": [], "run": [], "plan": []}
    mod = cli_sandbox_explain
    monkeypatch.setattr(mod, "configured_sandbox_mode", lambda: "auto")
    monkeypatch.setattr(mod, "effective_sandbox_mode", lambda mode: "standard")
    monkeypatch.setattr(mod, "detect_backend", lambda mode: "namespace")
    monkeypatch.setattr(mod, "_deny_reason", lambda command: None)
    monkeypatch.setattr(mod, "_forward_ssh_auth_sock", lambda mode, hidden: False)
    monkeypatch.setattr(mod, "_configured_harness", lambda: "kas")

    def _spawn_plan(
        backend: str, level: str, forward: bool, hidden: tuple = (), expose: tuple = ()
    ) -> SimpleNamespace:
        calls["plan"].append((backend, level, forward))
        calls.setdefault("plan_masks", []).append((hidden, expose))
        return _plan(tier=level)

    def _wrap(argv: list[str], mode: str, **kwargs: object) -> tuple[list[str], None]:
        """Stands in for ``wrap_argv``: records its options and applies the verdict."""
        calls["spawn"].append((argv, mode))
        calls["spawn_kwargs"].append(kwargs)
        out = kwargs.get("forward_ssh_auth_sock_out")
        if isinstance(out, list):
            out.append(bool(kwargs.get("forward_ssh_auth_sock")))
        return ["launcher", *argv], None

    monkeypatch.setattr(mod, "spawn_plan", _spawn_plan)
    monkeypatch.setattr(mod, "wrap_argv", _wrap)
    monkeypatch.setattr(mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(mod, "_agent_base_env", lambda: {"K": "V"})
    return calls


class _FakeProc:
    """A finished child: *stderr* waits in a real pipe, *rc* is its exit status."""

    def __init__(self, rc: int, stderr: str) -> None:
        reader, writer = os.pipe()
        os.write(writer, stderr.encode())
        os.close(writer)
        self.stderr = os.fdopen(reader, "rb")
        self._rc = rc

    def __enter__(self) -> "_FakeProc":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stderr.close()

    def wait(self) -> int:
        return self._rc


def _runner(calls: dict[str, list], rc: int, stderr: str = ""):
    def _popen(argv: list[str], env: dict[str, str], **kwargs: object) -> _FakeProc:
        calls["run"].append((argv, env))
        return _FakeProc(rc, stderr)

    return _popen


def test_runs_under_the_agent_tier_and_reports_masks(seams, capsys) -> None:
    rc = cli_sandbox_explain.explain(["ls", "/users/alex/.gnupg"], popen=_runner(seams, 2))
    out = capsys.readouterr().out
    assert rc == 2
    assert seams["spawn"] == [(["ls", "/users/alex/.gnupg"], "standard")]
    assert seams["run"] == [(["launcher", "ls", "/users/alex/.gnupg"], {"K": "V"})]
    assert seams["plan"] == [("namespace", "standard", False)]
    assert "tier: standard (agent.sandbox=auto)" in out
    assert "backend: namespace" in out
    assert "ran: yes, exit code 2" in out
    assert "hidden: /users/alex/.gnupg" in out
    assert "hidden: /users/alex/.netrc" in out
    assert "read-only: /users/alex/.kiro/crew/bin" in out
    assert "network: not restricted" in out
    assert "deny rules: none matched" in out
    assert "harness: kas (no harness credential mask)" in out
    assert seams["spawn_kwargs"][0]["extra_hidden_dirs"] == ()
    assert seams["plan_masks"] == [((), ())]


def test_a_denied_command_is_refused_before_launch(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_sandbox_explain,
        "_deny_reason",
        lambda command: f"Blocked by security policy: {command}",
    )
    rc = cli_sandbox_explain.explain(["rm", "-rf", "/"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert seams["spawn"] == [] and seams["run"] == []
    assert "ran: no (refused by a deny rule)" in out
    assert "deny rule: Blocked by security policy: rm -rf /" in out


def test_a_sandbox_that_refuses_the_launch_is_reported(seams, monkeypatch, capsys) -> None:
    def _refuse(argv: list[str], mode: str, **kwargs: object):
        raise RuntimeError("no sandbox backend on this host")

    monkeypatch.setattr(cli_sandbox_explain, "wrap_argv", _refuse)
    rc = cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert seams["run"] == []
    assert "ran: no (sandbox refused the launch: no sandbox backend on this host)" in out


def test_off_tier_says_there_is_no_confinement(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "effective_sandbox_mode", lambda mode: "off")
    monkeypatch.setattr(cli_sandbox_explain, "detect_backend", lambda mode: "none")
    rc = cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert seams["plan"] == []
    assert "confinement: none (no sandbox backend for this tier)" in out


def test_seatbelt_lists_refused_sockets_and_plan_refusals(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "detect_backend", lambda mode: "sandbox-exec")
    refusal = SimpleNamespace(kind="window", message="refused window %s", args=("/x",))
    monkeypatch.setattr(
        cli_sandbox_explain,
        "spawn_plan",
        lambda backend, level, forward, *masks: _plan(
            denied_socket_patterns=("/tmp/ssh-*",), refusals=(refusal,)
        ),
    )
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert "network: refused socket /tmp/ssh-*" in out
    assert "plan refused: refused window /x" in out


def test_an_empty_command_prints_usage(seams, capsys) -> None:
    assert cli_sandbox_explain.explain([], popen=_runner(seams, 0)) == 2
    assert "Usage: kirocrew sandbox explain -- COMMAND" in capsys.readouterr().err


def test_the_parser_takes_everything_after_the_separator() -> None:
    parser = argparse.ArgumentParser()
    cli_parser_gateway.register_sandbox_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["sandbox", "explain", "--", "ls", "-la", "--color"])
    assert args.sandbox_action == "explain"
    assert args.explain_argv == ["--", "ls", "-la", "--color"]


def test_the_dispatcher_hands_the_command_to_explain(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli_sandbox_explain, "explain", lambda argv: seen.append(argv) or 5)
    ns = argparse.Namespace(sandbox_action="explain", explain_argv=["--", "ls", "-la"])
    assert cli_server._sandbox_cmd(ns) == 5
    assert seen == [["ls", "-la"]]


def test_explain_never_hands_the_command_the_trusted_harness_delegation(seams) -> None:
    cli_sandbox_explain.explain(["./kiro-cli", "chat"], popen=_runner(seams, 0))
    assert seams["spawn_kwargs"][0]["is_kiro_cli"] is False


class _TouchOverlay:
    def extra_deny_patterns(self) -> tuple[str, ...]:
        return ("*touch*",)


def test_an_overlay_deny_pattern_refuses_the_command(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "_deny_reason", _REAL_DENY_REASON)
    monkeypatch.setattr(
        cli_sandbox_explain,
        "current_context",
        lambda: SimpleNamespace(security=PolicyAuthority(overlay=_TouchOverlay())),
    )
    rc = cli_sandbox_explain.explain(["touch", "blocked-file"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert seams["run"] == []
    assert "ran: no (refused by a deny rule)" in out


def test_an_operator_auto_deny_glob_refuses_the_command(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "_deny_reason", _REAL_DENY_REASON)
    monkeypatch.setattr(
        cli_sandbox_explain,
        "_hooks_section",
        lambda: {"auto_deny_tools": ["*frobnicate*"]},
    )
    rc = cli_sandbox_explain.explain(["frobnicate", "--now"], popen=_runner(seams, 0))
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert seams["run"] == []
    assert "ran: no (refused by a deny rule)" in capsys.readouterr().out


def test_a_refusal_after_spawn_is_reported_as_a_refusal(seams, monkeypatch, capsys) -> None:
    corroborated: list[tuple[str, str]] = []

    def _corroborate(output: str, *, mode: str = "strict", extra_hidden_dirs: tuple = ()):
        corroborated.append((output, mode, extra_hidden_dirs))
        return ("no_backend", "seccomp has no syscall table here", "use another host")

    monkeypatch.setattr(cli_sandbox_explain, "corroborate_launcher_refusal", _corroborate)
    line = "sandbox_launcher: BLOCKED\n"
    rc = cli_sandbox_explain.explain(["true"], popen=_runner(seams, 1, line))
    captured = capsys.readouterr()
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert corroborated == [(line, "standard", ())]
    assert "ran: no (sandbox refused the launch: seccomp has no syscall table here)" in captured.out


def test_an_uncorroborated_launcher_line_is_the_commands_own_output(
    seams, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli_sandbox_explain, "corroborate_launcher_refusal", lambda output, **options: None
    )
    rc = cli_sandbox_explain.explain(
        ["fake"], popen=_runner(seams, 1, "sandbox_launcher: forged\n")
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "ran: yes, exit code 1" in captured.out


def test_a_clean_exit_never_asks_the_launcher_to_corroborate(seams, monkeypatch) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "corroborate_launcher_refusal", lambda *a, **k: 1 / 0)
    assert cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0, "noise")) == 0


def test_a_signal_death_exits_with_the_shell_convention(seams, capsys) -> None:
    rc = cli_sandbox_explain.explain(["sleep", "9"], popen=_runner(seams, -9))
    assert rc == 137
    assert "ran: yes, exit code 137" in capsys.readouterr().out


@pytest.fixture
def real_env(seams, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """The real environment base under the real ``scrub_agent_subprocess_env``.

    The wrap stays the recording stand-in; this returns the options it recorded.
    """
    monkeypatch.setattr(cli_sandbox_explain, "_agent_base_env", _REAL_AGENT_BASE_ENV)
    return seams["spawn_kwargs"]


def test_a_forwarded_agent_socket_reaches_the_command_as_it_reaches_an_agent_child(
    seams, real_env, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/opt/crew")
    monkeypatch.setattr(cli_sandbox_explain, "_forward_ssh_auth_sock", lambda mode, hidden: True)
    cli_sandbox_explain.explain(["ssh-add", "-l"], popen=_runner(seams, 0))
    env = seams["run"][0][1]
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"
    assert "PYTHONPATH" not in env
    assert real_env[0]["forward_ssh_auth_sock"] is True
    assert real_env[0]["strip_python_env"] is True
    assert seams["plan"] == [("namespace", "standard", True)]
    assert "ssh-agent socket: forwarded" in capsys.readouterr().out


def test_a_withheld_agent_socket_is_scrubbed_and_reported(
    seams, real_env, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    cli_sandbox_explain.explain(["ssh-add", "-l"], popen=_runner(seams, 0))
    assert "SSH_AUTH_SOCK" not in seams["run"][0][1]
    assert seams["plan"] == [("namespace", "standard", False)]
    assert "ssh-agent socket: withheld" in capsys.readouterr().out


def test_the_scrub_follows_the_verdict_the_wrap_applied(
    seams, real_env, monkeypatch, capsys
) -> None:
    """A wrap that takes the forward away (a raised floor) keeps the socket out."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    monkeypatch.setattr(cli_sandbox_explain, "_forward_ssh_auth_sock", lambda mode, hidden: True)

    def _wrap_withdraws(argv: list[str], mode: str, **kwargs: object) -> tuple[list[str], None]:
        out = kwargs["forward_ssh_auth_sock_out"]
        assert isinstance(out, list)
        out.append(False)
        return ["launcher", *argv], None

    monkeypatch.setattr(cli_sandbox_explain, "wrap_argv", _wrap_withdraws)
    cli_sandbox_explain.explain(["ssh-add", "-l"], popen=_runner(seams, 0))
    assert "SSH_AUTH_SOCK" not in seams["run"][0][1]
    assert seams["plan"] == [("namespace", "standard", False)]
    assert "ssh-agent socket: withheld" in capsys.readouterr().out


def test_the_command_runs_inside_the_cgroup_scope(seams, monkeypatch) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "cgroup_scope_argv", lambda argv: ["scope", *argv])
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    assert seams["run"][0][0] == ["scope", "launcher", "true"]


def test_the_environment_starts_from_the_agents_base(seams, real_env, monkeypatch) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "agent_env_overlay", lambda: {"TEAM_FLAG": "on"})
    monkeypatch.setattr(cli_sandbox_explain, "augmented_path", lambda path: f"{path}:/extra")
    monkeypatch.setattr(cli_sandbox_explain, "append_mcp_path_dirs", lambda path: f"{path}:/mcp")
    monkeypatch.setenv("PATH", "/usr/bin")
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    env = seams["run"][0][1]
    assert env["TEAM_FLAG"] == "on"
    assert env["PATH"] == "/usr/bin:/extra:/mcp"


@pytest.fixture
def real_ssh_verdict(seams, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ssh-agent verdict, on a non-Windows host with no consent and no floor."""
    from kiro_crew import ssh_auth_sock_consent

    monkeypatch.setattr(sandbox.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(ssh_auth_sock_consent, "is_granted", lambda: False)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(
        cli_sandbox_explain, "_forward_ssh_auth_sock", sandbox._forward_ssh_auth_sock
    )


def _floor_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_sandbox_explain,
        "enforce_sandbox_floor",
        lambda harness, mode: calls.append((harness, mode)),
    )
    return calls


def test_an_enforced_harness_masks_the_command_as_it_masks_its_own_child(
    seams, real_env, real_ssh_verdict, monkeypatch, capsys
) -> None:
    floor = _floor_calls(monkeypatch)
    monkeypatch.setattr(cli_sandbox_explain, "_configured_harness", lambda: "codex")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    hidden = adapter_hidden_credential_dirs("codex")
    expose = adapter_expose_files("codex", hidden)
    assert any(os.path.basename(path) == ".ssh" for path in hidden)
    cli_sandbox_explain.explain(["cat", "~/.ssh/id_rsa"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert floor == [("codex", "standard")]
    assert real_env[0]["extra_hidden_dirs"] == hidden
    assert real_env[0]["extra_expose_files"] == expose
    assert real_env[0]["forward_ssh_auth_sock"] is False
    assert "SSH_AUTH_SOCK" not in seams["run"][0][1]
    assert seams["plan"] == [("namespace", "standard", False)]
    assert seams["plan_masks"] == [(hidden, expose)]
    assert f"harness: {label_for('codex')} (its credential mask applies)" in out
    assert "ssh-agent socket: withheld" in out


def test_an_unenforced_harness_keeps_the_agent_socket_on_the_standard_tier(
    seams, real_env, real_ssh_verdict, monkeypatch, capsys
) -> None:
    _floor_calls(monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    cli_sandbox_explain.explain(["cat", "~/.ssh/id_rsa"], popen=_runner(seams, 0))
    assert seams["run"][0][1]["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"
    assert real_env[0]["forward_ssh_auth_sock"] is True
    assert "ssh-agent socket: forwarded" in capsys.readouterr().out


def test_a_harness_that_would_start_unmasked_is_refused_before_launch(
    seams, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "_configured_harness", lambda: "codex")
    monkeypatch.setattr(cli_sandbox_explain, "effective_sandbox_mode", lambda mode: "off")
    monkeypatch.setattr(cli_sandbox_explain, "detect_backend", lambda mode: "none")
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    rc = cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == cli_sandbox_explain.REFUSED_EXIT
    assert seams["spawn"] == [] and seams["run"] == []
    assert "ran: no (the agent's harness does not start on this tier: " in out
    assert label_for("codex") in out


def test_the_report_names_what_the_harness_mask_hides_and_keeps_readable(
    seams, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli_sandbox_explain,
        "spawn_plan",
        lambda backend, level, forward, *masks: _plan(
            harness_dirs=("/users/alex/.ssh", "/users/alex/.gnupg"),
            expose=(("/users/alex/.aws/config", "config"),),
        ),
    )
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert "hidden (harness): /users/alex/.ssh" in out
    assert "hidden: /users/alex/.gnupg" in out
    assert "hidden (harness): /users/alex/.gnupg" not in out
    assert "readable inside a mask: /users/alex/.aws/config" in out


def test_the_strict_tier_lists_the_ssh_dir_it_hides(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_sandbox_explain,
        "spawn_plan",
        lambda backend, level, forward, *masks: _plan(tier="strict", hide_ssh=True),
    )
    cli_sandbox_explain.explain(["ls", "/users/alex/.ssh"], popen=_runner(seams, 0))
    assert "hidden: /users/alex/.ssh (known_hosts stays readable)" in capsys.readouterr().out


def test_a_harness_mask_over_the_ssh_dir_is_listed_once(seams, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli_sandbox_explain,
        "spawn_plan",
        lambda backend, level, forward, *masks: _plan(
            hide_ssh=True, harness_dirs=("/users/alex/.ssh",)
        ),
    )
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    lines = [line for line in capsys.readouterr().out.splitlines() if "/users/alex/.ssh" in line]
    assert lines == ["  hidden (harness): /users/alex/.ssh"]


@pytest.mark.skipif(sys.platform == "win32", reason="the namespace planner reads POSIX ids")
def test_the_real_strict_plan_reports_the_ssh_dir() -> None:
    plan = cli_sandbox_explain.spawn_plan("namespace", "strict", False)
    assert plan.hide_ssh
    lines = cli_sandbox_explain._plan_lines("namespace", "strict", False, (), ())
    assert f"hidden: {plan.ssh_dir} (known_hosts stays readable)" in lines


def test_seatbelt_seals_outside_the_readonly_list_are_reported(seams, monkeypatch, capsys) -> None:
    lifted = SimpleNamespace(
        path="/users/alex/.kiro/crew/governance",
        origin="tier",
        cancelled=True,
        read_only_when_cancelled=True,
    )
    monkeypatch.setattr(cli_sandbox_explain, "detect_backend", lambda mode: "sandbox-exec")
    monkeypatch.setattr(
        cli_sandbox_explain,
        "spawn_plan",
        lambda backend, level, forward, *masks: _plan(
            masks=(lifted,), runtime_parents=("/users/alex/.kiro/crew/run",)
        ),
    )
    cli_sandbox_explain.explain(["true"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert "read-only: /users/alex/.kiro/crew/run" in out
    assert "read-only: /users/alex/.kiro/crew/governance" in out
    assert "hidden: /users/alex/.kiro/crew/governance" not in out


def test_a_prompt_on_stderr_is_visible_while_the_command_waits() -> None:
    seen = threading.Event()

    class _Sink:
        def __init__(self) -> None:
            self.text = ""

        def write(self, text: str) -> int:
            self.text += text
            if "Continue? " in self.text:
                seen.set()
            return len(text)

        def flush(self) -> None:
            pass

    reader, writer = os.pipe()
    result: list[tuple[int, str, bool]] = []
    script = "import sys; sys.stderr.write('Continue? '); sys.stderr.flush(); sys.stdin.readline()"
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=reader, stderr=subprocess.PIPE)
    worker = threading.Thread(
        target=lambda: result.append(cli_sandbox_explain._pump_stderr(proc, _Sink()))
    )
    worker.start()
    try:
        assert seen.wait(wait.FALLBACK_TIMEOUT_SECS)
        assert worker.is_alive()
    finally:
        os.write(writer, b"y\n")
        os.close(writer)
        worker.join(wait.FALLBACK_TIMEOUT_SECS)
        os.close(reader)
        proc.stderr.close()
    assert result == [(0, "Continue? ", False)]


def test_the_stderr_tail_is_capped_while_the_stream_is_not(monkeypatch) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "STDERR_TAIL_BYTES", 8)
    chunks: list[str] = []
    sink = SimpleNamespace(write=chunks.append, flush=lambda: None)
    script = "import sys; sys.stderr.write('a' * 20 + 'tail-end')"
    with subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE) as proc:
        code, tail, held = cli_sandbox_explain._pump_stderr(proc, sink)
    assert (code, tail, held) == (0, "tail-end", False)
    assert "".join(chunks) == "a" * 20 + "tail-end"


def test_a_background_process_holding_stderr_does_not_hold_the_report(monkeypatch) -> None:
    """The command exited; a process it left running still holds the stderr pipe."""
    monkeypatch.setattr(cli_sandbox_explain, "STDERR_DRAIN_SECS", wait.DEFAULT_INTERVAL_SECS)
    reader, writer = os.pipe()
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stderr=writer,
    )
    os.close(writer)
    command = SimpleNamespace(stderr=os.fdopen(reader, "rb"), wait=lambda: 0)
    sink = SimpleNamespace(write=lambda text: len(text), flush=lambda: None)
    result: list[tuple[int, str, bool]] = []
    worker = threading.Thread(
        target=lambda: result.append(cli_sandbox_explain._pump_stderr(command, sink))
    )
    worker.start()
    try:
        worker.join(wait.FALLBACK_TIMEOUT_SECS)
        assert not worker.is_alive()
        assert holder.poll() is None
    finally:
        try:
            holder.communicate(b"", timeout=wait.FALLBACK_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=wait.FALLBACK_TIMEOUT_SECS)
            pytest.fail("the stdin-reader child did not exit after its stdin closed")
        finally:
            command.stderr.close()
    assert result == [(0, "", True)]


def test_the_report_says_when_a_background_process_still_holds_stderr(
    seams, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli_sandbox_explain, "_pump_stderr", lambda proc: (0, "", True))
    rc = cli_sandbox_explain.explain(["daemon-start"], popen=_runner(seams, 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "ran: yes, exit code 0 (a process it started still holds its stderr; "
        "that output is not shown)"
    ) in out
