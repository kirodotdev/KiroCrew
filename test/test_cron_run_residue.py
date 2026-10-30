"""Old per-run transcripts of stateless cron jobs are removed; nothing else is.

Every fire of a ``persistent_session=False`` job writes its own
``cron_<job>_<run>.jsonl``. Nothing read them after the run, yet they piled up
by the thousand and every full session listing stat'd each one. These tests pin
which files the prune removes and every case it must keep.
"""

from __future__ import annotations

import os
import time

import pytest

from kiro_crew import history as history_mod
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.run_residue import (
    RUN_TRANSCRIPT_TTL_SECS,
    prune_stale_run_transcripts,
)
from kiro_crew.history import ConversationLog

JOB = "abcd1234"
OLD = time.time() - RUN_TRANSCRIPT_TTL_SECS - 3600


def _job(**kw) -> CronJob:
    kw.setdefault("persistent_session", False)
    return CronJob(id=JOB, name="poll", message="m", **kw)


@pytest.fixture
def log(tmp_path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    return log


def _write(log: ConversationLog, key: str, *, age_from: float = OLD) -> str:
    with history_mod.allow_on_loop_persist():
        log.append(key, "user", "run")
    path = log._path(key)
    os.utime(path, (age_from, age_from))
    return os.fspath(path)


def _prune(log: ConversationLog, jobs=None, active=frozenset()) -> int:
    return prune_stale_run_transcripts(
        jobs={JOB: _job()} if jobs is None else jobs,
        active_keys=active,
        now=time.time(),
        log=log,
    )


def test_an_old_run_transcript_is_removed(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f")
    assert _prune(log) == 1
    assert not os.path.exists(path)


def test_a_deleted_jobs_old_three_part_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:deadbeef")
    assert _prune(log, jobs={}) == 0
    assert os.path.exists(path)


def test_a_recent_run_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f", age_from=time.time() - 60)
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_the_jobs_stable_transcript_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}")
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_a_persistent_jobs_three_part_key_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f")
    assert _prune(log, jobs={JOB: _job(persistent_session=True)}) == 0
    assert os.path.exists(path)


def test_an_agent_sequence_jobs_durable_key_is_kept(log) -> None:
    path = _write(log, f"cron:{JOB}:0f0f0f0f")
    job = _job(agent_sequence=["deadbeef", "0f0f0f0f"])
    assert _prune(log, jobs={JOB: job}) == 0
    assert os.path.exists(path)


def test_a_run_still_held_is_kept(log) -> None:
    key = f"cron:{JOB}:0f0f0f0f"
    path = _write(log, key)
    assert _prune(log, active=frozenset({key})) == 0
    assert os.path.exists(path)


def test_a_pinned_run_transcript_is_kept(log) -> None:
    key = f"cron:{JOB}:0f0f0f0f"
    with history_mod.allow_on_loop_persist():
        log.append(key, "user", "run")
        log.update_metadata(key, {"pinned": True})
    path = os.fspath(log._path(key))
    os.utime(path, (OLD, OLD))
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_a_non_cron_session_is_never_touched(log) -> None:
    path = _write(log, "dashboard:chat-1")
    assert _prune(log) == 0
    assert os.path.exists(path)


def test_one_pass_removes_at_most_the_cap(log) -> None:
    for i in range(5):
        _write(log, f"cron:{JOB}:0000000{i}")
    removed = prune_stale_run_transcripts(
        jobs={JOB: _job()}, active_keys=frozenset(), now=time.time(), log=log, cap=2
    )
    assert removed == 2
