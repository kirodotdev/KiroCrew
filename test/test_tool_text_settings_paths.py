"""Agent tool text names only Settings tabs the dashboard really has.

The MCP tools under ``src/kiro_crew/mcp_tools`` and ``src/kiro_crew/mcp_*.py``
describe themselves to the agent, and the agent repeats those words to the
person: "turn on X in Settings -> Y". The tabs are the entries of the ``tabs``
list in ``website/src/pages/SettingsPage.tsx``, each labelled through an i18n
key. A string that writes ``Settings -> <name>`` for a name that is no tab sends
the person to a page that does not exist. This test reads the labels from that
list and fails on any such path in a string literal of those modules.
Docstrings are developer notes, not tool text, so they are not checked.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src" / "kiro_crew"
SETTINGS_PAGE = ROOT / "website" / "src" / "pages" / "SettingsPage.tsx"
LOCALES = ROOT / "website" / "src" / "i18n" / "locales"

# `{ key: 'privacy', ..., label: i18nT('privacyDisclosure.settingsLabel'), ...`
_TAB_LABEL_KEY_RE = re.compile(r"\{\s*key:\s*'[a-z-]+',[^}]*?\blabel:\s*i18nT\('([^']+)'\)")

# `Settings`, an arrow (→, ->, › or >), then capitalised words.
_PATH_RE = re.compile(r"\bSettings\s*(?:→|->|›|>)\s*([A-Z][\w/]*(?:[ \t]+[A-Z/][\w/]*)*)")


def _lookup(catalog: dict, key: str) -> str | None:
    node: object = catalog
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def _tab_labels() -> list[str]:
    catalogs = [
        json.loads((LOCALES / name).read_text(encoding="utf-8"))
        for name in ("en.manual.json", "en.json")
    ]
    keys = _TAB_LABEL_KEY_RE.findall(SETTINGS_PAGE.read_text(encoding="utf-8"))
    assert len(keys) >= 10, f"could not read the tab list from {SETTINGS_PAGE}"
    labels = []
    for key in keys:
        label = next((v for c in catalogs if (v := _lookup(c, key)) is not None), None)
        assert label, f"tab label key {key!r} has no English string"
        labels.append(label)
    return labels


def _names_a_tab(name: str, labels: list[str]) -> bool:
    return any(name == label or name.startswith(label + " ") for label in labels)


def _tool_modules() -> list[Path]:
    return sorted({*(SRC / "mcp_tools").rglob("*.py"), *SRC.glob("mcp_*.py")})


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _unknown_tab_hits() -> list[str]:
    labels = _tab_labels()
    hits = []
    for path in _tool_modules():
        text = path.read_text(encoding="utf-8")
        if "Settings" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            for match in _PATH_RE.finditer(node.value):
                if not _names_a_tab(match.group(1), labels):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {match.group(0)!r}")
    return hits


def test_scan_covers_the_tool_modules() -> None:
    names = {p.name for p in _tool_modules()}
    assert "browser.py" in names
    assert any(n.startswith("mcp_") for n in names)


def test_pattern_reads_the_first_tab() -> None:
    labels = _tab_labels()
    found = [m.group(1) for m in _PATH_RE.finditer("in Settings -> Decisions, and")]
    assert found == ["Decisions"]
    assert not _names_a_tab("Decisions", labels)
    assert _names_a_tab("Developer -> Feature Previews", labels)
    assert _names_a_tab("Browser", labels)


def test_tool_text_names_only_real_settings_tabs() -> None:
    hits = _unknown_tab_hits()
    assert not hits, (
        "Each 'Settings -> <name>' in agent tool text must start with a real Settings "
        "tab label (the `tabs` list in website/src/pages/SettingsPage.tsx):\n" + "\n".join(hits)
    )
