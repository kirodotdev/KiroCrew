"""Every audit ``operation`` a builtin backend writes fits the route's charset.

A builtin spawned with the gateway route writes through ``SelClient``, which refuses an
operation the route would refuse, and a ``critical=True`` refusal fails the action it
guards. So a builtin's existing operation names must pass as they are, or its row
history changes and a critical write starts raising (md-notebook's
``md_notebook.settings.autoSync`` did, under a lower-case-only charset).

The scan reads source: a ``log_api_access`` call, direct or handed to
``asyncio.to_thread``, with ``operation=``. A string literal must match the route's
charset whole. An f-string's literal parts need only the safe characters. A name or an
expression is a dynamic site: it is not guessed, and the client refuses a bad value
with a warning.
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


def test_every_literal_builtin_operation_passes_the_route_and_the_client() -> None:
    whole, _parts = _scan()
    bad = [
        f"{rel}:{line} {op!r}"
        for rel, line, op in whole
        if not (sel_routes._NAME.fullmatch(op) and sel_client._OPERATION.fullmatch(op))
    ]
    assert bad == []


def test_every_fstring_part_of_a_builtin_operation_is_safe() -> None:
    _whole, parts = _scan()
    assert [f"{r}:{n} {t!r}" for r, n, t in parts if not _SAFE_PART.fullmatch(t)] == []
