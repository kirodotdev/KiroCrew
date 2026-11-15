"""The patrol tick's remote report pull (rfc-conductor-remote-dispatch §2, open question 1).

Drives the real routes, store and probe with the wave 4 stub crew. Pinned here:

* switch off: nothing is pulled and no peer call is made;
* switch on: a remote ``done`` is filed by one tick's pull, with no conductor read,
  and the work-ledger probe then raises its wake, as for a local report;
* a board with only local items makes no peer call;
* nothing runs in the background any more;
* an unreachable crew is marked down and a hanging board is cut off at its budget;
* the person-wait hold applies to a remote item only while its last pull is fresh,
  and a pull one patrol interval old is still fresh.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import CONDUCTOR, StubCrew, _remote_item, _state

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest, remote_puller
from kiro_crew.probes.work_ledger import WorkLedgerProbe, person_wait_fingerprint

_isolated = helpers._isolated
recorded = hub.recorded


def _switch(monkeypatch, on: bool) -> None:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: on)
    monkeypatch.setattr("kiro_crew.work_preview.remote_dispatch", lambda: on)


@pytest.fixture
def peer(monkeypatch) -> StubCrew:
    _switch(monkeypatch, True)
    return StubCrew()


def _status(item_id: str) -> str | None:
    # A read that meets a write's atomic replace mid-flight (Windows) reads absent.
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    return item.status if item is not None else None


def _wakes() -> list[str]:
    probe = WorkLedgerProbe(worker_running=lambda k: True, worker_closed=lambda k: False)
    ctx = SimpleNamespace(
        message=json.dumps({"conductor": CONDUCTOR}), job=SimpleNamespace(id="loop-1")
    )
    probe.identity(ctx)
    return [obs.key for obs in probe.observe(ctx).observations]


# ── switch off ────────────────────────────────────────────────────────────────


def test_switch_off_pulls_nothing(recorded, monkeypatch):
    stub = StubCrew()
    _switch(monkeypatch, True)

    async def go() -> None:
        item_id = await _remote_item(stub)
        stub.calls.clear()
        _switch(monkeypatch, False)
        stub.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        assert await remote_puller.pull_on_patrol(_state(stub), CONDUCTOR) is False
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status is None

    asyncio.run(go())
    assert stub.calls == []


# ── switch on: a remote report wakes without a conductor read ────────────────


def test_one_run_files_a_remote_done_and_the_probe_wakes(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        assert await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR) is True
        return item_id

    item_id = asyncio.run(go())
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert item.status == "done" and item.pr == 7
    assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["done"]
    assert any(key.startswith("report:") for key in _wakes())


def test_a_question_then_progress_between_two_runs_still_wakes_for_the_question(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    assert work_ledger.read_work_item(CONDUCTOR, item_id).status == "progress"
    (question,) = [e for e in work_ledger.read_events(CONDUCTOR, item_id) if e.status == "question"]
    assert f"report:{question.id}" in _wakes()


def test_a_second_run_files_nothing_new(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    asyncio.run(go())
    assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["question"]


def test_a_board_with_only_local_items_makes_no_peer_call(recorded, peer):
    async def go() -> bool:
        await helpers._board()
        peer.calls.clear()
        return await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    assert asyncio.run(go()) is False
    assert peer.calls == []


def test_nothing_runs_in_the_background(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        before = {t.get_name() for t in asyncio.all_tasks()}
        remote_puller.ensure_running(_state(peer))
        await asyncio.sleep(0)
        assert {t.get_name() for t in asyncio.all_tasks()} == before

    asyncio.run(go())
    assert not hasattr(remote_puller, "run_once")
    assert not hasattr(work_ledger, "REMOTE_PULL_INTERVAL_SECS")


def test_the_budgets_fit_the_patrol_interval():
    from kiro_crew.conductor_patrol import PATROL_INTERVAL_SECS

    assert remote_ingest.READ_DEADLINE_SECS < remote_puller.BOARD_TIMEOUT_SECS
    assert remote_puller.BOARD_TIMEOUT_SECS < PATROL_INTERVAL_SECS
    # One freshness window spans more than one patrol interval, so a hold set on one
    # tick's pull still stands at the next.
    assert work_ledger.REMOTE_PULL_FRESH_SECS == 2 * PATROL_INTERVAL_SECS


# ── bounds ───────────────────────────────────────────────────────────────────


def test_an_unreachable_crew_is_marked_down_and_never_raises(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    stored = work_ledger.read_remote_state(CONDUCTOR, item_id)
    assert stored is not None and stored.crew_state == "disconnected"
    assert work_ledger.read_work_item(CONDUCTOR, item_id).status is None


def test_a_hung_board_is_cut_off_at_its_budget(monkeypatch):
    async def ingest(state, board):
        await asyncio.Event().wait()

    monkeypatch.setattr(remote_ingest, "ingest_board", ingest)
    monkeypatch.setattr(remote_puller, "BOARD_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(remote_puller, "_needs_pull", lambda board: True)
    # Returns at all only because the hung board is cut off.
    assert asyncio.run(remote_puller.pull_on_patrol(SimpleNamespace(), "hangs")) is True


def test_a_failing_pull_never_raises(monkeypatch):
    async def ingest(state, board):
        raise RuntimeError("tunnel broke")

    monkeypatch.setattr(remote_ingest, "ingest_board", ingest)
    monkeypatch.setattr(remote_puller, "_needs_pull", lambda board: True)
    assert asyncio.run(remote_puller.pull_on_patrol(SimpleNamespace(), "b")) is True

    def broken(board):
        raise OSError("disk gone")

    monkeypatch.setattr(remote_puller, "_needs_pull", broken)
    assert asyncio.run(remote_puller.pull_on_patrol(SimpleNamespace(), "b")) is False


def test_a_conductor_with_no_live_session_still_gets_its_report_filed(recorded, peer, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger as routes

    async def go() -> str:
        item_id = await _remote_item(peer)
        # The conductor's session expired: no live unit, its slot's newest one stands.
        monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "")
        monkeypatch.setattr(session_ledger, "newest_work_unit", lambda key: "u-newest")
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    assert _status(item_id) == "done"
    assert [u for u, d in recorded if d.get("actor") == "worker"] == ["u-newest"]


def test_a_board_with_nowhere_to_file_makes_no_peer_call(recorded, peer, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger as routes

    async def go() -> str:
        item_id = await _remote_item(peer)
        monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "")
        monkeypatch.setattr(session_ledger, "newest_work_unit", lambda key: "")
        peer.calls.clear()
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    assert peer.calls == []
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).error == "crew_log_unit_unknown"


def test_a_dirty_board_is_not_pulled(recorded, peer):
    async def go() -> list[str]:
        await _remote_item(peer)
        work_ledger.mark_cache_dirty(CONDUCTOR, "test")
        peer.calls.clear()
        return await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    assert asyncio.run(go()) is False
    assert peer.calls == []


def test_a_report_the_crew_log_refuses_is_retried_once_per_window(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as routes

    async def setup() -> str:
        return await _remote_item(peer)

    item_id = asyncio.run(setup())
    tries: list[str] = []
    monkeypatch.setattr(
        routes.crew_log_emit, "on_work_recorded", lambda unit, data: tries.append(unit) and False
    )
    peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert len(tries) == 1
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).error == "crew_log_unrecorded"
    # The same stamp inside the window: no second attempt, no second audit row.
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert len(tries) == 1
    # Past the window it is tried again.
    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    record.unrecorded_at = "2020-01-01T00:00:00+00:00"
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert len(tries) == 2
    # A new peer report is tried at once: the unrecorded one it follows goes first,
    # and the newer one waits behind it while the crew log still refuses.
    peer.report("done", "shipped again", "2026-10-29T10:05:00+00:00", pr=7)
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert len(tries) == 3
    assert _status(item_id) is None


def test_a_changed_snapshot_in_the_same_second_is_not_held(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as routes

    asyncio.run(_remote_item(peer))
    tries: list[str] = []
    monkeypatch.setattr(
        routes.crew_log_emit,
        "on_work_recorded",
        lambda unit, data: tries.append(data.get("status")) and False,
    )
    peer.report("progress", "almost", "2026-10-29T10:00:00+00:00")
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    # Same second, new fields: a different snapshot, so the pull runs at once, and
    # the unrecorded progress it follows goes first; the done waits behind it.
    peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert tries == ["progress", "progress"]


def test_board_has_open_remote_names_only_a_board_with_a_crew_item(recorded, peer):
    async def go() -> None:
        await helpers._board()
        assert work_ledger.board_has_open_remote(CONDUCTOR) is False
        await _remote_item(peer)

    asyncio.run(go())
    assert work_ledger.board_has_open_remote(CONDUCTOR) is True
    assert work_ledger.board_has_open_remote("../escape") is False


# ── the person-wait hold ─────────────────────────────────────────────────────


def test_a_fresh_pull_lets_a_remote_board_hold_and_a_new_report_releases_it(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    asyncio.run(go())
    # The local slot table never names a peer slot, so production's resolver answers
    # closed for it; a remote item reads its closure from the pull record instead.
    held = person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True)
    assert held is not None

    async def again() -> None:
        peer.report("blocked", "still no login", "2026-10-29T10:05:00+00:00", reason="needs_human")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    asyncio.run(again())
    moved = person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True)
    assert moved is not None and moved != held

    peer.slots = []  # the peer slot is gone from a complete list
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None


def test_a_pull_that_could_not_read_the_roster_never_holds(recorded, peer, monkeypatch):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)

    asyncio.run(go())
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True) is not None

    async def unreadable(state, instance_id):
        return None

    # The worker's slot closes, but the roster read fails, so ``closed`` stays stale.
    monkeypatch.setattr(remote_ingest, "_slot_list", unreadable)
    peer.slots = []
    asyncio.run(remote_puller.pull_on_patrol(_state(peer), CONDUCTOR))
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True) is None


def test_a_stale_failed_or_switched_off_pull_never_holds(recorded, peer, monkeypatch):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is not None

    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    old = record.ingested_at
    record.ingested_at = "2020-01-01T00:00:00+00:00"
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None

    record.ingested_at, record.error = old, "crew_pull_timeout"
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None

    record.error = None
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)
    _switch(monkeypatch, False)
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None


def test_a_pull_one_patrol_interval_old_still_holds(recorded, peer):
    from kiro_crew.conductor_patrol import PATROL_INTERVAL_SECS

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.pull_on_patrol(_state(peer), CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    pulled = work_ledger.parse_stamp(work_ledger.read_remote_state(CONDUCTOR, item_id).ingested_at)
    # The next tick comes one interval later; a skipped tick, two.
    one = pulled + timedelta(seconds=PATROL_INTERVAL_SECS)
    two = pulled + timedelta(seconds=2 * PATROL_INTERVAL_SECS)
    assert work_ledger.remote_pull_fresh(CONDUCTOR, item, now=one) is True
    assert work_ledger.remote_pull_fresh(CONDUCTOR, item, now=two) is True
    assert work_ledger.remote_pull_fresh(CONDUCTOR, item, now=two + timedelta(seconds=1)) is False
    assert pulled.tzinfo is not None and pulled <= datetime.now(timezone.utc)
