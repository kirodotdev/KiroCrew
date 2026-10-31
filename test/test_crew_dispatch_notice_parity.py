"""Guard: the Crew Mode dispatch notice kind and its copy keys match the frontend.

``CREW_DISPATCH_NOTICE_KIND`` in ``kiro_crew.dashboard.crew_dispatch_notice`` is
the ``meta["kind"]`` wire token the gateway stamps on the one-per-turn dispatch
notice row. The frontend matches that exact string in
``website/src/pages/chat/transcriptRenderers.tsx`` to render the row as
``CrewDispatchNoticeCard``, which looks up its text and button label under
``pages.chat.crewDispatchNotice.*`` in the English catalog. Nothing links the
two sides but the strings, so a rename on one side silently drops the row back to
a raw-English fallback (or to the default notice card) on the other. Pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.dashboard.crew_dispatch_notice import CREW_DISPATCH_NOTICE_KIND

_REPO = Path(__file__).resolve().parents[1]
_RENDERERS = _REPO / "website" / "src" / "pages" / "chat" / "transcriptRenderers.tsx"
_CARD = _REPO / "website" / "src" / "pages" / "chat" / "CrewDispatchNoticeCard.tsx"
_EN = _REPO / "website" / "src" / "i18n" / "locales" / "en.json"

_TEXT_KEY = "pages.chat.crewDispatchNotice.text"
_BUTTON_KEY = "pages.chat.crewDispatchNotice.openCrewTab"


def test_frontend_matches_the_backend_notice_kind() -> None:
    renderers = _RENDERERS.read_text(encoding="utf-8")
    assert f"=== '{CREW_DISPATCH_NOTICE_KIND}'" in renderers, (
        f"transcriptRenderers.tsx does not match meta.kind === '{CREW_DISPATCH_NOTICE_KIND}', "
        "so the gateway's dispatch notice renders as the default notice card with no button."
    )
    assert "CrewDispatchNoticeCard" in renderers


def test_the_card_reads_the_catalog_keys() -> None:
    card = _CARD.read_text(encoding="utf-8")
    assert _TEXT_KEY in card, f"CrewDispatchNoticeCard.tsx does not read {_TEXT_KEY}"
    assert _BUTTON_KEY in card, f"CrewDispatchNoticeCard.tsx does not read {_BUTTON_KEY}"


def test_english_catalog_defines_the_keys() -> None:
    en = json.loads(_EN.read_text(encoding="utf-8"))
    block = en["pages"]["chat"].get("crewDispatchNotice")
    assert isinstance(block, dict), "pages.chat.crewDispatchNotice missing from en.json"
    assert set(block) == {"text", "openCrewTab"}, block
    assert block["text"].strip() and block["openCrewTab"].strip()
