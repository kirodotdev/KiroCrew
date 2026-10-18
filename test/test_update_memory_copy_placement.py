"""Every call that rewrites this install is preceded by the pre-update memory copy.

`kirocrew update`, the dashboard's apply button and the gateway's unattended applier
each take a fresh copy of every memory store before they rewrite the install, and refuse
when it did not land. The behaviour is tested per applier; this test pins the PLACEMENT,
so a rewrite added to one of these files without the copy fails here rather than relying
on a reviewer to notice.

Read from the syntax tree, not the text: in each top-level function that contains a
rewriting call, every rewrite must have a copy call after the previous rewrite and
before it, so one update path cannot borrow another path's copy. Nested functions (the
dashboard's background ``_apply`` worker) count as part of the function that schedules
them, because the copy runs in the handler before the worker is created.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

# The files whose functions rewrite this install. The managed-venv engine copies through
# its own ``before_promote`` hook (``wheel_apply.memory_snapshot_hook``), tested apart.
_APPLIER_FILES = (
    "cli_server_update.py",
    "dashboard/handlers/updates_apply.py",
    "slack/gateway_runtime/update_apply.py",
)

_COPY_CALLS = frozenset(
    {"_snapshot_memory_or_exit", "_snapshot_memory_or_refuse", "snapshot_memory_before_update"}
)
_POLICY_APPLY_CALLS = frozenset({"apply_policy_update", "_run_tracked_policy_apply"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _string_args(node: ast.Call) -> set[str]:
    """String constants among the call's arguments, one list or tuple deep."""
    found: set[str] = set()
    for arg in node.args:
        items = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
        for item in items:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                found.add(item.value)
    return found


def _is_copy(node: ast.Call) -> bool:
    """The copy step called directly, or handed to ``asyncio.to_thread`` and the like."""
    if _call_name(node) in _COPY_CALLS:
        return True
    return any(
        (isinstance(arg, ast.Name) and arg.id in _COPY_CALLS)
        or (isinstance(arg, ast.Attribute) and arg.attr in _COPY_CALLS)
        for arg in node.args
    )


def _is_rewrite(node: ast.Call) -> bool:
    """A git reset/fast-forward of the checkout, a policy apply, or the wheel installer."""
    if _call_name(node) in _POLICY_APPLY_CALLS:
        return True
    words = _string_args(node)
    if {"reset", "--hard"} <= words or {"merge", "--ff-only"} <= words:
        return True
    return _call_name(node) == "Popen" and {"sh", "-c"} <= words


def _function_sites(tree: ast.Module) -> list[tuple[str, list[int], list[int]]]:
    """``(function, rewrite lines, copy lines)`` for each top-level function that rewrites."""
    sites = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        rewrites = sorted(n.lineno for n in calls if _is_rewrite(n))
        if not rewrites or fn.name == "_run_tracked_policy_apply":
            continue
        copies = sorted(n.lineno for n in calls if _is_copy(n))
        sites.append((fn.name, rewrites, copies))
    return sites


def _uncovered(rewrites: list[int], copies: list[int]) -> list[int]:
    """Rewrite lines with no copy between them and the rewrite before them.

    A function with two update paths (the dashboard's policy apply, then its git
    fast-forward) has one copy per path. Counting any earlier copy would let the first
    path's copy cover the second path, so each rewrite needs a copy of its own, after
    the previous rewrite and before it.
    """
    missing, floor = [], 0
    for line in rewrites:
        if not any(floor < c < line for c in copies):
            missing.append(line)
        floor = line
    return missing


def _sites() -> list[tuple[str, str, list[int], list[int]]]:
    sites = []
    for rel in _APPLIER_FILES:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        sites.extend((rel, *site) for site in _function_sites(tree))
    return sites


def test_the_known_appliers_are_found() -> None:
    """A matcher that stopped matching would make the placement test pass vacuously."""
    found = {(rel, name) for rel, name, _, _ in _sites()}
    assert {
        ("cli_server_update.py", "_update"),
        ("cli_server_update.py", "_update_wheel"),
        ("dashboard/handlers/updates_apply.py", "api_update_apply"),
        ("slack/gateway_runtime/update_apply.py", "_auto_apply_update"),
    } <= found


@pytest.mark.parametrize(
    ("rel", "name", "rewrites", "copies"),
    [pytest.param(*site, id=f"{site[0]}::{site[1]}") for site in _sites()],
)
def test_every_rewrite_is_preceded_by_the_memory_copy(rel, name, rewrites, copies) -> None:
    missing = _uncovered(rewrites, copies)
    assert not missing, (
        f"{rel}::{name} rewrites the install at line(s) {missing} with no memory copy "
        f"since the previous rewrite (copies at {copies})"
    )


_TWO_PATHS = """
async def apply(policy):
    if policy:
        refused = await _snapshot_memory_or_refuse(state)
    await _run_tracked_policy_apply(apply_policy_update)
{git_copy}
    await asyncio.create_subprocess_exec("git", "merge", "--ff-only", target)
"""


@pytest.mark.parametrize(
    ("git_copy", "flagged"),
    [
        pytest.param("    refused = await _snapshot_memory_or_refuse(state)", False, id="own-copy"),
        pytest.param("", True, id="borrowed-copy"),
    ],
)
def test_a_second_path_cannot_borrow_the_first_paths_copy(git_copy, flagged) -> None:
    """Deleting the git path's own copy fails the pin, though the policy copy sits above."""
    ((_, rewrites, copies),) = _function_sites(ast.parse(_TWO_PATHS.format(git_copy=git_copy)))
    assert bool(_uncovered(rewrites, copies)) is flagged
