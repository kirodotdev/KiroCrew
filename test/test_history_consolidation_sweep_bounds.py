"""The idle sweep's loop-thread work stays flat however many sessions are tracked.

``check_idle_sessions`` runs on the event loop every heartbeat. A ``_busy`` that
asks the disk for each running key costs a sweep tracked x running file checks,
which freezes the loop on a host with a large backlog. ``_busy`` compares key
strings, and one sweep examines a bounded number of keys and starts a bounded
amount of work.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kiro_crew import history_consolidation as hc
from kiro_crew.history import ConversationLog, HistoryConsolidator


@pytest.fixture
def consolidator(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_persistence_disabled", lambda: False)
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    memory = MagicMock()
    c = HistoryConsolidator(log=log, memory=memory, migrated=True, history_idle_secs=60)
    c._activity_seeded = True  # no restart seed in these tests
    return c


def test_busy_reads_no_file(consolidator, monkeypatch):
    consolidator._running.update(f"dashboard:chat-{i}" for i in range(50))
    consolidator._running.add("slack:1700000000.000100")

    def no_disk(_key):
        raise AssertionError("_busy touched the disk")

    monkeypatch.setattr(consolidator._log, "_path", no_disk)

    assert consolidator._busy("dashboard:chat-7")
    assert not consolidator._busy("dashboard:chat-other")
    # A Slack thread's bare legacy key can name the same file as its canonical key.
    assert consolidator._busy("1700000000.000100")


def test_busy_matches_keys_with_one_filename(consolidator):
    # Two spellings that sanitize to one transcript name are one transcript.
    consolidator._running.add("dashboard:chat-a")

    assert consolidator._busy("dashboard_chat-a")
    assert not consolidator._busy("dashboard:chat-b")


def test_one_sweep_examines_a_bounded_number_of_keys(consolidator, monkeypatch):
    stamped: list[str] = []

    def stamp(key):
        stamped.append(key)
        raise FileNotFoundError  # a deleted transcript: forgotten, nothing started

    monkeypatch.setattr(consolidator, "_transcript_stamp", stamp)
    old = time.time() - 3600
    keys = [f"dashboard:chat-{i}" for i in range(500)]
    for key in keys:
        consolidator._last_activity[key] = old

    consolidator.check_idle_sessions()

    assert len(stamped) == hc._IDLE_EXAMINED_PER_SWEEP


def test_keys_past_the_budget_get_their_turn(consolidator, monkeypatch):
    stamped: list[str] = []

    def stamp(key):
        stamped.append(key)
        return None  # unreadable: no idle-clean skip

    monkeypatch.setattr(consolidator, "_transcript_stamp", stamp)
    monkeypatch.setattr(consolidator, "_inside_retry_deadline", lambda *_a: True)
    old = time.time() - 3600
    keys = [f"dashboard:chat-{i}" for i in range(3 * hc._IDLE_EXAMINED_PER_SWEEP)]
    for key in keys:
        consolidator._last_activity[key] = old

    for _ in range(3):
        consolidator.check_idle_sessions()

    assert sorted(stamped) == sorted(keys), "a key past one sweep's budget was never examined"


def test_a_sweep_starts_nothing_while_the_in_flight_cap_is_reached(consolidator, monkeypatch):
    stamped: list[str] = []
    monkeypatch.setattr(consolidator, "_transcript_stamp", lambda key: stamped.append(key))
    consolidator._running.update(f"dashboard:busy-{i}" for i in range(hc._IDLE_MAX_IN_FLIGHT))
    consolidator._last_activity["dashboard:chat-idle"] = time.time() - 3600

    consolidator.check_idle_sessions()

    assert stamped == []
