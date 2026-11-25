from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import agent_capabilities as handler
from kiro_crew.testing.wait import wait_until


class _Snapshot:
    def __init__(self, key: str, changed: list[bool] | None = None):
        self.entries = {key: Path(f"/{key}/SKILL.md")}
        self._changed = changed or [False]

    def changed(self) -> bool:
        return self._changed[0]


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    with handler._CATALOG_LOCK:
        handler._CATALOG_CACHE.clear()
    yield
    with handler._CATALOG_LOCK:
        handler._CATALOG_CACHE.clear()


def _patch_render(monkeypatch):
    monkeypatch.setattr(
        handler,
        "skill_uri_for_key",
        lambda key, _view, _catalog: f"skill://{key}",
    )


def _join(worker: threading.Thread) -> None:
    wait_until(
        lambda: not worker.is_alive(),
        describe=lambda: f"thread {worker.name!r} is still alive",
    )
    worker.join()


def test_stable_catalog_reuses_snapshot_and_reports_explicit_hit(monkeypatch):
    _patch_render(monkeypatch)
    walks = 0

    def walk(_view, _session):
        nonlocal walks
        walks += 1
        return _Snapshot("one")

    monkeypatch.setattr(handler, "walk_skill_catalog", walk)

    class Service:
        def get(self, _name):
            return {"skills": handler._catalog("project")}

    first = handler._get_with_cache_evidence(Service(), "A")
    second = handler._get_with_cache_evidence(Service(), "A")

    assert first["skills"] == second["skills"] == {"one": "skill://one"}
    assert first["cache"] == {"skill_catalog_reused": False}
    assert second["cache"] == {"skill_catalog_reused": True}
    assert walks == 1


def test_changed_snapshot_invalidates_once(monkeypatch):
    _patch_render(monkeypatch)
    stale = [False]
    snapshots = [_Snapshot("one", stale), _Snapshot("two")]
    walks = 0

    def walk(_view, _session):
        nonlocal walks
        snapshot = snapshots[walks]
        walks += 1
        return snapshot

    monkeypatch.setattr(handler, "walk_skill_catalog", walk)

    assert handler._catalog("project") == {"one": "skill://one"}
    stale[0] = True
    assert handler._catalog("project") == {"two": "skill://two"}
    assert handler._catalog("project") == {"two": "skill://two"}
    assert walks == 2


def test_catalog_lock_is_not_held_during_walk(monkeypatch):
    _patch_render(monkeypatch)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def walk(_view, _session):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        (first_entered if call == 1 else second_entered).set()
        wait_until(release.is_set)
        return _Snapshot(f"skill-{call}")

    monkeypatch.setattr(handler, "walk_skill_catalog", walk)
    results: list[dict[str, str]] = []
    workers = [
        threading.Thread(target=lambda: results.append(handler._catalog("project")))
        for _ in range(2)
    ]
    workers[0].start()
    wait_until(first_entered.is_set)
    workers[1].start()
    wait_until(
        second_entered.is_set,
        describe=lambda: "a filesystem walk held the catalog lock",
    )
    release.set()
    for worker in workers:
        _join(worker)
    assert len(results) == 2


def test_changed_check_after_competing_publish_does_not_hold_catalog_lock(monkeypatch):
    _patch_render(monkeypatch)
    slow_walk_entered = threading.Event()
    release_slow_walk = threading.Event()
    changed_entered = threading.Event()
    release_changed = threading.Event()

    class BlockingSnapshot(_Snapshot):
        def changed(self) -> bool:
            changed_entered.set()
            wait_until(release_changed.is_set)
            return False

    def walk(_view, _session):
        worker = threading.current_thread().name
        if worker == "slow-walker":
            slow_walk_entered.set()
            wait_until(release_slow_walk.is_set)
            return _Snapshot("slow")
        if worker == "publisher":
            return BlockingSnapshot("published")
        return _Snapshot("independent")

    monkeypatch.setattr(handler, "walk_skill_catalog", walk)
    results: dict[str, dict[str, str]] = {}
    slow = threading.Thread(
        name="slow-walker",
        target=lambda: results.setdefault("slow", handler._catalog("project")),
    )
    publisher = threading.Thread(
        name="publisher",
        target=lambda: results.setdefault("publisher", handler._catalog("project")),
    )
    slow.start()
    wait_until(slow_walk_entered.is_set)
    publisher.start()
    _join(publisher)

    release_slow_walk.set()
    wait_until(changed_entered.is_set)
    independent = threading.Thread(
        name="independent",
        target=lambda: results.setdefault("independent", handler._catalog("other-project")),
    )
    independent.start()
    _join(independent)
    assert results["independent"] == {"independent": "skill://independent"}

    release_changed.set()
    _join(slow)
    assert results["publisher"] == results["slow"] == {"published": "skill://published"}
