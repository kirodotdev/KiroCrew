"""Every executor-backed AutoNudge store write goes through ``_start_persistence``.

The shutdown drain is durable only because ``AutoNudgeService._start_persistence`` is
the single submission point for a store write that runs off the event loop: it
registers the write so ``shutdown()`` can await it. A ``_write_state`` (or ``_save``)
handed to an executor anywhere else would run unregistered, and a gateway stop could
leave it unfinished while reporting the drain complete. These tests walk the
service's sources and fail on such a submission.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew

_PACKAGE = Path(kiro_crew.__file__).resolve().parent
_SOURCES = (_PACKAGE / "autonudge.py", *sorted((_PACKAGE / "autonudge_service").glob("*.py")))
#: The store writers that fsync, and so must never run unregistered off the loop.
_WRITERS = frozenset({"_write_state", "_save"})
#: The calls that hand a callable to another thread.
_SUBMITTERS = frozenset({"run_in_executor", "to_thread", "submit"})
_SUBMISSION_POINT = "_start_persistence"


def _submissions(tree: ast.AST) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` for each thread hand-off given a store writer."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call):
                func = child.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called in _SUBMITTERS:
                    named = {
                        n.attr if isinstance(n, ast.Attribute) else n.id
                        for arg in (*child.args, *(kw.value for kw in child.keywords))
                        for n in ast.walk(arg)
                        if isinstance(n, (ast.Attribute, ast.Name))
                    }
                    if named & _WRITERS:
                        found.append((owner, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_store_writes_reach_an_executor_only_through_the_submission_point() -> None:
    offenders: list[str] = []
    at_the_point = 0
    for path in _SOURCES:
        for owner, line in _submissions(ast.parse(path.read_text(encoding="utf-8"))):
            if owner == _SUBMISSION_POINT:
                at_the_point += 1
            else:
                offenders.append(f"{path.name}:{line} in {owner}")
    # Not vacuous: the submission point itself still hands the writer to a thread.
    assert at_the_point >= 1
    assert offenders == []


def test_the_scan_finds_a_submission_outside_the_point() -> None:
    planted = ast.parse(
        "async def elsewhere(self):\n"
        "    await loop.run_in_executor(None, self._write_state, payload)\n"
        "    await asyncio.to_thread(lambda: self._save())\n"
        "    await loop.run_in_executor(None, self._serialize_state)\n"
    )
    assert _submissions(planted) == [("elsewhere", 2), ("elsewhere", 3)]
