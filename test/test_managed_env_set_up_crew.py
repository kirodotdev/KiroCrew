"""A sandbox Crew has no record of (not in the ledger, no
``kc-`` marker). Resume and Set up Crew on the user's click, the user-asked set, the one
``crew-alive`` read through a one-shot ticket, adoption into ``adopted``, and the re-check after
Resume. The guard's set and ticket, the client and crew-alive itself
are in test_managed_env_user_asked.py."""

from __future__ import annotations

import threading
import time

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_crews_from_list import FP, _env, _items
from test_managed_env_routes import call

from kiro_crew.managed_env import install
from kiro_crew.managed_env.engine import (
    SET_UP_CREW_TEXT,
    adopted_crew_name,
    sandbox_id_from_host,
    ssh_host_for,
)
from kiro_crew.managed_env.guard import SandboxGuard

ALIVE = install.COMMANDS_E[install.CMD_CREW_ALIVE][1]


class ReadRunner:
    """The install runner for the one read. Like the box's ssh, its ProxyCommand first asks the
    ssh-session route for a tuple (from another thread, as the real ssh child does); a refusal
    there is ssh's 255. Then crew-alive's answer from a queue (``crew=up`` by default)."""

    def __init__(self, routes, answers=(), via_route=True, after=None):
        self.routes = routes
        self.answers = list(answers)
        self.via_route = via_route
        self.after = after  # runs after the read, before its result is used (the call-time window)
        self.calls: list[tuple[str, str]] = []
        self.ssh: list[int] = []

    def run(self, host, command, stdin=None):
        self.calls.append((host, command))
        if self.via_route:
            sid = sandbox_id_from_host(host)
            got: list = []
            t = threading.Thread(
                target=lambda: got.append(
                    call(self.routes, "POST", f"/api/managed-environments/{sid}/ssh-session")
                )
            )
            t.start()
            t.join()
            self.ssh.append(got[0][0])
            if got[0][0] != 200:
                return install.RunResult(255, "mint-and-attach: refused")
        rc, out = self.answers.pop(0) if self.answers else (0, install.CREW_UP + "\n")
        if self.after is not None:
            self.after()
        return install.RunResult(rc, out)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _setup(bff, tmp_path, monkeypatch, *, answers=(), via_route=True):
    routes, reg, guard, client, _, _ = _env(bff, tmp_path, monkeypatch)
    # the routes' own fingerprint read (off the loop), as the client's: the test sign-in
    monkeypatch.setattr("kiro_crew.managed_env.engine.crews_from_list_fingerprint", lambda: FP)
    runner = ReadRunner(routes, answers, via_route)
    routes.engine._install_runner = runner
    clock = Clock()
    routes._now = clock
    guard._now = clock
    return routes, reg, guard, client, runner, clock


def _set_up(routes, sid):
    return call(routes, "POST", f"/api/managed-environments/{sid}/set-up-crew")


# ---- Set up Crew on a RUNNING box (case 4) ---------------------------------------------------


def test_set_up_crew_finds_crew_and_adopts_the_box(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    assert _items(routes)[sid]["own"] is False
    assert _set_up(routes, sid) == (200, {"result": "found", "line": SET_UP_CREW_TEXT["found"]})
    # one read, nothing else on the box; its one StartSshSession through the ticket
    assert runner.calls == [(ssh_host_for(sid), ALIVE)] and runner.ssh == [200]
    assert bff.count("StartSshSession") == 1
    rows = [r for r in reg.list() if r.ssh_host == ssh_host_for(sid)]
    assert [r.name for r in rows] == ["my-ide-box"]
    assert guard.adopted_ids() == {sid} and guard.is_actionable(sid)
    assert sid not in guard.own_ids()  # never the ledger
    item = _items(routes)[sid]
    assert item["own"] is True and item["deletable"] is False and "setup_line" not in item


def test_an_adopted_box_stays_actionable_on_the_next_list_and_after_a_restart(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, _, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    assert _set_up(routes, sid)[1]["result"] == "found"
    guard.clear_accepted()  # what a restart loses: every in-memory set
    assert not guard.is_actionable(sid)
    assert _items(routes)[sid]["own"] is True and guard.is_actionable(sid)
    # a fresh guard on the same state dir reads it back
    again = SandboxGuard(tmp_path / "state")
    again.accept_listed([dict(bff.sandboxes[sid])], FP)
    assert again.is_actionable(sid) and not again.is_own(sid)
    # the accepted set's rights: Stop and Idle yes, Delete no
    assert call(routes, "POST", f"/api/managed-environments/{sid}/stop")[0] == 202
    assert bff.count("StopSandbox") == 1
    assert call(routes, "POST", f"/api/managed-environments/{sid}/delete")[0] == 404
    assert bff.count("DeleteSandbox") == 0


def test_set_up_crew_with_no_crew_shows_the_line_and_adopts_nothing(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, runner, clock = _setup(
        bff, tmp_path, monkeypatch, answers=[(0, install.CREW_DOWN + "\n")]
    )
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    at = time.strftime("%H:%M", time.localtime(clock.t))
    line = f"{SET_UP_CREW_TEXT['not_found']} Checked at {at}."
    assert _set_up(routes, sid) == (200, {"result": "not_found", "line": line})
    assert reg.list() == [] and guard.adopted_ids() == set() and not guard.is_actionable(sid)
    assert _items(routes)[sid]["setup_line"] == line


def test_a_second_click_within_10_minutes_shows_the_cached_line_with_no_call(
    bff, tmp_path, monkeypatch
):
    routes, _, _, _, runner, clock = _setup(
        bff,
        tmp_path,
        monkeypatch,
        answers=[(0, install.CREW_DOWN), (0, install.CREW_DOWN)],
    )
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    first = _set_up(routes, sid)
    n = len(bff.requests)
    clock.t += 9 * 60
    assert _set_up(routes, sid) == first and len(runner.calls) == 1 and len(bff.requests) == n
    clock.t += 60  # 10 minutes after the read: a click reads again
    assert _set_up(routes, sid)[1]["result"] == "not_found" and len(runner.calls) == 2


def test_a_new_running_period_reads_again(bff, tmp_path, monkeypatch):
    routes, _, _, _, runner, _ = _setup(
        bff, tmp_path, monkeypatch, answers=[(0, install.CREW_DOWN), (0, install.CREW_DOWN)]
    )
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    _set_up(routes, sid)
    bff.sandboxes[sid]["status"] = "STOPPED"
    _items(routes)  # it left RUNNING: the key and the cached result go
    bff.sandboxes[sid]["status"] = "RUNNING"
    assert "setup_line" not in _items(routes)[sid]
    _set_up(routes, sid)
    assert len(runner.calls) == 2


def test_a_read_failure_is_not_cached(bff, tmp_path, monkeypatch):
    routes, _, guard, _, runner, _ = _setup(
        bff, tmp_path, monkeypatch, answers=[(255, ""), (0, "something else")]
    )
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    failed = (200, {"result": "failed", "line": SET_UP_CREW_TEXT["failed"]})
    assert _set_up(routes, sid) == failed
    assert _set_up(routes, sid) == failed  # not a clean answer either: failed, and read again
    assert len(runner.calls) == 2 and guard.adopted_ids() == set()


# ---- Resume on a STOPPED box with no record (case 3) -----------------------------------------


@pytest.mark.parametrize("via", ["resume", "set-up-crew"])
def test_resume_on_a_case_3_box_runs_the_read_at_running(bff, tmp_path, monkeypatch, via):
    routes, reg, guard, _, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "my-ide-box")
    routes._follow_answer(None, routes.list_sync())
    assert routes.forwards.holds(sid)  # Task 8b's hold is in place
    bff.get_statuses[sid].extend(["PENDING", "RUNNING"])
    status, body = call(routes, "POST", f"/api/managed-environments/{sid}/{via}")
    assert status == 202 and body["action"] == "resume"
    assert bff.ops()[1:] == ["ResumeSandbox", "GetSandbox", "GetSandbox", "StartSshSession"]
    # wait_running read RUNNING: the hold for that id went first, so the read got its tuple
    assert runner.ssh == [200] and not routes.forwards.holds(sid)
    assert guard.adopted_ids() == {sid} and _items(routes)[sid]["own"] is True


def test_resume_on_a_case_3_box_with_no_crew_leaves_case_4(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, runner, _ = _setup(
        bff, tmp_path, monkeypatch, answers=[(0, install.CREW_DOWN)]
    )
    sid = bff.add_other("STOPPED", "my-ide-box")
    _items(routes)
    bff.get_statuses[sid].extend(["RUNNING"])
    assert call(routes, "POST", f"/api/managed-environments/{sid}/resume")[0] == 202
    item = _items(routes)[sid]
    assert item["status"] == "RUNNING" and item["own"] is False
    assert item["setup_line"].startswith(SET_UP_CREW_TEXT["not_found"])
    assert reg.list() == [] and guard.adopted_ids() == set()


def test_resume_on_a_running_box_with_no_record_is_refused(bff, tmp_path, monkeypatch):
    routes, _, _, _, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    n = len(bff.requests)
    assert call(routes, "POST", f"/api/managed-environments/{sid}/resume")[0] == 404
    assert len(bff.requests) == n and runner.calls == []


# ---- what a user-asked id may not do ---------------------------------------------------------


@pytest.mark.parametrize("path", ["ssh-session", "stop", "delete", "idle-timeout", "start-crew"])
def test_a_user_asked_id_is_refused_everything_else(bff, tmp_path, monkeypatch, path):
    routes, _, guard, _, runner, _ = _setup(
        bff, tmp_path, monkeypatch, answers=[(0, install.CREW_DOWN)]
    )
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    _set_up(routes, sid)
    assert guard.is_asked(sid)  # asked, not found, so not adopted
    n = len(bff.requests)
    # ssh-session with no armed ticket is also the core's auto-connect
    assert call(routes, "POST", f"/api/managed-environments/{sid}/{path}")[0] == 404
    assert len(bff.requests) == n and len(runner.calls) == 1


# ---- the sign-in at call time -----------------------------------------------------------


def test_a_sign_in_change_then_resume_is_refused_with_no_call(bff, tmp_path, monkeypatch):
    routes, _, guard, client, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "my-ide-box")
    _items(routes)
    client.accept_fingerprint = lambda: "fp-other"  # the vault names another user now
    assert call(routes, "POST", f"/api/managed-environments/{sid}/resume")[0] == 202
    assert bff.count("ResumeSandbox") == 0 and bff.count("GetSandbox") == 0
    assert "refused" in routes.actions.get(sid)["error"]
    assert not guard.is_asked(sid) and runner.calls == []


def test_a_sign_in_change_then_the_read_is_refused_with_no_call(bff, tmp_path, monkeypatch):
    routes, _, guard, client, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    client.accept_fingerprint = lambda: "fp-other"
    assert _set_up(routes, sid)[1]["result"] == "failed"
    assert runner.ssh == [422] and bff.count("StartSshSession") == 0
    assert not guard.is_asked(sid) and guard.adopted_ids() == set()


@pytest.mark.parametrize("how", ["the landing clears the set", "the vault names another user"])
def test_a_sign_in_change_between_the_read_and_the_adopt_writes_no_row(
    bff, tmp_path, monkeypatch, how
):
    # the guard is read before any write, at call time
    routes, reg, guard, client, runner, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)

    def change():
        if how == "the landing clears the set":
            guard.clear_accepted()
        else:
            client.accept_fingerprint = lambda: "fp-other"

    runner.after = change
    assert _set_up(routes, sid) == (200, {"result": "failed", "line": SET_UP_CREW_TEXT["failed"]})
    assert runner.ssh == [200]  # the read itself ran, under the sign-in it was asked under
    assert reg.list() == [] and guard.adopted_ids() == set() and not guard.is_actionable(sid)


def test_a_failed_adopted_note_removes_the_row_it_wrote(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, _, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)

    def no_write(**k):
        raise OSError("disk full")

    monkeypatch.setattr(guard, "note_list_rows", no_write)
    assert _set_up(routes, sid)[1]["result"] == "failed"
    assert reg.list() == [] and not guard.is_actionable(sid)


def test_an_adopted_box_is_not_carried_to_another_users_list(bff, tmp_path, monkeypatch):
    routes, _, guard, client, _, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    _set_up(routes, sid)
    guard.clear_accepted()
    client.accept_fingerprint = lambda: "fp-other"
    del bff.sandboxes[sid]  # the other user's List doesn't have it
    _items(routes)
    assert not guard.is_actionable(sid)


# ---- removal, the ledger and the switch -----------------------------------------------------


def test_a_removed_adopted_row_is_not_re_added_and_leaves_adopted(bff, tmp_path, monkeypatch):
    routes, reg, guard, _, _, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    _set_up(routes, sid)
    reg.rows.clear()  # the user removes the row by hand
    _items(routes)
    assert guard.adopted_ids() == set() and sid in guard.list_rows()[1]
    guard.clear_accepted()
    assert _items(routes)[sid]["own"] is False and reg.list() == []


def test_an_adopted_id_never_reaches_the_ledger(bff, tmp_path, monkeypatch):
    routes, _, guard, _, _, _ = _setup(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    _set_up(routes, sid)
    assert guard.own_ids() == set()


def test_with_the_switch_off_there_is_no_route_no_resume_and_no_line(bff, tmp_path, monkeypatch):
    from kiro_crew.managed_env.engine import crews_from_list_fingerprint

    routes, _, guard, client, runner, _ = _setup(bff, tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    client.accept_fingerprint = crews_from_list_fingerprint  # production's, which reads the switch
    a = bff.add_other("RUNNING", "my-ide-box")
    b = bff.add_other("STOPPED", "my-other-box")
    answer = routes.list_sync()
    assert "crews_from_list" not in answer
    assert all("setup_line" not in s for s in answer["sandboxes"])
    assert _set_up(routes, a) == (404, {"error": "not found"})
    assert call(routes, "POST", f"/api/managed-environments/{b}/resume")[0] == 404
    assert bff.ops() == ["ListSandboxes"] and runner.calls == []


# ---- the ticket --------------------------------------------------------------------------


def test_an_exec_that_fails_before_the_proxycommand_leaves_no_ticket(bff, tmp_path, monkeypatch):
    routes, _, guard, _, runner, _ = _setup(bff, tmp_path, monkeypatch, via_route=False)
    runner.answers = [(255, "ssh: connect failed")]  # ssh never ran its ProxyCommand
    sid = bff.add_other("RUNNING", "my-ide-box")
    _items(routes)
    assert _set_up(routes, sid)[1]["result"] == "failed"
    assert guard.is_asked(sid) and guard.take_ticket(sid) is False  # disarmed in the finally
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 404


@pytest.mark.parametrize(
    "name,want",
    [
        ("my-ide-box", "my-ide-box"),
        ("My IDE Box", "my-ide-box"),
        ("--", "sbx-other001"),
        ("", "sbx-other001"),
    ],
)
def test_an_adopted_rows_name(name, want):
    assert adopted_crew_name(name, "other-001") == want
