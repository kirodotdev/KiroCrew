"""Channel delivery for ``POST /api/chat/slots/{slot}/note`` visible lines.

``/note`` writes two halves: a visible transcript line and a ``_pending_context``
entry drained onto the next user message. The context half is already
surface-agnostic — ``drain_pending_context`` runs inside ``_run_chat``, the one
runner every inbound surface goes through, so a note reaches the model whether
the next message is typed in the dashboard, Slack or Telegram.

The visible half was not. ``slot.append(broadcast=True)`` fans out over the
dashboard's own SSE/WS and nothing else, and every channel egress in the turn
loop (the user echo, the tool stream, the assistant reply, an approval prompt, an
auth error) is a separate site the note path never reached. So a session driven
from a channel got an agent that silently knew something its user was never
shown: the note had no visible provenance on the surface they were reading. That
is worse than no delivery — a missing notice is noticed, invisible provenance is
not.

Delivery reuses the two existing outbound paths rather than adding a third:

* **Slack** — ``state.slack_client.post_message``. Slack is deliberately absent
  from the ``channel_transports`` registry so it cannot ride the ladder, and it
  is the one channel that can be bound two ways: as a session's own origin
  (a Slack-born thread) or as a dashboard slot's mirror (``_slack_linked``).
  Both resolve here.
* **every other channel** — ``handlers.messaging.deliver_to_channel``, the
  shared governed proactive send. It owns the ladder resolve, the ``channels``
  governance gate, the recipient re-authorization, ``display_safe_for``, the
  per-transport chunking, ``delivery_confirmed`` and the SEL audit. This module
  deliberately does NOT re-spell any of that: a second copy drifts, dropping the
  audit and the confirmation check —
  so the only thing kept here is the one decision the shared helper does not
  make, which is skipping a PAUSED binding and continuing the ladder.

Both legs can fire for one note: a dashboard slot can hold a Slack thread link
and a non-Slack mirror at once, and each is a conversation with a user in it.

Best-effort by construction. The transcript line and the context entry are the
note's contract; a channel that is unreachable, paused, ungoverned or incapable
of a proactive send is logged and swallowed rather than failing the POST.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from typing import Any

from kiro_crew.dashboard.chat_utils import slack_mirror_is_paused
from kiro_crew.dashboard.slack_egress import (
    EGRESS_TOOL_NAME,
    OP_SLACK_SEND,
    _deliver_slack_governed,
    audit_egress,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.link import SLACK_NAMESPACE
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.platform import redact_via_context

logger = logging.getLogger(__name__)

#: The default source every sourceless caller shares. A note carrying it has no
#: label worth showing a channel reader.
_DEFAULT_SOURCE = "note"

#: Per-CHUNK delivery bound. Each chunk costs its own governance walk plus one
#: network round trip, so this is the unit a stall is detectable in. A leg's total
#: budget is this multiplied by the number of chunks that leg will really send --
#: never a flat figure, because a flat figure shrinks as the note grows.
#:
#: THERE IS NO SEPARATE PER-LEG CONSTANT. An earlier revision carried
#: ``_LEG_TIMEOUT_S = 8.0`` beside this one, described as "the ONLY ceiling on the
#: mirror". Once the budget started scaling with chunk count that description was
#: false in both directions: nothing read the constant, and the real ceiling for a
#: multi-chunk note is far above eight seconds: a 40,000-character note is 40 chunks
#: at ``_FALLBACK_CHUNK_CHARS``, so 320 seconds. A constant that no longer bounds anything but still
#: states a bound is worse than no constant, because a reader sizing a timeout
#: around it gets a number the code has not honoured since it was introduced.
_CHUNK_TIMEOUT_S = 8.0

#: The ONE divisor both legs budget from. Below every cap in the tree, so it can only
#: over-count chunks, and over-budgeting is the safe direction for a leak bound.
_FALLBACK_CHUNK_CHARS = 1000


def _leg_parts(text: str, chunk_chars: int) -> int:
    """How many chunks one leg is PROJECTED to send for *text*, at *chunk_chars*.

    A projection, not a count: callers pass ``_FALLBACK_CHUNK_CHARS``, which sits
    below every transport cap in the tree, so this over-estimates on purpose. A leg
    that really chunks at the Slack limit sends far fewer parts than this returns.
    """
    return max(1, math.ceil(len(text) / max(1, chunk_chars)))


def _leg_budget(text: str, chunk_chars: int) -> float:
    """Total time one leg may take for *text*, at *chunk_chars* per chunk.

    Scales with the chunks the leg is PROJECTED to send, so every accepted chunk is
    covered and then some. There is no ceiling here on purpose: a cap applied to the
    total would stop the budget scaling past some length, and past that point it is a
    flat whole-leg deadline again -- cancelled mid-loop, prefix already posted.

    Used as the actual cancelling deadline for EVERY leg -- see ``_run_leg``, which
    explains why no arm is left unbounded and why the bound is scaled per chunk
    instead of being one flat whole-leg deadline.
    """
    return _CHUNK_TIMEOUT_S * _leg_parts(text, chunk_chars)


async def _run_leg(
    leg: str,
    session_key: str,
    coro: Coroutine[Any, Any, None],
    *,
    timeout: float,
    audit_timeout: Callable[[], None],
    audit_error: Callable[[], None],
) -> None:
    """Await one delivery leg under its OWN bound.

    Returns nothing, and neither do the legs. Whether a leg delivered is a fact
    only its own log line and SEL rows carry: this function's caller dispatches
    in the background and has no one to report to, so a return value here would
    be surface no production code can consume.

    Absorbs both a stall and a raise so neither can reach the caller, because the
    OTHER leg's delivery is still owed and because the endpoint dispatches this in
    the background -- an escaping exception there would surface only as an
    unhandled-task warning, never as anything a caller could act on. A stall and a
    raise are logged apart because they need different operator responses -- a live
    channel that stopped answering versus a composition or transport fault.

    *audit_timeout* and *audit_error* are supplied by the caller rather than chosen
    here, because the row belongs in the leg's OWN stream and only the caller holds
    that leg's identifiers. Branching on *leg* instead would put a second emitter in
    this module, which is the divergence ``audit_channel_send`` exists to avoid. Both
    endings file one, so neither terminates unaudited.

    SEQUENTIAL BY DESIGN, one leg after the other, and the reason is specific:
    running the legs concurrently makes both race the governance profile store's
    cold first load, which is fail-closed and DENIES the loser ("profile store not
    yet loaded (concurrent first load)"). Concurrency would therefore drop a real
    delivery on the first note after a restart -- one leg denied by nothing but
    the presence of its sibling.

    *timeout* BOUNDS THE LEG at ``_CHUNK_TIMEOUT_S`` PER PROJECTED CHUNK rather than
    by one flat deadline, and there is no arm that disables it: a leg runs in a
    background task, so one that never returns is never discarded from the
    dispatcher's task set, and because the legs are sequential a leg that hangs also
    means the OTHER leg is never reached -- the note goes nowhere and the process
    keeps the task for its lifetime. Repeated notes then accumulate them.

    Bounding a chunk loop can leave a delivered PREFIX when the deadline fires, and
    that cost is real: a reader seeing three of eight parts cannot tell it from a
    note that was only ever three parts long. It is accepted here because it is
    ALREADY the behaviour of both helpers a leg calls -- each aborts mid-send and
    returns failure when a binding is revoked between parts, leaving the parts
    already sent in place, and each says so in its own comment. So a partial note is
    a case this chain already handles and reports; a leg that never returns is not.

    Scaling the bound per chunk is what keeps it from being the flat deadline the
    earlier design rejected: a leg sending eight parts gets eight chunks' grace, so
    the bound only fires when a leg is genuinely not progressing rather than when it
    is merely long. That bound is the ONLY limit on a leg -- there is no second,
    up-front size refusal, because a capped note cannot project past it.
    """
    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        # A stall is a TERMINAL outcome, so it files a row like the others: without
        # one, the timed-out leg is the single ending with no trace in the SEL.
        audit_timeout()
        logger.warning(
            "note mirror: %s leg exceeded %.0fs for %s; reporting it undelivered",
            leg,
            timeout,
            session_key,
        )
    except Exception:
        # A raise is TERMINAL too, so it files a row for the same reason the stall
        # arm does: a leg ending with no trace breaks the one-row-per-ending parity.
        audit_error()
        logger.warning(
            "note mirror: %s leg failed for %s; reporting it undelivered",
            leg,
            session_key,
            exc_info=True,
        )


def _note_channel_text(content: str, source: str) -> str:
    """Render a note for a channel reader.

    Prefixed because an unlabelled line arriving in a thread reads as the agent
    talking, and a note is neither the agent nor the user — attributing it to
    either is the confusion this module exists to remove.

    Plain text, no markdown emphasis: a channel client may render none (the same
    call ``chat_compaction_notice`` makes for its notice copy), and every
    transport applies its own dialect conversion on the way out, so emphasis
    markers here are either ignored or rewritten.
    """
    label = f"note · {source}" if source and source != _DEFAULT_SOURCE else "note"
    return f"📝 [{label}]\n{content}"


async def mirror_note_to_channels(
    state: Any,
    slot: Any,
    session_key: str,
    content: str,
    source: str,
    *,
    slack_link: tuple[str, str],
    channel_link: Any,
) -> None:
    """Deliver a note's visible line to every channel *session_key* is bound to.

    *slack_link* is REQUIRED, not defaulted, and that is deliberate: it is the
    Slack destination captured by ``_snapshot_slack_link`` BEFORE this coroutine was
    handed to a background task. A default would let a caller silently fall back to
    resolving the link late, which is exactly the retargeting defect the snapshot
    exists to remove -- so the type system asks for it instead of trusting anyone to
    remember. *channel_link* is the same contract for the non-Slack leg, captured by
    ``snapshot_channel_link`` through the SAME pause-aware ladder the delivery walks,
    and required for the same reason: both legs deliver from a background task, so
    both need the binding they were AUTHORED for rather than whatever is live when
    the task finally runs.

    Returns nothing, because no caller consumes a delivery report: the endpoint
    does not report which channels were reached, so there is no reason for this
    function to answer at all. A channel that
    cannot be reached is logged and SEL-audited rather than reported -- delivery
    is best-effort by contract, and the note's contract is the transcript line and
    the context entry, which the caller has already committed.

    *content* arrives already redacted for the transcript sink; this re-redacts
    through the DISPLAY-form floor before egress. Not belt-and-braces: the
    transcript pass is a literal scan, and a markdown-collapse credential
    (``AKIA**...**``, which a client reassembles whole on screen) survives it.
    ``redact_via_context`` rather than the shared sink's default pair because it
    is platform-aware — a loaded companion's extra credential regexes apply. The
    per-leg mention defang is applied on top of this, never instead of it:
    ``display_safe`` would otherwise silently downgrade the scan to the OSS
    baseline.
    """
    if not content.strip():
        return
    text, _ = redact_for_display(_note_channel_text(content, source), redact_via_context)

    # EACH LEG IS BOUNDED INDEPENDENTLY. Awaiting them in sequence under one
    # SHARED budget made the two legs each other's failure domain in three ways,
    # every one of which loses a delivery that should have happened:
    #
    #   * a STALLED Slack send consumed the whole budget, so the transport leg
    #     never ran at all and the note was permanently absent from a healthy
    #     second channel;
    #   * a RAISING Slack leg did the same -- `channel_egress_permitted`
    #     re-raises `PlatformCompositionError` rather than degrading, and that
    #     propagated out of here before the transport leg was reached;
    #   * a stall in EITHER leg discarded this list, so a sibling that had already
    #     delivered went unreported. Losing the record of a real delivery is the
    #     same class of harm as not delivering: a caller acting on `[]` re-writes
    #     a note the channel has already shown.
    #
    # Still sequential, deliberately -- see `_run_leg` for why concurrency is the
    # wrong fix here. What changed is that each leg carries its own bound and
    # absorbs its own failure, so a stall costs one leg's budget instead of the
    # whole note's. That budget is `_leg_budget(sent, _FALLBACK_CHUNK_CHARS)` --
    # `_CHUNK_TIMEOUT_S` per PROJECTED chunk, so it scales with the note rather than
    # flattening as it grows. The endpoint dispatches this in the background and does
    # not await it, so there is no request deadline for the two bounds to fit inside.
    # A stall is logged. Nothing is collected, because nothing reads a collection,
    # and `_run_leg` swallows a stall and a raise so the FIRST leg's failure cannot
    # stop the second from running. Delivery-outcome auditing is now SYMMETRIC: both
    # legs record their terminal outcome to the SEL.
    # Both legs budget from ONE conservative divisor. Resolving each transport's real
    # cap bought precision no decision consumes: over-budgeting is the safe direction.
    # Projected on the DEFANGED form each leg actually chunks: counting the raw note
    # under-counts, which would grant a multi-chunk leg only one chunk's grace.
    sent = display_safe(text)
    budget = _leg_budget(sent, _FALLBACK_CHUNK_CHARS)

    # No up-front size refusal, and none is reachable: nothing declines a leg on its
    # projected size, and the per-chunk bound is the only limit either leg carries.
    def _audit_transport_timeout() -> None:
        # Lazy for the same cycle reason the delivery import below carries.
        from kiro_crew.dashboard.handlers.messaging import audit_channel_send

        link = channel_link[0] if channel_link else None
        audit_channel_send(
            session_key=session_key,
            tool_name=EGRESS_TOOL_NAME,
            channel_type=getattr(link, "channel_type", None),
            outcome="error",
            reason="leg_timeout",
        )

    def _audit_transport_error() -> None:
        from kiro_crew.dashboard.handlers.messaging import audit_channel_send

        link = channel_link[0] if channel_link else None
        audit_channel_send(
            session_key=session_key,
            tool_name=EGRESS_TOOL_NAME,
            channel_type=getattr(link, "channel_type", None),
            outcome="error",
            reason="leg_error",
        )

    await _run_leg(
        SLACK_NAMESPACE,
        session_key,
        _deliver_slack(state, slot, session_key, text, slack_link),
        # Always bounded, at `_CHUNK_TIMEOUT_S` per projected chunk rather than one
        # flat deadline; `_run_leg` carries why no arm is left unbounded.
        timeout=budget,
        audit_timeout=lambda: audit_egress(
            channel_id=slack_link[1] if slack_link else "",
            operation=OP_SLACK_SEND,
            session_key=session_key,
            outcome="error",
            reason="leg_timeout",
        ),
        audit_error=lambda: audit_egress(
            channel_id=slack_link[1] if slack_link else "",
            operation=OP_SLACK_SEND,
            session_key=session_key,
            outcome="error",
            reason="leg_error",
        ),
    )
    await _run_leg(
        "transport",
        session_key,
        _deliver_via_transport(state, session_key, text, channel_link),
        timeout=budget,
        audit_timeout=_audit_transport_timeout,
        audit_error=_audit_transport_error,
    )


def snapshot_note_destinations(state: Any, slot: Any, session_key: str) -> tuple[Any, Any] | None:
    """Both destinations a note is AUTHORED for, read with nothing awaited between.

    One function so the immediate and the held path cannot drift: a second copy is
    how one of them silently stops matching the pause-aware ladder the send walks.

    Returns None rather than raising, and the two callers reach here at DIFFERENT
    times, so the reason differs. The IMMEDIATE caller has already committed both
    halves, so a raise would 500 a request whose work is done and the client's retry
    would write both halves a SECOND time. The HELD caller snapshots BEFORE either
    half commits and before the note is even queued, so there is nothing to
    double-write; a raise there would fail the write outright over a best-effort
    provenance leg. Either way ``dispatch_note_mirror`` skips a None snapshot, so the
    note lands and only the channel copy is lost.
    """
    try:
        # LAZY BECAUSE HOISTING RAISES AT IMPORT TIME, the same cycle the delivery
        # imports carry. Measure it in the HOIST direction only.
        from kiro_crew.dashboard.handlers.messaging import snapshot_channel_link

        return (
            _snapshot_slack_link(slot, state, session_key),
            snapshot_channel_link(state, session_key, skip_paused=True),
        )
    except Exception:
        logger.warning("note mirror snapshot failed for %s", session_key, exc_info=True)
        return None


def dispatch_note_mirror(
    state: Any,
    slot: Any,
    session_key: str,
    content: str,
    source: str,
    destinations: tuple[Any, Any] | None,
) -> None:
    """Background the mirror for destinations ALREADY snapshotted. Never raises.

    *destinations* is taken by the caller rather than here, because the authoring
    moment is the only one whose bindings the note was authorized for: a held note
    dispatches at flush, and re-reading then would deliver into a rebind.

    Absorbs everything, registration included. A partially-constructed state carries
    no ``_background_tasks``, and reaching for one unguarded turns a best-effort leg
    into a load-bearing one.
    """
    if destinations is None:
        return
    try:
        slack_link, channel_link = destinations
        task = asyncio.create_task(
            mirror_note_to_channels(
                state,
                slot,
                session_key,
                content,
                source,
                slack_link=slack_link,
                channel_link=channel_link,
            )
        )
        # Strong reference for the same reason auto-title holds its own: an unheld
        # task can be garbage-collected while it is still running.
        background = getattr(state, "_background_tasks", None)
        if isinstance(background, set):
            background.add(task)
            task.add_done_callback(background.discard)
    except Exception:
        logger.warning(
            "note mirror dispatch failed for %s; the note itself is written",
            session_key,
            exc_info=True,
        )


def _snapshot_slack_link(slot: Any, state: Any, session_key: str) -> tuple[str, str]:
    """Capture the Slack coordinates a note is AUTHORED for, before dispatch.

    The mirror runs in a background task, so any link read inside it is read
    LATER than the note was written. A relink landing in that gap would make the
    deferred lookup select the replacement thread and expose a note authored for
    the previous one -- delivery to a recipient it was never authorized for. So
    the coordinates are captured HERE, synchronously, on the caller's side of the
    dispatch boundary, and the background task revalidates against this snapshot
    rather than resolving afresh.

    WHERE A SESSION STORE EXISTS, THE STORE IS THE ONLY SOURCE. A complete
    persisted link is the binding; anything else is UNBOUND, and the slot fields are
    not consulted. That is stricter than it first looks necessary, and the reason is
    that the two surfaces go stale in one direction only: several writers persist a
    new link WITHOUT touching the slot fields (``slack.handler._on_applied`` at the
    privacy-mode apply, the interactions handler, and the runner's relink), so a slot
    bound earlier keeps STALE attributes indefinitely. An earlier revision read the
    map first and fell back to those attributes when the row was absent or partial,
    which left a real hole: clear or reassign the persisted row and the fallback
    resurrects the superseded slot binding, posting the note into a conversation the
    session has moved off. Falling back cannot distinguish "not written yet" from
    "deliberately cleared", so it cannot be done safely at all.

    The cost is a dashboard slot linked in this process before its row is written:
    its note does not reach Slack. That is the same answer this function already
    gives when the lookup RAISES, and for the same reason -- a binding we cannot
    read is one we cannot claim to have authorized, and a background note that goes
    nowhere is strictly cheaper than one that reaches the wrong audience.

    Slot fields are used ONLY when there is no session store at all, where they are
    the sole binding that exists rather than the older of two.
    """
    thread_ts = getattr(slot, "_slack_thread_ts", "") or ""
    channel_id = getattr(slot, "_slack_channel", "") or ""
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return thread_ts, channel_id
    try:
        persisted_ts, persisted_channel = sessions.get_slack_link(session_key)
    except Exception:
        logger.debug("note mirror: slack link lookup failed for %s", session_key, exc_info=True)
        return "", ""
    if persisted_ts and persisted_channel:
        return persisted_ts, persisted_channel
    return "", ""


async def _deliver_slack(
    state: Any, slot: Any, session_key: str, text: str, slack_link: tuple[str, str]
) -> None:
    """Post the note into the Slack thread it was AUTHORED for.

    Returns nothing: the hardened chain already records every refusal and every
    authorization decision as a SEL row, and whether a permitted send then
    succeeded is a delivery fact its own log line carries. A bool here would be
    read by nobody -- ``mirror_note_to_channels`` dispatches both legs and has no
    caller waiting on an outcome.

    The hardened chain lives in ``_deliver_slack_governed`` beside
    ``channel_egress_permitted``, and this leg is its ONE consumer. Only
    ``channel_egress_permitted`` is shared with the compaction notice: that notice
    posts directly after the gate rather than running this chain, deliberately, so
    its refusal set is not widened as a rider on a note change. What is left here is
    only what is specific to a note: which coordinates it was authorized for, and
    which pause control applies.

    *slack_link* is the snapshot taken before dispatch. It is NOT re-resolved here:
    this function runs inside a background task, so resolving now would pick up a
    relink that landed after the note was written and deliver into a thread the
    note was never authorized for. ``relink`` still reads LIVE, but only to compare
    against this snapshot -- a mismatch is a refusal, never a retarget.
    """
    thread_ts, channel_id = slack_link
    # NO SLACK CONFIGURED AT ALL -> return silently, BEFORE the audit below.
    #
    # This check exists because the audit below cannot tell the two cases apart on
    # its own. On a Telegram/Discord-only install `_snapshot_slack_link` finds
    # neither slot attributes nor a persisted row, so it returns ("", "") for EVERY
    # session -- which would send every note through the denial branch below and
    # emit a `no_authored_slack_thread` row per note, deployment-wide, carrying no
    # per-session information. That is the flood `_deliver_slack_governed` already
    # exempts via its own missing-client branch, but that branch is UNREACHABLE from
    # here: this leg returns below without ever calling it. So the exemption has to
    # be made at this seam or it does not apply to the note leg at all.
    if getattr(state, "slack_client", None) is None:
        return
    # THE NOTE LEG'S OWN PRECONDITION, deliberately not in the shared helper: a
    # note mirrors into the THREAD the session is bound to, so a channel with no
    # thread is not a note destination. The compaction notice legitimately posts
    # top-level, so requiring a thread there would drop its notices -- which is why
    # the shared helper keeps only the namespace check and this stays local.
    if not (thread_ts and channel_id):
        # AUDITED, symmetrically with the transport leg's `no_authored_channel_link`.
        # Both legs are answering the same question -- "did THIS session have a
        # destination on this leg when the note was written" -- so auditing one and
        # returning silently from the other makes the denial stream look complete
        # while half of it is missing, and an operator asking "why did this note
        # reach nothing" gets an answer for one leg only.
        #
        # Reaching HERE now means Slack IS configured and this SESSION has no
        # authored thread -- genuinely per-session, which is what makes the row worth
        # writing. The deployment-wide case returned above.
        audit_egress(
            channel_id=channel_id or "unknown",
            operation=OP_SLACK_SEND,
            session_key=session_key,
            outcome="denied",
            reason="no_authored_slack_thread",
        )
        return
    # THE SCOPE BOUNDARY OF THE HARDENED CHAIN, NAMED HERE RATHER THAN ONLY IN A
    # REVIEW: this call is its only entry point, so this is the seam where a
    # reader learns what is NOT behind it.
    #
    # Tier membership is enumerated once, in the proactive-Slack-egress-consolidation
    # RFC, and pinned mechanically by this module's census test. Not restated here.
    #
    # Deferred here is the CHAIN, not mid-send revalidation: that is adopted for EVERY
    # caller, and its per-chunk driver is shared with `deliver_to_channel`.
    #
    # A future caller adopting this chain must accept that widening deliberately.
    await _deliver_slack_governed(
        state,
        session_key,
        text,
        thread_ts=thread_ts,
        channel_id=channel_id,
        relink=lambda: _snapshot_slack_link(slot, state, session_key),
        is_paused=lambda: slack_mirror_is_paused(state, session_key),
    )


async def _deliver_via_transport(
    state: Any, session_key: str, text: str, authored_link: Any
) -> None:
    """Send the note through the shared governed send.

    Returns nothing, and deliberately reports neither WHETHER nor WHERE: the
    channel type the ladder selected reaches the log line the ladder itself
    writes, and no caller collects an outcome.

    Everything is the shared helper's: the origin-then-mirror walk, the pause
    skip, the governance gate, the recipient re-authorization,
    ``display_safe_for``, the per-transport chunking, ``delivery_confirmed``, the
    post-await revalidation and the SEL audit. This function contributes only the
    ``skip_paused`` intent.

    It deliberately does NOT name a ``channel_type`` and does not pre-read the
    link. Reading the link here to label the result and passing that type in
    would break the case this pairing exists for: with a PAUSED
    origin on one transport and an ACTIVE mirror on another, the caller's
    pause-blind read names the origin's type while the pause-aware ladder selects
    the mirror, and the type-match guard then rejected the live destination. The
    ladder selects, and the ladder logs which row it chose.
    """
    # A CAPTURED None means the authoring slot had NO channel binding at all, and
    # that is refused HERE rather than inside the shared helper. The helper's walk
    # runs when the TASK runs, so passing None through would let it read a binding
    # created after authoring and treat that as the authorized destination -- an
    # unbound slot at authoring time is not a licence to deliver to a later arrival.
    # This is the only site that can produce that state, so the shared helper stays
    # two-state and needs no sentinel to tell "absent" from "captured None".
    if authored_link is None:
        # NO CHANNEL SURFACE AT ALL -> silent, BEFORE the audit: with an empty registry
        # this fires for every note on every session and carries nothing per-session.
        if not getattr(state, "channel_transports", None):
            return
        # LAZY BECAUSE HOISTING RAISES AT IMPORT TIME, the same reason the delivery
        # import below carries -- module scope here fails with `ImportError: cannot
        # import name 'DashboardState' from partially initialized module
        # 'kiro_crew.dashboard.state'`. Audited through the transport leg's OWN
        # emitter, not slack_egress's: this refusal is a channel-transport decision,
        # and a Slack-namespaced row would file it where an operator auditing this
        # leg will not look.
        from kiro_crew.dashboard.handlers.messaging import audit_channel_send

        # The permission decision is recorded even though no destination was
        # evaluated. A refusal that returns silently is indistinguishable in the
        # audit trail from a note that was never written, which is precisely the
        # gap that makes a denial stream trustworthy or not.
        audit_channel_send(
            session_key=session_key,
            tool_name=EGRESS_TOOL_NAME,
            channel_type=None,
            outcome="denied",
            reason="no_authored_channel_link",
        )
        logger.info(
            "note mirror: %s had no channel binding when the note was written; "
            "not delivering to a binding that appeared afterwards",
            session_key,
        )
        return

    # LAZY BECAUSE HOISTING RAISES AT IMPORT TIME -- not a preference, and not merely
    # a cost argument. Moving this to module scope fails outright:
    #
    #   ImportError: cannot import name 'DashboardState' from partially initialized
    #   module 'kiro_crew.dashboard.state' (most likely due to a circular import)
    #
    # via handlers.messaging -> dashboard/handlers/__init__.py -> handlers_system ->
    # dashboard.state, which is mid-initialisation by then. The `top-level-imports`
    # rule exempts exactly this case. Note the cycle is DIRECTIONAL and so is easy to
    # measure wrongly: importing `handlers.messaging` FIRST succeeds and never pulls
    # this module, so a probe in that direction reports "no cycle" and is useless --
    # the failing direction is the one a hoist actually creates. There is a boot cost
    # too (649 -> 1136 modules, which `test_perf_boot_path.py` ratchets), but the
    # ImportError is what makes this mandatory rather than advisable.
    from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

    await deliver_to_channel(
        state,
        session_key,
        text,
        skip_paused=True,
        tool_name=EGRESS_TOOL_NAME,
        authored_link=authored_link,
    )
