"""``kiro_crew._facade_bindings.own_methods``: the one qualname-restore every split class uses.

The rule is pinned on synthetic owner functions (which members it renames, which it leaves
alone, how it treats ``staticmethod``/``classmethod`` and ``__wrapped__`` chains, and the
re-homing mode), then on every split class that calls it: each function the class binds
from an owner module reads as ``<Class>.<attribute>``, so an owner module missing from a
class's owner set fails here rather than reporting the owner's name in a traceback.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import types
from collections.abc import Iterator
from typing import Any

import pytest

from kiro_crew._facade_bindings import own_methods

_OWNER = "pkg.thing_owner"
_SIBLING = "pkg.thing_owner_models"  # shares the owner's prefix but is not an owner


def _owned(fn: Any, module: str = _OWNER) -> Any:
    fn.__module__ = module
    return fn


def _method(self: Any) -> int:
    return 1


def _static(value: int) -> int:
    return value


def _klass(cls: type) -> str:
    return cls.__name__


def _fresh(fn: Any) -> Any:
    """A copy of *fn*, so each test renames its own function object."""
    copy = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, fn.__defaults__)
    copy.__annotations__ = dict(fn.__annotations__)
    return copy


def test_an_owner_function_reads_as_a_method_of_the_class() -> None:
    fn = _owned(_fresh(_method))

    class Thing:
        run = fn

    assert own_methods(Thing, frozenset({_OWNER})) is Thing
    assert fn.__qualname__ == f"{Thing.__qualname__}.run"
    assert "self" not in fn.__annotations__
    assert fn.__annotations__ == {"return": "int"}
    assert fn.__module__ == _OWNER  # only the name moves; the owner still holds the body


def test_a_module_outside_the_owner_set_is_left_alone() -> None:
    sibling = _owned(_fresh(_method), _SIBLING)

    class Thing:
        run = sibling

    own_methods(Thing, frozenset({_OWNER}))
    assert sibling.__qualname__ == "_method"
    assert "self" in sibling.__annotations__


def test_static_and_class_methods_are_renamed_through_func_but_the_wrapper_is_kept() -> None:
    static_fn = _owned(_fresh(_static))
    class_fn = _owned(_fresh(_klass))

    class Thing:
        check = staticmethod(static_fn)
        kind = classmethod(class_fn)

    wrapper = vars(Thing)["check"]
    own_methods(Thing, frozenset({_OWNER}))
    assert static_fn.__qualname__ == f"{Thing.__qualname__}.check"
    assert class_fn.__qualname__ == f"{Thing.__qualname__}.kind"
    assert wrapper.__qualname__ == "_static"
    assert Thing.check(3) == 3 and Thing.kind() == "Thing"


def test_a_wrapper_chain_is_followed_while_each_link_is_an_owners() -> None:
    def _span(self: Any) -> Iterator[int]:
        yield 1

    inner = _owned(_span)
    outer = _owned(contextlib.contextmanager(inner))
    foreign = _owned(_fresh(_method), _SIBLING)
    shielded = _owned(functools.wraps(foreign)(lambda self: 0))
    shielded.__module__ = _OWNER

    class Thing:
        span = outer
        guarded = shielded

    own_methods(Thing, frozenset({_OWNER}))
    assert outer.__qualname__ == inner.__qualname__ == f"{Thing.__qualname__}.span"
    assert "self" not in inner.__annotations__
    assert shielded.__qualname__ == f"{Thing.__qualname__}.guarded"
    assert foreign.__qualname__ == "_method"  # the chain stops at the first non-owner


def test_non_function_members_are_ignored() -> None:
    class Thing:
        limit = 3
        label = "x"

        @property
        def size(self) -> int:
            return 1

    own_methods(Thing, frozenset({_OWNER, Thing.__module__}))
    assert Thing.limit == 3 and Thing.label == "x" and Thing().size == 1


def test_rehoming_names_the_class_module_on_every_object_including_the_wrapper() -> None:
    fn = _owned(_fresh(_method))
    static_fn = _owned(_fresh(_static))

    class Thing:
        run = fn
        check = staticmethod(static_fn)

    wrapper = vars(Thing)["check"]
    own_methods(Thing, frozenset({_OWNER}), module="pkg.thing")
    for obj, name in ((fn, "run"), (wrapper, "check"), (static_fn, "check")):
        assert obj.__qualname__ == f"{Thing.__qualname__}.{name}"
        assert obj.__module__ == "pkg.thing"


#: Every class that restores its members' qualnames with the shared helper.
_SPLIT_CLASSES = (
    ("kiro_crew.acp.runtime", "AcpRuntime"),
    ("kiro_crew.acp.session_handle", "AcpSessionHandle"),
    ("kiro_crew.dashboard.state", "_ChatSlot"),
    ("kiro_crew.dashboard.state", "DashboardState"),
    ("kiro_crew.history_consolidation", "HistoryConsolidator"),
    ("kiro_crew.kiro_prerequisite", "KiroPrerequisiteService"),
    ("kiro_crew.session_map", "SessionMap"),
)


def _reachable(member: Any) -> Iterator[types.FunctionType]:
    function = getattr(member, "__func__", member)
    while isinstance(function, types.FunctionType):
        yield function
        function = getattr(function, "__wrapped__", None)


@pytest.mark.parametrize(("module", "name"), _SPLIT_CLASSES)
def test_every_kiro_crew_function_a_split_class_binds_reads_as_its_method(
    module: str, name: str
) -> None:
    cls = getattr(importlib.import_module(module), name)
    wrong = sorted(
        f"{attr}: {fn.__qualname__} ({fn.__module__})"
        for attr, member in vars(cls).items()
        for fn in _reachable(member)
        if fn.__module__.startswith("kiro_crew.")
        and fn.__qualname__ != f"{cls.__qualname__}.{attr}"
    )
    assert not wrong
