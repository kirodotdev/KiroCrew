"""The send path: ``POST /api/chat``, its SSE writer, and the steer-or-queue decision a
busy slot's human send asks for.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _CREATABLE_MODES,
        _SSE_WRITE_DEADLINE_SECS,
        HUMAN_TURN_META_KEY,
        RESERVED_ROW_META_KEYS,
        STEER_AUTO,
        STEER_REQUEUED,
        STEER_STEERED,
        TURN_ACTOR_META_KEY,
        TURN_END_WIRE_CLS,
        DashboardState,
        KiroCrewConfig,
        _app_acquisition_conflict,
        _app_claim_refused,
        _app_slot_acquisition_denial,
        _app_slot_acquisition_recheck,
        _build_stream_chunk,
        _ChatSlot,
        _deny_app_session_settings,
        _emit_agent_assignment,
        _history_key_for,
        _maybe_auto_title,
        _normalize_slot_key,
        _record_explicit_agent_selection,
        _redact_meta,
        _run_chat,
        _start_next_queued_turn,
        app_slot_is_local_user_session,
        attachment_meta,
        audit_app_slot_denial,
        chat_message_frame,
        cron_creator_admission,
        cron_slot_creator,
        deny_app_session_control,
        drained_to_thread,
        effective_session_key,
        is_claude_code,
        is_harness_slash_command,
        is_registered_agent_name,
        load_window_off_loop,
        logger,
        maybe_auto_link_slack,
        maybe_auto_tag,
        members_mod,
        normalize_send_id,
        normalize_theme_consent_sha,
        pin_private_agent_store,
        queue_for_next_turn,
        queued_text_for_display,
        quote_meta,
        read_bounded_json,
        relay_archive_refusal,
        request_slot_origin,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        restore_agent_selection,
        row_mid,
        sel,
        slot_is_live,
        slot_not_found,
        spawn_guarded_turn,
        start_queue_persist,
        steer_into_running_turn,
        subagents_hold_user_messages,
        warm_project_agent_names,
        warn_if_not_durable,
    )


def _sweep_stale_permissions(slot: "_ChatSlot") -> None:
    """Mark unresolved permissions from prior turns as stale.

    Called once at turn-start, before the new user message is appended.
    Safe: if we're starting a new turn, any prior unresolved permission
    is definitionally orphaned — the LLM that requested it is gone.

    Note: if the same slot is open in multiple tabs, an in-flight pending
    approval in tab A may be marked stale by a turn-start in tab B. The
    failure mode is benign (user re-clicks approve); single-tab use is
    unaffected.
    """
    for msg in slot.messages:
        if msg.get("role") != "permission":
            continue
        try:
            cls = json.loads(msg.get("cls", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls, dict):
            # Valid JSON but not an object (e.g. [], "x", 123, null) — cannot
            # carry a "resolved" key; skip rather than raise TypeError and
            # abort the whole sweep. Mirrors parse_cls_meta() in state_parts/transcript_rows.py.
            continue
        if "resolved" in cls:
            continue
        cls["resolved"] = "stale"
        msg["cls"] = json.dumps(cls)
        slot._dirty = True
        sel().log_api_access(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources=cls.get("request_id", ""),
        )


def _send_binds_agent(agent: str, slot: Any) -> bool:
    """Whether a ``POST /api/chat`` naming *agent* writes it onto *slot*."""
    return bool(agent) and slot.agent in (None, "")


def _send_writes_persona(body: dict) -> bool:
    """Whether a ``POST /api/chat`` body writes the slot's persona fields.

    ``color_theme`` carries the write; ``theme_consent`` and
    ``theme_consent_sha`` are applied with it and never alone.
    """
    return "color_theme" in body


async def _send_harness_command(message: str) -> str:
    """The harness slash command *message* opens with, or ``""`` for plain text.

    The same predicate the runner forwards on (``is_harness_slash_command``), so
    what this refuses is exactly what would reach the harness as a command. The
    config is read only for a first word that starts with ``/``.
    """
    first_word = message.split()[0] if message.strip() else ""
    if not first_word.startswith("/"):
        return ""
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    cc_provider = is_claude_code(cfg.agent.provider)
    return first_word if is_harness_slash_command(first_word, cc_provider=cc_provider) else ""


def steer_is_auto(value: object) -> bool:
    """Whether a send's ``steer`` flag asks Jev to choose the path.

    Only the exact string, case- and space-insensitively. A boolean ``True`` is a
    MANUAL steer and must never read as auto: that flag is what every existing
    client sends, and reading it as a request to decide would put an oracle on a
    path the sender already answered.
    """
    return isinstance(value, str) and value.strip().lower() == STEER_AUTO


async def decided_message_handling(slot: Any, message: str) -> tuple[bool, dict | None]:
    """Ask ``message.steer`` whether *message* queues instead of steering.

    Returns ``(queues, record)``: whether to take the QUEUE path, and the decision
    row to stamp on the persisted user row (``None`` when nothing was decided, or
    when the row itself was refused).

    A BOOLEAN rather than the point's choice string, so the caller holds no copy of
    that vocabulary and cannot drift from it: every refusal -- the seam off, the
    session unsampled, a timeout, an answer outside the two options, a failed
    import -- is ``False``, which is the steer path a manual Steer and the
    composer's default have always taken.

    Called for a send the dashboard's own human made while a turn is running, and
    nowhere else (the busy branch's ``not request_app`` conjunct): an app token, an
    integration or a cron has nobody watching the reply, and every such send
    already falls through to the fail-closed queue. Consent and sampling are the
    seam's own gates, re-checked inside ``decide``.

    Never raises except cancellation. The seam may cost an observation and must
    never cost a send.
    """
    try:
        # Imported HERE, not at module scope: this module is on the gateway's boot
        # path and the decisions package is optional, off by default, and pulls the
        # config loader in behind it.
        from kiro_crew.decisions.points import message_steer

        session_key = effective_session_key(slot)
        decided = await message_steer.steer_or_queue(
            message,
            session_key=session_key,
            # The live list, sliced and read off the loop by the point; nothing is
            # written back.
            rows=getattr(slot, "messages", ()) or (),
        )
        if decided is None:
            return False, None
        # The row is written BEFORE the path is taken, and the receipt is stamped
        # only when it landed: the strip's thumbs POST this turn id, so a receipt
        # from a row `append` refused would invite a verdict about a decision the
        # log does not hold. Off the loop -- the append locks a file.
        record = await asyncio.to_thread(message_steer.record_outcome, session_key, decided)
        return decided.get("choice") == message_steer.CHOICE_QUEUE, record
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return False, None


#: Request key the send path stamps when the person typed into a Crew Mode worker:
#: ``(root, worker, message)``, read by :func:`api_chat` once the send succeeded.
_CREW_PERSON_STEER = "kirocrew_crew_person_steer"

#: Queue-entry key naming ``(root, worker)`` on a person's queued send into a
#: Crew Mode worker; the drain records the steer when it delivers that entry.
CREW_STEER_QUEUE_KEY = "_crew_person_steer"


def mark_crew_steer(entry: dict[str, Any], root: str, worker: str) -> None:
    """Mark queue *entry* as the person's steer into *worker* of Crew Mode *root*.

    In memory only, like a held steer: an entry a restart restores comes off a
    writable file, so it is still delivered but never recorded as the person's.
    """
    entry[CREW_STEER_QUEUE_KEY] = (root, worker)


#: A person's steer written into a crew member's running turn and not yet read by
#: it: slot key -> steer text -> ``(root, worker)``. Recorded on the ledger when the
#: turn reads it (:func:`crew_steer_consumed`), moved onto the queue entry when the
#: turn's end requeues it (:func:`crew_steer_requeued`), and dropped when it is taken
#: back or discarded (:func:`crew_steer_discarded`), so a withdrawn steer is never
#: on the ledger. In memory only: a restart loses the record, never adds one.
_HELD_CREW_STEERS: dict[str, dict[str, tuple[str, str]]] = {}


def hold_crew_steer(slot_key: str, message: str, root: str, worker: str) -> bool:
    """Hold *message*'s record until the running turn reads it (see above).

    ``True`` only when this call made the hold. An earlier unread steer of the same
    text keeps its hold, and the caller must not drop it when its own attempt fails
    (the duplicate is refused, but the first one is still read and recorded).
    """
    held = _HELD_CREW_STEERS.setdefault(slot_key, {})
    if message in held:
        return False
    held[message] = (root, worker)
    return True


def _take_held(slot_key: str, message: str) -> tuple[str, str] | None:
    held = _HELD_CREW_STEERS.get(slot_key)
    if not held:
        return None
    marker = held.pop(message, None)
    if not held:
        _HELD_CREW_STEERS.pop(slot_key, None)
    return marker


def crew_steer_consumed(state: Any, slot: Any, message: str) -> None:
    """The running turn read *message*: record the held steer, off the turn's path."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is None:
        return
    try:
        from kiro_crew.dashboard.handlers import crew_workers

        task = asyncio.get_running_loop().create_task(
            crew_workers.person_steer(state, marker[0], marker[1], message)
        )
        tasks = getattr(state, "_background_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except Exception:
        logger.warning("crew steer: a read steer was not recorded", exc_info=True)


def crew_steer_requeued(slot: Any, message: str, entry: dict[str, Any] | None) -> None:
    """*message* went back into the queue: the drain records it when it delivers it."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is not None and entry is not None:
        mark_crew_steer(entry, *marker)


def crew_steer_discarded(slot: Any, message: str) -> None:
    """*message* was taken back or thrown away unread: it is never recorded."""
    _take_held(getattr(slot, "key", ""), message)


def _person_crew_worker_root(request: web.Request, state: Any, slot: Any, app: str) -> str:
    """The Crew Mode root *slot* works for when the dashboard owner typed into it, else ``""``.

    Only the person's own send counts: no app token, the owner's dashboard request,
    and a slot the slot list stamps as some root's worker. ``""`` for every send
    while ``crew_mode.APPROVAL_ROUTING_LIVE`` is off (the Crew panel part turns it
    on), so a send typed into a crew member's own composer is an ordinary send until
    then. No I/O.
    """
    if app:
        return ""
    try:
        from kiro_crew import crew_mode

        if not crew_mode.APPROVAL_ROUTING_LIVE:
            return ""
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if not is_owner_dashboard_request(request):
            return ""
        from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

        # The lineage the slot frame reads: a session its conductor released is a
        # plain session again, and an adopted one belongs to its new conductor.
        crew_root, _is_root = crew_mode.live_stamps(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        ).get(slot.key, ("", False))
        return crew_root
    except Exception:
        logger.debug("crew steer: could not tell whether this send is to a worker", exc_info=True)
        return ""


def _send_id_of(user_meta: dict | None) -> str:
    return (normalize_send_id(user_meta.get("sendId")) or "") if user_meta else ""


def _committed_if_accepted(request: web.Request, resp: Any, user_meta: dict | None) -> Any:
    """Record the claimed send when *resp* accepted it; a refusal gives the claim back."""
    from kiro_crew.shared_crew import send_race

    if getattr(resp, "status", 500) < 400:
        send_race.commit_request(request, message_id=_send_id_of(user_meta))
    return resp


def _composer_locked_answer(
    request: web.Request, state: Any, slot: Any, steer: object
) -> web.StreamResponse | None:
    """409 ``composer_locked`` when someone else holds the session's typing lease.

    Only a dashboard user's send is held: an app or automation send has no
    composer and no lease. A steer into a running turn passes, so a locked
    collaborator can still steer or stop the turn that is running.
    """
    if not request.get("is_dashboard_user", False):
        return None
    if steer and getattr(slot, "turn_running", False):
        return None
    from kiro_crew.shared_crew import typing_lease
    from kiro_crew.shared_crew.identity import actor_of

    body = typing_lease.refuse_if_locked(state, slot.key, actor_of(request).principal)
    if body is None:
        return None
    return web.json_response(
        {
            "error": body["error"],
            "code": body["code"],
            "session_key": body["session_key"],
            "holder": body["holder"],
            "lease": body["lease"],
        },
        status=409,
    )


async def _settle_send_race(
    request: web.Request, state: Any, slot: Any, body: dict, message: str
) -> web.StreamResponse | None:
    """Claim the session's next send revision for this send, or answer it.

    None means the claim is granted and stored on the request: the branch that
    records the send commits it, and the claim is given back when the request's
    handler task ends without recording it (``send_race.Claim.owner``). A
    lost race answers 409 ``send_race`` naming the winner; a retry of a send this
    principal already won with answers that send and accepts nothing.
    """
    from kiro_crew.shared_crew import admission_entry, send_race
    from kiro_crew.shared_crew.identity import actor_of

    try:
        base = send_race.base_revision_of(body.get("base_revision"))
    except send_race.BaseRevisionError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_base_revision"}, status=400)
    registry = send_race.send_race_for(state)
    result = await registry.claim(
        slot.key,
        principal=actor_of(request).principal,
        base_revision=base,
        idempotency_key=admission_entry.explicit_idempotency_key(request),
    )
    if isinstance(result, send_race.Conflict):
        lost = send_race.conflict_body(result, session_key=slot.key)
        return web.json_response(
            {
                "error": lost["error"],
                "code": lost["code"],
                "session_key": lost["session_key"],
                "current_revision": lost["current_revision"],
                "winner": lost["winner"],
            },
            status=409,
        )
    if isinstance(result, send_race.Replay):
        recorded = await admission_entry.chat_replay(
            request, session_key=effective_session_key(slot), slot_key=slot.key, message=message
        )
        if recorded is not None:
            return admission_entry.chat_replay_response(recorded, slot_key=slot.key)
        return web.json_response(send_race.replay_body(result, slot_key=slot.key))
    request[send_race.CLAIM_REQUEST_KEY] = result.claim
    return None


async def api_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/chat — send message to a slot, stream response via SSE.

    A send the person types into a Crew Mode worker's own composer is a steer, the
    same as the side panel's steer route: once it was accepted, the one person-steer
    transaction (``crew_workers.person_steer``) writes its ``steer`` event on the
    ledger that holds the worker's item, and a JSON receipt carries ``ledger_event``
    and ``ledger_error`` so the composer never shows a recorded steer that was only
    delivered.
    """
    resp = await _api_chat_send(request)
    pending = request.get(_CREW_PERSON_STEER)
    # Only an accepted send is a steer; its receipt is always a plain 200.
    if not pending or resp.status != 200:
        return resp
    root, worker, message = pending
    from kiro_crew.dashboard.handlers import crew_workers

    receipt: Any = None
    if isinstance(resp, web.Response) and resp.content_type == "application/json":
        try:
            receipt = json.loads(resp.text or "{}")
        except ValueError:
            receipt = None
    if isinstance(receipt, dict) and receipt.get("stopped"):
        # Stop landed while the send was held: nothing reached the crew member.
        return resp
    if isinstance(receipt, dict) and receipt.get("steered") is True:
        # Written into the running turn, not read yet: recorded when the turn reads
        # it (``crew_steer_consumed``), never if the person takes it back first.
        receipt.update(ledger_event=None, ledger_error=None, ledger_pending=True)
        return web.json_response(receipt)
    state = request.app["state"]
    queue_id = receipt.get("queue_id") if isinstance(receipt, dict) else None
    if isinstance(receipt, dict) and receipt.get("queued") is True and not queue_id:
        # The turn's teardown requeued the steer while it was being written. The
        # hold set before the write already moved onto its entry
        # (``crew_steer_requeued``), and the drain may have delivered and recorded
        # it by now: record nothing here, and never look the entry up by its text.
        receipt.update(ledger_event=None, ledger_error=None, ledger_pending=True)
        return web.json_response(receipt)
    if isinstance(queue_id, str) and queue_id:
        # Queued, not delivered: the steer is recorded when the drain hands the
        # entry to the worker (with its text as edited then), and never when the
        # person cancels it first (edge review R2 F5).
        slot = (getattr(state, "_slots", None) or {}).get(worker)
        entry = next(
            (e for e in (getattr(slot, "_queue", None) or []) if e.get("id") == queue_id),
            None,
        )
        if entry is not None:
            mark_crew_steer(entry, root, worker)
            receipt.update(ledger_event=None, ledger_error=None, ledger_pending=True)
            return web.json_response(receipt)
    try:
        recorded = await crew_workers.person_steer(state, root, worker, message)
    except Exception:
        logger.warning("crew steer: the composer's steer was not recorded", exc_info=True)
        recorded = {"ledger_event": None, "ledger_error": "not_recorded"}
    if isinstance(receipt, dict):
        receipt.update(recorded)
        return web.json_response(receipt)
    return resp


async def _api_chat_send(request: web.Request) -> web.StreamResponse:
    """The send itself; :func:`api_chat` adds the Crew Mode worker steer record."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    message = body.get("message", "").strip()
    agent = body.get("agent", "")
    slot_name = body.get("slot")
    color_theme = body.get("color_theme", "")
    user_meta = body.get("meta")  # knowledge/files/pastes metadata from frontend
    if not isinstance(user_meta, dict):
        user_meta = None
    else:
        # The row's decision receipt is SERVER-minted and must never be one a
        # caller can write. `meta` rides verbatim onto the persisted user row
        # (`_redact_meta` redacts string values; it is not an allowlist) and onto
        # the queue entry, and the transcript renders `meta.decisions_strip` as a
        # Jev decision with a verdict control whose POST names the turn id in it.
        # So an app token or any other caller could otherwise stamp a decision
        # nobody made and file feedback against it. Dropped HERE, where the field
        # enters, so every downstream user of `user_meta` -- the dispatch row, the
        # busy-slot queue entry, the sub-agent hold -- is covered by one gate
        # rather than each remembering.
        # The message author is the server's to name (see the stamp below).
        from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY

        user_meta = {
            k: v
            for k, v in user_meta.items()
            if k not in RESERVED_ROW_META_KEYS and k != MESSAGE_AUTHOR_META_KEY
        }
        # The whole-message quote is bounded HERE, once, for every path below:
        # the immediate row persists `user_meta` verbatim, so a bound applied
        # only where the queue paths read it would leave that row unbounded.
        # A record the bound refuses is dropped whole; the text beside it still
        # carries the blockquote.
        if "quote" in user_meta:
            # Bounded once here; redacted here too when the sender is not the
            # session's own human (an app token), so every later read of
            # `user_meta` -- immediate row, queue entry, hold entry, frames --
            # carries the same form its text gets.
            bounded_quote = quote_meta(user_meta, user_origin=not bool(request.get("app", "")))
            user_meta.pop("quote")
            user_meta.update(bounded_quote)
        if not user_meta:
            user_meta = None
    theme_consent = body.get("theme_consent") is True
    # Content-bound persona consent: the sha256 hex the user
    # granted in the consent modal. Injection is gated on this matching the
    # persona text read from disk server-side; the legacy boolean above is
    # still parsed (backward-compatible bodies + logging) but does not grant
    # injection by itself. Normalize + full-match to 64 lowercase hex here so a
    # malformed value (non-ASCII "é", wrong length, non-str) becomes None
    # (absent) rather than reaching hmac.compare_digest and crashing the turn
    # with a TypeError.
    theme_consent_sha = normalize_theme_consent_sha(body.get("theme_consent_sha"))
    if not isinstance(color_theme, str) or not (
        color_theme == "" or color_theme.startswith("custom-")
    ):
        color_theme = ""
    if not isinstance(slot_name, str) and slot_name is not None:
        slot_name = None  # coerce non-string slot to auto-generate
    _requested_key = _normalize_slot_key(slot_name) if slot_name else ""
    # Which of the owner's tabs is talking in this slot, so a live guide
    # observation asks that tab and no other (``guide_observe``), and which
    # language that tab's dashboard shows (``X-UI-Lang``), so ``find_ui`` quotes
    # labels as that screen spells them. Only the owner's own send, only the
    # tab id and a shipped-catalog tag, only in memory.
    _guide_tab = request.headers.get("X-Guide-Tab")
    _ui_lang = request.headers.get("X-UI-Lang")
    if _requested_key and (_guide_tab or _ui_lang) and not request.get("app", ""):
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if is_owner_dashboard_request(request):
            from kiro_crew.dashboard.guide_observe import note_chat_sender

            note_chat_sender(state, _requested_key, _guide_tab, _ui_lang)
    # Ownership BEFORE get_or_create_slot below, whose memory-mode and
    # under-construction 409s would otherwise answer an app about a session it
    # may not see. Member, cron and workflow keys are refused here too. The
    # post-create check further down stays authoritative.
    acquisition_judged: Any = None
    request_app = request.get("app", "")
    if request_app and slot_name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request,
            state,
            request_app,
            slot_name,
            "chat_send",
            session_grant_route=True,
        )
        if denied is not None:
            return denied
    # A tab restored as a sidebar row loads its window here, with the transcript
    # read off the loop, before anything below reads its rows. The live slot is
    # read again after the await, so a close or a replacement during the load is
    # what the rest of this handler sees.
    if _requested_key and (_pending := state._slots.get(_requested_key)) is not None:
        await load_window_off_loop(_pending)
    existing = state._slots.get(_requested_key) if _requested_key else None
    # An app's auto-created slot must not land on a transcript it does not own:
    # its first save would stamp the app onto that transcript's metadata line.
    if (
        _requested_key
        and existing is None
        and await _app_claim_refused(
            state, request.get("app", ""), "chat_send", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()
    if (
        existing is not None
        and existing.mode == members_mod.DM_SLOT_MODE
        and not members_mod.is_dispatchable_member_name(existing.agent)
    ):
        sel().log_api_access(
            caller=request.remote or "",
            operation="chat_send",
            outcome="denied",
            source="member_pin",
            resources=f"slot={existing.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": "this thread's crew name cannot be dispatched",
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    member_pin_match = members_mod.member_pin_matches(
        getattr(existing, "mode", None), getattr(existing, "agent", None), agent
    )
    if not isinstance(agent, str) or not (
        agent == ""
        or is_registered_agent_name(agent)
        or member_pin_match
        # A free-form display name is admitted only as the configured member it
        # names (config read off-loop, on this miss path alone); a non-member
        # string that fails the grammar is not a template and stays refused.
        or await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent)
    ):
        _emit_agent_assignment(str(slot_name or ""), str(agent), outcome="denied_invalid")
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Honor memory_mode from the body when auto-creating a slot (e.g. AgentRock
    # skill dispatch defaults to "temporary"). Only validated values are passed
    # through; anything else is dropped so get_or_create_slot uses its default.
    # If the slot already exists, get_or_create_slot raises on a memory_mode
    # mismatch, matching POST /api/chat/slots semantics.
    requested_memory_mode = body.get("memory_mode")
    if requested_memory_mode not in ("persistent", "incognito", "temporary"):
        requested_memory_mode = None

    # Honor mode from the body when auto-creating a slot, mirroring memory_mode
    # above: an app whose worker slot lives only in gateway memory (e.g. Design
    # Critique) repeats mode on send(), so a slot recreated here after a
    # gateway restart keeps its non-"" surface and stays out of the chat
    # sidebar's surface allowlist. Only creation-allowlisted values pass;
    # anything else is dropped so get_or_create_slot uses its default. Unlike
    # memory_mode there is no mismatch error: get_or_create_slot ignores mode
    # for an already-existing slot.
    requested_mode = body.get("mode")
    if not isinstance(requested_mode, str) or requested_mode not in _CREATABLE_MODES:
        requested_mode = ""

    # member-* keys are RESERVED for member DM threads, which are born only
    # through POST /api/members/{slug}/thread. Auto-creating one here (e.g. a
    # send racing a gateway restart that dropped the live slot, or an app
    # token naming the key) would mint an ordinary unpinned slot on the
    # member key — every pin guard is conditioned on mode=="member", so the
    # squatter bypasses all of them AND 409s the real thread opener forever.
    # Refused, not dropped: the caller must re-open through the member route.
    if _requested_key:
        if _requested_key.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
            if _requested_key not in state._slots:
                return web.json_response(
                    {
                        "error": "member thread slots are created only via the member thread endpoint",
                        "code": "member_slot_reserved",
                    },
                    status=409,
                )

    # `relay=1` was the retired turn relay: an older owner gateway asking this
    # peer to run a turn in one of its slots. Refused whole, before slot
    # creation, so such a send can never auto-create a blank session here.
    if request.query.get("relay") == "1":
        return web.json_response(
            {"error": "the remote turn relay is retired", "code": "remote_relay_retired"},
            status=410,
        )
    cron_creator = await cron_slot_creator(request)
    if cron_creator:
        refusal, _, _ = await cron_creator_admission(request, state, slot_name, cron_creator)
        if refusal is not None:
            return refusal

    if request_app and _requested_key:
        denied = await _app_slot_acquisition_recheck(
            state, request_app, _requested_key, acquisition_judged, "chat_send"
        )
        if denied is not None:
            return denied
    created_in_send = slot_name is None or _normalize_slot_key(slot_name) not in state._slots
    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
            mode=requested_mode,
            memory_mode=requested_memory_mode,
            # Human request-layer path: a person sending a chat message. The
            # origin conjunct in state_parts/slot_directory.py still excludes app-token callers. A
            # slot an attested cron opens is not a person's and is not counted.
            count_user_session=not cron_creator,
        )
    except ValueError as exc:
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="chat_send",
            outcome="denied",
            source="memory_mode_mismatch",
            resources=f"slot={slot_name}",
            error=str(exc),
        )
        if request_app:
            return _app_acquisition_conflict(
                state, request_app, _requested_key, acquisition_judged, exc
            )
        return web.json_response({"error": str(exc)}, status=409)
    if cron_creator and created_in_send:
        # Attribute the slot to the cron that opened it, inside the synchronous
        # window after the mint, as session_control.create_session does for an
        # agent cron's child. Never re-stamped on an existing slot a cron names.
        slot._created_by = cron_creator

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps keep access to their own slots. The sessionApproval grant lets an
    # enabled app send a turn into an existing user-owned slot. A response
    # option click is one such turn. The grant never crosses into another
    # app's slot. The grant verdict is the one this request already read.
    denied = await deny_app_session_control(request, request_app, slot, slot.key, "chat_send")
    if denied is not None:
        return denied
    # The dashboard user and the slot's owning app may change the slot's settings
    # through this route; the sessionApproval grant reaches a user's session to
    # send a turn and nothing else.
    may_configure = not request_app or bool(getattr(slot, "_app", ""))
    steer = body.get("steer") if may_configure else None
    # Send now, after losing a send race, is a new submission that waits its turn
    # like any send: it never steers a running turn.
    if body.get("send_now") is True:
        steer = None
    if not may_configure:
        # The dedicated agent route refuses an app on a slot it does not own, and
        # persona consent is the user's own grant from the consent modal. Refused
        # rather than dropped, like the agent mismatch below: running the turn on
        # an agent other than the one named would be a silent substitution. A
        # harness slash command is refused here, above the busy branch, so a
        # queued copy cannot run it later.
        command = await _send_harness_command(message)
        # The slot may have closed, or a cron or channel binder may have
        # re-linked it, during the permission and config reads; the grant
        # reaches only a local user session.
        if (
            state._slots.get(slot.key) is not slot
            or slot.is_closing
            or not app_slot_is_local_user_session(slot)
        ):
            audit_app_slot_denial(
                request_app,
                "chat_send",
                slot.key,
                "slot closed or re-linked during the permission read",
            )
            return slot_not_found()
        # No await sits between this check and the agent write below for a slot
        # this grant reaches: the member awaits never run for one, and the
        # mismatch branch's awaits run only on a slot that already has an agent.
        # The persona condition reads only the request body.
        if command:
            return _deny_app_session_settings(request_app, slot.key, f"command={command[:64]}")
        if _send_binds_agent(agent, slot):
            return _deny_app_session_settings(request_app, slot.key, f"agent={agent}")
        if _send_writes_persona(body):
            return _deny_app_session_settings(request_app, slot.key, "field=color_theme")
        sel().log_api_access(
            caller=request_app,
            operation="chat_send",
            outcome="allowed",
            source="app_isolation",
            resources=f"permissions.sessionApproval|slot={slot.key}",
        )
        # The gateway mints the row id for a turn sent onto another's session.
        if user_meta is not None and "mid" in user_meta:
            user_meta = {k: v for k, v in user_meta.items() if k != "mid"} or None
    # The member-pin refusal sits AFTER the app-ownership 404s (a 409 here
    # for an app would be an existence oracle for slots it may not see) and
    # BEFORE the _human_seen attendance mark, so a denied request leaves the
    # slot exactly as it found it.
    if slot.mode == "member" and agent and agent != slot.agent:
        # Member DM threads are pinned to their crew. The generic mismatch
        # branch below would also refuse this, but the pin deserves its own
        # machine-readable refusal — and it must hold even for a member slot
        # whose agent is somehow empty (the elif below would otherwise adopt
        # the request's agent onto the pinned thread).
        _emit_agent_assignment(slot.key, agent, outcome="denied_member_pin")
        return web.json_response(
            {"error": "member thread agent is pinned", "code": "member_thread_agent_pinned"},
            status=409,
        )
    if slot.mode == "member":
        _member_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if slot.agent not in _member_cfg.agents:
            sel().log_api_access(
                caller=request.remote or "",
                operation="chat_send",
                outcome="denied",
                source="member_pin",
                resources=f"slot={slot.key}",
                error=f"registry no longer names {slot.agent}",
            )
            return web.json_response(
                {
                    "error": "this thread's crew no longer exists",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
        if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX):
            _send_binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, slot.key)
            if _send_binding is None or _send_binding.get("member", "") != slot.agent:
                sel().log_api_access(
                    caller=request.remote or "",
                    operation="chat_send",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slot={slot.key}",
                    error="member binding missing or mismatched",
                )
                return web.json_response(
                    {
                        "error": "this thread's binding is missing or no longer matches",
                        "code": "member_binding_missing",
                    },
                    status=409,
                )
    if not request_app:
        # A dashboard user (no app token) typed into this slot, so a human
        # demonstrably has it open. That restores the full 2h approval
        # window even on an app-owned tab — the deny-fast window is for slots
        # nobody is watching. Only a caller with an EMPTY request_app reaches
        # here, so an app cannot forge attendance for its own worker.
        slot._human_seen = True

    if slot.agent not in (None, ""):
        # Slot already has an agent — only reject explicit mismatches (non-empty different agent).
        # Empty agent in request means "use existing" (e.g. follow-up messages from frontend).
        if agent and slot.agent != agent:
            # Different NAMES can still be the same BINDING: slots record the
            # resolved default ALIAS at creation, while a client may send the
            # underlying kiro agent name (the E2E smoke tests do exactly this).
            # 409 only when the two names resolve to different dispatch targets.
            # Identity is EVERY dispatch-relevant binding field — kiro agent,
            # workspace, memory store, and model — because aliases configure
            # memory stores and model pins independently of workspace, and a
            # request landing in another alias's memory store is the exact
            # cross-scoping this guard exists to prevent. An unknown requested
            # name (requested_resolved=False) still 409s — the resolver would
            # silently fall back to the default, which is the same lie.
            # Resolution failure falls back to the strict name comparison
            # (fail closed), reported as its own outcome so a config-load
            # blip is not triaged as an agent-naming problem.
            same_binding = False
            resolution_failed = False
            compared_binding = (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
                slot.agent_kind,
            )
            try:
                # Config load is file IO (stat + read + jsonschema validate on a
                # cache miss), so it rides a thread like the member-slot load
                # above — never the event loop.
                _cfg = await asyncio.to_thread(KiroCrewConfig.load)
                # Resolve within the slot's PROJECT scope, exactly as dispatch
                # does (see the agent-switch path below): a project-scoped agent
                # exists only inside slot.project, so resolving without it would
                # fall back to default bindings and falsely equate a
                # project-agent slot with a request naming the default alias.
                # Keep both lookups on one captured selection and off-loop:
                # private store validation reads ownership files even on cache hits.
                await warm_project_agent_names(
                    compared_binding[1] or None, operation="api_chat", source="dashboard"
                )

                def _compare_bindings():
                    return (
                        resolve_session_agent_bindings(
                            resolve_agent_bindings,
                            _cfg,
                            compared_binding[3],
                            compared_binding[0],
                            compared_binding[1] or None,
                            agent_kind=compared_binding[6],
                        ),
                        resolve_agent_bindings(_cfg, agent, compared_binding[1] or None),
                    )

                _stored, _requested = await asyncio.to_thread(_compare_bindings)
                # Identity itself lives on ResolvedBindings, next to the field
                # set, so a new dispatch-relevant field cannot silently widen
                # this bypass. requested_resolved stays a separate caller-side
                # check: an unknown name would resolve to the default and MATCH
                # a default-bound slot, which is the lie this guard prevents.
                same_binding = _requested.requested_resolved and _stored.same_dispatch_binding(
                    _requested
                )
            except Exception:
                resolution_failed = True
                logger.warning(
                    "agent-conflict binding resolution failed; using strict name comparison",
                    exc_info=True,
                )
            if state._slots.get(slot.key) is not slot or compared_binding != (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
                slot.agent_kind,
            ):
                return web.json_response(
                    {"error": "slot changed during agent resolution", "code": "session_rebound"},
                    status=409,
                )
            if not same_binding:
                _emit_agent_assignment(
                    slot.key,
                    agent or "",
                    outcome=(
                        "denied_resolution_failed" if resolution_failed else "denied_mismatch"
                    ),
                )
                return web.json_response({"error": "slot agent mismatch"}, status=409)
            # The one outcome of this guard that overrides a 409 boundary must be
            # auditable alongside the denials and adoptions it sits between.
            _emit_agent_assignment(slot.key, agent, outcome="allowed_same_binding")
            logger.debug(
                "agent names differ but resolve to the same binding: slot=%s stored=%s requested=%s",
                slot.key,
                slot.agent,
                agent,
            )
        else:
            logger.debug("agent match for slot=%s agent=%s", slot.key, agent)
    elif _send_binds_agent(agent, slot):
        # Slot has no agent — set it if not running
        if slot.running:
            _emit_agent_assignment(slot.key, agent, outcome="denied_running")
            return web.json_response(
                {"error": "cannot set agent on running slot"},
                status=409,
            )
        slot.agent = agent
        _emit_agent_assignment(slot.key, agent)
    else:
        # No agent on slot, no agent in request — nothing to enforce.
        pass

    if not message:
        # One guard, above every dispatch branch. An empty wire text reaches
        # here only from programmatic callers (app tokens, curl, integrations)
        # — the dashboard composer always inlines staged files into the
        # message text. Such a send may still carry attachments in `meta`:
        # nothing downstream queues or broadcasts it, so any success receipt
        # would report work that was silently dropped. Refusing here keeps
        # every branch below (steer/queue, crew, subagent-hold, new turn)
        # unable to bypass the check. A guard placed below the busy branch is
        # bypassable, and that is exactly how a false `queued: true` receipt
        # happens. `message_required` is the backend-owned code already used
        # for this refusal (handlers/messaging.py).
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )

    # The person typing into a Crew Mode worker steers it: the same length limit as
    # the side panel's steer route, refused before anything is queued or shown, and
    # the steer recorded on the worker's item once the send is accepted.
    _crew_root = _person_crew_worker_root(request, state, slot, request_app)
    if _crew_root:
        from kiro_crew.dashboard.session_control_parts.session_messaging import (
            MAX_SEND_MESSAGE_CHARS,
        )

        if len(message) > MAX_SEND_MESSAGE_CHARS:
            return web.json_response(
                {
                    "error": f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
                    "code": "message_too_long",
                },
                status=400,
            )
        if _crew_root not in state._slots:
            # The worker's conductor is closed: the same admission the side
            # panel's steer route makes (a live root), checked before anything is
            # queued or delivered, so no worker turn starts behind the "reopen
            # it" notice. The person reopens the conductor to keep working.
            return web.json_response(
                {
                    "error": "this crew member's conductor is closed; reopen it to keep working",
                    "code": "crew_root_closed",
                    "root": _crew_root,
                },
                status=409,
            )
        request[_CREW_PERSON_STEER] = (_crew_root, slot.key, message)

    # The typing lease (Shared Crew): while another collaborator is typing in
    # this session, a send from anyone else is refused with the typist named,
    # and the sender keeps the draft. Steering a running turn is never refused.
    _locked = _composer_locked_answer(request, state, slot, steer)
    if _locked is not None:
        return _locked

    # First submit wins (Shared Crew): settle this send against the session's
    # send revision before any branch can queue, steer or start it. A loser is
    # refused here with the winner named, and its tab keeps the draft.
    from kiro_crew.shared_crew import send_race as _send_race

    _race_answer = await _settle_send_race(request, state, slot, body, message)
    if _race_answer is not None:
        return _race_answer

    if _send_writes_persona(body):
        slot.color_theme = color_theme
        slot.theme_consent = theme_consent
        slot.theme_consent_sha = theme_consent_sha

    from kiro_crew.dashboard import responsive_chat

    _responsive_eligible = False
    if body.get("responsive") is True or steer:
        _responsive_eligible = responsive_chat.eligible(
            request,
            slot,
            _member_cfg if slot.mode == members_mod.DM_SLOT_MODE else None,
            (
                _send_binding
                if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX)
                and slot.mode == members_mod.DM_SLOT_MODE
                else None
            ),
        )
        if _responsive_eligible and (not steer or not slot.turn_running):
            return _committed_if_accepted(
                request, await responsive_chat.accept(request, slot, message, user_meta), user_meta
            )

    # Shared Crew: a retry under a key already accepted answers its recorded work
    # and writes nothing (no row, no queue entry, no turn), whichever way the
    # first send went, so a retry can never run a prompt twice.
    from kiro_crew.dashboard.chat_runner import _drain_parked_queues
    from kiro_crew.shared_crew import admission_entry as _sc_entry

    _replay = await _sc_entry.chat_replay(
        request, session_key=effective_session_key(slot), slot_key=slot.key, message=message
    )
    if _replay is not None:
        return _sc_entry.chat_replay_response(_replay, slot_key=slot.key)

    if slot.turn_running or slot._turn_admission_reserved:
        # Mid-turn steer: inject into the RUNNING turn instead of queueing for
        # the next turn. Gated on an explicit `steer` flag + a live, steer-capable
        # inner AcpClient that _run_chat published on the slot. App-authenticated
        # sends cannot steer because doing so would inherit the live turn's human
        # provenance; they fall through to the fail-closed queue below.
        # Fire-and-forget —
        # the inline steer card materializes when kiro-cli echoes steering_consumed
        # (EVENT_STEER_CONSUMED). If steer is requested but unavailable (no live
        # client / unsupported backend / RPC error), fall through to the queue
        # path so the user's text is NEVER silently dropped.
        #
        # `steer: "auto"` is the composer's third mode: the sender asked Jev which
        # of the two shipped paths this message takes. Decided HERE, above both
        # branches, because the answer chooses between them -- and only here, where
        # a turn IS running (this branch's own condition) and the send is the
        # session's own human, are the point's preconditions already established.
        #
        # Resolved before the steer `if` rather than inside it, so the steer block
        # below keeps its exact shape: the only thing `auto` changes about it is one
        # more conjunct on its condition and the receipt it stamps.
        _auto_strip: dict | None = None
        _auto_queues = False
        if steer_is_auto(steer) and not request_app and not _responsive_eligible:
            # The turn the question is ABOUT, captured before the await. The
            # decision is a provider round-trip, so the turn it describes can end
            # while it is in flight -- and an answer about a turn that is gone is
            # not an answer about this send: "interrupt what it is doing" names
            # work that finished, and a successor turn is a different subject.
            # On a change the send takes the manual steer path (this branch's own
            # default) and carries NO receipt, because the decision that was made
            # is not about the turn the message now reaches. The row is still in
            # the log, where it belongs -- the receipt is what would misattribute
            # it.
            _turn_before = slot.task
            _auto_queues, _auto_strip = await decided_message_handling(slot, message)
            if slot.task is not _turn_before:
                _auto_queues = False
                _auto_strip = None
        # A turn still waiting for fair admission has not started: steering it would
        # deliver this text outside admission, so the send queues instead.
        from kiro_crew.shared_crew.admission_entry import admission_pending

        _admission_pending = admission_pending(slot.key)
        if steer and not request_app and not _auto_queues and not _admission_pending:
            # Client-minted send correlation id (the same `meta.sendId`
            # convention the plain send path persists): thread it through the
            # steer so the persisted row and the steer_push echo can be matched
            # back to the optimistic bubble by id rather than by text.
            # Raw client input — the sink (`normalize_send_id` at the top of
            # `steer_into_running_turn`) type-checks and length-bounds it,
            # treating anything unusable as absent (the old-client shape).
            # circular import: session_control imports this package's modules at module level.
            from kiro_crew.dashboard.session_control import containment_meta as _containment_meta

            _responsive_steer_kwargs: dict[str, Any] = (
                {"responsive": True} if _responsive_eligible else {}
            )
            _crew_held = request.get(_CREW_PERSON_STEER)
            # Held from before the write: the turn can read it before the RPC returns.
            _crew_owns_hold = False
            if _crew_held:
                _crew_owns_hold = hold_crew_steer(slot.key, message, _crew_held[0], _crew_held[1])
            outcome = await steer_into_running_turn(
                state,
                slot,
                message,
                send_id=user_meta.get("sendId") if user_meta else None,
                # This branch IS the composer: the text was typed into this
                # session's own surface by its authenticated human, which is what
                # earns a requeued entry the exemption from the drain's LINKED drop.
                # Stated rather than defaulted, because the default fails closed.
                user_origin=not bool(request_app),
                # Captured HERE, before the RPC suspends, for the same reason the peer
                # path captures it: the requeue runs in the turn's teardown and a slot
                # read there folds a mirror linked during the suspension into the
                # entry's own admission baseline. The LINKED exemption does not cover
                # that -- a new outbound mirror is never exempt, because the author
                # does not control mirror links -- so the composer needs the stamp too.
                admission=_containment_meta(state, slot),
                # The receipt for an `auto` send that was decided; absent for a
                # manual steer, which is what keeps that row byte-identical.
                decision_strip=_auto_strip,
                attachments=user_meta,
                **_responsive_steer_kwargs,
            )
            if _crew_owns_hold and outcome not in (STEER_STEERED, STEER_REQUEUED):
                # Refused outright: drop it. A requeued steer keeps its hold, which
                # the teardown's requeue moves onto the entry (or the turn's read
                # records, or a Stop drops); the RPC can return before either ran.
                crew_steer_discarded(slot, message)
            if outcome == STEER_STEERED:
                _send_race.commit_request(request, message_id=_send_id_of(user_meta))
                return web.json_response({"ok": True, "steered": True})
            if outcome == STEER_REQUEUED:
                # The turn's teardown moved it into the queue while the steer RPC
                # was suspended — queueing again would deliver the same text twice.
                _send_race.commit_request(request, message_id=_send_id_of(user_meta))
                return web.json_response({"ok": True, "queued": True})
            if _responsive_eligible:
                return _committed_if_accepted(
                    request,
                    await responsive_chat.accept(
                        request, slot, message, user_meta, steer_first=False
                    ),
                    user_meta,
                )
            # steer requested but unavailable -> fall through to queue below.
        # Queue the message - return JSON immediately (no SSE needed).
        # The existing SSE reader will pick up queued messages as _run_chat
        # processes the queue in its finally block. The message is non-empty
        # here (hoisted guard above the busy branch), so `queued: true`
        # always reports a real enqueue. `queue_id` lets the sender bind its
        # pre-send composer state to THIS entry (the dashboard's cancel-queued
        # restore), which no content-based key can do: serialization is not
        # injective and other tabs can queue colliding content.
        #
        # The client's `meta.sendId` rides on the entry too (same gate as the
        # steer path above: `normalize_send_id` treats anything unusable as
        # absent). The drain unions entry meta onto the row it writes, so the
        # queued send's row ends up carrying the same id a dispatched send's row
        # gets from `slot.append(..., meta=user_meta)` below -- the only way a
        # sender can prove ITS message landed without matching by text.
        # The attachment lists (`meta.files` / `meta.dirs`) ride the same way:
        # the renderer resolves `[attached_file N]` markers against them, and a
        # drained row without them truncates a spaced path at its first space.
        # The server-derived author rides the entry to the drain, which writes
        # it on the row and charges the drained turn to that person.
        from kiro_crew.shared_crew.attribution import author_meta as _author_meta
        from kiro_crew.shared_crew.identity import actor_of as _queued_actor_of

        # Fair admission bounds the queued message like any other: the sender's
        # and the Crew's queue depth and the request size, charged to the sender.
        _queued_admission = await _sc_entry.admit_queued_send(
            request, session_key=effective_session_key(slot), slot_key=slot.key, message=message
        )
        if _queued_admission.rejected:
            return _queued_admission.refusal_response()
        if _queued_admission.replayed:
            return _sc_entry.chat_replay_response(_queued_admission, slot_key=slot.key)
        qid = queue_for_next_turn(
            state,
            slot,
            message,
            directive_user_origin=not bool(request_app),
            # A queued turn reaches the runner through the DRAIN, so the dispatch
            # keyword this handler passes for an IMMEDIATE send cannot carry the
            # actor here. It rides the entry's meta instead, which is what
            # `_actor_for_queue_items` reads; unstamped, the drain falls back to
            # `user` and files an app's send as a person's. A script cron's
            # `send_to_session` is attested the same way (`cron_creator`).
            turn_actor="app" if request_app else ("cron" if cron_creator else ""),
            send_id=normalize_send_id(user_meta.get("sendId")) if user_meta else None,
            attachments=attachment_meta(user_meta),
            quote=quote_meta(user_meta).get("quote"),
            # The receipt travels whichever way the send went, including the one
            # case where the two disagree: `auto` answered steer and the steer was
            # UNAVAILABLE, so this path runs with a record saying steer. That is the
            # truth of the decision, and the row's own `steerState` is what says how
            # the delivery ended -- a receipt withheld there would lose the only
            # record that a decision was made at all.
            decision_strip=_auto_strip,
            # A steer that fell through here (unavailable, or refused as a
            # duplicate) still means "act now": released past the sub-agent hold
            # when this turn ends. An `auto` send Jev chose to queue is a real
            # queue and waits like one.
            steer_intent=bool(steer) and not request_app and not _auto_queues,
            author=None if request_app else _author_meta(_queued_actor_of(request)),
        )
        _sc_entry.stamp_queued_work(slot, qid, _queued_admission)
        _send_race.commit_request(request, message_id=qid)
        if not _queued_admission.passthrough:
            # The admission await may have outlived the running turn's handoff:
            # the guarded drain starts the entry if the slot went idle meanwhile.
            await _drain_parked_queues(state, [slot.key])
        return web.json_response(
            {"ok": True, "queued": True, "queue_id": qid},
            headers=_sc_entry.admission_headers(_queued_admission),
        )

    # Queue a message typed while background sub-agents are still running for
    # this slot. The slot.running queue path above covers the mid-turn case;
    # this covers the idle case (spawn_run is fire-and-forget, so the main slot
    # goes idle while children run). Without the hold, this message would start a
    # main turn immediately and interleave with the [Subagent completion event]
    # injections. Queue it instead (reusing the slot queue) — the queue drain
    # releases it after the last sub-agent finishes (see chat_runner _hold_users).
    # Opt-out: if the user explicitly chose steer mode, honour it — start a new
    # turn immediately so the message is processed without waiting for children.
    # An app on a session it does not own has no opt-out (`steer` is None there).
    _sub_key = effective_session_key(slot)
    _held = subagents_hold_user_messages(state, _sub_key)
    # Messages parked while a child was live stay parked once it is flagged
    # stalled (nothing starts a drain then). A send behind them joins the queue
    # and drains it, so it is answered after them rather than ahead of them.
    _behind_parked = bool(
        not _held
        and slot._queue
        and state.subagents is not None
        and state.subagents.running_agents_for(_sub_key)
    )
    if not steer and (_held or _behind_parked):
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Same fair-admission bound as the busy-slot queue above.
        _held_admission = await _sc_entry.admit_queued_send(
            request, session_key=effective_session_key(slot), slot_key=slot.key, message=message
        )
        if _held_admission.rejected:
            return _held_admission.refusal_response()
        if _held_admission.replayed:
            return _sc_entry.chat_replay_response(_held_admission, slot_key=slot.key)

        # Same entry-meta contract as the busy-slot branch: the client's `sendId`
        # and attachment lists ride on the queue entry so the drained row
        # carries them.
        _hold_meta: dict = containment_meta(state, slot)
        _hold_sid = normalize_send_id(user_meta.get("sendId")) if user_meta else None
        if _hold_sid:
            _hold_meta["sendId"] = _hold_sid
        _hold_attachments = attachment_meta(user_meta)
        _hold_meta.update(_hold_attachments)
        _hold_meta.update(quote_meta(user_meta))
        if request_app or cron_creator:
            # Same reason as the busy-slot queue above: this entry is drained
            # later, so only its meta can name the actor. One stamp for both,
            # the app winning as on the other two arms: a script cron's send is
            # `cron`, an app's is `app` whatever else the request carries.
            _hold_meta[TURN_ACTOR_META_KEY] = "app" if request_app else "cron"
        else:
            # The server-derived author rides the entry like the busy-slot
            # branch's: the drain writes it on the row and charges the turn.
            from kiro_crew.shared_crew.attribution import author_meta as _hold_author_meta
            from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY
            from kiro_crew.shared_crew.identity import actor_of as _hold_actor_of

            _hold_meta[MESSAGE_AUTHOR_META_KEY] = _hold_author_meta(_hold_actor_of(request))
        qid = slot.queue_append(
            message,
            meta=_hold_meta,
            directive_user_origin=not bool(request_app),
        )
        _sc_entry.stamp_queued_work(slot, qid, _held_admission)
        _send_race.commit_request(request, message_id=qid)
        _redacted = queued_text_for_display(message, user_origin=not bool(request_app))
        warn_if_not_durable(slot._queue, qid, slot.key)
        # Start the durable write here too, not only in the busy-slot branch.
        # This branch holds an IDLE slot, so no drain is coming to write the
        # prompt's transcript row and no turn-end flush is scheduled: the queue
        # is the only record of the user's words until the last sub-agent
        # finishes, which is unbounded. Waiting for the periodic flush would
        # leave a window as wide as its interval, so the accept and the write
        # start from the same place. Same single-flight and same self-limiting
        # skip as the other caller.
        start_queue_persist(state, slot)
        _hold_push: dict[str, Any] = {
            "slot": slot.key,
            "content": _redacted,
            "ts": datetime.now(timezone.utc).isoformat(),
            "queue_id": qid,
        }
        if _hold_attachments:
            # Same as the busy-slot frame: the card is a cancel's restore source.
            _hold_push["meta"] = _hold_attachments
        if _hold_sid:
            _hold_push.setdefault("meta", {})["sendId"] = _hold_sid
        if _hold_meta.get("quote"):
            _hold_push.setdefault("meta", {})["quote"] = _hold_meta["quote"]
        state.broadcast_ws("queue_push", _hold_push)
        if _behind_parked:
            await _start_next_queued_turn(state, slot)
        elif not _held_admission.passthrough:
            # The admission await may have outlived the sub-agent hold.
            await _drain_parked_queues(state, [slot.key])
        # Same receipt contract as the busy-slot queue branch: `queue_id` binds
        # the sender's pre-send composer state to this exact entry. An entry the
        # durable bounds refuse is reported in the log by the call above, not on
        # the receipt: the on-screen marker belongs with its consumer.
        return web.json_response({"ok": True, "queued": True, "queue_id": qid})

    # WS mode: return JSON immediately, chunks delivered via WebSocket
    ws_mode = request.query.get("ws") == "1"

    # Only block the global broadcast for an HTTP SSE reader that IS the slot's
    # own client. An app streaming a turn on a user's session is not the user's
    # dashboard, which keeps receiving its rows.
    slot._has_reader = not ws_mode and may_configure
    # That app's stream ends with its own turn (`TURN_END_WIRE_CLS`), so the
    # user's queued follow-ups never reach it.
    turn_scoped_stream = not may_configure
    slot._file_changes = []  # Reset file-change accumulator for the new turn
    # ── Sweep orphaned permissions from prior turns ──
    _sweep_stale_permissions(slot)

    # Refuse a relay archive BEFORE the send is recorded: a refusal that appended
    # first would leave a user row in local history for a turn that never ran.
    # Every turn-refusing validation above (app ownership, member reserve,
    # busy/steer/queue) has already run, so their answers still win.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    # Fair admission (Shared Crew), charged to the initiator and decided before the
    # user row is recorded, so a refusal leaves nothing behind. The reservation
    # holds the slot across the await: a concurrent send queues behind this one.
    from kiro_crew.shared_crew import admission_entry

    _prior_reserved = slot._turn_admission_reserved
    slot._turn_admission_reserved = True
    try:
        if admission_entry.admission_enforced():
            # The session is on disk before its work is: a crash right after the
            # accept cannot leave a work record for a session that is gone.
            from kiro_crew.shared_crew.host_hooks import ensure_slot_recorded

            if not await ensure_slot_recorded(state, slot):
                from kiro_crew.shared_crew.admission_gate import Admission

                slot._has_reader = False
                return Admission(None, "rejected", store_unavailable=True).refusal_response()
        turn_admission = await admission_entry.admit_chat_turn(
            request,
            session_key=effective_session_key(slot),
            slot_key=slot.key,
            message=message,
            send_id=_send_id_of(user_meta),
        )
    finally:
        slot._turn_admission_reserved = _prior_reserved
    if turn_admission.rejected:
        slot._has_reader = False
        return turn_admission.refusal_response()
    if turn_admission.replayed:
        # A concurrent send under the same key was accepted first.
        slot._has_reader = False
        return admission_entry.chat_replay_response(turn_admission, slot_key=slot.key)

    # No per-message browse marker: browsing is a capability, not a per-turn
    # gate. The agent drives a browser by running `playwright-cli` shell
    # commands, so the capability is simply whether that binary is on PATH. The
    # agent itself decides whether to operate a browser or read with web_fetch
    # (the system prompt and the kirocrew-commands / web-browse skills tell it
    # how), so the backend injects nothing here.

    # Until the runner is spawned, every exit still owes the accepted work a settlement.
    turn_dispatched = False
    _reserve_turn_admission = not request_app
    try:
        # A slot created by this send binds to its member's private store BEFORE
        # the user row is appended: a store failure then returns with nothing
        # persisted, and the assignment snapshot (agent, project, workspace,
        # session, message count) proves no other request rebound the slot while
        # the store was being resolved.
        if created_in_send:
            from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

            if is_owner_dashboard_request(request):
                async with slot._lock:
                    assignment = (
                        slot.agent,
                        slot.project,
                        slot.workspace,
                        effective_session_key(slot),
                        len(slot.messages),
                    )
                    selection_change = None
                    try:
                        cfg = await asyncio.to_thread(KiroCrewConfig.load)
                        assigned_store = await pin_private_agent_store(
                            state, assignment[3], assignment[0], cfg, memory_mode=slot.memory_mode
                        )
                        chosen = await asyncio.to_thread(
                            resolve_agent_bindings,
                            cfg,
                            assignment[0],
                            assignment[1] or None,
                            validate_memory_files=False,
                        )
                        selection_change = await _record_explicit_agent_selection(
                            assignment[3],
                            assignment[0],
                            chosen,
                            config=cfg,
                            memory_mode=slot.memory_mode,
                            app=slot._app or "",
                        )
                    except Exception as exc:
                        from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                        return _store_unavailable_response(slot.memory_store, exc)
                    if (
                        state._slots.get(slot.key) is not slot
                        or slot.running
                        or assignment
                        != (
                            slot.agent,
                            slot.project,
                            slot.workspace,
                            effective_session_key(slot),
                            len(slot.messages),
                        )
                    ):
                        await drained_to_thread(
                            restore_agent_selection, assignment[3], selection_change
                        )
                        return web.json_response(
                            {
                                "error": "Could not save the member assignment. Try again.",
                                "code": "session_rebound",
                            },
                            status=409,
                        )
                    if assigned_store:
                        slot.memory_store = assigned_store

        # A dashboard's busy snapshot can suppress its optimistic user bubble even
        # when this send starts a turn. Echo correlated sends BEFORE starting the
        # reply so every pane sees the user row in order, independently of when the
        # HTTP receipt arrives. sendId/mid reconcile an existing optimistic bubble;
        # callers without a correlation id keep their existing delivery contract.
        _user_row_meta = _redact_meta(user_meta) if user_meta else {}
        if user_meta and not request_app and isinstance(user_meta.get("quote"), dict):
            # The human's own quote record stays as typed, like the row's content
            # it must byte-match (`_redact_meta_for_role` keeps the same rule on
            # every later persist and emit); an app's was redacted at the bound.
            _user_row_meta["quote"] = user_meta["quote"]
        if not request_app:
            # A PERSON typed this. Marked explicitly rather than inferred, because the
            # row's role and presentation class cannot tell it apart from a turn the
            # gateway drives on its own (see history.HUMAN_TURN_META_KEY). An app
            # token reaches this same handler, so the marker rides the same
            # app-origin signal as `user_origin` and `turn_actor` above: an app's
            # send is not a human turn and must not advance the ranking stamp.
            _user_row_meta[HUMAN_TURN_META_KEY] = True
        else:
            # The queued path stamps the same actor on its entry, which the drain
            # unions onto the row; the title counter reads it off either row.
            _user_row_meta[TURN_ACTOR_META_KEY] = "app"
        # Shared Crew attribution: the server names the author from the actor the
        # auth middleware published, never from the body, and the first accepted
        # message of a never-stamped slot records its creator. Function-local
        # because this module's functions run on the chat_handlers facade globals.
        from kiro_crew.shared_crew.attribution import stamp_creator_if_fresh, stamp_message_author
        from kiro_crew.shared_crew.audit import schedule_action_audit
        from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType
        from kiro_crew.shared_crew.identity import actor_of

        _actor = actor_of(request)
        stamp_message_author(_user_row_meta, _actor)
        stamp_creator_if_fresh(
            slot,
            _actor,
            prior_user_turns=sum(1 for m in slot.messages if m.get("role") == "user"),
        )
        _user_row = slot.append("user", message, "msg msg-u", meta=_user_row_meta)
        _send_race.commit_request(
            request, message_id=str(_user_row.get("meta", {}).get("mid") or _send_id_of(user_meta))
        )
        schedule_action_audit(
            ActionType.MESSAGE,
            session_key=slot.key,
            initiator=_actor,
            outcome=ActionOutcome.ACCEPTED,
            target=str(_user_row.get("meta", {}).get("mid") or ""),
        )
        _user_mid = _user_row.get("meta", {}).get("mid")
        if not may_configure or (ws_mode and user_meta and user_meta.get("sendId")):
            # Raw user content belongs on the per-client slot-authorized WS path.
            # The global SSE queues have no slot gate. In-band sends keep
            # their existing stream contract and must not gain an extra WS echo.
            # An app's turn on a user's session always reaches the user's open tabs,
            # whose composer never drew it.
            state.broadcast_ws(
                "chat_message",
                chat_message_frame({**_user_row, "slot": slot.key}, include_metadata=True),
            )

        # Note: untitled slots display as "New Session…" via _ChatSlot.display_title
        # (serialization layer), so there's no bare chat-N flash to patch here. The
        # LLM titling is kicked off below, before _run_chat.

        # ── AutoNudge: user input cancels any pending nudge timer (user wins). ──
        responsive_chat.notify_user_input(request, slot)

        # Drain stale pending messages from previous turns that completed
        # after their SSE reader disconnected. Must happen BEFORE _run_chat
        # so we don't discard the new turn's output.
        slot.drain()

        # Kick off LLM titling now, from the first user message, so the title lands
        # *during* the first turn instead of waiting for the whole response to
        # finish (chat_done). Runs on an isolated background kiro-cli session
        # concurrent with the turn. No-ops once titled / in-flight; the instant
        # 60-char provisional stays as the fallback if the LLM SKIPs or errors.
        # Not from an app's text on a user's session: the user's own turns name it.
        if may_configure and not slot._titled and not slot._title_in_flight:
            _tt = asyncio.create_task(_maybe_auto_title(state, slot))
            # Expose the handle so chat_done's chained title→refresh pass can wait
            # for this attempt to settle (see _title_then_refresh in chat_runner).
            slot._title_task = _tt
            state._background_tasks.add(_tt)
            _tt.add_done_callback(state._background_tasks.discard)

        # Auto-tag: derive a tag from the session's project directory (deterministic,
        # no LLM). Fire-and-forget, same pattern as auto-title.
        if not getattr(slot, "_auto_tagged", False):
            _at = asyncio.create_task(maybe_auto_tag(state, slot))
            state._background_tasks.add(_at)
            _at.add_done_callback(state._background_tasks.discard)

        if _reserve_turn_admission:
            slot._turn_admission_reserved = True
        # Optional Slack thread for a NEW session (slack.auto_link_sessions), made
        # here rather than in the turn: the runner's echo below reads the link at
        # turn start, so the link has to exist before dispatch or the first message
        # never reaches the thread. Awaited, not fire-and-forget, for the same
        # reason; bounded inside. A link that lands after the hold still links,
        # and the thread receives the turns that start after it: this turn is
        # not replayed into it. Only a person's own send qualifies: an app-token
        # send is an injection into the slot, not the person opening it, and the
        # eligibility test rules out every non-dashboard origin besides.
        if _reserve_turn_admission:
            # Counts Stop presses; read before the await so one that lands inside it
            # is seen even though it found no task to cancel and settled at once.
            _stop_gen_before_hold = slot._stop_generation
            await maybe_auto_link_slack(state, slot)
            # The await above is the one suspension between accepting the row and
            # dispatching the turn; a close that lands inside it must not have its
            # turn run on the detached slot.
            if not slot_is_live(state, slot):
                return web.json_response(
                    {"error": "session closed", "code": "slot_closed"}, status=409
                )
            # A Stop pressed during the hold was aimed at this turn: it does not
            # start, and the row stays as a message that was stopped unanswered.
            # A send queued behind it during the hold starts now, as it would at
            # the end of a stopped turn.
            if slot._stop_generation != _stop_gen_before_hold:
                # Release capacity before the drain tries to admit its next turn.
                await turn_admission.abandon()
                # The reservation stays up across the drain's awaits (the
                # `finally` below drops it), so a send arriving meanwhile queues
                # instead of dispatching a second turn beside the drained one.
                if slot._queue:
                    await _start_next_queued_turn(state, slot)
                return web.json_response({"ok": True, "slot": slot.key, "stopped": True})

        # Edition message observer (CPP seam). Fire-and-forget, fail-safe: a
        # companion uses this to auto-ingest doc links pasted into chat. The public
        # Default is a no-op. Guarded so an observer error never blocks the turn;
        # deferred context read via the sel.py pattern (no platform import at load).
        responsive_chat.observe_user_message(request, message)

        # An unattended app-owned turn runs under the background concurrency
        # cap; run_background_turn passes an attended slot straight through, so the
        # interactive path is unchanged (no semaphore is even created).
        # The attachment ids this handler just accepted, so the ledger names the file
        # instead of leaving the turn's input unexplained. Passed only when there ARE
        # some: an ordinary send then calls `_run_chat` with exactly the arguments it
        # always did, which is what keeps the many test doubles of it valid.
        _accepted_attachment_meta = attachment_meta(user_meta)
        _accepted_attachments = [
            path for paths in _accepted_attachment_meta.values() for path in paths
        ]
        # ``request_app`` is stamped by the app-token auth middleware, not read from
        # the request body, so it is a fact about the caller a person cannot write --
        # which is what lets the turn's actor come from it. Passing it is what keeps
        # an app-authored send out of the ledger's ``user`` bucket: the actor
        # resolver's fallback is ``user``, so a site that observes an app and stays
        # silent records a person who never typed anything.
        _turn_kwargs: dict = {"_directive_user_origin": not bool(request_app)}
        if request_app:
            _turn_kwargs["_turn_actor"] = "app"
        elif cron_creator:
            # A script cron's `ctx.send_to_session`, attested by the transport:
            # the cron's turn, not a person's, so it is neither filed under
            # `user` nor quoted into a linked thread as if someone typed it.
            _turn_kwargs["_turn_actor"] = "cron"
        if _accepted_attachments:
            _turn_kwargs["_attachments"] = _accepted_attachments
            # Typed form for the refusal replay: keeps ``dirs`` entries as folders.
            _turn_kwargs["_attachment_meta"] = _accepted_attachment_meta
        # The turn task inherits its sender as initiator, so a turn running
        # outside admitted work is still found and stopped when the owner
        # removes that person (``access_set.stop_work``).
        from kiro_crew.shared_crew.admission_gate import actor_budget as _turn_actor_budget
        from kiro_crew.shared_crew.turn_gate import initiated_by as _turn_initiated_by

        with _turn_initiated_by("" if request_app else _turn_actor_budget(request)):
            task = spawn_guarded_turn(
                state,
                slot,
                admission_entry.admitted_or_direct(
                    slot.key,
                    turn_admission,
                    state.run_background_turn(
                        slot, _run_chat(state, slot, message, **_turn_kwargs)
                    ),
                    still_live=lambda: slot_is_live(state, slot),
                    on_ended=state.push_slots_update,
                ),
            )
        turn_dispatched = True
        slot.task = task
    finally:
        try:
            if not turn_dispatched:
                slot._has_reader = False
                await turn_admission.abandon()
        finally:
            if _reserve_turn_admission:
                slot._turn_admission_reserved = False
    state.push_slots_update()

    if ws_mode:
        # Carry the server-minted user-row `mid` back (see the append above). A
        # confirmed dashboard send reconciles it onto the optimistic bubble so
        # the message-pin control lights up immediately instead of only after
        # the chat_done refresh. Omitted when absent so the receipt shape is
        # unchanged for callers that never minted one.
        _receipt: dict[str, Any] = {"ok": True, "slot": slot.key}
        if _user_mid:
            _receipt["mid"] = _user_mid
        _admission_receipt = admission_entry.admission_receipt(turn_admission)
        if _admission_receipt is not None:
            _receipt["admission"] = _admission_receipt
            if turn_admission.queued:
                return web.json_response(_receipt, status=202)
        return web.json_response(_receipt)

    resp = web.StreamResponse()
    resp.headers.update(admission_entry.admission_headers(turn_admission))
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    # Declare this reader as the owner of `slot._pending` for as long as it is
    # draining, from before the first await after dispatch. A turn-end chunk
    # release must not run while an SSE reader still has undelivered tokens
    # queued, and `_has_reader` alone cannot carry that: the `done` branch below
    # clears it before this scope ends, and a turn-scoped reader never sets it.
    with slot.pending_consumer():
        try:
            await resp.prepare(request)
        except BaseException:
            # Release the broadcast this reader suppressed; the dispatched turn
            # keeps running, exactly as it does when the reader disconnects
            # mid-stream.
            slot._has_reader = False
            raise
        try:
            while True:
                pending = slot.drain()
                for msg in pending:
                    # Fail closed even if a dispatch path misses turn_end: only
                    # this request's gateway-minted user row belongs to its SSE.
                    foreign_user_row = msg.get("role") == "user" and (
                        not _user_mid or row_mid(msg) != _user_mid
                    )
                    if msg["cls"] == "done" or (
                        turn_scoped_stream and (msg["cls"] == TURN_END_WIRE_CLS or foreign_user_row)
                    ):
                        await write_sse_or_abort(request, resp, b"data: [DONE]\n\n")
                        slot._has_reader = False
                        return resp
                    if msg["cls"] == TURN_END_WIRE_CLS:
                        # A boundary for turn-scoped readers, not a row.
                        continue
                    if turn_scoped_stream and msg.get("role") == "queued":
                        # A placeholder for a later turn (a cron notification or
                        # an MCP-App message queued meanwhile), never this one.
                        continue
                    chunk = _build_stream_chunk(msg)
                    await write_sse_or_abort(request, resp, f"data: {chunk}\n\n".encode())
                try:
                    await asyncio.wait_for(slot.event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    await write_sse_or_abort(request, resp, b": keepalive\n\n")
        except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            slot.drain()
            slot._has_reader = False
    return resp


async def write_sse_or_abort(request: web.Request, resp: web.StreamResponse, data: bytes) -> None:
    """Write one SSE frame; abort the connection when the client stops reading.

    ``resp.write`` has no deadline of its own. A client that stops reading without
    closing its socket would park the write, and with it the stream loop and the
    slot state that loop owns (the pending queue and the broadcast hold), for as
    long as the socket stays open. Past the deadline the transport is aborted and
    the write raises ``ConnectionResetError``, so the caller's existing disconnect
    handling releases that state.
    """
    try:
        await asyncio.wait_for(resp.write(data), timeout=_SSE_WRITE_DEADLINE_SECS)
    except asyncio.TimeoutError:
        transport = request.transport
        if transport is not None:
            transport.abort()
        logger.info(
            "SSE client stopped reading for %.0fs; connection aborted", _SSE_WRITE_DEADLINE_SECS
        )
        raise ConnectionResetError("SSE client stopped reading") from None
