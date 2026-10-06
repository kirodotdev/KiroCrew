"""Private owners composed by :mod:`kiro_crew.slack.handler`.

The native Slack turn path's rules are split by responsibility across the modules of this
package, and ``kiro_crew.slack.handler`` stays their only import path and their only patch
surface: nothing but the handler module imports an owner.

Each owner holds functions and classes moved verbatim from the one-module file.
:func:`compose` makes every function an owner defines -- its module functions and the
methods of the classes it defines -- run on the handler module's globals rather than its
own. The gateway, the dashboard, the messaging transport and the tests reach these names
through the handler module and patch them there (``handler.is_yolo_mode``,
``._PendingApproval``, ``._build_approval_blocks``, ``.sel``, ``.kiro_agents_dir``,
``._EDIT_INTERVAL`` and dozens more). A function that read its owner's globals would keep
calling the unpatched object and the test would pass while exercising nothing, so the
handler stays ONE namespace, as the one-module file was. Four consequences follow:

* An owner's imports are inert for its functions. An owner imports the handler module
  only under ``TYPE_CHECKING``, and stdlib names plainly; both serve the type checker and
  the linter. Every name a function reads must exist in ``kiro_crew.slack.handler``, and
  ``test/test_slack_handler_composition_contract.py`` sweeps each function's bytecode to
  prove it does.
* A function's ``__module__`` reads ``kiro_crew.slack.handler`` as it did before the
  split, so reprs and pickling by reference are unchanged and it logs through that
  module's ``logger``. Its source file differs, so a log record's ``module``, ``filename``
  and ``lineno`` name the owner file. An owner's classes keep their own module, which is
  where ``inspect`` looks for a class's source.
* State stays on the handler module. Every module-level value -- the constants, the
  pending and linked approval registries, the voice state, the owner id and allowlist,
  the per-thread agent and project maps, the phase-emoji table, the privacy and
  auto-title tracker aliases -- is a ``kiro_crew.slack.handler`` global that owner
  functions reach by name, so a test that rebinds one there is the binding every
  function sees, and a ``global`` statement inside a rebound owner function writes the
  handler's namespace.
* The handler's own body runs BEFORE :func:`compose`, so nothing in it may call an owner
  function: the phase table built at import keeps ``_build_phase_emojis`` in the handler
  module for that reason.

:func:`compose` is the technique ``kiro_crew.hook_runtime.compose`` and the dashboard
handler packages apply. This package keeps its own copy because nothing but
``slack/handler.py`` may import a ``handler_runtime`` module, and the copies are pinned
byte-identical.

Which responsibility each owner holds, and which constructs stay in the handler module
because repository guards read them there, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (the "Native handler composition" section).
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
