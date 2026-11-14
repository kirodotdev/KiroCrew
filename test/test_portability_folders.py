"""Chat folders ride a whole-install export and import (`dashboard.folder_portability`)."""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path

import pytest

from kiro_crew.dashboard.folder_portability import (
    FolderMergeResult,
    add_missing_folders,
    import_folders,
    live_folder_merge,
    read_archive_folders,
)
from kiro_crew.portability import apply_import_zip, create_export_zip


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """An install under ``a/home/x`` with two chat folders, one with a project."""
    old_home = tmp_path / "a" / "home" / "x"
    new_home = tmp_path / "b" / "Users" / "x"
    mc = old_home / ".kiro" / "crew"
    mc.mkdir(parents=True)
    new_home.mkdir(parents=True)
    folders = [
        {"id": "f-app", "name": "App", "parent_id": "", "project_dir": str(old_home / "app")},
        {"id": "f-gone", "name": "Gone", "parent_id": "f-app", "project_dir": "/nowhere/here"},
    ]
    (mc / "folders.json").write_text(json.dumps(folders))
    monkeypatch.setenv("HOME", str(old_home))
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
    return old_home, new_home


def _export(tmp_path: Path) -> Path:
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(create_export_zip(include_sessions=True)[0])
    return zip_path


def _target(new_home: Path, monkeypatch, existing: list[dict] | None = None) -> Path:
    target = new_home / ".kiro" / "crew"
    target.mkdir(parents=True)
    if existing is not None:
        (target / "folders.json").write_text(json.dumps(existing))
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    return target


def test_export_carries_folders_json(homes, tmp_path):
    with zipfile.ZipFile(_export(tmp_path)) as zf:
        assert any(name.endswith("/folders.json") for name in zf.namelist())


@pytest.mark.skipif(sys.platform == "win32", reason="Home relocation is POSIX-only")
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_import_adds_missing_folders_and_keeps_existing(homes, tmp_path, monkeypatch, mode):
    _old_home, new_home = homes
    zip_path = _export(tmp_path)
    mine = {"id": "f-app", "name": "Mine", "parent_id": ""}
    target = _target(new_home, monkeypatch, existing=[mine])

    summary = apply_import_zip(zip_path, mode=mode)

    rows = {row["id"]: row for row in json.loads((target / "folders.json").read_text())}
    # A folder this install already has is left exactly as it is, in both modes.
    assert rows["f-app"] == mine
    # The archive's other folder arrives, without the old computer's project path.
    assert rows["f-gone"]["name"] == "Gone"
    assert "project_dir" not in rows["f-gone"]
    assert "folders (merged: 1 added, 1 already here)" in summary["items"]


def test_imported_folder_carries_no_project(homes, tmp_path, monkeypatch):
    _old_home, new_home = homes
    zip_path = _export(tmp_path)
    target = _target(new_home, monkeypatch)
    (new_home / "app").mkdir()

    apply_import_zip(zip_path, mode="merge")

    rows = {row["id"]: row for row in json.loads((target / "folders.json").read_text())}
    # A path from the archive is never resolved here, so it is not carried.
    assert set(rows) == {"f-app", "f-gone"}
    assert all("project_dir" not in row for row in rows.values())


def test_add_missing_folders_is_add_only():
    current = [{"id": "a", "name": "here"}]
    changed, result = add_missing_folders(current, [_row("a", "x"), _row("b", "B")])
    assert changed and result.added == {"b"} and result.already_here == 1
    assert current[0] == {"id": "a", "name": "here"}
    assert [row["id"] for row in current] == ["a", "b"]
    assert add_missing_folders(current, [_row("b", "B")])[0] is False


def _row(folder_id: str, name: str, parent_id: str = "") -> dict:
    return {"id": folder_id, "name": name, "parent_id": parent_id}


def test_add_skips_a_name_its_parent_already_holds():
    current = [{"id": "local", "name": "Code channels", "parent_id": ""}]
    _changed, result = add_missing_folders(
        current, [_row("arch", " code CHANNELS "), _row("kid", "Kid", "arch")]
    )
    # One folder of a name per parent, so a channel that finds it by name sees one;
    # the skipped folder's child moves to the top level.
    assert result.added == {"kid"} and result.skipped == 1
    assert next(row for row in current if row["id"] == "kid")["parent_id"] == ""


def test_add_never_nests_into_a_member_home():
    home = {"id": "home", "name": "Mate", "parent_id": "", "mate_home": True, "owner_app": "m"}
    current = [home]
    add_missing_folders(current, [_row("home", "Mate"), _row("in", "In", "home")])
    assert next(row for row in current if row["id"] == "in")["parent_id"] == ""


def test_archive_rows_keep_only_create_fields(tmp_path):
    rows = [
        {
            "id": "f",
            "name": "  F  ",
            "parent_id": "f",
            "mate_home": True,
            "owner_app": "member:x",
            "channel": "slack",
            "created_by_session": "s",
            "steering_dirs": ["/etc"],
            "tags": ["t"],
            "color": "not-a-colour",
            "icon": "text",
            "order": 3,
        },
        {"id": "g", "name": {"nested": 1}},
        {"id": "f", "name": "second f"},
        "not a row",
    ]
    (tmp_path / "folders.json").write_text(json.dumps(rows))
    read = read_archive_folders(tmp_path)
    assert read is not None and read[1] == 2
    read = read[0]
    assert isinstance(read[0].pop("created_at"), float)
    assert read == [
        {
            "id": "f",
            "name": "F",
            "collapsed": False,
            "hidden": False,
            "parent_id": "",
            "default_agent": "",
            "default_destination_crew": "",
        }
    ]


def _rows(staged: Path) -> list[dict]:
    read = read_archive_folders(staged)
    assert read is not None
    return read[0]


def test_folders_past_the_cap_are_counted_and_reported(tmp_path):
    from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS

    rows = [_row(f"f{i}", f"F{i}") for i in range(MAX_CHAT_FOLDERS + 1)]
    (tmp_path / "folders.json").write_text(json.dumps(rows))
    summary: dict = {"items": []}

    import_folders(tmp_path, summary, lambda incoming: FolderMergeResult(added=frozenset({"x"})))

    assert "folders (skipped: 1 not added)" in summary["items"]
    assert summary["refused_merges"] == ["folders"]


def test_archive_numbers_too_big_for_a_float_are_ignored(tmp_path):
    (tmp_path / "folders.json").write_text(
        '[{"id": "f", "name": "F", "created_at": 1' + "0" * 400 + "}]"
    )
    assert _rows(tmp_path)[0]["created_at"] < 1e10


def test_archive_default_agent_must_be_an_agent_name(tmp_path):
    rows = [
        {"id": "a", "name": "A", "default_agent": " my-agent "},
        {"id": "b", "name": "B", "default_agent": "x" * 65},
    ]
    (tmp_path / "folders.json").write_text(json.dumps(rows))
    agents = [row["default_agent"] for row in _rows(tmp_path)]
    assert agents == ["my-agent", ""]


def test_a_parent_loop_is_cut_without_moving_the_row_above_it(tmp_path):
    rows = [_row("d", "D", "a"), _row("a", "A", "b"), _row("b", "B", "a")]
    (tmp_path / "folders.json").write_text(json.dumps(rows))
    parents = {row["id"]: row["parent_id"] for row in _rows(tmp_path)}
    assert parents == {"d": "a", "a": "", "b": "a"}


def test_archive_folders_with_nan_are_refused(tmp_path):
    (tmp_path / "folders.json").write_text('[{"id": "f", "name": "F", "order": NaN}]')
    with pytest.raises(ValueError):
        read_archive_folders(tmp_path)


def test_live_merge_goes_through_the_folder_store():
    class _State:
        def __init__(self) -> None:
            self.folders = [{"id": "a", "name": "A", "parent_id": ""}]

        async def mutate_folders(self, mutate):
            _changed, result = mutate(self.folders)
            return result

    state = _State()

    async def run():
        merge = live_folder_merge(state, asyncio.get_running_loop())
        return await asyncio.wait_for(asyncio.to_thread(merge, [_row("b", "B")]), 30)

    result = asyncio.run(run())
    assert result.added == {"b"}
    assert [row["id"] for row in state.folders] == ["a", "b"]


def test_unreadable_archive_folders_are_refused_by_name(homes, tmp_path, monkeypatch):
    _old_home, new_home = homes
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(_export(tmp_path)) as src, zipfile.ZipFile(zip_path, "w") as dst:
        for info in src.infolist():
            data = b"{}" if info.filename.endswith("/folders.json") else src.read(info)
            dst.writestr(info, data)
    target = _target(new_home, monkeypatch)

    summary = apply_import_zip(zip_path, mode="merge")

    assert "folders" in summary["refused_merges"]
    assert not (target / "folders.json").exists()
