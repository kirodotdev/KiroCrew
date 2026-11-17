"""What the rootdir conftest does to stop an xdist worker growing without bound.

The xdist budget divides RAM by a per-worker reservation, so what a worker retains
costs parallelism. These guards are invisible in a passing run.

The rootdir conftest is reached through the plugin manager: ``import conftest`` binds
``test/conftest.py`` under prepend import mode, and two guards keep live module state.
"""

from __future__ import annotations

import array
import gc
import linecache
import pathlib
import types
import warnings
import weakref

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def root_conftest(request: pytest.FixtureRequest):
    """The live rootdir ``conftest.py`` module object."""
    plugin = request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))
    assert plugin is not None, "the rootdir conftest is not registered as a plugin"
    return plugin


def _warning_message(source: object) -> warnings.WarningMessage:
    """A recorded warning carrying *source*, the shape pytest keeps for the session."""
    return warnings.WarningMessage(
        message=RuntimeWarning("coroutine 'x' was never awaited"),
        category=RuntimeWarning,
        filename=__file__,
        lineno=1,
        source=source,
    )


class TestARecordedWarningDoesNotPinItsSubject:
    """``WarningMessage.source`` is the object the warning is ABOUT.

    For an un-awaited coroutine that is the coroutine and its frame, and pluggy keeps
    ``call_historic`` kwargs for the whole run, so one warning pins a test's objects.
    A backstop for files dense in such warnings, not an average saving.
    """

    def test_the_source_reference_is_dropped(self, root_conftest) -> None:
        sentinel = ["a big object graph"]
        message = _warning_message(sentinel)

        root_conftest.pytest_warning_recorded(message, "runtest", "nodeid", None)

        assert message.source is None

    def test_it_runs_LAST_so_the_rendered_text_is_already_built(self, root_conftest) -> None:
        """The ordering is the whole reason this loses no diagnostics.

        ``TerminalReporter.pytest_warning_recorded`` renders
        ``tracemalloc_message(source)`` first; running before it would silently truncate
        every warning report.
        """
        opts = getattr(root_conftest.pytest_warning_recorded, "pytest_impl", None) or {}
        assert opts.get("trylast") is True, (
            "clearing source before TerminalReporter has rendered the warning would "
            "drop the tracemalloc suffix from every warning report"
        )

    def test_the_rendered_text_depends_on_source_which_is_why_order_matters(self) -> None:
        """Demonstrates the loss this ordering avoids, rather than asserting it abstractly."""
        from _pytest.warnings import warning_record_to_str

        with_source = warning_record_to_str(_warning_message(["subject"]))
        without_source = warning_record_to_str(_warning_message(None))

        assert with_source != without_source
        assert "tracemalloc" in with_source.lower()
        assert "tracemalloc" not in without_source.lower()

    def test_no_warning_retained_by_this_session_still_holds_its_subject(
        self, request: pytest.FixtureRequest
    ) -> None:
        """The end-to-end property, read off the live run rather than a fixture.

        Walks the historic-call kwargs pluggy is holding for this very session. Anything
        with a live ``source`` here is a retained object graph.

        Skips unless a warning carrying a source was recorded: sourceless warnings alone
        would let this pass with the hook removed.
        """
        caller = request.config.hook.pytest_warning_recorded
        history = getattr(caller, "_call_history", None) or []
        messages = [
            kwargs["warning_message"]
            for kwargs, _cb in history
            if kwargs.get("warning_message") is not None
        ]
        if not any(m.category is RuntimeWarning and "await" in str(m.message) for m in messages):
            pytest.skip(
                "this session recorded no warning that would carry a source; the "
                "assertion is only meaningful in a run that did"
            )

        assert [m for m in messages if m.source is not None] == []


class TestTheCollectedItemTreeIsFrozen:
    """Collection leaves ~3M objects alive for the session, and the GC rescans them.

    That static set also delays full passes. ``gc.freeze()`` moves it out of reach;
    over 3,798 executions full passes went 1 -> 3, reclaimed 61,604 -> 423,380.
    """

    def test_collection_froze_the_static_object_population(self, root_conftest) -> None:
        """Asserted on the DELTA the hook recorded, not on ``gc.get_freeze_count()``.

        The interpreter arrives with a few hundred objects already frozen (375 measured
        here), so ``get_freeze_count() > 0`` passes with the hook deleted -- a vacuous
        assertion that would have let a revert through silently. The delta cannot be
        anything but this hook's work.
        """
        frozen = root_conftest._FROZEN_AT_COLLECTION

        assert frozen is not None, "pytest_collection_finish did not run"
        assert frozen > 1000, (
            f"only {frozen} objects were frozen; the collected item tree should be "
            "far larger, and without it every gen-2 pass rescans a static population"
        )

    def test_a_cycle_a_test_allocates_is_still_collected(self) -> None:
        """The collector must still reclaim what a test creates, after the freeze.

        It catches a disabled or broken collector, not a leak hidden by freezing.

        Checked through a weakref: ``gc.get_freeze_count()`` drifts down as frozen
        objects die, so an equality check on it is order-dependent.
        """

        class _Node:  # a plain object, because dicts are not weak-referenceable
            pass

        node = _Node()
        node.self_ref = node  # a cycle only the collector can break
        witness = weakref.ref(node)
        del node

        gc.collect()

        assert witness() is None, (
            "a cycle allocated after the freeze was not collected; freezing must not "
            "extend to anything a test creates"
        )


class TestLinecacheIsBounded:
    """``linecache`` keeps the full TEXT of every source file anything has read.

    This suite has ~21 guard tests that deliberately read all of ``src/kiro_crew``
    (652k lines), and nothing evicts that. Measured at 21.8 MiB per worker.
    """

    @pytest.fixture(autouse=True)
    def _reset_counter(self, root_conftest, monkeypatch: pytest.MonkeyPatch) -> None:
        """Isolate the module-level counter, so these tests do not perturb the run."""
        monkeypatch.setattr(root_conftest, "_TESTS_SINCE_LINECACHE_CLEAR", 0)

    def test_the_cache_is_cleared_once_the_interval_is_reached(
        self, root_conftest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(linecache, "clearcache", lambda: calls.append(1))

        for _ in range(root_conftest._LINECACHE_CLEAR_EVERY):
            root_conftest.pytest_runtest_logfinish("nodeid", None)

        assert calls == [1]

    def test_the_cache_survives_until_the_interval(
        self, root_conftest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing every test would make the next traceback re-read its source."""
        calls: list[int] = []
        monkeypatch.setattr(linecache, "clearcache", lambda: calls.append(1))

        for _ in range(root_conftest._LINECACHE_CLEAR_EVERY - 1):
            root_conftest.pytest_runtest_logfinish("nodeid", None)

        assert calls == []

    def test_it_keeps_clearing_for_the_whole_run(
        self, root_conftest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter must RESET, or the cache is bounded exactly once.

        A worker runs thousands of tests, so a guard that fires on the first interval
        and never again would show up as a saving in any short measurement and none at
        all in a real run -- the failure mode this suite is most prone to mis-measuring.
        """
        calls: list[int] = []
        monkeypatch.setattr(linecache, "clearcache", lambda: calls.append(1))

        for _ in range(root_conftest._LINECACHE_CLEAR_EVERY * 3):
            root_conftest.pytest_runtest_logfinish("nodeid", None)

        assert calls == [1, 1, 1]


class TestCollectedFixtureBookkeepingIsShared:
    """Every item resolves ~130 fixtures, and pytest stores that resolution per item.

    Measured over a full collection (283,605 items), the per-item request copies and
    per-function lookup tuples and dicts were 2,971 MiB of a 5,295 MiB worker; sharing
    them took the collection peak to 2,957 MiB.
    """

    @pytest.fixture
    def fresh_tables(self, root_conftest, monkeypatch: pytest.MonkeyPatch):
        """Empty interning tables, so a direct call cannot reach the session's own."""
        for name in (
            "_SHARED_FIXTUREDEF_TUPLES",
            "_SHARED_INITIALNAMES",
            "_SHARED_NAME2FIXTUREDEFS",
            "_SHARED_NAMES_CLOSURES",
        ):
            monkeypatch.setattr(root_conftest, name, {})
        monkeypatch.setattr(root_conftest, "_COMPACTED_FIXTUREINFO", set())
        return root_conftest

    @staticmethod
    def _item(defs: dict[str, tuple], *, asyncio: bool = False):
        from types import SimpleNamespace

        from _pytest.fixtures import FuncFixtureInfo

        info = FuncFixtureInfo(
            argnames=(),
            initialnames=tuple(defs),
            names_closure=list(defs),
            name2fixturedefs=dict(defs),
        )
        mark = pytest.mark.asyncio.mark if asyncio else None
        return SimpleNamespace(
            _request=object(),
            _fixtureinfo=info,
            # pytest makes ``fixturenames`` the very list ``names_closure`` is.
            fixturenames=info.names_closure,
            obj=lambda: None,
            get_closest_marker=lambda name: mark if name == "asyncio" else None,
        )

    def test_two_functions_with_the_same_fixtures_share_one_lookup(self, fresh_tables) -> None:
        floor, home = object(), object()
        first = self._item({"floor": (floor,), "home": (home,)})
        # Equal content in DIFFERENT containers: what two test functions get from pytest.
        second = self._item({"floor": (floor,), "home": (home,)})

        fresh_tables.compact_collected_item(first)
        fresh_tables.compact_collected_item(second)

        a, b = first._fixtureinfo, second._fixtureinfo
        assert b.name2fixturedefs is a.name2fixturedefs
        assert b.initialnames is a.initialnames
        assert b.name2fixturedefs == {"floor": (floor,), "home": (home,)}

    def test_a_shared_lookup_is_keyed_by_one_packed_id_per_name(self, fresh_tables) -> None:
        """8 bytes per name: a ``(name, id)`` tuple per name cost 347 MiB of a collection.

        One value is a list, as pytest's direct parametrization leaves it.
        """
        item = self._item({"floor": (object(),), "home": [object()]})

        fresh_tables.compact_collected_item(item)

        (key,) = fresh_tables._SHARED_NAME2FIXTUREDEFS
        values = item._fixtureinfo.name2fixturedefs.values()
        assert key == array.array("Q", map(id, values)).tobytes()

    def test_a_different_resolution_keeps_its_own_lookup(self, fresh_tables) -> None:
        """Sharing is by identity of the FixtureDefs, so an override is never merged away."""
        floor = object()
        base = self._item({"floor": (floor,)})
        overridden = self._item({"floor": (floor, object())})

        fresh_tables.compact_collected_item(base)
        fresh_tables.compact_collected_item(overridden)

        assert overridden._fixtureinfo.name2fixturedefs is not base._fixtureinfo.name2fixturedefs
        assert len(overridden._fixtureinfo.name2fixturedefs["floor"]) == 2

    def test_two_functions_with_the_same_closure_share_one_list(self, fresh_tables) -> None:
        floor = object()
        first, second = self._item({"floor": (floor,)}), self._item({"floor": (floor,)})

        fresh_tables.compact_collected_item(first)
        fresh_tables.compact_collected_item(second)

        assert second._fixtureinfo.names_closure is first._fixtureinfo.names_closure
        assert second.fixturenames is first.fixturenames is first._fixtureinfo.names_closure

    def test_an_asyncio_test_keeps_its_own_closure_list(self, fresh_tables) -> None:
        """pytest-asyncio inserts ``event_loop`` into such a test's ``fixturenames``."""
        floor = object()
        plain = self._item({"floor": (floor,)})
        marked = self._item({"floor": (floor,)}, asyncio=True)

        fresh_tables.compact_collected_item(plain)
        fresh_tables.compact_collected_item(marked)

        assert marked.fixturenames is not plain.fixturenames
        assert marked.fixturenames is marked._fixtureinfo.names_closure

    def test_a_parametrized_sibling_follows_the_shared_list(self, fresh_tables) -> None:
        """Siblings share one ``FuncFixtureInfo``; each item's own attribute must move too."""
        floor = object()
        first = self._item({"floor": (floor,)})
        other_function = self._item({"floor": (floor,)})
        sibling = types.SimpleNamespace(
            _request=object(),
            _fixtureinfo=other_function._fixtureinfo,
            fixturenames=other_function._fixtureinfo.names_closure,
        )

        fresh_tables.compact_collected_item(first)
        fresh_tables.compact_collected_item(other_function)
        fresh_tables.compact_collected_item(sibling)

        assert sibling.fixturenames is first.fixturenames

    def test_the_request_is_dropped_to_the_state_pytest_rebuilds_from(self, fresh_tables) -> None:
        item = self._item({"floor": (object(),)})

        fresh_tables.compact_collected_item(item)

        assert item._request is False

    def test_no_collected_item_waits_with_a_request_built(self, request) -> None:
        """The hook is wired: only the running test holds a request.

        pytest itself sets ``_request = False`` after each test, so every OTHER item
        in this worker, run or not, must hold none; one that does means collection
        kept the copy.
        """
        holding = [
            item.nodeid
            for item in request.session.items
            if item is not request.node and getattr(item, "_request", False)
        ]

        assert holding == []

    def test_this_items_lookup_is_the_shared_one(self, request, root_conftest) -> None:
        """End to end on the running item, which a pytest upgrade renaming a field would break.

        Checked against the hook's own tables, so it holds however the run was selected.
        """
        info = request.node._fixtureinfo
        lookup = info.name2fixturedefs
        shared = root_conftest._SHARED_NAME2FIXTUREDEFS
        tuples = root_conftest._SHARED_FIXTUREDEF_TUPLES

        assert any(candidate is lookup for candidate in shared.values())
        assert all(tuples.get((name, *map(id, defs))) is defs for name, defs in lookup.items())
        closures = root_conftest._SHARED_NAMES_CLOSURES.values()
        assert any(closure is request.node.fixturenames for closure in closures)


class TestCollectionCostPerItem:
    """Every xdist worker collects its whole share before it runs a test, so the cost of one
    collected item is multiplied by every item and every worker.

    Budgeted per item rather than in total, so adding tests never fails it; a change that
    makes every item heavier does. Measured on the full Linux suite (283,605 items) at
    ~11 KiB per item once fixture bookkeeping is shared, and ~19 KiB before. The fixed
    part covers importing ``kiro_crew`` and the conftests, which a file shard pays too.
    """

    FIXED_MIB = 600
    PER_ITEM_KIB = 16
    MIN_ITEMS = 5_000

    def test_collection_stays_inside_its_per_item_budget(self, root_conftest) -> None:
        start = root_conftest._RSS_AT_CONFIGURE_KIB
        peak = root_conftest._HWM_AT_COLLECTION_KIB
        items = root_conftest._ITEMS_AT_COLLECTION
        if start is None or peak is None:
            pytest.skip("needs /proc/self/status")
        if items < self.MIN_ITEMS:
            pytest.skip(f"only {items} items collected; the budget is about large runs")

        spent_kib = peak - start
        budget_kib = self.FIXED_MIB * 1024 + self.PER_ITEM_KIB * items
        assert spent_kib <= budget_kib, (
            f"collecting {items} items took {spent_kib / 1024:.0f} MiB, over the "
            f"{budget_kib / 1024:.0f} MiB budget ({self.FIXED_MIB} MiB + "
            f"{self.PER_ITEM_KIB} KiB per item). Look for a new autouse fixture, a "
            "module-level payload, or lost fixture sharing (testing-conventions.md, "
            "Running on a machine with little RAM)."
        )

    def test_items_are_counted_as_collected_not_as_selected(
        self, root_conftest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-k``/``-m`` drop items after collection, but VmHWM already paid for them.

        A count taken from ``session.items`` would shrink the budget under ``-k`` and fail
        a healthy run, so each collected item counts once, in ``pytest_itemcollected``.
        """
        monkeypatch.setattr(root_conftest, "_ITEMS_AT_COLLECTION", 0)
        monkeypatch.setattr(root_conftest, "compact_collected_item", lambda _item: None)

        for _ in range(3):
            root_conftest.pytest_itemcollected(object())

        assert root_conftest._ITEMS_AT_COLLECTION == 3

    def test_the_hooks_recorded_what_the_budget_reads(self, root_conftest, request) -> None:
        if root_conftest._RSS_AT_CONFIGURE_KIB is None:
            pytest.skip("needs /proc/self/status")
        assert root_conftest._HWM_AT_COLLECTION_KIB >= root_conftest._RSS_AT_CONFIGURE_KIB
        # Counted before ``-k``/``-m`` deselection: every collected item is either still
        # selected or was deselected. A count overwritten from ``session.items`` after
        # deselection fails here under any ``-k``.
        selected = len(request.session.items)
        assert selected > 0
        assert root_conftest._ITEMS_AT_COLLECTION == (
            selected + root_conftest._DESELECTED_AT_COLLECTION
        )
