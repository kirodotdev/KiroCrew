"""The plugin import is bounded in BREADTH as well as depth and per-file size.

``MAX_RESOURCE_BYTES`` bounds one file and ``MAX_SKILL_TREE_DEPTH`` bounds how
deep the walk goes. Neither bounds how MANY files a package may hand over, so a
plugin of individually legal files could copy without end -- the module's own
contract asks every loop over untrusted input for a ceiling, and this loop had
none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps import plugin_import


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src


def _files(root, n, size=1):
    for i in range(n):
        (root / f"f{i}.txt").write_text("x" * size, encoding="utf-8")


def test_the_item_ceiling_stops_the_copy_and_records_why(tree, tmp_path, monkeypatch):
    """The ceiling counts ITEMS: the destination directory is one of them."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 5)
    _files(tree, 12)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 4
    assert len(list((tmp_path / "out").iterdir())) == 4
    assert any("import budget spent" in s for s in skipped)


def test_empty_directories_are_charged_too(tree, tmp_path, monkeypatch):
    """Breadth of EMPTY directories is inodes spent, so it cannot be free."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 4)
    for i in range(20):
        (tree / f"d{i}").mkdir()
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 0
    made = sum(1 for p in (tmp_path / "out").rglob("*") if p.is_dir())
    assert made == 3, "root + 3 charged children, then the budget is spent"
    assert any("directory skipped" in s for s in skipped)


def test_the_byte_ceiling_stops_the_copy_too(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_BYTES", 30)
    _files(tree, 10, size=10)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 3
    assert any("import budget spent" in s for s in skipped)


def test_the_budget_is_shared_across_subdirectories(tree, tmp_path, monkeypatch):
    """Breadth is the unbounded axis, so a per-directory counter fixes nothing."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 6)
    for d in ("a", "b", "c"):
        sub = tree / d
        sub.mkdir()
        _files(sub, 3)
    copied, _ = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    # root dir + subdir a + its 3 files + subdir b = 6 items, so 3 files land.
    assert copied == 3


def test_a_package_inside_the_ceilings_copies_whole(tree, tmp_path):
    _files(tree, 7)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 7
    assert not [s for s in skipped if "budget" in s]


def test_one_budget_spans_every_skill_of_one_conversion(tmp_path, monkeypatch):
    """``_convert_skills`` walks up to MAX_SKILLS trees: the cap must hold across them."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 6)
    root = tmp_path / "pkg"
    skills = root / plugin_import.DEFAULT_SKILLS_DIR
    for name in ("alpha", "beta", "gamma"):
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# skill", encoding="utf-8")
        _files(d, 4)

    report = plugin_import.ImportReport(
        source_root=str(root), manifest_path="", source_format="test", app_name="pkg"
    )
    out = tmp_path / "out"
    emitted = plugin_import._convert_skills(root, None, out, report)

    assert emitted, "the skills were still discovered"
    copied = sum(1 for p in out.rglob("*") if p.is_file())
    assert copied == 5, "one item of the six is the skill's own directory"
    assert any("import budget spent after" in w for w in report.warnings)


def test_the_spent_budget_is_reported_once_at_the_top_level(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 2)
    root = tmp_path / "pkg"
    d = root / plugin_import.DEFAULT_SKILLS_DIR / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# skill", encoding="utf-8")
    _files(d, 6)

    report = plugin_import.ImportReport(
        source_root=str(root), manifest_path="", source_format="test", app_name="pkg"
    )
    plugin_import._convert_skills(root, None, tmp_path / "out", report)
    summaries = [w for w in report.warnings if "import budget spent after" in w]
    assert len(summaries) == 1


def test_a_file_swapped_for_a_symlink_mid_walk_is_not_followed(tmp_path, monkeypatch):
    """The TOCTOU the no-follow copy closes: judged and copied must be one object.

    The swap is performed between the walk's symlink check and the copy by
    patching the check itself to do it -- the only way to hit that window
    deterministically rather than by racing a real agent.
    """
    src = tmp_path / "src"
    src.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET", encoding="utf-8")
    victim = src / "f.txt"
    victim.write_text("fine", encoding="utf-8")

    real_is_symlink = Path.is_symlink

    def _swap(self):
        answer = real_is_symlink(self)
        if self == victim and self.exists() and not answer:
            self.unlink()
            self.symlink_to(secret)
        return answer

    monkeypatch.setattr(Path, "is_symlink", _swap)
    copied, skipped = plugin_import._copy_tree_without_symlinks(src, tmp_path / "out")
    monkeypatch.undo()

    assert copied == 0
    assert not (tmp_path / "out" / "f.txt").exists()
    assert skipped, "the refusal is recorded rather than silent"
