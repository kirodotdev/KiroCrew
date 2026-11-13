"""Docs name the Connections page by the path the dashboard really has.

The Connections cards (Connect, Disconnect, and each card's OAuth app) are the
Settings tab with the ``connections`` key. Customize (``nav.agent_capabilities``)
has no Connections tab, so ``Customize → Connections`` and
``Capabilities → Connections`` send the reader to a page that does not
exist. This test reads the labels from the English catalog, so a rename there
fails here first, and then fails on any doc path that reaches Connections
through another parent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CATALOG = ROOT / "website" / "src" / "i18n" / "locales" / "en.manual.json"
DOC_ROOTS = (ROOT / "src" / "kiro_crew" / "docs", ROOT / "docs")
# Design records describe the layout of their own time.
SKIP = (ROOT / "docs" / "request-for-change",)


def _labels() -> tuple[str, str, str]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    return (
        catalog["nav"]["settings"],
        catalog["settings"]["tabs"]["connections"]["label"],
        catalog["nav"]["agent_capabilities"],
    )


def _wrong_parent_hits(section: str, tab: str, old_section: str) -> list[str]:
    # Any word or two, an arrow (→, -> or >), then the tab name. `\s` spans a
    # line wrap, and bold markers around either part are ignored.
    pattern = re.compile(rf"\b(\w+(?: \w+)?)\**\s*(?:→|->|>)\s*\**\s*{re.escape(tab)}\b")
    hits = []
    for doc_root in DOC_ROOTS:
        for path in sorted(doc_root.rglob("*.md")):
            if any(path.is_relative_to(skip) for skip in SKIP):
                continue
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                parent = match.group(1)
                if parent.split()[-1] not in ("Capabilities", old_section):
                    continue
                if parent == section:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    return hits


def test_labels_match_the_dashboard() -> None:
    assert _labels() == ("Settings", "Connections", "Customize")


def test_connections_paths_go_through_settings() -> None:
    section, tab, old_section = _labels()
    hits = _wrong_parent_hits(section, tab, old_section)
    assert not hits, f"Write '{section} → {tab}' for the Connections cards:\n" + "\n".join(hits)
