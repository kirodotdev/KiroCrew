"""Routes behind the session-control MCP tools.

Strict-internal (loopback + ``X-Internal-Secret``): no browser calls these, and
they are the entry point to acting on another live conversation, so a cookie
fall-through would be a genuinely new path rather than a convenience.

The handlers do parsing and status mapping only — resolution, authorization and
the operations themselves live in ``dashboard/session_control.py`` so the verbs
that take a target share one guard.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers._shared import (
    _read_session_key,
    internal_memory_scope,
    member_scope_denied_refusal,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Request-scoped mark ``_private_caller_refusal`` sets when it admits the caller
# AS A CREW MEMBER (either spelling). Read back by :func:`_carried_fence` on every
# route, never by anything outside this module.
_MEMBER_ADMITTED = "session_control_member_admitted"


def _carried_fence(request: web.Request) -> bool | None:
    """The ownership-fence verdict this request's admission already settled.

    ``True`` when the gate admitted the caller as a crew member — a member is
    ALWAYS creator-fenced, and that decision was made on the caller's verified
    scope, so the route hands it to ``session_control.py`` as
    ``precomputed_ownership_fenced`` rather than letting the fence re-derive it
    from the config record after the body read has suspended. ``None`` for every
    other caller (owner / Global-V1), whose fence is evaluated inline as before.
    """
    return True if request.get(_MEMBER_ADMITTED) is True else None


async def _private_caller_refusal(request: web.Request) -> web.Response | None:
    """The execution-scope gate in front of the session-control routes.

    Runs only on the authenticated strict-internal branch (``internal_auth`` is
    ``True``). Captures the caller's canonical execution scope ONCE off-loop via
    :func:`internal_memory_scope`:

    * a capture failure returns that refusal verbatim
      (``member_identity_unavailable`` 409 for an unavailable or mismatched
      identity); ordinary transport/auth refusals retain their own status;
    * an owner / Global-V1 caller (``scope is None``) falls through to the
      handler, exactly as the surface behaved before member dispatch existed;
    * a scoped caller is admitted ONLY when it is a crew-member DM
      slot (``member-*`` session key) AND the surface is reachable for it — the
      member operating model, gated the rest of the way by
      ``session_control.py``'s own creator-ownership fence. Every other scoped
      caller gets the ``member_scope_denied`` 403 refusal.

    Reachable means ``member_dispatch_enabled()`` OR ``session_control_enabled()``
    is true, mirroring ``session_control.py``'s ``_member_bypass`` contract: the
    ``member_dispatch`` ceiling is a bypass ON TOP of the global switch, not a
    replacement for it. With ``member_dispatch`` off a member falls back UNDER
    the global switch (``_member_bypass``'s docstring and session-control.md
    "Member callers"), so a member is admitted whenever the switch is on even
    though its own bypass is withdrawn — refusing it there would put a member
    OUT of the surface the operator left open to everyone, not merely strip its
    bypass. Both reads run off the loop and fail closed on an unreadable config,
    so this gate can never open wider than the two switches behind it. The
    refusal is emitted through :func:`member_scope_denied_refusal` — the same
    audit and body :func:`private_owner_surface_refusal` produces — rather than
    by re-resolving the scope a second time.
    """
    scope, refusal = await internal_memory_scope(request, "session_control")
    if refusal is not None:
        return refusal
    if scope is None:
        # Owner / Global-V1 caller: no store scope, so nothing to refuse here.
        return None
    # A scoped caller. Admit ONLY a member DM slot while the surface
    # is reachable for it: the member's own ``member_dispatch`` bypass OR the
    # global ``session_control`` switch it otherwise falls back under, since
    # ``member_dispatch`` is a bypass ON TOP of the switch, not a replacement.
    #
    # "Crew member" here has the two spellings ``session_control.py``'s inner
    # fence recognises, and it MUST agree with that fence or the gate would admit
    # a caller the fence then refuses (or vice versa):
    #
    # * (a) a ``member-*`` DM SLOT (the existing PR behaviour), and
    # * (b) an ordinary dashboard chat slot whose bound memory store is a crew
    #   member's private V2 store — the store ``internal_memory_scope`` already
    #   resolved as ``scope``. ``scope`` is non-empty only for a V2 store, but
    #   this asks ``_store_is_member_owned`` the FURTHER question — is that V2
    #   store ``owner_member``-tagged — so admission never widens to a private V2
    #   caller whose store is not a crew member's, and the gate and the fence
    #   read member-ownership from the SAME config-record predicate.
    #
    # This is the surface-level, caller-independent reachability; the per-caller
    # ownership form ``session_control.py`` enforces inside
    # ``create_session``/``authorize_target`` (``_member_bypass`` /
    # ``_caller_is_ownership_fenced``, which bound a member to slots it created)
    # is a different predicate and stays there. All reads run off the loop in one
    # hop and fail closed on an unreadable config, so this gate never opens wider
    # than the two switches behind it.
    session_key = _read_session_key(request)

    def _caller_is_member_and_reachable() -> bool:
        # The SHARED predicate the chat folder/tag gate also uses
        # (``handlers/_shared.py``'s ``private_chat_route_refusal``), so the two
        # surface gates cannot drift on who a member is or when the surface is
        # reachable for it.
        return sc.member_admitted_to_scoped_surface(session_key, scope)

    if await asyncio.to_thread(_caller_is_member_and_reachable):
        # Admitted AS A MEMBER. Record that on the request so the route carries it
        # into ``authorize_target`` (``precomputed_ownership_fenced=True``): the
        # inner fence would otherwise re-derive member status from the MUTABLE
        # config record after the body read and the prewarms have suspended, and
        # an operator's own writer can flip that record in the window — un-assign
        # the member, drop ``memory_version`` (coerced to ``1`` by the loader), drop
        # the entry. Any of those would turn an admitted member into an unfenced
        # caller reaching a foreign same-workspace session. The admission was made
        # on the VERIFIED scope; it is the decision to keep.
        request[_MEMBER_ADMITTED] = True
        return None
    return await member_scope_denied_refusal("session_control")


async def _require_internal(request: web.Request) -> web.Response | None:
    """Refuse anything that did not present ``X-Internal-Secret``.

    These paths are on ``_STRICT_INTERNAL_API_PATHS``, but strict is not
    self-enforcing at the handler: with the header ABSENT the middleware falls
    through to cookie auth, and a ``local_only=False`` deployment reclassifies
    every strict path as "mixed". Either way a same-origin page holding only a
    dashboard cookie could reach here and — by choosing ``X-Session-Key`` —
    message, stop, or read any of the user's sessions AS one of them. The
    session key is an identity claim these routes authorize on, so it has to be
    backed by the secret rather than by whatever a browser sends.

    ``internal_auth`` is set only after a constant-time secret match, so
    requiring it closes the cookie path, the app-token path, and the
    non-loopback reclassification in one check. Returns the refusal, or ``None``
    when the caller is authentic.

    A crew-member DM slot is the ONE kind of scoped caller admitted here
    rather than refused: dispatching work into worker sessions it creates is the
    member operating model, so the surface lets it through to
    ``session_control.py``, where the SAME ownership fence every member caller is
    bound by (``authorize_target``'s ``not_creator``, and ``create_session``'s
    agent-workspace check) does the real gating. The admission is bounded by the
    surface being reachable for a member — its own ``agent.member_dispatch``
    bypass, OR the global ``agent.session_control`` switch it otherwise falls
    back under. With both off the member is refused here like any other scoped
    caller. Every OTHER scoped caller keeps the
    ``member_scope_denied`` refusal, and an owner / Global-V1 caller falls through
    exactly as before.
    """
    if request.get("internal_auth") is True:
        return await _private_caller_refusal(request)
    # Best-effort, the property `_audit_denied` exists to carry for exactly this
    # shape of site: a refusal logged BEFORE the audit middleware has run.
    # `log_api_access` only enqueues — SEL is warmed at gateway startup
    # (sel.warm_sel_singleton), so no thread hop is needed. Construction
    # can still raise on a FAILED warm (a trust root too short to sign the
    # chain), which unguarded would turn this 403 into a 500: losing the
    # denial in order to report it.
    try:
        sel().log_api_access(
            caller="unknown",
            operation=f"session_control.{request.path.rsplit('/', 1)[-1]}",
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="internal secret required",
        )
    except Exception:
        logger.warning("Failed to log a session-control denial to SEL", exc_info=True)
    return web.json_response({"error": "forbidden", "code": "internal_secret_required"}, status=403)


def _refusal(exc: sc.SessionControlError) -> web.Response:
    """Render a :class:`SessionControlError` as its HTTP response.

    Written as an explicit branch per status rather than
    ``status=exc.status`` so the route can only ever answer with a status from
    this closed set: an unmapped value degrades to 400 instead of forwarding
    whatever integer reached it. ``code`` is the field callers match on;
    ``message`` is advisory prose.
    """
    if exc.status == 403:
        return web.json_response({"error": exc.message, "code": exc.code}, status=403)
    if exc.status == 404:
        return web.json_response({"error": exc.message, "code": exc.code}, status=404)
    if exc.status == 409:
        return web.json_response({"error": exc.message, "code": exc.code}, status=409)
    if exc.status == 429:
        return web.json_response({"error": exc.message, "code": exc.code}, status=429)
    if exc.status == 500:
        # A genuine server-side failure: `close_target` raises this for the four
        # close-path failures (history write running / nudge retire / app hook /
        # history save), each of which left the tab open with every partial step
        # rolled back. It is not a client error, so it must not degrade to 400.
        return web.json_response({"error": exc.message, "code": exc.code}, status=500)
    return web.json_response({"error": exc.message, "code": exc.code}, status=400)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise sc.SessionControlError("invalid JSON", code="invalid_json")
    if not isinstance(body, dict):
        raise sc.SessionControlError("body must be a JSON object", code="invalid_body")
    return body


def _target(body: dict) -> str:
    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        raise sc.SessionControlError("target is required", code="target_required")
    return target.strip()


async def api_session_control_create(request: web.Request) -> web.Response:
    """POST /api/session-control/create — open a session this caller will own."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        # Warmed AFTER the body read, which suspends: a config edit landing in that
        # window would change the fingerprint and leave `create_session`'s own
        # synchronous gate re-reading the file on the loop. Nothing suspends between
        # here and that gate, which is the first thing `create_session` does.
        await sc.prewarm_enabled_check()
        result = await sc.create_session(
            state,
            caller_session_key=_read_session_key(request),
            title=str(body.get("title") or ""),
            agent=str(body.get("agent") or ""),
            folder_id=str(body.get("folder_id") or ""),
            # The fence verdict this request's admission already settled, for the
            # same reason every other route forwards it as
            # `precomputed_ownership_fenced`: `create_session` consults it after
            # many suspensions, and the inline predicate re-derives member status
            # from the mutable config record. Here it decides whether the child
            # may be bound to a member's private store.
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_fork(request: web.Request) -> web.Response:
    """POST /api/session-control/fork — open a session carrying another's transcript."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        # Strictly typed: this body is model-controlled, and `bool` is an `int`
        # subclass, so `True` would otherwise read as fork point 1.
        at_index = body.get("at_message_index")
        if at_index is not None and (isinstance(at_index, bool) or not isinstance(at_index, int)):
            raise sc.SessionControlError(
                "at_message_index must be a non-negative integer", code="invalid_field_type"
            )
        source = body.get("source", "")
        if not isinstance(source, str):
            raise sc.SessionControlError("source must be a string", code="invalid_field_type")
        # Warmed AFTER the body read, for the reason `api_session_control_create`
        # gives: nothing suspends between here and `fork_session`'s own gate.
        await sc.prewarm_enabled_check()
        result = await sc.fork_session(
            state,
            caller_session_key=_read_session_key(request),
            source=source,
            title=str(body.get("title") or ""),
            folder_id=str(body.get("folder_id") or ""),
            at_message_index=at_index,
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_stop(request: web.Request) -> web.Response:
    """POST /api/session-control/stop — stop another session's in-flight turn."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `stop_target` warms the config after its own SEL prewarm,
    # which is the last suspension before the gate. Warming here as well would be
    # dead work -- the body read and that SEL await both sit in between.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.stop_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_close(request: web.Request) -> web.Response:
    """POST /api/session-control/close — archive another session (tab ✕)."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `close_target` warms the SEL logger and the config after
    # its own SEL prewarm, the same ordering `stop_target`/`send_to_target` use
    # and for the same reason — the body read above is a suspension point.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.close_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_send(request: web.Request) -> web.Response:
    """POST /api/session-control/send — deliver a message to another session."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `send_to_target` warms the config after its own SEL
    # prewarm, the same ordering `stop_target` uses and for the same reason.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise sc.SessionControlError("message is required", code="message_required")
        # Strictly typed, never truthiness: this body is app-controlled, and
        # coercing "false" (a non-empty string) into True would silently cut into
        # a running turn for a caller that asked for the queue.
        steer = body.get("steer", False)
        if not isinstance(steer, bool):
            raise sc.SessionControlError("steer must be a boolean", code="invalid_steer")
        result = await sc.send_to_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            message=message,
            steer=steer,
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_adopt(request: web.Request) -> web.Response:
    """POST /api/session-control/adopt — take another session under this one."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.adopt_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_release(request: web.Request) -> web.Response:
    """POST /api/session-control/release — let a session out from under its parent."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.release_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_read(request: web.Request) -> web.Response:
    """GET /api/session-control/read — read another session's transcript tail."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # Stays at the top for read alone: everything between here and the gate is
    # synchronous query parsing (`request.query` is already available and
    # `read_messages` does not await), so there is no suspension to invalidate it.
    await sc.prewarm_enabled_check()
    state: DashboardState = request.app["state"]
    try:
        target = (request.query.get("target") or "").strip()
        if not target:
            raise sc.SessionControlError("target is required", code="target_required")
        limit_raw = request.query.get("limit")
        since_raw = request.query.get("since")
        try:
            limit = int(limit_raw) if limit_raw else sc.DEFAULT_READ_MESSAGES
            since = int(since_raw) if since_raw else None
        except ValueError:
            raise sc.SessionControlError(
                "limit and since must be integers", code="invalid_pagination"
            )
        result = sc.read_messages(
            state,
            caller_session_key=_read_session_key(request),
            target=target,
            limit=limit,
            since=since,
            caller_fenced=_carried_fence(request),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    await sc.mark_reader_for_mirrored_target(
        state,
        caller_session_key=_read_session_key(request),
        target_slot_key=str(result.get("target") or ""),
    )
    return web.json_response(result)
