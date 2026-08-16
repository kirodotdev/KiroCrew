"""Tests for the cloud CLI dispatch layer (cli_cloud.py) — thin wrappers only."""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import cli_cloud
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, ui
from kiro_crew.cloud.config import CloudConfig


def _args(**kw):
    ns = argparse.Namespace()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# Captured before any test can monkeypatch them, for the one case that drives the
# real engine functions (the agent-session chokepoint) instead of a stub.
_REAL_CANCEL_SPOT_REQUESTS = ec2.cancel_spot_requests
_REAL_PROBE_SPOT_REQUESTS = ec2.probe_spot_requests


def _sweep(**kw):
    """A cancel_spot_requests() outcome — all-empty (the on-demand answer) by default."""
    out = {
        "cancelled": [],
        "failed": [],
        "error": "",
        "error_kind": "",
        "terminated": [],
        "terminate_failed": [],
        "terminate_error": "",
    }
    out.update(kw)
    return out


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

    def test_launch_passes_spot_flag(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kwargs: captured.update(kwargs) or 0
        )

        rc = cli_cloud._cloud_launch(_args(profile="", region="", spot=True, yes=True))
        assert rc == 0
        assert captured["spot"] is True

    def test_launch_defaults_to_on_demand(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-west-2"))
        monkeypatch.setattr(
            cli_cloud.wizard, "launch", lambda **kwargs: captured.update(kwargs) or 0
        )

        assert cli_cloud._cloud_launch(_args(profile="", region="", yes=True)) == 0
        assert captured["spot"] is False

    @pytest.mark.parametrize("argv,expected", [(["--spot"], True), ([], False)])
    def test_spot_flag_parses_off_the_real_parser(self, monkeypatch, argv, expected):
        # Drives the REAL `cli.main` parser, so this guards that --spot is
        # actually declared on the `cloud launch` subparser — the dispatch tests
        # above hand-build a Namespace and would pass without it.
        from kiro_crew import cli

        captured = {}

        def fake_handle_cloud(args):
            captured["spot"] = getattr(args, "spot", None)
            raise SystemExit(0)

        monkeypatch.setattr(cli, "boot_platform", lambda *a, **k: None)
        monkeypatch.setattr(cli, "handle_cloud", fake_handle_cloud)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "cloud", "launch", *argv])
        with pytest.raises(SystemExit):
            cli.main()
        assert captured["spot"] is expected

    def test_launch_help_renders(self, monkeypatch, capsys):
        # argparse %-expands help strings, so an unescaped "%" (e.g. "60-90%")
        # raises TypeError on `--help` instead of printing. Render it for real.
        from kiro_crew import cli

        monkeypatch.setattr(cli, "boot_platform", lambda *a, **k: None)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "cloud", "launch", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        out = capsys.readouterr().out
        assert "--spot" in out
        # argparse hard-wraps help text, so match against a whitespace-collapsed
        # copy rather than pinning where the line breaks happen to fall.
        flat = " ".join(out.split())
        assert "60-90% cheaper" in flat
        # ...and it must not promise something AWS won't do: only EC2 can resume
        # an interruption-stopped Spot instance, so `cloud start` fails on one.
        assert "only EC2 can resume an interruption-stop" in flat

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


class TestStart:
    """`kirocrew cloud start` failing is THE tell that a --spot crew was
    interrupted (docs/guides/remote-crew-on-ec2.md says so), but the product used
    to print only the raw AWS error. A user who reads that as "the box is broken"
    reaches for Delete — and destroy takes the root volume, i.e. ~/.kiro/crew.
    """

    @staticmethod
    def _start_args():
        return _args(profile="dev", region="us-east-1", tag="kc-1")

    def test_a_failed_start_on_a_spot_crew_explains_the_interruption(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise ec2.aws.AWSError("IncorrectSpotRequestState", action="ec2:StartInstances")

        monkeypatch.setattr(ec2, "start", boom)
        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **k: {"Parameters": [{"ParameterKey": "Spot", "ParameterValue": "true"}]},
        )
        rc = cli_cloud._cloud_start(self._start_args())
        out = capsys.readouterr().out
        assert rc == 1
        # The real AWS error is still the headline — the hint is added, not
        # substituted, so nothing about the actual failure is hidden.
        assert "IncorrectSpotRequestState" in out
        assert "Only EC2 can restart" in out
        assert "Do NOT destroy" in out

    def test_a_failed_start_on_an_on_demand_crew_is_unchanged(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise ec2.aws.AWSError("IncorrectInstanceState", action="ec2:StartInstances")

        monkeypatch.setattr(ec2, "start", boom)
        monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: {"Parameters": []})
        rc = cli_cloud._cloud_start(self._start_args())
        out = capsys.readouterr().out
        assert rc == 1
        assert "IncorrectInstanceState" in out
        # No Spot story on a box that never ran --spot: it would send the user off
        # to wait for an auto-resume that is never coming.
        assert "Spot" not in out
        assert "Do NOT destroy" not in out

    def test_a_successful_start_makes_no_extra_aws_calls(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "start", lambda *a, **k: {"action": "start"})
        monkeypatch.setattr(
            ec2, "find_stack", lambda *a, **k: pytest.fail("the hint lookup is failure-path only")
        )
        assert cli_cloud._cloud_start(self._start_args()) == 0
        assert "Starting 'kc-1'" in capsys.readouterr().out


class TestDestroy:
    @pytest.fixture(autouse=True)
    def _no_spot_sweep(self, monkeypatch):
        # `cloud destroy` LOOKS for a leftover Spot request even when no stack
        # exists (read-only) and cancels only after the user confirms; stub both
        # to the on-demand answer — nothing found, nothing to cancel — so these
        # stay hermetic. Tests that care about the sweep override them.
        monkeypatch.setattr(ec2, "probe_spot_requests", lambda *a, **k: ([], _sweep()))
        monkeypatch.setattr(ec2, "cancel_spot_requests", lambda *a, **k: _sweep())

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

    def test_destroy_sweeps_spot_request_when_no_stack_exists(self, monkeypatch, capsys):
        # THE case the docs/help advertise ("run cloud destroy after a failed
        # --spot launch"): CloudFormation rolled the stack back, so describe()
        # says it doesn't exist — but the persistent Spot request outlived it.
        # The "nothing to remove" early return must NOT skip the sweep.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        called = {}
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: ([{"id": "sir-orphan", "instance_id": "i-0orphan"}], _sweep()),
        )

        def sweep(tag, profile="", region="", *a, **k):
            called.update(tag=tag, profile=profile, region=region)
            return _sweep(cancelled=["sir-orphan"], terminated=["i-0orphan"])

        monkeypatch.setattr(ec2, "cancel_spot_requests", sweep)
        monkeypatch.setattr(
            ec2, "destroy", lambda *a, **k: pytest.fail("no stack — delete-stack must not run")
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        assert called == {"tag": "kc-1", "profile": "dev", "region": "us-east-1"}
        out = capsys.readouterr().out
        assert "Cancelled Spot request sir-orphan" in out
        assert "Terminated its Spot instance i-0orphan" in out
        # It found and removed something, so the bare "nothing to remove" would
        # be a lie.
        assert "nothing to remove" not in out

    def test_destroy_no_stack_asks_before_cancelling_what_it_found(self, monkeypatch, capsys):
        # Cancelling is destructive in a way the verb hides: a `disabled` request
        # is one whose instance is STOPPED, and EC2 terminates that instance as
        # the request is cancelled — taking the root volume with ~/.kiro/crew on
        # it. A bare `kirocrew cloud destroy` (no -y, tag often from last_tag)
        # used to do that without asking anything, so the probe must be read-only
        # and the ids must be on screen before the question.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: (
                [
                    {"id": "sir-1", "instance_id": "i-0stopped"},
                    {"id": "sir-2", "instance_id": ""},
                ],
                _sweep(),
            ),
        )
        monkeypatch.setattr(
            ec2,
            "cancel_spot_requests",
            lambda *a, **k: pytest.fail("must not cancel before the user says yes"),
        )
        asked = []
        monkeypatch.setattr(
            ui, "confirm", lambda q, default=False: asked.append(q) or False
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=False)
        )
        assert rc == 0
        out = capsys.readouterr().out
        # What it found, before it asks: both ids, and the one that has a box.
        assert "sir-1" in out and "i-0stopped" in out
        assert "sir-2" in out
        # …and the consequence the verb "cancel" hides.
        assert "STOPPED is terminated by EC2" in out
        assert len(asked) == 1
        assert "Aborted — nothing was deleted." in out

    def test_destroy_no_stack_cancels_once_confirmed(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: ([{"id": "sir-1", "instance_id": "i-0stopped"}], _sweep()),
        )
        monkeypatch.setattr(
            ec2, "cancel_spot_requests", lambda *a, **k: _sweep(cancelled=["sir-1"])
        )
        monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=False)
        )
        assert rc == 0
        assert "Cancelled Spot request sir-1" in capsys.readouterr().out

    def test_destroy_no_stack_prompts_only_when_something_was_found(self, monkeypatch, capsys):
        # The empty case — every on-demand tag, and every already-cleaned one —
        # must stay the silent rc-0 "nothing to remove" it always was: a
        # confirmation prompt for cancelling nothing is a prompt people learn to
        # answer without reading.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ui, "confirm", lambda *a, **k: pytest.fail("nothing found — nothing to confirm")
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=False)
        )
        assert rc == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_destroy_no_stack_yes_skips_the_prompt(self, monkeypatch):
        # -y is the automation contract: it may not stop to ask, and it must
        # still sweep.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: ([{"id": "sir-1", "instance_id": ""}], _sweep()),
        )
        cancelled = {}

        def _cancel(*a, **k):
            cancelled["ran"] = True
            return _sweep(cancelled=["sir-1"])

        monkeypatch.setattr(ec2, "cancel_spot_requests", _cancel)
        monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("-y must not prompt"))
        assert (
            cli_cloud._cloud_destroy(
                _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
            )
            == 0
        )
        assert cancelled["ran"] is True

    def test_destroy_no_stack_warns_with_remedy_when_sweep_denied(self, monkeypatch, capsys):
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: ([{"id": "sir-1", "instance_id": ""}], _sweep()),
        )
        monkeypatch.setattr(
            ec2,
            "cancel_spot_requests",
            lambda *a, **k: _sweep(failed=["sir-1"], error="AccessDenied"),
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        # rc 1: the request is still live and will keep handing out replacement
        # instances, which is strictly worse than "delete did not confirm" — and
        # that path already exits 1 so automation can't assume teardown finished.
        assert rc == 1
        out = capsys.readouterr().out
        assert "Could NOT cancel" in out and "sir-1" in out
        assert (
            "aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1 "
            "--profile dev --region us-east-1" in out
        )
        assert "nothing to remove" not in out

    def test_destroy_no_stack_fails_when_the_lookup_itself_failed(self, monkeypatch, capsys):
        # A throttled/offline/unparseable describe is NOT the "old policy, no
        # permission" case: on a real --spot stack a persistent request may well
        # still be live. Warn (not a quiet detail), print the manual check, and
        # exit non-zero.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        # The failure happens in the read-only LOOKUP, so nothing is cancelled and
        # nothing is asked — there is nothing to show a prompt about.
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: (
                [],
                _sweep(
                    error="ec2:DescribeSpotInstanceRequests failed: Throttling: Rate exceeded",
                    error_kind=ec2.SWEEP_ERROR_FAILED,
                ),
            ),
        )
        monkeypatch.setattr(
            ec2, "cancel_spot_requests", lambda *a, **k: pytest.fail("the lookup failed — no sweep")
        )
        monkeypatch.setattr(
            ui, "confirm", lambda *a, **k: pytest.fail("nothing found — nothing to confirm")
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "Could NOT check for a leftover persistent Spot request" in out
        assert "Throttling" in out
        assert (
            "aws ec2 describe-spot-instance-requests --filters "
            f"Name=tag:{ec2.MANAGED_TAG_KEY},Values=true "
            f"Name=tag:{ec2.INSTANCE_TAG_KEY},Values=kc-1 --profile dev --region us-east-1" in out
        )
        # It never learned whether anything is billing, so neither reassurance
        # may be printed.
        assert "nothing to remove" not in out
        assert "no permission" not in out

    def test_destroy_no_stack_stays_quiet_and_clean_when_describe_is_denied(
        self, monkeypatch, capsys
    ):
        # No stack means no Spot parameter to appeal to, so this can prove
        # nothing either way. It stays a quiet rc-0 note (warning here would
        # false-alarm every on-demand destroy of an already-gone stack) — but the
        # note must NOT claim there was never a request, only that it could not
        # look.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: (
                [],
                _sweep(error="AccessDenied", error_kind=ec2.SWEEP_ERROR_ACCESS_DENIED),
            ),
        )
        monkeypatch.setattr(
            ec2, "cancel_spot_requests", lambda *a, **k: pytest.fail("the lookup failed — no sweep")
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "no permission" in out
        assert "Could NOT" not in out
        assert "nothing to remove" in out
        # Honest about what it does NOT know: a leftover Spot request from an
        # earlier --spot launch cannot be ruled out without the permission.
        assert "nothing to prove it either way" in out

    def test_destroy_no_stack_is_a_clean_noop_when_the_agent_guard_refuses(
        self, monkeypatch, capsys
    ):
        # The chokepoint refuses describe-spot-instance-requests from an agent
        # session. Nothing was mutated and nothing learned, so this stays the old
        # rc-0 "nothing to remove" with a pointer at the human path — not a
        # failure and not a billing warning.
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        monkeypatch.setattr(
            ec2,
            "probe_spot_requests",
            lambda *a, **k: (
                [],
                _sweep(
                    error="cloud AWS action 'ec2 describe-spot-instance-requests' is refused "
                    "from an agent session",
                    error_kind=ec2.SWEEP_ERROR_AGENT_SESSION,
                ),
            ),
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Agent sessions can't sweep Spot requests" in out
        assert "Could NOT" not in out
        assert "nothing to remove" in out

    def test_destroy_no_stack_agent_guard_end_to_end(self, monkeypatch, capsys):
        # Same case, driven through the REAL cancel_spot_requests with the env var
        # the chokepoint keys on — guards that the CloudActionDenied is caught in
        # the engine rather than escaping into handle_cloud's rc-1 catch-all.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-123")
        monkeypatch.setattr(ec2, "describe", lambda *a, **k: {"exists": False})
        # Undo the autouse stubs — this one wants the real engine functions. The
        # refusal now lands in the read-only probe, which is where the guard
        # fires; the cancel is never reached (and would refuse too).
        monkeypatch.setattr(ec2, "probe_spot_requests", _REAL_PROBE_SPOT_REQUESTS)
        monkeypatch.setattr(ec2, "cancel_spot_requests", _REAL_CANCEL_SPOT_REQUESTS)
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="us-east-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Agent sessions can't sweep Spot requests" in out
        assert "nothing to remove" in out

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

    def test_destroy_reports_cancelled_spot_requests(self, monkeypatch, capsys):
        # Only a --spot stack has one. Say it out loud: cancelling the persistent
        # request is what stops EC2 launching a REPLACEMENT instance when the
        # stack delete terminates this one.
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(
            ec2,
            "destroy",
            lambda *a, **k: {
                "destroyed": True,
                "spot_sweep": _sweep(cancelled=["sir-1"], terminated=["i-0abc"]),
            },
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
        out = capsys.readouterr().out
        assert "Cancelled Spot request sir-1" in out
        assert "no replacement instance" in out
        assert "Terminated its Spot instance i-0abc" in out
        assert "You won't be billed" in out  # clean sweep keeps the reassurance

    @pytest.mark.parametrize(
        "sweep_kw,expect",
        [
            (
                {"failed": ["sir-1"], "error": "AccessDenied"},
                "aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1",
            ),
            (
                {
                    "cancelled": ["sir-1"],
                    "terminate_failed": ["i-0orphan"],
                    "terminate_error": "AccessDenied",
                },
                "aws ec2 terminate-instances --instance-ids i-0orphan",
            ),
        ],
    )
    def test_destroy_suppresses_billing_claim_when_sweep_fails(
        self, monkeypatch, capsys, sweep_kw, expect
    ):
        # A live persistent request keeps launching replacements and an
        # un-terminated instance keeps billing, so "You won't be billed for it"
        # would be false. Warn with the ids and the exact runnable remedy instead.
        # Both results have the stack DELETED, which is what the engine really
        # returns for them: the failed-terminate row cancelled the request (so
        # nothing can relaunch), and the failed-cancel row is a Spot=false stack
        # still carrying a request from an earlier --spot generation — only a
        # Spot=true stack refuses to delete after a failed cancel (see
        # test_destroy_refuses_to_delete_a_spot_stack_when_the_sweep_failed).
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(
            ec2, "destroy", lambda *a, **k: {"destroyed": True, "spot_sweep": _sweep(**sweep_kw)}
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
            _args(profile="dev", region="eu-west-1", tag="kc-1", dry_run=False, yes=True)
        )
        # rc 1, same as the milder "delete started but did not confirm" path:
        # something may still be billing, so automation must not read this as a
        # finished teardown.
        assert rc == 1
        out = capsys.readouterr().out
        assert "You won't be billed" not in out
        assert "Spot cleanup did not fully succeed" in out
        assert f"{expect} --profile dev --region eu-west-1" in out
        assert "AccessDenied" in out

    @pytest.mark.parametrize(
        "kind,stack_is_spot,rc,billed",
        [
            # The stack — not the principal — settles whether an unanswered
            # lookup is harmless. An admin can launch --spot and a restricted
            # profile destroy it, so a denial on a Spot=true stack hides a
            # possibly live persistent request: warn, no reassurance, rc 1.
            (ec2.SWEEP_ERROR_ACCESS_DENIED, True, 1, False),
            # Spot=false: the stack never created a request, so the denial really
            # is harmless — quiet note, rc 0, reassurance kept (now justified by
            # the stack rather than by guessing at the caller's IAM policy).
            (ec2.SWEEP_ERROR_ACCESS_DENIED, False, 0, True),
            # The agent guard mutated nothing and blocks before AWS; a human
            # re-run sweeps. Graded the same way for the same reason.
            (ec2.SWEEP_ERROR_AGENT_SESSION, True, 1, False),
            (ec2.SWEEP_ERROR_AGENT_SESSION, False, 0, True),
            # Throttling/network/JSON: we never learned whether a persistent
            # request is live, and a stack re-deployed without --spot can still
            # carry one from its Spot generation. Loud either way.
            (ec2.SWEEP_ERROR_FAILED, True, 1, False),
            (ec2.SWEEP_ERROR_FAILED, False, 1, False),
        ],
    )
    def test_destroy_grades_a_failed_spot_lookup_by_cause(
        self, monkeypatch, capsys, kind, stack_is_spot, rc, billed
    ):
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        # An unanswered lookup on a Spot=true stack is also what makes the engine
        # REFUSE the delete (deleting would relaunch the request it could not
        # rule out), so the result carries `aborted` exactly where the engine
        # would set it — the rendering must be graded against a shape destroy can
        # actually return.
        monkeypatch.setattr(
            ec2,
            "destroy",
            lambda *a, **k: {
                "destroyed": not stack_is_spot,
                **({"aborted": True} if stack_is_spot else {}),
                "spot_sweep": _sweep(error="lookup boom", error_kind=kind),
                "stack_is_spot": stack_is_spot,
            },
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
        assert (
            cli_cloud._cloud_destroy(
                _args(profile="dev", region="eu-west-1", tag="kc-1", dry_run=False, yes=True)
            )
            == rc
        )
        out = capsys.readouterr().out
        assert ("You won't be billed" in out) is billed
        if not billed:
            assert "Could NOT check for a leftover persistent Spot request" in out
            assert (
                "aws ec2 describe-spot-instance-requests --filters "
                f"Name=tag:{ec2.MANAGED_TAG_KEY},Values=true "
                f"Name=tag:{ec2.INSTANCE_TAG_KEY},Values=kc-1 --profile dev --region eu-west-1"
                in out
            )
        # The remedies come first, then the verdict about the stack itself.
        assert ("Did NOT delete" in out) is stack_is_spot

    def test_destroy_refuses_to_delete_a_spot_stack_when_the_sweep_failed(
        self, monkeypatch, capsys
    ):
        # The engine refused (nothing was deleted), so the CLI must not print a
        # word of teardown success, must drop NO local state — the registration,
        # the source object and last_tag all still describe a LIVE crew — and must
        # exit 1 while telling the user what to do to make destroy work.
        monkeypatch.setattr(
            ec2, "describe", lambda *a, **k: {"exists": True, "instance_id": "i-0abc"}
        )
        monkeypatch.setattr(
            ec2,
            "destroy",
            lambda *a, **k: {
                "destroyed": False,
                "aborted": True,
                "spot_sweep": _sweep(failed=["sir-1"], error="AccessDenied"),
                "stack_is_spot": True,
            },
        )
        monkeypatch.setattr(
            connect_mod,
            "unregister_instance",
            lambda *a, **k: pytest.fail("the crew still exists — its registration must stay"),
        )
        import kiro_crew.cloud.source as source_mod

        monkeypatch.setattr(
            source_mod,
            "delete_source",
            lambda *a, **k: pytest.fail("nothing was deleted — the source must stay"),
        )
        monkeypatch.setattr(
            CloudConfig,
            "save",
            lambda self, *a: pytest.fail("last_tag still points at a live crew"),
        )
        rc = cli_cloud._cloud_destroy(
            _args(profile="dev", region="eu-west-1", tag="kc-1", dry_run=False, yes=True)
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "Did NOT delete the 'kc-1' stack" in out
        # The remedy that unblocks it, and the promise that nothing was lost.
        assert (
            "aws ec2 cancel-spot-instance-requests --spot-instance-request-ids sir-1 "
            "--profile dev --region eu-west-1" in out
        )
        assert "re-run `kirocrew cloud destroy`" in out
        assert "untouched" in out
        # Not a word that could read as a finished teardown.
        assert "You won't be billed" not in out
        assert "all AWS resources deleted" not in out
        assert "Removed the 'kc-1' stack" not in out

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
