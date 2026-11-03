"""The one read of the tag vocabulary that every persisted-tag prune goes through.

A persisted tag id (on a slot's metadata line, a folder record, a sidebar column) is
pruned against the vocabulary the process loaded from ``tags.json``. That vocabulary is
in one of three states, and this module is the only place that tells them apart:

* UNKNOWN -- ``tags.json`` was unreadable or not a list at boot
  (``state._tags_authoritative`` is False). :func:`known_tag_ids` returns ``None`` and
  :func:`prune_tag_ids` keeps every well-shaped id: pruning against an unknown (empty)
  vocabulary would strip every assignment, and the next save would make that durable.
* KNOWN-EMPTY -- the store parsed as ``[]``: an empty frozenset, which prunes every id
  (the user deleted the last tag).
* KNOWN -- the store parsed: ids it does not list are pruned.

A store that is readable but stale (a restored backup, a half-synced data home) parses
as KNOWN, and nothing in a parsed list tells it apart from a current one, so it prunes
like one. Every vocabulary mutation in this tree is commit-first, so the process never
produces such a store itself; whatever rule a future staleness signal needs is decided
here, once, for every prune site.

A leaf module (no dashboard imports) so the boot loader in ``state_parts`` and the slot
codec in ``slot_persistence`` can both use it without an import cycle.
"""

from __future__ import annotations

from typing import Any


def known_tag_ids(state: Any) -> frozenset[str] | None:
    """Return the vocabulary's tag ids, or ``None`` when the vocabulary is UNKNOWN.

    Only string ids count: a malformed vocabulary row (a hand-edited ``tags.json`` with a
    list-valued id) is "no id", never an unhashable member that would crash a reader.
    """
    if not getattr(state, "_tags_authoritative", True):
        return None
    return frozenset(
        tid
        for t in getattr(state, "_tags", None) or ()
        if isinstance(t, dict) and isinstance(tid := t.get("id"), str) and tid
    )


def prune_tag_ids(raw: Any, known: frozenset[str] | None) -> list[str]:
    """Filter a persisted ``tags`` / ``tag_ids`` value down to usable tag ids.

    Shape guards apply in every vocabulary state: ``raw`` must be a list (anything else
    yields ``[]``), and each entry must be a non-empty string BEFORE the membership test,
    so a non-string entry is dropped instead of raising ``unhashable type``. The result
    is order-preserving and de-duplicated. The vocabulary intersection applies only when
    ``known`` is not ``None`` (see the module docstring for the three states).
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tid in raw:
        if not isinstance(tid, str) or not tid or tid in seen:
            continue
        if known is not None and tid not in known:
            continue
        seen.add(tid)
        out.append(tid)
    return out
