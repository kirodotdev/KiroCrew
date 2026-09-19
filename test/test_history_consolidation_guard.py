"""Consolidation-boundary behavior across transcript rotation."""

from __future__ import annotations

import pytest

from kiro_crew import history as history_mod
from kiro_crew import history_rewrite
from kiro_crew.history import ConversationLog

KEY = "dashboard:rotation-fence"


def _append_rows(log: ConversationLog, count: int, *, prefix: str) -> None:
    with history_mod.allow_on_loop_persist():
        for index in range(count):
            log.append(KEY, "user", f"{prefix}{index}")


def _rotate_to_keep(
    log: ConversationLog,
    keep_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Force one real rotation that retains exactly *keep_count* message rows."""
    path = log._path(KEY)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    metadata_line = lines[0]
    message_lines = lines[1:]
    assert len(message_lines) > keep_count
    max_bytes = len(metadata_line.encode("utf-8")) + sum(
        len(line.encode("utf-8")) for line in message_lines[-keep_count:]
    )
    assert path.stat().st_size > max_bytes
    monkeypatch.setattr(history_rewrite, "_facade_session_keep_lines", lambda: keep_count)
    monkeypatch.setattr(history_rewrite, "_facade_session_max_bytes", lambda: max_bytes)
    with history_mod.allow_on_loop_persist(), log._locked(KEY):
        log._maybe_rotate(path, KEY)
    assert len(log.read_messages(KEY)) == keep_count
    return len(message_lines) - keep_count


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(5, 3, id="subtract-dropped-rows"),
        pytest.param(2, 0, id="floor-at-zero"),
        pytest.param(6, 4, id="fully-fenced-tail-stays-fenced"),
    ],
)
def test_rotation_rebases_the_consolidation_offset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    expected: int,
) -> None:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    _append_rows(log, 6, prefix="row-")
    log.mark_consolidated(KEY, offset)

    dropped = _rotate_to_keep(log, 4, monkeypatch)

    assert dropped == 2
    assert log.get_metadata(KEY)["last_consolidated"] == expected


def test_generation_mismatch_keeps_the_rotations_rebased_offset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    _append_rows(log, 6, prefix="row-")
    log.mark_consolidated(KEY, 5)
    _messages, total_at_snapshot, generation_at_snapshot = log.snapshot_for_consolidation(KEY)

    _rotate_to_keep(log, 4, monkeypatch)
    assert log.get_metadata(KEY)["last_consolidated"] == 3

    log.mark_consolidated(KEY, total_at_snapshot, generation_at_snapshot)

    assert log.get_metadata(KEY)["last_consolidated"] == 3


def test_oversized_stale_offset_keeps_the_rotations_rebased_offset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    _append_rows(log, 6, prefix="row-")
    log.mark_consolidated(KEY, 5)

    _rotate_to_keep(log, 4, monkeypatch)
    assert log.get_metadata(KEY)["last_consolidated"] == 3

    log.mark_consolidated(KEY, 6, generation=None)

    assert log.get_metadata(KEY)["last_consolidated"] == 3
