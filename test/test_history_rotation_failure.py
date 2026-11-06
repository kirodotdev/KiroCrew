"""A rotation that fails after a durable append never fails that append.

``ConversationLog.append`` writes and syncs the row, then rotates an oversized
transcript. Rotation is housekeeping: when it raises, the row is on disk, the
caller writing a turn's rows still writes the rest, and a rewrite that did not
land leaves no archive copy behind to be written again on the next append.
Compaction (``rewrite_session``) archives its dropped rows the same way and
removes that archive the same way when its rewrite does not land.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import history as history_module
from kiro_crew import history_rewrite
from kiro_crew.history import ARCHIVE_DIR_NAME, ConversationLog
from kiro_crew.history_offload import append_rows_if_absent_off_loop

PAD = "padding " * 20
Q = f"{PAD} question"
A = f"{PAD} answer"


def _refuse(*_a: Any, **_k: Any) -> None:
    raise OSError(28, "No space left on device")


_real_rewrite = history_rewrite.rewrite_transcript


def _lands_then_fails(path: Path, content: Any) -> None:
    """Replace the file, then raise: a failure after the rename."""
    _real_rewrite(path, content)
    raise OSError(5, "failed after the rename")


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConversationLog:
    monkeypatch.setattr(history_module, "_SESSION_MAX_BYTES", 400)
    monkeypatch.setattr(history_module, "_SESSION_KEEP_LINES", 2)
    log = ConversationLog(base_dir=tmp_path)
    for i in range(5):
        log.append("k", "user", f"message number {i:02d} with enough text to rotate")
    return log


def _archives(log: ConversationLog) -> list[Path]:
    adir = Path(log._dir) / ARCHIVE_DIR_NAME
    return sorted(adir.glob("*.jsonl")) if adir.is_dir() else []


def _contents(log: ConversationLog) -> list[str]:
    return [m["content"] for m in log._read_messages("k")]


def test_a_failed_rewrite_does_not_drop_the_rest_of_the_turn(
    log: ConversationLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_rewrite, "rewrite_transcript", _refuse)
    archives_before = _archives(log)

    append_rows_if_absent_off_loop(log, "k", [("user", Q, "", None), ("assistant", A, "", None)])

    assert _contents(log)[-2:] == [Q, A]
    assert _archives(log) == archives_before


def test_a_lasting_rewrite_fault_leaves_no_archive_per_append(
    log: ConversationLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_rewrite, "rewrite_transcript", _refuse)
    archives_before = _archives(log)

    for i in range(3):
        log.append("k", "user", f"{PAD} later {i}")

    assert _archives(log) == archives_before
    assert _contents(log)[-3:] == [f"{PAD} later {i}" for i in range(3)]


def test_a_rewrite_that_landed_keeps_its_archive(
    log: ConversationLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite that replaced the file and then failed: the archive is the only copy."""
    monkeypatch.setattr(history_rewrite, "rewrite_transcript", _lands_then_fails)
    archives_before = _archives(log)

    log.append("k", "user", f"{PAD} one more")

    assert len(_archives(log)) == len(archives_before) + 1


@pytest.fixture
def small_log(tmp_path: Path) -> ConversationLog:
    """Three short rows: under the rotation budget, so only compaction archives."""
    log = ConversationLog(base_dir=tmp_path)
    for i in range(3):
        log.append("k", "user", f"message {i}")
    return log


def test_a_compaction_whose_rewrite_did_not_land_leaves_no_archive(
    small_log: ConversationLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = small_log._read_messages("k")[-1:]
    monkeypatch.setattr(history_rewrite, "rewrite_transcript", _refuse)

    for _ in range(3):
        with pytest.raises(OSError):
            small_log.rewrite_session("k", keep)

    assert _archives(small_log) == []
    assert _contents(small_log) == [f"message {i}" for i in range(3)]


def test_a_compaction_whose_rewrite_landed_keeps_its_archive(
    small_log: ConversationLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = small_log._read_messages("k")[-1:]
    monkeypatch.setattr(history_rewrite, "rewrite_transcript", _lands_then_fails)

    with pytest.raises(OSError):
        small_log.rewrite_session("k", keep)

    assert len(_archives(small_log)) == 1
    assert _contents(small_log) == ["message 2"]


def test_a_non_integer_rotation_generation_still_rotates(log: ConversationLog) -> None:
    path = log._path("k")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meta = json.loads(lines[0])
    meta["rotation_generation"] = "2.0"
    path.write_text(json.dumps(meta) + "\n" + "".join(lines[1:]), encoding="utf-8")

    log.append("k", "user", f"{PAD} crosses the budget again")

    rotated = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rotated["rotation_generation"] == 1
    assert rotated["last_consolidated"] == 0
