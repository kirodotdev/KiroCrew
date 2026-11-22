"""The floor keeps a store a test caches in ``kiro_crew.context`` inside that test.

``ContextBuilder.__init__`` makes its own ``MemoryStore`` the process-wide default,
so without ``conftest_resets._isolate_context_store_caches`` a test that builds one
points every later test's default-store writes at its workspace. Driven on the real
fixture code for one setup -> undo cycle, so a single test observes the restore.
"""

from __future__ import annotations

import conftest_resets
import pytest

from kiro_crew import context


def _floor():
    definition = conftest_resets._isolate_context_store_caches
    marker = getattr(definition, "_fixture_function_marker", None) or getattr(
        definition, "_pytestfixturefunction", None
    )
    assert marker is not None and marker.autouse, "the floor is not an autouse fixture"
    return getattr(definition, "__wrapped__", definition)


def test_a_store_cached_during_a_test_is_gone_after_it_and_an_earlier_one_stays():
    earlier, during = object(), object()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(context._memory_stores, "kept-by-an-outer-fixture", earlier)
        floor = pytest.MonkeyPatch()
        _floor()(floor)
        context._memory_stores["default"] = during
        context._lesson_stores["default"] = during
        assert context._memory_stores["kept-by-an-outer-fixture"] is earlier
        floor.undo()
        assert context._memory_stores.get("default") is not during
        assert context._lesson_stores.get("default") is not during
        assert context._memory_stores["kept-by-an-outer-fixture"] is earlier
