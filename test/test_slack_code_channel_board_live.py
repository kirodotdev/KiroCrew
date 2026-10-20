"""The work board follows its workers wherever they run, and never goes stale.

Build 7 of the Slack Code integration: a conductor's workers in their own
worktrees (no channel folder) drive the board and the channel's status; every
event that changes a row refreshes it, with a once-a-minute backstop that
survives a restart; a failed edit is retried, a refused plan block falls back;
the board is Slack's own plan block; the step a working row shows is redacted
before it is cut; and a code channel shows no reaction ladder.

Shares the board suite's fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import test_slack_code_channel_board as suite

from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.workspace import service
from kiro_crew.testing.wait import async_wait_until

# The board suite's fixtures (autouse ones included) and helpers, bound here so
# they apply to this module too.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
publisher_clock = suite.publisher_clock
slack = suite.slack
StepClock = suite.StepClock
_Harness = suite._Harness
_Item = suite._Item
_SlackError = suite._SlackError
_NOW = suite._NOW
_binding = suite._binding
_link = suite._link
_publish = suite._publish
_reader = suite._reader
_row = suite._row
_snapshot = suite._snapshot

# ── build 7: the board follows its workers, wherever they run ──────────────


class _Slots:
    """A slot table both readers understand (``_slots`` and ``get_slot``)."""

    def __init__(self, **slots):
        self._slots = dict(slots)

    def get_slot(self, key):
        return self._slots.get(key)

    def slot_exists(self, key):
        return key in self._slots


def _live_slot(key, *, running=False, generation=0, agent="kirocrew-worker", project=""):
    return SimpleNamespace(
        key=key,
        running=running,
        turn_running=running,
        _turn_generation=generation,
        _queue=[],
        _approval_futures={},
        folder_id="F-elsewhere",
        agent=agent,
        project=project,
        messages=[],
    )


@pytest.fixture
def lineage(tmp_path):
    """A real code-channel store holding the conductor's worker records."""
    from kiro_crew.slack.code_channels import store as cc_store

    real = cc_store.CodeChannelStore(tmp_path / "slack-code-channels.json")
    real.load()
    cc_store.set_store(real)
    yield real
    cc_store.set_store(None)


def _watched(tmp_path, monkeypatch, *, folder_id=""):
    """A channel with NO folder (Kyle's build-7 channel) and a recording service."""
    from kiro_crew.slack.code_channels import store as cc_store
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    cc_store.store().put_binding(_binding())  # the lineage fixture's store
    # A cached board was published under the scope check: admitted here.
    source.set_scope_predicate(lambda state, binding: True)
    h = _Harness(tmp_path)
    rec = WorkspaceRecord(
        team_id="T1",
        channel_id="C1",
        session_key="chat-cond",
        worktree=str(tmp_path),
        folder_id=folder_id,
    )
    h.svc.store.put(rec)
    scheduled: list[float] = []
    monkeypatch.setattr(
        h.svc, "schedule_refresh", lambda binding, *, delay=0.0: scheduled.append(delay)
    )
    statuses: list[str] = []

    async def set_status(channel_id, status):
        statuses.append(status)
        return True

    monkeypatch.setattr(service, "_set_status", set_status)
    return h, rec, scheduled, statuses


def test_workers_in_their_own_worktrees_drive_the_board_and_the_status(
    tmp_path, monkeypatch, lineage
):
    """The build-7 bug: no channel folder, workers filed elsewhere -> never watched."""
    lineage.record_worker("chat-w1", "chat-cond")
    lineage.record_worker("chat-w2", "chat-cond")
    h, _rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    w1 = _live_slot("chat-w1", project=str(tmp_path / "wt-bitbucket"))
    w2 = _live_slot("chat-w2", project=str(tmp_path / "wt-gitlab"))
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1, "chat-w2": w2})
    asyncio.run(h.svc.watch_tick(dash))  # first pass only records
    assert scheduled == [] and statuses == []
    w1.turn_running, w1._turn_generation = True, 1  # a worker starts "fresh from main"
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [service.BOARD_DEBOUNCE_SECS] and statuses == ["processing"]
    w1.turn_running = False
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled[-1] == 0.0 and statuses[-1] == "active"


def test_a_board_posted_before_the_workers_started_still_follows_them(
    tmp_path, monkeypatch, lineage
):
    """The board names the workers even before the store's records are read."""
    h, rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    h.svc._board_refreshed[rec.key] = h.svc._clock()
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]
    del dash._slots["chat-w1"]  # the worker's session closed mid-turn
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled[-1] == 0.0


def test_a_workers_own_sessions_are_not_on_the_board(tmp_path, monkeypatch, lineage):
    """One level only: a worker's sub-worker is neither watched nor counted."""
    lineage.record_worker("chat-w1", "chat-cond")
    lineage.record_worker("chat-sub", "chat-w1")
    h, rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    sub = _live_slot("chat-sub")
    dash = _Slots(**{"chat-w1": _live_slot("chat-w1"), "chat-sub": sub})
    asyncio.run(h.svc.watch_tick(dash))
    sub.turn_running, sub._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [] and statuses == []
    assert set(h.svc._lineage_of(dash, h.svc.store.get("T1", "C1"))) == {"chat-w1"}


@pytest.mark.parametrize("agent", ["kirocrew-conductor", "kirocrew-team-lead"])
def test_a_team_lead_channel_is_treated_like_a_conductor_channel(
    tmp_path, monkeypatch, lineage, agent
):
    """No branch on the agent: watch, status, approval trigger and lineage are identical."""
    from kiro_crew.dashboard.session_control_parts.caller_access import code_channel_root

    lineage.record_worker("chat-w1", "chat-cond")
    lineage.put_binding(_binding())
    h, _rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond", agent=agent), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [service.BOARD_DEBOUNCE_SECS] and statuses == ["processing"]
    assert code_channel_root(dash, w1) == "chat-cond"
    armed: list[str] = []
    monkeypatch.setattr(board_pkg, "_schedule", lambda channel, delay=None: armed.append(channel))
    board_pkg.note_session_turn("chat-w1")  # an approval on a worker no board names yet
    assert armed == ["C1"]


def test_the_backstop_refreshes_a_live_board_once_a_minute_and_stops_when_idle(
    tmp_path, monkeypatch, lineage
):
    clock = StepClock(start=0.0, step=0.0)
    h, rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    h.svc._clock = clock
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU),))
    h.svc._board_refreshed[rec.key] = 0.0
    dash = _Slots()
    clock.now = 30.0
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == []  # refreshed 30s ago
    clock.now = service.BOARD_BACKSTOP_SECS + 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(state=model.IDLE),))
    clock.now += 600
    asyncio.run(h.svc.watch_tick(dash))
    assert len(scheduled) == 1  # nothing working or waiting: no backstop


def test_a_board_from_before_a_restart_is_read_again_on_the_first_pass(
    tmp_path, monkeypatch, lineage
):
    h, _rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    asyncio.run(h.svc.watch_tick(_Slots()))
    assert scheduled == []  # no board was ever posted here
    (tmp_path / "again").mkdir()
    h2, _rec2, scheduled2, _s2 = _watched(tmp_path / "again", monkeypatch)
    surface_store.store().update("C1", board_ts="100.1")
    asyncio.run(h2.svc.watch_tick(_Slots()))
    assert scheduled2 == [service.BOARD_DEBOUNCE_SECS]


def test_the_watcher_starts_at_gateway_boot(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    started: list[int] = []
    monkeypatch.setattr(ws_core, "_SERVICE", None)
    monkeypatch.setattr(ws_core, "start_watch", lambda: started.append(1))
    monkeypatch.setattr(ws_core, "register", lambda hooks, svc: None)
    from kiro_crew.slack.code_channels import hooks as cc_hooks

    monkeypatch.setattr(cc_hooks, "on_turn_start", lambda fn: None)

    async def boot():
        ws_core.install(lambda: None)

    asyncio.run(boot())
    assert started == [1]
    monkeypatch.setattr(ws_core, "_SERVICE", None)


# ── build 7: the publisher never leaves a live board stale ─────────────────


def _active_snapshot():
    return model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU, session_key="chat-w1"),))


def test_a_live_board_is_edited_at_least_once_a_minute_even_unchanged(slack, publisher_clock):
    publisher_clock.step = 30.0
    assert _publish(_active_snapshot()) == "posted"
    assert _publish(_active_snapshot()) == ""  # 30s later, nothing new
    publisher_clock.step = publisher.STALE_SECS
    assert _publish(_active_snapshot()) == "updated"  # stamp refreshed
    idle = model.BoardSnapshot(rows=(_row(state=model.IDLE),))
    _publish(idle)
    assert _publish(idle) == ""  # an idle board is left alone


def test_a_busy_board_is_edited_at_most_once_per_gap_and_the_rest_is_rearmed(
    slack, publisher_clock
):
    _publish(_snapshot())
    publisher_clock.step = 5.0
    moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
    assert _publish(moved) == "deferred"
    assert slack.of("update_message") == []
    assert 0 < publisher.take_retry("C1") <= publisher.MIN_EDIT_GAP_SECS
    assert publisher.take_retry("C1") == 0.0  # armed once


def test_a_failed_edit_backs_off_honours_retry_after_and_logs_once(slack, caplog):
    _publish(_snapshot())
    moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
    with caplog.at_level("DEBUG", logger=publisher.logger.name):
        slack.fail["update_message"] = "ratelimited"
        assert _publish(moved) == "failed"
        first = publisher.take_retry("C1")
        slack.fail["update_message"] = "service_unavailable"
        assert _publish(moved) == "failed"
        second = publisher.take_retry("C1")
    assert first == 5.0 and second == 15.0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    err = _SlackError("ratelimited")
    err.response = SimpleNamespace(
        get=lambda k, d=None: "ratelimited", headers={"Retry-After": "42"}
    )
    assert publisher._retry_after(err) == 42.0
    assert _publish(moved) == "updated"  # and it recovers


def test_the_publish_wrapper_rearms_the_channels_refresh(monkeypatch):
    armed: list[tuple] = []
    monkeypatch.setattr(board_pkg, "_schedule", lambda ch, delay=None: armed.append((ch, delay)))

    async def fake_publish(binding, snapshot, *, link_for, owner_id=""):
        publisher._retry_at[binding.channel_id] = 7.0
        return "deferred"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    from kiro_crew.slack.code_channels.board import links

    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: True)
    assert asyncio.run(board_pkg.publish(_binding(), _snapshot())) == "deferred"
    assert armed == [("C1", 7.0)]


def test_the_publish_wrapper_rechecks_the_scope_before_anything_is_sent(monkeypatch):
    sent: list = []

    async def fake_publish(binding, snapshot, *, link_for, owner_id=""):
        sent.append(snapshot)
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    from kiro_crew.slack.code_channels.board import links

    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    # The channel turned shared after the board was read: nothing is published.
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: False)
    assert asyncio.run(board_pkg.publish(_binding(), _snapshot())) == "out_of_scope"
    assert sent == []


def test_a_live_boards_pin_is_put_back(slack, publisher_clock):
    _publish(_active_snapshot())
    pins = len(slack.of("add_pin"))
    publisher_clock.step = publisher.REPIN_SECS
    _publish(_active_snapshot())
    assert len(slack.of("add_pin")) == pins + 1


def test_every_edit_carries_new_block_ids_and_a_refused_plan_falls_back_once(
    slack, monkeypatch, caplog
):
    sent: list[list] = []

    async def update(channel, ts, text="", blocks=None):
        slack.calls.append(("update_message", channel, ts, text))
        slack._maybe_fail("update_message")
        sent.append(blocks)

    monkeypatch.setattr(slack, "update_message", update)
    _publish(_snapshot())
    _publish(model.BoardSnapshot(rows=_snapshot().rows[:1]))
    _publish(_snapshot())
    ids = [{b["block_id"] for b in blocks} for blocks in sent]
    assert len(ids) == 2 and not ids[0] & ids[1]
    assert sent[0][-1]["type"] == "plan"
    slack.fail["update_message"] = "invalid_blocks"
    with caplog.at_level("WARNING", logger=publisher.logger.name):
        assert _publish(model.BoardSnapshot(rows=_snapshot().rows[:1])) == "updated"
        _publish(_snapshot())
    assert all(b["type"] != "plan" for b in sent[-1])  # the rows look, from now on
    assert sum("refused the plan block" in r.getMessage() for r in caplog.records) == 1


# ── build 7: the plan block ─────────────────────────────────────────────────


def test_plan_tasks_map_states_strip_the_shared_issue_prefix_and_cap_at_fifty():
    rows = (
        _row(
            "it_1",
            model.WORKING,
            title="#22452: Bitbucket",
            step="Running pytest -q",
            turn_started_at="2026-10-20T13:26:00+00:00",
        ),
        _row(
            "it_2",
            model.DONE,
            title="#22452: GitLab",
            pr_number=7,
            pr_state="open",
            pr_url="https://github.com/acme/widgets/pull/7",
            pr_ci="running",
            pr_checks=(12, 20, 3),
        ),
        _row("it_3", model.LOST, title="#22452: Azure"),
        _row("it_4", model.CLOSED, title="#22452: Gone", closed_as="rejected"),
        _row("it_5", model.IDLE, title="#22452: Later", summary="waiting for CI"),
    )
    plan = render.plan_block(model.BoardSnapshot(rows=rows), _link, now=_NOW)
    tasks = {t["task_id"]: t for t in plan["tasks"]}
    assert [tasks[i]["status"] for i in ("it_1", "it_2", "it_3", "it_4", "it_5")] == [
        "in_progress",
        "complete",
        "error",
        "error",
        "in_progress",
    ]
    assert tasks["it_1"]["title"] == "Bitbucket"
    details = tasks["it_1"]["details"]["elements"][0]["elements"]
    assert details == [
        {"type": "text", "text": "Running "},
        {"type": "text", "text": "pytest -q", "style": {"code": True}},
        {"type": "text", "text": " · 4m"},
    ]
    output = tasks["it_2"]["output"]["elements"][0]["elements"]
    assert output[0]["type"] == "link" and output[-1]["text"] == " · CI 12/20 passing · 3 running"
    assert "Idle" in json.dumps(tasks["it_5"]["details"]) and "waiting for CI" in json.dumps(
        tasks["it_5"]["details"]
    )
    many = tuple(_row(f"it_{i:04x}", model.IDLE) for i in range(60))
    capped = render.plan_block(model.BoardSnapshot(rows=many), _link, now=_NOW)["tasks"]
    assert len(capped) == render.MAX_TASKS and capped[-1]["title"].startswith("+11 more")


# ── build 7: the step is safe to publish ────────────────────────────────────


def test_a_secret_in_a_command_is_scrubbed_even_where_the_cut_falls():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    for pad in range(40, 72):
        command = "x" * pad + f" --token {key} rest"
        step = model.step_text(
            model.Activity(tool_kind="execute", tool_input=json.dumps({"command": command}))
        )
        assert len(step) <= model.STEP_CHARS
        # Redacted BEFORE the cut: no part of the key survives wherever the cut falls.
        assert "AKIA" not in step and "ABCDEFGH" not in step, (pad, step)


def test_host_paths_become_relative_or_home_and_newlines_fold():
    home = os.path.expanduser("~")
    project = os.path.join(home, "repos", "wt-bitbucket")
    edit = model.Activity(
        project=project,
        tool_kind="edit",
        tool_input=json.dumps({"path": f"{project}/src/kiro_crew/monitor/gitlab.py"}),
    )
    assert model.step_text(edit) == "Editing src/kiro_crew/monitor/gitlab.py"
    shell = model.Activity(
        project=project,
        tool_kind="execute",
        tool_input=json.dumps({"command": f"cat {home}/notes.txt\n  /var/tmp/run.log"}),
    )
    step = model.step_text(shell)
    assert step == "Running cat ~/notes.txt …/run.log" and home not in step
    mcp = model.Activity(tool_title="🔧 @kirocrew-work/work_report", tool_kind="other")
    assert model.step_text(mcp) == "work_report"


def test_a_working_row_without_a_call_in_flight_shows_its_fresh_report_or_thinking():
    assert model.step_text(model.Activity()) == model.STEP_THINKING
    assert model.step_text(model.Activity(), report="running the GitLab tests") == (
        "running the GitLab tests"
    )
    item = _Item(
        "it_1", status="progress", summary="half done", last_report_at="2026-10-20T13:00:00Z"
    )
    assert source._fresh_progress(item, "2026-10-20T12:59:00+00:00") == "half done"
    assert source._fresh_progress(item, "2026-10-20T13:05:00+00:00") == ""  # older than the turn


def test_worker_activity_reads_the_call_in_flight_from_the_slot_rows():
    slot = _live_slot("chat-w1", running=True, project="/p")
    slot.messages = [
        {"role": "user", "content": "go", "ts": "2026-10-20T13:20:00+00:00"},
        {"role": "assistant", "content": "ok"},
        {
            "role": "tool",
            "content": "🔧 Running",
            "meta": {"kind": "execute", "input": '{"command": "make test"}'},
        },
    ]
    activity = source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")
    assert activity.turn_started_at == "2026-10-20T13:20:00+00:00"
    assert model.step_text(activity) == "Running make test"
    slot.messages.append({"role": "assistant", "content": "reading the output"})
    assert model.tool_step(source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")) == ""


# ── build 7: the channel's status follows the workers ──────────────────────


def test_the_channel_status_is_suspended_while_a_worker_waits_on_you(
    tmp_path, monkeypatch, lineage
):
    lineage.record_worker("chat-w1", "chat-cond")
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    w1 = _live_slot("chat-w1", running=True, generation=1)
    dash = _Slots(**{"chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]
    h.svc._boards[rec.key] = model.BoardSnapshot(
        rows=(_row(state=model.WAITING_ON_YOU, session_key="chat-w1"),)
    )
    h.svc._board_refreshed[rec.key] = h.svc._clock()
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses[-1] == "suspended"
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(state=model.DONE),))
    w1.turn_running = False
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses[-1] == "active"


# ── build 7: no reaction ladder in a code channel ──────────────────────────


def test_a_code_channel_gets_no_reactions_and_other_channels_keep_them(monkeypatch):
    from kiro_crew.slack.renderer import SlackRenderer

    bound = {"C-code"}
    monkeypatch.setattr(
        cc_binding, "active_binding", lambda ch, thread_ts=None: object() if ch in bound else None
    )
    assert cc_binding.no_reactions("C-code") and not cc_binding.no_reactions("D-dm")
    code = SlackRenderer(slack=None, channel="C-code", thread_ts="1.0", reactions_enabled=True)
    dm = SlackRenderer(slack=None, channel="D-dm", thread_ts="1.0", reactions_enabled=True)
    assert code._reactions_enabled is False and dm._reactions_enabled is True


# ── build 7: every event that changes a row refreshes the board ────────────


def test_every_place_that_registers_an_approval_refreshes_the_board():
    """Derived from the source: an approval future registered anywhere must reach the board."""
    import ast
    import pathlib

    root = pathlib.Path(service.__file__).parents[3]
    sites = []
    for path in root.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if "_approval_futures" not in text:
            continue
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            registers = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "_approval_futures"
                    for t in node.targets
                )
                for node in ast.walk(fn)
            )
            if registers:
                calls = {
                    getattr(n.func, "id", getattr(n.func, "attr", ""))
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                }
                sites.append((str(path.relative_to(root)), fn.name, "note_session_turn" in calls))
    assert len(sites) >= 2, sites
    assert all(ok for *_where, ok in sites), [s for s in sites if not s[2]]


#: One valid ``work/recorded`` entry per ledger action. Keyed by EVERY action the
#: vocabulary names, so a new action without a case here fails the test below.
_LEDGER_CASES = {
    "goal": [{"action": "goal", "goal": "g", "round": 1}],
    "create": [
        {
            "action": "create",
            "item_id": "it_p",
            "title": "t",
            "acceptance": {"kind": "human_approval"},
        }
    ],
    "bind": [{"action": "bind", "item_id": "it_p", "worker_session_key": "chat-w1"}],
    "report": [
        {
            "action": "report",
            "actor": "worker",
            "by": "chat-w1",
            "item_id": "it_p",
            "status": status,
            "summary": f"s-{status}",
            "last_report_at": f"2026-10-20T13:0{n}:00Z",
        }
        for n, status in enumerate(("progress", "done", "blocked", "question"))
    ],
    "decide": [{"action": "decide", "item_id": "it_p", "decision": "go on"}],
    "verdict": [{"action": "verdict", "item_id": "it_p", "verdict": "fail", "fails": 1}],
    "accept": [{"action": "accept", "item_id": "it_p", "acceptance": {"kind": "human_approval"}}],
    "close": [{"action": "close", "item_id": "it_p", "state": "abandoned", "decision": "x"}],
}


def test_every_ledger_write_and_every_report_status_refreshes_the_board(monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, bus
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.work_vocab import WORK_ACTIONS, WORK_WORKER_STATUSES

    assert set(_LEDGER_CASES) == set(WORK_ACTIONS), "add a refresh case for the new action"
    reported = {c["status"] for c in _LEDGER_CASES["report"]}
    assert reported == set(WORK_WORKER_STATUSES), "add a case for the new report status"
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    for reset in (crew_log_emit.reset_caches, crew_log_eager.stop_for_tests):
        reset()
    crew_log.forget_slot_folds()
    bus.reset_for_tests()
    seen: list[str] = []
    monkeypatch.setattr(board_pkg, "note_ledger_write", lambda key: seen.append(key))
    board = "chat-cond"
    CrewLog.create(lg.KIND_SESSION, "acp-c", owner="o", agent="lead", slot=board)

    def write(fields):
        entry = {"slot": board, "actor": "conductor", "by": board, **fields}
        assert crew_log_emit.on_work_recorded("acp-c", entry, timeout=5.0) is True
        crew_log_emit.flush()
        assert crew_log_eager.drain(timeout=10.0)

    async def body():
        board_pkg.subscribe()
        for action in ("goal", "create", "bind", "report", "decide", "verdict", "accept", "close"):
            for fields in _LEDGER_CASES[action]:
                before = len(seen)
                await asyncio.to_thread(write, fields)
                await async_wait_until(
                    lambda: len(seen) > before,
                    describe=lambda: f"{action} {fields.get('status', '')} refreshed nothing",
                )

    try:
        asyncio.run(body())
    finally:
        board_pkg.reset_for_tests()
        bus.reset_for_tests()
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
    assert set(seen) == {board}


def test_a_read_board_carries_a_working_rows_current_call_and_turn_start():
    slot = _live_slot("chat-w1", running=True, project="/p")
    slot.messages = [
        {"role": "user", "content": "go", "ts": "2026-10-20T13:20:00+00:00"},
        {
            "role": "tool",
            "content": "🔧 Running",
            "meta": {"kind": "execute", "input": '{"command": "make test"}'},
        },
    ]
    items = [_Item("it_00000001", worker_session_key="chat-w1", created_at="1")]
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), items)))
    source.set_scope_predicate(lambda state, binding: True)
    snap = asyncio.run(source.read_board(_Slots(**{"chat-w1": slot}), _binding()))
    (row,) = snap.rows
    assert row.state == model.WORKING and row.step == "Running make test"
    assert row.turn_started_at == "2026-10-20T13:20:00+00:00"


def test_ci_counts_reach_the_board_but_never_the_dashboard_chip_cache(monkeypatch):
    from kiro_crew.dashboard.source_providers import chip_refresh
    from kiro_crew.slack.code_channels.workspace import pr as pr_mod

    assert pr_mod._checks({"ciCounts": "12/20/3"}) == (12, 20, 3)
    assert pr_mod._checks({"ciCounts": "12/20"}) is None and pr_mod._checks({}) is None
    assert pr_mod._checks({"ciCounts": "1/2/x"}) is None
    assert pr_mod._checks({"ciCounts": "12345/1/0"}) is None  # over 4 digits
    assert pr_mod._checks({"ciCounts": 7}) is None
    assert chip_refresh._CHIP_CI_COUNTS == "ciCounts"
    from kiro_crew.dashboard.source_providers import chip_status

    url = "https://github.com/acme/widgets/pull/99"

    async def fetch(_url):
        return {"state": "open", "ci": "passed", "ciCounts": "1/1/0"}

    monkeypatch.setattr(chip_refresh, "_fetch_check_status", fetch)
    asyncio.run(chip_refresh._refresh_check_status(url))
    cached = chip_status._check_cache.pop(url, None)
    assert cached is not None and "ciCounts" not in json.dumps(cached, default=str)
    assert "passed" in json.dumps(cached, default=str)


# ── build 7 review fixes ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "psql postgres://admin:S3cretPw9x@db.example/app",
        "git clone https://ci-bot:S3cretPw9x@git.example/r.git",
        "curl -sS https://u:S3cretPw9x@api.example/v1",
    ],
)
def test_a_password_in_a_url_is_redacted_before_any_path_is_rewritten(command):
    step = model.step_text(
        model.Activity(tool_kind="execute", tool_input=json.dumps({"command": command}))
    )
    assert "S3cretPw9x" not in step and step.startswith("Running ")


def test_cloud_internal_host_names_never_reach_the_channel():
    shell = model.Activity(
        tool_kind="execute",
        tool_input=json.dumps(
            {"command": "ssh ip-10-0-12-7.ec2.internal uptime; ping db.internal"}
        ),
    )
    step = model.step_text(shell)
    assert "ip-10-0-12-7" not in step and "db.internal" not in step and "host" in step


def test_a_live_board_nothing_moves_is_rearmed_for_its_stamp_deadline(slack, publisher_clock):
    """The stamp is timed from the last EDIT, so an unrelated read cannot push it back."""
    publisher_clock.step = 50.0
    assert _publish(_active_snapshot()) == "posted"
    assert publisher.take_retry("C1") == publisher.STALE_SECS  # just edited
    assert _publish(_active_snapshot()) == ""  # 50s later: nothing to show yet
    assert publisher.take_retry("C1") == pytest.approx(publisher.STALE_SECS - 50.0)
    idle = model.BoardSnapshot(rows=(_row(state=model.IDLE),))
    _publish(idle)
    publisher.take_retry("C1")
    _publish(idle)
    assert publisher.take_retry("C1") == 0.0  # an idle board arms nothing


def test_a_permanent_refusal_arms_no_retry(slack):
    _publish(_snapshot())
    slack.fail["update_message"] = "channel_not_found"
    assert _publish(model.BoardSnapshot(rows=_snapshot().rows[:1])) == "failed"
    assert publisher.take_retry("C1") == 0.0


def test_a_long_rearm_waits_on_its_own_timer_not_the_refresh_loop(monkeypatch):
    """A backoff never delays the next trigger: it is a timer, then a debounced refresh."""
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    calls: list[float] = []
    fake = SimpleNamespace(schedule_refresh=lambda binding, *, delay=0.0: calls.append(delay))
    monkeypatch.setattr(ws_core, "_SERVICE", fake)
    monkeypatch.setattr(cc_binding, "active_binding", lambda ch, thread_ts=None: _binding())

    async def body():
        assert board_pkg._schedule("C1", delay=120.0)
        assert calls == []  # nothing waits inside the channel's refresh loop
        assert board_pkg._schedule("C1", delay=0.05) is True
        assert calls == [service.BOARD_DEBOUNCE_SECS]  # a trigger is published at once
        assert board_pkg._schedule("C1", delay=3.5)  # earlier than 120 s: replaces it
        due, handle = board_pkg._timers["C1"]
        assert due - asyncio.get_running_loop().time() <= 3.5
        handle.cancel()

    asyncio.run(body())


def test_the_plan_rows_beyond_the_first_twenty_get_their_pr(tmp_path, monkeypatch):
    rows = tuple(_row(f"it_{i:04x}", model.WORKING, branch=f"feat/b{i}") for i in range(30))
    pr = suite.PrLookup(
        "found", suite.PullRequest(7, "https://github.com/acme/widgets/pull/7", "open", "passed")
    )
    h = _Harness(tmp_path, prs={"feat/b25": pr})
    repo = SimpleNamespace(host="github.com", slug="acme/widgets", owner="acme", name="widgets")
    monkeypatch.setattr(service, "parse_remote", lambda url: repo)
    monkeypatch.setattr(service.git_ops, "remote_url", lambda cwd: "git@github.com:acme/widgets")
    monkeypatch.setattr(h.svc, "_checkout_dir", lambda record: str(tmp_path))
    record = SimpleNamespace(key="T1:C1", worktree=str(tmp_path))
    filled = asyncio.run(h.svc._fill_worker_prs(record, model.BoardSnapshot(rows=rows)))
    assert filled.rows[25].pr_number == 7 and "feat/b25" in h.pr_queries


def test_a_blocked_row_does_not_keep_the_board_live():
    assert model.BLOCKED not in model.ACTIVE_STATES
    assert {model.WORKING, model.WAITING_ON_YOU} == set(model.ACTIVE_STATES)


def test_the_redaction_sees_the_raw_text_before_any_rewrite(monkeypatch):
    """A rewrite (here a host name inside a key) must not break a credential's shape first."""
    monkeypatch.setattr(model, "_host_names", lambda: ("ABCD",))
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    step = model.step_text(
        model.Activity(tool_kind="execute", tool_input=json.dumps({"command": f"aws --key {key}"}))
    )
    assert "EFGHIJKLMNOP" not in step and "AKIA" not in step


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("docker run -v /srv/app:/opt/secret/data img", "…/data"),
        ("PATH=/usr/bin:/opt/acme/bin make", "…/bin make"),
        ("scp x bastion:/var/lib/app/db .", "bastion:…/db"),
        ("cat file:///etc/shadow", "file://…/shadow"),
        ("curl https://github.com/acme/widgets/pull/7", "https://github.com/acme/widgets/pull/7"),
    ],
)
def test_paths_after_a_colon_or_slash_are_still_shortened(command, want):
    step = model.step_text(
        model.Activity(tool_kind="execute", tool_input=json.dumps({"command": command}))
    )
    assert want in step, step
    assert "/opt/secret" not in step and "/var/lib" not in step and "/etc/" not in step


def test_report_text_title_and_goal_lose_host_paths_and_extra_lines(tmp_path):
    home = os.path.expanduser("~")
    items = [
        _Item(
            "it_00000001",
            title=f"Fix {home}/repos/svc on dev-dsk-foo-1a.example.com",
            status="done",
            summary=f"Fixed {home}/repos/svc/x.py on 10.0.12.7\nsecond line with notes",
            worker_session_key="chat-w1",
        )
    ]
    record = SimpleNamespace(goal=f"Ship {home}/repos/svc\nmore", round=1)
    source.set_records_reader(_reader(lambda key: (record, items)))
    source.set_scope_predicate(lambda state, binding: True)
    snap = asyncio.run(source.read_board(_Slots(), _binding()))
    (row,) = snap.rows
    published = " ".join([row.title, row.summary, snap.goal])
    assert home not in published and "dev-dsk-foo" not in published and "10.0.12.7" not in published
    assert "second line" not in published and "more" not in snap.goal
    assert row.summary.startswith("Fixed ~/repos/svc/x.py on host")


def test_internal_names_are_replaced_whole_and_public_names_kept():
    step = model.safe_line("ping foo.internal.corp then github.com and 192.168.1.20")
    assert "host" in step and ".corp" not in step and "192.168" not in step
    assert "github.com" in step


def test_the_retry_is_taken_even_when_the_publish_raises(monkeypatch):
    armed: list[tuple] = []
    monkeypatch.setattr(board_pkg, "_schedule", lambda ch, delay=None: armed.append((ch, delay)))

    async def boom(binding, snapshot, *, link_for, owner_id=""):
        publisher._retry_at[binding.channel_id] = 15.0
        raise RuntimeError("pin step blew up")

    monkeypatch.setattr(publisher, "publish", boom)
    from kiro_crew.slack.code_channels.board import links

    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: True)
    with pytest.raises(RuntimeError):
        asyncio.run(board_pkg.publish(_binding(), _snapshot()))
    assert armed == [("C1", 15.0)] and publisher.take_retry("C1") == 0.0


@pytest.mark.parametrize("filler", ["a-" * 40000, "a." * 40000], ids=["dashes", "dots"])
def test_a_huge_command_is_scrubbed_without_quadratic_backtracking(filler):
    """A 80k-character heredoc must not stall the board pass (it was minutes before)."""
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    command = f"{filler} {key} x.internal"
    hits: list[str] = []
    # Bounded by input size, not wall time: the rewrite sees at most 8x the cut.
    original = model._host_paths_out

    def counting(text, project):
        hits.append(text)
        return original(text, project)

    model_patch = pytest.MonkeyPatch()
    model_patch.setattr(model, "_host_paths_out", counting)
    try:
        step = model.step_text(
            model.Activity(tool_kind="execute", tool_input=json.dumps({"command": command}))
        )
    finally:
        model_patch.undo()
    assert len(hits) == 1 and len(hits[0]) <= model.STEP_CHARS * 8
    assert len(step) <= model.STEP_CHARS and "AKIA" not in step
    assert model._INTERNAL_HOST.search(filler) is None  # and the scan itself is linear


# ── work_aggregate: the conductor slot's sidebar field ──────────────────────


@pytest.mark.parametrize(
    ("states", "want"),
    [
        ((model.WAITING_ON_YOU, model.LOST, model.WORKING, model.DONE), "waiting"),
        ((model.LOST, model.WORKING, model.DONE), "stuck"),
        ((model.BLOCKED, model.IDLE), "stuck"),
        ((model.WORKING, model.DONE, model.IDLE), "working"),
        ((model.DONE, model.CLOSED), "done"),
        ((model.DONE, model.IDLE), "idle"),
        ((model.IDLE,), "idle"),
    ],
)
def test_work_aggregate_shape_and_worst_first_state(states, want):
    rows = tuple(_row(f"it_{i}", s) for i, s in enumerate(states))
    snap = model.BoardSnapshot(rows=rows)
    agg = model.work_aggregate(snap)
    assert set(agg) == {"state", "label", "counts"}
    assert set(agg["counts"]) == {"working", "waiting", "stuck", "done", "total"}
    assert agg["state"] == want and agg["label"] == snap.aggregate
    assert agg["counts"]["total"] == len(states)
    assert agg["counts"]["stuck"] == sum(s in {model.BLOCKED, model.LOST} for s in states)
    json.dumps(agg)  # plain data only: it rides the slot frame


def test_work_aggregate_is_cleared_without_workers():
    assert model.work_aggregate(None) is None
    assert model.work_aggregate(model.BoardSnapshot(rows=())) is None


def test_the_board_read_publishes_the_aggregate_on_the_conductor_slot(monkeypatch):
    conductor = SimpleNamespace(key="chat-cond", _work_aggregate=None)
    pushed: list[tuple] = []
    state = _Slots(**{"chat-cond": conductor})
    state.push_slot_patch = lambda key, fields: pushed.append((key, list(fields)))
    working = model.BoardSnapshot(rows=(_row("it_1", model.WORKING), _row("it_2", model.DONE)))
    board_pkg._publish_aggregate(state, _binding(), working)
    assert conductor._work_aggregate["state"] == "working"
    assert pushed == [("chat-cond", ["work_aggregate"])]
    board_pkg._publish_aggregate(state, _binding(), working)
    assert len(pushed) == 1  # unchanged: no frame
    board_pkg._publish_aggregate(state, _binding(), None)  # no workers / no board
    assert conductor._work_aggregate is None and len(pushed) == 2


def test_work_aggregate_rides_the_slot_frame_and_its_patch():
    from kiro_crew.dashboard.state_parts import slots_push
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    assert "work_aggregate" in slots_push._SLOT_PATCH_FIELDS
    slot = _ChatSlot("chat-cond")
    assert slot._work_aggregate is None
    agg = {"state": "idle", "label": "1 idle · 0 of 1 done", "counts": {}}
    slot._work_aggregate = agg
    assert slot.to_dict()["work_aggregate"] == agg


# ── green round: the watch's own refresh, status, DNS and the wrap-up ───────

repo = suite.repo


def test_the_watch_refreshes_through_the_channels_stored_binding(
    tmp_path, repo, monkeypatch, lineage
):
    """A worker's turn end publishes the board under the production scope check.

    The watch used to hand ``refresh`` a stand-in binding with no archive stamp
    or anchor, which the real scope read as refused: the board was dropped
    instead of following the worker.
    """
    from kiro_crew.slack.code_channels.store import Binding

    bound = lineage.put_binding(
        Binding(
            channel_id="C1",
            team_id="T1",
            session_key="chat-cond",
            anchor_ts="1.0",
            project_dir=str(repo),
        )
    )
    lineage.record_worker("chat-w1", "chat-cond")
    conductor = _live_slot("chat-cond")
    conductor._slack_channel = "C1"
    worker = _live_slot("chat-w1", running=True, generation=1)
    dash = _Slots(**{"chat-cond": conductor, "chat-w1": worker})
    monkeypatch.setattr(
        cc_binding, "bound_slot", lambda state, b: conductor if b.anchor_ts == "1.0" else None
    )
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: "")
    items = [
        _Item(
            "it_00000001",
            status="progress",
            worker_session_key="chat-w1",
            created_at="2026-10-23T19:00:00Z",
        )
    ]
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), items)))

    async def no_status(channel_id, status):
        return True

    monkeypatch.setattr(service, "_set_status", no_status)
    h = _Harness(tmp_path)

    async def read_board(binding):
        return await source.read_board(dash, binding)

    h.svc._read_board = read_board
    asyncio.run(h.svc.ensure(bound))

    async def go():
        await h.svc.watch_tick(dash)  # first pass records
        worker.turn_running = False
        await h.svc.watch_tick(dash)  # the worker's turn ended
        await h.svc.drain()

    asyncio.run(go())
    assert h.published and h.published[-1].rows[0].session_key == "chat-w1"
    assert h.svc.board("T1", "C1") is not None


def test_an_archived_channel_is_not_watched(tmp_path, monkeypatch, lineage):
    import dataclasses

    lineage.put_binding(dataclasses.replace(_binding(), archived_at=1.0))
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    worker = _live_slot("chat-w1")
    dash = _Slots(**{"chat-w1": worker})
    asyncio.run(h.svc.watch_tick(dash))
    worker.turn_running, worker._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [] and statuses == []


def test_a_folderless_channels_running_conductor_keeps_its_status(tmp_path, monkeypatch, lineage):
    """The conductor's own running turn owns the status: no worker sends ``active`` over it."""
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    conductor = _live_slot("chat-cond", running=True, generation=1)
    worker = _live_slot("chat-w1", running=True, generation=1)
    dash = _Slots(**{"chat-cond": conductor, "chat-w1": worker})
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    worker.turn_running = False  # the last worker stops; the conductor still runs
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_the_host_name_lookup_runs_off_the_event_loop(monkeypatch):
    """``getfqdn`` may block on reverse DNS: the board read warms it in a thread."""
    import threading

    threads: list[int] = []

    def fqdn():
        threads.append(threading.get_ident())
        return "box.example.test"

    monkeypatch.setattr(model.socket, "getfqdn", fqdn)
    monkeypatch.setattr(model.socket, "gethostname", lambda: "box")
    model._host_names.cache_clear()
    source.set_scope_predicate(lambda state, binding: True)
    item = _Item(
        "it_00000001",
        status="progress",
        summary="ran the tests",
        worker_session_key="chat-w1",
        created_at="2026-10-23T19:00:00Z",
    )
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), [item])))

    async def go():
        await source.read_board(_Slots(**{"chat-w1": _live_slot("chat-w1")}), _binding())
        return threading.get_ident()

    try:
        loop_thread = asyncio.run(go())
    finally:
        model._host_names.cache_clear()
    assert threads and loop_thread not in threads


def test_the_wrap_up_shows_no_cached_outcomes_once_the_channel_turns_shared(monkeypatch):
    """The cached board may predate the channel turning shared: the wrap-up re-checks scope."""
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    snap = model.BoardSnapshot(rows=(_row(state=model.DONE, title="Fix login", pr_number=12),))
    monkeypatch.setattr(ws_core, "service", lambda: SimpleNamespace(board=lambda t, c: snap))
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    refusal = {"why": ""}
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: refusal["why"])
    assert asyncio.run(board_pkg.outcome_lines(_binding()))  # admitted: the cached rows
    refusal["why"] = "the channel is shared with another organization"
    assert asyncio.run(board_pkg.outcome_lines(_binding())) == []


def _scoped_binding(repo):
    from kiro_crew.slack.code_channels.store import Binding

    return Binding(
        channel_id="C1",
        team_id="T1",
        session_key="chat-cond",
        anchor_ts="1.0",
        project_dir=str(repo),
    )


def _bar_labels(h):
    return [item["label"] for item in h.client.last("set_context_bar")["items"]]


def test_a_bar_sent_outside_a_refresh_never_re_sends_a_refused_cached_board(
    tmp_path, repo, monkeypatch
):
    """The context bar lists the cached board's workers: re-checked like the wrap-up."""
    binding = _scoped_binding(repo)
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    refusal = {"why": ""}
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: refusal["why"])
    h = _Harness(tmp_path)
    source.set_scope_predicate(None)  # the production scope check
    rec = asyncio.run(h.svc.ensure(binding))
    snap = model.BoardSnapshot(rows=(_row(title="Fix login", session_key="chat-w1"),))
    h.svc._boards[rec.key] = snap
    assert asyncio.run(h.svc.republish_bar(binding)) == ""
    assert any("Fix login" in label for label in _bar_labels(h))  # admitted: shown
    refusal["why"] = "the channel is shared with another organization"
    asyncio.run(h.svc.republish_bar(binding))
    assert rec.key not in h.svc._boards
    assert not any("Fix login" in label for label in _bar_labels(h))
    # A turn start that re-sends the bar (no bar stored yet) checks the same scope.
    h.svc._boards[rec.key] = snap
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    asyncio.run(h.svc.ensure(binding))
    assert rec.key not in h.svc._boards
    assert not any("Fix login" in label for label in _bar_labels(h))


def test_a_refused_refresh_forgets_the_cached_board_even_when_the_pr_read_fails(
    tmp_path, repo, monkeypatch
):
    h = _Harness(tmp_path, snapshot=None)  # the read is refused (or gone)
    binding = _scoped_binding(repo)
    rec = asyncio.run(h.svc.ensure(binding))
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(title="Fix login"),))

    async def broken(record):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(h.svc, "_refresh_pr", broken)
    asyncio.run(h.svc.refresh(binding))
    assert rec.key not in h.svc._boards


def test_the_watch_follows_the_bindings_replacement_conductor(tmp_path, monkeypatch, lineage):
    """A replaced session reaches the binding before the record: the watch reads the binding."""
    import dataclasses

    lineage.put_binding(dataclasses.replace(_binding(), session_key="chat-new"))
    lineage.record_worker("chat-w1", "chat-new")
    h, rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    assert rec.session_key == "chat-cond"  # the record still names the old session
    conductor = _live_slot("chat-new", running=True, generation=1)
    worker = _live_slot("chat-w1")
    dash = _Slots(**{"chat-new": conductor, "chat-w1": worker})
    asyncio.run(h.svc.watch_tick(dash))
    worker.turn_running, worker._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]  # the new conductor's worker
    worker.turn_running = False
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []  # the running replacement owns the status


def test_a_channel_that_turns_shared_drops_its_cached_board_on_the_next_watch_pass(
    tmp_path, monkeypatch, lineage
):
    """The cached board's waiting rows no longer set the status of a refused channel."""
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    source.set_scope_predicate(None)  # the production scope check
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    monkeypatch.setattr(
        source,
        "_publish_refusal",
        lambda state, b: "the channel is shared with another organization",
    )
    h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU),))
    h.svc._board_refreshed[rec.key] = h.svc._clock()
    asyncio.run(h.svc.watch_tick(_Slots()))
    assert rec.key not in h.svc._boards
    assert "suspended" not in statuses


# ── pre-push review: nothing the watch sets outlives a failure or the channel ──


def test_a_failed_hand_back_is_sent_again_on_the_next_pass(tmp_path, monkeypatch, lineage):
    """The workers' status is forgotten only once Slack took the hand-back."""
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, _statuses = _watched(tmp_path, monkeypatch)
    sent: list[tuple[str, bool]] = []
    answers = {"active": [False, True]}

    async def set_status(channel_id, status):
        queue = answers.get(status)
        ok = queue.pop(0) if queue else True
        sent.append((status, ok))
        return ok

    monkeypatch.setattr(service, "_set_status", set_status)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running = False
    asyncio.run(h.svc.watch_tick(dash))  # the hand-back fails (a 429, a 5xx)
    asyncio.run(h.svc.watch_tick(dash))  # ... and is sent again
    asyncio.run(h.svc.watch_tick(dash))  # taken: nothing more
    assert sent == [("processing", True), ("active", False), ("active", True)]


def _waiting_board(h, rec, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h.svc._boards[rec.key] = model.BoardSnapshot(
        rows=(_row(state=model.WAITING_ON_YOU, session_key="chat-w1"),)
    )
    h.svc._board_refreshed[rec.key] = h.svc._clock()
    return _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": _live_slot("chat-w1")})


def test_a_stale_sharing_reading_holds_the_status_instead_of_flapping(
    tmp_path, monkeypatch, lineage
):
    """A board dropped only while sharing is re-read keeps the status it set, for a while."""
    h, rec, scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"] and scheduled == []
    # The 300 s reading went stale: the scope refuses until it is read again.
    source.set_scope_predicate(lambda state, binding: False)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: True)
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert rec.key not in h.svc._boards
    assert statuses == ["suspended"]  # held, not flipped to active
    assert service.BOARD_DEBOUNCE_SECS in scheduled  # the board is re-read soon
    start = h.svc._clock()
    monkeypatch.setattr(h.svc, "_clock", lambda: start + service.BOARD_BACKSTOP_SECS + 1)
    asyncio.run(h.svc.watch_tick(dash))  # the hold ran out: judged again
    assert statuses[-1] == "active"


def test_a_definitely_refused_board_is_not_held(tmp_path, monkeypatch, lineage):
    """Only a missing reading holds the status; a channel known shared lets it go at once."""
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    source.set_scope_predicate(lambda state, binding: False)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended", "active"]


@pytest.mark.parametrize("how", ["archived", "unbound", "feature off"])
def test_a_channel_the_watch_stops_watching_is_forgotten(tmp_path, monkeypatch, lineage, how):
    """Its status is handed back, its caches, timers and the sidebar aggregate go."""
    from kiro_crew.slack.code_channels import store as cc_store

    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    conductor = dash.get_slot("chat-cond")
    conductor._work_aggregate = {"state": "waiting"}
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]
    board_pkg.remember("C1", "chat-cond", ["chat-w1"])
    publisher._retry_at["C1"] = 5.0
    if how == "archived":
        cc_store.store().update_binding("C1", archived_at=1.0)
    elif how == "unbound":
        cc_store.store().drop_binding("C1")
    else:
        monkeypatch.setattr(service, "_gate_open", lambda: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended", "active"]
    assert h.svc._known_channels() == set()
    assert conductor._work_aggregate is None
    assert "chat-w1" not in board_pkg._worker_channels and "C1" not in publisher._retry_at
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended", "active"]  # once


def test_the_backstop_does_not_retry_a_board_slack_refused_for_good(tmp_path):
    h = _Harness(tmp_path)
    key = "T1:C1"
    h.svc._boards[key] = model.BoardSnapshot(rows=(_row(),))
    h.svc._board_refreshed[key] = h.svc._clock() - service.BOARD_BACKSTOP_SECS - 1
    assert h.svc._board_due(key, {})
    publisher._failed("C1", "update", _SlackError("not_in_channel"))
    assert publisher.refused_permanently("C1") and not h.svc._board_due(key, {})
    publisher._succeeded("C1", 0.0)
    assert h.svc._board_due(key, {})


def test_a_board_pointer_the_store_could_not_record_is_edited_not_posted_again(slack, monkeypatch):
    real = publisher.update_async
    calls = {"n": 0}

    async def flaky(channel, **fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return await real(channel, **fields)

    monkeypatch.setattr(publisher, "update_async", flaky)
    assert _publish(_snapshot()) == "posted"
    moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
    assert _publish(moved) == "updated"
    assert len(slack.of("post_blocks")) == 1
    assert surface_store.store().get("C1").board_ts == "100.1"
    assert "C1" not in publisher._unsaved_ts


def test_alerts_the_store_could_not_record_do_not_page_the_owner_again(slack, monkeypatch):
    real = publisher.update_async

    async def no_alert_store(channel, **fields):
        if "board_alerts" in fields:
            raise OSError("disk full")
        return await real(channel, **fields)

    monkeypatch.setattr(publisher, "update_async", no_alert_store)
    waiting = model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU, report_stamp="r1"),))
    _publish(waiting)
    _publish(waiting)
    assert len(slack.of("post_message")) == 1


def test_the_wrap_up_reads_the_board_without_the_readers_side_effects(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as workspace

    monkeypatch.setattr(workspace, "service", lambda: None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)

    async def read_board(state, binding):
        return model.BoardSnapshot(rows=(_row(state=model.DONE, session_key="chat-w9"),))

    async def side_effects(binding):
        raise AssertionError("the full reader re-arms triggers and files workers")

    monkeypatch.setattr(source, "read_board", read_board)
    monkeypatch.setattr(board_pkg, "read", side_effects)
    lines = asyncio.run(board_pkg.outcome_lines(_binding()))
    assert len(lines) == 1 and "chat-w9" not in board_pkg._worker_channels


def test_a_refresh_that_reads_the_board_again_ends_the_status_hold(tmp_path):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))
    h.svc._status_hold["T1:C1"] = h.svc._clock() + 600
    asyncio.run(h.svc.refresh(binding))
    assert "T1:C1" in h.svc._boards and "T1:C1" not in h.svc._status_hold


def test_a_channel_that_turns_shared_during_the_refresh_gets_neither_bar_nor_board(tmp_path, repo):
    """The scope is judged again after the gh and git work, before anything is sent."""
    h = _Harness(
        tmp_path,
        snapshot=model.BoardSnapshot(rows=(_row(title="Fix login", session_key="chat-w1"),)),
    )
    binding = _scoped_binding(repo)
    asyncio.run(h.svc.ensure(binding))
    source.set_scope_predicate(lambda state, b: False)  # shared meanwhile
    asyncio.run(h.svc.refresh(binding))
    assert h.published == [] and "T1:C1" not in h.svc._boards
    assert not any("Fix login" in label for label in _bar_labels(h))


def test_a_slack_off_channel_keeps_its_board_state(tmp_path, monkeypatch, lineage):
    """Its bar is refused but it is still bound: the board's state is not wiped each pass."""
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    stored = h.svc.store.get("T1", "C1")
    stored.slack_off = "not_agent_channel"
    h.svc.store.put(stored)
    publisher._unsaved_ts["C1"] = "100.9"
    asyncio.run(h.svc.watch_tick(dash))
    assert rec.key in h.svc._boards and publisher._unsaved_ts.get("C1") == "100.9"


def test_a_forgotten_channels_hand_back_is_retried_then_given_up(tmp_path, monkeypatch, lineage):
    from kiro_crew.slack.code_channels import store as cc_store

    h, rec, _scheduled, _statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    sent: list[str] = []

    async def refused(channel_id, status):
        sent.append(status)
        return False

    monkeypatch.setattr(service, "_set_status", refused)
    cc_store.store().drop_binding("C1")
    for _ in range(service._STATUS_RETRIES + 2):
        asyncio.run(h.svc.watch_tick(dash))
    assert sent == ["active"] * service._STATUS_RETRIES
    assert h.svc._known_channels() == set()


def test_a_forgotten_channel_leaves_a_running_conductor_turns_status_alone(
    tmp_path, monkeypatch, lineage
):
    from kiro_crew.slack.code_channels import store as cc_store

    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]
    dash.get_slot("chat-cond").turn_running = True  # its own turn now owns the status
    cc_store.store().drop_binding("C1")
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"] and h.svc._known_channels() == set()


def test_a_refused_hand_back_is_given_up_after_a_few_passes(tmp_path, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, _statuses = _watched(tmp_path, monkeypatch)
    sent: list[str] = []

    async def set_status(channel_id, status):
        sent.append(status)
        return status == "processing"

    monkeypatch.setattr(service, "_set_status", set_status)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running = False
    for _ in range(service._STATUS_RETRIES + 2):
        asyncio.run(h.svc.watch_tick(dash))
    assert sent == ["processing"] + ["active"] * service._STATUS_RETRIES
