"""Merge the Claude picker's registry rows with the rows its backend advertises.

:func:`kiro_crew.dashboard.handlers.agents._cc_models` keeps the entitlement filter,
the ``auto`` row and the configured default; this module decides which row each
advertised id lands on and which label that row shows.
"""

from __future__ import annotations

from collections.abc import Callable

from kiro_crew import model_registry


def merge_advertised_rows(
    registry_rows: list[dict], advertised: list[dict], key_of: Callable[[str], str]
) -> list[dict]:
    """Return *registry_rows* with *advertised* merged in, one row per model.

    *key_of* is the facade's ``_normalize_model_key``, passed in so a patch of
    ``agents._normalize_model_key`` reaches this merge too. An advertised id
    whose key matches a registry row takes that row's slot and supplies its wire
    id; two advertised ids that fold to one key keep a row each; an advertised id
    the registry lacks is appended.
    """
    merged: list[dict] = []
    seen: dict[str, int] = {}
    # A bare family alias names Claude Code's current model, not the versioned
    # registry entry it folds onto, so a versioned id claims that entry first.
    aliases = model_registry.CLAUDE_CODE_PASSTHROUGH_ALIASES
    advertised = sorted(advertised, key=lambda e: e.get("model_name", "") in aliases)
    # Advertised ids that carry a NATIVE label (``display_name`` differs from the
    # id -- i.e. the backend sent a real name, not the id-fallback
    # ``_advertised_cc_models`` fills in). Claude Code is authoritative for its
    # OWN picker text, so on a collision its label wins over the registry's,
    # which pins a stale version (bare ``opus`` -> "Opus 4.8 (1M context)" when
    # the backend calls it "Opus 5.5"). The wire id is always the advertised one.
    advertised_label: dict[str, dict] = {}
    labelled_keys: set[str] = set()
    for e in advertised:
        mid = e.get("model_name", "")
        key = key_of(mid)
        disp = e.get("display_name", "")
        # Only the first advertised id per key claims a registry slot, so only its
        # label may apply to that slot.
        if not key or key == "auto" or key in labelled_keys:
            continue
        labelled_keys.add(key)
        if disp and disp != mid:
            advertised_label[key] = e
    # Slots whose row is an advertised id, and the advertised names already seen.
    advertised_slots: set[int] = set()
    advertised_names: set[str] = set()
    sources = [(e, False) for e in registry_rows] + [(e, True) for e in advertised]
    for entry, is_advertised in sources:
        name = entry.get("model_name", "")
        key = key_of(name)
        if not key:
            continue
        if is_advertised:
            if name in advertised_names:
                continue
            advertised_names.add(name)
        if key in seen:
            slot = seen[key]
            if is_advertised and slot in advertised_slots:
                # Two advertised ids that fold to one key are distinct models, and
                # the backend is authoritative about that, so both keep their rows.
                merged.append(entry)
                continue
            # Collision: a registry row merges into the row already on its key, and
            # an advertised id merges into the registry row it claims. The advertised
            # id supplies the wire value; its NATIVE label (when it has one) also
            # replaces the registry display, since the live backend names its own
            # models more currently than the static catalog. Falls back to the
            # registry display when the backend advertised no name for this id.
            if key != "auto":
                label = advertised_label.get(key)
                if label is not None:
                    merged[slot] = {
                        **merged[slot],
                        "model_name": name,
                        "display_name": label.get("display_name", ""),
                        "description": label.get("description", ""),
                    }
                else:
                    # An alias keeps its own id rather than a versioned registry label.
                    alias_label = {"display_name": name} if name in aliases else {}
                    merged[slot] = {**merged[slot], "model_name": name, **alias_label}
            if is_advertised and key != "auto":
                advertised_slots.add(slot)
            continue
        seen[key] = len(merged)
        if is_advertised and key != "auto":
            advertised_slots.add(len(merged))
        merged.append(entry)
    return merged
