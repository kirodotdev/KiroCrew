"""The K23 read-after-exists-wait detector of ``test_flake_pattern_ratchet.py``.

On Windows ``exists()`` turns true the moment a writer's ``os.replace`` lands, while
that rename's handle or a scanner's can still refuse an open with ``PermissionError``,
and a writer that is still writing hands over a short file. So a wait on
``p.exists`` followed, in the same function, by a read of ``p`` fails at random.
Make the read the predicate:
``wait_until(lambda: p.read_text(), ignoring=(FileNotFoundError, PermissionError))``.
Known miss: a predicate that is a named function returning ``p.exists()``. The
detector takes the ratchet's parsed module (``calls``, ``enclosing_function``); the
ratchet owns counting and runs the cases kept here.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

_WAITS = frozenset({"wait_until", "async_wait_until"})
_READS = frozenset({"read_text", "read_bytes", "open"})

K23_TRIGGERS = ("exists",)


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _exists_target(predicate: ast.AST) -> str | None:
    """``p`` for a wait predicate ``p.exists`` or ``lambda: p.exists()``."""
    if isinstance(predicate, ast.Lambda):
        body = predicate.body
        if not (isinstance(body, ast.Call) and not body.args and not body.keywords):
            return None
        predicate = body.func
    if isinstance(predicate, ast.Attribute) and predicate.attr == "exists":
        return ast.unparse(predicate.value)
    return None


def _read_target(node: ast.Call) -> str | None:
    """``p`` for ``p.read_text()``, ``p.read_bytes()``, ``p.open()`` or ``open(p)``."""
    if isinstance(node.func, ast.Attribute) and node.func.attr in _READS:
        return ast.unparse(node.func.value)
    if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
        return ast.unparse(node.args[0])
    return None


def k23_read_after_exists_wait(module) -> Iterable[ast.AST]:
    """Each wait on ``p.exists`` that a read of ``p`` follows in the same function."""
    waits: dict[tuple[ast.AST | None, str], list[ast.Call]] = {}
    for node in module.calls:
        if _callee(node) in _WAITS and node.args:
            target = _exists_target(node.args[0])
            if target is not None:
                key = (module.enclosing_function(node), target)
                waits.setdefault(key, []).append(node)
    if not waits:
        return
    flagged: set[int] = set()
    for node in module.calls:
        target = _read_target(node)
        if target is None:
            continue
        for wait in waits.get((module.enclosing_function(node), target), ()):
            if wait.lineno < node.lineno and id(wait) not in flagged:
                flagged.add(id(wait))
                yield wait


K23_CASES: tuple[tuple[str, ...], str] = (
    (
        "def test_x(p):\n    wait_until(p.exists)\n    assert p.read_text() == 'x'\n",
        "async def test_x(tmp_path):\n    f = tmp_path / 'pid'\n"
        "    await async_wait_until(lambda: f.exists(), off_loop=True)\n"
        "    with open(f) as h:\n        h.read()\n    int(f.read_text())\n",
    ),
    # The read is the predicate; a wait with no read after it; a read in another
    # function; a different path read.
    "def test_x(p):\n"
    "    wait_until(lambda: p.read_text(), ignoring=(FileNotFoundError, PermissionError))\n"
    "    wait_until(p.exists)\n    assert q.read_text() == 'x'\n"
    "def test_y(p):\n    wait_until(p.exists)\n"
    "    def later():\n        return p.read_text()\n",
)
