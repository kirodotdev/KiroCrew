"""Conservative identity decisions, separate from broad alias retrieval."""

from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict
from typing import TYPE_CHECKING

from .store import MAX_ENTITY_ALIAS_LEN, MAX_ENTITY_ALIASES

if TYPE_CHECKING:
    from .store import KnowledgeStore

logger = logging.getLogger(__name__)

# SQLite's LOWER(): find_entity's canonical leg folds ASCII letters only.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

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


def resolve_or_add_entity(
    store: KnowledgeStore,
    name: str,
    entity_type: str,
    aliases: list[str],
    *,
    aliases_complete: bool = True,
    description: str | None = None,
) -> tuple[dict | None, int, str | None]:
    """Resolve again under the writer lock, then add the entity if still absent.

    For a caller whose unlocked :func:`resolve_entity` found nothing. Two ingests
    can miss the same new name at once, and nothing in ``entities`` is unique on
    it, so both would add a row. Holding ``BEGIN IMMEDIATE`` across the second
    lookup and the insert makes the later one find the earlier one's row.

    Returns ``(existing, tier, None)`` when the locked lookup finds an entity, or
    ``(None, 0, new_id)`` after adding one. ``add_entity`` commits the
    transaction; any failure rolls it back.
    """
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        existing, tier = resolve_entity(
            store, name, entity_type, aliases, aliases_complete=aliases_complete
        )
        if existing:
            db.execute("COMMIT")
            return existing, tier, None
        new_id = store.add_entity(
            name=name,
            entity_type=entity_type,
            description=description,
            aliases=aliases or None,
        )
        return None, 0, new_id
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def merge_split_entities(store: KnowledgeStore) -> int:
    """Merge entity pairs an alias bridge now joins; return how many were merged.

    Libraries ingested before aliases were stored hold one real-world entity as
    two nodes; the pair appears once a later ingest attaches the alias to one of
    them. A pair qualifies under the ingestion rule: one entity's stored alias is
    the other's canonical name (``split_entity_candidates``), the types agree and
    ``alias_merge_allowed`` holds; alias-to-alias overlap never pairs. Each of the
    two must have no other such partner, in either direction, so an ambiguous or
    chained group stays as it is, and no third entity of any type may answer to
    the bridge, since it would take the merged name's lookup. An entity storing
    ``MAX_ENTITY_ALIASES`` aliases counts as a partner but never merges: as with
    ingestion's overflow rule, the cap may have dropped an alias naming a third
    entity. The entity carrying the alias is kept and takes the other's mentions,
    relations and description. A pair merges only when every alias of the other
    is the kept entity's canonical name as ``find_entity`` matches it, exactly or
    under SQLite's ASCII-only ``LOWER()``, and has no cased letter beyond ASCII,
    so each stored name of the other that found either, in upper or lower case,
    still finds the kept one; an alias is never carried over, since one the rule refused
    (same script) would become a bridge on the next launch. A mutual pair keeps
    the older row unless only the newer one's merge qualifies.
    Run inside the store's ``maintenance_window``, so no ingest or bundle import
    writes an entity, alias or mention meanwhile.
    """
    pairs = []
    partners: dict[str, set[str]] = defaultdict(set)
    holders: dict[str, set[str]] = defaultdict(set)
    capped: set[str] = set()
    for keeper, target, bridge in store.split_entity_candidates():
        key = bridge.casefold()
        holders[key] |= {keeper["id"], target["id"]}
        if (keeper["entity_type"] or "concept") != (target["entity_type"] or "concept"):
            continue
        if not alias_merge_allowed(keeper["name"], target["name"], bridge):
            continue
        # Such an alias finds the keeper by its canonical name in upper or lower
        # case, before and after; LOWER() folds no cased letter beyond ASCII.
        name = keeper["name"].translate(_ASCII_LOWER)
        keeps_names = all(
            a.translate(_ASCII_LOWER) == name
            and all(c.isascii() or c.lower() == c.upper() for c in a)
            for a in target["aliases"]
        )
        pairs.append((keeper["id"], target["id"], key, keeps_names))
        partners[keeper["id"]].add(target["id"])
        partners[target["id"]].add(keeper["id"])
        capped.update(e["id"] for e in (keeper, target) if len(e["aliases"]) >= MAX_ENTITY_ALIASES)
    handled: set[str] = set()
    merged = 0
    for keep_id, target_id, key, keeps_names in pairs:
        if not keeps_names or holders[key] != {keep_id, target_id}:
            continue
        # A mutual pair is listed twice; the keeper with the older row wins.
        if keep_id in handled or partners[keep_id] != {target_id}:
            continue
        if partners[target_id] != {keep_id} or {keep_id, target_id} & capped:
            continue
        handled |= {keep_id, target_id}
        try:
            store.merge_entities(keep_id, target_id)
        except Exception:  # noqa: BLE001 -- one pair must not stop the others
            logger.warning("Split entity merge skipped for %s", target_id, exc_info=True)
            continue
        merged += 1
    if merged:
        logger.info("Merged %d split entity pair(s)", merged)
    return merged
