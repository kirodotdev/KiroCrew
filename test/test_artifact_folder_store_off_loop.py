"""Every artifact-folder-store call an HTTP handler makes runs off the loop.

``ArtifactFolderStore`` guards its whole API with one ``threading.Lock``, and
every mutating call — ``create``, ``rename``, ``reparent``, ``set_icon``,
``delete`` — holds that lock across ``_save()``: ``mkdir`` + ``mkstemp`` +
write + ``os.fsync`` + ``os.replace``. The handlers already push those
mutations into the shared executor, which is exactly what makes the READS
dangerous to leave inline: a read on the gateway loop can only take the lock
once the worker thread finishes somebody else's folder write, so the loop
stalls for the length of that filesystem write.

So the property pinned here is not "the read is fast" — it is *which thread
the store call actually ran on*. Each test wraps the real store methods,
records ``threading.get_ident()`` per call, and asserts no call landed on the
event loop's own thread. Recording inside the store (rather than asserting on
the handler's shape) keeps the tests true no matter how a handler reaches it.

Harness mirrors ``test_artifact_folder_handlers.py``: MagicMock requests and a
real store rooted under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactFolderStore, ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    _spawn_artifact_folder_icon_task,
    api_artifact_folder_create,
    api_artifact_folder_delete,
    api_artifact_folder_update,
    api_artifact_folders,
    api_artifact_set_folder,
)

#: Every read the handlers reach for. The mutators are already offloaded on
#: main; they are wrapped too so a regression that moves one back inline is
#: caught by the same assertion.
WATCHED = (
    "exists",
    "get",
    "breadcrumb",
    "resolve_path",
    "list_with_counts",
    "create",
    "rename",
    "set_icon",
    "delete",
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    store = ArtifactStore(root=tmp_path / "artifacts")
    fstore = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", fstore)
    return store, fstore


@pytest.fixture
def threads(monkeypatch) -> dict[str, list[int]]:
    """Record the thread ident of every watched folder-store call."""
    seen: dict[str, list[int]] = {}
    for name in WATCHED:
        original = getattr(ArtifactFolderStore, name)

        def _wrapper(self, *args, _name=name, _orig=original, **kwargs):
            seen.setdefault(_name, []).append(threading.get_ident())
            return _orig(self, *args, **kwargs)

        monkeypatch.setattr(ArtifactFolderStore, name, _wrapper)
    return seen


@pytest.fixture
def patch_restricted(monkeypatch):
    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


@pytest.fixture
def no_icon_task(monkeypatch):
    """The create/update handlers spawn a background icon task; it has its own
    test below and would otherwise add non-deterministic calls to the record."""
    monkeypatch.setattr(art_handlers, "_spawn_artifact_folder_icon_task", lambda *a, **k: None)


def _request(*, body: dict | None = None, match: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = {}
    encoded = json.dumps(body).encode() if isinstance(body, dict) else b""
    req.read = AsyncMock(return_value=encoded)
    req.app = {"state": MagicMock(), "_restricted_session": False}
    return req


def _assert_off_loop(seen: dict[str, list[int]], *expected: str) -> None:
    loop_thread = threading.get_ident()
    assert set(expected) <= set(seen), f"expected {expected} to be called, saw {sorted(seen)}"
    on_loop = {
        name: idents for name, idents in seen.items() if any(i == loop_thread for i in idents)
    }
    assert not on_loop, f"folder-store calls ran on the event loop thread: {sorted(on_loop)}"


@pytest.mark.asyncio
async def test_folder_list_reads_off_loop(stores, threads) -> None:
    _store, fstore = stores
    fstore.create("A")
    threads.clear()

    resp = await api_artifact_folders(_request())

    assert resp.status == 200
    _assert_off_loop(threads, "list_with_counts", "breadcrumb")


@pytest.mark.asyncio
async def test_folder_create_by_parent_id_reads_off_loop(
    stores, threads, patch_restricted, no_icon_task
) -> None:
    _store, fstore = stores
    parent = fstore.create("P")
    threads.clear()

    resp = await api_artifact_folder_create(
        _request(body={"name": "child", "parent_id": parent["id"]})
    )

    assert resp.status == 201
    # ``parent_id`` is the read-only resolver branch — the one main ran inline.
    _assert_off_loop(threads, "resolve_path", "create", "breadcrumb")


@pytest.mark.asyncio
async def test_folder_update_reads_off_loop(
    stores, threads, patch_restricted, no_icon_task
) -> None:
    _store, fstore = stores
    folder = fstore.create("F")
    threads.clear()

    resp = await api_artifact_folder_update(
        _request(body={"name": "renamed"}, match={"id": folder["id"]})
    )

    assert resp.status == 200
    _assert_off_loop(threads, "exists", "get", "rename", "breadcrumb")


@pytest.mark.asyncio
async def test_folder_delete_existence_check_off_loop(stores, threads, patch_restricted) -> None:
    _store, fstore = stores
    folder = fstore.create("F")
    threads.clear()

    resp = await api_artifact_folder_delete(_request(match={"id": folder["id"]}))

    assert resp.status == 200
    _assert_off_loop(threads, "exists", "delete")


@pytest.mark.asyncio
async def test_set_folder_by_id_reads_off_loop(stores, threads, patch_restricted) -> None:
    store, fstore = stores
    folder = fstore.create("F")
    art = store.create(name="a", content="x")
    threads.clear()

    resp = await api_artifact_set_folder(
        _request(body={"folder_id": folder["id"]}, match={"slug": art.slug})
    )

    assert resp.status == 200
    _assert_off_loop(threads, "resolve_path", "exists")


@pytest.mark.asyncio
async def test_icon_task_existence_check_off_loop(stores, threads, monkeypatch) -> None:
    _store, fstore = stores
    folder = fstore.create("F")

    async def _emoji(_state, _name):
        return "📁"

    monkeypatch.setattr(art_handlers, "generate_emoji_for_name", _emoji)
    threads.clear()

    req = _request()
    _spawn_artifact_folder_icon_task(req, folder["id"], "F")
    # Poll the RECORD, not the store: a ``fstore.get`` from this coroutine would
    # log the event loop's own thread and make the assertion below meaningless.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if "set_icon" in threads:
            break

    _assert_off_loop(threads, "exists", "set_icon")
    assert fstore.get(folder["id"])["icon"] == "📁"
