"""Tests for the cloud CLI dispatch layer (cli_cloud.py) — thin wrappers only."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil

import pytest

from kiro_crew import cli_cloud, platform_compat
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2
from kiro_crew.cloud.config import CloudConfig
from kiro_crew.cloud.launch_state import LaunchState


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
        # Launch inherits the machine's sign-in via ``kiro-cli whoami``; pin the probe so
        # the flag assertion never runs the host's kiro-cli (Builder ID: no identity).
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
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
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
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
    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_destroy_refuses_an_aliased_record_and_deletes_nothing(
        self, monkeypatch, capsys, tmp_path, shape
    ):
        """The whole command, not the helper: an alias must cost the stack nothing.

        A writable second name for the launch record puts any tag in it, and this is the verb
        that acts on the tag irreversibly. So the refusal has to land BEFORE the stack is
        described, let alone deleted, and has to read as a named refusal rather than a
        traceback -- the file, the shape, and the one command that clears it.
        """
        import os

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        real = tmp_path / "somewhere-else.json"
        real.write_text('{"profile": "p", "region": "us-east-1", "last_tag": "kc-theirs"}')
        record = tmp_path / "cloud_launch_state.json"
        if shape == "symlink":
            record.symlink_to(real)
        else:
            os.link(real, record)
            assert record.stat().st_nlink > 1, "the fixture did not produce a second link"

        def _must_not_run(*a, **k):
            raise AssertionError("an aliased record must not reach AWS")

        monkeypatch.setattr(ec2, "describe", _must_not_run)
        monkeypatch.setattr(ec2, "destroy", _must_not_run)

        rc = cli_cloud.handle_cloud(
            _args(cloud_action="destroy", profile="", region="", tag="", dry_run=False, yes=True)
        )

        assert rc == 1
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert "cloud_launch_state.json" in text
        # This platform's own delete command. The first version asserted a POSIX `rm` with POSIX
        # quoting and reddened the Windows shard: `shlex.quote` wraps a `C:\...` path in single
        # quotes, which cmd does not use for quoting, so the remedy named a command a Windows
        # operator does not have for a path their shell would not resolve.
        if platform_compat.IS_WINDOWS:  # pragma: no cover - asserted on the Windows shards
            assert f'del "{record}"' in text
        else:
            assert f"rm {shlex.quote(str(record))}" in text

    def test_launch_refuses_an_aliased_record_before_re_attaching(
        self, monkeypatch, capsys, tmp_path
    ):
        """Re-attaching to a forged tag is the same substitution, just quieter than a delete.

        ``launch`` resolves the saved tag to decide whether to keep an existing stack, and
        under ``--yes`` it keeps it without asking. So the same read refuses here, and the
        wizard is never entered.
        """
        from kiro_crew.cloud import wizard

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        real = tmp_path / "somewhere-else.json"
        real.write_text('{"profile": "p", "region": "us-east-1", "last_tag": "kc-theirs"}')
        (tmp_path / "cloud_launch_state.json").symlink_to(real)

        def _must_not_run(*a, **k):
            raise AssertionError("an aliased record must not reach the wizard")

        monkeypatch.setattr(wizard, "launch", _must_not_run)

        rc = cli_cloud.handle_cloud(_args(cloud_action="launch", profile="", region="", yes=True))

        assert rc == 1
        captured = capsys.readouterr()
        assert "cloud_launch_state.json" in captured.out + captured.err

    def test_a_failed_pointer_clear_still_reports_the_removal(self, monkeypatch, capsys):
        """The stack is gone, so nothing after it may fail the command.

        A full disk, a read-only filesystem or a lock that cannot be taken raises `OSError`
        from the clear -- after `ec2.destroy` confirmed. Exiting non-zero there tells automation
        the teardown failed while the AWS resources are actually deleted, and the AWS work
        cannot be retried. Same decision `wizard._record_launch` makes on the launch side.
        """
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(ec2, "destroy", lambda *a, **k: {"destroyed": True})
        monkeypatch.setattr(connect_mod, "unregister_instance", lambda *a, **k: True)
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(
            source_mod, "delete_source", lambda *a, **k: {"removed": True, "uri": "", "error": ""}
        )

        def _read_only(*a, **k):
            raise OSError("Read-only file system")

        monkeypatch.setattr(LaunchState, "clear_tag", classmethod(_read_only))

        rc = cli_cloud._cloud_destroy(
            _args(profile="", region="", tag="kc-1", dry_run=False, yes=True)
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "all AWS resources deleted" in out
        assert "Read-only file system" in out

    def test_destroy_refuses_an_aliased_legacy_config_and_deletes_nothing(
        self, monkeypatch, capsys, tmp_path
    ):
        """The same hole through the OLD file: an aliased `cloud.json` with no record yet.

        The record's guard did not cover its own fallback, and `cloud.json`'s guard sits on the
        launch seam, which a teardown never runs -- so a forged legacy `last_tag` reached this
        command. The pre-created empty record is the default state on any install that has
        spawned an agent, so the fallback is the normal path rather than an exotic one.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        real = tmp_path / "dotfiles-cloud.json"
        real.write_text('{"last_tag": "kc-theirs"}')
        (tmp_path / "cloud.json").symlink_to(real)
        (tmp_path / "cloud_launch_state.json").write_text("{}")

        def _must_not_run(*a, **k):
            raise AssertionError("an aliased config must not reach AWS")

        monkeypatch.setattr(ec2, "describe", _must_not_run)
        monkeypatch.setattr(ec2, "destroy", _must_not_run)

        rc = cli_cloud.handle_cloud(
            _args(cloud_action="destroy", profile="", region="", tag="", dry_run=False, yes=True)
        )

        assert rc == 1
        captured = capsys.readouterr()
        assert "cloud.json" in captured.out + captured.err

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
        monkeypatch.setattr(LaunchState, "record", classmethod(lambda cls, **k: None))
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
        monkeypatch.setattr(LaunchState, "record", classmethod(lambda cls, **k: None))
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
        monkeypatch.setattr(
            LaunchState,
            "record",
            classmethod(lambda cls, **k: saved.update(n=saved["n"] + 1)),
        )
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
        monkeypatch.setattr(cli_cloud.login_mod, "remote_identity_state", lambda *a, **k: "absent")
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
        monkeypatch.setattr(cli_cloud.login_mod, "remote_identity_state", lambda *a, **k: "absent")
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

    def test_flagless_login_over_an_identity_center_session_is_a_mismatch(
        self, monkeypatch, capsys
    ):
        """A flagless ``cloud login`` asks for Builder ID. On an instance that
        holds an Identity Center session that is a wrong-identity mismatch, not
        "not signed in": kiro-cli ignores a login over a live session, so
        starting one would do nothing and report nothing. The command names the
        target it was asked for, points at logout, exits 1, and never starts a
        sign-in."""
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "is_logged_in", lambda *a, **k: False)
        seen: list[object] = []

        def state(*a, **k):
            seen.append(k["target"])
            return "mismatch"

        monkeypatch.setattr(cli_cloud.login_mod, "remote_identity_state", state)
        monkeypatch.setattr(
            cli_cloud.login_mod,
            "start_device_login",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sign-in over a mismatch")),
        )
        rc = cli_cloud._cloud_login(_args(profile="", region="", tag="kc-1", no_browser=True))
        out = capsys.readouterr().out
        assert rc == 1
        assert seen and seen[0].is_default
        assert "DIFFERENT Kiro identity than Builder ID" in out and "cloud logout" in out

    def test_logout_signs_out_and_points_at_login(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "logout", lambda *a, **k: True)
        rc = cli_cloud._cloud_logout(_args(profile="", region="", tag="kc-1"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Signed out" in out
        assert "kirocrew cloud login" in out

    def test_logout_fails_when_session_survives(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(cli_cloud.login_mod, "logout", lambda *a, **k: False)
        rc = cli_cloud._cloud_logout(_args(profile="", region="", tag="kc-1"))
        assert rc == 1
        assert "Could not confirm the instance is signed out" in capsys.readouterr().out

    def test_logout_no_instance(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "kc-1")
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        assert cli_cloud._cloud_logout(_args(profile="", region="", tag="kc-1")) == 1
        assert "No running instance" in capsys.readouterr().out

    def test_logout_is_dispatched(self):
        assert cli_cloud._DISPATCH["logout"] is cli_cloud._cloud_logout

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


class TestDoctor:
    def test_doctor_probes_resolved_aws_binary(self, monkeypatch, capsys):
        """The doctor probes the binary resolved spawns execute, never the
        bare name — a bare-name probe disagrees with resolved spawn sites
        under a GUI-launched gateway's minimal PATH."""
        resolved = "/opt/aws-cli/aws"
        probed: list[str] = []

        def fake_which(name, *args, **kwargs):
            probed.append(name)
            return name if name == resolved else None

        monkeypatch.setattr(cli_cloud, "resolve_aws_bin", lambda: resolved)
        monkeypatch.setattr(shutil, "which", fake_which)
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud.ssm, "session_manager_plugin_installed", lambda: True)
        monkeypatch.setattr(
            cli_cloud.iam,
            "reachability_check",
            lambda profile, region: {"reachable": False, "note": ""},
        )

        assert cli_cloud.handle_cloud(_args(cloud_action="doctor")) == 0
        out = capsys.readouterr().out
        assert "aws CLI found" in out
        assert resolved in probed
        assert "aws" not in probed  # the bare-name probe is the defect

    def test_doctor_reports_a_refused_plugin_as_refused_not_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_cloud, "resolve_aws_bin", lambda: "/opt/aws-cli/aws")
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(
            cli_cloud.ssm,
            "session_manager_plugin_refusal",
            lambda: cli_cloud.ssm.PluginRefusal(
                path="/usr/local/sessionmanagerplugin/bin/session-manager-plugin",
                reason="executable is owned by another user (uid 1501)",
                remedy_command="sudo chown -R root:wheel /usr/local/sessionmanagerplugin",
            ),
        )
        monkeypatch.setattr(
            cli_cloud.iam,
            "reachability_check",
            lambda profile, region: {"reachable": False, "note": ""},
        )

        assert cli_cloud.handle_cloud(_args(cloud_action="doctor")) == 0
        out = capsys.readouterr().out
        assert (
            "found at /usr/local/sessionmanagerplugin/bin/session-manager-plugin but refused" in out
        )
        assert "owned by another user (uid 1501)" in out
        assert "sudo chown -R root:wheel /usr/local/sessionmanagerplugin" in out
        # The command makes whatever binary is in the tree an accepted credential
        # receiver, so the remedy asks for AWS provenance before it.
        assert "reinstall it from AWS first" in out
        assert out.index("reinstall it from AWS first") < out.index("sudo chown -R root:wheel")
        assert "session-manager-plugin not found" not in out

    @pytest.mark.parametrize(
        ("code", "next_step"),
        [
            ("other_owner", "Install a copy with AWS's own installer, then rerun."),
            ("world_writable", "Fix the file's ownership or permissions"),
            ("root_gateway", "Start the gateway from a regular account instead of root"),
            ("strict_policy", "Install it where only root can change the file"),
        ],
    )
    def test_doctor_reports_a_refused_aws_cli_as_refused_not_missing(
        self, monkeypatch, capsys, code, next_step
    ):
        """Found but refused is not missing: the install link would put the same file back."""
        monkeypatch.setattr(cli_cloud, "resolve_aws_bin", lambda: "aws")
        monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(
            cli_cloud.ssm,
            "aws_cli_refusal",
            lambda: cli_cloud.ssm.PluginRefusal(
                path="/opt/homebrew/bin/aws",
                reason="the check's words",
                remedy_command="",
                code=code,
            ),
        )
        monkeypatch.setattr(cli_cloud.ssm, "session_manager_plugin_installed", lambda: True)
        monkeypatch.setattr(
            cli_cloud.iam,
            "reachability_check",
            lambda profile, region: {"reachable": False, "note": "Profile did not resolve"},
        )

        assert cli_cloud.handle_cloud(_args(cloud_action="doctor")) == 0
        out = capsys.readouterr().out
        assert "aws CLI found at /opt/homebrew/bin/aws but refused: the check's words" in out
        assert next_step in out
        if code == "other_owner":
            # Making another account's file trusted is the risk the check guards.
            assert "Fix the file's ownership or permissions" not in out
            assert "/usr/local/bin belongs to the other account" in out
        assert "aws CLI not found" not in out
        assert "https://aws.amazon.com/cli/" not in out
        # The profile was never checked, so its advice would point the wrong way.
        assert "Profile did not resolve" not in out
        assert "The aws CLI above was refused, so the profile was not checked." in out

    @pytest.mark.parametrize(
        ("code", "next_step"),
        [
            ("other_owner", "Install a copy with AWS's own installer, then rerun."),
            ("root_gateway", "Start the gateway from a regular account instead of root"),
            ("", "Fix the file's ownership or permissions"),
        ],
    )
    def test_doctor_gives_a_refused_plugin_with_no_command_its_code_s_step(
        self, monkeypatch, capsys, code, next_step
    ):
        """The plugin line takes the same step per code as the aws CLI line."""
        monkeypatch.setattr(cli_cloud, "resolve_aws_bin", lambda: "/opt/aws-cli/aws")
        monkeypatch.setattr(shutil, "which", lambda *_a, **_k: "/opt/aws-cli/aws")
        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(cli_cloud.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(
            cli_cloud.ssm,
            "session_manager_plugin_refusal",
            lambda: cli_cloud.ssm.PluginRefusal(
                path="/opt/homebrew/bin/session-manager-plugin",
                reason="the check's words",
                remedy_command="",
                code=code,
            ),
        )
        monkeypatch.setattr(
            cli_cloud.iam,
            "reachability_check",
            lambda profile, region: {"reachable": False, "note": ""},
        )

        assert cli_cloud.handle_cloud(_args(cloud_action="doctor")) == 0
        out = capsys.readouterr().out
        assert next_step in out
        if code == "other_owner":
            assert "Fix the file's ownership or permissions" not in out


_ACCT = "123456789012"
_DENIED_CREATE = (
    "An error occurred (AccessDenied) when calling the CreatePolicy operation: "
    "User: arn:aws:iam::123456789012:user/launcher is not authorized to perform: "
    "iam:CreatePolicy on resource: policy {name}"
)


class _FakeIam:
    """An in-memory IAM policy store behind the two ``aws`` chokepoints the
    boundary sequence calls: ``run_aws`` (get-policy, create-policy) and
    ``checked_json`` (the content read before reuse). No network, no account."""

    def __init__(self, existing=None, deny_create=()):
        self.policies = dict(existing or {})
        self.deny_create = set(deny_create)
        self.calls: list[list[str]] = []

    @staticmethod
    def _flag(args, flag):
        return args[args.index(flag) + 1]

    def run_aws(self, args, *_a, **_k):
        self.calls.append(list(args))
        if args[:2] == ["iam", "get-policy"]:
            if self._flag(args, "--policy-arn") in self.policies:
                return (0, "{}", "")
            return (254, "", "An error occurred (NoSuchEntity) when calling GetPolicy")
        if args[:2] == ["iam", "create-policy"]:
            name = self._flag(args, "--policy-name")
            arn = f"arn:aws:iam::{_ACCT}:policy/{name}"
            if name in self.deny_create:
                return (254, "", _DENIED_CREATE.format(name=name))
            if arn in self.policies:
                return (254, "", "An error occurred (EntityAlreadyExists)")
            self.policies[arn] = json.loads(self._flag(args, "--policy-document"))
            return (0, "{}", "")
        raise AssertionError(f"unexpected aws call {args[:2]}")

    def checked_json(self, args, *_a, **_k):
        self.calls.append(list(args))
        arn = self._flag(args, "--policy-arn")
        if args[:2] == ["iam", "get-policy"]:
            return {"Policy": {"DefaultVersionId": "v1"}}
        if args[:2] == ["iam", "get-policy-version"]:
            return {"PolicyVersion": {"Document": self.policies[arn]}}
        raise AssertionError(f"unexpected checked_json {args[:2]}")

    def verbs(self):
        return [c[1] for c in self.calls if c[:1] == ["iam"]]

    def created_names(self):
        return [self._flag(c, "--policy-name") for c in self.calls if c[1] == "create-policy"]


def _expected_boundaries():
    """Every boundary the admin step owns, as ``{arn: content-fixed document}``."""
    from kiro_crew.cloud import iam

    return {
        iam.boundary_arn(_ACCT): iam.boundary_policy_document(_ACCT),
        iam.crew_boundary_arn(_ACCT): iam.crew_boundary_policy_document(),
        iam.crew_exec_boundary_arn(_ACCT): iam.crew_exec_boundary_policy_document(),
    }


class TestIamBoundary:
    """``kirocrew cloud iam-boundary`` drives the real create-once-and-verify
    sequence in ``cloud.source`` against a fake IAM store."""

    @pytest.fixture
    def fake_iam(self, monkeypatch):
        from kiro_crew.cloud import aws, source

        def install(**kw):
            fake = _FakeIam(**kw)
            monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
            monkeypatch.setattr(source, "_account_id", lambda *_a: _ACCT)
            monkeypatch.setattr(source, "_audit_iam_policy_change", lambda *_a, **_k: None)
            monkeypatch.setattr(aws, "run_aws", fake.run_aws)
            monkeypatch.setattr(aws, "checked_json", fake.checked_json)
            return fake

        return install

    def test_creates_all_three_boundaries_from_their_fixed_documents(self, fake_iam, capsys):
        from kiro_crew.cloud import iam

        fake = fake_iam()
        assert cli_cloud.handle_cloud(_args(cloud_action="iam-boundary")) == 0
        assert fake.created_names() == [
            iam.BOUNDARY_NAME,
            iam.CREW_BOUNDARY_NAME,
            iam.CREW_EXEC_BOUNDARY_NAME,
        ]
        assert fake.policies == _expected_boundaries()
        # Create-once: nothing that could re-version or remove a ceiling.
        for verb in ("create-policy-version", "delete-policy", "set-default-policy-version"):
            assert verb not in fake.verbs()
        out = capsys.readouterr().out
        for arn in _expected_boundaries():
            assert arn in out
        assert "now that all three boundaries exist" in out

    def test_a_rerun_leaves_matching_boundaries_untouched(self, fake_iam, capsys):
        fake = fake_iam(existing=_expected_boundaries())
        assert cli_cloud.handle_cloud(_args(cloud_action="iam-boundary")) == 0
        assert "create-policy" not in fake.verbs()
        # Each existing boundary was read back and compared before it counted.
        assert fake.verbs().count("get-policy-version") == 3
        assert fake.policies == _expected_boundaries()
        assert capsys.readouterr().out.count(" ready: arn:aws:iam::") == 3

    @pytest.mark.parametrize("drifted", ["kirocrew-crew-boundary", "kirocrew-crew-exec-boundary"])
    def test_a_drifted_crew_boundary_is_refused_not_overwritten(self, fake_iam, capsys, drifted):
        permissive = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
        existing = _expected_boundaries()
        drifted_arn = f"arn:aws:iam::{_ACCT}:policy/{drifted}"
        existing[drifted_arn] = permissive
        fake = fake_iam(existing=existing)

        assert cli_cloud.handle_cloud(_args(cloud_action="iam-boundary")) == 1
        assert fake.policies[drifted_arn] == permissive
        assert "create-policy" not in fake.verbs()
        out = capsys.readouterr().out
        assert f"'{drifted}' does NOT match" in out
        # The other two are still verified and reported.
        assert out.count(" ready: arn:aws:iam::") == 2
        # A refusal must not be followed by advice to drop the create grant.
        assert "now that all three boundaries exist" not in out
        assert "Keep the iam:CreatePolicy action" in out

    def test_a_denied_create_names_the_admin_step_and_the_rest_still_run(self, fake_iam, capsys):
        from kiro_crew.cloud import iam

        fake = fake_iam(deny_create={iam.CREW_BOUNDARY_NAME})
        assert cli_cloud.handle_cloud(_args(cloud_action="iam-boundary")) == 1
        assert iam.crew_boundary_arn(_ACCT) not in fake.policies
        assert iam.crew_exec_boundary_arn(_ACCT) in fake.policies
        out = capsys.readouterr().out
        head = out[out.index("could not create the permissions boundary") :][:200]
        assert "kirocrew cloud iam-boundary" in head
        assert "Grant `iam:CreatePolicy` and retry." in out

    def test_an_unresolved_account_is_reported_once(self, fake_iam, monkeypatch, capsys):
        from kiro_crew.cloud import source

        fake = fake_iam()
        monkeypatch.setattr(source, "_account_id", lambda *_a: "")
        assert cli_cloud.handle_cloud(_args(cloud_action="iam-boundary")) == 1
        assert fake.calls == []
        assert capsys.readouterr().out.count("could not resolve the AWS account id") == 1
