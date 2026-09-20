"""Cold discovery overlaps I/O without dropping instructions or changing keys."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_dir_link
from kiro_crew import skills
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import paths
from kiro_crew.skill_search_index import SkillSearchIndex


def _skill(root: Path, key: str, *, always: bool = False) -> Path:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {key}\ndescription: {key} description\n"
        f"always: {'true' if always else 'false'}\n---\nInstruction for {key}\n",
        encoding="utf-8",
    )
    return path


def test_cold_metadata_reads_overlap_and_join_with_tail_pinned_instruction(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    for i in range(64):
        _skill(root, f"skill-{i:03}", always=i == 63)
    loader = skills.SkillsLoader(root, install_builtins=False, config=KiroCrewConfig())
    read = loader._read_enumerated_skill_bytes
    rendezvous = threading.Barrier(2)
    workers: set[threading.Thread] = set()
    read_keys: list[str] = []
    lock = threading.Lock()
    caller = ContextVar("skill-catalog-caller", default="missing")
    previous = caller.get()
    caller.set("session-scope")

    def recording(path, within, **kwargs):
        assert caller.get() == "session-scope"
        with lock:
            workers.add(threading.current_thread())
            read_keys.append(path.parent.name)
        if path.parent.name in {"skill-000", "skill-001"}:
            try:
                rendezvous.wait(timeout=5)
            except threading.BrokenBarrierError:
                pytest.fail("independent metadata reads did not overlap")
        return read(path, within, **kwargs)

    monkeypatch.setattr(loader, "_read_enumerated_skill_bytes", recording)
    monkeypatch.setattr(
        loader, "get_always_skills", lambda *a, **kw: pytest.fail("re-read the catalog for pins")
    )
    required: list[str] = []
    try:
        pointer = loader.get_context(budget=4950, discovery_only=True, required_parts_out=required)
    finally:
        caller.set(previous)

    assert "Instruction for skill-063" in "".join(required)
    assert "skill_search" in pointer
    assert set(read_keys) == {f"skill-{i:03}" for i in range(64)}
    assert len([key for key in read_keys if key != "skill-063"]) == 63
    workers.discard(threading.current_thread())  # pinned body delivery is on the caller
    assert len(workers) >= 2
    assert all(not worker.is_alive() for worker in workers)


def test_parallel_walk_preserves_depth_first_alias_winner_and_prunes_cycles(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    target = root / "a" / "nested"
    _skill(root, "a/nested")
    _skill(root, "b")
    _skill(root, ".pending/hidden")
    make_dir_link(root / "z-alias", target)
    make_dir_link(target / "cycle", root)
    rendezvous = threading.Barrier(2)
    real_gate = skills.is_sensitive_resolved_path
    names = {os.path.realpath(root / "b" / "SKILL.md"), os.path.realpath(target / "SKILL.md")}
    visited: set[str] = set()
    lock = threading.Lock()

    def gate(path):
        with lock:
            first = path in names and path not in visited
            visited.add(path)
        if first:
            try:
                rendezvous.wait(timeout=5)
            except threading.BrokenBarrierError:
                pytest.fail("independent directory probes did not overlap")
        return real_gate(path)

    monkeypatch.setattr(skills, "is_sensitive_resolved_path", gate)
    assert [key for key, _ in skills._iter_skill_files(root)] == ["a/nested", "b"]


def test_leaf_sensitive_skill_is_still_refused(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    good = _skill(root, "good")
    secret = _skill(root, "secret")
    monkeypatch.setattr(
        skills, "is_sensitive_resolved_path", lambda path: path == os.path.realpath(secret)
    )
    assert skills._iter_skill_files(root) == [("good", good)]


def test_failed_alias_does_not_hide_an_enumerable_target(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = _skill(root, "z-target")
    alias = root / "a-alias"
    make_dir_link(alias, skill.parent)
    scan = os.scandir

    def failing_alias_scan(path):
        if Path(path) == alias:
            raise OSError("alias enumeration unavailable")
        return scan(path)

    with monkeypatch.context() as patch:
        patch.setattr(skills.os, "scandir", failing_alias_scan)
        assert skills._iter_skill_files(root) == [("z-target", skill)]


def test_sensitive_directory_is_refused_before_enumerating_children(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    good = _skill(root, "good")
    secret = _skill(root, "secret")
    sensitive = os.path.realpath(secret.parent)
    scan = os.scandir

    def guarded_scan(path):
        assert os.path.realpath(path) != sensitive, "enumerated a sensitive directory"
        return scan(path)

    with monkeypatch.context() as patch:
        patch.setattr(skills, "is_sensitive_resolved_path", lambda path: path == sensitive)
        patch.setattr(skills.os, "scandir", guarded_scan)
        assert skills._iter_skill_files(root) == [("good", good)]


class _ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.waiting.set()
            self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


@pytest.mark.parametrize("retarget", [False, True])
def test_inline_rebuilds_coalesce_and_waiters_refresh_roots(tmp_path, monkeypatch, retarget):
    roots = paths._ResolvedRoots(
        home=str(tmp_path),
        logical_home=str(tmp_path),
        crew_home=None,
        kiro_home=None,
        adapter_roots=(),
        os_home=None,
    )
    current = [roots]
    entered = threading.Event()
    release = threading.Event()
    observed_lock = _ObservedLock()
    builds = []

    def build(home_dirs, resolved):
        builds.append(resolved)
        if len(builds) == 1:
            entered.set()
            assert release.wait(5)
        return {resolved.home}

    monkeypatch.setattr(paths, "_home_targets_cache", {})
    monkeypatch.setattr(paths, "_home_targets_inline_lock", observed_lock)
    monkeypatch.setattr(paths, "_resolve_root_anchors", lambda home: current[0])
    monkeypatch.setattr(paths, "_home_dir_targets_uncached", build)
    monkeypatch.setattr(paths, "time", SimpleNamespace(monotonic=lambda: 100.0))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(paths._home_dir_targets, [".ssh"], inline=True)
        try:
            assert entered.wait(5)
            second = pool.submit(paths._home_dir_targets, [".ssh"], inline=True)
            assert observed_lock.waiting.wait(5)
            if retarget:
                current[0] = roots._replace(home=str(tmp_path / "moved"))
        finally:
            release.set()
        assert first.result(timeout=5) == {roots.home}
        assert second.result(timeout=5) == {current[0].home}
    assert len(builds) == (2 if retarget else 1)


def test_bounded_gate_never_waits_for_inline_rebuild_lock(tmp_path, monkeypatch):
    roots = paths._ResolvedRoots(
        home=str(tmp_path),
        logical_home=str(tmp_path),
        crew_home=None,
        kiro_home=None,
        adapter_roots=(),
        os_home=None,
    )
    monkeypatch.setattr(paths, "_home_targets_cache", {})
    monkeypatch.setattr(paths, "_resolved_root_key", lambda: roots)
    monkeypatch.setattr(paths, "_rebuild_targets_bounded", lambda *args: {roots.home})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with paths._home_targets_inline_lock:
            future = pool.submit(paths._home_dir_targets, [".ssh"])
            assert future.result(timeout=5) == {roots.home}


@pytest.mark.parametrize("existing_cache", [False, True])
def test_metadata_refresh_work_does_not_scan_other_skills(tmp_path, existing_cache):
    index_path = tmp_path / "skills.sqlite3"
    index = SkillSearchIndex(index_path)
    db = index._db()
    assert db is not None
    db.executemany(
        "INSERT INTO skill_meta_term(term, path) VALUES (?, ?)",
        [(f"otherword{i:04}", f"other/{i}") for i in range(4096)],
    )
    assert index.store_metadata([("target", "before", {"name": "previousrare"})])
    if existing_cache:
        # Simulate the existing schema from before the access-path optimization.
        db.execute("DROP INDEX IF EXISTS skill_meta_term_path")
        db.commit()
        index.close()
        index = SkillSearchIndex(index_path)
        db = index._db()
        assert db is not None

    steps = 0

    def count_work():
        nonlocal steps
        steps += 100
        return 0

    db.set_progress_handler(count_work, 100)
    try:
        assert index.store_metadata([("target", "after", {"name": "replacementrare"})])
    finally:
        db.set_progress_handler(None, 0)
    assert steps < 5000, "one metadata refresh scanned the unrelated catalog vocabulary"
    assert index.metadata_matches(["previousrare"]) == {}
    assert index.metadata_matches(["replacementrare"]) == {"target": {"replacementrare"}}
    assert index.metadata_matches(["otherword4095"]) == {"other/4095": {"otherword4095"}}
    index.close()
