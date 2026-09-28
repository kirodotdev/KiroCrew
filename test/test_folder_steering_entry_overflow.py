"""The entry-ceiling notice counts what the ceiling discarded, by kind.

A root holding more entries than ``_MAX_FOLDER_STEERING_ENTRIES`` ends the walk
(fail closed). The notice must then say what was left out in the terms the
walk actually saw: the Markdown files it had listed but will never read are
reported as FILES with their count, and the directories (the one the ceiling
fired in, its listed-but-unentered subdirectories, and every queued sibling)
as directories with theirs -- never a placeholder ``1 directory`` for
thousands of unread files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import folder_steering
from kiro_crew.folder_steering import (
    SteeringOmission,
    collect_folder_steering,
    render_folder_steering,
)

_needs_pinned_walk = pytest.mark.skipif(
    not folder_steering.pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-relative directory opens are unavailable on this host",
)


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _by_kind(omissions: list[SteeringOmission]) -> dict[str, int]:
    return {o.kind: o.count for o in omissions}


@_needs_pinned_walk
def test_a_root_of_only_markdown_past_the_ceiling_reports_the_files_it_discarded(
    tmp_path, monkeypatch
):
    """The finding's shape: N+1 Markdown files, no subdirectories."""
    ceiling = 12
    monkeypatch.setattr(folder_steering, "_MAX_FOLDER_STEERING_ENTRIES", ceiling)
    root = tmp_path / "standards"
    root.mkdir()
    for i in range(ceiling + 30):
        (root / f"rule_{i:03d}.md").write_text("always", encoding="utf-8")
    result = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert result.documents == [], "the directory could not be listed within budget"
    kinds = _by_kind(result.omissions)
    # Exactly the ceiling's worth of files were listed before it fired; all of
    # them are discarded unread and reported AS files.
    assert kinds["files"] == ceiling
    # The root itself is the one directory the walk never finished listing.
    assert kinds["entries"] == 1
    rendered = render_folder_steering(result)
    assert f"{ceiling} Markdown file(s) under" in rendered
    assert "were listed but not read" in rendered
    assert "1 directory(ies) under" in rendered


@_needs_pinned_walk
def test_subdirectories_listed_before_the_ceiling_are_counted_as_unlisted(tmp_path, monkeypatch):
    ceiling = 8
    monkeypatch.setattr(folder_steering, "_MAX_FOLDER_STEERING_ENTRIES", ceiling)
    root = tmp_path / "standards"
    for i in range(ceiling + 5):
        (root / f"team_{i:02d}").mkdir(parents=True)
    result = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert result.documents == []
    kinds = _by_kind(result.omissions)
    assert "files" not in kinds, "no Markdown was listed, so no files notice"
    # The root plus every subdirectory listed before the ceiling, none entered.
    assert kinds["entries"] == 1 + ceiling


@_needs_pinned_walk
def test_files_notice_names_the_entry_ceiling_not_the_document_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_steering, "_MAX_FOLDER_STEERING_ENTRIES", 5)
    root = tmp_path / "standards"
    root.mkdir()
    for i in range(20):
        (root / f"r{i}.md").write_text("always", encoding="utf-8")
    rendered = render_folder_steering(
        collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    )
    assert "5-entry ceiling" in rendered
    assert "document ceiling" not in rendered
