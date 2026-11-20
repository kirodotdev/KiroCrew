"""Every audit ``operation`` a builtin backend writes is stored as written.

A builtin spawned with the gateway route writes through ``SelClient``, and both the
client and the route pass the operation through ``normalize_operation``. A builtin's
existing operation names must come out unchanged, or its row history changes
(md-notebook's ``md_notebook.settings.autoSync`` is mixed case).

The scan reads source: a ``log_api_access`` call, direct or handed to
``asyncio.to_thread``, with ``operation=``. A string literal must normalize to itself.
An f-string's literal parts need only the safe characters. A name or an expression is a
dynamic site: it is not guessed, and the client normalizes whatever value it gets.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kiro_crew.apps import sel_client, sel_routes

_BUILTINS = Path(sel_routes.__file__).parent / "builtins"
_SAFE_PART = re.compile(r"[A-Za-z0-9_.-]*")


def _scan() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """``(whole literals, f-string parts)`` as ``(file, line, text)``."""
    whole: list[tuple[str, int, str]] = []
    parts: list[tuple[str, int, str]] = []
    for path in sorted(_BUILTINS.rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        rel = path.relative_to(_BUILTINS).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            handed = any(
                isinstance(a, ast.Attribute) and a.attr == "log_api_access" for a in node.args
            )
            if name != "log_api_access" and not handed:
                continue
            for kw in node.keywords:
                value = kw.value
                if kw.arg != "operation":
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    whole.append((rel, node.lineno, value.value))
                elif isinstance(value, ast.JoinedStr):
                    parts += [
                        (rel, node.lineno, str(p.value))
                        for p in value.values
                        if isinstance(p, ast.Constant)
                    ]
    return whole, parts


def test_the_scan_finds_the_builtins_audit_sites() -> None:
    whole, parts = _scan()
    assert "md_notebook.settings.autoSync" in {op for _p, _l, op in whole}
    assert parts


def test_every_literal_builtin_operation_is_stored_as_written() -> None:
    whole, _parts = _scan()
    bad = [
        f"{rel}:{line} {op!r}"
        for rel, line, op in whole
        if sel_client.normalize_operation(op) != op
    ]
    assert bad == []


def test_every_fstring_part_of_a_builtin_operation_is_safe() -> None:
    _whole, parts = _scan()
    assert [f"{r}:{n} {t!r}" for r, n, t in parts if not _SAFE_PART.fullmatch(t)] == []
