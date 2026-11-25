"""Launching a cloud crew without the Kiro sign-in.

A crew on another agent harness works without a Kiro sign-in, so the launch
lets the owner skip that step: at create time (``skip_signin``), or with Skip
while the device code is showing. A skipped step is skipped, not failed; the
remote login a mid-wait skip leaves behind is stopped; the crew still connects.

No AWS: the engine and the sign-in handle are fakes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import cli_cloud
from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard import cloud_launch_controls as controls
from kiro_crew.dashboard import handlers_cloud as hc
from kiro_crew.dashboard import handlers_cloud_signin_skip as skip_route


class Handle:
    """A device-code sign-in. ``on_wait`` runs inside ``wait`` with its event."""

    def __init__(self, *, url="https://x/?user_code=AB-12", code="AB-12", abort_ok=True):
        self.already_logged_in = False
        self.url = url
        self.code = code
        self.ports: list = []
        self.error = ""
        self.on_wait = None
        self.aborted = 0
        self.closed = False
        self._abort_ok = abort_ok

    def wait(self, cancel: threading.Event) -> bool:
        if self.on_wait is not None:
            self.on_wait()
        return False if cancel.is_set() else True

    def close(self) -> None:
        self.closed = True

    def abort(self) -> bool:
        self.aborted += 1
        return self._abort_ok


class Engine:
    def __init__(self, handle=None):
        self.handle = handle or Handle()
        self.calls: list = []

    def preflight(self, profile, region):
        self.calls.append("preflight")

    def provision(self, *, tag, size_key, profile, region):
        self.calls.append("provision")
        return "i-0abc123456789def0"

    def begin_signin(self, *, instance_id, profile, region):
        self.calls.append("begin_signin")
        return self.handle

    def register(self, *, instance_id, tag, profile, region):
        self.calls.append("register")

    def teardown(self, *, tag, profile, region):
        self.calls.append("teardown")
        return True


def _store(tmp_path):
    return lj.LaunchJobStore(root=tmp_path / "launch-jobs")


class TestRunLaunchSkip:
    def test_a_skip_chosen_at_create_never_starts_a_login(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced", skip_signin=True)
        eng = Engine()
        out = lj.run_launch(job, s, eng)

        assert "begin_signin" not in eng.calls
        assert eng.calls[-1] == "register"
        assert out.status == lj.DONE and out.error == ""
        step = out.step(lj.STEP_SIGNIN)
        assert step.state == lj.STEP_SKIPPED
        assert step.detail == lj.SIGNIN_SKIPPED_DETAIL
        assert out.step(lj.STEP_CONNECT).detail == "Added to My Crews."
        reread = _store(tmp_path).get(job.id)
        assert reread.signin_skipped is True and reread.signin_detected is False

    def test_a_skip_requested_before_the_step_starts_no_login(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        skip = threading.Event()
        skip.set()
        eng = Engine()
        out = lj.run_launch(job, s, eng, skip_signin=skip)
        assert "begin_signin" not in eng.calls
        assert out.signin_skipped is True
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED

    def test_skip_while_the_code_shows_stops_the_login_and_connects(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        skip = threading.Event()
        handle = Handle()
        seen: dict = {}

        def _press_skip():
            seen["during"] = _store(tmp_path).get(job.id)
            skip.set()

        handle.on_wait = _press_skip
        eng = Engine(handle)
        out = lj.run_launch(job, s, eng, skip_signin=skip)

        # The code was on screen while waiting, then the skip ended the wait.
        assert seen["during"].status == lj.AWAITING_SIGNIN
        assert seen["during"].signin.code == "AB-12"
        assert handle.aborted == 1, "a skipped login must not keep polling for the code"
        assert handle.closed is True
        assert out.status == lj.DONE and out.error == ""
        assert out.signin is None and out.signin_skipped is True
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED
        assert out.step(lj.STEP_SIGNIN).detail == lj.SIGNIN_SKIPPED_DETAIL
        assert eng.calls[-1] == "register"

    def test_skip_during_signin_start_without_a_url_stops_the_login(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        skip = threading.Event()
        handle = Handle(url="")

        class StartingEngine(Engine):
            def begin_signin(self, *, instance_id, profile, region):
                skip.set()
                return super().begin_signin(instance_id=instance_id, profile=profile, region=region)

        eng = StartingEngine(handle)
        out = lj.run_launch(job, s, eng, skip_signin=skip)

        assert out.status == lj.DONE and out.error == ""
        assert out.signin_skipped is True and out.signin_detected is False
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED
        assert out.step(lj.STEP_SIGNIN).detail == lj.SIGNIN_SKIPPED_DETAIL
        assert handle.aborted == 1
        assert handle.closed is True
        assert eng.calls[-1] == "register"
        assert s.get(job.id).signin_skipped is True

    def test_a_login_not_confirmed_stopped_is_said_on_the_step(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        skip = threading.Event()
        handle = Handle(abort_ok=False)
        handle.on_wait = skip.set
        out = lj.run_launch(job, s, Engine(handle), skip_signin=skip)
        assert out.status == lj.DONE and out.error == ""
        assert out.step(lj.STEP_SIGNIN).detail == (
            lj.SIGNIN_SKIPPED_DETAIL + lj.SKIP_ABORT_UNCONFIRMED_DETAIL
        )

    def test_without_a_skip_the_sign_in_runs_as_before(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        handle = Handle()
        out = lj.run_launch(job, s, Engine(handle))
        assert handle.aborted == 0
        assert out.signin_skipped is False and out.signin_detected is True
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_DONE

    def test_a_restart_after_connect_preserves_a_skipped_signin(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced", skip_signin=True)
        job.status = lj.RUNNING
        job.instance_id = "i-0abc123456789def0"
        for name in (lj.STEP_PREFLIGHT, lj.STEP_PROVISION, lj.STEP_CONNECT):
            job.step(name).state = lj.STEP_DONE
        signin_step = job.step(lj.STEP_SIGNIN)
        signin_step.state = lj.STEP_SKIPPED
        signin_step.detail = lj.SIGNIN_SKIPPED_DETAIL
        s.save(job)

        fresh = _store(tmp_path)
        assert fresh.reap_orphans() == [job.id]
        out = fresh.get(job.id)
        assert out.status == lj.DONE
        assert out.error == ""
        assert out.signin_skipped is True and out.signin_detected is False
        assert out.step(lj.STEP_SIGNIN).state == lj.STEP_SKIPPED
        assert out.step(lj.STEP_SIGNIN).detail == signin_step.detail

    def test_an_old_job_file_reads_as_not_skipped(self, tmp_path):
        s = _store(tmp_path)
        job = s.create(profile="dev", region="us-east-1", size_key="balanced")
        d = job.to_dict()
        d.pop("signin_skipped")
        assert lj.LaunchJob.from_dict(d).signin_skipped is False


# ── the routes ────────────────────────────────────────────────────────────────
def _state(tmp_path, engine):
    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_engine=engine,
        cloud_launch_store=_store(tmp_path),
    )


def _req(path, *, state, body=None, match_info=None):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", path, app=app, match_info=match_info or {})
    req["user"] = "owner-1"
    req["app"] = ""
    if body is not None:

        async def _json():
            return body

        req.json = _json  # type: ignore[assignment]
    return req


def _read(resp):
    return json.loads(resp.body.decode("utf-8"))


BASE_BODY = {"profile": "dev", "region": "us-east-1", "size_key": "balanced"}


@pytest.fixture(autouse=True)
def _posix_host(_floor_monkeypatch):
    _floor_monkeypatch.setattr(hc.sys, "platform", "linux")


class TestRoutes:
    @pytest.mark.asyncio
    async def test_create_with_skip_signin_launches_without_a_login(self, tmp_path):
        eng = Engine()
        state = _state(tmp_path, eng)
        resp = await hc.api_cloud_launch_create(
            _req("/api/cloud/launch", state=state, body={**BASE_BODY, "skip_signin": True})
        )
        assert resp.status == 202
        job = state.cloud_launch_store.get(_read(resp)["id"])
        assert job.status == lj.DONE and job.signin_skipped is True
        assert "begin_signin" not in eng.calls

    @pytest.mark.asyncio
    async def test_create_refuses_a_skip_that_is_not_a_boolean(self, tmp_path):
        state = _state(tmp_path, Engine())
        resp = await hc.api_cloud_launch_create(
            _req("/api/cloud/launch", state=state, body={**BASE_BODY, "skip_signin": "yes"})
        )
        assert resp.status == 400
        assert _read(resp)["code"] == "invalid_body"
        assert state.cloud_launch_store.list() == []

    @pytest.mark.asyncio
    async def test_skip_route_sets_the_running_launch_skip(self, tmp_path):
        state = _state(tmp_path, Engine())
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        job.status = lj.AWAITING_SIGNIN
        state.cloud_launch_store.save(job)
        skip = lj.SigninSkip()
        hc._register_skip(state, job.id, skip)

        resp = await skip_route.api_cloud_launch_signin_skip(
            _req("/x", state=state, match_info={"id": job.id})
        )
        assert resp.status == 202
        assert skip.is_set()

    @pytest.mark.asyncio
    async def test_skip_route_refuses_a_finished_launch(self, tmp_path):
        state = _state(tmp_path, Engine())
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        job.status = lj.DONE
        state.cloud_launch_store.save(job)
        skip = lj.SigninSkip()
        hc._register_skip(state, job.id, skip)
        resp = await skip_route.api_cloud_launch_signin_skip(
            _req("/x", state=state, match_info={"id": job.id})
        )
        assert resp.status == 409
        assert _read(resp)["code"] == "signin_skip_unavailable"
        assert not skip.is_set()

    @pytest.mark.asyncio
    async def test_skip_route_refuses_a_launch_no_worker_runs(self, tmp_path):
        state = _state(tmp_path, Engine())
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        job.status = lj.AWAITING_SIGNIN
        state.cloud_launch_store.save(job)
        resp = await skip_route.api_cloud_launch_signin_skip(
            _req("/x", state=state, match_info={"id": job.id})
        )
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_skip_route_answers_404_for_an_unknown_job(self, tmp_path):
        state = _state(tmp_path, Engine())
        resp = await skip_route.api_cloud_launch_signin_skip(
            _req("/x", state=state, match_info={"id": "0123456789ab"})
        )
        assert resp.status == 404

    @pytest.mark.parametrize("outcome", ["unconfirmed", "cancel", "error"])
    def test_worker_refuses_skip_when_the_signin_handle_closes(
        self, tmp_path, monkeypatch, outcome
    ):
        handle = Handle()
        state = _state(tmp_path, Engine(handle))
        job = state.cloud_launch_store.create(**BASE_BODY)
        seen = {}

        def wait(cancel):
            if outcome == "cancel":
                cancel.set()
            elif outcome == "error":
                raise RuntimeError("sign-in failed")
            return False

        def close():
            seen["accepted"] = controls.request_skip(state, job.id)
            seen["set"] = state.cloud_launch_skips[job.id].is_set()

        monkeypatch.setattr(handle, "wait", wait)
        monkeypatch.setattr(handle, "close", close)
        hc._start_worker(state, job, state.cloud_launch_engine)

        assert seen == {"accepted": False, "set": False}
        assert job.signin_skipped is False
        expected_status = {
            "unconfirmed": lj.DONE,
            "cancel": lj.CANCELLED,
            "error": lj.FAILED,
        }[outcome]
        assert job.status == expected_status

    def test_worker_honours_a_skip_requested_during_the_wait(self, tmp_path):
        handle = Handle()
        state = _state(tmp_path, Engine(handle))
        job = state.cloud_launch_store.create(**BASE_BODY)
        accepted = []
        handle.on_wait = lambda: accepted.append(controls.request_skip(state, job.id))

        hc._start_worker(state, job, state.cloud_launch_engine)

        assert accepted == [True]
        assert job.status == lj.DONE and job.signin_skipped is True
        assert job.signin is None and handle.aborted == 1
        assert state.cloud_launch_engine.calls[-1] == "register"

    def test_worker_honours_a_skip_that_wins_the_close_boundary(self, tmp_path, monkeypatch):
        handle = Handle()
        state = _state(tmp_path, Engine(handle))
        job = state.cloud_launch_store.create(**BASE_BODY)
        accepted: list = []
        close_skip = lj.SigninSkip.close
        monkeypatch.setattr(handle, "wait", lambda cancel: False)

        def close(skip):
            if not accepted:
                accepted.append(controls.request_skip(state, job.id))
            close_skip(skip)

        monkeypatch.setattr(lj.SigninSkip, "close", close)
        hc._start_worker(state, job, state.cloud_launch_engine)

        assert accepted == [True]
        assert job.status == lj.DONE and job.signin_skipped is True
        assert job.signin is None and handle.aborted == 1
        assert state.cloud_launch_engine.calls[-1] == "register"

    def test_skip_route_refuses_during_connect_after_an_unconfirmed_wait(
        self, tmp_path, monkeypatch
    ):
        handle = Handle()
        engine = Engine(handle)
        state = _state(tmp_path, engine)
        job = state.cloud_launch_store.create(**BASE_BODY)
        seen = {}
        monkeypatch.setattr(handle, "wait", lambda cancel: False)

        def register(**kwargs):
            seen["step"] = state.cloud_launch_store.get(job.id).step(lj.STEP_CONNECT).state
            seen["response"] = asyncio.run(
                skip_route.api_cloud_launch_signin_skip(
                    _req("/x", state=state, match_info={"id": job.id})
                )
            )
            seen["set"] = state.cloud_launch_skips[job.id].is_set()

        monkeypatch.setattr(engine, "register", register)
        hc._start_worker(state, job, engine)

        assert seen["step"] == lj.STEP_ACTIVE
        assert seen["response"].status == 409
        assert _read(seen["response"])["code"] == "signin_skip_unavailable"
        assert seen["set"] is False
        assert job.status == lj.DONE and job.signin_skipped is False
        assert job.signin.code == "AB-12" and handle.aborted == 0

    def test_the_worker_releases_its_skip_event(self, tmp_path):
        state = _state(tmp_path, Engine())
        job = state.cloud_launch_store.create(
            profile="dev", region="us-east-1", size_key="balanced"
        )
        hc._start_worker(state, job, state.cloud_launch_engine)
        assert state.cloud_launch_skips == {}


# ── the CLI ───────────────────────────────────────────────────────────────────
class TestCliNoLogin:
    def test_no_login_reaches_the_wizard_and_skips_the_identity(self, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(cli_cloud, "_resolve", lambda args: ("dev", "us-east-1"))
        monkeypatch.setattr(cli_cloud, "_note_unchosen_region", lambda *a, **k: None)

        def _no_identity(*a, **k):
            raise AssertionError("--no-login must not resolve a Kiro identity")

        monkeypatch.setattr(cli_cloud, "_resolve_login_target", _no_identity)
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: seen.update(kw) or 0)
        assert cli_cloud._cloud_launch(argparse.Namespace(no_login=True)) == 0
        assert seen["skip_login"] is True
        assert seen["login_target"] is None

    def test_the_launch_parser_takes_no_login(self):
        from kiro_crew import cli_parser_admin

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        cli_parser_admin.register_cloud_parser(sub)
        args = parser.parse_args(["cloud", "launch", "--no-login"])
        assert args.no_login is True
