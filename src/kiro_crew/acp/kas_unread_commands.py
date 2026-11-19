"""The ``allowedCommands`` entries of a spec that Kiro Crew cannot read, one by one.

On KAS (and claude) Kiro Crew answers ``toolsSettings.shell.allowedCommands`` at its
own gate, and refuses what they do not match under ``denyByDefault``, reading each
entry with :mod:`kiro_crew.platform.allowed_commands`. An entry outside what that
reader reads allows nothing: a command only it matches asks for approval, or is
refused under ``denyByDefault``. So each such entry is named, with the reason, in
the three places a person meets it:

* the session-start notice (:func:`name_unreadable_entries`, through
  ``kas_agents.spec_keys_without_carrier``);
* the ``denyByDefault`` refusal, which a cron run records: the entries ride the
  projected allowlist (:func:`unreadable_allowlist_entries`), match nothing there,
  and the gate's refusal names them (``allowed_commands.unreadable_note``);
* ``kirocrew doctor`` (:func:`unreadable_allowed_commands`, through
  ``agent_sdk.kas_tool_view``).

A usable entry past the bound Kiro Crew applies (``kas_permissions._MAX_ALLOWED_COMMANDS``)
allows nothing either. The session-start notice names each one by its position in the
list and its text (:func:`name_unreadable_entries`, :func:`over_limit_notice`), and
says the entries before it still apply.

Each unreadable read or write ``allowedPaths`` entry is named in the session-start
notice the same way (:func:`_unreadable_path_items`): the other entries still apply.

Every reader here takes ``toolsSettings`` with one key per kiro-cli alias group
(``canonical_tools_settings``), as the gate reads it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from kiro_crew.acp.kas_spec_grants import (
    READ_SETTINGS_KEYS,
    SHELL_SETTINGS_KEYS,
    WRITE_SETTINGS_KEYS,
)
from kiro_crew.platform import allowed_commands, allowed_paths

logger = logging.getLogger(__name__)

#: Most unreadable entries one projected allowlist carries for its refusal to name.
_MAX_CARRIED = 64

#: What follows ``<key>.allowedCommands`` in the one session-start notice item that
#: names the entries past the bound: ``toolsSettings.shell.allowedCommands past the
#: limit: entry 1025: dig .*; and 3 more after entry 1025``.
OVER_LIMIT_HEAD = ".allowedCommands past the limit"

#: How many entries past the bound that item names before it counts the rest.
_NAMED_OVER_LIMIT = 10

#: How much of one entry's text that item shows.
_SHOWN_ENTRY_CHARS = 80


def placed_allowed_commands(tools_settings: Any) -> list[tuple[str, int, str]]:
    """``(shell key, position as written, entry)`` of every distinct usable
    ``allowedCommands`` entry of both shell keys, in order. The position counts
    from 1 over the key's list as the spec wrote it, an entry read twice keeps its
    first place, and an entry the reader refuses is skipped."""
    if not isinstance(tools_settings, dict):
        return []
    placed: dict[str, tuple[str, int, str]] = {}
    for key in SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        entries = settings.get("allowedCommands") if isinstance(settings, dict) else None
        for at, raw in enumerate(entries if isinstance(entries, list) else (), 1):
            if allowed_commands.parse(raw) is not None:
                placed.setdefault(raw, (key, at, raw))
    return list(placed.values())


def _shown(entry: str) -> str:
    """*entry* as a notice shows it: whole, or its first :data:`_SHOWN_ENTRY_CHARS`."""
    return entry if len(entry) <= _SHOWN_ENTRY_CHARS else f"{entry[:_SHOWN_ENTRY_CHARS]}..."


def _over_limit_items(tools_settings: Any, limit: int) -> list[str]:
    """One ``<key>.allowedCommands past the limit: ...`` item per shell key whose
    usable entries run past *limit*: the first :data:`_NAMED_OVER_LIMIT` such entries
    by position and text (:func:`_shown`), then how many more follow."""
    by_key: dict[str, list[tuple[int, str]]] = {}
    for key, at, entry in placed_allowed_commands(tools_settings)[limit:]:
        by_key.setdefault(key, []).append((at, entry))
    items = []
    for key, cut in by_key.items():
        named = cut[:_NAMED_OVER_LIMIT]
        parts = [f"entry {at}: {_shown(entry)}" for at, entry in named]
        if len(cut) > len(named):
            parts.append(f"and {len(cut) - len(named)} more after entry {named[-1][0]}")
        items.append(f"{key}{OVER_LIMIT_HEAD}: {'; '.join(parts)}")
    return items


def _commands_over_limit_notice(agent: str, keys: list[str]) -> tuple[str, list[str]]:
    """The session-start sentence for the :data:`OVER_LIMIT_HEAD` items among *keys*,
    and *keys* without them.

    ``""`` when the same list is also named whole: the session then answers none of
    its entries, so saying the first ones apply would be wrong. A list named with
    ``tools_settings_reach.GATE_READ_MARK`` does not count as named whole: under
    ``denyByDefault`` the gate enforces its first entries as the allowlist and
    refuses what only a later entry names, which is what the sentence says.
    """
    # circular import: kas_permissions imports this module for its allowlist.
    from kiro_crew.acp.kas_permissions import _MAX_ALLOWED_COMMANDS as limit

    over = [key for key in keys if key.partition(": ")[0].endswith(OVER_LIMIT_HEAD)]
    rest = [key for key in keys if key not in over]
    lists = {key.partition(": ")[0].removesuffix(" past the limit") for key in over}
    if not over or lists & set(rest):
        return "", rest
    cut = " ".join(f"{key.partition(': ')[2]}." for key in over)
    return (
        f"Agent {agent} sets more {' and '.join(sorted(lists))} entries than the {limit} "
        f"Kiro Crew applies. Its first {limit} readable entries apply. These do not: {cut} "
        "Each allows nothing, so a command only it matches asks for approval, or is "
        "refused when denyByDefault is set. To apply them, shorten the list in the "
        f"agent's file to {limit} entries or fewer, for example by joining entries that "
        "differ in one word: (dig|host) .* covers both dig .* and host .*.",
        rest,
    )


def unreadable_paths_notice(agent: str, keys: list[str]) -> tuple[str, list[str]]:
    """The session-start sentence for the unreadable ``allowedPaths`` entries among
    *keys* (:func:`_unreadable_path_items`), and *keys* without them.

    It says the other entries still apply unless the same list is also named whole:
    the session then answers none of it. A list marked
    ``tools_settings_reach.GATE_READ_MARK`` counts as named whole, as an unreadable
    ``allowedCommands`` entry's list does.
    """
    from kiro_crew.agent_sdk.tools_settings_reach import GATE_READ_MARK

    mark = allowed_paths.UNREADABLE_ENTRY_MARK
    unread = [key for key in keys if mark in key]
    rest = [key for key in keys if mark not in key]
    if not unread:
        return "", rest
    lists = {key.partition(mark)[0] + ".allowedPaths" for key in unread}
    whole = lists & {key.removesuffix(GATE_READ_MARK) for key in rest}
    others = "" if whole else "; the agent's other entries still apply"
    return (
        f"Agent {agent} has allowedPaths entries Kiro Crew cannot read: {'; '.join(unread)}. "
        "Each covers nothing, so a read or write only it covers asks for approval, and "
        f"such a write is refused when fallbackAction is deny{others}.",
        rest,
    )


def _unreadable(tools_settings: Any) -> Iterator[tuple[str, object, str]]:
    """``(shell key, entry as written, reason)`` for each entry the reader refuses."""
    if not isinstance(tools_settings, dict):
        return
    for key in SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        entries = settings.get("allowedCommands") if isinstance(settings, dict) else None
        for raw in entries if isinstance(entries, list) else ():
            reason = allowed_commands.refusal_reason(raw)
            if reason is not None:
                yield key, raw, reason


def unreadable_allowed_commands(tools_settings: Any) -> list[tuple[str, str, str]]:
    """``(shell key, entry, reason)`` for each ``allowedCommands`` entry of
    *tools_settings* Kiro Crew cannot read, in the spec's order. An entry that is
    not a string, or holds a character a reader could not see, is shown as JSON."""
    return [
        (key, raw if isinstance(raw, str) and raw.isprintable() else json.dumps(raw), reason)
        for key, raw, reason in _unreadable(tools_settings)
    ]


def unreadable_allowlist_entries(tools_settings: Any) -> list[str]:
    """The unreadable entries a ``denyByDefault`` allowlist carries beside its usable
    ones, so its refusal can name them.

    Only an entry that is a printable string within the reader's length bound, each
    once: it is the spec's own text, matches nothing at the gate, and is shown as
    it is. Any other value never rides along, since its JSON form could read as a
    literal entry; the notice and ``kirocrew doctor`` still name it.
    """
    out = [
        raw
        for _key, raw, _reason in _unreadable(tools_settings)
        if isinstance(raw, str)
        and raw.isprintable()
        and 0 < len(raw) <= allowed_commands.MAX_PATTERN_LENGTH
    ]
    return list(dict.fromkeys(out))[:_MAX_CARRIED]


def name_unreadable_entries(tools_settings: Any, not_carried: list[str]) -> list[str]:
    """*not_carried*, ``tools_settings_to_permissions``'s ``<tool>.<key>`` paths,
    with each unreadable ``allowedCommands`` entry named on its own as
    ``<key>.allowedCommands: <entry> (<reason>)``
    (:data:`~kiro_crew.platform.allowed_commands.UNREADABLE_ENTRY_MARK`).

    The bare ``<key>.allowedCommands`` stays only when the value is not a list (it
    has no entries to name). Usable entries past the bound Kiro Crew applies are
    named in one item of their own (:func:`_over_limit_items`), so the notice never
    says a list whose first entries apply has no effect. Each ``web_fetch.blocked``
    entry KAS cannot carry is named the same way, in place of the bare
    ``web_fetch.blocked``, and each unreadable ``allowedPaths`` entry
    (:func:`_unreadable_path_items`).
    """
    # circular import: kas_permissions imports this module for its allowlist.
    from kiro_crew.acp.kas_permissions import _MAX_ALLOWED_COMMANDS

    unread = unreadable_allowed_commands(tools_settings)
    over_limit = _over_limit_items(tools_settings, _MAX_ALLOWED_COMMANDS)
    named = {f"{key}.allowedCommands" for key, _entry, _reason in unread}
    named |= {item.partition(OVER_LIMIT_HEAD)[0] + ".allowedCommands" for item in over_limit}
    blocked = _uncarried_web_fetch_blocked(tools_settings)
    if blocked:
        named.add("web_fetch.blocked")
    paths = _unreadable_path_items(tools_settings, not_carried)
    mark = allowed_paths.UNREADABLE_ENTRY_MARK
    named |= {item.partition(mark)[0] + ".allowedPaths" for item in paths}
    kept = [path for path in not_carried if path not in named]
    return (
        kept
        + [
            f"{key}{allowed_commands.UNREADABLE_ENTRY_MARK}{entry} ({reason})"
            for key, entry, reason in unread
        ]
        + over_limit
        + blocked
        + paths
    )


def _unreadable_path_items(tools_settings: Any, not_carried: list[str]) -> list[str]:
    """``<key>.allowedPaths: <entry> (<reason>)`` for each ``allowedPaths`` entry
    Kiro Crew cannot read, for each ``<key>.allowedPaths`` of *not_carried*.

    Only for a read or write list the session still answers some of: another
    tool's list, a value that is not a list,
    a list with no readable entry, and a family whose readable entries run past the
    bound Kiro Crew applies stay named whole.
    """
    # circular import: kas_permissions imports this module for its allowlist.
    from kiro_crew.acp.kas_permissions import _MAX_ALLOWED_PATHS, _usable_allowed_paths

    if not isinstance(tools_settings, dict):
        return []
    out = []
    for path in not_carried:
        key, dot, name = path.partition(".")
        settings = tools_settings.get(key)
        entries = settings.get("allowedPaths") if isinstance(settings, dict) else None
        if not dot or name != "allowedPaths" or not isinstance(entries, list):
            continue
        # Kiro Crew answers only the read and write lists; any other tool's list
        # (grep, glob, an unknown tool) applies no entry, so it stays named whole.
        if key in READ_SETTINGS_KEYS:
            family = READ_SETTINGS_KEYS
        elif key in WRITE_SETTINGS_KEYS:
            family = WRITE_SETTINGS_KEYS
        else:
            continue
        if not any(allowed_paths.parse(raw) is not None for raw in entries):
            continue
        if len(_usable_allowed_paths(tools_settings, family)) > _MAX_ALLOWED_PATHS:
            continue
        for raw in entries:
            reason = allowed_paths.refusal_reason(raw)
            if reason is not None:
                shown = raw if isinstance(raw, str) and raw.isprintable() else json.dumps(raw)
                out.append(f"{key}{allowed_paths.UNREADABLE_ENTRY_MARK}{shown} ({reason})")
    return out


def _uncarried_web_fetch_blocked(tools_settings: Any) -> list[str]:
    """``web_fetch.blocked: <entry> (<reason>)`` for each ``web_fetch.blocked`` entry
    the KAS translation drops (the same test ``tools_settings_to_permissions``
    applies), in the spec's order, so the notice names the entry and not only the
    key. kiro-cli refuses every URL such an entry matches; on KAS it refuses
    nothing unless :func:`fails_closed_on_uncarried_blocked` is on, when the
    translation denies every fetch."""
    from kiro_crew.acp.kas_permissions import (
        _MAX_PATTERN_LENGTH,
        _patterns,
        _regex_to_glob,
        _url_glob,
    )

    settings = tools_settings.get("web_fetch") if isinstance(tools_settings, dict) else None
    if not isinstance(settings, dict):
        return []
    out = []
    for raw in _patterns(settings.get("blocked")):
        glob = _url_glob(raw)
        if glob is not None and len(glob) <= _MAX_PATTERN_LENGTH:
            continue
        if _regex_to_glob(raw) is None:
            reason = "regex syntax KAS cannot express"
        elif glob is None:
            reason = "whole-URL rule; KAS checks host only"
        else:
            reason = f"longer than {_MAX_PATTERN_LENGTH} characters"
        shown = raw if raw.isprintable() else json.dumps(raw)
        mark = WEB_FETCH_REFUSED_MARK if fails_closed_on_uncarried_blocked() else ""
        out.append(f"web_fetch.blocked: {shown} ({reason}){mark}")
    return list(dict.fromkeys(out))


#: Ends each uncarried ``web_fetch.blocked`` item when
#: :func:`fails_closed_on_uncarried_blocked` is on: the translation then denies every
#: fetch (``tools_settings_to_permissions``), so the notice says so.
WEB_FETCH_REFUSED_MARK = "; every web fetch is refused until this entry is fixed"


def fails_closed_on_uncarried_blocked() -> bool:
    """Whether ``agent.kas_fail_closed_web_fetch_blocked`` is a JSON ``true``.

    Off by default and when absent. Read from the gateway's last applied config when
    there is one, else from disk. On when that read cannot establish the owner's choice
    (``config.json`` or ``config.local.json`` present but unreadable, or the ``agent``
    section discarded), so a broken config never drops an opted-in fence.
    """
    try:
        # Call-time: the config loader pulls in the config plane this leaf stays off.
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = live.snapshot() or KiroCrewConfig.load()
        if cfg._base_unreadable or cfg._overlay_unreadable or "agent" in cfg.degraded_sections:
            logger.warning(
                "could not establish agent.kas_fail_closed_web_fetch_blocked from degraded "
                "config; fencing"
            )
            return True
        return cfg.agent.kas_fail_closed_web_fetch_blocked is True
    except Exception:
        # An unreadable config must not drop an opted-in fence.
        logger.warning(
            "could not read agent.kas_fail_closed_web_fetch_blocked; fencing", exc_info=True
        )
        return True


#: What follows ``<key>`` in the one session-start notice item that names the
#: ``web_fetch.trusted`` entries past the bound Crew's gate grants from.
TRUSTED_OVER_LIMIT_HEAD = ".trusted past the limit"


def trusted_over_limit_items(tools_settings: Any) -> list[str]:
    """``web_fetch.trusted past the limit: entry 65: <entry>; ...`` naming each
    trusted entry past the bound by position and text (:func:`_shown`), the first
    :data:`_NAMED_OVER_LIMIT` of them, then how many more follow; ``[]`` when none
    is (``kas_spec_denies.trusted_past_bound``)."""
    # circular import: kas_spec_denies imports kas_permissions, which imports this.
    from kiro_crew.acp.kas_spec_denies import trusted_past_bound

    cut = trusted_past_bound(tools_settings)
    if not cut:
        return []
    named = cut[:_NAMED_OVER_LIMIT]
    parts = [f"entry {at}: {_shown(entry)}" for at, entry in named]
    if len(cut) > len(named):
        parts.append(f"and {len(cut) - len(named)} more after entry {named[-1][0]}")
    return [f"web_fetch{TRUSTED_OVER_LIMIT_HEAD}: {'; '.join(parts)}"]


def _trusted_over_limit_notice(agent: str, keys: list[str]) -> tuple[str, list[str]]:
    """The session-start sentence for a :func:`trusted_over_limit_items` item among
    *keys*, and *keys* without it. It says the first entries apply, so it is only
    made when the gate grants from the list (``kas_agents`` adds the item then)."""
    from kiro_crew.acp.kas_spec_denies import _MAX_DENIES as limit

    over = [key for key in keys if key.partition(": ")[0].endswith(TRUSTED_OVER_LIMIT_HEAD)]
    if not over:
        return "", keys
    key, _, cut = over[0].partition(": ")
    return (
        f"Agent {agent} sets more {key.removesuffix(' past the limit')} entries than the "
        f"{limit} Kiro Crew applies. Its first {limit} entries apply. These do not: {cut}. "
        "A fetch only one of them names asks for approval. To apply them, shorten the "
        f"list in the agent's file to {limit} entries or fewer.",
        [k for k in keys if k not in over],
    )


def over_limit_notice(agent: str, keys: list[str]) -> tuple[str, list[str]]:
    """The session-start sentences for the entries past the bound Kiro Crew applies
    among *keys*, and *keys* without them: ``allowedCommands``
    (:func:`_commands_over_limit_notice`) and ``web_fetch.trusted``
    (:func:`_trusted_over_limit_notice`)."""
    trusted, keys = _trusted_over_limit_notice(agent, keys)
    commands, keys = _commands_over_limit_notice(agent, keys)
    return " ".join(text for text in (commands, trusted) if text), keys
