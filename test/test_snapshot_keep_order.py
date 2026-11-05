"""``kirocrew snapshot --keep`` and ``--list`` order bundles by the stamp in their name.

A bundle's mtime is when it last landed on this disk: copying an older bundle
back in from off-host storage gives it a fresh one. Ordered by mtime, that older
bundle counts as the newest, and ``--keep 1`` deletes the bundle the run just
wrote. The stamp in the name is when the bundle was made.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, snapshot

_OLD = "kirocrew-snapshot-20200101T000000Z.tar.gz"


def _touch(path: Path, mtime: float) -> Path:
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))
    return path


def test_newest_first_orders_by_the_name_stamp(tmp_path: Path) -> None:
    from kiro_crew.snapshot_order import newest_first

    older = _touch(tmp_path / "kirocrew-snapshot-20240101T000000Z.tar.gz", 2_000_000_000)
    newer = _touch(tmp_path / "kirocrew-snapshot-20250101T000000Z.tar.gz", 1_000_000_000)
    assert newest_first([older, newer]) == [newer, older]


def test_a_name_without_a_stamp_falls_back_to_its_mtime(tmp_path: Path) -> None:
    from kiro_crew.snapshot_order import newest_first

    stamped = _touch(tmp_path / "kirocrew-snapshot-20240101T000000Z.tar.gz", 1_000_000_000)
    renamed = _touch(tmp_path / "kirocrew-snapshot-manual.tar.gz", 1_800_000_000)
    assert newest_first([stamped, renamed]) == [renamed, stamped]


def _snapshot(argv: list[str]) -> str:
    if not pinned_fs.supports_pinned_tree_walk():
        argv = [*argv, "--allow-unpinned-staging"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert snapshot.snapshot_main(argv) == 0
    return buf.getvalue()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "note.md").write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


@pytest.mark.usefixtures("home")
def test_keep_one_keeps_the_bundle_just_written_over_a_copied_in_older_one(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    # An old bundle copied back in: its mtime is later than anything this run writes.
    copied = _touch(out / _OLD, 4_000_000_000)

    _snapshot([str(out), "--keep", "1"])

    left = sorted(p.name for p in out.glob("kirocrew-snapshot-*.tar.gz"))
    assert len(left) == 1
    assert left != [copied.name]


@pytest.mark.usefixtures("home")
def test_keep_one_keeps_the_bundle_just_written_after_the_clock_went_back(
    tmp_path: Path,
) -> None:
    """A bundle stamped in the future by a run before a clock correction does not win."""
    out = tmp_path / "out"
    out.mkdir()
    future = _touch(out / "kirocrew-snapshot-29990101T000000Z.tar.gz", 1_000_000_000)

    _snapshot([str(out), "--keep", "1"])

    left = [p.name for p in out.glob("kirocrew-snapshot-*.tar.gz")]
    assert len(left) == 1
    assert left != [future.name]


def test_one_second_stamps_tie_break_on_mtime(tmp_path: Path) -> None:
    from kiro_crew.snapshot_order import newest_first

    first = _touch(tmp_path / "kirocrew-snapshot-20240101T000000Z.tar.gz", 1_000_000_000)
    second = _touch(tmp_path / "kirocrew-snapshot-20240101T000000Z-1.tar.gz", 1_000_000_005)
    assert newest_first([first, second]) == [second, first]


def test_an_impossible_stamp_falls_back_to_its_mtime(tmp_path: Path) -> None:
    from kiro_crew.snapshot_order import newest_first

    bad = _touch(tmp_path / "kirocrew-snapshot-20241399T000000Z.tar.gz", 1_900_000_000)
    good = _touch(tmp_path / "kirocrew-snapshot-20240101T000000Z.tar.gz", 1_000_000_000)
    assert newest_first([good, bad]) == [bad, good]


@pytest.mark.usefixtures("home")
def test_list_shows_the_newest_bundle_first(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    _touch(out / _OLD, 4_000_000_000)
    _snapshot([str(out), "--keep", "5"])

    listed = [line for line in _snapshot([str(out), "--list"]).splitlines() if line.strip()]
    assert Path(listed[-1]).name == _OLD
