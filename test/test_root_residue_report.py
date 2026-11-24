"""The repository root residue report says what a new entry holds and when it was written.

The entry's own name (``tmp``) names no writer; the paths inside it and their write times,
read against the CI log's UTC progress lines, are what point at the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import conftest_floor_tmp as floor

#: A fixed session start, so the offsets below are exact.
_STARTED = 1_800_000_000.0


def _write(path: Path, data: bytes, at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (at, at))


def test_a_directory_entry_lists_its_paths_with_kind_size_and_write_time(tmp_path: Path) -> None:
    _write(tmp_path / "tmp" / "pytest-of-root" / "x.json", b"{}", _STARTED + 12.5)
    os.utime(tmp_path / "tmp" / "pytest-of-root", (_STARTED + 12.0, _STARTED + 12.0))
    os.utime(tmp_path / "tmp", (_STARTED + 3.0, _STARTED + 3.0))

    lines = floor._root_residue_lines(tmp_path, "tmp", _STARTED)

    assert lines == [
        "    tmp  (dir, written 08:00:03Z, +3.0 s after session start)",
        "        tmp/pytest-of-root  (dir, written 08:00:12Z, +12.0 s after session start)",
        "        tmp/pytest-of-root/x.json  (file, 2 B, written 08:00:12Z, +12.5 s after session start)",
    ]


def test_the_listing_is_cut_at_its_cap(tmp_path: Path) -> None:
    for index in range(floor._ROOT_RESIDUE_SHOWN + 5):
        _write(tmp_path / "tmp" / f"f{index:03d}", b"", _STARTED)

    lines = floor._root_residue_lines(tmp_path, "tmp", _STARTED)

    assert len(lines) == 1 + floor._ROOT_RESIDUE_SHOWN + 1
    assert lines[-1] == f"        ... more under tmp, listing cut at {floor._ROOT_RESIDUE_SHOWN}"


def test_the_walk_stops_at_its_depth(tmp_path: Path) -> None:
    deep = tmp_path / "tmp"
    for level in range(floor._ROOT_RESIDUE_DEPTH + 2):
        deep = deep / f"d{level}"
    deep.mkdir(parents=True)

    listed = [
        line.split("  (")[0].strip()
        for line in floor._root_residue_lines(tmp_path, "tmp", None)[1:]
    ]

    assert max(len(Path(rel).parts) for rel in listed) == 1 + floor._ROOT_RESIDUE_DEPTH


def test_a_file_entry_is_one_line_and_an_empty_dir_says_so(tmp_path: Path) -> None:
    _write(tmp_path / "tmp", b"abc", _STARTED - 1.0)
    (tmp_path / "empty").mkdir()

    assert floor._root_residue_lines(tmp_path, "tmp", _STARTED) == [
        "    tmp  (file, 3 B, written 07:59:59Z, -1.0 s after session start)"
    ]
    assert floor._root_residue_lines(tmp_path, "empty", None)[1:] == ["        (empty)"]
