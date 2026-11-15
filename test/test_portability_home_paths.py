"""An archive exported under one home and imported under another (`home_path_rewrite`)."""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew import home_path_rewrite, portability
from kiro_crew.config.loader_locations import workspace_root
from kiro_crew.home_path_rewrite import PathRewrite, map_home_path
from kiro_crew.portability import apply_import_zip, create_export_zip


def _header(**fields) -> str:
    return json.dumps({"_type": "metadata", "title": "chat", "memory_mode": "persistent", **fields})


def _row(text: str) -> str:
    return json.dumps({"role": "user", "content": text, "ts": "2026-01-01T00:00:00"})


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """An install under the home ``a/home/x`` whose chats and settings name that home."""
    old_home = tmp_path / "a" / "home" / "x"
    new_home = tmp_path / "b" / "Users" / "x"
    elsewhere = tmp_path / "gone" / "work"  # outside both homes, missing on the importer
    mc = old_home / ".kiro" / "crew"
    sessions = mc / "sessions"
    sessions.mkdir(parents=True)
    new_home.mkdir(parents=True)
    (mc / "config.json").write_text(
        json.dumps({"dashboard": {"default_project": str(old_home / "app")}})
    )
    (mc / "workspace_dir").write_text(f"{old_home / 'work'}\n")
    (sessions / "dashboard_mapped.jsonl").write_text(
        _header(project=str(old_home / "app")) + "\n" + _row("mapped") + "\n"
    )
    (sessions / "dashboard_elsewhere.jsonl").write_text(
        _header(project=str(elsewhere)) + "\n" + _row("elsewhere") + "\n"
    )
    (sessions / "dashboard_plain.jsonl").write_text(_header() + "\n" + _row("plain") + "\n")
    os.utime(sessions / "dashboard_mapped.jsonl", (1_700_000_000, 1_700_000_000))
    monkeypatch.setenv("HOME", str(old_home))
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
    return old_home, new_home, elsewhere


def _export_then_import(homes, tmp_path, monkeypatch, mode: str) -> tuple[Path, dict]:
    _old_home, new_home, _ = homes
    zip_bytes = create_export_zip(include_sessions=True)[0]
    zip_path = tmp_path / f"export-{mode}.zip"
    zip_path.write_bytes(zip_bytes)
    target = new_home / ".kiro" / "crew"
    target.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    return target, apply_import_zip(zip_path, mode=mode)


def _project(target: Path, stem: str) -> str | None:
    header = json.loads((target / "sessions" / f"{stem}.jsonl").read_text().splitlines()[0])
    return header.get("project")


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_chat_projects_move_to_the_new_home(homes, tmp_path, monkeypatch, mode):
    old_home, new_home, _ = homes
    target, summary = _export_then_import(homes, tmp_path, monkeypatch, mode)

    assert _project(target, "dashboard_mapped") == str(new_home / "app")
    # A folder that is under neither home and missing here is dropped, so the chat
    # opens in the default workspace; the chat itself and its rows stay.
    assert _project(target, "dashboard_elsewhere") is None
    assert "elsewhere" in (target / "sessions" / "dashboard_elsewhere.jsonl").read_text()
    assert _project(target, "dashboard_plain") is None
    assert summary["home_paths_moved"] >= 1
    assert summary["sessions_project_cleared"] == 1
    # The chat keeps its own time, which the import copies onto the installed file.
    assert (target / "sessions" / "dashboard_mapped.jsonl").stat().st_mtime == 1_700_000_000
    for stem in ("dashboard_mapped", "dashboard_elsewhere", "dashboard_plain"):
        assert str(old_home) not in (target / "sessions" / f"{stem}.jsonl").read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_a_reimport_counts_no_cleared_chat(homes, tmp_path, monkeypatch):
    """Only chats this import installs are counted, so a second import of one export
    reports none: its chats are already here and are skipped."""
    target, first = _export_then_import(homes, tmp_path, monkeypatch, "merge")

    second = apply_import_zip(tmp_path / "export-merge.zip", mode="merge")

    assert first["sessions_project_cleared"] == 1
    assert second["sessions_added"] == 0
    assert "sessions_project_cleared" not in second
    assert _project(target, "dashboard_elsewhere") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_replace_moves_the_workspace_root_and_default_project(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes
    target, _summary = _export_then_import(homes, tmp_path, monkeypatch, "replace")

    assert (target / "workspace_dir").read_text().strip() == str(new_home / "work")
    config = json.loads((target / "config.json").read_text())
    assert config["dashboard"]["default_project"] == str(new_home / "app")
    # Opening a session resolves (and creates) the workspace root: it must be under
    # the new home, never the exporting one (the read-only ``/local`` of the report).
    root = workspace_root()
    assert root.is_relative_to(new_home.resolve())
    assert not old_home.joinpath("work").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_replace_moves_the_overlay_default_project(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes
    overlay = old_home / ".kiro" / "crew" / "config.local.json"
    overlay.write_text(
        json.dumps({"dashboard": {"default_project": str(old_home / "overlay-app")}}),
        encoding="utf-8",
    )

    target, _summary = _export_then_import(homes, tmp_path, monkeypatch, "replace")

    config = json.loads((target / "config.local.json").read_text(encoding="utf-8"))
    assert config["dashboard"]["default_project"] == str(new_home / "overlay-app")


def test_legacy_home_inferred_from_only_the_overlay(tmp_path):
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"user": "x"}), encoding="utf-8")
    (tmp_path / "config.local.json").write_text(
        json.dumps({"dashboard": {"default_project": "/a/home/x/app"}}), encoding="utf-8"
    )

    assert home_path_rewrite.archive_home(tmp_path) == ("/a/home/x",)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("name", ["workspace_dir", "project_dir"])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("legacy", [False, True])
def test_quoted_path_files_move_unquoted(tmp_path, name, quote, legacy):
    manifest = {"user": "x"} if legacy else {"home": "/a/home/x"}
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    path_file = tmp_path / name
    path_file.write_text(f"{quote}/a/home/x/work{quote}\n", encoding="utf-8")

    old_homes = home_path_rewrite.archive_home(tmp_path)
    assert old_homes == ("/a/home/x",)
    counts = home_path_rewrite.rewrite_staged_paths(tmp_path, old_homes, "/b/Users/x")

    assert path_file.read_text(encoding="utf-8") == "/b/Users/x/work\n"
    assert counts == PathRewrite(moved=1)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_an_archive_without_a_home_reads_it_from_its_paths(homes, tmp_path, monkeypatch):
    """An export made before the manifest named its home still moves."""
    old_home, new_home, _ = homes
    monkeypatch.setenv("USER", "x")
    zip_bytes = create_export_zip(include_sessions=True)[0]
    src = tmp_path / "with-home.zip"
    src.write_bytes(zip_bytes)
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(legacy, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename.endswith("/MANIFEST.json"):
                manifest = json.loads(data)
                assert manifest.pop("home") == str(old_home)
                manifest.pop("home_real", None)
                data = json.dumps(manifest).encode()
            else:
                data = data.replace(str(old_home).encode(), b"/a/home/x")
            zout.writestr(info, data)
    target = new_home / ".kiro" / "crew"
    target.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    apply_import_zip(legacy, mode="merge")

    assert _project(target, "dashboard_mapped") == str(new_home / "app")


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_same_home_changes_nothing(homes, tmp_path, monkeypatch):
    old_home, _new_home, elsewhere = homes
    zip_path = tmp_path / "same.zip"
    zip_path.write_bytes(create_export_zip(include_sessions=True)[0])
    target = old_home / "restore"
    target.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    summary = apply_import_zip(zip_path, mode="merge")

    assert _project(target, "dashboard_elsewhere") == str(elsewhere)
    assert "sessions_project_cleared" not in summary


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_a_folder_that_exists_here_is_kept(homes, tmp_path, monkeypatch):
    _old_home, _new_home, elsewhere = homes
    elsewhere.mkdir(parents=True)
    target, summary = _export_then_import(homes, tmp_path, monkeypatch, "merge")

    assert _project(target, "dashboard_elsewhere") == str(elsewhere)
    assert "sessions_project_cleared" not in summary


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/a/home/x", "/b/Users/x"),
        ("/a/home/x/app/src", "/b/Users/x/app/src"),
        ("/a/home/x/../../etc", None),
        ("relative/path", None),
        ("/elsewhere", None),
    ],
)
def test_map_home_path(value, expected):
    assert map_home_path(value, "/a/home/x", "/b/Users/x") == expected


def test_map_home_path_rejects_a_sibling_with_the_same_prefix():
    assert map_home_path("/srv/u/xy/app", "/srv/u/x", "/srv/v/x") is None


@pytest.mark.parametrize("field", ["home", "home_real"])
@pytest.mark.parametrize(
    "home", [None, 7, "", "/", "//", "relative/x", "/a/../b", "/a/b\x00", "/" + "x" * 4096]
)
def test_a_manifest_home_that_is_not_a_real_home_is_ignored(tmp_path, home, field):
    staged = tmp_path / "snap"
    staged.mkdir()
    (staged / "MANIFEST.json").write_text(json.dumps({field: home, "user": "unknown"}))
    assert home_path_rewrite.archive_home(staged) is None


@pytest.mark.parametrize("manifest", [{"home": "/root"}, {"user": "root"}])
def test_single_component_home_moves_paths(tmp_path, monkeypatch, manifest):
    exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/root" else exists(p))
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    workspace = tmp_path / "workspace_dir"
    workspace.write_text("/root/app\n", encoding="utf-8")

    old_home = home_path_rewrite.archive_home(tmp_path)
    assert old_home is not None
    counts = home_path_rewrite.rewrite_staged_paths(tmp_path, old_home, "/srv/root")

    assert workspace.read_text(encoding="utf-8").strip() == "/srv/root/app"
    assert counts == PathRewrite(moved=1)


@pytest.mark.parametrize(
    ("home", "home_real", "saved"),
    [
        ("/a/home/x", "/real/home/x", "/real/home/x/app"),
        ("/a/home/x", "/real/home/x", "/a/home/x/app"),
        ("/a/home/x", "/a/home/x/resolved", "/a/home/x/resolved/app"),
        ("/a/home/x/resolved", "/a/home/x", "/a/home/x/resolved/app"),
        ("/b/Users/x", "/real/home/x", "/real/home/x/app"),
        ("/", "/real/home/x", "/real/home/x/app"),
        ("/a/home/x", "/", "/a/home/x/app"),
    ],
)
def test_manifest_home_aliases_move_paths(tmp_path, home, home_real, saved):
    (tmp_path / "MANIFEST.json").write_text(
        json.dumps({"home": home, "home_real": home_real}), encoding="utf-8"
    )
    workspace = tmp_path / "workspace_dir"
    workspace.write_text(saved + "\n", encoding="utf-8")

    old_home = home_path_rewrite.archive_home(tmp_path)
    assert old_home is not None
    counts = home_path_rewrite.rewrite_staged_paths(tmp_path, old_home, "/b/Users/x")

    assert workspace.read_text(encoding="utf-8").strip() == "/b/Users/x/app"
    assert counts == PathRewrite(moved=1)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_export_under_symlinked_home_moves_resolved_paths(homes, tmp_path, monkeypatch, mode):
    old_home, new_home, _ = homes
    linked_home = tmp_path / "linked-home"
    os.symlink(old_home, linked_home, target_is_directory=True)
    resolved_home = linked_home.resolve()
    mc = old_home / ".kiro" / "crew"
    (mc / "workspace_dir").write_text(f"{resolved_home / 'work'}\n", encoding="utf-8")
    (mc / "config.json").write_text(
        json.dumps({"dashboard": {"default_project": str(resolved_home / "app")}}),
        encoding="utf-8",
    )
    (mc / "sessions" / "dashboard_mapped.jsonl").write_text(
        _header(project=str(resolved_home / "app")) + "\n" + _row("mapped") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(linked_home))
    plan_sessions = Mock(wraps=portability._plan_sessions)
    monkeypatch.setattr(portability, "_plan_sessions", plan_sessions)
    portability.plan_export(include_sessions=True)
    planned_manifest = plan_sessions.call_args.kwargs["manifest"]
    zip_bytes, manifest = create_export_zip(include_sessions=True)
    zip_path = tmp_path / "symlink-home.zip"
    zip_path.write_bytes(zip_bytes)
    with zipfile.ZipFile(zip_path) as archive:
        name = next(name for name in archive.namelist() if name.endswith("/MANIFEST.json"))
        written_manifest = json.loads(archive.read(name))

    target = new_home / ".kiro" / "crew"
    target.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    summary = apply_import_zip(zip_path, mode=mode)

    assert _project(target, "dashboard_mapped") == str(new_home / "app")
    assert summary["home_paths_moved"] >= 1
    if mode == "replace":
        assert (target / "workspace_dir").read_text().strip() == str(new_home / "work")
        config = json.loads((target / "config.json").read_text())
        assert config["dashboard"]["default_project"] == str(new_home / "app")
    for envelope in (planned_manifest, manifest, written_manifest):
        assert envelope["home"] == str(linked_home)
        assert envelope["home_real"] == str(resolved_home)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_both_spellings_of_this_home_change_nothing(tmp_path):
    """A restore on the exporting computer, whose home is a link, keeps every path."""
    real_home = tmp_path / "real" / "x"
    real_home.mkdir(parents=True)
    linked_home = tmp_path / "linked"
    os.symlink(real_home, linked_home, target_is_directory=True)
    workspace = tmp_path / "workspace_dir"
    workspace.write_text("/not/mounted/now\n", encoding="utf-8")

    counts = home_path_rewrite.rewrite_staged_paths(
        tmp_path, (str(real_home), str(linked_home)), str(linked_home)
    )

    assert workspace.read_text(encoding="utf-8").strip() == "/not/mounted/now"
    assert counts == PathRewrite()


@pytest.mark.parametrize("saved", ["~/not-made-yet", '"~/not-made-yet"'])
def test_a_tilde_workspace_is_left_as_written(tmp_path, saved):
    """A ``~`` path already follows this home, so it is never judged or dropped."""
    workspace = tmp_path / "workspace_dir"
    workspace.write_text(saved + "\n", encoding="utf-8")

    counts = home_path_rewrite.rewrite_staged_paths(tmp_path, ("/a/home/x",), "/b/Users/x")

    assert workspace.read_text(encoding="utf-8").strip() == saved
    assert counts == PathRewrite()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize(
    "name", ["config.json", "config.local.json", "workspace_dir", "project_dir"]
)
def test_staged_symlink_is_untouched(tmp_path, name):
    staged = tmp_path / "staged"
    staged.mkdir()
    outside = tmp_path / "outside"
    original = b'{"dashboard": {"default_project": "/a/home/x/app"}}'
    if name in ("workspace_dir", "project_dir"):
        original = b"/a/home/x/app\n"
    outside.write_bytes(original)
    path = staged / name
    path.symlink_to(outside)

    counts = home_path_rewrite.rewrite_staged_paths(staged, ("/a/home/x",), "/b/Users/x")

    assert outside.read_bytes() == original
    assert path.is_symlink()
    assert counts == PathRewrite()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize(
    "name", ["config.json", "config.local.json", "workspace_dir", "project_dir"]
)
def test_staged_symlink_swap_after_read_cannot_write_outside(tmp_path, monkeypatch, name):
    staged = tmp_path / "staged"
    staged.mkdir()
    outside = tmp_path / "outside"
    original = b"outside bytes must stay unchanged\n"
    outside.write_bytes(original)
    path = staged / name
    is_config = name in ("config.json", "config.local.json")
    saved = json.dumps({"dashboard": {"default_project": "/a/home/x/app"}})
    path.write_text(saved if is_config else "/a/home/x/app\n", encoding="utf-8")
    reader_name = "_read_json_object" if is_config else "_read_path_file"
    read = getattr(home_path_rewrite, reader_name)

    def swap_after_read(candidate):
        value = read(candidate)
        if candidate == path:
            path.unlink()
            path.symlink_to(outside)
        return value

    monkeypatch.setattr(home_path_rewrite, reader_name, swap_after_read)

    counts = home_path_rewrite.rewrite_staged_paths(staged, ("/a/home/x",), "/b/Users/x")

    assert outside.read_bytes() == original
    assert not path.is_symlink()
    if is_config:
        assert json.loads(path.read_text(encoding="utf-8"))["dashboard"]["default_project"] == (
            "/b/Users/x/app"
        )
    else:
        assert path.read_text(encoding="utf-8") == "/b/Users/x/app\n"
    assert counts == PathRewrite(moved=1)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("reader_name", ["_read_path_file", "_read_json_object", "_read_header"])
def test_staged_read_rejects_a_link_after_the_path_check(tmp_path, monkeypatch, reader_name):
    path = tmp_path / "staged"
    outside = tmp_path / "outside"
    original = (_header(project="/a/home/x/app") + "\n").encode("utf-8")
    outside.write_bytes(original)
    path.write_bytes(original)
    is_symlink = Path.is_symlink

    def swap_at_check(candidate):
        if candidate == path:
            path.unlink()
            path.symlink_to(outside)
            return False
        return is_symlink(candidate)

    monkeypatch.setattr(Path, "is_symlink", swap_at_check)
    # The header reader has no path check; plant its link before it opens.
    if reader_name == "_read_header":
        path.unlink()
        path.symlink_to(outside)

    result = getattr(home_path_rewrite, reader_name)(path)

    assert result == ("" if reader_name == "_read_path_file" else None)
    assert outside.read_bytes() == original


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_header_copy_rejects_a_staged_symlink(tmp_path):
    outside = tmp_path / "outside"
    original = (_header(project="/a/home/x/app") + "\n" + _row("outside") + "\n").encode("utf-8")
    outside.write_bytes(original)
    transcript = tmp_path / "chat.jsonl"
    transcript.symlink_to(outside)

    home_path_rewrite._replace_header(transcript, {"_type": "metadata"}, original.index(b"\n") + 1)

    assert outside.read_bytes() == original
    assert transcript.is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_staged_read_rejects_nonregular_files(tmp_path, kind):
    path = tmp_path / "staged"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    assert home_path_rewrite._read_path_file(path) == ""
    assert home_path_rewrite._read_json_object(path) is None
    assert home_path_rewrite._read_header(path) is None
    home_path_rewrite._replace_header(path, {"_type": "metadata"}, 0)
    assert path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("exists_here", [False, True])
@pytest.mark.parametrize("named", [False, True])
def test_legacy_home_that_exists_here_does_not_move(tmp_path, monkeypatch, exists_here, named):
    old_home = tmp_path / "a" / "home" / "x"
    if exists_here:
        old_home.mkdir(parents=True)
    staged = tmp_path / "staged"
    staged.mkdir()
    manifest = {"home": str(old_home)} if named else {"user": "x"}
    (staged / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    original = str(old_home / "pkg") + "\n"
    workspace = staged / "workspace_dir"
    workspace.write_text(original, encoding="utf-8")
    new_home = tmp_path / "b" / "Users" / "x"
    monkeypatch.setattr(portability, "this_home", lambda: str(new_home))
    summary = {}

    portability._move_home_paths(staged, summary)

    if exists_here and not named:
        assert workspace.read_text(encoding="utf-8") == original
        assert summary == {}
    else:
        assert workspace.read_text(encoding="utf-8") == str(new_home / "pkg") + "\n"
        assert summary["home_paths_moved"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_import_extraction_is_gateway_private(homes, tmp_path, monkeypatch, mode):
    from kiro_crew import sandbox_leaves, sandbox_precreate, sandbox_tiers, security

    _old_home, new_home, _ = homes
    target = new_home / ".kiro" / "crew"
    checked = []
    move = portability._move_home_paths

    def check_extraction(snap, summary):
        work = snap.parent
        private_root = work.parent
        assert private_root.parent == target
        assert work.stat().st_mode & 0o777 == 0o700
        assert private_root.stat().st_mode & 0o777 == 0o700
        assert work.stat().st_dev == target.stat().st_dev
        assert private_root.name in sandbox_leaves._CREW_HIDDEN_LEAVES
        assert private_root.name not in sandbox_leaves._CREW_SANDBOX_VISIBLE_LEAVES
        assert private_root.name in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        for sandbox_mode in ("standard", "cc", "strict"):
            assert sandbox_tiers.carveout_shadowed_by_foreign_mask(str(work), sandbox_mode)
        assert security.is_sensitive_path(str(work / "config.json"))
        checked.append(work)
        return move(snap, summary)

    monkeypatch.setattr(portability, "_move_home_paths", check_extraction)
    _export_then_import(homes, tmp_path, monkeypatch, mode)
    assert len(checked) == 1
    assert not checked[0].exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_replace_from_private_staging_still_restores_crons(homes, tmp_path, monkeypatch):
    """The sensitive-file strip judges members as before; the private leaf drops none."""
    old_home, _new_home, _ = homes
    (old_home / ".kiro" / "crew" / "crons.json").write_text(
        json.dumps({"jobs": []}), encoding="utf-8"
    )

    target, _summary = _export_then_import(homes, tmp_path, monkeypatch, "replace")

    assert json.loads((target / "crons.json").read_text(encoding="utf-8")) == {"jobs": []}


@pytest.mark.parametrize("name", ["workspace_dir", "project_dir"])
def test_oversized_path_file_is_not_inferred_or_rewritten(tmp_path, name):
    cap = home_path_rewrite._MAX_HOME_CHARS * 4 + 16
    original = b"/a/home/x/app".ljust(cap + 1, b" ")
    path = tmp_path / name
    path.write_bytes(original)
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"user": "x"}), encoding="utf-8")

    assert home_path_rewrite._archive_paths(tmp_path) == []
    assert home_path_rewrite.archive_home(tmp_path) is None
    counts = home_path_rewrite.rewrite_staged_paths(tmp_path, ("/a/home/x",), "/b/Users/x")

    assert counts == PathRewrite()
    assert path.read_bytes() == original


@pytest.mark.parametrize("extra", [0, 1])
def test_path_file_read_is_bounded(tmp_path, monkeypatch, extra):
    cap = home_path_rewrite._MAX_HOME_CHARS * 4 + 16
    path = tmp_path / "workspace_dir"
    path.write_bytes(b"/a/home/x/app".ljust(cap + extra, b" "))
    with home_path_rewrite._open_regular(path) as fh:
        read = Mock(wraps=fh.read)
        monkeypatch.setattr(fh, "read", read)
        monkeypatch.setattr(home_path_rewrite, "_open_regular", lambda candidate: fh)

        value = home_path_rewrite._read_path_file(path)

    # Every read the helper makes is bounded; no unbounded ``read()`` ever happens.
    assert read.call_count >= 1
    for call in read.call_args_list:
        assert call.args and 0 < call.args[0] <= cap + 1
    assert value == ("" if extra else "/a/home/x/app")
