"""A failed startup read of the session map must never erase the map on disk.

``SessionMap._load`` reads the file once, from the constructor, and every later
mutation rewrites the WHOLE file from memory. If an ``OSError`` on that read
(EIO, EACCES, or a Windows sharing violation from AV, an indexer or a sync
client) left
the map empty in memory, the next ordinary write would replace the file with
that empty map and drop every session_key -> sid binding.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from kiro_crew.session_map import SessionMap

_SEED = {
    "dashboard:alice": {"sid": "sid-ALICE", "slack_thread_ts": None, "slack_channel_id": None},
    "dashboard:bob": {"sid": "sid-BOB", "slack_thread_ts": None, "slack_channel_id": None},
}


@pytest.fixture()
def map_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    (tmp_path / "session_map.json").write_text(json.dumps(_SEED), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def unreadable_once(monkeypatch):
    """Fail the map read at the next ``SessionMap()`` load only."""
    armed = {"on": True}

    def flaky(self, *args, **kwargs):
        if self.name == "session_map.json" and armed.pop("on", False):
            raise OSError("EIO: handle held by a scanner")
        return real(self, *args, **kwargs)

    real = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text", flaky)


def _raw(map_dir: pathlib.Path) -> bytes:
    """The file's bytes, read without the patched ``Path`` methods."""
    with open(map_dir / "session_map.json", "rb") as handle:
        return handle.read()


def _assert_bindings_intact(map_dir: pathlib.Path) -> None:
    reloaded = SessionMap()
    assert reloaded._path == map_dir / "session_map.json"
    assert reloaded._data["dashboard:alice"]["sid"] == "sid-ALICE"
    assert reloaded._data["dashboard:bob"]["sid"] == "sid-BOB"


def test_a_write_after_a_failed_read_raises_and_leaves_the_file(map_dir, unreadable_once) -> None:
    before = _raw(map_dir)
    sm = SessionMap()
    assert sm._path == map_dir / "session_map.json"

    with pytest.raises(OSError, match="not readable at load"):
        sm.set_agent_override("dashboard:carol", "reviewer")

    assert _raw(map_dir) == before
    _assert_bindings_intact(map_dir)


@pytest.mark.asyncio
async def test_aflush_after_a_failed_read_fails_and_reowes_the_state(
    map_dir, unreadable_once
) -> None:
    """A persist-before-publish caller sees the failure, not a silent success."""
    before = _raw(map_dir)
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    task = sm._flush_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(OSError, match="not readable at load"):
        await sm.aflush()

    assert sm._dirty
    assert _raw(map_dir) == before
    _assert_bindings_intact(map_dir)


def test_corrupt_json_still_loads_empty_and_writes(map_dir) -> None:
    """Unchanged: an unparseable file is not a map worth keeping, so writes proceed."""
    (map_dir / "session_map.json").write_text("{not json", encoding="utf-8")
    sm = SessionMap()
    assert sm._data == {}
    sm.set_agent_override("dashboard:carol", "reviewer")
    sm.flush()
    on_disk = json.loads((map_dir / "session_map.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["dashboard:carol"]
