"""PPTX Maker's path containment folds Windows' extended-length prefix.

On Windows ``Path.resolve()`` on a file another thread is replacing can come back
as ``\\\\?\\C:\\...`` while a directory resolved separately comes back plain, and a
containment check comparing the two read the lone prefix as an escape. The core
sites are pinned in ``test/test_windows_extended_prefix_containment.py``; this is
the same check for ``paths._contained``. ``strip_extended_length_prefix`` folds the
prefix on every platform, so the case runs anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import paths

_PREFIX = "\\\\?\\"


def test_contained_folds_the_prefix_off_a_resolved_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "f.txt").write_text("x", encoding="utf-8")

    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        out = real_resolve(self, strict=strict)
        return Path(_PREFIX + str(out)) if out == inside else out

    monkeypatch.setattr(Path, "resolve", resolve)

    assert paths._contained(inside, root) is not None
    assert paths._contained(outside / "f.txt", root) is None
