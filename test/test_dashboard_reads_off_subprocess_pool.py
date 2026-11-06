"""Dashboard reads run on the discovery pool, never the subprocess pool.

Process-tree walks can hold every ``subprocess_executor()`` worker past their
callers' timeouts, so a quick dashboard read queued there waits behind them.
Each read named below must be handed to ``discovery_executor()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "dashboard"

_READS = [
    ("handlers/artifacts_parts/library_reads.py", "archived_session_names_in_folders"),
    ("chat_folder_suggest.py", "_match_by_project_dir"),
    ("chat_folder_suggest.py", "_folder_sample_titles"),
    ("handlers/artifacts_parts/artifact_folders.py", "_list_serialized"),
    ("handlers/terminal.py", "_session_cwd"),
    ("handlers/terminal.py", "_session_title"),
    ("handlers/terminal.py", "_scan"),
]


def _executors_for(path: Path, fn_name: str) -> list[str]:
    """Name of the pool factory at every ``run_in_executor(pool(), fn, ...)``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run_in_executor"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == fn_name
        ):
            continue
        pool = node.args[0]
        if isinstance(pool, ast.Call) and isinstance(pool.func, ast.Name):
            found.append(pool.func.id)
        else:
            found.append(ast.dump(pool))
    return found


@pytest.mark.parametrize(("rel", "fn_name"), _READS)
def test_read_is_submitted_to_the_discovery_pool(rel: str, fn_name: str) -> None:
    pools = _executors_for(_SRC / rel, fn_name)
    assert pools, f"{rel}: no run_in_executor call submits {fn_name}"
    assert set(pools) == {"discovery_executor"}, f"{rel}: {fn_name} runs on {pools}"
