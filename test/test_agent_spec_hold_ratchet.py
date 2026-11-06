"""Ratchet: the rebuild's hold flag has one reader.

A held rebuild is two facts, ``_conductor_spec_held`` and an open unreadable-spec
episode, and a reader that looks at the first alone skips the retry or seeds the
ceiling baseline during an episode, with nothing turning red. Only
``_spec_hold_open`` and the template-only probe ``_held_rebuild_would_repeat``
(which needs the flag by itself, and says so) may read it; everything else asks
``_spec_hold_open``. The scan is :mod:`ast`, so a docstring or comment naming the
flag is not a read, and paths are compared as POSIX relative paths so the result is
the same on every OS.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

#: The raw hold flag, and the only functions that may read it.
_HOLD_FLAG = "_conductor_spec_held"
HOLD_READERS = frozenset(
    {
        "agent_parts/spec_unreadable.py::_spec_hold_open",
        "agent_parts/spec_rebuild.py::_held_rebuild_would_repeat",
    }
)

#: One worker parses the tree once for the whole module.
pytestmark = pytest.mark.xdist_group("tree_scan_agent_spec_hold_ratchet")


def hold_flag_readers(source: str, module: str) -> set[str]:
    """``{"<module>::<function>"}`` for every function in *source* that reads the hold flag."""
    tree = ast.parse(source)
    readers: set[str] = set()

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            loaded = (
                isinstance(child, ast.Name)
                and child.id == _HOLD_FLAG
                and isinstance(child.ctx, ast.Load)
            ) or (
                isinstance(child, ast.Attribute)
                and child.attr == _HOLD_FLAG
                and isinstance(child.ctx, ast.Load)
            )
            if loaded:
                readers.add(f"{module}::{'.'.join(scope) or '<module>'}")
            visit(child, scope)

    visit(tree, [])
    return readers


def _source_files() -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("testing/"):
            continue
        files.append((rel, path.read_text(encoding="utf-8")))
    return files


@functools.lru_cache(maxsize=1)
def _all_hold_readers() -> frozenset[str]:
    readers: set[str] = set()
    for rel, source in _source_files():
        readers |= hold_flag_readers(source, rel)
    return frozenset(readers)


def test_the_hold_flag_is_read_only_through_the_episode_predicate():
    """Only ``_spec_hold_open`` and the template-only probe read the raw hold flag."""
    readers = _all_hold_readers()
    assert readers == HOLD_READERS, (
        f"readers of {_HOLD_FLAG} outside the allowed set: {sorted(readers - HOLD_READERS)}; "
        f"allowed readers that no longer read it: {sorted(HOLD_READERS - readers)}. "
        "Ask _spec_hold_open() instead, so an open unreadable-spec episode counts as a hold."
    )


def test_a_raw_hold_read_is_reported():
    """Mutation pin: a reader of the raw flag outside the allowed set goes red."""
    source = (
        "_conductor_spec_held = False\n"
        "def settle():\n"
        "    global _conductor_spec_held\n"
        "    _conductor_spec_held = True\n"
        "def retry():\n"
        "    if not _conductor_spec_held:\n"
        "        return\n"
        "def via_module(agent_mod):\n"
        "    return agent_mod._conductor_spec_held\n"
        "def asks_the_predicate():\n"
        "    return _spec_hold_open()\n"
    )
    assert hold_flag_readers(source, "m.py") == {"m.py::retry", "m.py::via_module"}
