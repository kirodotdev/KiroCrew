"""HTTP-handler tests for the cloud provisioning routes (handlers_cloud.py).

No AWS and no live gateway: requests are built with ``make_mocked_request``, the
launch engine is a fake, the store is tmp-rooted, and the AWS engine functions
(ec2/iam/ssm) are monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard import handlers_cloud as hc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _posix_host(monkeypatch):
    """Pin a POSIX platform for every test in this module.

    These routes are POSIX-only by design, so on Windows ``_guard()`` answers
    400 before any handler logic runs and every success-path assertion below
    would fail for a reason that has nothing to do with the code under test.
    Pinning the platform keeps the suite host-independent; the rejection itself
    is asserted by ``test_windows_rejected``, whose own monkeypatch overrides
    this fixture.
    """
    monkeypatch.setattr(hc.sys, "platform", "linux")


class FakeHandle:
    already_logged_in = True
    url = ""
    code = ""
    ports: list = []

    def wait(self, cancel: threading.Event) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeEngine:
    def preflight(self, profile, region):
        pass

    def provision(self, *, tag, size_key, profile, region):
        return "i-0abc123456789def0"

    def begin_signin(self, *, instance_id, profile, region):
        return FakeHandle()

    def register(self, *, instance_id, tag, profile, region):
        pass

    def teardown(self, *, tag, profile, region):
        pass


def _state(tmp_path):
    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_engine=FakeEngine(),
        cloud_launch_store=lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )


def _req(method, path, *, state, user=True, slack=False, body=None, match_info=None):
    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": "slack:x"} if slack else {}
    req = make_mocked_request(method, path, headers=headers, app=app, match_info=match_info or {})
    if user:
        # token_auth sets user + app together; a dashboard (non-app) owner token has
        # the owner subject and an empty app. `user=<str>` lets a test pose as a
        # different, non-owner subject (an allowed Slack user's !dashboard token).
        req["user"] = user if isinstance(user, str) else "owner-1"
        req["app"] = ""
    if body is not None:

        async def _json():
            return body

        req.json = _json  # type: ignore[assignment]
    return req


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


class TestGuards:
    async def test_slack_origin_rejected(self, tmp_path):
        resp = await hc.api_cloud_iam_policy(
            _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path), slack=True)
        )
        assert resp.status == 403

    async def test_unauthenticated_rejected(self, tmp_path):
        resp = await hc.api_cloud_iam_policy(
            _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path), user=False)
        )
        assert resp.status == 401

    async def test_windows_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc.sys, "platform", "win32")
        resp = await hc.api_cloud_iam_policy(
            _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path))
        )
        assert resp.status == 400
        assert "POSIX" in _body(resp)["error"]

    async def test_app_token_rejected(self, tmp_path):
        """token_auth sets request["app"] alongside request["user"], so checking
        "user" alone would let an app that declares /api/cloud in its manifest
        create, stop and terminate billable AWS resources unattended."""
        req = _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path))
        req["app"] = "some-app"
        resp = await hc.api_cloud_iam_policy(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_owner_only"

    async def test_non_owner_dashboard_user_rejected(self, tmp_path):
        """A dashboard session token is minted for every allowed Slack user
        (`!dashboard`): request["user"] is set to THEIR subject with an empty app,
        so the old app-only check cleared them into the owner's billable AWS control
        plane. Only the configured owner may pass."""
        req = _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path), user="allowed-slack-user")
        resp = await hc.api_cloud_iam_policy(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_owner_only"


class TestReadEndpoints:
    async def test_iam_policy_returns_document(self, tmp_path):
        resp = await hc.api_cloud_iam_policy(
            _req("GET", "/api/cloud/iam-policy", state=_state(tmp_path))
        )
        assert resp.status == 200
        doc = json.loads(_body(resp)["policy"])
        assert doc["Version"] == "2012-10-17"
        assert "Statement" in doc

    async def test_preflight_merges_plugin_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            hc.iam,
            "reachability_check",
            lambda p, r: {"reachable": True, "account": "123", "ec2_reachable": True},
        )
        monkeypatch.setattr(hc.ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(hc.ssm, "session_manager_plugin_install_command", lambda: "sudo dnf x")
        resp = await hc.api_cloud_preflight(
            _req("GET", "/api/cloud/preflight?profile=dev", state=_state(tmp_path))
        )
        body = _body(resp)
        assert body["reachable"] is True
        assert body["account"] == "123"
        assert body["session_manager_plugin"] is False
        # The remedy is resolved server-side, because only this process knows which
        # OS the check ran on.
        assert body["session_manager_plugin_command"] == "sudo dnf x"

    async def test_preflight_omits_the_remedy_once_the_plugin_is_present(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(hc.iam, "reachability_check", lambda p, r: {"reachable": True})
        monkeypatch.setattr(hc.ssm, "session_manager_plugin_installed", lambda: True)
        resp = await hc.api_cloud_preflight(
            _req("GET", "/api/cloud/preflight", state=_state(tmp_path))
        )
        assert _body(resp)["session_manager_plugin_command"] == ""


class TestPluginInstallCommand:
    """The remedy must match the GATEWAY's platform. A hardcoded Homebrew line is
    wrong for every Linux host — and Linux is the common case for a remote crew."""

    def _cmd(self, monkeypatch, system, arch, present):
        from kiro_crew.cloud import ssm

        monkeypatch.setattr(ssm.platform, "system", lambda: system)
        monkeypatch.setattr(ssm, "_normalized_arch", lambda: arch)
        monkeypatch.setattr(
            ssm.shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None
        )
        return ssm.session_manager_plugin_install_command()

    def test_macos_prefers_the_public_homebrew_cask_when_brew_exists(self, monkeypatch):
        assert self._cmd(monkeypatch, "Darwin", "arm64", {"brew"}) == (
            "brew install --cask session-manager-plugin"
        )

    def test_macos_without_brew_falls_back_to_aws_own_package(self, monkeypatch):
        cmd = self._cmd(monkeypatch, "Darwin", "arm64", set())
        assert "brew" not in cmd
        assert "mac_arm64/session-manager-plugin.pkg" in cmd
        assert "installer -pkg" in cmd
        # Downloaded into a private mktemp dir, never a predictable /tmp path a local
        # user could preplant/swap before `sudo installer` runs it as root.
        assert "mktemp -d" in cmd
        assert "/tmp/session-manager-plugin" not in cmd

    def test_macos_intel_gets_the_intel_package(self, monkeypatch):
        assert "/mac/session-manager-plugin.pkg" in self._cmd(
            monkeypatch, "Darwin", "x86_64", set()
        )

    def test_debian_family_gets_a_deb(self, monkeypatch):
        cmd = self._cmd(monkeypatch, "Linux", "x86_64", {"dpkg"})
        assert "ubuntu_64bit/session-manager-plugin.deb" in cmd
        assert "dpkg -i" in cmd
        assert "brew" not in cmd
        assert "mktemp -d" in cmd
        assert "/tmp/session-manager-plugin" not in cmd

    def test_rpm_family_installs_straight_from_the_url(self, monkeypatch):
        cmd = self._cmd(monkeypatch, "Linux", "arm64", {"rpm", "dnf"})
        assert "linux_arm64/session-manager-plugin.rpm" in cmd
        assert cmd.startswith("sudo dnf install -y")

    def test_an_unsupported_platform_returns_nothing_rather_than_a_wrong_command(
        self, monkeypatch
    ):
        assert self._cmd(monkeypatch, "Windows", "x86_64", set()) == ""


class TestLaunch:
    async def test_create_runs_job_to_done(self, tmp_path):
        state = _state(tmp_path)
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST", "/api/cloud/launch", state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )
        assert resp.status == 202
        job_id = _body(resp)["id"]
        got = await hc.api_cloud_launch_get(
            _req("GET", f"/api/cloud/launch/{job_id}", state=state, match_info={"id": job_id})
        )
        j = _body(got)
        assert j["status"] == lj.DONE
        assert j["instance_id"] == "i-0abc123456789def0"
        lst = await hc.api_cloud_launch_list(_req("GET", "/api/cloud/launch", state=state))
        assert any(x["id"] == job_id for x in _body(lst)["jobs"])

    async def test_create_persists_off_the_event_loop(self, tmp_path):
        # The blocking mkdir/write/replace in create() must run in an executor, not
        # on the loop thread, so a slow disk can't stall the gateway + its heartbeat.
        state = _state(tmp_path)
        loop_tid = threading.get_ident()
        real_create = state.cloud_launch_store.create
        seen: dict = {}

        def _record(**kw):
            seen["tid"] = threading.get_ident()
            return real_create(**kw)

        state.cloud_launch_store.create = _record  # type: ignore[method-assign]
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST", "/api/cloud/launch", state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )
        assert resp.status == 202
        assert seen["tid"] != loop_tid, "create() ran on the event loop thread"

    async def test_create_bad_size_400(self, tmp_path):
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST", "/api/cloud/launch", state=_state(tmp_path),
                body={"profile": "dev", "region": "us-east-1", "size_key": "nope"},
            )
        )
        assert resp.status == 400

    async def test_create_bad_json_400(self, tmp_path):
        req = _req("POST", "/api/cloud/launch", state=_state(tmp_path))

        async def _boom():
            raise ValueError("bad")

        req.json = _boom  # type: ignore[assignment]
        resp = await hc.api_cloud_launch_create(req)
        assert resp.status == 400

    async def test_get_unknown_404(self, tmp_path):
        resp = await hc.api_cloud_launch_get(
            _req("GET", "/api/cloud/launch/deadbeef", state=_state(tmp_path),
                 match_info={"id": "deadbeef"})
        )
        assert resp.status == 404

    async def test_cancel_unknown_404(self, tmp_path):
        resp = await hc.api_cloud_launch_cancel(
            _req("POST", "/api/cloud/launch/deadbeef/cancel", state=_state(tmp_path),
                 match_info={"id": "deadbeef"})
        )
        assert resp.status == 404

    async def test_cancel_sets_event_and_returns_job(self, tmp_path):
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="light")
        ev = threading.Event()
        hc._cancels(state)[job.id] = ev
        resp = await hc.api_cloud_launch_cancel(
            _req("POST", f"/api/cloud/launch/{job.id}/cancel", state=state,
                 match_info={"id": job.id})
        )
        assert resp.status == 200
        assert ev.is_set() is True

    async def test_cancel_after_a_restart_reports_a_terminal_job(self, tmp_path):
        """A job orphaned by a restart is reaped on first store use, so by the
        time cancel runs there is nothing live to cancel — and the response says
        so instead of implying a running launch was stopped."""
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="light")
        job.status = lj.RUNNING
        job.step(lj.STEP_PROVISION).state = lj.STEP_ACTIVE
        state.cloud_launch_store.save(job)
        # A restart is a NEW store over the same directory: the job file survives but
        # the in-memory ownership set does not, which is precisely what lets the reap
        # tell an abandoned job apart from one this process is driving. Reusing the
        # creating store would keep the job owned and never reap it.
        state.cloud_launch_store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
        assert hc._cancels(state).get(job.id) is None  # no worker owns it

        resp = await hc.api_cloud_launch_cancel(
            _req("POST", f"/api/cloud/launch/{job.id}/cancel", state=state,
                 match_info={"id": job.id})
        )

        assert resp.status == 200
        assert _body(resp)["status"] == lj.FAILED  # reaped as interrupted
        stored = state.cloud_launch_store.get(job.id)
        assert stored is not None and stored.terminal

    async def test_cancel_without_a_worker_terminalizes_instead_of_lying(self, tmp_path):
        """The backstop: a non-terminal job with no worker, reached after the
        reap already ran. Setting an event would cancel nothing while we answered
        200, so cancel must move the job to a terminal state itself."""
        state = _state(tmp_path)
        state.cloud_launch_reaped = True  # reap already happened this process
        job = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="light")
        job.status = lj.RUNNING
        job.step(lj.STEP_PROVISION).state = lj.STEP_ACTIVE
        state.cloud_launch_store.save(job)
        assert hc._cancels(state).get(job.id) is None

        resp = await hc.api_cloud_launch_cancel(
            _req("POST", f"/api/cloud/launch/{job.id}/cancel", state=state,
                 match_info={"id": job.id})
        )

        assert resp.status == 200
        assert _body(resp)["status"] == lj.CANCELLED
        stored = state.cloud_launch_store.get(job.id)
        assert stored is not None and stored.terminal
        assert stored.step(lj.STEP_PROVISION).state == lj.STEP_FAILED


class TestLaunchConcurrency:
    async def test_a_second_launch_while_one_runs_is_refused(self, tmp_path):
        """Two jobs means two tags and two CloudFormation stacks — two billed
        instances that the caller cannot undo. A retry must be refused."""
        state = _state(tmp_path)
        running = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        running.status = lj.RUNNING
        state.cloud_launch_store.save(running)
        state.cloud_launch_store.adopt(running.id)  # a worker here owns it

        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state,
                 body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"})
        )

        assert resp.status == 409
        body = _body(resp)
        assert body["code"] == "launch_already_running"
        assert body["job"]["id"] == running.id
        # and no second job was written
        assert len(state.cloud_launch_store.list()) == 1

    async def test_two_concurrent_launches_still_yield_one_job(self, tmp_path):
        """The guard has to hold across its own await: two POSTs arriving together
        would otherwise both observe "no active job" and provision two
        CloudFormation stacks — two billed instances with no way to undo."""
        release = threading.Event()

        class BlockingEngine(FakeEngine):
            def preflight(self, profile, region):
                release.wait(timeout=5)  # hold the worker inside the first launch

        state = _state(tmp_path)
        state.cloud_launch_sync = False  # real worker thread, so the job stays active
        state.cloud_launch_engine = BlockingEngine()

        def _post():
            return hc.api_cloud_launch_create(
                _req("POST", "/api/cloud/launch", state=state,
                     body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"})
            )

        try:
            r1, r2 = await asyncio.gather(_post(), _post())
            assert sorted([r1.status, r2.status]) == [202, 409]
            assert len(state.cloud_launch_store.list()) == 1
        finally:
            release.set()

    async def test_a_launch_is_allowed_once_the_previous_one_is_terminal(self, tmp_path):
        state = _state(tmp_path)
        old = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        old.status = lj.DONE
        state.cloud_launch_store.save(old)

        resp = await hc.api_cloud_launch_create(
            _req("POST", "/api/cloud/launch", state=state,
                 body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"})
        )

        assert resp.status == 202
        assert len(state.cloud_launch_store.list()) == 2


class TestSignin:
    async def test_signin_pending_returns_prompt(self, tmp_path):
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        job.status = lj.AWAITING_SIGNIN
        job.signin = lj.SigninPrompt(url="https://x/verify", code="BQTZ-XKFD", ports=[54123])
        state.cloud_launch_store.save(job)
        # A real in-flight job is claimed by its worker (_start_worker calls
        # adopt), which is what keeps the orphan reaper off it.
        state.cloud_launch_store.adopt(job.id)
        resp = await hc.api_cloud_launch_signin(
            _req("POST", f"/api/cloud/launch/{job.id}/signin", state=state,
                 match_info={"id": job.id})
        )
        assert resp.status == 200
        assert _body(resp)["signin"]["code"] == "BQTZ-XKFD"

    async def test_signin_none_pending_409(self, tmp_path):
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        resp = await hc.api_cloud_launch_signin(
            _req("POST", f"/api/cloud/launch/{job.id}/signin", state=state,
                 match_info={"id": job.id})
        )
        assert resp.status == 409


class TestInstanceMutations:
    async def test_stop_start_destroy_dispatch(self, tmp_path, monkeypatch):
        seen = {}

        def _mk(name):
            def _fn(tag, p, r, **kw):
                seen[name] = {"tag": tag, "profile": p, "region": r, "kw": kw}
                return {"action": name}

            return _fn

        monkeypatch.setattr(hc.ec2, "stop", _mk("stop"))
        monkeypatch.setattr(hc.ec2, "start", _mk("start"))
        monkeypatch.setattr(hc.ec2, "destroy", _mk("destroy"))
        # destroy also tears down local bookkeeping; stub both so the test never
        # reaches AWS (delete_source would otherwise shell out to the CLI).
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})
        monkeypatch.setattr(hc.connect_mod, "unregister_instance", lambda *a, **k: True)
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(hc.ec2, "describe", lambda *a, **k: {"instance_id": "i-0abc"})

        st = _req("POST", "/api/cloud/kc-3f9a/stop?profile=dev&region=us-east-1",
                  state=_state(tmp_path), match_info={"tag": "kc-3f9a"})
        r1 = await hc.api_cloud_stop(st)
        assert r1.status == 200
        assert seen["stop"] == {"tag": "kc-3f9a", "profile": "dev", "region": "us-east-1", "kw": {}}

        r2 = await hc.api_cloud_start(
            _req("POST", "/api/cloud/kc-7b21/start", state=_state(tmp_path),
                 match_info={"tag": "kc-7b21"})
        )
        assert r2.status == 200
        assert seen["start"]["tag"] == "kc-7b21"

        r3 = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-7b21", state=_state(tmp_path),
                 match_info={"tag": "kc-7b21"})
        )
        assert r3.status == 200
        assert seen["destroy"]["tag"] == "kc-7b21"
        assert seen["destroy"]["kw"].get("wait") is False

    async def test_destroy_cleans_up_local_state_after_deletion_confirms(
        self, tmp_path, monkeypatch
    ):
        """Confirmed deletion is what licenses dropping local state: without the
        cleanup the crew stays in the Instances registry and keeps showing in the
        crew list, where connecting to it fails because the box is gone."""
        calls = {}

        def _unregister(iid):
            calls["unregistered"] = iid
            return True

        def _delete_source(tag, p, r):
            calls["source"] = tag
            return {"removed": True}

        monkeypatch.setattr(
            hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-0abc123456789def0"}
        )
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda tag, p, r: True)
        monkeypatch.setattr(hc.connect_mod, "unregister_instance", _unregister)
        monkeypatch.setattr(hc.source_mod, "delete_source", _delete_source)

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-3f9a?instance_id=i-0abc123456789def0",
                 state=_state(tmp_path), match_info={"tag": "kc-3f9a"})
        )

        assert resp.status == 200
        assert _body(resp)["cleanup"] == "pending"  # the request only acks the delete
        assert calls["unregistered"] == "i-0abc123456789def0"
        assert calls["source"] == "kc-3f9a"

    async def test_destroy_keeps_local_state_when_deletion_does_not_confirm(
        self, tmp_path, monkeypatch
    ):
        """DELETE_FAILED means the crew is still there. Dropping its registration
        and source archive then would strand a live, billing instance the user can
        no longer see in the dashboard — so both must survive."""
        calls = {}
        monkeypatch.setattr(hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-0abc"})
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda tag, p, r: False)
        monkeypatch.setattr(
            hc.connect_mod, "unregister_instance",
            lambda iid: calls.setdefault("unregistered", True),
        )
        monkeypatch.setattr(
            hc.source_mod, "delete_source",
            lambda *a, **k: calls.setdefault("source", True) or {"removed": True},
        )

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-3f9a?instance_id=i-0abc", state=_state(tmp_path),
                 match_info={"tag": "kc-3f9a"})
        )

        assert resp.status == 200
        assert calls == {}, "local state must survive an unconfirmed deletion"

    async def test_destroy_resolves_the_instance_id_from_the_tag_when_omitted(
        self, tmp_path, monkeypatch
    ):
        """instance_id is an optional query param, so a caller that omits it would
        silently skip the unregister and leave a deleted crew listed. The route
        resolves it from the stack itself — before the delete, since the outputs
        are unreadable once the stack is gone."""
        calls = {}
        monkeypatch.setattr(
            hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-resolved123"}
        )
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(
            hc.connect_mod, "unregister_instance",
            lambda iid: calls.setdefault("unregistered", iid) is None or True,
        )
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-3f9a", state=_state(tmp_path),
                 match_info={"tag": "kc-3f9a"})  # no instance_id
        )

        assert resp.status == 200
        assert calls["unregistered"] == "i-resolved123"

    async def test_invalid_cloud_parameter_is_a_coded_400_not_a_500(
        self, tmp_path, monkeypatch
    ):
        """ec2.* validates tag/profile/region and raises ValidationError, which is
        NOT an AWSError — without its own arm a malformed tag becomes a 500."""
        from kiro_crew.validation import ValidationError

        def _boom(tag, p, r, **kw):
            raise ValidationError("tag", "required")

        monkeypatch.setattr(hc.ec2, "stop", _boom)

        resp = await hc.api_cloud_stop(
            _req("POST", "/api/cloud/%20/stop", state=_state(tmp_path),
                 match_info={"tag": " "})
        )

        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_cloud_parameter"

    async def test_a_mismatched_instance_id_query_cannot_unregister_another_crew(
        self, tmp_path, monkeypatch
    ):
        """`unregister_instance` matches its needle against every registered box with
        no cross-check against the tag, so honouring a caller-supplied id would let a
        mismatched value drop a still-living crew's registration. The id is derived
        from the stack being deleted; the query value is ignored."""
        calls = {}
        monkeypatch.setattr(hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-mine"})
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(
            hc.connect_mod, "unregister_instance",
            lambda iid: calls.setdefault("unregistered", iid) is None or True,
        )
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-3f9a?instance_id=i-someone-elses",
                 state=_state(tmp_path), match_info={"tag": "kc-3f9a"})
        )

        assert resp.status == 200
        assert calls["unregistered"] == "i-mine"
        assert calls["unregistered"] != "i-someone-elses"

    async def test_a_failed_id_lookup_still_deletes_the_stack(self, tmp_path, monkeypatch):
        """The id lookup shells out to AWS. If it throws — including non-AWSError
        types like a sandbox/exec failure — the delete must still go through, or the
        user is stranded with a crew they cannot remove."""
        def _explode(*a, **k):
            raise RuntimeError("no sandbox backend available")

        monkeypatch.setattr(hc.ec2, "describe", _explode)
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(hc.connect_mod, "unregister_instance", lambda iid: True)
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-9", state=_state(tmp_path),
                 match_info={"tag": "kc-9"})  # no instance_id -> forces the lookup
        )

        assert resp.status == 200

    async def test_a_retry_after_an_interrupted_teardown_still_clears_the_row(
        self, tmp_path, monkeypatch
    ):
        """A restart kills the teardown watcher mid-wait, so the stack goes but the
        registry row stays. On the retry `describe` can no longer answer — the stack is
        gone — so without the launch-job fallback the retry would delete nothing, resolve
        no id, skip the unregister again, and the row could never be cleared here."""
        calls = {}
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(profile="", region="us-east-1", size_key="balanced")
        job.tag = "kc-3f9a"
        job.instance_id = "i-fromjob"
        state.cloud_launch_store.save(job)

        def _gone(*a, **k):
            raise hc.AWSError("Stack with id kirocrew-kc-3f9a does not exist")

        monkeypatch.setattr(hc.ec2, "describe", _gone)
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": True})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(
            hc.connect_mod, "unregister_instance",
            lambda iid: calls.setdefault("unregistered", iid) is None or True,
        )
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-3f9a", state=state, match_info={"tag": "kc-3f9a"})
        )

        assert resp.status == 200
        assert calls["unregistered"] == "i-fromjob"

    async def test_destroy_denied_maps_403(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.aws import CloudActionDenied

        def _boom(tag, p, r, **kw):
            raise CloudActionDenied("nope")

        monkeypatch.setattr(hc.ec2, "describe", lambda *a, **k: {"instance_id": "i-0abc"})
        monkeypatch.setattr(hc.ec2, "destroy", _boom)
        resp = await hc.api_cloud_destroy(
            _req("DELETE", "/api/cloud/kc-1/", state=_state(tmp_path), match_info={"tag": "kc-1"})
        )
        assert resp.status == 403
