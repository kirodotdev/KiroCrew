"""Every runtime caller of an unconditional task-store cancel is named here.

The off-loop claimer moves a claimed row ``admitted -> starting`` with one
generation-fenced write, then registers the run with no await in between
(``claim_and_start``). A cancel that carries an unstarted-row ``only_from``
never lands on that ``starting`` row. An unconditional one -- ``TaskStore.cancel``
with no ``only_from``, or ``WaitLedger.cancel_tree`` -- can, and the run then
registers over a cancelled row whose every write is refused as ``stale_result``.

So each such caller must either run before any claim (the boot sweeps), or
reach a row whose start is in flight through ``cancel``, which joins the start
(``_live_agent_ids`` counts ``_starting_claims``). This test lists them, so a
new one fails here and its author decides which of the two it is.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: ``(path under src/kiro_crew, enclosing qualname)`` -> why it is safe.
ALLOWED: dict[tuple[str, str], str] = {
    ("taskq/waits.py", "WaitLedger.cancel_tree"): "the ledger primitive itself",
    ("taskq/waits.py", "WaitLedger.rebuild"): (
        "orphan cancel: runs inside open_store, before the store is attached for claims"
    ),
    ("taskq/reconcile.py", "_settle_one"): (
        "tombstone settle: reconcile_on_boot runs inside open_store, before the store "
        "is attached for claims"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin._cancel_live_or_row"): (
        "a row in _live_agent_ids goes to cancel, which joins the start"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin._cancel_children_db"): (
        "live_ids comes from _live_agent_ids; those rows go to cancel"
    ),
}


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: list[tuple[str, int]] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "cancel_tree":
            self.found.append((".".join(self.scope), node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "cancel"
            and "store" in ast.unparse(func.value).lower()
            and not any(kw.arg == "only_from" for kw in node.keywords)
        ):
            self.found.append((".".join(self.scope), node.lineno))
        self.generic_visit(node)


def _callers() -> dict[tuple[str, str], list[int]]:
    out: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "taskq" not in text and "TaskStore" not in text:
            continue
        finder = _Finder()
        finder.visit(ast.parse(text, filename=str(path)))
        rel = path.relative_to(SRC).as_posix()
        for qualname, line in finder.found:
            out.setdefault((rel, qualname), []).append(line)
    return out


def test_every_unconditional_task_store_cancel_caller_is_accounted_for() -> None:
    found = _callers()
    unknown = {key: lines for key, lines in found.items() if key not in ALLOWED}
    assert not unknown, (
        "an unconditional task-store cancel (TaskStore.cancel without only_from, or "
        "WaitLedger.cancel_tree) can land on a `starting` row whose run the claimer "
        "is about to register. Run it only before any claim, or route a row in "
        "_live_agent_ids through cancel, then name the caller in ALLOWED with why: "
        + "; ".join(f"{p}:{q} (lines {lines})" for (p, q), lines in sorted(unknown.items()))
    )


def test_the_named_callers_still_exist() -> None:
    gone = sorted(set(ALLOWED) - set(_callers()))
    assert not gone, f"ALLOWED names callers that no longer cancel unconditionally: {gone}"
