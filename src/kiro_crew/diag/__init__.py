"""Gateway self-diagnostics: record first, ask later.

Debugging the gateway from outside it means writing ad-hoc scripts — grep the
log, stat a file, read ``/proc`` — from a sandbox that cannot see what the
gateway sees. Three things make that unreliable: the sandbox mounts empty
placeholders over fenced paths, so a listing proves nothing; the path gate can
refuse a shell call fail-closed under load; and the numbers that would answer
"what was happening at 21:50:44?" exist only in memory. The adaptive controller
samples the host every five seconds into a ring buffer
(:class:`kiro_crew.adaptive.controller.AdaptiveController`) and discards the
samples; :mod:`kiro_crew.dashboard.stall_enrichment` captures sockets only once
the loop has already wedged.

This package closes that gap from inside the process that owns the truth:

* :mod:`kiro_crew.diag.recorder` — a background task that appends one JSON line
  of host and gateway state every 30 seconds, so a question about a past
  instant has an answer instead of a shrug.
* :mod:`kiro_crew.diag.threads` — GIL and thread-state introspection. CPython
  exposes no "who holds the GIL" API, so this offers three corroborating
  signals plus one exact on-demand measurement, and labels which is which.
* ``kiro_crew.diag.procs`` — the process family tree (added separately).

Everything here is non-controlling diagnostics: it observes and records, but
never steers the gateway. Nothing kills a process, and nothing widens a
privilege: reaping stays with :mod:`kiro_crew.session_scope_reap`, and the
sampling paths use in-process introspection rather than ``ptrace``.

Submodules are resolved lazily through :func:`__getattr__`. The recorder starts
on the gateway boot path, where an eager import chain is a measurable cost, and
HTTP routes want ``diag.get_recorder()`` without paying for the thread module.
A re-exported name lives in exactly one place, the submodule that defines it, so
reading or writing it through this package reaches that submodule and the two
spellings of a name cannot hold different values.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from kiro_crew.diag.recorder import Recorder, get_recorder

__all__ = ["Recorder", "get_recorder"]

#: Attribute name -> (submodule, symbol). Kept explicit rather than derived so a
#: typo resolves to an AttributeError here instead of importing something
#: unexpected.
_LAZY: dict[str, tuple[str, str]] = {
    "Recorder": ("kiro_crew.diag.recorder", "Recorder"),
    "get_recorder": ("kiro_crew.diag.recorder", "get_recorder"),
}

#: Package attribute -> ``(owning module, symbol on that module)``. The recorder is
#: imported on first access and the value is read from it every time -- never cached
#: here -- so ``diag.get_recorder`` and ``diag.recorder.get_recorder`` name one
#: value whatever read it first.
_OWNED: dict[str, tuple[str, str]] = dict(_LAZY)

_OWNERS: dict[str, ModuleType] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that defines ``name``, importing it on first use."""
    module_name = _OWNED[name][0]
    owner = _OWNERS.get(module_name)
    if owner is None:
        owner = _OWNERS[module_name] = importlib.import_module(module_name)
    return owner


def __getattr__(name: str) -> Any:
    """Read a re-exported name from the module that owns it (:pep:`562`)."""
    if name not in _OWNED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), _OWNED[name][1])


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the module that owns it.

    Binding the name in this package's own namespace instead would shadow the
    owner permanently, because ``__getattr__`` runs only for a name the package
    does not already hold: the shadow would win every later read, and the owner's
    value would become unreachable through this package. Forwarding the write
    leaves one value for a test harness to remember and one to put back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWNED:
            setattr(_owner(name), _OWNED[name][1], value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _OWNED:
            delattr(_owner(name), _OWNED[name][1])
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


def __dir__() -> list[str]:
    return sorted(__all__)
