"""Docs name the Connections page by the path the dashboard really has.

The Connections cards (Connect, Disconnect) are a tab of the sidebar section
labelled ``nav.agent_capabilities`` ("Customize"). The Settings tab with the
``connections`` key is labelled "OAuth Apps" and only holds the client ID and
secret. So ``Capabilities → Connections`` and ``Settings → Connections`` both
send the reader to a page that does not exist. This test reads the three labels
from the English catalog, so a rename there fails here first, and then fails on
any doc path that reaches Connections through another parent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CATALOG = ROOT / "website" / "src" / "i18n" / "locales" / "en.manual.json"
DOC_ROOTS = (ROOT / "src" / "kiro_crew" / "docs", ROOT / "docs")


def _labels() -> tuple[str, str, str]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    return (
        catalog["nav"]["agent_capabilities"],
        catalog["pages"]["capabilitiesPage"]["connections_label"],
        catalog["settings"]["tabs"]["connections"]["label"],
    )


def _wrong_parent_hits(section: str, tab: str) -> list[str]:
    # Any word or two, an arrow (→, -> or >), then the tab name. `\s` spans a
    # line wrap, and bold markers around either part are ignored.
    pattern = re.compile(rf"\b(\w+(?: \w+)?)\**\s*(?:→|->|>)\s*\**\s*{re.escape(tab)}\b")
    hits = []
    for doc_root in DOC_ROOTS:
        for path in sorted(doc_root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                parent = match.group(1)
                if parent.split()[-1] not in ("Capabilities", "Settings"):
                    continue
                if parent == section:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    return hits


def test_labels_match_the_dashboard() -> None:
    assert _labels() == ("Customize", "Connections", "OAuth Apps")


def test_connections_paths_go_through_customize() -> None:
    section, tab, settings_tab = _labels()
    hits = _wrong_parent_hits(section, tab)
    assert not hits, (
        f"Write '{section} → {tab}' for the Connections cards, or "
        f"'Settings → {settings_tab}' for the client ID and secret:\n" + "\n".join(hits)
    )
