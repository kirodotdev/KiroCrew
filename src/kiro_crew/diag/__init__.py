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
"""

from __future__ import annotations

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


def __getattr__(name: str) -> Any:
    """Resolve the re-exported names on first use (PEP 562)."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache, so the import cost is paid once
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
