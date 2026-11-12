"""Notes delete and move, and a Mochi pack save, survive a file another handle holds.

On Windows a rename fails with ``PermissionError`` while any other handle is open on
the file (an indexer, a viewer, an editor). ``open()`` shares read and write but not
delete, which is exactly the hold such a program takes, so each test holds the real
file that way and drives the real rename.

The hold ends inside the retry: the retry's first backoff sleep closes the handle, so
the first rename attempt always meets the hold and a later one does not. No timer
decides the outcome. The over-budget tests never close the handle until the call
returns, and pin what a lost rename leaves behind.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import IO

import pytest
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _clone, signed_client

from kiro_crew import atomic_write as aw
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.mochi.appearance_store import (
    MANIFEST_FILE,
    appearances_dir,
    save_sprite_pack,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows refuses to rename a file another handle holds"
)

PNG = "data:image/png;base64," + base64.b64encode(b"fake-png-bytes").decode()


def _hold(path: Path) -> IO[bytes]:
    """Open *path* the way an indexer does: share read and write, not delete."""
    return open(path, "rb")  # noqa: SIM115 - the test closes it at a chosen moment


def _retry_clock(monkeypatch: pytest.MonkeyPatch, *, release: IO[bytes] | None) -> list[float]:
    """Route the retry's sleeps through a recorder that closes *release* on the first one.

    Returns the list of requested sleeps, so a test can show the first attempt really
    met the hold. With ``release=None`` nothing is closed and the sleeps are skipped, so
    the retry spends its whole attempt count at once.
    """
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if release is not None:
            release.close()

    monkeypatch.setattr(aw, "time", SimpleNamespace(sleep=_sleep, monotonic=time.monotonic))
    return sleeps


@pytest.mark.asyncio
async def test_delete_of_a_note_released_within_the_budget_lands_in_the_trash(
    fixtures, monkeypatch
) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        root = Path((await _clone(client, remote))["localPath"])
        before = (root / "One.md").read_bytes()
        handle = _hold(root / "One.md")
        try:
            sleeps = _retry_clock(monkeypatch, release=handle)
            status, body = await client.delete("/api/note?path=One.md")
        finally:
            handle.close()
        assert status == 200, body
        assert sleeps, "the first rename did not meet the hold, so nothing was retried"
        assert not (root / "One.md").exists()
        assert (root / git_ops.TRASH_DIR / "One.md").read_bytes() == before


@pytest.mark.asyncio
async def test_delete_held_past_the_budget_keeps_the_note_and_drops_the_placeholder(
    fixtures, monkeypatch
) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        root = Path((await _clone(client, remote))["localPath"])
        handle = _hold(root / "One.md")
        try:
            sleeps = _retry_clock(monkeypatch, release=None)
            status, body = await client.delete("/api/note?path=One.md")
        finally:
            handle.close()
        assert status == 500, body
        assert body["code"] == "io_failed"
        assert len(sleeps) == aw._REPLACE_MAX_ATTEMPTS - 1
        assert (root / "One.md").exists()
        assert list((root / git_ops.TRASH_DIR).iterdir()) == []


@pytest.mark.asyncio
async def test_move_of_a_note_released_within_the_budget_moves_it(fixtures, monkeypatch) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        root = Path((await _clone(client, remote))["localPath"])
        before = (root / "One.md").read_bytes()
        handle = _hold(root / "One.md")
        try:
            sleeps = _retry_clock(monkeypatch, release=handle)
            status, body = await client.post(
                "/api/note/move", {"from": "One.md", "to": "moved/One.md"}
            )
        finally:
            handle.close()
        assert status == 200, body
        assert sleeps, "the first rename did not meet the hold, so nothing was retried"
        assert not (root / "One.md").exists()
        assert (root / "moved" / "One.md").read_bytes() == before


@pytest.mark.asyncio
async def test_move_onto_an_existing_note_is_still_refused(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        root = Path((await _clone(client, remote))["localPath"])
        (root / "Taken.md").write_text("# taken\n", encoding="utf-8")
        status, body = await client.post("/api/note/move", {"from": "One.md", "to": "Taken.md"})
        assert status == 409, body
        assert body["code"] == "note_already_exists"
        assert (root / "Taken.md").read_text(encoding="utf-8") == "# taken\n"
        assert (root / "One.md").exists()


@pytest.mark.asyncio
async def test_move_held_past_the_budget_keeps_the_note_in_place(fixtures, monkeypatch) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        root = Path((await _clone(client, remote))["localPath"])
        handle = _hold(root / "One.md")
        try:
            sleeps = _retry_clock(monkeypatch, release=None)
            status, body = await client.post(
                "/api/note/move", {"from": "One.md", "to": "moved/One.md"}
            )
        finally:
            handle.close()
        assert status == 500, body
        assert len(sleeps) == aw._REPLACE_MAX_ATTEMPTS - 1
        assert (root / "One.md").exists()
        assert not (root / "moved" / "One.md").exists()


def _pack(name: str, **over: object) -> dict[str, object]:
    return {"name": name, "frameWidth": 32, "frameHeight": 32, "assignments": {"idle": PNG}, **over}


def _manifest_name(pack_dir: Path) -> str:
    return json.loads((pack_dir / MANIFEST_FILE).read_text(encoding="utf-8"))["meta"]["name"]


def test_pack_save_over_a_held_pack_file_released_within_the_budget_lands(
    tmp_path, monkeypatch
) -> None:
    pack_id = save_sprite_pack(tmp_path, _pack("old"))
    pack_dir = appearances_dir(tmp_path) / pack_id
    handle = _hold(pack_dir / MANIFEST_FILE)
    try:
        sleeps = _retry_clock(monkeypatch, release=handle)
        assert save_sprite_pack(tmp_path, _pack("new", overwriteId=pack_id)) == pack_id
    finally:
        handle.close()
    assert sleeps, "the first rename did not meet the hold, so nothing was retried"
    assert _manifest_name(pack_dir) == "new"
    assert sorted(p.name for p in appearances_dir(tmp_path).iterdir()) == [pack_id]


def test_pack_save_held_past_the_budget_leaves_the_old_pack_live(tmp_path, monkeypatch) -> None:
    pack_id = save_sprite_pack(tmp_path, _pack("old"))
    pack_dir = appearances_dir(tmp_path) / pack_id
    handle = _hold(pack_dir / MANIFEST_FILE)
    try:
        sleeps = _retry_clock(monkeypatch, release=None)
        with pytest.raises(PermissionError):
            save_sprite_pack(tmp_path, _pack("new", overwriteId=pack_id))
    finally:
        handle.close()
    assert len(sleeps) == aw._REPLACE_MAX_ATTEMPTS - 1
    assert _manifest_name(pack_dir) == "old"
    assert sorted(p.name for p in appearances_dir(tmp_path).iterdir()) == [pack_id]
