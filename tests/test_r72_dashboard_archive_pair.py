"""Regression guard for separately supplied Crew 0.12 source + Dashboard ZIPs."""
from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile, ZipInfo
import pytest

from scripts.verify_crew12_dashboard_archives import ArchiveError, extract_owned_tree, missing_local_resources


def test_dashboard_windows_backslash_directory_entries_are_directories(tmp_path: Path):
    archive = tmp_path / "dashboard.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("dist\\assets\\", "")
        z.writestr("dist\\assets\\entry.js", "data-model-id")
        z.writestr("dist\\index.html", '<html><head><script src="/assets/entry.js"></script></head></html>')
    dist = tmp_path / "crew/static/dist"
    stats = extract_owned_tree(archive, "dist/", dist)
    assert stats["entries"] == 3
    assert (dist / "assets").is_dir()
    assert missing_local_resources(dist) == []


@pytest.mark.parametrize("path", ["dist/../escape.txt", "dist/../../outside", "dist/./bad.js", "dist/C:/drive.js"])
def test_archive_path_traversal_or_ambiguous_members_refused(tmp_path: Path, path: str):
    archive = tmp_path / "bad.zip"
    with ZipFile(archive, "w") as z:
        z.writestr(path, "bad")
    with pytest.raises(ArchiveError):
        extract_owned_tree(archive, "dist/", tmp_path / "output")


def test_archive_windows_case_collision_refused(tmp_path: Path):
    archive = tmp_path / "collision.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("dist/assets/X.js", "a")
        z.writestr("dist/assets/x.js", "b")
    with pytest.raises(ArchiveError, match="Windows-alias"):
        extract_owned_tree(archive, "dist/", tmp_path / "output")


def test_archive_symlink_refused(tmp_path: Path):
    archive = tmp_path / "symlink.zip"
    info = ZipInfo("dist/assets/evil")
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    with ZipFile(archive, "w") as z:
        z.writestr(info, "../outside")
    with pytest.raises(ArchiveError, match="symlink"):
        extract_owned_tree(archive, "dist/", tmp_path / "output")


def test_unresolved_dashboard_shell_reference_reported(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<head><link href="/logo.png"/><script src="/assets/app.js"></script></head>')
    (dist / "assets").mkdir()
    (dist / "assets/app.js").write_text("console.log('hi')")
    assert missing_local_resources(dist) == ["/logo.png"]
