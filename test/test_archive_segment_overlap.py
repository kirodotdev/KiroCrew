"""Two rotation segments can overlap, and the corpus must serve each row once.

`_maybe_rotate` writes the archive segment BEFORE rewriting the live file, and
that order is deliberate: until the archive lands, the live file is the only copy
of those rows. A crash between the two steps therefore leaves the archived prefix
in the live file as well, and the next rotation computes its own `dropped` from a
live file that still begins with those rows -- archiving them a second time as the
following segment.

`read_rotated_messages` concatenates segments, so without a cross-segment guard
those rows are served twice. That is not cosmetic: this corpus is the index space
`before`/`next_before` cursors and the fork index path resolve a rendered row's
position against, so one duplicated row shifts every index above it, silently. A
reader pages positions that no longer mean what they meant, and an
index-addressed fork copies a different cutoff than the one on screen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kiro_crew import history as history_mod
from kiro_crew.history_projection import TranscriptReadProjection


class _Log:
    def __init__(self, d: Path) -> None:
        self._dir = Path(d)


def _row(n: str, mid: str | None = None) -> dict:
    r: dict = {"role": "user", "content": n, "ts": f"2026-01-01T00:00:{n.zfill(2)}Z"}
    if mid:
        r["meta"] = {"mid": mid}
    return r


def _segment(adir: Path, stem: str, stamp: str, rows: list[dict]) -> Path:
    p = adir / f"{stem}{stamp}.jsonl"
    lines = [json.dumps({"reason": "rotate"})]
    lines += [json.dumps(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _proj(tmp_path: Path, segments: list[list[dict]], key: str = "slot-a") -> Any:
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER
    for i, rows in enumerate(segments):
        _segment(adir, stem, f"20260101-0000{i:02d}", rows)
    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    return proj


def _read(proj: Any, key: str = "slot-a") -> list[str]:
    rows = TranscriptReadProjection.read_rotated_messages(proj, key)
    return [r["content"] for r in rows]


def test_non_overlapping_segments_are_all_served(tmp_path: Path) -> None:
    """The guard must not eat rows from a healthy archive."""
    proj = _proj(tmp_path, [[_row("1"), _row("2")], [_row("3"), _row("4")]])
    assert _read(proj) == ["1", "2", "3", "4"]


def test_a_wholly_duplicated_segment_is_served_once(tmp_path: Path) -> None:
    """The crash re-archived the entire prefix: segment 2 repeats segment 1."""
    seg = [_row("1"), _row("2")]
    proj = _proj(tmp_path, [list(seg), list(seg) + [_row("3")]])
    assert _read(proj) == ["1", "2", "3"]


def test_a_partially_duplicated_segment_is_served_once(tmp_path: Path) -> None:
    """Only the tail of segment 1 was re-archived at the head of segment 2."""
    proj = _proj(tmp_path, [[_row("1"), _row("2")], [_row("2"), _row("3")]])
    assert _read(proj) == ["1", "2", "3"]


def test_overlap_is_matched_by_stable_id_when_present(tmp_path: Path) -> None:
    """`meta.mid` is the identity that cannot collide, so it decides."""
    proj = _proj(
        tmp_path,
        [
            [_row("1", mid="m1"), _row("2", mid="m2")],
            [_row("2", mid="m2"), _row("3", mid="m3")],
        ],
    )
    assert _read(proj) == ["1", "2", "3"]


def test_distinct_rows_sharing_ts_and_role_are_both_kept(tmp_path: Path) -> None:
    """A match DELETES a row, so `(ts, role)` alone must never be enough.

    Two different messages can share a second and a speaker. If the identity rule
    dropped on that pair alone, this second row -- genuinely its own message --
    would vanish from the corpus and shift every index above it.
    """
    a = {"role": "user", "content": "first", "ts": "2026-01-01T00:00:01Z"}
    b = {"role": "user", "content": "second", "ts": "2026-01-01T00:00:01Z"}
    proj = _proj(tmp_path, [[a], [b]])
    assert _read(proj) == ["first", "second"]


def test_an_interior_repeat_is_not_treated_as_overlap(tmp_path: Path) -> None:
    """Both producers persist a PREFIX, never an interior slice.

    A row that reappears in the middle of the next segment is not a re-archived
    prefix -- it is the reader's own repeated message, and dropping it would
    delete history the session really contains.
    """
    proj = _proj(tmp_path, [[_row("1"), _row("2")], [_row("9"), _row("2")]])
    assert _read(proj) == ["1", "2", "9", "2"]
