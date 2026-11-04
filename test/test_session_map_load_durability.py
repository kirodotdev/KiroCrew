"""A failed startup read of the session map must never erase the map on disk.

``SessionMap._load`` reads the file once, from the constructor, and every later
mutation rewrites the WHOLE file from memory. If an ``OSError`` on that read
(EIO, EACCES, or a Windows sharing violation from AV, an indexer or a sync
client) left
the map empty in memory, the next ordinary write would replace the file with
that empty map and drop every session_key -> sid binding.

A file that reads but is not usable JSON starts the map empty too; its bytes are
moved to ``session_map.json.corrupt-<utc>`` by the first write, and one leading
UTF-8 byte-order mark reads normally.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from kiro_crew import session_map_store
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


def _corrupt_copies(map_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(map_dir.glob("session_map.json.corrupt-*"))


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b"\xff\xfe not utf-8"],
    ids=["not-json", "a-list", "not-utf8"],
)
def test_an_unparseable_file_is_kept_aside_before_the_first_write(map_dir, content) -> None:
    """Writes proceed from an empty map, and the old bytes survive in a ``.corrupt-`` copy."""
    (map_dir / "session_map.json").write_bytes(content)
    sm = SessionMap()
    assert sm._data == {}
    assert _corrupt_copies(map_dir) == []

    sm.set_agent_override("dashboard:carol", "reviewer")
    sm.flush()

    copies = _corrupt_copies(map_dir)
    assert len(copies) == 1
    with open(copies[0], "rb") as handle:
        assert handle.read() == content
    on_disk = json.loads((map_dir / "session_map.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["dashboard:carol"]

    sm.set_agent_override("dashboard:dave", "reviewer")
    sm.flush()
    assert len(_corrupt_copies(map_dir)) == 1


def test_a_read_only_instance_never_moves_an_unparseable_file(map_dir) -> None:
    (map_dir / "session_map.json").write_bytes(b"{not json")
    SessionMap()
    assert _raw(map_dir) == b"{not json"
    assert _corrupt_copies(map_dir) == []


def test_a_bom_file_keeps_every_binding_on_the_next_write(map_dir) -> None:
    (map_dir / "session_map.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(_SEED).encode())
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    sm.flush()

    _assert_bindings_intact(map_dir)
    assert _corrupt_copies(map_dir) == []


def test_a_failed_move_aside_refuses_the_write_until_one_succeeds(map_dir, monkeypatch) -> None:
    (map_dir / "session_map.json").write_bytes(b"{not json")
    sm = SessionMap()
    real = session_map_store.replace_with_retry

    def refuse(src, dst, **kwargs):
        raise PermissionError("EACCES: rename refused")

    monkeypatch.setattr(session_map_store, "replace_with_retry", refuse)
    with pytest.raises(PermissionError):
        sm.set_agent_override("dashboard:carol", "reviewer")
    assert _raw(map_dir) == b"{not json"
    assert _corrupt_copies(map_dir) == []

    monkeypatch.setattr(session_map_store, "replace_with_retry", real)
    sm.flush()
    copies = _corrupt_copies(map_dir)
    assert len(copies) == 1
    with open(copies[0], "rb") as handle:
        assert handle.read() == b"{not json"
    on_disk = json.loads((map_dir / "session_map.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["dashboard:carol"]
