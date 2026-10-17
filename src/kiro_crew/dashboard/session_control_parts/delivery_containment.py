"""What a delivery was admitted under: the channel-mirror probes, the containment stamps
a queued entry carries, the notices a drop or a discard sends back to its sender, and
the audience-admission record a turn's publication is judged against.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    _sel_off_loop,
)
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


# Joins the rooms of a :func:`_probe_channel_mirror` identity. Never part of a
# channel or thread id on any surface the probe composes from (Slack, Discord and
# Telegram ids are alphanumeric, a Slack thread is a decimal timestamp), so the
# split in :func:`mirror_audience` is the exact inverse of the join.
MIRROR_IDENTITY_SEPARATOR = "|"


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
    re-check exists to catch.

    Read on the EFFECTIVE session key, because that is the key the mirror is
    registered under -- the slot key would miss a mirror on a session whose turns
    run under a different identity.

    Composed from BOTH store accessors the delivery legs read -- ``get_mirror_link``
    and ``get_slack_link`` -- because the first shadows the second: it returns the
    explicit ``mirror`` row whenever one exists and never looks at the Slack fields
    beside it, while the dashboard's slack-link binds its thread onto a channel-born
    slot's own session key (``DashboardState.link_slack``) without touching that
    row. Read through the mirror alone, a thread bound while an entry waited would
    leave this identity unchanged and the drain would deliver into a room the
    admission never saw. The thread is appended only when one is named: a
    threadless Slack row is bookkeeping, and the store itself never reads it as a
    mirror. Same two reads as :func:`owner_dm_refusal`, for the same reason.

    The identity is therefore a SET of rooms -- one ``type:channel:thread`` part
    per accessor that names one, joined by :data:`MIRROR_IDENTITY_SEPARATOR` --
    and the drain compares it as that set (:func:`mirror_audience`), not as one
    opaque string: a room the admission never saw is a retarget or a widening
    and drops, while a room that has since gone away is a narrowing and admits.
    Compared whole, a Slack thread UNLINKED while an entry waited would read as
    "the mirror changed" and drop a delivery whose audience only shrank.
    """
    if getattr(getattr(state, "sessions", None), "get_mirror_link", None) is None:
        # No store to ask: answer "not mirrored" WITHOUT touching the slot, the
        # same order the pre-split probe had. Callers that stamp containment
        # on a duck-typed slot (the spec-builder queue path) rely on it.
        return ""
    try:
        key = slot_history_key(slot)
    except Exception:
        logger.debug("mirror-link probe failed", exc_info=True)
        return None
    return _probe_channel_mirror_for_key(state, key)


def _probe_channel_mirror_for_key(state: "DashboardState", session_key: str) -> str | None:
    """:func:`_probe_channel_mirror` for a session that has no live slot.

    The mirror link lives in the session store keyed by the effective session
    key, so an ARCHIVED session can carry one just as a live slot can; the
    revive path reads it through this form because it has only the history key.
    Same tri-state, same fail-closed reading by the refusal paths, and the same
    two store reads (mirror row plus Slack binding) as the live form.
    """
    sessions = getattr(state, "sessions", None)
    if getattr(sessions, "get_mirror_link", None) is None:
        return ""
    binding = _read_mirror_binding(sessions, session_key)
    return None if binding is None else _mirror_identity_of(binding)


def _read_mirror_binding(sessions: Any, session_key: str) -> tuple[Any, str, str] | None:
    """ONE read of *session_key*'s outbound binding -- ``(mirror row, Slack thread,
    Slack channel)`` -- or ``None`` when the store cannot answer.

    The single store read a caller-side admission rests on. :func:`judge_owner_dm`
    judges its clauses on this tuple AND builds the audience it records from the
    same tuple (:func:`_mirror_identity_of`), so the row a gate validates and the
    row it records are one object: a retarget that lands after this read is a
    change the publisher sees against the record, never a room the record adopts
    unjudged. The probe (:func:`_probe_channel_mirror_for_key`) is this read
    reduced to its identity, so the drain and the publisher compare the same two
    accessors the delivery legs read: ``get_mirror_link`` and, through
    :func:`_slack_thread_of`, ``get_slack_link``.

    *sessions* must carry ``get_mirror_link``; the callers check that first, since
    a store without the accessor is "no mirror" (a duck-typed slot on the
    spec-builder queue path) rather than an unreadable one. A store whose accessor
    RAISES is unreadable and reads ``None`` -- the tri-state the two consumers need,
    see :func:`_probe_channel_mirror`.
    """
    try:
        link = sessions.get_mirror_link(session_key)
        slack_thread, slack_channel = _slack_thread_of(sessions, session_key)
    except Exception:
        logger.debug("mirror-link probe failed", exc_info=True)
        return None
    return link, slack_thread, slack_channel


def _mirror_identity_of(binding: tuple[Any, str, str]) -> str:
    """The probe identity of one :func:`_read_mirror_binding` -- its rooms, joined.

    One ``type:channel:thread`` part per accessor that names a room, joined by
    :data:`MIRROR_IDENTITY_SEPARATOR`; ``""`` when neither does. The Slack thread is
    appended only when one is named: a threadless Slack row is bookkeeping the store
    itself never reads as a mirror.
    """
    link, slack_thread, slack_channel = binding
    parts: list[str] = []
    if link:
        parts.append(
            f"{getattr(link, 'channel_type', '')}"
            f":{getattr(link, 'channel_id', '') or ''}"
            f":{getattr(link, 'thread_id', '') or ''}"
        )
    slack_identity = f"slack:{slack_channel}:{slack_thread}" if slack_thread else ""
    if slack_identity and slack_identity not in parts:
        parts.append(slack_identity)
    return MIRROR_IDENTITY_SEPARATOR.join(parts)


def mirror_audience(identity: Any) -> frozenset[str]:
    """The rooms a probe identity names, as the set the drain compares.

    Each member is one ``type:channel:thread`` part, compared WHOLE -- the colons
    inside a part are never split, so a surface whose ids contain colons still
    compares as one room. ``""`` (no mirror) and any non-string are the empty
    set. Order-insensitive by construction, which the probe's fixed part order
    does not need but a comparison must not depend on.
    """
    if not isinstance(identity, str) or not identity:
        return frozenset()
    return frozenset(part for part in identity.split(MIRROR_IDENTITY_SEPARATOR) if part)


def _slack_thread_of(sessions: Any, key: str) -> tuple[str, str]:
    """``(thread_ts, channel_id)`` of *key*'s Slack binding, ``("", "")`` when none.

    The probe's second read. Only the store's own answer shape counts -- a pair
    whose first member names a thread -- so a store without the accessor, or a
    stand-in that answers something else, reads as "no thread" rather than as an
    unverifiable probe: the mirror read alone was the whole probe before this read
    existed, and a second read must not turn every store that lacks it into a
    fail-closed drop. A store whose accessor RAISES still fails the probe, exactly
    as a raising mirror read does -- the caller's ``except`` is what decides that.
    """
    thread_getter = getattr(sessions, "get_slack_link", None)
    if not callable(thread_getter):
        return "", ""
    pair = thread_getter(key)
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        return "", ""
    thread, channel = pair
    if not isinstance(thread, str) or not thread:
        return "", ""
    return thread, str(channel or "")


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


# ── Drain-time re-validation of queued prompts ──
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


# Queue-entry meta key naming the slot that SENT a cross-session delivery, so a
# drain-time drop can be reported back to it. It rides ``meta`` rather than a
# consumption callback because ``meta`` is one of the keys a queued prompt is
# persisted with, while an entry carrying a callback is excluded from that write
# (``slot_queue_repository._is_durable_queue_entry``): recording the sender as a
# callback would trade a relay's survival across a restart for a notice that
# cannot survive one either. A requeued steer keeps it for free — the requeue
# copies the admission dict onto the new entry's meta.
SEND_ORIGIN_META_KEY = "send_origin_slot"


# Queue-entry meta key naming the CHANNEL CONVERSATION that sent a message into a
# resumed dashboard session mid-turn (``dashboard.channel_handoff``), so a
# drain-time drop can be reported back into that conversation: the channel was
# told "queued" at admission and, unlike a dashboard sender, reads neither the
# target's transcript nor the SEL. Same reasoning as the sender stamp above for
# why it is ``meta`` and how a requeued steer keeps it. It names a WRITE TARGET
# on a network surface, so the restore path strips it like the sender stamp, and
# the notice re-runs the outbound recipient authorization before it is sent.
CHANNEL_RECIPIENT_META_KEY = "channel_recipient"


# How much of a dropped delivery's own text the sender's notice quotes back, so
# a caller holding several deliveries in flight can tell which one went.
SEND_DROP_EXCERPT_CHARS = 120


# The two deliberate discards of a queued entry that are not the drain's
# authorization re-check: a person cancelling the entry's queue card, and a hard
# stop clearing the whole queue. Each is passed as the ``cause`` of
# :func:`notify_send_origin_discarded`, which words the sender's notice from it.
SEND_DISCARD_QUEUE_CANCEL = "queue_cancel"


SEND_DISCARD_HARD_STOP = "hard_stop"


# Sender-notice phrasing per discard cause. Neither discard changes a
# constraint, so the drain's containment wording would describe nothing here.
_SEND_DISCARD_REASONS = {
    SEND_DISCARD_QUEUE_CANCEL: "it was cancelled from that session's queue",
    SEND_DISCARD_HARD_STOP: "a hard stop of that session discarded its queue",
}


# Transcript-notice phrasing per snapshot field, for the drop notice a reader
# of the session must be able to understand without knowing this module.
_CONTAINMENT_CHANGE_LABELS = {
    "linked": "the session was linked to a channel",
    "mirrored": "the session gained an outbound channel mirror",
    "mirror_retarget": "the session's outbound mirror was retargeted to a different channel",
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
# the exact republication this drain re-check catches. `session_send` and
# automation entries never carry the flag and stay fully enforced.
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
    return _snapshot_from_probe(
        state, slot, _probe_channel_mirror(state, slot), on_probe_failure=on_probe_failure
    )


def _snapshot_from_probe(
    state: "DashboardState", slot: "_ChatSlot", probed: str | None, *, on_probe_failure: bool
) -> dict[str, Any]:
    """:func:`containment_snapshot` over a probe already taken.

    *probed* is :func:`_probe_channel_mirror`'s tri-state for *slot*. Split out so
    :func:`judge_owner_dm` can build the audience it records from the SAME read it
    judged rather than probing the store a second time -- the second read is where
    a retarget could slip in between validation and record. Every other field is a
    plain slot attribute read that cannot fail.
    """
    snap: dict[str, Any] = {
        "linked": bool(getattr(slot, "linked_session_key", "")),
        "mirrored": on_probe_failure if probed is None else bool(probed),
        "ephemeral": getattr(slot, "memory_mode", "persistent") != "persistent",
        "app": bool(getattr(slot, "_app", "")),
        "unattended": str(getattr(slot, "key", "")).startswith(UNATTENDED_SLOT_PREFIXES),
        "workspace": str(getattr(slot, "workspace", "default") or "default"),
    }
    if probed is not None:
        # The mirror's identity, compared by its rooms (:func:`mirror_audience`):
        # a RETARGETED mirror (A -> B) keeps the boolean true across the wait
        # while substituting the audience, so identity is what the drain must
        # compare -- and it must compare rooms, not the string, because a room
        # dropped while the entry waited is a narrowing, not a change of audience.
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
    return _meta_from_probe(state, slot, _probe_channel_mirror(state, slot))


def _meta_from_probe(
    state: "DashboardState", slot: "_ChatSlot", probed: str | None
) -> dict[str, Any]:
    """:func:`containment_meta` over a probe already taken -- the record shape an
    :class:`OwnerDmVerdict` carries, built from the read the verdict judged."""
    return {
        QUEUED_CONTAINMENT_META_KEY: _snapshot_from_probe(
            state, slot, probed, on_probe_failure=False
        )
    }


def send_origin_meta(state: "DashboardState", sender_slot_key: str) -> dict[str, Any]:
    """Queue-entry ``meta`` naming the slot a cross-session delivery came FROM.

    Stamped by the delivery paths that admit one session's text onto another
    session's queue, so a drain-time drop can be reported back to the sender
    (:func:`notify_send_origin_dropped`). The sender is told at admission that
    the message was queued rather than started; the drop itself is visible only
    on the target's transcript and in the audit trail, neither of which the
    sender reads.

    The stamp carries the sender's TAB IDENTITY beside its key, and both must be
    present or nothing is stamped. A slot key does not identify a session: the
    explicitly-named keys are deterministic (``cron-{job.id}``,
    ``workflow-{run_id}``, a channel's own), so a closed slot's key is handed to
    the next occupant, whose ``app``, ``origin`` and link scope are declared per
    creation and need not match the sender's. Resolving the notice from the key
    alone therefore appends one session's text to a DIFFERENT session's
    transcript once the sender closes and the key is reused. ``_tab_id`` is
    minted per slot object and is the identity the neighbouring save and close
    paths already compare on (``chat_persistence._slot_still_ours``).

    Empty for a caller with no slot of its own, and the key is then omitted
    rather than stamped blank: absent must mean "nobody to report to", which a
    blank string cannot be told apart from. A caller whose slot carries no tab
    identity is omitted the same way, because a stamp whose identity cannot be
    checked later is the one shape that must not produce a write.
    """
    key = str(sender_slot_key or "")
    if not key:
        return {}
    sender = state.get_slot(key)
    tab = str(getattr(sender, "_tab_id", "") or "")
    if not tab:
        return {}
    return {SEND_ORIGIN_META_KEY: {"slot": key, "tab": tab}}


def send_origin_slot(entry_meta: Any) -> str:
    """The sending slot key stamped on a queue entry, or ``""``.

    Pairs with :func:`send_origin_tab`: the key says where to write and the tab
    says which occupant of that key is owed the notice, so a caller that resolves
    a recipient needs BOTH to agree with the live slot.

    *entry_meta* is plumbing of any shape, so a missing, non-dict or malformed
    value reads as no sender and the drop proceeds exactly as it did before the
    stamp existed.

    A stamp this reads is one THIS process admitted. The restore path drops the
    key (:func:`~kiro_crew.dashboard.slot_queue_repository.sanitize_restored_queue`)
    because the value names a write target rather than being merely read: the
    drop resolves the recipient of its notice from this stamp and appends the
    entry's own text there, so a stamp carried back off an editable line would
    put attacker-chosen text in a session the editor does not own. The price is
    one notice: a delivery that outlives a restart and is then dropped reports to
    nobody, while the delivery itself still survives.
    """
    return _send_origin_field(entry_meta, "slot")


def send_origin_tab(entry_meta: Any) -> str:
    """The sending slot's tab identity stamped on a queue entry, or ``""``.

    The notice is owed to the slot OBJECT that sent the message, not to whatever
    currently answers to its key, so this is what tells a reused key apart from
    the original sender. See :func:`send_origin_meta` for why a key alone is not
    an identity.
    """
    return _send_origin_field(entry_meta, "tab")


def _send_origin_field(entry_meta: Any, field: str) -> str:
    """One string field of the sender stamp, or ``""`` for any other shape.

    Both readers fail closed through here on the same shapes, so a half-written
    or hand-edited stamp cannot answer one question and not the other -- which is
    what would let a key be trusted while its identity check silently passed.
    """
    if not isinstance(entry_meta, dict):
        return ""
    stamp = entry_meta.get(SEND_ORIGIN_META_KEY)
    if not isinstance(stamp, dict):
        return ""
    value = stamp.get(field)
    return value if isinstance(value, str) else ""


def send_drop_excerpt(text: Any) -> str:
    """The dropped message's own opening, for the notice the sender reads.

    A caller can have several deliveries in flight to several targets, and the
    target's key alone does not say WHICH message went. Whitespace is collapsed
    so a multi-line prompt stays one line in the notice, and the cut is marked
    with an ellipsis so a truncated quote is never mistaken for the whole text.
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= SEND_DROP_EXCERPT_CHARS:
        return flat
    return flat[:SEND_DROP_EXCERPT_CHARS].rstrip() + "…"


def channel_recipient_meta(
    channel_type: str, conversation_id: str, principal: str
) -> dict[str, Any]:
    """Queue-entry ``meta`` naming the channel conversation a message came FROM.

    Stamped by :func:`~kiro_crew.dashboard.channel_handoff.hand_to_resumed_slot`
    on both of its arms (the queue entry directly; the steer through its admission
    dict, which the requeue copies onto the entry), so a drain-time drop can be
    reported into that conversation (:func:`notify_channel_recipient_dropped`).

    *principal* is the platform user id the channel authorized on inbound. It
    rides along because the outbound recipient check needs one the SESSION KEY
    cannot supply: a dashboard session names no channel peer, and a Discord DM's
    conversation id is unrelated to the user id its roster holds, so without it the
    notice would be refused as an unidentifiable recipient. Empty when the
    channel has none to give (a thread route answers on its conversation id).

    Empty when either address field is missing, and then nothing is stamped
    rather than a half-address: a stamp that cannot be delivered to must not
    produce a write.
    """
    channel_type = str(channel_type or "")
    conversation_id = str(conversation_id or "")
    if not channel_type or not conversation_id:
        return {}
    return {
        CHANNEL_RECIPIENT_META_KEY: {
            "channel_type": channel_type,
            "conversation_id": conversation_id,
            "principal": str(principal or ""),
        }
    }


def channel_recipient_of(entry_meta: Any) -> tuple[str, str, str] | None:
    """``(channel_type, conversation_id, principal)`` from an entry's stamp, or None.

    *entry_meta* is plumbing of any shape: a missing, non-dict or malformed stamp
    -- a non-string field, an empty address -- reads as no recipient, and the drop
    proceeds unreported exactly as it does for a human-typed entry. Both readers
    of a write-target stamp fail closed on the same shapes.
    """
    if not isinstance(entry_meta, dict):
        return None
    stamp = entry_meta.get(CHANNEL_RECIPIENT_META_KEY)
    if not isinstance(stamp, dict):
        return None
    channel_type = stamp.get("channel_type")
    conversation_id = stamp.get("conversation_id")
    principal = stamp.get("principal", "")
    if not isinstance(channel_type, str) or not isinstance(conversation_id, str):
        return None
    if not channel_type or not conversation_id or not isinstance(principal, str):
        return None
    return channel_type, conversation_id, principal


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
    fail-closed floor stays the boolean set. ``mirror_identity`` is compared
    only when the entry recorded one too, but as a SET of rooms
    (:func:`mirror_audience`) rather than one identity: a room the admission
    never saw — a mirror retargeted to a different channel, or a Slack thread
    bound beside the admitted mirror while the entry waited — is an audience
    change the boolean cannot see, reported as ``mirror_retarget``; a room that
    has since gone away (a thread unlinked) is a narrowing and is not a change.

    *directive_user_origin* exempts the LINKED constraint only, for entries
    carrying the authenticated-human provenance flag: the author typed into the
    session's own surface and linking it is that owner's deliberate act, so
    dropping their already-typed messages when they link the session would
    destroy user speech on a supported flow (``api_chat`` applies no linked
    refusal to composer input). A NEW outbound mirror is never exempt — the
    message's author does not control mirror links, so it still drops. Every
    other constraint — ephemeral, app, unattended, workspace — applies
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
            # delivered when the store does not answer, because the audience
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
            # Room-set comparison: a mirror RETARGETED while the entry waited
            # (A -> B) keeps ``mirrored`` true at both ends while substituting
            # the audience, so the boolean can never see it, and a room ADDED
            # beside the admitted one (a Slack thread bound onto the session)
            # widens the audience the same way. Both hold a room the admission
            # never saw, which is the test. A room that has since gone away -- a
            # thread unlinked, a mirror cleared -- is a NARROWING: every room the
            # delivery can now reach was admitted, so it is not a change. Fires
            # only when both sides carry a verified, non-empty identity — a newly
            # GAINED mirror is the boolean's job, and an unverifiable side omits
            # the key. Never exempt for directive entries: the message's author
            # does not control mirror links.
            admitted_id = recorded.get("mirror_identity")
            if value and isinstance(admitted_id, str) and admitted_id:
                if mirror_audience(value) - mirror_audience(admitted_id):
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


def notify_send_origin_dropped(
    state: "DashboardState",
    *,
    origin: str,
    origin_tab: str = "",
    target_slot: "_ChatSlot",
    text: Any,
    constraints: list[str],
    mirror_unverified: bool = False,
) -> bool:
    """Tell the SENDING session that its queued delivery was dropped at the drain.

    ``send_to_target`` answers ``started: False`` when a busy target queues the
    message, and on its own that receipt says the message will run later. The
    drop notice, the retracted queue card and the broadcast all land on the
    TARGET, which the sender does not read, so the outcome a caller most needs —
    the message will never run — is the one it cannot see, and a caller polling
    the target's transcript waits for a reply that cannot come. This notice is
    what closes that.

    Returns whether a notice was appended. Four cases answer False and are not
    failures:

    * no stamp (``origin`` empty) — a human typed this into the composer, and
      there is no peer session waiting on it;
    * ``origin`` equals the target — a session that queued onto itself already
      has the target's own notice in the transcript it is reading, and a second
      row would report one drop twice;
    * the sending slot is gone — it was closed while the message waited, so
      there is no transcript left to write to. The SEL row still records the
      drop against the sender (:func:`audit_queued_drop`), which is what makes
      the outcome recoverable after the session is gone;
    * the key is live but holds a DIFFERENT occupant — ``origin_tab`` does not
      match the slot's ``_tab_id``. Named slot keys are deterministic and get
      reused, so this is the same case as the one above wearing the previous
      tenant's name, and writing anyway would put the sender's text on a session
      that never sent it. Treated as "the sender is gone", because it is.

    An absent ``origin_tab`` answers False whenever a slot is found, so a stamp
    that cannot be identity-checked never writes: the check is not skippable by
    omitting its input.

    Best-effort, like the target-side notice: a failure here is logged and the
    drop still proceeds. Withholding the message is the authorization decision,
    and it must not depend on the report landing.
    """
    target_key = str(getattr(target_slot, "key", ""))
    sender = _send_origin_recipient(state, origin, origin_tab, target_key)
    if sender is None:
        return False
    try:
        excerpt = send_drop_excerpt(text)
        sender.append(
            "notice",
            f"⚠️ Message you sent to {target_key} was dropped before it ran: "
            + describe_containment_change(constraints, mirror_unverified=mirror_unverified)
            + " after it was queued, so the authorization that admitted it no "
            + "longer holds. It was not delivered and will not run."
            + (f' Text: "{excerpt}"' if excerpt else ""),
            "msg msg-info",
        )
    except Exception:  # pragma: no cover - reporting must not block the drop
        logger.exception(
            "Failed to report a dropped delivery to its sender (origin=%s, target=%s)",
            origin,
            target_key,
        )
        return False
    return True


def notify_send_origin_discarded(
    state: "DashboardState",
    *,
    entry_meta: Any,
    target_slot: "_ChatSlot",
    text: Any,
    cause: str,
) -> bool:
    """Tell the SENDING session that a deliberate discard removed its queued delivery.

    The drain is not the only place a stamped entry leaves the queue unrun. A
    person cancelling its queue card (``api_chat_slot_queue_cancel``) and a hard
    stop clearing the queue (``stop_slot_turn``) discard it too, and the sender's
    last word on the message is still the ``started: False`` receipt that said it
    would run later. Every other sign of the discard lands on the target, which
    the sender does not read.

    *cause* is :data:`SEND_DISCARD_QUEUE_CANCEL` or :data:`SEND_DISCARD_HARD_STOP`
    and selects the wording: a deliberate discard changes no authorization, so the
    notice names what discarded the entry rather than a constraint.

    *entry_meta* is the discarded entry's own ``meta``, read here rather than by
    the caller so both discard sites read the stamp the way the drain does. The
    recipient is resolved exactly as :func:`notify_send_origin_dropped` resolves
    it, so the same four cases answer False without writing: no stamp, a
    self-send, a closed sender, and a key now held by a different tab.

    Best-effort: a failure is logged and the discard stands, because the person
    or the stop already decided it.
    """
    target_key = str(getattr(target_slot, "key", ""))
    origin = send_origin_slot(entry_meta)
    sender = _send_origin_recipient(state, origin, send_origin_tab(entry_meta), target_key)
    if sender is None:
        return False
    try:
        reason = _SEND_DISCARD_REASONS[cause]
        excerpt = send_drop_excerpt(text)
        sender.append(
            "notice",
            f"⚠️ Message you sent to {target_key} was discarded before it ran: "
            + reason
            + ". It was not delivered and will not run."
            + (f' Text: "{excerpt}"' if excerpt else ""),
            "msg msg-info",
        )
    except Exception:
        logger.exception(
            "Failed to report a discarded delivery to its sender (origin=%s, target=%s)",
            origin,
            target_key,
        )
        return False
    return True


def _send_origin_recipient(
    state: "DashboardState", origin: str, origin_tab: str, target_key: str
) -> "_ChatSlot | None":
    """The live sending slot a sender notice may be written to, or None.

    One resolver for both sender notices, so the four non-notice cases
    :func:`notify_send_origin_dropped` documents cannot hold on one discard path
    and not another: no stamp, a self-send, a closed sender, and a reused key
    whose current occupant is a different tab. An absent *origin_tab* never
    matches a live slot, so the identity check cannot be skipped by omitting it.
    """
    if not origin or origin == target_key:
        return None
    sender = state._slots.get(origin)
    if sender is None:
        return None
    if str(getattr(sender, "_tab_id", "") or "") != str(origin_tab or ""):
        return None
    return sender


def notify_channel_recipient_dropped(
    state: "DashboardState",
    *,
    entry_meta: Any,
    target_slot: "_ChatSlot",
    text: Any,
    constraints: list[str],
    mirror_unverified: bool = False,
) -> bool:
    """Tell the CHANNEL CONVERSATION that sent a message that the drain dropped it.

    The channel counterpart of :func:`notify_send_origin_dropped`. A message a
    channel handed to a resumed dashboard slot's queue
    (``dashboard.channel_handoff``) was confirmed "queued" in that conversation,
    and the conversation reads neither the target's transcript nor the SEL -- so
    without this the one outcome the author most needs, that the message will
    never run, is the one they are never told, and they wait for a reply that
    cannot come.

    Returns whether a notice was SCHEDULED: True once the entry carries a channel
    stamp and the task below exists. Answers False, and is not a failure, when the
    entry carries no stamp (a dashboard-typed or restored entry) or when no loop
    is running to carry the task. Whether the notice is then SENT is decided
    inside the task, and its refusals are logged and audited there.

    The send goes through the cross-surface ladder every proactive channel
    delivery takes (``chat_runner._resolve_channel_target``: channels governance,
    a registered transport that can send proactively, and the RECIPIENT
    re-check), with the principal the stamp recorded -- the platform user the
    channel authorized on inbound -- because a dashboard session key names no
    channel peer for the ladder to derive one from. A revoked recipient gets no
    notice; the refusal is audited by the ladder. The mirror pause is NOT
    consulted: this is a delivery receipt to the message's own author, not turn
    output, and the pause mutes output.

    Nothing of the ladder runs on the calling thread. The drain that drops the
    entry is synchronous on the event loop by contract (no suspension between its
    snapshot and the dequeue), so the send cannot be awaited there -- and the
    ladder's governance vet is the call every other async caller offloads,
    because it reads and validates policy on the shared loop. Both therefore run
    inside the scheduled task: the resolve through ``asyncio.to_thread``, the
    send awaited after it. The task is held in the state's background set so it
    cannot be collected mid-flight. Best-effort throughout: a failure is logged
    and the drop, which is the authorization decision, stands.

    The quoted excerpt is redacted through the same egress chain every channel
    delivery uses: the author typed the text, but this is a network write and the
    conversation may be read on a shared screen.
    """
    recipient = channel_recipient_of(entry_meta)
    if recipient is None:
        return False
    channel_type, conversation_id, principal = recipient
    # circular import: chat_runner imports this module's helpers at module level.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target
    from kiro_crew.dashboard.chat_utils import _redact_for_display

    link = ChannelLink(channel_type, channel_id=conversation_id)
    session_key = slot_history_key(target_slot)
    excerpt = _redact_for_display(sanitize_outbound(send_drop_excerpt(text)))
    notice = (
        "⚠️ Your queued message to that session was dropped before it ran: "
        + describe_containment_change(constraints, mirror_unverified=mirror_unverified)
        + " after it was queued, so the authorization that admitted it no longer "
        + "holds. It was not delivered and will not run; send it again if it still applies."
        + (f' Text: "{excerpt}"' if excerpt else "")
    )

    async def _send() -> None:
        try:
            target = await asyncio.to_thread(
                _resolve_channel_target, state, session_key, link, principal=principal
            )
        except Exception:
            logger.warning(
                "channel drop notice: send ladder failed for %s; the drop is not reported",
                channel_type,
                exc_info=True,
            )
            return
        if target is None:
            return
        resolved_link, transport = target
        try:
            await transport.send_message(
                resolved_link.channel_id, notice, thread_id=resolved_link.thread_id
            )
        except Exception:
            logger.warning("channel drop notice: send to %s failed", channel_type, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("channel drop notice: no running loop to carry the send to %s", channel_type)
        return False
    task = loop.create_task(_send())
    background = getattr(state, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)
    return True


def audit_queued_drop(
    slot: "_ChatSlot", queue_id: str, constraints: list[str], *, origin: str = ""
) -> None:
    """Record one drain-time drop in the SEL, best-effort and off the loop.

    Logged as a denied tool invocation on the TARGET's EFFECTIVE session — a
    linked slot's turns run under ``linked_session_key``, so filing under the
    slot key would hide exactly the drops this feature exists to record. The
    slot key stays in ``resources``/``metadata``.

    *origin* is the sending slot for a cross-session delivery, read from the
    entry's own stamp (:data:`SEND_ORIGIN_META_KEY`), so the trail names who was
    waiting on the dropped message. A human-typed entry carries no stamp and the
    field is omitted rather than recorded empty.
    """
    _audit_queue_drain(
        slot, outcome="denied", queue_ids=[queue_id], newly_held=constraints, origin=origin
    )


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
    slot: "_ChatSlot",
    *,
    outcome: str,
    queue_ids: list[str],
    newly_held: list[str] | None,
    origin: str = "",
) -> None:
    slot_key = str(getattr(slot, "key", ""))
    session_key = effective_session_key(slot)
    metadata: dict[str, Any] = {
        "target": slot_key,
        "queue_ids": ",".join(queue_ids),
    }
    if newly_held is not None:
        metadata["newly_held"] = ",".join(newly_held)
    if origin:
        # Omitted rather than recorded empty: absent means "no sending slot was
        # stamped" (a human typed it), which a reader must be able to tell from a
        # cross-session delivery whose sender happens to be unnamed.
        metadata["origin"] = origin

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


# The key prefix of an audience admission in ``_ChatSlot._steer_audience_fences``,
# so an admission and a peer steer's uuid-keyed entry cannot collide and a reader
# of the record can tell the two writers apart.
AUDIENCE_ADMISSION_KEY_PREFIX = "admission:"


def record_audience_admission(slot: Any, admission: dict[str, Any]) -> None:
    """Record *admission* on *slot* as the audience its current turn was admitted under.

    *admission* is the ``admission`` of the :class:`OwnerDmVerdict` the gate just
    judged -- the containment of the very row the clauses saw, in the queue-entry
    ``meta`` shape. This function takes it rather than probing the store itself, on
    purpose: a record built from a second read would describe whatever row was live
    at the second read, and a retarget landing between the two would be recorded as
    the admitted audience without having been judged -- the publisher, comparing the
    live row against that record, would then see no change and publish the private
    read into the new room. Handed the judged row, the record can only ever be the
    audience that was actually admitted.

    The read-side twin of the peer steer's record: the publisher compares the
    containment holding at reply delivery against every entry in
    ``_steer_audience_fences`` and withholds the cross-surface legs when a
    constraint newly holds -- a mirror gained, a mirror retargeted, a Slack thread
    bound beside it -- because a sent reply cannot be recalled. The channel-neutral
    leg (``chat_runner._deliver_cross_surface_reply``) judges the ONE binding it
    resolves its target from (:func:`publication_withheld`); the Slack legs ask
    ``cross_surface_withheld``. Written at the caller-side admission gates
    (:func:`refuse_caller_surface` for every targeted session-control verb,
    :func:`_refuse_ineligible_creator` for a creation, which passes no other gate,
    the work ledger's ``_caller_key`` for every ledger route) rather than per verb,
    so a read path cannot be added without it.

    ONE entry per distinct audience per turn: the key is the snapshot itself, so a
    conductor polling a worker every few seconds re-records the same audience as
    a no-op and the record stays the size of the number of DISTINCT containment
    states the turn passed through -- each of which is a real change the
    publisher must see -- never the number of calls. That is what bounds it
    without a cap, and a cap is exactly what this record must not have: evicting
    an admission would turn the fail-closed publication gate fail-open. The turn's
    teardown empties the record, so it is turn-scoped like the steer's entries.
    Keyed under :data:`AUDIENCE_ADMISSION_KEY_PREFIX` so it cannot collide with a
    steer's uuid key, which the steer path pops by name.

    Written ONLY inside a turn whose teardown clears it (:func:`_in_runner_turn`):
    the dashboard runner's ``_run_chat`` publishes ``_active_turn_session_key`` on
    the slot at turn start and retires it in the same ``finally`` that empties this
    record. A channel-born session's own turns run in the messaging driver, which
    sets no such marker and runs no such teardown -- so an owner-DM conductor that
    calls these tools from its channel turn would leave an entry that nothing
    clears, and a mirror change afterwards would make the publisher withhold the
    mirror leg of an unrelated dashboard reply on that slot, judged by an admission
    that was never about it. Nothing is lost by not recording there: the
    dispatcher publishes its own replies to the conversation the turn came from,
    and the runner's publisher, the only reader of this record, publishes only
    runner turns.

    A caller with no slot record of its own leaves nothing, which is the behaviour
    such a caller has today.

    NO OPT-OUT from the in-a-turn guard, because every caller is inside the turn its
    admission is about. A producer that merely has a turn PENDING cannot be: an entry
    nothing clears makes the publisher withhold the mirror leg of an unrelated later
    reply, judged by an admission that was never about it. The work-ledger wake is
    the one shape that could want it -- it delivers into a turn it starts -- and it
    carries a pointer to ``work_ledger_read`` rather than a board, so the read that
    needs an admission is the conductor's own call inside its own turn.
    """
    fences = getattr(slot, "_steer_audience_fences", None)
    if slot is None or not isinstance(fences, dict):
        return
    if not _in_runner_turn(slot):
        return
    key = AUDIENCE_ADMISSION_KEY_PREFIX + json.dumps(
        admission.get(QUEUED_CONTAINMENT_META_KEY, {}), sort_keys=True, default=str
    )
    if key not in fences:
        fences[key] = admission


def _in_runner_turn(slot: Any) -> bool:
    """Whether *slot* is inside a dashboard-runner turn -- one ``_run_chat`` is running.

    Read off ``_active_turn_session_key``, the identity ``_run_chat`` publishes on
    the slot once its turn is admitted and compare-and-clears in its teardown. It
    is the runner's own marker, so it names exactly the turns whose ``finally``
    empties ``_steer_audience_fences``: a channel turn driven by the messaging
    driver never sets it, and neither does the gap between two stages of a plan.
    Preferred over ``turn_running`` for that reason -- the task predicate also
    counts a stage controller that owns the slot while no runner turn, and no
    runner teardown, is in flight.
    """
    return bool(getattr(slot, "_active_turn_session_key", "") or "")


def publication_withheld(
    state: "DashboardState", slot: Any, binding: tuple[Any, str, str] | None
) -> bool:
    """Whether *slot*'s turn must NOT publish its reply to the audience *binding* names.

    The channel-neutral delivery leg's half of the audience fence. *binding* is the
    ONE :func:`_read_mirror_binding` that ``chat_runner._deliver_cross_surface_reply``
    resolves its target from, judged here against every admission the turn recorded
    in ``_steer_audience_fences`` -- :func:`record_audience_admission`'s, the peer
    steer's, the handoff's hold -- through the same comparison the Slack legs ask of
    ``cross_surface_withheld``: :func:`newly_held_constraints` over a snapshot built
    from that read (:func:`_snapshot_from_probe`). The decision and the delivery see
    one row. Judged from a second read, the decision would describe one row and the
    send would land in another whenever a retarget fell between them -- the
    mirror-link writer runs off the loop (``chat_mirror._claim_binding`` under
    ``asyncio.to_thread``), so nothing orders it against two reads on the loop --
    and a transcript the decision never saw would reach the new room. Handed the
    row the send resolves from, the judgement can only ever be about that room; a
    retarget landing after this read changes what the NEXT reply is judged against,
    not where this one goes.

    A binding of ``None`` is an unreadable store and is judged as an unverifiable
    mirror (``mirror_unverified``), which withholds whenever the turn recorded an
    admission: the drain's fail-closed reading, for the drain's reason. A slot with
    no record -- a caller outside a turn, a turn that admitted nothing -- withholds
    nothing, as it does today.
    """
    fences = getattr(slot, "_steer_audience_fences", None)
    if not fences:
        return False
    probed = None if binding is None else _mirror_identity_of(binding)
    now = _snapshot_from_probe(state, slot, probed, on_probe_failure=True)
    return any(newly_held_constraints(now, admission) for admission in fences.values())
