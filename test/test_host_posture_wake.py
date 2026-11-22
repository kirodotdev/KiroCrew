"""The conductor's slow layer of the host memory guard (rfc-host-memory-guard.md §5).

§8 tests 12 and 13: a conductor sees only its own workers' rows, and one posture
change (memory or disk) wakes each watching conductor once while a repeat wakes
nobody. Runs with no gateway: the notifier, the probe, the push and the route's
projection are each driven with stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kiro_crew import work_ledger
from kiro_crew.notifications import resource_pressure as rp
from kiro_crew.resource_status import POSTURE_AMPLE, POSTURE_CRITICAL, POSTURE_TIGHT, TempHeadroom

CONDUCTOR = "chat-conductor"
OTHER_CONDUCTOR = "chat-other-conductor"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _status(posture: str, available_gb: float = 8.0) -> SimpleNamespace:
    return SimpleNamespace(posture=posture, available_gb=available_gb)


def _headroom(root: str, space_pct: float, inodes: int = 100_000) -> TempHeadroom:
    return TempHeadroom(root, space_pct, 50.0, inodes)


# -- disk posture --------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("space", "inodes", "level"),
    [
        (50.0, 100_000, POSTURE_AMPLE),
        (8.0, 100_000, POSTURE_TIGHT),
        (500 / 1000 * 100, 900, POSTURE_TIGHT),
        (2.0, 100_000, POSTURE_CRITICAL),
        (50.0, 150, POSTURE_CRITICAL),
    ],
)
def test_disk_posture_reads_tight_by_the_low_rule_and_critical_under_the_floors(
    space, inodes, level
):
    assert rp.disk_posture([_headroom("/tmp", space, inodes)])[0] == level


def test_disk_posture_is_the_worst_volume_and_names_only_the_low_ones():
    level, text = rp.disk_posture([_headroom("/tmp", 50.0), _headroom("/scratch", 2.0)])
    assert level == POSTURE_CRITICAL
    assert text.startswith("/scratch LOW on space, 2% space free") and "/tmp" not in text


def test_disk_posture_names_the_inode_axis_when_inodes_trip_it():
    level, text = rp.disk_posture([_headroom("/tmp", 50.0, inodes=150)])
    assert level == POSTURE_CRITICAL
    assert "LOW on inodes" in text and "(150 inodes)" in text


def test_combined_posture_is_the_worse_of_memory_and_disk():
    assert rp.combined_posture(_status(POSTURE_AMPLE), (POSTURE_TIGHT, "/tmp 6% free")) == (
        POSTURE_TIGHT,
        "memory 8.0 GB free; /tmp 6% free",
    )
    assert rp.combined_posture(_status(POSTURE_CRITICAL, 1.4), (POSTURE_TIGHT, "x"))[0] == (
        POSTURE_CRITICAL
    )
    assert rp.combined_posture(_status("unknown"), (POSTURE_CRITICAL, "x")) is None


# -- the notifier's pull --------------------------------------------------------- #


def _notifier(pulls: list[int], disk: list[tuple[str, str]]) -> rp.ResourcePressureNotifier:
    notifier = rp.ResourcePressureNotifier(
        SimpleNamespace(push=lambda _note: None),
        disk_fn=lambda: disk[0],
        on_posture_change=lambda: pulls.append(1),
    )
    return notifier


def _feed(notifier, memory: str, disk: tuple[str, str]) -> None:
    notifier._disk = disk
    notifier.observe_posture(_status(memory))


def test_one_change_pulls_once_and_a_repeat_pulls_nothing():
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed(notifier, POSTURE_AMPLE, (POSTURE_AMPLE, ""))
    assert pulls == [] and rp.current_posture() == (POSTURE_AMPLE, "memory 8.0 GB free")
    _feed(notifier, POSTURE_TIGHT, (POSTURE_AMPLE, ""))
    _feed(notifier, POSTURE_TIGHT, (POSTURE_AMPLE, ""))
    assert pulls == [1]


def test_a_disk_only_change_pulls_too():
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed(notifier, POSTURE_AMPLE, (POSTURE_AMPLE, ""))
    _feed(notifier, POSTURE_AMPLE, (POSTURE_CRITICAL, "/tmp 2% free"))
    assert pulls == [1]
    assert rp.current_posture() == (POSTURE_CRITICAL, "memory 8.0 GB free; /tmp 2% free")


def test_a_flapping_posture_pulls_per_change_and_unknown_is_no_signal():
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    for memory in (POSTURE_TIGHT, "unknown", POSTURE_AMPLE, POSTURE_TIGHT, POSTURE_TIGHT):
        _feed(notifier, memory, (POSTURE_AMPLE, ""))
    assert len(pulls) == 3


def test_the_sample_reads_disk_in_the_worker_thread_and_publishes(monkeypatch):
    pulls: list[int] = []
    notifier = rp.ResourcePressureNotifier(
        SimpleNamespace(push=lambda _note: None),
        probe_fn=lambda: SimpleNamespace(
            posture=POSTURE_AMPLE, available_gb=9.0, agent_ceiling=None
        ),
        disk_fn=lambda: (POSTURE_TIGHT, "/tmp 7% free"),
        on_posture_change=lambda: pulls.append(1),
    )
    monkeypatch.setattr(rp, "check_agents_slice_pressure", lambda: None)
    monkeypatch.setattr(rp, "reclaim_agents_slice_page_cache", lambda: None)
    asyncio.run(notifier.maybe_sample())
    assert rp.current_posture() == (POSTURE_TIGHT, "memory 9.0 GB free; /tmp 7% free")
    assert pulls == [1]


# -- the push ------------------------------------------------------------------- #


def test_the_push_fires_only_boards_with_an_active_work_ledger_loop(monkeypatch):
    from kiro_crew import autonudge, conductor_wake

    fired: list[tuple[str, str]] = []

    async def _fire(_svc, board, item_id=""):
        fired.append((board, item_id))
        return f"loop-{board}"

    async def _go():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(autonudge, "get_instance", lambda: object())
        monkeypatch.setattr(conductor_wake, "_service_loop", lambda _svc: loop)
        monkeypatch.setattr(conductor_wake, "_watched_boards", lambda _svc: {"b2", "b1"})
        monkeypatch.setattr(conductor_wake, "_fire", _fire)
        future = conductor_wake.wake_for_posture_change()
        assert future is not None
        return await asyncio.wrap_future(future)

    assert asyncio.run(_go()) == ["loop-b1", "loop-b2"]
    assert fired == [("b1", conductor_wake.POSTURE_ITEM), ("b2", conductor_wake.POSTURE_ITEM)]


def test_the_push_without_a_service_schedules_nothing(monkeypatch):
    from kiro_crew import autonudge, conductor_wake

    monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    assert conductor_wake.wake_for_posture_change() is None


# -- the probe's wake ------------------------------------------------------------ #


def _bound_item(conductor: str, worker: str) -> str:
    work_ledger.ensure_conductor(conductor, goal="keep the host healthy")
    item_id = str(
        work_ledger.apply_conductor_action(
            conductor, "create", title="a worker", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    work_ledger.apply_conductor_action(
        conductor, "bind", item_id=item_id, worker_session_key=worker
    )
    return item_id


def _posture_briefs(conductor: str, job: str = "loop-1") -> list[str]:
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    ctx = SimpleNamespace(message=json.dumps({"conductor": conductor}), job=SimpleNamespace(id=job))
    probe = WorkLedgerProbe(worker_running=lambda _k: True)
    probe.identity(ctx)
    return [o.brief for o in probe.observe(ctx).observations if "host posture" in o.brief]


def test_one_wake_per_level_change_per_conductor_and_none_on_a_repeat(monkeypatch):
    _bound_item(CONDUCTOR, "chat-w1")
    _bound_item(OTHER_CONDUCTOR, "chat-w2")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_AMPLE, "memory 8.0 GB free"))
    assert _posture_briefs(CONDUCTOR) == []  # ample with nothing seen is the baseline
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.1 GB free"))
    for conductor in (CONDUCTOR, OTHER_CONDUCTOR):
        assert _posture_briefs(conductor) == [
            "[work-ledger wake] host posture is tight (memory 3.1 GB free)"
        ]
        assert _posture_briefs(conductor) == []
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_AMPLE, "memory 8.0 GB free"))
    assert len(_posture_briefs(CONDUCTOR)) == 1  # back to ample is a change too
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    assert len(_posture_briefs(CONDUCTOR)) == 1  # flapping back is one more


def test_a_posture_change_releases_a_person_wait_hold(monkeypatch):
    """Every open item waits on a person, so the patrol holds on an unchanged
    fingerprint; a new posture level must change it so the posture wake runs."""
    from kiro_crew.probes.work_ledger import person_wait_fingerprint

    item_id = _bound_item(CONDUCTOR, "chat-w1")
    work_ledger.apply_worker_report(
        CONDUCTOR, item_id, status="blocked", summary="waits on approval", reason="approval"
    )
    held = person_wait_fingerprint(CONDUCTOR)
    assert held is not None
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.1 GB free"))
    tight = person_wait_fingerprint(CONDUCTOR)
    assert tight not in (None, held)
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    assert person_wait_fingerprint(CONDUCTOR) == tight


def test_no_posture_yet_wakes_nobody():
    _bound_item(CONDUCTOR, "chat-w1")
    assert _posture_briefs(CONDUCTOR) == []


# -- the per-worker rows --------------------------------------------------------- #


def _sessions(*rows: dict) -> dict:
    return {"sessions": list(rows), "tasks": []}


def test_worker_rows_hold_only_the_callers_own_bound_items(monkeypatch, tmp_path):
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    first = _bound_item(CONDUCTOR, "chat-w1")
    second = _bound_item(CONDUCTOR, "chat-w2")
    _bound_item(OTHER_CONDUCTOR, "chat-stranger")
    monkeypatch.setattr(sms, "_largest_test_tree_mb", lambda _pid: 0.0)
    payload = _sessions(
        {"key": "dashboard_chat-w1", "pid": 11, "sharers": 1, "rss_mb": 900.0, "procs": 7},
        {"key": "chat-w2", "pid": 12, "sharers": 1, "rss_mb": 300.0, "procs": 2},
        {"key": "chat-stranger", "pid": 13, "sharers": 1, "rss_mb": 5000.0, "procs": 40},
    )
    rows = sms.caller_worker_rows(
        payload,
        CONDUCTOR,
        running=lambda key: key == "chat-w1",
        project_of=lambda _key: str(tmp_path),
    )
    by_item = {r["item_id"]: r for r in rows}
    assert sorted(by_item) == sorted([first, second])
    one, two = by_item[first], by_item[second]
    assert one["rss_mb"] == 900.0 and one["procs"] == 7 and one["turn"] == "busy"
    assert two["turn"] == "idle" and two["test_mb"] == 0.0
    assert isinstance(one["volume_free_pct"], float)
    # Item ids are random hex, so they are left out before probing for fragments.
    served = json.dumps([{k: v for k, v in r.items() if k != "item_id"} for r in rows])
    for secret in ("chat-", "pid", str(tmp_path), "5000"):
        assert secret not in served
    assert set(rows[0]) == {"item_id", "rss_mb", "procs", "test_mb", "turn", "volume_free_pct"}


def test_a_caller_with_no_board_or_a_closed_item_gets_no_rows(monkeypatch):
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    item_id = _bound_item(CONDUCTOR, "chat-w1")
    kwargs = {"running": lambda _k: False, "project_of": lambda _k: ""}
    assert sms.caller_worker_rows(_sessions(), "chat-not-a-conductor", **kwargs) == []
    work_ledger.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    assert sms.caller_worker_rows(_sessions(), CONDUCTOR, **kwargs) == []


def test_a_worker_rebound_elsewhere_is_not_listed(monkeypatch):
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    _bound_item(CONDUCTOR, "chat-w1")
    monkeypatch.setattr(work_ledger, "read_binding", lambda _key: (OTHER_CONDUCTOR, "it_x"))
    rows = sms.caller_worker_rows(
        _sessions(), CONDUCTOR, running=lambda _k: False, project_of=lambda _k: ""
    )
    assert rows == []


def test_a_shared_runtime_withholds_counts_and_the_tree_walk(monkeypatch):
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    _bound_item(CONDUCTOR, "chat-w1")
    walked: list[object] = []
    monkeypatch.setattr(sms, "_largest_test_tree_mb", lambda pid: walked.append(pid) or 0.0)
    payload = _sessions({"key": "chat-w1", "pid": 11, "sharers": 3, "rss_mb": 100.0, "procs": 9})
    (row,) = sms.caller_worker_rows(
        payload, CONDUCTOR, running=lambda _k: False, project_of=lambda _k: ""
    )
    assert row["procs"] is None and row["test_mb"] is None and walked == []


@pytest.mark.parametrize(
    ("argv", "runner"),
    [
        (b"pytest\0-n\0002\0", True),
        (b"python3\0-m\0pytest\0", True),
        (b"/venv/bin/python\0/venv/bin/pytest\0tests/\0", True),
        (b"node\0/work/pytest-dashboard/server.js\0", False),
        (b"bash\0-c\0sleep 5\0", False),
    ],
)
def test_an_interpreter_prefixed_pytest_counts_as_a_test_run(argv, runner):
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    assert sms._measures_as_test_runner(argv) is runner


def test_the_tree_walk_counts_a_run_left_in_the_runtimes_session(monkeypatch):
    """A run reparented to init but still in the runtime's session is its tree."""
    from kiro_crew import platform_compat
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    asked: list[bool] = []

    def child_map(*, session_members=False, **_kw):
        asked.append(session_members)
        return {10: [11]}

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform_compat, "proc_child_map", child_map)
    monkeypatch.setattr(sms, "_proc_cmdline", lambda pid: b"pytest\0" if pid == 11 else b"sh\0")
    import kiro_crew.session_pid as sp
    import kiro_crew.session_pid_rss as spr

    for module in (sp, spr):
        monkeypatch.setattr(module, "_read_rss_pages", lambda pid: 256 * 100)
    assert sms._largest_test_tree_mb(10) == round(256 * 100 * sp._PAGE_SIZE / 2**20, 1)
    assert asked == [True]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="walks /proc")
def test_the_largest_test_tree_is_measured_from_a_real_process_tree():
    """A ``pytest``-named child and its own child are one tree, below a plain shell."""
    from kiro_crew.dashboard.handlers import session_memory_scope as sms

    script = "exec -a pytest bash -c 'sleep 30 & wait' & wait"
    # Its own process group, so the forked ``pytest`` bash and its sleep are reaped too.
    shell = subprocess.Popen(["bash", "-c", script], start_new_session=True)
    try:
        from kiro_crew.testing.wait import wait_until

        size = wait_until(lambda: sms._largest_test_tree_mb(shell.pid))
        assert size > 0
        assert sms._largest_test_tree_mb(2**30) == 0.0  # no such pid
    finally:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait(timeout=30)
    assert sms._largest_test_tree_mb(None) is None


def test_the_tool_renders_a_your_workers_block():
    from kiro_crew.mcp_tools.spawn_parts.session_memory import render_session_memory

    lines = render_session_memory(
        {
            "self": None,
            "runs": {"count": 0},
            "workers": [
                {
                    "item_id": "it_aaaa1111",
                    "rss_mb": 900.0,
                    "procs": 7,
                    "test_mb": 640.0,
                    "turn": "busy",
                    "volume_free_pct": 42.0,
                },
                {"item_id": "it_bbbb2222", "test_mb": 0.0, "turn": "idle"},
            ],
        }
    )
    assert "  Your workers (by item):" in lines
    assert "    it_aaaa1111: 900 MB, 7 processes, tests 640 MB, busy, work volume 42% free" in lines
    assert (
        "    it_bbbb2222: unmeasured, ? processes, no test run, idle, work volume unmeasured"
        in lines
    )


def test_the_tool_renders_no_workers_block_without_rows():
    from kiro_crew.mcp_tools.spawn_parts.session_memory import render_session_memory

    assert not any("Your workers" in line for line in render_session_memory({"runs": {}}))


# -- a step of decline at the same level (rfc-resource-control.md §4.2) --------- #


def _feed_free(
    notifier,
    memory: str,
    free_gb: float,
    disk_pct: float | None = None,
    disk_level: str = POSTURE_AMPLE,
) -> None:
    notifier._disk = (disk_level, "")
    notifier._disk_free_pct = disk_pct
    notifier.observe_posture(_status(memory, free_gb))


def test_tight_steps_wake_per_gb_and_a_repeat_or_a_smaller_fall_does_not():
    """RFC §10 test 6."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed_free(notifier, POSTURE_TIGHT, 4.0)
    assert pulls == [1] and rp.worsened() is None
    _feed_free(notifier, POSTURE_TIGHT, 3.5)
    assert pulls == [1] and rp.worsened() is None
    _feed_free(notifier, POSTURE_TIGHT, 3.0)
    assert pulls == [1, 1]
    token, text = rp.worsened()
    assert token.startswith("tight:") and token.endswith(":1:mem3.0:disk-")
    assert text == (
        "host posture still tight and worse (memory 3.0 GB free, was 4.0 GB at the last wake)"
    )
    _feed_free(notifier, POSTURE_TIGHT, 3.0)
    assert pulls == [1, 1]


def test_a_recovery_inside_the_level_wakes_nobody_and_raises_the_reference():
    """RFC §10 test 7."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed_free(notifier, POSTURE_TIGHT, 4.0)
    _feed_free(notifier, POSTURE_TIGHT, 3.0)
    _feed_free(notifier, POSTURE_TIGHT, 3.8)
    assert len(pulls) == 2
    _feed_free(notifier, POSTURE_TIGHT, 2.8)
    assert len(pulls) == 3 and "was 3.8 GB" in rp.worsened()[1]


def test_the_critical_step_is_half_a_gb():
    """RFC §10 test 8."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed_free(notifier, POSTURE_CRITICAL, 1.9)
    _feed_free(notifier, POSTURE_CRITICAL, 1.5)
    assert pulls == [1]
    _feed_free(notifier, POSTURE_CRITICAL, 1.4)
    token = rp.worsened()[0]
    assert pulls == [1, 1] and token.startswith("critical:") and token.endswith(":mem1.4:disk-")


def test_the_worst_disk_steps_two_points_while_tight():
    """RFC §10 test 9."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    for pct in (9.0, 7.5):
        _feed_free(notifier, POSTURE_AMPLE, 30.0, disk_pct=pct, disk_level=POSTURE_TIGHT)
    assert pulls == [1]
    _feed_free(notifier, POSTURE_AMPLE, 30.0, disk_pct=7.0, disk_level=POSTURE_TIGHT)
    assert pulls == [1, 1]
    assert rp.worsened()[1] == (
        "host posture still tight and worse " "(worst temp root 7% free, was 9% at the last wake)"
    )


def test_only_the_axis_at_the_level_can_step():
    """Opus 0f80ad76b09c: on a host tight on disk alone, ample memory swinging
    30 -> 29 -> 30 -> 29 GB is no step; likewise a disk swing on a memory-tight host."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    for free in (30.0, 29.0, 30.0, 29.0, 28.0):
        _feed_free(notifier, POSTURE_AMPLE, free, disk_pct=8.0, disk_level=POSTURE_TIGHT)
    assert pulls == [1] and rp.worsened() is None
    pulls.clear()
    other = _notifier(pulls, [(POSTURE_AMPLE, "")])
    for pct in (60.0, 55.0, 60.0, 50.0):
        _feed_free(other, POSTURE_TIGHT, 3.5, disk_pct=pct)
    assert pulls == [1] and rp.worsened() is None
    # A real fall on the binding axis still steps.
    _feed_free(other, POSTURE_TIGHT, 2.5, disk_pct=50.0)
    assert len(pulls) == 2 and "was 3.5 GB" in rp.worsened()[1]


def test_a_level_change_clears_the_step_and_ample_never_steps():
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed_free(notifier, POSTURE_TIGHT, 3.8)
    _feed_free(notifier, POSTURE_TIGHT, 2.5)
    assert rp.worsened() is not None
    _feed_free(notifier, POSTURE_CRITICAL, 1.9)
    assert rp.worsened() is None and len(pulls) == 3
    _feed_free(notifier, POSTURE_AMPLE, 9.0)
    _feed_free(notifier, POSTURE_AMPLE, 5.0)  # ample falling is not news
    assert rp.worsened() is None and len(pulls) == 4


def test_the_disk_read_reports_the_worst_free_space(monkeypatch):
    monkeypatch.setattr(rp, "temp_roots", lambda: ["/a", "/b"])
    monkeypatch.setattr(
        rp, "temp_headroom", lambda root: _headroom(root, 40.0 if root == "/a" else 7.5)
    )
    reading = rp._read_disk_posture()
    assert reading[0] == POSTURE_TIGHT and reading[2] == 7.5


def test_the_probe_wakes_once_per_step_and_not_without_one(monkeypatch):
    _bound_item(CONDUCTOR, "chat-w1")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.1 GB free"))
    assert len(_posture_briefs(CONDUCTOR)) == 1  # the level wake
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 2.0 GB free"))
    assert _posture_briefs(CONDUCTOR) == []  # no step published: no re-wake
    step = ("tight:mem2.0:disk-", "host posture still tight and worse (memory 2.0 GB free)")
    monkeypatch.setattr(rp, "_worsened", step)
    assert _posture_briefs(CONDUCTOR) == [f"[work-ledger wake] {step[1]}"]
    assert _posture_briefs(CONDUCTOR) == []
    monkeypatch.setattr(rp, "_worsened", ("tight:mem1.0:disk-", "host posture still tight"))
    assert len(_posture_briefs(CONDUCTOR)) == 1


def test_steps_charge_the_posture_budget_and_stop_at_its_cap(monkeypatch):
    """RFC §10 test 12, step half."""
    from kiro_crew import ledger_wake

    _bound_item(CONDUCTOR, "chat-w1")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.9 GB free"))
    woke = len(_posture_briefs(CONDUCTOR))
    for step in range(1, 3 * ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR):
        monkeypatch.setattr(rp, "_worsened", (f"tight:mem{step}", "host posture still tight"))
        woke += len(_posture_briefs(CONDUCTOR))
    assert woke == ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR


# -- a worker that grows (rfc-resource-control.md §4.3) --------------------------- #

_GB = 1024.0
_HOST = 64 * _GB


def test_the_share_wakes_once_and_re_arms_only_below_twenty_percent():
    """RFC §10 test 10, on an ample host."""

    def step(share: float) -> list[str]:
        return rp.growth_steps({"chat-w1": share * _HOST}, _HOST, POSTURE_AMPLE, 1.0)

    assert step(0.10) == []
    assert step(0.25) == ["chat-w1"]
    assert step(0.26) == []
    assert step(0.22) == [] and step(0.25) == []  # not re-armed above 20%
    assert step(0.19) == [] and step(0.25) == ["chat-w1"]
    _stamp, size, share, grown = rp.worker_growth()["chat-w1"]
    assert size == 0.25 * _HOST and round(share) == 25 and grown == 0.0


def test_growth_wakes_on_a_tight_host_and_not_on_an_ample_one():
    """RFC §10 test 11."""
    assert rp.growth_steps({"chat-w1": 1 * _GB}, _HOST, POSTURE_AMPLE, 1.0) == []
    assert rp.growth_steps({"chat-w1": 4 * _GB}, _HOST, POSTURE_AMPLE, 2.0) == []
    # Measured from the last wake about it (here its first sight), so tight wakes now.
    assert rp.growth_steps({"chat-w1": 4 * _GB}, _HOST, POSTURE_TIGHT, 3.0) == ["chat-w1"]
    assert rp.worker_growth()["chat-w1"][3] == 3 * _GB
    assert rp.growth_steps({"chat-w1": 5.9 * _GB}, _HOST, POSTURE_TIGHT, 4.0) == []
    assert rp.growth_steps({"chat-w1": 6 * _GB}, _HOST, POSTURE_TIGHT, 5.0) == ["chat-w1"]
    assert rp.growth_steps({}, _HOST, POSTURE_TIGHT, 6.0) == []
    assert rp.worker_growth() == {}  # a session that ended drops out


def _growth_briefs(job: str = "loop-1") -> list[str]:
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    ctx = SimpleNamespace(message=json.dumps({"conductor": CONDUCTOR}), job=SimpleNamespace(id=job))
    probe = WorkLedgerProbe(worker_running=lambda _k: True)
    probe.identity(ctx)
    return [o.brief for o in probe.observe(ctx).observations if "worker it_" in o.brief]


def test_the_growth_wake_names_the_item_and_charges_its_budget(monkeypatch):
    """RFC §10 test 12, growth half: the item's budget, not the posture entry's."""
    from kiro_crew import ledger_wake
    from kiro_crew.conductor_wake import POSTURE_ITEM

    item_id = _bound_item(CONDUCTOR, "chat-w1")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    monkeypatch.setattr(
        rp,
        "_worker_growth",
        {
            "chat-w1": ("1.000", 16.2 * _GB, 27.0, 3.1 * _GB),
            "chat-not-a-worker": ("1.000", 20 * _GB, 60.0, 4 * _GB),
        },
    )
    assert _growth_briefs() == [
        f"[work-ledger wake] worker {item_id} grew to 16.2 GB "
        "(27% of RAM; +3.1 GB since the last wake)"
    ]
    assert _growth_briefs() == []  # the same step wakes nobody again
    assert ledger_wake.already_woke(CONDUCTOR, item_id, "growth:1.000", watcher="loop-1")
    assert not ledger_wake.already_woke(CONDUCTOR, POSTURE_ITEM, "growth:1.000", watcher="loop-1")
    monkeypatch.setattr(rp, "_worker_growth", {"chat-w1": ("2.000", 9 * _GB, 28.0, 0.0)})
    assert _growth_briefs() == [f"[work-ledger wake] worker {item_id} holds 9.0 GB (28% of RAM)"]


def test_growth_wakes_stop_at_the_items_hourly_cap(monkeypatch):
    from kiro_crew import ledger_wake

    _bound_item(CONDUCTOR, "chat-w1")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    woke = 0
    for step in range(3 * ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR):
        monkeypatch.setattr(
            rp, "_worker_growth", {"chat-w1": (f"{step}.000", 9 * _GB, 28.0, 2 * _GB)}
        )
        woke += len(_growth_briefs())
    assert woke <= ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR


def test_a_step_or_growth_releases_a_person_wait_hold(monkeypatch):
    from kiro_crew.probes.work_ledger import person_wait_fingerprint

    item_id = _bound_item(CONDUCTOR, "chat-w1")
    work_ledger.apply_worker_report(
        CONDUCTOR, item_id, status="blocked", summary="waits on approval", reason="approval"
    )
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.1 GB free"))
    held = person_wait_fingerprint(CONDUCTOR)
    monkeypatch.setattr(rp, "_worsened", ("tight:mem2.0:disk-", "worse"))
    worse = person_wait_fingerprint(CONDUCTOR)
    assert worse not in (None, held)
    monkeypatch.setattr(rp, "_worker_growth", {"chat-w1": ("1.000", 9 * _GB, 28.0, 0.0)})
    assert person_wait_fingerprint(CONDUCTOR) not in (None, held, worse)
    monkeypatch.setattr(rp, "_worker_growth", {"chat-other": ("1.000", 9 * _GB, 28.0, 0.0)})
    assert person_wait_fingerprint(CONDUCTOR) == worse  # not this board's worker


def test_a_watchdog_reading_pulls_once_and_folds_dashboard_keys(monkeypatch):
    pulled: list[int] = []
    monkeypatch.setattr(rp, "_pull_watching_conductors", lambda: pulled.append(1))
    monkeypatch.setattr("kiro_crew.platform_compat.host_total_mib", lambda: int(_HOST))
    rp.note_session_rss({"dashboard_chat-w1": 0.3 * _HOST, "chat-loose": 0.3 * _HOST})
    assert pulled == [1]
    assert set(rp.worker_growth()) == {"chat-w1", "chat-loose"}
    rp.note_session_rss({"chat-w1": 0.3 * _HOST, "chat-loose": 0.3 * _HOST})
    assert pulled == [1]  # no new step, no pull


def test_the_watchdog_hands_over_only_own_runtime_sizes(monkeypatch):
    from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP

    seen: list[dict] = []
    monkeypatch.setattr(rp, "note_session_rss", lambda sizes: seen.append(dict(sizes)))
    monkeypatch.setattr(RUNTIME_OWNERSHIP, "leases_on_pid", lambda pid: 3 if pid == 12 else 1)
    sessions = [("chat-a", 11, None), ("chat-b", 12, None), ("chat-c", 13, None)]
    rp.note_watchdog_sizes(sessions, {11: 900, 12: 5000})
    assert seen == [{"chat-a": 900.0}]


def test_a_renewed_fall_to_an_earlier_reading_is_a_new_step():
    """GPT a8b37eb49a9a / Opus 74fc18755a62: 4 -> 3 -> 4 -> 3 in one tight period,
    and the same reading in a later tight period, each publish a token not seen yet."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    tokens: list[str] = []
    for level, free in [
        (POSTURE_TIGHT, 4.0),
        (POSTURE_TIGHT, 3.0),
        (POSTURE_TIGHT, 4.0),
        (POSTURE_TIGHT, 3.0),
        (POSTURE_AMPLE, 9.0),
        (POSTURE_TIGHT, 4.0),
        (POSTURE_TIGHT, 3.0),
    ]:
        _feed_free(notifier, level, free)
        if rp.worsened() is not None and rp.worsened()[0] not in tokens:
            tokens.append(rp.worsened()[0])
    assert len(tokens) == 3 and all(t.endswith(":mem3.0:disk-") for t in tokens)


def test_the_probe_wakes_again_for_the_same_reading_in_a_later_tight_period(monkeypatch):
    """Opus 74fc18755a62, end to end through the notifier and the probe."""
    _bound_item(CONDUCTOR, "chat-w1")
    notifier = _notifier([], [(POSTURE_AMPLE, "")])
    worse: list[str] = []
    for level, free in [
        (POSTURE_TIGHT, 4.0),
        (POSTURE_TIGHT, 3.0),
        (POSTURE_AMPLE, 9.0),
        (POSTURE_TIGHT, 4.0),
        (POSTURE_TIGHT, 3.0),
    ]:
        _feed_free(notifier, level, free)
        worse += [b for b in _posture_briefs(CONDUCTOR) if "still tight" in b]
    assert len(worse) == 2


def test_two_workers_growing_in_one_tick_both_wake(monkeypatch):
    """GPT bd25b95e6287: one shared stamp must not collapse two items' wakes."""
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    first = _bound_item(CONDUCTOR, "chat-w1")
    second = _bound_item(CONDUCTOR, "chat-w2")
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    step = ("1.000", 9 * _GB, 28.0, 2 * _GB)
    monkeypatch.setattr(rp, "_worker_growth", {"chat-w1": step, "chat-w2": step})
    from kiro_crew.probes import work_ledger as probe_module
    from kiro_crew.testing.clock import ManualClock

    # A frozen clock on the probe's own binding: both wakes are built at one instant.
    ManualClock().install(monkeypatch, probe_module)
    ctx = SimpleNamespace(
        message=json.dumps({"conductor": CONDUCTOR}), job=SimpleNamespace(id="loop-1")
    )
    probe = WorkLedgerProbe(worker_running=lambda _k: True)
    probe.identity(ctx)
    keys = [o.key for o in probe.observe(ctx).observations if "worker it_" in o.brief]
    assert len(set(keys)) == 2
    assert any(first in k for k in keys) and any(second in k for k in keys)


def test_the_conductor_is_told_when_growth_wakes_are_off(monkeypatch):
    """RFC §10 test 13: ``watchdog_rss_max_mb = 0`` silences the wake, and says so."""
    from kiro_crew.config import live
    from kiro_crew.dashboard.handlers import session_memory_scope as sms
    from kiro_crew.mcp_tools.spawn_parts.session_memory import render_session_memory

    off = SimpleNamespace(session=SimpleNamespace(watchdog_rss_max_mb=0))
    on = SimpleNamespace(session=SimpleNamespace(watchdog_rss_max_mb=4096))
    monkeypatch.setattr(live, "snapshot", lambda: off)
    assert sms._growth_wake_on() is False
    monkeypatch.setattr(live, "snapshot", lambda: on)
    assert sms._growth_wake_on() is True
    lines = render_session_memory(
        {"runs": {}, "workers": [{"item_id": "it_a", "turn": "idle"}], "growth_wake": "off"}
    )
    assert any(line.startswith("  Worker growth wakes: off") for line in lines)


def test_a_remote_workers_key_never_takes_a_local_trees_growth(monkeypatch):
    """GPT review: a remote item's key names a peer's session, so a local tree with
    the same key must not wake it or spend its budget."""
    import dataclasses

    _bound_item(CONDUCTOR, "chat-w1")
    listed = work_ledger.list_work_items
    monkeypatch.setattr(
        work_ledger,
        "list_work_items",
        lambda key: [dataclasses.replace(i, worker_instance_id="peer-1") for i in listed(key)],
    )
    monkeypatch.setattr(rp, "_current_posture", (POSTURE_TIGHT, "memory 3.0 GB free"))
    monkeypatch.setattr(rp, "_worker_growth", {"chat-w1": ("1.000", 9 * _GB, 28.0, 2 * _GB)})
    assert _growth_briefs() == []


def test_a_step_on_one_axis_resets_both_references():
    """GPT a8b37eb49a9a (head 2084c7be55): with both axes tight, (4 GB, 9%) ->
    (3 GB, 8%) steps on memory; (2.9 GB, 7%) is then only 0.1 GB / 1 point below
    that wake, so it is no step."""
    pulls: list[int] = []
    notifier = _notifier(pulls, [(POSTURE_AMPLE, "")])
    _feed_free(notifier, POSTURE_TIGHT, 4.0, disk_pct=9.0, disk_level=POSTURE_TIGHT)
    _feed_free(notifier, POSTURE_TIGHT, 3.0, disk_pct=8.0, disk_level=POSTURE_TIGHT)
    assert len(pulls) == 2
    _feed_free(notifier, POSTURE_TIGHT, 2.9, disk_pct=7.0, disk_level=POSTURE_TIGHT)
    assert len(pulls) == 2
    _feed_free(notifier, POSTURE_TIGHT, 2.9, disk_pct=6.0, disk_level=POSTURE_TIGHT)
    assert len(pulls) == 3 and "was 8% at the last wake" in rp.worsened()[1]
