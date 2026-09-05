"""Session control: letting one chat session observe and interrupt another.

Four operations — create a session, stop its turn, close (archive) it, and read
its transcript — plus the authorization that decides whether a caller may address
a target at all. The operations are deliberately thin: they reuse the same
creation, stop, close and history paths the dashboard itself uses, so a controlled
session behaves exactly like one a human is typing into.

**One verb here writes into another session's conversation: ``session_send``.**
Reading returns a transcript tail; stopping cancels an in-flight turn the way the
Stop button does; closing archives the session the way the tab ✕ does (the
conversation is saved to history and can be reopened — closing is not deletion);
creating opens an empty session in the user's sidebar; sending
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
(:func:`authorize_target`) for the three operations that take a target — stop,
close and read — so a guard cannot be present on one verb and missing on another.
``session_create`` has no target; it checks the caller's own eligibility with the
same refusals.
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
from kiro_crew.dashboard.chat_folders import _unhide_folder
from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE, allow_create
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    SlotOrigin,
    _safe_folder_tree,
)
from kiro_crew.dashboard.stop_retry import allow_escalation
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

# A cron run's own slot (``cron-<job_id>``, minted by ``inject_cron_result_to_dashboard``).
CRON_SLOT_PREFIX = "cron-"

# A background workflow's result slot (``workflow-<run_id>``, minted by
# ``workflow_inject`` only when the originating tab is gone).
WORKFLOW_SLOT_PREFIX = "workflow-"

# Slot-key prefixes for sessions no human is watching. As a TARGET both are
# always refused: a message would start a fresh agent turn in a display-only slot
# nobody reads.
#
# As a SOURCE the two differ, and the difference is ownership. What must not
# happen is a scheduled job reaching the user's OWN conversations, which is a
# question about scope rather than about attendance -- an unattended job already
# starts turns in the session that owns it every time it delivers with
# ``send_message(session="origin")``. A cron slot can be held to that scope,
# because `authorize_target` fences it to slots carrying its own
# ``_created_by`` (see :func:`_cron_caller`), so it is admitted as a source. A
# workflow result slot cannot: it exists only when the originating tab is already
# gone, so there is no owning session to fence it to and nothing it would
# legitimately dispatch. It stays refused.
#
# Membership here is the fail direction for a prefix added later: a new
# unattended surface is refused as a source until it is given a fence of its own.
UNATTENDED_SLOT_PREFIXES = (CRON_SLOT_PREFIX, WORKFLOW_SLOT_PREFIX)

# The ``linked_session_key`` a cron slot carries (``cron:<job_id>``), written by
# ``inject_cron_result_to_dashboard`` to bind the run's transcript to the tab.
# It is NOT a channel link, and the caller-side linked refusals exist for channel
# links: they keep a session whose conversation is mirrored to Slack/Telegram from
# reading a peer's transcript into that thread. A cron link mirrors nothing and
# has no audience, so it is exempted where the caller is judged. The TARGET-side
# refusal is deliberately not exempted -- a cron tab is already unreachable as a
# target by prefix, and the exemption would otherwise widen to every linked slot.
CRON_LINK_PREFIX = "cron:"

# The ``created_by`` tag an app's own cron job carries (``app:<app_name>``,
# written by ``apps/cron_sdk.py``). Spelled here rather than imported: this
# module sits below the apps package, and the value is a persisted data format
# rather than something that package exports.
APP_CRON_OWNER_PREFIX = "app:"


def _member_caller(caller_key: str) -> bool:
    """Whether *caller_key* is a crew member's pinned DM slot.

    A member DM session dispatches its real work into worker sessions it
    creates and patrols — that is the member operating model, not an optional
    capability — so the surface authorizes it WITHOUT the global
    ``agent.session_control`` opt-in. What bounds it instead is ownership:
    :func:`authorize_target` restricts a member caller to slots it created
    itself, so the automatic grant never reaches the user's own sessions.

    Spelled through the members module's own prefix constant (imported
    lazily — members imports validation which sits below this module in the
    layering) rather than a restated literal, so the two cannot drift.
    """
    from kiro_crew.members import DM_SLOT_KEY_PREFIX

    return caller_key.startswith(DM_SLOT_KEY_PREFIX)


def _cron_caller(caller_key: str) -> bool:
    """Whether *caller_key* is a cron job's own slot.

    A cron caller is admitted to the surface DESPITE being unattended, and is
    bounded the same way a crew member is: :func:`authorize_target` refuses it on
    any slot it did not create itself, so its reach covers the sessions it
    dispatched and never the user's own conversations.

    It differs from a member caller in one way that matters. A member bypasses
    the global ``agent.session_control`` switch, because dispatching into workers
    is the member operating model rather than an opt-in. A cron does NOT: the
    switch is the user's statement that agents may open and drive sessions at
    all, and a job running while they are asleep is the last caller that should
    be exempt from it.

    Keyed on the slot-key prefix rather than on the slot's ``linked_session_key``
    or its ``SlotOrigin``, so the answer is available before the slot is resolved
    and cannot change under a caller: a slot key is immutable, while both of the
    others are fields a later write could alter.
    """
    return caller_key.startswith(CRON_SLOT_PREFIX)


def _caller_is_ownership_fenced(state: "DashboardState", caller_key: str) -> bool:
    """Whether *caller_key* may only reach slots it created itself.

    Three populations, one predicate, so the fence and the admissions that depend
    on it cannot drift apart:

    * a crew member's DM slot, which bypasses the config switch;
    * a cron job's own slot, which bypasses the unattended refusal;
    * **anything either of them created**, which is the part a key prefix cannot
      see. A created child is minted with a plain ``chat-`` key and INHERITS the
      creator's agent, so a fenced caller running a session-control agent would
      otherwise get an unfenced deputy for free: create a child, seed it, and the
      child -- an ordinary caller by key -- reads any same-workspace session and
      reports back through the transcript its creator is allowed to read. The
      fence has to follow authority, not spelling.

    ``_created_by`` is the marker for that third population and needs no lineage
    walk: :func:`create_session` is its ONLY writer (a person's own tab and a fork
    reach ``get_or_create_slot`` directly and stay unattributed), so a non-empty
    value means "an agent made this session" at any depth. A grandchild carries
    its parent's key there and is fenced by the same test, and a chain whose
    middle slot has been closed cannot fail open because no chain is walked.

    There is deliberately NO attendance exemption. ``_ChatSlot._human_seen`` looks
    like the right hatch and is not: it records that a human has EVER driven the
    slot, is monotonic and persisted, and says nothing about who authored the turn
    running now. Releasing the fence on it would hand the creator its deputy back
    for the price of the user glancing at the tab once -- cron creates the child,
    the user types into it, and from then on every cron-authored turn in that child
    runs unfenced. The question this predicate can answer is "whose authority is
    this session", not "is a person at the keyboard", so a person working in an
    agent-created session keeps that session's reach rather than their own.
    """
    if _member_caller(caller_key) or _cron_caller(caller_key):
        return True
    slot = state.get_slot(caller_key)
    if slot is None:
        return False
    return bool(getattr(slot, "_created_by", ""))


def _app_owned_cron_refusal(state: "DashboardState", caller_key: str) -> tuple[str, str] | None:
    """``(message, code)`` when *caller_key* is an APP's cron, else ``None``.

    An app-scoped SESSION is refused by the ``_app`` check that sits beside every
    call site of this one, but a cron tab does not carry that tag:
    ``inject_cron_result_to_dashboard`` mints it without ``app=``, so an app's own
    scheduled job reaches this surface with ``_app == ""`` and would pass. Left
    unchecked, an app could create a persistent, sidebar-visible session that is
    NOT app-scoped -- precisely the confinement escape the ``_app`` refusal exists
    to prevent, reached through the app's scheduled job instead of its session.

    App ownership therefore has to be read from the JOB, and it has TWO spellings
    there because two writers record it differently: the app cron SDK tags
    ``created_by = "app:{app_name}"``, while ``mcp_cron``'s own ``cron_add``
    records the calling session in ``session_key`` and never writes ``created_by``
    at all -- so an app-scoped session's job carries its authority only in the
    second. Both are checked, and the second delegates to ``_app`` on the owning
    slot rather than re-deriving app-ness, so there is one definition of "is this
    an app" and not a third. **A new field on the job that can name a principal is
    a hole here until it is added to this function.**

    Fail-CLOSED when ``session_key`` names a session that is no longer open: its
    ``_app`` cannot be read, and "could not verify the owner is not an app" must
    not read as "has no owner". ``mcp_cron``'s ``cron_add`` records an app's
    authority ONLY in ``session_key`` -- so once that slot is gone, allowing the
    job would let an app escape confinement through a cron it authored and then
    abandoned by closing its session. The cost is a genuinely user-created
    dispatching cron whose authoring tab has closed is refused too; that caller
    can reopen a tab, whereas an app session minted outside its confinement cannot
    be undone. This is the same fail-closed direction the missing-job case below
    takes, and what still bounds the app case beyond it is that a LIVE app session
    has its jobs refused directly by the ``_app`` read.

    Fail-CLOSED on a job that cannot be found, or a registry that cannot answer:
    "could not verify the owner" must not read as "has no owner", the same
    direction ``agent_unverifiable`` takes on its own unreadable input. Nothing
    legitimate is refused by it, because a cron whose job is gone is not running.

    Returns rather than raises so each call site keeps its own idiom -- the create
    path raises ``SessionControlError`` directly, while ``authorize_target`` must
    go through its ``deny`` closure to get the audit record and the 403.
    """
    if not _cron_caller(caller_key):
        return None
    job_id = caller_key[len(CRON_SLOT_PREFIX) :]
    found: Any = None
    try:
        for job in state.crons.list_jobs(include_disabled=True):
            if str(getattr(job, "id", "")) == job_id:
                found = job
                break
    except Exception:
        logger.warning(
            "session_control: cron owner lookup failed for %s -- refusing",
            caller_key,
            exc_info=True,
        )
        found = None
    if found is None:
        return (
            "the scheduled job behind this session could not be found, so its "
            "ownership cannot be verified",
            "cron_owner_unverifiable",
        )
    refusal = (
        "app-owned scheduled jobs cannot create or control sessions",
        "app_owned_cron_caller",
    )
    if str(getattr(found, "created_by", "") or "").startswith(APP_CRON_OWNER_PREFIX):
        return refusal
    owning_key = str(getattr(found, "session_key", "") or "")
    if owning_key:
        # Resolved through this module's own :func:`caller_slot_key` rather than a
        # ``removeprefix("dashboard:")``: chat_utils documents that the naive strip
        # is wrong for every non-dashboard session key, and the resolver already
        # matches on the identity each slot actually writes.
        owning_slot = state.get_slot(caller_slot_key(state, owning_key))
        if owning_slot is None:
            # The job names an owning session, but no live slot carries that key
            # any more -- the authoring tab was closed or evicted. Its ``_app``
            # tag lived only on that slot (``mcp_cron``'s ``cron_add`` records the
            # caller in ``session_key`` and never writes ``created_by``), so the
            # one place app-ness could be read is gone. That is precisely the
            # confinement escape: an app creates a cron through ``cron_add``,
            # closes its session, and its scheduled job then dispatches a
            # persistent, non-app, sidebar-visible session this gate can no longer
            # recognise as the app's.
            #
            # "Could not verify the owner is not an app" therefore fails CLOSED,
            # the same direction the missing-job and unreadable-registry cases
            # above take. This narrows the docstring's former "known residual":
            # the residual was an accepted fail-OPEN, and a fail-open on an
            # unresolvable owner is a security-gate defect (anchor
            # backend-security-controls). The cost is that a genuinely
            # user-created dispatching cron whose authoring tab has closed is
            # refused too -- but that caller can reopen a tab, whereas nothing can
            # undo an app session minted outside its confinement.
            return (
                "the session that authored this scheduled job is no longer open, "
                "so its ownership cannot be verified",
                "cron_owner_unverifiable",
            )
        if str(getattr(owning_slot, "_app", None) or ""):
            return refusal
    return None


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
    if (refusal := _app_owned_cron_refusal(state, getattr(caller_slot, "key", ""))) is not None:
        # The same confinement, reached through an app's cron rather than its
        # session -- a cron tab carries no ``_app`` tag for the check above to
        # read. See :func:`_app_owned_cron_refusal`.
        raise SessionControlError(refusal[0], code=refusal[1])
    if getattr(caller_slot, "memory_mode", "persistent") != "persistent":
        # An incognito/temporary caller is defined by leaving nothing behind.
        # A persistent child it owns would outlive it, carrying its work into
        # storage the caller was promised would not retain anything.
        raise SessionControlError(
            "incognito and temporary sessions cannot create sessions",
            code="ephemeral_caller",
        )
    caller_link = getattr(caller_slot, "linked_session_key", "")
    if caller_link and not caller_link.startswith(CRON_LINK_PREFIX):
        # A cron tab's link is its own run transcript, not a channel thread, and
        # is exempt -- see CRON_LINK_PREFIX. Everything else is a channel link.
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
    folder_id: str = "",
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

    The caller's session POSTURE (``_trust`` / ``_trust_reads``) transfers to the
    child, so a trusted operator's dispatched worker does not stall on a prompt
    nobody is watching -- the posture ``parent_trusted`` already gives a
    ``spawn_run`` subagent. Per-command grants (``_trusted_patterns``) and a
    ``SafetyOverride`` scoped grant (``_trust_scope``) are both deliberately
    excluded, and the transferred value is the one held at allocation time rather
    than at entry, so revoking mid call yields an untrusted child. See the block
    around the assignment.

    ``folder_id`` files the slot as part of creation (#6118): it is assigned in
    the same synchronous window that configures the slot, the whole
    allocation-to-persist span runs under ``suspend_slots_push`` so the slot's
    first broadcast frame already shows it filed, and the placement rides in the
    persist-at-birth metadata so it survives a restart. An unknown folder
    refuses the whole create -- nothing exists yet, so refusal loses nothing,
    matching the move path's posture -- and existence is confirmed READ-ONLY
    under the folder-store lock (``read_folders``) before the allocation; the
    Model-B un-hide runs only after the filing has landed, so a refused create
    leaves no folder-tree mutation behind. Authorization needs no new path: the
    folder tree cannot be reshaped from here (the id must already exist), and
    every caller class the move path's app-ownership rule exists to stop is
    already refused above it -- an app-scoped caller cannot create a session at
    all (`app_scoped_caller`).
    """
    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        raise SessionControlError(
            "caller session could not be identified", code="caller_unidentified"
        )
    # The caller is resolved BEFORE the config gate so a member DM session —
    # for which dispatching work into workers is the operating model, not an
    # opt-in — passes without `agent.session_control`. Every other caller
    # still needs the switch. The member's automatic grant is bounded by
    # ownership in `authorize_target`, not here: creation makes the caller
    # the owner by construction.
    if not session_control_enabled() and not _member_caller(caller_key):
        raise SessionControlError(
            "session control is disabled in config (agent.session_control)",
            code="session_control_disabled",
        )
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES) and not _cron_caller(caller_key):
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
    #
    # A CRON caller is the exception, and it is the one case where USER would be
    # wrong rather than merely coarse. `inject_cron_result_to_dashboard` tags a
    # cron's own slot CRON precisely so its output stays out of `slots:user` ("a
    # USER label would expose it to any app holding `slots:user`"), and the trust
    # model states the same rule from the other side: inferring USER for a
    # background caller "put cron output inside `slots:user`". Minting a
    # USER-labelled child would hand a cron the exposure its own slot is denied,
    # by the simple route of creating a session and writing there instead.
    #
    # The tag therefore follows the caller's AUTHORITY, not its key prefix, and
    # for the same reason the ownership fence does: a created child INHERITS its
    # creator's agent, so a cron's child can itself call this verb, and a
    # prefix-only test mints that grandchild USER (its caller key is a plain
    # `chat-`) -- the two-hop version of the very route this comment says must be
    # denied. Reading the caller slot's own ``_origin`` closes it transitively: the
    # child carries CRON, so ITS children do too, at any depth.
    #
    # Nothing is lost by the narrower tag: only APP tokens are filtered by origin
    # (`_serialize_for_client` returns the unfiltered payload to a dashboard
    # user), so a CRON-origin descendant stays in the sidebar exactly as today's
    # cron tabs do -- which is the property the paragraph above is protecting.
    #
    # Computed from the RE-RESOLVED caller below rather than here, because it is a
    # decision input to the allocation and everything above this point was read
    # before the coroutine suspended.

    if folder_id:
        # Confirmed under the folder-store lock -- the only place existence
        # cannot go stale against a concurrent delete (see `read_folders`) --
        # and READ-ONLY on purpose: the Model-B un-hide is a durable mutation,
        # and it runs only after the filing actually lands (below, after the
        # persist), so a create the re-gate refuses leaves no folder-tree state
        # behind. Placed BEFORE the re-gate so the last suspension this
        # coroutine takes is here: after the re-gate nothing suspends until the
        # slot is fully configured, so the folder confirmed here cannot be
        # deleted before the assignment lands (folder mutations run on this
        # loop).
        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise SessionControlError("folder not found", code="folder_not_found")

    # Re-resolved and re-gated HERE, adjacent to the allocation, because every
    # decision above was made before this coroutine suspended -- for the
    # project directory, the config load, and the folder confirmation -- and the
    # inputs to those decisions are live state that can flip inside any of those
    # windows.
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
    # The child's origin tag, read off the caller that is live NOW -- see the
    # reasoning above the folder gate. `_cron_caller` covers a cron's own tab;
    # `_origin` carries the tag onward to every descendant of one.
    child_origin = (
        SlotOrigin.CRON
        if _cron_caller(caller_key) or getattr(live_caller, "_origin", "") == SlotOrigin.CRON
        else SlotOrigin.USER
    )
    # The RATE guard, ahead of the capacity ceilings below. Those bound how many
    # sessions can exist; this bounds how fast one caller may open them, which is
    # the property an auto-approved verb loses -- a waived prompt leaves a loop
    # nothing to push back on. Deliberately the control that needs no durable
    # state: a lifetime quota means nothing across a restart unless every
    # rehydrate path carries its attribution, while a five-minute window buys a
    # restart one window rather than a clean slate.
    if not allow_create(SESSION_CREATE, caller_key):
        raise SessionControlError(
            "too many sessions created recently; retry shortly",
            code="create_rate_limited",
            status=429,
        )
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise SessionControlError(
            f"slot cap reached ({MAX_LIVE_SLOTS})",
            code="slot_cap_reached",
            status=429,
        )
    # Then the per-creator sub-ceiling. The global cap above bounds the TOTAL but
    # not the distribution, so without this one caller can hold all 500 and the
    # person's own next chat tab gets the 429 -- the resource is bounded, but not
    # from anyone else's point of view. This is the bound that makes the verb safe
    # to auto-approve: the worst case of an automated creator looping on it is its
    # own 50 slots, not everyone's 500.
    if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
        raise SessionControlError(
            f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
            code="creator_slot_cap_reached",
            status=429,
        )

    # The agent rides in the constructor rather than being assigned afterwards, for
    # the same reason: it decides which workspace actually EXECUTES the turn, so it
    # must never be observable as empty. Everything after this point is synchronous
    # until the slot is fully configured.
    #
    # The whole allocation-to-persist span runs under `suspend_slots_push`:
    # `get_or_create_slot` broadcasts on a leading edge, so without the suspend an
    # idle gateway serializes and sends the new slot BEFORE `folder_id` is
    # assigned -- every client (and any app on `slots:user`) would render the
    # session at the top level for a frame, the observable unfiled state #6118
    # exists to remove. It also covers the persist and its failure retraction, so
    # a slot whose birth write fails is never broadcast at all. Same pattern the
    # move path uses ("file the slot before the coalesced broadcast").
    with state.suspend_slots_push():
        slot = state.get_or_create_slot(
            None, agent=agent_name, workspace=workspace, origin=child_origin
        )
        # Attribute the slot to the caller that asked for it, which is what makes the
        # per-creator ceiling above countable. Written here, inside the synchronous
        # window that follows the mint, so no suspension point separates the cap test
        # from this write -- otherwise two concurrent creates could both pass a ceiling
        # that one of them had already filled. Only this entry point sets it: a
        # person's own tab and a fork reach `get_or_create_slot` directly and stay
        # unattributed, so ordinary human use never consumes an automated caller's
        # share.
        slot._created_by = caller_key
        # The creator's interactive auto-approve grant follows the work it is
        # handing off. Without this a trusted operator dispatches a worker that
        # then blocks on an approval prompt nobody is watching -- the same failure
        # `parent_trusted` already closes for `spawn_run` subagents, which read the
        # parent's stored policy and start auto-approved. A dispatched session is
        # the same delegation with a sidebar tab, so it takes the same posture.
        #
        # Read off `live_caller`, not the entry-time `caller_slot`: the two are
        # identity-checked to be the same object above, but the grant itself is
        # mutable state the operator can revoke inside any of the suspensions this
        # coroutine took, so the value that transfers is the one held NOW, in the
        # synchronous window that follows the last gate. Revoking before the create
        # lands means the child is born untrusted, which is the direction that
        # fails safe.
        #
        # What transfers is SESSION POSTURE, and only that. Two fields carry, two
        # deliberately do not, and the exclusions are the load-bearing part:
        #
        # * `_trust` -- the human's "trust this session" click. It does not expire,
        #   the click is its own audit record, and copying it changes no property
        #   of the grant. The session-store half needs no write here: the child has
        #   no ACP session yet (`set_approval_policy` would silently no-op on a
        #   missing session), and `chat_runner` already assigns the persistable
        #   policy from `_trust` on every session create/resume, so the subagent
        #   spawn gate sees it from the child's first turn.
        # * `_trust_reads` -- the same posture, narrowed to read-only bash. It has
        #   to carry too, or the setting a CAUTIOUS operator picks is the one whose
        #   own workers still stall. Bounded by construction: what it admits has no
        #   side effects, which is what separates it from the command grants below.
        #
        # * `_trusted_patterns` -- NOT inherited. These are per-command grants
        #   ("`npm test` is fine"), not a posture, and the distinction decides it:
        #   a pattern is judged against the session the operator was LOOKING at,
        #   while a dispatched worker runs model-authored work they have not seen,
        #   so the same glob can admit a command the grant was never asked about.
        #   Inheriting them also buys nothing where it would be safe -- with
        #   `_trust` set the child already auto-approves via `_slot_is_trusted`, so
        #   the pattern list is dead weight; it changes the outcome ONLY when the
        #   operator withheld session trust and granted single commands instead,
        #   which is exactly the case that must keep asking. So the child starts
        #   with `_ChatSlot.__init__`'s empty set and earns its own grants.
        # * `_trust_scope` -- NOT inherited. It names a TTL-bounded, SEL-audited
        #   `SafetyOverride` grant that is re-checked on every approval; forking
        #   the key would hand a second session a credential whose revocation
        #   nothing here can observe. An unattended worker that needs one gets its
        #   own, armed by whatever owns its lifecycle.
        #
        # Not persisted at birth, matching every other slot: trust is in-memory by
        # construction, so a restart returns the child to interactive along with
        # its creator.
        inherited_trust = bool(getattr(live_caller, "_trust", False))
        inherited_trust_reads = bool(getattr(live_caller, "_trust_reads", False))
        slot._trust = inherited_trust
        slot._trust_reads = inherited_trust_reads
        # cwd must follow the workspace too, or file search and project-scoped agents
        # resolve against a directory the slot does not claim -- the same
        # authorization-vs-execution split as the agent binding, one layer down.
        if not slot.project:
            slot.project = project_dir
        if folder_id:
            # Filed inside the same synchronous window that configures the slot, so
            # the session is never observable unfiled -- the atomicity #6118 exists
            # for. Existence was confirmed under the store lock above, and folder
            # mutations run on this loop, so the folder cannot have been deleted
            # between that check and this assignment. No `_folder_changed` flag: the
            # slot's first turn carries the armed first-turn breadcrumb injection
            # (`is_new` in chat_runner), so the [FOLDER] line reaches the model
            # without it.
            slot.folder_id = folder_id
        if title.strip():
            slot.title = sanitize_outbound(title.strip())[:200]
            slot._titled = True
        # Persist at birth. `save_slot_off_loop` cannot do this: the save it wraps
        # returns early on an empty message window -- a full save has nothing to
        # write -- so a freshly created session, which has no messages by
        # definition, would write nothing at all. The tool would then hand back a
        # session that does not survive a restart.
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
                    # reach that path: `_save_slot_to_history` runs a full save only
                    # when the window has messages, so for a session that is created
                    # and then sits idle THIS dict is the only record on disk.
                    # Omitting `origin` is silently destructive on the next restart:
                    # rehydrate falls back to the fail-closed empty sentinel, so a
                    # session opened as USER comes back unattributed and `slots:user`
                    # subscribers stop seeing it. Checked field-by-field against the
                    # save path; these are the only fields a slot carries at birth
                    # that it does not already write.
                    "tab_id": slot._tab_id,
                    "origin": slot._origin,
                    "created_at": metadata_now_iso(),
                    "workspace": slot.workspace,
                    "agent": slot.agent or "",
                    "project": slot.project or "",
                    "title": slot.title or "",
                    "memory_mode": getattr(slot, "memory_mode", "persistent"),
                    # Only when filed, mirroring the normal save path, which omits
                    # `folder_id` from the metadata line when empty. Without this
                    # the filing would not survive a restart: for an idle newborn
                    # THIS dict is the only record of the placement on disk.
                    **({"folder_id": slot.folder_id} if slot.folder_id else {}),
                    # Creator attribution, only when this entry point set it. The
                    # member ownership boundary in `authorize_target` reads it, so
                    # losing it on restart would strand every worker a member
                    # dispatched — controllable in memory, orphaned after reboot.
                    **({"created_by": slot._created_by} if slot._created_by else {}),
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
        if slot.folder_id:
            # Model-B un-hide, applied only NOW that the filing has actually
            # landed -- running it any earlier persists `hidden = False` for a
            # create a later gate can still refuse, durably reversing a choice
            # the user made for a call that failed. The move path holds the same
            # order (assign, confirm, then un-hide). If the folder was deleted
            # while the persist was in the worker thread, the delete's own sweep
            # already unfiled this slot (it is published), so the guard reads
            # the fresh value and skips; the metadata line can then briefly
            # carry a dangling folder_id, the same accepted residual a move
            # racing a delete leaves, and readers fall back to "(unfiled)".
            #
            # Best-effort: the create is already COMMITTED (slot published,
            # persisted at birth), so a folder-store write failure here must not
            # propagate -- the request would report failure for a session that
            # exists, and the caller's retry would create a duplicate. A folder
            # left hidden with a session inside is the recoverable lesser harm.
            try:
                await _unhide_folder(state, slot.folder_id)
            except Exception:
                logger.warning(
                    "create_session: filing committed for %s but un-hiding folder %s failed",
                    slot.key,
                    slot.folder_id,
                    exc_info=True,
                )
        state.push_slots_update()
    _audit(
        caller_session_key=caller_key,
        operation="create",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "agent": slot.agent or "",
            "folder_id": slot.folder_id or "",
            # What the child was BORN with, so an auto-approved tool call in it is
            # traceable to the creator's grant rather than appearing unexplained.
            # Always present: "false" is the record that the grant did not transfer.
            "inherited_trust": "true" if inherited_trust else "false",
            "inherited_trust_reads": "true" if inherited_trust_reads else "false",
        },
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": slot.title or slot.key,
    }


def authorize_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    operation: str,
    skip_enabled_check: bool = False,
) -> "_ChatSlot":
    """Resolve *target* and decide whether *caller* may act on it.

    Deny-by-default: every refusal raises :class:`SessionControlError` and is
    recorded in the SEL, so an attempt to reach a session that is out of bounds
    is visible after the fact even though nothing happened.

    ``skip_enabled_check`` omits ONLY the ``session_control_enabled()`` config
    read. It exists for a re-check that must run SYNCHRONOUSLY with no event-loop
    suspension (``close_target``'s point-of-no-return callback): the feature was
    already confirmed enabled when the operation was first authorized, whether
    session control got switched off mid-operation is not a containment boundary,
    and the config read is the one part of this function that can touch the disk
    on a cache miss. Every containment and identity refusal still runs.
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

    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        # Without a resolved caller the self-target guard is blind, and a session
        # that can reach every peer while being unidentifiable is exactly the
        # shape this surface must not have.
        raise deny("caller session could not be identified", "caller_unidentified")
    # Resolved before the config gate: a member DM session is authorized
    # WITHOUT `agent.session_control` — dispatching and patrolling workers is
    # its operating model — and is bounded instead by the ownership check
    # below, which restricts it to slots it created itself.
    if not skip_enabled_check and not session_control_enabled() and not _member_caller(caller_key):
        raise deny(
            "session control is disabled in config (agent.session_control)",
            "session_control_disabled",
        )
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES) and not _cron_caller(caller_key):
        raise deny(
            "unattended sessions (scheduled runs) cannot control other sessions",
            "unattended_caller",
        )
    if (refusal := _app_owned_cron_refusal(state, caller_key)) is not None:
        # The app-confinement refusal, reached through an app's cron rather than
        # its session -- a cron tab carries no ``_app`` tag for the check further
        # down to read. See :func:`_app_owned_cron_refusal`.
        #
        # Placed BEFORE `_resolve_slot`, unlike the other caller-side refusals: a
        # caller refused for its own identity must not learn anything from the
        # attempt, and resolving first makes the refusal an existence oracle -- a
        # guessed target answers `target_not_found` (404) when it does not exist
        # and this refusal (403) when it does, so a caller allowed to touch
        # NOTHING could enumerate the user's session keys and titles by the shape
        # of the error. The prefix gate above is already on this side of the
        # resolution for the same reason.
        raise deny(refusal[0], refusal[1])

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
    caller_link = getattr(caller_slot, "linked_session_key", "")
    if caller_link and not caller_link.startswith(CRON_LINK_PREFIX):
        # The exfiltration direction, and the reason this is not merely the
        # mirror of the target-side check: a linked caller's own conversation is
        # a channel thread, so anything it reads lands in front of whoever is in
        # that channel. `session_read_message` would hand a private dashboard
        # transcript to Slack/Discord readers who were never party to it.
        #
        # `CHANNEL_AGENT_BLOCKED_TOOLS` already blocks these tools for channel
        # AGENTS, but that guard keys on the agent identity; a linked SLOT is a
        # second route to the same surface and has to be closed on its own.
        #
        # A cron tab's link is exempt because it is not a channel: it names the
        # job's own run transcript and republishes to nobody, so a read through it
        # reaches no audience the caller did not already have. See
        # CRON_LINK_PREFIX.
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
    if (
        _caller_is_ownership_fenced(state, caller_key)
        and getattr(slot, "_created_by", "") != caller_key
    ):
        # The fence every exempted caller class is bounded by, plus anything they
        # created. It reaches ONLY the sessions the caller made itself
        # (`created_by` is written at birth and rehydrated on restart). Always
        # enforced -- even when the global switch is on -- so no exemption
        # silently widens to the user's own sessions because of an unrelated
        # opt-in.
        #
        # For a cron caller this fence stands in place of the `unattended_caller`
        # refusal every other unattended caller gets: a scheduled job reaches the
        # sessions it dispatched and nothing else. Fail-closed on an unowned slot,
        # which is what an ownerless rehydrate looks like.
        if _cron_caller(caller_key):
            fence_reason = "a scheduled run can only control sessions it created itself"
        elif _member_caller(caller_key):
            fence_reason = "a crew member can only control worker sessions it created itself"
        else:
            fence_reason = "an agent-created session can only control sessions it created itself"
        raise deny(fence_reason, "not_creator")

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
    FIRST ``sel()`` of a process CONSTRUCTS the log -- trust-dir creation and
    key validation, blocking file IO -- and this can genuinely
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
    creates the trust dir and validates keys — blocking file IO), and a construct
    that RAISES, which unguarded turns a 403 into a
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

    A stop cancels cooperatively. The button escalates to a hard kill when a
    second press lands while the first is still pending; this verb deliberately
    does not do that for a repeat it cannot tell apart from a RETRY, because a
    client that got no response inside its request timeout re-sends the same
    request, and the kill path discards the target's queue and pending steers. So
    within ``stop_retry.WINDOW_SECS`` of this caller's first stop of this target, a
    repeat returns the existing "stop already in progress" no-op instead. A stop
    arriving after that window still escalates, so a genuine second decision keeps
    the capability — only a blind retry cannot reach it (issue #5074).

    Withholding the escalation never costs the caller the stop it asked for: a
    repeat that finds the target running again soft-stops it as a first call would.

    Still no force flag: escalation is decided by the target's own stop state and
    the window above, never by anything the caller can ask for, so advertising one
    would promise a hard kill a first call cannot deliver.
    """
    # Prewarmed BEFORE `authorize_target`, and that ordering is load-bearing.
    # `stop_slot_turn`'s IDLE branch logs to the SEL with no await before it, so on
    # a fresh gateway a first `session_stop` against an idle slot would CONSTRUCT
    # the log on the loop -- trust-dir creation and key validation, blocking file
    # IO. Constructing it off-loop first makes that call a cheap cache hit.
    # Per-request, not a boot step: prewarming at startup is what
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
    # Both calls below are SYNCHRONOUS, which is what lets them sit here at all:
    # the rule the comment above states is that nothing may SUSPEND between the
    # gate and the act, and neither of these does.
    #
    # `caller_slot_key` repeats the slot walk `authorize_target` just did rather
    # than changing what that function returns for all three verbs. The walk is
    # bounded by `MAX_LIVE_SLOTS` and touches no filesystem, and with no
    # suspension between them the two resolutions cannot disagree — a rebind
    # landing in that window is impossible, not merely unlikely.
    caller_key = caller_slot_key(state, caller_session_key)
    may_escalate = allow_escalation(caller_key, slot.key)
    # Deferred: ``chat_handlers`` imports ``dashboard.chat`` transitively, which
    # reaches back into the gateway at import time — a module-scope import here
    # closes that cycle through ``handlers.session_control`` -> ``server``.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    result = await stop_slot_turn(state, slot, source="session_control", escalate=may_escalate)
    _audit(
        caller_session_key=caller_session_key,
        operation="stop",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "result": result.get("info", "stopping"),
            # Recorded on the ALLOWED line, not only inside `stop_slot_turn`'s
            # own audit: this is the layer that made the retry judgement, so the
            # session-control trail has to show it was made.
            "escalation_withheld": not may_escalate,
        },
    )
    return {"ok": True, "target": slot.key, **result}


async def close_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
) -> dict[str, Any]:
    """Close *target*, the same archival the tab ✕ performs.

    Non-destructive: the conversation is saved to history and can be reopened
    later — closing dismisses the LIVE tab, it does not delete the transcript.
    An in-flight turn is cancelled first (its work is discarded), so this is a
    strictly heavier act than :func:`stop_target`; the description tells the
    caller to read the session before closing it.

    Reuses the dashboard's own close path (:func:`chat_handlers.close_slot`), so
    a controlled close and a human ✕ share the identical nudge-retirement and
    app-notification ordering that keeps a dismissed tab from being resurrected.
    Its three failure modes surface as their own ``SessionControlError`` codes
    rather than a generic 500, so a caller can tell "the app refused the
    dismissal" from "history could not be saved".
    """
    # Same prewarm ordering as `stop_target`, for the same reasons: the SEL write
    # inside `authorize_target`'s deny path must be a cache hit, and the config
    # warm must be the LAST suspension before the synchronous gate.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the close
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="close",
    )
    slot_key = slot.key
    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_handlers import SlotCloseError, close_slot

    def _reassert_closeable() -> None:
        # Re-run the SAME target gate at close_slot's point of no return —
        # SYNCHRONOUSLY, so there is NO event-loop suspension between it and the
        # pop and nothing can change between the final authorization and the
        # archival. The initial gate above ran before close_slot's awaits
        # (nudge-loop retirement takes the AutoNudge lock; the app hook awaits
        # external work), and a target that was unmirrored/unlinked then can gain
        # a channel mirror or link in that window — archiving a now-channel-backed
        # session the caller was never allowed to reach.
        #
        # `skip_enabled_check=True` omits the ONE part of authorize_target that
        # can touch the disk (`session_control_enabled()`'s config read): the
        # feature was already confirmed enabled above, whether it was switched off
        # mid-close is not a containment boundary, and skipping it is what lets
        # this run with no await — an async prewarm-then-check would put an await
        # back before the pop and reopen the very window this closes. Every
        # containment and identity refusal still runs.
        try:
            live = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=slot_key,
                operation="close",
                skip_enabled_check=True,
            )
        except SessionControlError as exc:
            # A stale-authorization refusal (mirrored/linked/workspace/caller-gone)
            # becomes a SlotCloseError carrying that same status, so it round-trips
            # to the caller as the specific 403 rather than a generic close failure.
            raise SlotCloseError(exc.message, code=exc.code, status=exc.status) from exc
        if live is not slot:
            # The key was re-minted onto a DIFFERENT session while close_slot
            # awaited (a concurrent close+reopen). authorize_target resolves by
            # key, so it would authorize the replacement — but close_slot pops
            # `name` and tears down / saves the ORIGINAL slot it holds. Comparing
            # identity (not mere presence) is the same guard `create_session` uses
            # for its re-minted-key window; abort so the replacement lives.
            raise SlotCloseError(
                "the target session was replaced during the close",
                code="target_replaced",
                status=409,
            )

    try:
        await close_slot(state, slot, slot_key, pre_pop_check=_reassert_closeable)
    except SlotCloseError as exc:
        # The close path already rolled back every partial step and logged the
        # cause; re-raise it as the surface's own error so the caller sees the
        # specific reason (nudge/app/history) rather than a bare failure. Audited
        # as a denied operation so the trail shows the close was attempted and did
        # not take.
        _audit(
            caller_session_key=caller_session_key,
            operation="close",
            slot_key=slot_key,
            outcome="denied",
            detail={"code": exc.code},
        )
        raise SessionControlError(exc.message, status=exc.status, code=exc.code) from exc
    _audit(
        caller_session_key=caller_session_key,
        operation="close",
        slot_key=slot_key,
        outcome="allowed",
    )
    return {"ok": True, "target": slot_key}


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

    # A crew-bound target executes its turns on the peer, not here. The delivery
    # below hands ``_run_chat`` to ``enqueue_or_run_prompt``, which has no
    # remote/executor branch — so on a bound target it would run the crew's work
    # on THIS machine and diverge the local and peer transcripts, the same failure
    # the send / regenerate / rewind / continue paths refuse. Relaying a
    # cross-session send is a separate mechanism (open a peer turn, mirror it
    # back); until that exists the send is refused rather than run locally
    # (GPT #7693). Keyed on ``executor``, so a half-open binding is refused too.
    if slot.executor == "remote":
        raise SessionControlError(
            "that session runs on a remote crew; sending into a crew-bound "
            "session from another session is not supported yet",
            code="remote_target_unsupported",
            status=409,
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
    # and credits each trimmed row to a frozen-prefix counter, so window length
    # stops growing once trimming starts. A cursor derived from that length
    # would freeze at the cap and never see another reply; adding the
    # frozen-prefix count makes it monotonic for the session's whole life.
    raw_window = list(slot.messages)
    # The DURABLE frozen-prefix counter, never ``_disk_older_count``: that one
    # counts every trimmed row, transient ones included, while the positions
    # below are built over the durable rows the filter keeps. Basing on the
    # all-rows counter shifted every position as soon as a transient row was
    # trimmed, and a ``since`` read then served a durable message the caller
    # already had. ``_disk_older_durable_count`` counts exactly the rows the
    # ``TRANSIENT_ROLES`` filter below would have kept, so the two spaces agree
    # for the session's whole life. Defensive ``getattr`` matches how the
    # existing code reads ``_disk_older_count``: a slot restored by an older
    # build simply has no trimmed prefix yet.
    base = int(getattr(slot, "_disk_older_durable_count", 0) or 0)
    # Stop the cursor before the streaming tail (see ``TRANSIENT_ROLES``): those
    # rows are deleted when the segment flushes, so a cursor past them would sit
    # beyond the list that replaces them and never return the finished reply.
    messages = [m for m in raw_window if m.get("role") not in TRANSIENT_ROLES]
    durable_end = len(messages)
    total = base + durable_end
    if since is not None:
        if since < 0:
            raise SessionControlError("since must be >= 0", code="invalid_since")
        if since < base:
            # The cursor points into the trimmed prefix: those rows exist only
            # on disk now, and this read serves the in-memory window. Starting
            # at ``base`` instead would silently skip every row in
            # ``[since, base)`` — a poller that lagged a whole window behind
            # would lose messages with nothing in the response saying so. The
            # refusal is loud and the tail-read fallback recovers, exactly like
            # the past-the-end case below.
            raise SessionControlError(
                "this session is long enough that the messages at your cursor "
                "have been trimmed from memory — read without `since` to get "
                "the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        # A cursor PAST the end is the remaining inexact case, and it is not the
        # same as a stale one: rewind and regenerate shrink a transcript, so
        # `total` can move backwards under a caller that is still holding the old
        # position. Clamping it to `total` would start the read at the end and
        # silently skip every replacement row written below the old cursor, with
        # nothing in the response saying so. So this refuses loudly rather than
        # answer approximately. Reads without `since` are unaffected.
        if since > total:
            raise SessionControlError(
                "this session is shorter than your cursor — it was rewound or "
                "regenerated, so earlier positions no longer line up — read "
                "without `since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        start = since
        # Positions are absolute; the window slice below is offset-relative, so
        # subtract the durable prefix that is no longer in memory.
        offset = start - base
    else:
        # A tail read never refuses (only a `since` below the trimmed prefix or
        # past the end is), and the two spaces come apart here: slice the
        # in-memory window by OFFSET, but report the index in ABSOLUTE terms so
        # the number still means "position in the session". Conflating them
        # returned an empty window, because `total` counts the frozen prefix
        # the list does not hold.
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
        # Returned on trimmed sessions too: positions are based on the
        # durable-only prefix counter, so they stay exact after rows age into
        # the frozen prefix. The refusals above cover the cases that genuinely
        # cannot be exact (a cursor under the trimmed prefix, or past the end of
        # a rewound transcript).
        "next_since": start + len(out),
        "messages": out,
    }
