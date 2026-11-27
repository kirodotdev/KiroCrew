# SPDX-License-Identifier: Apache-2.0
"""Fold the spellings of one tool in an agent's ``toolsSettings`` into one entry.

An agent can set one tool under two kiro-cli spellings (``shell`` and
``execute_bash``, ``write`` and ``fs_write``). kiro-cli reads only the first
spelling of its alias group (``platform.tools_settings_aliases``), so carrying
both onto a Bot's worker could not be exact. Add abilities folds them into one
entry under that first spelling instead, keeping the stricter value of each key:
the union of what either denies, the intersection of what either allows. The
folded entry never allows a call that either spelling alone would not, so the
worker is never wider than the agent. A key the fold cannot combine that way is
named and the add is refused.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.platform import tools_settings_aliases as aliases

#: How each key combines, by the first spelling of its alias group. ``union``: a
#: deny list, every entry kept. ``intersect``: a grant list, only entries both
#: carry (missing means none). ``restrict``: a list that narrows when present and
#: allows everything when missing (both present: the intersection; one: that one).
#: ``any``: a flag that limits when true. ``all``: a flag that grants when true.
#: ``fallback``: a write ``fallbackAction``, read as the gate reads it
#: (``kas_tool_view.write_fallback_reading``): a spelling that does not set it, or
#: sets ``None`` or ``""``, means kiro-cli's default ``"interactive"``; ``"deny"``
#: on any spelling wins; all default leaves the key out; else every spelling must
#: give the same value.
#: A key not listed here is kept only when every spelling sets it the same.
_COMBINE: dict[str, dict[str, str]] = {
    "shell": {
        "deniedCommands": "union",
        "allowedCommands": "intersect",
        "denyByDefault": "any",
        "autoAllowReadonly": "all",
    },
    "read": {"deniedPaths": "union", "allowedPaths": "intersect"},
    "write": {"deniedPaths": "union", "allowedPaths": "intersect", "fallbackAction": "fallback"},
    "web_fetch": {"blocked": "union", "trusted": "intersect"},
    "crew": {"availableAgents": "restrict", "trustedAgents": "intersect"},
}

_MISSING = object()


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _combine(kind: str, values: list[Any], keep_fallback: bool = False) -> Any:
    """The stricter of *values* under *kind*, ``_MISSING`` to leave the key out,
    or raise ``ValueError`` when a value is not of the kind's type.

    *keep_fallback*: write ``fallbackAction`` values that disagree with no deny
    among them give the first one other than ``"interactive"`` instead of raising.
    """
    present = [v for v in values if v is not _MISSING]
    if kind == "fallback":
        from kiro_crew.agent_sdk.kas_tool_view import write_fallback_reading

        # Each value as the gate reads it: a missing one, None and "" are the
        # default "interactive"; any case of "deny", or a non-string, is "deny".
        read = [write_fallback_reading(None if v is _MISSING else v) for v in values]
        if "deny" in read:
            return "deny"
        if all(v == "interactive" for v in read):
            return _MISSING
        if len(set(read)) > 1:
            if not keep_fallback:
                raise ValueError
            return next(v for v in read if v != "interactive")
        return read[0]
    if kind in ("union", "intersect", "restrict"):
        if not all(isinstance(v, list) for v in present):
            raise ValueError
        if kind == "union":
            out: list[Any] = []
            for value in present:
                out.extend(v for v in value if _canon(v) not in {_canon(o) for o in out})
            return out
        if kind == "intersect" and len(present) < len(values):
            return _MISSING
        if not present:
            return _MISSING
        first, *rest = present
        return [v for v in first if all(_canon(v) in {_canon(r) for r in other} for other in rest)]
    if not all(isinstance(v, bool) for v in present):
        raise ValueError
    if kind == "any":
        return any(present) if present else _MISSING
    # ``all``: a grant flag holds only when every spelling sets it.
    if len(present) < len(values):
        return _MISSING
    return all(present)


def fold_alias_settings(
    settings: Any, *, keep_fallback: bool = False
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """*settings* with each alias group written under two or more spellings folded.

    With *keep_fallback*, write spellings that disagree on ``fallbackAction``
    (no deny among them) fold to the first value other than ``"interactive"``
    instead of being refused, so the caller's own check of what the worker keeps
    can leave that value out under the folded key.

    Returns ``(settings, folds, refused)``. Each fold is ``{"kept": <key>,
    "spellings": [<keys as written>]}``. *refused* names each
    ``toolsSettings.<key>.<setting>`` the fold cannot combine (a value of the
    wrong type, or a key with no rule that the spellings set differently, written
    under the group's first spelling); when it is not empty, *settings* is
    returned unchanged. Anything that is not an object is returned unchanged.
    """
    if not isinstance(settings, dict):
        return settings, [], []
    out = dict(settings)
    folds: list[dict[str, Any]] = []
    refused: list[str] = []
    for group in aliases._TOOLS_SETTINGS_ALIAS_GROUPS:
        present = [key for key in group if key in settings]
        if len(present) < 2:
            continue
        winner = present[0]
        entries = [settings[key] for key in present]
        bad = [key for key, entry in zip(present, entries) if not isinstance(entry, dict)]
        if bad:
            refused.extend(f"toolsSettings.{key}" for key in bad)
            continue
        rules = _COMBINE.get(group[0], {})
        merged: dict[str, Any] = {}
        for name in dict.fromkeys(k for entry in entries for k in entry):
            values = [entry.get(name, _MISSING) for entry in entries]
            kind = rules.get(name)
            if kind is None:
                if len({_canon(v) for v in values if v is not _MISSING}) == 1 and (
                    _MISSING not in values
                ):
                    merged[name] = values[0]
                else:
                    refused.append(f"toolsSettings.{winner}.{name}")
                continue
            try:
                value = _combine(kind, values, keep_fallback)
            except ValueError:
                refused.append(f"toolsSettings.{winner}.{name}")
                continue
            if value is not _MISSING:
                merged[name] = value
        for key in present:
            del out[key]
        kept = aliases._filed_under(winner)
        out[kept] = merged
        folds.append({"kept": kept, "spellings": present})
    if refused:
        return settings, [], refused
    return out, folds, []
