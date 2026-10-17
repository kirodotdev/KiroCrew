"""What the rootdir conftest does to stop an xdist worker growing without bound.

A worker's memory is the constraint that decides how many of them fit, and therefore
how the suite behaves on a laptop: the xdist budget divides available RAM by a
per-worker reservation, so anything a worker retains costs parallelism directly. A
worker starts at ~750 MiB just from collecting every testpath, and then grows as
it runs -- measured at 293 MiB per 1,000 tests in the worst region.

Three mechanisms in the rootdir conftest bound that growth. Each is cheap, each is
invisible in a passing run, and each would be silently reverted by anyone who did not
know why it was there -- which is what these tests are for.

They reach the ROOTDIR conftest through the plugin manager, keyed on its absolute
path. That indirection is necessary, not stylistic: ``import conftest`` from a module
in ``test/`` binds ``test/conftest.py``, because pytest's prepend import mode puts each
test module's own directory on ``sys.path`` first. The plugin manager is also the only
way to get the LIVE module object, which matters because two of these guards keep
module-level state.
"""

from __future__ import annotations

import gc
import linecache
import pathlib
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

    For ``RuntimeWarning: coroutine '...' was never awaited`` that object is the
    coroutine, which holds its frame, which holds every local the test built. And every
    recorded warning is retained for the whole run, because pytest records them with
    ``call_historic`` and pluggy never clears a historic call's kwargs. So one
    un-awaited coroutine pins a whole test's object graph, in every worker, until the
    session ends.

    A BACKSTOP, not a general win, and the difference is measured. Where un-awaited
    coroutines cluster it is large: ``test_dashboard_chat.py`` emits 60 across 590
    tests, and clearing ``source`` takes that file's peak from 284 to 219 MiB. On a
    mixed slice it is worth nothing -- an AsyncMock-dense 3,401-test set emits 11 and
    measures +1.7 MiB, i.e. noise. So the value here is removing a tail risk for one
    attribute write per warning, not an average saving.
    """

    def test_the_source_reference_is_dropped(self, root_conftest) -> None:
        sentinel = ["a big object graph"]
        message = _warning_message(sentinel)

        root_conftest.pytest_warning_recorded(message, "runtest", "nodeid", None)

        assert message.source is None

    def test_it_runs_LAST_so_the_rendered_text_is_already_built(self, root_conftest) -> None:
        """The ordering is the whole reason this loses no diagnostics.

        ``TerminalReporter.pytest_warning_recorded`` renders the warning to a plain
        string immediately, and that string includes ``tracemalloc_message(source)`` --
        the "Object allocated at:" traceback when tracemalloc is tracing, and the
        "Enable tracemalloc" pointer when it is not. Running last means the text exists
        before the reference goes. Running FIRST would silently truncate every warning
        report, which no test failure would ever reveal.
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

        Skips unless the session has actually recorded a warning CARRYING a source --
        which is the precondition the assertion needs, not merely "some warning was
        recorded". Without that distinction this passed with the hook reverted, because a
        small session records plenty of sourceless warnings and none with a subject.
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

    Worse, on the interpreters this suite supports a full pass is scheduled from how
    much long-lived material exists, so that static population also DELAYS collection
    -- a test's own cyclic garbage waits longer to be reclaimed. (3.13 replaced the
    generational threshold with an incremental collector; the static set is unscanned
    either way, which is the part this depends on.)

    ``gc.freeze()`` moves the static set out of reach. Measured over 3,798 executions:
    full passes 1 -> 3, objects they reclaimed 61,604 -> 423,380.
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

        Named for what it checks. It does NOT prove that freezing cannot hide a leak
        -- freezing happens between tests, so a cycle created inside a test body is
        never frozen whichever way the hook is written, and a mutation that freezes
        mid-run does not fail this. What it does catch is the collector being disabled
        or broken outright, which is the way this guard could go wrong in practice.

        Asserted through a weakref rather than by comparing ``gc.get_freeze_count()``
        before and after. That count is NOT stable during a run -- frozen objects are
        still reclaimed by refcounting when they die, so it drifts downward on its own.
        An equality assertion on it passed in a short session and failed in the full
        suite, which is the order-dependence flake class these conventions warn about.
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
