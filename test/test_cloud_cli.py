"""Tests for the cloud CLI dispatch layer (cli_cloud.py) — thin wrappers only."""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import cli_cloud
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2
from kiro_crew.cloud.config import CloudConfig


def _args(**kw):
    ns = argparse.Namespace()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestDispatch:
    def test_unknown_action(self):
        assert cli_cloud.handle_cloud(_args(cloud_action="nope")) == 1

    def test_no_action_prints_help(self, capsys):
        assert cli_cloud.handle_cloud(_args(cloud_action=None)) == 0
        assert "kirocrew cloud" in capsys.readouterr().out

    def test_iam_policy(self, capsys):
        assert cli_cloud.handle_cloud(_args(cloud_action="iam-policy")) == 0
        out = capsys.readouterr().out
        assert "cloudformation:CreateStack" in out

    def test_launch_passes_new_flag(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kwargs: captured.update(kwargs) or 0
        )

        rc = cli_cloud._cloud_launch(
            _args(profile="", region="", size="balanced", yes=True, new=True)
        )
        assert rc == 0
        assert captured["force_new"] is True

    def test_launch_passes_subnet_flag(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "ap-southeast-1"))
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kwargs: captured.update(kwargs) or 0
        )

        rc = cli_cloud._cloud_launch(
            _args(profile="", region="", subnet="subnet-0123456789abcdef0", yes=True)
        )
        assert rc == 0
        assert captured["subnet_id"] == "subnet-0123456789abcdef0"

    def test_dispatch_keyboard_interrupt_returns_130(self, monkeypatch, capsys):
        def raise_interrupt(_args):
            raise KeyboardInterrupt

        monkeypatch.setitem(cli_cloud._DISPATCH, "list", raise_interrupt)

        assert cli_cloud.handle_cloud(_args(cloud_action="list")) == 130
        assert "Interrupted" in capsys.readouterr().out

    def test_setup_cloud_step_skips_on_eof(self, monkeypatch, capsys):
        # Piped `kirocrew setup` (no stdin) must skip the cloud step, not crash.
        from kiro_crew import cli_setup

        def raise_eof(_prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        cli_setup._maybe_setup_cloud()  # must not raise
        assert "Skipped" in capsys.readouterr().out

    def test_dispatch_validation_error_fails_cleanly(self, monkeypatch, capsys):
        # A malformed user value (e.g. --tag with a bad charset) must render
        # the same clean one-liner as AWSError — never a raw traceback.
        from kiro_crew.validation import ValidationError

        def raise_validation(_args):
            raise ValidationError("tag", "invalid characters")

        monkeypatch.setitem(cli_cloud._DISPATCH, "status", raise_validation)

        assert cli_cloud.handle_cloud(_args(cloud_action="status")) == 1
        out = capsys.readouterr().out
        assert "tag" in out
        assert "Traceback" not in out

    def test_dispatch_aws_error_fails_cleanly(self, monkeypatch, capsys):
        # AWS failures outside an action's own try/except (e.g. the
        # ec2.describe() in status) must also render the clean one-liner.
        from kiro_crew.cloud.aws import AWSError

        def raise_aws(_args):
            raise AWSError("token has expired", action="sts:GetCallerIdentity")

        monkeypatch.setitem(cli_cloud._DISPATCH, "status", raise_aws)

        assert cli_cloud.handle_cloud(_args(cloud_action="status")) == 1
        out = capsys.readouterr().out
        assert "expired" in out
        assert "Traceback" not in out

    def test_connect_rejects_out_of_range_local_port(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_ensure_session_manager_plugin", lambda: True)
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda a: "kc-1")
        monkeypatch.setattr(
            cli_cloud.ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )

        rc = cli_cloud.handle_cloud(_args(cloud_action="connect", local_port=99999))
        assert rc == 1
        assert "1-65535" in capsys.readouterr().out


class TestResolve:
    def test_resolve_prefers_args(self, monkeypatch):
        monkeypatch.setattr(
            CloudConfig,
            "load",
            classmethod(lambda cls, *a: CloudConfig(profile="saved", region="us-west-2")),
        )
        p, r = cli_cloud._resolve(_args(profile="cliprof", region="eu-west-1"))
        assert p == "cliprof"
        assert r == "eu-west-1"

    def test_resolve_falls_back_to_config(self, monkeypatch):
        monkeypatch.setattr(
            CloudConfig,
            "load",
            classmethod(lambda cls, *a: CloudConfig(profile="saved", region="us-west-2")),
        )
        p, r = cli_cloud._resolve(_args(profile="", region=""))
        assert p == "saved"
        assert r == "us-west-2"

    def test_resolve_tag_uses_last(self, monkeypatch):
        monkeypatch.setattr(
            CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig(last_tag="kc-last"))
        )
        assert cli_cloud._resolve_tag(_args(tag="")) == "kc-last"

    def test_resolve_tag_explicit(self):
        assert cli_cloud._resolve_tag(_args(tag="kc-x")) == "kc-x"

    def test_resolve_tag_missing_exits(self, monkeypatch):
        monkeypatch.setattr(
            CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig(last_tag=""))
        )
        with pytest.raises(SystemExit):
            cli_cloud._resolve_tag(_args(tag=""))


class TestListStatus:
    def test_list_empty(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "list_instances", lambda *a, **k: [])
        assert cli_cloud._cloud_list(_args(profile="", region="")) == 0
        assert "No KiroCrew cloud instances" in capsys.readouterr().out

    def test_list_rows(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ec2,
            "list_instances",
            lambda *a, **k: [{"tag": "kc-1", "instance_id": "i-0abc", "instance_state": "running"}],
        )
        cli_cloud._cloud_list(_args(profile="", region=""))
        out = capsys.readouterr().out
        assert "kc-1" in out and "i-0abc" in out

    def test_status_absent(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        assert cli_cloud._cloud_status(_args(profile="", region="", tag="kc-1")) == 0
        assert "No instance found" in capsys.readouterr().out


class TestConnect:
    def test_connect_not_ready_returns_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud.ssm, "session_manager_plugin_installed", lambda: True)
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(
            connect_mod,
            "connect",
            lambda *a, **k: connect_mod.Connection(
                instance_id="i-0abc",
                local_port=5599,
                remote_port=5476,
                ready=False,
                error="not ready",
            ),
        )
        rc = cli_cloud._cloud_connect(_args(profile="", region="", tag="kc-1"))
        assert rc == 1
        assert "Dashboard tunnel did not become ready" in capsys.readouterr().out


class TestDestroy:
    def test_destroy_dry_run(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ec2,
            "destroy",
            lambda *a, **k: {
                "argv": ["cloudformation", "delete-stack", "--stack-name", "kirocrew-kc-1"]
            },
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=True, yes=False)
        )
        assert rc == 0
        assert "delete-stack" in capsys.readouterr().out

    def test_destroy_absent_noop(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_destroy_confirmed(self, monkeypatch, capsys):
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        destroyed = {}
        monkeypatch.setattr(
            ec2, "destroy", lambda *a, **k: destroyed.update(called=True) or {"destroyed": True}
        )
        monkeypatch.setattr(connect_mod, "unregister_instance", lambda *a, **k: True)
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(
            source_mod, "delete_source", lambda *a, **k: {"removed": True, "uri": "", "error": ""}
        )
        monkeypatch.setattr(
            CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig(last_tag="kc-1"))
        )
        monkeypatch.setattr(CloudConfig, "save", lambda self, *a: None)
        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        assert destroyed["called"] is True
        assert "all AWS resources deleted" in capsys.readouterr().out

    def test_destroy_warns_when_source_cleanup_fails(self, monkeypatch, capsys):
        # Stack deletion confirmed but the source object couldn't be removed:
        # destroy still succeeds (rc 0) but must warn with the manual cleanup
        # command rather than silently leaving a private tarball behind.
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(ec2, "destroy", lambda *a, **k: {"destroyed": True})
        monkeypatch.setattr(connect_mod, "unregister_instance", lambda *a, **k: True)
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(
            source_mod,
            "delete_source",
            lambda *a, **k: {
                "removed": False,
                "uri": "s3://kirocrew-src-1/kc-1/kirocrew-src.tar.gz",
                "error": "AccessDenied",
            },
        )
        monkeypatch.setattr(
            CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig(last_tag="kc-1"))
        )
        monkeypatch.setattr(CloudConfig, "save", lambda self, *a: None)
        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "could not be removed" in out
        assert "aws s3 rm s3://kirocrew-src-1/kc-1/kirocrew-src.tar.gz" in out

    def test_destroy_unconfirmed_returns_nonzero_and_preserves_state(self, monkeypatch, capsys):
        # If ec2.destroy() doesn't confirm deletion, destroy must NOT report
        # success, must NOT clear last_tag / delete the source, and must exit
        # non-zero so automation doesn't assume teardown finished.
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(ec2, "destroy", lambda *a, **k: {"destroyed": False})
        import kiro_crew.cloud.source as source_mod

        def _boom(*a, **k):  # pragma: no cover - must not run on unconfirmed delete
            raise AssertionError("source must not be deleted when teardown is unconfirmed")

        monkeypatch.setattr(source_mod, "delete_source", _boom)
        saved = {"n": 0}
        monkeypatch.setattr(
            CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig(last_tag="kc-1"))
        )
        monkeypatch.setattr(CloudConfig, "save", lambda self, *a: saved.update(n=saved["n"] + 1))
        monkeypatch.setattr(connect_mod, "unregister_instance", lambda *a, **k: True)

        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 1
        assert saved["n"] == 0  # last_tag preserved
        assert "did not confirm" in capsys.readouterr().out

    def test_size_choices_exposed(self):
        assert "balanced" in cli_cloud.add_size_choices()


class TestCloudLogin:
    def test_already_logged_in_short_circuits(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "is_logged_in", lambda *a, **k: True)
        rc = cli_cloud._cloud_login(_args(profile="", region="", tag="kc-1", no_browser=True))
        assert rc == 0
        assert "already signed in" in capsys.readouterr().out

    def test_login_surfaces_device_url_and_waits(self, monkeypatch, capsys):
        from kiro_crew.cloud.login import LoginPrompt

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "is_logged_in", lambda *a, **k: False)
        monkeypatch.setattr(
            cli_cloud.login_mod,
            "start_device_login",
            lambda *a, **k: LoginPrompt(
                url="https://view.awsapps.com/start/#/device?user_code=ABCD-1234", code="ABCD-1234"
            ),
        )
        monkeypatch.setattr(cli_cloud.login_mod, "resume_login_daemon", lambda *a, **k: None)
        monkeypatch.setattr(cli_cloud.login_mod, "wait_until_logged_in", lambda *a, **k: True)
        rc = cli_cloud._cloud_login(_args(profile="", region="", tag="kc-1", no_browser=True))
        out = capsys.readouterr().out
        assert rc == 0
        # Assert the full device URL is echoed (exact string, not a host
        # substring — the latter trips CodeQL's URL-sanitization heuristic).
        assert "https://view.awsapps.com/start/#/device?user_code=ABCD-1234" in out
        assert "ABCD-1234" in out
        assert "Signed in" in out

    def test_login_not_approved_returns_1(self, monkeypatch, capsys):
        from kiro_crew.cloud.login import LoginPrompt

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "is_logged_in", lambda *a, **k: False)
        monkeypatch.setattr(
            cli_cloud.login_mod,
            "start_device_login",
            lambda *a, **k: LoginPrompt(url="https://x/device?user_code=Z", code="Z"),
        )
        monkeypatch.setattr(cli_cloud.login_mod, "resume_login_daemon", lambda *a, **k: None)
        monkeypatch.setattr(cli_cloud.login_mod, "wait_until_logged_in", lambda *a, **k: False)
        rc = cli_cloud._cloud_login(_args(profile="", region="", tag="kc-1", no_browser=True))
        assert rc == 1
        assert "not detected yet" in capsys.readouterr().out

    def test_login_is_dispatched(self, monkeypatch):
        called = {}

        def fake_login(_a):
            called["hit"] = True
            return 0

        monkeypatch.setitem(cli_cloud._DISPATCH, "login", fake_login)
        assert cli_cloud.handle_cloud(_args(cloud_action="login")) == 0
        assert called.get("hit") is True

    def test_tunnel_is_alias_of_connect(self):
        assert cli_cloud._DISPATCH["tunnel"] is cli_cloud._DISPATCH["connect"]
