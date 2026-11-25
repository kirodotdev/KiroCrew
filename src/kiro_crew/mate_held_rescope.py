"""Narrow the held limits an earlier release recorded too widely, once per Bot.

An earlier release recorded a hook on a tool Crew could not map (``use_aws``) as
covering every call, and a held read or write rule that names paths as covering
every read or write (``mate_held_scope``). Each such Bot then asked a person for
calls its limits never spoke about, Trust included. This pass reads each held
Bot's old agent again, finds the rule each recorded text came from
(``mate_held_rules.held_scopes``), and records the narrower reach in its place.

It only ever narrows: a hook text must itself name the same tool in its matcher,
and a permissions-rule text must name every candidate path. A toolsSettings text
names only its tool, so its paths are read from the old agent's file. A text whose source cannot
be read, or that two restrictions of different reach share, keeps its recorded
reach, and nothing is dropped. Each Bot is read once
(``held_scope_rechecked``); one whose old agent cannot be read is tried again on
the next start. Run at startup beside the other moved-Bot repairs. Never raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The ``migrated.json`` key listing every Bot whose held limits were narrowed.
RESCOPED_KEY = "held_scope_rechecked"


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def narrowed(rule: Any, scopes: dict[str, dict[str, Any] | None]) -> Any:
    """*rule* with the narrower reach *scopes* gives its text, else *rule* unchanged."""
    from kiro_crew.mate_held_rules import ANY
    from kiro_crew.mate_held_scope import named_tool

    if not isinstance(rule, dict):
        return rule
    text = str(rule.get("text") or "")
    found = scopes.get(text)
    if not isinstance(found, dict):
        return rule
    was, now = rule.get("capability"), found.get("capability")
    # The old agent may have changed since migration; only the recorded text
    # can prove which tool or paths the original limit named.
    if was == ANY and now != ANY:
        _, separator, matcher = text.partition(" hook on ")
        # ``@builtin/<tool>`` names that one built-in, as ``_hook_capability`` reads it.
        if matcher.startswith("@builtin/"):
            matcher = matcher.partition("/")[2]
        tool = named_tool(matcher)
        if text.startswith("its ") and separator and tool and tool == now:
            return {**rule, "capability": now}
    if was == now and found.get("paths") and not rule.get("paths"):
        # A permissions-rule text names its paths, so it must name these. A
        # toolsSettings text names only its tool: its paths come from the old
        # agent as it reads now. shortcut: an owner who edits that file between
        # the move and this one-time pass narrows the hold to the edited paths;
        # read the paths from the move's own record once it keeps them.
        names_paths = not text.startswith("its toolsSettings for ")
        if not names_paths or all(path in text for path in found["paths"]):
            return {**rule, "paths": found["paths"]}
    return rule


def rescope_held_limits() -> list[str]:
    """Narrow every held Bot's recorded limits not narrowed yet; the Bots changed."""
    from kiro_crew.mate_config_migration import _read_ledger, _worker_base, _write_ledger
    from kiro_crew.mate_held_rules import HELD_KEY, held_scopes
    from kiro_crew.mate_ledger_lock import ledger_lock
    from kiro_crew.mate_mount_repair import _old_agent
    from kiro_crew.mate_restriction_carry import carried_policy

    changed: list[str] = []
    try:
        data = _read_ledger()
        entries = data.get(HELD_KEY)
        threads = data.get("threads")
        done = set(_strings(data.get(RESCOPED_KEY)))
        sources = {
            name: threads[name]["from"]
            for name in (entries if isinstance(entries, dict) else {})
            if name not in done
            and isinstance(threads, dict)
            and isinstance(threads.get(name), dict)
            and isinstance(threads[name].get("from"), str)
            and threads[name]["from"]
        }
        if not sources:
            return changed
        base = _worker_base()
    except Exception:  # noqa: BLE001 - best effort; the next start tries again
        logger.warning("held Bot rules: not narrowed (ledger or worker base)", exc_info=True)
        return changed
    scopes: dict[str, dict[str, dict[str, Any] | None]] = {}
    for name, source in sorted(sources.items()):
        try:
            old = _old_agent(source)
            if old is not None:
                scopes[name] = held_scopes(base, carried_policy(base, old))
        except Exception:  # noqa: BLE001 - one Bot never stops the others
            logger.warning("held Bot rules: %s's old agent not read", name, exc_info=True)
    if not scopes:
        return changed
    try:
        with ledger_lock():
            data = _read_ledger()
            entries = data.get(HELD_KEY)
            entries = entries if isinstance(entries, dict) else {}
            for name, by_text in scopes.items():
                entry = entries.get(name)
                if not isinstance(entry, dict) or not isinstance(entry.get("rules"), list):
                    continue
                rules = [narrowed(r, by_text) for r in entry["rules"]]
                if rules != entry["rules"]:
                    entries[name] = {**entry, "rules": rules}
                    changed.append(name)
            data[HELD_KEY] = entries
            data[RESCOPED_KEY] = sorted(set(_strings(data.get(RESCOPED_KEY))) | set(scopes))
            _write_ledger(data)
    except Exception:  # noqa: BLE001 - the recorded limits stay as wide as they were
        logger.warning("held Bot rules: narrowed limits not recorded", exc_info=True)
        return []
    for name in changed:
        logger.warning("held Bot rules: %s's held limits now cover only the calls they name", name)
    return changed
