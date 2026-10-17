"""A dashboard chat's transcript keeps file-change copies for its newest turns only.

Every editing turn stores whole before/after copies of each file it wrote in
``meta.file_changes``. Each turn is bounded, but nothing bounded how many turns
kept them, so a long-lived chat's transcript grew by every edited file, twice, on
every editing turn, for as long as the chat lived.

The bound lives where a save already rebuilds the file: when it reads the frozen
prefix (the rows older than the in-memory window) from disk, rows older than the
chat's newest ``session.file_changes_keep_turns`` file-change rows lose their
copies and keep their file list. The originals go to the session archive first.
These tests drive the real save, ``_save_slot_to_history``, against a real
transcript.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import history as history_module
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, _save_slot_to_history
from kiro_crew.dashboard.slot_persistence import transcript_merge
from kiro_crew.dashboard.slot_persistence.transcript_merge import (
    FILE_CHANGES_ARCHIVE_REASON,
    FILE_CHANGES_OLDER_TURN,
    compact_older_file_change_copies,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

KEY = "dashboard_chatfc"


def _make_state(tmp_path: Path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _ts(n: int) -> str:
    return f"2026-10-11T10:{n // 60:02d}:{n % 60:02d}.000000"


def _card(i: int) -> dict[str, Any]:
    """An assistant row whose turn changed two files, as ``_flush_file_changes`` writes it."""
    return {
        "role": "assistant",
        "content": f"edited, turn {i}",
        "ts": _ts(2 * i + 1),
        "meta": {
            "mid": f"mid-{i}",
            "file_changes": [
                {"path": f"/repo/a{i}.py", "before": f"old a {i}\n" * 50, "after": f"new a {i}\n"},
                {
                    "path": f"/repo/b{i}.py",
                    "before": "",
                    "after": f"created b {i}\n",
                    "truncated": True,
                    "snapshot_limit_chars": 200000,
                },
            ],
        },
    }


def _plain(i: int) -> dict[str, Any]:
    return {
        "role": "user",
        "content": f'ask {i}: the "file_changes": [{{"before": "not a key"}}] text',
        "ts": _ts(2 * i),
        "meta": {"mid": f"user-{i}"},
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / f"{KEY}.jsonl"
    meta = {"_type": "metadata", "created_at": "2026-10-11T09:00:00", "last_consolidated": 0}
    path.write_text("".join(json.dumps(r) + "\n" for r in [meta, *rows]), encoding="utf-8")
    return path


def _rows(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines[1:] if ln.strip()]


def _archives(tmp_path: Path) -> list[tuple[dict, list[str]]]:
    out = []
    for p in sorted((tmp_path / "archive").glob("*.jsonl")):
        header, *lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        out.append((json.loads(header), lines))
    return out


def _changes(rows: list[dict]) -> list[object]:
    """Each row's file-change list, the one part of a row the bound may touch."""
    return [r.get("meta", {}).get("file_changes") for r in rows]


def _has_copies(row: dict) -> bool:
    return any(fc.get("before") or fc.get("after") for fc in row["meta"]["file_changes"])


@pytest.fixture
def keep(monkeypatch: pytest.MonkeyPatch):
    """Set the configured number of turns that keep their copies."""

    def _set(n: int) -> None:
        monkeypatch.setattr(transcript_merge, "_file_changes_keep_turns", lambda: n)

    return _set


def _loaded_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict], window: int
) -> tuple[DashboardState, Any, Path]:
    """A slot whose in-memory window is the last *window* rows; the rest is the prefix."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    path = _write(tmp_path, rows)
    state = _make_state(tmp_path)
    slot = _rehydrate_slot_from_history(state, KEY.removeprefix("dashboard_"))
    assert slot is not None
    older = len(rows) - window
    slot._disk_older_count = older
    slot._disk_window_len = window
    del slot.messages[:older]
    slot._frozen_prefix_cache = None
    slot._dirty = True
    return state, slot, path


def _history(n_cards: int) -> list[dict]:
    rows: list[dict] = []
    for i in range(n_cards):
        rows += [_plain(i), _card(i)]
    return rows


class TestTheSaveBoundsOlderTurns:
    def test_rows_older_than_the_newest_n_lose_their_copies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(5)
        rows = _history(12)  # 24 rows; the window holds the last 4 (2 cards)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=4)

        assert _save_slot_to_history(state, slot) is True

        written = _rows(path)
        cards = [r for r in written if r["role"] == "assistant"]
        # Newest 5 cards keep everything: 2 in the window, 3 in the prefix.
        assert [_has_copies(r) for r in cards] == [False] * 7 + [True] * 5
        for old in cards[:7]:
            for fc in old["meta"]["file_changes"]:
                assert (fc["before"], fc["after"]) == ("", "")
                assert fc["content_omitted"] is True and fc["truncated"] is True
                assert fc["omitted_reason"] == FILE_CHANGES_OLDER_TURN
                assert "snapshot_limit_chars" not in fc
        # The file list, the row text and every row's identity survive.
        assert [r["meta"]["mid"] for r in written] == [r["meta"]["mid"] for r in rows]
        assert [r["content"] for r in written] == [r["content"] for r in rows]
        assert [fc["path"] for fc in cards[0]["meta"]["file_changes"]] == [
            "/repo/a0.py",
            "/repo/b0.py",
        ]

    def test_recent_turns_keep_their_full_copies_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(5)
        rows = _history(12)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=4)

        _save_slot_to_history(state, slot)

        recent = [r for r in _rows(path) if r["role"] == "assistant"][-5:]
        assert [r["meta"]["file_changes"] for r in recent] == [
            c["meta"]["file_changes"] for c in rows if c["role"] == "assistant"
        ][-5:]

    def test_the_window_is_never_changed_even_past_n(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        """Rows in memory are what the open tab renders; the bound leaves them alone."""
        keep(1)
        rows = _history(6)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=6)  # 3 cards

        _save_slot_to_history(state, slot)

        cards = [r for r in _rows(path) if r["role"] == "assistant"]
        assert [_has_copies(r) for r in cards] == [False, False, False, True, True, True]
        assert all(_has_copies(m) for m in slot.messages if m.get("role") == "assistant")

    def test_the_originals_reach_the_archive_before_the_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(2)
        rows = _history(5)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)
        before = path.read_text(encoding="utf-8").splitlines(keepends=True)[1:]

        _save_slot_to_history(state, slot)

        [(header, lines)] = _archives(tmp_path)
        assert header["reason"] == FILE_CHANGES_ARCHIVE_REASON
        assert header["count"] == 3
        changed = [ln for ln in before if json.loads(ln)["role"] == "assistant"][:3]
        assert lines == changed

    def test_a_second_save_neither_rewrites_nor_re_archives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(2)
        rows = _history(5)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)
        _save_slot_to_history(state, slot)
        first = path.read_text(encoding="utf-8")

        slot._dirty = True
        _save_slot_to_history(state, slot)  # served from the prefix cache
        slot._frozen_prefix_cache = None
        slot._dirty = True
        _save_slot_to_history(state, slot)  # re-read from disk: nothing left to drop

        assert path.read_text(encoding="utf-8") == first
        assert len(_archives(tmp_path)) == 1

    def test_zero_keeps_every_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(0)
        rows = _history(6)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        _save_slot_to_history(state, slot)

        assert _changes(_rows(path)) == _changes(rows)
        assert _archives(tmp_path) == []

    def test_a_failed_archive_keeps_the_rows_whole_and_retries_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(1)
        rows = _history(4)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        def refuse(*_a: Any, **_k: Any) -> None:
            raise OSError("archive directory is full")

        with monkeypatch.context() as scoped:
            scoped.setattr(transcript_merge, "_archive_lines", refuse)
            assert _save_slot_to_history(state, slot) is True
        assert _changes(_rows(path)) == _changes(rows)

        slot._frozen_prefix_cache = None
        slot._dirty = True
        _save_slot_to_history(state, slot)
        cards = [r for r in _rows(path) if r["role"] == "assistant"]
        assert [_has_copies(r) for r in cards] == [False, False, False, True]
        assert len(_archives(tmp_path)) == 1

    def test_a_rewrite_save_leaves_the_prefix_to_the_next_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        """A rewrite archives every old line it drops, so it must not also archive these."""
        keep(1)
        rows = _history(4)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        _save_slot_to_history(state, slot, rewrite=True)

        assert [_has_copies(r) for r in _rows(path) if r["role"] == "assistant"] == [True] * 4
        assert not any(h["reason"] == FILE_CHANGES_ARCHIVE_REASON for h, _ in _archives(tmp_path))


class TestCompactOlderFileChangeCopies:
    def _lines(self, rows: list[dict]) -> list[str]:
        return [json.dumps(r) + "\n" for r in rows]

    def test_counts_only_real_file_change_rows(self) -> None:
        """A row whose TEXT mentions the key is not a card and does not use up the budget."""
        lines = self._lines(_history(3))
        out, originals = compact_older_file_change_copies(lines, keep_rows=1)

        assert [json.loads(ln) for ln in out[0::2]] == [json.loads(ln) for ln in lines[0::2]]
        assert [_has_copies(json.loads(ln)) for ln in out[1::2]] == [False, False, True]
        assert originals == [lines[1], lines[3]]

    def test_a_row_the_turn_budget_already_emptied_is_left_alone(self) -> None:
        row = _card(0)
        for fc in row["meta"]["file_changes"]:
            fc.update(before="", after="", content_omitted=True, turn_budget_chars=400000)
        out, originals = compact_older_file_change_copies(self._lines([row]), keep_rows=0)

        assert originals == []
        assert json.loads(out[0]) == row

    def test_an_unreadable_line_is_kept_as_it_is(self) -> None:
        torn = json.dumps(_card(0))[:-40] + "\n"
        out, originals = compact_older_file_change_copies([torn], keep_rows=0)

        assert (out, originals) == ([torn], [])

    def test_a_last_line_without_a_newline_keeps_that_shape(self) -> None:
        line = json.dumps(_card(0))
        out, originals = compact_older_file_change_copies([line], keep_rows=0)

        assert not out[0].endswith("\n")
        assert originals == [line + "\n"]

    @pytest.mark.parametrize("seed", range(300))
    def test_the_bound_holds_for_any_history(self, seed: int) -> None:
        """Whatever the mix, exactly the newest *keep* cards keep copies and nothing else moves."""

        rng = random.Random(seed)
        rows = [_card(i) if rng.random() < 0.4 else _plain(i) for i in range(rng.randint(0, 40))]
        keep_rows = rng.randint(0, 12)
        lines = self._lines(rows)

        out, originals = compact_older_file_change_copies(lines, keep_rows=keep_rows)

        card_at = [i for i, r in enumerate(rows) if r["role"] == "assistant"]
        kept = set(card_at[-keep_rows:]) if keep_rows else set()
        assert len(out) == len(lines)
        for i, (row, line) in enumerate(zip(rows, out)):
            if i in kept or row["role"] != "assistant":
                assert line == lines[i]
            else:
                assert not _has_copies(json.loads(line))
                assert [fc["path"] for fc in json.loads(line)["meta"]["file_changes"]] == [
                    fc["path"] for fc in row["meta"]["file_changes"]
                ]
        assert originals == [lines[i] for i in card_at if i not in kept]


def _config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: dict) -> None:
    """Point the real config loader at a file holding *session* as its session section."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"session": session}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)


class TestTheSetting:
    """The bound ships opt-in: unset, every turn keeps its copies, as before it existed."""

    def test_defaults_to_zero_which_keeps_every_turn(self) -> None:
        assert KiroCrewConfig().session.file_changes_keep_turns == 0

    def test_the_default_keeps_every_turn_whole_through_the_real_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No setting, a chat far past any count: the save changes no row and archives nothing."""
        _config_file(tmp_path, monkeypatch, {})
        rows = _history(120)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        _save_slot_to_history(state, slot)

        assert _changes(_rows(path)) == _changes(rows)
        assert _archives(tmp_path) == []

    def test_a_configured_count_trims_and_archives_through_the_real_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_file(tmp_path, monkeypatch, {"file_changes_keep_turns": 2})
        rows = _history(5)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        _save_slot_to_history(state, slot)

        cards = [r for r in _rows(path) if r["role"] == "assistant"]
        assert [_has_copies(r) for r in cards] == [False, False, False, True, True]
        ((header, archived),) = _archives(tmp_path)
        assert header["reason"] == FILE_CHANGES_ARCHIVE_REASON
        older_cards = [r for r in rows if r["role"] == "assistant"][:3]
        assert [json.loads(ln) for ln in archived] == older_cards

    @pytest.mark.parametrize(
        ("stored", "loaded"),
        [(0, 0), (7, 7), (-3, 0), (100_001, 100_001), (10**9, 10**9), ("lots", 0)],
    )
    def test_the_save_reads_the_loaded_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: object, loaded: int
    ) -> None:
        """Clamped only at 0: a large count is a real choice, not a typo to cut down."""
        _config_file(tmp_path, monkeypatch, {"file_changes_keep_turns": stored})

        assert transcript_merge._file_changes_keep_turns() == loaded


def test_the_card_reads_the_reason_this_module_writes() -> None:
    """The card tells an older turn from a budget-demoted one by this exact value."""
    card = Path(__file__).resolve().parents[1] / "website/src/components/FileChangeChips.tsx"
    assert f"const OLDER_TURN = '{FILE_CHANGES_OLDER_TURN}'" in card.read_text(encoding="utf-8")


class TestTheArchiveIsDurableBeforeTheReplace:
    """Once the replace lands the archive holds the only copy, so it is synced first."""

    def test_the_archive_file_and_directory_sync_before_the_transcript_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        keep(1)
        rows = _history(6)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)
        events: list[str] = []
        archive_dir = tmp_path / "archive"

        class _RecordingOs:
            """``history``'s ``os``, recording each file fsync it is asked for."""

            def __getattr__(self, name: str) -> Any:
                return getattr(os, name)

            @staticmethod
            def fsync(fd: int) -> None:
                events.append("file-fsync")
                os.fsync(fd)

        real_fsync_dir = history_module.fsync_dir
        real_atomic_write = chat_persistence.atomic_write

        def recording_fsync_dir(directory: Any, **kwargs: Any) -> None:
            events.append(f"dir-fsync:{Path(directory).name}")
            real_fsync_dir(directory, **kwargs)

        def recording_atomic_write(*args: Any, **kwargs: Any) -> Any:
            events.append("replace")
            return real_atomic_write(*args, **kwargs)

        monkeypatch.setattr(history_module, "os", _RecordingOs())
        monkeypatch.setattr(history_module, "fsync_dir", recording_fsync_dir)
        monkeypatch.setattr(chat_persistence, "atomic_write", recording_atomic_write)

        assert _save_slot_to_history(state, slot) is True

        assert len(_archives(tmp_path)) == 1
        # The first archive also created its directory, so the parent is synced too.
        assert events == [
            "file-fsync",
            f"dir-fsync:{archive_dir.name}",
            f"dir-fsync:{tmp_path.name}",
            "replace",
        ]

    def test_a_failed_archive_sync_keeps_the_rows_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep
    ) -> None:
        """A sync that raises is a failed archive: the save keeps every copy."""
        keep(1)
        rows = _history(4)
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)

        def refuse(*_a: Any, **_k: Any) -> None:
            raise OSError(5, "device did not take the write")

        monkeypatch.setattr(history_module, "fsync_dir", refuse)

        assert _save_slot_to_history(state, slot) is True
        assert _changes(_rows(path)) == _changes(rows)
