"""Run a local session's turn on a connected peer crew and mirror it back.

The shape of the thing
----------------------
A slot bound with ``executor == "remote"`` lives entirely in the LOCAL product —
local sidebar row, local transcript, local history and search, local theme — but
its turns are executed by a peer gateway reached over an already-open instance
tunnel. This module is the seam: it POSTs the turn to the peer, reads the peer's
SSE stream, and replays it locally so that every existing frontend consumer
(``chatSlice``, ``useWebSocket``) sees exactly the frames a local turn produces
and needs no branch for "this session is remote".

Two channels arrive interleaved on the one stream:

* **transcript rows** — what the peer appended to its own window
  (``user``/``assistant``/``chunk``/``thinking``/``error``/…). These are replayed
  as local ``slot.append`` calls, which is what makes the local transcript a true
  mirror and gets the conversation into local history for free.
* **mirrored WebSocket frames** — everything a turn says about itself that is not
  a transcript row (``tool_call``, ``tool_result``, ``chat_segment``,
  ``chat_done`` …), carried in band because the peer was asked for them with
  ``?relay=1``. See :mod:`kiro_crew.dashboard.remote_mirror`. These are
  re-broadcast locally with the slot key rewritten to the LOCAL key.

Why the peer runs in SSE mode
-----------------------------
The peer's WebSocket transport would require proxying ``/api/ws`` — a
long-lived, differently-authenticated, differently-framed channel that the
instance proxy does not carry. Its SSE transport is a plain HTTP response the
existing tunnel already streams chunk by chunk, and the peer runs the turn
detached from the request either way, so nothing is lost by choosing it.

What is deliberately NOT here
-----------------------------
Resume-attach. If the local gateway restarts mid-turn, the peer keeps running
(that is the upside of the split) but this relay's reader is gone and the local
transcript stops at the last row it saw. Rejoining needs the peer's in-flight
tail, which is a separate concern from dispatch; the turn is not lost on the peer
and the next local send re-synchronises the visible conversation.

What a restart is NOT allowed to be is *silent*, which is the two things this
module does do about it (see :func:`relay_remote_turn` and
:func:`peer_is_connected`):

* An in-flight turn is marked with ``slot._relay_in_flight``, persisted before
  the stream opens and cleared when it ends. On reload a slot still carrying the
  marker gets an explicit "interrupted by a restart" row appended
  (``chat_persistence`` rehydrate), so the transcript never just stops mid-turn.
* A remote session is LOCKED while its tunnel is down — the send path refuses a
  turn with a "reconnecting" 409 until ``peer_is_connected`` is true — so a user
  cannot fire a fresh turn into a half-open tunnel a restart has not rebuilt yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator

from aiohttp import web

import kiro_crew
from kiro_crew.apps.version import parse_version
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import _redact_deep
from kiro_crew.dashboard.remote_mirror import MIRROR_CLS_PREFIX
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Ceiling on one un-terminated SSE record while the relay waits for the blank
#: line that ends it. A well-behaved peer emits records far below this (a chunk
#: is a token delta; the largest honest record is a finalized assistant message).
#: The cap exists so a peer that never sends the terminator cannot grow the
#: buffer without bound — the relay refuses the stream instead.
_MAX_SSE_RECORD_BYTES = 8 * 1024 * 1024

#: Ceiling on a peer's reply to a small control request (open a slot, stop a
#: turn). These answer with one flat JSON object, so anything larger is a broken
#: or hostile peer and is refused rather than decoded.
_MAX_PEER_SLOT_REPLY_BYTES = 64 * 1024

#: Transcript roles the relay must NOT replay locally.
#:
#: ``user`` — the local side already appended the user's own message before
#: dispatching, so replaying the peer's copy would show it twice.
#: ``done`` — the SSE terminator's sentinel role; the stream's ``[DONE]`` is what
#: ends the turn, and appending a row for it would put a phantom row in history.
_SKIP_ROLES = frozenset({"user", "done"})
# Roles that mark a FINISHED transcript message. When finalizing the streamed
# segment we walk back over the trailing `chunk` deltas and step over transient
# rows (a mid-stream stop relays a `system` stop_event row), but must STOP at the
# first of these so a prior finalized message and its own segment are never
# touched. `chunk`/`thinking`/`system` are deliberately absent — they are the
# streamed or transient rows the walk steps over.
_SEGMENT_BOUNDARY_ROLES = frozenset({"assistant", "tool_call", "tool_result", "user", "error"})


class RemoteTurnError(Exception):
    """The peer could not be asked to run the turn.

    Carries a user-facing message only. The peer's credential, the tunnel port
    and any exception detail beyond its type stay in the log — the transcript is
    a surface the user reads and copies out of.
    """


async def ensure_version_parity(mgr: Any, instance_id: str) -> None:
    """Raise :class:`RemoteTurnError` unless the peer runs a compatible build.

    Remote execution is fenced by ``major.minor`` version parity, not full-string
    equality. The two ends exchange a frame vocabulary that carries no version of
    its own, so a peer a FEATURE release ahead can emit a frame this relay
    silently drops, and one a feature release behind can lack a route this relay
    depends on — both surface to the user as a session that mostly works, which is
    worse than one that plainly refuses. A PATCH-level skew within the same
    ``major.minor`` series does not move that vocabulary, so it is allowed: 0.6.0
    and 0.6.3 interoperate, while 0.6.x vs 0.7.x is refused.

    An *unknown* peer version is a mismatch, not a pass: a peer too old to serve
    ``/api/version`` cannot be proven compatible, so it is refused with an
    actionable message rather than optimistically attempted. A version string
    that is not semver-shaped on EITHER end (a packaging build id) also cannot be
    proven compatible by series, so it falls back to strict full-string equality.
    """
    ok, value = await mgr.peer_version(instance_id)
    local = kiro_crew.__version__
    if not ok:
        if value == "capability_peer_too_old":
            raise RemoteTurnError(
                f"This crew is running an older Kiro Crew than this machine "
                f"({local}) and cannot report its version. Update it to {local} "
                f"to run sessions on it."
            )
        raise RemoteTurnError(
            "Could not confirm this crew's Kiro Crew version, so the session was "
            "not dispatched to it. Reconnect the crew and try again."
        )
    # Compare the major.minor SERIES via the shared parser rather than a second
    # hand-rolled regex. ``parse_version`` raises ``ValueError`` both on a
    # non-semver string (a packaging build id) AND on an oversized numeric segment
    # — CPython caps ``int(str)`` at 4300 digits, so a peer returning thousands of
    # leading digits would otherwise raise OUTSIDE the RemoteTurnError handler and
    # 500 the create (GPT/opus #8543). Either way we cannot prove series
    # compatibility, so fall back to strict full-string equality.
    try:
        mismatch = parse_version(local)[:2] != parse_version(value)[:2]
    except ValueError:
        mismatch = value != local
    if mismatch:
        # The peer's reported version is an ARBITRARY string: the transport proves
        # only that ``/api/version`` answered with a non-empty str. Redact BEFORE
        # bounding — truncating first could split a credential across the cut and
        # leave the tail unmatched — so a compromised or misconfigured peer cannot
        # use this mismatch message as an echo channel into the user's transcript.
        shown = redact_peer_text(value)[:64]
        raise RemoteTurnError(
            f"This crew runs Kiro Crew {shown} but this machine runs {local}. "
            f"A session only runs on a crew at the same major.minor version — "
            f"update whichever end is behind."
        )


def iter_sse_records(buffer: bytearray, chunk: bytes) -> Iterator[bytes]:
    """Feed *chunk* into *buffer* and yield each complete SSE record.

    Split on the blank line that terminates a record rather than on single
    newlines: a record is ``data: <json>\\n\\n`` and the JSON payload can itself
    contain escaped newlines, so a line-oriented reader would cut records in
    half. *buffer* is mutated in place and retains the trailing partial record
    between calls.

    Raises :class:`RemoteTurnError` when the buffer passes
    :data:`_MAX_SSE_RECORD_BYTES` without a terminator.
    """
    buffer.extend(chunk)
    while True:
        boundary = buffer.find(b"\n\n")
        if boundary < 0:
            if len(buffer) > _MAX_SSE_RECORD_BYTES:
                raise RemoteTurnError("The crew sent a malformed response stream.")
            return
        record = bytes(buffer[:boundary])
        del buffer[: boundary + 2]
        if record:
            yield record


def parse_sse_record(record: bytes) -> dict[str, Any] | None:
    """The row carried by one SSE record, ``None`` for anything not a row.

    Returns ``None`` for the keepalive comment (``: keepalive``), for a record
    with no ``data:`` line, and for undecodable JSON — a garbled record is
    dropped rather than allowed to end an otherwise healthy turn. The terminator
    is reported as the sentinel ``{"__done__": True}`` so the caller can act on
    it without re-parsing.
    """
    for raw_line in record.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue  # ": keepalive", or a field the SSE spec allows and we ignore
        payload = line[len(b"data:") :].strip()
        if payload == b"[DONE]":
            return {"__done__": True}
        try:
            decoded = json.loads(payload)
        except ValueError:
            logger.debug("Relay dropped an undecodable SSE record")
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _finalize_streamed_segment(slot: "_ChatSlot") -> None:
    """Drop the trailing run of ``chunk`` rows and release them from the queue.

    Mirrors what the peer's own ``_flush_segment`` did to its window just before
    it appended the finalized assistant message: the streamed deltas are replaced
    by the single finished message, so keeping them locally would render the
    answer twice. Releasing the pending copies matters as much as the window
    rewrite — ``append`` put the SAME dict in both, so dropping only the window
    rows would leak every token of the segment in the queue.

    The walk drops the trailing ``chunk`` rows but STEPS OVER a transient row the
    peer may have interleaved — a mid-stream stop on the peer relays a ``system``
    stop_event row that lands between the last chunk and the finalized
    ``assistant`` row (its dict ``cls`` is stripped to ``""`` crossing the relay,
    so it is an ordinary ``system`` row here). The old walk stopped at that row
    and stranded every chunk, rendering the answer twice. Stepping over it while
    dropping only the ``chunk`` rows fixes that; the walk still stops at the first
    finished message (``_SEGMENT_BOUNDARY_ROLES``) so a PRIOR segment is untouched.
    """
    drop: list[int] = []
    for index in range(len(slot.messages) - 1, -1, -1):
        role = slot.messages[index].get("role")
        if role == "chunk":
            drop.append(index)
        elif role in _SEGMENT_BOUNDARY_ROLES:
            break
        # else: a transient row (a relayed stop_event / thinking) — keep it, and
        # keep walking, since chunks of THIS segment can still sit behind it.
    if drop:
        drop_set = set(drop)
        slot.messages = [m for i, m in enumerate(slot.messages) if i not in drop_set]
    slot.release_pending_chunks()


def redact_peer_text(text: str) -> str:
    """Public alias of :func:`_redact_relayed` for peer strings OUTSIDE a turn.

    A relayed turn's rows are not the only peer-controlled text that reaches a
    local surface: a refusal message, a capability label, a frame's CSS class and
    an accepted workspace name all originate on the other machine and all end up
    rendered, broadcast or persisted here. They cross the same trust boundary for
    the same reason, so they run the same chain rather than each site inventing
    one. Exported from this module so every peer string is scrubbed by the code
    registered as the redaction sink for this boundary, instead of spreading
    redactor calls across the callers.
    """
    return _redact_relayed(text)


def _redact_relayed(text: str) -> str:
    """Redact one peer-supplied transcript string, in the repo's fixed order.

    A locally-run turn never appends or broadcasts model-authored text without
    this pair (``chat_runner`` applies it before ``slot.append("assistant", …)``,
    and ``_redact_display_text`` covers every other display surface). A relayed
    turn appends and broadcasts to the SAME surfaces — the local transcript, the
    local WebSocket, the local ConversationLog — so the guarantee has to hold on
    this side of the wire too. The peer redacts its own copy with this same pass,
    which makes the local one idempotent in the healthy case; that is exactly why
    it is cheap enough to not depend on the peer having done it. Both redactors
    return their input unchanged when nothing matches, so clean prose is passed
    through byte-identical.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _replay_mirrored_frame(
    state: "DashboardState", slot: "_ChatSlot", event: str, encoded: str
) -> None:
    """Re-broadcast one mirrored peer frame under the LOCAL slot key.

    The peer's frames name the peer's slot. Rewriting the identifier is the whole
    translation: every other field is already in the local frontend's vocabulary,
    which is what lets a remote-bound session reuse the unmodified consumption
    path instead of forking it.

    Every string in the frame is redacted before the broadcast, for the reason in
    :func:`_redact_relayed`. A mirrored frame carries tool inputs and outputs —
    the field most likely to hold a credential — and its shape is open-ended (the
    mirror is a denylist), so the recursive pass is what keeps a frame type added
    later covered by default instead of shipping raw.
    """
    try:
        data = json.loads(encoded)
    except ValueError:
        # ``event`` is not logged. It crossed the relay boundary, so it is
        # peer-controlled text, and the peer is the last party whose string should
        # be echoed into a log file — redacted or not, the redactor is a denylist.
        # The event name would only ever be diagnostic here; "an undecodable frame
        # arrived" is the fact worth recording.
        logger.debug("Relay dropped an undecodable mirrored frame")
        return
    if not isinstance(data, dict):
        return
    data = _redact_deep(data)
    if "slot" in data:
        data["slot"] = slot.key
    if "key" in data:  # slot_title / session_summary carry `key`, not `slot`
        data["key"] = slot.key
    if event == "slot_clear":
        # DROPPED, not mirrored. A clear is the one mirrored frame that DESTROYS
        # local data, and nothing about a frame arriving over the relay proves the
        # user asked for it — the peer alone decides when to send it. Honouring it
        # would let whatever is on the far end of the tunnel empty this slot's rows
        # and, via ``_dirty``, have the emptied transcript written back over the
        # user's own history: unrecoverable, and triggered off-machine.
        #
        # An earlier revision did mirror it onto ``slot.messages``, to stop a
        # broadcast-only clear from blanking the open view and then restoring every
        # row on the next reload. Dropping the broadcast as well settles that
        # inconsistency the safe way round: the view keeps showing exactly what is
        # persisted, instead of a blank that reappears. The peer's own
        # "conversation cleared" confirmation still arrives as an ordinary
        # transcript row, so the user sees that the crew reset ITS context — what
        # they keep is their own record of the conversation.
        return
    state.broadcast_ws(event, data)


class _ChunkSequencer:
    """Local ``seq`` numbers for relayed chunks.

    The peer's own sequence is not reusable: it counts that peer's turn, while
    the local frontend orders chunks within the LOCAL slot and a second relayed
    turn would restart the peer's count mid-conversation.
    """

    def __init__(self) -> None:
        self._seq = 0

    def next(self) -> int:
        self._seq += 1
        return self._seq


def _apply_row(
    state: "DashboardState",
    slot: "_ChatSlot",
    row: dict[str, Any],
    sequencer: _ChunkSequencer,
) -> None:
    """Replay one peer row into the local slot."""
    role = row.get("type") or ""
    if not isinstance(role, str) or not role:
        return
    content = row.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content)

    if role.startswith(MIRROR_CLS_PREFIX):
        _replay_mirrored_frame(state, slot, role[len(MIRROR_CLS_PREFIX) :], content)
        return
    if role in _SKIP_ROLES:
        return

    # Redact BEFORE the row reaches any local surface: `slot.append` both stores
    # it in the window and hands it to the ConversationLog, so a pass applied
    # afterwards would already have persisted the raw text.
    content = _redact_relayed(content)
    cls = row.get("cls", "")
    # Redacted like the content: the class rides the same broadcast and the same
    # stored row, so a peer that puts a credential here reaches every surface the
    # text would have.
    cls = _redact_relayed(cls) if isinstance(cls, str) else ""
    meta = row.get("meta")
    if isinstance(meta, dict):
        meta = _redact_deep(meta)
        # Keep the durable tool correlation (tool name, input, output, call id)
        # the peer stored, but DROP its ``mid``: that is a per-gateway row
        # delivery id, and adopting the peer's would collide with the local mid
        # space — ``slot.append`` mints a fresh local one when none is supplied.
        meta = {k: v for k, v in meta.items() if k != "mid"} or None
    else:
        meta = None

    if role == "chunk":
        slot.append("chunk", content, "chunk")
        state.broadcast_ws(
            "chat_chunk", {"slot": slot.key, "content": content, "seq": sequencer.next()}
        )
        return
    if role == "thinking":
        slot.append("thinking", content, cls or "thinking")
        state.broadcast_ws("chat_thinking", {"slot": slot.key, "content": content})
        return
    if role == "context_usage":
        # Not a transcript row anywhere in this codebase. The local producer
        # (``broadcast_context_usage``) only broadcasts the reading and records a
        # durable snapshot, and ``context_usage`` sits in ``MIRROR_SKIP_EVENTS``
        # exactly because it already arrives as its OWN wire frame instead of a
        # ``relay:`` mirror — which is what lands it here as a bare role. Letting
        # it fall through to the generic append below wrote the raw JSON payload
        # into the user's transcript once per reading, while the header meter,
        # which reads the WS event, never moved.
        #
        # Re-broadcast it under the LOCAL slot key instead (the payload names the
        # peer's), through the same entry point the local path uses so the snapshot
        # is recorded too and the meter survives a reload. A payload that no longer
        # parses after redaction is dropped: a missing reading leaves the meter at
        # its last value, which is strictly better than a JSON row in the history.
        try:
            usage = json.loads(content)
        except ValueError:
            return
        if not isinstance(usage, dict):
            return
        usage = _redact_deep(usage)
        usage["slot"] = slot.key
        state.broadcast_context_usage(slot.key, usage)
        return

    # Every other role is an ordinary transcript row. ``append`` emits the
    # ``chat_message`` frame itself, so there is nothing to broadcast here — and
    # nothing to special-case for a local SSE reader either, which drains the
    # appended row from the queue exactly as it would for a local turn.
    if role == "assistant":
        _finalize_streamed_segment(slot)
    slot.append(role, content, cls, meta=meta)


async def _require_manager(state: "DashboardState") -> Any:
    """The instance manager, or a refusal explaining that peers are unavailable."""
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        raise RemoteTurnError("Remote crews are not available on this gateway.")
    return mgr


def peer_is_connected(mgr: Any, instance_id: str) -> bool:
    """True when the tunnel to *instance_id* is up and can carry a turn.

    Read defensively on purpose. ``state.instances_manager`` is duck-typed
    (``Any``) and stubbed in tests, and a gateway that JUST restarted has not
    re-established its instance tunnels yet — so a missing manager, a missing
    status, an unexpected shape, or any state other than a connected tunnel all
    mean "not ready to run a turn". The send path locks a remote session on a
    False here rather than firing a turn into a half-open or absent tunnel, which
    is the silent-loss window finding F1 describes: the peer either never receives
    the turn or answers into a stream nothing is reading.

    Compared by the enum's string value rather than importing ``TunnelState`` so
    a stubbed manager returning a plain object with a ``state.value`` works too.
    """
    if mgr is None:
        return False
    try:
        st = mgr.status(instance_id)
    except Exception:
        return False
    return getattr(getattr(st, "state", None), "value", None) == "connected"


def remote_bound_refusal(slot: "_ChatSlot") -> "web.Response | None":
    """A 409 for a turn-starting action on a crew-bound slot, else ``None``.

    ``api_chat`` routes a plain send through :func:`relay_remote_turn`, but the
    OTHER turn-starting endpoints — regenerate, edit-and-resend, rewind, continue
    — carry no remote branch: they dispatch ``_run_chat`` directly, so on a bound
    slot they would run the crew's turn on THIS machine (local tools, local
    credentials) and diverge the local and peer transcripts. Several truncate and
    persist history *before* dispatch, so the local transcript would also be
    rewritten by an action the peer never sees. Until those paths learn to relay,
    they refuse a bound slot outright.

    Keyed on ``executor == "remote"`` (the binding intent), not ``is_remote`` (the
    fully-populated triple): a half-open binding must be refused here too, exactly
    as the send path refuses it — never silently run locally.
    """
    if slot.executor == "remote":
        return web.json_response(
            {
                "error": (
                    "this action runs on the crew that owns the session and is not "
                    "available for a remote-bound session yet — open the session on "
                    "the crew to regenerate, edit, rewind, or continue it there"
                ),
                "code": "remote_action_unsupported",
            },
            status=409,
        )
    return None


async def create_peer_slot(
    state: "DashboardState", instance_id: str, *, agent: str = "", model: str = ""
) -> str:
    """Create the slot on *instance_id* that will execute a local session's turns.

    Returns the PEER's slot key. That key is only meaningful inside a request
    routed back through the same instance — it is not a local session key and
    must never be handed to a local lookup.

    *agent* and *model* are forwarded only when the caller was given them
    EXPLICITLY, and they come from the peer's own rosters (the crew picker reads
    ``/api/instances/{id}/capabilities``). Omitting them is the default because
    this machine's default agent names a crew from this machine's roster: sending
    it would either fail there or bind a different crew than the name implies,
    where an omission lets the peer apply its own default — which is the point of
    the session running on it.
    """
    mgr = await _require_manager(state)
    await ensure_version_parity(mgr, instance_id)
    create_body: dict[str, str] = {}
    if agent:
        create_body["agent"] = agent
    if model:
        create_body["model"] = model
    try:
        async with mgr.proxy_request(
            instance_id,
            "POST",
            "api/chat/slots",
            data=json.dumps(create_body).encode(),
            content_type="application/json",
        ) as upstream:
            if not 200 <= upstream.status < 300:
                raise RemoteTurnError(
                    f"The crew refused to open a session (HTTP {upstream.status})."
                )
            raw = await upstream.content.read(_MAX_PEER_SLOT_REPLY_BYTES + 1)
    except RemoteTurnError:
        raise
    except Exception as e:
        logger.info("Peer slot create on %s failed (%s)", instance_id, type(e).__name__)
        raise RemoteTurnError(
            "Could not reach that crew to open a session. Reconnect it and try again."
        ) from None
    if len(raw) > _MAX_PEER_SLOT_REPLY_BYTES:
        raise RemoteTurnError("The crew returned an oversized reply when opening a session.")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise RemoteTurnError("The crew returned a malformed reply when opening a session.")
    key = payload.get("key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or not key:
        raise RemoteTurnError("The crew opened a session but did not name it.")
    return key


async def forward_peer_stop(state: "DashboardState", slot: "_ChatSlot", force: bool) -> bool:
    """Ask the peer to stop the turn it is running for *slot*.

    Returns whether the peer accepted. Stopping has to travel: the turn is not
    running in this process, so the local cooperative-then-hard escalation has
    nothing to cancel and a local-only stop would leave the peer generating into
    a stream the user believes they interrupted.
    """
    if not slot.is_remote:
        return False
    try:
        mgr = await _require_manager(state)
        async with mgr.proxy_request(
            slot.instance_id,
            "POST",
            f"api/chat/slots/{slot.remote_slot}/stop",
            params={"force": "true"} if force else None,
            data=b"{}",
            content_type="application/json",
        ) as upstream:
            await upstream.content.read(_MAX_PEER_SLOT_REPLY_BYTES + 1)
            return 200 <= upstream.status < 300
    except Exception as e:
        logger.info(
            "Peer stop for slot %s on %s failed (%s)",
            slot.key,
            slot.instance_id,
            type(e).__name__,
        )
        return False


#: The header picks that have to travel, mapped to the peer's route segment.
#: A closed map rather than an f-string over the caller's word: every entry is
#: interpolated into a proxied URL, so an open mapping would let a future caller
#: aim this at any per-slot route the peer exposes.
_PEER_CONTROL_SEGMENTS = {
    "agent": "agent",
    "model": "model",
    "workspace": "workspace",
    "reasoning_effort": "reasoning-effort",
}


async def forward_peer_selection(
    state: "DashboardState", slot: "_ChatSlot", control: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Apply one header pick on the peer slot that actually runs the turns.

    The pickers for a bound session are populated from the PEER's rosters, so a
    pick that landed only locally would name an agent, model or workspace that
    the machine answering the turn never hears about — the header would show one
    thing and the reply would come from another, with nothing reporting a
    problem.

    Raises ``RemoteTurnError`` so the caller can refuse the request outright. A
    control that reports success while changing nothing is the worse failure:
    the user has no way to tell that their pick was dropped.

    Returns the peer's decoded success body, because what the peer ACCEPTED is
    not always what was asked for — its ``/agent`` route resolves the agent
    against ITS bindings and answers with the workspace that resolution chose.
    An empty dict when the reply is oversized or not a JSON object, so a broken
    peer degrades to "nothing to mirror" rather than failing a pick the peer
    already took.
    """
    segment = _PEER_CONTROL_SEGMENTS.get(control)
    if segment is None:
        raise ValueError(f"not a forwardable peer control: {control!r}")
    mgr = await _require_manager(state)
    await ensure_version_parity(mgr, slot.instance_id)
    try:
        async with mgr.proxy_request(
            slot.instance_id,
            "POST",
            f"api/chat/slots/{slot.remote_slot}/{segment}",
            data=json.dumps(body).encode(),
            content_type="application/json",
        ) as upstream:
            raw = await upstream.content.read(_MAX_PEER_SLOT_REPLY_BYTES + 1)
            status = upstream.status
    except RemoteTurnError:
        raise
    except Exception as e:
        logger.info(
            "Peer %s pick for slot %s on %s failed (%s)",
            control,
            slot.key,
            slot.instance_id,
            type(e).__name__,
        )
        raise RemoteTurnError(
            "Could not reach that crew to apply the change. Reconnect it and try again."
        ) from None
    if 200 <= status < 300:
        # Decoded, not discarded: the peer's accepted state can differ from the
        # request (see the docstring), and dropping it left the local slot naming
        # a workspace the machine running the turns had already moved off — the
        # two-ends divergence the caller's immediate persist exists to prevent.
        # Bounded and parsed exactly as the refusal path below is.
        if len(raw) <= _MAX_PEER_SLOT_REPLY_BYTES:
            try:
                accepted = json.loads(raw)
            except ValueError:
                accepted = None
            if isinstance(accepted, dict):
                return accepted
        return {}
    # The peer's own refusal is the useful message — it knows why (an agent that
    # was removed there, a model its account cannot serve, a workspace with
    # messages already sent). Only its `error` string is surfaced, and only when
    # it is a short string: the rest of a peer reply is not trusted for display.
    detail = ""
    if len(raw) <= _MAX_PEER_SLOT_REPLY_BYTES:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            # Redacted BEFORE the clamp, so a credential cannot survive by
            # sitting past the 200th character: this string is raised as a
            # RemoteTurnError and lands in an error row the user reads.
            detail = _redact_relayed(payload["error"])[:200]
    raise RemoteTurnError(detail or f"The crew refused the change (HTTP {status}).")


def _drop_unsent_user_row(slot: "_ChatSlot", message: str) -> None:
    """Roll back the user row appended before dispatch when the peer never got it.

    ``api_chat`` appends the user row and THEN dispatches this relay, so on a
    pre-stream refusal (the peer received nothing) that row is the window tail and
    the relay has appended nothing after it. Removing it keeps local history from
    carrying a turn the peer never saw — a retry then re-appends one copy instead
    of a second (GPT #7693). ``append`` only mutates the in-memory window and marks
    the slot dirty; the durability write is this turn's ``finally`` save, which
    runs after this pop, so nothing stale reaches disk. Guarded on the tail being a
    user row so it is a no-op if anything unexpected sits there.
    """
    msgs = slot.messages
    if not msgs or msgs[-1].get("role") != "user":
        return
    popped = msgs.pop()
    slot.total_messages = max(0, slot.total_messages - 1)
    try:
        slot._pending.remove(popped)
    except ValueError:
        pass
    slot._dirty = True


async def relay_remote_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    *,
    chunks: AsyncIterator[bytes] | None = None,
) -> None:
    """Run one turn for *slot* on its bound peer, replaying the result locally.

    *chunks* exists for tests: pass an async byte iterator to drive the replay
    without a tunnel. In production it is ``None`` and the stream comes from the
    instance manager's proxy.

    Errors reach the user as an ``error`` transcript row and a ``chat_done``, the
    same shape a failed local turn takes, so the composer unblocks and the
    session stays usable rather than appearing to hang.

    KNOWN GAP — a tool the peer wants approved stalls the turn there. The
    approval card is rendered from the SLOT PROJECTION (``pending_approval`` /
    ``approval_id``), not from a streamed frame, so it is built from local slot
    state that a relayed turn never populates: the card does not appear here, and
    ``api_chat_slot_approve`` would find no local future to resolve. Closing it
    needs the peer's pending approval mirrored onto this slot's projection and the
    decision forwarded back — a second mechanism, deferred with resume-attach
    rather than half-built. Until then, run peer-bound sessions on a crew whose
    approval policy does not stop for the tools you expect to use.
    """
    sequencer = _ChunkSequencer()
    # Mark the turn in-flight and persist that BEFORE any streaming, so a gateway
    # crash mid-turn is detectable on reload. The relay task dies with the
    # gateway while the peer keeps running, and its tail is never mirrored here —
    # without this marker the reloaded transcript would simply stop mid-turn with
    # nothing saying why (finding F1). ``chat_persistence`` writes the flag only
    # while it is True; the ``finally`` below clears it and the save there records
    # the cleared state, so a normally-completed turn leaves no stale marker.
    #
    # ``save_slot_off_loop`` writes unconditionally (it does not gate on
    # ``_dirty``), so the marker persists without dirtying the slot. Dirtying here
    # would be indistinguishable from a peer frame scheduling a write — the very
    # thing the mirror path is forbidden from doing.
    slot._relay_in_flight = True
    await save_slot_off_loop(state, slot)
    # A terminal outcome (success / truncation / error) means the tail is now in
    # the local window and a reload has nothing to recover — only then is the
    # marker cleared. Cancellation is NOT terminal: it leaves this flag set.
    cancelled = False
    # Flips true once the peer ACCEPTS the turn — i.e. the stream yields anything
    # at all, INCLUDING the empty acceptance sentinel ``_peer_turn_chunks`` emits
    # the instant the peer answers 2xx. A refusal RAISED BEFORE that (version-parity
    # skew, a non-2xx status, a connection error) means the peer never received this
    # turn, so the user row appended before dispatch must be rolled back or a retry
    # duplicates local history the peer never saw (GPT #7693). Once the peer is
    # reached, a zero-byte OR truncated stream KEEPS the row: the peer owns the turn
    # and may still be running it, so dropping the prompt would erase a message the
    # peer accepted — the earlier ``received_bytes`` gate wrongly dropped it whenever
    # a 2xx response closed before emitting a byte (GPT #7693, this round).
    peer_reached = False
    try:
        if chunks is None:
            chunks = _peer_turn_chunks(state, slot, message)
        buffer = bytearray()
        saw_terminator = False
        async for chunk in chunks:
            peer_reached = True
            for record in iter_sse_records(buffer, chunk):
                row = parse_sse_record(record)
                if row is None:
                    continue
                if row.get("__done__"):
                    saw_terminator = True
                    break
                _apply_row(state, slot, row, sequencer)
            if saw_terminator:
                break
        if not saw_terminator:
            # The stream ending without the peer's terminator is a TRUNCATION,
            # not a completed turn: a dropped tunnel, a peer that died mid-turn,
            # or a proxy that closed the response early all land here. Falling
            # through would broadcast `chat_done` over a partial transcript, so
            # the user reads an answer that stops mid-sentence as if the crew had
            # finished — with nothing anywhere saying otherwise. Raised into the
            # handler below so it reaches them as the same error row every other
            # relay failure takes.
            raise RemoteTurnError(
                "The crew's reply ended before the turn finished, so what is "
                "above may be incomplete. The turn may still be running there."
            )
    except asyncio.CancelledError:
        # The relay task was cancelled (a graceful gateway restart, or the
        # CHAT_TURN_TIMEOUT ceiling) while the peer keeps running detached. This is
        # NOT a terminal outcome: the tail is still being produced over there, so
        # the in-flight marker MUST survive — clearing it here would let a reload
        # present the truncated transcript as complete, with no interruption row
        # (finding F1 / GPT). Flag it so the ``finally`` skips the clear, and
        # re-raise so cancellation still propagates.
        cancelled = True
        raise
    except RemoteTurnError as e:
        if not peer_reached:
            _drop_unsent_user_row(slot, message)
        slot.append("error", str(e), "msg msg-err")
    except Exception:
        logger.warning("Relayed turn failed for slot %s", slot.key, exc_info=True)
        if not peer_reached:
            _drop_unsent_user_row(slot, message)
        slot.append(
            "error",
            "The crew running this session stopped responding. The turn may still "
            "be running there.",
            "msg msg-err",
        )
    finally:
        # Persist BEFORE unblocking the composer, mirroring the local turn path's
        # explicit ``await save_slot_off_loop(state, slot)`` in ``chat_runner``.
        # Every row this turn produced was appended to the in-memory window only;
        # the periodic flush SKIPS non-dirty slots and runs on its own schedule,
        # so a crash between ``chat_done`` and that flush loses a transcript this
        # machine owns — the peer keeps no copy we could re-read. In ``finally``
        # rather than on the success path alone so the error rows above are saved
        # too. ``best_effort=True`` (the default) never raises: a write failure
        # re-arms ``slot._dirty`` for the flush and returns, which is why this can
        # sit ahead of the unblock below without risking it.
        #
        # On CANCELLATION none of this runs: the marker stays set (persisted True
        # at the top), no chat_done is broadcast, and the CancelledError re-raised
        # above propagates — the turn is not finished, so the composer must not be
        # unblocked and a reload must still recover the interruption. A terminal
        # outcome (success / truncation / error) takes the branch below.
        if not cancelled:
            # Clear the in-flight marker FIRST, so the save records a turn that
            # finished: whether it completed, truncated or errored, the tail is now
            # in the local window and there is nothing for a reload to recover.
            slot._relay_in_flight = False
            await save_slot_off_loop(state, slot)
            # The composer is unblocked by ``chat_done``, so skipping it on the
            # error paths would leave the session looking permanently busy.
            state.broadcast_ws("chat_done", {"slot": slot.key})

    # A message sent DURING a relayed turn is REFUSED — the busy branch of
    # ``api_chat`` returns 409 ``remote_turn_busy`` for a remote (or ``relay=1``)
    # slot rather than queuing it. That refusal is deliberate: this coroutine
    # REPLACES ``_run_chat`` rather than wrapping it, so a peer-bound slot reaches
    # no local drain, and draining a queued follow-up through the local dispatcher
    # would run the crew's turn on THIS machine. ``_start_next_queued_turn`` was
    # tried for the drain and REVERTED: it carries no ``is_remote``/``executor``
    # branch and dispatches ``_run_chat`` unconditionally — the wrong-machine
    # execution the ``executor == "remote"`` guard at the primary dispatch exists
    # to prevent. Relaying a queued follow-up instead of refusing it would need an
    # executor-aware queue dispatcher (routing ANY non-local executor, not just
    # this one), a change to the shared local turn path and a decision of its own
    # rather than a tail-call here — so the honest 409 stands until then.


async def _peer_turn_chunks(
    state: "DashboardState", slot: "_ChatSlot", message: str
) -> AsyncIterator[bytes]:
    """Stream the peer's SSE response for one turn, chunk by chunk."""
    mgr = await _require_manager(state)
    await ensure_version_parity(mgr, slot.instance_id)
    body = json.dumps({"message": message, "slot": slot.remote_slot}).encode()
    # ``relay=1`` asks the peer to mirror its WebSocket frames onto this stream;
    # without it the reply carries the prose and none of the tool activity.
    async with mgr.proxy_request(
        slot.instance_id,
        "POST",
        "api/chat",
        params={"relay": "1"},
        data=body,
        content_type="application/json",
    ) as upstream:
        if not 200 <= upstream.status < 300:
            raise RemoteTurnError(
                f"The crew refused the turn (HTTP {upstream.status}). "
                f"It may have restarted — reconnect it and try again."
            )
        # The peer ACCEPTED the turn (2xx) and now owns it. Emit an empty
        # acceptance sentinel BEFORE the content loop so the consumer marks the
        # peer reached even when the body carries zero bytes before the tunnel
        # closes — that is a truncation of a turn the peer is running, not a
        # pre-acceptance refusal, so the user row must be KEPT, not rolled back
        # (GPT #7693). ``iter_sse_records`` treats the empty chunk as a no-op.
        yield b""
        async for chunk in upstream.content.iter_any():
            yield chunk
