"""HTTP routes and the route hook behind change cards (``card_*`` tools).

Two halves with deliberately different auth, like the guide surface:

* **Agent half** (``/api/cards/agent/*``) is MCP-only and strict-internal: the
  prefix sits in ``server._STRICT_INTERNAL_API_PATHS``, every handler re-asserts
  ``internal_auth``, and the caller's slot comes SOLELY from the verified
  ``X-Session-Key`` through the same checks the guide uses
  (:func:`kiro_crew.dashboard.handlers.guide._resolve_agent_caller`). An agent can
  propose and read; nothing here can apply.

* **Browser half** (``/api/cards/pending``, ``/api/cards/{id}/preview|cancel|dismiss``)
  is cookie-authed and OWNER-only. It edits and cancels proposals; it cannot apply
  either.

A card is applied only by the browser sending each plan step to the EXISTING
settings-page route with ``X-Card-Id`` / ``X-Card-Revision`` / ``X-Card-Op`` /
``X-Card-Step``. :func:`change_card_middleware` runs on exactly those routes
(``change_card_catalog.HOOKED_ROUTES``). With the headers it refuses anything but
the dashboard owner (an internal-secret caller, an app token or an agent is a 403
whatever it claims), requires the request to equal the plan step exactly, re-reads
the current state before the first write (``409 changed_since_preview``, or
``changed_since_apply`` for an undo), runs the real handler unchanged, and records
the step only from that handler's own 2xx response.

Every owner mutation through those routes -- with or without a card -- also
becomes one concise line in Global memory history (names only, never a secret,
token, env value or server spec), best-effort, never failing the request.

``card_update`` WebSocket frames go to owner sockets only.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import urllib.parse
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard import chat_cards
from kiro_crew.dashboard.change_cards import CardError, CardStore, card_store_for
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

WS_CARD_UPDATE = "card_update"

HEADER_CARD_ID = "X-Card-Id"
HEADER_CARD_REVISION = "X-Card-Revision"
HEADER_CARD_OP = "X-Card-Op"
HEADER_CARD_STEP = "X-Card-Step"

#: Request attribute the card-Undo path sets so ``api_cron_delete`` deletes the
#: card-created schedule only while it still fingerprints to what the card left
#: — a compare-and-delete under the cron store's own lock, closing the window
#: between the pre-delete snapshot check and the delete-by-id. Set ONLY on the
#: owner-authed card step; a direct DELETE never carries it.
REQ_CARD_UNDO_CRON_EXPECT = "card_undo_cron_expect"
#: Set on a ``crewmate.import`` Undo's rules clear: the digest of the rules the
#: import wrote. The rules route clears only while they still hash to it.
REQ_CARD_UNDO_RULES_EXPECT = "card_undo_rules_expect"

#: Request attribute a ``folder.steering`` card's Apply and Undo steps set to
#: the steering list the step replaces; the folder PATCH compares it under the
#: folder store's lock and answers 409 when another save landed first. Set ONLY
#: on the owner-authed card step.
REQ_CARD_FOLDER_STEERING_EXPECT = "card_folder_steering_expect"

#: Request attribute a card-hooked write route sets to the records it replaced,
#: read under the same lock as its write. A ``crewmate.key_results`` card's Apply
#: keeps them in the card's private ``before`` so its Undo restores what was
#: there at the write, not at the preview: a reading recorded in between is kept.
#: Never in a response; a direct write sets it and nothing reads it.
REQ_CARD_PRE_WRITE = "card_pre_write"

#: Request attribute a ``crewmate.goal`` or ``crewmate.key_results`` card's Undo
#: step sets to the card's private pre-change snapshot (``before``). The plan's
#: Undo body is public and so carries that text scrubbed; the goal routes put
#: the raw value back where the body is exactly its scrubbed form. Set ONLY on
#: the owner-authed card step; a direct write never carries it.
REQ_CARD_RAW_RESTORE = "card_raw_restore"

#: Request attribute a ``crewmate.key_results`` card's Apply step sets, so the
#: key-results route can tell the crewmate the user confirmed its milestones.
#: Set ONLY on the owner-authed card step; a Goals-page edit never carries it.
REQ_CARD_KEY_RESULTS_APPLY = "card_key_results_apply"

_SLOT_QUERY_MAX = 256
_BODY_MAX = 1024 * 1024
_QUERY_MAX = 200
#: Routes whose memory line names the value before and after the change.
_VALUE_ROUTES = frozenset({("PATCH", "/api/config/kirocrew"), ("PUT", "/api/dashboard/config")})

#: Strong references to fire-and-forget memory writes, so none is collected mid-run.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _audit(
    caller: str, operation: str, outcome: str, error: str = "", *, resources: str = "/api/cards"
) -> None:
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:  # pragma: no cover - an audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _card_resources(card: dict[str, Any]) -> str:
    """The audit resource for a workflow card's rows; every other card keeps ``/api/cards``.

    A workflow card puts runnable code into the library, so its proposal row names
    the card and its kind, and the owner's decline (the one decision no route
    records) gets a row of its own. Its save is the definition route's own row.
    """
    if card.get("kind") not in catalog.WORKFLOW_KINDS:
        return "/api/cards"
    return f"/api/cards/{card.get('id') or ''} {card.get('kind')}"


def _refusal(exc: CardError | catalog.CardCatalogError) -> web.Response:
    if isinstance(exc, catalog.CardCatalogError):
        return web.json_response({"error": exc.message, "code": exc.code}, status=400)
    status = exc.status if exc.status in (400, 403, 404, 409, 429, 503) else 400
    return web.json_response({"error": exc.message, "code": exc.code}, status=status)


def _store(request: web.Request) -> CardStore:
    return card_store_for(request.app["state"])


async def _broadcast(state: Any, card: dict[str, Any]) -> int:
    # Every public change of a card passes here, so this is where its row in the
    # conversation learns the new status (and the crew log the outcome).
    await chat_cards.load_conversation_window(state, card)
    chat_cards.record_change_status(state, card)
    # A finished import card has no use for the file it was raised from.
    await _release_staged_copy(state, card)
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return 0
    try:
        return int(await deliver(WS_CARD_UPDATE, {"card": card}))
    except Exception:
        logger.debug("card_update delivery failed", exc_info=True)
        return 0


def _slot_is_open(
    state: Any, closed_meta_keys: "frozenset[str] | set[str]" = frozenset()
) -> Callable[[str], bool]:
    """The "this card's conversation is still open" predicate for retirement.

    Retirement cancels a proposal nobody can confirm, so it must fire only on a
    slot the user ACTUALLY closed -- never on one merely absent. Absence is not
    closure: ``slot_exists`` cannot carry this because it reads false in the
    window BEFORE the open-tab restore starts (``open_slots_restored`` False and
    ``restoring_open_slots`` False both -- a tab the restore has not reached
    yet), and a restore-budget skip is left out of ``unrestored_slot_keys``
    entirely, so both would be read as closed and cancelled under the user.

    Two gates, in order:

    1. **Until the open-tab restore has run once** (``open_slots_restored`` is
       not yet True) nothing is proven closed -- the live slot set is not yet
       authoritative -- so every key reads OPEN and no card retires.

    2. **After the restore** a key retires only on POSITIVE closure evidence:
       an in-memory close tombstone (:func:`channel_slots.slot_closed_since` --
       the slot was popped and :func:`note_slot_closed` recorded the instant),
       or a persisted ``closed`` flag read off the loop by :func:`_settle` and
       passed in as *closed_meta_keys*. A key with neither -- absent but never
       recorded closed -- stays OPEN. There is NO fallback to ``get_slot`` or
       bare ``slot_exists``: absence must never stand in as closure proof.
    """
    from kiro_crew.dashboard.channel_slots import slot_closed_since

    def is_open(key: str) -> bool:
        # Gate 1: the restore has not run, so the live set is not authoritative.
        if not bool(getattr(state, "open_slots_restored", False)):
            return True
        # A reopened live slot wins over a close recorded before its reopening.
        exists = getattr(state, "slot_exists", None)
        if callable(exists) and exists(key):
            return True
        # Gate 2: open unless there is real evidence this slot was closed.
        if key in closed_meta_keys:
            return False
        try:
            if slot_closed_since(state, key, 0.0):
                return False
        except Exception:
            logger.debug("close-tombstone read failed for %s", key, exc_info=True)
        return True

    return is_open


async def _settle(request: web.Request) -> None:
    await _settle_app(request.app)


async def _settle_app(app: web.Application) -> None:
    """Expire / prune, persist, then broadcast what changed. Raises :class:`CardError`.

    Persist-before-you-publish, for reads too: when the write fails, every
    record the housekeeping changed or pruned is put back to what disk holds,
    nothing is broadcast, and ``503 checkpoint_failed`` is raised so the read
    handler answers that instead of state disk does not hold. The transitions
    follow from the clock and the open conversations, so the next settlement
    whose write succeeds derives them again and announces them then.
    """
    state = app["state"]
    store = card_store_for(state)
    await store.warm()
    # Retirement cancels a proposal nobody can confirm, so it must fire ONLY on
    # a slot the user actually closed -- never on one merely absent, and never
    # before the open-tab restore has made the live slot set authoritative.
    # ``_slot_is_open`` gates on ``open_slots_restored`` and demands positive
    # closure evidence: an in-memory close tombstone, or a persisted ``closed``
    # flag. The tombstone is a loop-local dict read synchronously inside the
    # predicate; the persisted flag needs a disk read, which cannot run inside
    # the synchronous ``housekeep`` sweep, so it is resolved HERE off the loop
    # for exactly the pending cards' slots and handed in. Both skip entirely
    # until the restore has run -- there is no authoritative "closed" before it.
    closed_meta_keys = await _closed_on_disk(state, store)
    await _sweep_staged_copies(store)
    changed, undo = store.housekeep(_slot_is_open(state, closed_meta_keys))
    try:
        await store.flush(strict=True)
    except CardError as exc:
        store.undo_housekeeping(undo)
        raise CardError(exc.status, exc.code, "the change cards could not be saved; try again")
    for card in changed:
        await _broadcast(state, card)


async def _closed_on_disk(state: Any, store: CardStore) -> "frozenset[str]":
    """Slot keys of pending cards whose session persists a ``closed`` flag.

    Positive closure evidence read off the loop, so the synchronous retirement
    sweep never blocks on disk and never treats a bare absence as closure. Empty
    until the open-tab restore has run (``open_slots_restored``): before that the
    live set is not authoritative, so nothing is proven closed and no read is
    worth doing. Each candidate slot's metadata is read through
    ``get_metadata_status`` -- never ``get_metadata`` -- so an unreadable or
    absent record reports NOT-readable and is left OPEN, not retired.
    """
    if not bool(getattr(state, "open_slots_restored", False)):
        return frozenset()
    log = getattr(state, "conversation_log", None)
    reader = getattr(log, "get_metadata_status", None)
    if not callable(reader):
        return frozenset()
    from kiro_crew.dashboard.chat_utils import drained_to_thread, slot_transcript_key

    keys = {
        rec["slot_key"] for rec in store.pending(None) if rec.get("status") == cards.STATUS_PENDING
    }
    closed: set[str] = set()
    for key in keys:
        try:
            meta, readable = await drained_to_thread(reader, slot_transcript_key(key))
        except Exception:
            logger.debug("persisted-close read failed for %s", key, exc_info=True)
            continue
        if readable and meta and meta.get("closed"):
            closed.add(key)
    return frozenset(closed)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise CardError(400, "invalid_json", "invalid JSON body") from None
    if not isinstance(body, dict):
        raise CardError(400, "invalid_body", "body must be a JSON object")
    return body


async def _publish(
    request: web.Request, rec: dict[str, Any], revert: dict[str, Any]
) -> dict[str, Any]:
    """Persist *rec*'s transition, then tell owner tabs. Raises :class:`CardError`.

    Persist-before-you-publish for every durable transition: when the write fails
    the record is put back to *revert* (its state before the transition, which is
    what disk still holds), nothing is broadcast, and ``503 checkpoint_failed`` is
    raised for the caller to answer with.
    """
    store = _store(request)
    try:
        await store.flush(strict=True)
    except CardError as exc:
        store.restore(rec, revert)
        raise CardError(exc.status, exc.code, "the change card could not be saved; try again")
    public = store.public(rec)
    await _broadcast(request.app["state"], public)
    return public


#: What the card says when its step ran but the result could not be saved.
_UNRECORDED_MESSAGE = (
    "This step ran, but Kiro Crew could not save its result, so the card cannot "
    "continue or undo it. Check the change before asking for it again."
)


async def _publish_committed(request: web.Request, rec: dict[str, Any]) -> CardError | None:
    """Persist a step's recorded result, then publish it. Never publishes an unsaved success.

    When the checkpoint fails the card is settled for review instead
    (:meth:`CardStore.mark_needs_review`), that state is what tabs are told, and
    the returned :class:`CardError` is what the step's caller answers with.
    """
    store = _store(request)
    try:
        await store.flush(strict=True)
    except CardError as exc:
        store.mark_needs_review(rec, cards.CODE_CHECKPOINT_FAILED, _UNRECORDED_MESSAGE)
        await store.flush()
        await _broadcast(request.app["state"], store.public(rec))
        return CardError(exc.status, exc.code, _UNRECORDED_MESSAGE)
    await _broadcast(request.app["state"], store.public(rec))
    return None


#: What the card says when its route was cut off (it raised, or the gateway
#: stopped it) after it may already have written something.
_CUT_OFF_MESSAGE = (
    "This step was cut off before it reported back, so it may or may not have "
    "taken effect. Check the change before asking for it again."
)


async def _settle_cut_off(request: web.Request, rec: dict[str, Any]) -> None:
    """Settle a card whose mutating route raised or was cancelled mid-run.

    The route may have written before it stopped, so the step is never made
    replayable: the card is marked for review (:meth:`CardStore.mark_needs_review`,
    ``interrupted``), the same verdict a restart gives the admission already on
    disk. The save and the broadcast run shielded, so a cancellation (gateway
    shutdown) cannot cut them off half-way; the caller re-raises afterwards.
    """
    store = _store(request)
    store.mark_needs_review(rec, cards.CODE_INTERRUPTED, _CUT_OFF_MESSAGE)

    async def _persist_then_tell() -> None:
        try:
            await store.flush(strict=True)
        except CardError:
            # Disk still holds the admission, which a reload reads as
            # interrupted: the same verdict, so telling tabs is still true.
            logger.warning("could not save the review mark of card %s", rec.get("id"))
        await _broadcast(request.app["state"], store.public(rec))

    task = asyncio.ensure_future(_persist_then_tell())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        pass  # the shielded task still finishes; the caller re-raises
    except Exception:
        logger.debug("settling cut-off card %s failed", rec.get("id"), exc_info=True)


# ── agent half ──


async def _resolve_agent_caller(request: web.Request, operation: str) -> tuple[str, str]:
    from kiro_crew.dashboard.guide_runs import GuideError
    from kiro_crew.dashboard.handlers.guide import _resolve_agent_caller as resolve

    try:
        return await resolve(request, operation)
    except GuideError as exc:
        raise CardError(exc.status, exc.code, exc.message.replace("guide", "propose")) from None


async def api_cards_agent_kinds(request: web.Request) -> web.Response:
    """GET /api/cards/agent/kinds — the registered card catalog."""
    try:
        await _resolve_agent_caller(request, "cards.kinds")
    except CardError as exc:
        return _refusal(exc)
    return web.json_response({"kinds": catalog.list_kinds()})


async def api_cards_agent_settings(request: web.Request) -> web.Response:
    """GET /api/cards/agent/settings?q= — up to ten Settings matches for a card.

    Each row says whether a ``setting.change`` card can write it and, when it
    can, its current value and the values it accepts. A credential-like setting
    never has its value echoed. ``label`` (and ``path``, the Settings click path,
    parent first) are spelled as the caller's dashboard shows them
    (:func:`kiro_crew.dashboard.handlers.guide.caller_ui_locale`), so a quoted
    label matches the screen; ``label_locale`` names that language.
    """
    try:
        slot_key, _sk = await _resolve_agent_caller(request, "cards.find_setting")
    except CardError as exc:
        return _refusal(exc)
    query = (request.query.get("q") or "").strip()
    if not query or len(query) > _QUERY_MAX:
        return web.json_response(
            {"error": "q must be 1-200 characters", "code": "invalid_query"}, status=400
        )
    rows = await asyncio.to_thread(cards.find_settings, query)
    from kiro_crew.dashboard.handlers.guide import caller_ui_locale

    ui_lang, _source = caller_ui_locale(request.app["state"], slot_key)
    rows = await asyncio.to_thread(cards.localize_setting_rows, rows, ui_lang)
    return web.json_response({"settings": rows})


async def api_cards_agent_capabilities(request: web.Request) -> web.Response:
    """GET /api/cards/agent/capabilities?member= — a member's capability rows for a card."""
    try:
        await _resolve_agent_caller(request, "cards.member_capabilities")
    except CardError as exc:
        return _refusal(exc)
    member = (request.query.get("member") or "").strip()
    if not member or len(member) > _QUERY_MAX:
        return web.json_response(
            {"error": "member is required", "code": "invalid_member"}, status=400
        )
    try:
        view = await asyncio.to_thread(cards.member_capabilities_view, request.app, member)
    except catalog.CardCatalogError as exc:
        return _refusal(exc)
    return web.json_response(view)


async def api_cards_agent_diagnose(request: web.Request) -> web.Response:
    """GET /api/cards/agent/diagnose[?topic=] — symptom findings and non-default settings.

    ``findings`` are the read-only probes of :mod:`kiro_crew.diagnose_probes`, each
    ``{id, status, summary, evidence, fix?}``, run against this gateway's state.
    ``non_default`` lists every config and dashboard-config key whose value is not
    the shipped default, joined to its Settings entry when one maps to it; a
    credential-like key reports only whether it is set. ``recent_changes`` is the
    newest ``Dashboard: ...`` lines from Global memory history -- unless the
    calling session's memory reads are disabled (a Temporary session), the same
    test the recall routes refuse on (``_shared._blocks_reads_session``): then it
    is ``[]`` and ``recent_changes_withheld`` is ``"memory_reads_disabled"``, and the
    rest, which is not memory, is still answered. Read-only.
    """
    try:
        await _resolve_agent_caller(request, "cards.diagnose")
    except CardError as exc:
        return _refusal(exc)
    topic = (request.query.get("topic") or "").strip()
    if len(topic) > _QUERY_MAX:
        return web.json_response(
            {"error": "topic must be at most 200 characters", "code": "invalid_query"}, status=400
        )
    from kiro_crew.dashboard.handlers._shared import _blocks_reads_session

    reads_memory = not _blocks_reads_session(request.app["state"], request)
    try:
        result = await asyncio.to_thread(
            cards.diagnose_settings, topic, request.app, include_history=reads_memory
        )
    except Exception:
        logger.warning("settings diagnosis failed", exc_info=True)
        return web.json_response(
            {"error": "settings could not be read", "code": "diagnose_failed"}, status=500
        )
    if not reads_memory:
        result["recent_changes_withheld"] = "memory_reads_disabled"
    return web.json_response(result)


async def api_cards_agent_propose(request: web.Request) -> web.Response:
    """POST /api/cards/agent/propose — ``{kind, params, reason?}`` → Card.

    The card lands in the CALLER's own slot. ``delivered_clients`` 0 means no tab
    is showing it yet; it waits for ``GET /api/cards/pending``.
    """
    try:
        slot_key, sk = await _resolve_agent_caller(request, "cards.propose")
        body = await _json_body(request)
        unknown = sorted(set(body) - {"kind", "params", "reason"})
        if unknown:
            raise CardError(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
    except CardError as exc:
        return _refusal(exc)
    kind = str(body.get("kind") or "")
    if kind in catalog.SELF_ONLY_KINDS:
        # A crewmate raises its own page offer through its kirocrew-mate tool, so the
        # asked-once record is kept; propose_change never raises it.
        return _refusal(CardError(403, "self_only", "only the crewmate itself can offer this"))
    if kind in catalog.GATEWAY_RESOLVED_KINDS:
        # The server entry on this card is one the gateway built; a caller-named
        # entry would be a launch line the agent wrote.
        _audit(sk, "cards.propose", "denied", "gateway_resolved")
        return _refusal(
            CardError(403, "use_request_server", "ask for an MCP server with request_server")
        )
    params = body.get("params")
    if (
        kind == catalog.KIND_CREWMATE_CAPABILITIES
        and isinstance(params, dict)
        and "origins" in params
    ):
        # Where each server comes from is the gateway's own record, written only
        # by ``request_capabilities`` after it resolves the named additions
        # (``member_self``); a caller never supplies one.
        _audit(sk, "cards.propose", "denied", "origins_not_accepted")
        return _refusal(
            CardError(400, "invalid_param", "'origins' is set by the gateway, not the caller")
        )
    if kind == catalog.KIND_CREWMATE_CAPABILITIES and await asyncio.to_thread(
        _crewmate_drives_capabilities_off, request.app["state"], slot_key, sk
    ):
        _audit(sk, "cards.propose", "denied", "dynamic_capabilities_off")
        return _refusal(
            CardError(
                403,
                "dynamic_capabilities_off",
                "a crewmate asks for its worker's capabilities with request_capabilities, "
                "which offers the user the Preview first; use that instead",
            )
        )
    if kind in catalog.WORKFLOW_KINDS and await asyncio.to_thread(
        _crewmate_caller, request.app["state"], slot_key, sk
    ):
        # The library is the owner's, global to every crewmate; a crewmate (its
        # chat or its worker) asks the owner's assistant instead.
        _audit(sk, "cards.propose", "denied", "workflow_crewmate_caller")
        return _refusal(
            CardError(
                403,
                "owner_assistant_only",
                "only the main assistant's own dashboard chat can propose a saved workflow",
            )
        )
    return await propose_for_caller(
        request,
        slot_key,
        sk,
        kind=kind,
        params=body.get("params"),
        reason=body.get("reason"),
    )


def _crewmate_caller(state: Any, slot_key: str, sk: str) -> bool:
    """Whether the caller is a crewmate's own thread or one of its workers. Blocking."""
    from kiro_crew import members
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    if is_crewmate_thread(state.get_slot(slot_key)):
        return True
    return members.is_crewmate_worker_session(slot_key) or members.is_crewmate_worker_session(sk)


def _crewmate_drives_capabilities_off(state: Any, slot_key: str, sk: str) -> bool:
    """A crewmate (its chat or its worker) asking for a capability card it may not raise.

    While that crewmate's Dynamic capabilities preview is off
    (``kiro_crew.crewmate_dynamic_capabilities``) the crewmate-driven capability
    path is withheld, ``propose_change`` included. A worker whose crewmate
    cannot be named counts as off. Any other caller -- the main assistant asked
    to add a tool to a crewmate -- proposes the card as before. Blocking.
    """
    from kiro_crew import crewmate_dynamic_capabilities as dyn
    from kiro_crew import members
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    slot = state.get_slot(slot_key)
    if is_crewmate_thread(slot):
        return dyn.restricted_for(slot.agent)
    if members.is_crewmate_worker_session(slot_key) or members.is_crewmate_worker_session(sk):
        return dyn.restricted_for(_worker_crewmate(slot))
    return False


def _worker_crewmate(slot: Any) -> str:
    """The crewmate a worker *slot* runs for, from its execution record, else ``""``."""
    if slot is None:
        return ""
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.chat_utils import slot_history_key
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
        from kiro_crew.execution_context import read_session_execution

        record = read_session_execution(slot_history_key(slot))
        if record is None:
            return ""
        return crewmate_name_for(KiroCrewConfig.load(), record) or ""
    except Exception:  # noqa: BLE001 - an unnamed crewmate counts as off
        logger.debug("worker crewmate unreadable for %s", getattr(slot, "key", ""), exc_info=True)
        return ""


async def propose_for_caller(
    request: web.Request,
    slot_key: str,
    sk: str,
    *,
    kind: str,
    params: Any,
    reason: Any,
    claim: Callable[[], Awaitable[Callable[[], Awaitable[None]]]] | None = None,
) -> web.Response:
    """Propose one card of *kind* in the verified caller's slot. The route body.

    Shared with the ``kirocrew-mate`` routes (:mod:`handlers.member_self`),
    which resolve the same caller and fix the kind and the member themselves,
    so a crewmate's request for its own goal, model or tools becomes exactly
    the card ``propose_change`` would have made: same validation, same
    preview, same store, same row in the conversation.

    *claim*, when given, persists what the card's existence must leave behind (the
    page offer's asked-once record). It runs after the card is built and before it
    is stored or shown; a :class:`CardError` from it refuses the card. It returns
    the undo, run when the card is then not published, so nothing is claimed for a
    card nobody saw.
    """
    try:
        # An app's kind and an app's card key are never a chat caller's: see
        # propose_app_card.
        if kind in catalog.APP_ONLY_KINDS or slot_key.startswith(
            (APP_SLOT_PREFIX, OWNER_SLOT_PREFIX)
        ):
            raise CardError(403, "app_only", "only the owner can propose this change")
        params = catalog.validate_params(kind, params)
        params = await cards.resolve_params(kind, params)
        cards.check_param_text(params)
        reason = cards.clean_reason(reason)
        state = request.app["state"]
        preview, before, context = await cards.build(kind, params, state=state, app=request.app)
        await _settle(request)
    except (CardError, catalog.CardCatalogError) as exc:
        return _refusal(exc)
    undo_claim: Callable[[], Awaitable[None]] | None = None
    try:
        if claim is not None:
            undo_claim = await claim()
        rec = _store(request).propose(
            slot_key=slot_key,
            session_key=sk,
            kind=kind,
            params=params,
            reason=reason,
            preview=preview,
            before=before,
            context=context,
        )
    except (CardError, catalog.CardCatalogError) as exc:
        if undo_claim is not None:
            await undo_claim()
        return _refusal(exc)
    store = _store(request)
    try:
        await store.flush(strict=True)
    except CardError as exc:
        # Never shown, never in the transcript: the proposal did not happen.
        store.discard(rec["id"])
        if undo_claim is not None:
            await undo_claim()
        return _refusal(CardError(exc.status, exc.code, "the change card could not be saved"))
    public = store.public(rec)
    # The card is part of the conversation from here on: its row lands at this
    # point of the transcript, before the frame that makes it live.
    await chat_cards.load_conversation_window(request.app["state"], public)
    chat_cards.record_change_proposed(request.app["state"], public)
    delivered = await _broadcast(request.app["state"], public)
    _audit(sk, "cards.propose", "ok", resources=_card_resources(public))
    return web.json_response({**public, "delivered_clients": delivered})


async def api_cards_agent_status(request: web.Request) -> web.Response:
    """GET /api/cards/agent/status[?card_id=] — the caller's own card."""
    try:
        slot_key, _sk = await _resolve_agent_caller(request, "cards.status")
        await _settle(request)
        store = _store(request)
        rec = store.status_for_caller(slot_key, request.query.get("card_id") or None)
    except CardError as exc:
        return _refusal(exc)
    return web.json_response(store.public(rec))


# ── crewmate import cards (rfc-crewmate-share.md §5.4) ──

#: The card key of an app's cards, ``app:<name>``. No chat slot key carries it
#: (``propose_for_caller`` refuses one that does), so the store's per-slot cap
#: counts per app, and no conversation can list, report or retire the card.
APP_SLOT_PREFIX = "app:"
#: The reserved card keys of owner surfaces that are not a conversation. A chat
#: slot key never takes one: every slot key is folded to the filename charset
#: (``slot_directory._normalize_slot_key``), which has no ``:``, and
#: ``propose_for_caller`` refuses the prefix besides.
OWNER_SLOT_PREFIX = "owner:"
#: Where the crewmate dropdown's Import from file keeps its card, with an empty
#: ``session_key``; ``GET /api/cards/pending?slot=`` reopens it.
IMPORT_SLOT_KEY = OWNER_SLOT_PREFIX + "crewmate-import"
_FREE_NAME_TRIES = 9
#: The card states in which an import card may still need its staged copy. A
#: ``failed`` card can be retried (``CardStore.begin_step``), so it keeps it
#: until it is cancelled, dismissed or expires.
_STAGE_LIVE = frozenset({cards.STATUS_PENDING, cards.STATUS_APPLYING, cards.STATUS_FAILED})
#: The one start-up sweep of staged copies no live card names. Shared: every
#: caller awaits the same run to its end, so no upload stages while it scans.
_STAGE_SWEEP: asyncio.Task[None] | None = None
#: Digests an upload holds from before it stages its copy until its card is
#: stored (or refused), by count: two tabs may upload the same file. A cancel
#: of an older card that names the same digest keeps the copy meanwhile.
_STAGE_HELD: collections.Counter[str] = collections.Counter()


def _live_import_shas(store: CardStore) -> set[str]:
    """The staged-copy digests a live import card or an in-flight upload names."""
    return set(+_STAGE_HELD) | {
        str((rec.get("params") or {}).get("bundle_sha256"))
        for rec in store.pending(None)
        if rec.get("kind") == catalog.KIND_CREWMATE_IMPORT
        and rec.get("status") in _STAGE_LIVE
        and (rec.get("params") or {}).get("bundle_sha256")
    }


async def _release_staged_copy(state: Any, card: dict[str, Any]) -> None:
    """Delete a finished import card's staged copy, unless a live card still names it."""
    from kiro_crew import crewmate_stage

    sha = (card.get("params") or {}).get("bundle_sha256")
    if card.get("kind") != catalog.KIND_CREWMATE_IMPORT or not sha:
        return
    if card.get("status") in _STAGE_LIVE:
        return
    if sha in _live_import_shas(card_store_for(state)):
        return
    await asyncio.to_thread(crewmate_stage.discard, str(sha))


async def _sweep_staged_copies(store: CardStore) -> None:
    """Once per gateway start: delete every staged copy no live import card names.

    Every caller waits for the same run to finish. An upload stages only after
    it, so the sweep's keep set can never miss a copy staged while it scanned.
    """
    global _STAGE_SWEEP
    task = _STAGE_SWEEP
    if task is None or task.get_loop() is not asyncio.get_running_loop():
        task = _STAGE_SWEEP = asyncio.get_running_loop().create_task(_run_stage_sweep(store))
    await asyncio.shield(task)


async def _run_stage_sweep(store: CardStore) -> None:
    from kiro_crew import crewmate_stage

    try:
        await store.warm()  # the store's first load is disk work, off the loop
        await asyncio.to_thread(crewmate_stage.sweep, _live_import_shas(store))
    except Exception:  # pragma: no cover - a sweep must never fail a card read
        logger.debug("staged crewmate sweep failed", exc_info=True)


async def propose_app_card(app: web.Application, app_name: str, params: Any) -> dict[str, Any]:
    """Raise one ``crewmate.import`` card for app *app_name* (``ctx.crewmate``)."""
    return await _propose_import_card(app, APP_SLOT_PREFIX + app_name, params)


async def _propose_import_card(app: web.Application, slot_key: str, params: Any) -> dict[str, Any]:
    """Raise one ``crewmate.import`` card under *slot_key*; returns the stored card.

    Writes nothing but the card. Only the owner's press applies its plan, through
    owner routes ``change_card_middleware`` refuses to an app token. A taken name
    becomes the first free ``<name> 2`` .. ``<name> 9``; the owner may edit it.
    Raises :class:`CardError` or :class:`catalog.CardCatalogError`.
    """
    kind = catalog.KIND_CREWMATE_IMPORT
    params = await cards.resolve_params(kind, catalog.validate_params(kind, params))
    cards.check_param_text(params)
    base = params["name"]
    for n in range(2, _FREE_NAME_TRIES + 2):
        try:
            preview, before, context = await cards.build(kind, params, state=app["state"], app=app)
            break
        except catalog.CardCatalogError as exc:
            if exc.code != "already_exists" or n > _FREE_NAME_TRIES:
                raise
            suffix = f" {n}"
            name = base[: catalog._CREWMATE_NAME_MAX - len(suffix)].rstrip() + suffix
            params = catalog.validate_params(kind, {**params, "name": name})
    await _settle_app(app)
    store = card_store_for(app["state"])
    rec = store.propose(
        slot_key=slot_key,
        session_key="",
        kind=kind,
        params=params,
        reason="",
        preview=preview,
        before=before,
        context=context,
    )
    try:
        await store.flush(strict=True)
    except CardError as exc:
        store.discard(rec["id"])
        raise CardError(exc.status, exc.code, "the change card could not be saved") from None
    _audit(slot_key, "cards.propose", "ok")
    return store.public(rec)


#: ``application/zip`` is not a CORS-simple type, so a cross-origin page cannot
#: post a file here without the browser asking first.
_CREWMATE_CONTENT_TYPE = "application/zip"


async def _read_crewmate_body(request: web.Request) -> bytes:
    """The uploaded ``.crewmate`` bytes, refused past the §5.2 cap without reading the rest."""
    from kiro_crew.crewmate_bundle import FILE_MAX_BYTES

    if (request.content_type or "").lower() != _CREWMATE_CONTENT_TYPE:
        raise CardError(400, "invalid_content_type", "send the file as application/zip")
    if request.content_length is not None and request.content_length > FILE_MAX_BYTES:
        raise CardError(400, "too_large", "the file is over 8 MiB")
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await request.content.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > FILE_MAX_BYTES:
            raise CardError(400, "too_large", "the file is over 8 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


async def api_members_import(request: web.Request) -> web.Response:
    """POST /api/members/import — one ``.crewmate`` file -> one pending ``crewmate.import`` card.

    Owner only: an app token, an internal-secret caller and an agent get 403. The
    file is vetted (zip inventory first, then every §5.2 rule) before a card
    exists, and a refusal raises no card and keeps nothing. A file that passes
    is kept as an owner-only staged copy (:mod:`kiro_crew.crewmate_stage`), the
    one thing raising the card writes besides the card itself; the card names
    it by sha256 and lands under :data:`IMPORT_SLOT_KEY`. Only the owner's press
    on it creates the crewmate.
    """
    denied = await _require_owner(request, "crewmate.import")
    if denied is not None:
        return denied
    if request.get("app"):
        return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
    from kiro_crew import crewmate_stage
    from kiro_crew.crewmate_bundle import crewmate_import_params, read_crewmate_file

    def _vet(data: bytes) -> dict[str, Any]:
        files = read_crewmate_file(data)
        return crewmate_import_params(files.bundle, files)

    params: dict[str, Any] = {}
    held = ""
    # The start-up sweep runs before this file is staged, never after it.
    await _sweep_staged_copies(_store(request))
    try:
        data = await _read_crewmate_body(request)
        params = await asyncio.to_thread(_vet, data)
        if params.get("carried"):
            held = str(params["bundle_sha256"])
            _STAGE_HELD[held] += 1
            await asyncio.to_thread(crewmate_stage.stage, data)
        card = await _propose_import_card(request.app, IMPORT_SLOT_KEY, params)
    except (CardError, catalog.CardCatalogError) as exc:
        _unhold(held)
        held = ""
        sha = params.get("bundle_sha256")
        if sha and sha not in _live_import_shas(_store(request)):
            await asyncio.to_thread(crewmate_stage.discard, str(sha))
        _audit("owner", "crewmate.import", "refused", exc.code)
        return _refusal(exc)
    except OSError:
        _unhold(held)
        held = ""
        logger.warning("could not keep the checked crewmate file", exc_info=True)
        return web.json_response(
            {"error": "the file could not be kept for the card", "code": "stage_failed"},
            status=500,
        )
    finally:
        # Stored: the card itself now names the copy.
        _unhold(held)
    delivered = await _broadcast(request.app["state"], card)
    return web.json_response({**card, "delivered_clients": delivered})


def _unhold(sha: str) -> None:
    if sha:
        _STAGE_HELD[sha] -= 1
        if _STAGE_HELD[sha] <= 0:
            del _STAGE_HELD[sha]


async def api_members_import_files(request: web.Request) -> web.Response:
    """GET /api/members/import/{sha256}/files — every carried file of a live import card, in full.

    Owner only. Answers only for a staged copy a pending or applying import card
    names, re-checks it (sha256 and every bundle rule) and returns each skill and
    script file as text, and the picture as a data URL, so the card shows what
    the press would write. Never executes anything.
    """
    denied = await _require_owner(request, "crewmate.import_files.read")
    if denied is not None:
        return denied
    if request.get("app"):
        return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
    from kiro_crew import crewmate_stage

    sha = request.match_info["sha256"]
    try:
        await _settle(request)
    except CardError as exc:
        return _refusal(exc)
    if sha not in _live_import_shas(_store(request)):
        return web.json_response(
            {"error": "no open import card names this file", "code": "not_found"}, status=404
        )

    def _read() -> dict[str, Any]:
        import base64

        from kiro_crew.crewmate_bundle import file_text, read_crewmate_file

        files = read_crewmate_file(crewmate_stage.load(sha))
        out: dict[str, Any] = {
            "skills": {
                name: {
                    path: file_text(blob, f"skills/{name}/{path}") for path, blob in folder.items()
                }
                for name, folder in files.skills.items()
            },
            "scripts": {
                file: file_text(blob, f"crons/{file}") for file, blob in files.scripts.items()
            },
        }
        if files.avatar is not None:
            ext, blob = files.avatar
            mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
            out["avatar"] = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
        return out

    try:
        return web.json_response(await asyncio.to_thread(_read))
    except crewmate_stage.StageError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    except catalog.CardCatalogError as exc:
        return _refusal(exc)


async def app_card_status(app: web.Application, app_name: str, card_id: str) -> dict[str, Any]:
    """``{status, crew?}`` of app *app_name*'s own card; ``crew`` only once applied."""
    await _settle_app(app)
    rec = card_store_for(app["state"]).status_for_caller(APP_SLOT_PREFIX + app_name, card_id)
    out: dict[str, Any] = {"status": rec["status"]}
    evidence = rec.get("evidence") or []
    if rec["status"] == "applied" and evidence and isinstance(evidence[0].get("name"), str):
        out["crew"] = evidence[0]["name"]
    return out


# ── browser half ──


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    if request.get("internal_auth") is True:
        return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
    return await require_owner_dashboard_request(request, operation)


async def api_cards_pending(request: web.Request) -> web.Response:
    """GET /api/cards/pending[?slot=] — live cards plus those finished in the last 24h."""
    denied = await _require_owner(request, "cards.pending")
    if denied is not None:
        return denied
    slot = request.query.get("slot") or None
    if slot is not None and len(slot) > _SLOT_QUERY_MAX:
        return web.json_response({"error": "slot is too long", "code": "invalid_slot"}, status=400)
    try:
        await _settle(request)
    except CardError as exc:
        return _refusal(exc)
    return web.json_response({"cards": _store(request).pending(slot)})


async def api_cards_preview(request: web.Request) -> web.Response:
    """POST /api/cards/{id}/preview — ``{params, revision}`` → Card at a new revision."""
    denied = await _require_owner(request, "cards.preview")
    if denied is not None:
        return denied
    try:
        await _store(request).warm()
        body = await _json_body(request)
        store = _store(request)
        rec = store.get(request.match_info["card_id"])
        revert = store.snapshot(rec)
        params = catalog.validate_params(rec["kind"], body.get("params"))
        params = await cards.resolve_params(rec["kind"], params)
        catalog.check_editable(rec["kind"], rec["params"], params)
        preview, before, context = await cards.build(
            rec["kind"], params, state=request.app["state"], app=request.app
        )
        store.revise(
            rec,
            revision=body.get("revision"),
            params=params,
            preview=preview,
            before=before,
            context=context,
        )
        return web.json_response(await _publish(request, rec, revert))
    except (CardError, catalog.CardCatalogError) as exc:
        return _refusal(exc)


async def api_cards_cancel(request: web.Request) -> web.Response:
    """POST /api/cards/{id}/cancel — ``{revision}``."""
    denied = await _require_owner(request, "cards.cancel")
    if denied is not None:
        return denied
    try:
        await _store(request).warm()
        body = await _json_body(request)
        store = _store(request)
        rec = store.get(request.match_info["card_id"])
        revert = store.snapshot(rec)
        store.cancel(rec, body.get("revision"))
        published = await _publish(request, rec, revert)
        if rec.get("kind") in catalog.WORKFLOW_KINDS:
            _audit(
                str(request.get("user") or "owner"),
                "cards.decline",
                "rejected",
                resources=_card_resources(rec),
            )
        return web.json_response(published)
    except CardError as exc:
        return _refusal(exc)


async def api_cards_dismiss(request: web.Request) -> web.Response:
    """POST /api/cards/{id}/dismiss — hide a finished card."""
    denied = await _require_owner(request, "cards.dismiss")
    if denied is not None:
        return denied
    try:
        await _store(request).warm()
        store = _store(request)
        rec = store.get(request.match_info["card_id"])
        revert = store.snapshot(rec)
        store.dismiss(rec)
        public = await _publish(request, rec, revert)
    except CardError as exc:
        return _refusal(exc)
    # A dismissed failed card leaves the waiting list, and the dismiss writes no log
    # entry (its status stays ``failed``): tell a crewmate page's feed directly.
    from kiro_crew.dashboard import crewmate_feed

    crewmate_feed.question_retired(str(rec.get("slot_key") or ""))
    return web.json_response(public)


# ── the route hook ──


def route_key(request: web.Request) -> tuple[str, str] | None:
    """``(method, route template)`` when *request* hit a hooked route."""
    try:
        resource = request.match_info.route.resource
    except AttributeError:
        return None
    template = getattr(resource, "canonical", None) if resource is not None else None
    if not isinstance(template, str):
        return None
    key = (request.method.upper(), template)
    return key if key in catalog.HOOKED_ROUTES else None


async def _read_body(request: web.Request) -> tuple[Any, bool]:
    """``(parsed JSON or None, ok)``, leaving the body readable for the handler.

    Handlers read the body via ``request.json()`` or by iterating
    ``request.content``, so the bytes read here are fed back as a fresh payload
    stream. A body over the cap is refused.
    """
    if not request.can_read_body:
        return None, True
    length = request.content_length
    if length is not None and length > _BODY_MAX:
        return None, False
    parts: list[bytes] = []
    size = 0
    # The raw payload, not ``request.content``: that property is cached on first
    # access, so touching it here would pin the handler to the drained stream.
    async for chunk in request._payload.iter_chunked(2**16):  # noqa: SLF001
        size += len(chunk)
        if size > _BODY_MAX:
            return None, False
        parts.append(chunk)
    raw = b"".join(parts)
    _replay_payload(request, raw)
    if not raw:
        return None, True
    try:
        return json.loads(raw.decode("utf-8")), True
    except (ValueError, UnicodeDecodeError):
        return None, False


def _replay_payload(request: web.Request, raw: bytes) -> None:
    """Give *request* a payload stream that yields *raw* again from the start."""
    from aiohttp.streams import StreamReader

    # A limit above the body's size keeps feed_data from pausing the real
    # connection's transport, which a handler that never reads would not resume.
    limit = max(2**16, len(raw) + 1)
    stream = StreamReader(request.protocol, limit=limit, loop=asyncio.get_running_loop())
    if raw:
        stream.feed_data(raw)
    stream.feed_eof()
    request._payload = stream  # noqa: SLF001 - aiohttp keeps no public setter


def _response_json(resp: web.StreamResponse) -> Any:
    if not isinstance(resp, web.Response):
        return None
    body = resp.body
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def match_step(
    plan_step: dict[str, Any],
    *,
    method: str,
    path: str,
    query: dict[str, str],
    body: Any,
    evidence: list[dict[str, Any]],
) -> str | None:
    """Why the request differs from *plan_step*, or ``None`` when it is exactly it.

    The path is compared decoded, the query as a mapping, and the body through
    :func:`kiro_crew.dashboard.change_cards.canonical`. A ``fill`` field is the
    only allowed difference: a ``user`` fill must be a non-empty string (and is
    never read further), a ``step`` fill must equal what that earlier step's
    real response returned. A ``step`` fill into the path replaces its
    placeholder segment, and only with a plain identifier.
    """
    if plan_step["method"] != method:
        return "method"
    plan_path, _, plan_query = plan_step["path"].partition("?")
    fills = plan_step.get("fill") or []
    for fill in fills:
        if not catalog.is_path_fill(fill):
            continue
        idx = int(fill["step"])
        value = (evidence[idx] if idx < len(evidence) else {}).get(fill["key"])
        if not isinstance(value, str) or not catalog.PATH_FILL_VALUE.match(value):
            return f"fill:{fill['field']}"
        plan_path = plan_path.replace(catalog.placeholder(fill), value)
    if urllib.parse.unquote(plan_path) != path:
        return "path"
    if dict(urllib.parse.parse_qsl(plan_query)) != query:
        return "query"
    expected = plan_step.get("body")
    if expected is None:
        return None if body in (None, {}) else "body"
    if not isinstance(body, dict) or not isinstance(expected, dict):
        return "body"
    got = dict(body)
    want = dict(expected)
    for fill in fills:
        if catalog.is_path_fill(fill):
            continue
        field = fill["field"]
        value = got.pop(field, None)
        want.pop(field, None)
        if fill["source"] == "user":
            if not isinstance(value, str) or not value.strip():
                return f"fill:{field}"
        else:
            idx = int(fill["step"])
            source = evidence[idx] if idx < len(evidence) else {}
            if value is None or value != source.get(fill["key"]):
                return f"fill:{field}"
    return None if cards.canonical(got) == cards.canonical(want) else "body"


def _strip_user_fills(plan_step: dict[str, Any] | None, body: Any) -> Any:
    """*body* without any value only the person typed, for memory description."""
    if not isinstance(body, dict) or not plan_step:
        return body
    out = dict(body)
    for fill in plan_step.get("fill") or []:
        if fill["source"] == "user":
            out.pop(fill["field"], None)
    return out


def _memory_body(route: tuple[str, str], body: Any) -> Any:
    if route == ("POST", "/api/secrets") and isinstance(body, dict):
        return {"name": body.get("name")}
    return body


def _record_memory(
    route: tuple[str, str], match_info: dict[str, str], body: Any, before: Any, via: str
) -> None:
    """Queue one Global memory history line for an owner mutation. Never raises."""
    try:
        text = catalog.describe_manual_change(route, match_info, _memory_body(route, body), before)
    except Exception:
        logger.debug("describing dashboard change failed", exc_info=True)
        return
    if not text:
        return
    entry = f"Dashboard: {text} ({via})"

    def _write() -> None:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.memory_sections import memory_writes_off_key
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

        # An automatic writer: with memory.persistence_enabled or
        # memory.writes_enabled off, the change still happens but leaves no
        # history line.
        if memory_writes_off_key(KiroCrewConfig.load().memory) is not None:
            return
        ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE).append_history(entry)

    async def _run() -> None:
        try:
            await asyncio.to_thread(_write)
        except Exception:
            logger.debug("dashboard memory event failed", exc_info=True)

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _before_value(route: tuple[str, str], body: Any, match_info: dict[str, str]) -> Any:
    """What a memory line needs from before the change: an old value, or a display name."""
    if route in _VALUE_ROUTES:
        return await asyncio.to_thread(_config_before, route, body)
    if route == ("DELETE", "/api/agents/{name}"):
        return await asyncio.to_thread(_crewmate_display_name, match_info.get("name", ""))
    return None


def _crewmate_display_name(key: str) -> str | None:
    """The name a person sees for crewmate *key* (the create line used it too)."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        agent = KiroCrewConfig.load().agents.get(key)
    except Exception:
        return None
    name = getattr(agent, "display_name", "") if agent is not None else ""
    return name.strip() if isinstance(name, str) and name.strip() else None


def _config_before(route: tuple[str, str], body: Any) -> Any:
    if route == ("PUT", "/api/dashboard/config") and isinstance(body, dict):
        out = {}
        for key in body:
            if key in catalog.DASHBOARD_KEYS:
                try:
                    out[key] = cards._config_value(f"dashboard.{key}")
                except Exception:
                    out[key] = None
        return out
    if route != ("PATCH", "/api/config/kirocrew") or not isinstance(body, dict):
        return None
    path = body.get("path")
    if not isinstance(path, str) or not catalog._CONFIG_PATH_RE.fullmatch(path):
        return None
    try:
        return cards._config_value(path)
    except Exception:
        return None


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


#: A fingerprint value no live cron job can ever produce (``cron_job_revision``
#: returns a 16-char hex digest). Armed when an Undo issues a cron DELETE for
#: which the card recorded NO schedule fingerprint, so the compare-and-delete
#: fails closed — a missing fingerprint refuses the delete rather than removing a
#: schedule unconditionally.
_CRON_FINGERPRINT_MISSING = "__card_undo_missing_fingerprint__"


def _arm_card_undo_cron_guard(
    request: web.Request, rec: dict[str, Any], op: str, plan_step: dict[str, Any]
) -> None:
    """Arm an Undo's name-addressed writes with the identity the card recorded.

    Each route re-checks it under the lock it writes under and answers 409
    ``changed_since_apply`` when it differs:

    * a cron DELETE in a ``schedule.create`` or ``crewmate.create`` Undo, against
      ``after["revision"]`` / ``after["schedule"]``; with no recorded fingerprint
      it fails closed on an impossible sentinel;
    * a crewmate DELETE in a ``crewmate.create`` Undo, against the ``member_id``
      the create returned;
    * a crewmate PUT in a ``crewmate.update`` Undo, against
      ``after["member_id"]``;
    * the rules clear in a ``crewmate.import`` Undo, against ``after["rules"]``.

    A ``folder.steering`` Apply or Undo carries the list it replaces: the
    folder's list before the card for Apply, the list the card wrote for Undo.

    Only the owner-authed card step carries these keys; no public HTTP
    parameter is added.
    """
    kind = rec["kind"]
    if kind == catalog.KIND_CREWMATE_IMPORT and str(plan_step.get("path", "")).endswith(
        "/import-files"
    ):
        from kiro_crew.dashboard.handlers.crewmate_import_files import REQ_CARD_IMPORT_FILES

        # Only an admitted import card step may write (or take back) carried files.
        request[REQ_CARD_IMPORT_FILES] = rec["id"]
    if kind == catalog.KIND_FOLDER_STEERING:
        replaced = (rec.get("params") if op == catalog.OP_UNDO else rec.get("before")) or {}
        request[REQ_CARD_FOLDER_STEERING_EXPECT] = catalog.string_items(
            replaced.get("steering_dirs")
        )
        return
    if op != catalog.OP_UNDO:
        if kind == catalog.KIND_CREWMATE_KEY_RESULTS:
            # The user just confirmed the crewmate's milestones: the route
            # tells the crewmate so (:func:`goal_setup.schedule_milestones_nudge`).
            request[REQ_CARD_KEY_RESULTS_APPLY] = True
        elif kind == catalog.KIND_CREWMATE_DASHBOARD_PAGE:
            # The person said yes on the page offer: only this step may start the
            # crewmate's write-and-apply wake (``member_page_offer``).
            from kiro_crew.dashboard.handlers.member_page_offer import (
                REQ_CARD_PAGE_OFFER_APPLY,
                REQ_CARD_PAGE_OFFER_PAGE,
            )

            request[REQ_CARD_PAGE_OFFER_APPLY] = True
            # The page the person said yes over, as the card last previewed it: a
            # refreshed card names the template in force at the refresh.
            before = rec.get("before")
            page = before.get("page") if isinstance(before, dict) else None
            request[REQ_CARD_PAGE_OFFER_PAGE] = page if isinstance(page, dict) else None
        return
    if kind in (catalog.KIND_CREWMATE_GOAL, catalog.KIND_CREWMATE_KEY_RESULTS):
        before = rec.get("before")
        request[REQ_CARD_RAW_RESTORE] = before if isinstance(before, dict) else {}
        return
    path = str(plan_step.get("path", ""))
    after = rec.get("after") or {}
    method = str(plan_step.get("method", "")).upper()
    if method == "PUT" and path.startswith("/api/agents/") and kind == catalog.KIND_CREWMATE_UPDATE:
        from kiro_crew.dashboard.handlers.undo_identity_guard import (
            REQ_CARD_UNDO_CREWMATE_EXPECT,
        )

        # The crewmate the apply wrote to; the update route refuses when the
        # name now belongs to a different crewmate.
        member_id = after.get("member_id")
        request[REQ_CARD_UNDO_CREWMATE_EXPECT] = member_id if isinstance(member_id, str) else ""
        return
    if method == "PUT" and path.endswith("/rules") and kind == catalog.KIND_CREWMATE_IMPORT:
        # Fail closed: with no recorded digest the sentinel never matches.
        digest = after.get("rules")
        request[REQ_CARD_UNDO_RULES_EXPECT] = (
            digest if isinstance(digest, str) and digest else _CRON_FINGERPRINT_MISSING
        )
        return
    if method != "DELETE":
        return
    if path.startswith("/api/crons/") and kind in (
        catalog.KIND_SCHEDULE_CREATE,
        catalog.KIND_CREWMATE_CREATE,
        catalog.KIND_CREWMATE_IMPORT,
    ):
        if kind == catalog.KIND_CREWMATE_IMPORT:
            # One fingerprint per cron the import made, keyed by the id in this path.
            job_id = urllib.parse.unquote(path[len("/api/crons/") :])
            revision = (after.get("crons") or {}).get(job_id)
        else:
            revision = after.get("revision" if kind == catalog.KIND_SCHEDULE_CREATE else "schedule")
        # Fail closed: a cron DELETE in an Undo with no recorded fingerprint must
        # not delete unconditionally, so an impossible sentinel forces the 409.
        request[REQ_CARD_UNDO_CRON_EXPECT] = (
            revision if isinstance(revision, str) and revision else _CRON_FINGERPRINT_MISSING
        )
    elif path.startswith("/api/agents/") and kind in catalog.CREWMATE_CREATING_KINDS:
        from kiro_crew.dashboard.handlers.undo_identity_guard import (
            REQ_CARD_UNDO_CREWMATE_EXPECT,
        )

        evidence = rec.get("evidence") or []
        member_id = evidence[0].get("member_id") if evidence else None
        request[REQ_CARD_UNDO_CREWMATE_EXPECT] = member_id if isinstance(member_id, str) else ""


def _is_owner_browser(request: web.Request) -> bool:
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if request.get("internal_auth") is True:
        return False
    return is_owner_dashboard_request(request)


@web.middleware  # type: ignore[misc]
async def change_card_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Verify and record card steps on the hooked routes; log owner mutations."""
    route = route_key(request)
    if route is None:
        return await handler(request)
    card_id = request.headers.get(HEADER_CARD_ID)
    if not card_id:
        if route not in catalog.MEMORY_ROUTES or not _is_owner_browser(request):
            return await handler(request)
        # An ordinary save is never refused here: a body with no declared length
        # or over the cap goes to the route untouched and records no memory line.
        length = request.content_length
        if request.can_read_body and (length is None or length > _BODY_MAX):
            return await handler(request)
        body, ok = await _read_body(request)
        if not ok:
            return await handler(request)
        before = await _before_value(route, body, dict(request.match_info))
        resp = await handler(request)
        if 200 <= resp.status < 300:
            _record_memory(route, dict(request.match_info), body, before, "via settings page")
        return resp
    return await _run_card_step(request, handler, route, card_id)


async def _run_card_step(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    route: tuple[str, str],
    card_id: str,
) -> web.StreamResponse:
    if not _is_owner_browser(request):
        _audit(str(request.get("user") or "unknown"), "cards.apply", "denied", "not the owner")
        return _deny(403, "owner_only", "only the dashboard owner can confirm a change card")
    state = request.app["state"]
    store = card_store_for(state)
    op = (request.headers.get(HEADER_CARD_OP) or "").strip().lower()
    body, ok = await _read_body(request)
    if not ok:
        return _deny(400, "invalid_body", "the request body is not JSON")
    try:
        await store.warm()
        rec = store.get(card_id)
        # The record as it was before this step was admitted: what disk holds
        # until the admission is saved, and what an unsaved verdict reverts to.
        revert = store.snapshot(rec)
        admitted = store.begin_step(
            rec,
            revision=request.headers.get(HEADER_CARD_REVISION),
            op=op,
            index=request.headers.get(HEADER_CARD_STEP),
        )
    except CardError as exc:
        return _refusal(exc)
    if admitted["replay"]:
        return web.json_response({"ok": True, "card_replay": True, "card": store.public(rec)})
    plan_step = admitted["step"]
    index = (rec.get("inflight") or {}).get("step", 0)
    # A poll (``repeat`` GET) changes nothing, so only a write needs its
    # admission and its result on disk before anything is published.
    mutates = str(plan_step.get("method", "")).upper() != "GET"
    handler_started = False
    mismatch = match_step(
        plan_step,
        method=request.method.upper(),
        path=request.path,
        query=dict(request.query),
        body=body,
        evidence=store.step_evidence(rec, op),
    )
    if mismatch is not None:
        store.abort_step(rec)
        await store.flush()
        _audit(str(request.get("user") or "owner"), "cards.apply", "denied", f"mismatch:{mismatch}")
        return _deny(409, "plan_mismatch", f"this request does not match the card ({mismatch})")
    try:
        if index == 0:
            if op == catalog.OP_APPLY:
                current = await cards.read_state(
                    rec["kind"], rec["params"], [], state=state, app=request.app
                )
                if cards.canonical(cards.comparable_state(rec["kind"], current)) != cards.canonical(
                    cards.comparable_state(rec["kind"], rec["before"])
                ):
                    store.abort_step(rec)
                    unsaved = await _published_or_refusal(request, rec, revert)
                    if unsaved is not None:
                        return unsaved
                    return _deny(
                        409,
                        "changed_since_preview",
                        "this changed since the card was shown; preview it again",
                    )
            else:
                current = await cards.read_state(
                    rec["kind"], rec["params"], rec["evidence"], state=state, app=request.app
                )
                if cards.undo_snapshot_changed(rec["kind"], current, rec.get("after")):
                    store.abort_step(rec)
                    rec["error"] = {
                        "code": "changed_since_apply",
                        "message": "changed after it was applied",
                    }
                    unsaved = await _published_or_refusal(request, rec, revert)
                    if unsaved is not None:
                        return unsaved
                    return _deny(
                        409,
                        "changed_since_apply",
                        "this changed after the card was applied; undo refused",
                    )
        elif op == catalog.OP_UNDO and await cards.resumed_undo_is_stale(
            rec, index, state=state, app=request.app
        ):
            store.abort_step(rec)
            rec["error"] = {
                "code": "changed_since_apply",
                "message": "changed after it was applied",
            }
            unsaved = await _published_or_refusal(request, rec, revert)
            if unsaved is not None:
                return unsaved
            return _deny(
                409,
                "changed_since_apply",
                "this changed after the card was applied; undo refused",
            )
        before = await _before_value(route, body, dict(request.match_info))
        if mutates:
            # Persist-before-you-publish: the admission is on disk before the
            # route writes anything, so a gateway that stops mid-step reloads
            # this card as interrupted (never re-admitted) instead of pending.
            try:
                await store.flush(strict=True)
            except CardError as exc:
                store.abort_step(rec)
                await store.flush()
                _audit(str(request.get("user") or "owner"), "cards.apply", "error", exc.code)
                return _refusal(
                    CardError(
                        exc.status, exc.code, "the change card could not be saved; nothing changed"
                    )
                )
        handler_started = True
        _arm_card_undo_cron_guard(request, rec, op, plan_step)
        resp = await handler(request)
    except web.HTTPException as exc:
        if handler_started and mutates and exc.status >= 400:
            # A route that RAISES its refusal answered the same as one that
            # returns it: recorded as that failure, then raised on.
            unsaved = await _record_failed_step(request, rec, op, index, exc.status, None)
            if unsaved is not None:
                return unsaved
            raise
        await _abandon_step(request, rec, handler_started=handler_started, mutates=mutates)
        raise
    except BaseException:
        # Includes CancelledError (a gateway shutting down mid-request).
        await _abandon_step(request, rec, handler_started=handler_started, mutates=mutates)
        raise
    data = _response_json(resp)
    if not 200 <= resp.status < 300:
        if mutates:
            unsaved = await _record_failed_step(request, rec, op, index, resp.status, data)
            return unsaved or resp
        store.record_failure(rec, op=op, index=index, status=resp.status, body=data)
        if rec["status"] == cards.STATUS_PARTIAL:
            await _complete(request, rec, partial=True)
        return await _published_or_refusal(request, rec, revert) or resp
    evidence = catalog.extract_evidence(
        rec["kind"], op, index, data if isinstance(data, dict) else None
    )
    pre_write = request.get(REQ_CARD_PRE_WRITE)
    if (
        op == catalog.OP_APPLY
        and rec["kind"] == catalog.KIND_CREWMATE_KEY_RESULTS
        and isinstance(pre_write, list)
        and isinstance(rec.get("before"), dict)
    ):
        # The records the write itself replaced (read under its lock), so the
        # Undo derived from ``before`` puts back what was there at Apply.
        rec["before"] = {**rec["before"], cards.KR_RECORDS_KEY: pre_write}
    outcome = store.record_success(rec, op=op, index=index, evidence=evidence)
    survivors: list[str] = []
    if outcome == "done" and op == catalog.OP_UNDO:
        survivors = await cards.undo_survivors(
            rec, state=request.app["state"], app=request.app, response=data
        )
    memory_body = _strip_user_fills(plan_step, body)
    if survivors and isinstance(memory_body, dict) and "changes" in memory_body:
        # History names what was actually removed, never a requested removal
        # that did not happen. Only a body that lists its removals can be
        # narrowed; any other partial Undo records nothing (below).
        memory_body = {
            **memory_body,
            "changes": [
                c
                for c in memory_body.get("changes") or []
                if not (isinstance(c, dict) and c.get("name") in survivors)
            ],
        }
    # An Undo whose removal is only partial and whose body cannot be narrowed
    # to what was removed records nothing, rather than a removal that did not
    # fully happen.
    partial_unnamed = bool(survivors) and not (
        isinstance(memory_body, dict) and "changes" in memory_body
    )
    if route in catalog.MEMORY_ROUTES and not partial_unnamed:
        _record_memory(
            route,
            dict(request.match_info),
            memory_body,
            before,
            "via change card" if op == catalog.OP_APPLY else "via change card undo",
        )
    if outcome == "done":
        if op == catalog.OP_APPLY and rec["status"] == cards.STATUS_APPLYING:
            await _complete(request, rec, partial=False)
        elif op == catalog.OP_UNDO:
            if survivors:
                # The route answered 2xx but the thing is still installed: the
                # card must not say it was undone, and Undo can be pressed again.
                rec["undo_evidence"] = []
                if rec["kind"] == catalog.KIND_CONNECTION_CONNECT:
                    # A half-removed grant is the state the retry reverses: a
                    # retry may finish this cleanup, while a grant re-established
                    # since then reads differently and is still refused.
                    rec["after"] = await cards.read_state(
                        rec["kind"],
                        rec["params"],
                        rec["evidence"],
                        state=request.app["state"],
                        app=request.app,
                    )
                store.record_failure(
                    rec,
                    op=op,
                    index=index,
                    status=500,
                    body={
                        "code": "undo_incomplete",
                        "error": f"not removed: {', '.join(survivors)[:200]}",
                    },
                )
            else:
                store.complete_undo(rec)
    if mutates:
        unrecorded = await _publish_committed(request, rec)
        if unrecorded is not None:
            _audit(str(request.get("user") or "owner"), "cards.apply", "error", unrecorded.code)
            return _refusal(unrecorded)
        return resp
    # A poll: its verdict (an approval finishing the card, among others) is on
    # disk before any tab hears of it, or the poll is answered 503 and re-run.
    return await _published_or_refusal(request, rec, revert) or resp


async def _published_or_refusal(
    request: web.Request, rec: dict[str, Any], revert: dict[str, Any]
) -> web.Response | None:
    """:func:`_publish`, answering a failed save with its refusal instead of raising."""
    try:
        await _publish(request, rec, revert)
    except CardError as exc:
        return _refusal(exc)
    return None


async def _record_failed_step(
    request: web.Request, rec: dict[str, Any], op: str, index: int, status: int, data: Any
) -> web.Response | None:
    """Record a mutating route's non-2xx and publish it once saved.

    Returns the refusal to answer with when the verdict could not be saved (the
    card is then settled for review, :func:`_publish_committed`), else ``None``.
    """
    store = _store(request)
    store.record_failure(rec, op=op, index=index, status=status, body=data)
    if rec["status"] == cards.STATUS_PARTIAL:
        await _complete(request, rec, partial=True)
    unsaved = await _publish_committed(request, rec)
    if unsaved is None:
        return None
    _audit(str(request.get("user") or "owner"), "cards.apply", "error", unsaved.code)
    return _refusal(unsaved)


async def _abandon_step(
    request: web.Request, rec: dict[str, Any], *, handler_started: bool, mutates: bool
) -> None:
    """A step that raised. Retryable only when no write can have happened.

    Before the route was called (a check or a read raised), or for a poll (a GET
    changes nothing), the admission is withdrawn and the step may be sent again.
    Once a mutating route has started, its outcome is unknown, so the card is
    settled for review instead and never replayed (:func:`_settle_cut_off`).
    """
    if handler_started and mutates:
        await _settle_cut_off(request, rec)
        return
    store = _store(request)
    store.abort_step(rec)
    await store.flush()


async def _complete(request: web.Request, rec: dict[str, Any], *, partial: bool) -> None:
    """Read what the change made and derive the undo plan from it."""
    store = card_store_for(request.app["state"])
    applied = catalog.applied_write_count(rec["plan"]["apply"], len(rec["evidence"]))
    try:
        after = await cards.read_state(
            rec["kind"], rec["params"], rec["evidence"], state=request.app["state"], app=request.app
        )
    except Exception:
        logger.debug("post-apply read failed for card %s", rec["id"], exc_info=True)
        after = {}
    if rec["kind"] == catalog.KIND_SECRET_SAVE and rec["evidence"]:
        # The save reported the revision it wrote, under the vault's lock; the
        # read above could already see a value another tab saved over it.
        written = rec["evidence"][0].get("revision")
        if isinstance(written, str) and written:
            after = {"exists": True, "revision": written}
    context = {"rule_id": after.get("id")}
    try:
        undo, reason = catalog.build_undo(
            rec["kind"], rec["params"], rec["before"], rec["evidence"], applied, context
        )
    except Exception:
        logger.debug("undo plan failed for card %s", rec["id"], exc_info=True)
        undo, reason = None, "undo_unavailable"
    if rec.get("undo_unavailable_reason") and undo is None:
        reason = rec["undo_unavailable_reason"]
    if undo is not None and not cards.after_shows_the_write(
        rec["kind"], rec["params"], after, rec["evidence"]
    ):
        # Something else changed these fields between the write and the read:
        # an Undo restoring them would erase that change.
        undo, reason = None, "changed_since_apply"
    store.complete_apply(
        rec,
        after=after,
        undo=undo,
        undo_reason=reason,
        status=cards.STATUS_PARTIAL if partial else cards.STATUS_APPLIED,
    )
    from kiro_crew import crewmate_dynamic_capabilities

    # The nudge is the crewmate-driven capability path: that
    # crewmate's Dynamic capabilities preview
    # (``kiro_crew.crewmate_dynamic_capabilities``). The card names its crewmate.
    raw_params = rec.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if rec["kind"] == catalog.KIND_CREWMATE_CAPABILITIES and await asyncio.to_thread(
        crewmate_dynamic_capabilities.enabled_for, str(params.get("member") or "")
    ):
        # A card press starts no turn: tell the crewmate, so the work that waited
        # on the capability moves to a worker that has it.
        from kiro_crew.dashboard import goal_setup

        goal_setup.schedule_capabilities_nudge(
            request.app.get("state"),
            rec["slot_key"],
            str(rec.get("title") or ""),
            card_member=str(params.get("member") or ""),
        )
    from kiro_crew import mate_tool_access

    if rec["kind"] == catalog.KIND_CREWMATE_UPDATE and mate_tool_access.is_tool_access_card(params):
        # The user turned the crewmate's preview on (Turn on): the request that
        # raised this card becomes its own capabilities card, and the conductor
        # is reloaded onto the request tools and told.
        from kiro_crew.dashboard import goal_setup

        goal_setup.schedule_tool_access_nudge(
            request.app.get("state"),
            rec["slot_key"],
            app=request.app,
            session_key=str(rec.get("session_key") or ""),
            card_member=str(params.get("name") or ""),
        )


def register_change_card_routes(app: web.Application) -> None:
    """Mount both halves. The agent half's prefix must stay strict-internal."""
    app.router.add_get("/api/cards/agent/kinds", api_cards_agent_kinds)
    app.router.add_get("/api/cards/agent/settings", api_cards_agent_settings)
    app.router.add_get("/api/cards/agent/capabilities", api_cards_agent_capabilities)
    app.router.add_get("/api/cards/agent/diagnose", api_cards_agent_diagnose)
    app.router.add_post("/api/cards/agent/propose", api_cards_agent_propose)
    app.router.add_get("/api/cards/agent/status", api_cards_agent_status)
    from kiro_crew.dashboard.handlers.template_server_request import api_cards_agent_request_server

    app.router.add_post("/api/cards/agent/request-server", api_cards_agent_request_server)
    app.router.add_get("/api/cards/pending", api_cards_pending)
    app.router.add_post("/api/cards/{card_id}/preview", api_cards_preview)
    app.router.add_post("/api/cards/{card_id}/cancel", api_cards_cancel)
    app.router.add_post("/api/cards/{card_id}/dismiss", api_cards_dismiss)
    app.router.add_post("/api/members/import", api_members_import)
    app.router.add_get("/api/members/import/{sha256}/files", api_members_import_files)
