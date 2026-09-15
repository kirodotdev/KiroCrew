#!/usr/bin/env python3
"""check_loop_bound_locks.py — no bare module-global asyncio primitives.

An ``asyncio.Lock`` binds to the event loop it is first used on; acquiring it
from a different loop raises ``RuntimeError`` on Python 3.10+. A module-global
``asyncio.Lock()`` is therefore a latent crash in any process that runs more
than one event loop over the module's lifetime (pytest-asyncio workers, a
gateway restart-in-process), and — issue #4800's headline — when the acquire
site sits under a blanket ``except Exception``, the crash does not even
surface: it turned into three separate order-dependent CI flake classes
(#4177, #4789) before the mechanism was found.

The repo-wide conversion (#4800) routed every module-global lock through
``kiro_crew.loop_lock.LoopBoundLock``, which keeps one inner lock per running
loop. This gate keeps the *declaration* form of the class extinct: it fails on
any NEW module-scope ``asyncio.Lock()`` / ``Event()`` / ``Queue()``
instantiation.

## What is flagged

An assignment (plain or annotated) whose value expression instantiates
``asyncio.Lock``, ``asyncio.Event`` or ``asyncio.Queue`` — directly, through a
module alias (``import asyncio as aio``), through a from-import (``from
asyncio import Lock``, ``from asyncio.locks import Lock``), or nested inside a
container/conditional (``_L = {"k": asyncio.Lock()}``) — executing at import
time: module body, a module-level ``if``/``try``/``with``/``match`` block, or
a class body.

## Known limits, on purpose

* ``asyncio.Semaphore`` shares the loop-binding defect but is deliberately NOT
  gated: the tree's five module-global semaphores expose their internal
  ``_value`` to production code and tests, so their conversion is a wider,
  separately-reviewed change (tracked on #4800). ``Condition`` and
  ``BoundedSemaphore`` ride with that deferral — gating a primitive whose
  remedy does not exist yet would only teach people to reach for the hatch.
* The *registry* form — a module-global dict whose values are locks created
  inside coroutines — is structurally invisible to a declaration scan. #4800
  converted the five known registries to hold ``LoopBoundLock`` values; new
  ones are a review concern, not a gate concern.
* Locks created inside functions/methods are correct (a running loop exists
  there) and never flagged. Aliases like ``Lock = asyncio.Lock`` create no
  instance and are not flagged.

## Escape hatch

A module-global that genuinely wants import-time binding can opt out with a
``loop-lock-ok`` comment on the declaration line, stating why a single-loop
lifetime is guaranteed.

## Usage

    python3 scripts/check_loop_bound_locks.py          # scan src/kiro_crew
    python3 scripts/check_loop_bound_locks.py --test   # self-test the rule
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOT = os.path.join("src", "kiro_crew")
MARKER = "loop-lock-ok"
PRIMITIVES = {"Lock", "Event", "Queue"}

REMEDY = (
    "Declare it as `kiro_crew.loop_lock.LoopBoundLock()` (locks), or create the "
    "primitive inside the coroutine that uses it. A module-global that truly "
    f"needs import-time binding can carry a `{MARKER}` comment saying why a "
    "single-loop lifetime is guaranteed."
)


def _module_scope_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return (asyncio module aliases, primitive from-import names).

    Reads only module-scope import statements (including module-level
    ``if``/``try`` blocks): a from-import inside a function must not turn an
    unrelated module-level ``Lock()`` call (say, ``threading.Lock`` bound to
    the same name) into a false positive.
    """
    aliases: set[str] = set()
    names: set[str] = set()
    for node, _in_class in _import_time_statements(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "asyncio" or mod.startswith("asyncio."):
                for alias in node.names:
                    if alias.name in PRIMITIVES:
                        names.add(alias.asname or alias.name)
    return aliases, names


def _instantiates_primitive(value: ast.expr, aliases: set[str], from_names: set[str]) -> str | None:
    """Return the primitive name when any call nested in ``value`` creates one."""
    for node in ast.walk(value):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id in aliases
            and fn.attr in PRIMITIVES
        ):
            return fn.attr
        if isinstance(fn, ast.Name) and fn.id in from_names:
            return fn.id
    return None


def _import_time_statements(tree: ast.Module):
    """Yield (stmt, in_class) for every statement that runs at import time.

    Walks the module body and the bodies of module-level compound statements
    (``if``/``try``/``with``/``for``/``while``/``match``) and class bodies —
    everything the interpreter executes on import — while never descending
    into function or lambda bodies, where a running loop exists and binding to
    it is correct.
    """
    stack: list[tuple[ast.stmt, bool]] = [(node, False) for node in tree.body]
    while stack:
        node, in_class = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node, in_class
        child_in_class = in_class or isinstance(node, ast.ClassDef)
        for field in ("body", "orelse", "finalbody"):
            for child in getattr(node, field, None) or []:
                if isinstance(child, ast.stmt):
                    stack.append((child, child_in_class))
        for handler in getattr(node, "handlers", None) or []:
            stack.extend((child, child_in_class) for child in handler.body)
        for case in getattr(node, "cases", None) or []:  # match statements
            stack.extend((child, child_in_class) for child in case.body)


def scan_file(path: str) -> list[tuple[int, str]]:
    """Return (lineno, message) violations for one Python file."""
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return []
    if "asyncio" not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    aliases, from_names = _module_scope_names(tree)
    if not aliases and not from_names:
        return []
    violations: list[tuple[int, str]] = []
    for node, _in_class in _import_time_statements(tree):
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
        else:
            continue
        primitive = _instantiates_primitive(value, aliases, from_names)
        if primitive is None:
            continue
        line_text = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if MARKER in line_text:
            continue
        violations.append(
            (
                node.lineno,
                f"module-global asyncio.{primitive}() binds to the import-time loop",
            )
        )
    return violations


def scan_tree(root: str) -> list[str]:
    """Scan every .py under ``root``; return formatted violation lines."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", "node_modules"}]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            for lineno, msg in scan_file(path):
                out.append(f"{os.path.relpath(path, REPO_ROOT)}:{lineno}: {msg}")
    return sorted(out)


# ── self-test ────────────────────────────────────────────────────────────────

_PROBES: list[tuple[str, str, int]] = [
    # (description, source, expected violation count)
    ("bare module-global Lock", "import asyncio\n_L = asyncio.Lock()\n", 1),
    ("annotated module-global Lock", "import asyncio\n_L: asyncio.Lock = asyncio.Lock()\n", 1),
    ("module-global Event", "import asyncio\n_E = asyncio.Event()\n", 1),
    ("module-global Queue", "import asyncio\n_Q = asyncio.Queue()\n", 1),
    (
        "module-global Semaphore deferred, not gated (see docstring)",
        "import asyncio\n_S = asyncio.Semaphore(2)\n",
        0,
    ),
    ("from-import spelling", "from asyncio import Lock\n_L = Lock()\n", 1),
    (
        "from-import of the locks submodule",
        "from asyncio.locks import Lock\n_L = Lock()\n",
        1,
    ),
    ("module alias spelling", "import asyncio as aio\n_L = aio.Lock()\n", 1),
    (
        "renamed from-import",
        "from asyncio import Lock as _AsyncLock\n_L = _AsyncLock()\n",
        1,
    ),
    (
        "lock nested in a container value",
        'import asyncio\n_L = {"k": asyncio.Lock()}\n',
        1,
    ),
    (
        "lock in a conditional expression",
        "import asyncio\n_L = asyncio.Lock() if True else None\n",
        1,
    ),
    (
        "module-level try block",
        "import asyncio\ntry:\n    _L = asyncio.Lock()\nexcept Exception:\n    pass\n",
        1,
    ),
    (
        "module-level if block",
        "import asyncio\nif True:\n    _L = asyncio.Lock()\n",
        1,
    ),
    (
        "module-level match body",
        "import asyncio\nmatch 1:\n    case 1:\n        _L = asyncio.Lock()\n",
        1,
    ),
    (
        "class-body lock",
        "import asyncio\nclass C:\n    lock = asyncio.Lock()\n",
        1,
    ),
    (
        "escape hatch honoured",
        "import asyncio\n_L = asyncio.Lock()  # loop-lock-ok: single-loop tool process\n",
        0,
    ),
    (
        "LoopBoundLock is the remedy, not a violation",
        "from kiro_crew.loop_lock import LoopBoundLock\n_L = LoopBoundLock()\n",
        0,
    ),
    (
        "function-local lock is fine",
        "import asyncio\nasync def f():\n    lock = asyncio.Lock()\n    return lock\n",
        0,
    ),
    (
        "method-local lock is fine",
        "import asyncio\nclass C:\n    def f(self):\n        self._lock = asyncio.Lock()\n",
        0,
    ),
    ("alias without a call is fine", "import asyncio\nLock = asyncio.Lock\n", 0),
    (
        "function-local from-import must not poison module scope",
        "import threading\ndef f():\n    from asyncio import Lock\n    return Lock()\n"
        "Lock = threading.Lock\n_L = Lock()\n",
        0,
    ),
]


def self_test() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, (desc, source, expected) in enumerate(_PROBES):
            path = os.path.join(tmp, f"probe_{i}.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
            got = len(scan_file(path))
            status = "ok" if got == expected else "FAIL"
            if got != expected:
                failures += 1
            print(f"  [{status}] {desc}: expected {expected}, got {got}")
    if failures:
        print(f"self-test: {failures} probe(s) failed", file=sys.stderr)
        return 1
    print(f"self-test: all {len(_PROBES)} probes passed")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_test()
    root = os.path.join(REPO_ROOT, SCAN_ROOT)
    violations = scan_tree(root)
    if violations:
        print(
            "check_loop_bound_locks: bare module-global asyncio primitive(s) found.\n"
            "These bind to the import-time (or first-use) event loop and raise\n"
            "RuntimeError when acquired from another loop (Python 3.10+). See #4800.\n",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  {line}", file=sys.stderr)
        print(f"\n{REMEDY}", file=sys.stderr)
        return 1
    print("check_loop_bound_locks: no bare module-global asyncio primitives.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
