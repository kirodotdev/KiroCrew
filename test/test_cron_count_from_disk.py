"""Regression tests for ``CronService.count_enabled_from_disk``.

The dashboard WS status pusher needs an enabled-job count OFF the event loop.
Routing ``list_jobs`` through ``asyncio.to_thread`` was unsafe: ``list_jobs`` →
``_sync()`` → ``_load()`` → ``_arm_timer()`` calls ``asyncio.create_task`` with
no running loop in the worker thread (``RuntimeError``) AND cancels the live
timer first, silently stopping all scheduled jobs.

``count_enabled_from_disk`` is a pure read-only file parse: it must return the
enabled count and NEVER touch ``self._jobs``, ``self._last_mtime`` or the timer.
"""

from __future__ import annotations

import json

from kiro_crew.cron import CronService, _record_is_enabled


def _write_store(path, jobs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")


def test_count_enabled_from_disk_counts_only_enabled(tmp_path):
    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [
            {"id": "a", "name": "a", "message": "m", "schedule": {"kind": "every"}},
            {
                "id": "b",
                "name": "b",
                "message": "m",
                "schedule": {"kind": "every"},
                "user_paused": True,
            },
            {
                "id": "c",
                "name": "c",
                "message": "m",
                "schedule": {"kind": "every"},
                "auto_paused": True,
            },
            {
                "id": "d",
                "name": "d",
                "message": "m",
                "schedule": {"kind": "every"},
                "enabled": False,
            },
        ],
    )
    # a=enabled, b=user_paused, c=auto_paused, d=legacy !enabled → only 1.
    assert mgr.count_enabled_from_disk() == 1


def test_count_enabled_from_disk_missing_file_is_zero(tmp_path):
    mgr = CronService(base_dir=tmp_path)
    assert mgr.count_enabled_from_disk() == 0


def test_count_enabled_from_disk_never_arms_timer_or_mutates_state(tmp_path):
    """FAILURE SCENARIO (why list_jobs was unsafe off-thread): _arm_timer runs
    when _running. count_enabled_from_disk must NOT sync, reload, or arm."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [{"id": "a", "name": "a", "message": "m", "schedule": {"kind": "every"}}],
    )
    # Simulate the running manager whose in-memory state is intentionally empty
    # (never loaded). A read-only count must not repopulate it or arm a timer.
    mgr._running = True
    armed = {"n": 0}
    mgr._arm_timer = lambda: armed.__setitem__("n", armed["n"] + 1)  # type: ignore[method-assign]

    assert mgr.count_enabled_from_disk() == 1
    assert armed["n"] == 0, "count must never arm the timer"
    assert mgr._jobs == [], "count must not mutate in-memory jobs"
    assert mgr._last_mtime == 0.0, "count must not touch mtime tracking"


def test_count_path_and_load_path_share_one_predicate(tmp_path):
    """Both readers derive enabled from ``_record_is_enabled`` — a single owner.

    Locks the anti-drift guarantee from the Arbiter finding: the off-thread
    count and the scheduler deserialization must agree on the enabled set for
    every pause-state shape, so a future pause-field change cannot land in one
    reader and silently diverge the dashboard count from the scheduler.
    """
    jobs = [
        {"id": "a", "name": "a", "message": "m", "schedule": {"kind": "every"}},
        {"id": "b", "name": "b", "message": "m", "schedule": {"kind": "every"}, "user_paused": True},
        {"id": "c", "name": "c", "message": "m", "schedule": {"kind": "every"}, "auto_paused": True},
        {"id": "d", "name": "d", "message": "m", "schedule": {"kind": "every"}, "enabled": False},
        {"id": "e", "name": "e", "message": "m", "schedule": {"kind": "every"}, "enabled": True, "user_paused": False},
    ]
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, jobs)

    # Count path.
    count_enabled = mgr.count_enabled_from_disk()
    # Load path: enabled flag of each deserialized job.
    mgr._load()
    load_enabled = sum(1 for job in mgr._jobs if job.enabled)
    # Shared predicate applied directly to the raw records.
    predicate_enabled = sum(1 for j in jobs if _record_is_enabled(j))

    assert count_enabled == load_enabled == predicate_enabled == 2
    # Per-record agreement, not just the totals.
    for job, raw in zip(mgr._jobs, jobs):
        assert job.enabled == _record_is_enabled(raw)
