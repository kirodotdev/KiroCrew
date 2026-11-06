"""The user's consent for crewmate-to-crewmate messages, read from the config.

Two keys in the ``agent`` section hold it: ``mate_messaging`` (the switch,
default off) and ``mate_messaging_allow`` (the ``"sender>target"`` pairs).
The send route and the delivery (:mod:`kiro_crew.dashboard.mate_peer_messages`)
read them through :func:`read_settings`, and the crewmate prompt
(:mod:`kiro_crew.context_assembly.member`) names ``message_mate`` only when
:func:`open_for` is true, so a crewmate with no consent is not told about it.
"""

from __future__ import annotations

from typing import Any


def parse_pairs(entries: Any) -> frozenset[tuple[str, str]]:
    """The ``(sender, target)`` pairs of ``agent.mate_messaging_allow`` entries.

    An entry is ``"sender>target"`` with exact crew names. An entry with no
    ``>``, more than one, an empty side, the same name on both sides or a ``*``
    is ignored, so a wildcard can never open every pair.
    """
    pairs: set[tuple[str, str]] = set()
    if not isinstance(entries, (list, tuple)):
        return frozenset()
    for entry in entries:
        if not isinstance(entry, str) or entry.count(">") != 1 or "*" in entry:
            continue
        sender, target = (side.strip() for side in entry.split(">"))
        if sender and target and sender != target:
            pairs.add((sender, target))
    return frozenset(pairs)


def read_settings(config: Any = None) -> tuple[bool, frozenset[tuple[str, str]]]:
    """``(switch on, allowed pairs)`` from *config*, else the live config. Blocking IO."""
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    agent = getattr(config, "agent", None)
    enabled = getattr(agent, "mate_messaging", False) is True
    return enabled, parse_pairs(getattr(agent, "mate_messaging_allow", []))


def open_for(member: str, config: Any = None) -> bool:
    """Whether the switch is on and an allowed pair names *member*. Never raises."""
    try:
        enabled, pairs = read_settings(config)
    except Exception:  # noqa: BLE001 - an unreadable config gives no consent
        return False
    return enabled and any(member in pair for pair in pairs)
