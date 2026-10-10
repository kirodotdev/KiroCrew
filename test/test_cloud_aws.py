"""Unit tests for the cloud AWS chokepoint (cloud/aws.py)."""

from __future__ import annotations

import functools
import json
import os
import signal
import sys
import threading

import pytest

from kiro_crew.cloud import aws


class TestBuildArgv:
    @pytest.fixture(autouse=True)
    def _bare_resolver(self, monkeypatch):
        """Pin the shared resolver to the bare name so argv-shape assertions
        stay deterministic across hosts (with/without an installed CLI)."""
        monkeypatch.setattr(aws, "resolve_aws_bin", lambda: "aws")

    def test_bare(self):
        assert aws._build_argv(["sts", "get-caller-identity"], "", "") == [
            "aws",
            "sts",
            "get-caller-identity",
        ]

    def test_profile_and_region(self):
        argv = aws._build_argv(["ec2", "describe-vpcs"], "dev", "us-east-1")
        assert argv == ["aws", "ec2", "describe-vpcs", "--profile", "dev", "--region", "us-east-1"]

    def test_profile_only(self):
        argv = aws._build_argv(["s3", "ls"], "prod", "")
        assert argv == ["aws", "s3", "ls", "--profile", "prod"]

    def test_argv_head_resolved_absolutely_under_minimal_path(self, monkeypatch, tmp_path):
        """A GUI-launched gateway's minimal PATH must not yield a bare 'aws'
        head that fails execvp: the builder routes through the deploy engine's
        well-known-dirs resolver."""
        import os as _os

        if _os.name == "nt":
            pytest.skip("fallback install dirs are POSIX literals; dead on Windows by design")
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)
        # Undo this class's bare-name pin: this test exercises the real resolver.
        monkeypatch.setattr(aws, "resolve_aws_bin", engine.resolve_aws_bin)

        argv = aws._build_argv(["sts", "get-caller-identity"], "dev", "")
        assert argv[0] == str(fake_aws)
        assert argv[1:] == ["sts", "get-caller-identity", "--profile", "dev"]


class TestRunAws:
    def test_success_returns_process_output(self, monkeypatch):
        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "out", "err"

        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: FakeProc())

        assert aws.run_aws(["sts", "get-caller-identity"]) == (0, "out", "err")

    def test_spawn_gets_widened_path_for_credential_process(self, monkeypatch, tmp_path):
        """A GUI-launched gateway's minimal PATH must not hide ``credential_process``.

        ``run_aws`` hands the child :func:`aws_spawn_env` for the resolved head, so
        the CLI's own by-name lookups search the AWS bin dirs too.
        """
        from kiro_crew.deploy import engine

        bin_dir = tmp_path / "aws-bin"
        bin_dir.mkdir()
        head = str(bin_dir / "aws")
        monkeypatch.setattr(aws, "resolve_aws_bin", lambda: head)
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(bin_dir),))
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "cgroup_scope_argv", lambda argv: argv)
        seen: dict = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "", ""

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return FakeProc()

        monkeypatch.setattr(aws, "popen_limited", fake_popen)

        assert aws.run_aws(["sts", "get-caller-identity"]) == (0, "", "")
        assert seen["argv"][0] == head
        assert seen["env"] is not None
        assert seen["env"]["PATH"].split(os.pathsep) == ["/usr/bin", "/bin", str(bin_dir)]

    def test_aws_cli_missing_returns_127_not_traceback(self, monkeypatch):
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))

        def raise_fnf(*a, **k):
            raise FileNotFoundError("aws not found")

        monkeypatch.setattr(aws, "popen_limited", raise_fnf)
        rc, out, err = aws.run_aws(["sts", "get-caller-identity"])
        assert rc == 127
        assert "aws CLI not found" in err
        assert out == ""

    def test_env_credentials_hint_when_env_auth(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        hint = aws.env_credentials_hint()
        assert "environment variables" in hint
        assert "profile" in hint

    def test_env_credentials_hint_empty_without_env_auth(self, monkeypatch):
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        assert aws.env_credentials_hint() == ""

    def test_keyboard_interrupt_terminates_child(self, monkeypatch):
        class FakeProc:
            terminated = False
            killed = False

            def communicate(self, timeout):
                raise KeyboardInterrupt

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 0

            def kill(self):
                self.killed = True

        proc = FakeProc()
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: proc)

        with pytest.raises(KeyboardInterrupt):
            aws.run_aws(["cloudformation", "deploy"])
        assert proc.terminated is True
        assert proc.killed is False


class TestAccessDeniedParsing:
    def test_is_access_denied_true(self):
        assert aws.is_access_denied("An error occurred (AccessDenied) when calling ...")
        assert aws.is_access_denied("User: arn:... is not authorized to perform: ec2:RunInstances")
        assert aws.is_access_denied("UnauthorizedOperation")

    def test_is_access_denied_false(self):
        assert not aws.is_access_denied("Parameter validation failed: invalid region")
        assert not aws.is_access_denied("")

    def test_map_missing_action_extracts_token(self):
        err = (
            "An error occurred (AccessDenied) when calling the RunInstances operation: "
            "User: arn:aws:iam::123:user/x is not authorized to perform: ec2:RunInstances "
            "on resource: arn:aws:ec2:..."
        )
        assert aws.map_missing_action(err) == "ec2:RunInstances"

    def test_map_missing_action_strips_trailing_punct(self):
        err = "is not authorized to perform: iam:PassRole."
        assert aws.map_missing_action(err) == "iam:PassRole"

    def test_map_missing_action_none_when_not_denied(self):
        assert aws.map_missing_action("some client error") is None

    def test_map_missing_action_none_when_no_marker(self):
        # Denied, but no "to perform:" clause (e.g. UnauthorizedOperation alone).
        assert aws.map_missing_action("UnauthorizedOperation") is None


class TestChecked:
    def test_success_returns_stdout(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "OK", ""))
        assert (
            aws.checked(["sts", "get-caller-identity"], "dev", action="sts:GetCallerIdentity")
            == "OK"
        )

    def test_failure_raises_with_missing_action(self, monkeypatch):
        err = "is not authorized to perform: ec2:RunInstances on resource ..."
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", err))
        with pytest.raises(aws.AWSError) as ei:
            aws.checked(["ec2", "run-instances"], "dev", action="ec2:RunInstances")
        assert ei.value.missing_action == "ec2:RunInstances"
        assert "grant `ec2:RunInstances`" in str(ei.value)
        assert ei.value.returncode == 255

    def test_failure_non_auth_has_no_missing_action(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (2, "", "Parameter validation failed"))
        with pytest.raises(aws.AWSError) as ei:
            aws.checked(["ec2", "run-instances"], "dev", action="ec2:RunInstances")
        assert ei.value.missing_action is None
        assert "Parameter validation failed" in str(ei.value)


class TestCheckedJson:
    def test_parses_json(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, json.dumps({"Account": "123"}), ""))
        out = aws.checked_json(
            ["sts", "get-caller-identity"], "dev", action="sts:GetCallerIdentity"
        )
        assert out == {"Account": "123"}

    def test_appends_output_json(self, monkeypatch):
        captured: dict = {}

        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            captured["args"] = args
            return (0, "{}", "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        aws.checked_json(["ec2", "describe-vpcs"], "dev", action="ec2:DescribeVpcs")
        assert "--output" in captured["args"]
        assert "json" in captured["args"]

    def test_bad_json_raises(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "not json", ""))
        with pytest.raises(aws.AWSError):
            aws.checked_json(["ec2", "describe-vpcs"], "dev", action="ec2:DescribeVpcs")


class TestChokepointHumanActionGuard:
    """run_aws must refuse non-read-only calls from an agent session
    (KIROCREW_SESSION_KEY set), covering mutations AND token-minting SSM calls
    even when run_aws is imported directly, bypassing the shell denylist."""

    def test_readonly_calls_allowed_under_agent_session(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-1")
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))

        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "out", ""

        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: FakeProc())
        for readonly in (
            ["sts", "get-caller-identity"],
            ["ec2", "describe-instances"],
            ["cloudformation", "list-stacks"],
            ["ssm", "describe-instance-information"],
            ["resourcegroupstaggingapi", "get-resources"],
        ):
            rc, _o, _e = aws.run_aws(readonly)
            assert rc == 0, f"read-only {readonly} should be allowed"

    def test_mutations_and_token_mint_refused_under_agent_session(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-1")
        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: pytest.fail("must not spawn aws"))
        for sensitive in (
            ["cloudformation", "delete-stack", "--stack-name", "kirocrew-x"],
            ["cloudformation", "deploy"],
            ["ec2", "terminate-instances", "--instance-ids", "i-1"],
            ["ec2", "stop-instances", "--instance-ids", "i-1"],
            ["ssm", "send-command", "--instance-ids", "i-1"],  # token mint path
            ["ssm", "start-session", "--target", "i-1"],
            ["s3", "rm", "s3://kirocrew-src-x/y"],
        ):
            with pytest.raises(aws.CloudActionDenied):
                aws.run_aws(sensitive)

    def test_secret_reads_denied_under_agent_session(self, monkeypatch):
        # An EXACT allowlist (not a get-*/list-* prefix) must deny secret-bearing
        # reads even though they start with get-/list-.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-1")
        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: pytest.fail("must not spawn aws"))
        for secret_read in (
            ["secretsmanager", "get-secret-value", "--secret-id", "x"],
            ["ssm", "get-parameter", "--name", "x", "--with-decryption"],
            ["ssm", "get-command-invocation", "--command-id", "c"],  # returns token output
            ["ssm", "list-command-invocations"],
            ["iam", "list-access-keys"],
        ):
            with pytest.raises(aws.CloudActionDenied):
                aws.run_aws(secret_read)

    def test_all_allowed_without_session_key(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))

        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "", ""

        monkeypatch.setattr(aws, "popen_limited", lambda *a, **k: FakeProc())
        # A human terminal (no session key) can run a mutation.
        rc, _o, _e = aws.run_aws(["cloudformation", "delete-stack", "--stack-name", "x"])
        assert rc == 0


# --------------------------------------------------------------------------- #
# A timed-out ``aws`` call must reap the CLI's WHOLE process group, so a child
# the CLI spawned (its ``credential_process`` helper) that inherited the piped
# stdout/stderr cannot keep the post-timeout drain blocked. The reproducer runs
# the real ``run_aws`` and the real ``popen_limited`` on a real child; only the
# argv builder, the sandbox wrappers, the chokepoint guard and the env builder
# are stubbed, and none of those is part of the wait.
# --------------------------------------------------------------------------- #
_HELPER_LINGER_SECS = 25
_JOIN_BOUND_SECS = 8


def _fake_cli_argv(pid_file, *, with_helper, helper_leaves_group=False):
    """Argv for a python stand-in CLI that outlives a 1s call timeout.

    With ``with_helper`` it forks a child that inherits this process's piped
    stdout/stderr and lingers -- the shape of ``credential_process`` outliving a
    killed CLI -- and records that child's pid to ``pid_file`` so the test can
    reap it by its recorded pid, never by name. With ``helper_leaves_group`` the
    child calls ``setsid`` first, so no group signal reaches it.
    """
    leave_group = "    os.setsid()\n" if helper_leaves_group else ""
    with_helper_src = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "{leave_group}"
        "    time.sleep({linger})\n"
        "    os._exit(0)\n"
        "open({pid_file!r}, 'w').write(str(pid))\n"
        "time.sleep({linger})\n"
    ).format(leave_group=leave_group, linger=_HELPER_LINGER_SECS, pid_file=str(pid_file))
    no_helper_src = "import time\ntime.sleep({linger})\n".format(linger=_HELPER_LINGER_SECS)
    return [sys.executable, "-c", with_helper_src if with_helper else no_helper_src]


def _reap_recorded_pid(pid_file):
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group reproducer")
class TestRunAwsTimeoutReapsHelpers:
    @pytest.fixture(autouse=True)
    def _children_run_in_tmp_path(self, tmp_path, _floor_monkeypatch):
        """Every real child these tests start runs in ``tmp_path``, never in the checkout."""
        real = aws.popen_limited
        _floor_monkeypatch.setattr(aws, "popen_limited", functools.partial(real, cwd=tmp_path))

    def _run_in_thread(self, monkeypatch, args, *, timeout):
        monkeypatch.setattr(aws, "assert_chokepoint_allowed", lambda a: None)
        monkeypatch.setattr(aws, "_build_argv", lambda a, p, r: list(a))
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(aws, "aws_spawn_env", lambda head: dict(os.environ))
        captured: dict = {}

        def _call():
            captured["value"] = aws.run_aws(args, timeout=timeout)

        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(_JOIN_BOUND_SECS)
        return thread, captured

    def test_timed_out_call_returns_while_helper_holds_pipes(self, tmp_path, monkeypatch):
        # A drain bound longer than the helper's life: only the group signal
        # can free the call inside the join bound.
        monkeypatch.setattr(aws, "_POST_KILL_DRAIN_SECS", _HELPER_LINGER_SECS * 2, raising=False)
        pid_file = tmp_path / "helper.pid"
        args = _fake_cli_argv(pid_file, with_helper=True)
        thread, captured = self._run_in_thread(monkeypatch, args, timeout=1)
        try:
            assert not thread.is_alive(), (
                "run_aws stayed blocked past its 1s timeout: the helper that "
                "inherited the pipes was never reaped"
            )
            rc, _out, err = captured["value"]
            assert rc == 124
            assert "timed out after 1s" in err
        finally:
            _reap_recorded_pid(pid_file)
            thread.join(_HELPER_LINGER_SECS + 5)

    def test_timed_out_call_returns_when_helper_leaves_the_group(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aws, "_POST_KILL_DRAIN_SECS", 1, raising=False)
        pid_file = tmp_path / "helper.pid"
        args = _fake_cli_argv(pid_file, with_helper=True, helper_leaves_group=True)
        thread, captured = self._run_in_thread(monkeypatch, args, timeout=1)
        try:
            assert not thread.is_alive(), (
                "run_aws stayed blocked past its 1s timeout and its drain bound: "
                "a helper outside the group held the pipes"
            )
            rc, _out, err = captured["value"]
            assert rc == 124
            assert "timed out after 1s" in err
        finally:
            _reap_recorded_pid(pid_file)
            thread.join(_HELPER_LINGER_SECS + 5)

    def test_timed_out_call_with_no_helper_returns_124(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "helper.pid"
        args = _fake_cli_argv(pid_file, with_helper=False)
        thread, captured = self._run_in_thread(monkeypatch, args, timeout=1)
        try:
            assert not thread.is_alive()
            rc, _out, err = captured["value"]
            assert rc == 124
            assert "timed out after 1s" in err
        finally:
            thread.join(_HELPER_LINGER_SECS + 5)

    def test_call_that_finishes_in_time_is_unchanged(self, monkeypatch):
        args = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('ok'); sys.stderr.write('e')",
        ]
        thread, captured = self._run_in_thread(monkeypatch, args, timeout=30)
        thread.join(_JOIN_BOUND_SECS)
        assert not thread.is_alive()
        assert captured["value"] == (0, "ok", "e")

    def test_the_cli_runs_in_tmp_path_not_in_the_checkout(self, tmp_path, monkeypatch):
        args = [sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"]
        thread, captured = self._run_in_thread(monkeypatch, args, timeout=30)
        assert not thread.is_alive()
        rc, out, _err = captured["value"]
        assert rc == 0
        ran_in = os.path.realpath(out)
        assert ran_in == os.path.realpath(tmp_path), f"the CLI ran in {out}, not in {tmp_path}"

    def test_a_reaped_leaders_group_number_is_never_signalled(self, tmp_path, monkeypatch):
        # Another holder of the Popen (the deploy wizard's interrupt cleanup, through
        # proc_sink) can reap the CLI first. Its pid, which is the group id, is then
        # free and may name an unrelated group, so the timeout path must not signal it.
        import subprocess

        signalled: list[tuple[int, int]] = []

        def _record(pgid: int, sig: int) -> bool:
            signalled.append((pgid, sig))
            return True

        monkeypatch.setattr(aws, "kill_process_group", _record)
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True, cwd=tmp_path
        )
        pgid = proc.pid  # start_new_session: the leader's pid is the group id
        assert proc.wait(timeout=_JOIN_BOUND_SECS) == 0  # reaped by another holder

        assert aws._reap_timed_out_call(proc, pgid) == ""
        assert signalled == [], f"a reaped leader's group number was signalled: {signalled}"
