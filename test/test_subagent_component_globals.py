"""Pin the globals a rebound component implementation is allowed to load.

``subagent_manager._component.bind_component_globals`` rebinds every ``*_impl`` on the
``subagent._MANAGER_COMPONENTS`` types onto ``kiro_crew.subagent``'s module globals, so
one of those functions executes with ``subagent.py``'s globals and an import at the top
of its own module is invisible to it. Every global it loads has to exist in
``subagent.py``.

A name that does not resolve there raises ``NameError`` only when its line runs, and
several of these implementations load theirs inside a ``try:`` whose ``except Exception``
denies, so the miss presents as a gate that refuses everything rather than as an error.
The sweep below turns it into a test failure at collection speed instead.
"""

from __future__ import annotations

import builtins
import types
from collections.abc import Iterator
from dis import get_instructions

import kiro_crew.subagent as subagent_module

_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})


def _global_names(code: types.CodeType) -> Iterator[str]:
    """Yield every global name a code object and its nested bodies reference."""
    for instruction in get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _rebound_implementations() -> Iterator[tuple[str, types.FunctionType]]:
    """Yield each ``*_impl`` carrying ``subagent``'s globals, named by its owning type."""
    namespace = vars(subagent_module)
    for component_type in subagent_module._MANAGER_COMPONENTS:
        for name, implementation in vars(component_type).items():
            if not name.endswith("_impl") or not isinstance(implementation, types.FunctionType):
                continue
            if implementation.__globals__ is namespace:
                yield f"{component_type.__name__}.{name}", implementation


def test_component_implementations_are_rebound_onto_subagent_globals() -> None:
    """The sweep proves nothing unless the rebind put implementations there to sweep."""
    assert list(_rebound_implementations())


def test_the_sweep_reports_a_global_subagent_does_not_define() -> None:
    """A name that resolves nowhere is what the sweep exists to see, nesting included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_subagent_globals  # noqa: F821

        return _inner

    assert "_absent_from_subagent_globals" in set(_global_names(_probe.__code__))


def test_rebound_implementations_only_load_globals_subagent_defines() -> None:
    """Every global a rebound implementation loads resolves in ``subagent`` or builtins."""
    namespace = vars(subagent_module)
    unresolved = [
        (qualified_name, global_name)
        for qualified_name, implementation in _rebound_implementations()
        for global_name in sorted(set(_global_names(implementation.__code__)))
        if global_name not in namespace and not hasattr(builtins, global_name)
    ]
    assert unresolved == []
