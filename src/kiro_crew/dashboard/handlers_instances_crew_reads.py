"""The MCP crew-scoped session reads: ``/api/crew-sessions/{search,list,read}``.

These three GET routes back the ``crew=`` scope on the kirocrew-core session read
tools (search_chat_history / list_sessions / get_chat_session). An agent on this
hub reads a connected peer crew's sessions ON DEMAND over the existing tunnel:
nothing is copied to this machine, every row comes back redacted and capped, and
the peer's own ``ConversationLog`` stays the one transcript -- the ownership model
``docs/request-for-change/rfc-remote-crew-sidebar.md`` adopts, applied to the
agent's read tools rather than the browser's window.

They are MCP-only: the tools reach the gateway over the ``X-Internal-Secret``
handshake and cannot present as an owner-dashboard request, so they do NOT ride
``api_instances_search_sessions``' owner guard. Instead :func:`_authorize_crew_read`
asserts the same property -- the positively identified owner, and nobody else --
through the evidence this transport carries (the attested ``X-Session-Key`` and its
ancestry). The remote data itself stays gated by the tunnel credential, which never
leaves ``SshTunnelManager``.

Transport reuse (no fourth hand-rolled copy of the peer-request dance): all
three legs ride the generic ``mgr.proxy_request`` carrier, read under
``read_capped_response`` with
``PEER_SESSIONS_REPLY_MAX_BYTES``. Every field a peer returns is untrusted input --
redacted then clamped locally before it reaches the caller.

An owner module of the ``kiro_crew.dashboard.handlers_instances`` facade: it is
imported by that facade and never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import quote

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import is_owner_minted_session_key
from kiro_crew.context import RECALL_ROLES
from kiro_crew.dashboard.handlers._shared import _owner_denial_response, read_capped_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.handlers_instances_gate import (
    _PEER_FIELD_MAX_CHARS,
    MANAGER_UNAVAILABLE_CODE,
    _audit,
    _manager_unavailable_response,
    _registry,
)
from kiro_crew.dashboard.handlers_instances_peer import _cap_str
from kiro_crew.history import INCOGNITO_EXCLUDED_HEADER, SEARCH_MIN_CHARS, is_incognito_transcript
from kiro_crew.instances.constants import PEER_SESSIONS_REPLY_MAX_BYTES
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError
from kiro_crew.member_memory_auth import private_memory_store_for_session
from kiro_crew.validation import MAX_SHORT_STRING, sanitize_string

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

# The unsplit module's logger name, shared with every owner of the facade family so
# the mirror sees one object (and an operator's log filter sees one route group).
logger = logging.getLogger("kiro_crew.dashboard.handlers_instances")

# A bounded read must return within the MCP client's _get timeout (10s): a
# reachable-but-slow peer that overruns would otherwise be reported to the agent
# as "unreachable". Kept under 10s with margin.
_CREW_PROXY_TIMEOUT = 8.0

# Per-message ceiling for a peer TRANSCRIPT body, in CHARACTERS, because that is
# the unit ``_cap_str`` clamps in. A transcript message is legitimately long, so it
# must not take the short metadata clamp, but it does not go unclamped either:
# passing the reply's BYTE bound straight into a character clamp reads as a unit
# confusion even though UTF-8 makes it safe (a decoded character is never fewer
# than one byte, so the byte cap already bounds the character count). The real
# bound remains ``PEER_SESSIONS_REPLY_MAX_BYTES`` on the whole reply; this is the
# per-field ceiling underneath it, and no honest message reaches it.
_PEER_MESSAGE_MAX_CHARS = PEER_SESSIONS_REPLY_MAX_BYTES

# A peer reply larger than this is decoded and shaped OFF the event loop. The
# gateway runs every chat stream, every other request and the liveness heartbeat
# on one loop, and a crew transcript read legitimately runs to megabytes: decoding
# it and running the multi-pattern redaction chain over every message inline would
# hold that loop for the whole pass. Same threshold as the peer proxy's
# ``_redact_sse_event_async``, for the same reason. A small reply stays inline,
# where the thread hop would cost more than the work.
_OFF_LOOP_BYTES = 65536

#: The refusal every crew-read route gives while ``instances.crew_reads_enabled``
#: is off. Its own code so a tool can tell "disabled on this install" from a
#: refused identity, and because it is the one denial expected on every install
#: that has not opted in.
CREW_READS_DISABLED_CODE = "crew_reads_disabled"

# A SHORT peer field (title, snippet, preview, agent, date) longer than this is
# REPLACED by ``_FIELD_TOO_LONG``, never cut. The redaction chain is linear in its
# input, and a hostile peer could otherwise put megabytes behind a 2 KiB field, so
# the work per field has to be bounded -- but a cut cannot do it: cutting the raw
# text can split a credential so the detector that would have masked it cannot
# see its shape (the chain strips hidden Unicode AFTER the cut, so padding can
# move a secret's delimiter past any fixed cut and have the padding vanish
# afterwards). The field is therefore judged whole or not at all: an honest short
# field never approaches this bound, and a field past it carries nothing the
# agent needs, so it is refused as a unit and the chain runs on nothing.
_FIELD_MAX_RAW_CHARS = _PEER_FIELD_MAX_CHARS * 4
_FIELD_TOO_LONG = "[omitted: peer field over the size cap]"

_T = TypeVar("_T")


def _cap_field(value: object) -> str:
    """A short peer field, redacted whole and clamped, or the placeholder if over-long.

    The whole string is handed to ``_cap_str`` (hidden-Unicode strip, then the
    redaction chain, then the clamp), so every detector sees the field exactly as
    the peer sent it; see ``_FIELD_MAX_RAW_CHARS`` for why an over-long field is
    replaced rather than cut.
    """
    if not isinstance(value, str):
        return ""
    if len(value) > _FIELD_MAX_RAW_CHARS:
        return _FIELD_TOO_LONG
    return _cap_str(value, _PEER_FIELD_MAX_CHARS)


def _decode_and_shape(body: bytes, shape: "Callable[[object], _T]") -> _T:
    """Decode one peer reply and shape its rows, as one unit of (possibly off-loop) work.

    A body that is not JSON shapes as ``None``, which every shaper reports as
    malformed, the same answer the decoder gave before shaping moved here.
    """
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    return shape(payload)


def _audit_crew(
    request: web.Request, operation: str, outcome: str, *, request_id: str = "", error: str = ""
) -> None:
    """The crew-read audit event, with the calling session's key on EVERY outcome.

    Spec §18.3: every call carries the originating MCP caller's session key, so an
    audit reviewer can see which agent session read, or tried to read, a remote
    crew's history. The refusals are the events that matter most for that review,
    and a gate arm that audited without the key would leave the trail unable to
    say who was turned away. One wrapper, so no arm can forget: the key is read
    off the header as declared, verified or not, because the audit records what
    the caller SAID it was and the gate's ``error`` records whether that held.
    """
    _audit(
        operation,
        outcome,
        request_id=request_id,
        error=error,
        caller=request.headers.get("X-Session-Key", "").strip(),
    )


def _peer_row_is_restricted(value: object) -> bool:
    """Whether a peer row's ``memory_mode`` marks it incognito/temporary.

    Written as a predicate rather than an inline membership test because the
    peer's payload is UNTRUSTED: a hostile or malformed peer answering
    ``memory_mode: []`` must be a refusal, not a ``TypeError`` surfacing as a 500.
    Only ``key`` is otherwise type-checked on that payload. The hub keeps this
    filter even though the peer now excludes under its own locks: it is the
    version-independent floor, and it costs nothing.

    The three cases are deliberately NOT collapsed:

    * ABSENT (``None``) is the ordinary case and answers False. Most rows carry no
      ``memory_mode`` at all, so treating a missing mode as restricted would drop
      nearly every legitimate row.
    * A ``str`` takes the membership test — the actual rule this mirrors.
    * Anything else PRESENT (a list, dict, or number) cannot be a valid mode, so it
      answers True and the row is dropped. Fail CLOSED here: the only reason a peer
      sends a non-string mode is to dodge a filter whose whole job is keeping a
      marked session out of a remote agent's reach, and a type guard that merely
      skipped the test would admit exactly the row the filter exists to refuse.
    """
    if value is None:
        return False
    if isinstance(value, str):
        # The canonical predicate, which normalizes case: a peer is untrusted
        # input, so a header spelled ``Incognito`` must refuse like ``incognito``
        # does, exactly as the guarded transcript read on the peer refuses it.
        return is_incognito_transcript(value)
    return True


def _slot_refusal(slot: object) -> "tuple[str, str] | None":
    """The CREW-SPECIFIC reasons the resolved tab may not read a peer, or ``None``.

    Returns ``(code, audit_error)``. The shared caller resolver
    (``handlers/guide.py``'s ``_resolve_agent_caller``) has already established
    that *slot* is the caller's own live, non-app, attended tab running a turn a
    person sent from the dashboard and not a channel's; these are the two arms
    that gate adds nothing for and this one needs, both read from gateway memory
    the turn runner writes, never the agent-writable metadata line:

    * ``_turn_owner_sent`` -- the person who sent the turn is the OWNER. A
      dashboard token is also minted for a channel user the install allow-lists
      (``/kirocrew dashboard`` on Telegram, Teams and Webex), whose sends pass the
      resolver's user-sent arm: ``_turn_user_sent`` records that a person sent the
      turn, not which one. The owner mark is the gateway's own record of
      ``is_owner_dashboard_request`` for the request that dispatched the turn:
      passed on the turn's own ``_run_chat`` call (``_directive_owner_origin``) by
      the dashboard send, regenerate and rewind handlers, carried on a queued
      entry under the same name and reduced by the drain over every entry it
      consumes, adopted at the turn's outermost entry only for a user-sent opener
      (an undeclaring dispatch starts from False, and a dispatch that never
      started leaves nothing on the slot), cleared by a steer that is not the owner's and on every turn
      exit (``slot_ladder`` documents the field). The MCP
      request itself carries no ``user`` claim, so this is the only place the
      owner's identity can be asserted for a tool call.
    * ``_created_by`` / ``_lineage_minted`` -- the slot was opened by automation on
      some caller's behalf (``session_create``, a firing cron) rather than by the
      person's own tab. Refused in either spelling: the in-process mint witness, or
      the attribution restored after a restart. A worker whose restored attribution
      was CLEARED does read as unattributed here, and that is accepted because the
      resolver's user-sent arm closes it on its own: the only turns such an orphan
      can run are a ``session_send`` (never user-sent) or the person opening the
      tab and typing (then the person IS here), since ``cron_creator_admission``
      fences a cron to slots that still carry its own ``_created_by`` -- a blank one
      "matches no caller" (``_created_by_other``). The creator check therefore
      refuses the ordinary worker cheaply and without a second source of truth.
    """
    if getattr(slot, "_turn_owner_sent", False) is not True:
        return "not_owner_turn", "the running turn was not sent by the owner"
    if bool(getattr(slot, "_lineage_minted", False)) or str(getattr(slot, "_created_by", "") or ""):
        return "owner_only", "calling slot was opened on another caller's behalf"
    return None


async def _authorize_crew_read(request: web.Request, operation: str) -> "web.Response | None":
    """Positively authorize a crew-read: the owner, in their own tab, right now.

    Returns ``None`` when the caller is authorized; the 403 response otherwise.

    These routes disclose ANOTHER crew's session titles, snippets and full
    transcripts, executed on that peer with the OWNER's manager-held credential.
    That is the same disclosure ``api_instances_search_sessions`` gates for the
    browser, so it takes the same bar -- the positively identified owner -- and
    reaches it the way every tool call that acts for the person does: through the
    shared caller resolver, ``handlers/guide.py``'s ``_resolve_agent_caller``,
    plus the arms that only a crew read needs.

    **Why not ``is_owner_dashboard_request`` alone.** That helper requires a
    dashboard ``user`` claim, and the internal-secret branch never sets one (see
    ``token_auth``: identity there comes from the calling session, not a cookie).
    Applied literally it would refuse every legitimate call, so this asserts the
    same *property* through the evidence this transport actually carries.

    **What is NOT authorized, and why nothing is resolved on its behalf.** A
    delegated session -- a ``subagent:`` run, a ``side:`` chat, a worker that
    ``session_create`` or a cron opened -- carries a key the gateway minted, but a
    key says nothing about FOR WHOM. The records that could say so are each
    unfit to decide it: a subagent's run folder is agent-writable; a slot's
    ``created_by`` is restored from the agent-writable metadata line after a
    restart; a cron's ``session_key`` lives in ``crons.json``, which the sandbox
    lets the agent rewrite; and the parent slot's own turn provenance is reset
    when the parent's turn ends while a background child is still running. Each of
    these was found, in turn, to let a channel participant's or a cron's work read a
    peer with the owner's authority. So no ancestry is walked: a delegated caller
    is refused by namespace, and ``crew=`` is an owner's-tab capability. A child
    that needs a peer's history asks its parent, which is where the person is.

    The gate, in order, each arm with its own refusal code so an audit reader sees
    which signal failed rather than a bare 403:

    1. *Owner-minted namespace* (``owner_only``): a declared key that is not
       :func:`is_owner_minted_session_key` -- ``dashboard`` -- is refused before
       anything is resolved for it. ``subagent``, ``side``, ``cron``, every
       channel namespace, the legacy ``channel:`` shape, the workflow family and
       anything minted by a transport added later all take this refusal, so an
       unforeseen identity class fails closed. First because it is a property of
       the key's spelling, needing no trust, and because the resolver's
       ``caller_slot_key`` maps a channel session key onto the dashboard slot
       that mirrors it -- a resolution that is right for a guide and wrong here.
       An absent key is left to the resolver, which names that case itself.
    2. *The shared caller resolver* (``_resolve_agent_caller``, its own codes):
       the internal secret (``internal_secret_required``); no app claim
       (``app_caller``); a present key (``missing_session_key``) that the
       transport attests (``unattested_caller``: the Unix-socket peer check or the
       launcher's signed per-session token, because the internal secret is held by
       every same-machine process the gateway spawns); a live slot for it
       (``no_live_slot``, which also covers an app-owned tab popped with a call in
       flight); not app-scoped (``app_scoped_caller``); not an unattended cron or
       workflow slot (``unattended_caller``); a turn running in it
       (``no_dashboard_turn``); not a channel-opened or channel-steered turn
       (``channel_caller``); and, because the three crew operations are in
       ``_USER_TURN_OPERATIONS``, a turn a person sent (``not_user_turn``). Its
       refusal is re-audited here as a crew-read denial carrying the caller's key.
    3. *Private-memory fence* (``private_memory``): the resolved key is checked
       with ``private_memory_store_for_session``; a session bound to a private
       member store may not read another crew's sessions, and a binding that
       cannot be read refuses too. The MCP tools apply the same fence before any
       wire call (``mcp_tools/sessions.py``), but the tool process is not the only
       holder of the internal secret and an attested key: the fenced session's own
       shell can read the sandbox-visible ``.local_secret`` and call this route
       over the socket with its own key, which attestation accepts because the
       process IS that session. The fence is the gateway's to hold, as
       ``workflows/service.py`` holds it.
    4. *The crew-specific slot arms* (:func:`_slot_refusal`): the turn was sent
       by the OWNER (``not_owner_turn``), and the slot was not opened on another
       caller's behalf (``owner_only``).
    5. *Non-owner dashboard subject*: if a ``user`` claim IS present (a
       ``!dashboard``-minted browser token reaching a mixed-internal route), it
       must be the configured owner -- delegated to the merged helper. The owner
       arm of step 4 is what covers the ordinary tool call, which carries no
       claim: it asserts the same predicate for the request that dispatched the
       turn, recorded by the gateway when that request was handled.

    Nothing here reads the slot's ``channel_origin`` metadata flag or any other
    field of the transcript's metadata line: that file is agent-writable, and
    ``handlers/sessions`` already records that provenance for a cross-session
    decision must come from outside agent-writable storage. Every signal the gate
    reads is either transport attestation or gateway memory the turn runner writes.
    """
    from kiro_crew.dashboard.guide_runs import GuideError
    from kiro_crew.dashboard.handlers.guide import _resolve_agent_caller

    # The feature flag comes first: default off, and an install that has not
    # turned it on resolves nothing for the caller and reaches no peer. Its own
    # code, so the tool can tell "disabled here" from a refused identity.
    if not KiroCrewConfig.load().instances.crew_reads_enabled:
        _audit_crew(request, operation, "denied", error=CREW_READS_DISABLED_CODE)
        return web.json_response(
            {
                "error": (
                    "crew-scoped session reads are disabled "
                    "(set instances.crew_reads_enabled=true)"
                ),
                "code": CREW_READS_DISABLED_CODE,
            },
            status=403,
        )
    declared_key = request.headers.get("X-Session-Key", "").strip()
    if declared_key and not is_owner_minted_session_key(declared_key):
        _audit_crew(
            request, operation, "denied", error="caller is not an owner-minted dashboard session"
        )
        return web.json_response({"error": "forbidden", "code": "owner_only"}, status=403)
    try:
        slot_key, caller_key = await _resolve_agent_caller(request, f"instances.{operation}")
    except GuideError as exc:
        _audit_crew(request, operation, "denied", error=exc.message)
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    try:
        fenced_store = await asyncio.to_thread(private_memory_store_for_session, caller_key)
    except (OSError, ValueError):
        # An unreadable binding is indistinguishable from a fenced member whose
        # store could not be confirmed; on a cross-crew read "unknown" refuses.
        _audit_crew(request, operation, "denied", error="private-memory binding unreadable")
        return web.json_response(
            {"error": "this session's private memory is unavailable", "code": "private_memory"},
            status=403,
        )
    if fenced_store:
        _audit_crew(request, operation, "denied", error="private-memory session")
        return web.json_response(
            {
                "error": "a private-memory session cannot read another crew's sessions",
                "code": "private_memory",
            },
            status=403,
        )
    state: DashboardState = request.app["state"]
    refusal = _slot_refusal(state.get_slot(slot_key))
    if refusal is not None:
        code, why = refusal
        _audit_crew(request, operation, "denied", error=why)
        return web.json_response({"error": "forbidden", "code": code}, status=403)
    if str(request.get("user") or "") and not is_owner_dashboard_request(request):
        _audit_crew(request, operation, "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew session reads are owner-only")
    return None


#: :func:`_resolve_crew_id`'s answer for a display name more than one registered
#: crew carries. The registry de-duplicates ids, never names, so a name alone can
#: fail to pick one crew; reading "the first row" would hand the agent a different
#: crew's corpus than the one it meant, so the name is refused and the caller is
#: told to use an id.
CREW_AMBIGUOUS_CODE = "crew_ambiguous"


def _crew_refusal(code: str) -> web.Response:
    """The response for a ``crew=`` value that resolves to no single crew."""
    if code == CREW_AMBIGUOUS_CODE:
        return web.json_response(
            {
                "error": "more than one crew has that name; use its instance id",
                "code": CREW_AMBIGUOUS_CODE,
            },
            status=409,
        )
    return web.json_response({"error": "unknown crew", "code": "crew_unknown"}, status=404)


async def _resolve_crew_id(state: "DashboardState", crew: str) -> "tuple[str, str]":
    """Map a crew id-or-name to ``(instance_id, "")``, or ``("", <refusal code>)``.

    ``reg.list`` re-reads instances.json under a threading lock, so it is run off
    the loop (same rule as every other registry touch in these handlers).

    An exact id is resolved FIRST across ALL entries, so an id can never be
    shadowed by an earlier entry whose NAME happens to equal it; only then is the
    value tried as a name, and a name resolves only when EXACTLY ONE crew carries
    it (``CREW_AMBIGUOUS_CODE`` otherwise). ``crew_unknown`` covers an empty value,
    an unreadable registry and a value no entry matches alike: an unknown crew is
    refused, never read as a wider scope.

    The caller's value arrived through :func:`sanitize_string` (NFC, hidden
    characters dropped); registry names are stored as typed. A name is therefore
    compared through the same pipeline, so a name the owner typed with a
    decomposed accent still resolves, and two names that differ only in a form
    the pipeline folds away are ambiguous rather than silently the first one.
    Ids are slugs (``_slugify``) and are compared as stored.
    """
    if not crew:
        return "", "crew_unknown"
    try:
        reg = _registry(state)
        entries = await asyncio.to_thread(reg.list)
    except Exception:
        return "", "crew_unknown"
    for e in entries:
        if getattr(e, "id", None) == crew:
            return e.id, ""
    named = [
        e.id for e in entries if sanitize_string(str(getattr(e, "name", "") or "")).strip() == crew
    ]
    if len(named) == 1:
        return named[0], ""
    if named:
        return "", CREW_AMBIGUOUS_CODE
    return "", "crew_unknown"


def _peer_status_error(status: int, *, names_a_session: bool) -> ProxyRequestError:
    """The typed failure for a peer's non-2xx answer to a crew read.

    Three outcomes, because the caller acts differently on each: a ``503`` is a
    retryable state on the peer (its transcript lock is busy, or its own manager is
    not up) and is passed on as a ``503`` so the agent retries rather than
    reporting a broken peer; a ``404`` on a path that names one session
    (*names_a_session*) is a key that names nothing on that peer; anything else
    is a refusal the peer chose, reported with its status and nothing from its
    body. The wording is neutral on purpose: the hub does not read the peer's
    body, so it does not claim to know WHICH retryable state or refusal it was.
    """
    if status == 503:
        return ProxyRequestError(
            "crew_peer_busy", "peer is temporarily unavailable; retry", http_status=503
        )
    if status == 404 and names_a_session:
        return ProxyRequestError(
            "crew_session_not_found", "no such session on that crew", http_status=404
        )
    return ProxyRequestError(
        "crew_peer_refused", f"peer refused the read (HTTP {status})", http_status=502
    )


async def _crew_proxy_json(
    mgr,
    instance_id: str,
    path: str,
    params: "dict[str, str]",
    *,
    shape: "Callable[[object], _T]",
    confirm_header: str = "",
    names_a_session: bool = False,
) -> _T:
    """GET a peer path over the tunnel; decode the reply and return ``shape`` of it.

    ``shape`` is the caller's row-shaping pass -- the per-field redaction and
    clamping of the peer's untrusted text -- and it is given the decoded JSON
    (``list`` or ``dict``), or ``None`` for a 2xx reply that is empty or not JSON,
    which every shaper reports as malformed. Decode and shape run as ONE unit so a
    reply over ``_OFF_LOOP_BYTES`` takes both off the event loop in a single
    thread hop; a smaller reply runs them inline. They run outside the peer's time
    budget below: a thread cannot be cancelled, and a slow redaction is not a slow
    peer, so it must not be reported as one.

    Body reading is delegated to ``read_capped_response`` — the same helper the
    adopt path and the peer-slots read use — because a single ``read(n)`` on a
    chunked reply resolves at the first buffered chunk and silently yields a
    TRUNCATED body. It enforces ``PEER_SESSIONS_REPLY_MAX_BYTES`` against the
    accumulated total and stops mid-stream, so a hostile peer cannot exhaust hub
    memory before any per-field clamp runs, and returns at most ``cap + 1`` bytes
    so ``len(body) > cap`` remains the over-cap sentinel.

    Bounded by a total-time budget (``_CREW_PROXY_TIMEOUT``) so a reachable-but-
    slow peer returns within the MCP client's own read timeout instead of being
    misreported as unreachable. Raises ``ProxyRequestError`` for every outcome
    the caller should name -- transport, credential and timeout failures, the
    peer's own non-2xx (:func:`_peer_status_error`, so its retryable ``503`` and
    its ``404`` survive the hop) and a reply over the read cap
    (``crew_reply_too_large``). Returns ``None`` only for a reply that is 2xx but
    not JSON, which the caller reports as malformed. ``names_a_session`` says the
    path addresses ONE session, so a peer ``404`` means that key names nothing
    there; on a list path a ``404`` is a refusal like any other.

    ``confirm_header`` names a response header the peer MUST set for a 2xx reply
    to count. It is the capability gate for a parameter the peer could silently
    ignore: a gateway from before the parameter existed answers 200 with the
    unfiltered body and no header, and that reply is refused here as
    ``crew_peer_too_old`` BEFORE its body is read, so the hub never holds rows the
    peer could not vouch for. The check is on the reply itself rather than on a
    version table, so it needs no list of which builds understand which parameter
    and cannot drift from one. Empty means no confirmation is required.
    """

    async def _fetch():
        async with mgr.proxy_request(instance_id, "GET", path, params=params) as resp:
            if not 200 <= resp.status < 300:
                # The peer's status is the diagnosis, so it is kept rather than
                # folded into "malformed": a 503 is the transcript lock the peer's
                # exclude_incognito arm reports as retryable, and a 404 is a key
                # that names nothing there. The peer's body is NOT echoed -- it is
                # untrusted text and the status already says what happened.
                raise _peer_status_error(resp.status, names_a_session=names_a_session)
            if confirm_header and not resp.headers.get(confirm_header):
                raise ProxyRequestError(
                    "crew_peer_too_old",
                    "peer did not confirm the incognito exclusion; update the peer",
                    http_status=502,
                )
            try:
                body = await read_capped_response(resp, PEER_SESSIONS_REPLY_MAX_BYTES)
            except Exception as e:
                # An INTERRUPTED body is its own failure. The carrier wraps only
                # the phase BEFORE the response is yielded, so a tunnel that drops
                # mid-body (``ClientConnectionError``) or a chunked reply that ends
                # short of its declared length (``ClientPayloadError``) raises
                # straight through here, past ``wait_for``'s timeout arm and the
                # handlers' ``except ProxyRequestError``: an unaudited 500 for a
                # peer-side fault. Same guard as the peer-slots read. Refused
                # rather than decoding the prefix: a short transcript and a
                # complete one are indistinguishable here. Only the exception
                # TYPE is logged, never the partial (untrusted) body.
                # ``CancelledError`` is a BaseException and is NOT absorbed.
                logger.info(
                    "crew read %s from %s was interrupted (%s)",
                    path,
                    instance_id,
                    type(e).__name__,
                )
                raise ProxyRequestError(
                    "proxy_peer_unreachable",
                    "peer reply was interrupted before the body completed",
                    http_status=502,
                ) from e
        if len(body) > PEER_SESSIONS_REPLY_MAX_BYTES:
            # Over the reply cap is a size the hub refuses, not a peer that
            # answered garbage; a session that outgrew the bound is told so.
            raise ProxyRequestError(
                "crew_reply_too_large",
                "peer reply exceeds the hub's read cap",
                http_status=502,
            )
        return body

    try:
        body = await asyncio.wait_for(_fetch(), timeout=_CREW_PROXY_TIMEOUT)
    except asyncio.TimeoutError:
        raise ProxyRequestError(
            "proxy_peer_timeout",
            "peer did not return within the read budget",
            http_status=504,
        ) from None
    if not body:
        return shape(None)
    if len(body) > _OFF_LOOP_BYTES:
        return await asyncio.to_thread(_decode_and_shape, body, shape)
    return _decode_and_shape(body, shape)


def _shape_search_rows(payload: object, limit: int) -> "list[dict] | None":
    """The hub's rows for a peer search reply, or ``None`` for a malformed reply."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("sessions")
    out: list[dict] = []
    if isinstance(rows, list):
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            if not isinstance(key, str) or not key or len(key) > _PEER_FIELD_MAX_CHARS:
                continue
            if _peer_row_is_restricted(row.get("memory_mode")):
                continue  # mirror the local tool's incognito/temporary exclusion
            out.append(
                {
                    "key": key,
                    "title": _cap_field(row.get("title")),
                    "snippet": _cap_field(row.get("snippet")),
                    "date": _cap_field(row.get("created") or row.get("date")),
                }
            )
    return out


def _shape_list_rows(payload: object, limit: int) -> "list[dict] | None":
    """The hub's rows for a peer session-list reply, or ``None`` for a malformed reply."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("sessions")
    out: list[dict] = []
    if isinstance(rows, list):
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            if not isinstance(key, str) or not key or len(key) > _PEER_FIELD_MAX_CHARS:
                continue
            if _peer_row_is_restricted(row.get("memory_mode")):
                continue  # mirror the local tool's incognito/temporary exclusion
            item: dict = {"key": key, "title": _cap_field(row.get("title"))}
            for field in ("agent", "created"):
                redacted = _cap_field(row.get(field))
                if redacted:
                    item[field] = redacted
            msgs = row.get("messages")
            if isinstance(msgs, (int, float)) and not isinstance(msgs, bool):
                try:
                    if math.isfinite(msgs):
                        item["messages"] = msgs
                except OverflowError:
                    pass
            preview = row.get("preview")
            if isinstance(preview, str) and preview:
                item["preview"] = _cap_field(preview)
            out.append(item)
    return out


def _shape_read_messages(payload: object, max_messages: int) -> "list[dict] | None":
    """The hub's messages for a peer transcript reply, or ``None`` for a malformed reply.

    Mirrors get_chat_session: keep only RECALL_ROLES (drops system/tool/thinking
    rows the local tool excludes), THEN tail-cap to max_messages.
    """
    # api_session_detail returns a raw message ARRAY (not wrapped).
    if not isinstance(payload, list):
        return None
    recall = [m for m in payload if isinstance(m, dict) and str(m.get("role", "")) in RECALL_ROLES]
    out: list[dict] = []
    for message in recall[-max_messages:]:
        role = message.get("role")
        out.append(
            {
                "role": str(role)[:64] if isinstance(role, str) and role else "?",
                # A transcript body is legitimately long, so it takes the
                # per-message ceiling rather than the short-field clamp. _cap_str
                # still runs the redaction chain and strips hidden Unicode first,
                # which is what stops a credential smuggled through a zero-width
                # character from surviving into the agent's context.
                "content": _cap_str(message.get("content"), _PEER_MESSAGE_MAX_CHARS),
            }
        )
    return out


async def api_crew_sessions_search(request: web.Request) -> web.Response:
    """GET /api/crew-sessions/search?crew=&q=&limit= — MCP crew-scoped search.

    Rides the generic carrier like the other two legs, because the search needs
    the peer's confirmation HEADER and ``search_sessions_remote`` returns only
    the body; re-redacts every peer row here, since the peer's text is untrusted.
    """
    denied = await _authorize_crew_read(request, "crew_search")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    crew = sanitize_string(request.query.get("crew", "")).strip()[:MAX_SHORT_STRING]
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        # A sub-threshold query still passed the internal-secret gate, so record
        # the decision (mirrors api_instances_search_sessions' short-query audit).
        _audit_crew(
            request,
            "crew_search",
            "success",
            request_id="short-query",
        )
        return web.json_response({"sessions": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "10")), 50))
    except (TypeError, ValueError):
        limit = 10
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        # No tunnel manager: instances off, or a headless gateway, which builds
        # none. Its own answer, not "unknown crew" -- the crew may well be in
        # the registry; what is missing is the thing that could reach it.
        _audit_crew(
            request, "crew_search", "failure", request_id=crew, error=MANAGER_UNAVAILABLE_CODE
        )
        return _manager_unavailable_response()
    iid, refusal = await _resolve_crew_id(state, crew)
    if refusal:
        _audit_crew(request, "crew_search", "denied", request_id=crew, error=refusal)
        return _crew_refusal(refusal)
    try:
        # exclude_incognito=1 on the peer's search: it judges each match against
        # its on-disk line and re-reads the snippet under ONE lock hold, drops
        # restricted matches before its limit, and confirms with the header; a
        # peer that cannot confirm is refused unread, as on the other two legs.
        out = await _crew_proxy_json(
            mgr,
            iid,
            "/api/sessions/search",
            {"q": q, "limit": str(limit), "exclude_incognito": "1"},
            shape=partial(_shape_search_rows, limit=limit),
            confirm_header=INCOGNITO_EXCLUDED_HEADER,
        )
    except ProxyRequestError as e:
        _audit_crew(request, "crew_search", "failure", request_id=iid, error=e.code)
        return web.json_response({"error": e.message, "code": e.code}, status=e.http_status)
    if out is None:
        _audit_crew(request, "crew_search", "failure", request_id=iid, error="malformed")
        return web.json_response(
            {"error": "peer returned a malformed reply", "code": "crew_malformed_reply"},
            status=502,
        )
    _audit_crew(
        request,
        "crew_search",
        "success",
        request_id=iid,
    )
    return web.json_response({"sessions": out})


async def api_crew_sessions_list(request: web.Request) -> web.Response:
    """GET /api/crew-sessions/list?crew=&limit= — MCP crew-scoped session list."""
    denied = await _authorize_crew_read(request, "crew_list")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    crew = sanitize_string(request.query.get("crew", "")).strip()[:MAX_SHORT_STRING]
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 100))
    except (TypeError, ValueError):
        limit = 20
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        # No tunnel manager: instances off, or a headless gateway, which builds
        # none. Its own answer, not "unknown crew" -- the crew may well be in
        # the registry; what is missing is the thing that could reach it.
        _audit_crew(
            request, "crew_list", "failure", request_id=crew, error=MANAGER_UNAVAILABLE_CODE
        )
        return _manager_unavailable_response()
    iid, refusal = await _resolve_crew_id(state, crew)
    if refusal:
        _audit_crew(request, "crew_list", "denied", request_id=crew, error=refusal)
        return _crew_refusal(refusal)
    try:
        # exclude_incognito=1: the peer drops restricted rows BEFORE paging (so
        # ``limit`` counts eligible sessions) and re-judges each page row's
        # on-disk line under its lock beside the preview read; the header is its
        # proof, and a peer that cannot give it is refused unread.
        out = await _crew_proxy_json(
            mgr,
            iid,
            "/api/sessions",
            {"limit": str(limit), "preview": "1", "exclude_incognito": "1"},
            shape=partial(_shape_list_rows, limit=limit),
            confirm_header=INCOGNITO_EXCLUDED_HEADER,
        )
    except ProxyRequestError as e:
        _audit_crew(request, "crew_list", "failure", request_id=iid, error=e.code)
        return web.json_response({"error": e.message, "code": e.code}, status=e.http_status)
    if out is None:
        _audit_crew(request, "crew_list", "failure", request_id=iid, error="malformed")
        return web.json_response(
            {"error": "peer returned a malformed reply", "code": "crew_malformed_reply"},
            status=502,
        )
    _audit_crew(
        request,
        "crew_list",
        "success",
        request_id=iid,
    )
    return web.json_response({"sessions": out})


async def api_crew_sessions_read(request: web.Request) -> web.Response:
    """GET /api/crew-sessions/read?crew=&key=&max_messages= — MCP transcript read.

    The peer route is ``/api/sessions/{key}``; the key is vetted (no separators,
    no ``..``) and percent-encoded into the path so it cannot traverse.
    """
    denied = await _authorize_crew_read(request, "crew_read")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    crew = sanitize_string(request.query.get("crew", "")).strip()[:MAX_SHORT_STRING]
    key = request.query.get("key", "")
    try:
        max_messages = max(1, min(int(request.query.get("max_messages", "50")), 200))
    except (TypeError, ValueError):
        max_messages = 50
    if (
        not key
        or "/" in key
        or "\\" in key
        or key in ("..", ".")
        or len(key) > _PEER_FIELD_MAX_CHARS
    ):
        # A refused key is a denied crew read like every other arm's refusal, and
        # it is audited as one: the audit middleware skips GETs, so without this
        # row SEL would hold the caller's authorization and not the decision that
        # turned the traversal attempt away.
        _audit_crew(request, "crew_read", "denied", request_id=crew, error="crew_bad_key")
        return web.json_response(
            {"error": "invalid session_key", "code": "crew_bad_key"}, status=400
        )
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        # No tunnel manager: instances off, or a headless gateway, which builds
        # none. Its own answer, not "unknown crew" -- the crew may well be in
        # the registry; what is missing is the thing that could reach it.
        _audit_crew(
            request, "crew_read", "failure", request_id=crew, error=MANAGER_UNAVAILABLE_CODE
        )
        return _manager_unavailable_response()
    iid, refusal = await _resolve_crew_id(state, crew)
    if refusal:
        _audit_crew(request, "crew_read", "denied", request_id=crew, error=refusal)
        return _crew_refusal(refusal)
    peer_path = "/api/sessions/" + quote(key, safe="")
    try:
        # exclude_incognito=1: the peer refuses (empty) an incognito/temporary
        # transcript, so this read enforces the same EB-7b exclusion the local
        # get_chat_session does — not just the search/list discovery filter.
        # The peer proves it understood the parameter with INCOGNITO_EXCLUDED_HEADER
        # on its reply; a reply without it is an older peer serving the unfiltered
        # transcript, and it is refused unread. Enforced here, by the reader, not
        # merely honored by the peer.
        # Incognito is enforced on BOTH legs: search/list drop incognito/temporary
        # rows (discovery), and this read sends exclude_incognito=1 AND requires
        # the peer's confirmation header, so a marked session comes back empty and
        # an older peer that could not confirm is never shaped at all.
        out = await _crew_proxy_json(
            mgr,
            iid,
            peer_path,
            {"exclude_incognito": "1"},
            shape=partial(_shape_read_messages, max_messages=max_messages),
            confirm_header=INCOGNITO_EXCLUDED_HEADER,
            names_a_session=True,
        )
    except ProxyRequestError as e:
        _audit_crew(request, "crew_read", "failure", request_id=iid, error=e.code)
        return web.json_response({"error": e.message, "code": e.code}, status=e.http_status)
    if out is None:
        _audit_crew(request, "crew_read", "failure", request_id=iid, error="malformed")
        return web.json_response(
            {"error": "peer returned a malformed reply", "code": "crew_malformed_reply"},
            status=502,
        )
    _audit_crew(
        request,
        "crew_read",
        "success",
        request_id=iid,
    )
    return web.json_response({"messages": out})
