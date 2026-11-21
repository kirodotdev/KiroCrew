"""Per-test guards that run behind a few fixture names.

Every autouse fixture name costs each test a share of pytest's own fixture work
(resolving the name, a setup hook call, a finalizer, a teardown), whatever its
body does. A guard is a plain function written like a fixture; a ``GuardSet``
runs its guards around every test from a handful of autouse fixtures, in the
order pytest ran them when each was its own autouse fixture, so a test pays for
those few names and not for one per guard.

Two sets use this: the rootdir conftest's floors (``conftest_floor_guards``) and
the resets and pins of ``test/conftest.py`` (``conftest_guards``). A plain module
and not a part of either conftest; the rootdir conftest loads it by path, so the
in-package suites, which do not have ``test/`` on ``sys.path``, reach it too.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

if sys.version_info < (3, 11):  # pragma: no cover - the backport pytest itself uses
    from exceptiongroup import BaseExceptionGroup

Guard = Callable[..., Any]


def _is_fixture(obj: Any) -> bool:
    return (
        getattr(obj, "_fixture_function_marker", None) is not None
        or getattr(obj, "_pytestfixturefunction", None) is not None
    )


class GuardSet:
    """The per-test guards of one conftest, cut into segments that run in name order.

    *bounds* are the autouse fixtures of that conftest that stay fixtures because a
    test overrides or requests one by name, in name order. pytest sets up the
    autouse fixtures of one conftest in name order, so a guard runs in the segment
    between the two bounds its name sorts between, which is where it ran as a
    fixture of its own. *fixtures* are the fixture names a guard may take as
    parameters; a guard may also name an earlier guard, which then runs first.
    """

    def __init__(self, owner: str, bounds: tuple[str, ...], fixtures: tuple[str, ...]) -> None:
        self.owner = owner
        self.bounds = bounds
        self.fixtures = fixtures
        #: ``name -> guard``, filled by ``guard`` as the conftest parts import.
        self.registry: dict[str, Guard] = {}
        #: The guards of each segment in run order, set by ``seal``.
        self.segments: tuple[tuple[Guard, ...], ...] | None = None
        #: The test module and class last checked for a shadowing fixture.
        self.shadow_checked: tuple[Any, Any] = (None, None)
        #: What the guards a later guard names yielded for the running test.
        self.yielded: dict[str, Any] = {}

    def guard(self, function: Guard) -> Guard:
        """Mark *function* as a guard this set's fixtures run around every test.

        Written like a fixture: code before a single ``yield`` runs at setup and
        code after it at teardown, or no ``yield`` at all for setup only. Returned
        unchanged, so a test can drive it directly.
        """
        function.__per_test_guard__ = tuple(  # type: ignore[attr-defined]
            inspect.signature(function).parameters
        )
        # A second copy of the conftest, loaded by path by a test, leaves the
        # sealed set running the first copy's guards.
        if self.segments is None:
            self.registry[function.__name__] = function
        return function

    def seal(self, conftest_globals: dict[str, Any]) -> None:
        """Sort the guards the conftest binds into the segments, once.

        Called at the end of the conftest. A guard the conftest does not bind, one
        whose name sorts outside every segment, or one that names something that
        is neither an allowed fixture nor a guard that runs before it would never
        run correctly, so each raises here, at collection.
        """
        if self.segments is not None:
            return
        unbound = sorted(
            name for name, guard in self.registry.items() if conftest_globals.get(name) is not guard
        )
        if unbound:
            raise RuntimeError(
                f"per-test guards {self.owner} does not bind: {unbound}. Add each to "
                "the conftest's import block."
            )
        segments: list[list[Guard]] = [[] for _ in self.bounds[1:]]
        placed: dict[str, int] = {}

        def place(name: str, index: int, chain: tuple[str, ...]) -> None:
            if name in placed:
                return
            if name in chain:
                raise RuntimeError(f"per-test guards name each other in a cycle: {chain + (name,)}")
            guard = self.registry[name]
            for param in guard.__per_test_guard__:
                if param in self.registry:
                    place(param, index, chain + (name,))
                elif param not in self.fixtures:
                    raise TypeError(
                        f"{name} takes {param!r}; a per-test guard of {self.owner} takes "
                        f"only {self.fixtures} or an earlier guard. Something a test "
                        "requests or overrides by name stays a fixture."
                    )
            placed[name] = index
            segments[index].append(guard)

        stray = []
        for name in sorted(self.registry):
            index = sum(bound < name for bound in self.bounds) - 1
            if 0 <= index < len(segments):
                place(name, index, ())
            elif name not in placed:
                stray.append(name)
        if stray:
            raise RuntimeError(
                f"per-test guards whose names sort outside {self.bounds[0]!r}.."
                f"{self.bounds[-1]!r}: {stray}. Rename them to sort inside it."
            )
        self.segments = tuple(tuple(segment) for segment in segments)

    def sealed_segments(self) -> tuple[tuple[Guard, ...], ...]:
        """The sealed segments, for the fixtures that run them and the tests that pin them."""
        if self.segments is None:
            raise RuntimeError(f"{self.owner} has not sealed its per-test guards")
        return self.segments

    def refuse_shadowing_fixtures(self, request: Any) -> None:
        """Fail a test whose module or class defines a fixture named like a guard.

        A fixture replaces a conftest fixture of the same name, but a guard is not
        one, so the guard would run as well and the test would not get the override
        it asked for. Checked once per module and class.
        """
        owners = (request.module, request.cls)
        if owners[0] is self.shadow_checked[0] and owners[1] is self.shadow_checked[1]:
            return
        for owner in owners:
            shadows = sorted(
                name
                for name in self.registry
                if owner is not None and _is_fixture(getattr(owner, name, None))
            )
            if shadows:
                pytest.fail(
                    f"{getattr(owner, '__name__', owner)} defines a fixture named like a "
                    f"per-test guard of {self.owner}: {shadows}. The guard still runs, "
                    "so this does not override it. Undo its effect in the test, or make "
                    "the guard a fixture again and add its name to the set's bounds.",
                    pytrace=False,
                )
        self.shadow_checked = owners

    def run_segment(self, index: int, request: Any, **fixtures: Any) -> Iterator[None]:
        """Run segment *index* around the test, handing each guard what it names."""
        if index:
            yield from run_guards(self.sealed_segments()[index], request, self.yielded, **fixtures)
            return
        self.refuse_shadowing_fixtures(request)
        self.yielded.clear()
        try:
            yield from run_guards(self.sealed_segments()[0], request, self.yielded, **fixtures)
        finally:
            self.yielded.clear()


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
    guards: tuple[Guard, ...], request: Any, yielded: dict[str, Any], **fixtures: Any
) -> Iterator[None]:
    """Set up *guards* in order, yield for the test, tear them down in reverse.

    The contract each guard had as its own fixture: a guard that raises at setup
    stops the ones after it and tears down the ones before it, and a guard that
    raises at teardown does not stop the others' teardowns. A parameter is filled
    from *fixtures*, else with what the earlier guard of that name left in *yielded*.
    """
    available = {"request": request, **fixtures}
    started: list[tuple[str, Iterator[Any]]] = []
    try:
        for guard in guards:
            cycle = guard(
                *[
                    available[name] if name in available else yielded[name]
                    for name in guard.__per_test_guard__
                ]
            )
            value = None
            if inspect.isgenerator(cycle):
                value = next(cycle)
                started.append((guard.__name__, cycle))
            yielded[guard.__name__] = value
        yield
    finally:
        _finish(started)
