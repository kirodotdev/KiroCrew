"""The auto_research watchdog: the unresponsive deadline, the cycle-file and finding
helpers it reads, and terminal settlement (the stop tombstone, cancellation and
durable loop removal). ``test_auto_research.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from auto_research_helpers import isolate_fixture  # noqa: F401 - fixture

from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    create_campaign,
    get_campaign,
    update_campaign_status,
)
from kiro_crew.testing.fixtures import one_pool_thread  # noqa: F401 (fixture)
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

# --- Watchdog unresponsive grace ---


class TestUnresponsiveDeadline:
    def test_generous_floor_and_scaling(self):
        from kiro_crew.apps.builtins.auto_research.handlers import (
            _FIRST_CYCLE_GRACE_SECS,
            _unresponsive_deadline,
        )

        # Small idle -> generous floor (deep research cycles take minutes), not
        # the tight idle*2 that falsely failed healthy slow cycles.
        assert _unresponsive_deadline(60) == _FIRST_CYCLE_GRACE_SECS
        # Large idle scales above the floor.
        assert _unresponsive_deadline(400) == 800


class TestWatchdogStopTombstone:
    @pytest.mark.asyncio
    async def test_terminal_status_persistence_runs_off_event_loop(self, monkeypatch):
        """SQLite/sidecar persistence cannot block the watchdog's event loop."""
        from kiro_crew.apps.builtins.auto_research import handlers as h

        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        persisted_off_loop = False

        def _persist(_cid, _status, **_kwargs):
            nonlocal persisted_off_loop
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                persisted_off_loop = True
            else:
                pytest.fail("campaign status persistence ran on the event loop")

        monkeypatch.setattr(h, "update_campaign_status", _persist)
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: True)
        monkeypatch.setattr(h, "_stop_loop", AsyncMock())
        monkeypatch.setattr(h, "_emit_sse", lambda _event: None)

        last_counts = {"a1b2c3d4": 1}
        last_ts = {"a1b2c3d4": 1.0}
        await h._settle_campaign_from_watchdog(
            "a1b2c3d4",
            [],
            last_counts,
            last_ts,
            observed_started_at=1.0,
        )

        assert persisted_off_loop
        assert last_counts == {}
        assert last_ts == {}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("one_pool_thread")
    async def test_cancellation_waits_for_persisted_loop_removal(self, tmp_path, monkeypatch):
        """Shutdown cannot split terminal status from durable loop cleanup."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        cid = "a1b2c3d4"
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key=f"research-{cid}", message="watch", idle_secs=60)
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        status_write_started = threading.Event()
        release_status_write = threading.Event()

        def _persist(_cid, _status, **_kwargs):
            status_write_started.set()
            assert release_status_write.wait(
                timeout=default_timeout()
            ), "the test never released the status write"

        monkeypatch.setattr(h, "update_campaign_status", _persist)
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: True)
        task = asyncio.create_task(
            h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=1.0)
        )
        await async_wait_until(
            status_write_started.is_set,
            describe=lambda: "the terminal status write never started",
        )

        task.cancel()
        # flake-ok: one-turn yield barrier before a negative assert, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not task.done()
        release_status_write.set()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: unbounded await of a task this test just released, pre-existing shape, moved verbatim; tracked for a de-flake pass
            await task

        assert svc.get_by_slot(f"research-{cid}") is None
        assert loop.id not in svc._timers
        svc.stop()

        restored = AutoNudgeService(base_dir=tmp_path)
        await restored.start()
        assert restored.get_by_slot(f"research-{cid}") is None
        restored.stop()

    @pytest.mark.asyncio
    async def test_slow_settlement_cannot_remove_resumed_replacement(self, tmp_path, monkeypatch):
        """Terminal cleanup is bound to the loop that produced the signal.

        Once terminal status is visible, Resume may replace the old slot-bound
        loop while settlement is still finishing its persistence work. Cleanup
        must not re-resolve the slot and remove that replacement.
        """
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        cid = "a1b2c3d4"
        slot_key = f"research-{cid}"
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        old_loop = await svc.add(slot_key=slot_key, message="old run", idle_secs=60)
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        status_write_started = threading.Event()
        release_status_write = threading.Event()

        def _persist(_cid, _status, **_kwargs):
            status_write_started.set()
            assert release_status_write.wait(
                timeout=default_timeout()
            ), "the test never released the status write"

        monkeypatch.setattr(h, "update_campaign_status", _persist)
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: True)
        monkeypatch.setattr(h, "_emit_sse", lambda _event: None)

        settlement = asyncio.create_task(
            h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=1.0)
        )
        await async_wait_until(
            status_write_started.is_set,
            describe=lambda: "the terminal status write never started",
        )

        replacement = await svc.add(
            slot_key=slot_key,
            message="resumed run",
            idle_secs=60,
        )
        assert replacement.id != old_loop.id
        release_status_write.set()
        # flake-ok: unbounded await of a task this test just released, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await settlement

        assert svc.get_by_slot(slot_key) is replacement
        svc.stop()

    @pytest.mark.asyncio
    async def test_cancellation_propagates_after_status_write_failure(self, tmp_path, monkeypatch):
        """A failed status write cannot swallow watchdog shutdown."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        cid = "a1b2c3d4"
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key=f"research-{cid}", message="watch", idle_secs=60)
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        status_write_started = threading.Event()
        release_status_write = threading.Event()

        def _persist(_cid, _status, **_kwargs):
            status_write_started.set()
            assert release_status_write.wait(
                timeout=default_timeout()
            ), "the test never released the status write"
            raise OSError("status store unavailable")

        monkeypatch.setattr(h, "update_campaign_status", _persist)
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: True)
        task = asyncio.create_task(
            h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=1.0)
        )
        await async_wait_until(
            status_write_started.is_set,
            describe=lambda: "the terminal status write never started",
        )

        task.cancel()
        release_status_write.set()
        # The settle runs real durable work after the cancel (a SQLite open and
        # schema check off the loop); on a loaded hosted Windows runner that takes
        # seconds, so the wait is the lost-run bound, not a number.
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled settlement finishing")

        # A non-terminal campaign keeps its loop so restart can retry settling.
        assert svc.get_by_slot(f"research-{cid}") is loop
        svc.stop()

    @pytest.mark.asyncio
    async def test_settlement_failure_is_logged_without_replacing_cancellation(
        self, tmp_path, monkeypatch, caplog
    ):
        """The watchdog reports a settlement failure it swallowed during shutdown.

        The shield-and-settle discipline deliberately lets the ORIGINAL
        cancellation win over a worker failure; the failure's only remaining
        trace is the watchdog's log record. Pin that record so folding the
        inline settlement loop onto ``_settle_before_cancellation`` (or any
        later refactor of it) cannot silently drop the reporting path.
        """
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        cid = "a1b2c3d4"
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        await svc.add(slot_key=f"research-{cid}", message="watch", idle_secs=60)
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        status_write_started = threading.Event()
        release_status_write = threading.Event()

        def _persist(_cid, _status, **_kwargs):
            status_write_started.set()
            assert release_status_write.wait(
                timeout=default_timeout()
            ), "the test never released the status write"
            raise OSError("status store unavailable")

        monkeypatch.setattr(h, "update_campaign_status", _persist)
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: True)
        task = asyncio.create_task(
            h._settle_campaign_from_watchdog(cid, [], {}, {}, observed_started_at=1.0)
        )
        await async_wait_until(
            status_write_started.is_set,
            describe=lambda: "the terminal status write never started",
        )

        try:
            task.cancel()
            release_status_write.set()
            with caplog.at_level(logging.ERROR, logger=h.__name__):
                with pytest.raises(asyncio.CancelledError):
                    await await_until(task, what="the cancelled settlement finishing")

            failure_records = [
                r
                for r in caplog.records
                if "terminal settlement failed during shutdown" in r.getMessage()
            ]
            assert failure_records, "the swallowed settlement failure must be logged"
            assert (
                failure_records[0].exc_info is not None
            ), "the log record must carry the worker failure's traceback"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_partial_terminal_persistence_still_removes_durable_loop(
        self, _isolate: Path, monkeypatch
    ):
        """A terminal SQLite commit cannot retain a restartable loop."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        observed_started_at = get_campaign(cid)["started_at"]

        store = _isolate / "autonudge"
        svc = AutoNudgeService(base_dir=store)
        await svc.start()
        loop = await svc.add(
            slot_key=f"research-{cid}",
            message="watch",
            idle_secs=60,
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        real_write_status = h.write_status

        def _fail_terminal_sidecar(campaign_id: str, status: str, **kwargs):
            if status == CampaignStatus.STOPPED:
                raise OSError("status sidecar unavailable")
            real_write_status(campaign_id, status, **kwargs)

        monkeypatch.setattr(h, "write_status", _fail_terminal_sidecar)
        events = []
        monkeypatch.setattr(h, "_emit_sse", events.append)
        last_counts = {cid: 0}
        last_ts = {cid: 1.0}

        with pytest.raises(OSError, match="status sidecar unavailable"):
            await h._settle_campaign_from_watchdog(
                cid,
                [],
                last_counts,
                last_ts,
                observed_started_at=observed_started_at,
            )

        assert get_campaign(cid)["status"] == CampaignStatus.STOPPED
        status_payload = json.loads((h._campaign_dir(cid) / "status.json").read_text())
        assert status_payload["status"] == CampaignStatus.RUNNING
        assert svc.get_by_slot(f"research-{cid}") is None
        assert loop.id not in svc._timers
        assert last_counts == {}
        assert last_ts == {}
        assert events == []
        svc.stop()

        restored = AutoNudgeService(base_dir=store)
        await restored.start()
        assert restored.get_by_slot(f"research-{cid}") is None
        restored.stop()

    @pytest.mark.asyncio
    async def test_transient_terminal_loop_removal_is_retried(self, _isolate: Path, monkeypatch):
        """A transient final store failure is retried to durable removal."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        observed_started_at = get_campaign(cid)["started_at"]

        store = _isolate / "autonudge"
        svc = AutoNudgeService(base_dir=store)
        await svc.start()
        await svc.add(
            slot_key=f"research-{cid}",
            message="watch",
            idle_secs=60,
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        real_write_state = svc._write_state
        removal_failed = False

        def _fail_first_removal(payload: dict) -> None:
            nonlocal removal_failed
            if not payload["loops"] and not removal_failed:
                removal_failed = True
                raise OSError("autonudge store unavailable")
            real_write_state(payload)

        monkeypatch.setattr(svc, "_write_state", _fail_first_removal)

        await h._settle_campaign_from_watchdog(
            cid,
            [],
            {cid: 0},
            {cid: 1.0},
            observed_started_at=observed_started_at,
        )

        assert removal_failed
        assert get_campaign(cid)["status"] == CampaignStatus.STOPPED
        assert svc.get_by_slot(f"research-{cid}") is None
        svc.stop()

        restored = AutoNudgeService(base_dir=store)
        await restored.start()
        assert restored.get_by_slot(f"research-{cid}") is None
        restored.stop()

    @pytest.mark.asyncio
    async def test_exhausted_terminal_loop_removal_stays_inactive_after_restart(
        self, _isolate: Path, monkeypatch
    ):
        """A terminal cleanup failure cannot leave restartable persisted work."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AutoNudgeService

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        observed_started_at = get_campaign(cid)["started_at"]

        store = _isolate / "autonudge"
        svc = AutoNudgeService(base_dir=store)
        await svc.start()
        loop = await svc.add(
            slot_key=f"research-{cid}",
            message="watch",
            idle_secs=60,
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda _cid, _files, *, stopped_reason="": (CampaignStatus.STOPPED, None),
        )

        real_write_state = svc._write_state
        removal_attempts = 0

        def _fail_every_removal(payload: dict) -> None:
            nonlocal removal_attempts
            if not payload["loops"]:
                removal_attempts += 1
                raise OSError("autonudge store unavailable")
            real_write_state(payload)

        monkeypatch.setattr(svc, "_write_state", _fail_every_removal)

        last_counts = {cid: 0}
        last_ts = {cid: 1.0}
        with pytest.raises(OSError, match="autonudge store unavailable"):
            await h._settle_campaign_from_watchdog(
                cid,
                [],
                last_counts,
                last_ts,
                observed_started_at=observed_started_at,
            )

        assert removal_attempts == h._TERMINAL_LOOP_REMOVAL_ATTEMPTS
        assert get_campaign(cid)["status"] == CampaignStatus.STOPPED
        retained = svc.get_by_slot(f"research-{cid}")
        assert retained is not None
        assert retained.id == loop.id
        assert retained.active is False
        assert retained.id not in svc._timers
        assert last_counts == {}
        assert last_ts == {}
        svc.stop()

        restored = AutoNudgeService(base_dir=store)
        await restored.start()
        restored_loop = restored.get_by_slot(f"research-{cid}")
        assert restored_loop is not None
        assert restored_loop.id == loop.id
        assert restored_loop.active is False
        assert restored_loop.id not in restored._timers
        restored.stop()

    @pytest.mark.asyncio
    async def test_new_run_defers_stale_tombstone_without_rearming(
        self, _isolate: Path, monkeypatch
    ):
        """A resume gets one poll for launch to remove prior-run stop evidence."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AUTONUDGE_STOP_REASON

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        findings = _isolate / "research" / cid / "findings"
        findings.mkdir(parents=True, exist_ok=True)
        findings.joinpath("cycle_001.json").write_text(
            json.dumps({"summary": "prior run synthesis", "verification": None})
        )

        stale_loop = SimpleNamespace(
            id="stale-stop",
            slot_key=f"research-{cid}",
            active=False,
            stopped_reason=AUTONUDGE_STOP_REASON,
        )
        svc = MagicMock()
        # _launch_loop removes the stale record between the first and second
        # watchdog polls in the production race this test models.
        svc.get_by_slot.side_effect = [stale_loop, None]
        svc.update = AsyncMock()
        svc.remove = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: True)

        sleep_calls = 0

        async def _run_two_polls(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 2:
                raise asyncio.CancelledError

        # flake-ok: patches the shared asyncio.sleep under monkeypatch, pre-existing shape, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(h.asyncio, "sleep", _run_two_polls)
        slot = SimpleNamespace(_trust=True, running=False)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})

        # flake-ok: unbounded await of the watchdog loop the patched sleep ends, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await h._watchdog_loop({"state": state})

        assert get_campaign(cid)["status"] == CampaignStatus.RUNNING
        svc.update.assert_not_awaited()
        svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_settles_on_first_safe_poll_without_rearming(self, _isolate: Path, monkeypatch):
        """A stop tombstone waits for the worker turn, then outranks expiry
        handling instead of being revived like an app-disable pause."""
        from kiro_crew.apps.builtins.auto_research import handlers as h
        from kiro_crew.autonudge import AUTONUDGE_STOP_REASON

        campaign = create_campaign(
            {
                "question": "Research a sufficiently detailed question here",
                "sources": ["web"],
            }
        )
        cid = campaign["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        db = h._get_db()
        db.execute(
            "UPDATE campaigns SET started_at=? WHERE id=?",
            # flake-ok: wall-clock backdate one second past the trust TTL, pre-existing shape, moved verbatim; tracked for a de-flake pass
            (time.time() - h._TRUST_TTL_SECS - 1, cid),
        )
        db.commit()
        db.close()
        findings = _isolate / "research" / cid / "findings"
        findings.mkdir(parents=True, exist_ok=True)
        findings.joinpath("cycle_001.json").write_text(
            json.dumps({"summary": "final synthesis", "verification": None})
        )

        loop = SimpleNamespace(
            id="loop-stop",
            slot_key=f"research-{cid}",
            active=False,
            stopped_reason=AUTONUDGE_STOP_REASON,
        )
        svc = MagicMock()
        svc.get_by_slot.return_value = loop
        svc.update = AsyncMock()
        svc.remove = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: True)

        sleep_calls = 0
        slot = SimpleNamespace(_trust=True, running=True)
        events: list[dict] = []
        advance_threads: list[int] = []

        def _advance_exploration_off_loop(campaign_id):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                advance_threads.append(threading.get_ident())
            else:
                pytest.fail("final-cycle bookkeeping ran on the event loop")
            assert campaign_id == cid

        async def _finish_worker_then_stop(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                findings.joinpath("cycle_002.json").write_text(
                    json.dumps({"summary": "last worker-turn synthesis", "verification": None})
                )
                slot.running = False
            elif sleep_calls > 2:
                raise asyncio.CancelledError

        async def _remove_after_turn(_loop_id):
            assert slot.running is False

        svc.remove.side_effect = _remove_after_turn
        # flake-ok: patches the shared asyncio.sleep under monkeypatch, pre-existing shape, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(h.asyncio, "sleep", _finish_worker_then_stop)
        monkeypatch.setattr(h, "_emit_sse", events.append)
        monkeypatch.setattr(h, "_advance_exploration", _advance_exploration_off_loop)
        original_list_cycle_files = h._list_cycle_files
        scan_threads: list[int] = []

        def _list_cycle_files_off_loop(campaign_id):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                scan_threads.append(threading.get_ident())
            else:
                pytest.fail("tombstone finding scan ran on the event loop")
            return original_list_cycle_files(campaign_id)

        monkeypatch.setattr(h, "_list_cycle_files", _list_cycle_files_off_loop)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})

        # flake-ok: unbounded await of the watchdog loop the patched sleep ends, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await h._watchdog_loop({"state": state})

        settled = get_campaign(cid)
        assert settled["status"] == CampaignStatus.STOPPED
        assert settled["total_cycles"] == 2
        assert events == [
            {
                "type": "new_finding",
                "campaign_id": cid,
                "finding": {
                    "summary": "last worker-turn synthesis",
                    "verification": None,
                },
            },
            {"type": "stopped", "campaign_id": cid},
        ]
        assert len(advance_threads) == 1
        assert len(scan_threads) == 2
        svc.update.assert_not_awaited()
        svc.remove.assert_awaited_once_with(loop.id)


class TestWatchdogFindingHelpers:
    def test_list_cycle_files_sorted_newest_last(self):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        camp = h.create_campaign(
            {
                "question": "Watchdog helper question padded enough",
                "sub_questions": [],
                "sources": ["web"],
                "max_cycles": 20,
            }
        )
        cid = camp["id"]
        fdir = h._campaign_dir(cid) / "findings"
        fdir.mkdir(parents=True, exist_ok=True)
        for n in (1, 2, 10, 12):
            (fdir / f"cycle_{n:03d}.json").write_text(json.dumps({"cycle": n}))
        files = h._list_cycle_files(cid)
        assert len(files) == 4
        assert files[-1].name == "cycle_012.json"

    def test_list_cycle_files_invalid_id_empty(self):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        assert h._list_cycle_files("../../etc") == []

    def test_read_finding_file_redacts(self, tmp_path: Path):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        p = tmp_path / "cycle_001.json"
        p.write_text(json.dumps({"summary": "leaked AKIAIOSFODNN7EXAMPLE here"}))
        out = h._read_finding_file(p)
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(out)

    def test_read_finding_file_bad_json_returns_empty(self, tmp_path: Path):
        from kiro_crew.apps.builtins.auto_research import handlers as h

        p = tmp_path / "cycle_001.json"
        p.write_text("not json{{{")
        assert h._read_finding_file(p) == {}
