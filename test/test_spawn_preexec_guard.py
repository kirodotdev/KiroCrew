"""No spawn may hand CPython a ``preexec_fn`` (issue #935).

``preexec_fn`` forces a plain ``fork()`` of the multi-GB, ~118-thread gateway and
runs Python bytecode in the child before ``exec``. A lock another thread held at
fork time cannot be released there, and a child that wedges takes the whole
gateway with it: ``Popen._execute_child`` blocks on the event loop thread in an
unbounded ``os.read(errpipe_read, ...)`` with no ``await`` point for a timeout to
reach, and because ``child_exec()`` closes fds only AFTER ``preexec_fn``, the
orphan keeps a duplicate of every inherited fd -- the dashboard's listening socket
included.

Async spawns go through ``sandbox.create_subprocess_limited`` and synchronous ones
through ``sandbox.run_limited`` / ``sandbox.popen_limited``, all of which apply the
same limits after ``exec``. This is the tripwire that keeps a new call site from
quietly reintroducing the fork.

A synchronous spawn wedges the calling worker thread rather than the event loop,
so it is the milder half of the hazard -- but it is the same ``fork()`` of the same
process, and it is checked the same way. The synchronous check carries a shrink-only
``_SYNC_UNMIGRATED`` ratchet (empty: every synchronous spawn is migrated), keyed on
each spawn's ``argv`` expression rather than a bare count so that migrating one
spawn and adding a different one in the same function cannot cancel out. A NEW
synchronous site anywhere under ``src/kiro_crew`` fails immediately, and removing
an entry is the only way the ratchet changes without a visible diff a reviewer has
to approve.
"""

from __future__ import annotations

import ast
import functools
from collections import Counter
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


@functools.lru_cache(maxsize=1)
def _async_spawns_with_preexec() -> dict[str, int]:
    """Map ``<relpath>::<func>`` -> line for async spawns passing ``preexec_fn``.

    Cached: this AST-parses all ~630 files under ``src/kiro_crew`` and both tests in
    this file call it, so an unmemoized second pass re-parsed the whole tree for the
    same answer. The source tree cannot change mid-run. Callers must not mutate the
    returned dict -- both only read it.
    """
    found: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        # Cheap substring pre-filter: parsing is the expensive step, and a file with
        # no `preexec_fn` text cannot contain a match.
        source = path.read_text(encoding="utf-8")
        if "preexec_fn" not in source:
            continue
        tree = ast.parse(source, str(path))
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


# ---------------------------------------------------------------------------
# Synchronous spawns
# ---------------------------------------------------------------------------

# ``subprocess`` entry points that spawn. ``run``/``call``/``check_call``/
# ``check_output`` all funnel into ``Popen``, so every one of them reaches the
# same ``fork()``.
_SYNC_SPAWNS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

# The sync wrappers' own no-shim fallbacks, for a host where the shim cannot run
# (non-POSIX, or a truncated install) and dropping the resource caps would be
# worse than the fork risk. The async twin of this is in ``_ALLOWED``.
_SYNC_ALLOWED = frozenset(
    {
        "sandbox.py::run_limited",
        "sandbox.py::popen_limited",
    }
)

# Shrink-only ratchet: ``<relpath>::<func>`` -> the ``argv`` expression of each
# synchronous spawn in that function still on the ``preexec_fn`` path. Empty: every
# synchronous spawn goes through ``run_limited`` / ``popen_limited``. The ratchet
# stays so a NEW synchronous ``preexec_fn`` spawn anywhere under ``src/kiro_crew``
# fails immediately; nothing may be added here.
#
# Keyed on the argv EXPRESSION rather than a bare count so that migrating one
# spawn and adding a different one in the same function cannot cancel out: a count
# alone is unchanged by that swap, which would let a new fork-path spawn in an
# already-listed function pass unnoticed. Line numbers would also distinguish it,
# but they drift on any edit above the call and would fail on unrelated changes;
# the argv expression only changes when the call site itself does.
_SYNC_UNMIGRATED: dict[str, tuple[str, ...]] = {}


def _subprocess_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module aliases, directly-imported spawn names) for *tree*.

    Resolved per file rather than assuming the literal name ``subprocess``:
    ``import subprocess as sp`` and ``from subprocess import Popen`` both already
    appear in this tree, so a check keyed on the literal name would hand a future
    author a one-line way around the guard.
    """
    mods = {"subprocess"}
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    mods.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SYNC_SPAWNS:
                    direct.add(alias.asname or alias.name)
    return mods, direct


@functools.lru_cache(maxsize=1)
def _sync_spawns_with_preexec() -> dict[str, tuple[tuple[int, str], ...]]:
    """Map ``<relpath>::<func>`` -> ``(line, argv expression)`` per sync spawn.

    Cached for the same reason as the async scan: both tests below read it, and
    the source tree cannot change mid-run. Callers must not mutate the result.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "preexec_fn" not in source:
            continue
        tree = ast.parse(source, str(path))
        mods, direct = _subprocess_names(tree)
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                target = func.value
                spawns = func.attr in _SYNC_SPAWNS and (
                    isinstance(target, ast.Name) and target.id in mods
                )
            elif isinstance(func, ast.Name):
                spawns = func.id in direct
            else:
                spawns = False
            if not spawns:
                continue
            if not any(kw.arg == "preexec_fn" for kw in node.keywords):
                continue
            enclosing = "<module>"
            best = -1
            for f in funcs:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and f.lineno > best:
                    best, enclosing = f.lineno, f.name
            argv_expr = ast.unparse(node.args[0]) if node.args else "<no positional argv>"
            found.setdefault(f"{rel}::{enclosing}", []).append((node.lineno, argv_expr))
    return {key: tuple(sorted(sites)) for key, sites in found.items()}


def test_no_sync_spawn_forks_python_in_the_child():
    live = _sync_spawns_with_preexec()
    offenders: dict[str, list[tuple[int, str]]] = {}
    for key, sites in live.items():
        if key in _SYNC_ALLOWED:
            continue
        budget = Counter(_SYNC_UNMIGRATED.get(key, ()))
        for line, expr in sites:
            if budget[expr]:
                budget[expr] -= 1
            else:
                offenders.setdefault(key, []).append((line, expr))
    assert not offenders, (
        "These synchronous spawns pass preexec_fn, which forks the threaded "
        "gateway and runs Python in the child before exec:\n  "
        + "\n  ".join(
            f"{key} (line {line}, argv={expr})"
            for key, sites in sorted(offenders.items())
            for line, expr in sites
        )
        + "\n\nUse kiro_crew.sandbox.run_limited(...) or popen_limited(...) "
        "instead: they apply the same resource limits AFTER exec, where the "
        "process is single-threaded."
    )


def test_the_sync_exemptions_only_shrink():
    """An exemption that outlived its call site would mask the next regression."""
    live = _sync_spawns_with_preexec()
    stale_allowed = _SYNC_ALLOWED - set(live)
    assert not stale_allowed, sorted(stale_allowed)
    stale: dict[str, list[str]] = {}
    for key, allowed in _SYNC_UNMIGRATED.items():
        remaining = Counter(allowed) - Counter(expr for _, expr in live.get(key, ()))
        if remaining:
            stale[key] = sorted(remaining.elements())
    assert not stale, (
        "_SYNC_UNMIGRATED names spawns that are no longer on the preexec path -- "
        "migrate credit must be banked by removing these entries, or they mask a "
        "regression:\n  "
        + "\n  ".join(f"{key}: {', '.join(exprs)}" for key, exprs in sorted(stale.items()))
    )
