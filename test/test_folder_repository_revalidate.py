"""``FolderRepository.mutate``'s guarded transactions (``revalidate``).

A precondition the callback judged is re-decided after the confirmed write,
still under the lock. The previous list is recorded as a confirmed rollback
image BEFORE the speculative write, and :meth:`FolderRepository.load` prefers
that image, so a refused change can never become durable -- not through a
failed rollback, a cancellation, or a restart. Driven against real files.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.loop_lock import LoopBoundLock

BEFORE = [{"id": "f1", "name": "Org"}]
REFUSED = [{"id": "f1", "name": "Org", "steering_dirs": ["/srv/standards"]}]


def _json_writer(path: Path, rows: Any) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


class _Store:
    """A real folders.json plus a writer that can be made to fail or block."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "folders.json"
        _json_writer(self.path, BEFORE)
        self.folders: list[dict[str, Any]] = [dict(f) for f in BEFORE]
        self.fail_main_after = None  # fail main-file writes after the Nth
        self.main_writes = 0
        self.fail_all = False
        self.repo = FolderRepository(lambda: MagicMock())
        self.lock = LoopBoundLock()

    @property
    def image(self) -> Path:
        return FolderRepository.rollback_image_path(self.path)

    def disk(self) -> Any:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if path == self.path:
            self.main_writes += 1
            if self.fail_all or (
                self.fail_main_after is not None and self.main_writes > self.fail_main_after
            ):
                raise OSError("disk full")
        FolderRepository.write_confirmed(path, rows, _json_writer)

    async def run(self, revalidate, mutate=None, on_committed=None):
        def _add_steering(live):
            live[0]["steering_dirs"] = ["/srv/standards"]
            return True, None

        return await self.repo.mutate(
            lambda: self.folders,
            self.lock,
            mutate or _add_steering,
            lambda: self.path,
            self.write,
            on_committed,
            revalidate,
        )


@pytest.mark.asyncio
async def test_a_refusing_revalidate_rolls_back_memory_and_disk(tmp_path):
    store = _Store(tmp_path)
    committed: list[bool] = []

    value = await store.run(
        lambda: "steering_caller_changed", on_committed=lambda: committed.append(True)
    )

    assert value == "steering_caller_changed"
    assert store.folders == BEFORE
    assert store.disk() == BEFORE
    assert not store.image.exists()
    assert committed == []


@pytest.mark.asyncio
async def test_a_passing_revalidate_commits_and_leaves_no_image(tmp_path):
    store = _Store(tmp_path)
    committed: list[bool] = []

    value = await store.run(lambda: None, on_committed=lambda: committed.append(True))

    assert value is None
    assert store.disk() == REFUSED
    assert not store.image.exists(), "the commit is durable across a restart"
    assert committed == [True]


@pytest.mark.asyncio
async def test_a_rollback_that_cannot_land_is_restored_on_the_next_start(tmp_path):
    """The failure the fence exists for: the speculative write landed, every
    rollback write fails, the gateway restarts. The image wins at load."""
    store = _Store(tmp_path)
    store.fail_main_after = 1  # the speculative write lands, rollbacks fail

    value = await store.run(lambda: "steering_caller_changed")

    assert value == "steering_caller_changed"
    assert store.folders == BEFORE, "memory holds the restored list"
    assert store.disk() == REFUSED, "the main file kept the refused change"
    assert json.loads(store.image.read_text()) == BEFORE

    restarted = FolderRepository(lambda: MagicMock())
    assert restarted.load(store.path, []) == BEFORE


@pytest.mark.asyncio
async def test_the_next_mutation_settles_an_unsettled_rollback(tmp_path):
    store = _Store(tmp_path)
    store.fail_main_after = 1
    await store.run(lambda: "steering_caller_changed")
    store.fail_main_after = None

    await store.repo.mutate(
        lambda: store.folders,
        store.lock,
        lambda live: (False, None),
        lambda: store.path,
        store.write,
    )

    assert store.disk() == BEFORE
    assert not store.image.exists()


@pytest.mark.asyncio
async def test_a_transient_rollback_failure_is_retried(tmp_path):
    store = _Store(tmp_path)
    calls = {"n": 0}
    real = store.write

    def _once_flaky(path, rows):
        if path == store.path:
            calls["n"] += 1
            if calls["n"] == 2:  # the first rollback attempt
                raise OSError("disk hiccup")
        real(path, rows)

    store.write = _once_flaky  # type: ignore[method-assign]
    value = await store.run(lambda: "steering_caller_changed")

    assert value == "steering_caller_changed"
    assert store.disk() == BEFORE
    assert not store.image.exists()


@pytest.mark.asyncio
async def test_an_unrecordable_image_aborts_before_any_speculative_write(tmp_path):
    store = _Store(tmp_path)
    real = store.write

    def _no_image(path, rows):
        if path == store.image:
            raise OSError("no space for the image")
        real(path, rows)

    store.write = _no_image  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await store.run(lambda: None)

    assert store.folders == BEFORE
    assert store.disk() == BEFORE, "the main file was never touched"


@pytest.mark.asyncio
async def test_a_cancelled_rollback_is_drained_before_the_lock_is_released(tmp_path):
    """Cancelling the caller mid-rollback must not leave the worker to land its
    stale write after a newer edit took the lock."""
    store = _Store(tmp_path)
    rollback_started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    real = store.write
    main = {"n": 0}

    def _writer(path, rows):
        if path == store.path:
            main["n"] += 1
            if main["n"] == 2:  # the rollback write
                rollback_started.set()
                release.wait(5)
                order.append("rollback")
        real(path, rows)

    store.write = _writer  # type: ignore[method-assign]
    first = asyncio.ensure_future(store.run(lambda: "steering_caller_changed"))
    await asyncio.to_thread(rollback_started.wait, 5)
    first.cancel()

    def _newer(live):
        order.append("newer-edit")
        live[0]["name"] = "Renamed"
        return True, None

    second = asyncio.ensure_future(
        store.repo.mutate(
            lambda: store.folders, store.lock, _newer, lambda: store.path, store.write
        )
    )
    await asyncio.sleep(0.05)
    assert "newer-edit" not in order, "the lock is held until the rollback worker finishes"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert order.index("rollback") < order.index("newer-edit")
    assert store.disk() == [{"id": "f1", "name": "Renamed"}]
    assert not store.image.exists()


def test_an_unguarded_load_ignores_nothing_but_prefers_an_image(tmp_path):
    """No image: the main file is what loads, unchanged behaviour."""
    path = tmp_path / "folders.json"
    _json_writer(path, REFUSED)
    assert FolderRepository(lambda: MagicMock()).load(path, []) == REFUSED


@pytest.mark.asyncio
async def test_a_commit_whose_image_cannot_be_removed_is_not_published(tmp_path):
    """Persist before you publish: with the image stuck, a restart would restore
    the old list, so the change must not be reported as committed."""
    store = _Store(tmp_path)
    committed: list[bool] = []

    def _stuck(_path):
        raise OSError("image is read-only")

    store.repo._remove = _stuck  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await store.run(lambda: None, on_committed=lambda: committed.append(True))

    assert committed == [], "nothing published"
    assert store.folders == BEFORE, "memory matches what a restart restores"
    assert FolderRepository(lambda: MagicMock()).load(store.path, []) == BEFORE


@pytest.mark.asyncio
async def test_a_mutation_over_an_unsettled_store_is_refused(tmp_path):
    store = _Store(tmp_path)
    store.fail_main_after = 1
    await store.run(lambda: "steering_caller_changed")  # leaves the store unsettled
    applied: list[bool] = []

    def _rename(live):
        applied.append(True)
        live[0]["name"] = "Renamed"
        return True, None

    with pytest.raises(OSError):
        await store.repo.mutate(
            lambda: store.folders, store.lock, _rename, lambda: store.path, store.write
        )
    assert applied == [], "the mutation never ran"
    assert store.folders == BEFORE


@pytest.mark.asyncio
async def test_a_cancelled_speculative_write_is_drained_and_abandoned(tmp_path):
    store = _Store(tmp_path)
    started = threading.Event()
    release = threading.Event()
    real = store.write
    main = {"n": 0}

    def _writer(path, rows):
        if path == store.path:
            main["n"] += 1
            if main["n"] == 1:  # the speculative write
                started.set()
                release.wait(5)
        real(path, rows)

    store.write = _writer  # type: ignore[method-assign]
    task = asyncio.ensure_future(store.run(lambda: None))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.folders == BEFORE
    assert store.disk() == BEFORE, "the drained speculative write was settled back"
    assert not store.image.exists()


@pytest.mark.asyncio
async def test_an_image_left_by_a_cancelled_record_cannot_revert_later_edits(tmp_path):
    """Cancelled while the image is recorded (a client disconnect): the image
    lands VALID, and removing it fails once. It must not survive to be restored
    over a later ordinary edit at the next start."""
    store = _Store(tmp_path)
    started = threading.Event()
    release = threading.Event()
    real = store.write

    def _slow_image(path, rows):
        if path == store.image:
            started.set()
            release.wait(5)
        real(path, rows)

    store.write = _slow_image  # type: ignore[method-assign]
    real_remove = store.repo._remove
    removals = {"n": 0}

    def _remove_fails_once(path):
        removals["n"] += 1
        if removals["n"] == 1:
            raise OSError("image is busy")
        real_remove(path)

    store.repo._remove = _remove_fails_once  # type: ignore[method-assign]
    first = asyncio.ensure_future(store.run(lambda: None))
    await asyncio.to_thread(started.wait, 5)
    first.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert store.image.exists(), "the precondition: a valid image was left behind"
    assert store.folders == BEFORE

    def _rename(live):
        live[0]["name"] = "Renamed"
        return True, None

    await store.repo.mutate(
        lambda: store.folders, store.lock, _rename, lambda: store.path, store.write
    )

    assert not store.image.exists(), "the next mutation settled the store first"
    restarted = FolderRepository(lambda: MagicMock())
    assert restarted.load(store.path, []) == [{"id": "f1", "name": "Renamed"}]


def test_the_gateway_boot_loads_folders_off_the_event_loop():
    """``load`` may read and parse a rollback image as well as folders.json;
    both async boot paths must run it in a worker, never on the loop."""
    import ast

    import kiro_crew.dashboard.server as server

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    direct, offloaded = 0, 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "load_folders":
            direct += 1
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "load_folders"
        ):
            offloaded += 1
    assert direct == 0, "state.load_folders() is called on the event loop"
    assert offloaded == 2, "both boot paths offload the folder load"


@pytest.mark.asyncio
@pytest.mark.parametrize("image_text", ["{not json", '{"a": "dict, not a list"}'])
async def test_an_unreadable_image_fails_closed_instead_of_loading_the_refused_change(
    tmp_path, image_text
):
    """A rollback that could not land left the refused change in folders.json;
    if the image is then unreadable at the next start, the main file must not
    load, and no write may paper over it."""
    store = _Store(tmp_path)
    store.fail_main_after = 1
    await store.run(lambda: "steering_caller_changed")
    assert store.disk() == REFUSED, "the precondition: the refused change is on disk"
    store.image.write_text(image_text, encoding="utf-8")

    restarted = FolderRepository(lambda: MagicMock())
    assert restarted.load(store.path, []) == [], "folders.json is not loaded"

    def _rename(live):
        live.append({"id": "f2", "name": "New"})
        return True, None

    folders: list[dict[str, Any]] = []
    with pytest.raises(OSError):
        await restarted.mutate(
            lambda: folders, LoopBoundLock(), _rename, lambda: store.path, store.write
        )
    with pytest.raises(OSError):
        restarted.save(store.path, folders, _json_writer)
    assert folders == []
    assert store.disk() == REFUSED, "nothing was written over the unreadable image"

    store.image.unlink()
    assert restarted.load(store.path, []) == REFUSED, "a reload without the image unblocks"
    assert restarted._store_blocked is False
