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


@pytest.mark.asyncio
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

    async def test_windows_may_still_read_launch_history(self, tmp_path, monkeypatch):
        """The two read-only launch routes answer on Windows; the writes do not.

        The Remote Crew list waits for BOTH the instances and the launch history
        before it renders anything, so POSIX-gating this read replaced the whole
        list — hand-added SSH crews included — with a cloud-provisioning error,
        leaving no way to connect, edit or remove any of them. The route parses a
        local job store and shells to nothing, so there is nothing to gate.
        """
        state = _state(tmp_path)
        monkeypatch.setattr(hc.sys, "platform", "win32")

        lst = await hc.api_cloud_launch_list(_req("GET", "/api/cloud/launch", state=state))
        assert lst.status == 200
        # Empty because THIS store is tmp-rooted and untouched — the route reports
        # whatever is persisted, so it is not asserting "Windows is always empty".
        assert _body(lst)["jobs"] == []

        got = await hc.api_cloud_launch_get(
            _req("GET", "/api/cloud/launch/nope", state=state, match_info={"id": "nope"})
        )
        assert got.status == 404  # reached the store, not the platform gate

        # Provisioning itself stays refused — the deploy engine needs bash/aws.
        created = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )
        assert created.status == 400
        assert _body(created)["code"] == "posix_host_required"

    async def test_windows_launch_read_still_owner_only(self, tmp_path, monkeypatch):
        """Dropping the POSIX gate must not drop the owner gate with it."""
        monkeypatch.setattr(hc.sys, "platform", "win32")
        resp = await hc.api_cloud_launch_list(
            _req("GET", "/api/cloud/launch", state=_state(tmp_path), slack=True)
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_owner_only"

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
        req = _req(
            "GET", "/api/cloud/iam-policy", state=_state(tmp_path), user="allowed-slack-user"
        )
        resp = await hc.api_cloud_iam_policy(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "cloud_owner_only"


@pytest.mark.asyncio
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

    def test_an_unsupported_platform_returns_nothing_rather_than_a_wrong_command(self, monkeypatch):
        assert self._cmd(monkeypatch, "Windows", "x86_64", set()) == ""


@pytest.mark.asyncio
class TestLaunch:
    async def test_create_runs_job_to_done(self, tmp_path):
        state = _state(tmp_path)
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
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
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )
        assert resp.status == 202
        assert seen["tid"] != loop_tid, "create() ran on the event loop thread"

    async def test_create_bad_size_400(self, tmp_path):
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=_state(tmp_path),
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
            _req(
                "GET",
                "/api/cloud/launch/deadbeef",
                state=_state(tmp_path),
                match_info={"id": "deadbeef"},
            )
        )
        assert resp.status == 404

    async def test_cancel_unknown_404(self, tmp_path):
        resp = await hc.api_cloud_launch_cancel(
            _req(
                "POST",
                "/api/cloud/launch/deadbeef/cancel",
                state=_state(tmp_path),
                match_info={"id": "deadbeef"},
            )
        )
        assert resp.status == 404

    async def test_cancel_sets_event_and_returns_job(self, tmp_path):
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="light")
        ev = threading.Event()
        hc._cancels(state)[job.id] = ev
        resp = await hc.api_cloud_launch_cancel(
            _req(
                "POST", f"/api/cloud/launch/{job.id}/cancel", state=state, match_info={"id": job.id}
            )
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
            _req(
                "POST", f"/api/cloud/launch/{job.id}/cancel", state=state, match_info={"id": job.id}
            )
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
            _req(
                "POST", f"/api/cloud/launch/{job.id}/cancel", state=state, match_info={"id": job.id}
            )
        )

        assert resp.status == 200
        assert _body(resp)["status"] == lj.CANCELLED
        stored = state.cloud_launch_store.get(job.id)
        assert stored is not None and stored.terminal
        assert stored.step(lj.STEP_PROVISION).state == lj.STEP_FAILED


@pytest.mark.asyncio
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
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
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
                _req(
                    "POST",
                    "/api/cloud/launch",
                    state=state,
                    body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
                )
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
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )

        assert resp.status == 202
        assert len(state.cloud_launch_store.list()) == 2


@pytest.mark.asyncio
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
            _req(
                "POST", f"/api/cloud/launch/{job.id}/signin", state=state, match_info={"id": job.id}
            )
        )
        assert resp.status == 200
        assert _body(resp)["signin"]["code"] == "BQTZ-XKFD"

    async def test_signin_none_pending_409(self, tmp_path):
        state = _state(tmp_path)
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        resp = await hc.api_cloud_launch_signin(
            _req(
                "POST", f"/api/cloud/launch/{job.id}/signin", state=state, match_info={"id": job.id}
            )
        )
        assert resp.status == 409


@pytest.mark.asyncio
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

        st = _req(
            "POST",
            "/api/cloud/kc-3f9a/stop?profile=dev&region=us-east-1",
            state=_state(tmp_path),
            match_info={"tag": "kc-3f9a"},
        )
        r1 = await hc.api_cloud_stop(st)
        assert r1.status == 200
        assert seen["stop"] == {"tag": "kc-3f9a", "profile": "dev", "region": "us-east-1", "kw": {}}

        r2 = await hc.api_cloud_start(
            _req(
                "POST",
                "/api/cloud/kc-7b21/start",
                state=_state(tmp_path),
                match_info={"tag": "kc-7b21"},
            )
        )
        assert r2.status == 200
        assert seen["start"]["tag"] == "kc-7b21"

        r3 = await hc.api_cloud_destroy(
            _req(
                "DELETE",
                "/api/cloud/kc-7b21",
                state=_state(tmp_path),
                match_info={"tag": "kc-7b21"},
            )
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
            _req(
                "DELETE",
                "/api/cloud/kc-3f9a?instance_id=i-0abc123456789def0",
                state=_state(tmp_path),
                match_info={"tag": "kc-3f9a"},
            )
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
        not see in the dashboard — so both must survive."""
        calls = {}
        monkeypatch.setattr(hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-0abc"})
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda tag, p, r: False)
        monkeypatch.setattr(
            hc.connect_mod,
            "unregister_instance",
            lambda iid: calls.setdefault("unregistered", True),
        )
        monkeypatch.setattr(
            hc.source_mod,
            "delete_source",
            lambda *a, **k: calls.setdefault("source", True) or {"removed": True},
        )

        resp = await hc.api_cloud_destroy(
            _req(
                "DELETE",
                "/api/cloud/kc-3f9a?instance_id=i-0abc",
                state=_state(tmp_path),
                match_info={"tag": "kc-3f9a"},
            )
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
        monkeypatch.setattr(hc.ec2, "describe", lambda tag, p, r: {"instance_id": "i-resolved123"})
        monkeypatch.setattr(hc.ec2, "destroy", lambda tag, p, r, **kw: {"destroyed": False})
        monkeypatch.setattr(hc.ec2, "wait_for_delete", lambda *a, **k: True)
        monkeypatch.setattr(
            hc.connect_mod,
            "unregister_instance",
            lambda iid: calls.setdefault("unregistered", iid) is None or True,
        )
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req(
                "DELETE",
                "/api/cloud/kc-3f9a",
                state=_state(tmp_path),
                match_info={"tag": "kc-3f9a"},
            )  # no instance_id
        )

        assert resp.status == 200
        assert calls["unregistered"] == "i-resolved123"

    async def test_invalid_cloud_parameter_is_a_coded_400_not_a_500(self, tmp_path, monkeypatch):
        """ec2.* validates tag/profile/region and raises ValidationError, which is
        NOT an AWSError — without its own arm a malformed tag becomes a 500."""
        from kiro_crew.validation import ValidationError

        def _boom(tag, p, r, **kw):
            raise ValidationError("tag", "required")

        monkeypatch.setattr(hc.ec2, "stop", _boom)

        resp = await hc.api_cloud_stop(
            _req("POST", "/api/cloud/%20/stop", state=_state(tmp_path), match_info={"tag": " "})
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
            hc.connect_mod,
            "unregister_instance",
            lambda iid: calls.setdefault("unregistered", iid) is None or True,
        )
        monkeypatch.setattr(hc.source_mod, "delete_source", lambda *a, **k: {"removed": True})

        resp = await hc.api_cloud_destroy(
            _req(
                "DELETE",
                "/api/cloud/kc-3f9a?instance_id=i-someone-elses",
                state=_state(tmp_path),
                match_info={"tag": "kc-3f9a"},
            )
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
            _req(
                "DELETE", "/api/cloud/kc-9", state=_state(tmp_path), match_info={"tag": "kc-9"}
            )  # no instance_id -> forces the lookup
        )

        assert resp.status == 200

    async def test_a_retry_after_an_interrupted_teardown_still_clears_the_row(
        self, tmp_path, monkeypatch
    ):
        """A restart kills the teardown watcher mid-wait, so the stack goes but the
        registry row stays. On the retry `describe` cannot answer — the stack is
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
            hc.connect_mod,
            "unregister_instance",
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


# ── The remote_provisioners CPP seam ─────────────────────────────────────────
class _Provisioner:
    """A minimal RemoteProvisioner stand-in (duck-typed like the real dataclass)."""

    def __init__(
        self, id, kind=None, label="", posix_only=True, step_labels=(), confirm_before_launch=""
    ):
        self.id = id
        self.kind = kind or id
        self.label = label or id
        self.posix_only = posix_only
        self.step_labels = tuple(step_labels)
        self.confirm_before_launch = confirm_before_launch


class _Provider:
    def __init__(self, rows, engines):
        self._rows = rows
        self._engines = engines
        self.asked: list = []
        self.confirmed: list = []

    def provisioners(self):
        return list(self._rows)

    def engine_for(self, provisioner_id, *, confirmed_recipient=""):
        self.asked.append(provisioner_id)
        self.confirmed.append(confirmed_recipient)
        return self._engines[provisioner_id]


class RecordingEngine(FakeEngine):
    def __init__(self):
        self.calls: list = []

    def provision(self, *, tag, size_key, profile, region):
        self.calls.append(("provision", tag, size_key, profile, region))
        return "ds-devspace-0001"


def _compose(monkeypatch, provider):
    """Install *provider* as ``current_context().remote_provisioners``."""
    from kiro_crew.platform import context as ctx_mod

    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: SimpleNamespace(remote_provisioners=provider)
    )


@pytest.mark.asyncio
class TestProvisionerSeam:
    def test_the_engine_is_resolved_off_the_event_loop(self):
        """Resolving an engine now touches the disk, so it must not run on the loop.

        The seam reads ``cloud.json`` to decide whether the Fargate lane exists, and
        this handler's sibling store calls are already wrapped for exactly this
        reason -- one says so in a comment: a slow disk would stall the gateway's
        other requests and its heartbeat behind it. An unwrapped resolve beside
        wrapped calls is also the inconsistency a later reader re-litigates, so the
        property is pinned rather than left to a comment.

        Asserted on the code because the cost is a blocking syscall inside a
        coroutine: a behavioural test would have to make the disk slow to see it,
        and this class is pinned the same way elsewhere in the suite.

        Read as a TREE, not as a substring. A substring pins the call's line breaks as
        well as its meaning, so adding one argument reformats the line and reddens a test
        about the event loop -- which says nothing about the event loop.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(hc.api_cloud_launch_create)))
        wrapped = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_in_executor"
            and "_engine" in ast.unparse(n)
        ]
        assert wrapped, "the engine resolve is not inside an _in_executor call"

    async def test_a_lane_with_a_recipient_refuses_a_launch_that_confirms_nothing(
        self, tmp_path, monkeypatch
    ):
        """Required because the ROW says so, not because the id is Fargate.

        The value it demands is the same one `GET /api/cloud/provisioners` publishes, so a
        client cannot reach a launch without having read what it must confirm -- which is the
        difference between the operator judging the recipient and echoing a string.
        """
        row = _Provisioner(
            "lane_with_recipient", confirm_before_launch="img@sha256:aaa <- arn:secret"
        )
        engine = RecordingEngine()
        state = _state(tmp_path)
        _compose(monkeypatch, _Provider([row], {row.id: engine}))

        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                },
            )
        )

        assert resp.status == 400
        body = _body(resp)
        assert body["code"] == "recipient_not_confirmed"
        # It ECHOES the value, so a stale client can correct itself rather than guess.
        assert body["confirm_before_launch"] == row.confirm_before_launch
        assert not engine.calls, "a job ran without the recipient being confirmed"

    async def test_a_stale_confirmation_is_refused(self, tmp_path, monkeypatch):
        """The tampering case at the HTTP edge: the operator confirmed what the row said, the
        block changed, and the row now resolves something else."""
        row = _Provisioner("lane_with_recipient", confirm_before_launch="new-image <- arn:secret")
        engine = RecordingEngine()
        state = _state(tmp_path)
        _compose(monkeypatch, _Provider([row], {row.id: engine}))

        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                    "confirm_recipient": "old-image <- arn:secret",
                },
            )
        )

        assert resp.status == 400
        assert _body(resp)["code"] == "recipient_not_confirmed"
        assert not engine.calls

    async def test_a_matching_confirmation_launches(self, tmp_path, monkeypatch):
        """The complement, so neither refusal above can be satisfied by refusing everything."""
        row = _Provisioner(
            "lane_with_recipient", confirm_before_launch="img@sha256:aaa <- arn:secret"
        )
        engine = RecordingEngine()
        state = _state(tmp_path)
        _compose(monkeypatch, _Provider([row], {row.id: engine}))

        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                    "confirm_recipient": row.confirm_before_launch,
                },
            )
        )

        assert resp.status == 202, _body(resp)

    async def test_a_lane_with_nothing_to_confirm_is_unaffected(self, tmp_path, monkeypatch):
        """The built-in EC2 lane: no value on the row, no requirement, and a body that omits
        the field launches exactly as it did before this existed."""
        row = _Provisioner("plain_lane")
        engine = RecordingEngine()
        state = _state(tmp_path)
        _compose(monkeypatch, _Provider([row], {row.id: engine}))

        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "balanced",
                },
            )
        )

        assert resp.status == 202, _body(resp)

    async def test_the_published_row_carries_the_value_a_launch_will_demand(
        self, tmp_path, monkeypatch
    ):
        """One value, two readers. If the list published something else, an operator confirming
        what they were shown would be refused by the launch."""
        row = _Provisioner(
            "lane_with_recipient", confirm_before_launch="img@sha256:aaa <- arn:secret"
        )
        state = _state(tmp_path)
        _compose(monkeypatch, _Provider([row], {row.id: RecordingEngine()}))

        listed = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=state)
        )

        published = _body(listed)["provisioners"][0]["confirm_before_launch"]
        assert published == row.confirm_before_launch

    async def test_a_provider_on_the_older_signature_still_gets_an_engine(
        self, tmp_path, monkeypatch
    ):
        """``confirmed_recipient`` is keyword-only WITH a default, so a provider written before
        it existed is still conforming -- and calling it with the keyword raises ``TypeError``,
        which reached the client as HTTP 500. A lane with nothing to confirm is called exactly
        as it was before the keyword existed."""
        row = _Provisioner("legacy_lane")
        engine = RecordingEngine()

        class OldProvider:
            def provisioners(self):
                return [row]

            def engine_for(self, provisioner_id):  # no confirmed_recipient
                return engine

        state = _state(tmp_path)
        # The SEAM has to answer, not the test hook: this case is about how _engine calls the
        # provider, and the injected engine returns before it ever reaches one.
        state.cloud_launch_engine = None
        _compose(monkeypatch, OldProvider())
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "balanced",
                },
            )
        )

        assert resp.status == 202, _body(resp)

    async def test_a_conforming_provider_still_receives_the_confirmation(
        self, tmp_path, monkeypatch
    ):
        """The other half: dispatching by signature must not quietly stop delivering the value
        to a provider that does accept it."""
        row = _Provisioner("lane_with_recipient", confirm_before_launch="img <- arn:secret")
        engine = RecordingEngine()
        seen: list = []

        class NewProvider:
            def provisioners(self):
                return [row]

            def engine_for(self, provisioner_id, *, confirmed_recipient=""):
                seen.append(confirmed_recipient)
                return engine

        state = _state(tmp_path)
        state.cloud_launch_engine = None  # the seam must answer, not the test hook
        _compose(monkeypatch, NewProvider())
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                    "confirm_recipient": row.confirm_before_launch,
                },
            )
        )

        assert resp.status == 202, _body(resp)
        assert seen == [row.confirm_before_launch]

    async def test_a_provider_that_cannot_take_the_confirmation_its_row_demands_is_refused(
        self, tmp_path, monkeypatch
    ):
        """Refused with a coded 400, not launched.

        A lane that publishes a credential recipient and then cannot be handed the operator's
        answer to it would let them believe they had confirmed something. A 500 would be wrong
        twice: the request is well formed, and the remedy belongs to the deployment.
        """
        row = _Provisioner("mismatched_lane", confirm_before_launch="img <- arn:secret")
        engine = RecordingEngine()

        class OldProviderWithRecipient:
            def provisioners(self):
                return [row]

            def engine_for(self, provisioner_id):
                return engine

        state = _state(tmp_path)
        state.cloud_launch_engine = None  # the seam must answer, not the test hook
        _compose(monkeypatch, OldProviderWithRecipient())
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                    "confirm_recipient": row.confirm_before_launch,
                },
            )
        )

        assert resp.status == 400, _body(resp)
        assert _body(resp)["code"] == "lane_cannot_confirm"
        assert not engine.calls

    async def test_an_aliased_config_answers_400_and_not_500(self, tmp_path, monkeypatch):
        """The alias refusal stays where a launch consumes the file; only its exit changes.

        A symlinked or hardlinked ``cloud.json`` is what stow, chezmoi and
        ``rsync --link-dest`` leave behind, so this reaches ordinary operators. It has to read
        as something they can fix -- the message names the file and the remedy -- rather than
        as a gateway fault.
        """
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        row = _Provisioner("aliased_lane")

        class RefusingProvider:
            def provisioners(self):
                return [row]

            def engine_for(self, provisioner_id, *, confirmed_recipient=""):
                raise SandboxCeilingUnsealable(
                    "cloud.json is a symlink; replace the link with a regular file"
                )

        state = _state(tmp_path)
        state.cloud_launch_engine = None  # the seam must answer, not the test hook
        _compose(monkeypatch, RefusingProvider())
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                },
            )
        )

        assert resp.status == 400, _body(resp)
        body = _body(resp)
        assert body["code"] == "config_alias_refused"
        # The operator has to be able to act on it, so the file and the remedy travel with it.
        assert "cloud.json" in body["error"] and "regular file" in body["error"]

    async def test_the_alias_refusal_is_not_confused_with_an_unknown_lane(
        self, tmp_path, monkeypatch
    ):
        """Two different 400s, because the operator does two different things about them.

        Folding a host-setup refusal into ``unknown_provisioner`` would tell someone whose
        configuration is symlinked that the lane does not exist.
        """
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        row = _Provisioner("aliased_lane")

        class RefusingProvider:
            def provisioners(self):
                return [row]

            def engine_for(self, provisioner_id, *, confirmed_recipient=""):
                raise SandboxCeilingUnsealable("cloud.json has a second hardlink")

        state = _state(tmp_path)
        state.cloud_launch_engine = None  # the seam must answer, not the test hook
        _compose(monkeypatch, RefusingProvider())
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": row.id,
                    "profile": "p",
                    "region": "us-east-1",
                    "size_key": "1024/2048",
                },
            )
        )

        assert _body(resp)["code"] != "unknown_provisioner"

    def test_the_confirmed_recipient_is_read_from_the_body_unaltered(self):
        """The operator's confirmation reaches the seam as they gave it.

        It is the one input here that does NOT come from ``cloud.json``, which is the whole
        reason it can contradict it. So this handler neither validates nor normalises it: a
        check here would be a second copy of the engine's rule, and the two drifting is how a
        value passes one and fails the other. It reads the field and forwards it.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(hc.api_cloud_launch_create)))
        reads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n).startswith("body.get('confirm_recipient'")
        ]
        assert reads, "the launch body's confirm_recipient is never read"
        forwarded = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and "_engine" in ast.unparse(n)
            and "confirm_recipient" in ast.unparse(n)
        ]
        assert forwarded, "the confirmation is read and then not passed to the engine"

    async def test_stock_listing_is_the_builtin_lane(self, tmp_path):
        """No composition: exactly the EC2 descriptor, drawn by the core's own form."""
        resp = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=_state(tmp_path))
        )
        assert resp.status == 200
        rows = _body(resp)["provisioners"]
        assert [r["id"] for r in rows] == ["aws_ec2"]
        assert rows[0]["kind"] == "aws_ec2"
        assert rows[0]["posix_only"] is True
        assert [s["key"] for s in rows[0]["steps"]] == [
            lj.STEP_PREFLIGHT,
            lj.STEP_PROVISION,
            lj.STEP_SIGNIN,
            lj.STEP_CONNECT,
        ]

    async def test_listing_answers_on_windows(self, tmp_path, monkeypatch):
        """The tab needs the list to pick a form; per-row ``posix_only`` carries
        the platform answer, so the listing itself must not be POSIX-gated."""
        monkeypatch.setattr(hc.sys, "platform", "win32")
        resp = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=_state(tmp_path))
        )
        assert resp.status == 200

    async def test_listing_is_owner_only(self, tmp_path):
        resp = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=_state(tmp_path), slack=True)
        )
        assert resp.status == 403

    async def test_composed_rows_are_listed_with_their_step_labels(self, tmp_path, monkeypatch):
        provider = _Provider(
            [
                _Provisioner("aws_ec2"),
                _Provisioner(
                    "devspace",
                    kind="amazon_devspace",
                    label="Amazon DevSpace",
                    posix_only=False,
                    step_labels=((lj.STEP_PROVISION, "Create the DevSpace"), ("bogus", "x")),
                ),
            ],
            engines={},
        )
        _compose(monkeypatch, provider)
        resp = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=_state(tmp_path))
        )
        rows = _body(resp)["provisioners"]
        assert [r["id"] for r in rows] == ["aws_ec2", "devspace"]
        dev = rows[1]
        assert dev["kind"] == "amazon_devspace"
        assert dev["label"] == "Amazon DevSpace"
        assert dev["posix_only"] is False
        by_key = {s["key"]: s["label"] for s in dev["steps"]}
        assert by_key[lj.STEP_PROVISION] == "Create the DevSpace"
        # An override for a key that is not a step is ignored, and the other
        # three keep the core's labels: the KEYS are the contract.
        assert "bogus" not in by_key
        assert by_key[lj.STEP_CONNECT] == "Connect"

    async def test_degraded_seam_read_keeps_the_builtin_lane(self, tmp_path, monkeypatch):
        class Broken:
            def provisioners(self):
                raise RuntimeError("adapter down")

        _compose(monkeypatch, Broken())
        resp = await hc.api_cloud_provisioners(
            _req("GET", "/api/cloud/provisioners", state=_state(tmp_path))
        )
        assert [r["id"] for r in _body(resp)["provisioners"]] == ["aws_ec2"]

    async def test_launch_defaults_to_the_builtin_and_persists_provider_id(self, tmp_path):
        """A pre-seam client body (no provider_id) launches exactly what it did."""
        state = _state(tmp_path)
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "dev", "region": "us-east-1", "size_key": "balanced"},
            )
        )
        assert resp.status == 202
        assert _body(resp)["provider_id"] == "aws_ec2"
        lst = await hc.api_cloud_launch_list(_req("GET", "/api/cloud/launch", state=state))
        assert all("provider_id" in j for j in _body(lst)["jobs"])

    async def test_launch_unknown_provisioner_400(self, tmp_path):
        state = _state(tmp_path)
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"provider_id": "nope", "profile": "", "region": "", "size_key": "x"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_provisioner"
        # Refused BEFORE a job existed: nothing for a restart to reap as interrupted.
        assert state.cloud_launch_store.list() == []

    async def test_launch_routes_to_the_provisioners_engine(self, tmp_path, monkeypatch):
        """The seam's engine drives the job, the size is NOT checked against the EC2
        ladder, and the job's steps carry the provisioner's labels."""
        engine = RecordingEngine()
        provider = _Provider(
            [
                _Provisioner("aws_ec2"),
                _Provisioner(
                    "devspace",
                    kind="amazon_devspace",
                    step_labels=((lj.STEP_PROVISION, "Create the DevSpace"),),
                ),
            ],
            engines={"devspace": engine},
        )
        _compose(monkeypatch, provider)
        state = _state(tmp_path)
        state.cloud_launch_engine = None  # let the seam, not the test hook, answer
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={
                    "provider_id": "devspace",
                    "profile": "",
                    "region": "us-west-2",
                    "size_key": "dev.standard1.large",
                },
            )
        )
        assert resp.status == 202, _body(resp)
        job = _body(resp)
        assert job["provider_id"] == "devspace"
        assert job["status"] == lj.DONE
        assert job["instance_id"] == "ds-devspace-0001"
        assert {s["key"]: s["label"] for s in job["steps"]}[
            lj.STEP_PROVISION
        ] == "Create the DevSpace"
        assert provider.asked == ["devspace"]
        assert engine.calls == [("provision", job["tag"], "dev.standard1.large", "", "us-west-2")]

    async def test_launch_builtin_still_validates_size_against_the_ec2_ladder(
        self, tmp_path, monkeypatch
    ):
        provider = _Provider([_Provisioner("aws_ec2")], engines={"aws_ec2": FakeEngine()})
        _compose(monkeypatch, provider)
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=_state(tmp_path),
                body={"provider_id": "aws_ec2", "profile": "", "region": "", "size_key": "nope"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_launch_request"

    async def test_listed_but_engineless_provisioner_400_and_no_job(self, tmp_path, monkeypatch):
        provider = _Provider([_Provisioner("aws_ec2"), _Provisioner("ghost")], engines={})
        _compose(monkeypatch, provider)
        state = _state(tmp_path)
        state.cloud_launch_engine = None
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"provider_id": "ghost", "profile": "", "region": "", "size_key": "x"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_provisioner"
        assert state.cloud_launch_store.list() == []

    async def test_posix_gate_is_per_provisioner(self, tmp_path, monkeypatch):
        """A provisioner that does not shell to aws may launch from a Windows
        gateway; the built-in still may not."""
        monkeypatch.setattr(hc.sys, "platform", "win32")
        engine = RecordingEngine()
        provider = _Provider(
            [_Provisioner("aws_ec2"), _Provisioner("devspace", posix_only=False)],
            engines={"devspace": engine},
        )
        _compose(monkeypatch, provider)
        state = _state(tmp_path)
        state.cloud_launch_engine = None
        ok = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"provider_id": "devspace", "profile": "", "region": "", "size_key": "s"},
            )
        )
        assert ok.status == 202, _body(ok)
        refused = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=_state(tmp_path),
                body={
                    "provider_id": "aws_ec2",
                    "profile": "",
                    "region": "",
                    "size_key": "balanced",
                },
            )
        )
        assert refused.status == 400
        assert _body(refused)["code"] == "posix_host_required"

    async def test_test_hook_engine_outranks_the_seam(self, tmp_path, monkeypatch):
        """``state.cloud_launch_engine`` keeps the launch-job tests independent of
        any composed context, so it wins when set."""
        provider = _Provider([_Provisioner("aws_ec2")], engines={"aws_ec2": RecordingEngine()})
        _compose(monkeypatch, provider)
        state = _state(tmp_path)  # carries FakeEngine via the hook
        resp = await hc.api_cloud_launch_create(
            _req(
                "POST",
                "/api/cloud/launch",
                state=state,
                body={"profile": "", "region": "", "size_key": "balanced"},
            )
        )
        assert resp.status == 202
        assert provider.asked == []
