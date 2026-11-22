"""The crew-log read-budget floor: CI fails a test whose gateway reads went over budget.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.

The budget (``kiro_crew.crew_log.read_budget``) counts only in a process that called
``mark_gateway_process()``, and only reports. With ``KIROCREW_CREW_LOG_READ_BUDGET=raise``
(the Linux ``Backend Tests`` job) every test runs as the gateway, and at its teardown
:func:`check_read_budget` fails it when a read went over ``delta + K`` at a product site
not in :data:`KNOWN_OVER_BUDGET_SITES`. The check sits here, not in the read: a raise
inside the read would be swallowed by the read's own handlers and lost on worker
threads. See ``docs/system-specs/modules/crew-log-projection.md`` section 4.1.
"""

from __future__ import annotations

import os
import pathlib

import pytest

#: The switch, and the value that turns the check on. The gateway reads ``raise`` as
#: ``report``; only this floor tells the two apart.
_READ_BUDGET_ENV = "KIROCREW_CREW_LOG_READ_BUDGET"

#: Whether this run checks, read ONCE at conftest import: it decides whether the fixture
#: below is DEFINED, and an autouse fixture that exists costs every test even when idle
#: (see ``conftest_floor_tmp._TMP_PER_TEST``).
_READ_BUDGET_RAISE = os.environ.get(_READ_BUDGET_ENV, "").strip().lower() == "raise"

#: Over-budget product sites the check lets through, as ``"<path> <function>"`` -- the
#: first caller frame outside the read machinery, as the WARNING prints it, without its
#: ``:line`` and with ``/`` for ``os.sep``. This dict may only SHRINK: a fixed site is
#: removed, and a new entry needs a reviewer's eye on why its bound holds.
_GAP = "pre-existing, see #33827"
KNOWN_OVER_BUDGET_SITES: dict[str, str] = {
    "crew_log/projection_work.py work_slots_naming_board": (
        "allowed: an operator rebuild walks every unit; one caller, "
        "work_ledger.rebuild_from_projection"
    ),
    "crew_log/read.py read_page": (
        "no bound: CrewLog.resolve walks a citation from seq 1 to its from_seq, so a "
        f"deep citation reads most of the unit; {_GAP}"
    ),
    "crew_log/session_tree_projection_seed.py _until_skipped": (
        "no bound: _read_unit's first fold also runs whole when a stored mark is "
        f"refused, so a unit whose mark keeps failing is walked on every seed; {_GAP}"
    ),
    "crew_log/emit_session.py _seed_attempts": (
        "no bound: also re-run after the attempt map evicts a session, and 2+ segments "
        f"turn the prefilter off; {_GAP}"
    ),
    "dashboard_templates/instance.py _fold_history_from_log": (
        "no bound: history() never writes the savepoint, so every read without one "
        f"walks the whole unit; {_GAP}"
    ),
    "eventlog/log.py _ensure_loaded": (
        f"no bound: a member log has no savepoint and its size is measured, not capped; {_GAP}"
    ),
    "eventlog/log.py history": (
        "no bound: a member log past MAX_RETAINED_EVENTS pages and streams the store "
        f"with no savepoint; {_GAP}"
    ),
    "eventlog/log.py <genexpr>": (
        f"no bound: iter_events streams a member log whole, it has no savepoint; {_GAP}"
    ),
    "crew_log/emit_crew.py _newest_dispatch_for": (
        f"no bound: walks the whole unit on every crew report that names an item; {_GAP}"
    ),
    "crew_log/read.py _tapped": (
        "no bound: _listed_row walks a unit with any skipped line whole on every "
        f"listing; {_GAP}"
    ),
    "apps/builtins/issue_radar/backend/crew_store_log.py _landed_since": (
        f"no bound: iter_from with no mark re-reads the unit below after_seq; {_GAP}"
    ),
    "crew_log/slot_prime.py _read_backfilled": (
        "no bound: with the backfill child off or failed, the slot folds cold on the "
        f"gateway thread; {_GAP}"
    ),
    "crew_log/eager.py _advance_session": (
        "no bound: the eager worker folds a unit with no session savepoint whole, on "
        f"the gateway; {_GAP}"
    ),
    "crew_log/slot_backfill_child.py _stage": (
        "allowed: runs in the backfill child, which never marks the gateway; only a "
        "test calls it in-process"
    ),
}

#: This suite's own directory: a helper module here is test code, not a gateway site.
_TEST_DIR = pathlib.Path(__file__).resolve().parent


def _site_of(frame: str) -> "tuple[str, str]":
    """``("<path> <func>", path)`` for one ``path:line func`` frame, *path* with ``/``."""
    where, _, func = frame.partition(" ")
    path = where.rsplit(":", 1)[0].replace(os.sep, "/")
    return f"{path} {func}", path


def _is_test_read(path: str) -> bool:
    """Whether a site's *path* is a test calling the read API, not a product call site.

    The WARNING prints a path inside the package relative to ``kiro_crew/`` and any
    other path as its basename. A test module or conftest anywhere is a test, and so is
    a bare name that is a module in this suite's directory (a shared helper). Anything
    else, a stdlib frame included, is checked like product code.
    """
    name = path.rsplit("/", 1)[-1]
    if name.startswith(("test_", "conftest")):
        return True
    return "/" not in path and (_TEST_DIR / name).is_file()


def _unlisted_sites(warned: "set[tuple[str, str]]") -> "list[tuple[str, str, str]]":
    """``(site, fold, frame)`` for every warned product site not on the list, sorted."""
    out: "list[tuple[str, str, str]]" = []
    for fold, frame in warned:
        site, path = _site_of(frame)
        if site in KNOWN_OVER_BUDGET_SITES or _is_test_read(path):
            continue
        out.append((site, fold, frame))
    return sorted(out)


def check_read_budget() -> None:
    """Fail the running test when its gateway reads went over budget at an unlisted site.

    Reads ``read_budget._warned``: the sites that went over ``delta + K`` since the last
    reset, as ``(fold, "path:line func")``. Resets the budget afterwards, pass or fail.
    """
    from kiro_crew.crew_log import read_budget

    try:
        with read_budget._lock:
            warned = set(read_budget._warned)
        unlisted = _unlisted_sites(warned)
    finally:
        read_budget.reset_for_tests()
    if unlisted:
        lines = "\n".join(f"  {site}  (fold={fold}, at {frame})" for site, fold, frame in unlisted)
        pytest.fail(
            f"crew log read over budget (delta + K, K={read_budget.K}) at a gateway "
            f"site not in test/conftest_floor_read_budget.py KNOWN_OVER_BUDGET_SITES:\n"
            f"{lines}\n"
            "Its 'crew log read over budget' WARNING in the captured log names the unit, "
            "the records read and three caller frames (caller=a < b < c, nearest first). "
            "Bound the read, or list the site with why its bound holds.",
            pytrace=False,
        )


if _READ_BUDGET_RAISE:

    @pytest.fixture(autouse=True)
    def _crew_log_read_budget_check():
        """Count this test's crew-log reads as the gateway's; check them at teardown.

        A conftest autouse fixture sets up first and tears down last, so a test module
        that resets the budget in its own fixture has cleared it before this check reads.
        """
        from kiro_crew.crew_log import read_budget

        read_budget.reset_for_tests()
        read_budget.mark_gateway_process()
        yield
        check_read_budget()
