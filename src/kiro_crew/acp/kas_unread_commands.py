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

Every reader here takes ``toolsSettings`` with one key per kiro-cli alias group
(``canonical_tools_settings``), as the gate reads it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from kiro_crew.acp.kas_spec_grants import SHELL_SETTINGS_KEYS
from kiro_crew.platform import allowed_commands

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


def over_limit_notice(agent: str, keys: list[str]) -> tuple[str, list[str]]:
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
    ``web_fetch.blocked``.
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
    kept = [path for path in not_carried if path not in named]
    return (
        kept
        + [
            f"{key}{allowed_commands.UNREADABLE_ENTRY_MARK}{entry} ({reason})"
            for key, entry, reason in unread
        ]
        + over_limit
        + blocked
    )


def _uncarried_web_fetch_blocked(tools_settings: Any) -> list[str]:
    """``web_fetch.blocked: <entry> (<reason>)`` for each ``web_fetch.blocked`` entry
    the KAS translation drops (the same test ``tools_settings_to_permissions``
    applies), in the spec's order, so the notice names the entry and not only the
    key. kiro-cli refuses every URL such an entry matches; on KAS it refuses
    nothing."""
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
        out.append(f"web_fetch.blocked: {shown} ({reason})")
    return list(dict.fromkeys(out))
