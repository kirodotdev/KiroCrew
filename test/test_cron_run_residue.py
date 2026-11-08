"""Only tagged per-run cron transcripts that are expired or past the keep count go.

Untagged three-part keys may belong to durable agent sequences, independent
of the current job settings. Active, pinned and recent transcripts within each
job's keep count are retained.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import history as history_mod
from kiro_crew.cron_service.run_residue import (
    RUN_TRANSCRIPT_KEEP_PER_JOB,
    RUN_TRANSCRIPT_PRUNE_CAP,
    RUN_TRANSCRIPT_TTL_SECS,
    prune_stale_run_transcripts,
)
from kiro_crew.history import ConversationLog

JOB = "abcd1234"
NOW = 1_800_000_000.0
OLD = NOW - RUN_TRANSCRIPT_TTL_SECS - 3600


@pytest.fixture
def log(tmp_path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    return log


def _write(log: ConversationLog, key: str, *, age_from: float = OLD, tagged: bool = True) -> str:
    with history_mod.allow_on_loop_persist():
        log.append(key, "user", "run")
        if tagged:
            log.update_metadata(key, {"cron_run": "ephemeral"})
    path = log._path(key)
    os.utime(path, (age_from, age_from))
    return os.fspath(path)


def _prune(log: ConversationLog, active=frozenset()) -> int:
    return prune_stale_run_transcripts(active_keys=active, now=NOW, log=log)


def test_an_old_run_transcript_is_removed(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f")
    assert _prune(log) == 1
    assert not os.path.exists(path)


def test_an_untagged_old_three_part_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:deadbeef", tagged=False)
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_a_recent_run_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f", age_from=NOW - 60)
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_the_jobs_stable_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}")
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_an_agent_sequence_jobs_durable_key_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f", tagged=False)
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_a_run_still_held_is_kept(log) -> None:
    key = f"cron:{JOB}:0f0f0f0f"
    path = _write(log, key)
    assert _prune(log, active=frozenset({key})) == 0
    assert os.path.exists(path)


def test_a_pinned_run_transcript_is_kept(log) -> None:
    key = f"cron:{JOB}:0f0f0f0f"
    path = _write(log, key)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(key, {"pinned": True})
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_a_non_cron_session_is_never_touched(log) -> None:
    path = _write(log, "dashboard:chat-1")
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_one_pass_removes_at_most_the_cap(log) -> None:
    for i in range(5):
        _write(log, f"cron:{JOB}:0000000{i}")
    removed = prune_stale_run_transcripts(active_keys=frozenset(), now=NOW, log=log, cap=2)
    assert removed == 2


def test_a_busy_job_keeps_only_its_newest_runs_however_recent(log) -> None:
    """A job firing every minute must not pile up a week of files."""
    paths = [_write(log, f"cron:{JOB}:{i:08x}", age_from=NOW - 60 * (i + 1)) for i in range(25)]
    assert _prune(log) == 25 - RUN_TRANSCRIPT_KEEP_PER_JOB
    kept = [os.path.exists(p) for p in paths]
    # Newest first: the first KEEP stay, every older one is gone.
    assert kept == [True] * RUN_TRANSCRIPT_KEEP_PER_JOB + [False] * 5


def test_each_job_keeps_its_own_newest_runs(log) -> None:
    other = "1234abcd"
    for job in (JOB, other):
        for i in range(RUN_TRANSCRIPT_KEEP_PER_JOB):
            _write(log, f"cron:{job}:{i:08x}", age_from=NOW - 60 * (i + 1))
    assert _prune(log) == 0


def test_a_held_run_past_the_keep_count_is_kept(log) -> None:
    keys = [f"cron:{JOB}:{i:08x}" for i in range(RUN_TRANSCRIPT_KEEP_PER_JOB + 1)]
    for i, key in enumerate(keys):
        _write(log, key, age_from=NOW - 60 * (i + 1))
    oldest = keys[-1]
    assert _prune(log, active=frozenset({oldest})) == 0
    assert os.path.exists(log._path(oldest))


def test_an_untagged_file_past_the_keep_count_is_kept(log) -> None:
    for i in range(RUN_TRANSCRIPT_KEEP_PER_JOB):
        _write(log, f"cron:{JOB}:{i:08x}", age_from=NOW - 60 * (i + 1))
    path = _write(log, f"cron:{JOB}:ffffffff", age_from=NOW - 3600, tagged=False)
    assert _prune(log) == 0
    assert os.path.exists(path)


class _Service:
    def active_session_keys(self) -> frozenset[str]:
        return frozenset()


@pytest.mark.parametrize(("removed", "backlog"), [(RUN_TRANSCRIPT_PRUNE_CAP, True), (3, False)])
def test_the_reaper_reports_a_backlog_only_when_a_pass_fills_its_cap(
    monkeypatch, removed, backlog
) -> None:
    import asyncio

    from kiro_crew.cron_service import run_residue
    from kiro_crew.cron_service.service import run_teardown

    monkeypatch.setattr(run_residue, "prune_stale_run_transcripts", lambda **_kw: removed)
    assert asyncio.run(run_teardown._prune_run_residue(_Service(), NOW)) is backlog


def test_the_reaper_reports_no_backlog_when_a_pass_raises(monkeypatch) -> None:
    import asyncio

    from kiro_crew.cron_service import run_residue
    from kiro_crew.cron_service.service import run_teardown

    def boom(**_kw):
        raise OSError("disk")

    monkeypatch.setattr(run_residue, "prune_stale_run_transcripts", boom)
    assert asyncio.run(run_teardown._prune_run_residue(_Service(), NOW)) is False
