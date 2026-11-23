"""A restore never installs the archive's host-local workspace root or session map.

``workspace_dir`` roots every session work directory, outbox and upload root, and
``session_map.json`` holds the working folder a resumed session starts in. Both name paths
on the machine that made the archive, so an archive from someone else must not set them
here, in replace mode or in merge mode, whether this install already has the file or not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv
from test_portability_home_paths import homes as _portability_homes
from test_snapshot import _make_snapshot, _setup_fake_kirocrew

from kiro_crew import portability
from kiro_crew.config.loader_locations import normalize_workspace_path, workspace_root
from kiro_crew.portability import apply_import_zip, create_export_zip
from kiro_crew.snapshot import restore_main

_no_gateway = snapshot_helpers._no_gateway
homes = _portability_homes  # the import tests below use that module's two-homes fixture

_ARCHIVE_WORKSPACE = "/archive-host/elsewhere/workspace\n"
_ARCHIVE_SESSION_MAP = '{"chat-1": {"cwd": "/archive-host/elsewhere/project"}}'
_LIVE_WORKSPACE = "/live-host/workspace\n"
_LIVE_SESSION_MAP = '{"chat-2": {"cwd": "/live-host/project"}}'


@pytest.fixture
def tarball(tmp_path, monkeypatch):
    """An archive whose workspace_dir and session_map.json name another host's paths."""
    src = tmp_path / "src"
    _setup_fake_kirocrew(src)
    (src / "workspace_dir").write_text(_ARCHIVE_WORKSPACE)
    (src / "session_map.json").write_text(_ARCHIVE_SESSION_MAP)
    monkeypatch.setenv("KIROCREW_HOME", str(src))
    return _make_snapshot(src, tmp_path / "out")


def _restore(tarball, mode):
    args = [str(tarball), "--mode", mode, "--force", "--components", "config"]
    return restore_main(args + unpinnable_argv())


def _live_home(tmp_path, monkeypatch):
    live = tmp_path / "live"
    _setup_fake_kirocrew(live)
    (live / "workspace_dir").write_text(_LIVE_WORKSPACE)
    (live / "session_map.json").write_text(_LIVE_SESSION_MAP)
    monkeypatch.setenv("KIROCREW_HOME", str(live))
    return live


def _empty_home(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(fresh))
    return fresh


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_the_live_copies_are_kept(tarball, tmp_path, monkeypatch, mode):
    live = _live_home(tmp_path, monkeypatch)
    _restore(tarball, mode)
    assert (live / "workspace_dir").read_text() == _LIVE_WORKSPACE
    assert (live / "session_map.json").read_text() == _LIVE_SESSION_MAP


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_nothing_is_created_when_the_install_has_none(tarball, tmp_path, monkeypatch, mode):
    fresh = _empty_home(tmp_path, monkeypatch)
    _restore(tarball, mode)
    assert not (fresh / "workspace_dir").exists()
    assert not (fresh / "session_map.json").exists()


def test_replace_still_installs_the_settings_files(tarball, tmp_path, monkeypatch):
    fresh = _empty_home(tmp_path, monkeypatch)
    _restore(tarball, "replace")
    assert (fresh / "config.json").is_file()
    assert (fresh / "hooks.json").is_file()


def test_the_summary_names_what_was_kept(tarball, tmp_path, monkeypatch, capsys):
    _live_home(tmp_path, monkeypatch)
    _restore(tarball, "replace")
    out = capsys.readouterr().out
    said = "not restored (the backup's copy points at the machine that made it); this install's"
    assert f"workspace_dir: {said} copy is kept" in out
    assert f"session_map.json: {said} copy is kept" in out


def test_the_summary_does_not_claim_a_copy_was_kept_when_there_was_none(
    tarball, tmp_path, monkeypatch, capsys
):
    _empty_home(tmp_path, monkeypatch)
    _restore(tarball, "replace")
    out = capsys.readouterr().out
    assert "workspace_dir: not restored (the backup's copy points at the machine" in out
    assert "copy is kept" not in out


# The dashboard import moves the archive's saved folders under THIS home first, so it may
# install the workspace root when the moved folder is an ordinary one under this home.


def _import_replace(homes, tmp_path, monkeypatch, folder, prepare_live=None):
    """Export an install whose workspace_dir names *folder*, replace-import it into another."""
    old_home, new_home, _ = homes
    (old_home / ".kiro" / "crew" / "workspace_dir").write_text(f"{folder}\n")
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(create_export_zip(include_sessions=True)[0])
    target = new_home / ".kiro" / "crew"
    target.mkdir(parents=True)
    live = target / "workspace_dir"
    live.write_text(_LIVE_WORKSPACE)
    if prepare_live is not None:
        prepare_live(live)
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    return target, apply_import_zip(zip_path, mode="replace")


def _not_installed(summary):
    return [i for i in summary["items"] if i.startswith("workspace_dir (not installed")]


_OUTSIDE_HOME = "it is not a folder inside this home"
_NOT_PLAIN = "the saved folder is not a plain absolute path"
_HOLDS_DATA_HOME = "it is the data home or a folder holding it"
_PROTECTED = "it is a protected folder"


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize(
    ("where", "why"),
    [
        ("link_to_etc", _OUTSIDE_HOME),
        ("ssh", _PROTECTED),
        ("outside_home", _OUTSIDE_HOME),
        ("home_itself", _OUTSIDE_HOME),
        ("data_home", _HOLDS_DATA_HOME),
        ("data_home_parent", _HOLDS_DATA_HOME),
        ("relative", _NOT_PLAIN),
        ("dot_dot", _NOT_PLAIN),
        ("embedded_nul", _NOT_PLAIN),
    ],
)
def test_import_keeps_the_live_workspace_dir_for_an_unsafe_folder(
    homes, tmp_path, monkeypatch, where, why
):
    old_home, new_home, _ = homes
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (new_home / "link").symlink_to("/etc")
    folder = {
        "link_to_etc": old_home / "link",
        "ssh": old_home / ".ssh",
        "outside_home": outside,
        "home_itself": old_home,
        "data_home": old_home / ".kiro" / "crew",
        "data_home_parent": old_home / ".kiro",
        "relative": "work/project",
        "dot_dot": f"{outside}/../outside-dir",
        "embedded_nul": f"{old_home}/work\x00x",
    }[where]

    target, summary = _import_replace(homes, tmp_path, monkeypatch, folder)

    assert (target / "workspace_dir").read_text() == _LIVE_WORKSPACE
    assert any(why in item for item in _not_installed(summary)), summary["items"]


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_installs_an_ordinary_folder_under_this_home(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes

    target, summary = _import_replace(homes, tmp_path, monkeypatch, old_home / "work" / "x")

    assert normalize_workspace_path((target / "workspace_dir").read_text()) == (
        new_home / "work" / "x"
    )
    assert not _not_installed(summary)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_backs_up_the_live_workspace_dir_before_installing(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes

    target, summary = _import_replace(homes, tmp_path, monkeypatch, old_home / "work" / "x")

    backups = [
        path
        for path in target.rglob("pre-restore-*")
        if path.is_dir() and (path / "workspace_dir").is_file()
    ]
    assert len(backups) == 1
    assert (backups[0] / "workspace_dir").read_text() == _LIVE_WORKSPACE
    assert normalize_workspace_path((target / "workspace_dir").read_text()) == (
        new_home / "work" / "x"
    )
    assert not _not_installed(summary)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_does_not_copy_through_a_live_workspace_dir_symlink(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes
    secret = new_home / "secret.txt"
    marker = b"workspace-dir-secret-marker-9dc9c548\n"
    secret.write_bytes(marker)

    def link_live_workspace_to_secret(live):
        live.unlink()
        live.symlink_to(os.path.relpath(secret, live.parent))

    target, summary = _import_replace(
        homes,
        tmp_path,
        monkeypatch,
        old_home / "work" / "x",
        prepare_live=link_live_workspace_to_secret,
    )

    backups = [path for path in target.rglob("pre-restore-*") if path.is_dir()]
    assert backups
    for backup in backups:
        for path in backup.rglob("*"):
            if path.is_symlink():
                assert path.resolve(strict=False) != secret.resolve()
            elif path.is_file():
                assert path.read_bytes() != marker
    assert secret.read_bytes() == marker
    assert "full replace" in summary["items"]


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_keeps_live_workspace_dir_for_non_utf8_resolved_folder(homes, tmp_path, monkeypatch):
    _old_home, new_home, _ = homes
    raw_folder = os.fsencode(new_home) + b"/bad\xff"
    try:
        os.mkdir(raw_folder)
    except OSError as exc:
        pytest.skip(f"filesystem refused a non-UTF-8 folder name: {exc}")
    folder = Path(os.fsdecode(raw_folder))
    alias = new_home / "bad-name"
    try:
        os.symlink(os.fsencode(folder), os.fsencode(alias), target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"filesystem refused the test symlink: {exc}")

    target, summary = _import_replace(homes, tmp_path, monkeypatch, alias)

    assert (target / "workspace_dir").read_text() == _LIVE_WORKSPACE
    refused = _not_installed(summary)
    assert refused
    assert any("not valid UTF-8" in item for item in refused)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_writes_the_resolved_folder_not_the_raw_value(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes
    (new_home / "real").mkdir()
    (new_home / "alias").symlink_to(new_home / "real")

    target, summary = _import_replace(homes, tmp_path, monkeypatch, old_home / "alias")

    assert (
        normalize_workspace_path((target / "workspace_dir").read_text())
        == (new_home / "real").resolve()
    )
    assert not _not_installed(summary)


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_preserves_quoted_trailing_space_in_workspace_dir(homes, tmp_path, monkeypatch):
    _old_home, new_home, _ = homes
    validated = new_home / ".ssh "
    validated.mkdir()

    target, summary = _import_replace(homes, tmp_path, monkeypatch, f'"{validated}"')

    root = workspace_root()
    assert not root.is_relative_to((new_home / ".ssh").resolve())
    if not _not_installed(summary):
        assert normalize_workspace_path((target / "workspace_dir").read_text()) == (
            validated.resolve()
        )


# Nothing that can refuse may run after the replace has committed: a failure there would
# report an import as failed that has already replaced the settings and trees.


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_completes_when_the_staged_workspace_dir_is_a_directory(
    homes, tmp_path, monkeypatch
):
    old_home, _new_home, _ = homes
    real_move = portability._move_home_paths

    def leave_a_directory_at_the_name(snap, summary):
        cleared = real_move(snap, summary)
        (snap / "workspace_dir").unlink()
        (snap / "workspace_dir").mkdir()
        return cleared

    monkeypatch.setattr(portability, "_move_home_paths", leave_a_directory_at_the_name)

    target, summary = _import_replace(homes, tmp_path, monkeypatch, old_home / "work")

    assert (target / "workspace_dir").read_text() == _LIVE_WORKSPACE
    assert any("the saved folder is not a plain file" in i for i in _not_installed(summary))
    assert "full replace" in summary["items"]


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_rolls_back_when_the_workspace_dir_cannot_be_installed(homes, tmp_path, monkeypatch):
    old_home, new_home, _ = homes
    real_copy = portability.pinned_fs.copy_file_pinned
    failed: list[bool] = []

    def fail_for_workspace_dir(*args, **kwargs):
        destination = kwargs.get("dst_name")
        if destination is None and len(args) > 1 and args[1] is not None:
            destination = Path(args[1]).name
        if destination == "workspace_dir" and not failed:
            failed.append(True)
            raise OSError(28, "No space left on device")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(portability.pinned_fs, "copy_file_pinned", fail_for_workspace_dir)

    with pytest.raises(OSError, match="No space left on device"):
        _import_replace(homes, tmp_path, monkeypatch, old_home / "work")

    target = new_home / ".kiro" / "crew"
    assert failed
    assert (target / "workspace_dir").read_text() == _LIVE_WORKSPACE


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
def test_import_keeps_the_live_workspace_dir_when_this_home_is_unknown(
    homes, tmp_path, monkeypatch
):
    old_home, _new_home, _ = homes
    monkeypatch.setattr(portability, "this_home", lambda: None)

    target, summary = _import_replace(homes, tmp_path, monkeypatch, old_home / "work")

    assert (target / "workspace_dir").read_text() == _LIVE_WORKSPACE
    assert any("this install's home is unknown" in i for i in _not_installed(summary))
