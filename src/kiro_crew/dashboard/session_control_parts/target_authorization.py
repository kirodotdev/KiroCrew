"""Whether a caller may address a target: the owner-DM judgement, a creator's
eligibility, slot resolution, the refusal factories and :func:`authorize_target`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    SessionControlError,
    _app_owned_cron_refusal,
    _caller_is_ownership_fenced,
    _channel_link_of,
    _created_by_other,
    _cron_caller,
    _member_bypass,
    _member_caller,
    _sel_off_loop,
    caller_slot_key,
    session_control_enabled,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    _has_channel_mirror,
    _meta_from_probe,
    _mirror_identity_of,
    _read_mirror_binding,
    record_audience_admission,
)
from kiro_crew.dashboard.state import load_window_off_loop
from kiro_crew.history import transcript_stem
from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, ChannelLink, parse_session_key
from kiro_crew.messaging.transport import DM_TARGET_PREFIX, sole_direct_target
from kiro_crew.security import redact
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


# The channels whose 1:1 DM session can be recognised as the configured owner's
# own conversation by :func:`owner_dm_refusal`. Membership asserts three facts
# that were VERIFIED against the transport, and a surface is added only by
# verifying all of them again for it:
#
# * the dispatcher mints its DM key as ``{surface}:{agent}:direct:{peer}``
#   (``build_dm_session_key`` with ``chat_type=direct``), so the key names the one
#   human in the conversation and a thread, group, forum or unified key does not
#   parse as one;
# * ``configured_targets()`` advertises exactly that peer as ``user:{peer}`` and
#   draws from CONFIGURED state alone -- never from identities learned off inbound
#   traffic, which is the gap ``constants.CHANNEL_OWNER_DM_NAMESPACES`` names for
#   Weixin and WeCom and the reason this set is a subset of it; and
# * the transport attests, from its own state, which person a DM conversation it
#   opened or received a message from belongs to (``direct_peer_of``), so a
#   dashboard-born slot's persisted mirror row -- a conversation id, never a
#   principal -- can be placed as the owner's DM or refused. Discord answers from
#   the ``create_dm_channel`` / inbound-DM pairing its client records; Telegram's
#   private ``chat_id`` is the peer.
#
# Every other channel fails closed here and keeps its full containment.
OWNER_DM_CONDUCTOR_SURFACES: frozenset[str] = frozenset({"discord", "telegram"})


ORIGIN_NOT_ON_RECORD = (
    "this conversation's origin is not on record -- the channel dispatcher records "
    "it on each inbound message and it is not kept across a gateway restart, so "
    "send a message from the DM and retry"
)
"""The refusal an owner DM meets between a gateway restart and its next inbound turn.

The origin (``SessionManager.get_origin_link``) is held in memory only, while the
slot and its mirror are persisted and re-surfaced at boot -- so a monitor-loop cycle
or a dashboard-tab turn that runs before the owner's next channel message finds
every other clause satisfied and this one not. Naming it keeps a caller from
hunting for a link it cannot clear; the exemption itself stays withheld, because a
mirror without the recorded origin cannot be told from a retarget.
"""


MIRROR_PEER_NOT_ON_RECORD = (
    "the channel does not place the mirror's conversation as anyone's 1:1 direct "
    "message -- it is not a DM this channel opened or received a message from (a "
    "room, a thread, a group, a chat outside its roster), or that record was lost "
    "with a gateway restart; if it is the owner's DM, send a message from it (or "
    "link it again) and retry"
)
"""The mirrored shape's counterpart of :data:`ORIGIN_NOT_ON_RECORD`.

A mirror row persists a conversation id and no principal, and the transport's
answer to "whose DM is this" (``direct_peer_of``) is a peer or nothing -- so this
one refusal covers the two things "nothing" can mean, which the predicate cannot
tell apart at this seam and does not pretend to: a conversation that is not a DM
the transport would place at all (a guild room or thread, a Telegram group or
forum, a chat outside the roster the transport opens DMs for), and a Discord DM
whose in-process pairing a gateway restart dropped, until the DM is re-opened or
its peer writes into it. The remedy is therefore stated for the DM it may be and
for nothing else. A DM the transport DOES place with someone other than the
roster's sole owner never reads this; it reads :data:`NOT_THE_SOLE_OWNER`.
"""


# The two clause reasons both shapes of :func:`owner_dm_refusal` share, spelled once
# so the channel-born and the mirrored dashboard-born slot are refused in the same
# words for the same missing fact.
NOT_THE_SOLE_OWNER = "the channel's roster does not name this conversation's peer as its sole owner"


SLACK_THREAD_BESIDE = "the session also mirrors to a Slack thread"


def _sole_owner(state: "DashboardState", surface: str) -> tuple[Any, str, str]:
    """``(transport, owner, "")`` -- *surface*'s LIVE transport and the one
    ``user:<id>`` target it names as owner (``""`` when it names none) -- or
    ``(None, "", why)`` when the clause cannot be established at all.

    The roster clause both shapes of :func:`owner_dm_refusal` walk, in one place:
    the transport is the roster in force NOW (reloaded live, the very set that
    admits the peer's turns), read in memory so the predicate stays callable from
    ``close_target``'s no-suspension re-check; an absent transport means the
    channel is not running and refuses; an unreadable roster refuses. The caller
    compares *owner* against the peer it established for its own shape.
    """
    transport = state.get_channel_transport(surface)
    if transport is None:
        return None, "", f"the {surface} channel is not running"
    try:
        owner = sole_direct_target(transport.configured_targets())
    except Exception:
        logger.debug("owner-DM check: %s targets unreadable", surface, exc_info=True)
        return None, "", f"the {surface} roster is unreadable"
    return transport, owner, ""


@dataclass(frozen=True)
class OwnerDmVerdict:
    """What ONE reading of a caller's outbound binding decides, as one object.

    ``refusal`` is the predicate's answer -- ``""`` when the slot is the configured
    owner's own DM (:func:`owner_dm_refusal` is this field alone). ``mirrored`` is
    the gates' fail-closed reading of the same row -- an unreadable store counts as
    mirrored -- for the ``mirrored_caller`` refusal, so a gate never re-probes the
    store to decide it. ``admission`` is the containment the verdict rests on, in the
    queue-entry ``meta`` shape (:func:`containment_meta`), built from the very row
    the clauses judged: the object :func:`record_audience_admission` stores, so the
    audience a gate admits and the audience it records cannot be two different rows.
    """

    refusal: str
    mirrored: bool
    admission: dict[str, Any]


def owner_dm_refusal(state: "DashboardState", slot: "_ChatSlot") -> str:
    """Why *slot* is not the configured owner's own DM -- ``""`` when it is.

    The refusal field of :func:`judge_owner_dm`, for callers that need the reason
    alone (the ledger's :func:`session_owner_dm_refusal`, the re-checks after an
    await). A GATE that admits on the answer must call :func:`judge_owner_dm` itself
    and record the verdict's ``admission``: the reason and the audience come from one
    read there, and this view discards the audience.
    """
    return judge_owner_dm(state, slot).refusal


def judge_owner_dm(state: "DashboardState", slot: "_ChatSlot") -> OwnerDmVerdict:
    """Judge *slot* against the owner-DM exemption on ONE read of its binding.

    The ONE predicate the three channel-containment gates consult -- the creator
    gate and the target gate here, the ledger gate in ``handlers/work_ledger.py``
    (through :func:`session_owner_dm_refusal`) -- so they cannot drift: a gate
    keying on the live link, one on the key prefix and one on the mirror store
    would each admit and refuse different slots, and a key prefix can never be
    cleared while a link can. That containment exists because a channel session
    acts on words from a thread other people are in, and what it reads lands in
    front of them. For a 1:1 DM whose only human is the operator, the "audience"
    being protected is the operator themself, and refusing it makes every Discord
    and Telegram conversation a session that can dispatch nothing.

    The answer is an :class:`OwnerDmVerdict`, and every field of it comes from ONE
    read of the slot's binding (:func:`_read_mirror_binding`): the clauses below
    judge that row, the ``mirrored`` flag is that row's fail-closed reading, and
    the ``admission`` the gates record is built from that row and no other
    (:func:`_meta_from_probe`). A gate that judged one read and recorded a second
    would leave a window between them -- a retarget landing there (a link handler
    on another thread) would be recorded as the admitted audience without ever
    having been judged, and the publisher, comparing the live row against that
    record, would find nothing changed and publish a privately-read transcript
    into the new room. Reading once closes the window by construction: whatever
    lands after the read is a difference against the record.

    Two shapes reach that audience, and the predicate admits both on the same
    positive facts -- a verified surface, a live roster naming exactly one owner
    who is the conversation's peer, and no wider room bound beside it -- differing
    only in where the conversation and its peer are read from:

    * a CHANNEL-BORN slot (``linked_session_key`` is a channel key): the DM the
      session lives in, whose key names the peer; and
    * a DASHBOARD-BORN slot whose OUTBOUND mirror (``SessionMap.get_mirror_link``)
      is such a DM -- a conductor tab the operator linked to their own phone. Its
      words come from the dashboard and its readers are the mirror's, so the
      audience is identical; what differs is that the mirror row persists a
      conversation id and no principal, so the peer is read off the transport's
      own record of the DM (``direct_peer_of``).

    Every clause is a positive fact, and the first one that cannot be established
    is the answer, so a gate that refuses can say which fact was missing without a
    second walk that could disagree with the first. Every reason is generic -- a
    surface name at most, never an id -- because it is rendered into the refusal
    the caller reads in its own channel. For the channel-born shape, in order:

    * The key parses under the canonical grammar as a DIRECT conversation with
      exactly one peer, on a surface in :data:`OWNER_DM_CONDUCTOR_SURFACES`. A
      thread, group or forum key names a wider audience; a ``unified`` bucket
      names no peer; the legacy two-segment Slack shape does not parse; a surface
      not verified for this fails closed by construction.
    * The channel's LIVE transport names exactly one owner and it is that peer:
      :func:`~kiro_crew.messaging.transport.sole_direct_target` over
      ``configured_targets()``, the same one-identity rule ``/sessions`` and the
      proactive owner DM apply, and the same reasoning -- an allow-list is a list
      of people permitted to talk to the agent, not a claim that any of them is
      the operator, so several entries name nobody. Read off the transport rather
      than the config record because it is the roster in force NOW (reloaded live,
      the very set that admits the peer's turns) and an in-memory read, which
      keeps this callable from ``close_target``'s no-suspension re-check. An
      absent transport means the channel is not running, and a session nobody can
      drive is not admitted on the strength of a stale key.
    * The outbound mirror, if any, IS the conversation the session lives in. The
      dispatcher binds the DM as its own mirror on every turn, and that mirror is
      the same audience -- but the dashboard can retarget a mirror at any thread
      or channel, and a retargeted DM republishes what it reads to people who
      are not the owner. So the mirror must equal the ORIGIN conversation the
      dispatcher recorded (``SessionManager.get_origin_link``, written on every
      inbound turn beside the mirror bind); an unknown origin, an unreadable
      store or a mirror that names anywhere else refuses. Compared as a whole
      :class:`ChannelLink` because the DM channel id is not the peer id on every
      surface (Discord's is the id ``create_dm_channel`` returned), so no
      derivation from the key could stand in for the recorded truth. An
      UNLINKED DM reads ``None`` here and is admitted: the dispatcher's first
      turn stamps the conversation's namespaced bucket into the legacy
      ``slack_channel_id`` field and ``clear_mirror_link`` pops only the
      ``mirror`` row, so ``!unlink`` / ``/unlink`` and the dashboard's
      mirror-unlink leave a threadless Slack row behind -- and
      ``SessionMap.get_mirror_link`` filters that row at the source (an empty
      ``thread_ts`` never enters Slack's thread index, so it is bookkeeping that
      names no audience) rather than handing every reader a Slack link nobody
      chose. This clause therefore carries no copy of that rule, and neither does
      ``bind_origin_mirror``. The converse row is a second audience the mirror
      read CANNOT see: ``get_mirror_link``
      returns the explicit ``mirror`` row whenever one exists and never looks at
      the Slack fields beside it, while the dashboard's slack-link writes its
      thread onto the slot's effective key -- this session, for a channel-born
      slot (``DashboardState.link_slack``) -- and the turn path posts every
      dashboard-driven reply into that thread straight off ``get_slack_link``.
      So the thread is read through ``get_slack_link`` as well, and a non-empty
      ``thread_ts`` refuses: the DM's mirror still equals its origin, and the
      Slack thread is a room full of people who are not the owner.

    For the dashboard-born shape, in order:

    * The slot is an attended chat tab -- a person's own, or one an agent created
      -- and not an unattended one (``UNATTENDED_SLOT_PREFIXES``). A cron tab's
      ``cron:`` link is not a channel link, so it reaches this shape with a plain
      key, and a cron slot is otherwise an admitted (fenced) session-control
      source; but the audience argument below rests on the owner typing the words
      the mirror republishes, which no scheduled run's tab satisfies, so such a tab
      keeps today's mirror refusal even when its mirror is the owner's DM.
    * The slot HAS an outbound mirror. An unmirrored dashboard tab is not the
      owner's DM -- it is nobody's channel conversation -- and the gates never
      consult this predicate for it, since they ask only about a linked or
      mirrored caller.
    * The mirror is on a surface in :data:`OWNER_DM_CONDUCTOR_SURFACES` and names
      no thread. The Slack mirror the store synthesizes for a threaded Slack row
      fails the first test; a Telegram forum topic fails the second. A Discord
      thread carries its snowflake as the channel id with no thread id, so it is
      caught by the peer clause instead: a guild room is never on record as
      anyone's DM.
    * The channel's LIVE transport names exactly one owner -- the same roster
      clause, through the same call -- and the transport places the mirror's
      conversation as a DM with exactly that person (``direct_peer_of``). The
      record it answers from is the one the transport writes when it opens the DM
      (the dashboard link's ``resolve_configured_target``) or when an authorized
      message arrives from it; on Discord that record lives in this process, so
      a mirror linked before a gateway restart reads as unplaced until the DM is
      re-opened or its peer writes into it, and a mirror aimed at a room reads the
      same, since a room is never anyone's DM (:data:`MIRROR_PEER_NOT_ON_RECORD`
      names both causes and the remedy for the DM it may be; a DM the transport
      places with someone other than the sole owner reads
      :data:`NOT_THE_SOLE_OWNER` instead). Nothing is derived from the
      conversation id itself: whether it equals the peer is a per-platform fact
      only the transport knows.
    * No Slack thread is bound beside the mirror row, read through
      ``get_slack_link`` for the reason given above -- the same clause, on the
      slot's own effective key.

    There is no origin clause for this shape because there is no origin: the slot
    was born in the dashboard, and the dashboard is where its words come from. The
    retarget the channel-born origin clause catches is caught here by the peer
    clause -- a mirror moved to a room or to a stranger's DM stops naming the sole
    owner -- and a retarget while a session-control call is in flight is judged at
    the next call, since every gate consults the CURRENT row and never an earlier
    admission (the ledger re-checks after each read it awaits across); what such a
    call already read is covered by the audience the gate recorded from THIS read,
    which the publisher compares against the row live at delivery.

    What this deliberately does NOT establish is unfenced reach for the channel-born
    shape: an admitted DM is creator-fenced by :func:`_caller_is_ownership_fenced`,
    so a wrong inference costs the sessions the DM created and never the person's
    own tabs. Nor does it change the fence for the dashboard-born shape, in either
    direction: a person's own tab keeps the reach it had before the link and an
    agent-created tab stays fenced by its ``_created_by`` mark (see the fence's own
    notes on why the mirror is not a fifth population). Group and thread sessions,
    every other channel, every other mirror, and
    ``channel.CHANNEL_AGENT_BLOCKED_TOOLS`` are untouched.
    """
    sessions = getattr(state, "sessions", None)
    binding: tuple[Any, str, str] | None
    probed: str | None
    if getattr(sessions, "get_mirror_link", None) is None:
        # No store to ask: "no mirror" without touching the slot, exactly as
        # :func:`_probe_channel_mirror` answers (a duck-typed slot on the queue
        # path). The clauses still refuse the shape that needs the store.
        binding, probed = None, ""
    else:
        try:
            key = slot_history_key(slot)
        except Exception:
            logger.debug("mirror-link probe failed", exc_info=True)
            key = None
        binding = None if key is None else _read_mirror_binding(sessions, key)
        probed = None if binding is None else _mirror_identity_of(binding)
    return OwnerDmVerdict(
        refusal=_owner_dm_clauses(state, slot, sessions, binding),
        mirrored=True if probed is None else bool(probed),
        admission=_meta_from_probe(state, slot, probed),
    )


def _owner_dm_clauses(
    state: "DashboardState",
    slot: "_ChatSlot",
    sessions: Any,
    binding: tuple[Any, str, str] | None,
) -> str:
    """The clauses of :func:`judge_owner_dm`, judged on the read it took.

    *binding* is the slot's ``(mirror row, Slack thread, Slack channel)`` from
    :func:`_read_mirror_binding`, ``None`` when the store could not answer -- the
    ONLY reading of the mirror this function sees, so its verdict and the audience
    recorded beside it describe one row. The origin (channel-born shape) is read
    here: it is a validation input the record does not carry, not an audience.
    """
    link = _channel_link_of(slot)
    if not link:
        # ── The dashboard-born shape: judged on its outbound mirror row. ──
        if str(getattr(slot, "key", "")).startswith(UNATTENDED_SLOT_PREFIXES):
            # A cron tab's ``cron:`` link is stripped above, so it lands here with
            # a plain key -- and a cron slot IS admitted as a session-control
            # source (fenced to what it created). What this shape admits is the
            # PERSON's tab, whose words the owner types; a scheduled run's tab
            # mirrored to the owner's DM keeps the mirror refusal it meets today,
            # and a workflow result tab is refused as a source before this runs.
            return "the session is unattended, not a person's dashboard tab"
        if sessions is None:
            return "the session store is unavailable"
        if binding is None:
            return "the session store is unreadable"
        mirror, slack_thread, _slack_channel = binding
        if mirror is None:
            return "the session is neither channel-born nor mirrored"
        surface = str(getattr(mirror, "channel_type", "") or "")
        if surface not in OWNER_DM_CONDUCTOR_SURFACES:
            return "the outbound mirror is not on a verified owner-DM surface"
        if getattr(mirror, "thread_id", None):
            return "the outbound mirror names a thread, not a 1:1 direct message"
        transport, owner, why = _sole_owner(state, surface)
        if why:
            return why
        try:
            peer = str(transport.direct_peer_of(str(getattr(mirror, "channel_id", "") or "")) or "")
        except Exception:
            logger.debug("owner-DM check: %s peer lookup failed", surface, exc_info=True)
            return f"the {surface} channel cannot place the mirror"
        if not peer:
            return MIRROR_PEER_NOT_ON_RECORD
        if not owner or owner != f"{DM_TARGET_PREFIX}{peer}":
            return NOT_THE_SOLE_OWNER
        if slack_thread:
            return SLACK_THREAD_BESIDE
        return ""
    # ── The channel-born shape: judged on the conversation it lives in. ──
    parsed = parse_session_key(link)
    if parsed is None:
        return "the session key does not name a channel conversation"
    if parsed.surface not in OWNER_DM_CONDUCTOR_SURFACES:
        return f"{parsed.surface} is not a verified owner-DM surface"
    if parsed.chat_type != CHAT_TYPE_DIRECT or len(parsed.scope) != 1:
        return "the conversation is not a 1:1 direct message"
    _transport, owner, why = _sole_owner(state, parsed.surface)
    if why:
        return why
    if not owner or owner != f"{DM_TARGET_PREFIX}{parsed.scope[0]}":
        return NOT_THE_SOLE_OWNER
    if sessions is None:
        return "the session store is unavailable"
    if binding is None:
        return "the session store is unreadable"
    try:
        origin = sessions.get_origin_link(link)
    except Exception:
        logger.debug("owner-DM check: session store unreadable for %s", link, exc_info=True)
        return "the session store is unreadable"
    mirror, slack_thread, _slack_channel = binding
    if not isinstance(origin, ChannelLink):
        return ORIGIN_NOT_ON_RECORD
    if slack_thread:
        return SLACK_THREAD_BESIDE
    if mirror is None or (isinstance(mirror, ChannelLink) and mirror == origin):
        return ""
    return "the outbound mirror points somewhere other than this conversation"


def session_owner_dm_refusal(state: "DashboardState", session_key: str) -> str:
    """:func:`owner_dm_refusal` for a caller known only by its session key.

    The ledger gate holds an ``X-Session-Key`` and no slot, so it resolves the
    slot the way every session-control verb does -- :func:`caller_slot_key`, the
    identity ``list_sessions`` reports -- and judges THAT slot. Resolving through
    the same function is what makes "the ledger gate and session control agree on
    the same slot" true by construction rather than by two lookups happening to
    coincide; a key that resolves to no open slot is refused, as
    :func:`authorize_target` refuses an unidentifiable caller.
    """
    slot_key = caller_slot_key(state, session_key)
    slot = state.get_slot(slot_key) if slot_key else None
    if slot is None:
        return "the session key resolves to no open slot"
    return owner_dm_refusal(state, slot)


def _refuse_moved_caller_identity(
    state: "DashboardState",
    caller_key: str,
    caller_slot: "_ChatSlot",
    identity: tuple[str, str, str],
) -> None:
    """Refuse a caller whose memory identity differs from *identity*.

    *identity* is the ``(history key, agent, memory store)`` triple
    :func:`create_session` captured before its first suspension point; the live
    slot is re-read and compared here, at every later point that derives a
    verdict from the caller. A caller that is gone, or whose key has been
    re-minted onto another session, is refused as not open; one that survived
    but changed what it is -- a channel link landing on it changes its history
    key, a reassignment changes its store or agent -- is refused as the identity
    change it is, so the decisions taken on the earlier identity are never
    applied to the new one and the refusal names the cause rather than whichever
    downstream gate happened to read the new identity first.
    """
    live = state.get_slot(caller_key)
    if live is None or live is not caller_slot:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    if (slot_history_key(live), live.agent, live.memory_store) != identity:
        raise SessionControlError(
            "caller session changed memory assignment while the session was being created",
            code="caller_memory_changed",
        )


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
    # The channel link and mirror refusals share ONE exemption with
    # `authorize_target`'s caller half: :func:`owner_dm_refusal` answering ``""``,
    # a 1:1 DM whose only human is the configured owner -- the DM the session lives
    # in, or the DM a dashboard tab mirrors to -- with no wider room bound beside
    # it. It waives both together, because the predicate has already established
    # that the mirror IS the DM -- waiving the link alone would refuse every owner
    # DM on the origin mirror its dispatcher binds each turn. The refusal names the
    # clause that failed: the code stays the same, but a DM that lost its origin to
    # a gateway restart, or a mirror whose peer this process holds no record of, is
    # told to send a message rather than left hunting for a link it cannot clear.
    # Both refusals are decided from the ONE read the verdict took: its ``mirrored``
    # flag is that row's fail-closed reading, so the gate never probes the store a
    # second time to decide what the first read already saw.
    verdict = judge_owner_dm(state, caller_slot)
    if why := verdict.refusal:
        if _channel_link_of(caller_slot):
            # A cron tab's link is its own run transcript, not a channel thread,
            # and is exempt -- see CRON_LINK_PREFIX. Everything else is a channel
            # link.
            raise SessionControlError(
                "channel-linked sessions cannot create sessions; the owner-DM "
                f"exemption is withheld because {why}",
                code="linked_session_caller",
            )
        if verdict.mirrored:
            raise SessionControlError(
                "sessions mirrored to a channel cannot create sessions; the owner-DM "
                f"exemption is withheld because {why}",
                code="mirrored_caller",
            )
    # Admitted. A creation's result -- the child's key and title -- becomes part of
    # the caller's reply exactly as a read does, and ``create_session`` and a
    # ``fork_session`` of the caller's own transcript never pass ``authorize_target``,
    # so this is the ONE gate such a turn passes: record the audience here too, from
    # the row this verdict judged. Applied twice per creation (entry and allocation),
    # the second write is the no-op the audience-keyed record makes it.
    record_audience_admission(caller_slot, verdict.admission)


def _resolve_slot(
    state: "DashboardState",
    target: str,
    *,
    candidates: "list[_ChatSlot] | None" = None,
) -> "_ChatSlot | None":
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

    ``candidates`` restricts every form, including direct key lookup, to a set the
    caller already proved safe to name. Broadcast uses that form so resolving a
    guessed private title cannot reveal the slot key that carries it.
    """
    found: list[_ChatSlot] = []
    pool = list(state._slots.values()) if candidates is None else list(candidates)

    def _add(candidate: "_ChatSlot") -> None:
        if not any(c is candidate for c in found):
            found.append(candidate)

    slot = state.get_slot(target)
    if slot is not None and any(candidate is slot for candidate in pool):
        _add(slot)
    for candidate in pool:
        try:
            if transcript_stem(slot_history_key(candidate)) == target:
                _add(candidate)
        except Exception:
            continue
    wanted = target.strip().casefold()
    if wanted:
        for candidate in pool:
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


async def load_target_window(state: "DashboardState", target: str) -> None:
    """Load the pending window of the live session *target* names, off the loop.

    For a verb whose gate and delivery read the target's rows with no suspension
    between them: it runs before that verb's prewarm, the last suspension ahead
    of the gate. It resolves *target* as the gate does and decides nothing, so a
    target the gate then refuses has had only the load its next reader would do.
    """
    target = (target or "").strip()
    if not target:
        return
    try:
        slot = _resolve_slot(state, target)
    except SessionControlError:
        return
    if slot is not None:
        await load_window_off_loop(slot)


def _broadcast_resolution_slots(
    state: "DashboardState",
    *,
    caller_key: str,
    caller_slot: "_ChatSlot",
    ownership_fenced: bool,
) -> "list[_ChatSlot]":
    """Live slots whose names *caller_key* may safely resolve before delivery.

    This mirrors :func:`authorize_target`'s target containment clauses without
    replacing that gate: every retained key is authorized again at delivery.
    Building the set BEFORE reading a candidate's title prevents an ownership-
    fenced caller from using title resolution to learn a private slot key: a name
    outside the set stays the caller's own string all the way into its refusal row,
    so a guessed title is never answered with the slot key that carries it.

    THE KEY IS WHAT THIS WITHHOLDS, NOT EXISTENCE. The unresolved name is still
    delivered to :func:`authorize_target`, whose ``_resolve_slot`` call is
    UNRESTRICTED -- so a guessed title that happens to match a slot the caller may
    not touch comes back as that slot's containment class (``not_creator``,
    ``mirrored_target``, ``ephemeral_target``, ``app_scoped_target``,
    ``workspace_mismatch``) rather than ``target_not_found``, and the difference
    tells the caller something is there. That is not a leak this function opened
    and not one it can close: ``session_send`` on the same name answers identically,
    which is the documented contract ("this can reach nothing a session_send
    could not"). The codes are also the only guidance a caller gets about a target
    it named legitimately, so collapsing them here would cost a conductor the
    reason its own worker is unreachable. The narrow property this set buys is the
    one that matters for a guessing caller: a refusal row never hands back a
    durable handle it did not already hold.
    """
    out: list[_ChatSlot] = []
    for slot in list(state._slots.values()):
        if slot.key == caller_key or slot.key.startswith(UNATTENDED_SLOT_PREFIXES):
            continue
        if getattr(slot, "memory_mode", "persistent") != "persistent":
            continue
        if getattr(slot, "_app", "") or getattr(slot, "linked_session_key", ""):
            continue
        if _has_channel_mirror(state, slot):
            continue
        if getattr(slot, "workspace", "default") != getattr(caller_slot, "workspace", "default"):
            continue
        if ownership_fenced and _created_by_other(slot, caller_key):
            continue
        out.append(slot)
    return out


#: The refusal factory's product: it RETURNS the error to raise rather than raising,
#: so the audit write happens exactly once at the point of refusal and a caller
#: collecting per-target outcomes can record one without aborting its own loop.
Deny = Callable[..., SessionControlError]


def _deny_factory(*, caller_session_key: str, operation: str, target: str) -> Deny:
    """Build the ``deny`` used by every gate in this module.

    Lifted out of :func:`authorize_target` so the verbs that have NO target
    (:func:`created_session_status`) refuse through the same audited path rather
    than growing a second one. ``target`` is the audit's ``resources`` field and is
    empty for a targetless verb, which reads in the trail as "this refusal was not
    about a particular session".
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

    return deny


def refuse_caller_identity(
    state: "DashboardState",
    *,
    caller_session_key: str,
    deny: Deny,
    skip_enabled_check: bool = False,
) -> str:
    """The caller-side gate that runs BEFORE any target is resolved.

    Returns the caller's slot key. Extracted from :func:`authorize_target` so the
    targetless verbs share ONE copy of these three refusals rather than a second
    sequence that can drift from this one; every check and every code is the same
    text it was inline, and :func:`authorize_target` still calls it at the same
    point in its own order, so no precedence the suite pins has moved.

    Why these three are on this side of the resolution is the existence-oracle
    argument the ``_app_owned_cron_refusal`` comment gives: a caller refused for
    its OWN identity must learn nothing from the attempt, and a gate that resolves
    first answers ``target_not_found`` (404) for a session that does not exist and
    a 403 for one that does.
    """
    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        # Without a resolved caller the self-target guard is blind, and a session
        # that can reach every peer while being unidentifiable is exactly the
        # shape this surface must not have.
        raise deny("caller session could not be identified", "caller_unidentified")
    # Resolved before the config gate: a member DM session is authorized
    # WITHOUT `agent.session_control` — dispatching and patrolling workers is
    # its operating model — while the operator ceiling `agent.member_dispatch`
    # (default true = today's behaviour) is on. Turn that ceiling off and the
    # member falls back under the switch. The member's reach stays bounded by
    # the ownership check below, which restricts it to slots it created itself.
    if (
        not skip_enabled_check
        and not session_control_enabled()
        and not _member_bypass(state, caller_key)
    ):
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
        raise deny(refusal[0], refusal[1])
    return caller_key


def refuse_caller_surface(
    state: "DashboardState",
    *,
    caller_key: str,
    deny: Deny,
) -> "_ChatSlot":
    """The caller-side gate about the caller's own SURFACE. Returns its slot.

    The caller's own isolation gates it too, and for the same reasons the target's
    does: an incognito or temporary session is one the user asked to leave no
    trace, and an app-scoped session belongs to its app. Either one reaching a
    persistent peer would launder content across the boundary it was created to
    have — in the direction the target-side checks cannot see.

    A second helper rather than one with :func:`refuse_caller_identity`, because
    :func:`authorize_target` runs these two on OPPOSITE sides of its target
    resolution and merging them would move a refusal's precedence. A targetless
    verb resolves nothing, so it calls both back to back.
    """
    # The slot-field half lives in :func:`_check_caller_slot_fields` so a
    # synchronous last-word check can re-assert it adjacent to a publish
    # (``revive_session``'s ``final_check``); the two channel refusals stay here
    # because their owner-DM exemption reads the session store.
    caller_slot = _check_caller_slot_fields(state, caller_key, deny)
    # The one exemption from both caller-side channel refusals below, shared with
    # `_refuse_ineligible_creator` so the two halves cannot drift on WHO is exempt:
    # :func:`owner_dm_refusal` answering ``""``, a 1:1 DM whose only human is the
    # configured owner -- the DM the session lives in, or the DM a dashboard tab
    # mirrors to -- with no wider room bound beside it. It waives both refusals
    # together, because it has already established that the mirror IS the DM --
    # waiving the link alone would refuse every owner DM on the origin mirror its
    # dispatcher binds each turn. An admitted channel-born DM is creator-fenced
    # further down; an admitted dashboard tab keeps the fence verdict it had. The
    # refusal names the clause that failed, code unchanged. ONE read: the verdict's
    # ``mirrored`` flag and the ``admission`` recorded below come from the row the
    # clauses judged, so nothing here probes the store a second time.
    verdict = judge_owner_dm(state, caller_slot)
    if why := verdict.refusal:
        if _channel_link_of(caller_slot):
            # The exfiltration direction, and the reason this is not merely the
            # mirror of the target-side check: a linked caller's own conversation
            # is a channel thread, so anything it reads lands in front of whoever
            # is in that channel. `session_read_message` would hand a private
            # dashboard transcript to Slack/Discord readers who were never party
            # to it.
            #
            # `CHANNEL_AGENT_BLOCKED_TOOLS` already blocks these tools for channel
            # AGENTS, but that guard keys on the agent identity; a linked SLOT is
            # a second route to the same surface and has to be closed on its own.
            #
            # A cron tab's link is exempt because it is not a channel: it names
            # the job's own run transcript and republishes to nobody, so a read
            # through it reaches no audience the caller did not already have. See
            # CRON_LINK_PREFIX.
            raise deny(
                "channel-linked sessions cannot control other sessions; the owner-DM "
                f"exemption is withheld because {why}",
                "linked_session_caller",
            )
        if verdict.mirrored:
            # The exfiltration direction again, via the outbound mechanism: a
            # mirrored caller republishes its own turns to a channel, so a peer's
            # transcript it reads lands in front of that channel's audience.
            raise deny(
                "sessions mirrored to a channel cannot control other sessions; the "
                f"owner-DM exemption is withheld because {why}",
                "mirrored_caller",
            )
    # Admitted. What this verb returns -- a peer's transcript, the roster of
    # created sessions and their titles, a delivery receipt -- becomes part of the
    # caller's reply, which the caller's own turn publishes to its mirror LATER,
    # resolved live. Record the audience it was admitted under -- the row the
    # verdict judged, not a fresh read -- here at the one gate every verb passes
    # rather than per verb, so no read path can miss it.
    record_audience_admission(caller_slot, verdict.admission)
    return caller_slot


def _check_caller_slot_fields(
    state: "DashboardState",
    caller_key: str,
    deny: "Callable[..., SessionControlError]",
) -> "_ChatSlot":
    """The caller-slot refusals answerable from the slot's own fields, no store.

    Split out so a synchronous last-word check can re-assert them adjacent to a
    publish (``revive_session``'s ``final_check``); the two channel refusals stay
    in :func:`refuse_caller_surface`, because their owner-DM exemption reads the
    session store.
    """
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
    return caller_slot


def _live_target_refusal(slot: "_ChatSlot") -> tuple[str, str] | None:
    """The target-side containment refusal a LIVE slot earns, or ``None``.

    One spelling of the four fields that decide whether a live session may be
    reached at all -- the unattended prefix, memory mode, app scope and channel
    link -- shared by :func:`authorize_target` (on the resolved target) and
    :func:`revive_session` (on the slot the resume hydrated), so the two cannot
    drift. The outbound-mirror probe and the workspace compare sit beside it in
    each caller: the first needs the state and a thread choice, the second the
    caller's slot.

    A channel-linked session's conversation is mirrored to Slack/Telegram, so
    reaching it crosses a surface boundary in both directions: a message would
    surface to whoever reads that thread, and a read would pull the channel's
    content back. It is also the one target whose STOP cannot be honoured: the
    stop path addresses the session as ``dashboard:<slot>`` while a linked slot's
    turns actually run under its ``linked_session_key``, so the cancel would miss
    and the target would keep executing after a reported success. Refusing is the
    honest answer until the stop path resolves the effective key.
    """
    if slot.key.casefold().startswith(UNATTENDED_SLOT_PREFIXES):
        return ("unattended sessions (scheduled runs) cannot be controlled", "unattended_target")
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        return ("incognito and temporary sessions are not addressable", "ephemeral_target")
    if getattr(slot, "_app", ""):
        return ("app-scoped sessions are not addressable", "app_scoped_target")
    if getattr(slot, "linked_session_key", ""):
        return ("channel-linked sessions are not addressable", "linked_session_target")
    return None


def _not_creator_reason(
    state: "DashboardState",
    caller_key: str,
    caller_slot: "_ChatSlot",
    precomputed_ownership_fenced: bool | None,
) -> str:
    """The wording of an ownership-fence refusal; see the note in authorize_target.

    The cron prefix and the channel link are readable without config; the inline
    path keeps the per-class wording since it is already reading config anyway.
    """
    if _cron_caller(caller_key):
        return "a scheduled run can only control sessions it created itself"
    elif _channel_link_of(caller_slot):
        return "an owner-DM channel session can only control sessions it created itself"
    elif precomputed_ownership_fenced is not None:
        return "this session can only control sessions it created itself"
    elif _member_caller(state, caller_key):
        return "a crew member can only control worker sessions it created itself"
    else:
        return "an agent-created session can only control sessions it created itself"


def authorize_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    operation: str,
    skip_enabled_check: bool = False,
    precomputed_ownership_fenced: bool | None = None,
    allow_self: bool = False,
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

    ``precomputed_ownership_fenced`` is an ownership-fence verdict the caller of
    this function already holds, honoured instead of re-deriving one here. Two
    callers hold one:

    * The HTTP gate (``handlers/session_control.py``'s ``_private_caller_refusal``)
      admits a crew member on its VERIFIED private scope and passes ``True`` down
      through every route. The inline fence (:func:`_caller_is_ownership_fenced`
      → :func:`_member_caller` → :func:`_store_is_member_owned`) re-reads the
      MUTABLE config record, and an operator's own writer can flip that record —
      un-assign the member, drop ``memory_version`` (coerced to ``1`` by the
      loader), drop the entry — in the awaits between the gate and this call. A
      member admitted as one must stay bounded to what it created for the whole
      request, so the verified decision travels with the request rather than
      being recomputed from whatever the record says at the fence.
    * ``close_target`` resolves the verdict ONCE up front (behind
      ``prewarm_enabled_check``) and passes it to both its initial gate and its
      SYNCHRONOUS point-of-no-return re-check, so no ``KiroCrewConfig.load()`` runs
      on the loop inside ``close_slot``'s no-suspension window — the same
      blocking-IO hazard ``skip_enabled_check`` closes for the switch read.

    When ``None`` (an owner or agent-created caller the gate did not admit as a
    member) the fence is evaluated inline as before.

    ``allow_self`` waives the self-target refusal, and with it the ownership fence for
    that one case. Three verbs pass it. A release, where the target itself is a
    legitimate caller because a session taken over must not depend on its holder still
    running to get out. A close, so a session that has finished its one job can archive
    its own tab instead of leaving it for the person or its creator to dismiss; the close
    is the same recoverable archival a peer close is, and it cancels the caller's own
    running turn, which is the turn asking for it. And a rename, because a session naming
    itself acts on nothing but its own sidebar label. It waives nothing else -- an ephemeral, app-scoped or
    channel-linked caller is still refused, and a target that is not the caller is
    still judged by every rule above.
    """

    deny = _deny_factory(caller_session_key=caller_session_key, operation=operation, target=target)

    caller_key = refuse_caller_identity(
        state,
        caller_session_key=caller_session_key,
        deny=deny,
        skip_enabled_check=skip_enabled_check,
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

    if slot.key == caller_key and not allow_self:
        raise deny("a session cannot control itself", "self_target")
    if (refusal := _live_target_refusal(slot)) is not None:
        raise deny(refusal[0], refusal[1])
    if _has_channel_mirror(state, slot):
        # Same boundary as the channel-link refusal, reached by the other
        # mechanism: an outbound mirror republishes this session's turns to a
        # channel, so a read would pull that channel's content back and a stop
        # would act on a conversation other people are party to.
        raise deny("sessions mirrored to a channel are not addressable", "mirrored_target")

    caller_slot = refuse_caller_surface(state, caller_key=caller_key, deny=deny)

    if getattr(slot, "workspace", "default") != getattr(caller_slot, "workspace", "default"):
        # Workspaces are the memory boundary; reaching across one would let a
        # session act on work it cannot see.
        raise deny("target session belongs to a different workspace", "workspace_mismatch")
    # Resolve the fence verdict ONCE. A caller passing ``precomputed_ownership_fenced``
    # already holds it — the HTTP gate's verified member admission, or
    # ``close_target``'s up-front pass — so honour that value rather than
    # re-deriving it from the config record here (see the docstring). Everyone
    # else evaluates it inline.
    ownership_fenced = (
        _caller_is_ownership_fenced(state, caller_key)
        if precomputed_ownership_fenced is None
        else precomputed_ownership_fenced
    )
    # A caller addressing ITSELF is not reaching a peer, so the fence has nothing to
    # protect and is waived -- reachable only under ``allow_self``, since the
    # self-target refusal above denies this case for every other verb. Without the
    # waiver an agent-created session could never release itself from a parent,
    # because its own ``_created_by`` names its creator and not itself.
    self_addressed = slot.key == caller_key
    if ownership_fenced and not self_addressed and _created_by_other(slot, caller_key):
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
        #
        # The reason is cosmetic (the error string only). A carried verdict says
        # WHETHER the caller is fenced, not WHY, and telling the member wording
        # from the agent-created wording needs ``_member_caller``, which can read
        # config — the ``close_target`` re-check runs in a no-suspension window and
        # MUST NOT reach it. So on a carried verdict the text names the rule
        # rather than a class it cannot see; the cron prefix is still readable
        # without config, and the inline path keeps the three-way wording since
        # it is already reading config anyway.
        fence_reason = _not_creator_reason(
            state, caller_key, caller_slot, precomputed_ownership_fenced
        )
        raise deny(fence_reason, "not_creator")

    return slot
