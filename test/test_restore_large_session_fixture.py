"""CI fixture for bounded-time restore of a large session.

The target is a restore that stays bounded for a large transcript and a large crew-log
unit. This file builds, at test time, a synthetic home with made-up ids and text in the
real row and record shapes, and pins the restore's COST by counting work, never by
timing it, so it cannot flake on a slow runner:

* the transcript half writes ONE transcript at the 16 MiB threshold plus 40 small tabs,
  and counts the rows the open-tab restore read parses (``_read_messages``);
* the crew-log half writes ONE slot unit through the real :class:`CrewLog` writer and
  counts the walks (``CrewLog._walk_from``) a cold prime of every slot fold makes.

Where the restore is over the bar, the test asserts the measured count as a CEILING (a
change that makes it worse fails) and a strict ``xfail`` states the TARGET. A change that
reaches the target removes the ``xfail`` marker; strict means it cannot reach it unnoticed.

SCALE. The transcript is at the full 16 MiB threshold: building it is a plain file write
and the counts do not depend on timing. The crew-log unit is about 2 MiB, one eighth of a
16 MiB unit, because a cold walk decodes every record once per fold and the full size
would cost seconds on a CI runner. The walk COUNT per fold does not depend on the unit's
size (one walk per fold at 1 MiB and at 136 MiB alike), so the scaled unit pins the same
quantity the full one would.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.projection_budget import _SLOT_FOLD_ROW_BYTES
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence_parts import restore_drivers
from kiro_crew.history import ConversationLog

#: The transcript threshold and the tail window proposed above it.
THRESHOLD_BYTES = 16 * 1024 * 1024
TAIL_WINDOW_ROWS = 500
#: Open tabs besides the large one.
SMALL_TABS = 40
ROW_TEXT_BYTES = 3000

GATEWAY = "gateway"
SLOT = "dashboard:1"
UNIT = "s-restore-fixture"
CREW_LOG_TURNS = 320
CREW_LOG_BODY_BYTES = 3000
CREW_LOG_MIN_BYTES = 2 * 1024 * 1024
BIG_KEY = "dashboard:chat-1-big"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()


# --------------------------------------------------------------------------- #
# transcript half
# --------------------------------------------------------------------------- #


def _write_transcript(path: Path, target_bytes: int) -> int:
    """A transcript of at least *target_bytes*, in the row shape ``append`` writes."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    head = {"_type": "metadata", "created_at": start.isoformat(), "last_consolidated": 0}
    lines = [json.dumps(head)]
    size = len(lines[0]) + 1
    rows = 0
    while size < target_bytes:
        role = "user" if rows % 2 == 0 else "assistant"
        row = {
            "role": role,
            "content": f"row {rows} " + "lorem ipsum " * (ROW_TEXT_BYTES // 12),
            "ts": (start + timedelta(seconds=rows)).isoformat(),
        }
        line = json.dumps(row)
        lines.append(line)
        size += len(line) + 1
        rows += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


@pytest.fixture
def large_home(tmp_path) -> tuple[ConversationLog, int]:
    """A sessions dir with one transcript at the threshold and 40 small tabs."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    log = ConversationLog(sessions)
    key = chat_persistence.slot_transcript_key(BIG_KEY.removeprefix("dashboard:"))
    rows = _write_transcript(log._path(key), THRESHOLD_BYTES)
    for i in range(SMALL_TABS):
        small = chat_persistence.slot_transcript_key(f"chat-{i + 2}-small")
        log.append(small, "user", f"hello {i}")
        log.append(small, "assistant", f"hi {i}")
    assert log._path(key).stat().st_size >= THRESHOLD_BYTES
    return ConversationLog(sessions), rows


@pytest.fixture
def parsed_rows(monkeypatch) -> dict[str, int]:
    """Rows the full and the tail transcript readers parsed, counted at the reader."""
    counts = {"full": 0, "tail": 0}
    real_full = ConversationLog._read_messages
    real_tail = ConversationLog._read_tail_messages

    def full(self, *args, **kwargs):
        out = real_full(self, *args, **kwargs)
        counts["full"] += len(out)
        return out

    def tail(self, *args, **kwargs):
        out = real_tail(self, *args, **kwargs)
        counts["tail"] += len(out)
        return out

    monkeypatch.setattr(ConversationLog, "_read_messages", full)
    monkeypatch.setattr(ConversationLog, "_read_tail_messages", tail)
    return counts


def _prefetch(log: ConversationLog, *, lazy: bool) -> None:
    key = chat_persistence.slot_transcript_key(BIG_KEY.removeprefix("dashboard:"))
    chat_persistence._prefetch_open_slot_inputs(log, key, kiro_model_map={}, lazy=lazy)


def test_fixture_is_at_the_threshold(large_home):
    log, rows = large_home
    key = chat_persistence.slot_transcript_key(BIG_KEY.removeprefix("dashboard:"))
    assert log._path(key).stat().st_size >= THRESHOLD_BYTES
    assert rows > TAIL_WINDOW_ROWS * 4


def test_a_sidebar_tab_reads_only_the_lazy_tail_at_the_threshold(large_home, parsed_rows):
    """The lazy (non-eager) restore of the large tab never parses the whole transcript."""
    log, _ = large_home
    _prefetch(log, lazy=True)
    assert parsed_rows["full"] == 0
    assert 0 < parsed_rows["tail"] <= restore_drivers.LAZY_TAIL_ROWS


def test_an_eager_tab_parses_the_whole_transcript_once(large_home, parsed_rows):
    """CEILING: an eager tab at the threshold reads its rows in full, once.

    A tail window is the target; short of it, a second full read of the same transcript in
    one restore is a regression.
    """
    log, rows = large_home
    _prefetch(log, lazy=False)
    assert parsed_rows["full"] <= rows


@pytest.mark.xfail(
    strict=True,
    reason="target: an eager tab above the threshold reads a tail window, not every row",
)
def test_an_eager_tab_reads_only_a_tail_window_target(large_home, parsed_rows):
    log, _ = large_home
    _prefetch(log, lazy=False)
    assert parsed_rows["full"] + parsed_rows["tail"] <= TAIL_WINDOW_ROWS


# --------------------------------------------------------------------------- #
# crew-log half
# --------------------------------------------------------------------------- #


def _write_unit() -> tuple[CrewLog, int]:
    """One slot unit through the real writer: the per-turn event mix, scaled."""
    handle = CrewLog.create(lg.KIND_SESSION, UNIT, owner="raymond", agent="kirocrew", slot=SLOT)
    body = "lorem ipsum " * (CREW_LOG_BODY_BYTES // 12)
    entries = 0
    for first in range(1, CREW_LOG_TURNS + 1, 20):
        items = []
        for turn in range(first, min(first + 20, CREW_LOG_TURNS + 1)):
            items += [
                {"type": "turn/started", "data": {"turn": turn, "actor": "user", "depth": 0}},
                {
                    "type": "message/received",
                    "data": {"turn": turn, "role": "user", "source": "dashboard", "text": body},
                },
                {"type": "step/started", "data": {"turn": turn, "step": 1}},
                {
                    "type": "message/sent",
                    "data": {"turn": turn, "step": 1, "text": body},
                },
                {"type": "step/completed", "data": {"turn": turn, "step": 1, "ms": 120}},
                {
                    "type": "turn/completed",
                    "data": {
                        "turn": turn,
                        "stop_reason": "end_turn",
                        "depth": 0,
                        "duration_ms": 900,
                        "model": "opus",
                        "provider": "kiro",
                        "credits": 0.5,
                        "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
                    },
                },
            ]
        handle.append_many(items, src=GATEWAY)
        entries += len(items)
    return handle, entries


@pytest.fixture
def walks(monkeypatch) -> dict[str, int]:
    counts = {"walks": 0}
    real = CrewLog._walk_from

    def counting(self, *args, **kwargs):
        counts["walks"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "_walk_from", counting)
    return counts


def _cold_prime_every_slot_fold() -> None:
    for name in _SLOT_FOLD_ROW_BYTES:
        crew_log.fold_slot_warm(name, (UNIT,), slot=SLOT)


def test_unit_fixture_size():
    handle, entries = _write_unit()
    size = sum(
        p.stat().st_size for p in Path(os.fspath(handle.path)).parent.rglob("*") if p.is_file()
    )
    assert size >= CREW_LOG_MIN_BYTES
    assert entries == CREW_LOG_TURNS * 6


def test_a_cold_prime_walks_the_unit_once_per_fold(walks):
    """CEILING: one walk per slot fold. More than that is a regression."""
    _write_unit()
    folds = len(_SLOT_FOLD_ROW_BYTES)
    assert folds >= 10
    walks["walks"] = 0
    _cold_prime_every_slot_fold()
    assert 1 <= walks["walks"] <= folds


@pytest.mark.xfail(
    strict=True,
    reason="target: one walk of the unit feeds every fold of the slot",
)
def test_a_cold_prime_walks_the_unit_once_target(walks):
    _write_unit()
    walks["walks"] = 0
    _cold_prime_every_slot_fold()
    assert walks["walks"] == 1
