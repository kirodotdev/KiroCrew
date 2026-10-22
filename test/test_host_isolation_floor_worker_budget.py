"""The isolation floor's worker budget: how many xdist workers the host can back.

Cores alone oversubscribe and total RAM alone is the machine's number, not what this
process may use. These tests pin where the budget is registered (the rootdir conftest,
so every testpath gets it), how ``xdist_budget.py`` combines its memory readings (each
one optional, the tightest wins, a starved host bounded, an unknown reading never
collapsing the run), how it reads ``/proc/meminfo`` and the cgroup limit, and that a tiny
host still gets one worker. The other floor guards are in ``test_host_isolation_floor.py``
and the between-test restores in ``test_host_isolation_floor_restores.py``; the repo-root
anchor comes from ``host_isolation_floor_helpers.py``.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from host_isolation_floor_helpers import (
    _REPO_ROOT,
)

# ── the worker budget ─────────────────────────────────────────────────────


class TestTheWorkerBudgetIsMemoryBounded:
    """How many xdist workers the host can actually back, not just how many cores.

    Cores alone oversubscribe: two worktrees each taking 10 workers on a 10-core box
    once produced a load average of ~590 with zero tests completing in 21 minutes. But
    TOTAL RAM alone is also the wrong number -- it is the MACHINE's, so it over-reports
    inside a memory-capped container and says nothing about a host already using most
    of its memory for something else.

    The failure mode that matters most here is the inverted one: a reading that comes
    back wrongly SMALL collapses the whole run to one worker, which looks like a hang
    rather than a bug. So each reading must degrade to "skip this bound", never to zero.
    """

    #: The per-worker reservation is now platform-aware (3 GiB on Linux/Windows,
    #: 16 GiB on macOS), so an assertion here computed against the live selector
    #: would read one number on Linux and another on macOS. These tests are about
    #: the shared division MECHANISM, not the platform default that feeds it, so
    #: the autouse fixture below forces the selectors to the Linux/Windows
    #: constants on every platform; each assertion then derives its expected
    #: count from ``_GIB_PER_WORKER`` directly. The platform default itself is
    #: covered by ``test_xdist_host_budget.py::test_per_worker_reservation_is_platform_aware``.

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module undoes the shared monkeypatch
    def _pin_per_worker_reservation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_gib_per_worker", lambda: budget._GIB_PER_WORKER)
        monkeypatch.setattr(
            budget, "_gib_per_worker_available", lambda: budget._GIB_PER_WORKER_AVAILABLE
        )

    def test_the_budget_is_registered_from_the_rootdir_not_from_test_conftest(self) -> None:
        """The gap that was silent for every testpath but ``test/``.

        ``test/conftest.py`` is not loaded when the target is ``transfer`` or an
        in-package app suite, so a budget registered there resolved ``-n auto`` to
        the raw core count for those invocations -- and ignored every knob, with
        nothing in the output to say so, because an absent budget is
        indistinguishable from a budget that chose not to clamp.

        Asserted against the SOURCE of both files, so it holds however the run was
        invoked: a test that inspected only the live plugin manager would pass from
        ``test/`` even after a regression put the hook back in the wrong file.
        """
        root_conftest = (_REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
        # test/conftest.py binds most of its definitions from plain modules beside it.
        floor = set((_REPO_ROOT / "test").glob("conftest_floor_*.py"))
        parts = sorted(set((_REPO_ROOT / "test").glob("conftest_*.py")) - floor)
        suite_conftest = "\n".join(
            p.read_text(encoding="utf-8") for p in (_REPO_ROOT / "test" / "conftest.py", *parts)
        )
        hook = "def pytest_xdist_auto_num_workers"

        assert hook in root_conftest, (
            "the worker budget must be registered from the ROOTDIR conftest, which is "
            "the only one every testpath loads"
        )
        assert hook not in suite_conftest, (
            "a budget registered in test/conftest.py is absent from transfer/ and from "
            "the in-package app suites, and the absence is silent"
        )

    def test_the_budget_module_is_importable_from_every_testpath(self) -> None:
        """The repository root is on ``sys.path`` for every invocation shape.

        That is what lets one module serve both conftests. It holds because pytest
        imports the rootdir conftest in ``prepend`` mode -- but it is load-bearing
        and invisible, so it is pinned rather than assumed.
        """
        import xdist_budget

        assert pathlib.Path(xdist_budget.__file__).parent == _REPO_ROOT
        assert callable(xdist_budget.resolve_workers)
        assert callable(xdist_budget.release_worker_slots)

    @pytest.fixture
    def slot_dir_env(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
        """Point the host-global slot directory at a tmp dir, and release what is taken.

        Mirrors ``test_xdist_host_budget.py``'s ``slot_dir``, and both halves matter. The
        real slot directory is under ``~/.cache``, shared with every other run on the
        machine, so a test that claims slots must not compete there. And a claim HOLDS an
        open file descriptor for the process lifetime by design -- that is how the kernel
        owns the lease -- so the descriptors have to be closed here or the test keeps real
        capacity for the rest of the session, and on Windows the open handle also blocks
        ``tmp_path`` cleanup and fails teardown.

        ``_held_slots`` is REPLACED rather than cleared, so the suite's own list is never
        touched and ``monkeypatch`` restores it even if the test fails.
        """
        import xdist_budget as budget

        monkeypatch.setenv(budget._SLOT_DIR_ENV, str(tmp_path / "slots"))
        held: list[int] = []
        monkeypatch.setattr(budget, "_held_slots", held)
        yield tmp_path
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_every_reading_is_optional_and_the_tightest_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 64)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 8 * 1024)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        # The 8 GiB cgroup ceiling is the tightest real reading, and the
        # unavailable one (0) is skipped rather than read as "no memory".
        # Derived from the reservation rather than restated: the constant tracks a
        # remeasured per-worker footprint and has moved (2 -> 3 GiB when the
        # collection floor doubled), so a literal here pins the wrong thing and
        # goes red for a reason that is not this test's subject.
        assert budget._static_memory_bounded_capacity(32) == 8 // budget._GIB_PER_WORKER

    def test_a_starved_host_is_bounded_rather_than_read_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inversion this unit choice exists to prevent.

        In whole GiB, 860 MiB free truncates to 0 -- indistinguishable from "could not
        determine", which is SKIPPED. The live bound would therefore drop out on exactly
        the starved host it protects, leaving the static total-RAM term to allow 16
        workers on under a gigabyte of free memory.
        """
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_available_mib", lambda: 860)

        assert budget._live_memory_bounded_cap(32) == 1

    def test_a_small_container_ceiling_is_not_read_as_no_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same inversion, reached through the cgroup reading instead."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 256)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 512)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        assert budget._static_memory_bounded_capacity(32) == 1

    def test_the_static_bound_shapes_the_shared_range_not_just_this_run(
        self, monkeypatch: pytest.MonkeyPatch, slot_dir_env: pathlib.Path
    ) -> None:
        """The memory budget is SHARED between concurrent runs, not granted to each.

        This is the property that decides where each bound goes. A 64-core / 32 GiB host
        can back only as many workers as the reservation allows, so that many SLOTS must
        exist in total -- a first run takes them all and a second gets its floor. Put the
        static bound only on the per-run cap and both runs take the full share each:
        double the workers against a single budget, which is the swapping incident the
        budget exists to prevent, reached from the other end.

        The expected count is DERIVED from the reservation, not restated. The constant
        tracks a remeasured per-worker footprint and has already moved once (2 -> 3 GiB
        when the collection floor doubled); a literal would fail here for a reason that
        has nothing to do with where the bound is applied, which is the only thing this
        test is about.
        """
        import xdist_budget as budget

        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(budget, "_host_total_gib", lambda: 32)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)
        monkeypatch.delenv(budget._MAX_WORKERS_ENV, raising=False)
        # Kiro Crew seeds this cap at every agent spawn boundary; a run started
        # from an agent shell would otherwise read the spawner's cap here.
        monkeypatch.delenv(budget._XDIST_ENV_CAP, raising=False)

        expected = 32 // budget._GIB_PER_WORKER
        assert expected < 64, "the memory bound must be the tighter one for this to mean anything"
        first = budget.resolve_workers()
        # A second run in this same process cannot re-lock what it already holds, so the
        # slot RANGE is what the assertion has to pin: the memory-backed share, never 64.
        assert first == expected
        assert budget._static_memory_bounded_capacity(64) == expected

    def test_the_live_bound_does_not_shrink_the_shared_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: a transient reading must not reshape the namespace.

        Slots fill from index 0, so a range shortened by a momentary dip excludes exactly
        the slots an earlier run left free -- collapsing the later run while the machine
        idles.
        """
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 128)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 2048)

        assert budget._static_memory_bounded_capacity(32) == 32

    def test_an_unavailable_reading_never_collapses_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS has no /proc/meminfo and no /sys/fs/cgroup; Windows raises on
        ``os.sysconf``. All three readings returning 0 must leave the core count
        standing."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 0)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        assert budget._static_memory_bounded_capacity(12) == 12

    def test_a_tiny_host_still_gets_one_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Slow beats stalled: the floor is one worker, never zero."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 1)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 1024)

        assert budget._static_memory_bounded_capacity(8) == 1

    @pytest.mark.parametrize(
        ("kb", "expected"),
        [
            (99_328_704, 97_000),  # a large host, in whole MiB
            (8_388_608, 8192),
            (1_048_576, 1024),  # exactly 1 GiB
            (900_000, 878),  # under 1 GiB: a REAL reading, not the unknown sentinel
            (500, 0),  # half a MiB: genuinely below the resolution, reads as unknown
        ],
    )
    def test_meminfo_is_parsed_into_whole_mib(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, kb: int, expected: int
    ) -> None:
        """Parsing is asserted against a FIXTURE, never against the live host.

        ``assert available > 0`` on the real reading looks like a smoke test and is
        actually the wall-clock-race flake class applied to memory: the value truncates
        to whole GiB, so any host with under 1 GiB free returns 0 -- which is the
        function's own "could not determine" sentinel, i.e. a CORRECT return that the
        assertion would call a failure. Small CI containers are the most exposed.

        The platform is pinned for the same reason the reading is: this exercises the
        ``/proc/meminfo`` branch specifically, and reaching it must not depend on the
        host the test happens to run on -- otherwise the whole parametrization silently
        stops asserting anything on the macOS and Windows shards.
        """
        import xdist_budget as budget
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            f"MemTotal:       131549320 kB\nMemAvailable:   {kb} kB\nBuffers: 1 kB\n",
            encoding="utf-8",
        )
        real_open = open

        def _fake(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return real_open(meminfo, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake)

        assert budget._host_available_mib() == expected

    def test_available_never_exceeds_total_on_a_real_host(self) -> None:
        """The one invariant that holds at ANY memory level, so it cannot flake.

        Gated on the READING rather than on ``/proc/meminfo``, so it runs on macOS
        and Windows too. Gating it on the Linux file is what kept the only
        real-host assertion here from ever executing on the platforms whose
        readings were added last.
        """
        import xdist_budget as budget

        available = budget._host_available_mib()
        if available == 0:
            pytest.skip("no available-memory reading on this platform")
        assert available <= budget._host_total_gib() * 1024

    def test_a_missing_meminfo_is_reported_as_unknown_not_as_zero_memory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """On Linux a missing ``/proc/meminfo`` is unknown, never zero memory.

        Zero would be indistinguishable from a genuinely starved host, and the
        two must diverge: ``_bounded_by`` SKIPS an unknown reading and would
        otherwise collapse every run to a single worker.
        """
        import xdist_budget as budget

        # Imported in-body, not at module scope: test_host_isolation_floor.py's ratchet asserts
        # that no import-time ``~/.kiro`` binding escapes the isolation fixtures,
        # and importing kiro_crew during COLLECTION binds them against the real
        # home before any fixture has run.
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        real_open = open

        def _no_meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _no_meminfo)

        assert budget._host_available_mib() == 0

    def test_a_platform_with_no_reading_at_all_is_unknown_not_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform none of the three branches claims returns 0 = unknown.

        The branch that stands in for the unknown host. macOS and Windows now have
        readings of their own, so this covers only the genuinely unknown host --
        and it must stay 0 so such a host keeps its parallelism instead of
        silently dropping to one worker.
        """
        import xdist_budget as budget
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        assert budget._host_available_mib() == 0

    def test_an_unlimited_cgroup_is_not_read_as_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """cgroup v2 spells "no limit" as the literal ``max``.

        v1 instead uses a huge sentinel (0x7FFFFFFFFFFFF000), which needs no special
        case: divided into GiB it is a number no ``min()`` will ever pick. Both are
        exercised here because the two files are read in the same loop.
        """
        import xdist_budget as budget

        limit_file = tmp_path / "memory.max"
        limit_file.write_text("max\n", encoding="utf-8")
        v1_file = tmp_path / "memory.limit_in_bytes"
        v1_file.write_text(f"{0x7FFFFFFFFFFFF000}\n", encoding="utf-8")

        real_open = open

        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return real_open(v1_file, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        # "max" is skipped outright; the v1 sentinel converts to a ceiling far above
        # any real core count, so neither can bind.
        assert budget._static_memory_bounded_capacity(8) <= 8
        assert budget._cgroup_limit_mib() >= 8 * 1024

    def test_a_real_cgroup_ceiling_binds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The container case: 8 GiB inside a cgroup on a 256 GiB machine."""
        import xdist_budget as budget

        limit_file = tmp_path / "memory.max"
        limit_file.write_text(str(8 * 1024**3), encoding="utf-8")
        real_open = open

        # Falls THROUGH for every other path rather than raising. `builtins.open` is
        # shared by every thread in this worker, and the SEL writer is a session-lived
        # daemon thread that opens a file on each flush -- a blanket raise would hand it
        # FileNotFoundError for a path that exists, and could kill the writer so that a
        # later, unrelated SEL test on this worker fails for a reason it cannot see.
        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        assert budget._cgroup_limit_mib() == 8 * 1024
