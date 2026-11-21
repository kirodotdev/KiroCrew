"""The user's List decides which sandboxes are crews, on any of their
dashboards. Cases 1, 1c and 2: rows from the List for Crew-made
(``kc-``) boxes, Stop / Resume / Idle / Connect on them, Delete on this home's ledger only, and
Start Crew (one ``hook-run``)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry, Row
from test_managed_env_routes import TOKEN, call, own

from kiro_crew.managed_env import install
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import (
    START_CREW_TEXT,
    SandboxLaunchEngine,
    crews_from_list_fingerprint,
    ssh_host_for,
)
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import SandboxRoutes

FP = "fp-test0"


class FakeRunner:
    """The install runner: records each command; answers rc from a queue (0 by default)."""

    def __init__(self, rcs=()):
        self.calls: list[tuple[str, str]] = []
        self.rcs = list(rcs)

    def run(self, host, command, stdin=None):
        self.calls.append((host, command))
        return install.RunResult(self.rcs.pop(0) if self.rcs else 0, "")


def _no_ssh(*a, **k):
    raise AssertionError("an ssh run at listing")


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _env(bff, tmp_path, monkeypatch, *, fp=FP, rcs=()):
    from kiro_crew.dashboard.handlers import _shared

    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(lambda: TOKEN, guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock)
    client.accept_fingerprint = lambda: fp
    reg = FakeRegistry()
    runner = FakeRunner(rcs)
    engine = SandboxLaunchEngine(
        client=client, registry_factory=lambda: reg, ssh=_no_ssh, install_runner=runner
    )
    # no List writes an ssh config; any ensure_block call is recorded (and writes nothing)
    blocks: list[tuple] = []
    monkeypatch.setattr(
        "kiro_crew.managed_env.ssh_config.ensure_block", lambda *a, **k: blocks.append(a) or True
    )
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    return routes, reg, guard, client, runner, blocks


def _items(routes):
    return {s["sandbox_id"]: s for s in routes.list_sync()["sandboxes"]}


def test_an_accepted_kc_box_gets_one_row_with_no_ssh(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, runner, blocks = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "kc-0a1b2c")
    answer = routes.list_sync()
    assert answer["rows_added"] is True
    rows = [r for r in reg.list() if r.ssh_host == ssh_host_for(sid)]
    assert [(r.name, r.provisioner_id) for r in rows] == [("kc-0a1b2c", "kiro_managed_env")]
    assert bff.ops() == ["ListSandboxes"] and runner.calls == [] and blocks == []
    item = {s["sandbox_id"]: s for s in answer["sandboxes"]}[sid]
    assert item["own"] is True and item["deletable"] is False and item["instance_id"] == rows[0].id
    # the same List again: no second row (idempotent); no ssh config write either time
    assert "rows_added" not in routes.list_sync() and len(reg.list()) == 1 and blocks == []
    # a row still here is not taken for a user removal
    assert guard.list_rows() == ({sid}, set())
    # never in the ledger; the ledger-only counts are as before
    assert sid not in guard.own_ids()


def test_production_routes_write_no_ssh_config_at_listing(bff, tmp_path, monkeypatch):
    # the production wiring (register_sandbox_routes) passes no block writer either
    from aiohttp import web

    from kiro_crew.managed_env.routes import register_sandbox_routes

    _, reg, _, client, _, blocks = _env(bff, tmp_path, monkeypatch)
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: reg, ssh=_no_ssh)
    app = web.Application()
    routes = register_sandbox_routes(app)
    routes._engine = engine
    bff.add_other("RUNNING", "kc-0a1b2c")
    assert routes.list_sync()["rows_added"] is True and len(reg.list()) == 1
    assert blocks == []


def test_two_lists_at_once_write_one_row(bff, tmp_path, monkeypatch):
    routes, reg, *_ = _env(bff, tmp_path, monkeypatch)
    bff.add_other("RUNNING", "kc-0a1b2c")
    go = threading.Barrier(2)
    inside = threading.Barrier(2, timeout=0.5)
    add = reg.add

    def slow_add(**k):
        # both Lists pass the same-host check before either adds, unless the lock keeps one out
        try:
            inside.wait()
        except threading.BrokenBarrierError:
            pass
        return add(**k)

    monkeypatch.setattr(reg, "add", slow_add)

    def one():
        go.wait()
        routes.list_sync()

    ts = [threading.Thread(target=one) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(reg.list()) == 1


@pytest.mark.parametrize("act", ["stop", "resume"])
def test_stop_and_resume_take_an_accepted_id(bff, tmp_path, monkeypatch, act):
    routes, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING" if act == "stop" else "STOPPED", "kc-0a1b2c")
    routes.list_sync()
    status, _ = call(routes, "POST", f"/api/managed-environments/{sid}/{act}")
    assert status in (200, 202)
    assert bff.count("StopSandbox" if act == "stop" else "ResumeSandbox") == 1


def test_idle_and_ssh_session_take_an_accepted_id(bff, tmp_path, monkeypatch):
    routes, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 200
    assert bff.count("StartSshSession") == 1
    assert routes.engine.guard.is_actionable(sid)


def test_delete_stays_on_the_ledger(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "kc-0a1b2c")
    mine = own(bff, guard, reg, status="STOPPED")
    items = _items(routes)
    assert items[sid]["deletable"] is False and items[mine]["deletable"] is True
    assert call(routes, "POST", f"/api/managed-environments/{sid}/delete") == (
        404,
        {"error": "not a sandbox of this crew client"},
    )
    assert bff.count("DeleteSandbox") == 0


@pytest.mark.parametrize("name", ["my-ide-box", "kc-0A1B2C", "kc-0a1b2c-1"])
def test_a_non_marker_id_is_never_actionable(bff, tmp_path, monkeypatch, name):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", name)
    items = _items(routes)
    assert items[sid]["own"] is False and reg.list() == []
    n = len(bff.requests)
    for path in ("stop", "resume", "idle-timeout", "ssh-session", "start-crew"):
        assert call(routes, "POST", f"/api/managed-environments/{sid}/{path}")[0] == 404
    assert len(bff.requests) == n and bff.naming(sid) == []


def test_a_sign_in_change_clears_the_set_until_a_list_of_the_new_user(bff, tmp_path, monkeypatch):
    routes, _, guard, client, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    assert guard.is_actionable(sid)
    guard.clear_accepted()  # what the landing does first, in memory
    assert not guard.is_actionable(sid)
    client.accept_fingerprint = lambda: "fp-test1"
    del bff.sandboxes[sid]  # the new user's List doesn't have it
    item = _items(routes)[sid]
    assert not guard.is_actionable(sid) and guard.accepted_fingerprint == "fp-test1"
    # the last user's List-made row stays, GONE, with no Delete; its Connect calls nothing
    assert item["status"] == "GONE" and item["deletable"] is False
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 404
    assert bff.count("StartSshSession") == 0


def test_a_failed_list_leaves_the_set_and_the_rows(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    bff.add_other("RUNNING", "kc-ffffff")
    bff.queue_error("resume_400_text", op="ListSandboxes")  # a 400: no stop, no list
    answer = routes.list_sync()
    assert answer["error"] and len(reg.list()) == 1 and guard.is_actionable(sid)


def test_a_row_the_user_removed_stays_removed_across_sign_ins(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    reg.rows.clear()  # the user removes the row by hand
    routes.list_sync()
    assert reg.list() == [] and sid in guard.list_rows()[1]
    guard.clear_accepted()
    client.accept_fingerprint = lambda: "fp-again"
    routes.list_sync()
    assert reg.list() == []


def test_a_box_gone_from_the_list_keeps_its_gone_row(bff, tmp_path, monkeypatch):
    routes, reg, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    del bff.sandboxes[sid]
    assert _items(routes)[sid]["status"] == "GONE" and len(reg.list()) == 1


def test_a_launch_in_progress_gets_no_list_made_row(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    own(bff, guard, None, status="PENDING", name="kc-abcdef")  # in the ledger, no row yet
    routes.list_sync()
    assert reg.list() == []


def test_with_no_fingerprint_the_answer_is_as_before(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch, fp="")
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    answer = routes.list_sync()
    item = {s["sandbox_id"]: s for s in answer["sandboxes"]}[sid]
    assert reg.list() == [] and item["own"] is False and "deletable" not in item
    assert "rows_added" not in answer


@pytest.mark.parametrize("off", ["lane", "row_lifecycle", "crews_from_list"])
def test_the_fingerprint_is_empty_unless_the_rule_applies(monkeypatch, off):
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: off != "lane")
    monkeypatch.setattr(
        "kiro_crew.managed_env.settings.lane_enabled", lambda: off != "row_lifecycle"
    )
    monkeypatch.setattr(
        "kiro_crew.managed_env.settings.lane_enabled", lambda: off != "crews_from_list"
    )

    def no_read():
        raise AssertionError("read the vault with the rule off")

    monkeypatch.setattr("kiro_crew.auth.bridge.vault_identity_fingerprint", no_read)
    assert crews_from_list_fingerprint() == ""


def test_a_route_check_reads_no_fingerprint(bff, tmp_path, monkeypatch):
    routes, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()

    def no_read():
        raise AssertionError("a route check read the fingerprint")

    monkeypatch.setattr("kiro_crew.auth.bridge.vault_identity_fingerprint", no_read)
    reads: list[int] = []
    routes.engine.client.accept_fingerprint = lambda: reads.append(1) or FP
    assert routes.engine.guard.is_actionable(sid) and reads == []  # the route check: memory only
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 200
    assert reads == [1]  # one read, in the client call, none in the route check


@pytest.mark.parametrize("path,op", [("ssh-session", "StartSshSession"), ("stop", "StopSandbox")])
def test_a_sign_in_change_refuses_the_last_users_box_before_the_landing(
    bff, tmp_path, monkeypatch, path, op
):
    # the vault names another sign-in, and no landing List has run yet
    routes, _, guard, client, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    client.accept_fingerprint = lambda: "fp-test1"
    assert guard.is_actionable(sid)  # the route check still passes, from memory
    status, body = call(routes, "POST", f"/api/managed-environments/{sid}/{path}")
    assert bff.count(op) == 0 and bff.count("StartSshSession") == 0
    assert not guard.is_actionable(sid) and guard.accepted_fingerprint == ""
    if path == "ssh-session":
        assert status == 422 and body["kind"] == "refused"
    else:  # the act route answers 202; the refusal is the action's error (read by a new List)
        del bff.sandboxes[sid]  # the new user's List doesn't have it
        assert status == 202 and "refused" in _items(routes)[sid]["action"]["error"]
        assert bff.count(op) == 0 and not guard.is_actionable(sid)


def test_a_signed_out_watch_pass_clears_the_set(bff, tmp_path, monkeypatch):
    routes, _, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    assert _landing(routes, monkeypatch) is True and guard.is_actionable(sid)
    assert _landing(routes, monkeypatch, state="signin_required") is False
    assert not guard.is_actionable(sid) and bff.count("ListSandboxes") == 1
    # signed in again: the landing List runs again
    assert _landing(routes, monkeypatch) is True and bff.count("ListSandboxes") == 2


class _App(dict):
    pass


def _landing(routes, monkeypatch, *, state="signed_in", fp=FP):
    monkeypatch.setattr(
        "kiro_crew.managed_env.core_signin.sign_in_status", lambda: {"state": state}
    )
    monkeypatch.setattr("kiro_crew.managed_env.engine.crews_from_list_fingerprint", lambda: fp)
    return asyncio.run(routes.landing_once(_App()))


def test_the_start_list_registers_and_holds_a_stopped_box_by_its_id(bff, tmp_path, monkeypatch):
    routes, reg, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "kc-0a1b2c")
    assert _landing(routes, monkeypatch) is True
    assert len(reg.list()) == 1 and routes.forwards.holds(sid)
    assert bff.ops() == ["ListSandboxes"]
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 409
    assert bff.count("StartSshSession") == 0
    # the same sign-in again: no second List
    assert _landing(routes, monkeypatch) is False and bff.count("ListSandboxes") == 1


@pytest.mark.parametrize("how", ["signed_out", "row_lifecycle_off"])
def test_no_start_list_when_signed_out_or_the_rule_is_off(bff, tmp_path, monkeypatch, how):
    routes, reg, *_ = _env(bff, tmp_path, monkeypatch)
    bff.add_other("STOPPED", "kc-0a1b2c")
    if how == "row_lifecycle_off":
        monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    state = "signin_required" if how == "signed_out" else "signed_in"
    assert _landing(routes, monkeypatch, state=state) is False
    assert bff.requests == [] and reg.list() == []


def _start(routes, sid):
    return call(routes, "POST", f"/api/managed-environments/{sid}/start-crew")


def test_start_crew_is_one_hook_run_with_no_port_read(bff, tmp_path, monkeypatch):
    routes, _, _, _, runner, _ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    assert _start(routes, sid) == (200, {"started": True, "line": ""})
    assert runner.calls == [(ssh_host_for(sid), install._cmd(install.CMD_HOOK_RUN))]


@pytest.mark.parametrize(
    "rc,line",
    [
        (255, START_CREW_TEXT["other"].format(rc=255)),  # a lone 255: no retry
        (install.NOT_KAS_BUNDLE_EXIT, START_CREW_TEXT["files"]),
        (install.LAUNCHER_FAILED_EXIT, START_CREW_TEXT["launcher"]),
    ],
)
def test_start_crew_failures_are_one_exec_and_one_line(bff, tmp_path, monkeypatch, rc, line):
    routes, _, _, _, runner, _ = _env(bff, tmp_path, monkeypatch, rcs=[rc, 0])
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    assert _start(routes, sid) == (200, {"started": False, "line": line})
    assert len(runner.calls) == 1


def test_start_crew_is_absent_with_the_switch_off(bff, tmp_path, monkeypatch):
    # crews_from_list off, the route is 404 as before, even on a ledger box
    routes, reg, guard, client, runner, _ = _env(bff, tmp_path, monkeypatch)
    mine = own(bff, guard, reg)
    assert routes.list_sync()["crews_from_list"] is True
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    client.accept_fingerprint = crews_from_list_fingerprint  # production's, which reads the switch
    assert "crews_from_list" not in routes.list_sync()
    assert _start(routes, mine) == (404, {"error": "not found"})
    assert runner.calls == [] and bff.count("ListSandboxes") == 2 and len(bff.requests) == 2


@pytest.mark.parametrize("status", ["PENDING", "STOPPING", "STOPPED", "a failed List"])
def test_start_crew_runs_only_where_the_last_list_read_running(bff, tmp_path, monkeypatch, status):
    # not on PENDING, STOPPING or STOPPED, nor after a List that read nothing
    routes, _, _, _, runner, _ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    if status == "a failed List":
        bff.queue_error("resume_400_text", op="ListSandboxes")
    else:
        bff.sandboxes[sid]["status"] = status
    routes.list_sync()  # no hold: list_sync alone holds nothing
    assert routes.engine.guard.is_actionable(sid) and not routes.forwards.holds(sid)
    assert _start(routes, sid) == (409, {"error": "sandbox is not RUNNING", "kind": "not_ready"})
    assert runner.calls == []


def test_start_crew_on_a_held_box_runs_nothing(bff, tmp_path, monkeypatch):
    routes, _, _, _, runner, _ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "kc-0a1b2c")
    routes._follow_answer(None, routes.list_sync())
    assert _start(routes, sid)[0] == 409 and runner.calls == []


def test_the_rows_writer_never_takes_the_same_name_branch(bff, tmp_path, monkeypatch):
    routes, reg, *_ = _env(bff, tmp_path, monkeypatch)
    reg.rows["row-x"] = Row("row-x", "kc-0a1b2c", "crew-sbx-somewhere-else")
    sid = bff.add_other("RUNNING", "kc-0a1b2c")
    routes.list_sync()
    assert reg.rows["row-x"].ssh_host == "crew-sbx-somewhere-else"
    assert [r.ssh_host for r in reg.list() if r.id != "row-x"] == [ssh_host_for(sid)]


def _write_cfg(monkeypatch, tmp_path, lane):
    import json

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"sandbox": {"lane": lane}}))
