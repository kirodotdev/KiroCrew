"""Session control: letting one chat session observe and interrupt another.

Three operations — create a session, stop its turn, read its transcript — plus the
authorization that decides whether a caller may address a target at all. The
operations are deliberately thin: they reuse the same creation, stop and history
paths the dashboard itself uses, so a controlled session behaves exactly like one
a human is typing into.

**One verb here writes into another session's conversation: ``session_send``.**
Reading returns a transcript tail; stopping cancels an in-flight turn the way the
Stop button does; creating opens an empty session in the user's sidebar; sending
delivers a message the target runs as its next turn, redacted through
``sanitize_outbound`` and prefixed with a ``[sent by session … via session_send]``
envelope so it can never render as something the person typed. An IDLE target runs
it under the authorization that admitted it; a BUSY target queues it, and the
generic drain re-asserts the target-side containment before the entry becomes a
turn (issue #5911): producers stamp the constraints that held at admission
(:func:`containment_meta`), and ``chat_runner``'s drain drops — with a visible
notice and an SEL record — any entry for which a constraint holds at delivery
that did not hold at admission. A human-typed queued message shares the same
window and the same re-check.

Authorization is deny-by-default and checked in one place
(:func:`authorize_target`) for the two operations that take a target, so a guard
cannot be present on one verb and missing on another. ``session_create`` has no
target; it checks the caller's own eligibility with the same refusals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    default_project_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, SlotOrigin
from kiro_crew.history import metadata_now_iso, transcript_stem
from kiro_crew.security import redact, redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.validation import MAX_LONG_STRING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# Reads are cheap but not free — each one walks the target's in-memory window.
MAX_READ_MESSAGES = 100
DEFAULT_READ_MESSAGES = 20

# Per-message content cap for reads, so pulling a transcript tail cannot return
# a multi-megabyte tool payload verbatim.
MAX_READ_CONTENT_CHARS = 4000

# Slot-key prefixes for sessions no human is watching: a cron run's own slot
# (``cron-<job_id>``) and a background workflow's result slot
# (``workflow-<run_id>``, created by ``workflow_inject`` only when the
# originating tab is gone). They are refused as BOTH source and target. As a
# target, a message would start a fresh agent turn in a display-only slot nobody
# reads; as a source, a scheduled job would be able to type into the user's live
# conversations unattended. Notifications are the supported path for those
# (``send_message``), not session control.
UNATTENDED_SLOT_PREFIXES = ("cron-", "workflow-")

# Roles a read must not count, taken from the persistence layer's own list rather
# than restated here: those are exactly the rows rehydration DROPS, so any cursor
# that counted them would name a different position after a restart than before
# it. ``chunk`` runs are deleted when a segment flushes and ``done`` markers never
# persist at all, so counting either inflates ``total``, the list shrinks back
# under it, and the next ``since=next_since`` read skips the finished reply for good.
TRANSIENT_ROLES = _PERSISTENCE_TRANSIENT_ROLES


class SessionControlError(Exception):
    """A refusal carrying the HTTP status AND the machine-readable reason.

    ``code`` is the contract the dashboard and the MCP tools match on; ``message``
    is advisory English prose (RFC 9457 3.1.3). Prose alone would be
    untranslatable by construction, since callers render it verbatim.
    """

    def __init__(
        self, message: str, status: int = 400, code: str = "session_control_error"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def session_control_enabled() -> bool:
    """Whether the session-control surface is switched on in config.

    A config read that RAISES resolves to disabled, not to the field's default.
    ``load()`` can fail on a malformed section that has nothing to do with this
    feature, and treating that as "enabled" would let unrelated corruption
    silently undo an explicit ``session_control: false`` — the one switch
    standing between two of the user's sessions. Failing closed costs a
    refusal the user can diagnose from the log line; failing open costs the
    opt-out.
    """
    try:
        return bool(KiroCrewConfig.load().agent.session_control)
    except Exception:
        logger.warning(
            "session_control: config read failed — refusing until config loads", exc_info=True
        )
        return False


async def prewarm_enabled_check() -> None:
    """Warm the config cache in a thread so the sync gate reads the cached path.

    :func:`session_control_enabled` cannot await -- ``authorize_target`` is
    synchronous, and ``read_messages`` is synchronous with it -- but its
    ``KiroCrewConfig.load()`` re-reads and validates the file on the FIRST call
    after a config edit, and doing that inline blocks the loop for every other
    session.

    Must be called with NOTHING that suspends between it and the gate. An
    ``await`` in that gap reopens exactly the hole this closes: a config edit
    landing in the window changes the fingerprint, so the gate's own read misses
    the cache and does the synchronous read anyway. That is why this is not done
    once at the top of each handler -- reading a request body suspends, and so
    does the SEL prewarm inside ``stop_target``.

    Lives here rather than in the handlers so the three call sites share one
    implementation, and so ``stop_target`` can warm it after its own prewarm
    without the handler layer reaching back into it.

    Best-effort: a failure is the gate's business, and the gate fails closed on
    its own.
    """
    try:
        await asyncio.to_thread(session_control_enabled)
    except Exception:  # pragma: no cover - the gate re-reads and decides
        logger.debug("session-control config prewarm failed; the gate will read it inline")


def caller_slot_key(state: "DashboardState", session_key: str) -> str:
    """Map a caller's session key to its slot key, or ``""`` when unknown.

    The MCP process authenticates as a session key (the history key), while
    every operation here is slot-keyed. Resolution walks the live slots and
    matches on the key each slot actually writes, which is the same identity
    ``list_sessions`` reports — so "who am I" cannot disagree between the two.

    An unresolvable caller is not fatal: it only means the self-target guard has
    nothing to compare against, which :func:`authorize_target` treats as a
    refusal rather than a pass.
    """
    if not session_key:
        return ""
    for slot in list(state._slots.values()):
        try:
            history_key = slot_history_key(slot)
            if session_key in (history_key, slot.key, transcript_stem(history_key)):
                return slot.key
        except Exception:
            continue
    return ""


def _probe_channel_mirror(state: "DashboardState", slot: "_ChatSlot") -> str | None:
    """The identity of *slot*'s outbound channel mirror, ``""`` when the
    conversation is not mirrored, or ``None`` when the session store could not
    answer.

    The tri-state exists because the two consumers need OPPOSITE fail-closed
    treatments of an unreadable store, and a collapsed boolean forces one of
    them to lie: the refusal paths must treat unknown as mirrored (refuse rather
    than open the boundary), while the queue-drain notice must not claim "the
    session gained a mirror" for a state change that is merely unverifiable.

    The identity (channel type + channel + thread) rather than a bare boolean,
    because a mirror can be RETARGETED while a queue waits: rebinding session
    mirror A to channel B keeps the boolean true from admission to drain while
    substituting the audience — exactly the republication change the drain
    re-check exists to catch (#5911).

    Read on the EFFECTIVE session key, because that is the key the mirror is
    registered under -- the slot key would miss a mirror on a session whose turns
    run under a different identity.
    """
    sessions = getattr(state, "sessions", None)
    getter = getattr(sessions, "get_mirror_link", None)
    if getter is None:
        return ""
    try:
        link = getter(slot_history_key(slot))
    except Exception:
        logger.debug("mirror-link probe failed", exc_info=True)
        return None
    if not link:
        return ""
    return (
        f"{getattr(link, 'channel_type', '')}"
        f":{getattr(link, 'channel_id', '') or ''}"
        f":{getattr(link, 'thread_id', '') or ''}"
    )


def _has_channel_mirror(
    state: "DashboardState", slot: "_ChatSlot", *, on_probe_failure: bool = True
) -> bool:
    """Boolean view of :func:`_probe_channel_mirror` for the refusal paths.

    `linked_session_key` catches a channel-BORN slot. It does not catch a
    dashboard-born slot that was later given an OUTBOUND mirror link, which
    reaches a channel just as surely: the link lives in the session store, not
    on the slot, so a slot with an empty `linked_session_key` can still be
    republishing every turn to Slack or Telegram.

    Best-effort by design: a store that cannot answer returns *on_probe_failure*,
    and the default (``True``) keeps the refusal paths failing closed -- an
    unreadable link is treated as mirrored rather than opening the boundary.
    The enqueue-time containment snapshot passes ``False`` because ITS fail-closed
    direction is inverted: recording "not mirrored" for an unreadable link is the
    least-authorized admission state, so the drain-side re-check re-validates the
    entry instead of waving it through (see :func:`containment_snapshot`).
    """
    probed = _probe_channel_mirror(state, slot)
    return on_probe_failure if probed is None else bool(probed)


# ── Drain-time re-validation of queued prompts (issue #5911) ──
#
# Authorization is decided when a prompt is ADMITTED — `authorize_target` for
# `session_send`, the authenticated composer for a human — but a busy target
# QUEUES the prompt and delivers it later, and the containment those decisions
# rest on can change in between: a target authorized while unlinked can gain a
# channel or mirror link before its queue drains, and the queued prompt would
# then execute and republish to an audience its admission never contemplated.
# Producers stamp the constraints that held at admission on the queue entry
# (`containment_meta`); `chat_runner`'s drain recomputes them and drops any
# entry for which a constraint holds at delivery that did not hold at admission.

# Queue-entry meta key carrying the admission-time containment snapshot.
QUEUED_CONTAINMENT_META_KEY = "queued_containment"

# Transcript-notice phrasing per snapshot field, for the drop notice a reader
# of the session must be able to understand without knowing this module.
_CONTAINMENT_CHANGE_LABELS = {
    "linked": "the session was linked to a channel",
    "mirrored": "the session gained an outbound channel mirror",
    "mirror_retarget": "the session's outbound mirror was retargeted to a different channel",
    "crew": "the session was switched to crew mode",
    "ephemeral": "the session became incognito/temporary",
    "app": "the session became app-scoped",
    "unattended": "the session became unattended",
    "workspace": "the session moved to a different workspace",
}


# Snapshot keys that are NOT constraints: carried for notice wording and
# telemetry only, never compared by :func:`newly_held_constraints`.
_NON_CONSTRAINT_KEYS = frozenset({"mirror_unverified"})

# The one constraint a directive user-origin entry is exempt from at the drain:
# a channel LINK on the entry's own session. The author of a directive entry is
# an authenticated human typing into that session's own surface, and linking it
# is that surface owner's deliberate act — dropping their already-typed messages
# when they link would destroy user speech on a supported flow (`api_chat`
# applies no linked refusal to composer input). `mirrored` is deliberately NOT
# exempt: directive content can be authored by any allowed human in a linked
# thread while only the session owner adds outbound mirror links, so a NEW
# mirror widens the audience beyond anything the message's author controlled —
# the exact republication issue #5911 closes. `session_send` and automation
# entries never carry the flag and stay fully enforced.
_AUDIENCE_CONSTRAINTS = frozenset({"linked"})


def containment_snapshot(
    state: "DashboardState", slot: "_ChatSlot", *, on_probe_failure: bool
) -> dict[str, Any]:
    """The target-side containment constraints of :func:`authorize_target`, as
    they hold for *slot* right now.

    Two call sites with OPPOSITE fail-closed directions, hence the mandatory
    ``on_probe_failure``: the enqueue-time snapshot passes ``False`` so an
    unreadable mirror link records the least-authorized admission state (the
    drain then re-validates the entry), while the drain-time snapshot passes
    ``True`` so an unreadable link refuses delivery rather than opening the
    boundary. When the drain-side probe fails, ``mirror_unverified`` is set so
    the drop notice can say the state could not be verified instead of claiming
    a mirror appeared — the refusal is the same, the wording must not lie.
    Every other field is a plain slot attribute read that cannot fail.

    ``workspace`` is the seventh refusal (:func:`authorize_target`'s
    ``workspace_mismatch``), an identity rather than a boolean: a CHANGE — the
    slot moving to another workspace while the entry waited — invalidates the
    admission, because the prompt would run with memory, lessons and project
    context its admission never saw. It is compared only when the entry
    recorded one; the unmarked fail-closed baseline stays the boolean set,
    since there is no least-authorized workspace to assume.

    ``unattended`` keys on the slot-key prefix exactly as ``authorize_target``
    does. A slot key is immutable, so this field can never flip between enqueue
    and drain for a TAGGED entry — it is carried for the unmarked fail-closed
    path, where the baseline is all-False and any held constraint must count.
    """
    probed = _probe_channel_mirror(state, slot)
    snap: dict[str, Any] = {
        "linked": bool(getattr(slot, "linked_session_key", "")),
        "mirrored": on_probe_failure if probed is None else bool(probed),
        "crew": getattr(slot, "mode", "") == "crew",
        "ephemeral": getattr(slot, "memory_mode", "persistent") != "persistent",
        "app": bool(getattr(slot, "_app", "")),
        "unattended": str(getattr(slot, "key", "")).startswith(UNATTENDED_SLOT_PREFIXES),
        "workspace": str(getattr(slot, "workspace", "default") or "default"),
    }
    if probed is not None:
        # The mirror's identity, compared like ``workspace``: a RETARGETED
        # mirror (A -> B) keeps the boolean true across the wait while
        # substituting the audience, so identity is what the drain must compare.
        # Omitted on probe failure — there is no identity to compare then, and
        # the drain fails closed on the unverifiable boolean instead
        # (:func:`newly_held_constraints` treats ``mirror_unverified`` as a
        # mirror change regardless of the admission snapshot).
        snap["mirror_identity"] = probed
    if probed is None and on_probe_failure:
        snap["mirror_unverified"] = True
    return snap


def containment_meta(state: "DashboardState", slot: "_ChatSlot") -> dict[str, Any]:
    """Queue-entry ``meta`` recording the containment that held at admission.

    Every producer of a plain (user-speech) queue entry stamps this at enqueue;
    the drain compares it against the constraints holding at delivery and drops
    the entry when one is newly held (:func:`newly_held_constraints`). An entry
    without the stamp fails closed — it is checked against the full
    current-constraint set — so an untagged producer can never ride a queued
    prompt past a boundary the tagged paths respect.
    """
    return {QUEUED_CONTAINMENT_META_KEY: containment_snapshot(state, slot, on_probe_failure=False)}


def newly_held_constraints(
    now: dict[str, Any], entry_meta: Any, *, directive_user_origin: bool = False
) -> list[str]:
    """Containment constraints in *now* that the entry's admission never saw.

    *now* is the drain-time :func:`containment_snapshot`; *entry_meta* is the
    queue entry's ``meta`` (any shape — untrusted plumbing, so a missing or
    malformed snapshot degrades to the all-False baseline and the entry is
    checked against every currently-held boolean constraint, failing closed).

    A constraint recorded ``True`` at admission is not a change: the prompt was
    knowingly admitted under it (a human typing into a channel-born session, an
    app relaying into its own slot), and dropping it would refuse designed
    behaviour rather than close a window.

    ``workspace`` compares by identity and only when the entry recorded one —
    an unmarked entry has no least-authorized workspace to assume, so its
    fail-closed floor stays the boolean set. ``mirror_identity`` compares the
    same way: a mirror retargeted to a different channel while the entry waited
    is an audience substitution the boolean cannot see, reported as
    ``mirror_retarget``.

    *directive_user_origin* exempts the LINKED constraint only, for entries
    carrying the authenticated-human provenance flag: the author typed into the
    session's own surface and linking it is that owner's deliberate act, so
    dropping their already-typed messages when they link the session would
    destroy user speech on a supported flow (``api_chat`` applies no linked
    refusal to composer input). A NEW outbound mirror is never exempt — the
    message's author does not control mirror links, so it still drops. Every
    other constraint — crew, ephemeral, app, unattended, workspace — applies
    to directive entries too.
    """
    recorded: dict[str, Any] = {}
    if isinstance(entry_meta, dict):
        raw = entry_meta.get(QUEUED_CONTAINMENT_META_KEY)
        if isinstance(raw, dict):
            recorded = raw
    changed: list[str] = []
    for name, value in now.items():
        if name in _NON_CONSTRAINT_KEYS:
            continue
        if name == "workspace":
            admitted_ws = recorded.get("workspace")
            if isinstance(admitted_ws, str) and admitted_ws != value:
                changed.append(name)
            continue
        if name == "mirrored":
            # Fail closed on an unverifiable drain-side probe REGARDLESS of the
            # admission snapshot: an entry admitted under mirror A cannot be
            # delivered when the store no longer answers, because the audience
            # may have been retargeted since admission and there is no identity
            # to compare (the probe-failure snapshot omits ``mirror_identity``).
            # Matching ``authorize_target``'s posture — unreadable state refuses
            # rather than opens the boundary; the notice wording says the state
            # could not be verified (``mirror_unverified``), never that a mirror
            # appeared.
            if value and (now.get("mirror_unverified") or not bool(recorded.get(name, False))):
                changed.append(name)
            continue
        if name == "mirror_identity":
            # Identity comparison, like workspace: a mirror RETARGETED while the
            # entry waited (A -> B) keeps ``mirrored`` true at both ends while
            # substituting the audience, so the boolean can never see it. Fires
            # only when both sides carry a verified, non-empty identity — a
            # newly GAINED mirror is the boolean's job, and an unverifiable side
            # omits the key. Never exempt for directive entries: the message's
            # author does not control mirror links.
            admitted_id = recorded.get("mirror_identity")
            if value and isinstance(admitted_id, str) and admitted_id and admitted_id != value:
                changed.append("mirror_retarget")
            continue
        if directive_user_origin and name in _AUDIENCE_CONSTRAINTS:
            continue
        if value and not bool(recorded.get(name, False)):
            changed.append(name)
    return changed


def describe_containment_change(constraints: list[str], *, mirror_unverified: bool = False) -> str:
    """One transcript-ready phrase naming what changed, for the drop notice.

    *mirror_unverified* swaps the mirrored wording: when the drain-side probe
    failed, the refusal stands (fail closed) but the notice must describe an
    unverifiable state, not assert a mirror appeared.
    """
    labels = dict(_CONTAINMENT_CHANGE_LABELS)
    if mirror_unverified:
        labels["mirrored"] = "the session's channel-mirror state could not be verified"
    return "; ".join(labels.get(c, c) for c in constraints)


def audit_queued_drop(slot: "_ChatSlot", queue_id: str, constraints: list[str]) -> None:
    """Record one drain-time drop in the SEL, best-effort and off the loop.

    Logged as a denied tool invocation on the TARGET's EFFECTIVE session — a
    linked slot's turns run under ``linked_session_key``, so filing under the
    slot key would hide exactly the drops this feature exists to record. The
    slot key stays in ``resources``/``metadata``. The admission-time caller may
    be long gone, so there is no caller identity to attribute the drop to.
    """
    _audit_queue_drain(slot, outcome="denied", queue_ids=[queue_id], newly_held=constraints)


def audit_queued_allow(slot: "_ChatSlot", queue_ids: list[str]) -> None:
    """Record that re-validated queued entries were AUTHORIZED to become a turn.

    The allow side of the same permission decision :func:`audit_queued_drop`
    records the deny side of — both outcomes are auditable, matching
    ``authorize_target``'s convention of logging ``allowed`` operations and not
    only refusals. Emitted at CONSUMPTION (the moment the drain hands the
    entries to a turn), not per sweep pass, so an entry that waits across
    several drains produces one row when it actually executes rather than one
    per re-check. One row covers the whole consumed batch.
    """
    _audit_queue_drain(slot, outcome="allowed", queue_ids=queue_ids, newly_held=None)


def _audit_queue_drain(
    slot: "_ChatSlot", *, outcome: str, queue_ids: list[str], newly_held: list[str] | None
) -> None:
    slot_key = str(getattr(slot, "key", ""))
    session_key = effective_session_key(slot)
    metadata: dict[str, Any] = {
        "target": slot_key,
        "queue_ids": ",".join(queue_ids),
    }
    if newly_held is not None:
        metadata["newly_held"] = ",".join(newly_held)

    def _do() -> None:
        sel().log_tool_invocation(
            session_key=session_key,
            agent="",
            source="dashboard",
            tool_name="queue_drain_revalidation",
            tool_kind="command",
            outcome=outcome,
            resources=f"target={slot_key}",
            metadata=metadata,
        )

    _sel_off_loop(_do, "queue-drain revalidation audit")


def _refuse_ineligible_creator(state: "DashboardState", caller_slot: "_ChatSlot") -> None:
    """Refuse a caller that may not manufacture a session.

    A caller that may not CONTROL a peer may not manufacture one either --
    otherwise a channel-bound session creates a session and then drives it,
    reaching the same place the caller-side refusals exist to prevent. This set
    therefore mirrors `authorize_target`'s caller half exactly; a refusal present
    there and missing here is a hole.

    Extracted so it can be applied TWICE: once on entry, so an ineligible caller
    is refused before any work is done and with the refusal precedence a caller
    can rely on, and again immediately before the slot is allocated. Two of these
    answers are not stable -- `_has_channel_mirror` reads the live session store,
    and a dashboard-born session can be given an outbound mirror link at any
    moment -- so an eligibility decided before a suspension point says nothing
    about eligibility at the moment of allocation.
    """
    if getattr(caller_slot, "_app", ""):
        # An app-scoped session is confined to its own app's slots. Creating a
        # plain user-origin slot would put a persistent, sidebar-visible session
        # outside that confinement, owned by the app.
        raise SessionControlError(
            "app-scoped sessions cannot create sessions", code="app_scoped_caller"
        )
    if getattr(caller_slot, "memory_mode", "persistent") != "persistent":
        # An incognito/temporary caller is defined by leaving nothing behind.
        # A persistent child it owns would outlive it, carrying its work into
        # storage the caller was promised would not retain anything.
        raise SessionControlError(
            "incognito and temporary sessions cannot create sessions",
            code="ephemeral_caller",
        )
    if getattr(caller_slot, "linked_session_key", ""):
        raise SessionControlError(
            "channel-linked sessions cannot create sessions",
            code="linked_session_caller",
        )
    if _has_channel_mirror(state, caller_slot):
        raise SessionControlError(
            "sessions mirrored to a channel cannot create sessions",
            code="mirrored_caller",
        )


def _resolve_slot(state: "DashboardState", target: str) -> "_ChatSlot | None":
    """Find the live slot *target* names: by slot key, transcript stem, or title.

    All three forms are things a caller actually holds. ``list_sessions`` reports
    FILENAME STEMS (``dashboard_chat-7``), not slot keys (``chat-7``), and the
    tool description tells callers to pass what it returned — so matching only
    ``slot.key`` refused the documented happy path with ``target_not_found``.
    Title matching covers what the caller sees on screen; it is exact and
    case-insensitive.

    Every form is resolved before anything is returned, and a string that matches
    two DIFFERENT slots across forms is refused as ambiguous. Returning on the
    first key hit would silently prefer it over a title the caller was reading off
    the screen, and picking the wrong conversation is exactly the outcome this
    function must never produce — ``session_stop`` discards a live turn's work.
    The doctrine is already the module's own for title-vs-title collisions; it
    applies no less when the collision crosses forms.
    """
    found: list[_ChatSlot] = []

    def _add(candidate: "_ChatSlot") -> None:
        if not any(c is candidate for c in found):
            found.append(candidate)

    slot = state.get_slot(target)
    if slot is not None:
        _add(slot)
    for candidate in list(state._slots.values()):
        try:
            if transcript_stem(slot_history_key(candidate)) == target:
                _add(candidate)
        except Exception:
            continue
    wanted = target.strip().casefold()
    if wanted:
        for candidate in list(state._slots.values()):
            if (candidate.display_title or "").strip().casefold() == wanted:
                _add(candidate)

    if len(found) > 1:
        raise SessionControlError(
            f"{len(found)} sessions match {target!r} (as a session key, transcript "
            "name, or title) — address it by its session key instead",
            status=409,
            code="ambiguous_target",
        )
    return found[0] if found else None


async def create_session(
    state: "DashboardState",
    *,
    caller_session_key: str,
    title: str = "",
    agent: str = "",
) -> dict[str, Any]:
    """Open a new session in the caller's workspace, persisted at birth.

    The new slot is an ordinary dashboard session -- it appears in the sidebar, the
    user can read it, type into it and close it -- so this gives a workstream a home
    of its own rather than a private channel the user cannot see. It starts empty:
    the person is the one who types the first message into it.

    The caller's own eligibility is checked against the SAME caller-side refusal
    set `authorize_target` applies (`authorize_target` cannot be reused here:
    there is no target yet), and the child inherits the caller's workspace. Both
    matter because a caller refusal missing here, or a workspace not inherited,
    would hand back a session outside the boundary the other verbs enforce.
    """
    if not session_control_enabled():
        raise SessionControlError(
            "session control is disabled in config (agent.session_control)",
            code="session_control_disabled",
        )
    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        raise SessionControlError(
            "caller session could not be identified", code="caller_unidentified"
        )
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES):
        raise SessionControlError(
            "unattended sessions (scheduled runs) cannot create sessions",
            code="unattended_caller",
        )
    caller_slot = state.get_slot(caller_key)
    if caller_slot is None:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    _refuse_ineligible_creator(state, caller_slot)

    # The child is created in the CALLER'S workspace, not the default one.
    # Workspace is the memory boundary and `authorize_target` refuses a
    # cross-workspace target, so a child left in "default" would be a boundary
    # crossing its own creator could not then read or stop.
    workspace = getattr(caller_slot, "workspace", "default") or "default"
    # An unnamed agent inherits the CALLER'S, not the global default: the caller is
    # already running in this workspace, so its agent is the one bound here, and
    # falling to the global default would put the child on another workspace's
    # memory store the moment the default is bound elsewhere. It also matches what
    # creating a session to hand work to means -- the same kind of session.
    # Sanitized like `title` below, and for the same reason: this value arrives
    # from the calling model, is persisted verbatim to the metadata line, and is
    # pushed to every dashboard client. The schema caps its LENGTH; sanitizing is
    # what keeps a credential-shaped string out of storage and out of the sidebar.
    # An inherited caller agent is already internal, but running both through the
    # same call keeps the guard on the field rather than on one of its sources.
    agent_name = sanitize_outbound(agent.strip() or (getattr(caller_slot, "agent", "") or ""))

    log = state.conversation_log
    if log is None:
        # No durable store means the session cannot be persisted at birth, so it
        # would vanish on the next restart. Refusing is the honest answer;
        # returning a key would hand back a session that is dead on arrival.
        raise SessionControlError(
            "session history is unavailable, so the session cannot be persisted",
            code="history_unavailable",
        )

    # Resolved BEFORE the slot exists, because `get_or_create_slot` publishes into
    # the slot table and `await` is a suspension point: a slot that is visible
    # while its agent and project are still unset can be addressed in that window,
    # and `/api/chat` would then resolve bindings from a blank agent -- running the
    # turn against the DEFAULT workspace's memory store rather than this one.
    # `default_project_dir` needs only the workspace name, so nothing forces it to
    # run after construction.
    #
    # Offloaded: it resolves a realpath, stats the directory and screens it against
    # the sensitive-path list, so it is filesystem work the loop should not wait on.
    # The rule's own tiebreaker applies -- a leaked worker thread is survivable, a
    # frozen loop is not.
    project_dir = await asyncio.to_thread(default_project_dir, workspace)

    # ONE invariant covers every branch of agent resolution: the agent that will
    # actually ANSWER must be bound to the caller's workspace. Authorization reads
    # `slot.workspace` while execution follows the agent's own binding, so any
    # branch where those disagree carries another workspace's memory store into
    # the child. Enumerating the branches instead of stating the invariant is how
    # the empty-agent case was missed:
    #
    #   agent given, binding matches   -> allowed, dispatches that agent
    #   agent given, binding differs   -> refused (agent_workspace_mismatch)
    #   agent given, name unresolvable -> refused (agent_unresolved), because the
    #                                     default would answer under the requested
    #                                     name
    #   agent omitted                  -> `resolve_agent_bindings` falls to
    #                                     config.default_agent, so the SAME check
    #                                     applies to whatever would answer; an
    #                                     omitted agent is not an unchecked one
    #   config unreadable              -> refused (agent_unverifiable), because
    #                                     "cannot verify" must not read as "fine"
    #
    # Resolved with the child's own `project_dir`: a materialized kiro agent is
    # declared per project directory rather than registered in `config.agents`, so
    # resolving without it reports an app's agent as unresolvable and would refuse
    # a name that does resolve for the session being created.
    try:
        # Offloaded: a cache miss reads and validates the config file, so leaving it
        # on the loop stalls every other gateway task, not just this request. It is
        # awaited HERE, still ahead of the caller re-resolve below, so the decisions
        # that authorize the allocation are all made after the last suspension.
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        raise SessionControlError(
            "cannot verify the effective agent's workspace binding",
            code="agent_unverifiable",
        ) from None
    bindings = resolve_agent_bindings(cfg, agent_name, project_dir)
    agent_workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
    if agent_workspace != workspace:
        who = repr(agent_name) if agent_name else "the default agent"
        raise SessionControlError(
            f"{who} is bound to workspace {agent_workspace!r}, not the caller's " f"{workspace!r}",
            code="agent_workspace_mismatch",
        )
    if not bindings.requested_resolved:
        # The workspace check above passed for whatever ANSWERS -- the default
        # agent -- so no memory boundary is crossed. What would be wrong is the
        # record: `slot.agent` stores this name, and `ResolvedBindings` states the
        # contract for exactly this caller class, that a caller storing the
        # requested name must not advertise it when the request was not honored.
        # A session that names one agent while another answers misleads every later
        # reader of the sidebar and of `list_sessions`.
        #
        # Refused rather than silently rewritten to the effective agent, because
        # the caller asked for a specific one and nothing is lost by refusing: no
        # session exists yet, and a corrected name is one retry away. (An existing
        # slot is the opposite case -- there the stored name is the user's own
        # intent and is kept verbatim, since a momentarily stale resolution must
        # not permanently rebind it.)
        raise SessionControlError(
            f"{agent_name!r} does not resolve to a configured agent",
            code="agent_unresolved",
        )

    # SlotOrigin.USER, not SYSTEM: the visibility semantics must match an
    # ordinary session, because the point of creating it here is that the user
    # can see and take over the work. SYSTEM-origin slots fall outside the
    # `slots:user` WS scope, which would hide it from the sidebar.

    # Re-resolved and re-gated HERE, adjacent to the allocation, because every
    # decision above was made before this coroutine suspended -- twice, for the
    # project directory and the config load -- and the inputs to those decisions are
    # live state that can flip inside either window.
    #
    # Re-reading the slot TABLE is the part that matters most: closing the caller's
    # tab removes its slot, and a Python reference to the removed object stays
    # perfectly usable, so re-running the gate on the object resolved earlier would
    # authorize against a caller whose authority has already ended. Identity is
    # compared rather than mere presence, because the key can be re-minted onto a
    # different session inside the same window. `_has_channel_mirror` reads the
    # session store, so an outbound mirror link registered while this waited would
    # otherwise leave a now-channel-backed caller publishing a persistent session
    # outside its containment; `live_slot_count` reads the slot table, so two
    # concurrent creations could each pass the ceiling and then both land over it.
    #
    # Nothing suspends between this point and the fully-configured slot below, so
    # the gate and the act it authorizes stay adjacent -- the same discipline
    # `stop_target` keeps by prewarming its SEL logger ABOVE its gate rather than
    # between gate and act.
    live_caller = state.get_slot(caller_key)
    if live_caller is None or live_caller is not caller_slot:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    # A slot that survived but MOVED workspaces has invalidated both decisions that
    # read it: the memory boundary the child inherits, and the agent-binding check
    # above, whose whole question was whether the answering agent is bound to THIS
    # workspace. Re-running that check here is not an option -- it needs
    # `KiroCrewConfig.load()`, which is filesystem work that must not run on the
    # event loop -- so a moved caller is refused instead of re-authorized.
    if (getattr(live_caller, "workspace", "default") or "default") != workspace:
        raise SessionControlError(
            "caller session changed workspace while the session was being created",
            code="caller_workspace_changed",
        )
    _refuse_ineligible_creator(state, live_caller)
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise SessionControlError(
            f"slot cap reached ({MAX_LIVE_SLOTS})",
            code="slot_cap_reached",
            status=429,
        )

    # The agent rides in the constructor rather than being assigned afterwards, for
    # the same reason: it decides which workspace actually EXECUTES the turn, so it
    # must never be observable as empty. Everything after this point is synchronous
    # until the slot is fully configured.
    slot = state.get_or_create_slot(
        None, agent=agent_name, workspace=workspace, origin=SlotOrigin.USER
    )
    # cwd must follow the workspace too, or file search and project-scoped agents
    # resolve against a directory the slot does not claim -- the same
    # authorization-vs-execution split as the agent binding, one layer down.
    if not slot.project:
        slot.project = project_dir
    if title.strip():
        slot.title = sanitize_outbound(title.strip())[:200]
        slot._titled = True
    # Persist at birth. `save_slot_off_loop` cannot do this: the save it wraps
    # returns early on an empty message window -- before its `force` check -- so a
    # freshly created session, which has no messages by definition, would write
    # nothing at all. The tool would then hand back a session that does not survive
    # a restart.
    #
    # Awaited, and a failure RETRACTS the slot rather than merely propagating: an
    # unpersisted slot stays in the table, usable in memory and addressable by its
    # creator, then vanishes on restart. Reporting the failure while leaving that
    # behind is the worse of the two outcomes, because the caller sees an error and
    # the session exists anyway. Same retraction the fork path uses on a failed
    # build.
    try:
        await asyncio.to_thread(
            log.update_metadata,
            slot_history_key(slot),
            {
                "_type": "metadata",
                # The slot's OWN durable identity, and its origin, both of which
                # the normal save path writes -- but a slot created here may never
                # reach that path: `_save_slot_to_history` returns early on an
                # empty message window, so for a session that is created and then
                # sits idle THIS dict is the only record on disk. Omitting `origin`
                # is silently destructive on the next restart: rehydrate falls back
                # to the fail-closed empty sentinel, so a session opened as USER
                # comes back unattributed and `slots:user` subscribers stop seeing
                # it. Checked field-by-field against the save path; these are the
                # only fields a slot carries at birth that it does not already
                # write.
                "tab_id": slot._tab_id,
                "origin": slot._origin,
                "created_at": metadata_now_iso(),
                "workspace": slot.workspace,
                "agent": slot.agent or "",
                "project": slot.project or "",
                "title": slot.title or "",
                "memory_mode": getattr(slot, "memory_mode", "persistent"),
            },
        )
    except Exception:
        # Retract, but never at the cost of work already in flight. The slot is
        # addressable from the moment `get_or_create_slot` publishes it, which is
        # before this await, so a turn can have started on it while the write was
        # in the worker thread. Popping the slot then would leave that turn running
        # with nothing pointing at it -- unreachable, unstoppable, and invisible to
        # the stop verb. A phantom session that vanishes on the next restart is the
        # lesser harm, so liveness wins over tidiness and the slot stays.
        if not slot.running and not slot.messages:
            state._slots.pop(slot.key, None)
        state.push_slots_update()
        raise
    state.push_slots_update()
    _audit(
        caller_session_key=caller_key,
        operation="create",
        slot_key=slot.key,
        outcome="allowed",
        detail={"agent": slot.agent or ""},
    )
    return {"ok": True, "target": slot.key, "title": slot.title or slot.key}


def authorize_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    operation: str,
) -> "_ChatSlot":
    """Resolve *target* and decide whether *caller* may act on it.

    Deny-by-default: every refusal raises :class:`SessionControlError` and is
    recorded in the SEL, so an attempt to reach a session that is out of bounds
    is visible after the fact even though nothing happened.
    """

    def deny(reason: str, code: str, status: int = 403) -> SessionControlError:
        # Off the loop for the same reason `_audit` is: this can be the process's
        # FIRST `sel()`, which constructs the log. A denial is the likeliest
        # first-ever session-control call on a fresh gateway -- the feature refuses
        # before it ever allows -- so this path is not the rare one.
        #
        # Redacted BEFORE the write, because the audit sink is durable and served
        # back: `sel.py` documents that on-disk records are not redacted by the
        # writer, and `/api/sel/events` returns `recent()` rows verbatim to the
        # dashboard. `target` is raw MCP input, and `target_not_found` interpolates
        # it into `reason`, so both carry caller text. Redacting at this chokepoint
        # rather than at the one interpolating call site keeps a future `deny`
        # caller from reopening it. `redact` is what `sel._forward_event` already
        # applies to events on the forward path; this closes the same gap on the
        # path the dashboard reads.
        #
        # Only the audit copy is redacted: the returned message goes to the caller
        # that supplied the string, so it keeps naming the target it was given.
        _audit_target = redact(target)
        _audit_reason = redact(reason)
        _sel_off_loop(
            lambda: sel().log_api_access(
                caller=f"session:{caller_session_key or 'unknown'}",
                operation=f"session_control.{operation}",
                outcome="denied",
                source="mcp",
                resources=f"target={_audit_target}:{code}",
                error=_audit_reason,
            ),
            "session-control denial audit",
        )
        return SessionControlError(reason, status=status, code=code)

    if not session_control_enabled():
        raise deny(
            "session control is disabled in config (agent.session_control)",
            "session_control_disabled",
        )

    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        # Without a resolved caller the self-target guard is blind, and a session
        # that can reach every peer while being unidentifiable is exactly the
        # shape this surface must not have.
        raise deny("caller session could not be identified", "caller_unidentified")
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES):
        raise deny(
            "unattended sessions (scheduled runs) cannot control other sessions",
            "unattended_caller",
        )

    try:
        slot = _resolve_slot(state, target)
    except SessionControlError as exc:
        raise deny(exc.message, exc.code, status=exc.status) from exc
    if slot is None:
        # 404 rather than 403: naming a session that is not open is a mistake,
        # not an authorization failure. Only sessions the dashboard currently
        # holds are addressable — a closed tab is out of scope, because waking
        # one would resurrect a conversation the user put away.
        raise deny(f"no open session matches {target!r}", "target_not_found", status=404)

    if slot.key == caller_key:
        raise deny("a session cannot control itself", "self_target")
    if slot.key.startswith(UNATTENDED_SLOT_PREFIXES):
        raise deny("unattended sessions (scheduled runs) cannot be controlled", "unattended_target")
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        raise deny("incognito and temporary sessions are not addressable", "ephemeral_target")
    if getattr(slot, "_app", ""):
        raise deny("app-scoped sessions are not addressable", "app_scoped_target")
    if getattr(slot, "linked_session_key", ""):
        # A channel-linked session's conversation is mirrored to Slack/Telegram,
        # so reaching it crosses a surface boundary in both directions: a message
        # would surface to whoever reads that thread, and a read would pull the
        # channel's content back. That alone is reason enough to keep it out.
        #
        # It is also the one target whose STOP cannot be honoured: the stop path
        # addresses the session as ``dashboard:<slot>`` while a linked slot's turns
        # actually run under its ``linked_session_key``, so the cancel would miss
        # and the target would keep executing after a reported success. Refusing
        # is the honest answer until the stop path resolves the effective key.
        raise deny("channel-linked sessions are not addressable", "linked_session_target")
    if _has_channel_mirror(state, slot):
        # Same boundary, reached by the other mechanism: an outbound mirror
        # republishes this session's turns to a channel, so a read would pull
        # that channel's content back and a stop would act on a conversation
        # other people are party to.
        raise deny("sessions mirrored to a channel are not addressable", "mirrored_target")
    if getattr(slot, "mode", "") == "crew":
        # A crew session's ingress is NOT a turn. `/api/chat` routes it to
        # `state.crew.ingest`, which makes the message a durable queue entry and
        # fans it out to topic sub-sessions; the orchestrator acks instantly and
        # the message is only shown once the entry is durable. Delivering here as
        # a turn instead would run generic work that is neither queued nor routed
        # -- accepted, apparently fine, and silently outside the mode.
        #
        # Refused rather than emulated, for the same reason a channel-linked
        # target is: a target whose turn lifecycle differs needs its own
        # handling rather than a second, drifting copy of the orchestrator's
        # rules.
        raise deny("crew-mode sessions are not addressable", "crew_mode_target")

    # The caller's own isolation gates it too, and for the same reasons the
    # target's does: an incognito or temporary session is one the user asked to
    # leave no trace, and an app-scoped session belongs to its app. Either one
    # reaching a persistent peer would launder content across the boundary it
    # was created to have — in the direction the target-side checks cannot see.
    caller_slot = state.get_slot(caller_key)
    if caller_slot is None:
        raise deny("caller session is no longer open", "caller_gone")
    if getattr(caller_slot, "_app", ""):
        raise deny("app-scoped sessions cannot control other sessions", "app_scoped_caller")
    if getattr(caller_slot, "memory_mode", "persistent") != "persistent":
        raise deny(
            "incognito and temporary sessions cannot control other sessions",
            "ephemeral_caller",
        )
    if getattr(caller_slot, "linked_session_key", ""):
        # The exfiltration direction, and the reason this is not merely the
        # mirror of the target-side check: a linked caller's own conversation is
        # a channel thread, so anything it reads lands in front of whoever is in
        # that channel. `session_read_message` would hand a private dashboard
        # transcript to Slack/Discord readers who were never party to it.
        #
        # `CHANNEL_AGENT_BLOCKED_TOOLS` already blocks these tools for channel
        # AGENTS, but that guard keys on the agent identity; a linked SLOT is a
        # second route to the same surface and has to be closed on its own.
        raise deny(
            "channel-linked sessions cannot control other sessions",
            "linked_session_caller",
        )
    if _has_channel_mirror(state, caller_slot):
        # The exfiltration direction again, via the outbound mechanism: a mirrored
        # caller republishes its own turns to a channel, so a peer's transcript it
        # reads lands in front of that channel's audience.
        raise deny(
            "sessions mirrored to a channel cannot control other sessions",
            "mirrored_caller",
        )

    if getattr(slot, "workspace", "default") != getattr(caller_slot, "workspace", "default"):
        # Workspaces are the memory boundary; reaching across one would let a
        # session act on work it cannot see.
        raise deny("target session belongs to a different workspace", "workspace_mismatch")

    return slot


def _audit(
    *,
    caller_session_key: str,
    operation: str,
    slot_key: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one completed session-control operation in the SEL.

    Logged as a tool invocation rather than an API access because that is what
    it is from the caller's side, and because it carries the per-call detail
    (how the message landed, whether a stop escalated) that makes the audit
    line answer "what actually happened to the other session".

    Dispatched OFF the loop when one is running, mirroring
    ``update_metadata_off_loop``. ``log_tool_invocation`` only enqueues, but the
    FIRST ``sel()`` of a process CONSTRUCTS the log -- trust-dir creation, key
    validation, and on Windows an ``icacls`` subprocess -- and this can genuinely
    be that first call: ``sel_audit_middleware`` logs AFTER ``await handler(...)``,
    so on a fresh gateway the first authenticated request constructs the log
    inside whatever handler runs first. Offloading here covers every call site
    without adding a step to the boot path -- which the boot-path rule forbids and
    a background prewarm would only race rather than close.
    """

    def _do() -> None:
        sel().log_tool_invocation(
            session_key=caller_session_key,
            agent="",
            source="mcp",
            tool_name=f"session_{operation}",
            tool_kind="command",
            outcome=outcome,
            resources=f"target={slot_key}",
            metadata=dict(detail or {}, target=slot_key),
        )

    _sel_off_loop(_do, "session-control audit")


def _sel_off_loop(write: "Callable[[], None]", what: str) -> None:
    """Run one SEL write off the event loop, best-effort.

    Shared by every session-control SEL write so the property holds in one place
    instead of per call site -- the denial audit was the THIRD site of this class
    to be found separately, having been missed while the other two were fixed.

    Two failure modes, both handled: a loop-blocking construct (a ``sel()`` that
    creates the trust dir, validates keys, and on Windows shells out to
    ``icacls``), and a construct that RAISES, which unguarded turns a 403 into a
    500 -- losing the refusal in order to report it. An audit that cannot be
    written must never change what the caller is told.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            write()
        except Exception:  # noqa: BLE001 - an audit failure must not fail the op
            logger.warning("%s failed inline", what, exc_info=True)
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning("%s failed off-loop: %r", what, exc)

    loop.run_in_executor(None, write).add_done_callback(_report)


async def stop_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
) -> dict[str, Any]:
    """Stop *target*'s in-flight turn, via the same path as the Stop button.

    A first call cancels cooperatively; calling again while that is pending
    escalates to a hard kill. The escalation is decided by the target's own stop
    state, not by anything the caller can ask for -- which is why there is no
    force flag: `stop_slot_turn` escalates on a second press regardless of one,
    so advertising it would promise a hard kill a first call cannot deliver.
    """
    # Prewarmed BEFORE `authorize_target`, and that ordering is load-bearing.
    # `stop_slot_turn`'s IDLE branch logs to the SEL with no await before it, so on
    # a fresh gateway a first `session_stop` against an idle slot would CONSTRUCT
    # the log on the loop -- trust-dir creation, key validation, and on Windows an
    # `icacls` subprocess. Constructing it off-loop first makes that call a cheap
    # cache hit. Per-request, not a boot step: prewarming at startup is what
    # `no-new-work-on-gateway-boot-path` forbids, and a background task would only
    # narrow the race rather than close it.
    #
    # It must sit ABOVE the gate because `await` is a suspension point: between
    # `authorize_target` and `stop_slot_turn` the loop must not yield, or a user
    # action landing in that window (linking the target to a channel) makes the
    # decision stale and the `mirrored_target` refusal is bypassed -- the turn gets
    # cancelled on a session that became channel-backed after the check passed.
    # Nothing may suspend between this gate and the act it authorizes.
    #
    # Best-effort on purpose: construction can raise (a trust root too short to
    # sign the chain), and this is a latency guard, not an authorization one --
    # failing it must not turn a stop into a 500.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the stop
        logger.warning("session-control SEL prewarm failed", exc_info=True)

    # The config warm goes HERE, not in the handler: the SEL prewarm above is an
    # `await`, and so is reading the request body, so a warm done before either of
    # them can be invalidated by a config edit landing in the gap -- leaving
    # `authorize_target`'s synchronous `session_control_enabled` to re-read and
    # validate the file on the loop, which is the whole thing the warm exists to
    # avoid. This is the last suspension before the gate.
    await prewarm_enabled_check()

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="stop",
    )
    # Deferred: ``chat_handlers`` imports ``dashboard.chat`` transitively, which
    # reaches back into the gateway at import time — a module-scope import here
    # closes that cycle through ``handlers.session_control`` -> ``server``.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    result = await stop_slot_turn(state, slot, source="session_control")
    _audit(
        caller_session_key=caller_session_key,
        operation="stop",
        slot_key=slot.key,
        outcome="allowed",
        detail={"result": result.get("info", "stopping")},
    )
    return {"ok": True, "target": slot.key, **result}


#: Cap on one delivered message. Aliased to ``validation.MAX_LONG_STRING`` rather
#: than restated as its own number: a seed prompt is that shape, the MCP schema
#: layer already rejects on that constant, and two spellings of one 50k limit
#: would drift apart the first time either moved.
MAX_SEND_MESSAGE_CHARS = MAX_LONG_STRING

#: Provenance prefix on every delivered message. The target's transcript renders
#: the message as a user row, and without this line it is indistinguishable from
#: something the person typed — the same reason auto-nudge tags its injected
#: turns ``[auto-nudge cycle N]``. The model in the target session sees it too,
#: so it can weigh the instruction as coming from a peer session, not its user.
_SEND_PROVENANCE = "[sent by session {caller} via session_send]\n\n"


async def send_to_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    message: str,
) -> dict[str, Any]:
    """Deliver *message* to *target* as its next agent turn.

    The delivery path is the same queue-vs-run decision the dashboard composer
    uses (``enqueue_or_run_prompt``): an idle target starts a turn immediately,
    a busy one queues the message for its next turn. Both outcomes are reported
    distinctly — ``started`` says which happened — because "it ran" and "it will
    run later" must not look the same to a caller coordinating several sessions.
    A queued delivery is re-validated at the drain (issue #5911): the entry
    carries the containment that held here, and a constraint newly held at
    delivery time drops it with a visible notice instead of executing it under
    the weaker authorization that admitted it.

    The turn is NOT charged against the background-turn cap, and deliberately so:
    that cap only binds unattended (app-owned) slots, and every target this
    function can authorize is attended — see the comment at the delivery call.
    """
    # Same prewarm ordering as `stop_target`, for the same reasons: the SEL
    # write inside `authorize_target`'s deny path must be a cache hit, and the
    # config warm must be the LAST suspension before the synchronous gate.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the send
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    body = message.strip()
    if not body:
        raise SessionControlError("message is empty", code="message_empty", status=400)
    if len(body) > MAX_SEND_MESSAGE_CHARS:
        raise SessionControlError(
            f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
            code="message_too_long",
            status=400,
        )

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="send",
    )

    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_runner import _run_chat

    caller_key = caller_slot_key(state, caller_session_key)
    # Sanitized on the same grounds as the steer path (``chat_delivery`` sanitizes
    # before ``slot.append``): this body comes from ANOTHER session and is persisted
    # into — and broadcast from — the target's transcript, so raw content must never
    # reach that surface. The length gate above deliberately measures the RAW body:
    # redaction can only shrink the text, so validating the raw form is the honest
    # limit and keeps the error keyed to what the caller actually sent.
    prompt = _SEND_PROVENANCE.format(caller=caller_key or "unknown") + sanitize_outbound(body)

    # `_run_chat` is passed straight through, NOT wrapped in
    # `state.run_background_turn`: that cap is structurally unreachable here.
    # `run_background_turn` returns the coroutine untouched for an attended slot
    # (`state.py`, "this wrapper is inert"), `_ChatSlot.unattended` is
    # `bool(self._app) and not self._human_seen`, and `authorize_target` refuses
    # every `_app` target above (`app_scoped_target`) — so no target this
    # function can reach is ever unattended, and a wrapper would only add a
    # never-taken timeout arm. The composer's own queued path does the same
    # (`server.py` passes `_run_chat` directly).
    started = bool(slot.enqueue_or_run_prompt(prompt, _run_chat, state))
    try:
        state.push_slots_update()
    except Exception:  # pragma: no cover - sidebar refresh is best-effort
        logger.debug("session_send: push_slots_update failed", exc_info=True)

    _audit(
        caller_session_key=caller_session_key,
        operation="send",
        slot_key=slot.key,
        outcome="allowed",
        detail={"started": started, "chars": len(body)},
    )
    return {"ok": True, "target": slot.key, "started": started}


def read_messages(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    limit: int = DEFAULT_READ_MESSAGES,
    since: int | None = None,
) -> dict[str, Any]:
    """Read *target*'s transcript tail plus enough state to poll it.

    ``next_since`` is the cursor to poll with; passing it back as ``since`` on the
    next call returns only what arrived in between, which is the whole
    wait → read poll loop. ``running`` says whether the target is still
    working, so a caller knows the difference between "nothing new yet" and
    "finished and idle".
    """
    if limit < 1 or limit > MAX_READ_MESSAGES:
        raise SessionControlError(
            f"limit must be between 1 and {MAX_READ_MESSAGES}", code="invalid_limit"
        )
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="read",
    )

    # Indexes are ABSOLUTE positions in the session, not offsets into the live
    # window. A slot keeps only the most recent ``_MAX_SLOT_MESSAGES`` in memory
    # and credits each trimmed row to ``_disk_older_count``, so window length
    # stops growing once trimming starts. A cursor derived from that length
    # would freeze at the cap and never see another reply; adding the
    # frozen-prefix count makes it monotonic for the session's whole life.
    raw_window = list(slot.messages)
    base = int(getattr(slot, "_disk_older_count", 0) or 0)
    # Stop the cursor before the streaming tail (see ``TRANSIENT_ROLES``): those
    # rows are deleted when the segment flushes, so a cursor past them would sit
    # beyond the list that replaces them and never return the finished reply.
    messages = [m for m in raw_window if m.get("role") not in TRANSIENT_ROLES]
    durable_end = len(messages)
    total = base + durable_end
    if since is not None:
        if since < 0:
            raise SessionControlError("since must be >= 0", code="invalid_since")
        if base:
            # ``_disk_older_count`` counts every trimmed row, transient ones
            # included (persistence writes them and only skips them when reading
            # back), while the positions above are built over DURABLE rows only.
            # The two agree until a transient row is trimmed into the frozen
            # prefix — then `base` advances with no durable row behind it, every
            # position shifts, and a `since` read serves a durable message the
            # caller already had.
            #
            # An exact cursor needs a durable-only prefix count, which does not
            # exist yet and cannot be added from here: ``_disk_older_count`` has a
            # contract with the save model (it is the frozen prefix saves must not
            # rewrite) and is read by backfill, rewind and channel slots. So this
            # refuses loudly instead of quietly duplicating. Tail reads (no
            # ``since``) still work, and the window is 10,000 rows, so only a very
            # long-lived session reaches this at all. Tracked for the real fix.
            raise SessionControlError(
                "this session is long enough that older messages have been "
                "trimmed, and cursor positions are no longer exact — read without "
                "`since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        # `base` is 0 from here on — the guard above refused every trimmed
        # session — so the absolute position and the window offset coincide.
        #
        # A cursor PAST the end is the remaining inexact case, and it is not the
        # same as a stale one: rewind and regenerate shrink a transcript, so
        # `total` can move backwards under a caller that is still holding the old
        # position. Clamping it to `total` would start the read at the end and
        # silently skip every replacement row written below the old cursor, with
        # nothing in the response saying so. That is the failure the trimmed-session
        # guard above refuses loudly rather than answer approximately, so this
        # refuses the same way. Reads without `since` are unaffected.
        if since > total:
            raise SessionControlError(
                "this session is shorter than your cursor — it was rewound or "
                "regenerated, so earlier positions no longer line up — read "
                "without `since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        start = since
        offset = start
    else:
        # A tail read is still served on a trimmed session (only `since` reads are
        # refused), so the two spaces come apart here: slice the in-memory window
        # by OFFSET, but report the index in ABSOLUTE terms so the number still
        # means "position in the session". Conflating them returned an empty
        # window, because `total` counts the frozen prefix the list does not hold.
        offset = max(0, durable_end - limit)
        start = base + offset
    window = messages[offset:][:limit]

    out: list[dict[str, Any]] = []
    for offset, msg in enumerate(window):
        content = str(msg.get("content", "") or "")
        # ``redact_and_truncate`` scans the COMPLETE text before slicing. Cutting
        # first would split a credential straddling the boundary into a prefix
        # that no longer matches the scanner, so the fragment would ship.
        emitted = redact_and_truncate(content, MAX_READ_CONTENT_CHARS)
        row: dict[str, Any] = {
            "index": start + offset,
            "role": str(msg.get("role", "") or ""),
            "content": emitted,
            "ts": str(msg.get("ts", "") or ""),
        }
        if len(content) > MAX_READ_CONTENT_CHARS:
            row["truncated"] = True
        out.append(row)

    _audit(
        caller_session_key=caller_session_key,
        operation="read",
        slot_key=slot.key,
        outcome="allowed",
        detail={"returned": len(out)},
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sanitize_outbound(slot.display_title),
        # Busy means "more output is coming", which is exactly what a poller needs
        # to decide whether to wait. `slot.running` alone is not that: during a
        # multi-stage plan each stage's `_run_chat` closes its own turn, so it
        # briefly reads False BETWEEN stages and a poller would conclude the work
        # had finished and stop before the later stages produced anything.
        "running": bool(slot.running or getattr(slot, "_in_stage_execution", False)),
        # True when the target is mid-reply: rows exist that the cursor
        # deliberately does not cover yet, so "nothing new" here does not mean
        # "nothing happening".
        **({"streaming": True} if durable_end < len(raw_window) else {}),
        "queue_depth": len(slot._queue),
        "total": total,
        # The cursor to poll with next. This is NOT `total`: when more than
        # `limit` rows are new, the window stops short of the end, and a caller
        # that polled `since=total` would jump the gap and never see the rows in
        # between. `next_since` is the absolute position just past the last row
        # actually returned, so consecutive polls cover every row exactly once.
        # `total` stays in the response as the backlog depth — the difference
        # from `next_since` is how far behind the caller still is.
        #
        # Omitted once rows have been trimmed, because positions stop being exact
        # there (see the `cursor_unavailable` refusal above). Handing back a
        # cursor that the next call would reject is worse than saying it is gone,
        # so its ABSENCE is the signal: a caller with no `next_since` falls back
        # to tail reads. No separate flag says the same thing -- two encodings of
        # one fact can disagree, and the reader already has to handle the absent
        # key.
        **({"next_since": start + len(out)} if not base else {}),
        "messages": out,
    }
