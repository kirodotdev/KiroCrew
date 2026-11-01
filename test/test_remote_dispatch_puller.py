"""The hub's background report puller (rfc-conductor-remote-dispatch §2, open question 1).

Drives the real routes, store and probe with the wave 4 stub crew. Pinned here:

* switch off: the puller never starts, a run pulls nothing and no peer call is made;
* switch on: a remote ``done`` is filed by one puller run, with no conductor read,
  and the work-ledger probe then raises its wake, as for a local report;
* the real loop files a report within one interval;
* an unreachable crew is marked down and a hanging board is cut off without
  stalling another board's pull;
* the person-wait hold applies to a remote item only while its last pull is fresh;
* ``stop`` ends the task and nothing starts after it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import CONDUCTOR, StubCrew, _read, _remote_item, _state

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest, remote_puller
from kiro_crew.probes.work_ledger import WorkLedgerProbe, person_wait_fingerprint
from kiro_crew.testing.wait import async_wait_until, await_until

_isolated = helpers._isolated
recorded = helpers.recorded


@pytest.fixture(autouse=True)
def _fresh_puller():
    remote_puller.reset_for_tests()
    yield
    remote_puller.reset_for_tests()


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


def test_switch_off_never_starts_the_puller_and_pulls_nothing(recorded, monkeypatch):
    stub = StubCrew()
    _switch(monkeypatch, True)

    async def go() -> None:
        item_id = await _remote_item(stub)
        remote_puller.reset_for_tests()
        stub.calls.clear()
        _switch(monkeypatch, False)
        stub.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        await remote_puller.start_if_enabled(_state(stub))
        assert remote_puller._puller is None
        # A loop started by hand ends at once with the switch off.
        await remote_puller._run(remote_puller._Puller(_state(stub)))
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status is None

    asyncio.run(go())
    assert stub.calls == []


# ── switch on: a remote report wakes without a conductor read ────────────────


def test_one_run_files_a_remote_done_and_the_probe_wakes(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        assert await remote_puller.run_once(_state(peer)) == [CONDUCTOR]
        return item_id

    item_id = asyncio.run(go())
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert item.status == "done" and item.pr == 7
    assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["done"]
    assert any(key.startswith("report:") for key in _wakes())


def test_a_second_run_files_nothing_new(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        await remote_puller.run_once(_state(peer))
        await remote_puller.run_once(_state(peer))

    asyncio.run(go())
    assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["question"]


def test_the_loop_files_a_report_within_one_interval(recorded, peer, monkeypatch):
    monkeypatch.setattr(remote_puller, "PULL_INTERVAL_SECS", 0.05)

    async def go() -> str:
        item_id = await _remote_item(peer)
        remote_puller.ensure_running(_state(peer))
        # The first run has pulled (nothing to file yet); the report lands after it.
        await async_wait_until(lambda: work_ledger.read_remote_state(CONDUCTOR, item_id))
        peer.report("blocked", "no login", "2026-10-29T10:00:00+00:00")
        await async_wait_until(lambda: _status(item_id) == "blocked")
        await remote_puller.stop()
        return item_id

    item_id = asyncio.run(go())
    assert _status(item_id) == "blocked"


def test_the_interval_and_budgets_fit_together():
    assert remote_puller.BOARD_TIMEOUT_SECS < remote_puller.PULL_INTERVAL_SECS
    assert remote_ingest.READ_DEADLINE_SECS < remote_puller.BOARD_TIMEOUT_SECS
    assert 30 <= remote_puller.PULL_INTERVAL_SECS <= 60


# ── bounds ───────────────────────────────────────────────────────────────────


def test_an_unreachable_crew_is_marked_down_and_never_raises(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await remote_puller.run_once(_state(peer))
        return item_id

    item_id = asyncio.run(go())
    stored = work_ledger.read_remote_state(CONDUCTOR, item_id)
    assert stored is not None and stored.crew_state == "disconnected"
    assert work_ledger.read_work_item(CONDUCTOR, item_id).status is None


def test_a_hanging_board_does_not_stall_another(recorded, peer, monkeypatch):
    real = remote_ingest.ingest_board
    hung = asyncio.Event()

    async def ingest(state, board):
        if board == "hangs":
            hung.set()
            await asyncio.Event().wait()
        await real(state, board)

    monkeypatch.setattr(remote_ingest, "ingest_board", ingest)
    monkeypatch.setattr(work_ledger, "boards_with_open_remote", lambda: ["hangs", CONDUCTOR])

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        run = asyncio.ensure_future(remote_puller.run_once(_state(peer)))
        await await_until(hung)
        # The healthy board files while the other is still hung, at the real budget.
        await async_wait_until(lambda: _status(item_id) == "done")
        assert not run.done()
        run.cancel()
        return item_id

    item_id = asyncio.run(go())
    assert _status(item_id) == "done"


def test_a_hung_board_is_cut_off_at_its_budget(monkeypatch):
    async def ingest(state, board):
        await asyncio.Event().wait()

    monkeypatch.setattr(remote_ingest, "ingest_board", ingest)
    monkeypatch.setattr(remote_puller, "BOARD_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(work_ledger, "boards_with_open_remote", lambda: ["hangs"])
    # Returns at all only because the hung board is cut off.
    assert asyncio.run(remote_puller.run_once(SimpleNamespace())) == ["hangs"]


def test_a_conductor_with_no_live_session_still_gets_its_report_filed(recorded, peer, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger as routes

    async def go() -> str:
        item_id = await _remote_item(peer)
        # The conductor's session expired: no live unit, its slot's newest one stands.
        monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "")
        monkeypatch.setattr(session_ledger, "newest_work_unit", lambda key: "u-newest")
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
        await remote_puller.run_once(_state(peer))
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
        await remote_puller.run_once(_state(peer))
        return item_id

    item_id = asyncio.run(go())
    assert peer.calls == []
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).error == "crew_log_unit_unknown"


def test_a_dirty_board_is_not_pulled(recorded, peer):
    async def go() -> list[str]:
        await _remote_item(peer)
        work_ledger.mark_cache_dirty(CONDUCTOR, "test")
        peer.calls.clear()
        return await remote_puller.run_once(_state(peer))

    assert asyncio.run(go()) == []
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
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert len(tries) == 1
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).error == "crew_log_unrecorded"
    # The same stamp inside the window: no second attempt, no second audit row.
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert len(tries) == 1
    # Past the window it is tried again.
    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    record.unrecorded_at = "2020-01-01T00:00:00+00:00"
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert len(tries) == 2
    # A new peer report is tried at once.
    peer.report("done", "shipped again", "2026-10-29T10:05:00+00:00", pr=7)
    asyncio.run(remote_puller.run_once(_state(peer)))
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
    asyncio.run(remote_puller.run_once(_state(peer)))
    # Same second, new fields: a different snapshot, so it is tried at once.
    peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=7)
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert tries == ["progress", "done"]


def test_a_capped_run_rotates_so_no_board_is_left_out(monkeypatch):
    monkeypatch.setattr(remote_puller, "MAX_BOARDS_PER_RUN", 2)
    boards = ["a", "b", "c"]
    seen = [remote_puller._next_window(boards) for _ in range(3)]
    assert seen == [["a", "b"], ["c", "a"], ["b", "c"]]
    assert remote_puller._next_window(["a"]) == ["a"]


def test_the_boot_path_never_imports_the_puller():
    import subprocess

    code = (
        "import sys, asyncio; "
        "from kiro_crew.dashboard.server_runtime import service_hooks; "
        "asyncio.run(service_hooks._stop_remote_puller(None)); "
        "print('kiro_crew.dashboard.handlers.remote_puller' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert out.stdout.strip() == "False"


def test_boards_with_open_remote_names_only_boards_with_a_crew_item(recorded, peer):
    async def go() -> None:
        await helpers._board()
        assert work_ledger.boards_with_open_remote() == []
        await _remote_item(peer)

    asyncio.run(go())
    assert work_ledger.boards_with_open_remote() == [CONDUCTOR]


@pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs privilege there")
def test_boards_are_found_under_a_symlinked_data_home(recorded, peer, monkeypatch, tmp_path):
    from kiro_crew import work_ledger_store

    asyncio.run(_remote_item(peer))
    real = work_ledger_store._work_ledger_root()
    link = tmp_path / "linked-home"
    link.symlink_to(real.parent, target_is_directory=True)
    monkeypatch.setattr("kiro_crew.work_ledger._work_ledger_root", lambda: link / real.name)
    assert work_ledger.boards_with_open_remote() == [CONDUCTOR]


# ── the person-wait hold ─────────────────────────────────────────────────────


def test_a_fresh_pull_lets_a_remote_board_hold_and_a_new_report_releases_it(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.run_once(_state(peer))

    asyncio.run(go())
    # The local slot table never names a peer slot, so production's resolver answers
    # closed for it; a remote item reads its closure from the pull record instead.
    held = person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True)
    assert held is not None

    async def again() -> None:
        peer.report("blocked", "still no login", "2026-10-29T10:05:00+00:00", reason="needs_human")
        await remote_puller.run_once(_state(peer))

    asyncio.run(again())
    moved = person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True)
    assert moved is not None and moved != held

    peer.slots = []  # the peer slot is gone from a complete list
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None


def test_a_pull_that_could_not_read_the_roster_never_holds(recorded, peer, monkeypatch):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await remote_puller.run_once(_state(peer))

    asyncio.run(go())
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True) is not None

    async def unreadable(state, instance_id):
        return None

    # The worker's slot closes, but the roster read fails, so ``closed`` stays stale.
    monkeypatch.setattr(remote_ingest, "_slot_list", unreadable)
    peer.slots = []
    asyncio.run(remote_puller.run_once(_state(peer)))
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: True) is None


def test_a_stale_failed_or_switched_off_pull_never_holds(recorded, peer, monkeypatch):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await _read(peer)
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


# ── shutdown ────────────────────────────────────────────────────────────────


def test_stop_ends_the_task_and_nothing_starts_after_it(recorded, peer, monkeypatch):
    monkeypatch.setattr(remote_puller, "PULL_INTERVAL_SECS", 3600)

    async def go() -> None:
        await remote_puller.start_if_enabled(_state(peer))
        task = remote_puller._puller.task
        await asyncio.sleep(0)
        await remote_puller.stop()
        assert task.done()
        remote_puller.ensure_running(_state(peer))
        assert remote_puller._puller is None

    asyncio.run(go())
