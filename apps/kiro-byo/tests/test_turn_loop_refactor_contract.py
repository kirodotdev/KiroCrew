from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TURN_RUNNER = ROOT / "src" / "kiro_byo" / "turn_runner.py"


def _loop_function() -> ast.FunctionDef:
    tree = ast.parse(TURN_RUNNER.read_text(encoding="utf-8"))
    runner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TurnRunnerMixin"
    )
    return next(
        node for node in runner.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_provider_tool_loop"
    )


def test_provider_loop_split_keeps_historical_control_edges() -> None:
    fn = _loop_function()
    assert fn.end_lineno is not None
    assert fn.end_lineno - fn.lineno + 1 < 350

    continues = [node for node in ast.walk(fn) if isinstance(node, ast.Continue)]
    assert len(continues) == 10

    step_adds = []
    step_subs = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.AugAssign):
            continue
        target = node.target
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "state"
            and target.attr == "step"
        ):
            continue
        if isinstance(node.op, ast.Add):
            step_adds.append(node)
        elif isinstance(node.op, ast.Sub):
            step_subs.append(node)
    assert len(step_adds) == 1
    assert len(step_subs) == 4
    assert not any(isinstance(node, ast.Nonlocal) for node in ast.walk(fn))


def test_provider_loop_state_is_a_dataclass_not_nonlocal_sprawl() -> None:
    source = TURN_RUNNER.read_text(encoding="utf-8")
    assert "class _ProviderLoopState:" in source
    for field in (
        "step: int = 0",
        "tool_calls_total: int = 0",
        "input_tokens: int = 0",
        "relay_next_role: str = \"lead\"",
        "execution_guard: ExecutionGuard",
        "turn_state: TurnState",
    ):
        assert field in source
