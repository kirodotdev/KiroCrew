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


#: What the source text of any string whose value holds ``kirocrew `` contains:
#: the word followed by the space itself, or by the quote or backslash that ends
#: the literal when the space starts the next implicitly concatenated piece. A
#: source without it holds no hint, so the scan skips parsing it.
#: shortcut: a hint spelled with an escape (``kirocrew\x20``) or split inside the
#: word is missed; widen this pattern if a message is ever written so.
_CANDIDATE = re.compile(r"""kirocrew[ "'\\]""")

_DEFS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _scan(source: str) -> list[tuple[int, str]]:
    docstrings: set[int] = set()
    strings: list[ast.Constant] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, _DEFS):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node)
    found = []
    for node in strings:
        if id(node) not in docstrings:
            found.extend((node.lineno, m.group(1)) for m in _HINT.finditer(node.value))
    return sorted(found)


def _hinted_commands(source: str) -> list[tuple[int, str]]:
    if not _CANDIDATE.search(source):
        return []
    return _scan(source)


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


def test_the_candidate_filter_keeps_every_hint(monkeypatch):
    assert "kirocrew " in _HINT.pattern
    source = '''
def f():
    """`kirocrew nosuch` in a docstring."""
    a = "Start it with `kirocrew start`."
    b = ("Run `kirocrew" " bogus` first.")
    return a, b
'''
    assert _hinted_commands(source) == _scan(source) == [(4, "start"), (5, "bogus")]

    def _refuse(*args, **kwargs):
        raise AssertionError("a source with no candidate is parsed")

    monkeypatch.setattr(ast, "parse", _refuse)
    assert _hinted_commands("x = 'Run `kiro-crew start` in ~/.kirocrew/.'\n") == []
