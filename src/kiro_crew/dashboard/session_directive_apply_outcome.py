"""What applying a directive returns, and the notices a refused or armed one surfaces.

The outcome record and its terminal and quiet-end markers, the directive families the
dispatcher routes on, the refusal and success notices a monitor arm surfaces to the
user or a crewmate, and the refusal every directive raises. A new outcome field, notice
or directive family goes here. Its function-local imports are deliberate; the facade's
module docstring says why.

Served through :mod:`kiro_crew.dashboard.session_directive_apply`, which callers import
and tests patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to
this module, and this module never imports it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.session_surface import has_dashboard_surface

logger = logging.getLogger("kiro_crew.dashboard.session_directive_apply")


QUIET_END_OUTCOME_PREFIX = "Nothing new to report."

# The directives whose SUCCESSFUL application is the turn's intended terminal
# output: the model ends the turn without a closing reply, so the
# runner must not read the textless end as a failed generation. Membership
# here is documentation and a test pin; the runtime signal is
# ``DirectiveOutcome.ends_turn``, set by the applier only on the path where the
# effect actually landed (a quiet end recorded), never
# derived from the outcome prose.
TERMINAL_DIRECTIVES = frozenset({"nothing_to_do"})


@dataclass(frozen=True)
class DirectiveOutcome:
    """The applied result of one session directive.

    ``text`` is the human-readable confirmation every path has always returned
    (recorded on the transcript, read by the model gateway-on). ``ends_turn`` is
    the structured terminal-turn signal: True only when a directive in
    ``TERMINAL_DIRECTIVES`` applied its effect, so the runner skips the
    empty-response recovery for this turn. A refusal, an error and a delivery
    failure all leave it False — the model must then fall back to a plain reply.
    """

    text: str
    ends_turn: bool = False


# Card directives require a connected dashboard surface. ``set_project`` is
# admitted by the dispatcher's user-surface provenance gate, then separately requires
# the current turn to own the slot it would mutate. (``ask_question`` is not a
# directive: it blocks and returns the answers as its own tool result.)
_DASHBOARD_ONLY_DIRECTIVES = frozenset({"suggest_followup"})
_USER_SURFACE_DIRECTIVES = frozenset({"set_project", "reset_conversation", "chat_tag"})
# Directives whose effect is "this session will be woken later". A refusal of
# one of these is the failure the caller can least observe: the MCP tool has
# already answered "requested" over its own pipe by the time this consumer
# runs, and the model's turn is over -- so a refusal that stays in the log
# leaves a session that believes it armed a loop and is never woken again.
_ARMING_DIRECTIVES = frozenset({"monitor_start", "monitor_watch"})
# Directives that REVISE the loop this session already has. A refusal of one is
# unobservable in the same way and for the same reason: the tool has already
# answered "update requested" over its own pipe, so a denial that stays in the
# log leaves the agent reporting a revision that never landed while the loop
# keeps waking on its OLD instruction. The distinction from an arm is what the
# reader must be told -- automation is still running here, it is
# just running the previous text -- so the two share the mechanism and not the
# wording.
_REVISION_DIRECTIVES = frozenset({"monitor_update"})
# Directives that END the loop this session already has. A wake-delivered stop
# is gated on two checks (``_refuse_stale_wake_stop``, re-taken under the
# service lock by ``_StaleWakeStopGuard`` for the legacy writes). Identity: the wake's
# loop must still be this session's monitor, or a stale wake would stop the
# loop that REPLACED the one which delivered it. Retention: a row a person
# paused or stopped is retained evidence, and a legacy stop removes its row, so
# a wake may not delete it; a row the SYSTEM deactivated still passes and is
# the stop applier's business. The refusal is as unobservable as a refused arm
# or revision (the tool has already answered over its own pipe), so it is
# surfaced the same way, in stop wording: nothing here was armed or revised.
_STOP_DIRECTIVES = frozenset({"monitor_stop", "autonudge_stop"})

# Transcript row prefix for a refused arm. Fixed text so the frontend and tests
# can match on it; the authorizer's reason follows the colon.
ARM_REFUSAL_NOTICE_PREFIX = "⚠️ Automation loop NOT armed: "
# ``meta.kind`` stamped on every arm/revision/stop refusal notice. The crewmate
# chat's notice allowlist (``crewmateBubbles.CREWMATE_HIDDEN_NOTICE_KINDS``)
# hides a notice ONLY by its kind, so a refusal must carry one to be dropped
# there; every other notice (a dropped queued message, a model-fallback hint)
# keeps drawing. Fixed string, mirrored by that allowlist and the tests.
ARM_REFUSAL_NOTICE_KIND = "arm_refusal"
ARM_SUCCESS_NOTICE_PREFIX = "✅ Automation loop armed: "
# Same contract for a refused revision. Deliberately NOT the arm wording: a
# denied monitor_update leaves a loop in place, so "NOT armed" would report the
# wrong state, and the fact the reader acts on is which instruction the next
# wake will run.
REVISION_REFUSAL_NOTICE_PREFIX = (
    "⚠️ Automation loop NOT updated — it kept its previous instruction: "
)
# Same contract for a refused stop. Neither of the two above fits: nothing was
# armed and nothing was revised, and the loop the refusal names is untouched.
STOP_REFUSAL_NOTICE_PREFIX = "⚠️ Automation loop NOT stopped: "

# Live-only queue-entry meta keys that carry the ORIGINATING wake's provenance
# across the re-routed refusal into the crewmate's successor turn. The refusal
# enqueue is a system-injection kind, so the entry is NON-DURABLE
# (`_is_durable_queue_entry` excludes any entry carrying a `kind`) and these
# never reach disk — matching the rule that provenance flags grant authority
# and are never persisted, so a hand-edited restored queue line cannot forge
# self-wake authority. The drain reads them off the live entry and sets
# `_run_chat`'s `_directive_self_wake` / `_directive_loop_id`, so a crewmate
# that retries `monitor_stop` from this successor turn still hits the stale-wake
# guard instead of removing a paused loop record a person kept.
REFUSAL_SELF_WAKE_META_KEY = "refusalSelfWake"
REFUSAL_WAKE_LOOP_ID_META_KEY = "refusalWakeLoopId"


def _surface_arm_refusal(
    state: Any,
    slot: Any,
    kind: str,
    reason: str,
    *,
    prefix: str = ARM_REFUSAL_NOTICE_PREFIX,
    session_key: str = "",
    producer_is_self_wake: bool = False,
    producer_wake_loop_id: str = "",
) -> None:
    """Append a ``notice`` row so a refused arm is VISIBLE where the session lives.

    The directive consumer runs AFTER the model received the tool's own
    non-committal ack ("Monitor loop requested ...", see
    ``mcp_tools/control_parts/monitor.py``), and gateway-off the applier's string cannot
    replace that ack -- it only overwrites the tool_result row. The one surface
    that reliably reaches whoever is watching the session (the user, a member
    thread's reader, the next turn's transcript replay) is a row of its own, so
    a refusal gets one. Slot-less callers (a channel transport's TurnDriver)
    have no transcript window; the returned string is their only surface.

    A crewmate's DM is the one surface where the ``notice`` row does not reach
    its reader: that chat draws messages only (``crewmateBubbles.isCrewmateChatRow``
    drops every ``notice``), and a ``notice`` row is not one of the roles the
    history replay feeds back to the model either, so there a refused arm would
    reach NEITHER the person nor the crewmate. For a member session the refusal
    is therefore ALSO queued for the crewmate's next turn as an injected
    message -- the same path a ``[Cron notification]`` takes -- so the crewmate
    can stop and re-arm the loop itself. The person-facing ``notice`` row is
    still written (it stays the surface for every non-crewmate session, and the
    Sessions page keeps drawing it).

    ``prefix`` selects the wording for the class of directive that was refused
    (an arm by default, a revision for ``monitor_update``, a stop for
    ``monitor_stop`` / ``autonudge_stop``). Only the leading
    text differs: the redaction, the row role and the best-effort contract are
    the same guarantees either way, which is why this is one helper.

    Best-effort: a notice is telemetry about a refusal that has already been
    audited, so it must never turn a clean denial into an exception.
    """
    if slot is None:
        return
    try:
        # Local import: state -> chat_utils -> ... cycles with this module the
        # same way ``sel`` does (see the facade's module docstring).
        from kiro_crew.dashboard.state import append_and_surface
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        # The reason interpolates the authorizer's message, which can echo an
        # LLM-derived value (a target, a slot key), so scrub it like every other
        # transcript egress before it is persisted or broadcast.
        text, _ = redact_exfiltration_urls(f"{prefix}{reason}")
        text, _ = redact_credentials(text)
        append_and_surface(
            state, slot, "notice", text, "msg msg-info", meta={"kind": ARM_REFUSAL_NOTICE_KIND}
        )
        _deliver_refusal_to_crewmate(
            slot,
            session_key,
            text,
            producer_is_self_wake=producer_is_self_wake,
            producer_wake_loop_id=producer_wake_loop_id,
        )
    except Exception:
        logger.debug("arm-refusal notice for %s could not be surfaced", kind, exc_info=True)


def _deliver_refusal_to_crewmate(
    slot: Any,
    session_key: str,
    text: str,
    *,
    producer_is_self_wake: bool = False,
    producer_wake_loop_id: str = "",
) -> None:
    """Queue the refusal for a crewmate's next turn, so the agent still sees it.

    A crewmate's DM draws messages only and never replays a ``notice`` row to
    the model, so the ``notice`` written above reaches neither its reader nor
    the crewmate. Only there is a second surface needed: the refusal is queued
    with ``AUTOMATION_NOTICE_KIND`` so it drains as an ``inject`` row (never a
    ``user`` row shown as the person's own bubble) that the crewmate's next turn
    carries, and the crewmate can call ``monitor_stop`` + ``monitor_start``
    itself. Every other session keeps the ``notice`` row as its only surface.

    When the refused directive was itself delivered by the loop's own wake, the
    originating ``producer_is_self_wake`` / ``producer_wake_loop_id`` are stamped
    on the (non-durable) entry meta so the drain can rebuild them on the
    successor turn: a crewmate that retries ``monitor_stop`` from that turn then
    still passes the stale-wake guard rather than silently removing a paused loop
    record a person kept. The entry carries a ``kind``, so it is never persisted
    (`_is_durable_queue_entry`) and the authority these flags grant cannot be
    forged by a hand-edited restored queue line.

    Best-effort, like its caller: a delivery failure is telemetry about a
    refusal already audited and must never fail the denial. The system-injection
    kind makes an idle crewmate pick it up on its next drain, and keeps it out of
    a user-message merge (``steer_queue``), so it is never counted as the person
    stepping in.
    """
    from kiro_crew.dashboard.chat_utils import AUTOMATION_NOTICE_KIND
    from kiro_crew.members import is_member_session_key

    key = session_key or getattr(slot, "key", "") or ""
    if not is_member_session_key(key):
        return
    append = getattr(slot, "queue_append", None)
    if not callable(append):
        return
    meta: dict[str, Any] | None = None
    if producer_is_self_wake:
        # Carry the originating wake's provenance to the successor turn, so a
        # retried stop from it still passes the stale-wake guard. Only when the
        # refused directive was itself a self-wake, so an ordinary refusal does
        # not fabricate wake authority.
        meta = {REFUSAL_SELF_WAKE_META_KEY: True}
        if producer_wake_loop_id:
            meta[REFUSAL_WAKE_LOOP_ID_META_KEY] = producer_wake_loop_id
    append(f"[Automation notice] {text}", kind=AUTOMATION_NOTICE_KIND, meta=meta)


def _surface_arm_success(state: Any, slot: Any, summary: str) -> None:
    """Append a ``notice`` row saying the loop IS armed, and how.

    The twin of :func:`_surface_arm_refusal`, for the same reason: the MCP
    tool's own ack is deliberately non-committal ("requested"), so without a
    row of its own a successful arm is as invisible as a refused one. Same
    best-effort contract -- a notice failure never fails the arm.
    """
    if slot is None:
        return
    try:
        from kiro_crew.dashboard.state import append_and_surface
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        text, _ = redact_exfiltration_urls(f"{ARM_SUCCESS_NOTICE_PREFIX}{summary}")
        text, _ = redact_credentials(text)
        append_and_surface(state, slot, "notice", text, "msg msg-info")
    except Exception:
        logger.debug("arm-success notice could not be surfaced", exc_info=True)


def _describe_interval(secs: int) -> str:
    secs = int(secs)
    if secs % 3600 == 0:
        hours = secs // 3600
        return f"{hours} hour" + ("" if hours == 1 else "s")
    if secs % 60 == 0:
        minutes = secs // 60
        return f"{minutes} min"
    return f"{secs}s"


def _describe_next_wake(loop: Any, *, verb: str = "first wake") -> str:
    """Render the armed record's next deadline as "first wake in ~Ns (HH:MM:SS UTC)".

    Read off the ARMED loop, never off the request: ``next_due_ts`` is what the
    timer actually arms toward, so this is the one number that cannot disagree
    with the fire. An unset or unreadable deadline yields "" and the caller
    omits the clause rather than inventing a time.
    """
    try:
        due = float(getattr(loop, "next_due_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return ""
    if due <= 0:
        return ""
    remaining = max(0, int(round(due - time.time())))
    stamp = time.strftime("%H:%M:%S UTC", time.gmtime(due))
    return f"{verb} in ~{remaining}s ({stamp})"


# The provenance gate's set: the user-surface directives plus ``suggest_followup``,
# whose surface gate alone (``_DASHBOARD_ONLY_DIRECTIVES``) only asks whether a tab
# exists -- which a cron or sub-agent turn riding a user's slot satisfies.
_USER_ORIGIN_DIRECTIVES = _USER_SURFACE_DIRECTIVES | frozenset({"suggest_followup"})


def _has_user_surface(session_key: str) -> bool:
    """Return whether *session_key* names a user-facing conversation."""
    return has_dashboard_surface(session_key) or is_channel_session_key(session_key)


class _DirectiveDenied(Exception):
    """Raised by an applier when the directive is REFUSED — a permission
    decision (e.g. a sensitive-path block), an unsupported session type, or an
    authorizer refusal. Audited as ``outcome="denied"`` by the wrapper. The
    distinction from a plain returned string matters for the SEL chain: every
    path where the effect was NOT applied must never audit ``success``."""


_DirectiveDenied.__module__ = "kiro_crew.dashboard.session_directive_apply"
