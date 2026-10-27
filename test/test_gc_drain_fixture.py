"""Verify that the module-scoped _drain_gc_cycles fixture clears cross-module GC cycles.

The autouse fixture calls gc.collect() twice after each test module ends, so a
self-referential cycle left by module A is freed before module B starts.  Two
inner test modules run via pytester prove the module-scope boundary works.
"""

from __future__ import annotations

import gc

import pytest

pytest_plugins = ("pytester",)


def test_module_scoped_gc_drain_clears_cross_module_cycles(
    pytester: pytest.Pytester,
) -> None:
    """A GC cycle left by module A is gone by the time module B's first test runs."""
    pytester.makeconftest("""\
        import gc
        import pytest

        @pytest.fixture(autouse=True, scope="module")
        def _drain_gc_cycles():
            yield
            gc.collect()
            gc.collect()
    """)

    pytester.makepyfile(
        test_a_cycle_source="""\
        import gc
        import weakref

        cycle_ref = None


        def test_leaves_a_cycle() -> None:
            global cycle_ref

            class _Tracked:
                back = None

            gc.disable()
            obj = _Tracked()
            obj.back = obj  # cycle: refcount stays above zero after del
            cycle_ref = weakref.ref(obj)
            del obj
            assert cycle_ref() is not None, "cycle must be alive at end of test"
    """,
    )

    pytester.makepyfile(
        test_b_cycle_consumer="""\
        import sys


        def test_cycle_is_cleared() -> None:
            mod = sys.modules.get("test_a_cycle_source")
            assert mod is not None, "test_a module must have been imported"
            assert mod.cycle_ref is not None, "test_a must have set cycle_ref"
            assert mod.cycle_ref() is None, (
                "_drain_gc_cycles did not clear the cross-module cycle; "
                "the module-scoped fixture may not have run"
            )
    """,
    )

    _was_gc_on = gc.isenabled()
    try:
        result = pytester.runpytest(
            "test_a_cycle_source.py",
            "test_b_cycle_consumer.py",
            "--override-ini=addopts=",
            "-v",
        )
    finally:
        if _was_gc_on:
            gc.enable()
    result.assert_outcomes(passed=2)
