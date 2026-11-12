"""Unit tests for the cloud launch wizard orchestration."""

from __future__ import annotations

import io

import pytest

from kiro_crew.cloud import aws
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam, login, ssm, wizard
from kiro_crew.cloud.launch_state import LaunchState


def _patch_post_launch(monkeypatch, *, logged_in: bool = True) -> dict[str, list[str]]:
    calls: dict[str, list[str]] = {"login": [], "connect": [], "register": []}
    monkeypatch.setattr(
        iam,
        "reachability_check",
        lambda *_a, **_k: {
            "reachable": True,
            "account": "123456789012",
            "ec2_reachable": True,
            "cloudformation_reachable": True,
            "ssm_reachable": True,
        },
    )
    monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: True)

    def fake_is_logged_in(instance_id, *_a, **_k):
        calls["login"].append(instance_id)
        return logged_in

    def fake_connect(instance_id, *_a, **_k):
        calls["connect"].append(instance_id)
        return connect_mod.Connection(
            instance_id=instance_id,
            local_port=5599,
            remote_port=5476,
            token="tok",
            url="http://localhost:5599/?token=tok",
            ready=True,
        )

    def fake_register(instance_id, *, name, profile="", region="", remote_port=5476):
        calls["register"].append(instance_id)
        return "reg-1"

    monkeypatch.setattr(login, "is_logged_in", fake_is_logged_in)
    monkeypatch.setattr(connect_mod, "connect", fake_connect)
    monkeypatch.setattr(connect_mod, "register_instance", fake_register)
    # Keep the launch-progress streamer hermetic + instant: no real polling,
    # no stack-event / on-box-log calls (deploy is stubbed to return at once).
    monkeypatch.setattr(wizard, "_sleep", lambda *_a: None)
    monkeypatch.setattr(ec2, "list_stack_events", lambda *_a, **_k: [])
    monkeypatch.setattr(wizard, "_fetch_bootstrap_log", lambda *_a, **_k: [])
    # Resume-path readiness check (a resumed stack may be stopped): default to a
    # running + SSM-online instance so the common resume case is instant; the
    # dedicated stopped/terminated tests override these.
    monkeypatch.setattr(ec2, "_instance_state", lambda *_a, **_k: "running")
    monkeypatch.setattr(ssm, "instance_is_managed", lambda *_a, **_k: True)
    return calls


class TestLaunchSubnetFlag:
    def test_malformed_subnet_fails_before_any_aws_call(self, monkeypatch, capsys):
        # Validation runs before the wizard's first AWS reachability check, so a
        # typo'd --subnet renders one clean line instead of a traceback later.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.iam,
            "reachability_check",
            lambda *a, **k: pytest.fail("must not reach AWS on a malformed subnet id"),
        )

        assert wizard.launch(profile="dev", region="us-west-2", subnet_id="subnet-XYZ") == 1
        assert "subnet_id" in capsys.readouterr().out

    def test_subnet_with_existing_stack_fails_under_yes(self, monkeypatch, capsys):
        # Non-interactive: an explicitly requested pin that would be silently
        # ignored must exit early, not warn-and-proceed into the wrong network.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.iam,
            "reachability_check",
            lambda *a, **k: {
                "reachable": True,
                "account": "1",
                "ec2_reachable": True,
                "cloudformation_reachable": True,
                "ssm_reachable": True,
            },
        )
        monkeypatch.setattr(wizard, "_ensure_session_manager_plugin", lambda **k: True)
        monkeypatch.setattr(wizard, "_select_existing_launch", lambda *a, **k: object())

        rc = wizard.launch(
            profile="dev",
            region="us-west-2",
            subnet_id="subnet-0123456789abcdef0",
            assume_yes=True,
        )
        assert rc == 1
        assert "--subnet cannot apply" in capsys.readouterr().out

    def test_subnet_threads_through_to_deploy(self, monkeypatch):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        captured = {}

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            wizard.iam,
            "reachability_check",
            lambda *a, **k: {
                "reachable": True,
                "account": "1",
                "ec2_reachable": True,
                "cloudformation_reachable": True,
                "ssm_reachable": True,
            },
        )
        monkeypatch.setattr(wizard, "_ensure_session_manager_plugin", lambda **k: True)
        monkeypatch.setattr(wizard, "_select_existing_launch", lambda *a, **k: None)

        def fake_deploy_with_progress(**kwargs):
            captured.update(kwargs)
            return ec2.DeployResult(
                tag=kwargs["tag"],
                stack_name=ec2.stack_name(kwargs["tag"]),
                region="us-west-2",
                instance_id="i-new",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(wizard, "_deploy_with_progress", fake_deploy_with_progress)

        rc = wizard.launch(
            profile="dev",
            region="us-west-2",
            subnet_id="subnet-0123456789abcdef0",
            assume_yes=True,
        )
        assert rc == 0
        assert captured["subnet_id"] == "subnet-0123456789abcdef0"


class TestLaunchResume:
    def test_resumes_existing_saved_stack_without_deploy(self, monkeypatch, capsys):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[str] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(last_tag)
            ),
        )
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 0
        assert calls["login"] and all(
            x == "i-old" for x in calls["login"]
        )  # sign-in + verify both check i-old
        assert calls["connect"] == ["i-old"]
        assert calls["register"] == ["i-old"]
        assert save_calls == []
        assert "Resuming existing CloudFormation stack" in capsys.readouterr().out

    def test_resume_onto_a_wrong_identity_session_registers_but_exits_1(self, monkeypatch, capsys):
        """A resumed instance can hold a valid session for a DIFFERENT identity
        than the pinned target. The wizard already detects it and prints the
        logout recovery; the exit code must say the same thing. The dashboard is
        still opened and the crew registered (the recovery needs both), but a
        launch that did not deliver the identity it was asked for is not 0.
        Contrast: a merely NOT-signed-in box stays a warning and exits 0."""
        from kiro_crew.cloud.login_target import KiroLoginTarget

        target = KiroLoginTarget(
            license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
        )
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch, logged_in=False)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )
        monkeypatch.setattr(
            ec2, "deploy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy"))
        )
        started = []
        monkeypatch.setattr(
            login, "start_device_login", lambda *a, **k: started.append(1) or login.LoginPrompt()
        )

        monkeypatch.setattr(login, "remote_identity_state", lambda *a, **k: "mismatch")
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, login_target=target)
        out = capsys.readouterr().out
        assert rc == 1
        assert calls["connect"] == ["i-old"] and calls["register"] == ["i-old"]
        assert started == []  # a login over a live session is not attempted
        assert "DIFFERENT Kiro identity" in out and "kirocrew cloud logout" in out
        assert "is live on AWS" not in out

        # Same resume, no session at all: a warning, and the launch still succeeds.
        monkeypatch.setattr(login, "remote_identity_state", lambda *a, **k: "absent")
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, login_target=target)
        assert rc == 0
        assert "is live on AWS" in capsys.readouterr().out

    def test_mismatch_verified_by_the_signin_attempt_is_sticky_too(self, monkeypatch, capsys):
        """Both identity probes are transiently unknown, so the sign-in step
        falls through to ``start_device_login`` -- and kiro-cli refuses it over
        a live session for the WRONG identity (``prompt.identity_mismatch``).
        That verdict is recorded like the probe's would have been: the
        operational recheck, still unknown, cannot read it down to an exit-0
        "not signed in" warning."""
        from kiro_crew.cloud.login_target import KiroLoginTarget

        target = KiroLoginTarget(
            license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
        )
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch, logged_in=False)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )
        monkeypatch.setattr(login, "remote_identity_state", lambda *a, **k: "unknown")
        started: list[int] = []

        def refused_over_wrong_session(*a, **k):
            started.append(1)
            return login.LoginPrompt(identity_mismatch=True, error="already logged in")

        monkeypatch.setattr(login, "start_device_login", refused_over_wrong_session)
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, login_target=target)
        out = capsys.readouterr().out
        assert rc == 1
        assert started  # the sign-in step did try, and kiro-cli refused
        assert calls["connect"] == ["i-old"] and calls["register"] == ["i-old"]
        assert "different Kiro identity" in out and "kirocrew cloud logout" in out
        assert "is live on AWS" not in out

    def test_verified_mismatch_survives_a_transient_recheck_failure(self, monkeypatch, capsys):
        """The sign-in step verifies a wrong-identity session; the operational
        recheck then hits a transient SSM fault and can only say "could not
        check". That does not un-verify the mismatch: the launch still exits 1
        with the logout recovery, instead of degrading to the exit-0 "not signed
        in" warning under the wrong identity."""
        from kiro_crew.cloud.aws import AWSError
        from kiro_crew.cloud.login_target import KiroLoginTarget

        target = KiroLoginTarget(
            license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
        )
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch, logged_in=False)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )
        monkeypatch.setattr(
            login,
            "start_device_login",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no login")),
        )
        probes: list[int] = []

        def flaky_identity_state(*a, **k):
            probes.append(1)
            if len(probes) == 1:
                return "mismatch"  # the sign-in step's verified verdict
            raise AWSError("SSM SendCommand throttled")  # the recheck cannot look

        monkeypatch.setattr(login, "remote_identity_state", flaky_identity_state)
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, login_target=target)
        out = capsys.readouterr().out
        assert len(probes) == 2
        assert rc == 1
        assert calls["connect"] == ["i-old"] and calls["register"] == ["i-old"]
        assert "DIFFERENT Kiro identity" in out and "kirocrew cloud logout" in out
        assert "is live on AWS" not in out

    def test_builder_id_launch_onto_an_identity_center_session_is_a_mismatch(
        self, monkeypatch, capsys
    ):
        """The default (Builder ID) target is checked for a wrong-identity
        session like any other: a resumed instance holding an Identity Center
        session is a mismatch for it (wrong license, wrong models), so the
        launch exits 1 with the flagless recovery -- including when the human
        declines the interactive re-login, the path that otherwise reads
        as an exit-0 "not signed in" warning."""
        from kiro_crew.cloud.login_target import KiroLoginTarget

        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch, logged_in=False)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )
        monkeypatch.setattr(
            login,
            "start_device_login",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no login")),
        )
        targets: list[KiroLoginTarget] = []

        def idc_session(*a, **k):
            targets.append(k["target"])
            return "mismatch"

        monkeypatch.setattr(login, "remote_identity_state", idc_session)
        # Interactive: the human keeps the existing instance, then declines the
        # re-login prompt.
        monkeypatch.setattr(wizard.ui, "choose", lambda *a, **k: 0)
        monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: False)
        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=False, login_target=None)
        out = capsys.readouterr().out
        assert rc == 1
        assert targets and all(t.is_default for t in targets)
        assert calls["connect"] == ["i-old"] and calls["register"] == ["i-old"]
        assert "DIFFERENT Kiro identity than Builder ID" in out
        recovery = [ln for ln in out.splitlines() if "`kirocrew cloud logout`, then " in ln]
        assert recovery and all(
            ln.rstrip().endswith("`kirocrew cloud login`") and "--identity-provider" not in ln
            for ln in recovery
        )
        assert "is live on AWS" not in out

    def test_stale_saved_tag_failed_stack_is_not_resumed(self, monkeypatch):
        # A new launch saves last_tag before provisioning, so the saved tag may
        # point at a FAILED / no-instance stack: `launch` must NOT resume it
        # (aborting at "instance not ready") — it must fall through to a fresh
        # new launch.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-broken")
        _patch_post_launch(monkeypatch)

        # The stale saved stack exists but is ROLLBACK_COMPLETE with no instance.
        def fake_describe(tag, *_a, **_k):
            if tag == "kc-broken":
                return {
                    "tag": tag,
                    "exists": True,
                    "stack_name": "kirocrew-kc-broken",
                    "stack_status": "ROLLBACK_COMPLETE",
                    "instance_id": "",
                    "region": "us-west-2",
                }
            # the fresh launch's post-deploy describe
            return {
                "tag": tag,
                "exists": True,
                "stack_name": f"kirocrew-{tag}",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-fresh",
                "region": "us-west-2",
                "instance_state": "running",
            }

        monkeypatch.setattr(ec2, "describe", fake_describe)
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])  # no other stacks
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-fresh")
        deployed: list[str] = []

        def fake_deploy(**kw):
            deployed.append(kw["tag"])
            return ec2.DeployResult(
                tag=kw["tag"],
                stack_name=f"kirocrew-{kw['tag']}",
                region="us-west-2",
                instance_id="i-fresh",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(wizard, "_deploy_with_progress", lambda **kw: fake_deploy(**kw))

        wizard.launch(profile="dev", region="us-west-2", assume_yes=True)
        # A brand-new launch happened (broken tag ignored), not a resume.
        assert deployed == ["kc-fresh"]

    def test_resume_starts_a_stopped_instance_before_ssm(self, monkeypatch, capsys):
        # `launch` after `cloud stop`: the resumed instance is STOPPED, so the
        # wizard must start it and wait for SSM Online before sign-in/tunnel
        # (which are all over SSM and would otherwise fail).
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "region": "us-west-2",
                "instance_state": "stopped",
            },
        )
        # State says stopped; after start it's running + SSM online.
        monkeypatch.setattr(ec2, "_instance_state", lambda *_a, **_k: "stopped")
        monkeypatch.setattr(ssm, "instance_is_managed", lambda *_a, **_k: True)
        started = {"n": 0}
        monkeypatch.setattr(
            wizard, "aws_start_instance", lambda *a, **k: started.update(n=started["n"] + 1)
        )
        monkeypatch.setattr(
            ec2, "deploy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy"))
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 0
        assert started["n"] == 1  # the stopped instance was started
        assert "starting it" in capsys.readouterr().out

    def test_resume_of_terminated_instance_fails_clean(self, monkeypatch, capsys):
        # A saved stack whose instance is terminated can't be resumed — fail with
        # a clear message pointing at --new, not an opaque SSM error.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "region": "us-west-2",
                "instance_state": "terminated",
            },
        )
        monkeypatch.setattr(ec2, "_instance_state", lambda *_a, **_k: "terminated")
        # SSM must never be polled once we detect terminated.
        monkeypatch.setattr(
            ssm,
            "instance_is_managed",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not poll SSM")),
        )
        monkeypatch.setattr(
            ec2, "deploy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy"))
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        assert "terminated" in capsys.readouterr().out

    def test_hold_tunnel_false_closes_and_returns(self, monkeypatch, capsys):
        # Embedded in `kirocrew setup`, the wizard must NOT block on the
        # tunnel child — it closes it and returns so setup can finish.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        _patch_post_launch(monkeypatch)

        class _LiveProc:
            closed = False

            def poll(self):
                return None

            def wait(self, timeout=None):  # pragma: no cover - must not block
                raise AssertionError("hold_tunnel=False must not wait on the tunnel")

            def terminate(self):
                self.closed = True

            def kill(self):
                self.closed = True

        proc = _LiveProc()

        def fake_connect_with_proc(instance_id, *_a, **_k):
            conn = connect_mod.Connection(
                instance_id=instance_id,
                local_port=5599,
                remote_port=5476,
                token="tok",
                url="http://localhost:5599/?token=tok",
                ready=True,
            )
            conn.process = proc  # type: ignore[assignment]
            return conn

        monkeypatch.setattr(connect_mod, "connect", fake_connect_with_proc)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "public_dns": "",
                "region": "us-west-2",
                "instance_state": "running",
            },
        )

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, hold_tunnel=False)
        assert rc == 0
        assert proc.closed is True
        assert "reopen anytime" in capsys.readouterr().out

    def test_missing_saved_stack_falls_back_to_new_launch(self, monkeypatch):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        deploy_calls: list[str] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "describe", lambda *_a, **_k: {"exists": False})
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])

        def fake_deploy(*, tag, tier, profile, region, **_kw):
            deploy_calls.append(tag)
            return ec2.DeployResult(
                tag=tag,
                stack_name="kirocrew-kc-new",
                region=region,
                instance_id="i-new",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(ec2, "deploy", fake_deploy)

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 0
        assert deploy_calls == ["kc-new"]
        assert calls["login"] and all(
            x == "i-new" for x in calls["login"]
        )  # sign-in + verify both check i-new
        assert save_calls == [("dev", "us-west-2", "kc-new")]

    def test_force_new_ignores_existing_saved_stack(self, monkeypatch):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        deploy_calls: list[str] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not describe")),
        )

        def fake_deploy(*, tag, tier, profile, region, **_kw):
            deploy_calls.append(tag)
            return ec2.DeployResult(
                tag=tag,
                stack_name="kirocrew-kc-new",
                region=region,
                instance_id="i-new",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(ec2, "deploy", fake_deploy)

        assert (
            wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True) == 0
        )
        assert deploy_calls == ["kc-new"]
        assert calls["login"] and all(
            x == "i-new" for x in calls["login"]
        )  # sign-in + verify both check i-new

    def test_failed_new_launch_keeps_the_record_naming_its_stack(self, monkeypatch):
        # The tag is saved before provisioning, so a FAILED launch leaves the record naming
        # the stack it tried to create: `status` and `destroy` find what it left behind.
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "get_stack_failures", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "find_stack", lambda *_a, **_k: None)
        monkeypatch.setattr(ec2, "deploy", lambda **_k: (_ for _ in ()).throw(aws.AWSError("no")))

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        assert save_calls == [("dev", "us-west-2", "kc-new")]

    @pytest.mark.parametrize(
        ("status", "missing_action", "keep", "bills"),
        [
            (
                "ROLLBACK_COMPLETE",
                "iam:PassRole",
                False,
                "It holds no instance, so it does not bill.",
            ),
            ("CREATE_FAILED", "", True, "It may still be billing until you remove it."),
        ],
    )
    def test_a_failed_deploy_names_the_stack_it_left_and_how_to_remove_it(
        self, monkeypatch, capsys, status, missing_action, keep, bills
    ):
        """The line names the tag, Region and profile the stack is in, so it reaches the
        stack even when the launch record could not be written."""
        cfg = LaunchState(profile="", region="us-east-1", last_tag="")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "get_stack_failures", lambda *_a, **_k: [])
        looked_up: list[tuple] = []
        monkeypatch.setattr(
            ec2,
            "find_stack",
            lambda *a, **_k: looked_up.append(a) or {"StackStatus": status},
        )
        err = aws.AWSError("cloudformation deploy failed", missing_action=missing_action)
        monkeypatch.setattr(ec2, "deploy", lambda **_k: (_ for _ in ()).throw(err))

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, keep_on_failure=keep)

        assert rc == 1
        assert looked_up == [("kc-new", "dev", "us-west-2")]
        out = capsys.readouterr().out
        assert f"The stack kirocrew-kc-new is still in your account ({status}). {bills}" in out
        assert "kirocrew cloud destroy --tag kc-new --region us-west-2 --profile dev" in out

    def test_a_record_that_cannot_be_written_does_not_abort_the_launch(self, monkeypatch, capsys):
        """A pointer is never worth failing a launch over.

        A failed write warns and continues: the deploy, sign-in and the dashboard tunnel
        matter more to the operator than a pointer.
        """
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        calls = _patch_post_launch(monkeypatch)
        warnings: list[str] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(lambda cls, **k: (_ for _ in ()).throw(OSError("read-only file system"))),
        )
        monkeypatch.setattr(wizard.ui, "warn", lambda msg, *a, **k: warnings.append(str(msg)))
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "deploy", lambda **_k: ec2.DeployResult(True, "i-new", "", "", ""))

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True)

        assert rc == 0, "a pointer that could not be saved failed a launch that succeeded"
        # The steps AFTER the record still ran: that is the whole point.
        assert calls["login"], "sign-in was skipped because the pointer could not be saved"
        # And it is not silent: the operator is told, and told the flags that reach the stack.
        assert any("launch record" in w for w in warnings), warnings
        assert "pass --tag kc-new --region us-west-2 --profile dev" in capsys.readouterr().out

    def test_successful_new_launch_persists_tag(self, monkeypatch):
        # On a SUCCESSFUL launch the tag IS persisted (so a later `launch`/status
        # can resume it).
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])

        def fake_deploy(**_k):
            return ec2.DeployResult(
                tag="kc-new",
                stack_name="kirocrew-kc-new",
                region="us-west-2",
                instance_id="i-abc",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(wizard, "_deploy_with_progress", lambda **_k: fake_deploy())

        wizard.launch(profile="dev", region="us-west-2", assume_yes=True)
        assert ("dev", "us-west-2", "kc-new") in save_calls

    def test_yes_keeps_no_stack_this_installation_has_no_record_of(self, monkeypatch, capsys):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(
            ec2,
            "list_stacks",
            lambda *_a, **_k: [
                {
                    "tag": "kc-found",
                    "stack_name": "kirocrew-kc-found",
                    "stack_status": "CREATE_COMPLETE",
                }
            ],
        )
        monkeypatch.setattr(ec2, "live_instance_tags", lambda *_a, **_k: {"kc-found"})
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        out = capsys.readouterr().out
        assert "this installation has no record of (kc-found)" in out
        assert "kirocrew cloud launch --new" in out
        assert calls["login"] == []
        assert save_calls == []

    def test_a_found_stack_is_not_the_default(self, monkeypatch):
        defaults: list[int] = []

        def fake_choose(title, options, default_index=0):
            defaults.append(default_index)
            return default_index

        monkeypatch.setattr(wizard.ui, "choose", fake_choose)
        found = wizard._ExistingLaunch(tag="kc-found", stack_name="kirocrew-kc-found")
        saved = wizard._ExistingLaunch(tag="kc-saved", stack_name="kirocrew-kc-saved", saved=True)

        assert wizard._choose_existing_launch([found]) is None
        assert wizard._choose_existing_launch([found, found]) is None
        assert wizard._choose_existing_launch([saved]) == saved
        assert defaults == [1, 2, 0]

    def test_discovery_offers_only_a_stack_with_a_live_instance(self, monkeypatch, capsys):
        rows = [
            {"tag": "kc-live", "stack_name": "kirocrew-kc-live", "stack_status": "CREATE_COMPLETE"},
            {"tag": "kc-gone", "stack_name": "kirocrew-kc-gone", "stack_status": "CREATE_COMPLETE"},
            {
                "tag": "kc-new",
                "stack_name": "kirocrew-kc-new",
                "stack_status": "CREATE_IN_PROGRESS",
            },
            {"tag": "kc-bad", "stack_name": "kirocrew-kc-bad", "stack_status": "ROLLBACK_COMPLETE"},
        ]
        live_reads: list[str] = []

        def fake_live(*_a, **_k):
            live_reads.append("read")
            return {"kc-live", "kc-new", "kc-bad"}

        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: rows)
        monkeypatch.setattr(ec2, "live_instance_tags", fake_live)

        found = wizard._discover_existing_launches(LaunchState(), "dev", "us-west-2")

        assert live_reads == ["read"]  # one read for all four stacks

        assert found == [wizard._ExistingLaunch("kc-live", "kirocrew-kc-live", "CREATE_COMPLETE")]
        out = capsys.readouterr().out
        assert "Not offered: kirocrew-kc-gone (no live Kiro Crew instance)" in out
        assert "Not offered: kirocrew-kc-new (CREATE_IN_PROGRESS)" in out
        assert "Not offered: kirocrew-kc-bad (ROLLBACK_COMPLETE)" in out
        assert "kirocrew cloud status --tag kc-gone --region us-west-2 --profile dev" in out
        assert "kirocrew-kc-live" not in out

    def test_interactive_existing_stack_can_create_new_installation(self, monkeypatch, capsys):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        deploy_calls: list[str] = []
        choices: list[tuple[str, list[tuple[str, str]]]] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": tag == "kc-old",
                "stack_name": f"kirocrew-{tag}",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-old",
                "region": "us-west-2",
            },
        )

        def fake_choose(title, options, default_index=0):
            choices.append((title, options))
            return 1

        def fake_deploy(*, tag, tier, profile, region, **_kw):
            deploy_calls.append(tag)
            return ec2.DeployResult(
                tag=tag,
                stack_name="kirocrew-kc-new",
                region=region,
                instance_id="i-new",
                status="CREATE_COMPLETE",
            )

        monkeypatch.setattr(wizard.ui, "choose", fake_choose)
        monkeypatch.setattr(ec2, "deploy", fake_deploy)
        # The first deploy names the account and asks, `--size` included.
        monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: True)

        assert wizard.launch(profile="dev", region="us-west-2", size_key="balanced") == 0
        assert deploy_calls == ["kc-new"]
        assert calls["login"] and all(
            x == "i-new" for x in calls["login"]
        )  # sign-in + verify both check i-new
        assert save_calls == [("dev", "us-west-2", "kc-new")]
        assert choices[0][0] == "Existing Kiro Crew cloud deployment"
        assert choices[0][1][0][0] == "Keep and resume existing"
        assert choices[0][1][1][0] == "Create a new installation"
        assert "existing stack is unchanged" in capsys.readouterr().out

    def test_interactive_multiple_discovered_stacks_can_choose_one(self, monkeypatch):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        choices: list[tuple[str, list[tuple[str, str]]]] = []

        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(
                lambda cls, *, profile, region, last_tag, path=None: save_calls.append(
                    (profile, region, last_tag)
                )
            ),
        )
        monkeypatch.setattr(
            ec2,
            "list_stacks",
            lambda *_a, **_k: [
                {"tag": "kc-a", "stack_name": "kirocrew-kc-a"},
                {"tag": "kc-b", "stack_name": "kirocrew-kc-b"},
            ],
        )
        monkeypatch.setattr(ec2, "live_instance_tags", lambda *_a, **_k: {"kc-a", "kc-b"})
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": f"kirocrew-{tag}",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": f"i-{tag}",
                "region": "us-west-2",
            },
        )
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        def fake_choose(title, options, default_index=0):
            choices.append((title, options))
            return 1

        monkeypatch.setattr(wizard.ui, "choose", fake_choose)

        assert wizard.launch(profile="dev", region="us-west-2") == 0
        assert calls["login"] and all(
            x == "i-kc-b" for x in calls["login"]
        )  # sign-in + verify both check i-kc-b
        assert save_calls == [("dev", "us-west-2", "kc-b")]
        assert choices == [
            (
                "Existing Kiro Crew cloud deployments",
                [
                    ("Keep kc-a", "kirocrew-kc-a"),
                    ("Keep kc-b", "kirocrew-kc-b"),
                    ("Create a new installation", "Leaves existing AWS stacks untouched."),
                ],
            )
        ]

    def test_multiple_discovered_stacks_fail_safe(self, monkeypatch, capsys):
        cfg = LaunchState(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            ec2,
            "list_stacks",
            lambda *_a, **_k: [
                {"tag": "kc-a", "stack_name": "kirocrew-kc-a"},
                {"tag": "kc-b", "stack_name": "kirocrew-kc-b"},
            ],
        )
        monkeypatch.setattr(ec2, "live_instance_tags", lambda *_a, **_k: {"kc-a", "kc-b"})
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        out = capsys.readouterr().out
        assert "this installation has no record of (kc-a, kc-b)" in out


class TestSessionManagerPluginPrereq:
    def test_ensure_session_manager_plugin_installs_when_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_refusal", lambda: None)
        monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: True)
        monkeypatch.setattr(
            wizard.ssm,
            "install_session_manager_plugin",
            lambda: wizard.ssm.PluginInstallResult(ok=True, message="installed"),
        )

        assert wizard._ensure_session_manager_plugin() is True
        assert "installed" in capsys.readouterr().out

    def test_ensure_session_manager_plugin_decline(self, monkeypatch):
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_refusal", lambda: None)
        monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: False)

        assert wizard._ensure_session_manager_plugin() is False

    def test_ensure_session_manager_plugin_names_a_refusal_instead_of_offering_install(
        self, monkeypatch, capsys
    ):
        """A found-but-refused plugin is not offered a reinstall."""
        refusal = wizard.ssm.PluginRefusal(
            path="/usr/local/sessionmanagerplugin/bin/session-manager-plugin",
            reason="executable is owned by another user (uid 1501)",
            remedy_command="sudo chown -R root:wheel /usr/local/sessionmanagerplugin",
            code="other_owner",
        )
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_refusal", lambda: refusal)

        def _no_prompt(*_a, **_k):
            raise AssertionError("must not offer to install a refused plugin")

        monkeypatch.setattr(wizard.ui, "confirm", _no_prompt)
        monkeypatch.setattr(wizard.ssm, "install_session_manager_plugin", _no_prompt)

        assert wizard._ensure_session_manager_plugin(assume_yes=True) is False
        out = capsys.readouterr()
        text = out.out + out.err
        assert refusal.path in text and refusal.reason in text


class TestDeployProgressNetworkNote:
    def test_a_fallback_network_is_printed(self, monkeypatch, capsys):
        # `kirocrew cloud launch` prints the VPC it picked by itself.
        def fake_deploy(*, on_network_fallback=None, **kw):
            assert on_network_fallback is not None
            on_network_fallback("No default VPC in us-east-2, so ...")
            return "result"

        monkeypatch.setattr(wizard.ec2, "deploy", fake_deploy)
        monkeypatch.setattr(wizard, "_stream_progress", lambda worker, *a: worker.join())
        out = wizard._deploy_with_progress(
            tag="kc-1", tier=wizard.sizes.default_tier(), profile="dev", region="us-east-2"
        )
        assert out == "result"
        assert "No default VPC in us-east-2, so ..." in capsys.readouterr().out


class TestDeployProgressInterrupt:
    def test_ctrl_c_terminates_the_deploy_child(self, monkeypatch):
        # A Ctrl+C on the main (poll) thread must terminate the detached
        # `aws cloudformation deploy` child, not orphan it for up to 1800s.
        import threading

        sink_called = threading.Event()

        class FakeProc:
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = FakeProc()

        def fake_deploy(*, proc_sink=None, **kw):
            # Hand the child to the sink, signal readiness, then block (the
            # worker is a daemon thread — the test never joins it).
            if proc_sink:
                proc_sink(proc)
            sink_called.set()
            import time as _t

            _t.sleep(30)

        # _stream_progress waits until the sink has run, THEN raises Ctrl+C — so
        # the interrupt deterministically finds the captured proc to terminate.
        def fake_stream(*a, **k):
            sink_called.wait(timeout=5)
            raise KeyboardInterrupt()

        monkeypatch.setattr(wizard.ec2, "deploy", fake_deploy)
        monkeypatch.setattr(wizard, "_stream_progress", fake_stream)
        import pytest

        with pytest.raises(KeyboardInterrupt):
            wizard._deploy_with_progress(
                tag="kc-1", tier=wizard.sizes.default_tier(), profile="dev", region="us-east-1"
            )
        assert proc.terminated is True

    def test_any_exception_terminates_the_deploy_child(self, monkeypatch):
        # Not just Ctrl+C: ANY exception out of the progress poll loop (e.g. a
        # transient error it doesn't swallow) must terminate the detached deploy
        # child and propagate — otherwise the ~1800s `aws cloudformation deploy`
        # child is orphaned and worker.join() is skipped.
        import threading

        sink_called = threading.Event()

        class FakeProc:
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = FakeProc()

        def fake_deploy(*, proc_sink=None, **kw):
            if proc_sink:
                proc_sink(proc)
            sink_called.set()
            import time as _t

            _t.sleep(30)

        def fake_stream(*a, **k):
            sink_called.wait(timeout=5)
            raise RuntimeError("transient poll failure")

        monkeypatch.setattr(wizard.ec2, "deploy", fake_deploy)
        monkeypatch.setattr(wizard, "_stream_progress", fake_stream)
        import pytest

        with pytest.raises(RuntimeError, match="transient poll failure"):
            wizard._deploy_with_progress(
                tag="kc-1", tier=wizard.sizes.default_tier(), profile="dev", region="us-east-1"
            )
        assert proc.terminated is True

    def test_ctrl_c_during_a_launch_names_the_stack_it_may_leave(
        self, tmp_path, monkeypatch, capsys
    ):
        # The deploy child stops, CloudFormation does not: the user is told which stack may
        # still be creating and billing, how to check it and how to remove it, and the
        # record already names it so a bare `kirocrew cloud status` finds it.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])

        def _interrupted(**_k):
            raise KeyboardInterrupt()

        monkeypatch.setattr(wizard, "_deploy_with_progress", _interrupted)

        with pytest.raises(KeyboardInterrupt):
            wizard.launch(profile="dev", region="us-west-2", assume_yes=True)

        out = capsys.readouterr().out
        rate = f"${wizard.sizes.default_tier().approx_usd_per_hr:.2f}/hr"
        assert "AWS may still be creating kirocrew-kc-new" in out and rate in out, out
        scope = "--tag kc-new --region us-west-2 --profile dev"
        assert out.index(f"kirocrew cloud status {scope}") < out.index(
            f"kirocrew cloud destroy {scope}"
        ), out
        assert LaunchState.load().last_tag == "kc-new"


class TestSizeKeyGuard:
    def test_unknown_size_key_returns_clean_error(self, monkeypatch, capsys):
        # The public launch() entrypoint can be called with an arbitrary
        # size_key; an unknown one must yield a clean rc=1 + message, not an
        # uncaught KeyError traceback.
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(LaunchState, "load", classmethod(lambda cls, *a: LaunchState()))
        monkeypatch.setattr(LaunchState, "record", classmethod(lambda cls, **k: None))
        rc = wizard.launch(
            profile="dev", region="us-east-1", size_key="ginormous", assume_yes=True, force_new=True
        )
        assert rc == 1
        assert "unknown size" in capsys.readouterr().out


class TestThePriorPointerIsClearedBeforeANewStackExists:
    """A failed pointer write may leave "no target", never "the wrong target".

    The old pointer is gone before the new tag is written, so no failed write can leave
    `kc-old` saved while a new stack comes up for a bare `cloud destroy` to delete
    irreversibly. Real `launch()` and a real record on disk, since the property is what is
    on disk at two moments.
    """

    @staticmethod
    def _saved(tmp_path, monkeypatch, tag: str = "kc-old") -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="dev", region="us-west-2", last_tag=tag)
        assert LaunchState.load().last_tag == tag, "the fixture did not save a prior pointer"

    @staticmethod
    def _deploy_ok(**_k):
        return ec2.DeployResult(
            tag="kc-new",
            stack_name="kirocrew-kc-new",
            region="us-west-2",
            instance_id="i-new",
            status="CREATE_COMPLETE",
        )

    def test_a_failed_record_write_does_not_leave_the_old_tag(self, tmp_path, monkeypatch):
        """`kc-old` saved, `--new` succeeds, the new tag's write hits ENOSPC.

        The launch still succeeds (rc 0) and the pointer left is EMPTY, not the old stack's.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(wizard, "_deploy_with_progress", self._deploy_ok)
        monkeypatch.setattr(
            wizard.LaunchState,
            "record",
            classmethod(lambda cls, **k: (_ for _ in ()).throw(OSError("No space left on device"))),
        )

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True)

        assert rc == 0, "a pointer that could not be saved failed a launch that succeeded"
        assert LaunchState.load().last_tag == "", "the pointer still names the OLD stack"

    def test_a_clear_that_cannot_be_written_aborts_before_anything_is_created(
        self, tmp_path, monkeypatch
    ):
        """Abort while an abort is still free -- no stack, no bill, and the pointer untouched."""
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(
            wizard.LaunchState,
            "try_clear_tag",
            classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(OSError("Read-only fs"))),
        )

        def _must_not_deploy(**_k):
            raise AssertionError("provisioning started although the pointer was not cleared")

        monkeypatch.setattr(wizard, "_deploy_with_progress", _must_not_deploy)

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True)

        assert rc == 1
        assert LaunchState.load().last_tag == "kc-old", "the pointer changed on an aborted launch"

    def test_a_declined_clear_aborts_before_provisioning(self, tmp_path, monkeypatch):
        """A refusal is not a commit.

        A decline means another launch recorded its tag in between. Writing this launch's
        tag anyway, and failing that write, would leave a pointer to the OTHER launch's
        live stack for a bare `destroy` to delete.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(
            wizard.LaunchState,
            "try_clear_tag",
            classmethod(lambda cls, expect, path=None: (False, "kc-old")),
        )
        deploys: list[str] = []

        def _must_not_deploy(**kw):
            deploys.append(str(kw.get("tag", "")))
            raise AssertionError("provisioning started although the pointer was not cleared")

        monkeypatch.setattr(wizard, "_deploy_with_progress", _must_not_deploy)

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True)

        assert rc == 1
        assert deploys == [], "the deploy was invoked despite the declined clear"
        # The declined clear wrote nothing, so the other launch's pointer is left intact.
        assert LaunchState.load().last_tag == "kc-old"

    def test_a_decline_with_no_pointer_saved_still_launches(self, tmp_path, monkeypatch):
        """The other state a decline covers, and it is the safe one.

        `clear_tag` declines whenever the pointer does not name the tag this launch observed --
        which is also true when there is NO pointer, the state this function exists to reach.
        Aborting on the bool alone would fail launches that are already in the shape the fix
        wants, so the decision uses the tag the compare saw rather than the bool.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(
            wizard.LaunchState,
            "try_clear_tag",
            classmethod(lambda cls, expect, path=None: (False, "")),
        )
        monkeypatch.setattr(wizard, "_deploy_with_progress", self._deploy_ok)

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True)

        assert rc == 0
        assert LaunchState.load().last_tag == "kc-new"

    def test_the_new_tag_is_on_disk_when_provisioning_starts(self, tmp_path, monkeypatch):
        """The ordering, observed from inside the deploy rather than inferred from the source.

        Writing it after the deploy would still end with `kc-new` saved, while leaving the
        window an interrupted launch falls into: a new stack coming up that no record names.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        seen = {}

        def _deploy_watching(**kw):
            seen["tag_on_disk"] = LaunchState.load().last_tag
            return self._deploy_ok(**kw)

        monkeypatch.setattr(wizard, "_deploy_with_progress", _deploy_watching)

        rc = wizard.launch(profile="dev", region="us-west-2", assume_yes=True, force_new=True)

        assert rc == 0
        assert seen["tag_on_disk"] == "kc-new", seen
        assert LaunchState.load().last_tag == "kc-new"

    def test_a_rejected_profile_never_replaces_the_saved_record(
        self, tmp_path, monkeypatch, capsys
    ):
        """`--new` skips discovery, so only an up-front check keeps `user@site` off disk.

        Saved, it would be the default every bare `cloud list`/`status`/`destroy` refuses.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        deploys: list[str] = []

        def _deploy(**kw):
            deploys.append(kw["profile"])
            return self._deploy_ok(**kw)

        monkeypatch.setattr(wizard, "_deploy_with_progress", _deploy)

        rc = wizard.launch(profile="user@site", region="us-west-2", assume_yes=True, force_new=True)

        assert LaunchState.load() == LaunchState(
            profile="dev", region="us-west-2", last_tag="kc-old"
        )
        assert rc == 1
        assert deploys == [], "provisioning started with a rejected profile"
        assert "profile: invalid format" in capsys.readouterr().out

    def test_resuming_the_saved_stack_does_not_clear_its_pointer(self, tmp_path, monkeypatch):
        """Only a NEW stack invalidates the old pointer.

        On a resume the tag does not change, so the saved pointer is still correct and clearing
        it would throw away a live instance's pointer for nothing.
        """
        self._saved(tmp_path, monkeypatch)
        _patch_post_launch(monkeypatch)
        cleared: list[str] = []
        monkeypatch.setattr(
            wizard.LaunchState,
            "clear_tag",
            classmethod(lambda cls, expect, path=None: cleared.append(expect) or True),
        )
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda *_a, **_k: {
                "exists": True,
                "instance_id": "i-old",
                "stack_name": "kirocrew-kc-old",
                "stack_status": "CREATE_COMPLETE",
                "region": "us-west-2",
            },
        )
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])

        def _must_not_deploy(**_k):
            raise AssertionError("a resume provisioned a new stack")

        monkeypatch.setattr(wizard, "_deploy_with_progress", _must_not_deploy)

        wizard.launch(profile="dev", region="us-west-2", assume_yes=True)

        assert cleared == [], cleared


class TestFirstDeployConfirmsAccount:
    """A first stack deploy names the AWS account and profile, and asks first."""

    def _setup(self, monkeypatch):
        cfg = LaunchState(profile="", region="us-west-2", last_tag="")
        monkeypatch.setattr(wizard.LaunchState, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.LaunchState, "record", classmethod(lambda cls, **k: None))
        monkeypatch.setattr(
            wizard.iam,
            "reachability_check",
            lambda *a, **k: {
                "reachable": True,
                "account": "111122223333",
                "arn": "arn:aws:sts::111122223333:assumed-role/Admin/me",
                "ec2_reachable": True,
                "cloudformation_reachable": True,
                "ssm_reachable": True,
            },
        )
        monkeypatch.setattr(wizard, "_ensure_session_manager_plugin", lambda **k: True)
        monkeypatch.setattr(wizard, "_select_existing_launch", lambda *a, **k: None)
        deploys: list[dict] = []

        def fake_deploy(**kwargs):
            deploys.append(kwargs)
            raise AssertionError("stop after deploy")

        monkeypatch.setattr(wizard, "_deploy_with_progress", fake_deploy)
        prompts: list[str] = []
        return deploys, prompts

    def _decline(self, prompts):
        def fake_confirm(text, default=True):
            prompts.append(text)
            return False

        return fake_confirm

    def test_size_flag_still_asks_and_decline_deploys_nothing(self, monkeypatch, capsys):
        deploys, prompts = self._setup(monkeypatch)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
        monkeypatch.setattr(wizard.ui, "confirm", self._decline(prompts))

        rc = wizard.launch(profile="prod", region="us-west-2", size_key="light")

        assert rc == 0
        assert deploys == []
        assert len(prompts) == 1
        assert "111122223333" in prompts[0] and "profile prod" in prompts[0]
        assert "nothing was created" in capsys.readouterr().out

    @pytest.mark.parametrize("tty", [False, True])
    def test_end_of_input_is_no_and_deploys_nothing(self, monkeypatch, capsys, tty):
        deploys, _ = self._setup(monkeypatch)
        stdin = io.StringIO("")  # `cloud launch < /dev/null`, or Ctrl-D at a terminal
        monkeypatch.setattr(stdin, "isatty", lambda: tty)
        monkeypatch.setattr("sys.stdin", stdin)

        rc = wizard.launch(profile="prod", region="us-west-2", size_key="light")

        assert rc == 0
        assert deploys == []
        out = capsys.readouterr().out
        assert "nothing was created" in out
        assert ("re-run the same command with --yes" in out) is not tty

    def test_prompt_names_profile_from_aws_profile_env(self, monkeypatch, capsys):
        deploys, prompts = self._setup(monkeypatch)
        monkeypatch.setenv("AWS_PROFILE", "work-prod")
        monkeypatch.setattr(wizard.ui, "choose", lambda *a, **k: 0)
        monkeypatch.setattr(wizard.ui, "confirm", self._decline(prompts))

        assert wizard.launch(profile="", region="us-west-2") == 0
        assert deploys == []
        assert len(prompts) == 1
        assert "111122223333" in prompts[0]
        assert "work-prod" in prompts[0] and "AWS_PROFILE" in prompts[0]
        assert "assumed-role/Admin/me" in capsys.readouterr().out

    def test_prompt_names_default_profile(self, monkeypatch):
        deploys, prompts = self._setup(monkeypatch)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
        monkeypatch.setattr(wizard.ui, "choose", lambda *a, **k: 0)
        monkeypatch.setattr(wizard.ui, "confirm", self._decline(prompts))

        assert wizard.launch(profile="", region="us-west-2") == 0
        assert len(prompts) == 1 and "default profile" in prompts[0]

    def test_yes_skips_the_prompt(self, monkeypatch):
        deploys, prompts = self._setup(monkeypatch)
        monkeypatch.setattr(
            wizard.ui, "confirm", lambda *a, **k: pytest.fail("--yes must not prompt")
        )

        with pytest.raises(AssertionError, match="stop after deploy"):
            wizard.launch(profile="prod", region="us-west-2", size_key="light", assume_yes=True)
        assert len(deploys) == 1
