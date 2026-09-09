"""Cached and direct JSONL lesson access obeys the affected store's recovery fence."""

import dataclasses

import pytest
from member_memory_helpers import env as _member_env

from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory_startup import MemoryStartup, MemoryStartupUnavailable

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
    # No private manifest or vector tier: legacy is the named V1 JSONL path.
    roots["legacy"].mkdir()
    stores = {name: LessonStore(base_dir=root) for name, root in roots.items()}
    original = Lesson("2026-09-08", "Keep the accepted decision", "knowledge")
    replacement = Lesson("2026-09-09", "Replace the accepted decision", "knowledge")
    for store in stores.values():
        store.save(original)
        assert _fields(store.load_all()) == _fields([original])  # Populate the mtime cache.
    before = stores[failed].path.read_bytes()
    startup = MemoryStartup.begin()
    try:
        startup.fail_store(failed, ValueError("staged recovery failed"))
        assert startup.complete()
        for store in (stores[failed], LessonStore(base_dir=roots[failed])):
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                store.load_all()
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                store.save(replacement)
            with pytest.raises(MemoryStartupUnavailable, match=failed):
                store.remove(original.rule)
            # The persistence primitive also refuses before atomic replacement.
            with store._lock:
                with pytest.raises(MemoryStartupUnavailable, match=failed):
                    store._write_all([replacement])
        assert stores[failed].path.read_bytes() == before
        for name, store in stores.items():
            if name != failed:
                assert _fields(store.load_all()) == _fields([original])
                store.save(replacement)
                assert _fields(store.load_all()) == _fields([original, replacement])
    finally:
        startup.stop()
        startup.release()


def test_preparing_gateway_does_not_create_lesson_file(env):
    store = LessonStore(base_dir=env.home / "memory_stores" / "member-alice")
    assert not store.path.exists()
    startup = MemoryStartup.begin()
    try:
        with pytest.raises(MemoryStartupUnavailable):
            store.save(Lesson("2026-09-08", "Do not publish before recovery", "knowledge"))
        assert not store.path.exists()
    finally:
        startup.stop()
        startup.release()
