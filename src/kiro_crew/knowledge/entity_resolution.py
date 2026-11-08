"""Conservative identity decisions, separate from broad alias retrieval."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from .store import MAX_ENTITY_ALIAS_LEN

if TYPE_CHECKING:
    from .store import KnowledgeStore

# Short Latin names deliberately lose automatic alias merging: spelling alone
# cannot distinguish a real short name from an abbreviation such as CR or BTS.
_SHORT_LATIN_NAME_MAX = 4
_SCRIPT_PREFIXES = (
    "LATIN",
    "HANGUL",
    "GREEK",
    "CYRILLIC",
    "ARABIC",
    "HEBREW",
    "DEVANAGARI",
    "HIRAGANA",
    "KATAKANA",
)
_JAPANESE_SCRIPTS = frozenset({"HAN", "HIRAGANA", "KATAKANA"})


def sqlite_text(text: object) -> bool:
    """Reject malformed extracted names before any SQLite bind."""
    if not isinstance(text, str) or not text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _name_profile(text: str) -> frozenset[str] | None:
    """Known letters only; NFKC affects this decision, never persisted spellings."""
    scripts: set[str] = set()
    compact = []
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if category.startswith("L"):
            name = unicodedata.name(char, "")
            if name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
                script = "HAN"
            elif name == "KATAKANA-HIRAGANA PROLONGED SOUND MARK":
                if not scripts or not scripts <= _JAPANESE_SCRIPTS:
                    return None
                continue
            else:
                script = next((s for s in _SCRIPT_PREFIXES if name.startswith(s + " ")), "")
            if not script:
                return None
            scripts.add(script)
            compact.append(char)
        elif category == "Nd":
            compact.append(char)
        elif category in {"Mn", "Mc"} and scripts:
            continue
        elif category.startswith("P") or category == "Zs":
            continue
        else:
            # Controls, format characters, symbols and unknown characters abstain.
            return None
    if not scripts:
        return None
    if scripts == {"LATIN"} and len(compact) <= _SHORT_LATIN_NAME_MAX:
        return None
    if len(scripts) > 1 and not scripts <= _JAPANESE_SCRIPTS:
        return None
    return frozenset(scripts)


def alias_merge_allowed(name: str, canonical: str, bridge: str) -> bool:
    """Require disjoint supported scripts and no short abbreviation endpoint.

    Han with kana is a supported Japanese profile. Shared scripts (including
    Han shared by Chinese and Japanese names) abstain. Cross-script spelling
    is an eligibility filter, not proof that an extracted translation is true.
    """
    # At the storage length cap, a bridge may be a truncated spelling of a
    # different name. Persisted aliases do not record whether truncation occurred.
    if len(bridge) >= MAX_ENTITY_ALIAS_LEN:
        return False
    incoming = _name_profile(name)
    stored = _name_profile(canonical)
    return bool(incoming and stored and incoming.isdisjoint(stored) and _name_profile(bridge))


def resolve_entity(
    store: KnowledgeStore,
    name: str,
    entity_type: str,
    aliases: list[str],
    *,
    aliases_complete: bool = True,
) -> tuple[dict | None, int]:
    """Canonical priority, otherwise exactly one eligible target across both routes."""
    resolved = None
    tier = 0
    for candidate, candidate_tier, bridge in store.entity_identity_candidates(
        name, aliases, canonical_only=not aliases_complete
    ):
        if candidate_tier == 1:
            return candidate, 1
        if (candidate.get("entity_type") or "concept") != entity_type:
            continue
        if not alias_merge_allowed(name, candidate["name"], bridge):
            continue
        if resolved is not None and candidate["id"] != resolved["id"]:
            return None, 0
        resolved = candidate
        # If both routes reach the same id, no enrichment is needed: the
        # incoming name already appears in that entity's persisted aliases.
        tier = min(tier or candidate_tier, candidate_tier)
    return resolved, tier
