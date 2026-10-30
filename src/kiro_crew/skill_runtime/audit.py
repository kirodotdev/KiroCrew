"""Read-only overlap audit across pending candidates and live skills.

``audit`` compares skills pairwise on description and positive-trigger word sets and
reports connected clusters. It never writes, stages, approves or dismisses anything:
acting on a cluster stays a human step on the Skills page.

The pairwise pass is O(n²) and the inputs are untrusted frontmatter, so every limit is
enforced while results are admitted rather than trimmed afterwards: entry and relation
counts, the text kept per field and the bytes read per file. Omission counts let a
caller tell a complete audit from a bounded one.
"""

from __future__ import annotations

import heapq
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

# ``audit`` uses ``skills.auto_similarity_threshold`` for the duplicate cutoff.
# OVERLAP is the lower symmetric relationship boundary; COVERAGE is the share of
# a pending candidate's description words a live skill must contain before the
# candidate counts as covered by it (``subsumed``).
_AUDIT_OVERLAP_THRESHOLD = 0.5
_AUDIT_COVERAGE_THRESHOLD = 0.85
# The pairwise pass is O(n²), so both the skills admitted and the matching pairs
# retained are capped inside ``audit`` itself, before any caller-side truncation.
_AUDIT_MAX_ENTRIES = 400
_AUDIT_MAX_RELATIONS = 2000
# Bound untrusted frontmatter before tokenization. The audit retains word sets,
# so capping only the response would still let one field consume unbounded RAM.
_AUDIT_MAX_TEXT_CHARS = 4096
# A live skill name longer than this is omitted and counted, not truncated: a
# cut name would not resolve to its row.
_AUDIT_MAX_NAME_CHARS = 256
# Largest pending ``.meta.json`` and largest live ``SKILL.md`` the audit reads. A
# larger candidate file is audited as having no description or triggers; a larger
# live file is omitted and counted.
_AUDIT_MAX_META_BYTES = 64 * 1024


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _trigger_words(triggers: str) -> set[str]:
    """Positive trigger words only: a ``!``-prefixed trigger is an exclusion."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    words: set[str] = set()
    for raw in (triggers or "").split(","):
        phrase = raw.strip()
        if phrase and not phrase.startswith("!"):
            words.update(sk.words_of(phrase))
    return words


def _is_builtin(skill_file: Path) -> bool:
    """True for a package-owned builtin skill (shipped tree or provenance-marked copy)."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    try:
        skill_file.resolve().relative_to(sk._BUILTIN_SKILLS_DIR.resolve())
        return True
    except (OSError, ValueError):
        pass
    return os.path.lexists(skill_file.parent / sk._PROVENANCE_MARKER)


def _pending_slugs_bounded(loader: SkillsLoader, limit: int) -> tuple[list[str], int]:
    """Return the first ``limit`` pending slugs in order, and how many exist.

    Only ``limit`` names are kept while the directory is scanned, so a large
    queue costs a scan, never retained state.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    root = loader._pending_root()
    if limit <= 0 or not root.is_dir():
        return [], 0
    seen = 0

    def slugs() -> Iterator[str]:
        nonlocal seen
        with os.scandir(root) as it:
            for item in it:
                if not sk._AUTO_NAME_PATTERN.match(item.name):
                    continue
                child = root / item.name
                if child.is_dir() and (child / "SKILL.md").exists():
                    seen += 1
                    yield item.name

    return heapq.nsmallest(limit, slugs()), seen


def audit(loader: SkillsLoader) -> dict[str, object]:
    """Audit pending and live skills pairwise and return bounded overlap data.

    Pending skills are admitted first. Untrusted description and trigger text is
    capped before word sets are built.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    duplicate_threshold = sk.KiroCrewConfig.load().skills.auto_similarity_threshold
    overlap_threshold = _AUDIT_OVERLAP_THRESHOLD

    entries: list[dict] = []
    pending_slugs, pending_seen = _pending_slugs_bounded(loader, _AUDIT_MAX_ENTRIES)
    omitted_entries = pending_seen - len(pending_slugs)
    for slug in pending_slugs:
        meta = loader._read_pending_meta(slug, max_bytes=_AUDIT_MAX_META_BYTES)
        description = str(meta.get("description", ""))[:_AUDIT_MAX_TEXT_CHARS]
        triggers = str(meta.get("triggers", ""))[:_AUDIT_MAX_TEXT_CHARS]
        # An update candidate is meant to overlap the live skill it replaces, so
        # that pair is skipped below rather than reported. ``target`` is untrusted
        # meta: only a string that resolves to an auto-skill name is kept.
        update_of: str | None = None
        target = meta.get("target")
        if meta.get("kind") == "update" and isinstance(target, str):
            target_slug = loader._auto_slug_from_name(target[:_AUDIT_MAX_NAME_CHARS])
            if loader._is_pending_slug_safe(target_slug):
                update_of = f"{sk.AUTO_SKILL_NAMESPACE}/{target_slug}"
        entries.append(
            {
                "id": f"pending:{slug}",
                "kind": "pending",
                # Built from the slug, never read from ``.meta.json``: the slug
                # already passed the 64-character slug grammar, while the stored
                # name is untrusted and unbounded.
                "name": f"{sk.AUTO_SKILL_NAMESPACE}/{slug}",
                "slug": slug,
                "update_of": update_of,
                "description_words": _words(description),
                "trigger_words": _trigger_words(triggers),
            }
        )
    # The visible set, so skills of a disabled app stay out of the audit just as
    # they stay off the Skills page.
    for name, skill_file, within in loader._iter_visible():
        if len(entries) >= _AUDIT_MAX_ENTRIES or len(name) > _AUDIT_MAX_NAME_CHARS:
            omitted_entries += 1
            continue
        # Cap the live SKILL.md on the read itself, and parse without caching,
        # so nothing the audit reads lands in the long-lived frontmatter cache.
        # A file over the cap is refused before its bytes are retained, however
        # it changed after enumeration.
        try:
            raw = loader._read_enumerated_skill_bytes(
                skill_file, within, max_bytes=_AUDIT_MAX_META_BYTES
            )
        except OSError:
            raw = None
        if raw is None:
            omitted_entries += 1
            continue
        meta = loader._parse_frontmatter_text(sk._decode_skill_text(raw, strict=False))
        description = str(meta.get("description", ""))[:_AUDIT_MAX_TEXT_CHARS]
        triggers = str(meta.get("triggers", ""))[:_AUDIT_MAX_TEXT_CHARS]
        entries.append(
            {
                "id": f"live:{name}",
                "kind": "live",
                "name": name,
                "builtin": _is_builtin(skill_file),
                "description_words": _words(description),
                "trigger_words": _trigger_words(triggers),
            }
        )

    relations: list[dict] = []
    omitted_relations = 0
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(entries))}
    for left_index in range(len(entries)):
        for right_index in range(left_index + 1, len(entries)):
            left = entries[left_index]
            right = entries[right_index]
            if (
                left["kind"] == "live"
                and right["kind"] == "live"
                and (left.get("builtin") or right.get("builtin"))
            ):
                continue
            score = max(
                _jaccard(left["description_words"], right["description_words"]),
                _jaccard(left["trigger_words"], right["trigger_words"]),
            )
            covered = False
            if left["kind"] != right["kind"]:
                pending = left if left["kind"] == "pending" else right
                live = left if left["kind"] == "live" else right
                if pending["update_of"] is not None and pending["update_of"] == live["name"]:
                    continue
                trigger_subset = bool(pending["trigger_words"]) and (
                    pending["trigger_words"] <= live["trigger_words"]
                )
                candidate_words = pending["description_words"]
                coverage = (
                    len(candidate_words & live["description_words"]) / len(candidate_words)
                    if candidate_words
                    else 0.0
                )
                covered = trigger_subset or coverage >= _AUDIT_COVERAGE_THRESHOLD
            if score >= duplicate_threshold:
                classification = "duplicate"
            elif covered:
                classification = "subsumed"
            elif score >= overlap_threshold:
                classification = "overlapping"
            else:
                continue
            if len(relations) >= _AUDIT_MAX_RELATIONS:
                omitted_relations += 1
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            relations.append(
                {
                    "classification": classification,
                    "score": round(score, 4),
                    "members": [left["id"], right["id"]],
                }
            )

    rank = {"overlapping": 0, "subsumed": 1, "duplicate": 2}
    clusters: list[dict] = []
    visited: set[int] = set()
    for start_index in range(len(entries)):
        if start_index in visited or not adjacency[start_index]:
            continue
        stack = [start_index]
        component: set[int] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        visited.update(component)
        member_ids = {entries[index]["id"] for index in component}
        component_relations = [
            relation for relation in relations if set(relation["members"]) <= member_ids
        ]
        members = []
        for index in sorted(component, key=lambda item: entries[item]["id"]):
            entry = entries[index]
            member = {"id": entry["id"], "kind": entry["kind"], "name": entry["name"]}
            if entry["kind"] == "pending":
                member["slug"] = entry["slug"]
            members.append(member)
        clusters.append(
            {
                "classification": max(
                    (relation["classification"] for relation in component_relations),
                    key=rank.__getitem__,
                ),
                "score": max(relation["score"] for relation in component_relations),
                "members": members,
                "relations": component_relations,
            }
        )
    clusters.sort(
        key=lambda cluster: (
            -rank[cluster["classification"]],
            -cluster["score"],
            tuple(member["id"] for member in cluster["members"]),
        )
    )
    return {
        "clusters": clusters,
        "omitted_entries": omitted_entries,
        "omitted_relations": omitted_relations,
    }
