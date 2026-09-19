"""The rootdir conftest gates pytest-asyncio 0.20.3's per-test fixture rescan.

pytest-asyncio 0.20.3 walks every registered fixture definition on every collected test
function to wrap the async ones. Fixtures it already wrapped are skipped by a set
lookup, but every synchronous one is re-inspected each time, so the cost is
O(tests x fixtures). Measured with cProfile on a ``--collect-only`` over this suite:
87,244 scans x ~1,420 fixtures = 123.8 million coroutine checks, 1,098 of 1,285
profiled seconds -- 85% of collection, paid in full by every xdist worker. With the
gate the same collection took 68 s instead of 478 s and produced the same 112,246
node ids.

The gate (``conftest._AsyncFixtureScanGate``) lets a scan run only while a fixture has
been registered since the last one. Every registration goes through
``FixtureManager._register_fixture``, which the conftest wraps to raise the flag.

These tests reach the rootdir conftest through the plugin manager, keyed on its
absolute path: ``import conftest`` from a module under ``test/`` binds
``test/conftest.py`` under pytest's prepend import mode.
"""

from __future__ import annotations

import pathlib
from unittest.mock import Mock

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)


@pytest.fixture
def root_conftest(request: pytest.FixtureRequest):
    """The live rootdir ``conftest.py`` module object."""
    plugin = request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))
    assert plugin is not None, "the rootdir conftest is not registered as a plugin"
    return plugin


class TestTheGateOnItsOwn:
    """Scan semantics in isolation: dirty -> one scan -> clean, until a registration."""

    def test_the_first_call_scans_and_later_calls_do_not(self, root_conftest) -> None:
        scan = Mock()
        gate = root_conftest._AsyncFixtureScanGate(scan)
        config, holder = object(), set()
        gate(config, holder)
        gate(config, holder)
        gate(config, holder)
        scan.assert_called_once_with(config, holder)
        assert gate.scans == 1
        assert gate.dirty is False

    def test_a_registration_re_arms_exactly_one_more_scan(self, root_conftest) -> None:
        scan = Mock()
        gate = root_conftest._AsyncFixtureScanGate(scan)
        gate(None, set())
        gate.mark_dirty()
        gate.mark_dirty()  # two registrations before the next test function: still one scan
        gate(None, set())
        gate(None, set())
        assert scan.call_count == 2
        assert gate.scans == 2

    def test_the_scan_sees_the_same_arguments_the_plugin_passed(self, root_conftest) -> None:
        seen: list[tuple[object, set]] = []
        gate = root_conftest._AsyncFixtureScanGate(lambda c, h: seen.append((c, h)))
        config, holder = object(), {"sentinel"}
        gate(config, holder)
        assert seen == [(config, holder)]

    def test_a_scan_that_raises_leaves_the_gate_dirty(self, root_conftest) -> None:
        """A failed scan must not be recorded as done, or the next call would skip it."""
        gate = root_conftest._AsyncFixtureScanGate(Mock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            gate(None, set())
        assert gate.dirty is True
        assert gate.scans == 0


class TestTheGateIsInstalledInThisSession:
    """The conftest's ``pytest_configure`` really replaced both seams in this process."""

    def test_pytest_asyncio_calls_the_gate(self, root_conftest) -> None:
        import pytest_asyncio.plugin as pa

        assert isinstance(pa._preprocess_async_fixtures, root_conftest._AsyncFixtureScanGate)

    def test_fixture_registration_raises_the_flag(self, root_conftest, request) -> None:
        import pytest_asyncio.plugin as pa
        from _pytest.fixtures import FixtureManager

        gate = pa._preprocess_async_fixtures
        assert getattr(FixtureManager._register_fixture, "__wrapped__", None) is not None
        # By the time a test RUNS, collection has finished and the flag has been consumed
        # by the last scan of collection -- unless a plugin registered a fixture later.
        gate.dirty = False
        fm = request.session._fixturemanager
        fm._register_fixture(
            name="_scan_gate_probe_fixture",
            func=lambda: None,
            nodeid=request.node.nodeid,
        )
        try:
            assert gate.dirty is True
        finally:
            fm._arg2fixturedefs.pop("_scan_gate_probe_fixture", None)

    def test_collection_ran_far_fewer_scans_than_test_functions(
        self, root_conftest, request
    ) -> None:
        """The whole point: scans track registrations (modules), not test functions."""
        import pytest_asyncio.plugin as pa

        gate = pa._preprocess_async_fixtures
        collected = len(request.session.items)
        # Every module registers at least its own fixtures, so scans ~= modules collected;
        # without the gate the plugin scans once per test FUNCTION. A 3x margin keeps
        # this true even for a single-file run where most items share one module.
        assert gate.scans <= max(3, collected // 3 + 3), (gate.scans, collected)

    def test_a_moved_private_seam_warns_instead_of_crashing_collection(
        self, root_conftest, monkeypatch
    ) -> None:
        """Both seams are private; a pytest/pytest-asyncio bump must degrade, not abort."""
        import pytest_asyncio.plugin as pa
        from _pytest.fixtures import FixtureManager

        before_gate = pa._preprocess_async_fixtures
        before_register = FixtureManager._register_fixture
        monkeypatch.delattr(FixtureManager, "_register_fixture")
        monkeypatch.setattr(pa, "_preprocess_async_fixtures", lambda config, holder: None)
        with pytest.warns(RuntimeWarning, match="fixture-scan gate not installed"):
            root_conftest._gate_pytest_asyncio_fixture_scan()
        assert not isinstance(pa._preprocess_async_fixtures, root_conftest._AsyncFixtureScanGate)
        monkeypatch.undo()
        assert pa._preprocess_async_fixtures is before_gate
        assert FixtureManager._register_fixture is before_register

    def test_installing_twice_is_a_no_op(self, root_conftest) -> None:
        import pytest_asyncio.plugin as pa
        from _pytest.fixtures import FixtureManager

        before_gate = pa._preprocess_async_fixtures
        before_register = FixtureManager._register_fixture
        root_conftest._gate_pytest_asyncio_fixture_scan()
        assert pa._preprocess_async_fixtures is before_gate
        assert FixtureManager._register_fixture is before_register


class TestAsyncFixturesStillWrap:
    """Behaviour preserved: a fixture registered AFTER the gate went clean is still wrapped.

    This suite has no async fixtures of its own -- the pinned pytest-asyncio 0.20.3 cannot
    drive one under pytest >= 8.1 (its wrapper reads ``fixturedef.unittest``), so the suite
    avoids them by convention. The proof therefore stops at the wrap: run a small inner
    session through ``pytester`` in-process (same patched plugin module, same gate), and
    check the plugin processed the new fixture -- it is in ``_HOLDER`` and carries the
    ``request``/``event_loop`` argnames the wrap injects.
    """

    def test_a_late_registered_async_fixture_is_still_wrapped(
        self, pytester: pytest.Pytester
    ) -> None:
        import pytest_asyncio.plugin as pa

        pytester.makepyfile("""
            import asyncio
            import pytest
            import pytest_asyncio

            @pytest_asyncio.fixture
            async def answer():
                await asyncio.sleep(0)
                return 42

            @pytest.mark.asyncio
            async def test_uses_the_async_fixture(answer):
                assert answer == 42
            """)
        gate = pa._preprocess_async_fixtures
        gate.dirty = False  # the state a clean gate is in once outer collection is over
        before = set(pa._HOLDER)
        result = pytester.runpytest_inprocess(
            "-p", "no:cacheprovider", "-n0", "-o", "addopts=", "--collect-only", "-q"
        )
        result.stdout.fnmatch_lines(["*test_uses_the_async_fixture*"])
        wrapped = {fd.argname: fd for fd in set(pa._HOLDER) - before}
        assert "answer" in wrapped, sorted(wrapped)
        assert {"request", "event_loop"} <= set(wrapped["answer"].argnames)
