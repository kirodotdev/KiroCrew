"""Live translation claims no start priority of its own.

Meeting translation is a per-line stream nobody waits on, so the session it
opens names no ``start_priority`` at all: the core's BACKGROUND default is the
whole statement. The core's claimer pins are in ``test/test_start_priority.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kiro_crew.apps.builtins.meetings.backend.domain import translate

_FUNCTION = "run_oneshot_translation"
_CALLEE = "get_or_create"


def _calls_in(path: Path, function: str, callee: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Attribute) and call.func.attr == callee)
                    or (isinstance(call.func, ast.Name) and call.func.id == callee)
                )
            ]
    raise AssertionError(f"{function} not found in {path}")


def test_an_unattended_stream_claims_nothing() -> None:
    calls = _calls_in(Path(translate.__file__), _FUNCTION, _CALLEE)
    assert calls, f"{_FUNCTION} no longer opens its session through {_CALLEE}"
    assert [k.arg for call in calls for k in call.keywords if k.arg == "start_priority"] == []
