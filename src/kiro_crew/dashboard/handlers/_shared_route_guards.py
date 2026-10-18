"""The route-level refusals handler modules share, and the owner-surface guard.

What a cron session may not control, who may create a cron job and in which mode,
which chat folder and tag routes a private member is admitted to, and
``guard_owner_surface_routes``, which wraps every route of a handler module as an
owner surface unless the module names it as member- or resource-scoped.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from kiro_crew.dashboard.handlers._shared_access import require_owner_for_dashboard_caller
from kiro_crew.dashboard.handlers._shared_member_scope import (
    _audit_private_memory_denial,
    internal_memory_scope,
    member_request_scope,
)
from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


#: The three chat routes a script cron opens, seeds and sets the approval mode of
#: its sessions on. Session control is what ``agent.session_control`` switches
#: off, and these are its writes.
_CRON_SESSION_CONTROL_PATHS = frozenset(
    {
        "/api/chat",
        "/api/chat/",
        "/api/chat/slots",
        "/api/chat/slots/",
        "/api/chat/mode",
        "/api/chat/mode/",
    }
)


async def _audit_cron_chat_denial(request: web.Request, error: str, resources: str = "") -> None:
    """Record a cron's refused chat-route call, best-effort, off the loop.

    One shape for every refusal the cron gate on these routes makes, so the
    switch, the creator fence and the mode rule read alike in the log. The SEL
    write can touch the disk, so it runs in a thread, and its failure is logged
    rather than raised: an audit that cannot be written must not turn a
    refusal into an admission.
    """

    def _write() -> None:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller="internal",
            operation="chat.control",
            outcome="denied",
            source="session_control",
            resources=resources or request.path,
            error=error,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception:
        logger.debug("SEL audit for a refused cron chat call (%s) failed", error, exc_info=True)


async def _cron_session_control_refusal(request: web.Request) -> web.Response | None:
    """Refuse a cron's session-control write while ``agent.session_control`` is off.

    This sits in :func:`private_chat_route_refusal` because that is the one gate
    every internal chat-route call passes after the internal secret validates.
    Only a ``cron:`` key is checked, because the switch gates a cron caller the
    same way the session-control routes do. Owner and member callers keep their
    own gates. Only the three routes a script cron writes to are checked. The
    folder routes are not session control.
    """
    if request.method != "POST" or request.path not in _CRON_SESSION_CONTROL_PATHS:
        return None
    if not request.headers.get("X-Session-Key", "").startswith("cron:"):
        return None
    from kiro_crew.dashboard.session_control import session_control_enabled

    # The config read can touch the disk, so it runs off the loop.
    if await asyncio.to_thread(session_control_enabled):
        return None
    message = "session control is disabled in config (agent.session_control)"
    await _audit_cron_chat_denial(request, "session_control_disabled")
    return web.json_response({"error": message, "code": "session_control_disabled"}, status=403)


async def cron_slot_creator(request: web.Request) -> str:
    """The attested ``cron:<job id>`` key of the caller, or ``""`` for anyone else.

    The two slot-creating chat routes stamp a slot a cron opens with it: origin
    CRON and ``_created_by`` set, so the slot is neither counted nor exposed as a
    person's own tab. Read from the scope the chat-route gate already resolved
    for this request, so only a key the transport attests is given a cron's
    attribution; a caller that merely asserts the header gets none.
    """
    if request.get("internal_auth") is not True:
        return ""
    scope = await member_request_scope(request)
    session = scope.session or ""
    if not scope.verified or not session.startswith("cron:"):
        return ""
    return session


async def cron_creator_admission(
    request: web.Request, state: Any, slot_name: str | None, cron_creator: str
) -> tuple[web.Response | None, bool, dict[str, Any]]:
    """Judge a ``cron:`` caller and return its refusal and persisted snapshot.

    A script cron opens a slot with ``POST /api/chat/slots``, seeds it with
    ``POST /api/chat`` and sets its approval mode with ``POST /api/chat/mode``.
    The first two mint a fresh slot under any key that is not live, and all three
    act on whatever live slot a key names. This mirrors
    session-control's ``_created_by_other`` fence, so a cron reaches only slots
    it created: a live slot is judged on its ``_created_by``, and a key with no
    live slot is judged on the ``created_by`` its persisted metadata line
    records. Reading that line is what keeps a cron from minting a closed
    session's key as its own. A key with no live slot and no transcript is left
    to mint, as it is for any caller, and the new slot carries the cron as its
    creator. A transcript that cannot be read refuses. Like the switch check,
    this keys on the session key the caller presents.

    The persisted read runs off the loop. The live slot is judged again after
    that read, so a slot opened while it ran is judged as live. The caller
    therefore makes its mint decision with no await after this returns. The
    mode route mints nothing and calls this only once its slot is live, so for
    it the whole judgement is the synchronous ``_created_by`` read.
    The persisted flag and metadata describe the same read used for the creator
    verdict, so the create caller restores a closed session's saved title
    without a second store lookup.
    """
    if not cron_creator or not slot_name:
        return None, False, {}
    key = _normalize_slot_key(str(slot_name))
    if not key:
        return None, False, {}
    from kiro_crew.dashboard.session_control import _created_by_other

    persisted = False
    persisted_meta: dict[str, Any] = {}
    slot = state._slots.get(key)
    if slot is None:
        log = getattr(state, "conversation_log", None)
        if log is None:
            return None, False, {}
        from kiro_crew.dashboard.chat_utils import slot_transcript_key

        history_key = slot_transcript_key(key)

        def _persisted_slot() -> tuple[bool, dict[str, Any]]:
            if not log.has_log(history_key):
                return False, {}
            return True, log.get_metadata(history_key)

        try:
            persisted, persisted_meta = await asyncio.to_thread(_persisted_slot)
        except Exception:
            logger.debug("persisted creator of slot %s unreadable", key, exc_info=True)
            refused = True
        else:
            refused = persisted and _created_by_other(
                SimpleNamespace(_created_by=str(persisted_meta.get("created_by") or "")),
                cron_creator,
            )
        slot = state._slots.get(key)
        if slot is None and not refused:
            return None, persisted, persisted_meta
    if slot is not None and not _created_by_other(slot, cron_creator):
        return None, persisted, persisted_meta
    await _audit_cron_chat_denial(request, "not_creator", f"{request.path} slot={key}")
    return (
        web.json_response(
            {
                "error": "a scheduled run can only control sessions it created itself",
                "code": "not_creator",
            },
            status=403,
        ),
        persisted,
        persisted_meta,
    )


#: Approval modes that grant auto-approval to the SLOT they name, as opposed to
#: the process-global YOLO grant. ``api_chat_mode`` reads it to keep a slot-scoped
#: grant from revoking the global one, and :func:`cron_mode_refusal` reads it as
#: the whole allowlist for a ``cron:`` caller: both are creation-time postures for
#: one unattended session, while ``yolo`` is the global grant and ``normal`` is the
#: off-switch at any scope, so a cron gets neither. A tuple, not a set: membership
#: is tested against a request-supplied value, and tuple ``in`` compares by
#: equality rather than hashing, so a non-string body value such as a list
#: answers False instead of raising.
_SLOT_SCOPED_TRUST_MODES = ("trust", "trust_reads")


async def cron_mode_refusal(
    request: web.Request, cron_creator: str, mode: object, slot_name: object
) -> web.Response | None:
    """The mode rule for a ``cron:`` caller on ``POST /api/chat/mode``, or ``None``.

    ``ScriptContext.set_session_mode`` is the cron path to that route. A cron
    sets ``trust`` or ``trust_reads``, and nothing else, on the one slot it
    names: any other mode answers 403 ``mode_not_allowed`` and an unnamed slot
    answers 400 ``slot_required``, each audited. The mode is judged first, so a
    cron asking for ``yolo`` is refused as a cron before the request reaches
    governance or the safety override, and whatever the slot. A mode that is not
    a string at all, a list say, is refused the same way rather than raising. The
    creator fence, :func:`cron_creator_admission`, is the handler's job once the
    slot resolves. Like the other two checks, this keys on the attested key the
    caller presents.
    """
    if not cron_creator:
        return None
    where = f"{request.path} slot={slot_name or ''} mode={mode!r}"
    if mode not in _SLOT_SCOPED_TRUST_MODES:
        await _audit_cron_chat_denial(request, "mode_not_allowed", where)
        return web.json_response(
            {
                "ok": False,
                "error": "a scheduled run can set only trust or trust_reads on a session it created",
                "code": "mode_not_allowed",
            },
            status=403,
        )
    if not slot_name:
        await _audit_cron_chat_denial(request, "slot_required", where)
        return web.json_response(
            {
                "ok": False,
                "error": "a scheduled run must name the session it created",
                "code": "slot_required",
            },
            status=400,
        )
    return None


async def private_chat_route_refusal(request: web.Request) -> web.Response | None:
    """Keep member tools within their admitted chat controls."""
    switched_off = await _cron_session_control_refusal(request)
    if switched_off is not None:
        return switched_off
    scope, refusal = await internal_memory_scope(request, "chat.control")
    if refusal is not None:
        # Let the ordinary internal-auth middleware produce its established
        # caller_record_missing 403 for delegated work removed mid-run. Memory
        # routing must not turn that host/app security decision into a 409.
        session = request.headers.get("X-Session-Key", "")
        if session.startswith(("cron:", "subagent:")):
            from kiro_crew.dashboard.token_auth import caller_record_is_missing

            state = request.app.get("state")
            if state is not None and caller_record_is_missing(
                session,
                getattr(getattr(state, "crons", None), "_jobs", None),
                getattr(getattr(state, "subagents", None), "_agents", None),
            ):
                return None
    if refusal is not None or scope is None:
        return refusal
    # The follow-up card is an agent-facing callback on its current tab. Other
    # chat controls belong to the owner; private work uses scoped spawn/history.
    if request.method == "POST" and request.path.endswith("/followup"):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        key = request.headers.get("X-Session-Key", "")
        slot_name = request.match_info.get("slot", "")
        state = request.app["state"]
        slot = state._slots.get(slot_name)
        if (
            key.startswith("dashboard:")
            and slot_name == key.removeprefix("dashboard:")
            and slot is not None
            and effective_session_key(slot) == key
            and slot.memory_store == scope
        ):
            return None
    # A crew member is admitted to the chat FOLDER and TAG routes it needs to
    # organise its own worker sessions (session_create with folder=,
    # chat_folder_file_self, chat_tag_assign, chat_session_pin,
    # session_move_to_column), and to the
    # read-only session LIST its folder tools use to resolve its own slot -- the
    # surface session-control already opens to the same members. This is COARSE admission only: it lets
    # the caller reach the handler, whose own per-resource fence
    # (``chat_folders``/``chat_tags``' ``owner_app``/``folder_principal`` for the
    # tree, ``member_owns_slot`` for filing/tagging and the session-list filter,
    # ``_refuse_vocabulary_write`` for the shared tag list) is what decides which
    # folder or session it may touch or see. Every OTHER ``/api/chat/*`` route
    # keeps the owner-only refusal below.
    if await _member_admitted_to_chat_folder_tag_route(request, scope):
        return None
    return await private_owner_surface_refusal(request, "chat.control")


#: Methods a crew-member caller is admitted for on the chat folder/tag routes.
#: The path is matched STRUCTURALLY by :func:`_admitted_chat_route_methods`
#: against the exact registered patterns, never by a raw prefix, so a sibling
#: literal that shares a prefix (``/api/chat/folders/reorder``,
#: ``/api/chat/tag-columns/order``) is NOT admitted and keeps the owner-only
#: refusal.
#:
#: The admitted VERBS are exactly the ones a member may actually do -- a verb
#: whose handler has no member fence is not admitted here, so the gate can never
#: forward a request the fence would have to refuse (or, worse, one no fence
#: covers). Concretely: a member creates folders and renames/reparents its OWN
#: (POST + PATCH on the tree), but does NOT delete folders (delete is refused for
#: every agent principal); it READS the shared tag vocabulary (GET) but does NOT
#: coin/rename/delete tags (``chat_tags.api_chat_tag_delete`` has no
#: vocabulary fence at all, so admitting DELETE would let a member remove a
#: shared tag); it files/tags only its own or created sessions, and a tag it
#: assigns is still subject to the agent tag-grants policy
#: (``chat_tags.agent_tag_change_refusal``): it may apply a tag the owner opened
#: to agents (``add-only``/``add-remove``) or any rowless ordinary label, but a
#: tag the owner reserved (a protected ``none`` row) is refused the same as for
#: any other agent. The per-handler
#: ownership fence (``owner_app``/``folder_principal`` for the tree,
#: ``member_owns_slot`` for filing/tagging, ``_refuse_vocabulary_write`` for tag
#: creation/rename) is still the authoritative gate; this set just refuses to
#: forward anything outside a member's real capability.
_MEMBER_CHAT_FOLDERS_METHODS = frozenset({"GET", "POST"})


_MEMBER_CHAT_FOLDER_ID_METHODS = frozenset({"PATCH"})


#: The reorder (sibling-position) leg of a folder move; its handler fences every
#: row to the caller's own folder, so a member renumbers only what it owns.
_MEMBER_CHAT_FOLDER_REORDER_METHODS = frozenset({"POST"})


_MEMBER_CHAT_TAGS_METHODS = frozenset({"GET"})


#: The board's column list is admitted READ-only, like the tag vocabulary: it
#: names tags and live-state lanes, never a session. Its writes (POST, and the
#: ``/{id}`` and ``/order`` routes, which are not matched at all) stay refused;
#: ``_refuse_vocabulary_write`` in each write handler refuses a member too.
_MEMBER_CHAT_TAG_COLUMNS_METHODS = frozenset({"GET"})


_MEMBER_CHAT_SLOT_FOLDER_METHODS = frozenset({"PATCH"})


_MEMBER_CHAT_SLOT_TAGS_METHODS = frozenset({"PUT"})


#: Pinning a session (``chat_session_pin``); ``api_chat_slot_pin`` applies the
#: same ``member_owns_slot`` fence as filing and tagging.
_MEMBER_CHAT_SLOT_PIN_METHODS = frozenset({"PATCH"})


#: Moving a session into a board column (``session_move_to_column``);
#: ``api_chat_slot_drop`` applies the same ``member_owns_slot`` fence and the
#: agent tag-grants policy ``PUT /tags`` applies.
_MEMBER_CHAT_SLOT_DROP_METHODS = frozenset({"POST"})


#: The session LIST is admitted read-only: the folder/tag MCP tools
#: (chat_folder_file_self / chat_folder_move_session / chat_folder_tree) read it
#: to resolve the caller's own slot. ``api_chat_slots`` filters the response for
#: a member to its own + created sessions (the same set the tree read shows), so
#: admitting GET here does not widen what a member can enumerate.
_MEMBER_CHAT_SLOTS_METHODS = frozenset({"GET"})


def _admitted_chat_route_methods(path: str) -> frozenset[str] | None:
    """The methods a member is admitted for on *path*, or ``None`` if not admitted.

    Structural, path-shape matching that mirrors the routes registered in
    ``routes/sessions.py`` / ``routes/chat.py`` EXACTLY. A trailing single
    segment on ``/folders/`` is a folder id (``{id}``); the reserved literal
    ``/api/chat/folders/reorder`` and every ``/api/chat/tag-*`` except the
    read-only column list ``/api/chat/tag-columns`` are deliberately excluded.
    ``/api/chat/tags/{id}`` is NOT admitted for any method -- a member
    neither renames nor deletes shared tags -- so its DELETE (which has no
    vocabulary fence) is refused at the gate. ``/api/chat/slots`` is the session
    LIST only; a deeper ``/api/chat/slots/<slot>/...`` sub-resource other than
    the fenced ``/folder``, ``/tags``, ``/pin`` and ``/drop`` writes is NOT
    matched here.
    """
    if path in ("/api/chat/folders", "/api/chat/folders/"):
        return _MEMBER_CHAT_FOLDERS_METHODS
    if path in ("/api/chat/tags", "/api/chat/tags/"):
        return _MEMBER_CHAT_TAGS_METHODS
    if path in ("/api/chat/tag-columns", "/api/chat/tag-columns/"):
        return _MEMBER_CHAT_TAG_COLUMNS_METHODS
    if path in ("/api/chat/slots", "/api/chat/slots/"):
        return _MEMBER_CHAT_SLOTS_METHODS
    if path == "/api/chat/folders/reorder":
        # The sibling-position half of a folder MOVE. Admitted so a member's
        # combined reparent (PATCH /folders/{id}) + reorder does not commit only
        # the reparent and leave positioning half-applied. The reorder handler
        # fences every row to the caller's own folder (``folder_principal`` +
        # ``_subtree_holds_foreign_folder``), so a member can renumber only its
        # own folders.
        return _MEMBER_CHAT_FOLDER_REORDER_METHODS
    id_part = _single_id_segment(path, "/api/chat/folders/")
    if id_part is not None and id_part not in ("reorder", "cleanup"):
        return _MEMBER_CHAT_FOLDER_ID_METHODS
    slot = _single_id_segment(path, "/api/chat/slots/", suffix="/folder")
    if slot is not None:
        return _MEMBER_CHAT_SLOT_FOLDER_METHODS
    slot = _single_id_segment(path, "/api/chat/slots/", suffix="/tags")
    if slot is not None:
        return _MEMBER_CHAT_SLOT_TAGS_METHODS
    slot = _single_id_segment(path, "/api/chat/slots/", suffix="/pin")
    if slot is not None:
        return _MEMBER_CHAT_SLOT_PIN_METHODS
    slot = _single_id_segment(path, "/api/chat/slots/", suffix="/drop")
    if slot is not None:
        return _MEMBER_CHAT_SLOT_DROP_METHODS
    return None


def _single_id_segment(path: str, prefix: str, *, suffix: str = "") -> str | None:
    """The single path segment between *prefix* and *suffix*, or ``None``.

    Returns the segment only when *path* is exactly ``prefix<seg>suffix`` with a
    non-empty ``seg`` that itself contains no ``/`` -- so a deeper path (a
    sub-resource of ``{id}``/``{slot}``) does NOT match the one-segment route.
    """
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[: -len(suffix)]
    if not rest or "/" in rest:
        return None
    return rest


async def _member_admitted_to_chat_folder_tag_route(request: web.Request, scope: str) -> bool:
    """Whether this verified-scope caller is a member reaching an admitted route.

    ``scope`` is the store :func:`internal_memory_scope` already resolved and
    VERIFIED for this request. Admission is the SHARED member predicate
    (``session_control.member_admitted_to_scoped_surface``) the session-control
    gate uses, restricted to the exact ``(method, path shape)`` pairs
    :func:`_admitted_chat_route_methods` recognises. The config reads inside the
    predicate are blocking, so the whole test runs off the loop in one hop and
    fails closed.
    """
    allowed = _admitted_chat_route_methods(request.path)
    if allowed is None or request.method not in allowed:
        return False
    from kiro_crew.dashboard import session_control as sc

    session_key = request.headers.get("X-Session-Key", "").strip()

    def _admit() -> bool:
        return sc.member_admitted_to_scoped_surface(session_key, scope)

    if not await asyncio.to_thread(_admit):
        return False
    # Carry the VERIFIED member principal onto the request so the handler's
    # ownership fence (``chat_folders.folder_principal`` / ``member_owns_slot``)
    # reads it WITHOUT a second, loop-blocking config read after the body-parse
    # await -- the same "decide once on the verified scope, carry it" discipline
    # the session-control gate applies with ``precomputed_ownership_fenced``.
    try:
        request[MEMBER_CHAT_PRINCIPAL_KEY] = f"member:{scope}" if scope else ""
    except TypeError:  # a request double without item assignment
        pass
    return True


async def member_scope_denied_refusal(operation: str) -> web.Response:
    """The ``member_scope_denied`` 403, audited under *operation*.

    Extracted so a caller that has ALREADY resolved a private scope (the
    session-control gate, which then decides member callers separately) can emit
    the exact same refusal and audit line as :func:`private_owner_surface_refusal`
    WITHOUT re-running :func:`internal_memory_scope` a second time. One
    implementation, so the two paths cannot drift on the wording or the audit.
    """
    await _audit_private_memory_denial(
        operation, "Agent tools cannot use the owner's aggregate controls."
    )
    return web.json_response(
        {
            "error": "This operation requires the owner. Use the member's scoped tools instead.",
            "code": "member_scope_denied",
        },
        status=403,
    )


async def private_owner_surface_refusal(
    request: web.Request, operation: str
) -> web.Response | None:
    """Member tools retain the ordinary owner permission for aggregate controls."""
    scope, refusal = await internal_memory_scope(request, operation)
    if refusal is not None or scope is None:
        return refusal
    return await member_scope_denied_refusal(operation)


#: A check that runs before a route handler; a response it returns is the answer.
RouteGuard = Callable[[web.Request], Awaitable[web.Response | None]]


def guarded_route(handler: Callable[..., Any], guard: RouteGuard) -> Callable[..., Any]:
    """Run ``guard`` before ``handler``; its refusal, when it makes one, is the response."""

    @functools.wraps(handler)
    async def guarded(request: web.Request) -> web.StreamResponse:
        refusal = await guard(request)
        if refusal is not None:
            return refusal
        return await handler(request)

    return guarded


def owner_surface_guard(operation: str) -> RouteGuard:
    """The owner check as a route guard, audited under ``operation``."""

    async def guard(request: web.Request) -> web.Response | None:
        return await private_owner_surface_refusal(request, operation)

    return guard


def dashboard_owner_surface_guard(operation: str) -> RouteGuard:
    """:func:`owner_surface_guard`, then the dashboard-owner check, audited under ``operation``.

    For a surface that is the owner's own data (a transcript, the history list): a
    non-owner dashboard session holds the same empty app claim as the owner's, so
    the private-member check alone admits it. App-token and internal callers pass
    :func:`require_owner_for_dashboard_caller` untouched.
    """

    async def guard(request: web.Request) -> web.Response | None:
        refusal = await private_owner_surface_refusal(request, operation)
        if refusal is not None:
            return refusal
        return await require_owner_for_dashboard_caller(request, operation)

    return guard


def owner_surface_route(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Check owner permissions before ``handler`` runs; the audit label is its name."""
    return guarded_route(handler, owner_surface_guard(handler.__name__))


def guard_owner_surface_routes(
    namespace: dict[str, Any],
    *,
    member_scoped: frozenset[str],
    resource_scoped: Mapping[str, RouteGuard] | None = None,
    prefix: str = "api_",
    dashboard_owner_only: bool = False,
) -> None:
    """Wrap every ``<prefix>*`` coroutine in a handler module's ``namespace`` as an owner surface.

    Fail-closed: only the routes the module names in ``member_scoped`` (they
    verify and scope their own caller) run unwrapped, and a route named in
    ``resource_scoped`` runs behind that guard (the caller's access to the
    route's own resource) instead of the owner check. A handler added later is
    refused to private callers unless it is listed here on purpose. Call it
    once, at the bottom of the module, so the re-exported names are the guarded
    ones. With ``dashboard_owner_only`` the owner check is
    :func:`dashboard_owner_surface_guard`, which also refuses a non-owner
    dashboard session.
    """
    if dashboard_owner_only:
        owner_guard = dashboard_owner_surface_guard
    else:
        owner_guard = owner_surface_guard
    scoped = dict(resource_scoped or {})
    missing = (member_scoped | scoped.keys()) - namespace.keys()
    if missing:
        raise RuntimeError(f"scoped routes are not defined: {sorted(missing)}")
    for name, value in list(namespace.items()):
        if (
            name.startswith(prefix)
            and inspect.iscoroutinefunction(value)
            and name not in member_scoped
        ):
            guard = scoped.get(name)
            namespace[name] = guarded_route(value, guard or owner_guard(value.__name__))
