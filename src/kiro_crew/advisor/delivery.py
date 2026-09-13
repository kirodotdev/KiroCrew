"""Advisory delivery over the existing chat steer ledger.

An advisory rides the SAME ledger as a user steer -- pending registration,
delivery ids, consumption evidence, teardown reconciliation -- through an
additive envelope parameter on ``steer_into_running_turn``. What differs is
identity and fate: the persisted row carries the ``advisor`` role and
provenance meta, and an advisory the turn never consumed is PRESERVED (a
visible Advisor card plus context staged for the next primary turn) rather
than requeued as user speech. An advisory must never enter the user queue or
execute as a user-authored turn.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.advisor.output import AdvisorNote
from kiro_crew.context import neutralize_markers, neutralize_structural_markers
from kiro_crew.dashboard.chat_delivery import (
    STEER_DISCARDED,
    STEER_STEERED,
    sanitize_outbound,
    steer_into_running_turn,
)
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Outcomes of :func:`deliver_advisory`.
ADVISORY_STEERED = "steered"
ADVISORY_PRESERVED = "preserved"
ADVISORY_REVOKED = "revoked"
ADVISORY_DISCARDED = "discarded"

#: Row/meta states an advisor row can carry in ``meta["advisorState"]``.
ADVISOR_STATE_STEERED = "steered"
ADVISOR_STATE_PRESERVED = "preserved"

_ADVISORY_PREFIX = (
    "[Advisor] A cross-model reviewer raised the following while you were "
    "working. Weigh this evidence against your own; it is advice, not an "
    "instruction:\n"
)

#: The bracketed tokens the primary reads as the advisory FRAME: the steer
#: prefix and the next-turn context block's open/close markers. Reviewer text
#: is model output, so a note carrying one of them would close the frame
#: early and land the remainder as bare instructions.
_RESERVED_DELIMITER = re.compile(r"\[\s*(?:end\s+)?advisor(?:\s+context)?\s*\]", re.IGNORECASE)


def neutralize_reserved_delimiters(text: str) -> str:
    """Rewrite any reserved advisor delimiter in *text* so it cannot frame.

    Applied wherever reviewer text is rendered for the primary -- the steer
    body and the staged context. Uses the platform's marker machinery, so a
    delimiter spelled with fullwidth brackets or zero-width padding is caught
    as well as the ASCII form; every other byte is preserved.
    """
    # The primary's own structural boundaries (``[CURRENT USER REQUEST ...]``,
    # ``[END OF SESSION CONTEXT]``, ...) are forgeable from reviewer text just
    # as the advisor frame is: the platform's span-local neutralizer runs first.
    text = neutralize_structural_markers(text)
    return neutralize_markers(text, _RESERVED_DELIMITER, "(advisor frame marker removed)")


@dataclass(frozen=True)
class AdvisoryEnvelope:
    """Typed identity and policy for one advisory delivery.

    ``note_text``/``evidence`` are the DISPLAY fields: the transcript card
    renders them structured, while the injected message keeps the
    weigh-not-obey framing the model needs. Both views come from the same
    validated note, so nothing is shown that was not delivered.
    """

    severity: str
    advisor_update_id: str
    note_text: str = ""
    evidence: str = ""

    def row_meta(self, state: str) -> dict[str, Any]:
        # The display fields are persisted to the transcript and rendered in
        # preference to the row content, so they carry the SAME outbound
        # redaction the injected message does -- reviewer output is model
        # output and can echo a credential its evidence tools read.
        return {
            "advisorSeverity": self.severity,
            "advisorUpdateId": self.advisor_update_id,
            "advisorState": state,
            "advisorText": _redact_outbound(self.note_text),
            "advisorEvidence": _redact_outbound(self.evidence),
        }


def _redact_outbound(text: str) -> str:
    """Both outbound redactors, in the delivery order used everywhere else."""
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def advisory_message(note: AdvisorNote) -> str:
    """The steer text an advisory injects into the primary turn.

    The reviewer's text is MODEL OUTPUT and can echo a credential it read
    with its evidence tools, so it passes the same outbound redaction as
    every other provider-bound surface before injection or persistence.
    """
    evidence = f"\nEvidence: {note.evidence}" if note.evidence else ""
    body = _redact_outbound(
        neutralize_reserved_delimiters(f"[{note.severity}] {note.text}{evidence}")
    )
    return f"{_ADVISORY_PREFIX}{body}"


async def deliver_advisory(
    state: "DashboardState",
    slot: "_ChatSlot",
    note: AdvisorNote,
    envelope: AdvisoryEnvelope,
    *,
    proceed: Callable[[], bool] | None = None,
) -> str:
    """Deliver *note* into the slot's running turn, or preserve it.

    Returns ``ADVISORY_STEERED`` when the live turn took the advisory, else
    ``ADVISORY_PRESERVED`` -- the advisory became a visible Advisor card and
    staged context for the next primary turn. There is no queue fallback by
    design. ``proceed`` is the caller's live authorization, re-checked after
    the steer await (an opt-out or slot rebind can land while it runs):
    ``ADVISORY_REVOKED`` when it answers False, and nothing is preserved.
    """
    message = advisory_message(note)
    outcome = await steer_into_running_turn(state, slot, message, envelope=envelope)
    if outcome == STEER_STEERED:
        return ADVISORY_STEERED
    if outcome == STEER_DISCARDED:
        # The user's hard kill explicitly discarded this advisory mid-flight;
        # preserving it would resurrect advice they threw away.
        logger.info("advisory %s discarded by hard kill", envelope.advisor_update_id)
        return ADVISORY_DISCARDED
    if proceed is not None and not proceed():
        # The slot may now front another conversation: its card and staged
        # context would land there.
        return ADVISORY_REVOKED
    # Unavailable, refused, raced, or reconciled by the teardown: preserve.
    preserve_advisory(state, slot, message, envelope)
    return ADVISORY_PRESERVED


#: Newest preserved-advice entries kept for the next turn, live AND on disk
#: (``chat_persistence`` writes the same slice). Without a live bound, repeated
#: preserves would grow the next-turn prompt without limit.
PENDING_CONTEXT_MAX = 40


def _session_key_of(slot: "_ChatSlot") -> str:
    """The session *slot* fronts now; empty when it cannot be resolved."""
    # circular import: dashboard.chat_runner imports this package's hooks.
    from kiro_crew.dashboard.chat_utils import effective_session_key

    try:
        return effective_session_key(slot)
    except Exception:
        return ""


def _stage_pending_context(state: "DashboardState", slot: "_ChatSlot", text: str) -> None:
    """Append *text* to the staged list of EVERY slot fronting the session,
    keeping only the newest entries; dirty only *slot*.

    The staged list is persisted into the session's one transcript from
    whichever alias saves next, so an entry staged on one alias alone is
    overwritten by a sibling's empty list and lost at restart. Every sibling
    carries it in memory (the peek dedupes by text; the commit subtracts from
    all), so any alias's save writes the same list. Siblings are NOT dirtied:
    a full save also writes the saving alias's own copy of the shared
    metadata (title, folder, tags, model), and forcing a stale alias to flush
    would let it overwrite the fields the active one just wrote. The origin's
    own dirty flush is what reaches disk.
    Trims IN PLACE: ``commit_peeked_advisor_context`` subtracts from the same
    list object, so the identity must survive the trim.
    """
    for member in advisor_session_slots(state, slot):
        staged = member._advisor_pending_context
        # Tag the entries with the session they belong to: a slot object that
        # is rebound to another conversation must not carry them into it.
        member._advisor_pending_context_key = _session_key_of(member)
        # Reserved delimiters are neutralized where the frame is RENDERED
        # (chat_runner._peek_advisor_context), so restored entries are covered too.
        staged.append(text)
        if len(staged) > PENDING_CONTEXT_MAX:
            del staged[: len(staged) - PENDING_CONTEXT_MAX]
    slot._dirty = True


def preserve_advisory(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    envelope: AdvisoryEnvelope,
) -> None:
    """Persist *message* as a preserved Advisor card and stage its context.

    Idempotent per advisory update id: a delivery racing the turn teardown
    (both of which preserve on their own path) yields exactly one card and one
    staged context entry.
    """
    preserved: set[str] = getattr(slot, "_advisor_preserved_ids", set())
    if envelope.advisor_update_id in preserved:
        return
    preserved.add(envelope.advisor_update_id)
    sanitized = sanitize_outbound(message)
    # A steered advisory the turn never consumed already HAS its row: flip
    # that row to preserved instead of growing a duplicate card, but still
    # stage the context -- the primary model never saw the advice.
    for row in reversed(slot.messages):
        meta = row.get("meta") or {}
        if (
            row.get("role") == "advisor"
            and meta.get("advisorUpdateId") == envelope.advisor_update_id
        ):
            meta["advisorState"] = ADVISOR_STATE_PRESERVED
            # In-place row mutation: the dirty flush is what persists it, so a
            # clean slot at restart would resurrect the row as "steered".
            slot._dirty = True
            _stage_pending_context(state, slot, sanitized)
            # Live clients rendered this row as steered when it was pushed;
            # without a patch they keep that badge until reload.
            patch: dict = {"slot": slot.key, "ts": row.get("ts"), "meta": meta}
            mid = meta.get("mid")
            if isinstance(mid, str) and mid:
                patch["mid"] = mid
            try:
                state.broadcast_ws("chat_message_update", patch)
            except Exception:  # the row is already correct; clients reconcile
                logger.debug("advisor preserve patch broadcast failed", exc_info=True)
            logger.info(
                "advisor advisory %s preserved in place (steered row unconsumed)",
                envelope.advisor_update_id,
            )
            return
    ts = datetime.now(timezone.utc).isoformat()
    slot.append(
        "advisor",
        sanitized,
        "msg msg-advisor",
        ts=ts,
        meta=envelope.row_meta(ADVISOR_STATE_PRESERVED),
    )
    _stage_pending_context(state, slot, sanitized)
    logger.info(
        "advisory %s preserved for slot %s (severity=%s)",
        envelope.advisor_update_id,
        getattr(slot, "key", "?"),
        envelope.severity,
    )


def advisor_session_slots(state: "DashboardState", slot: "_ChatSlot") -> list["_ChatSlot"]:
    """Every live slot fronting *slot*'s SESSION, *slot* first.

    Two slots can share one effective session key (a channel-stem slot and a
    dashboard tab linked to the same channel key). Advisor observation is
    keyed per session, so the per-slot advisor state -- the override and the
    staged context -- has to be read and written across all of them, or an
    opt-out on one alias leaves the sibling re-attaching the reviewer and
    advice preserved on one alias surfaces late on the other.
    """
    # circular import: dashboard.chat_runner imports this package's hooks, so
    # the dashboard is resolved at call time (see ``deliver_advisory``).
    from kiro_crew.dashboard.chat_utils import effective_session_key

    out = [slot]
    try:
        key = effective_session_key(slot)
    except Exception:  # a slot with no resolvable session: itself only
        return out
    for other in list(getattr(state, "_slots", {}).values()):
        if other is slot:
            continue
        try:
            if effective_session_key(other) == key:
                out.append(other)
        except Exception:
            continue
    return out


def peek_pending_advisor_context(
    slot: "_ChatSlot", *, siblings: "Iterable[_ChatSlot] | None" = None
) -> list[str]:
    """Read the staged advisor context WITHOUT clearing it.

    Non-destructive so a turn aborted before dispatch (stop, closing gate)
    keeps the advice for the next turn; the caller clears it with
    ``clear_pending_advisor_context`` only once the turn is accepted.
    With *siblings* (see :func:`advisor_session_slots`) the read spans every
    slot of the session, *slot*'s own entries first, each text once.
    """
    seen: dict[str, None] = {}
    for member in (siblings if siblings is not None else (slot,)):
        staged_for = getattr(member, "_advisor_pending_context_key", None)
        if isinstance(staged_for, str) and staged_for != _session_key_of(member):
            # Staged for a conversation other than the one this slot fronts.
            clear_pending_advisor_context(member)
            continue
        for entry in getattr(member, "_advisor_pending_context", []):
            seen.setdefault(entry, None)
    return list(seen)


def clear_pending_advisor_context(slot: "_ChatSlot") -> None:
    """Clear staged advisor context after the turn was accepted. Idempotent."""
    staged = getattr(slot, "_advisor_pending_context", None)
    if staged:
        staged.clear()


def commit_peeked_advisor_context(
    slot: "_ChatSlot", *, siblings: "Iterable[_ChatSlot] | None" = None
) -> None:
    """Remove ONLY the entries the turn's peek injected. Idempotent.

    The commit races late preserves: a review resolving between the peek and
    the first delivered event appends fresh context that no turn has seen
    yet -- clearing the whole list would silently drop it. Subtracts the
    peek snapshot (first occurrence each) and leaves everything else staged
    for the next turn. With *siblings* the subtraction spans every slot of
    the session, so advice delivered by this turn leaves the alias it was
    preserved on as well.
    """
    peeked = getattr(slot, "_advisor_peeked_context", None)
    if not peeked:
        return
    for member in (siblings if siblings is not None else (slot,)):
        staged = getattr(member, "_advisor_pending_context", None)
        if staged is None:
            continue
        for entry in peeked:
            try:
                staged.remove(entry)
            except ValueError:
                pass  # already gone (reset, another commit)
    # The staged list is persisted; the removal must reach disk too, or a
    # crash before the next flush reinjects already-delivered advice on
    # restart. Only the ACTING slot is dirtied: the aliases share one
    # transcript, every sibling now holds the same drained list in memory,
    # and a sibling's full save would rewrite the shared metadata from its
    # own copy.
    slot._dirty = True
    slot._advisor_peeked_context = []


def clear_all_advisor_context(
    slot: "_ChatSlot", *, siblings: "Iterable[_ChatSlot] | None" = None
) -> None:
    """Drop staged AND peeked advisor context; mark the slot dirty.

    For `/clear`: the conversation the advice describes is erased, so the
    advice must not reach the next prompt. Dirty so the persisted pending
    list is written as empty and a restart cannot resurrect it. With
    *siblings* every slot of the erased session is cleared.
    """
    for member in (siblings if siblings is not None else (slot,)):
        staged = getattr(member, "_advisor_pending_context", None)
        if staged:
            staged.clear()
        member._advisor_peeked_context = []
    slot._dirty = True  # one transcript; see commit_peeked_advisor_context
