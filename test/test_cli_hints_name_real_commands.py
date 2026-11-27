"""Every ``kirocrew <command>`` a message tells a person to run is a real command.

A hint like "Start it with `kirocrew start`" reads as an instruction, so a name
the CLI does not register sends the reader to argparse's invalid-choice error at
the moment they are already stuck. The scan reads every string literal under
``src/kiro_crew`` that is not a docstring and checks the command named after a
backtick, or after ``run``, ``try``, ``use`` or ``with:``, against
``cli_help.COMMAND_GROUPS``. ``test_cli_help.py`` holds that table equal to the
commands the parser offers. ``mcp-*`` names are the agent backend's servers,
registered and hidden.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kiro_crew.cli_help import COMMAND_GROUPS

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: ``kirocrew <name>`` where the text tells someone to run it.
_HINT = re.compile(r"(?:`|\bwith: |\b(?:[Rr]un|[Tt]ry|[Uu]se):? )kirocrew ([a-z][a-z0-9-]*)")

_KNOWN = frozenset(name for _, rows in COMMAND_GROUPS for name, _ in rows)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _hinted_commands(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.extend((node.lineno, m.group(1)) for m in _HINT.finditer(node.value))
    return sorted(found)


def _unknown(name: str) -> bool:
    return name not in _KNOWN and not name.startswith("mcp-")


def test_every_hinted_command_is_registered():
    unknown = []
    for path in sorted(_SRC.rglob("*.py")):
        for line, name in _hinted_commands(path.read_text(encoding="utf-8")):
            if _unknown(name):
                unknown.append(f"{path.relative_to(_SRC).as_posix()}:{line}: kirocrew {name}")
    assert unknown == []


def test_the_scan_sees_each_hint_shape_and_skips_docstrings():
    source = '''
"""Module docstring naming `kirocrew nosuch` is not a hint."""

def f():
    """Neither is `kirocrew nosuch2` here."""
    a = "Start it with `kirocrew start`."
    b = f"run: kirocrew bogus {1}"
    c = "start it with: kirocrew gateway"
    d = "SELinux hosts with kirocrew under $HOME"
    return a, b, c, d
'''
    named = [name for _, name in _hinted_commands(source)]
    assert named == ["start", "bogus", "gateway"]
    assert [n for n in named if _unknown(n)] == ["start", "bogus"]
