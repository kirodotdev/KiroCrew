"""Unit tests for the cloud AWS chokepoint (cloud/aws.py)."""

from __future__ import annotations

import functools
import json
import os
import select
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
        monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(bin_dir), str(bin_dir)),))
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

    def test_spawn_decodes_child_output_as_utf8(self, monkeypatch):
        """The child inherits ``PYTHONUTF8=1`` and writes UTF-8 on every platform.

        A text-mode spawn without ``encoding=`` decodes with the locale's code
        page, which on Windows is the ANSI code page, so a non-ASCII stack name,
        path or localized error would come back as mojibake.
        """
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "cgroup_scope_argv", lambda argv: argv)
        seen: dict = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "caf\u00e9", ""

        def fake_popen(argv, **kwargs):
            seen.update(kwargs)
            return FakeProc()

        monkeypatch.setattr(aws, "popen_limited", fake_popen)

        assert aws.run_aws(["sts", "get-caller-identity"]) == (0, "caf\u00e9", "")
        assert seen.get("text") is True
        assert seen.get("encoding") == "utf-8"
        assert seen.get("errors") == "replace"

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
# A timed-out ``aws`` call must return promptly even while a process the CLI
# started still holds the piped stdout/stderr: ``proc.kill()`` stops only the
# CLI, so the drain after it is bounded. The reproducer runs the real
# ``run_aws`` and the real ``popen_limited`` on real children; only the argv
# builder, the sandbox wrappers, the chokepoint guard and the env builder are
# stubbed, and none of those is part of the wait. The stand-in CLI and its
# helper block reading a pipe the test holds, so they live until the test
# closes it: no sleep decides how long anything runs.
# --------------------------------------------------------------------------- #
_JOIN_BOUND_SECS = 8


def _stand_in_cli(hold_fd, ready_fd=None):
    """Argv for a python stand-in CLI that blocks until ``hold_fd`` reaches end of file.

    With ``ready_fd`` it first forks a helper that inherits the piped stdout and
    stderr, the shape of a process the CLI started that outlives a killed CLI. The
    helper writes one byte to ``ready_fd`` and then blocks on ``hold_fd`` too, holding
    the pipes until the test closes the other end of ``hold_fd``.
    """
    src = "import os\n"
    if ready_fd is not None:
        src += (
            "if os.fork() == 0:\n"
            "    os.write({ready}, b'r')\n"
            "    os.read({hold}, 1)\n"
            "    os._exit(0)\n"
        )
    src += "os.read({hold}, 1)\n"
    return [sys.executable, "-c", src.format(hold=hold_fd, ready=ready_fd)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX reproducer: the stand-in forks")
class TestATimedOutCallReturnsWhileAHelperHoldsThePipes:
    @pytest.fixture(autouse=True)
    def _children_run_in_tmp_path(self, tmp_path, _floor_monkeypatch):
        """Every real child these tests start runs in ``tmp_path``, never in the checkout."""
        real = aws.popen_limited
        _floor_monkeypatch.setattr(aws, "popen_limited", functools.partial(real, cwd=tmp_path))

    def _run_in_thread(self, monkeypatch, args, *, timeout, pass_fds=()):
        monkeypatch.setattr(aws, "assert_chokepoint_allowed", lambda a: None)
        monkeypatch.setattr(aws, "_build_argv", lambda a, p, r: list(a))
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(aws, "aws_spawn_env", lambda head: dict(os.environ))
        if pass_fds:
            spawn = aws.popen_limited
            monkeypatch.setattr(aws, "popen_limited", functools.partial(spawn, pass_fds=pass_fds))
        captured: dict = {}
        procs: list = []

        def _call():
            captured["value"] = aws.run_aws(args, timeout=timeout, proc_sink=procs.append)

        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(_JOIN_BOUND_SECS)
        return thread, captured, procs

    def test_a_timed_out_call_returns_while_a_helper_holds_the_pipes(self, monkeypatch):
        # The call times out at 2 s and drains for at most 1 s, well inside the join
        # bound, while the helper holds the pipes until `finally` closes `hold_w`.
        monkeypatch.setattr(aws, "_POST_KILL_DRAIN_SECS", 1, raising=False)
        hold_r, hold_w = os.pipe()
        ready_r, ready_w = os.pipe()
        thread = None
        try:
            args = _stand_in_cli(hold_r, ready_w)
            thread, captured, procs = self._run_in_thread(
                monkeypatch, args, timeout=2, pass_fds=(hold_r, ready_w)
            )
            up, _, _ = select.select([ready_r], [], [], _JOIN_BOUND_SECS)
            assert up and os.read(ready_r, 1) == b"r", "the helper never started"
            assert not thread.is_alive(), (
                "run_aws stayed blocked past its 2s timeout and its drain bound: "
                "a helper still held the pipes"
            )
            rc, out, err = captured["value"]
            assert (rc, out) == (124, "")
            assert "timed out after 2s" in err
            assert procs[0].stdout.closed and procs[0].stderr.closed, "our read ends stayed open"
        finally:
            os.close(hold_w)  # end of file: the helper exits, and on main the call returns
            if thread is not None:
                thread.join(_JOIN_BOUND_SECS)
            for fd in (hold_r, ready_r, ready_w):
                os.close(fd)

    def test_timed_out_call_with_no_helper_returns_124(self, monkeypatch):
        hold_r, hold_w = os.pipe()
        thread = None
        try:
            args = _stand_in_cli(hold_r)
            thread, captured, _procs = self._run_in_thread(
                monkeypatch, args, timeout=1, pass_fds=(hold_r,)
            )
            assert not thread.is_alive()
            rc, _out, err = captured["value"]
            assert rc == 124
            assert "timed out after 1s" in err
        finally:
            os.close(hold_w)
            if thread is not None:
                thread.join(_JOIN_BOUND_SECS)
            os.close(hold_r)

    def test_call_that_finishes_in_time_is_unchanged(self, monkeypatch):
        args = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('ok'); sys.stderr.write('e')",
        ]
        thread, captured, _procs = self._run_in_thread(monkeypatch, args, timeout=30)
        thread.join(_JOIN_BOUND_SECS)
        assert not thread.is_alive()
        assert captured["value"] == (0, "ok", "e")

    def test_utf8_output_decodes_and_a_bad_byte_degrades(self, monkeypatch):
        # A real child writes UTF-8 plus one byte that is not valid UTF-8. The
        # call returns the text with U+FFFD in that one spot instead of raising
        # UnicodeDecodeError out of communicate().
        args = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff');"
            " sys.stderr.buffer.write(b'\\xe2\\x9c\\x93')",
        ]
        thread, captured, _procs = self._run_in_thread(monkeypatch, args, timeout=30)
        assert not thread.is_alive()
        assert captured["value"] == (0, "caf\u00e9 \ufffd", "\u2713")

    def test_the_cli_runs_in_tmp_path_not_in_the_checkout(self, tmp_path, monkeypatch):
        args = [sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"]
        thread, captured, _procs = self._run_in_thread(monkeypatch, args, timeout=30)
        assert not thread.is_alive()
        rc, out, _err = captured["value"]
        assert rc == 0
        ran_in = os.path.realpath(out)
        assert ran_in == os.path.realpath(tmp_path), f"the CLI ran in {out}, not in {tmp_path}"
