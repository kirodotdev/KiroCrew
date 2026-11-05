"""A leased marker write syncs with no transcript lock held and checks the lease at the commit.

The consolidation marker writers stage and sync the whole transcript before they
take the transcript lock. A leased pass must not wrap them in the lease fence (that
lock would make every appender of the session wait on the flush), so the lease is
checked under the lock that commits the rename, after the staging, and a lease lost
while staging writes nothing.
"""

import contextlib
import json

import pytest

from kiro_crew import history as history_mod
from kiro_crew import history_rewrite
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation import _LEASE_TOKEN, _LeaseLostMidRun

KEY = "dashboard:chat-lease-commit"


def _seed(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(3):
            log.append(KEY, "user", f"m{i}")
    cons = HistoryConsolidator(log=log, memory=None, migrated=True, sessions=None)
    lease = cons._acquire_consolidation_lease(KEY)
    assert lease
    return log, cons, lease


def _marker(log):
    return json.loads(log._path(KEY).read_text(encoding="utf-8").splitlines()[0]).get(
        "last_consolidated", 0
    )


def _record(monkeypatch, log, cons, events, on_stage=None):
    depth = [0]
    real_locked = log._locked

    @contextlib.contextmanager
    def locked(key):
        with real_locked(key):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    real_stage = history_rewrite.stage_replacement

    def stage(path, content, **kw):
        events.append(("stage", depth[0] > 0))
        if on_stage is not None:
            on_stage()
        return real_stage(path, content, **kw)

    real_current = cons._lease_is_current

    def current(key, token):
        events.append(("validate", depth[0] > 0))
        return real_current(key, token)

    monkeypatch.setattr(log, "_locked", locked)
    monkeypatch.setattr(history_rewrite, "stage_replacement", stage)
    monkeypatch.setattr(cons, "_lease_is_current", current)


def test_a_leased_marker_write_stages_unlocked_then_checks_the_lease_under_the_lock(
    tmp_path, monkeypatch
):
    log, cons, lease = _seed(tmp_path)
    events: list = []
    _record(monkeypatch, log, cons, events)

    cons._mark_consolidated_fenced(KEY, 2, log.rotation_generation(KEY), lease)

    assert events == [("stage", False), ("validate", True)]
    assert _marker(log) == 2


def test_a_lease_lost_while_staging_commits_nothing(tmp_path, monkeypatch):
    log, cons, lease = _seed(tmp_path)
    events: list = []

    def steal():
        # Another process takes the lease: a raw write, so no marker path runs.
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta[_LEASE_TOKEN] = "someone-else"
        lines[0] = json.dumps(meta) + "\n"
        path.write_text("".join(lines), encoding="utf-8")

    _record(monkeypatch, log, cons, events, on_stage=steal)

    with pytest.raises(_LeaseLostMidRun):
        cons._mark_consolidated_fenced(KEY, 2, log.rotation_generation(KEY), lease)

    assert events[0] == ("stage", False)
    assert ("validate", True) in events
    assert _marker(log) == 0


def test_a_lease_renewal_runs_no_fsync_while_the_transcript_lock_is_held(tmp_path, monkeypatch):
    """The renewal is a small locked conditional write: unsynced, so no appender waits."""
    import os

    log, cons, lease = _seed(tmp_path)
    depth = [0]
    real_locked = log._locked

    @contextlib.contextmanager
    def locked(key):
        with real_locked(key):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    synced_under_lock = []
    real_fsync = os.fsync

    def fsync(fd):
        if depth[0] > 0:
            synced_under_lock.append(fd)
        return real_fsync(fd)

    # Count the locked writes, not changed bytes: a renewal in the same clock
    # tick as the acquisition can write an identical line.
    writes = []
    real_write = log._update_metadata_locked

    def write(key, fields):
        writes.append(key)
        return real_write(key, fields)

    monkeypatch.setattr(log, "_locked", locked)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(log, "_update_metadata_locked", write)

    assert cons._renew_consolidation_lease(KEY, lease) is True

    assert writes == [KEY]
    assert synced_under_lock == []
