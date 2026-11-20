"""Per-test guards of ``test/conftest.py`` that run behind a few fixture names.

Every autouse fixture name costs each test a share of pytest's own fixture work
(resolving the name, a setup hook call, a finalizer, a teardown), whatever its
body does. A guard here is a plain function marked ``@per_test_guard``; the five
``_*_guards`` fixtures below run every one of them around every test, in the
order pytest ran them when each was its own autouse fixture, so the suite pays
for five names and not one per guard.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest
registers each fixture as the suite conftest's own; see the import block there.
Plain module, never collected.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

if sys.version_info < (3, 11):  # pragma: no cover - the backport pytest itself uses
    from exceptiongroup import BaseExceptionGroup

#: The only parameters a guard may take. A guard patches through
#: ``_floor_monkeypatch``, never the shared ``monkeypatch``.
_GUARD_PARAMS = ("request", "_floor_monkeypatch")

#: Autouse fixtures of ``test/conftest.py`` that stay fixtures because a test
#: overrides or requests one by name. pytest sets up the autouse fixtures of one
#: conftest in name order, so these names are where the guards' order is cut.
SEGMENT_BOUNDS = (
    "_a_shared_monkeypatch_first",
    "_drop_live_config_snapshot",
    "_git_identity",
    "_isolate_context_store_caches",
    "_no_live_catalog_network",
    "_windows_restrict_to_owner_stub",
)

#: ``name -> guard``, filled by ``@per_test_guard`` as the conftest parts import.
_REGISTRY: dict[str, Callable[..., Any]] = {}
#: The guards of each segment in run order, set by ``seal_guards``.
_SEGMENTS: tuple[tuple[Callable[..., Any], ...], ...] | None = None
#: The test module and class last checked for a fixture that shadows a guard.
_SHADOW_CHECKED: tuple[Any, Any] = (None, None)


def per_test_guard(function: Callable[..., Any]) -> Callable[..., Any]:
    """Mark *function* as a guard the ``_*_guards`` fixtures run around every test.

    Written like a fixture: code before a single ``yield`` runs at setup and code
    after it at teardown, or no ``yield`` at all for setup only. Returned unchanged,
    so a test can drive it directly.
    """
    params = tuple(inspect.signature(function).parameters)
    unknown = [name for name in params if name not in _GUARD_PARAMS]
    if unknown:
        raise TypeError(
            f"{function.__name__} takes {unknown}; a per-test guard takes only "
            f"{_GUARD_PARAMS}. Something a test requests or overrides by name stays "
            "a fixture."
        )
    function.__per_test_guard__ = params  # type: ignore[attr-defined]
    _REGISTRY[function.__name__] = function
    return function


def seal_guards(conftest_globals: dict[str, Any]) -> None:
    """Sort the guards bound in ``test/conftest.py`` into the segments, once.

    Called at the end of the conftest. A guard the conftest does not bind, or one
    whose name sorts outside every segment, would never run, so both raise here,
    at collection, and not in the first test that inherits the leak.
    """
    global _SEGMENTS
    unbound = sorted(
        name for name, guard in _REGISTRY.items() if conftest_globals.get(name) is not guard
    )
    if unbound:
        raise RuntimeError(
            f"per-test guards test/conftest.py does not bind: {unbound}. Add each to "
            "the conftest's import block."
        )
    names = sorted(_REGISTRY)
    segments = tuple(
        tuple(_REGISTRY[name] for name in names if low < name < high)
        for low, high in zip(SEGMENT_BOUNDS, SEGMENT_BOUNDS[1:])
    )
    placed = {guard.__name__ for segment in segments for guard in segment}
    stray = sorted(set(names) - placed)
    if stray:
        raise RuntimeError(
            f"per-test guards whose names sort outside {SEGMENT_BOUNDS[0]!r}.."
            f"{SEGMENT_BOUNDS[-1]!r}: {stray}. Rename them to sort inside it."
        )
    _SEGMENTS = segments


def guard_segments() -> tuple[tuple[Callable[..., Any], ...], ...]:
    """The sealed segments, for the tests that pin them."""
    if _SEGMENTS is None:
        raise RuntimeError("test/conftest.py has not sealed its per-test guards")
    return _SEGMENTS


def _is_fixture(obj: Any) -> bool:
    return (
        getattr(obj, "_fixture_function_marker", None) is not None
        or getattr(obj, "_pytestfixturefunction", None) is not None
    )


def refuse_shadowing_fixtures(request: Any) -> None:
    """Fail a test whose module or class defines a fixture named like a guard.

    A fixture replaces a conftest fixture of the same name, but a guard is not
    one, so the guard would run as well and the test would not get the override
    it asked for. Checked once per module and class.
    """
    global _SHADOW_CHECKED
    owners = (request.module, request.cls)
    if owners[0] is _SHADOW_CHECKED[0] and owners[1] is _SHADOW_CHECKED[1]:
        return
    for owner in owners:
        shadows = sorted(
            name
            for name in _REGISTRY
            if owner is not None and _is_fixture(getattr(owner, name, None))
        )
        if shadows:
            pytest.fail(
                f"{getattr(owner, '__name__', owner)} defines a fixture named like a "
                f"per-test guard of test/conftest.py: {shadows}. The guard still runs, "
                "so this does not override it. Undo its effect in the test, or make "
                "the guard a fixture again and add its name to SEGMENT_BOUNDS.",
                pytrace=False,
            )
    _SHADOW_CHECKED = owners


def _finish(started: list[tuple[str, Iterator[Any]]]) -> None:
    """Run each started guard's teardown, last started first; raise what they raised."""
    errors: list[BaseException] = []
    while started:
        name, cycle = started.pop()
        try:
            next(cycle)
        except StopIteration:
            continue
        except BaseException as exc:  # noqa: BLE001 - every later guard still tears down
            errors.append(exc)
            continue
        errors.append(RuntimeError(f"per-test guard {name} has more than one 'yield'"))
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("errors while tearing down per-test guards", errors)


def run_guards(
    guards: tuple[Callable[..., Any], ...], request: Any, floor_monkeypatch: Any
) -> Iterator[None]:
    """Set up *guards* in order, yield for the test, tear them down in reverse.

    The contract each guard had as its own fixture: a guard that raises at setup
    stops the ones after it and tears down the ones before it, and a guard that
    raises at teardown does not stop the others' teardowns.
    """
    available = {"request": request, "_floor_monkeypatch": floor_monkeypatch}
    started: list[tuple[str, Iterator[Any]]] = []
    try:
        for guard in guards:
            cycle = guard(*[available[name] for name in guard.__per_test_guard__])
            if cycle is not None:
                next(cycle)
                started.append((guard.__name__, cycle))
        yield
    finally:
        _finish(started)


def _segment(index: int, request: Any, floor_monkeypatch: Any) -> Iterator[None]:
    yield from run_guards(guard_segments()[index], request, floor_monkeypatch)


# Each fixture is named to sort between the two ``SEGMENT_BOUNDS`` names it runs
# between, which is what puts its guards where they ran as fixtures of their own.


@pytest.fixture(autouse=True)
def _b_guards(request, _floor_monkeypatch):
    """The guards named between ``_a_shared_monkeypatch_first`` and ``_drop_live_config_snapshot``."""
    refuse_shadowing_fixtures(request)
    yield from _segment(0, request, _floor_monkeypatch)


@pytest.fixture(autouse=True)
def _e_guards(request, _floor_monkeypatch):
    """The guards named between ``_drop_live_config_snapshot`` and ``_git_identity``."""
    yield from _segment(1, request, _floor_monkeypatch)


@pytest.fixture(autouse=True)
def _h_guards(request, _floor_monkeypatch):
    """The guards named between ``_git_identity`` and ``_isolate_context_store_caches``."""
    yield from _segment(2, request, _floor_monkeypatch)


@pytest.fixture(autouse=True)
def _j_guards(request, _floor_monkeypatch):
    """The guards named between ``_isolate_context_store_caches`` and ``_no_live_catalog_network``."""
    yield from _segment(3, request, _floor_monkeypatch)


@pytest.fixture(autouse=True)
def _o_guards(request, _floor_monkeypatch):
    """The guards named between ``_no_live_catalog_network`` and ``_windows_restrict_to_owner_stub``."""
    yield from _segment(4, request, _floor_monkeypatch)


#: The fixture that runs each segment, in ``SEGMENT_BOUNDS`` order.
SEGMENT_FIXTURES = ("_b_guards", "_e_guards", "_h_guards", "_j_guards", "_o_guards")
