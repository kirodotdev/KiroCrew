"""Every ``respond(`` in the stdio dispatch loop either settles its request or is recorded as unarmed.

``on_response_outcome`` hooks (a ``spawn_sub_agents`` commit or drop) run only
when ``_settle_response_outcome`` is called for their request. A path that
answers an ARMED call with ``respond(`` and never settles it leaves the hook
waiting until the loop exits, and its collection's claim expires into a
duplicate turn. So each ``respond(`` either sits in a ``try`` whose ``finally``
settles the request, or answers a request that was never armed (a parse error,
an unknown method, a refusal before dispatch). The second kind is counted per
enclosing function in ``_UNARMED``: a new ``respond(`` path changes a count and
fails here until it settles, or is reviewed as unarmed and recorded.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import Counter

from kiro_crew import mcp_shared

#: ``respond(`` calls with no settle beside them, per enclosing function, each
#: answering a request ``_arm_response_outcome`` never armed.
_UNARMED = {
    # initialize, tools/list, ping, a refusal before dispatch, an unknown method:
    # none of them runs a tool, so none is armed.
    "_dispatch": 7,
    # A ping, a busy refusal and tools/list answered while a worker runs: each
    # answers a request other than the worker's own.
    "_run_stdio_dispatch_loop": 3,
    # The refused call and every queued one, before dispatch.
    "_refuse_from_pruned_install": 2,
    # A dispatch that raised before its worker started (the worker arms itself).
    "_answer_internal_error": 1,
    # A tools/call refused at validation, before dispatch.
    "_refuse_tool_call": 1,
    # An invalid request, answered before dispatch.
    "_servable": 1,
}


def _is_call(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _settles(stmts: list[ast.stmt]) -> bool:
    return any(_is_call(n, "_settle_response_outcome") for s in stmts for n in ast.walk(s))


def unpaired_responds(source: str) -> Counter[str]:
    """Count, per innermost enclosing function, ``respond(`` calls not under a settling ``finally``."""
    tree = ast.parse(textwrap.dedent(source))
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, func: str, settled: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func, settled = node.name, False
        if isinstance(node, ast.Try):
            inner = settled or _settles(node.finalbody)
            for child in node.body + node.handlers + node.orelse:
                visit(child, func, inner)
            for child in node.finalbody:
                visit(child, func, settled)
            return
        if _is_call(node, "respond") and not settled:
            counts[func] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, func, settled)

    visit(tree, "<module>", False)
    return counts


def _loop_source() -> str:
    return inspect.getsource(mcp_shared._run_stdio_dispatch_loop)


def _unarmed_counts(source: str) -> dict[str, int]:
    return dict(unpaired_responds(source))


def test_every_respond_in_the_dispatch_loop_settles_or_is_recorded_unarmed() -> None:
    assert _unarmed_counts(_loop_source()) == _UNARMED, (
        "a respond( path changed in _run_stdio_dispatch_loop: settle its request in a finally "
        "(_settle_response_outcome) or, if it answers a request that was never armed, record it in _UNARMED"
    )


def test_the_armed_paths_are_the_ones_paired() -> None:
    """The healthy side: both armed answers (the sync path and the worker's
    delivery) are found under a settling ``finally``, so the sweep is not
    passing because it sees no ``respond(`` at all."""
    tree = ast.parse(textwrap.dedent(_loop_source()))
    paired = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _settles(node.finalbody):
            paired += sum(1 for s in node.body for n in ast.walk(s) if _is_call(n, "respond"))
    assert paired >= 2


def test_mutation_dropping_a_settle_fails_the_sweep() -> None:
    """Mutation pin: delete one ``_settle_response_outcome`` from a settling
    ``finally`` and the sweep reports a new unpaired ``respond(``."""
    source = textwrap.dedent(_loop_source())
    line = "_settle_response_outcome(req_id, written)"
    assert source.count(line) == 1
    mutated = source.replace(line, "pass")
    assert _unarmed_counts(mutated) != _unarmed_counts(source)
    # And a new respond( with no settle is caught too: one added right after
    # the settling try, in the same function (``_dispatch``).
    indent = next(ln for ln in source.splitlines() if ln.strip() == line)[: -len(line)]
    added = source.replace(line, f"{line}\n{indent[:-4]}respond(req_id, None)", 1)
    assert unpaired_responds(added)["_dispatch"] == unpaired_responds(source)["_dispatch"] + 1
