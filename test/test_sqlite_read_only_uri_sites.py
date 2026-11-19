"""Every read-only SQLite open in the package builds its URI with ``sqlite_uri``.

``Path.as_uri()`` turns a Windows UNC path into ``file://host/share/...``, which
SQLite cannot open, so a store under a UNC data home fails to read. Only
``sqlite_uri.read_only_uri`` rewrites that to the empty-authority form, so an
inline ``as_uri()`` + ``mode=ro`` anywhere else is that bug again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew

_PACKAGE = Path(kiro_crew.__file__).resolve().parent
#: The builder itself, and the skill scripts that run without the package installed.
_EXEMPT = (_PACKAGE / "sqlite_uri.py", _PACKAGE / "builtin_skills")


def _builds_inline_read_only_uri(node: ast.AST) -> bool:
    """Whether *node* joins an ``as_uri()`` call and a ``mode=ro`` literal."""
    calls_as_uri = any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "as_uri"
        for sub in ast.walk(node)
    )
    names_mode_ro = any(
        isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "mode=ro" in sub.value
        for sub in ast.walk(node)
    )
    return calls_as_uri and names_mode_ro


def _inline_sites(source: str, rel: str) -> list[str]:
    sites = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) and _builds_inline_read_only_uri(node):
            sites.append(f"{rel}:{node.lineno}")
    return sorted(set(sites))


def test_no_module_builds_a_read_only_uri_from_as_uri_inline() -> None:
    found: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path == _EXEMPT[0] or _EXEMPT[1] in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "mode=ro" not in text:
            continue
        found += _inline_sites(text, path.relative_to(_PACKAGE).as_posix())
    assert found == [], (
        "build read-only SQLite URIs with kiro_crew.sqlite_uri.read_only_uri; an inline "
        f"as_uri() + '?mode=ro' cannot open a store on a UNC path: {found}"
    )


def test_the_scan_sees_each_inline_spelling() -> None:
    # The shapes the package used before every site moved to the builder.
    for source in (
        'u = p.as_uri() + "?mode=ro"',
        'u = f"{p.resolve().as_uri()}?mode=ro"',
        'u = p.as_uri() + ("?mode=ro" if wal else "?mode=ro&immutable=1")',
    ):
        assert _inline_sites(source, "x.py") == ["x.py:1"], source
    assert _inline_sites("u = read_only_uri(p)", "x.py") == []
    assert _inline_sites('u = read_only_uri(p) + "&immutable=1"', "x.py") == []
