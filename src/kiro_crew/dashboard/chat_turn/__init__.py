"""Private owners composed by :mod:`kiro_crew.dashboard.chat_runner`.

The dashboard chat runner is split by responsibility across the modules of this
package, and ``kiro_crew.dashboard.chat_runner`` stays its only import path and
its only patch surface: nothing but the runner imports an owner.

Each owner holds the runner's top-level helpers for its responsibility, moved
verbatim, and functions extracted from ``_run_chat`` and ``_start_next_queued_turn``
whose bodies keep the original statements and take the original locals as
same-named parameters. :func:`compose` makes every function an owner defines -- its
module functions and the methods of the classes it defines -- run on the runner's
module globals rather than its own. Tests patch the runner's names
(``chat_runner.sel``, ``.save_slot_off_loop``, ``._flush_file_changes`` and about a
hundred more). A function that read its owner's globals would keep calling the unpatched
object and the test would pass while exercising nothing, so the runner stays ONE
namespace, as the one-module file was. Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the runner and
  its siblings only under ``TYPE_CHECKING``; stdlib names, and the project constants
  the module evaluates when it loads (``steer_queue`` from ``chat_utils``,
  ``tool_approval`` from ``deny_guidance``), are imported plainly. Both serve the
  type checker and the linter, and what the module itself evaluates when it loads
  (a default argument, a class base, a constant). Every name a function
  reads must exist in ``kiro_crew.dashboard.chat_runner`` -- which is why the
  runner keeps imports only its owners read, marked ``# noqa: F401`` -- and
  ``test_chat_runner_composition_contract.py`` sweeps each function's bytecode to
  prove it does.
* A function's ``__module__`` reads ``kiro_crew.dashboard.chat_runner`` as it did
  before the split, so reprs and pickling by reference are unchanged, and it logs
  through the runner's ``logger``. Its source file differs, so a log record's
  ``module``, ``filename`` and ``lineno`` name the owner file. An owner's classes
  keep their own module, which is where ``inspect`` looks for a class's source.
* State the runner owns stays on the runner. A module-level mutable (a registry,
  a cache, a semaphore) is a runner global that owner functions reach by name
  through the runner's namespace, so a test that rebinds it on the runner is the
  one every function sees.

The technique is the one ``kiro_crew.slack.gateway_runtime.compose`` applies to the
Slack gateway. That one also binds orchestrator methods onto the gateway's class,
and the runner has no such class, so this is the module-function half of it.

Which responsibility each owner holds, and which constructs stay in the runner
because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (Chat runner composition).
"""

from __future__ import annotations

from collections.abc import Iterable
from types import FunctionType, ModuleType
from typing import Any


def compose(namespace: dict[str, Any], owners: Iterable[ModuleType]) -> None:
    """Run every function *owners* define on *namespace*.

    *namespace* is the runner's ``globals()``. The runner calls this once, after
    its own body has bound every name, so a single pass replaces every binding of
    an owner function -- in the runner, in the owners and in the owners' classes --
    with its rebound copy, and no module is left holding the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a runner imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every runner import and their
    methods follow the most recent one.
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
