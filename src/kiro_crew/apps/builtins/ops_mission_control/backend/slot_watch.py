"""Reconcile incident status against the live investigation slot.

The board's job is to be true. An incident whose agent is parked on a tool
approval is **blocked on the operator**, but nothing in the dispatch path notices
that: the incident keeps whatever status it had when the agent started, so the
board reports "Dispatched" — progressing — for work that has actually stopped and
is waiting for a human. That is the one failure mode an ops board cannot have,
because the operator's whole reason to look at it is to find what needs them.

This module derives the truth from the slot rather than trusting stored intent:

* slot has a pending approval  → ``needs_human`` / ``awaiting_approval``
* slot is waiting for input    → ``needs_human`` / ``awaiting_input``
* slot is running              → ``investigating``, not blocked
* slot finished, no diagnosis  → ``needs_human`` / ``awaiting_diagnosis``

Derived, not stored, so it cannot go stale: if the operator approves the command
from the embedded chat, the very next reconcile puts the incident back to
``investigating`` without anyone having to remember to clear a flag.

Observed live during beta testing: the investigating agent's FIRST action was a
read-only AWS probe, which parked on a ``permission`` message while the incident
was still ``dispatched`` — hence ``dispatched -> needs_human`` is a legal edge.

See ``docs/system-specs/modules/ops-mission-control.md``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    BLOCKED_ON_APPROVAL,
    BLOCKED_ON_DIAGNOSIS,
    BLOCKED_ON_INPUT,
    LEGAL_TRANSITIONS,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    Incident,
)

logger = logging.getLogger(__name__)

#: Message role the gateway writes when a tool call is awaiting approval. A slot
#: whose LAST message carries this role is parked, even when the slot's own
#: ``pending_approval`` flag has not been set yet (the flag and the message are
#: written by different paths, and the message lands first).
_PERMISSION_ROLE = "permission"

#: Statuses we will move an incident OUT of during reconciliation. Terminal states
#: are never touched: a resolved incident is finished, and reviving it because its
#: slot still exists would resurrect closed work.
_RECONCILABLE: frozenset[str] = frozenset({"dispatched", "investigating", "needs_human"})


def _slot_is_blocked(slot: dict[str, Any]) -> str:
    """Return the ``BLOCKED_ON_*`` reason for a slot, or "" if it is not blocked."""
    if slot.get("pending_approval"):
        return BLOCKED_ON_APPROVAL
    if slot.get("waiting_for_input"):
        return BLOCKED_ON_INPUT

    # The flag lags the transcript, so also treat a trailing permission message as
    # blocked. Without this the board stays wrong for however long that gap is.
    messages = slot.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == _PERMISSION_ROLE:
            return BLOCKED_ON_APPROVAL
    return ""


def derive_status(incident: Incident, slot: dict[str, Any] | None) -> tuple[str, str]:
    """Compute ``(status, blocked_reason)`` for an incident from its slot.

    Returns the incident's CURRENT values unchanged when there is nothing to say —
    a missing slot is not evidence of anything (the agent may not have created it
    yet), so it must never be read as "blocked" or as "done".
    """
    if slot is None:
        return incident.status, incident.blocked_reason

    reason = _slot_is_blocked(slot)
    if reason:
        return STATUS_NEEDS_HUMAN, reason

    if slot.get("running"):
        # Actively working: not blocked, whatever it was before.
        return STATUS_INVESTIGATING, ""

    # Idle slot with turns taken but no diagnosis recorded: the agent stopped
    # without reaching a conclusion, which needs a person to look.
    messages = slot.get("messages")
    took_turns = isinstance(messages, list) and len(messages) > 1
    if took_turns and not incident.diagnosis:
        return STATUS_NEEDS_HUMAN, BLOCKED_ON_DIAGNOSIS

    return incident.status, ""


def reconcile(incident_id: str, slot: dict[str, Any] | None) -> Incident | None:
    """Apply the derived status to one incident. Returns it when changed.

    Illegal transitions are skipped rather than raised: reconciliation is a
    background truth-sync, and a status the grammar forbids means our derivation
    is wrong for this case — not that the caller did something invalid.
    """
    incident = store.get_incident(incident_id)
    if incident is None or incident.status not in _RECONCILABLE:
        return None

    status, reason = derive_status(incident, slot)
    if status == incident.status and reason == incident.blocked_reason:
        return None

    if status != incident.status and status not in LEGAL_TRANSITIONS.get(
        incident.status, frozenset()
    ):
        logger.debug(
            "ops-mission-control: skipping reconcile %s: %s -> %s is not a legal edge",
            incident_id,
            incident.status,
            status,
        )
        return None

    try:
        return store.transition(incident_id, status, blocked_reason=reason)
    except json.JSONDecodeError:
        # Corruption is excluded from the tolerance below on purpose, and this clause
        # exists to stop it being taken by accident: `JSONDecodeError` subclasses
        # `ValueError`, which the next clause catches for the unrelated case of an
        # illegal transition. Sharing one handler would file "the ledger is corrupt"
        # under "we lost a race" at debug level.
        #
        # Unlike the EACCES path below, corruption is not transient: it will fail every
        # future reconcile identically until a person fixes the file, so swallowing it
        # means this instance quietly stops syncing status forever.
        raise
    except (KeyError, ValueError):
        logger.debug("ops-mission-control: reconcile lost a race on %s", incident_id)
        return None
    except OSError:
        # Reconciliation's whole job is to survive a mess, so it keeps its tolerant
        # contract even though the store below it is now strict: `transition` reads the
        # index for update, which refuses rather than publishing over a read it could not
        # make, and that refusal must not turn a best-effort reconcile into a raised
        # error on the board request that triggered it.
        #
        # Tolerating it is safe BECAUSE the store refused: the mutation was abandoned
        # before writing, so the incident keeps whatever status it already had -- which is
        # exactly what returning ``None`` here already means -- and the next reconcile
        # pass retries from a clean read. Logged one level up from the race cases
        # (`warning`, not `debug`) because an unreadable index is a real fault worth
        # seeing, where losing a race is ordinary. Found in review (GPT 5.6).
        logger.warning(
            "ops-mission-control: reconcile skipped %s, dispatch index I/O failed",
            incident_id,
            exc_info=True,
        )
        return None


def blocked_summary(incidents: list[Incident]) -> dict[str, int]:
    """Count incidents waiting on a person, by reason — for the board's header."""
    counts: dict[str, int] = {}
    for inc in incidents:
        if inc.status == STATUS_NEEDS_HUMAN and inc.blocked_reason:
            counts[inc.blocked_reason] = counts.get(inc.blocked_reason, 0) + 1
    return counts
