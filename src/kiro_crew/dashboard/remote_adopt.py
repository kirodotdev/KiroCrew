"""Adopt an EXISTING peer session into a fresh local slot (teleport-to-local).

``create_peer_slot`` MINTS a session on a crew and binds a new local slot to it.
Adopt is the other direction: the peer already holds the conversation — it is a
row the user is looking at in the merged Sessions list — and the local slot is
created to *reach* it. The bind is the same post-create stamp either way
(``executor``/``instance_id``/``remote_slot`` in ``api_chat_slot_create``), so
everything downstream of it (``relay_remote_turn``, the sidebar, the hub-driven
dedupe in ``read_peer_slots``) is untouched by this module.

Two things adopt owes that a mint does not, and they are the two halves of this
file:

**Validation.** The peer key arrives from the CALLER, so it is untrusted. It is
checked against :func:`~kiro_crew.dashboard.handlers_instances.read_peer_slots` —
the same read the sidebar renders — which makes the check free of new policy: a
key absent from that view is forged, closed, or a slot this hub already drives,
and none of the three is adoptable. It is also where the metadata the local slot
inherits comes from (``agent``, ``title``, ``memory_mode``), so nothing the
caller sends decides what the adopted session claims to be.

**Backfill.** A minted peer session is empty, so the local transcript starts
empty and stays honest. An adopted one is not: without a backfill the user opens
a session whose history is invisible, then sends a turn the peer answers WITH
that history — a transcript that disagrees with the conversation. So the peer's
messages are copied in once, at birth.

Backfill is deliberately BEST-EFFORT (:func:`backfill_adopted_slot` never
raises). A slot that opens with a notice instead of its history is usable; a
create that 502s because the transcript read timed out leaves the user with
nothing, having already validated the target. The trust boundary is unchanged by
any of this: a remote-bound slot's local transcript already holds relayed peer
rows by ``relay_remote_turn``'s design, and every row copied here goes through
the same :func:`~kiro_crew.dashboard.remote_relay.redact_peer_text` sink those do.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.chat_utils import _redact_deep
from kiro_crew.dashboard.handlers_instances import PeerSlotsUnavailable, read_peer_slots
from kiro_crew.dashboard.remote_relay import (
    RemoteTurnError,
    _require_manager,
    ensure_version_parity,
    redact_peer_text,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: The wire code for "that is not a session you can adopt". One code for a forged
#: key, a closed session and a slot this hub already drives, for the reason the
#: create handler's app-token refusal gives: a distinct code per reason would turn
#: this into an existence oracle for sessions on the peer.
ADOPT_TARGET_UNKNOWN = "adopt_target_unknown"

#: The wire code for "the crew did not tell us this session's memory mode". Its own
#: code rather than a reuse of the one above: unlike a forged key, this names a
#: readable session the hub is DECLINING to open, so the client can say why and a
#: retry against an upgraded peer can succeed. No oracle concern -- it reveals only
#: that a row lacked a field the peer already sends for its own sessions.
ADOPT_PEER_MODE_UNKNOWN = "adopt_peer_mode_unknown"

#: Byte ceiling on the peer's transcript reply, bounding what is BUFFERED before
#: anything is decoded — the discipline every peer read on this boundary follows.
#: Generous rather than tight because the decision (plan B4) is FULL history: a
#: multi-MB session is the case this is for, not the case it refuses. Meta carries
#: the tool inputs and outputs, which is most of the volume.
PEER_TRANSCRIPT_REPLY_MAX_BYTES = 16 * 1024 * 1024

#: Row ceiling after decoding, bounding what is then PROCESSED (each row runs the
#: redaction chain). Under ``state._MAX_SLOT_MESSAGES`` (10000) on purpose: rows
#: past the local window cap would be trimmed off the head by ``slot.append``
#: anyway, so copying them buys nothing and costs a redaction pass each. The TAIL
#: is kept when this binds — the recent turns are the ones the next turn is about.
PEER_TRANSCRIPT_MAX_ROWS = 4000

#: The tail size asked for when the full read came back oversized. The peer clamps
#: ``limit`` to 500 (``api_chat_slot_detail``), so this is the largest honest ask.
PEER_TRANSCRIPT_FALLBACK_LIMIT = 500

#: Roles copied into the local transcript verbatim.
#:
#: ``thinking`` is included because the peer keeps its own copy of those rows and
#: ``_apply_row`` relays them for a live turn — excluding them here would make an
#: adopted transcript diverge from the same session's relayed continuation.
_BACKFILL_ROLES = frozenset(
    {"user", "assistant", "thinking", "tool_call", "tool_result", "system", "error"}
)

#: Roles DROPPED, each for its own reason:
#:
#: ``chunk``/``done`` are wire-only and the peer's own projection already removed
#: them (``_collapse_wire_rows``); if a peer on another build sends them anyway
#: they are stream bookkeeping, not transcript.
#:
#: ``permission`` is the one that is a judgement rather than a formality. It
#: renders an approval bar, and an adopted one has no local future to resolve — the
#: buttons would answer ``404 no pending approval`` on a card the user cannot
#: dismiss. The record of the approval is not lost from the conversation: the
#: ``tool_call``/``tool_result`` pair it gated is copied. This is the same
#: pending-approval gap ``relay_remote_turn`` already has (plan E1), not a new one.
_BACKFILL_SKIP_ROLES = frozenset({"chunk", "done", "permission"})

#: The role a still-streaming segment arrives as. The peer's projection folds a
#: run of ``chunk`` rows into one row under this name, and it appears ONLY while a
#: turn is in flight over there — finalizing the segment drops those rows and
#: appends the finished ``assistant`` message instead (``_finalize_streamed_segment``).
#:
#: It is DROPPED rather than mapped to ``assistant``. Mapping it kept the visible
#: text of an in-flight turn, which reads like the better trade until you follow
#: what happens next: this hub never mirrors a turn it did not start, so the
#: suffix never arrives, and the frozen snapshot is then re-serialized into the
#: local transcript file where nothing distinguishes it from a reply that really
#: ended there. The user is left with a truncated answer presented as a complete
#: one, permanently, with no way to tell. Dropping it loses the same text but says
#: so in the transcript (``_BACKFILL_IN_FLIGHT_NOTICE``, carried in the same leading
#: notice row as the truncation one), which is the difference between an absence and
#: a falsehood.
#:
#: Refusing the adopt outright was the other option and is worse: adopting a
#: session the peer is actively answering in is a normal, supported case, so a
#: refusal would break the feature to avoid the bug.
_STREAMING_ROLE = "streaming"


#: The three notices a backfill can leave in the transcript. Text, not codes:
#: nothing branches on them, the user reads them where the missing history would
#: have been. Named so the tests pin the CONDITION rather than the wording.
#:
#: Each NAMES the machine rather than saying "the crew". Two reasons, both from a
#: blind read of the shipped strings: "crew" already names the autonomous-agent
#: feature elsewhere in the product, so it reads as the wrong noun here; and a
#: notice that does not say WHICH machine leaves the user with no way to act on it
#: when several are connected. The name is the same one the sidebar badge shows.
def _backfill_failed_notice(peer: str) -> str:
    return (
        f"This session's earlier history could not be copied from {peer}. "
        f"The conversation continues there, and {peer} still answers with it."
    )


def _backfill_truncated_notice(peer: str) -> str:
    return f"Older messages in this session stay on {peer} and are not copied here."


def _backfill_in_flight_notice(peer: str) -> str:
    return (
        f"{peer} is still answering in this session. That reply is not copied here; "
        f"it finishes on {peer}."
    )


class AdoptTargetUnknown(Exception):
    """The supplied peer key is not a session this hub may adopt."""


def adopted_slot_for(state: "DashboardState", instance_id: str, remote_slot: str) -> Any:
    """The local slot already bound to ``(instance_id, remote_slot)``, or ``None``.

    Called TWICE on the adopt path, and the two calls answer different questions.

    The FIRST runs before the peer is read at all, so the common retry — a double
    click on the same peer row — is answered without a tunnel round-trip or a
    second transcript copy.

    The SECOND runs immediately before the slot is created, after every ``await``
    on the path, and it is the one that actually decides. Two concurrent identical
    POSTs both clear the first check, because ``resolve_adopt_target`` and
    ``fetch_adopted_backfill`` each suspend: without the recheck they would mint
    two local slots bound to ONE peer session, which is not merely a duplicate row
    — each accumulates its own turns, so the two transcripts diverge and
    ``read_peer_slots`` filters the peer's own row on whichever binding it happens
    to see. Nothing between the recheck and ``get_or_create_slot`` awaits, and
    asyncio runs one task at a time, so check-and-create is atomic there without
    taking a lock.

    ``is_remote`` requires the WHOLE binding, so a half-written slot never matches
    and can never make a genuine adopt look like a duplicate.
    """
    return next(
        (
            slot
            for slot in state._slots.values()
            if slot.is_remote
            and slot.instance_id == instance_id
            and slot.remote_slot == remote_slot
        ),
        None,
    )


async def resolve_adopt_target(
    state: "DashboardState", instance_id: str, remote_slot: str
) -> dict[str, Any]:
    """Return the peer's OWN row for *remote_slot*, or refuse.

    Raises :class:`AdoptTargetUnknown` when the key is not in the peer's live
    list, and :class:`RemoteTurnError` when the list could not be read at all —
    the two outcomes the contract maps to ``404 adopt_target_unknown`` and
    ``502 remote_bind_failed``.

    Version parity is asserted first, exactly as ``create_peer_slot`` does: an
    adopted session's turns run through the same relay, so a peer that cannot
    carry one must be refused here rather than after a slot exists pointing at it.

    ``row_cap=None`` because this read does no per-row work: the route's cap
    exists to bound the redaction it runs over every surviving row, and applying
    it here would make a real key on a peer with many open sessions look forged.
    The byte cap inside the read still bounds memory.
    """
    mgr = await _require_manager(state)
    await ensure_version_parity(mgr, instance_id)
    try:
        peer = await read_peer_slots(state, instance_id, row_cap=None)
    except PeerSlotsUnavailable as e:
        # Every read failure collapses to ONE outcome for the caller: from the
        # adopt side "the crew could not be asked" is a single condition however
        # the read failed, and the create handler already owns a code for it.
        # The peer's own code survives in the log line, not on the wire.
        logger.info("Adopt target read on %s failed (%s)", instance_id, e.code)
        raise RemoteTurnError(
            "Could not reach that crew to open its session. Reconnect it and try again."
        ) from None
    for row in peer.rows:
        if isinstance(row, dict) and row.get("key") == remote_slot:
            return row
    raise AdoptTargetUnknown("that session is not open on this crew")


def peer_row_metadata(row: dict[str, Any]) -> dict[str, str]:
    """The fields an adopted local slot inherits from the validated peer row.

    Read off the PEER's row rather than the request body: the peer already chose
    the agent, already named the session, and — the one that matters — already has
    a ``memory_mode``. That mode is the user's privacy boundary, so an adopted
    session must carry the peer's, not this machine's default; defaulting it would
    let a session the user opened as ``incognito`` over there start writing memory
    the moment it was opened here.

    Every value is bounded and pushed through the peer-text sink, because these
    land on a local slot (and its sidebar row) as strings the peer authored. An
    unrecognised ``memory_mode`` is dropped rather than coerced — the create
    handler validates the mode it is given, and inventing one here would smuggle
    a value past that validation.
    """
    out: dict[str, str] = {}
    agent = row.get("agent")
    if isinstance(agent, str) and agent:
        out["agent"] = redact_peer_text(agent)[:128]
    title = row.get("title")
    if isinstance(title, str) and title:
        out["title"] = redact_peer_text(title)[:200]
    mode = row.get("memory_mode")
    if isinstance(mode, str) and mode in ("persistent", "incognito", "temporary"):
        out["memory_mode"] = mode
    return out


async def _read_peer_transcript(
    state: "DashboardState",
    instance_id: str,
    remote_slot: str,
    *,
    limit: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """One bounded read of the peer's transcript for *remote_slot*.

    ``limit=0`` asks for the FULL history: ``api_chat_slot_detail`` returns the
    whole chained corpus when neither ``limit`` nor ``before`` is supplied, and a
    non-zero *limit* is the bounded-tail fallback.

    Raises :class:`RemoteTurnError` for every failure, including an oversized
    reply — the caller decides whether that is fatal (it is not; see
    :func:`backfill_adopted_slot`).
    """
    mgr = await _require_manager(state)
    params = {"limit": str(limit)} if limit else None
    try:
        async with mgr.proxy_request(
            instance_id,
            "GET",
            f"api/chat/slots/{remote_slot}",
            params=params,
        ) as upstream:
            if not 200 <= upstream.status < 300:
                raise RemoteTurnError(
                    f"The crew refused to send that session's history (HTTP {upstream.status})."
                )
            # Accumulated to EOF rather than one ``read(cap + 1)``, for the reason
            # ``read_peer_slots`` carries at length: ``StreamReader.read(n)``
            # returns as soon as ANY data is buffered, so a single call yields a
            # PREFIX of a multi-chunk body — and a transcript is the read most
            # likely to span many chunks, so a single read would have failed the
            # HEALTHY case. Refused the moment the cap is crossed so the bound
            # holds against an unbounded reply.
            chunks: list[bytes] = []
            received = 0
            async for chunk in upstream.content.iter_chunked(65536):
                received += len(chunk)
                if received > PEER_TRANSCRIPT_REPLY_MAX_BYTES:
                    raise RemoteTurnError("The crew's session history was too large to copy.")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except RemoteTurnError:
        raise
    except Exception as e:
        # Only the exception TYPE is logged, following every other peer read here:
        # never the partial body (untrusted peer bytes) and never the credential.
        # ``CancelledError`` is a BaseException and is deliberately not absorbed.
        logger.info(
            "Transcript read for %s on %s failed (%s)", remote_slot, instance_id, type(e).__name__
        )
        raise RemoteTurnError("Could not read that session's history from the crew.") from None
    try:
        # Off the event loop, for the same reason `prepare_backfill_rows` is: this
        # decodes up to PEER_TRANSCRIPT_REPLY_MAX_BYTES (16 MiB) of peer JSON, and
        # a long adopted history is an explicitly supported case rather than an
        # abuse. Parsing it inline stalled every other client — including unrelated
        # sessions' turns — for the whole parse, and it was the BIGGER of the two
        # costs while only the cheaper redaction pass was being offloaded.
        payload = await asyncio.to_thread(json.loads, raw)
    except ValueError:
        raise RemoteTurnError("The crew returned a malformed session history.") from None
    if not isinstance(payload, dict):
        raise RemoteTurnError("The crew returned a malformed session history.")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        # An empty transcript is a LIST, so a non-list here is a shape fault, not
        # an empty session. Treated as a failure so the notice explains itself
        # rather than the slot silently opening blank.
        raise RemoteTurnError("The crew returned a session history with no messages.")
    # The peer says there is MORE than it sent. Its own log has rotated (or the
    # window is paginated), so what arrived is a tail, not the conversation. This
    # is truncation the peer imposed rather than truncation we chose, and it is
    # invisible from the rows alone -- every row present looks complete. Reported
    # through the same notice as our own caps, because the user's question is the
    # same either way: is this all of it? Silently persisting the tail as the whole
    # history is the one outcome that answers that question wrongly and forever.
    peer_has_more = payload.get("has_more") is True
    return [row for row in messages if isinstance(row, dict)], peer_has_more


def prepare_backfill_rows(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, bool]:
    """Map peer transcript rows to local append arguments. Pure, CPU-bound.

    Returns ``(rows, dropped_older, in_flight)`` — the second is how many of the OLDEST rows
    the row cap discarded, which the caller turns into a notice so a truncated
    history is attributable instead of looking like the whole conversation.

    Pure on purpose: it runs in a worker thread (see :func:`backfill_adopted_slot`)
    because the redaction battery over a whole history is exactly the loop-blocking
    cost ``api_chat_slot_detail`` already offloads for the same reason. Keeping the
    slot out of it is what makes that safe — the appends happen on the loop.

    Redaction happens HERE, before any row reaches ``slot.append``: that call both
    stores the row in the window and hands it to the ``ConversationLog``, so a pass
    applied afterwards would already have persisted the raw text. The peer redacts
    its own copy through this same chain, which makes the pass idempotent in the
    healthy case and is why it is cheap enough not to depend on the peer having run
    it.
    """
    kept: list[dict[str, Any]] = []
    in_flight = False
    for row in messages:
        role = row.get("role")
        if not isinstance(role, str) or not role:
            continue
        if role in _BACKFILL_SKIP_ROLES:
            continue
        if role == _STREAMING_ROLE:
            # Dropped, not mapped to ``assistant`` — see ``_STREAMING_ROLE``. The
            # flag is what turns the drop into a visible notice rather than a
            # silently missing reply.
            in_flight = True
            continue
        if role not in _BACKFILL_ROLES:
            # An unknown role from a peer on another build is not appended as a
            # transcript row: nothing local knows how to render it, and the local
            # window is re-serialized into a real transcript file, so an
            # unrecognised role would be persisted as one.
            continue
        content = row.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content)
        # Redacted for every role, INCLUDING ``user``. The local rule leaves
        # user-authored text raw because its author is its only reader — but this
        # text was authored on another machine and arrives over a wire, so the
        # relay's rule applies instead: peer bytes are redacted on this side too.
        content = redact_peer_text(content)
        cls = row.get("cls", "")
        # The class rides the same stored row and the same broadcast, so a peer
        # that puts a credential here reaches every surface the text would.
        cls = redact_peer_text(cls) if isinstance(cls, str) else ""
        ts = row.get("ts", "")
        if not isinstance(ts, str):
            ts = ""
        meta = row.get("meta")
        if isinstance(meta, dict):
            meta = _redact_deep(meta)
            # Keep the durable tool correlation the peer stored but DROP its
            # ``mid``: that is a per-gateway row delivery id, and adopting the
            # peer's would collide with the local mid space. Same rule as
            # ``_apply_row``.
            meta = {k: v for k, v in meta.items() if k != "mid"} or None
        else:
            meta = None
        kept.append({"role": role, "content": content, "cls": cls, "ts": ts, "meta": meta})
    dropped_older = max(0, len(kept) - PEER_TRANSCRIPT_MAX_ROWS)
    if dropped_older:
        kept = kept[dropped_older:]
    return kept, dropped_older, in_flight


class AdoptBackfill(NamedTuple):
    """A prepared backfill: the rows to append, and one notice to append first.

    Split from the append so the NETWORK read and the redaction pass happen
    outside ``api_chat_slot_create``'s ``suspend_slots_push`` block, which is
    process-wide — every other client's slot updates coalesce while it is held, so
    a transcript read inside it would defer them for the length of a peer
    round-trip. What runs inside the suspension is :func:`apply_adopted_backfill`,
    which is list appends.

    ``notice`` is ``""`` when the whole history was copied. It is deliberately not
    an error CODE: nothing branches on it, it is text the user reads in the
    transcript at the point where the missing history would have been.
    """

    rows: list[dict[str, Any]]
    notice: str


async def _peer_display_name(state: "DashboardState", instance_id: str) -> str:
    """The name the sidebar badge shows for this crew, or the id if unknown.

    NEVER raises and never returns empty. Every caller is building user-facing
    notice text on a path whose whole contract is that it does not fail the adopt,
    so a registry that cannot be read degrades to the instance id -- the same
    degradation the sidebar badge makes -- rather than propagating.

    Off-loop because the registry read fsyncs, which is the rule every registry
    touch in these handlers follows.
    """
    from kiro_crew.dashboard.handlers_instances import _registry

    try:
        reg = _registry(state)
        for inst in await asyncio.to_thread(reg.list):
            if inst.id == instance_id:
                return redact_peer_text(str(inst.name or ""))[:128] or instance_id
    except Exception:
        logger.debug("Peer name lookup for %s failed; using the id", instance_id, exc_info=True)
    return instance_id


async def fetch_adopted_backfill(
    state: "DashboardState", instance_id: str, remote_slot: str
) -> AdoptBackfill:
    """Read and prepare the adopted session's history. NEVER raises.

    Non-fatal by contract (plan B5): the slot is validated and about to be bound,
    and it is fully usable with no history — the peer holds the conversation and
    answers the next turn with it either way. Failing the create instead would
    take a working session away from the user over a read that timed out, so every
    failure becomes a notice IN the transcript, where the user can see what is
    missing and why.

    An oversized full read falls back to a bounded TAIL rather than giving up
    (plan B4): a prefix of an over-cap JSON document cannot be parsed, so the tail
    has to be asked for separately, and the recent turns are worth a second
    round-trip.
    """
    truncated_to_tail = False
    peer_has_more = False
    # Resolved ONCE, before any read, so every notice below names the same machine
    # the sidebar badge shows. Degrades to the instance id rather than failing:
    # a notice naming an id is still actionable, while a notice that says nothing
    # is the defect being fixed. Off-loop for the reason every registry touch in
    # these handlers is -- the read fsyncs.
    peer = await _peer_display_name(state, instance_id)
    try:
        try:
            messages, peer_has_more = await _read_peer_transcript(state, instance_id, remote_slot)
        except RemoteTurnError:
            messages, peer_has_more = await _read_peer_transcript(
                state, instance_id, remote_slot, limit=PEER_TRANSCRIPT_FALLBACK_LIMIT
            )
            truncated_to_tail = True
    except RemoteTurnError as e:
        logger.info("Adopt backfill for %s on %s failed: %s", remote_slot, instance_id, e)
        return AdoptBackfill([], _backfill_failed_notice(peer))
    except Exception:
        # A bug in the copy must not fail an otherwise-good adopt either.
        logger.warning("Adopt backfill for %s raised", remote_slot, exc_info=True)
        return AdoptBackfill([], _backfill_failed_notice(peer))

    try:
        # Off-loop for the reason ``api_chat_slot_detail`` offloads the same work:
        # the redaction battery over a whole history is loop-blocking on a
        # multi-MB session, and this one also runs ``_redact_deep`` over every
        # row's meta, which is where the tool payloads are.
        rows, dropped_older, in_flight = await asyncio.to_thread(prepare_backfill_rows, messages)
    except Exception:
        logger.warning("Adopt backfill shaping for %s raised", remote_slot, exc_info=True)
        return AdoptBackfill([], _backfill_failed_notice(peer))

    logger.info(
        "Prepared %d backfill rows for %s on %s (tail_only=%s, older_dropped=%d)",
        len(rows),
        remote_slot,
        instance_id,
        truncated_to_tail,
        dropped_older,
    )
    # Both conditions can hold at once — a long session the peer is still
    # answering in — and they describe different missing text, so neither may
    # shadow the other. Joined into ONE notice row because that is what the
    # transcript renders where the missing history would have been.
    notices = [
        text
        for text, on in (
            (
                _backfill_truncated_notice(peer),
                bool(dropped_older or truncated_to_tail or peer_has_more),
            ),
            (_backfill_in_flight_notice(peer), in_flight),
        )
        if on
    ]
    return AdoptBackfill(rows, " ".join(notices))


def apply_adopted_backfill(slot: "_ChatSlot", backfill: AdoptBackfill) -> int:
    """Append *backfill* to *slot*'s window on the event loop. Returns rows landed.

    ``broadcast=False`` is the sanctioned replay door — the restore, fork,
    transfer and window-rebuild paths all use it — and it is required here twice
    over: the slot is not yet visible to any client (the create handler holds
    ``suspend_slots_push``), and broadcasting would push meta straight out.
    ``mint_mid=False`` for the same reason those paths pass it: these are
    historical rows, not newly delivered ones.

    Deliberately leaves the slot DIRTY. The create handler's existing
    ``save_slot_off_loop(..., force=True)`` — already unconditional for a bound
    create — is what persists them, so nothing here marks the window as
    already-on-disk (``_disk_window_len``): saying so before the save would tell
    the next flush these rows need no writing.

    The notice goes FIRST, so it reads as a header on the history that follows
    rather than a footer on the conversation.
    """
    if backfill.notice:
        slot.append("system", backfill.notice, "msg msg-info", broadcast=False)
    for row in backfill.rows:
        slot.append(
            row["role"],
            row["content"],
            row["cls"],
            ts=row["ts"],
            broadcast=False,
            meta=row["meta"],
            mint_mid=False,
        )
    return len(backfill.rows)
