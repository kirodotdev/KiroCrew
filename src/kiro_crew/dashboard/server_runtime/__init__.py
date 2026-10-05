"""Private owners composed by :mod:`kiro_crew.dashboard.server`.

The dashboard gateway's bootstrap is split by responsibility across the modules of
this package, and ``kiro_crew.dashboard.server`` stays their only import path and
their only patch surface: nothing but the server module imports an owner.

Each owner holds functions moved verbatim from the one-module file, plus the boot
phases ``start_dashboard`` and ``start_api_server`` delegate to. :func:`compose`
makes every function an owner defines -- its module functions and the methods of
the classes it defines -- run on the server module's globals rather than its own.
Tests patch the server's names (``server.sel``, ``._start_site``, ``._bind_once``,
``.data_home``, ``.current_context``, ``._extra_frame_ancestors`` and dozens more),
and a function that read its owner's globals would keep calling the unpatched
object while the test passed exercising nothing, so the server stays ONE
namespace, as the one-module file was. Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the server
  module only under ``TYPE_CHECKING``, and stdlib and third-party names (such as
  ``aiohttp.web``) plainly; both serve the type checker and the linter. Every name a
  function reads must exist in ``kiro_crew.dashboard.server`` -- which is why that
  module keeps imports only its owners read, marked ``# noqa: F401`` -- and
  ``test_dashboard_server_composition_contract.py`` sweeps each function's bytecode
  to prove it does.
* A function's ``__module__`` reads ``kiro_crew.dashboard.server`` as it did before
  the split, so reprs, pickling by reference and every route walk that selects
  handlers by ``__module__`` are unchanged, and it logs through that module's
  ``logger``. Its source file differs, so a log record's ``module``, ``filename``
  and ``lineno`` name the owner file. An owner's classes keep their own module,
  which is where ``inspect`` looks for a class's source.
* State stays on the server module. Every module-level value (the internal-path
  sets, the CSP and cache policies, the timeouts and budgets, the tailnet awake
  cache, the own-address warm task set, ``logger``) is a server global that owner
  functions reach by name, so a test that rebinds it there is the binding every
  function sees. No owner defines a module-level value.

:func:`compose` is the technique ``kiro_crew.dashboard.agent_admin.compose``,
``kiro_crew.dashboard.file_api.compose`` and
``kiro_crew.dashboard.messaging_api.compose`` apply to the agents, file and
messaging handlers. This package keeps its own copy because nothing but
``dashboard/server.py`` may import a ``server_runtime`` module.

Which responsibility each owner holds, and which constructs stay in the server
module because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (the ``server.py`` bullet
under "Dashboard").
"""

from __future__ import annotations

from collections.abc import Iterable
from types import FunctionType, ModuleType
from typing import Any


def compose(namespace: dict[str, Any], owners: Iterable[ModuleType]) -> None:
    """Run every function *owners* define on *namespace*.

    *namespace* is the handlers module's ``globals()``. The handlers module calls
    this once, after its own body has bound every name, so a single pass replaces
    every binding of an owner function -- in the handlers module, in the owners and
    in the owners' classes -- with its rebound copy, and no module is left holding
    the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a handlers module imported a second time into the
    same process rebinds the owners onto its fresh namespace. The owners are
    imported once per process, so their classes are shared by every handlers import
    and their methods follow the most recent one.
    """
    owners = tuple(owners)
    rebound: dict[int, tuple[FunctionType, FunctionType]] = {}
    classes: list[type] = []
    for owner in owners:
        path = owner.__file__
        for value in list(vars(owner).values()):
            if isinstance(value, FunctionType) and value.__code__.co_filename == path:
                rebound[id(value)] = (value, _rebind(value, namespace))
            elif isinstance(value, type) and value.__module__ == owner.__name__:
                classes.append(value)
                for member in vars(value).values():
                    for fn in _functions_of(member):
                        if fn.__code__.co_filename == path:
                            rebound[id(fn)] = (fn, _rebind(fn, namespace))

    def _swap(value: Any) -> Any:
        if isinstance(value, FunctionType):
            pair = rebound.get(id(value))
            return pair[1] if pair is not None and pair[0] is value else value
        if isinstance(value, (staticmethod, classmethod)):
            fn = _swap(value.__func__)
            return value if fn is value.__func__ else type(value)(fn)
        if isinstance(value, property):
            parts = (_swap(value.fget), _swap(value.fset), _swap(value.fdel))
            if parts == (value.fget, value.fset, value.fdel):
                return value
            return property(*parts, value.__doc__)
        return value

    for holder in [namespace, *(vars(owner) for owner in owners)]:
        for name, value in list(holder.items()):
            swapped = _swap(value)
            if swapped is not value:
                holder[name] = swapped
    for cls in classes:
        for name, value in list(vars(cls).items()):
            swapped = _swap(value)
            if swapped is not value:
                setattr(cls, name, swapped)


def _functions_of(member: Any) -> list[FunctionType]:
    """The plain functions a class member wraps: itself, a static/class method, a property."""
    if isinstance(member, FunctionType):
        return [member]
    if isinstance(member, (staticmethod, classmethod)) and isinstance(
        member.__func__, FunctionType
    ):
        return [member.__func__]
    if isinstance(member, property):
        return [
            fn for fn in (member.fget, member.fset, member.fdel) if isinstance(fn, FunctionType)
        ]
    return []


def _rebind(fn: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    """A copy of *fn* whose globals are *namespace*; every other attribute kept."""
    new = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    new.__kwdefaults__ = fn.__kwdefaults__
    new.__annotations__ = fn.__annotations__
    new.__dict__.update(fn.__dict__)
    new.__doc__ = fn.__doc__
    new.__qualname__ = fn.__qualname__
    new.__module__ = namespace["__name__"]
    new.__type_params__ = fn.__type_params__
    return new
