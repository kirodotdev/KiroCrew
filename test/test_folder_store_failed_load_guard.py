"""A folder store whose file fails to load at boot is never replaced by the next create.

``folders.json`` and ``cron_folders.json`` are hand-editable. A leading UTF-8
BOM loads like plain UTF-8. Any other failed load (a sharing violation on
Windows, a typo, a non-list root) marks the folder set unknown: the next write
re-reads the file, takes its rows when the read succeeds, and refuses without
writing when it does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers.cron_board import api_cron_folders_create

_BOM = b"\xef\xbb\xbf"
_FOLDERS = [{"id": "f1", "name": "One", "parent_id": ""}, {"id": "f2", "name": "Two"}]
_CRON = [{"id": "c1", "name": "C1", "order": 0}, {"id": "c2", "name": "C2", "order": 1}]


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return tmp_path


def _ids(path: Path) -> list[str]:
    return [row["id"] for row in json.loads(path.read_text(encoding="utf-8"))]


def _unreadable(rows: list[dict]) -> dict[str, bytes]:
    text = json.dumps(rows)
    return {
        "utf16le": text.encode("utf-16-le"),
        "typo": text.encode()[:-1],
        "non_list_root": json.dumps({"rows": rows}).encode(),
        # Deeper than the interpreter stack: json.loads raises RecursionError.
        "deep_nesting": b"[" * 100_000 + b"]" * 100_000,
    }


def _add_folder(folder_id: str):
    def _mutate(folders: list[dict]) -> tuple[bool, str]:
        folders.append({"id": folder_id, "name": folder_id, "parent_id": ""})
        return True, folder_id

    return _mutate


def _fail_reads_of(monkeypatch, name: str) -> None:
    """Make ``Path.read_text`` on *name* raise, as a Windows share-mode-0 holder does."""
    real = Path.read_text

    def _read_text(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)


# ── folders.json ──


@pytest.mark.asyncio
async def test_bom_folders_file_keeps_its_rows_on_create(cfg, tmp_path) -> None:
    (cfg / "folders.json").write_bytes(_BOM + json.dumps(_FOLDERS).encode())
    state = _make_state(tmp_path)
    state.load_folders()
    await state.mutate_folders(_add_folder("f3"))
    assert _ids(cfg / "folders.json") == ["f1", "f2", "f3"]


@pytest.mark.asyncio
async def test_folders_held_at_load_then_released_keeps_rows_on_create(
    cfg, tmp_path, monkeypatch
) -> None:
    (cfg / "folders.json").write_text(json.dumps(_FOLDERS), encoding="utf-8")
    state = _make_state(tmp_path)
    with monkeypatch.context() as held:
        _fail_reads_of(held, "folders.json")
        state.load_folders()
    assert state._folders == []
    await state.mutate_folders(_add_folder("f3"))
    assert _ids(cfg / "folders.json") == ["f1", "f2", "f3"]
    assert [f["id"] for f in state._folders] == ["f1", "f2", "f3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["utf16le", "typo", "non_list_root", "deep_nesting"])
async def test_unreadable_folders_file_refuses_create_and_keeps_bytes(cfg, tmp_path, kind) -> None:
    original = _unreadable(_FOLDERS)[kind]
    (cfg / "folders.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_folders()
    with pytest.raises(OSError):
        await state.mutate_folders(_add_folder("f3"))
    assert (cfg / "folders.json").read_bytes() == original
    assert state._folders == []


@pytest.mark.asyncio
async def test_folders_still_held_at_write_refuses_and_keeps_bytes(
    cfg, tmp_path, monkeypatch
) -> None:
    (cfg / "folders.json").write_text(json.dumps(_FOLDERS), encoding="utf-8")
    original = (cfg / "folders.json").read_bytes()
    state = _make_state(tmp_path)
    _fail_reads_of(monkeypatch, "folders.json")
    state.load_folders()
    with pytest.raises(OSError):
        await state.mutate_folders(_add_folder("f3"))
    state._folders.append({"id": "boot", "name": "boot"})
    state.save_folders()
    assert (cfg / "folders.json").read_bytes() == original


@pytest.mark.asyncio
async def test_missing_folders_file_still_creates_the_store(cfg, tmp_path) -> None:
    state = _make_state(tmp_path)
    state.load_folders()
    await state.mutate_folders(_add_folder("f1"))
    assert _ids(cfg / "folders.json") == ["f1"]


# ── cron_folders.json ──


def test_bom_cron_folders_file_keeps_its_rows_on_create(cfg, tmp_path) -> None:
    (cfg / "cron_folders.json").write_bytes(_BOM + json.dumps(_CRON).encode())
    state = _make_state(tmp_path)
    state.load_cron_folders()
    state.create_cron_folder("New", "c3")
    assert _ids(cfg / "cron_folders.json") == ["c1", "c2", "c3"]


def test_cron_folders_held_at_load_then_released_keeps_rows_on_create(
    cfg, tmp_path, monkeypatch
) -> None:
    (cfg / "cron_folders.json").write_text(json.dumps(_CRON), encoding="utf-8")
    state = _make_state(tmp_path)
    with monkeypatch.context() as held:
        _fail_reads_of(held, "cron_folders.json")
        state.load_cron_folders()
    assert state._cron_folders == []
    state.create_cron_folder("New", "c3")
    assert _ids(cfg / "cron_folders.json") == ["c1", "c2", "c3"]


@pytest.mark.parametrize("kind", ["utf16le", "typo", "non_list_root", "deep_nesting"])
@pytest.mark.parametrize("op", ["create", "rename", "delete"])
def test_unreadable_cron_folders_file_refuses_writes_and_keeps_bytes(
    cfg, tmp_path, kind, op
) -> None:
    original = _unreadable(_CRON)[kind]
    (cfg / "cron_folders.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_cron_folders()
    with pytest.raises(OSError):
        if op == "create":
            state.create_cron_folder("New", "c3")
        elif op == "rename":
            state.rename_cron_folder("c1", "Renamed")
        else:
            state.delete_cron_folder("c1")
    assert (cfg / "cron_folders.json").read_bytes() == original


@pytest.mark.asyncio
async def test_cron_folder_create_route_answers_500_on_an_unreadable_store(cfg, tmp_path) -> None:
    original = _unreadable(_CRON)["typo"]
    (cfg / "cron_folders.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_cron_folders()
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/cron-folders", api_cron_folders_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/cron-folders", json={"name": "New"})
        assert resp.status == 500
    assert (cfg / "cron_folders.json").read_bytes() == original


def test_missing_cron_folders_file_and_malformed_rows_still_round_trip(cfg, tmp_path) -> None:
    state = _make_state(tmp_path)
    state.load_cron_folders()
    state.create_cron_folder("First", "c1")
    assert _ids(cfg / "cron_folders.json") == ["c1"]
    bad = {"id": "bad", "name": ""}
    (cfg / "cron_folders.json").write_text(json.dumps([*_CRON, bad]), encoding="utf-8")
    state.load_cron_folders()
    state.create_cron_folder("New", "c3")
    assert _ids(cfg / "cron_folders.json") == ["c1", "c2", "c3", "bad"]


@pytest.mark.asyncio
async def test_adopt_by_name_after_a_held_load_reuses_the_folder_on_disk(
    cfg, tmp_path, monkeypatch
) -> None:
    """The app cron bridge matches by name: it must see the rows on disk, not the empty list."""
    from kiro_crew.dashboard.handlers.cron_board import create_or_adopt_cron_folder

    (cfg / "cron_folders.json").write_text(json.dumps(_CRON), encoding="utf-8")
    state = _make_state(tmp_path)
    with monkeypatch.context() as held:
        _fail_reads_of(held, "cron_folders.json")
        state.load_cron_folders()
    folder_id, error = await create_or_adopt_cron_folder(state, "C1")
    assert (folder_id, error) == ("c1", None)
    assert _ids(cfg / "cron_folders.json") == ["c1", "c2"]


@pytest.mark.asyncio
async def test_adopt_by_name_on_an_unreadable_store_fails_and_keeps_bytes(cfg, tmp_path) -> None:
    from kiro_crew.dashboard.handlers.cron_board import create_or_adopt_cron_folder

    original = _unreadable(_CRON)["typo"]
    (cfg / "cron_folders.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_cron_folders()
    folder_id, error = await create_or_adopt_cron_folder(state, "C1")
    assert folder_id == "" and error == "failed to save folder"
    assert (cfg / "cron_folders.json").read_bytes() == original
