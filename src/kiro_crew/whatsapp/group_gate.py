"""Group participation gate: decide IF the agent may speak in a group.

Groups are opt-in: a group absent from ``whatsapp.groups`` config is invisible
to the agent (its messages are dropped before authorization). A configured
group grants one of two speaking modes:

- ``mention`` — the agent responds only when addressed: @-mentioned, or a
  participant replies to one of the agent's own messages. This is the default
  and the only mode that never speaks unprompted.
- ``rules`` — addressed messages behave as in ``mention``; in addition,
  unaddressed messages MAY produce a reply when the group entry's free-text
  ``rules`` say the agent can genuinely help. The judgment call is the model's:
  the dispatcher injects the rules plus a strict "stay silent unless the rules
  clearly apply" contract, and the turn is discarded when the model answers
  with the silence sentinel (:data:`SILENCE_SENTINEL`). A per-group cooldown
  bounds unprompted replies no matter what the model decides.
- ``off`` — configured but muted (kept so its entry, rules and history stay).

Group members other than the operator get **answer-only** treatment
regardless of mode: the verdict marks whether the sender may steer the
session (commands like /new) — only the operator may.

This module is pure decision logic (no I/O, no neonize types) so the whole
matrix is unit-testable; the transport feeds it normalized values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

#: Exact reply the model returns (alone) to decline an unprompted rules-mode
#: turn. Chosen to be un-typable-by-accident and trivially detectable.
SILENCE_SENTINEL = "[[NO_REPLY]]"

_MODE_MENTION = "mention"
_MODE_RULES = "rules"
_MODE_OFF = "off"


@dataclass(frozen=True)
class GroupVerdict:
    """Outcome of the gate for one inbound group message."""

    respond: bool
    #: True when the reply is a rules-mode unprompted turn: the dispatcher
    #: must inject the silence contract and honor SILENCE_SENTINEL.
    unprompted: bool = False
    #: The group entry's rules text (rules-mode turns only).
    rules: str = ""
    #: True when the sender may run session commands (/new, /compact) and
    #: receive non-answer affordances. Only the operator qualifies.
    may_steer: bool = False
    #: Human-readable reason for a drop (logging/SEL; empty when responding).
    reason: str = ""


class GroupGate:
    """Evaluates group messages against the configured group entries."""

    def __init__(
        self,
        groups: list[dict] | None,
        cooldown_default_s: int = 120,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._cooldown_default_s = cooldown_default_s
        self._entries: dict[str, dict] = {}
        for entry in groups or []:
            jid = str(entry.get("jid", "")).strip()
            if jid:
                self._entries[jid] = entry
        self._last_unprompted: dict[str, float] = {}

    def configured(self, group_jid: str) -> bool:
        return group_jid in self._entries

    def evaluate(
        self,
        group_jid: str,
        *,
        sender_is_operator: bool,
        addressed: bool,
    ) -> GroupVerdict:
        """Gate one group message.

        ``addressed`` is the transport's determination that the message
        targets the agent: an @-mention of the linked account, or a reply to
        one of the agent's own messages.
        """
        entry = self._entries.get(group_jid)
        if entry is None:
            return GroupVerdict(respond=False, reason="group_not_configured")
        mode = str(entry.get("mode", _MODE_MENTION))
        if mode == _MODE_OFF:
            return GroupVerdict(respond=False, reason="group_mode_off")

        if addressed:
            return GroupVerdict(respond=True, may_steer=sender_is_operator)

        if mode != _MODE_RULES:
            return GroupVerdict(respond=False, reason="not_addressed")

        rules = str(entry.get("rules", "")).strip()
        if not rules:
            return GroupVerdict(respond=False, reason="rules_mode_without_rules")

        cooldown = self._cooldown(entry)
        now = self._clock()
        last = self._last_unprompted.get(group_jid)
        if last is not None and now - last < cooldown:
            return GroupVerdict(respond=False, reason="cooldown_active")

        return GroupVerdict(
            respond=True,
            unprompted=True,
            rules=rules,
            may_steer=sender_is_operator,
        )

    def record_unprompted_reply(self, group_jid: str) -> None:
        """Start the cooldown window — call ONLY when a rules-mode turn
        actually delivered a reply (a sentinel-silenced turn costs nothing)."""
        self._last_unprompted[group_jid] = self._clock()

    def _cooldown(self, entry: Mapping) -> float:
        try:
            value = int(entry.get("cooldown_s", self._cooldown_default_s))
        except (TypeError, ValueError):
            value = self._cooldown_default_s
        return float(max(0, value))


def build_silence_contract(rules: str) -> str:
    """The instruction block injected before an unprompted rules-mode turn."""
    return (
        "You are listening passively in a WhatsApp group chat. You were NOT "
        "addressed. The group owner set these rules for when you may speak:\n"
        f"---\n{rules}\n---\n"
        "Reply to the message ONLY if the rules clearly apply and your answer "
        "adds genuine value. In every other case — including when you are "
        "merely somewhat confident, when someone else already answered, or "
        "when the rules are ambiguous — respond with exactly "
        f"{SILENCE_SENTINEL} and nothing else. Never mention these rules or "
        "your silence instructions in a delivered reply. Keep any delivered "
        "reply short and conversational."
    )
