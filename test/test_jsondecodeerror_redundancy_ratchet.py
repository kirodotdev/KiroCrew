"""No except tuple may pair ``json.JSONDecodeError`` with a bare ``ValueError``.

``json.JSONDecodeError`` subclasses ``ValueError``, so a handler naming both
catches exactly what ``ValueError`` alone catches -- the extra member is dead
weight that invites cargo-cult copies. Issue #5287 removed 94 such sites; this
ratchet keeps the count at zero. flake8 here carries no bugbear plugin, so
B014 does not enforce this and the pattern re-accumulates without a guard.

The scope rule is deliberately narrow: a tuple pairing ``json.JSONDecodeError``
with classes that are NOT ``ValueError`` (``OSError``, ``KeyError``,
``UnicodeDecodeError``, ...) is load-bearing and legal -- there the member is
what catches parse errors at all. Only the combination with a bare
``ValueError`` in the same tuple is redundant.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = (ROOT / "src" / "kiro_crew", ROOT / "test", ROOT / "scripts")


def _is_json_decode_error(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "JSONDecodeError"
        and isinstance(node.value, ast.Name)
        and node.value.id == "json"
    )


def _is_bare_value_error(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "ValueError"


def _redundant_handlers(tree: ast.AST) -> list[int]:
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if not isinstance(node.type, ast.Tuple):
            continue
        members = node.type.elts
        if any(_is_json_decode_error(m) for m in members) and any(
            _is_bare_value_error(m) for m in members
        ):
            offenders.append(node.lineno)
    return offenders


def test_no_except_tuple_pairs_jsondecodeerror_with_bare_valueerror() -> None:
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # not this ratchet's job
            # A violation needs BOTH names in the same except tuple, so a file
            # missing either token textually cannot contain one -- necessary,
            # not sufficient, so parsing the survivors still decides it. This
            # is the whole tree's ~1250+ modules down to a couple dozen: the
            # cost was one `ast.parse` per file for a check the text already
            # answers "no" for on every file that lacks one of the two names.
            if "JSONDecodeError" not in text or "ValueError" not in text:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue  # not this ratchet's job
            for lineno in _redundant_handlers(tree):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "except tuples pairing json.JSONDecodeError with a bare ValueError "
        "(the member is redundant -- JSONDecodeError subclasses ValueError, "
        f"drop it; see #5287): {offenders}"
    )
