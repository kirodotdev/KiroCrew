"""The reviewer's builtin reads are gated BEFORE execution by a kiro-cli
``preToolUse`` hook.

kiro-cli approves its builtin ``fs_read`` / ``grep`` natively: no permission
request reaches Crew, so ``advisor_permission_gate`` never saw those reads.
kiro-cli does run the agent spec's ``preToolUse`` hooks for them, hands the
hook the tool input on stdin, and blocks the call when the hook exits 2 (an
exit 1 or a missing command lets the read PROCEED -- verified live on
kiro-cli 2.21.4). ``kiro_crew.advisor.read_gate`` is that hook: it runs the
same gate and exits 2 on a denial or on ANY failure. The managed spec carries
the two hook entries, pointing at the interpreter that runs the gateway, and
the installer refuses to spawn a reviewer whose hook command could not run.
"""

from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest

from kiro_crew.advisor import read_gate


def _run(payload: object, gate) -> tuple[int, str]:
    stdin = StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    stderr = StringIO()
    rc = read_gate.run(stdin=stdin, stderr=stderr, gate=gate)
    return rc, stderr.getvalue()


def _hook_payload(tool_name: str, tool_input: dict) -> dict:
    # The shape kiro-cli 2.21.4 hands a preToolUse hook for a builtin read.
    return {
        "hook_event_name": "preToolUse",
        "cwd": "/work/project",
        "session_id": "s1",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


class TestExitCodes:
    def test_an_approved_read_exits_zero(self):
        rc, err = _run(
            _hook_payload("read", {"operations": [{"path": "src/a.py"}]}), lambda ev, cwd: ""
        )
        assert (rc, err) == (0, "")

    def test_a_denied_read_exits_two_with_the_reason_on_stderr(self):
        rc, err = _run(
            _hook_payload("read", {"operations": [{"path": "deploy/secrets/x"}]}),
            lambda ev, cwd: "Blocked: denied by governance policy",
        )
        assert rc == 2
        assert "denied by governance policy" in err

    def test_malformed_stdin_exits_two(self):
        rc, _ = _run("not json", lambda ev, cwd: "")
        assert rc == 2

    def test_a_gate_that_raises_exits_two(self):
        def boom(ev, cwd):
            raise RuntimeError("hook layer unavailable")

        rc, err = _run(_hook_payload("read", {"operations": [{"path": "a"}]}), boom)
        assert rc == 2
        assert "hook layer unavailable" in err


class TestEventShape:
    def test_kiro_cli_read_kind_is_judged_as_fs_read_with_the_raw_input(self):
        seen = {}

        def gate(ev, cwd):
            seen["tool_name"] = ev.tool_name
            seen["raw"] = ev.raw_tool_params
            seen["cwd"] = cwd
            return ""

        payload = _hook_payload("read", {"operations": [{"mode": "Line", "path": "src/a.py"}]})
        assert _run(payload, gate)[0] == 0
        assert seen == {
            "tool_name": "fs_read",
            "raw": {"operations": [{"mode": "Line", "path": "src/a.py"}]},
            "cwd": "/work/project",
        }

    def test_grep_keeps_its_name(self):
        seen = {}

        def gate(ev, cwd):
            seen["tool_name"] = ev.tool_name
            return ""

        assert _run(_hook_payload("grep", {"pattern": "x", "path": "."}), gate)[0] == 0
        assert seen["tool_name"] == "grep"

    def test_an_unexpected_tool_name_is_passed_through_for_the_ceiling_to_deny(self):
        # The gate's read-only ceiling denies anything but fs_read / grep; the
        # adapter must not launder an unknown name into an allowed one.
        seen = {}

        def gate(ev, cwd):
            seen["tool_name"] = ev.tool_name
            return "Blocked: outside the read-only ceiling"

        assert _run(_hook_payload("write", {"path": "a"}), gate)[0] == 2
        assert seen["tool_name"] == "write"


class TestTheRealGateThroughTheHook:
    def test_a_sensitive_path_is_blocked_before_execution(self, monkeypatch):
        from kiro_crew.advisor import composition

        monkeypatch.setattr(composition, "_hook_manager", lambda: pytest.fail("floor denies first"))
        payload = _hook_payload(
            "read", {"operations": [{"path": str(Path.home() / ".ssh" / "id_rsa")}]}
        )
        rc, err = _run(payload, gate=None)
        assert rc == 2
        assert "sensitive" in err


class TestTheManagedSpecCarriesTheHook:
    def test_installed_spec_gates_fs_read_and_grep_with_the_gateway_interpreter(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.advisor import composition

        monkeypatch.setattr(read_gate, "self_test", lambda cwd: "")
        monkeypatch.setattr(composition, "credential_mask_applies", lambda *a, **k: True)
        monkeypatch.setattr(composition, "reviewer_process_cwd", lambda: tmp_path / "cwd")
        target = composition.ensure_advisor_agent_installed(tmp_path / "agents")
        spec = json.loads(target.read_text(encoding="utf-8"))
        hooks = spec["hooks"]["preToolUse"]
        assert sorted(h["matcher"] for h in hooks) == ["fs_read", "grep"]
        for h in hooks:
            assert h["command"] == read_gate.hook_command()
        assert read_gate.hook_command().startswith(read_gate.shell_quote(sys.executable))
        assert "kiro_crew.advisor.read_gate" in read_gate.hook_command()

    def test_install_refuses_when_the_hook_interpreter_cannot_run(self, tmp_path, monkeypatch):
        """A missing hook command makes kiro-cli run the read UNGATED, so the
        installer must fail closed rather than write a spec whose hook cannot
        execute."""
        from kiro_crew.advisor import composition

        monkeypatch.setattr(composition, "credential_mask_applies", lambda *a, **k: True)
        monkeypatch.setattr(composition, "reviewer_process_cwd", lambda: tmp_path / "cwd")
        monkeypatch.setattr(
            read_gate, "self_test", lambda cwd: pytest.fail("interpreter check comes first")
        )
        monkeypatch.setattr(read_gate, "_interpreter", lambda: str(tmp_path / "no-such-python"))
        with pytest.raises(composition.AdvisorSpecError, match="hook"):
            composition.ensure_advisor_agent_installed(tmp_path / "agents")

    def test_the_packaged_spec_itself_declares_no_hooks(self):
        """The hook command is host-specific (interpreter path), so it is added
        at install time; the packaged spec stays free of absolute paths."""
        packaged = Path(read_gate.__file__).parent / "agents" / "kirocrew-advisor.json"
        assert "hooks" not in json.loads(packaged.read_text(encoding="utf-8"))


class TestTheHookIsSelfTestedAtInstall:
    """The interpreter check proves the command can START; it does not prove the
    module it runs still judges. An editable install runs the hook from the
    source tree, so the installer executes the complete hook command with a
    known-denied and a known-allowed payload and refuses to spawn unless both
    verdicts are right."""

    def _runs(self, verdicts: dict[str, int], calls: list):
        import subprocess

        def fake_run(argv, *, input, **kw):
            calls.append((argv, json.loads(input)))
            path = json.dumps(json.loads(input)["tool_input"])
            rc = verdicts["deny"] if ".ssh" in path else verdicts["allow"]
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="x")

        return fake_run

    def test_correct_verdicts_pass_and_run_the_real_command_line(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(read_gate.subprocess, "run", self._runs({"deny": 2, "allow": 0}, calls))
        assert read_gate.self_test(cwd=tmp_path) == ""
        assert len(calls) == 2
        assert all(argv == read_gate.hook_argv() for argv, _ in calls)
        assert {c[1]["tool_name"] for c in calls} == {"read"}

    @pytest.mark.parametrize(
        "verdicts", [{"deny": 0, "allow": 0}, {"deny": 2, "allow": 2}, {"deny": 1, "allow": 0}]
    )
    def test_a_wrong_verdict_is_reported(self, tmp_path, monkeypatch, verdicts):
        monkeypatch.setattr(read_gate.subprocess, "run", self._runs(verdicts, []))
        assert read_gate.self_test(cwd=tmp_path)

    def test_install_refuses_when_the_self_test_fails(self, tmp_path, monkeypatch):
        from kiro_crew.advisor import composition

        monkeypatch.setattr(composition, "credential_mask_applies", lambda *a, **k: True)
        monkeypatch.setattr(composition, "reviewer_process_cwd", lambda: tmp_path / "cwd")
        monkeypatch.setattr(read_gate, "self_test", lambda cwd: "deny probe exited 0")
        with pytest.raises(composition.AdvisorSpecError, match="self-test"):
            composition.ensure_advisor_agent_installed(tmp_path / "agents")


@pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX command line")
def test_shell_quote_makes_a_path_with_spaces_one_argument():
    assert read_gate.shell_quote("/opt/my venv/bin/python") == "'/opt/my venv/bin/python'"
