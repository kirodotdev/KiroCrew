"""The untracked-runtime report, carried into the reconciler reading, and its user-confirmed reclaim.

The report-only arm in ``session_pid`` finds a managed runtime that is in neither
pid file. These tests pin that its hits reach the reconciler's reading with RSS,
and that the one action that may end such a process -- an explicit, owner-only,
confirmed reclaim -- walks the same gated path the reconciler's own kill arm uses
and refuses every live-owner shape.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import runtime_reconcile as rr
from kiro_crew.dashboard import handlers_system

LEAK = 4242
CHILD = 4243
LIVE_ROOT = 5000
HOME = "/home/u/.kiro/crew"


def _reconciler(
    *,
    reported: set[int] | None = None,
    confirmed: set[int] | None = None,
    table: dict[int, tuple[int, int]] | None = None,
    instances: dict[int, str] | None = None,
    recorded: set[int] | None = None,
    protected: set[int] | None = None,
    session_leader: set[int] | None = None,
    group_leader: set[int] | None = None,
    leased: set[int] | None = None,
    authorize: bool = True,
    platform: bool = True,
    killed: list[int] | None = None,
    identity_of: Any = None,
    homes: dict[int, str] | None = None,
) -> rr.RuntimeReconciler:
    """A reconciler with every reclaim seam faked: no real processes, no real signals."""
    reported = {LEAK} if reported is None else reported
    return rr.RuntimeReconciler(
        slice_pids=set,
        recorded_pids=lambda: set(recorded or ()),
        is_alive=lambda pid: True,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,
        is_sandbox_tool=lambda pid: False,
        leases_on=lambda pid: 1 if pid in (leased or ()) else 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: authorize,
        epoch_of=lambda pid: 0,
        commit_teardown=lambda pid, epoch: True,
        release_teardown=lambda pid: None,
        identity_of=identity_of or (lambda pid: f"id-{pid}"),
        kill_tree=lambda pid, expected=None: (killed if killed is not None else []).append(pid)
        or 1,
        forget=lambda pid: "retracted",
        notify_dead=lambda pid: None,
        audit=lambda pid, outcome, why: None,
        age_secs=lambda pid: 10_000.0,
        untracked_pids=lambda: set(reported),
        confirm_untracked=lambda: set(reported if confirmed is None else confirmed),
        process_table=lambda: dict(
            table if table is not None else {LEAK: (1, 300 << 20), CHILD: (LEAK, 100 << 20)}
        ),
        spawn_instance_of=lambda pid: (
            {LEAK: "inst-a", CHILD: "inst-a"} if instances is None else instances
        ).get(pid),
        protected_pids=lambda: set(protected or ()),
        spawn_home_of=lambda pid: (homes if homes is not None else {LEAK: HOME}).get(pid),
        own_home=lambda: HOME,
        session_leader_alive=lambda pid: pid in (session_leader or ()),
        group_leader_alive=lambda pid: pid in (group_leader or ()),
        reclaim_platform=platform,
    )


def test_a_reported_leak_appears_in_the_reading_with_its_tree_rss() -> None:
    reading = _reconciler().run_once()
    assert reading.leaked_untracked == 1
    assert reading.leaked_rss_bytes == 400 << 20, "the root and its child are summed"
    assert reading.leaked == ((LEAK, 400 << 20),)
    assert rr.RuntimeReconciler.last_reading.fget is not None


def test_the_reading_is_empty_off_linux() -> None:
    reading = _reconciler(platform=False).run_once()
    assert reading.leaked_untracked == 0 and reading.leaked == ()


def test_reclaim_signals_a_confirmed_leak_through_the_kill_seam() -> None:
    killed: list[int] = []
    result = _reconciler(killed=killed).reclaim_untracked()
    assert result.supported and result.killed == (LEAK,), result
    assert killed == [LEAK]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"session_leader": {LEAK}}, "session leader"),
        ({"group_leader": {LEAK}}, "group leader"),
        ({"recorded": {LEAK}}, "tracked"),
        ({"protected": {LEAK}}, "tracked"),
        ({"leased": {LEAK}}, "leased"),
        ({"authorize": False}, "ownership gate"),
        ({"instances": {}}, "spawn instance"),
        ({"confirmed": set()}, "no longer detected"),
        ({"homes": {LEAK: "/home/u/other-home"}}, "another data home"),
        ({"homes": {}}, "another data home"),
    ],
    ids=[
        "session-leader",
        "group-leader",
        "tracked",
        "protected",
        "leased",
        "gate",
        "no-instance",
        "stale",
        "sibling-home",
        "no-home",
    ],
)
def test_reclaim_refuses_every_live_owner_shape(kwargs: dict[str, Any], why: str) -> None:
    killed: list[int] = []
    result = _reconciler(killed=killed, **kwargs).reclaim_untracked()
    assert killed == [], f"nothing is signalled when {why}"
    assert any(why in reason for _pid, reason in result.refused), result.refused


def test_reclaim_refuses_a_live_descendant_of_a_live_runtime() -> None:
    """The env stamps are inherited: a live root sharing the spawn instance vetoes."""
    killed: list[int] = []
    table = {LIVE_ROOT: (1, 10), LEAK: (1, 10)}
    instances = {LIVE_ROOT: "inst-a", LEAK: "inst-a"}
    result = _reconciler(killed=killed, table=table, instances=instances).reclaim_untracked()
    assert killed == []
    assert result.refused == ((LEAK, "another live process shares its spawn instance"),)


def test_reclaim_refuses_a_pid_whose_identity_moved_before_the_gate() -> None:
    reads = iter(["id-old", "id-new"])
    killed: list[int] = []
    result = _reconciler(killed=killed, identity_of=lambda pid: next(reads)).reclaim_untracked()
    assert killed == []
    assert result.refused == ((LEAK, "process identity changed or unreadable"),)


def test_a_zero_kill_budget_refuses_every_reclaim() -> None:
    """``session.reconcile_max_kills: 0`` withholds this arm as it does the scheduled one."""
    killed: list[int] = []
    reconciler = _reconciler(killed=killed)
    reconciler.set_max_kills(0)
    result = reconciler.reclaim_untracked()
    assert killed == []
    assert result.refused == ((LEAK, "kill budget spent"),)


def test_every_reclaim_refusal_is_audited_including_a_stale_pid() -> None:
    audited: list[tuple[int, str, str]] = []
    reconciler = _reconciler(confirmed=set())
    reconciler._audit = lambda pid, outcome, why: audited.append((pid, outcome, why))
    reconciler.reclaim_untracked()
    assert audited == [(LEAK, "refused", "reclaim: no longer detected")]


def test_reclaim_refuses_off_linux() -> None:
    killed: list[int] = []
    result = _reconciler(killed=killed, platform=False).reclaim_untracked()
    assert killed == [] and result.supported is False


# ── the dashboard routes ─────────────────────────────────────────────────────


class _Sessions:
    def __init__(self, reconciler: rr.RuntimeReconciler | None) -> None:
        self._r = reconciler

    def runtime_reconciler(self) -> rr.RuntimeReconciler | None:
        return self._r


def _request(
    method: str, path: str, reconciler: Any, body: dict | None = None, user: str = "owner"
) -> web.Request:
    """A request as the token middleware leaves it: a dashboard subject, no app token."""
    app = web.Application()
    app["state"] = type("S", (), {"sessions": _Sessions(reconciler), "owner_id": "owner"})()
    req = make_mocked_request(method, path, app=app)
    req["user"] = user
    req["app"] = ""

    async def _json() -> dict:
        return body or {}

    req.json = _json  # type: ignore[method-assign]
    return req


@pytest.mark.asyncio
async def test_the_read_route_reports_count_and_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "RECLAIM_PLATFORM", True)
    reconciler = _reconciler()
    reconciler.run_once()
    resp = await handlers_system.api_leaked_runtimes(
        _request("GET", "/api/system/leaked-runtimes", reconciler)
    )
    data = json.loads(resp.body)
    assert data == {
        "supported": True,
        "count": 1,
        "rss_bytes": 400 << 20,
        "runtimes": [{"pid": LEAK, "rss_bytes": 400 << 20}],
    }


@pytest.mark.asyncio
async def test_the_read_route_says_unsupported_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "RECLAIM_PLATFORM", False)
    reconciler = _reconciler()
    reconciler.run_once()
    resp = await handlers_system.api_leaked_runtimes(
        _request("GET", "/api/system/leaked-runtimes", reconciler)
    )
    assert json.loads(resp.body)["supported"] is False


@pytest.mark.asyncio
async def test_reclaim_without_confirm_does_nothing() -> None:
    killed: list[int] = []
    reconciler = _reconciler(killed=killed)
    resp = await handlers_system.api_leaked_runtimes_reclaim(
        _request("POST", "/api/system/leaked-runtimes/reclaim", reconciler, {})
    )
    assert resp.status == 400 and killed == []


@pytest.mark.asyncio
async def test_reclaim_with_confirm_goes_through_the_gate() -> None:
    killed: list[int] = []
    reconciler = _reconciler(killed=killed)
    resp = await handlers_system.api_leaked_runtimes_reclaim(
        _request("POST", "/api/system/leaked-runtimes/reclaim", reconciler, {"confirm": True})
    )
    assert resp.status == 200 and killed == [LEAK]
    assert json.loads(resp.body)["killed"] == [LEAK]


@pytest.mark.asyncio
async def test_reclaim_refuses_a_non_owner() -> None:
    killed: list[int] = []
    req = _request(
        "POST",
        "/api/system/leaked-runtimes/reclaim",
        _reconciler(killed=killed),
        {"confirm": True},
        user="someone-else",
    )
    resp = await handlers_system.api_leaked_runtimes_reclaim(req)
    assert resp.status == 403 and killed == []
    assert json.loads(resp.body)["code"] == "owner_only"


def test_the_live_process_table_names_this_process() -> None:
    """Positive control for the real table the seams default to."""
    if not rr.RECLAIM_PLATFORM:
        pytest.skip("the process table is read from /proc")
    assert rr.same_uid_process_table()[os.getpid()][1] > 0
