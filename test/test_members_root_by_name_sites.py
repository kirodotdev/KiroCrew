# SPDX-License-Identifier: Apache-2.0
"""Where ``members`` is a link, no site looks the folder up by name and then writes there.

``open_member_folder`` opens the folder a link at ``members`` names once and holds it.
Four sites outside it name ``members`` as a string. Each is either routed through
that held folder (it writes) or declared read-only (it writes nothing): the tests below
show the write sites keep to the held folder and the read-only sites leave the tree as it was.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import member_notes, members, pinned_fs
from kiro_crew.eventlog import service as eventlog_service

SLUG = "ada"
LEGACY_NAMES = ("activity.jsonl", "activity.jsonl.1")

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="descriptor open needs a pinned walk"
)
needs_briefing = pytest.mark.skipif(
    not members.member_briefing_supported(), reason="briefings need a no-follow pinned walk"
)


def _link_members(tmp_path: Path) -> tuple[Path, Path]:
    """Point ``members`` at a folder elsewhere; return ``(link, that folder)``."""
    outside = tmp_path / "outside"
    outside.mkdir()
    named = members.members_root()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, named)
    return named, outside


def _tree(root: Path) -> list[tuple[str, bytes, int]]:
    """Every name under *root* with its bytes and mtime: a change to any of them shows."""
    found = []
    for path in sorted(root.rglob("*")):
        data = path.read_bytes() if path.is_file() else b""
        found.append((str(path.relative_to(root)), data, path.stat().st_mtime_ns))
    return found


@requires_symlinks
@needs_pinned_walk
@needs_briefing
def test_the_notes_writer_does_not_resolve_the_members_link_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    named, outside = _link_members(tmp_path)
    slug = members.member_slug(SLUG)
    real_resolve = Path.resolve

    def spy_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named:
            raise AssertionError("the link at members was resolved by name")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    member_notes.write_member_notes(slug, "keep to the folder that was opened")
    written = outside / slug / members.BRIEFING_FILE_NAME
    assert written.read_text(encoding="utf-8") == "keep to the folder that was opened\n"


@requires_symlinks
@needs_pinned_walk
def test_the_legacy_activity_cleanup_removes_nothing_behind_a_linked_members(
    tmp_path: Path,
) -> None:
    """The cleanup unlinks, and it holds ``members`` first: a link there is refused."""
    _named, outside = _link_members(tmp_path)
    slug = members.member_slug(SLUG)
    (outside / slug).mkdir()
    for leaf in LEGACY_NAMES:
        (outside / slug / leaf).write_text("not the member's\n", encoding="utf-8")
    before = _tree(outside)
    assert eventlog_service._remove_legacy_activity(slug) is False
    assert _tree(outside) == before


@requires_symlinks
@needs_pinned_walk
def test_the_member_dir_path_helper_writes_nothing_under_a_linked_members(
    tmp_path: Path,
) -> None:
    """``member_dir`` only builds a path; a caller that writes opens ``open_member_folder``."""
    _named, outside = _link_members(tmp_path)
    slug = members.member_slug(SLUG)
    before = _tree(outside)
    assert members.member_dir(slug) == outside.resolve() / slug
    assert _tree(outside) == before


@requires_symlinks
@needs_pinned_walk
@needs_briefing
def test_the_briefing_read_writes_nothing_under_a_linked_members(tmp_path: Path) -> None:
    _named, outside = _link_members(tmp_path)
    slug = members.member_slug(SLUG)
    (outside / slug).mkdir()
    (outside / slug / members.BRIEFING_FILE_NAME).write_text("a briefing\n", encoding="utf-8")
    before = _tree(outside)
    text, _mtime, _truncated = members.read_member_briefing_bounded(slug)
    assert text.strip() == "a briefing"
    assert _tree(outside) == before
