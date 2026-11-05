"""Only tagged, expired per-run cron transcripts are removed.

Untagged three-part keys may belong to durable agent sequences, independent
of the current job settings. Active, recent and pinned transcripts are retained.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import history as history_mod
from kiro_crew.cron_service.run_residue import (
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
