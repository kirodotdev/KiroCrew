"""What a transcript row is: role sets, row identities and counts, and the row predicates.

Also the ``chat_message`` frame and note both broadcast doors serialise through,
``append_and_surface`` for the dual writers, and the permission-row resolution
marker. Module functions only; nothing here holds dashboard state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


# ── Shared helpers ──


def parse_cls_meta(cls_val: str) -> dict | None:
    """Parse a JSON-encoded ``cls`` string into a meta dict.

    Returns the parsed dict (with ``tool_input`` sanitized) or ``None``
    if ``cls_val`` is not valid JSON or not a dict.  Used by both
    ``_prepare_messages`` (HTTP history) and ``_broadcast_chat_message``
    (live WS push) so the frontend sees an identical ``meta`` structure.
    """
    if not cls_val:
        return None
    try:
        meta = json.loads(cls_val)
        if not isinstance(meta, dict):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    # Defence-in-depth: sanitize LLM-controlled content at every read boundary
    if isinstance(meta.get("tool_input"), str):
        sanitized, _ = redact_exfiltration_urls(meta["tool_input"])
        sanitized, _ = redact_credentials(sanitized)
        meta["tool_input"] = sanitized

    # Normalize: backend stores as request_id, frontend expects approval_id
    if "request_id" in meta and "approval_id" not in meta:
        meta["approval_id"] = meta.pop("request_id")

    return meta


def chat_message_frame(note: dict, *, include_metadata: bool) -> dict[str, Any]:
    """Serialise a broadcast note into the wire ``chat_message`` frame.

    ONE serialiser for both delivery doors — the WebSocket arm in
    ``_broadcast_note`` and the SSE arm in ``handlers/updates_streams.py:api_stream``.
    They are fed the same note by ``_broadcast()``, so a field added here
    reaches both; building the frame twice is how the SSE door drifts into
    dropping ``meta`` while the WS one carries it.

    ``include_metadata`` is a REQUIRED keyword and names a property of the
    TRANSPORT, not a preference: whether that door has per-client authorization
    downstream of this call.

    * The WS door does (``_send_ws_all`` -> ``_ws_client_allowed``, a
      deny-by-default event-scope gate, then ``_serialize_for_client``), so it
      passes ``True`` and lets the gate decide per socket.
    * The SSE queue does NOT. ``_broadcast()`` fans the raw note out to every
      registered queue with no per-app filtering, so ``api_stream`` must make
      the decision itself and passes ``include_metadata`` only for a
      dashboard-user token.

    That asymmetry is load-bearing. ``meta`` carries tool/LLM content
    (``tool_input``, a live ``oauth_url``, ``approval_id``), so putting it on an
    unfiltered queue exposes it to any app token granted that route regardless
    of its ``slots:*`` scope — the same class as leaking public-repo status onto
    ``/api/stream`` by enriching a payload that feeds both doors. Keep enrichment
    on the door that filters.

    ``cls``/``meta`` are conditional in BOTH directions when included: carried
    when the note has them (``meta.mid`` is the per-row delivery identity a
    client dedups on, so a frame without it cannot be recognised as a
    redelivery), and omitted entirely when it does not — an absent value must
    not arrive as a ``null`` or ``{}`` key a consumer has to special-case.
    """
    frame: dict[str, Any] = {
        "slot": note["slot"],
        "role": note["role"],
        "content": note["content"],
        "ts": note.get("ts", ""),
    }
    if not include_metadata:
        return frame
    if note.get("cls"):
        frame["cls"] = note["cls"]
    if note.get("meta"):
        frame["meta"] = note["meta"]
    return frame


#: The tool row a turn ends on when the agent called ``nothing_to_do``. Read from
#: the runner's ``ends_turn`` stamp plus whatever TRUSTED identity the backend
#: sent (``_tool_identity_fields``), never from the row's title text, so a shell
#: command printing the tool's name cannot close a turn.
QUIET_END_TOOL = "nothing_to_do"


QUIET_END_SERVER = "kirocrew-core"


def is_quiet_end_row(m: dict) -> bool:
    """True when *m* is the APPLIED ``nothing_to_do`` directive's tool row.

    A quiet end is a FINISHED turn: the agent ran its checks, had nothing the
    user needs to read, and said so through the directive rather than by
    stopping bare. Without this the transcript tail is ``[user|nudge, tool…]``
    -- shape-identical to a gateway that died mid-turn -- so the composer
    would offer Resume and the sidebar would flag the session as interrupted
    on every quiet patrol cycle.

    The deciding fact is ``meta.ends_turn``: the runner stamps it only on the
    row of a terminal directive the applier APPLIED
    (``DirectiveOutcome.ends_turn``), never from model text or the title. A
    REFUSED call (a person opened the turn) carries no flag, so it stays an
    unanswered turn.

    The backend's identity halves are checked when present and may be absent:
    KAS names only the server on its ``tool_call`` frame and no tool, and
    opencode sends no ``_meta.kiro`` at all -- the runner resolved the directive
    from the wire title and its parked record instead, so a required tool name
    would read every applied quiet end there as interrupted. A half that IS
    present and names something else still refuses the row.

    Mirrors ``isQuietEndRow`` in ``website/src/store/chat/selectors.ts``.
    """
    if m.get("role") != "tool":
        return False
    meta = m.get("meta")
    if not isinstance(meta, dict) or meta.get("ends_turn") is not True:
        return False
    tool = meta.get("tool_name")
    server = meta.get("mcp_server")
    return tool in (None, "", QUIET_END_TOOL) and server in (None, "", QUIET_END_SERVER)


def chat_message_note(slot_key: str, msg: dict, *, workspace: str | None) -> dict[str, Any]:
    """Build the ``chat_message`` broadcast note for an appended row.

    The ONE builder shared by the append door (``_broadcast_chat_message``) and
    the reader-suppressed frame in ``append_and_surface``, so a row leaves the
    backend in one form whichever door delivers it. *workspace* scopes the
    allowed-link hosts, as in ``_prepare_messages``.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")
    # This site and _prepare_messages (the HTTP history path) share ONE
    # helper — chat_utils.redact_display_content — so a row's *content*
    # leaves the backend in one byte form regardless of which consumer
    # receives it, including structured (list/dict) legacy content, which
    # is redacted recursively rather than skipped. Scope: content only —
    # `cls` / `meta` and the live `chat_chunk` stream are deliberately not
    # covered (see the direct_meta comment below). Gate is `!= "user"` for
    # the same reason as there: every non-user role can carry model/tool
    # output, and user-authored content stays raw (the user typed it and
    # is the only one who sees it back).
    # Deferred import: chat_utils imports from this module at module
    # level, so the reverse import must stay function-level.
    from kiro_crew.dashboard.chat_utils import (
        redact_display_content,
        serialize_wire_content,
        with_allowed_links_restored,
    )

    restored_meta: dict | None = None
    if role != "user" and content:
        # The same allowed-host scope as _prepare_messages, so the live
        # frame and the history agree on an allowed link: its placeholder
        # becomes the address again, then the display pass runs.
        from kiro_crew.security.exfil import scoped_exempt_hosts
        from kiro_crew.security.redaction_allow import allowed_hosts_for

        with scoped_exempt_hosts(allowed_hosts_for(workspace)):
            if isinstance(content, str) and isinstance(msg.get("meta"), dict):
                shown = with_allowed_links_restored({"content": content, "meta": msg["meta"]})
                content = shown["content"]
                restored_meta = shown["meta"]
            content = redact_display_content(content)
    else:
        # The wire-string invariant covers EVERY row: a structured user
        # row or a falsy container serializes to text without redaction.
        content = serialize_wire_content(content)
    payload: dict[str, Any] = {
        "_type": "chat_message",
        "slot": slot_key,
        "role": role,
        "content": content,
        "ts": msg.get("ts", ""),
    }
    # Include cls for backward compatibility
    cls_val = msg.get("cls", "")
    if cls_val:
        payload["cls"] = cls_val
        # Parse cls as JSON to send structured meta field for new frontend
        meta = parse_cls_meta(cls_val)
        if meta is not None:
            payload["meta"] = meta
    # Also include direct meta (e.g. tool_call_id on tool messages).
    #
    # Deliberately NOT redacted here, unlike the `cls` branch above (which is
    # sanitised by parse_cls_meta). Two reasons, both load-bearing:
    #
    # 1. This is the LIVE oauth banner's egress path. _emit_mcp_oauth_request
    #    appends the banner with a real `oauth_url`, already gated by
    #    security.oauth_url_contains_credential — the shared security gate, which
    #    exempts standard high-entropy OAuth values only at exact code-owned
    #    authorization endpoints while scanning everything else fail-closed.
    #    Running _redact_meta_for_role here would blank a genuine
    #    Google/GitHub consent URL and break the user's ability to authorize
    #    an MCP server.
    # 2. chat_utils imports from this module, so importing the redactors the
    #    other way would be a cycle.
    #
    # What makes that safe: live tool meta is redacted at source (_tool_meta),
    # and a DISK-LOADED message reaches this path only when the caller opts
    # in per-role. Both restore loops pass broadcast=False, and the ONE
    # exception is refresh_channel_window, which replays a channel
    # transcript's tail and passes broadcast_user=True so a message typed in
    # Slack renders at all (nothing rendered it optimistically here). That
    # exception cannot carry unredacted meta: ConversationLog.append writes
    # only role/content/ts/source_thread/source_user for such a row -- no
    # meta dict -- so the arm below never fires for it, and the row's
    # content is human-typed, which is deliberately raw at every other
    # boundary too. The invariant is pinned by
    # test_rehydrate_does_not_broadcast_replayed_messages and
    # test_restore_recent_sessions_does_not_broadcast_either. Do not relax
    # it further without re-checking that meta is still absent.
    direct_meta = restored_meta if restored_meta is not None else msg.get("meta")
    if direct_meta and isinstance(direct_meta, dict):
        payload["meta"] = {**(payload.get("meta") or {}), **direct_meta}
    return payload


def is_stop_event_row(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Three carriers, and the in-memory one is the easy miss: the stop is appended
    as ``slot.append("system", stop_msg, stop_msg)`` with **no** ``meta=`` kwarg,
    so ``_ChatSlot.append`` never creates a ``meta`` key and the discriminator
    exists ONLY inside the JSON-encoded ``cls``/``content``. ``parse_cls_meta()``
    is what unpacks it, and it runs on the way OUT to a client
    (``_prepare_messages`` / ``_broadcast_chat_message``) — which is why the
    frontend sees ``meta.kind`` while the live window does not. Checking only
    ``kind``/``meta.kind`` here therefore matched a restored row but never a
    freshly-stopped one, silently diverging from the frontend mirror in exactly
    the case the two must agree on.

    Mirrors ``isStopEvent`` in ``website/src/store/chatSlice.ts``.
    """
    if m.get("kind") == "stop_event":
        return True
    # A truthy non-dict `meta` (a corrupt or foreign transcript row) is data,
    # not a match: `.get` on it would raise into every caller — including the
    # disk-tail preview walk, which already tolerates unparseable lines,
    # non-dict rows, and the non-string `cls` refused below. Same guard, same
    # sibling field.
    meta = m.get("meta") or {}
    if isinstance(meta, dict) and meta.get("kind") == "stop_event":
        return True
    # Live window: the discriminator is still JSON inside `cls`. Prefilter on
    # the literal before parsing — this runs from `to_dict()` on the
    # push_slots_update path for every walked tail row, and `parse_cls_meta`
    # costs a json.loads plus credential/URL redaction when the row carries a
    # string tool_input (permission cards). `"stop_event"` is the literal
    # discriminator, so a cls without the substring can never parse to a match.
    # Non-string `cls` (an object-valued row from a foreign writer or a
    # corrupted transcript) is refused up front: the membership test would
    # raise on it, and `parse_cls_meta` would only swallow it into None anyway.
    cls_val = m.get("cls") or ""
    if not isinstance(cls_val, str) or "stop_event" not in cls_val:
        return False
    parsed = parse_cls_meta(cls_val)
    return bool(parsed and parsed.get("kind") == "stop_event")


#: ``meta.injectKind`` values stamped on an ``inject`` row that DISPATCHED a
#: turn (the queue drain, the cron injectors, the synthesis kick-off, an app
#: message's delivery). Every other inject row -- a ``/note`` breadcrumb, a
#: Stop-hook halt card, a policy refusal notice -- is appended without one and
#: opens nothing. Mirrors ``TURN_INJECT_KINDS`` in
#: ``website/src/store/chat/selectors.ts``, which is keyed by the ``InjectKind``
#: type so a new kind cannot be stamped without being classified there.
#: Wider than ``_TURN_OPENING_INJECT_KINDS`` in ``chat_handlers.py`` on
#: purpose: that set counts turns for the session-start failure streak and
#: walks past ``recovery`` / ``user_replay`` because they resume the SAME
#: turn; here the question is whether a dispatch happened that got no reply,
#: and a recovery or replay dispatch that died is exactly such a turn.
_TURN_INJECT_KINDS: frozenset[str] = frozenset(
    {"cron", "mcp_app", "recovery", "user_replay", "synthesis", "automation_notice"}
)


#: The dispatching inject kinds that CONTINUE the turn above them rather than
#: opening one of their own: a recovery (Resume, an automatic retry) and a replay
#: of the user's own words. The rest of ``_TURN_INJECT_KINDS`` begin new work.
_TURN_CONTINUING_INJECT_KINDS: frozenset[str] = frozenset({"recovery", "user_replay"})


TURN_OPENING_INJECT_KINDS: frozenset[str] = _TURN_INJECT_KINDS - _TURN_CONTINUING_INJECT_KINDS


def _is_turn_inject(meta: object) -> bool:
    """Whether an ``inject`` row's meta says it dispatched a turn."""
    return isinstance(meta, dict) and meta.get("injectKind") in _TURN_INJECT_KINDS


def is_turn_interrupted(messages: list[dict]) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Two shapes qualify: the last turn-opening row is the USER's, a monitor
    loop's NUDGE, or a runner-authored INJECT's (nothing came back at all — a gateway restart
    mid-turn leaves exactly this), or the last conversational row is the
    ASSISTANT's but an error row follows it (the turn streamed partway then died,
    which is otherwise shape-identical to a clean completion). An inject counts
    as an opener only when it carries a dispatching ``meta.injectKind`` (see
    ``_TURN_INJECT_KINDS``): a queued continuation, a recovery or a synthesis
    turn IS a turn, and without it the scan walks past an interrupted one and
    can reach the previous turn's Stop card, which then hides the newer
    interruption. An untagged inject -- a ``/note`` breadcrumb, a Stop-hook halt
    card, a refusal notice -- dispatched nothing and is looked through.

    Two shapes are explicitly excluded. A trailing ``stop_event``: the user
    pressing Stop is a deliberate ending, not an interruption, and stopping
    before the reply emitted any text produces the same ``[user, ...]`` tail as
    a crash. And a ``/compact`` request answered by its compaction notice (the
    assistant row ``chat_utils._append_compaction_notice`` tags
    ``meta.kind="compaction"``): the slash command IS the whole request and the
    notice IS its result, so nothing is missing. The discriminator is
    deliberately BOTH halves -- the tag alone cannot decide, because an
    automatic compaction can write the same tagged row inside an ordinary turn
    whose real reply never arrived, and that tail is a genuine interruption.

    Selects the wording injected for the model (``_MANUAL_RESUME_MSG`` vs
    ``_MANUAL_CONTINUE_MSG``), gates whether the composer offers the Resume
    control (the ``continuable && interrupted`` composition in
    ``website/src/pages/chat/page/ComposerDockView.tsx``), and feeds the ``interrupted`` field of
    the slot summary so the sidebar can stop rendering a goal loop as actively
    working while its session sits behind a Resume button. A False result means
    "as far as the transcript shows, the last turn finished or was ended on
    purpose", NOT "there is nothing to do": a force-quit runs no ``finally``, so
    the error row that would have proved an interruption was never written.

    Mirrors ``selectTurnInterrupted`` in ``website/src/store/chat/selectors.ts`` —
    the two must agree, or the composer promises one thing and the agent is
    told another.

    Deliberately does not distinguish "produced some output" from "produced
    none": ``_MANUAL_RESUME_MSG`` is worded to hold in both cases, so the
    distinction would buy a branch and nothing else.
    """
    saw_trailing_error = False
    saw_compaction_result = False
    # A tool row met AFTER the quiet end (later in time): the model broke the
    # contract and kept working, so the quiet-end row is not the turn's last
    # act and must not close it -- a gateway that died in that later work
    # would otherwise hide behind it.
    saw_later_tool = False
    for m in reversed(messages):
        role = m.get("role")
        meta = m.get("meta") or {}
        # A deliberate Stop ENDS the turn; it does not interrupt it. Tested
        # before the user/assistant branch because stopping before the reply
        # emitted any text leaves ``[user, stop_event]`` -- shape-identical to
        # "the gateway died before anything came back". See ``is_stop_event_row``
        # for why the discriminator has to be resolved from three carriers.
        # Only the NEWEST turn's terminator reaches here -- an older stop card
        # is never scanned, because a later user/inject/assistant row returns
        # first.
        if is_stop_event_row(m):
            return False
        # A quiet end (``nothing_to_do``) is the turn's deliberate ending too:
        # same reasoning as the Stop card, same position in the scan. Only the
        # newest turn's row reaches here, for the same reason -- and only when
        # it IS the turn's last tool row.
        if is_quiet_end_row(m):
            if not saw_later_tool:
                return False
        elif role == "tool":
            saw_later_tool = True
        if is_system_notice(role, meta):
            # Remember a compaction RESULT row on the newest turn. Skipping the
            # row is still right in general (an auto-compaction notice inside
            # an ordinary turn is not that turn's reply), but when the user row
            # this scan lands on IS the ``/compact`` request, this row is that
            # request's whole result -- see the user branch below. The recycle
            # and stuck-turn notices borrow ``kind="compaction"`` for the
            # follow-up scan's skip and mark themselves with ``meta["notice"]``;
            # they report no compaction, so they must not complete one.
            if (
                isinstance(meta, dict)
                and meta.get("kind") == "compaction"
                and not meta.get("notice")
            ):
                saw_compaction_result = True
            continue
        if role == "inject" and m.get("content") and _is_turn_inject(meta):
            return True
        # A monitor loop's cycle row always dispatches a turn; unanswered, it
        # is the same shape as an unanswered user row.
        if role == "nudge" and m.get("content"):
            return True
        if role in ("user", "assistant") and m.get("content"):
            if role != "user":
                return saw_trailing_error
            # A ``/compact`` answered by its compaction notice is a FINISHED
            # turn -- unless an error row trails the notice, which is the same
            # evidence the plain-assistant branch honors. Matched on the first
            # whitespace token, the same rule the runner uses for
            # ``user_requested_compaction``.
            content = m.get("content")
            if (
                saw_compaction_result
                and isinstance(content, str)
                and content.split()[:1] == ["/compact"]
            ):
                return saw_trailing_error
            return True
        if role == "error":
            saw_trailing_error = True
    # The walk ran off the start of the window without meeting a conversational
    # row: a long turn can push its own opener and reply into the frozen prefix,
    # leaving only tool rows here. A trailing error row is still the evidence
    # the assistant branch above honors -- the turn ended in it and nothing
    # newer proves completion -- so it decides the same way.
    return saw_trailing_error


def _mark_permission_resolved(
    messages: list[dict],
    request_id: str,
    decision: str,
    *,
    only_if_pending: bool = False,
) -> bool:
    """Persist a resolved decision into a permission message's cls JSON.

    Returns True when a permission message was written. Callers holding the
    owning slot MUST set ``slot._dirty = True`` on a True return — the periodic
    flush skips non-dirty slots, so an unflagged in-place mutation can be lost
    on restart and the card comes back as an unanswerable orphan.

    ``only_if_pending`` leaves an already-resolved message untouched (and
    returns False). Use it for backstop callers that must not clobber a richer
    decision already recorded by the primary resolver — e.g. "trust"/"yolo",
    which the UI renders as "Trusted — auto-approving future calls" and would
    otherwise be flattened to a bare "approved".
    """
    for msg in reversed(messages):
        if msg.get("role") == "permission":
            try:
                cls = json.loads(msg.get("cls", "{}"))
                if not isinstance(cls, dict):
                    # Valid JSON but not an object — cannot carry "resolved".
                    # Mirrors parse_cls_meta() / _sweep_stale_permissions().
                    continue
                if cls.get("request_id") == request_id:
                    if only_if_pending and "resolved" in cls:
                        return False
                    cls["resolved"] = decision
                    msg["cls"] = json.dumps(cls)
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
    return False


#: Transient/streaming roles that are never persisted by the save path
#: (``chat_persistence._build_message_entry`` returns ``None`` for them) and are
#: skipped by durable readers. Defined here — beside the trim path that must
#: count the durable rows it folds into the frozen prefix — and re-exported by
#: ``chat_persistence`` as ``_TRANSIENT_ROLES`` for its readers, so there is one
#: definition rather than two that can drift.
_TRANSIENT_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission", "elicitation"})


def durable_row_count(rows: Iterable[dict]) -> int:
    """How many of *rows* a durable read returns: everything non-transient.

    The one shared counting rule for the durable-only frozen-prefix counter
    (``_disk_older_durable_count``): every site that sets or advances it counts
    with this, so the restore paths, the channel restore and the trim path can
    never disagree about which rows are durable.
    """
    return sum(1 for m in rows if m.get("role") not in _TRANSIENT_ROLES)


#: Roles that exist only on the wire: appended so a reader/flush can see them,
#: never broadcast as a `chat_message` and never persisted (the mirror of
#: ``_TRANSIENT_ROLES`` above minus the rows that ARE broadcast).
#: They get no ``meta.mid`` — see ``_ChatSlot.append``.
_WIRE_ONLY_ROLES = frozenset({"chunk", "done", "streaming"})


def row_mid(row: Any) -> str | None:
    """The delivery identity stamped on an appended window row, or ``None``.

    The ONE extraction of ``meta.mid`` shared by every dual-writer that reads
    the id off a ``_ChatSlot.append`` return to stamp its durable transcript
    copy. Mirrors the read side (``_append_unflushed_tail`` matches only a
    non-empty ``str``): any other shape reads as "no identity" here rather than
    being persisted as an id the reader is structurally unable to match.
    Tolerates a non-dict *row* so a caller handed a test double degrades to
    ``None`` (an id-less durable copy) instead of raising.
    """
    if not isinstance(row, dict):
        return None
    meta = row.get("meta")
    mid = meta.get("mid") if isinstance(meta, dict) else None
    return mid if isinstance(mid, str) and mid else None


def _compaction_keep_record(key: str) -> dict[str, Any] | None:
    """This session's pending ``compaction.keep`` record, or ``None``. Never raises.

    ``None`` is the overwhelmingly common answer -- the seam is off until the owner
    consents to a third, whole-transcript scope -- and it is also what a scoring run
    that missed the compaction produces. The notice row then looks exactly as it does
    without the seam, which is the point's own stated contract: the measurement may
    cost an observation and must never cost the notice.

    The read is DESTRUCTIVE (``take_record``): the record describes ONE compaction, so
    leaving it in place would attach it to the next one on this key.

    Imported inside the function and guarded as a whole: the decisions package is an
    optional subsystem and this module is on the gateway boot path, so a build without
    it -- or with a broken point file -- still appends the notice.
    """
    try:
        from kiro_crew.decisions.points.compaction_keep import take_record

        return take_record(key)
    except Exception:
        logger.debug("compact notice: no decision record for %s", key, exc_info=True)
        return None


def append_and_surface(
    state: "DashboardState",
    slot: "_ChatSlot",
    role: str,
    content: str,
    cls: str = "",
    *,
    meta: dict | None = None,
    broadcast_user: bool = False,
) -> dict[str, Any]:
    """Append a row and surface it live -- through exactly one identity-carrying door.

    The ONE way to append a window row that must also render in the open chat
    immediately. ``_ChatSlot.append`` already delivers a live ``chat_message``
    (via ``_on_message`` -> ``_broadcast_chat_message``, carrying the minted
    ``meta.mid``) whenever ``not slot._has_reader`` -- so an unconditional
    manual ``broadcast_ws("chat_message", ...)`` after an append ships the SAME
    row a second time. Worse, the hand-built frames carried no ``meta.mid``,
    and the frontend's redelivery guard declines mid-less frames rather than
    guessing (``isRedeliveredMessage``), so each extra copy renders as a new
    bubble.

    The manual frame is emitted only in the one case append's own callback is
    suppressed (``slot._has_reader``: an HTTP stream reader is draining
    ``_pending`` for the actively streaming client, and other windows still
    need the row) -- mirroring the pattern at ``handlers/files.py`` -- and it
    carries the appended row's ``ts`` + ``meta`` (mid included), so a client
    that receives the row through two doors can now recognise "this row again"
    instead of rendering a duplicate.

    ``user`` rows: ``append`` skips broadcasting them by default because the
    composer that submitted them already rendered them optimistically -- true
    only of a message typed in THIS dashboard. Callers surfacing a user row
    that originated elsewhere (a channel mirror, a Go-button label) pass
    ``broadcast_user=True`` and get the same single identity-carrying delivery.

    Redaction is the caller's job (unchanged from the sites this replaces):
    content passed here must already be display-safe. Both doors also run the
    same display pass on non-user content: the reader-suppressed frame below is
    built by ``chat_message_note`` + ``chat_message_frame``, the same builders
    the append door's WebSocket arm uses, so the frame is identical whether or
    not a stream reader is attached. A route kind belongs in ``meta``, which
    both doors carry.

    Returns the appended row (so callers can read ``row_mid`` off it).
    """
    if broadcast_user:
        msg = slot.append(role, content, cls, broadcast_user=True, meta=meta)
    else:
        msg = slot.append(role, content, cls, meta=meta)
    if getattr(slot, "_has_reader", False):
        workspace = getattr(slot, "workspace", None)
        note = chat_message_note(
            slot.key, msg, workspace=workspace if isinstance(workspace, str) else None
        )
        state.broadcast_ws("chat_message", chat_message_frame(note, include_metadata=True))
    return msg
