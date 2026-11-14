"""The work board's edge cases: lineage status, publish scope and the publisher.

Split from ``test_slack_code_channel_board_live.py`` (the file cap): replaced
conductors, refused bars, same-second answers and finished calls. Shares the
board suites' fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live

from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.workspace import service
from kiro_crew.testing.wait import async_wait_until, await_until

# The board suites' fixtures (autouse ones included) and helpers, bound here so
# they apply to this module too.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
publisher_clock = suite.publisher_clock
slack = suite.slack
repo = suite.repo
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
lineage = live.lineage
_Slots = live._Slots
_live_slot = live._live_slot
_watched = live._watched
_waiting_board = live._waiting_board
_active_snapshot = live._active_snapshot

# ── GPT / Opus round on 64cc0b6a74 ─────────────────────────────────────────


def test_a_long_run_of_failures_keeps_the_capped_backoff_computable():
    """GPT: 3 ** 647 overflowed a float, so the retry was never armed again."""
    publisher._failures["C1"] = 5000
    assert publisher._failed("C1", "update", _SlackError("ratelimited")) == "failed"
    assert publisher.take_retry("C1") == publisher._RETRY_CAP_SECS


def test_a_long_running_worker_keeps_asking_the_driver_for_processing(
    tmp_path, monkeypatch, lineage
):
    """GPT: Slack ends ``processing`` after an hour; the watch re-asks every pass."""
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    for _ in range(3):
        asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"] * 3


@pytest.mark.parametrize("answer", ["out_of_scope", "archived", "stopped", "denied"])
def test_a_board_not_shown_at_send_time_clears_the_sidebar_summary(monkeypatch, answer):
    from kiro_crew.slack.code_channels.board import links

    conductor = SimpleNamespace(key="chat-cond", _work_aggregate={"label": "earlier"})
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "chat-cond" else None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    source.set_scope_predicate(lambda st, b: True)

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        return answer

    monkeypatch.setattr(publisher, "publish", fake_publish)
    snap = model.BoardSnapshot(rows=(_row(),))
    assert asyncio.run(board_pkg.publish(_binding(), snap)) == answer
    assert conductor._work_aggregate is None


def test_a_refresh_that_fails_after_its_read_clears_the_sidebar_summary(tmp_path, monkeypatch):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))
    cleared: list = []
    monkeypatch.setattr(service, "_clear_aggregate", lambda b: cleared.append(b))

    async def boom(b, snap):
        raise RuntimeError("Slack went away")

    h.svc._publish_board = boom  # the board step fails after the read
    asyncio.run(h.svc.refresh(binding))
    assert cleared == [binding] and h.published == []
    board_pkg._timers.pop("C1", None)


def test_a_failing_channel_step_never_keeps_the_board_from_going_out(tmp_path, monkeypatch):
    """Adversarial F1: a channel PR read that raised every pass meant no board, ever."""
    from kiro_crew.slack.code_channels.workspace import git_ops

    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))

    async def boom(record):
        raise git_ops.SandboxUnavailable("no sandbox backend")

    monkeypatch.setattr(h.svc, "_refresh_pr", boom)
    asyncio.run(h.svc.refresh(binding))
    assert len(h.published) == 1 and "T1:C1" in h.svc._board_refreshed


def test_the_watch_loop_does_not_inherit_the_starting_turns_context(tmp_path, monkeypatch):
    """A loop started inside a turn would tie the status driver's writes to that turn."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")
    h = _Harness(tmp_path)
    seen: list[str] = []

    async def tick(state):
        seen.append(marker.get())
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_WATCH_SECS", 0.0)
    monkeypatch.setattr(h.svc, "watch_tick", tick)

    async def main():
        marker.set("the starting turn")
        h.svc.start_watch(lambda: object())
        try:
            await h.svc._watch
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert seen == [""]


# ── GPT on a061b43b5d: lineage-driven status obeys the board's publish scope ──


def _lineage_watch(tmp_path, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)  # confirmed
    w1 = _live_slot("chat-w1")
    cond = _live_slot("chat-cond")
    dash = _Slots(**{"chat-cond": cond, "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    return h, rec, statuses, w1, cond, dash


def test_a_refused_channel_gets_no_status_from_a_lineage_worker(tmp_path, monkeypatch, lineage):
    h, _rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    source.set_scope_predicate(lambda state, b: False)  # Slack Connect-shared
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_channel_refused_mid_run_hands_the_workers_status_back(tmp_path, monkeypatch, lineage):
    h, rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing", "active"] and rec.key not in h.svc._working
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing", "active"]  # once


def test_a_refused_channel_never_overrides_a_running_conductor(tmp_path, monkeypatch, lineage):
    h, _rec, statuses, w1, cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]
    cond.turn_running = True  # the conductor's own turn owns the status now
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]


def test_a_confirmed_refusal_ends_a_status_hold(tmp_path, monkeypatch, lineage):
    h, rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    h.svc._status_hold[rec.key] = h.svc._clock() + 600  # held while sharing was unknown
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert rec.key not in h.svc._status_hold and statuses[-1] == "active"


def test_a_worker_filed_into_the_channel_folder_still_obeys_the_scope(
    tmp_path, monkeypatch, lineage
):
    """The board files lineage workers into the channel's folder: lineage still decides."""
    from kiro_crew.slack.code_channels.workspace import folders

    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch, folder_id="F-chan")
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    monkeypatch.setattr(folders, "subtree_ids", lambda state, fid: {fid})
    monkeypatch.setattr(
        folders,
        "sessions_in",
        lambda rows, ids, worktree="": {"chat-w1": (w1.turn_running, w1._turn_generation)},
    )
    asyncio.run(h.svc.watch_tick(dash))
    source.set_scope_predicate(lambda state, b: False)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_cached_waiting_row_sets_no_suspended_on_a_refused_channel(
    tmp_path, monkeypatch, lineage
):
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    # Admitted by the pass's first check, refused by the status check.
    verdicts = iter([True] + [False] * 10)
    source.set_scope_predicate(lambda state, b: next(verdicts))
    asyncio.run(h.svc.watch_tick(dash))
    assert "suspended" not in statuses and rec.key not in h.svc._boards


def test_the_board_is_not_pinned_once_the_scope_refuses(slack):
    _publish(_snapshot())
    pins = len(slack.of("add_pin"))
    surface_store.store().update("C1", board_pinned=False)  # someone unpinned it
    asyncio.run(
        publisher.publish(
            _binding(), _snapshot(), link_for=_link, owner_id="U0", admits=lambda: False
        )
    )
    assert len(slack.of("add_pin")) == pins


# ── the watch's status, scope and refresh edges ────────────────────────────────────────────────


def test_a_second_approval_under_the_same_report_pages_the_owner_again(slack):
    """Two approvals with no report between share report_stamp: each still pages once."""

    def asking(approval_id):
        return model.BoardSnapshot(
            rows=(
                _row(
                    state=model.WAITING_ON_YOU,
                    waiting_on="approval",
                    report_stamp="r1",
                    approval_key=model.approval_key(approval_id),
                ),
            )
        )

    _publish(asking("req-1"))
    _publish(asking("req-1"))  # the same approval: once
    assert len(slack.of("post_message")) == 1
    _publish(asking("req-2"))  # answered, then the next one inside one debounce
    assert len(slack.of("post_message")) == 2


def test_the_approval_key_comes_from_the_pending_approvals_request_id(monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_status

    slot = SimpleNamespace(
        key="chat-w1",
        project="",
        to_dict=lambda: {"pending_approval_info": {"tool_kind": "execute", "request_id": "r-9"}},
    )
    monkeypatch.setattr(source, "find_slot", lambda state, key: slot)
    monkeypatch.setattr(
        session_status,
        "_pending_approval_fields",
        lambda state, s: {"pending_approval": True, "pending_approval_tool": "Running: ls"},
    )
    asked = source.approval_activity(object(), "chat-w1")
    assert asked.approval_id == "r-9"
    assert model.approval_key("r-9") and model.approval_key("r-9") != model.approval_key("r-8")
    assert model.approval_key("") == ""


def test_a_conductor_turn_between_passes_makes_the_watch_resend_the_waiting_status(
    tmp_path, monkeypatch, lineage
):
    """The conductor's own hooks overwrote the status: the watch must not trust its cache."""
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]  # unchanged: nothing re-sent
    dash.get_slot("chat-cond")._turn_generation += 1  # a short turn started and ended
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended", "suspended"]


def test_closing_an_idle_worker_refreshes_the_board(tmp_path, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": _live_slot("chat-w1")})
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == []
    del dash._slots["chat-w1"]  # the owner closed the idle worker
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [0.0]
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [0.0]  # once


# ── security: publish only what the channel admits ──


@pytest.mark.parametrize("where", ["C1", "C-origin"])
@pytest.mark.parametrize("mode", ["off", "review"])
def test_a_channel_or_origin_set_off_or_review_admits_no_board(monkeypatch, where, mode):
    """The activation gate is part of THE scope check, so every board path obeys it."""
    from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW

    source.set_scope_predicate(lambda state, binding: True)  # the exemption admits it
    binding = replace(_binding(), origin_channel_id="C-origin")
    assert source.scope_admits(None, binding)
    activation = ACTIVATION_OFF if mode == "off" else ACTIVATION_REVIEW
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({where: activation})
    )
    assert not source.scope_admits(None, binding)


def test_the_board_publish_honours_an_off_channel(monkeypatch):
    from kiro_crew.config.loader import ACTIVATION_OFF
    from kiro_crew.slack.code_channels.board import links

    sent: list = []

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        sent.append(snapshot)
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    source.set_scope_predicate(lambda state, binding: True)
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_OFF})
    )
    assert asyncio.run(board_pkg.publish(_binding(), _snapshot())) == "out_of_scope"
    assert sent == []


def test_worker_activity_refreshes_nothing_in_an_off_channel(tmp_path, repo, monkeypatch):
    """Off or Review: no bar, no diff tab, no board from a worker-triggered refresh."""
    from kiro_crew.config.loader import ACTIVATION_REVIEW

    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(title="Fix login"),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("a worker edited this\n")  # a new diff
    before = len(h.client.calls)
    monkeypatch.setattr(
        cc_binding,
        "live_config",
        lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_REVIEW}),
    )
    asyncio.run(h.svc.refresh(binding))
    assert h.client.calls[before:] == [] and h.published == []


def test_the_watch_writes_no_worker_status_in_an_off_channel(tmp_path, monkeypatch, lineage):
    from kiro_crew.config.loader import ACTIVATION_OFF

    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_OFF})
    )
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_board_whose_scope_flips_mid_publish_sends_no_more(slack):
    """Asked again before each write: a refusal during the edit's retry posts nothing."""
    calls = {"n": 0}

    def admits():
        calls["n"] += 1
        return calls["n"] <= 2  # the post's two asks (the loop's, the send gate's) only

    waiting = model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU, report_stamp="r1"),))
    asyncio.run(
        publisher.publish(_binding(), waiting, link_for=_link, owner_id="U0", admits=admits)
    )
    assert len(slack.of("post_blocks")) == 1
    assert slack.of("post_message") == []  # the needs-you mention was re-checked and refused
    slack.fail["update_message"] = "message_not_found"
    calls["n"] = 0
    out = asyncio.run(
        publisher.publish(_binding(), _snapshot(), link_for=_link, owner_id="U0", admits=admits)
    )
    assert out == "out_of_scope" and len(slack.of("post_blocks")) == 1  # no repost


def test_the_publish_wrapper_hands_the_publisher_the_live_scope_check(monkeypatch):
    from kiro_crew.slack.code_channels.board import links

    seen: dict = {}

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        seen["admits"] = admits
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    verdict = {"now": True}
    source.set_scope_predicate(lambda state, binding: verdict["now"])
    asyncio.run(board_pkg.publish(_binding(), _snapshot()))
    assert callable(seen["admits"]) and seen["admits"]()
    verdict["now"] = False  # the publisher's later re-checks see the change
    assert not seen["admits"]()


def test_the_conductors_turn_hooks_forget_the_watchs_status_record(tmp_path, monkeypatch):
    """The base's turn hooks hand the status to the conductor; the watch's record goes."""
    h = _Harness(tmp_path)
    monkeypatch.setattr(h.svc, "ensure", lambda binding: asyncio.sleep(0))
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda binding, *, delay=0.0: None)
    key = "T1:C1"
    for hook in (h.svc.on_turn_start, h.svc.on_turn_end):
        h.svc._working[key] = "suspended"
        h.svc._status_failures[key] = 2
        h.svc._status_hold[key] = h.svc._clock() + 60
        asyncio.run(hook(_binding()))
        assert key not in h.svc._working
        assert key not in h.svc._status_failures and key not in h.svc._status_hold


# ── filing, foreign repositories and stored rows ────────────────────────────────────────────────


def test_a_worker_filed_at_creation_and_later_unfiled_stays_out(monkeypatch):
    """GPT: an already-foldered worker was not remembered, so unfiling it was overridden."""
    from kiro_crew.slack.code_channels.board import filing

    state = suite._State(
        {
            "chat-cond": suite._slot("chat-cond", folder="F"),
            "chat-w1": suite._slot("chat-w1", folder="F"),
        }
    )
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)

    async def fake_place(st, slot, folder):
        slot.folder_id = folder
        return True

    monkeypatch.setattr(filing, "_place", fake_place)
    snap = model.BoardSnapshot(rows=(_row("it_00000001", session_key="chat-w1"),))
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    state.slots["chat-w1"].folder_id = ""  # the owner moves it out
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    assert state.slots["chat-w1"].folder_id == ""


def _filing_board(monkeypatch, state):
    from kiro_crew.slack.code_channels.board import filing

    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)

    async def fake_place(st, slot, folder):
        slot.folder_id = folder
        return True

    monkeypatch.setattr(filing, "_place", fake_place)

    def run(*keys):
        rows = tuple(_row(f"it_{i:08x}", session_key=k) for i, k in enumerate(keys, 1))
        return asyncio.run(filing.file_unfiled_workers(_binding(), model.BoardSnapshot(rows=rows)))

    return run


def test_filing_markers_cap_at_the_live_slot_limit_and_outlast_500_lifetime_workers(monkeypatch):
    """GPT: a FIFO cut forgot the oldest marker, re-filing a worker the owner moved out."""
    from kiro_crew.dashboard.state import MAX_LIVE_SLOTS
    from kiro_crew.slack.code_channels.board import filing

    assert filing._max_filed() == MAX_LIVE_SLOTS
    state = suite._State({"chat-cond": suite._slot("chat-cond", folder="F")})
    run = _filing_board(monkeypatch, state)
    state.slots["w0"] = suite._slot("w0")
    state.existing.add("w0")
    assert run("w0") == 1
    state.slots["w0"].folder_id = ""  # the owner moves it out, and it stays open
    for i in range(1, MAX_LIVE_SLOTS + 101):
        key = f"w{i}"
        state.slots[key] = suite._slot(key)
        state.existing.add(key)
        assert run(key) == 1, key
        del state.slots[key]  # the worker's session ends
        state.existing.discard(key)
    assert run("w0") == 0 and state.slots["w0"].folder_id == ""
    filed = surface_store.store().get("C1").board_filed
    assert "w0" in filed and len(filed) <= MAX_LIVE_SLOTS


def test_a_filing_marker_the_store_failed_to_keep_still_stops_a_refile(monkeypatch):
    """GPT r47: a failed marker write let the next read file the worker over the owner."""
    from kiro_crew.slack.code_channels.board import filing

    state = suite._State({"chat-cond": suite._slot("chat-cond", folder="F")})
    run = _filing_board(monkeypatch, state)
    state.slots["w1"] = suite._slot("w1")
    calls: list[tuple] = []

    async def failing_once(channel, **fields):
        calls.append(fields)
        if len(calls) == 1:
            raise OSError("store down")
        return None

    monkeypatch.setattr(filing, "update_async", failing_once)
    try:
        assert run("w1") == 1  # filed; its marker write fails
        state.slots["w1"].folder_id = ""  # the owner moves it out
        assert run("w1") == 0 and state.slots["w1"].folder_id == ""
        assert calls[-1] == {"board_filed": ["w1"]}  # the kept list is written again
        assert "C1" not in filing._unsaved
    finally:
        filing.forget("C1")


def test_a_full_unsaved_table_declines_new_filings_and_drops_no_marker(monkeypatch):
    """r48 G3: clearing the table at its cap forgot a placement and refiled over the owner."""
    from kiro_crew.slack.code_channels.board import filing

    state = suite._State({"chat-cond": suite._slot("chat-cond", folder="F")})
    run = _filing_board(monkeypatch, state)
    state.slots["w1"] = suite._slot("w1")
    monkeypatch.setattr(filing, "_MAX_UNSAVED", 1)
    monkeypatch.setitem(filing._unsaved, "C-other", ["w9"])  # the table is full
    try:
        assert run("w1") == 0 and state.slots["w1"].folder_id == ""  # declined, not placed
        assert filing._unsaved == {"C-other": ["w9"]}
    finally:
        filing.forget("C-other")


def test_a_record_already_in_the_folder_is_a_placement_done(tmp_path):
    """GPT r48: a dirty-slot flush that saved the live folder first made filing undo it."""
    from kiro_crew.slack.code_channels.board import filing

    st, slot = _filing_case(tmp_path, {"title": "t", "folder_id": "F"})
    assert asyncio.run(filing._place(st, slot, "F")) is True
    assert slot.folder_id == "F" and st.conversation_log.get_metadata("sess-w1")["folder_id"] == "F"


def test_at_the_cap_open_restoring_and_unreadable_markers_stay_and_new_filing_waits(monkeypatch):
    from kiro_crew.slack.code_channels.board import filing

    class _Doubtful(suite._State):
        def slot_exists(self, key):
            if key == "w-unreadable":
                raise OSError("registry unreadable")
            return super().slot_exists(key)

    state = _Doubtful({"chat-cond": suite._slot("chat-cond", folder="F")})
    run = _filing_board(monkeypatch, state)
    monkeypatch.setattr(filing, "_max_filed", lambda: 3)
    for key in ("w-restoring", "w-building", "w-unreadable"):
        state.slots[key] = suite._slot(key)
        state.existing.add(key)
        assert run(key) == 1
        del state.slots[key]  # get_slot answers None: restore not reached, or being built
    state.existing.discard("w-unreadable")  # only the raising check speaks for it
    state.slots["w-new"] = suite._slot("w-new")
    state.existing.add("w-new")
    assert run("w-new") == 0 and state.slots["w-new"].folder_id == ""  # waits
    assert surface_store.store().get("C1").board_filed == (
        "w-restoring",
        "w-building",
        "w-unreadable",
    )
    state.existing.discard("w-building")  # that session ends
    assert run("w-new") == 1
    assert surface_store.store().get("C1").board_filed == ("w-restoring", "w-unreadable", "w-new")


class _FilingState:
    """The dashboard surface ``filing._place`` touches, over a real transcript store."""

    def __init__(self, tmp_path, slot):
        from kiro_crew.history import ConversationLog

        self.conversation_log = ConversationLog(base_dir=tmp_path)
        self._slots = {slot.key: slot}
        self.pushes = 0

    def get_slot(self, name):
        return self._slots.get(name)

    def push_slots_update(self):
        self.pushes += 1


def _filing_case(tmp_path, meta=None):
    slot = SimpleNamespace(key="chat-w1", folder_id="", linked_session_key="sess-w1")
    st = _FilingState(tmp_path, slot)
    if meta is not None:
        st.conversation_log.update_metadata("sess-w1", meta)
    return st, slot


def test_filing_merges_only_the_folder_and_keeps_the_rest_of_the_record(tmp_path):
    from kiro_crew.slack.code_channels.board import filing

    st, slot = _filing_case(tmp_path, {"title": "t", "model": "m", "pinned": True})
    assert asyncio.run(filing._place(st, slot, "F")) is True
    meta = st.conversation_log.get_metadata("sess-w1")
    assert meta["folder_id"] == "F" and meta["title"] == "t" and meta["model"] == "m"
    assert meta["pinned"] is True and slot.folder_id == "F" and st.pushes == 1


@pytest.mark.parametrize(
    ("meta", "kept"),
    [
        ({"title": "t", "closed": True}, {"closed": True}),  # closed meanwhile
        ({"title": "t", "folder_id": "F-person"}, {"folder_id": "F-person"}),  # placed meanwhile
        (None, None),  # deleted: no record to merge into
    ],
)
def test_filing_never_overwrites_a_close_a_placement_or_recreates_a_record(tmp_path, meta, kept):
    from kiro_crew.slack.code_channels.board import filing

    st, slot = _filing_case(tmp_path, meta)
    assert asyncio.run(filing._place(st, slot, "F")) is False
    assert slot.folder_id == ""
    stored = st.conversation_log.get_metadata("sess-w1")
    if kept is None:
        assert stored == {}
    else:
        assert stored.items() >= kept.items() and stored.get("folder_id") != "F"


def test_filing_rolls_back_when_the_slot_is_replaced_or_the_write_raises(tmp_path, monkeypatch):
    from kiro_crew.slack.code_channels.board import filing

    st, slot = _filing_case(tmp_path, {"title": "t"})
    real = st.conversation_log.update_metadata_if

    def replaced(*a, **k):
        st._slots["chat-w1"] = SimpleNamespace(key="chat-w1", folder_id="")
        return real(*a, **k)

    monkeypatch.setattr(st.conversation_log, "update_metadata_if", replaced)
    assert asyncio.run(filing._place(st, slot, "F")) is False and slot.folder_id == ""

    st, slot = _filing_case(tmp_path / "b", {"title": "t"})

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(st.conversation_log, "update_metadata_if", boom)
    assert asyncio.run(filing._place(st, slot, "F")) is False and slot.folder_id == ""


@pytest.mark.parametrize("wrote", [True, False])
def test_a_cancelled_filing_undoes_the_live_field_only_if_the_write_did_not_land(tmp_path, wrote):
    import threading

    from kiro_crew.slack.code_channels.board import filing

    st, slot = _filing_case(tmp_path, {"title": "t"})
    entered, release = threading.Event(), threading.Event()

    def held_write(*a, **k):
        entered.set()
        release.wait(30)
        return wrote

    st.conversation_log.update_metadata_if = held_write

    async def go():
        task = asyncio.ensure_future(filing._place(st, slot, "F"))
        try:
            await async_wait_until(entered.is_set, describe=lambda: "the write never started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert slot.folder_id == "F"  # the thread has not answered yet
        finally:
            release.set()
        # The write itself (the shielded thread call) runs on after the cancel: wait
        # for it, so its done callback (the undo, registered first) has run.
        (write,) = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await await_until(write, what="the held write")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert slot.folder_id == ("F" if wrote else "")


def _two_repos(tmp_path, repo, monkeypatch):
    """The channel on acme/widgets; a worker whose session moved to acme/other."""
    other = tmp_path / "other"
    other.mkdir()
    suite._git("init", "-q", str(other), cwd=tmp_path)
    suite._git("remote", "add", "origin", "git@github.com:acme/other.git", cwd=other)
    w1 = _live_slot("chat-w1", project=str(other))
    state = _Slots(**{"chat-w1": w1})
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(service.git_ops, "remote_url", _remote_for(repo, other))
    return other


def _remote_for(repo, other):
    def remote(cwd):
        return (
            "git@github.com:acme/other.git"
            if str(cwd).startswith(str(other))
            else "git@github.com:acme/widgets"
        )

    return remote


def test_a_stored_board_record_reads_back_from_disk(tmp_path):
    """The base's strict row check must not read the board's bool and lists as corrupt."""
    path = tmp_path / "surface.json"
    first = surface_store.SurfaceStore(path)
    first.update(
        "C9", board_ts="100.1", board_pinned=True, board_alerts=("it_1:r1",), board_filed=("w1",)
    )
    again = surface_store.SurfaceStore(path).get("C9")
    assert again is not None
    assert again.board_pinned is True and again.board_filed == ("w1",)
    assert again.board_alerts == ("it_1:r1",)


@pytest.mark.parametrize(
    "bad", [{"board_pinned": "yes"}, {"board_filed": "w1"}, {"board_alerts": [1]}]
)
def test_a_wrong_typed_board_field_is_an_unreadable_row(tmp_path, bad):
    path = tmp_path / "surface.json"
    surface_store.SurfaceStore(path).update("C9", board_ts="100.1")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("records", data)
    row = rows[0] if isinstance(rows, list) else rows["C9"]
    row.update(bad)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    # The base's rule: a row this build can't read makes the file unreadable, and
    # it is never rewritten (so nothing in it is silently shortened or lost).
    with pytest.raises(surface_store.SurfaceStoreUnavailable):
        surface_store.SurfaceStore(path).get("C9")
    assert path.read_bytes() == before


# ── a cached board belongs to the conductor it was read for ──


def _cached_for(h, conductor, title="Old worker"):
    key = "T1:C1"
    snap = model.BoardSnapshot(rows=(_row(state=model.DONE, title=title),))
    h.svc._boards[key] = snap
    h.svc._board_owner[key] = conductor
    return key, snap


def test_a_board_cached_for_a_replaced_conductor_is_not_reused(tmp_path):
    h = _Harness(tmp_path)
    key, snap = _cached_for(h, "chat-cond")
    assert h.svc.board("T1", "C1", session_key="chat-cond") is snap
    # The binding now names a replacement conductor: its caller gets nothing.
    assert h.svc.board("T1", "C1", session_key="chat-cond-2") is None
    # Once the service learns the new conductor, every read site drops it.
    h.svc._conductor_of[key] = "chat-cond-2"
    assert h.svc._cached_board(key) is None
    assert key not in h.svc._boards and key not in h.svc._board_owner
    assert not h.svc._board_due(key, {})


# ── publisher: the shared retry rule and the board pin (rounds on 7669f7cde1 and 64403785b0) ──


def test_the_board_classifies_and_waits_out_slack_errors_by_the_shared_rule(slack):
    """``slack.retry`` is the one Retry-After parser and retry classifier."""
    for name in ("_retry_after", "_PERMANENT"):
        assert not hasattr(publisher, name)
    # A 429's Retry-After is honoured, whatever its header's case.
    assert (
        publisher._failed("C1", "update", _SlackError("ratelimited", headers={"retry-after": "42"}))
        == "failed"
    )
    assert publisher.take_retry("C1") == 42.0 and not publisher.refused_permanently("C1")
    # A Retry-After on anything but a 429 is not read, and a negative one is ignored.
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError("internal_error", headers={"Retry-After": "42"}))
    assert publisher.take_retry("C1") == 5.0
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError("ratelimited", headers={"Retry-After": "-9"}))
    assert publisher.take_retry("C1") == 5.0
    # Slack's own "try again" answers come as an ok:false body on a 200: retried.
    for code in ("internal_error", "fatal_error", "request_timeout"):
        publisher.forget("C1")
        publisher._failed("C1", "update", _SlackError(code))
        assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")
    # Slack's own refusal (an ok:false body on a 200) is not retried on a timer.
    publisher.forget("C1")
    assert publisher._failed("C1", "update", _SlackError("msg_too_long")) == "failed"
    assert publisher.refused_permanently("C1") and publisher.take_retry("C1") == 0.0
    # A post Slack answered with no ts is retried.
    publisher.forget("C1")
    publisher._failed("C1", "post", None)
    assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")


def test_an_install_that_grants_pins_write_pins_the_board_on_its_first_publish(slack):
    """The manifest asks for ``pins:write`` with code channels, and a fresh grant pins."""
    from kiro_crew import slack_manifest
    from kiro_crew.slack import features

    on = slack_manifest.requirements(frozenset({"code_channels"}))
    off = slack_manifest.requirements(frozenset())
    assert publisher.PIN_SCOPE == "pins:write"
    assert "pins:write" in on.bot_scopes and "pins:write" not in off.bot_scopes
    features.record_granted_scopes(["chat:write", "code_channels:manage", "pins:write"])
    assert _publish(_active_snapshot()) == "posted"
    assert slack.of("add_pin") == [("add_pin", "C1", "100.1")]
    assert surface_store.store().get("C1").board_pinned


def test_a_re_approved_pins_write_pins_at_once_not_after_the_backoff(slack, publisher_clock):
    """An install that hadn't re-approved: the 10-minute backoff, until the grant lands."""
    from kiro_crew.slack import features

    slack.fail["add_pin"] = "missing_scope"  # granted: chat:write, code_channels:manage
    assert _publish(_active_snapshot()) == "posted"
    publisher_clock.step = publisher.MIN_EDIT_GAP_SECS
    _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=2))
    assert len(slack.of("add_pin")) == 1  # backing off
    features.record_granted_scopes(["chat:write", "code_channels:manage", "pins:write"])
    _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=3))
    assert len(slack.of("add_pin")) == 2
    assert surface_store.store().get("C1").board_pinned


def test_a_pin_slack_refuses_is_retried_only_every_repin_interval(slack, publisher_clock):
    """An install without ``pins:write``: one failing ``pins.add`` per interval, not per publish."""
    slack.fail["add_pin"] = "missing_scope"
    assert _publish(_active_snapshot()) == "posted"
    assert len(slack.of("add_pin")) == 1
    publisher_clock.step = publisher.MIN_EDIT_GAP_SECS
    for n in range(3):
        _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=n + 2))
    assert len(slack.of("add_pin")) == 1
    publisher_clock.step = publisher.REPIN_SECS
    _publish(_active_snapshot())
    assert len(slack.of("add_pin")) == 2
    assert surface_store.store().get("C1").board_pinned


# ── a refused bar or Code tab doesn't stop the board's watch ──


def test_a_slack_off_channel_still_watches_its_workers(tmp_path, monkeypatch, lineage):
    """Slack refused its bar (``slack_off``), but its board keeps publishing.

    A worker that goes from idle to running, with no report or approval, still
    refreshes the board, as it does in any other bound channel.
    """
    h, rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    stored = h.svc.store.get("T1", "C1")
    stored.slack_off = "not_agent_channel"
    h.svc.store.put(stored)
    asyncio.run(h.svc.watch_tick(dash))  # the first look at its workers
    scheduled.clear()
    dash._slots["chat-w1"].turn_running = True
    dash._slots["chat-w1"]._turn_generation = 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled, "the worker's turn start must refresh the board"


# ── a same-second decide never answers a newer question ──

_SAME_SECOND = "2026-11-05T10:00:00+00:00"


@pytest.mark.parametrize("decide_last", [False, True], ids=["question-after", "decide-after"])
def test_the_store_seam_reads_a_same_second_answer_from_event_order(monkeypatch, decide_last):
    """The decide and the question share one stamp: only the tail's order tells."""
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board

    asked = work_ledger.WorkItem(
        item_id="it_0000000a",
        worker_session_key="chat-w1",
        status="question",
        reason="needs_human",
        last_report_at=_SAME_SECOND,
    )
    asked.decided_at = _SAME_SECOND
    decide = work_ledger.WorkEvent(ts=_SAME_SECOND, item_id="it_0000000a", kind="decision")
    report = work_ledger.WorkEvent(
        ts=_SAME_SECOND, item_id="it_0000000a", kind="report", status="question"
    )
    events = [report, decide] if decide_last else [decide, report]
    monkeypatch.setattr(work_ledger, "read_conductor", lambda key: work_ledger.ConductorRecord())
    monkeypatch.setattr(work_ledger, "list_work_items", lambda key: [asked])
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: ("cond", "it_0000000a"))
    monkeypatch.setattr(work_ledger, "cache_dirty", lambda key: None)
    monkeypatch.setattr(work_ledger, "read_events", lambda slot, item, limit=None: list(events))
    _record, items = asyncio.run(work_ledger_board.own_worker_records("cond"))
    assert items[0].answered_by_event is decide_last


def test_a_question_after_a_same_second_decide_waits_on_the_owner_and_pages_them(slack):
    """The board's own read: waiting on you, the owner paged, the waiting count kept."""
    asked = _Item(
        "it_0000000e",
        title="Pick a retry limit",
        status="question",
        reason="needs_human",
        worker_session_key="chat-w1",
        last_report_at=_SAME_SECOND,
    )
    asked.decided_at = _SAME_SECOND  # the decide that answered the EARLIER report
    asked.answered_by_event = False  # the tail says the question came after it
    snap = source.project(SimpleNamespace(goal="", round=1), [asked], lambda k: model.LIVE_IDLE)
    [row] = snap.rows
    assert row.state == model.WAITING_ON_YOU and row.needs_owner
    assert "1 waiting on you" in snap.aggregate
    agg = model.work_aggregate(snap)
    assert agg["state"] == "waiting" and agg["counts"]["waiting"] == 1
    assert _publish(snap) == "posted"
    [alert] = slack.of("post_message")
    assert "Pick a retry limit" in alert[2] and alert[3] == "100.1"


# ── a finished call is not "Running" ──


def test_a_finished_tool_call_is_not_shown_as_running():
    """The worker's command returned (``meta.done``) and it is reasoning, with no new row."""
    slot = _live_slot("chat-w1", running=True, project="/p")
    slot.messages = [
        {"role": "user", "content": "go", "ts": "2026-10-20T13:20:00+00:00"},
        {
            "role": "tool",
            "content": "🔧 Running",
            "meta": {"kind": "execute", "input": '{"command": "make test"}', "done": True},
        },
    ]
    activity = source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")
    assert activity.turn_started_at == "2026-10-20T13:20:00+00:00"  # the turn start stays
    assert model.tool_step(activity) == ""
    assert model.step_text(activity) == model.STEP_THINKING
    slot.messages[-1]["meta"]["done"] = False  # still in flight: shown as before
    assert model.step_text(source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")) == (
        "Running make test"
    )


# ── a Stop fences the work in flight at the press, not the board's later passes ──


def _stopped_channel(binding):
    """The conductor's turn starts (a real ``_turn_started_at`` entry), then Stop."""
    asyncio.run(surface.note_turn_start(binding, None))
    surface.mark_stopped(binding.channel_id)
    assert surface.channel_stopped(binding.channel_id)  # the stopped turn is fenced


def _unstop(channel):
    surface._stopped_at.pop(channel, None)
    surface._turn_started_at.pop(channel, None)
    board_pkg._timers.pop(channel, None)


def test_a_stop_with_the_conductor_idle_still_lets_helper_agent_events_publish(tmp_path, repo):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    _stopped_channel(binding)

    async def go():
        await h.svc.on_turn_end(binding)  # the stopped turn's own refresh: skipped
        await h.svc.drain()
        after_stopped_turn = len(h.published)
        await h.svc.refresh(binding, recheck_pr=False)  # a helper agent's turn ends
        return after_stopped_turn

    try:
        assert asyncio.run(go()) == 0
        assert len(h.published) == 1
        with surface.own_pass():
            token = surface.admit()
            surface.mark_stopped(binding.channel_id)  # a Stop of the pass in flight
            try:
                assert surface.channel_stopped(binding.channel_id)
            finally:
                surface.release(token)
    finally:
        _unstop(binding.channel_id)


def test_a_delayed_rearm_still_refreshes_after_a_stop_of_the_turn_that_armed_it(
    tmp_path, repo, monkeypatch
):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    real_schedule = board_pkg._schedule

    def fake_schedule(channel_id, delay=None):
        if delay is not None:
            return real_schedule(channel_id, delay)
        h.svc.schedule_refresh(binding, delay=0.0)
        return True

    monkeypatch.setattr(board_pkg, "_schedule", fake_schedule)

    async def go():
        await surface.note_turn_start(binding, None)
        token = surface.admit()  # the turn's work arms the re-arm
        try:
            assert board_pkg._arm_timer("C1", 3600.0)
        finally:
            surface.release(token)
        surface.mark_stopped("C1")  # then the person stops that turn
        _due, handle = board_pkg._timers["C1"]
        handle._run()  # the timer fires (run in the context it was armed with)
        handle.cancel()
        await h.svc.drain()

    try:
        asyncio.run(go())
    finally:
        _unstop("C1")
    assert len(h.published) == 1  # the board's own pass is not fenced by that Stop


# ── Slack's "try again" answers: the board retries them, the shared rule doesn't ──


def _ok_false(code):
    from unittest.mock import MagicMock

    from slack_sdk.errors import SlackApiError

    resp = MagicMock()
    resp.status_code = 200
    resp.get = lambda key, default=None: code if key == "error" else default
    return SlackApiError(message=code, response=resp)


@pytest.mark.parametrize("code", sorted(publisher._BOARD_TRANSIENT))
def test_the_board_retries_slacks_try_again_answers_the_shared_rule_keeps_its_list(code):
    from kiro_crew.slack.retry import is_retryable_slack_error

    # The DM open and the OPTIONS expiry read this rule: it is unchanged for them.
    assert not is_retryable_slack_error(_ok_false(code))
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError(code))
    assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")


def test_the_dm_open_and_options_expiry_still_treat_a_200_internal_error_as_final():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack.outbound import PostedOptions, expire_options
    from kiro_crew.slack.retry import open_dm_with_retry

    slack = MagicMock()
    slack.open_dm = AsyncMock(side_effect=_ok_false("internal_error"))
    with pytest.raises(Exception):
        asyncio.run(open_dm_with_retry(slack, "U1"))
    assert slack.open_dm.await_count == 1  # not retried
    slack.update_message = AsyncMock(side_effect=_ok_false("internal_error"))
    posted = PostedOptions(channel="C1", ts="1.0", choices=("a",), blocks=(), text="t")
    assert asyncio.run(expire_options(slack, posted)) is True  # settled, not kept


# ── home-relative paths ──


@pytest.mark.parametrize(("name", "token"), [("posix", "~"), ("nt", "%USERPROFILE%")])
def test_a_home_path_is_shown_home_relative_on_each_platform(name, token):
    assert model._home_token(name) == token


# ── GPT on b954b1acf7: a Stop that lands during an admitted publish ──


def _publish_stopped_during(monkeypatch, owner, name):
    """Publish as admitted work while Slack's Stop lands inside ``owner.name``."""
    stopped = [False]
    monkeypatch.setattr(surface, "channel_stopped", lambda channel: stopped[0])
    real = getattr(owner, name)

    async def _stop_then(*a, **k):
        stopped[0] = True
        return await real(*a, **k)

    monkeypatch.setattr(owner, name, _stop_then)

    async def go():
        token = surface.admit()  # a refresh is admitted work
        try:
            return await publisher.publish(_binding(), _snapshot(), link_for=_link, owner_id="U0")
        finally:
            surface.release(token)

    return asyncio.run(go())


def test_a_stop_during_the_policy_check_sends_no_board(slack, monkeypatch):
    assert _publish_stopped_during(monkeypatch, surface, "outbound_permitted") == "stopped"
    assert slack.calls == []


def test_a_stop_during_the_post_pins_nothing_and_pages_nobody(slack, monkeypatch):
    # "stopped": the pin and the page are owed to the pass the refresh re-arms.
    assert _publish_stopped_during(monkeypatch, slack, "post_blocks") == "stopped"
    assert len(slack.of("post_blocks")) == 1  # Slack already holds this one
    assert slack.of("add_pin") == [] and slack.of("post_message") == []


def test_a_path_scrub_cuts_only_whole_path_segments(monkeypatch):
    """Adversarial: ``/srv/u/repo2`` came out as ``.2``, ``/srv/ux`` as ``~x``."""
    monkeypatch.setattr(model.os.path, "expanduser", lambda p: "/srv/u" if p == "~" else p)
    home = model._home_token()
    out = model._host_paths_out("see /srv/u/repo/a /srv/u/repo2/x and /srv/ux/y", "/srv/u/repo")
    assert out == f"see a {home}/…/x and …/y"
    assert model._host_paths_out("built /srv/u/repo.", "/srv/u/repo") == "built .."
    # A project above home is not cut: the user name would be left behind.
    assert model._host_paths_out("cat /srv/u/x", "/srv") == f"cat {home}/…/x"
    # Windows' home token keeps what follows it (CI on windows-latest).
    monkeypatch.setattr(model, "_home_token", lambda os_name=None: "%USERPROFILE%")
    out = model._host_paths_out("see /srv/u/repo2/x", "/srv/u/repo")
    assert out == "see %USERPROFILE%/…/x"
    # A native Windows home, with either separator after it.
    monkeypatch.setattr(model.os.path, "expanduser", lambda p: "D:\\Work\\ra" if p == "~" else p)
    monkeypatch.setattr(model.os.path, "realpath", lambda p: p)
    out = model._host_paths_out("run D:\\Work\\ra/secrets/x.txt D:\\Work\\ra\\a\\b.txt", "")
    assert out == "run %USERPROFILE%/…/x.txt %USERPROFILE%\\…\\b.txt"


# ── the publication boundary for paths and host names (r41, r44 reviews) ──────
# Every case below is read from the published text element: the approval's call
# (the code element of the row's details) or the working row's step. Homes are
# /srv/ci-user (POSIX) and D:\Work\ci-user (Windows, simulated with ntpath; the
# windows-latest CI lanes run the same tests natively).

_POSIX_HOME = "/srv/ci-user"
_WIN_HOME = "D:\\Work\\ci-user"


def _published(monkeypatch, raw, project=""):
    """The pending approval's call as the board publishes it: its code element's text."""
    snap = suite._approval_board(monkeypatch, "execute", title=f"Running: {raw}", project=project)
    return suite._approval_call(snap)


def _published_step(monkeypatch, kind, path, project=""):
    """A working row's read or edit step as published, from its structured path value."""
    activity = model.Activity(
        project=project, tool_kind=kind, tool_input=json.dumps({"path": path})
    )
    snap = model.BoardSnapshot(rows=(_row(step=model.step_text(activity)),))
    (section,) = render.plan_block(snap, suite._link, now=suite._NOW)["tasks"][0]["details"][
        "elements"
    ]
    return "".join(e["text"] for e in section["elements"] if e["type"] == "text").split(" · ")[0]


def _home(monkeypatch, home, token):
    monkeypatch.setattr(model.os.path, "expanduser", lambda p: home if p == "~" else p)
    monkeypatch.setattr(model.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(model, "_home_token", lambda os_name=None: token)
    monkeypatch.setattr(model, "_host_names", lambda: ("test-host.example", "test-host"))


def _on_windows(monkeypatch):
    _home(monkeypatch, _WIN_HOME, "%USERPROFILE%")


def _on_posix(monkeypatch):
    _home(monkeypatch, _POSIX_HOME, "~")


_WIN_CASES = [
    # r41: one home, every spelling, the same marker.
    ("type D:/Work/ci-user", "", "type %USERPROFILE%"),
    ("type d:\\work\\ci-user", "", "type %USERPROFILE%"),
    ("type D:\\Work/ci-user", "", "type %USERPROFILE%"),
    ("type D:\\\\Work\\\\ci-user", "", "type %USERPROFILE%"),
    ("type d:\\WORK\\Ci-User\\s\\x.txt", "", "type %USERPROFILE%\\…\\x.txt"),
    ("type D:\\Work\\ci-user/s/x.txt", "", "type %USERPROFILE%/…/x.txt"),
    # r41: a project above home, spelled another way, is never cut.
    ("type d:\\work\\ci-user\\secret\\x.txt", "d:\\work", "type %USERPROFILE%\\…\\x.txt"),
    ("type D:/Work/ci-user/secrets/x.txt", "D:/Work", "type %USERPROFILE%/…/x.txt"),
    # r44 B4: a project that normalises to above home is not cut either.
    (
        "type D:\\Work\\ci-user\\..\\ci-user\\secret\\x.txt",
        "D:\\Work\\ci-user\\..",
        "type %USERPROFILE%\\…\\x.txt",
    ),
    # Inside the project: relative; a sibling is not the project.
    ("type d:\\work\\ci-user\\repo\\src\\app.py", _WIN_HOME + "\\repo", "type src\\app.py"),
    ("type D:\\Work\\ci-user\\repo2\\x.txt", _WIN_HOME + "\\repo", "type %USERPROFILE%\\…\\x.txt"),
    ("type 'C:\\BUILD\\repo$other\\private\\ci-user\\x.txt'", "c:/build/repo", "type '…/x.txt'"),
    # r41 F2: flagged drives, lowercase device UNC, current-drive roots.
    ("clang -isystemC:\\build\\private\\include", "", "clang …/include"),
    ("clang -isystemd:\\work\\ci-user\\secrets\\x.txt", "", "clang …/x.txt"),
    ("type \\\\?\\unc\\fileserver\\share\\ci-user\\x.txt", "", "type …/x.txt"),
    ("type \\\\?\\UNC\\fileserver\\share\\x.txt", "", "type …/x.txt"),
    ("type \\Users\\ci-user\\secret\\x.txt", "", "type …/x.txt"),
    # r44 B5: every rooted and drive-relative form; the account is never the name kept.
    ("type \\a\\ci-user\\secret\\x.txt", "", "type …/x.txt"),
    ("type \\Users\\ci-user", "", "type …"),
    ("type '\\Data\\my docs\\ci-user\\x.txt'", "", "type '…/x.txt'"),
    ("type C:Users\\ci-user\\secret\\x.txt", "", "type …/x.txt"),
    ("type D:Work\\ci-user", "", "type …"),
    # UNC, device and quoted forms.
    ("type \\\\fileserver\\share\\secret\\x.txt", "", "type …/x.txt"),
    ("type \\\\fileserver\\share", "", "type …/share"),
    ("type \\\\.\\C:\\build\\secret\\x.txt", "", "type …/x.txt"),
    ("type \\\\?\\D:\\Work\\ci-user\\x.txt", "", "type …/x.txt"),
    ("type //fileserver/share/x.txt", "", "type …/x.txt"),
    ('type "C:\\Program Files\\Acme App\\x.txt"', "", 'type "…/x.txt"'),
    # r41/r44 S1: a doubled separator after the home keeps no tail.
    ("type D:\\Work\\ci-user\\\\a\\\\b.txt", "", "type %USERPROFILE%\\…\\b.txt"),
    ("type %USERPROFILE%\\\\secrets\\\\x.txt", "", "type %USERPROFILE%\\…\\x.txt"),
    # A raw home marker that climbs out of home is not home-relative.
    ("type %USERPROFILE%\\..\\other\\x.txt", "", "type …/x.txt"),
    # A bare drive names no directory; a regex escape is cut (over-redaction).
    ("dir C:\\", "", "dir C:\\"),
    ("grep \\d\\+ x", "", "grep …/+ x"),
    # r45 B5: a drive-relative path with no separator; the account is not kept.
    ("type C:ci-user", "", "type …"),
    ("type c:ci-user and C:notes.txt", "", "type … and …/notes.txt"),
    # An unquoted path with spaces.
    ("cat C:\\Data\\my docs\\ci-user\\x.txt", "", "cat …/my …/x.txt"),
]


@pytest.mark.parametrize(("raw", "project", "want"), _WIN_CASES)
def test_a_windows_path_in_any_spelling_is_published_safely(monkeypatch, raw, project, want):
    _on_windows(monkeypatch)
    assert _published(monkeypatch, raw, project) == want


_LONG_QUOTED = "cat '/srv/my docs/ci-user/secret/" + "a" * 600 + "'"

_POSIX_CASES = [
    # r41 F3 / r44 B2: legal punctuation and escapes stay inside the one path.
    ("cat /srv/a$b/../ci-user/secrets/x.txt", "", "cat ~/…/x.txt"),
    ("cat /srv/a\\ b/../ci-user/secret/x.txt", "", "cat ~/…/x.txt"),
    ("cat /srv/a,b/../ci-user/secret/x.txt", "", "cat ~/…/x.txt"),
    ("cat /srv/a…b/../ci-user/secrets/x.txt", "", "cat ~/…/x.txt"),
    ("cat /opt/a,b/ci-user/x.txt", "", "cat …/x.txt"),
    # r41 F3 / r44 B1: quoted paths, the other quote inside, unterminated, past the bound.
    ("cat '/srv/a b/../ci-user/secrets/x.txt'", "", "cat '~/…/x.txt'"),
    ('cat "/srv/a\'b/../ci-user/secret/x.txt"', "", 'cat "~/…/x.txt"'),
    ("cat '/srv/a\"b/../ci-user/secret/x.txt'", "", "cat '~/…/x.txt'"),
    ("cat '/srv/ci-user/secret/x.txt", "", "cat …"),
    (_LONG_QUOTED, "", "cat …"),
    # r44 B3: a sibling that starts with the project's name is not the project.
    ("cat '/opt/repo=other/private/ci-user/x.txt'", "/opt/repo", "cat '…/x.txt'"),
    ("cat '/opt/repo$other/private/ci-user/x.txt'", "/opt/repo", "cat '…/x.txt'"),
    ("cat '/opt/repo..other/private/ci-user/x.txt'", "/opt/repo", "cat '…/x.txt'"),
    ("cat /opt/repo/src/x.py /opt/repo2/y.py", "/opt/repo", "cat src/x.py …/y.py"),
    # r44 B4: equivalent home spellings and normalised ancestors.
    ("cat /srv//ci-user", "", "cat ~"),
    ("cat /srv/./ci-user", "", "cat ~"),
    ("cat /srv/ci-user/../ci-user/x.txt", "", "cat ~/…/x.txt"),
    ("cat /srv/ci-user/../ci-user/x.txt", "/srv/ci-user/..", "cat ~/…/x.txt"),
    ("cat /srv/ci-user/s/x /srv/ci-user", "", "cat ~/…/x ~"),
    # r41 F4: a literal percent is no exemption.
    ("cat %/etc/x and %FOO%/secret/path", "", "cat …/x and …/path"),
    ("cat %/srv/./ci-user/secrets/x.txt", "", "cat …/x.txt"),
    # Raw home markers: parent navigation is collapsed, never kept.
    ("cat ~/../ci-user/x", "", "cat …/x"),
    ("cat ~/..", "", "cat …"),
    ("cat ~/a/b/../c.txt", "", "cat ~/…/c.txt"),
    # A shortened marker with several names is not already safe; one with one is.
    ("cat …/a/b/c.txt …/x.txt", "", "cat …/c.txt …/x.txt"),
    # r41 F5 / r44 B6: host names in any case, never rebuilt from what is left.
    ("ping TEST-HOST.EXAMPLE, Test-Host and DB.INTERNAL.CORP", "", "ping host, host and host"),
    ("ping test-test-host TEST-TEST-HOST", "", "ping host host"),
    ("cat /var/log/test-host.log", "", "cat …"),
    # Public URLs and ordinary relative paths are kept; internal URLs are not.
    (
        "curl https://github.com/acme/widgets/pull/7",
        "",
        "curl https://github.com/acme/widgets/pull/7",
    ),
    ("curl https://db.internal.corp/x/y https://test-host.example/a", "", "curl …/y …/a"),
    ("cat src/x.py ./docs/a.md 10/12 and/or", "", "cat src/x.py ./docs/a.md 10/12 and/or"),
    ("fix A / B", "", "fix A / B"),
    ("cat ../x.txt", "", "cat …/x.txt"),
    # r45 B1: a quote after leading punctuation still binds the whole path.
    ('cat ("/srv/private docs/ci-user/secret x.txt")', "", 'cat ("…/secret x.txt")'),
    ('cat ("/srv/private docs/ci-user/secret x.txt', "", "cat …"),
    ('cat ["/srv/a b/ci-user/x"]', "", 'cat ["…/x"]'),
    ("the user's /etc/x file", "", "the user's …/x file"),
    # r45 B2: a quote across lines, a backslash line continuation.
    ('cat "/srv/private\ndocs/ci-user/secret x.txt"', "", "cat …"),
    ("cat /srv/private\\\ndocs/ci-user/secret/x.txt", "", "cat …/x.txt"),
    # An unquoted path with spaces: what follows a shortened path is shortened too,
    # and the account is never a word on its own.
    ("cp /srv/my docs/ci-user/x.txt", "", "cp …/my …/x.txt"),
    ("cat /srv/my ci-user", "", "cat …/my …"),
    # r45 B3: a Slack link's label is not part of its URL.
    ("see <https://example.com/a|/srv/ci-user/secrets/x.txt>", "", "see <…/x.txt>"),
    ("see <https://example.com/a|/srv/ci-user>", "", "see <…>"),
    # r45 B4: a POSIX name holding backslashes keeps only its last part.
    ("cat /var/private/x\\ci-user\\secret\\z.txt", "", "cat …/z.txt"),
    ("cat /var/private/x\\ci-user", "", "cat …"),
    # r45 B6: an ASCII ``...`` is a raw marker, not a directory.
    ("cat .../a/ci-user/x.txt", "", "cat …/x.txt"),
    # r45 N1: a hidden home name stays hidden the same way.
    ("cat ~/ci-user", "", "cat ~/…"),
    # r46 R1: what follows a home-relative shortening is shortened too.
    ("cat /srv/ci-user/my docs/confidential/x.txt", "", "cat ~/…/my …/x.txt"),
    ("cat ~/my docs/confidential/x.txt", "", "cat ~/…/my …/x.txt"),
    # r46 R3: CRLF continuation; a quote across CR or a vertical tab is unsure.
    ("cat /srv/private\\\r\ndocs/confidential/x.txt", "", "cat …/x.txt"),
    ('cat "/srv/private/x\rdocs.txt"', "", "cat …"),
    ('cat "/srv/private/x\x0bdocs.txt"', "", "cat …"),
    # r46 R4: a quote after an underscore opens too.
    ('cat _"/srv/private docs/confidential/x.txt', "", "cat …"),
    ('cat _"/srv/private docs/confidential/secret x.txt"', "", "cat …/secret x.txt"),
    # r46 R5: a drive-qualified name after punctuation, or as a path's last name.
    ("type --path=C:private !C:private", "", "type … …"),
    ("cat /var/private/C:notes.txt", "", "cat …"),
    # r46 R6: an account inside a relative path, a quoted continuation.
    ("cat docs/ci-user/x.txt", "", "cat …/x.txt"),
    ('cat /srv/my "docs/private/x.txt"', "", 'cat …/my "…/x.txt"'),
    # r47 F6: a command's lines are folded into one, so the continuation carries.
    ("cat /srv/my\rdocs/private/x.txt", "", "cat …/my …/x.txt"),
    ("cat /srv/my\ndocs/private/x.txt", "", "cat …/my …/x.txt"),
]


@pytest.mark.parametrize(("raw", "project", "want"), _POSIX_CASES)
def test_a_posix_path_or_host_name_is_published_safely(monkeypatch, raw, project, want):
    _on_posix(monkeypatch)
    assert _published(monkeypatch, raw, project) == want


@pytest.mark.parametrize(
    ("kind", "path", "project", "want"),
    [
        # r44 B2: a structured path with spaces is ONE value.
        ("read", "/srv/a b/../ci-user/secret/x.txt", "", "Reading ~/…/x.txt"),
        ("edit", "/srv/a b/ci-user/secret/x.txt", "", "Editing …/x.txt"),
        ("read", "/opt/repo/docs/a b.md", "/opt/repo", "Reading docs/a b.md"),
        ("read", "/opt/repo/" + "a" * 600, "/opt/repo", "Reading …"),
    ],
)
def test_a_structured_path_is_published_as_one_whole_value(monkeypatch, kind, path, project, want):
    _on_posix(monkeypatch)
    assert _published_step(monkeypatch, kind, path, project) == want


def test_a_structured_windows_path_with_spaces_keeps_no_account(monkeypatch):
    _on_windows(monkeypatch)
    step = _published_step(monkeypatch, "edit", "C:\\Data\\my docs\\ci-user\\x.txt")
    assert step == "Editing …/x.txt"
    assert _published_step(monkeypatch, "read", "C:ci-user") == "Reading …"


def test_a_private_address_is_replaced_whole_so_no_host_name_is_rebuilt(monkeypatch):
    """r45 B7: ``10.0.0.1.example`` came out as the machine's ``host.example``."""
    _on_posix(monkeypatch)
    monkeypatch.setattr(model, "_host_names", lambda: ("host.example",))
    for raw in ("ping 10.0.0.1.example", "ping 192.168.0.1.example", "ping 100.64.0.1.example"):
        assert _published(monkeypatch, raw) == "ping host"


@pytest.mark.parametrize(
    ("home", "token", "raw", "want"),
    [
        # r46 R2: an account of any length or shape is never published.
        ("/srv/ab", "~", "account ab is running", "account … is running"),
        ("/srv/u", "~", "account u is running", "account … is running"),
        ("/srv/.ci-user", "~", "account .ci-user is running", "account … is running"),
        (
            "D:\\Users\\Jane Doe",
            "%USERPROFILE%",
            "account Jane Doe is running",
            "account … is running",
        ),
        ("D:\\Users\\O'Neil", "%USERPROFILE%", "account O'Neil is running", "account … is running"),
        # r47 F1-F3: edge punctuation, display-folded white space, Unicode case folds.
        ("/srv/ci-user", "~", "account -ci-user and ci-user- run", "account … and … run"),
        ("/srv/.ci-user", "~", "account .ci-user. is running", "account …. is running"),
        ("/srv/.ci-user", "~", "cat docs/.ci-user./x.txt", "cat …/x.txt"),
        ("/srv/.ci-user", "~", "cat /var/private/.ci-user.", "cat …."),
        ("D:\\Users\\Jane Doe", "%USERPROFILE%", "account Jane  Doe ok", "account … ok"),
        ("D:\\Users\\Jane Doe", "%USERPROFILE%", "account Jane\tDoe ok", "account … ok"),
        ("D:\\Users\\Jane Doe", "%USERPROFILE%", "account Jane\rDoe ok", "account … ok"),
        (
            "D:\\Users\\Straße",
            "%USERPROFILE%",
            "account Straße and STRASSE ok",
            "account … and … ok",
        ),
        (
            "D:\\Users\\Jane Doe",
            "%USERPROFILE%",
            "type D:\\Users\\Jane Doe\\x.txt",
            "type …",
        ),
    ],
)
def test_an_account_name_is_never_published(monkeypatch, home, token, raw, want):
    _home(monkeypatch, home, token)
    assert _published(monkeypatch, raw) == want


@pytest.mark.parametrize(
    ("home", "hosts", "raw"),
    [
        # r48 G1: an account inside a host name must not leave part of the host.
        ("/srv/test", ("test-host.example", "test-host"), "ping test-host.example"),
        ("/srv/ci-user", ("dev-ci-user-box.example",), "ping dev-ci-user-box.example"),
        ("D:\\Users\\Straße", ("strasse-device.example",), "ping strasse-device.example"),
    ],
)
def test_a_host_name_holding_the_account_is_replaced_whole(monkeypatch, home, hosts, raw):
    _home(monkeypatch, home, "~")
    monkeypatch.setattr(model, "_host_names", lambda: hosts)
    # The host run and the account span overlap: the union goes, as ``…`` (it
    # holds the account), with no part of either left.
    once = _published(monkeypatch, raw)
    assert once == "ping …" and _published(monkeypatch, once) == once


@pytest.mark.parametrize(
    ("home", "real", "raw"),
    [
        # r51 G5: overlapping occurrences of an account, or of two account
        # spellings, are each found, so no part of one is left.
        ("D:\\Users\\Jane Jane", None, "account Jane Jane Jane is running"),
        ("D:\\Users\\Straße Straße", None, "account STRASSE STRASSE STRASSE is running"),
        ("/srv/Jane Doe", "/srv/Doe Zee", "account Jane Doe Zee is running"),
        # r52 G6: a shorter spelling at the same start never hides a longer one.
        ("/srv/Jane          Doe", "/srv/Jane Doe Zee", "account Jane Doe Zee is running"),
        ("/srv/Jane Doe Zee", "/srv/Jane          Doe", "account Jane Doe Zee is running"),
        ("/srv/Jane          Doe", "/srv/Jane Doe Zee", "account Jane\tDoe\tZee is running"),
    ],
)
def test_overlapping_account_occurrences_are_all_replaced(monkeypatch, home, real, raw):
    _home(monkeypatch, home, "~")
    monkeypatch.setattr(model, "_host_names", lambda: ("other-machine",))
    monkeypatch.setattr(model.os.path, "realpath", lambda p: real if real and p == home else p)
    monkeypatch.setattr(model.os.path, "isabs", lambda p: p.startswith("/"))
    once = _published(monkeypatch, raw)
    assert once == "account … is running" and _published(monkeypatch, once) == once


@pytest.mark.parametrize(
    ("home", "real", "raw"),
    [
        # r54 G8: an account across tokens is found before any token is rewritten.
        ("/srv/Jane Doe Zee", None, "cat /private/Jane Doe Zee"),
        ("/srv/Jane Doe Zee", None, "cat /private/Jane\tDoe\tZee"),
        ("/srv/Jane Doe Zee", None, "cat docs/Jane Doe Zee"),
        ("/srv/Jane Doe", None, "cat /private/Jane Doe"),
        ("/srv/Jane          Doe", "/srv/Jane Doe Zee", "cat /private/Jane Doe Zee"),
        ("/srv/Jane          Doe", "/srv/Jane Doe Zee", "cat /private/Jane\tDoe\tZee"),
    ],
)
def test_an_account_across_path_tokens_is_hidden_whole(monkeypatch, home, real, raw):
    _home(monkeypatch, home, "~")
    monkeypatch.setattr(model, "_host_names", lambda: ("other-machine",))
    monkeypatch.setattr(model.os.path, "realpath", lambda p: real if real and p == home else p)
    monkeypatch.setattr(model.os.path, "isabs", lambda p: p.startswith("/"))
    once = _published(monkeypatch, raw)
    assert once == "cat …" and _published(monkeypatch, once) == once


@pytest.mark.parametrize(
    "raw",
    [
        # r55 G9: an account the scan bound cuts through is hidden from its start.
        "cat Jane" + " " * 549 + "Doe",
        "cat Jane" + " " * 1593 + "Doe",
        "cat Jane" + " " * 1591 + "Doe",
        "cat Jane" + "\t" * 1593 + "Doe",
    ],
)
def test_an_account_the_scan_bound_cuts_is_hidden_from_its_start(monkeypatch, raw):
    _home(monkeypatch, "/srv/Jane Doe", "~")
    monkeypatch.setattr(model, "_host_names", lambda: ("test-host",))
    assert _published(monkeypatch, raw) == "cat …"
    for limit in (120, 140, 200):  # title, summary, goal
        assert model.safe_line(raw, limit=limit, first_line=True) == "cat …"


@pytest.mark.parametrize("home", ["/srv/host ", "/srv/ host", "/srv/\thost\t"])
def test_a_padded_account_named_host_never_becomes_the_host_mark(monkeypatch, home):
    _home(monkeypatch, home, "~")
    once = _published(monkeypatch, "ping 10.0.0.1")
    assert once == "ping …" and _published(monkeypatch, once) == once


def test_a_blank_home_name_matches_nothing_and_the_scrub_ends(monkeypatch):
    """r52 G7: a whitespace-only home name compiled an empty pattern that never advanced."""
    _home(monkeypatch, "/srv/   ", "~")
    monkeypatch.setattr(model, "_host_names", lambda: ("other-machine",))
    assert model._host_paths_out("!" * 560, "") == "!" * 560


def test_host_runs_and_account_spans_side_by_side_are_replaced_each_whole(monkeypatch):
    """Adjacent ranges stay apart; an overlapping chain is one range."""
    _home(monkeypatch, "D:\\Users\\Jane Doe", "%USERPROFILE%")
    monkeypatch.setattr(model, "_host_names", lambda: ("test-host",))
    assert _published(monkeypatch, "Jane Doe test-host 10.0.0.1") == "… host host"
    assert _published(monkeypatch, "Jane Doe-test-host") == "…"
    assert _published(monkeypatch, "x-test-host-Jane Doe") == "…"


@pytest.mark.parametrize(
    ("home", "hosts", "raw"),
    [
        # r49 G4: an account holding a host name (either word, an apostrophe, a
        # private-host shape) is replaced whole, never left in part.
        ("D:\\Users\\Jane Doe", ("jane",), "account Jane Doe is running"),
        ("D:\\Users\\Jane Doe", ("doe",), "account Jane\tDoe is running"),
        ("D:\\Users\\O'Neil", ("neil",), "account O'Neil is running"),
        (
            "D:\\Users\\dev-dsk-tenant Doe",
            ("other-machine",),
            "account dev-dsk-tenant Doe is running",
        ),
    ],
)
def test_an_account_holding_a_host_name_is_replaced_whole(monkeypatch, home, hosts, raw):
    _home(monkeypatch, home, "%USERPROFILE%")
    monkeypatch.setattr(model, "_host_names", lambda: hosts)
    assert _published(monkeypatch, raw) == "account … is running"


def test_a_token_the_scan_bound_cuts_is_never_shown_in_part(monkeypatch):
    """r48 G2: the separator past the scan bound left the path's first name shown."""
    _on_posix(monkeypatch)
    raw = "cat " + "a" * 2000 + "/private/x.txt"
    assert model.safe_line(raw, limit=120) == "cat …"
    assert _published(monkeypatch, raw) == "cat …"


def test_the_host_placeholder_is_never_the_account(monkeypatch):
    """r46 R2: on an account named ``host`` the placeholder must not name it."""
    _home(monkeypatch, "/srv/host", "~")
    once = _published(monkeypatch, "ping 10.0.0.1")
    assert once == "ping …" and _published(monkeypatch, once) == once


def test_a_display_cut_never_shows_part_of_a_path(monkeypatch):
    """r46 R4: the final cut fell inside a quoted path in a title."""
    _on_posix(monkeypatch)
    raw = 'cat ("/srv/private docs/ci-user/secret ' + "a" * 600 + '")'
    line = model.safe_line(raw, limit=120)
    assert line == "cat…"
    assert model.safe_line("see /opt/acme/" + "b" * 200, limit=40) == "see…"
    # r47 F4/F5: a cut at a space inside a quote, and a path whose separator is
    # past the cut, are classified from the whole token.
    assert model.safe_line('cat "src/' + "A" * 110 + ' tail.txt"', limit=120) == "cat…"
    assert model.safe_line("cat " + "A" * 250 + "/private/x.txt", limit=120) == "cat…"


def test_a_structured_path_is_redacted_whole_before_it_is_shortened(monkeypatch):
    _on_posix(monkeypatch)
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    step = _published_step(monkeypatch, "read", f"/opt/x/{key}/notes.txt")
    assert key not in step and "EFGHIJKLMNOP" not in step and step.startswith("Reading ")


@pytest.mark.parametrize(
    "case", [c[:2] for c in _POSIX_CASES] + [("win", c[0]) for c in _WIN_CASES]
)
def test_a_published_line_published_again_is_unchanged(monkeypatch, case):
    first, second = case
    if first == "win":
        _on_windows(monkeypatch)
        raw, project = second, ""
    else:
        _on_posix(monkeypatch)
        raw, project = first, second
    once = _published(monkeypatch, raw, project)
    assert _published(monkeypatch, once, project) == once


def test_running_helpers_counts_the_watched_sessions_running_but_the_channels_own(tmp_path):
    h = _Harness(tmp_path)
    h.svc._seen["T1:C1"] = {
        "dashboard_chat-cond": (True, 1),  # the channel's own session
        "chat-w1": (True, 1),
        "chat-w2": (False, 1),  # idle
        "dashboard_chat-w3": (True, 2),
    }
    assert h.svc.running_helpers(suite._FakeBinding()) == 2
    assert h.svc.running_helpers(suite._FakeBinding(channel_id="C2")) == 0
    source.set_scope_predicate(lambda state, b: False)  # shared: the board withholds them
    assert h.svc.running_helpers(suite._FakeBinding()) == 0


def test_a_stop_clears_the_suspended_the_last_turn_left_for_the_worker_status(monkeypatch):
    """Adversarial: an idle Stop set ``active``, then the watch set ``suspended`` again."""
    from kiro_crew.slack.code_channels import status

    monkeypatch.setattr(status, "_driver", lambda: None)
    monkeypatch.setitem(status._after_turn, "C-held", "suspended")
    asyncio.run(status.settle_after_stop("C-held"))
    assert status.after_turn("C-held") == ""


# ── GPT / adversarial on 0878ae2b6c: gate at the send, published means sent ──


def test_a_stop_mid_pass_on_the_last_helper_event_still_ends_with_a_published_board(
    tmp_path, repo, monkeypatch
):
    """Adversarial: the fenced pass carried the last event, so nothing re-armed it."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(state=model.DONE),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    real_find = h.svc._find_pr
    pressed: list[bool] = []

    async def find_then_stop(repo_ref, branch):
        if not pressed:
            pressed.append(True)
            surface.mark_stopped("C1")  # the person presses Stop during the gh work
        return await real_find(repo_ref, branch)

    h.svc._find_pr = find_then_stop
    shown: list = []

    async def publish_board(b, snap):  # answers as board.publish does under the fence
        if surface.channel_stopped(b.channel_id):
            return "stopped"
        shown.append(snap)
        return "posted"

    h.svc._publish_board = publish_board
    monkeypatch.setattr(
        board_pkg, "_schedule", lambda ch, delay=None: h.svc.schedule_refresh(binding) or True
    )

    async def go():
        await surface.note_turn_start(binding, None)  # the channel has a real turn entry
        await h.svc.refresh(binding, recheck_pr=False)  # the helper agent's last event
        assert pressed and shown == []  # that pass was fenced
        _due, handle = board_pkg._timers["C1"]
        handle._run()  # the one re-publish, in the clean context it was armed with
        handle.cancel()
        await h.svc.drain()

    try:
        asyncio.run(go())
    finally:
        _unstop("C1")
        board_pkg._unsent.pop("C1", None)
    assert len(shown) == 1 and shown[0].rows[0].state == model.DONE


def test_no_pass_edits_the_board_before_slacks_retry_after(slack, monkeypatch):
    """GPT on d939cc22bb: the 60 s stamp timer (or the watch) retried inside Retry-After."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    _publish(_snapshot())
    slack.fail["update_message"] = "ratelimited"
    real = slack._maybe_fail

    def with_retry_after(method):
        if slack.fail.get(method) == "ratelimited":
            slack.fail.pop(method)
            raise _SlackError("ratelimited", status=429, headers={"Retry-After": "120"})
        return real(method)

    monkeypatch.setattr(slack, "_maybe_fail", with_retry_after)
    moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
    now[0] += 30.0  # past the edit gap
    assert _publish(moved) == "failed"
    edits = len(slack.of("update_message"))
    publisher.take_retry("C1")
    now[0] += 60.0  # the stamp's own re-arm, or a watch pass, fires here
    assert publisher.holding("C1")
    assert _publish(moved) == "deferred" and len(slack.of("update_message")) == edits
    assert publisher.take_retry("C1") == pytest.approx(60.0)  # re-armed for the rest
    now[0] += 61.0
    assert not publisher.holding("C1")
    assert _publish(moved) == "updated" and not publisher.holding("C1")


def test_a_re_armed_pass_that_cannot_read_its_record_stays_pending(tmp_path, repo, monkeypatch):
    """Adversarial N2: an exit before the read used up the one pending re-publish."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    board_pkg._unsent["C1"] = 1  # an earlier pass left the board unsent; this is its re-arm
    monkeypatch.setattr(h.svc._store, "get", lambda team, channel: None)  # unreadable store

    async def no_ensure(b, **kw):
        return None

    monkeypatch.setattr(h.svc, "ensure", no_ensure)

    async def go():
        await h.svc.refresh(binding)
        assert "C1" in board_pkg._timers  # still one re-publish pending
        board_pkg._timers["C1"][1].cancel()

    asyncio.run(go())
    assert board_pkg._unsent["C1"] == 2 and h.published == []


def test_an_edit_the_clients_stop_fence_would_drop_counts_as_unsent(slack):
    """Adversarial F2: a muted chat.update returned like a sent one, so the board read as sent."""
    from kiro_crew.slack.agent_sessions import fence

    assert _publish(_snapshot()) == "posted"
    edits = len(slack.of("update_message"))

    async def go():
        reset = fence.enter_turn("C1", "999.0")
        try:
            fence.note_message("C1", "100.1")  # the turn's scope holds the board message
            fence.current_scope().ended = True  # Slack's native Stop ended that turn
            moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
            return await publisher.publish(_binding(), moved, link_for=_link, owner_id="U0")
        finally:
            fence.exit_turn(reset)

    assert asyncio.run(go()) == "stopped"  # unsent: the refresh re-arms it
    assert len(slack.of("update_message")) == edits


def test_retry_after_is_timed_from_slacks_answer_not_the_passs_start(slack, monkeypatch):
    """GPT on 6e414c23dc: a slow 429 answer shortened Slack's required wait."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    _publish(_snapshot())
    real = slack._maybe_fail

    def slow_429(method):
        if method == "update_message":
            now[0] += 30.0  # Slack answers 30 s into the request
            raise _SlackError("ratelimited", status=429, headers={"Retry-After": "120"})
        return real(method)

    monkeypatch.setattr(slack, "_maybe_fail", slow_429)
    now[0] += 30.0  # past the edit gap
    assert _publish(model.BoardSnapshot(rows=_snapshot().rows[:1])) == "failed"
    answered = now[0]
    now[0] = answered + 119.0
    assert publisher.holding("C1")  # the full 120 s from the answer
    now[0] = answered + 121.0
    assert not publisher.holding("C1")


# ── GPT on 6e414c23dc: a bound bounds every field it retains ──


def _long_history():
    """2,000 settled items (accepted, rejected, abandoned) and 5 open ones."""
    finals = ("accepted", "rejected", "abandoned")
    items = [
        suite._Item(
            f"it_{n:08x}",
            title=f"Old {n}",
            state=finals[n % 3],
            created_at=f"2026-10-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z",
        )
        for n in range(2000)
    ]
    items += [
        suite._Item(f"it_f{n:07x}", title=f"Live {n}", created_at="2026-10-02T00:00:00Z")
        for n in range(5)
    ]
    return items


def test_a_long_history_keeps_the_newest_settled_rows_and_every_total(monkeypatch):
    record = SimpleNamespace(goal="", round=1)
    live = lambda key: model.LIVE_IDLE  # noqa: E731
    bounded = source.project(record, _long_history(), live)
    monkeypatch.setattr(model, "MAX_SETTLED", 10**6)
    unbounded = source.project(record, _long_history(), live)
    settled = [r for r in bounded.rows if r.settled]
    assert len(settled) == 100 and len(bounded.rows) == 105
    assert settled[-1].title == "Old 1999" and settled[0].title == "Old 1900"  # the newest
    assert bounded.total == unbounded.total == 2005 and bounded.dropped == 1900
    assert bounded.counts == unbounded.counts and bounded.done == unbounded.done == 667
    assert bounded.aggregate == unbounded.aggregate == "5 idle · 1333 closed · 667 of 2005 done"
    assert bounded.overall == unbounded.overall
    assert bounded.omitted == unbounded.omitted
    assert model.work_aggregate(bounded) == model.work_aggregate(unbounded)
    for look in ("plan", "rows"):
        same = [
            render.board_blocks(s, _link, updated="10:00 UTC", look=look, now=suite._NOW)
            for s in (bounded, unbounded)
        ]
        assert same[0] == same[1], look
    assert render.fallback_text(bounded) == render.fallback_text(unbounded)


def test_the_page_clock_keeps_only_the_items_paged_inside_the_gap(slack, monkeypatch):
    """Class sweep: the per-item page times grew with every item ever paged."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    for n in range(5):
        row = _row(f"it_{n:08x}", model.WAITING_ON_YOU, report_stamp=f"r{n}")
        _publish(model.BoardSnapshot(rows=(row,)))
        now[0] += publisher.PAGE_GAP_SECS  # each one paged a minute apart
    assert len(slack.of("post_message")) == 5
    assert len(publisher._paged["C1"]) <= 1  # older page times are let go
