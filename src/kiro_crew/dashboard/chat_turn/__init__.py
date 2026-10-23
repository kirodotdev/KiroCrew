"""Private owners composed by :mod:`kiro_crew.dashboard.chat_runner`.

The dashboard chat runner is split by responsibility across the modules of this
package, and ``kiro_crew.dashboard.chat_runner`` stays its only import path and
its only patch surface: nothing but the runner imports an owner.

Each owner holds the runner's top-level helpers for its responsibility, moved
verbatim, and the phases of a dashboard turn: functions cut from ``_run_chat`` and
``_start_next_queued_turn`` whose bodies keep the original statements. The phases
that exist take the locals they read as parameters and return what they change; a
phase newly cut from ``_run_chat`` takes the turn's :class:`turn_locals._ChatTurn`
instead and reads the state the turn shares off it. :func:`compose` binds on the
runner every name an owner defines, so ``chat_runner.<name>`` resolves for each, and
makes every function an owner defines -- its module functions and the methods of the
classes it defines -- run on the runner's module globals rather than its own. Tests
patch the runner's names (``chat_runner.sel``, ``.save_slot_off_loop``,
``._flush_file_changes`` and about a hundred more). A function that read its owner's
globals would keep calling the unpatched object and the test would pass while
exercising nothing, so the runner stays ONE namespace, as the one-module file was.
Three consequences follow:

* An owner imports at run time each name it reads, in a function or when the module
  loads (a default argument, a class base, a constant), apart from the runner's and its
  siblings' own names, which it imports only under ``TYPE_CHECKING``. Its functions
  still read every name off the runner's globals: :func:`_bind` binds on the
  runner each name an owner binds that the runner lacks, its imports included, and
  ``test_chat_runner_composition_contract.py`` sweeps each function's bytecode to
  prove every name it reads is bound there. The runner imports only what its own code,
  its importers or its tests read, a name the one-module runner bound that no owner
  imports, a module object (which :func:`_bind` leaves out), and its
  ``kiro_crew.acp*`` / ``kiro_crew.providers*`` statements. A name bound on the runner
  and in an owner is the SAME object, or the runner fails to import.
* A function's ``__module__`` reads ``kiro_crew.dashboard.chat_runner`` as it did
  before the split, so reprs and pickling by reference are unchanged, and it logs
  through the runner's ``logger``. Its source file differs, so a log record's
  ``module``, ``filename`` and ``lineno`` name the owner file. An owner's classes
  keep their own module, which is where ``inspect`` looks for a class's source.
* State the runner owns stays on the runner. A module-level mutable (a registry,
  a cache, a semaphore) is a runner global that owner functions reach by name
  through the runner's namespace, so a test that rebinds it on the runner is the
  one every function sees.

The rebinding is the shared ``kiro_crew._facade_compose.compose`` the other composed
facades call, and :func:`compose` runs it after binding the owners' names on the
runner. That binding step and :func:`owner_modules` stay in this package because the
shared helper does neither: it is handed its owners, and binds no name the facade
lacks.

Which responsibility each owner holds, and which constructs stay in the runner
because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (Chat runner composition).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterable
from types import ModuleType
from typing import Any

from kiro_crew._facade_compose import compose as _facade_compose


def owner_modules() -> tuple[ModuleType, ...]:
    """Every owner module of this package, in name order: the one already imported
    if there is one, else imported now. A file whose name is not an identifier is
    no module of the package and is skipped."""
    owners = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda info: info.name):
        if not info.name.isidentifier():
            continue
        name = f"{__name__}.{info.name}"
        owners.append(sys.modules.get(name) or importlib.import_module(name))
    return tuple(owners)


def compose(namespace: dict[str, Any], owners: Iterable[ModuleType]) -> None:
    """Bind on *namespace* what *owners* define, and run every function they define on it.

    *namespace* is the runner's ``globals()``. The runner calls this once, after
    its own body has bound every name. First every name an owner binds that the
    runner lacks is bound on it (:func:`_bind`), so ``chat_runner.<name>`` resolves
    for every owner's function, class and constant without a by-name re-export;
    then the shared ``kiro_crew._facade_compose.compose``, given no host class,
    replaces every binding of an owner function -- in the runner, in the owners and
    in the owners' classes -- with its rebound copy, and no module is left holding
    the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a runner imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every runner import and their
    methods follow the most recent one.
    """
    owners = tuple(owners)
    _bind(namespace, owners)
    _facade_compose(namespace, owners)


def _bind(namespace: dict[str, Any], owners: tuple[ModuleType, ...]) -> None:
    """Bind on *namespace* every name an owner binds at runtime that *namespace* lacks.

    A module object is left out (the runner imports the modules its owners read) and
    so are dunder names and ``TYPE_CHECKING``. A name both already bind must be the
    same object: owner functions read the runner's binding, so a different object
    there would be a different value from the one the owner's own code evaluated,
    and the import fails rather than run with two.
    """
    for owner in owners:
        for name, value in vars(owner).items():
            if name.startswith("__") or name == "TYPE_CHECKING" or isinstance(value, ModuleType):
                continue
            held = namespace.get(name, _ABSENT)
            if held is _ABSENT:
                namespace[name] = value
            elif held is not value:
                raise ImportError(
                    f"{owner.__name__}.{name} and {namespace.get('__name__')}.{name} are "
                    "different objects; one namespace cannot hold both"
                )


_ABSENT = object()
