"""``cloud/wizard.py``: the helpers ``test_cloud_wizard.py`` reaches only through ``launch``.

The progress streamer, the SSM readiness wait, the sign-in check and the launch-discovery
helpers are called directly here. ``wizard._sleep`` is replaced by a recorder, so no test waits,
and every AWS, SSM and prompt seam is a stand-in: nothing runs ``aws`` or reads a terminal.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew.cloud import aws, ec2, login, ssm, ui, wizard
from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.launch_state import LaunchState
from kiro_crew.cloud.login import LoginPrompt
from kiro_crew.cloud.login_target import KiroLoginTarget

ARGS = ("i-0abc", "dev", "us-east-1")


def _boom(message: str) -> mock.Mock:
    """A stand-in that raises ``AWSError(message)`` when called."""
    return mock.Mock(side_effect=AWSError(message))


@pytest.fixture
def slept(monkeypatch):
    """Record every ``wizard._sleep`` instead of sleeping."""
    calls: list[float] = []
    monkeypatch.setattr(wizard, "_sleep", calls.append)
    return calls


def _event(
    event_id: str, resource: str, status: str, reason: str = "", physical_id: str = ""
) -> dict[str, str]:
    return {
        "id": event_id,
        "resource": resource,
        "status": status,
        "reason": reason,
        "physical_id": physical_id,
    }


def _worker(*alive: bool) -> mock.Mock:
    """A thread stand-in whose ``is_alive`` answers ``alive`` in turn, one per poll pass."""
    return mock.Mock(**{"is_alive.side_effect": list(alive)})


class TestStreamProgress:
    def test_events_print_once_and_the_box_log_is_tailed_from_the_instance_event(
        self, monkeypatch, capsys, slept
    ):
        start = _event("e1", "crew-a", "CREATE_IN_PROGRESS")  # the stack's own start is noise
        instance = _event("e2", "Instance", "CREATE_IN_PROGRESS", physical_id="i-0abc")
        denied = _event("e3", "Role", "CREATE_FAILED", "AccessDenied: " + "x" * 300)
        done = _event("e4", "Instance", "CREATE_COMPLETE", physical_id="i-0abc")
        polls = [[start, instance, denied], [start, instance, denied, done]]
        monkeypatch.setattr(ec2, "list_stack_events", mock.Mock(side_effect=polls))
        # The stack's InstanceId output only exists once the WaitCondition completes.
        monkeypatch.setattr(ec2, "describe", mock.Mock(return_value={"instance_id": ""}))
        tail = mock.Mock(side_effect=[["booting", "y" * 400], ["booting", "ready"]])
        monkeypatch.setattr(wizard, "_fetch_bootstrap_log", tail)
        seen_events: set[str] = set()
        seen_lines: set[str] = set()

        wizard._stream_progress(
            _worker(True, True, False), "crew-a", "dev", "us-east-1", seen_events, seen_lines, ""
        )

        out = capsys.readouterr().out
        assert seen_events == {"e1", "e2", "e3", "e4"}
        assert seen_lines == {"booting", "y" * 400, "ready"}
        assert out.count("CREATE_IN_PROGRESS") == 1  # the instance's line, once across both polls
        assert "CREATE_FAILED" in out
        assert "x" * 186 in out  # the reason is cut at 200 characters, "AccessDenied: " included
        assert "x" * 187 not in out
        assert out.count("[box] booting") == 1
        assert "[box] ready" in out
        assert "y" * 180 in out  # a box log line is cut at 180 characters
        assert "y" * 181 not in out
        tail.assert_called_with("i-0abc", "dev", "us-east-1")
        assert slept == [wizard._PROGRESS_POLL_SECS] * 2

    def test_a_failed_event_poll_is_swallowed_and_no_log_is_tailed_without_an_instance(
        self, monkeypatch, slept
    ):
        monkeypatch.setattr(ec2, "list_stack_events", _boom("throttled"))
        tail = mock.Mock()
        monkeypatch.setattr(wizard, "_fetch_bootstrap_log", tail)
        wizard._stream_progress(
            _worker(True, True, False), "crew-a", "dev", "us-east-1", set(), set(), ""
        )
        tail.assert_not_called()
        assert slept == [wizard._PROGRESS_POLL_SECS] * 2

    def test_a_quiet_stretch_prints_the_elapsed_time(self, monkeypatch, capsys, slept):
        monkeypatch.setattr(ec2, "list_stack_events", lambda *_a: [])
        monkeypatch.setattr(wizard, "_now", mock.Mock(side_effect=[100, 110, 131, 140]))
        wizard._stream_progress(
            _worker(True, True, True, False), "crew-a", "dev", "us-east-1", set(), set(), ""
        )
        out = capsys.readouterr().out
        assert out.count("elapsed") == 1  # once 30 s pass with nothing new, not every poll
        assert "still working… 0:31 elapsed" in out


class TestEnsureRunning:
    def test_a_stopped_instance_that_will_not_start_fails_the_wait(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "_instance_state", lambda *_a: "stopped")
        monkeypatch.setattr(wizard, "aws_start_instance", _boom("UnauthorizedOperation"))
        managed = mock.Mock()
        monkeypatch.setattr(ssm, "instance_is_managed", managed)
        assert wizard._ensure_running_and_ssm_ready(*ARGS) is False
        managed.assert_not_called()
        assert "could not start the instance: UnauthorizedOperation" in capsys.readouterr().out

    def test_a_pending_instance_is_waited_for_until_ssm_is_online(self, monkeypatch, capsys, slept):
        monkeypatch.setattr(ec2, "_instance_state", lambda *_a: "pending")
        monkeypatch.setattr(ssm, "instance_is_managed", mock.Mock(side_effect=[False, True]))
        assert wizard._ensure_running_and_ssm_ready(*ARGS) is True
        assert slept == [wizard._SSM_READY_POLL_SECS]
        out = capsys.readouterr().out
        assert "Instance is pending; waiting for it to run" in out
        assert "Instance is running and SSM is online." in out

    def test_an_ssm_agent_that_never_comes_online_times_out(self, monkeypatch, capsys, slept):
        monkeypatch.setattr(ec2, "_instance_state", lambda *_a: "running")
        monkeypatch.setattr(ssm, "instance_is_managed", lambda *_a: False)
        assert wizard._ensure_running_and_ssm_ready(*ARGS) is False
        assert sum(slept) == wizard._SSM_READY_TIMEOUT_SECS
        assert "did not report SSM Online in time" in capsys.readouterr().out

    def test_aws_start_instance_starts_that_instance_id(self, monkeypatch):
        checked = mock.Mock(return_value="")
        monkeypatch.setattr(aws, "checked", checked)
        wizard.aws_start_instance(*ARGS)
        checked.assert_called_once_with(
            ["ec2", "start-instances", "--instance-ids", "i-0abc"],
            "dev",
            "us-east-1",
            action="ec2:StartInstances",
        )


class TestVerifyOperational:
    @pytest.fixture(autouse=True)
    def _signed_out(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(login, "is_logged_in", lambda *_a, **_k: False)
        _floor_monkeypatch.setattr(login, "remote_identity_state", lambda *_a, **_k: "absent")

    def test_declining_the_sign_in_prompt_leaves_the_box_unsigned(self, monkeypatch):
        start = mock.Mock()
        monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
        monkeypatch.setattr(login, "start_device_login", start)
        assert wizard._verify_operational(*ARGS) == "unsigned"
        start.assert_not_called()

    def test_a_device_login_that_cannot_start_leaves_the_box_unsigned(self, monkeypatch, capsys):
        monkeypatch.setattr(login, "start_device_login", _boom("SendCommand was denied"))
        assert wizard._verify_operational(*ARGS, assume_yes=True) == "unsigned"
        assert "SendCommand was denied" in capsys.readouterr().out

    def test_a_session_found_while_starting_the_login_counts_as_signed_in(self, monkeypatch):
        prompt = LoginPrompt(already_logged_in=True)
        monkeypatch.setattr(login, "start_device_login", lambda *_a, **_k: prompt)
        assert wizard._verify_operational(*ARGS, assume_yes=True) == "ok"

    @pytest.mark.parametrize(
        ("opened", "signed", "note", "verdict"),
        [
            (True, True, "approve the code to finish.", "ok"),
            (False, False, "and approve the code.", "unsigned"),
        ],
    )
    def test_the_device_login_shows_its_code_and_waits_for_approval(
        self, monkeypatch, capsys, opened, signed, note, verdict
    ):
        prompt = LoginPrompt(url="https://device.example.invalid/x", code="ABCD-1234")
        prompt.browser_opened = opened
        prompt.close = mock.Mock()
        monkeypatch.setattr(login, "start_device_login", lambda *_a, **_k: prompt)
        resume = mock.Mock()
        wait = mock.Mock(return_value=signed)
        monkeypatch.setattr(login, "resume_login_daemon", resume)
        monkeypatch.setattr(login, "wait_until_logged_in", wait)

        assert wizard._verify_operational(*ARGS, assume_yes=True) == verdict

        out = capsys.readouterr().out
        assert prompt.url in out
        assert note in out
        assert "Verification code: ABCD-1234" in out
        assert ("Signed in — new chats will work now." in out) is signed
        resume.assert_called_once_with(*ARGS, target=KiroLoginTarget())
        wait.assert_called_once_with(*ARGS, target=KiroLoginTarget())
        prompt.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ssm.CommandResult("Success", "booting\n\n  \nready\n", "", 0), ["booting", "ready"]),
        (ssm.CommandResult("Failed", "ignored", "boom", 1), []),
        (AWSError("SendCommand was denied"), []),
    ],
)
def test_the_bootstrap_log_tail_is_best_effort(monkeypatch, outcome, expected):
    run = mock.Mock(side_effect=[outcome])  # an exception in the list is raised, a result returned
    monkeypatch.setattr(ssm, "run_command", run)
    assert wizard._fetch_bootstrap_log(*ARGS) == expected
    args, kwargs = run.call_args
    assert (args[0], args[2:]) == ("i-0abc", ("dev", "us-east-1"))
    assert "tail -n 6 /var/log/kirocrew-setup.log" in args[1]
    assert kwargs == {"run_as": "root", "total_wait": 30}


def test_a_failed_plugin_install_is_reported_and_refused(monkeypatch, capsys):
    result = ssm.PluginInstallResult(ok=False, message="sudo: a password is required")
    monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
    monkeypatch.setattr(ssm, "reinstall_cannot_clear_refusal", lambda: False)
    monkeypatch.setattr(ssm, "install_session_manager_plugin", lambda: result)
    assert wizard._ensure_session_manager_plugin(assume_yes=True) is False
    out = capsys.readouterr().out
    assert "Could not install session-manager-plugin." in out
    assert "sudo: a password is required" in out


class TestLaunchDiscovery:
    LAUNCHES = [
        wizard._ExistingLaunch(tag="crew-a", stack_name="stack-a"),
        wizard._ExistingLaunch(tag="crew-b", stack_name="stack-b"),
    ]

    def test_discovery_skips_blank_and_repeated_tags(self, monkeypatch):
        rows = [
            {"tag": ""},
            {"tag": "crew-a", "stack_name": "stack-a", "stack_status": "CREATE_COMPLETE"},
            {"tag": "crew-a", "stack_name": "stack-again"},
        ]
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a: rows)
        monkeypatch.setattr(ec2, "live_instance_tags", lambda *_a: {"crew-a"})
        found = wizard._discover_existing_launches(LaunchState(), "dev", "us-east-1")
        assert found == [wizard._ExistingLaunch("crew-a", "stack-a", "CREATE_COMPLETE")]

    def test_choosing_a_new_installation_among_several_stacks_keeps_none(self, monkeypatch, capsys):
        monkeypatch.setattr(ui, "choose", lambda title, options, default_index=0: len(options) - 1)
        assert wizard._choose_existing_launch(self.LAUNCHES) is None
        assert "Creating a new installation; existing stacks are unchanged." in (
            capsys.readouterr().out
        )

    def test_a_stack_that_no_longer_exists_cannot_be_resumed(self, monkeypatch):
        monkeypatch.setattr(ec2, "describe", lambda *_a: {"exists": False})
        assert wizard._resume_tag("crew-a", "dev", "us-east-1") is None

    def test_a_chosen_launch_whose_stack_vanished_selects_nothing(self, monkeypatch):
        chosen = self.LAUNCHES[0]
        monkeypatch.setattr(wizard, "_discover_existing_launches", lambda *_a: [chosen])
        monkeypatch.setattr(wizard, "_choose_existing_launch", lambda *_a, **_k: chosen)
        monkeypatch.setattr(wizard, "_resume_tag", lambda *_a: None)
        assert wizard._select_existing_launch(LaunchState(), "dev", "us-east-1") is None
