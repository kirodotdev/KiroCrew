"""No async spawn may hand CPython a ``preexec_fn`` (issue #935).

``preexec_fn`` forces a plain ``fork()`` of the multi-GB, ~118-thread gateway and
runs Python bytecode in the child before ``exec``. A lock another thread held at
fork time cannot be released there, and a child that wedges takes the whole
gateway with it: ``Popen._execute_child`` blocks on the event loop thread in an
unbounded ``os.read(errpipe_read, ...)`` with no ``await`` point for a timeout to
reach, and because ``child_exec()`` closes fds only AFTER ``preexec_fn``, the
orphan keeps a duplicate of every inherited fd -- the dashboard's listening socket
included.

Async spawns go through ``sandbox.create_subprocess_limited``, which applies the
same limits after ``exec``. This is the tripwire that keeps a new call site from
quietly reintroducing the fork.

Synchronous ``subprocess.run`` / ``Popen`` spawns are NOT covered yet: they wedge
a worker thread rather than the event loop. Removing that exemption is tracked as
the follow-up to issue #935.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

# The one legitimate ``preexec_fn`` on an async spawn: the wrapper's own fallback
# for a host with no usable shim (non-POSIX, or a truncated install), where
# dropping the resource caps would be worse than the fork risk.
_ALLOWED = frozenset(
    {
        # The wrapper's own fallback for a host with no usable shim (non-POSIX, or
        # a truncated install), where dropping the resource caps would be worse
        # than the fork risk.
        "sandbox.py::create_subprocess_limited",
        # The user's interactive terminal. It carries NO resource policy (no
        # rlimits, no OOM bias), so the shim had nothing to deliver for it and
        # cost an interpreter startup on every terminal open. Its preexec_fn is a
        # single pre-resolved ioctl with no allocation and no lock acquisition --
        # the only shape where a fork-child callable is defensible. Residual risk
        # is accepted and documented at the call site.
        "dashboard/handlers/terminal.py::api_terminal_ws",
    }
)


def _async_spawns_with_preexec() -> dict[str, int]:
    """Map ``<relpath>::<func>`` -> line for async spawns passing ``preexec_fn``."""
    found: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not node.func.attr.startswith("create_subprocess_"):
                continue
            if not any(kw.arg == "preexec_fn" for kw in node.keywords):
                continue
            enclosing = "<module>"
            best = -1
            for f in funcs:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and f.lineno > best:
                    best, enclosing = f.lineno, f.name
            found[f"{rel}::{enclosing}"] = node.lineno
    return found


def test_no_async_spawn_forks_python_in_the_child():
    offenders = {k: v for k, v in _async_spawns_with_preexec().items() if k not in _ALLOWED}
    assert not offenders, (
        "These async spawns pass preexec_fn, which forks the threaded gateway and "
        "runs Python in the child before exec:\n  "
        + "\n  ".join(f"{key} (line {line})" for key, line in sorted(offenders.items()))
        + "\n\nUse kiro_crew.sandbox.create_subprocess_limited(...) instead: it "
        "applies the same resource limits AFTER exec, where the process is "
        "single-threaded. See issue #935."
    )


def test_the_allowlist_still_describes_a_real_fallback():
    """A stale exemption would mask the very regression this file guards."""
    live = _async_spawns_with_preexec()
    assert _ALLOWED <= set(live), sorted(_ALLOWED - set(live))
