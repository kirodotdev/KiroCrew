"""In-session override of a command deny rule, decided on the dashboard approval card.

When the gate's ``deny-rules`` tier refuses a shell command in a session a person
is driving from the dashboard, the turn puts up an approval card instead of the
flat refusal: Allow once, Allow for this session, or Deny. Only the owner's own
dashboard click can send the two allow decisions (:data:`DENY_OVERRIDE_ACTIONS`,
checked by the slot approve route); any other answer to the card, including a
trust or YOLO sweep, is read as a refusal (:func:`override_decision`).

An allow does not approve the call by itself. The turn judges it again with the
rule's key waived (``HookManager.on_tool_call(..., deny_waivers=...)``), and the
transport floor judges it once more with the same keys, so any other rule the
command breaks still refuses, and every waiver used writes a ``deny_exception``
audit event or refuses. "Allow for this session" keeps the key on the slot in
memory (``_ChatSlot._deny_waivers``): it ends with the slot or a gateway restart
and never touches ``denied_commands.json``.

Never offered: for a rule a governance policy pins, for any other gate tier, for
an app-owned, channel-linked or Slack-linked slot, for a backend subagent's
request, or for a command whose bytes are not verified. Design:
``docs/request-for-change/rfc-deny-session-override.md``.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.security.deny_waiver import deny_key_is_pinned, deny_reason_key
from kiro_crew.trust_patterns import extract_bash_command

logger = logging.getLogger(__name__)

#: The gate tier whose refusals a person may override.
DENY_OVERRIDE_TIER = "deny-rules"
#: Card decision: run this one call with the rule waived.
DENY_OVERRIDE_ONCE = "deny_override_once"
#: Card decision: waive the rule for this slot until it ends or the gateway restarts.
DENY_OVERRIDE_SESSION = "deny_override_session"
#: The decisions only an override card accepts, and only from the owner's dashboard.
DENY_OVERRIDE_ACTIONS = frozenset({DENY_OVERRIDE_ONCE, DENY_OVERRIDE_SESSION})
#: The permission-row field that marks a card as an override card.
DENY_OVERRIDE_META_KEY = "deny_override_rule"
#: Slot origins (``SlotOrigin``) no person at the dashboard created: an app, a
#: cron job or the gateway itself. An untagged slot (``""``) and ``"user"`` remain.
_NOT_A_PERSON_ORIGINS = frozenset({"app", "cron", "system"})


@dataclass
class DenyOverrideOffer:
    """One override card's state, carried on the turn from the deny to the answer.

    ``key`` is the refusal key the card names; ``reason`` is the refusal text,
    already redacted for display; ``detail`` is a plain sentence on what the rule
    blocks (``deny_waiver.deny_key_description``), empty when none is known;
    ``scope`` is set from the answer (``"once"`` / ``"session"``) and empty until
    then.
    """

    key: str
    reason: str
    detail: str = ""
    scope: str = ""


def is_deny_override_card(messages: Any, request_id: str) -> bool:
    """Whether the newest permission row for *request_id* in *messages* is an override card."""
    if not request_id:
        return False
    for message in reversed(messages or ()):
        if not isinstance(message, dict) or message.get("role") != "permission":
            continue
        try:
            meta = json.loads(message.get("cls") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("request_id") == request_id:
            return bool(meta.get(DENY_OVERRIDE_META_KEY))
    return False


def slot_may_override(slot: Any, event: Any) -> bool:
    """Whether *event* on *slot* is a call a person at the dashboard can override.

    The slot must be one a person created and drives from the dashboard: not
    app-owned, not bound to a channel, cron or workflow session, not mirrored to
    Slack. The request must be the agent's own (not a backend subagent's) shell
    command whose bytes the transport delivered unredacted.
    """
    if getattr(slot, "_app", "") or getattr(slot, "unattended", False):
        return False
    if getattr(slot, "_origin", "") in _NOT_A_PERSON_ORIGINS:
        return False
    if getattr(slot, "linked_session_key", ""):
        return False
    if getattr(slot, "_slack_linked", False):
        return False
    if (
        getattr(slot, "channel_origin", False)
        or getattr(slot, "_channel_runtime_origin", False)
        or getattr(slot, "_turn_channel_origin", False)
    ):
        return False
    if getattr(event, "sub_session_id", "") or getattr(event, "child_low_fidelity", False):
        return False
    if not getattr(event, "is_shell", False) or getattr(event, "tool_input_redacted", False):
        return False
    tool_input = getattr(event, "tool_input", "") or ""
    return bool(tool_input and extract_bash_command(tool_input))


def slot_deny_waivers(slot: Any, event: Any) -> frozenset[str]:
    """The keys *slot*'s session overrides waive for *event*, or none.

    A request :func:`slot_may_override` would not offer a card for gets no
    waiver either, so a session override never reaches a subagent's or an
    unverified call.
    """
    waivers = getattr(slot, "_deny_waivers", None)
    if not waivers or not slot_may_override(slot, event):
        return frozenset()
    return frozenset(waivers)


def _pinned_rule_ids() -> "set[str] | None":
    """Every rule id a governance policy pins, or ``None`` when that is unreadable.

    The display accessor on purpose: it unions every loaded profile's pins with
    the active ceiling, so a rule pinned anywhere is never offered.
    """
    from kiro_crew import security

    try:
        return set(security.pinned_builtin_command_ids_for_snapshot())
    except Exception:
        logger.warning("governance pins unreadable; no deny override offered", exc_info=True)
        return None


def override_offer_key(tool_result: Any, slot: Any, event: Any) -> str | None:
    """The refusal key a card may offer to override for *tool_result*, else ``None``."""
    if getattr(tool_result, "tier", "") != DENY_OVERRIDE_TIER:
        return None
    key = deny_reason_key(getattr(tool_result, "reason", "") or "")
    if key is None or not slot_may_override(slot, event):
        return None
    pinned = _pinned_rule_ids()
    if pinned is None or deny_key_is_pinned(key, pinned):
        return None
    return key


def override_decision(outcome: str) -> tuple[str, str]:
    """``(outcome, scope)`` for the answer an override card received.

    Only :data:`DENY_OVERRIDE_ACTIONS` approve, with scope ``"once"`` or
    ``"session"``. ``rejected_once`` keeps its meaning. Everything else,
    a plain ``approved`` included (a trust or YOLO sweep, another surface's
    one-shot approve), is a refusal: no path but the owner's own click grants an
    override.
    """
    if outcome == DENY_OVERRIDE_ONCE:
        return "approved", "once"
    if outcome == DENY_OVERRIDE_SESSION:
        return "approved", "session"
    if outcome == "rejected_once":
        return "rejected_once", ""
    return "rejected", ""


def client_accepts_deny_waivers(client: Any) -> bool:
    """Whether *client*'s ``approve_tool`` can carry the waivers to its floor.

    A transport that cannot would judge the call without them and refuse it, so
    the override is not offered there in the first place.
    """
    try:
        params = inspect.signature(client.approve_tool).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return "deny_waivers" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
