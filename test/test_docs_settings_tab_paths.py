"""User guides name Settings tabs the dashboard really has.

The guides under ``src/kiro_crew/docs`` ship in the package and tell a person
where to click: ``Settings → Browser``, ``Settings → Privacy``. The tabs are the
entries of the ``tabs`` list in ``website/src/pages/SettingsPage.tsx``, each
labelled through an i18n key. A guide that writes ``Settings → <name>`` for a
name that is no tab sends the reader to a page that does not exist. This test
reads the labels from that list and fails on any such path.

A path inside another app (``Discord Settings → Advanced``, macOS
``System Settings``) is not ours, so a ``Settings`` that follows an arrow or
another app's name is not checked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SETTINGS_PAGE = ROOT / "website" / "src" / "pages" / "SettingsPage.tsx"
LOCALES = ROOT / "website" / "src" / "i18n" / "locales"
GUIDES = ROOT / "src" / "kiro_crew" / "docs"

# `{ key: 'privacy', ..., label: i18nT('privacyDisclosure.settingsLabel'), ...`
_TAB_LABEL_KEY_RE = re.compile(r"\{\s*key:\s*'[a-z-]+',[^}]*?\blabel:\s*i18nT\('([^']+)'\)")

# `Settings`, an arrow (→, ->, › or >), then capitalised words. `\s` spans a
# line wrap, and bold markers around either part are ignored.
_PATH_RE = re.compile(r"\bSettings\**\s*(?:→|->|›|>)\s*\**\s*([A-Z][\w/]*(?:[ \t]+[A-Z/][\w/]*)*)")

# The word right before `Settings` when the path is inside another app.
_OTHER_APPS = frozenset({"Discord", "GitHub", "Slack", "System", "Windows", "Xcode"})


def _in_another_app(text: str, start: int) -> bool:
    before = text[:start].rstrip("* \t\n")
    if before.endswith(("→", "->", "›", ">")):
        return True
    words = before.split()
    return bool(words) and words[-1].strip("*(") in _OTHER_APPS


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


def _unknown_tab_hits() -> list[str]:
    labels = _tab_labels()
    hits = []
    for path in sorted(GUIDES.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in _PATH_RE.finditer(text):
            if _in_another_app(text, match.start()):
                continue
            if not _names_a_tab(match.group(1), labels):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    return hits


def test_tab_labels_include_the_known_tabs() -> None:
    labels = _tab_labels()
    for expected in ("Browser", "Developer", "Messaging Channels", "Privacy", "Secrets"):
        assert expected in labels


def test_pattern_skips_another_apps_settings() -> None:
    labels = _tab_labels()
    assert [m.group(1) for m in _PATH_RE.finditer("Settings → Decisions")] == ["Decisions"]
    assert not _names_a_tab("Decisions", labels)
    assert _names_a_tab("Developer → Feature Previews", labels)
    for text in (
        "**Discord → Settings → Advanced**",
        "Enable **Discord Settings → Advanced**",
        "(System Settings › Privacy & Security)",
    ):
        start = text.index("Settings")
        assert _in_another_app(text, start), text
    assert not _in_another_app("Open **Settings → Browser**", 7)


def test_guides_name_only_real_settings_tabs() -> None:
    hits = _unknown_tab_hits()
    assert not hits, (
        "Each 'Settings → <name>' must start with a real Settings tab label "
        "(the `tabs` list in website/src/pages/SettingsPage.tsx):\n" + "\n".join(hits)
    )
