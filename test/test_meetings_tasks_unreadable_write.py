"""A task edit never saves over a ``tasks.json`` it could not read.

``tasks.json`` is written by the task-extractor agent, so a malformed file is a
real case. The display read shows it as an empty list. Every route that writes
the list back reads it through ``store.read_tasks_for_update``: a file that does
not parse is moved aside to ``tasks.json.corrupt-<utc>`` before the save, and a
read that fails refuses the write and leaves the file as it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    reset_module_state_fixture,
    root_fixture,
)
from meetings_routes_helpers import BASE

from kiro_crew.apps.builtins.meetings.backend import store

_MEETING = "standup"
_BAD = b'{"tasks": [{"id": "t1", "description": "keep me"}'  # truncated mid-write


def _tasks_file(root: Path) -> Path:
    return store.tasks_path(_MEETING, root)


def _asides(root: Path) -> list[Path]:
    return sorted(_tasks_file(root).parent.glob("tasks.json.corrupt-*"))


async def _init(client) -> None:
    resp = await client.post(f"{BASE}/meetings/{_MEETING}/init", json={})
    assert resp.status == 200


class TestCorruptTasksFileIsKept:
    @pytest.mark.asyncio
    async def test_add_moves_an_unparseable_file_aside_before_saving(self, app, root: Path):
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_bytes(_BAD)
            resp = await client.post(
                f"{BASE}/meetings/{_MEETING}/tasks", json={"description": "new one"}
            )
            assert resp.status == 200
        asides = _asides(root)
        assert len(asides) == 1
        assert asides[0].read_bytes() == _BAD
        saved = json.loads(_tasks_file(root).read_text(encoding="utf-8"))
        assert [t["description"] for t in saved["tasks"]] == ["new one"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b'[{"id": "t1", "description": "keep me"}]',
            b'{"items": [{"id": "t1", "description": "keep me"}]}',
            b'{"tasks": {"t1": {"description": "keep me"}}}',
        ],
        ids=["list", "no-tasks-key", "tasks-not-a-list"],
    )
    async def test_json_that_is_not_a_task_document_is_moved_aside(
        self, app, root: Path, body: bytes
    ):
        """Valid JSON that is not a task document reads as empty for display."""
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_bytes(body)
            resp = await client.post(
                f"{BASE}/meetings/{_MEETING}/tasks", json={"description": "new one"}
            )
            assert resp.status == 200
        asides = _asides(root)
        assert [a.read_bytes() for a in asides] == [body]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["patch", "delete"])
    async def test_edit_and_delete_keep_the_bytes(self, app, root: Path, method: str):
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_bytes(_BAD)
            if method == "patch":
                resp = await client.patch(
                    f"{BASE}/meetings/{_MEETING}/tasks",
                    json={"id": "t1", "fields": {"assignee": "Bob"}},
                )
            else:
                resp = await client.delete(f"{BASE}/meetings/{_MEETING}/tasks", json={"id": "t1"})
            assert resp.status == 404
        # Whatever the route answers, the agent's bytes are still on disk.
        assert [a.read_bytes() for a in _asides(root)] == [_BAD]

    @pytest.mark.asyncio
    async def test_a_failed_read_refuses_the_write(self, app, root: Path, monkeypatch):
        good = {"meeting_id": _MEETING, "tasks": [{"id": "t1", "description": "keep me"}]}
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_text(json.dumps(good), encoding="utf-8")
            before = _tasks_file(root).read_bytes()

            def _eio(_path, **_kwargs):
                raise OSError(5, "Input/output error")

            monkeypatch.setattr(store, "read_bytes_with_retry", _eio)
            resp = await client.post(
                f"{BASE}/meetings/{_MEETING}/tasks", json={"description": "new one"}
            )
            assert resp.status == 500
            assert (await resp.json())["code"] == "tasks_unreadable"
        assert _tasks_file(root).read_bytes() == before
        assert _asides(root) == []

    @pytest.mark.asyncio
    async def test_a_failed_move_aside_refuses_the_write(self, app, root: Path, monkeypatch):
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_bytes(_BAD)

            def _no_move(_src, _dst):
                raise PermissionError(13, "Permission denied")

            monkeypatch.setattr(store, "replace_with_retry", _no_move)
            resp = await client.post(
                f"{BASE}/meetings/{_MEETING}/tasks", json={"description": "new one"}
            )
            assert resp.status == 500
        assert _tasks_file(root).read_bytes() == _BAD


class TestByteOrderMark:
    @pytest.mark.asyncio
    async def test_a_bom_file_reads_and_keeps_its_tasks(self, app, root: Path):
        doc = {"meeting_id": _MEETING, "tasks": [{"id": "t1", "description": "keep me"}]}
        async with client_for(app) as client:
            await _init(client)
            _tasks_file(root).write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))
            listed = (await (await client.get(f"{BASE}/meetings/{_MEETING}/tasks")).json())["tasks"]
            assert [t["id"] for t in listed] == ["t1"]
            resp = await client.post(
                f"{BASE}/meetings/{_MEETING}/tasks", json={"description": "new one"}
            )
            assert resp.status == 200
        saved = json.loads(_tasks_file(root).read_text(encoding="utf-8"))
        assert [t["description"] for t in saved["tasks"]] == ["keep me", "new one"]
        assert _asides(root) == []


def test_a_missing_file_starts_empty_without_an_aside(root: Path):
    store.ensure_data_dirs(root)
    _tasks_file(root).parent.mkdir(parents=True, exist_ok=True)
    doc = store.read_tasks_for_update(_MEETING, root)
    assert doc["tasks"] == []
    assert _asides(root) == []
