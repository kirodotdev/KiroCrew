"""The Files backend's containment check folds Windows' extended-length prefix.

On Windows ``Path.resolve()`` on a file another thread is replacing can come back
as ``\\\\?\\C:\\...`` while a directory resolved separately comes back plain, and a
containment check comparing the two read the lone prefix as an escape. The core
sites are pinned in ``test/test_windows_extended_prefix_containment.py``; this is
the same check for ``server._is_within``. ``strip_extended_length_prefix`` folds
the prefix on every platform, so the case runs anywhere.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.file_explorer import server

_PREFIX = "\\\\?\\"


def _prefixed(path: Path | str) -> str:
    return _PREFIX + str(path)


def test_is_within_folds_the_prefix_off_both_operands(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "f.txt").write_text("x", encoding="utf-8")

    assert server._is_within(Path(_prefixed(inside)), root)
    assert server._is_within(inside, Path(_prefixed(root)))
    assert not server._is_within(Path(_prefixed(outside / "f.txt")), root)
