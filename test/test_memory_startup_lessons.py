"""Cached and direct V1 JSONL / V2 SQLite lessons obey the store recovery fence."""

import dataclasses
import json

import pytest
from member_memory_helpers import env as _member_env

from kiro_crew.config import loader
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory_startup import MemoryStartup, MemoryStartupUnavailable
from kiro_crew.vector_memory import open_member_database

env = _member_env


def _fields(lessons: list[Lesson]) -> list[tuple]:
    """Lessons as field tuples, so the assertion is about DATA not class identity.

    ``Lesson`` is a plain dataclass, so its ``__eq__`` also requires
    ``other.__class__ is self.__class__``. On a macOS runner under xdist these
    comparisons failed with two operands whose reprs were CHARACTER-IDENTICAL down
    to every field -- the signature of one module imported under two identities
    (``/tmp`` is a symlink to ``/private/tmp`` there, so a path-derived
    ``sys.path`` entry can yield ``kiro_crew.learn`` twice), which makes the two
    ``Lesson`` classes distinct objects.

    What this test is about is WHICH lessons survived the recovery fence, and every
    field of every operand already matched -- so comparing field tuples asserts the
    behaviour under test and is immune to an import-graph artifact that says nothing
    about it. The double import itself is worth its own investigation; this
    comparison does not explain it and is not meant to.
    """
    return [dataclasses.astuple(lesson) for lesson in lessons]


@pytest.mark.parametrize("failed", ["default", "legacy", "member-alice"])
def test_failed_restore_fences_only_its_cached_and_new_lesson_store(env, failed):
    roots = {
        "default": env.home,
        "legacy": env.home / "memory_stores" / "legacy",
        "member-alice": env.home / "memory_stores" / "member-alice",
        "member-bob": env.home / "memory_stores" / "member-bob",
    }
    roots["legacy"].mkdir()
    config_path = env.home / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["memory_stores"]["legacy"] = {}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    stores = {name: LessonStore(base_dir=roots[name]) for name in ("default", "legacy")}
    stores.update({name: env.tiers[name] for name in ("member-alice", "member-bob")})
    original = Lesson("2026-09-08", "Keep the accepted decision", "knowledge")
    replacement = Lesson("2026-09-09", "Use deployment health checks", "knowledge")

    def read(name, store):
        return store.get_lessons() if name.startswith("member-") else _fields(store.load_all())

    def save(name, store, lesson):
        return (
            store.write_lesson(lesson.rule, lesson.category)
            if name.startswith("member-")
            else store.save(lesson)
        )

    def remove(name, store):
        return (
            store.delete_lesson(original.rule, exact=True)
            if name.startswith("member-")
            else store.remove(original.rule)
        )

    before = {}
    for name, store in stores.items():
        save(name, store, original)
        before[name] = read(name, store)
        assert len(before[name]) == 1
    startup = MemoryStartup.begin()
    try:
        startup.fail_store(failed, ValueError("staged recovery failed"))
        assert startup.complete()
        cached = stores[failed]
        if failed.startswith("member-"):
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                open_member_database(
                    roots[failed] / "memory.db", member_id="alice", store_id=failed
                )
            candidates = [cached]
        else:
            candidates = [cached, LessonStore(base_dir=roots[failed])]
        for store in candidates:
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                read(failed, store)
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                save(failed, store, replacement)
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                remove(failed, store)
            if not failed.startswith("member-"):
                with store._lock:
                    with pytest.raises(MemoryStartupUnavailable, match=failed):
                        store._write_all([replacement])
        for name, store in stores.items():
            if name != failed:
                assert read(name, store) == before[name]
                save(name, store, replacement)
                if name.startswith("member-"):
                    values = [json.loads(row["value_json"]) for row in read(name, store)]
                    rules = [
                        value["rule"] if isinstance(value, dict) else value for value in values
                    ]
                    assert sorted(rules) == sorted([original.rule, replacement.rule])
                else:
                    assert read(name, store) == _fields([original, replacement])
    finally:
        startup.stop()
        startup.release()
    assert read(failed, stores[failed]) == before[failed]
    for name in ("member-alice", "member-bob"):
        assert not (roots[name] / "lessons.jsonl").exists()
        with pytest.raises(ValueError, match="only in the member database"):
            LessonStore(base_dir=roots[name])


def test_preparing_gateway_does_not_create_lesson_file(env):
    root = env.home / "memory_stores" / "member-alice"
    store = env.tiers["member-alice"]
    assert store.count_lessons() == 0
    startup = MemoryStartup.begin()
    try:
        with pytest.raises(MemoryStartupUnavailable):
            store.write_lesson("Do not publish before recovery", "knowledge")
        with pytest.raises(MemoryStartupUnavailable):
            open_member_database(root / "memory.db", member_id="alice", store_id="member-alice")
        assert not (root / "lessons.jsonl").exists()
    finally:
        startup.stop()
        startup.release()
    assert store.count_lessons() == 0
