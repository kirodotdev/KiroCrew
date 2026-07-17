"""Unit tests for the cloud AWS chokepoint (cloud/aws.py)."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud import aws


class TestBuildArgv:
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


class TestRunAws:
    def test_success_returns_process_output(self, monkeypatch):
        class FakeProc:
            returncode = 0

            def communicate(self, timeout):
                return "out", "err"

        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))
        monkeypatch.setattr(aws.subprocess, "Popen", lambda *a, **k: FakeProc())

        assert aws.run_aws(["sts", "get-caller-identity"]) == (0, "out", "err")

    def test_aws_cli_missing_returns_127_not_traceback(self, monkeypatch):
        monkeypatch.setattr(aws, "wrap_argv", lambda argv, mode: (argv, ""))

        def raise_fnf(*a, **k):
            raise FileNotFoundError("aws not found")

        monkeypatch.setattr(aws.subprocess, "Popen", raise_fnf)
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
        monkeypatch.setattr(aws.subprocess, "Popen", lambda *a, **k: proc)

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

        monkeypatch.setattr(aws.subprocess, "Popen", lambda *a, **k: FakeProc())
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
        monkeypatch.setattr(
            aws.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn aws")
        )
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
        monkeypatch.setattr(
            aws.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn aws")
        )
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

        monkeypatch.setattr(aws.subprocess, "Popen", lambda *a, **k: FakeProc())
        # A human terminal (no session key) can run a mutation.
        rc, _o, _e = aws.run_aws(["cloudformation", "delete-stack", "--stack-name", "x"])
        assert rc == 0
