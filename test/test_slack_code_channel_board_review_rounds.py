"""The work board's review rounds: lineage status, publish scope and the publisher.

Split from ``test_slack_code_channel_board_live.py`` (the file cap): the fixes
the GPT and Opus review rounds asked for on the board's later heads. Shares the
board suites' fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live

from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import model, publisher, source
from kiro_crew.slack.code_channels.workspace import service

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


def test_the_sidebar_summary_comes_from_the_pr_filled_board(monkeypatch):
    """Opus: the aggregate was built before the workers' PRs were read, so no "PR open"."""
    from kiro_crew.slack.code_channels.board import links

    earlier = {"state": "working", "label": "an earlier summary", "counts": {}}
    conductor = SimpleNamespace(key="chat-cond", _work_aggregate=earlier)
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "chat-cond" else None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    source.set_scope_predicate(lambda st, b: True)
    unfilled = model.BoardSnapshot(rows=(_row(pr_number=12),))

    async def read_board(st, b):
        return unfilled

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        return "updated"

    async def no_filing(binding, snapshot):
        return None

    monkeypatch.setattr(source, "read_board", read_board)
    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(board_pkg, "_file_workers", no_filing)
    asyncio.run(board_pkg.read(_binding()))
    assert conductor._work_aggregate is earlier  # the unfilled read publishes nothing
    filled = model.BoardSnapshot(rows=(_row(pr_number=12, pr_state="open"),))
    asyncio.run(board_pkg.publish(_binding(), filled))
    assert "PR open" in conductor._work_aggregate["label"]
    source.set_scope_predicate(lambda st, b: False)
    asyncio.run(board_pkg.publish(_binding(), filled))
    assert conductor._work_aggregate is None  # refused at send: cleared


def test_the_bar_records_the_workspace_keys_its_workers_took(tmp_path, repo):
    """Opus: the bar tool's "Not shown" note must count the worker items too."""
    h = _Harness(tmp_path)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    h.svc._boards[rec.key] = model.BoardSnapshot(
        rows=tuple(_row(f"it_{n:08x}", title=f"W{n}", session_key=f"chat-w{n}") for n in range(4))
    )
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    asyncio.run(h.svc.republish_bar(binding))
    keys = h.svc.bar_base_keys("T1", "C1")
    # The workers take the bar's Workers item (one tally, not one item each); the
    # dashboard link only takes a slot the agent's items leave free.
    assert keys is not None and "workers" in keys
    assert keys == [i["key"] for i in _bar_labels_items(h) if i["key"] != "dashboard"]


def _bar_labels_items(h):
    return h.client.last("set_context_bar")["items"]


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
    filled = model.BoardSnapshot(rows=(_row(pr_number=12, pr_state="open"),))
    assert asyncio.run(board_pkg.publish(_binding(), filled)) == answer
    assert conductor._work_aggregate is None


def test_a_refresh_that_fails_after_its_read_clears_the_sidebar_summary(tmp_path, monkeypatch):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))
    cleared: list = []
    monkeypatch.setattr(service, "_clear_aggregate", lambda b: cleared.append(b))

    async def boom(record, board):
        raise RuntimeError("gh went away")

    monkeypatch.setattr(h.svc, "_fill_worker_prs", boom)
    asyncio.run(h.svc.refresh(binding))
    assert cleared == [binding] and h.published == []


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


def test_a_reported_pr_number_wins_over_a_later_pr_on_a_reused_branch(tmp_path, repo):
    """GPT on b980216ae0: branch discovery picked open #22 over the worker's merged #11."""
    from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest

    url = "https://github.com/acme/widgets/pull/"
    prs = {
        "#11": PrLookup("found", PullRequest(11, url + "11", "merged", "")),
        "feat/login": PrLookup("found", PullRequest(22, url + "22", "open", "running")),
    }
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", model.DONE, branch="feat/login", pr_number=11),)
    )
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert row.pr_number == 11 and row.pr_state == "merged" and row.pr_url.endswith("/11")
    assert "feat/login" not in h.pr_queries[1:]  # the worker's branch is never searched


def test_a_branch_with_no_reported_number_is_still_discovered(tmp_path, repo):
    from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest

    prs = {"feat/x": PrLookup("found", PullRequest(5, "https://github.com/a/b/pull/5", "open", ""))}
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.published[-1].rows[0].pr_number == 5


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
        lambda state, ids: {"chat-w1": (w1.turn_running, w1._turn_generation)},
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


# ── GPT round on 3374623b8a ────────────────────────────────────────────────


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


def test_a_worker_in_another_repository_shows_none_of_its_pr_or_branch(tmp_path, repo, monkeypatch):
    """GPT: #12 in the worker's repository B was read from the channel's repository A."""
    from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest

    _two_repos(tmp_path, repo, monkeypatch)
    prs = {
        "#12": PrLookup(
            "found", PullRequest(12, "https://github.com/acme/widgets/pull/12", "open", "passed")
        )
    }
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", branch="feat/b", pr_number=12, session_key="chat-w1"),)
    )
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert (row.pr_number, row.pr_url, row.branch) == (0, "", "")
    assert "#12" not in h.pr_queries and "feat/b" not in h.pr_queries[1:]


def test_a_worker_in_the_channels_repository_keeps_its_pr(tmp_path, repo, monkeypatch):
    from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest

    w1 = _live_slot("chat-w1", project=str(repo))
    monkeypatch.setattr(surface, "dashboard_state", lambda: _Slots(**{"chat-w1": w1}))
    monkeypatch.setattr(service.git_ops, "remote_url", lambda cwd: "git@github.com:acme/widgets")
    prs = {
        "#12": PrLookup(
            "found", PullRequest(12, "https://github.com/acme/widgets/pull/12", "open", "passed")
        )
    }
    snap = model.BoardSnapshot(rows=(_row("it_00000001", pr_number=12, session_key="chat-w1"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.published[-1].rows[0].pr_url.endswith("/12")


def test_the_wrap_up_fallback_read_is_filled_like_a_refresh(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as workspace

    raw = model.BoardSnapshot(rows=(_row(state=model.DONE, pr_number=12, session_key="chat-w1"),))
    cleaned = model.BoardSnapshot(rows=(_row(state=model.DONE, session_key="chat-w1"),))
    calls: list = []

    class Svc:
        def board(self, team, channel, session_key=""):
            return None

        async def filled(self, binding, snapshot):
            calls.append(snapshot)
            return cleaned

    monkeypatch.setattr(workspace, "service", lambda: Svc())
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)

    async def read_board(state, binding):
        return raw

    monkeypatch.setattr(source, "read_board", read_board)
    lines = asyncio.run(board_pkg.outcome_lines(_binding()))
    assert calls == [raw] and not any("#12" in line for line in lines)


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


# ── GPT round on c8bc9ce15b: a cached board belongs to the conductor it was read for ──


def _cached_for(h, conductor, title="Old worker"):
    key = "T1:C1"
    snap = model.BoardSnapshot(rows=(_row(state=model.DONE, title=title, pr_number=12),))
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


def test_a_refresh_for_a_new_conductor_never_shows_the_old_ones_workers(
    tmp_path, repo, monkeypatch
):
    """The bar is built from the cache only when it was read for this conductor."""
    from kiro_crew.slack.code_channels.store import Binding

    h = _Harness(tmp_path, snapshot=None)  # the new conductor's ledger: no workers yet
    new = Binding(
        channel_id="C1",
        team_id="T1",
        session_key="chat-cond-2",
        anchor_ts="1.0",
        project_dir=str(repo),
    )
    asyncio.run(h.svc.ensure(new))
    _cached_for(h, "chat-cond", title="Old worker")
    asyncio.run(h.svc.refresh(new))
    labels = [item["label"] for item in h.client.last("set_context_bar")["items"]]
    assert not any("Old worker" in label for label in labels)
    assert "T1:C1" not in h.svc._boards


def test_a_bar_republished_for_a_new_conductor_never_shows_the_old_ones_workers(
    tmp_path, repo, monkeypatch
):
    """Before any watch pass or refresh: the agent's own bar edit (republish_bar)."""
    from kiro_crew.slack.code_channels.store import Binding

    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    h = _Harness(tmp_path)
    old = Binding(
        channel_id="C1",
        team_id="T1",
        session_key="chat-cond",
        anchor_ts="1.0",
        project_dir=str(repo),
    )
    asyncio.run(h.svc.ensure(old))
    key, _snap = _cached_for(h, "chat-cond", title="Old worker")
    h.svc._conductor_of[key] = "chat-cond"
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    new = replace(old, session_key="chat-cond-2")
    assert asyncio.run(h.svc.republish_bar(new)) == ""
    labels = [item["label"] for item in h.client.last("set_context_bar")["items"]]
    assert not any("Old worker" in label for label in labels)
    assert key not in h.svc._boards and not h.svc._status_hold


def test_the_wrap_up_reads_the_new_conductors_ledger_not_the_old_cache(tmp_path, monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    h = _Harness(tmp_path)
    _cached_for(h, "chat-cond", title="Old worker")
    monkeypatch.setattr(ws_core, "service", lambda: h.svc)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    source.set_scope_predicate(lambda state, binding: True)
    fresh = model.BoardSnapshot(rows=(_row(state=model.DONE, title="New worker"),))
    read_for: list[str] = []

    async def read_board(state, binding):
        read_for.append(binding.session_key)
        return fresh

    monkeypatch.setattr(source, "read_board", read_board)
    replaced = _binding(session="chat-cond-2")
    lines = asyncio.run(board_pkg.outcome_lines(replaced))
    assert read_for == ["chat-cond-2"]
    assert any("New worker" in line for line in lines)
    assert not any("Old worker" in line for line in lines)


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


# ── GPT round on 6862be93e3: a refused bar or Code tab doesn't stop the board's watch ──


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
