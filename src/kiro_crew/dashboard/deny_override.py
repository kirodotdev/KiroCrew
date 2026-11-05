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

Never offered: for a rule a governance policy pins or the companion
``SecurityOverlay`` adds, for a self-guard rule (a self-protection floor, or any
self-protection or credential-exfiltration rule, see
``deny_waiver.deny_key_is_self_guard``), for any other gate tier, for an app-owned,
channel-linked or Slack-linked slot, on a backend whose sub-agent calls its
transport cannot tell apart (codex and any backend not proven, see
``security.deny_waiver``), for a backend subagent's request, or for a command
whose bytes are not verified. Design:
``docs/request-for-change/rfc-deny-session-override.md``.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.security.denied_rules import _rule_id_for_pattern
from kiro_crew.security.deny_waiver import (
    deny_key_is_overlay,
    deny_key_is_pinned,
    deny_key_is_self_guard,
    deny_reason_key,
)
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


#: Rules whose prefix family would misdescribe them, read before the prefixes.
#: The S3 copy and S3 API upload rules send any local file off the machine, not
#: only keys. The rest get ``""``, the card's generic line, because no family line
#: fits: the SSM read rules only read a command's output, the SSM run rules run
#: commands on remote instances, and the export rules set keys rather than read
#: or send them.
_FAMILY_BY_RULE_ID: dict[str, str] = {
    "credential-exfil-s3-cp": "file_upload",
    "credential-exfil-s3-mv": "file_upload",
    "credential-exfil-s3-sync": "file_upload",
    "aws-destructive-s3api-put-object": "file_upload",
    "aws-destructive-s3api-multipart-upload": "file_upload",
    "aws-destructive-ssm-get-command-invocation": "",
    "aws-destructive-ssm-list-command-invocations": "",
    "aws-destructive-ssm-send-command": "",
    "aws-destructive-ssm-start-session": "",
    "credential-exfil-export-aws-access": "",
    "credential-exfil-export-aws-secret": "",
}
#: Rule-id prefix -> the family an override card names in plain words. The card
#: shows one translated line per family (``components.denyOverride.family.*`` in
#: the dashboard catalog); the server sends only the family's slug. The first
#: matching prefix wins, so a narrower prefix sits above a wider one.
_FAMILY_BY_RULE_PREFIX: tuple[tuple[str, str], ...] = (
    ("git-publish-", "git_publish"),
    ("self-protection-", "self_protection"),
    ("sandbox-escape-", "sandbox_escape"),
    ("credential-exfil-", "credentials"),
    ("data-exfil-", "file_upload"),
    ("reverse-shell-", "remote_control"),
    ("aws-destructive-", "cloud"),
    ("iac-teardown-", "cloud"),
    ("local-destructive-rm-", "wipe"),
    ("local-destructive-mkfs", "wipe"),
    ("local-destructive-dd-", "wipe"),
    ("local-destructive-chmod-", "permissions"),
    ("local-destructive-chown-", "permissions"),
    ("local-destructive-git-reset-", "discard_changes"),
    ("pipe-to-shell-", "download_run"),
    ("sql-", "database"),
)


def deny_key_family(key: str) -> str:
    """The plain-language family of the rule *key* names, or ``""`` when unknown.

    *key* is a rule id (a git-publish tag, a floor id) or a pattern, resolved to
    its built-in rule id. An operator rule, or any id no prefix covers, has no
    family; the card then shows its generic line.
    """
    if not key:
        return ""
    rule_id = _rule_id_for_pattern(key) or key
    if rule_id in _FAMILY_BY_RULE_ID:
        return _FAMILY_BY_RULE_ID[rule_id]
    for prefix, family in _FAMILY_BY_RULE_PREFIX:
        if rule_id.startswith(prefix):
            return family
    return ""


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
    Slack. The turn must be one the owner sent there, with no messaging-channel
    steer admitted into it. The request must come from a backend whose sub-agent calls its
    transport tells apart (``AcpEvent.child_calls_marked``), be the agent's own
    (not a backend subagent's) and be a shell command whose bytes the transport
    delivered unredacted.
    """
    if getattr(slot, "_app", "") or getattr(slot, "unattended", False):
        return False
    if getattr(slot, "_origin", "") in _NOT_A_PERSON_ORIGINS:
        return False
    if getattr(slot, "linked_session_key", ""):
        return False
    if getattr(slot, "_slack_linked", False):
        return False
    # Only a turn the OWNER sent from the dashboard (``_turn_owner_sent``, which
    # implies ``_turn_user_sent``): only the owner can grant an override, so a
    # turn another dashboard user sent, a cron delivery, an app injection, a loop
    # wake or a sub-agent's completion gets no card and does not reuse a session
    # waiver.
    if getattr(slot, "_turn_owner_sent", False) is not True:
        return False
    if (
        getattr(slot, "channel_origin", False)
        or getattr(slot, "_channel_runtime_origin", False)
        or getattr(slot, "_turn_channel_origin", False)
        # A messaging-channel steer admitted into this turn narrows it to
        # channel authority for the rest of the turn.
        or getattr(slot, "_turn_channel_narrowed", False)
    ):
        return False
    # Only a backend whose transport is proven to mark a sub-agent's call
    # (``deny_waiver.child_calls_marked``); on any other, codex included, a child's
    # call would look like the parent's, so nothing below could keep it out.
    if getattr(event, "child_calls_marked", False) is not True:
        return False
    if getattr(event, "sub_session_id", "") or getattr(event, "child_low_fidelity", False):
        return False
    # A native KAS sub-agent shares this session's id, so it has no
    # sub_session_id; the transport marks its calls instead.
    if getattr(event, "native_child_call", False):
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


def _overlay_patterns() -> "set[str] | None":
    """The companion ``SecurityOverlay``'s patterns, or ``None`` when unreadable.

    Read through the platform authority's ``effective_patterns`` (the overlay's
    ADD-only patterns; the static baseline is empty), the same list the gate
    judges with, so a rule the overlay adds is never offered.
    """
    from kiro_crew.platform.context import current_context

    try:
        return set(current_context().security.effective_patterns())
    except Exception:
        logger.warning("security overlay unreadable; no deny override offered", exc_info=True)
        return None


def override_offer_key(tool_result: Any, slot: Any, event: Any) -> str | None:
    """The refusal key a card may offer to override for *tool_result*, else ``None``."""
    if getattr(tool_result, "tier", "") != DENY_OVERRIDE_TIER:
        return None
    key = deny_reason_key(getattr(tool_result, "reason", "") or "")
    # A self-guard rule keeps its flat refusal: no card, so no waiver either.
    if key is None or deny_key_is_self_guard(key) or not slot_may_override(slot, event):
        return None
    pinned = _pinned_rule_ids()
    if pinned is None or deny_key_is_pinned(key, pinned):
        return None
    overlay = _overlay_patterns()
    if overlay is None or deny_key_is_overlay(key, overlay):
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
