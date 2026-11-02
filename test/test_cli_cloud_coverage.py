"""``cli_cloud.py``: verbs and failure branches that ``test_cloud_cli.py`` leaves unpinned.

Every AWS, SSM and prompt seam is replaced. Profile ``dev``, region ``us-east-1`` and tag
``crew-a`` are pinned, and ``ec2.describe`` answers with a running instance by default.
"""

from __future__ import annotations

import argparse
import shutil
from typing import Any
from unittest import mock

import pytest

from kiro_crew import cli_cloud
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam
from kiro_crew.cloud import login as login_mod
from kiro_crew.cloud import source, ssm
from kiro_crew.cloud.aws import AWSError, CloudActionDenied
from kiro_crew.cloud.launch_state import LaunchState

INSTANCE = {"exists": True, "instance_id": "i-0abc"}
URL = "http://localhost:5599/"


def _args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _boom(message: str, **kw: Any) -> mock.Mock:
    """A stand-in that raises ``AWSError(message)`` when called."""
    return mock.Mock(side_effect=AWSError(message, **kw))


def _describe(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(ec2, "describe", mock.Mock(return_value=state))


@pytest.fixture(autouse=True)
def _launch_record(_floor_monkeypatch):
    _floor_monkeypatch.setattr(cli_cloud, "_resolve", lambda _a: ("dev", "us-east-1"))
    _floor_monkeypatch.setattr(cli_cloud, "_resolve_tag", lambda _a: "crew-a")
    _describe(_floor_monkeypatch, INSTANCE)


def test_list_reports_an_aws_failure_and_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(ec2, "list_instances", _boom("the SSO session has expired"))
    assert cli_cloud._cloud_list(_args()) == 1
    assert "the SSO session has expired" in capsys.readouterr().out


def test_status_prints_the_stack_the_instance_and_the_region(monkeypatch, capsys):
    stack = {"stack_name": "kirocrew-crew-a", "stack_status": "CREATE_COMPLETE"}
    _describe(monkeypatch, {**INSTANCE, **stack, "instance_state": "running", "region": "eu-1"})
    assert cli_cloud._cloud_status(_args()) == 0
    out = capsys.readouterr().out
    assert "stack:    kirocrew-crew-a (CREATE_COMPLETE)" in out
    assert "instance: i-0abc [running]" in out
    assert "region:   eu-1" in out


class TestConnect:
    @pytest.fixture(autouse=True)
    def _plugin(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(cli_cloud, "_ensure_session_manager_plugin", lambda: True)

    @pytest.fixture
    def serve(self, monkeypatch):
        """Make ``connect`` return a ready ``Connection`` built from ``fields``; give the mock."""

        def install(**fields: Any) -> mock.Mock:
            fields = {"ready": True, "url": URL, **fields}
            fake = mock.Mock(return_value=connect_mod.Connection("i-0abc", 5599, 5476, **fields))
            monkeypatch.setattr(connect_mod, "connect", fake)
            return fake

        return install

    @pytest.mark.parametrize("state", [{"exists": False}, {"exists": True, "instance_id": ""}])
    def test_a_fargate_crew_has_no_instance_to_tunnel_to(self, monkeypatch, capsys, state):
        _describe(monkeypatch, state)
        assert cli_cloud._cloud_connect(_args()) == 1
        out = capsys.readouterr().out
        assert "No running instance for tag 'crew-a'." in out
        assert "A Fargate crew is reached from Settings > Remote Crew" in out

    def test_a_tunnel_refused_by_aws_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(connect_mod, "connect", _boom("StartSession was denied"))
        assert cli_cloud._cloud_connect(_args()) == 1
        assert "StartSession was denied" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("token", "opened", "shown", "hidden"),
        [
            ("", False, "Tunnel open, but could not mint a dashboard token.", "Dashboard tunnel"),
            ("minted", True, "Dashboard tunnel open.", "in your browser"),
            ("minted", False, "Open this URL in your browser:", "could not mint"),
        ],
    )
    def test_a_ready_tunnel_prints_its_url_and_waits_on_the_plugin(
        self, serve, capsys, token, opened, shown, hidden
    ):
        plugin = mock.Mock()
        connect = serve(token=token, browser_opened=opened, process=plugin)
        assert cli_cloud._cloud_connect(_args()) == 0
        port = connect_mod.DEFAULT_LOCAL_PORT
        connect.assert_called_once_with(
            "i-0abc", "dev", "us-east-1", local_port=port, open_browser=True
        )
        out = capsys.readouterr().out
        assert shown in out
        assert hidden not in out
        assert URL in out
        plugin.wait.assert_called_once_with()

    def test_ctrl_c_closes_the_tunnel_and_exits_cleanly(self, monkeypatch, serve, capsys):
        plugin = mock.Mock(**{"wait.side_effect": KeyboardInterrupt})
        kill = mock.Mock()
        monkeypatch.setattr(connect_mod, "_kill_process_tree", kill)
        serve(token="minted", process=plugin)
        assert cli_cloud._cloud_connect(_args()) == 0
        kill.assert_called_once_with(plugin)
        assert "Tunnel closed." in capsys.readouterr().out

    @pytest.mark.parametrize("fields", [{"ready": False}, {"url": ""}])
    def test_a_connection_with_no_url_and_no_error_exits_one(self, serve, capsys, fields):
        serve(**fields)
        assert cli_cloud._cloud_connect(_args()) == 1
        assert "Connected but could not mint a dashboard token." in capsys.readouterr().out


class TestLogin:
    @pytest.fixture
    def signed_out(self, monkeypatch):
        """A signed-out instance whose device sign-in returns ``prompt`` or raises ``error``."""

        def install(prompt: Any = None, error: Exception | None = None) -> None:
            monkeypatch.setattr(login_mod, "is_logged_in", lambda *_a, **_k: False)
            monkeypatch.setattr(login_mod, "remote_identity_state", lambda *_a, **_k: "absent")
            monkeypatch.setattr(login_mod, "wait_until_logged_in", mock.Mock())
            start = mock.Mock(return_value=prompt, side_effect=error)
            monkeypatch.setattr(login_mod, "start_device_login", start)

        return install

    def test_it_needs_a_running_instance(self, monkeypatch, capsys):
        _describe(monkeypatch, {"exists": False})
        assert cli_cloud._cloud_login(_args()) == 1
        assert "No running instance for tag 'crew-a'." in capsys.readouterr().out

    def test_a_rejected_identity_target_exits_two_early(self, monkeypatch, capsys):
        is_logged_in = mock.Mock()
        monkeypatch.setattr(login_mod, "is_logged_in", is_logged_in)
        assert cli_cloud._cloud_login(_args(idp_region="us-east-1")) == 2
        is_logged_in.assert_not_called()
        assert "region without a start URL is meaningless" in capsys.readouterr().out

    def test_a_failure_starting_the_device_sign_in_exits_one(self, signed_out, capsys):
        signed_out(error=AWSError("SendCommand was denied"))
        assert cli_cloud._cloud_login(_args()) == 1
        assert "SendCommand was denied" in capsys.readouterr().out

    def test_a_session_found_while_starting_counts_as_signed_in(self, signed_out, capsys):
        signed_out(login_mod.LoginPrompt(already_logged_in=True))
        assert cli_cloud._cloud_login(_args()) == 0
        login_mod.wait_until_logged_in.assert_not_called()
        assert "Signed in." in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("prompt", "shown"),
        [
            (login_mod.LoginPrompt(error="the device flow is unavailable"), "flow is unavailable"),
            (login_mod.LoginPrompt(ports=[8000, 8001]), "SSM port-forward for 8000, 8001"),
        ],
    )
    def test_no_device_url_says_why(self, signed_out, capsys, prompt, shown):
        signed_out(prompt)
        assert cli_cloud._cloud_login(_args()) == 1
        out = capsys.readouterr().out
        assert "Could not start device sign-in on the instance." in out
        assert shown in out


VERBS = [
    ("stop", "Stopped 'crew-a'. Compute billing paused", "Resume with: kirocrew cloud start"),
    ("start", "Starting 'crew-a'.", "Reopen the dashboard with: kirocrew cloud connect"),
]


@pytest.mark.parametrize(("verb", "done", "next_step"), VERBS)
def test_stop_and_start_act_on_the_resolved_tag(monkeypatch, capsys, verb, done, next_step):
    action = mock.Mock()
    monkeypatch.setattr(ec2, verb, action)
    assert getattr(cli_cloud, f"_cloud_{verb}")(_args()) == 0
    action.assert_called_once_with("crew-a", "dev", "us-east-1")
    out = capsys.readouterr().out
    assert done in out
    assert next_step in out


@pytest.mark.parametrize(("verb", "done", "next_step"), VERBS)
def test_a_refused_stop_or_start_exits_one_without_success_lines(
    monkeypatch, capsys, verb, done, next_step
):
    monkeypatch.setattr(ec2, verb, _boom("UnauthorizedOperation: the role may not do this"))
    assert getattr(cli_cloud, f"_cloud_{verb}")(_args()) == 1
    out = capsys.readouterr().out
    assert "UnauthorizedOperation: the role may not do this" in out
    assert done not in out
    assert next_step not in out


class TestDestroyFailures:
    def test_declining_the_prompt_deletes_nothing(self, monkeypatch, capsys):
        confirm, destroy = mock.Mock(return_value=False), mock.Mock()
        monkeypatch.setattr(cli_cloud.ui, "confirm", confirm)
        monkeypatch.setattr(ec2, "destroy", destroy)
        assert cli_cloud._cloud_destroy(_args(yes=False)) == 0
        confirm.assert_called_once_with(
            "Remove KiroCrew instance 'crew-a' from AWS?", default=False  # brand-ok: exact prompt
        )
        destroy.assert_not_called()
        assert "Aborted — nothing was deleted." in capsys.readouterr().out

    def test_a_refused_delete_exits_one_and_keeps_the_local_state(self, monkeypatch, capsys):
        unregister, clear_tag = mock.Mock(), mock.Mock()
        monkeypatch.setattr(ec2, "destroy", _boom("DELETE_FAILED: the role is in use"))
        monkeypatch.setattr(connect_mod, "unregister_instance", unregister)
        monkeypatch.setattr(LaunchState, "clear_tag", clear_tag)
        assert cli_cloud._cloud_destroy(_args(yes=True)) == 1
        unregister.assert_not_called()
        clear_tag.assert_not_called()
        out = capsys.readouterr().out
        assert "DELETE_FAILED: the role is in use" in out
        assert "all AWS resources deleted" not in out


class TestBoundaryUpgradeFailures:
    def test_a_denied_upgrade_names_the_grant_and_tries_the_kms_boundary(self, monkeypatch, capsys):
        grant = "iam:CreatePolicyVersion"
        denied = AWSError("not allowed", action=grant, missing_action=grant)
        upgrade = mock.Mock(side_effect=[denied, source.UPGRADE_CURRENT])
        monkeypatch.setattr(source, "upgrade_instance_boundary", upgrade)
        assert cli_cloud._cloud_iam_boundary(_args(upgrade=True, ssm_kms=True)) == 1
        assert [c.kwargs["kms"] for c in upgrade.call_args_list] == [False, True]
        out = capsys.readouterr().out
        assert "Grant `iam:CreatePolicyVersion` and retry." in out
        assert "KMS-session instance boundary already holds this build's document" in out

    def test_an_unresolved_account_stops_after_the_first_boundary(self, monkeypatch, capsys):
        unresolved = AWSError(
            "could not resolve the AWS account id", action="sts:GetCallerIdentity"
        )
        upgrade = mock.Mock(side_effect=unresolved)
        monkeypatch.setattr(source, "upgrade_instance_boundary", upgrade)
        assert cli_cloud._cloud_iam_boundary(_args(upgrade=True, ssm_kms=True)) == 1
        assert upgrade.call_count == 1
        assert capsys.readouterr().out.count("could not resolve the AWS account id") == 1


class TestDoctor:
    @pytest.fixture(autouse=True)
    def _aws_cli_present(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(cli_cloud, "resolve_aws_bin", lambda: "aws")
        _floor_monkeypatch.setattr(shutil, "which", lambda *_a, **_k: "/usr/bin/aws")

    def test_a_missing_plugin_with_no_refusal_gets_the_install_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: None)
        monkeypatch.setattr(iam, "reachability_check", lambda p, r: {"reachable": False})
        assert cli_cloud._cloud_doctor(_args()) == 0
        out = capsys.readouterr().out
        assert "session-manager-plugin not found — needed to connect over SSM" in out
        assert ssm.session_manager_plugin_install_hint() in out

    def test_a_reachable_account_is_listed_with_each_service_in_order(self, monkeypatch):
        shown: list[tuple[str, str]] = []
        monkeypatch.setattr(cli_cloud.ui, "ok", lambda msg: shown.append(("ok", msg)))
        monkeypatch.setattr(cli_cloud.ui, "warn", lambda msg: shown.append(("warn", msg)))
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: True)
        reach = {"reachable": True, "account": "acct-demo", "ec2_reachable": True}
        reach |= {"cloudformation_reachable": False, "ssm_reachable": True}
        monkeypatch.setattr(iam, "reachability_check", lambda profile, region: reach)
        assert cli_cloud._cloud_doctor(_args()) == 0
        assert ("ok", "AWS reachable — account acct-demo · us-east-1") in shown
        services = [s for s in shown if s[1].startswith(("ec2", "cloudformation", "ssm"))]
        assert services == [
            ("ok", "ec2: reachable"),
            ("warn", "cloudformation: not reachable"),
            ("ok", "ssm: reachable"),
        ]


class TestEnsurePlugin:
    @pytest.fixture(autouse=True)
    def _plugin_missing(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        _floor_monkeypatch.setattr(ssm, "reinstall_cannot_clear_refusal", lambda: False)

    def test_declining_the_prompt_returns_false_and_installs_nothing(self, monkeypatch, capsys):
        confirm, install = mock.Mock(return_value=False), mock.Mock()
        monkeypatch.setattr(cli_cloud.ui, "confirm", confirm)
        monkeypatch.setattr(ssm, "install_session_manager_plugin", install)
        assert cli_cloud._ensure_session_manager_plugin() is False
        confirm.assert_called_once_with("Install session-manager-plugin now?", default=True)
        install.assert_not_called()
        out = capsys.readouterr().out
        assert "session-manager-plugin is required for SSM dashboard tunnels." in out
        assert "Install it later with: kirocrew cloud doctor" in out

    @pytest.mark.parametrize(
        ("ok", "message", "shown"),
        [
            (True, "installed with the AWS package", "installed with the AWS package"),
            (True, "", "session-manager-plugin installed"),
            (False, "sudo: a password is required", "Could not install session-manager-plugin."),
        ],
    )
    def test_the_install_result_decides_the_answer(self, monkeypatch, capsys, ok, message, shown):
        monkeypatch.setattr(cli_cloud.ui, "confirm", lambda *_a, **_k: True)
        result = ssm.PluginInstallResult(ok=ok, message=message)
        monkeypatch.setattr(ssm, "install_session_manager_plugin", lambda: result)
        assert cli_cloud._ensure_session_manager_plugin() is ok
        out = capsys.readouterr().out
        assert "Installing session-manager-plugin locally." in out
        assert shown in out
        assert message in out


def test_a_denied_cloud_action_prints_the_reason_and_exits_one(monkeypatch, capsys):
    denied = mock.Mock(side_effect=CloudActionDenied("cloud stop is a human action"))
    monkeypatch.setitem(cli_cloud._DISPATCH, "stop", denied)
    assert cli_cloud.handle_cloud(_args(cloud_action="stop")) == 1
    assert "cloud stop is a human action" in capsys.readouterr().out
