"""A connected crew's version skew, and the explicit "Update remote" action.

Two routes and one field:

* ``GET /api/instances/versions`` reads every CONNECTED crew's ``/api/version``
  through the closed-path capability carrier and reports ``behind`` -- the
  exact, pre-release-aware compare against this gateway's build.
* ``behind`` also rides the capabilities reply, so the crew-window header can
  show the badge without a second read.
* ``POST /api/instances/{id}/update`` runs the remote's own ``kirocrew update``
  and then ``kirocrew restart`` as a tracked background job.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import kiro_crew
from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.instances.ssh_tunnel_manager import TunnelState


class _Req:
    """Request stub mirroring aiohttp's mapping surface (see the capabilities tests)."""

    def __init__(self, state, instance_id, identity, body=None):
        self.app = {"state": state}
        self.match_info = {"id": instance_id}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = identity
        self._body = body
        self.can_read_body = body is not None

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, instance_id="nobita", app="", user="local-app", body=None):
    return _Req(state, instance_id, {"user": user, "app": app}, body)


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


class _Registry:
    def __init__(self, names):
        self._rows = [SimpleNamespace(id=iid, name=name) for iid, name in names.items()]

    def list(self):
        return list(self._rows)

    def get(self, iid):
        return next((r for r in self._rows if r.id == iid), None)


def _versions_state(versions, *, states=None, names=None):
    """A manager whose CONNECTED peers answer ``/api/version`` from *versions*.

    A value of ``None`` makes that peer's read fail; an exception instance makes
    it raise.
    """
    states = states or {iid: TunnelState.CONNECTED for iid in versions}
    calls: list[tuple[str, str]] = []

    async def _peer_capability(iid, path):
        calls.append((iid, path))
        outcome = versions.get(iid)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return False, {"code": "capability_unreachable"}
        return True, {"version": outcome}

    mgr = SimpleNamespace(
        peer_capability=_peer_capability,
        status_all=lambda: {iid: SimpleNamespace(state=s) for iid, s in states.items()},
    )
    state = SimpleNamespace(
        instances_manager=mgr,
        instances_registry=_Registry(names or {iid: iid.upper() for iid in states}),
    )
    return state, calls


async def _body(resp):
    return json.loads(resp.body.decode())


class TestRemoteIsBehind:
    @pytest.mark.parametrize(
        ("local", "remote", "behind"),
        [
            ("0.9.1", "0.9.0", True),
            ("0.9.0", "0.9.0rc3", True),
            ("0.9.0rc4", "0.9.0rc3", True),
            ("0.9.0", "0.9.0", False),
            ("0.9.0", "0.9.1", False),
            ("0.9.0", "", False),
            ("0.9.0", "not-a-version", False),
            ("", "0.9.0", False),
        ],
    )
    def test_exact_prerelease_aware_order(self, local, remote, behind):
        assert hi._remote_is_behind(local, remote) is behind


@pytest.mark.asyncio
class TestVersionsRoute:
    async def test_reports_each_connected_crew_and_flags_the_older_one(self, monkeypatch):
        _enable_instances(monkeypatch)
        monkeypatch.setattr(kiro_crew, "__version__", "0.9.1")
        state, calls = _versions_state(
            {"a": "0.9.1", "b": "0.9.0"}, names={"a": "Alpha", "b": "Bravo"}
        )

        resp = await hi.api_instances_versions(_request(state))

        assert resp.status == 200
        data = await _body(resp)
        assert data["local_version"] == "0.9.1"
        assert data["instances"] == [
            {
                "id": "a",
                "name": "Alpha",
                "version": "0.9.1",
                "behind": False,
                "updatable": True,
                "error": "",
            },
            {
                "id": "b",
                "name": "Bravo",
                "version": "0.9.0",
                "behind": True,
                "updatable": True,
                "error": "",
            },
        ]
        assert sorted(calls) == [("a", "/api/version"), ("b", "/api/version")]

    async def test_a_chained_or_fargate_crew_is_not_offered_an_update(self, monkeypatch):
        _enable_instances(monkeypatch)
        monkeypatch.setattr(kiro_crew, "__version__", "0.9.1")
        state, _ = _versions_state({"plain": "0.9.0", "chained": "0.9.0", "fg": "0.9.0"})
        state.instances_registry._rows = [
            SimpleNamespace(id="plain", name="Plain", connection_method="ssh"),
            SimpleNamespace(id="chained", name="Chained", via_instance_id="plain"),
            SimpleNamespace(id="fg", name="Fargate", connection_method="fargate"),
        ]

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert {r["id"]: r["updatable"] for r in data["instances"]} == {
            "plain": True,
            "chained": False,
            "fg": False,
        }

    async def test_only_connected_crews_are_read(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state(
            {"a": "0.0.1", "b": "0.0.1"},
            states={"a": TunnelState.CONNECTED, "b": TunnelState.ERROR},
        )

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert [r["id"] for r in data["instances"]] == ["a"]
        assert calls == [("a", "/api/version")]

    async def test_an_unanswered_or_raising_read_is_unknown_never_behind(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": None, "b": ConnectionResetError("tunnel died")})

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert [(r["version"], r["behind"], r["error"]) for r in data["instances"]] == [
            ("", False, "capability_unreachable"),
            ("", False, "capability_unreachable"),
        ]

    async def test_a_placeholder_zero_version_is_unknown_never_behind(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": "0"})

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert [(r["version"], r["behind"], r["error"]) for r in data["instances"]] == [
            ("", False, "capability_malformed_reply"),
        ]

    async def test_a_failed_read_carries_the_carriers_own_code(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": "0.9.0"})

        async def _refused(_iid, _path):
            return False, {"code": "capability_unauthorized"}

        state.instances_manager.peer_capability = _refused

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert data["instances"][0]["error"] == "capability_unauthorized"

    async def test_a_peer_version_string_is_redacted_and_clamped(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": "9" * 500})

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert len(data["instances"][0]["version"]) == hi._CAP_MAX_VERSION_STR

    async def test_no_manager_answers_an_empty_list(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = SimpleNamespace(instances_manager=None)

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert data == {"local_version": kiro_crew.__version__, "instances": []}

    async def test_an_app_token_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state({"a": "0.0.1"})

        resp = await hi.api_instances_versions(_request(state, app="some-app"))

        assert resp.status == 403
        assert calls == []

    async def test_a_non_owner_dashboard_subject_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state({"a": "0.0.1"})

        resp = await hi.api_instances_versions(_request(state, user="U-someone-else"))

        assert resp.status == 403
        assert calls == []


@pytest.mark.asyncio
class TestCapabilitiesCarryBehind:
    async def _caps(self, monkeypatch, peer_version):
        _enable_instances(monkeypatch)
        monkeypatch.setattr(kiro_crew, "__version__", "0.9.1")

        async def _peer_capability(_iid, path):
            if path == "/api/version":
                return True, {"version": peer_version}
            return True, []

        state = SimpleNamespace(instances_manager=SimpleNamespace(peer_capability=_peer_capability))
        return await _body(await hi.api_instances_capabilities(_request(state)))

    async def test_a_patch_behind_peer_is_compatible_and_behind(self, monkeypatch):
        data = await self._caps(monkeypatch, "0.9.0")
        assert data["version_match"] is True
        assert data["behind"] is True

    async def test_an_equal_peer_is_not_behind(self, monkeypatch):
        data = await self._caps(monkeypatch, "0.9.1")
        assert data["behind"] is False


# ── the update job, on the real manager ──────────────────────────────────


_ECS_TARGET = (
    "ecs:crews_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-1234567890"
)


def _manager(tmp_path, versions=("0.9.0", "0.9.1"), *, verify_secs=0.0):
    """A real manager whose peers answer ``/api/version`` from *versions* in turn.

    The last answer repeats; ``None`` is a peer that does not answer. The
    post-restart window defaults to closed, so a version that never changes
    ends the job at once instead of after the production window.
    """
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    reg = InstancesRegistry(path=tmp_path / "instances.json")

    async def _no_mint(*_a, **_k):
        raise AssertionError("an update never mints a token")

    mgr = SshTunnelManager(reg, base_port=53790, mint_token=_no_mint)
    answers = list(versions)
    reads: list[str] = []

    async def _peer_capability(iid, path):
        assert path == "/api/version"
        reads.append(iid)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if answer is None:
            return False, {"code": "capability_peer_not_connected"}
        return True, {"version": answer}

    mgr.peer_capability = _peer_capability
    mgr._remote_update_verify_secs = verify_secs
    mgr._remote_update_verify_interval_secs = 0.0
    mgr._remote_update_idle_poll_secs = 0.0
    mgr.version_reads = reads
    return reg, mgr


class _Runner:
    """Records every remote ``kirocrew`` call; answers from *script* in order.

    Each answer is ``(rc, err)``. A call can be held open on an event so a test
    observes the job mid-run, released by the test, never by a sleep.
    """

    def __init__(
        self,
        *answers,
        hold: asyncio.Event | None = None,
        hold_restart: asyncio.Event | None = None,
    ):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []
        self.hold = hold
        # Held on the idle-gated restart that answers idle (the last answer), so
        # a test reads the waiting phase before the job moves past it.
        self.hold_restart = hold_restart
        self.entered = asyncio.Event()

    async def ssh(self, host, sub, **kwargs):
        self.calls.append((sub, {"host": host, **kwargs}))
        self.entered.set()
        if self.hold is not None and sub == "update":
            await self.hold.wait()
        if self.hold_restart is not None and sub == _WHEN_IDLE and len(self.answers) == 1:
            await self.hold_restart.wait()
        return self.answers.pop(0)

    async def ssm(self, target, sub, **kwargs):
        self.calls.append((sub, {"target": target, **kwargs}))
        return self.answers.pop(0)


def _patch_runner(monkeypatch, runner):
    from kiro_crew.instances import ssh_tunnel_manager as stm

    monkeypatch.setattr(stm, "run_remote_kirocrew", runner.ssh)
    monkeypatch.setattr(stm, "run_remote_kirocrew_ssm", runner.ssm)


#: The idle-gated restart the job runs in place of a bare ``restart``.
_WHEN_IDLE = "restart --when-idle --max-wait 0"


def _busy(turns: int, background: int) -> tuple[int, str]:
    """What the crew's ``restart --when-idle`` answers while work is in flight."""
    from kiro_crew.cli_restart_idle import busy_marker

    return 75, busy_marker(turns, background)


async def _phase(mgr, iid, phase):
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(
        lambda: (mgr.remote_update_job(iid) or {}).get("phase") == phase,
        describe=lambda: mgr.remote_update_job(iid),
    )


async def _finished(mgr, iid):
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(
        lambda: (mgr.remote_update_job(iid) or {}).get("state") != "running",
        describe=lambda: mgr.remote_update_job(iid),
    )
    return mgr.remote_update_job(iid)


@pytest.mark.asyncio
class TestUpdateJob:
    async def test_runs_the_literal_update_then_restart(self, tmp_path, monkeypatch):
        from kiro_crew.instances.remote_update import REMOTE_UPDATE_TIMEOUT_SECS

        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner((0, ""), (0, ""))
        _patch_runner(monkeypatch, runner)

        started = await mgr.start_remote_update("box")

        assert started["ok"] is True and started["job"]["state"] == "running"
        job = await _finished(mgr, "box")
        assert job["state"] == "succeeded" and job["phase"] == "done"
        assert (job["version_before"], job["version_after"]) == ("0.9.0", "0.9.1")
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE]
        update_kwargs = runner.calls[0][1]
        assert update_kwargs["host"] == "box-alias"
        assert update_kwargs["timeout_secs"] == REMOTE_UPDATE_TIMEOUT_SECS
        assert update_kwargs["stdout_tail"] is True
        assert update_kwargs["marker_port"] == 5476
        # The restart is the same call restart_remote makes: no update budget.
        assert "timeout_secs" not in runner.calls[1][1]
        assert job["code"] == "" and job["force"] is False
        await mgr.shutdown()

    async def test_a_busy_crew_is_waited_on_then_restarted(self, tmp_path, monkeypatch):
        """The restart waits while the crew reports work in flight."""
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        hold = asyncio.Event()
        runner = _Runner((0, ""), _busy(2, 1), _busy(0, 1), (0, ""), hold_restart=hold)
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("box")
        await _phase(mgr, "box", "waiting_for_idle")
        waiting = mgr.remote_update_job("box")
        hold.set()
        job = await _finished(mgr, "box")

        assert waiting["state"] == "running"
        assert waiting["waiting"] in ({"turns": 2, "background": 1}, {"turns": 0, "background": 1})
        assert job["state"] == "succeeded"
        # Every ask was the idle-gated restart; nothing restarted the crew while busy.
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE, _WHEN_IDLE, _WHEN_IDLE]
        await mgr.shutdown()

    async def test_a_crew_still_busy_at_the_ceiling_is_never_restarted(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        mgr._remote_update_idle_wait_secs = 0.0
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner((0, ""), _busy(1, 3))
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "failed" and job["code"] == "still_busy"
        assert job["phase"] == "waiting_for_idle"
        assert job["waiting"] == {"turns": 1, "background": 3}
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE]
        await mgr.shutdown()

    async def test_restart_now_skips_the_update_and_the_wait(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner((0, ""))
        _patch_runner(monkeypatch, runner)

        started = await mgr.start_remote_update("box", force=True)
        job = await _finished(mgr, "box")

        assert started["job"]["force"] is True
        assert job["state"] == "succeeded"
        # The update already ran in the job that stopped short of its restart.
        assert [sub for sub, _ in runner.calls] == ["restart"]
        await mgr.shutdown()

    @pytest.mark.parametrize(
        "answer",
        [
            (2, "kirocrew: error: unrecognized arguments: --when-idle --max-wait 0"),
            (69, "kirocrew-restart-gate-unsupported"),
        ],
        ids=["old-cli", "old-gateway"],
    )
    async def test_an_old_crew_is_refused_and_not_restarted(self, tmp_path, monkeypatch, answer):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner((0, ""), answer)
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "failed" and job["code"] == "peer_cannot_wait"
        assert "Update it from its own dashboard" in job["error"]
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE]
        await mgr.shutdown()

    async def test_restart_remote_refuses_a_busy_crew_and_force_restarts(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner(_busy(1, 2), (0, ""))
        _patch_runner(monkeypatch, runner)

        busy = await mgr.restart_remote("box")
        forced = await mgr.restart_remote("box", force=True)

        assert busy["ok"] is False and busy["code"] == "busy"
        assert (busy["turns"], busy["background"]) == (1, 2)
        assert forced["ok"] is True
        assert [sub for sub, _ in runner.calls] == [_WHEN_IDLE, "restart"]
        await mgr.shutdown()

    async def test_a_crew_that_cannot_check_its_work_offers_restart_now(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        refusal = (78, "boom | stdout tail: Not restarted ... kirocrew-restart-gate-refused")
        runner = _Runner((0, ""), refusal, refusal)
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")
        restart = await mgr.restart_remote("box")

        assert job["state"] == "failed" and job["code"] == "idle_check_failed"
        assert restart["ok"] is False and restart["code"] == "idle_check_failed"
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE, _WHEN_IDLE]
        await mgr.shutdown()

    async def test_restart_remote_refuses_an_old_crew(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((2, "usage: kirocrew ...")))

        out = await mgr.restart_remote("box")

        assert out["ok"] is False and out["code"] == "peer_cannot_wait"
        assert "Restart it from its own dashboard" in out["message"]
        await mgr.shutdown()

    async def test_a_failed_update_never_restarts_and_keeps_the_runner_tail(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner((1, "no network | stdout tail: feed unreachable"))
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "failed" and job["phase"] == "updating"
        assert job["error"] == "no network | stdout tail: feed unreachable"
        assert [sub for sub, _ in runner.calls] == ["update"]
        await mgr.shutdown()

    async def test_a_long_error_tail_is_clamped_to_its_end(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((1, "x" * 5000 + "the reason")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert len(job["error"]) == 700
        assert job["error"].endswith("the reason")
        await mgr.shutdown()

    async def test_a_failed_restart_after_the_update_reports_the_restart_phase(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (3, "systemd refused")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "failed" and job["phase"] == "restarting"
        assert job["error"] == "systemd refused"
        await mgr.shutdown()

    async def test_an_update_that_installs_nothing_is_reported_unchanged(
        self, tmp_path, monkeypatch
    ):
        """``kirocrew update`` exits 0 when it is already current or managed elsewhere."""
        reg, mgr = _manager(tmp_path, versions=("0.9.0",))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "unchanged" and job["phase"] == "done"
        assert job["version_before"] == job["version_after"] == "0.9.0"
        await mgr.shutdown()

    async def test_the_old_gateway_answering_after_restart_is_not_the_outcome(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path, versions=("0.9.0", "0.9.0", None, "0.9.1"), verify_secs=600)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "succeeded"
        assert (job["version_before"], job["version_after"]) == ("0.9.0", "0.9.1")
        assert len(mgr.version_reads) == 4
        await mgr.shutdown()

    async def test_a_crew_that_never_answers_after_restart_is_unverified(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path, versions=("0.9.0", None))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "unverified"
        assert job["version_after"] == ""
        await mgr.shutdown()

    async def test_an_unread_version_before_the_update_is_unverified(self, tmp_path, monkeypatch):
        """With no version from before, any answer after proves no change."""
        reg, mgr = _manager(tmp_path, versions=(None, "0.9.0"))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "unverified" and job["phase"] == "done"
        assert (job["version_before"], job["version_after"]) == ("", "0.9.0")
        await mgr.shutdown()

    async def test_a_peer_version_that_is_not_version_shaped_is_dropped(
        self, tmp_path, monkeypatch
    ):
        reg, mgr = _manager(tmp_path, versions=("0.9.0", "<script>alert(1)</script>"))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "unverified" and job["version_after"] == ""
        await mgr.shutdown()

    async def test_a_placeholder_zero_version_is_no_answer(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path, versions=("0.9.0", "0"))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "unverified" and job["version_after"] == ""
        await mgr.shutdown()

    async def test_a_credential_that_fits_the_version_shape_is_dropped(self, tmp_path, monkeypatch):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        reg, mgr = _manager(tmp_path, versions=("0.9.0", token))
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _patch_runner(monkeypatch, _Runner((0, ""), (0, "")))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert token not in json.dumps(job)
        assert job["state"] == "unverified" and job["version_after"] == ""
        await mgr.shutdown()

    async def test_a_second_click_while_running_is_refused_not_queued(self, tmp_path, monkeypatch):
        from kiro_crew.testing.wait import await_until

        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        hold = asyncio.Event()
        runner = _Runner((0, ""), (0, ""), hold=hold)
        _patch_runner(monkeypatch, runner)

        first = await mgr.start_remote_update("box")
        await await_until(runner.entered, what="the update reached the runner")
        second = await mgr.start_remote_update("box")
        hold.set()
        job = await _finished(mgr, "box")

        assert first["ok"] is True
        assert second == {
            "ok": False,
            "code": "already_running",
            "message": "Box is already updating",
        }
        assert job["state"] == "succeeded"
        assert [sub for sub, _ in runner.calls] == ["update", _WHEN_IDLE]
        await mgr.shutdown()

    async def test_ssm_rides_the_ssm_runner(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(
            name="Cloud",
            connection_method="ssm",
            ssm_target="i-0abc123456789def0",
            aws_region="us-east-1",
            instance_id="cloud",
        )
        runner = _Runner((0, ""), (0, ""))
        _patch_runner(monkeypatch, runner)

        await mgr.start_remote_update("cloud")
        job = await _finished(mgr, "cloud")

        assert job["state"] == "succeeded"
        assert [(sub, kw["target"]) for sub, kw in runner.calls] == [
            ("update", "i-0abc123456789def0"),
            (_WHEN_IDLE, "i-0abc123456789def0"),
        ]
        await mgr.shutdown()

    async def test_a_fargate_crew_is_refused_like_restart(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(
            name="Fargate crew",
            connection_method="fargate",
            ssm_target=_ECS_TARGET,
            aws_profile="dev",
            aws_region="eu-west-2",
            instance_id="fg",
            remote_port=8080,
        )
        runner = _Runner()
        _patch_runner(monkeypatch, runner)

        update = await mgr.start_remote_update("fg")
        restart = await mgr.restart_remote("fg")

        assert update["ok"] is False and update["code"] == "headless"
        assert "no Kiro Crew gateway to update" in update["message"]
        assert restart["ok"] is False and "no Kiro Crew gateway to restart" in restart["message"]
        assert runner.calls == []
        assert mgr.remote_update_job("fg") is None
        await mgr.shutdown()

    async def test_a_chained_crew_is_refused_like_restart(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="B", ssh_host="b-host", instance_id="b")
        reg.add(
            name="C",
            ssh_host="c-host",
            instance_id="c",
            via_instance_id="b",
            via_remote_port=53999,
            via_remote_id="c-on-b",
        )
        runner = _Runner()
        _patch_runner(monkeypatch, runner)

        update = await mgr.start_remote_update("c")
        restart = await mgr.restart_remote("c")

        assert update["ok"] is False and update["code"] == "chained"
        assert "cannot run commands" in update["message"]
        assert "Update it from that crew" in update["message"]
        assert "Restart it from that crew" in restart["message"]
        assert runner.calls == []
        await mgr.shutdown()

    async def test_an_unknown_or_invalid_instance_is_refused(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")
        runner = _Runner()
        _patch_runner(monkeypatch, runner)

        assert (await mgr.start_remote_update("ghost"))["code"] == "not_found"
        assert (await mgr.start_remote_update("bad"))["code"] == "invalid_transport"
        assert runner.calls == []
        await mgr.shutdown()

    async def test_nothing_runs_until_the_update_is_started(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        runner = _Runner()
        _patch_runner(monkeypatch, runner)

        assert mgr.remote_update_job("box") is None
        assert runner.calls == []
        await mgr.shutdown()


@pytest.mark.asyncio
class TestJobBookkeeping:
    async def test_a_raising_body_fails_the_job_with_the_type_name_only(self):
        from kiro_crew.instances.remote_update import RemoteUpdateJobs

        jobs = RemoteUpdateJobs()

        async def _boom(_job):
            raise RuntimeError("detail that must not reach the dashboard")

        jobs.start("box", _boom)
        job = await _finished(SimpleNamespace(remote_update_job=lambda i: _dict(jobs, i)), "box")

        assert job["state"] == "failed"
        assert job["error"] == "update failed: RuntimeError"

    async def test_a_body_that_ends_without_an_outcome_is_failed(self):
        from kiro_crew.instances.remote_update import RemoteUpdateJobs

        jobs = RemoteUpdateJobs()

        async def _silent(_job):
            return None

        jobs.start("box", _silent)
        job = await _finished(SimpleNamespace(remote_update_job=lambda i: _dict(jobs, i)), "box")

        assert job["state"] == "failed"
        assert "without an outcome" in job["error"]

    async def test_shutdown_cancels_a_running_job_and_says_so(self):
        from kiro_crew.instances.remote_update import RemoteUpdateJobs
        from kiro_crew.testing.wait import await_until

        jobs = RemoteUpdateJobs()
        entered = asyncio.Event()

        async def _forever(_job):
            entered.set()
            await asyncio.Event().wait()

        jobs.start("box", _forever)
        await await_until(entered, what="the job body started")
        jobs.cancel_all()
        job = await _finished(SimpleNamespace(remote_update_job=lambda i: _dict(jobs, i)), "box")

        assert job["state"] == "failed"
        assert "shut down" in job["error"]

    async def test_a_finished_job_can_be_replaced_by_a_new_click(self):
        from kiro_crew.instances.remote_update import RemoteUpdateJobs

        jobs = RemoteUpdateJobs()

        async def _ok(job):
            job.finish("succeeded")

        first = jobs.start("box", _ok)
        await _finished(SimpleNamespace(remote_update_job=lambda i: _dict(jobs, i)), "box")
        second = jobs.start("box", _ok)

        assert first is not None and second is not None and second is not first
        await _finished(SimpleNamespace(remote_update_job=lambda i: _dict(jobs, i)), "box")
        assert jobs.get("box").state == "succeeded"


def _dict(jobs, iid):
    job = jobs.get(iid)
    return job.to_dict() if job is not None else None


_STOPPED_UNIT_REFUSAL = (
    "❌ The kirocrew system service is installed (/etc/systemd/system/kirocrew.service) "
    "but stopped; start it with your service manager. No gateway was started outside it.\n"
)


def _fake_ssh(monkeypatch, *answers):
    """Fake only the ssh child, so the real runner builds the failure message."""
    from kiro_crew.instances import token_mint as tm

    queue = list(answers)

    class _Proc:
        def __init__(self, rc, out):
            self.returncode = rc
            self._out = out

        async def communicate(self):
            return (self._out.encode(), b"")

    async def _spawn(*_a, **_k):
        return _Proc(*queue.pop(0))

    monkeypatch.setattr(tm.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(tm, "ssh_spawn_argv_env", lambda argv: (argv, {}))


@pytest.mark.asyncio
class TestRestartRefusalReachesTheCrewPage:
    """A remote whose unit is stopped refuses `kirocrew restart` on STDOUT; the
    crew page must show that hint, not a bare exit code."""

    async def test_restart_remote_carries_the_start_hint(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _fake_ssh(monkeypatch, (1, _STOPPED_UNIT_REFUSAL))

        result = await mgr.restart_remote("box")

        assert result["ok"] is False
        assert "start it with your service manager" in result["message"]
        await mgr.shutdown()

    async def test_update_then_restart_carries_the_start_hint(self, tmp_path, monkeypatch):
        reg, mgr = _manager(tmp_path)
        reg.add(name="Box", ssh_host="box-alias", instance_id="box")
        _fake_ssh(monkeypatch, (0, ""), (1, _STOPPED_UNIT_REFUSAL))

        await mgr.start_remote_update("box")
        job = await _finished(mgr, "box")

        assert job["state"] == "failed" and job["phase"] == "restarting"
        assert "start it with your service manager" in job["error"]
        await mgr.shutdown()


class TestRunnerStdoutTail:
    def test_the_ssh_runner_appends_a_redacted_stdout_tail_only_when_asked(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        secret = "AKIA" + "ABCDEFGHIJKLMNOP"

        class _Proc:
            returncode = 1

            async def communicate(self):
                return (f"❌ feed unreachable\nkey {secret}\n".encode(), b"boom")

        async def _spawn(*_a, **_k):
            return _Proc()

        monkeypatch.setattr(tm.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(tm, "ssh_spawn_argv_env", lambda argv: (argv, {}))

        rc, plain = asyncio.run(tm.run_remote_kirocrew("box", "restart"))
        assert (rc, plain) == (1, "boom")

        rc, tailed = asyncio.run(tm.run_remote_kirocrew("box", "update", stdout_tail=True))
        assert rc == 1
        assert tailed.startswith("boom | stdout tail: ")
        assert "feed unreachable" in tailed
        assert secret not in tailed

    def test_the_ssm_runner_appends_a_redacted_stdout_tail_only_when_asked(self, monkeypatch):
        from kiro_crew.cloud.ssm import CommandResult
        from kiro_crew.instances import ssm_token_mint as sm

        secret = "AKIA" + "ABCDEFGHIJKLMNOP"

        async def _send(*_a, **_k):
            return CommandResult(
                status="Failed",
                stdout=f"feed unreachable\nkey {secret}\n",
                stderr="boom",
                exit_code=1,
            )

        monkeypatch.setattr(sm, "_send_over_ssm", _send)

        rc, plain = asyncio.run(sm.run_remote_kirocrew_ssm("i-0abc123456789def0", "restart"))
        assert (rc, plain) == (1, "boom")

        rc, tailed = asyncio.run(
            sm.run_remote_kirocrew_ssm("i-0abc123456789def0", "update", stdout_tail=True)
        )
        assert rc == 1
        assert tailed.startswith("boom | stdout tail: ")
        assert "feed unreachable" in tailed
        assert secret not in tailed


# ── the update routes ────────────────────────────────────────────────────


class _UpdateMgr:
    def __init__(self, result=None, job=None):
        self.result = result or {"ok": True, "job": {"state": "running"}}
        self.job = job
        self.started: list[str] = []
        self.forced: list[bool] = []

    async def start_remote_update(self, iid, *, force=False):
        self.started.append(iid)
        self.forced.append(force)
        return self.result

    def remote_update_job(self, iid):
        return self.job


def _update_state(mgr, names=None):
    return SimpleNamespace(
        instances_manager=mgr,
        instances_registry=_Registry(names if names is not None else {"nobita": "Nobita"}),
    )


@pytest.mark.asyncio
class TestUpdateRoutes:
    async def test_a_click_starts_the_job_and_answers_202(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr)))

        assert resp.status == 202
        assert (await _body(resp))["job"]["state"] == "running"
        assert mgr.started == ["nobita"]
        assert mgr.forced == [False]

    async def test_restart_now_sends_force(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(
            _request(_update_state(mgr), body={"force": True})
        )

        assert resp.status == 202
        assert mgr.forced == [True]

    @pytest.mark.parametrize(
        "body", [{"force": "yes"}, {"force": True, "cmd": "rm"}, [], ValueError("bad json")]
    )
    async def test_a_malformed_body_is_refused_before_anything_runs(self, monkeypatch, body):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr), body=body))

        assert resp.status == 400
        assert mgr.started == []

    async def test_an_app_token_is_refused_before_anything_runs(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr), app="some-app"))

        assert resp.status == 403
        assert mgr.started == []

    async def test_an_app_token_is_refused_even_past_guard(self, monkeypatch):
        """The route's own owner check stands on its own, not only via ``_guard``."""
        _enable_instances(monkeypatch)
        monkeypatch.setattr(hi, "_guard", lambda _req, _op: None)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr), app="some-app"))

        assert resp.status == 403
        assert mgr.started == []

    async def test_a_non_owner_and_an_unauthenticated_caller_are_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        other = await hi.api_instances_update_remote(
            _request(_update_state(mgr), user="U-someone-else")
        )
        anon = await hi.api_instances_update_remote(_request(_update_state(mgr), user=""))

        assert other.status == 403 and anon.status == 401
        assert mgr.started == []

    async def test_a_slack_origin_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()
        request = _request(_update_state(mgr))
        request.headers["X-Session-Key"] = "slack:C123"

        resp = await hi.api_instances_update_remote(request)

        assert resp.status == 403
        assert mgr.started == []

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            ("chained", 409),
            ("headless", 409),
            ("already_running", 409),
            ("invalid_transport", 400),
            ("not_found", 404),
        ],
    )
    async def test_each_refusal_maps_to_its_status(self, monkeypatch, code, status):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr(result={"ok": False, "code": code, "message": "nope"})

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr)))

        assert resp.status == status
        assert await _body(resp) == {"ok": False, "code": code, "error": "nope"}

    async def test_an_unregistered_instance_is_404_before_the_manager(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _UpdateMgr()

        resp = await hi.api_instances_update_remote(_request(_update_state(mgr, names={})))

        assert resp.status == 404
        assert mgr.started == []

    async def test_the_status_read_returns_the_latest_job(self, monkeypatch):
        _enable_instances(monkeypatch)
        job = {"state": "failed", "error": "no network"}
        mgr = _UpdateMgr(job=job)

        resp = await hi.api_instances_update_remote_status(_request(_update_state(mgr)))

        assert resp.status == 200
        assert await _body(resp) == {"job": job}
        assert mgr.started == []

    async def test_the_status_read_refuses_an_app_token(self, monkeypatch):
        _enable_instances(monkeypatch)

        resp = await hi.api_instances_update_remote_status(
            _request(_update_state(_UpdateMgr()), app="some-app")
        )

        assert resp.status == 403


def test_the_wait_ceiling_is_the_clis_when_idle_default():
    from kiro_crew import cli_restart_idle as cli
    from kiro_crew.instances.remote_update import REMOTE_UPDATE_IDLE_WAIT_SECS

    assert REMOTE_UPDATE_IDLE_WAIT_SECS == cli.DEFAULT_MAX_WAIT_SECS


def test_the_gate_answers_match_what_the_cli_prints():
    from kiro_crew import cli_restart_idle as cli
    from kiro_crew.instances import remote_update as ru

    assert ru._GATE_EXIT_BUSY == cli.EXIT_BUSY
    assert ru._GATE_EXIT_UNSUPPORTED == cli.EXIT_GATE_UNSUPPORTED
    assert ru._GATE_BUSY_MARKER == cli.BUSY_MARKER
    line = cli.busy_marker(4, 7)
    assert line == 'kirocrew-restart-busy {"turns":4,"background":7}'
    assert ru.read_gate_answer(cli.EXIT_BUSY, line) == ru.GateAnswer("busy", 4, 7)
    unsupported = ru.read_gate_answer(cli.EXIT_GATE_UNSUPPORTED, cli.UNSUPPORTED_MARKER)
    assert unsupported.outcome == "unsupported"
    assert ru.read_gate_answer(1, "boom").outcome == "failed"
    # A busy exit without its marker line restarted nothing and gives no counts.
    assert ru.read_gate_answer(cli.EXIT_BUSY, "").outcome == "refused"
    assert ru._GATE_EXIT_REFUSED == cli.EXIT_GATE_REFUSED
    assert ru._GATE_REFUSED_MARKER == cli.REFUSED_MARKER
    refused = ru.read_gate_answer(cli.EXIT_GATE_REFUSED, f"x | stdout tail: {cli.REFUSED_MARKER}")
    assert refused.outcome == "refused"
    # The refused exit without its marker is an ordinary failure.
    assert ru.read_gate_answer(cli.EXIT_GATE_REFUSED, "").outcome == "failed"
    assert ru.RESTART_WHEN_IDLE_SUBCOMMAND == _WHEN_IDLE


def test_a_login_banner_filling_stderr_cannot_hide_the_busy_marker():
    """The runner keeps the head of stderr and the tail of stdout; the marker is in the latter."""
    from kiro_crew.instances import remote_update as ru
    from kiro_crew.instances.token_mint import _with_stdout_tail

    banner = ("Authorized use only. " * 30)[:300]
    marker = 'kirocrew-restart-busy {"turns":2,"background":1}'
    err = _with_stdout_tail(banner, f"Not restarted ...\n{marker}")

    assert ru.read_gate_answer(75, err) == ru.GateAnswer("busy", 2, 1)


def test_a_one_line_stdout_tail_still_yields_the_marker():
    """With a single stdout line the marker shares a line with the stderr head."""
    from kiro_crew.instances import remote_update as ru
    from kiro_crew.instances.token_mint import _with_stdout_tail

    err = _with_stdout_tail("banner", 'kirocrew-restart-busy {"turns":0,"background":3}')

    assert ru.read_gate_answer(75, err) == ru.GateAnswer("busy", 0, 3)


def test_the_marker_survives_the_stdout_tail_redaction():
    from kiro_crew import cli_restart_idle as cli
    from kiro_crew.instances import remote_update as ru
    from kiro_crew.instances.token_mint import _redacted_output_tail, _with_stdout_tail

    stdout = f"❌ Not restarted: busy.\n{cli.busy_marker(12, 0)}\n"
    err = _with_stdout_tail("", _redacted_output_tail(stdout))

    assert ru.read_gate_answer(cli.EXIT_BUSY, err) == ru.GateAnswer("busy", 12, 0)


@pytest.mark.parametrize(
    "line",
    [
        # A key is missing.
        'kirocrew-restart-busy {"turns":2}',
        'kirocrew-restart-busy {"background":2}',
        # A value is not a non-negative int.
        'kirocrew-restart-busy {"turns":"2","background":1}',
        'kirocrew-restart-busy {"turns":2.0,"background":1}',
        'kirocrew-restart-busy {"turns":true,"background":1}',
        'kirocrew-restart-busy {"turns":null,"background":1}',
        'kirocrew-restart-busy {"turns":-1,"background":1}',
        # Not one JSON object after the prefix.
        'kirocrew-restart-busy {"turns":2,"background":1} trailing',
        "kirocrew-restart-busy garbage",
        "kirocrew-restart-busy [2, 1]",
        "kirocrew-restart-busy ",
        "kirocrew-restart-busy",
        # The prefix is not exact, or the line is not the last one.
        'kirocrew-restart-busy{"turns":2,"background":1}',
        'xkirocrew-restart-busy {"turns":2,"background":1}',
        'Not restarted kirocrew-restart-busy {"turns":2,"background":1}',
        # The format before the JSON marker.
        "kirocrew-restart-busy turns=2 background=1",
        'kirocrew-restart-busy {"turns":2,"background":1}\nsomething after',
    ],
)
def test_an_unreadable_busy_marker_is_an_idle_check_failure(line):
    """A busy exit whose counts cannot be read is refused, never busy and never idle."""
    from kiro_crew.instances import remote_update as ru

    assert ru.read_gate_answer(75, line) == ru.GateAnswer("refused")


@pytest.mark.asyncio
async def test_a_restart_with_an_unreadable_busy_marker_is_refused_idle_check_failed(
    tmp_path, monkeypatch
):
    reg, mgr = _manager(tmp_path)
    reg.add(name="Box", ssh_host="box-alias", instance_id="box")
    runner = _Runner((75, "kirocrew-restart-busy garbage"))
    _patch_runner(monkeypatch, runner)

    result = await mgr.restart_remote("box")

    assert result["ok"] is False
    assert result["code"] == "idle_check_failed"
    assert [call[0] for call in runner.calls] == [_WHEN_IDLE]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_the_idle_gated_restart_asks_for_the_stdout_tail(tmp_path, monkeypatch):
    reg, mgr = _manager(tmp_path)
    reg.add(name="Box", ssh_host="box-alias", instance_id="box")
    runner = _Runner((0, ""))
    _patch_runner(monkeypatch, runner)

    await mgr.restart_remote("box")

    assert runner.calls[0][0] == _WHEN_IDLE
    assert runner.calls[0][1]["stdout_tail"] is True
    await mgr.shutdown()


@pytest.mark.parametrize(
    ("body", "force"),
    [(None, False), ({}, False), ({"force": False}, False), ({"force": True}, True)],
)
def test_the_force_body_reads_only_a_boolean(body, force):
    from kiro_crew.instances.remote_update import read_force_flag

    assert read_force_flag(body) is force


@pytest.mark.parametrize("body", [{"force": 1}, {"force": True, "x": 1}, [], "force"])
def test_any_other_force_body_is_malformed(body):
    from kiro_crew.instances.remote_update import read_force_flag

    assert read_force_flag(body) is None
