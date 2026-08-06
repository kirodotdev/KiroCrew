"""Unit tests for the cloud launch wizard orchestration."""

from __future__ import annotations

from kiro_crew.cloud import aws
from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam, login, ssm, wizard
from kiro_crew.cloud.config import CloudConfig


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


class TestLaunchResume:
    def test_resumes_existing_saved_stack_without_deploy(self, monkeypatch, capsys):
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[str] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig, "save", lambda self, *a: save_calls.append(self.last_tag)
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

    def test_stale_saved_tag_failed_stack_is_not_resumed(self, monkeypatch):
        # Defense in depth for a cloud.json written by an OLD build that saved
        # last_tag before the deploy confirmed: if the saved tag points at a
        # FAILED / no-instance stack, `launch` must NOT resume it (aborting at
        # "instance not ready") — it must fall through to a fresh new launch.
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-broken")
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
        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.CloudConfig, "save", lambda self, *a: None)
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
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.CloudConfig, "save", lambda self, *a: None)
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
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.CloudConfig, "save", lambda self, *a: None)
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
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
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
        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.CloudConfig, "save", lambda self, *a: None)
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
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        deploy_calls: list[str] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
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
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        deploy_calls: list[str] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(wizard.CloudConfig, "save", lambda self, *a: None)
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

    def test_failed_new_launch_does_not_persist_tag(self, monkeypatch):
        # A FAILED first launch must NOT persist last_tag — otherwise cloud.json
        # points at a rolled-back / no-instance stack and the next `launch`
        # resumes it and aborts at "instance not ready" instead of retrying clean.
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
        )
        monkeypatch.setattr(wizard, "_new_tag", lambda: "kc-new")
        monkeypatch.setattr(ec2, "list_stacks", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "get_stack_failures", lambda *_a, **_k: [])
        monkeypatch.setattr(ec2, "deploy", lambda **_k: (_ for _ in ()).throw(aws.AWSError("no")))

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        # NOTHING persisted on failure — the broken tag never reaches cloud.json.
        assert save_calls == []

    def test_successful_new_launch_persists_tag_after_deploy(self, monkeypatch):
        # On a SUCCESSFUL launch the tag IS persisted (so a later `launch`/status
        # can resume it) — but only after the deploy confirms.
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
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

    def test_discovers_single_stack_when_local_tag_missing(self, monkeypatch, capsys):
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
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
        monkeypatch.setattr(
            ec2,
            "describe",
            lambda tag, *_a, **_k: {
                "tag": tag,
                "exists": True,
                "stack_name": "kirocrew-kc-found",
                "stack_status": "CREATE_COMPLETE",
                "instance_id": "i-found",
                "region": "us-west-2",
            },
        )
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 0
        assert calls["login"] and all(
            x == "i-found" for x in calls["login"]
        )  # sign-in + verify both check i-found
        assert save_calls == [("dev", "us-west-2", "kc-found")]
        assert "Resuming existing CloudFormation stack" in capsys.readouterr().out

    def test_interactive_existing_stack_can_create_new_installation(self, monkeypatch, capsys):
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-old")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        deploy_calls: list[str] = []
        choices: list[tuple[str, list[tuple[str, str]]]] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
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

        assert wizard.launch(profile="dev", region="us-west-2", size_key="balanced") == 0
        assert deploy_calls == ["kc-new"]
        assert calls["login"] and all(
            x == "i-new" for x in calls["login"]
        )  # sign-in + verify both check i-new
        assert save_calls == [("dev", "us-west-2", "kc-new")]
        assert choices[0][0] == "Existing KiroCrew cloud deployment"
        assert choices[0][1][0][0] == "Keep and resume existing"
        assert choices[0][1][1][0] == "Create a new installation"
        assert "existing stack is unchanged" in capsys.readouterr().out

    def test_interactive_multiple_discovered_stacks_can_choose_one(self, monkeypatch):
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="")
        calls = _patch_post_launch(monkeypatch)
        save_calls: list[tuple[str, str, str]] = []
        choices: list[tuple[str, list[tuple[str, str]]]] = []

        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            wizard.CloudConfig,
            "save",
            lambda self, *a: save_calls.append((self.profile, self.region, self.last_tag)),
        )
        monkeypatch.setattr(
            ec2,
            "list_stacks",
            lambda *_a, **_k: [
                {"tag": "kc-a", "stack_name": "kirocrew-kc-a"},
                {"tag": "kc-b", "stack_name": "kirocrew-kc-b"},
            ],
        )
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
                "Existing KiroCrew cloud deployments",
                [
                    ("Keep kc-a", "kirocrew-kc-a"),
                    ("Keep kc-b", "kirocrew-kc-b"),
                    ("Create a new installation", "Leaves existing AWS stacks untouched."),
                ],
            )
        ]

    def test_multiple_discovered_stacks_fail_safe(self, monkeypatch, capsys):
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="")
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.CloudConfig, "load", classmethod(lambda cls, *a: cfg))
        monkeypatch.setattr(
            ec2,
            "list_stacks",
            lambda *_a, **_k: [
                {"tag": "kc-a", "stack_name": "kirocrew-kc-a"},
                {"tag": "kc-b", "stack_name": "kirocrew-kc-b"},
            ],
        )
        monkeypatch.setattr(
            ec2,
            "deploy",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
        )

        assert wizard.launch(profile="dev", region="us-west-2", assume_yes=True) == 1
        out = capsys.readouterr().out
        assert "multiple existing KiroCrew cloud stacks found" in out
        assert "kc-a" in out and "kc-b" in out


class TestSessionManagerPluginPrereq:
    def test_ensure_session_manager_plugin_installs_when_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(wizard.ssm, "session_manager_plugin_installed", lambda: False)
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
        monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: False)

        assert wizard._ensure_session_manager_plugin() is False


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


class TestSizeKeyGuard:
    def test_unknown_size_key_returns_clean_error(self, monkeypatch, capsys):
        # The public launch() entrypoint can be called with an arbitrary
        # size_key; an unknown one must yield a clean rc=1 + message, not an
        # uncaught KeyError traceback.
        _patch_post_launch(monkeypatch)
        monkeypatch.setattr(wizard.ec2, "find_stack", lambda *a, **k: None)
        monkeypatch.setattr(CloudConfig, "load", classmethod(lambda cls, *a: CloudConfig()))
        monkeypatch.setattr(CloudConfig, "save", lambda self, *a: None)
        rc = wizard.launch(
            profile="dev", region="us-east-1", size_key="ginormous", assume_yes=True, force_new=True
        )
        assert rc == 1
        assert "unknown size" in capsys.readouterr().out
