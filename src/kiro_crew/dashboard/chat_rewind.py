"""Rewind — edit a past user message and re-run from that point in place.

Unlike ``edit_resend`` (which truncates ``slot.messages`` in memory but
leaves the backing kiro-cli session file with stale forward turns),
``rewind`` swaps the underlying ACP session for a fresh one. This mirrors
kiro-cli's native ``/rewind`` slash command, which "rewinds the conversation
to a previous turn, forks into a new session" — except the *user-visible*
slot identity (key, title, folder, sidebar position) is preserved. The
orphaned kiro-cli session file is deleted so it does not pollute
``kiro-cli chat -l`` / the resume picker.

Contract:
- ``POST /api/chat/slots/{slot}/rewind``
- Body: ``{at_message_index: int, content: str}`` (or ``{ts, content}``)
- Prepares and persists the truncated history before replacing the live slot,
  discards the current ACP conversation, deletes the orphaned kiro-cli session
  file (best-effort), and re-runs from the edited prompt against a fresh ACP
  session.
"""

from __future__ import annotations

import asyncio
import copy
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import _run_chat, _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.remote_relay import remote_bound_refusal
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_map import _kiro_sessions_dir

logger = logging.getLogger(__name__)


async def _delete_orphan_kiro_session(session_id: str) -> None:
    """Delete the orphaned kiro-cli session JSONL file, best-effort.

    The file lives at ``~/.kiro/sessions/cli/<session_id>.json`` (or
    ``.jsonl`` depending on kiro-cli version). Failures are logged at
    debug only — kiro-cli's own GC will eventually reclaim it.
    """
    if not session_id:
        return
    for suffix in (".json", ".jsonl"):
        candidate = _kiro_sessions_dir() / f"{session_id}{suffix}"
        try:
            await asyncio.to_thread(candidate.unlink, missing_ok=True)
        except OSError as exc:
            logger.debug("rewind: could not delete %s: %s", candidate, exc)


async def api_chat_slot_rewind(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/rewind — edit a past message and re-run in place.

    Body: ``{at_message_index?: int, ts?: str, content: str}``

    Effect: replaces the slot's ACP session with a fresh one primed only with
    messages up to (but not including) ``at_message_index``, then runs the
    edited prompt against it. Slot key, title, folder, sidebar position, and
    color are unchanged.
    """
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check — mirror fork's contract so apps can't rewind
    # slots they don't own.
    if request_app:
        if not slot._app or slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.slot_rewind",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot rewind unscoped or unowned slot",
            )
            # 404 (not 403): indistinguishable from a missing slot —
            # anti-enumeration (CWE-204); true reason logged via SEL above.
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # A crew-bound slot has no local rewind: it would rebuild the LOCAL ACP
    # session and re-run the edited turn on this machine, diverging from the peer.
    # AFTER the app-ownership 404 above so a foreign app cannot tell a remote slot
    # apart from a missing one via the 409 (GPT #7693).
    refusal = remote_bound_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    raw_index = body.get("at_message_index", body.get("index"))
    ts = body.get("ts")
    raw_content = body.get("content")
    if not isinstance(raw_content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    content = raw_content.strip()
    if not content:
        return web.json_response({"error": "content is required"}, status=400)
    if len(content) > 32_768:
        return web.json_response({"error": "content too long (max 32768 chars)"}, status=400)

    async with slot._lock:
        if slot.running:
            return web.json_response({"error": "slot is running"}, status=409)

        msgs = slot.messages

        # The frontend builds its index against read_messages_chained() — see
        # chat_fork.py and chat_handlers.api_chat_slot_detail. slot.messages
        # holds at most the last 500 messages of that chained view; older
        # messages live in archived sibling session files and are summarised
        # by slot._disk_older_count. Validate inputs against the chained
        # length so error messages match what the user sees, and translate
        # back to a slot.messages-relative index for the truncation below.
        # Deliberately the all-rows counter: the frontend's index space is the
        # chained DISK read plus the raw window, so on-disk-line units are the
        # ones that line up (the durable-only counter measures a different,
        # role-filtered space).
        disk_older = getattr(slot, "_disk_older_count", 0)
        chained_len = disk_older + len(msgs)

        # Resolve the index. ``ts`` takes precedence over ``at_message_index``
        # so frontends can route around message-list reordering.
        if ts:
            if not isinstance(ts, str):
                return web.json_response({"error": "ts must be a string"}, status=400)
            index = next(
                (i for i, m in enumerate(msgs) if m.get("ts") == ts and m.get("role") == "user"),
                -1,
            )
            if index < 0:
                # Fall back to scanning the archived chained portion so we
                # can return a clear refusal instead of "user message not
                # found for ts" — the message exists but is out of reach.
                if disk_older > 0 and state.conversation_log is not None:
                    try:
                        chained = await asyncio.to_thread(
                            state.conversation_log.read_messages_chained,
                            slot_history_key(slot),
                        )
                    except Exception:
                        logger.debug("rewind: chained scan for ts failed", exc_info=True)
                        chained = []
                    if any(
                        m.get("ts") == ts and m.get("role") == "user" for m in chained[:disk_older]
                    ):
                        return web.json_response(
                            {
                                "error": "cannot rewind into archived history; "
                                "reload the slot or use fork instead"
                            },
                            status=400,
                        )
                return web.json_response({"error": "user message not found for ts"}, status=400)
        elif isinstance(raw_index, bool) or not isinstance(raw_index, int):
            return web.json_response(
                {"error": "at_message_index must be a non-negative integer"},
                status=400,
            )
        elif raw_index < 0 or raw_index >= chained_len:
            return web.json_response(
                {
                    "error": f"at_message_index {raw_index} out of range "
                    f"(have {chained_len} messages)"
                },
                status=400,
            )
        elif raw_index < disk_older:
            return web.json_response(
                {
                    "error": f"cannot rewind to index {raw_index}: in archived history "
                    f"(older than offset {disk_older}); "
                    f"reload the slot or use fork instead"
                },
                status=400,
            )
        elif msgs[raw_index - disk_older].get("role") != "user":
            return web.json_response({"error": "index is not a user message"}, status=400)
        else:
            index = raw_index - disk_older

        # Capture orphan info before any mutation, so we can clean up the
        # kiro-cli session file even if the swap path errors out partway.
        session_key = effective_session_key(slot)

        # An app may rewind only the slot's OWN dashboard session. An
        # app-owned slot can carry a channel link (``linked_session_key``),
        # and ``session_key`` then addresses a foreign channel conversation
        # -- rewinding through it would clear the native identity of a
        # session the app does not own. Same 404-not-403 shape as the
        # ownership check above (anti-enumeration); SEL records the truth.
        if request_app and getattr(slot, "linked_session_key", ""):
            sel().log_api_access(
                caller=request_app,
                operation="chat.slot_rewind",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot rewind a channel-linked slot",
            )
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

        # The transcript this rewind was authorized against. A concurrent
        # rebinding (a cron injection re-linking the slot) moves the slot to
        # another transcript while the boundaries below are pending; the
        # commit re-checks this key so the edit can never land on state it
        # never read.
        expected_history_key = slot_history_key(slot)
        orphan_kiro_session_id = ""
        if state.sessions is not None:
            try:
                orphan_kiro_session_id = state.sessions._session_map.get(session_key) or ""
            except Exception:
                logger.debug("rewind: failed to read session_map", exc_info=True)

        # Prepare the prospective state on a copy. The dirty-slot flush can run
        # while either durable boundary below is pending, so exposing a truncated
        # live window here could make a rejected edit permanent.
        prospective_slot = copy.copy(slot)
        prospective_slot.messages = list(slot.messages[:index])
        prospective_slot._queue = []
        prospective_slot._pending = list(slot._pending)
        prospective_slot._question_pending = dict(slot._question_pending)
        prospective_slot._on_question_retired = None
        prospective_slot.event = asyncio.Event()
        if prospective_slot._pending:
            prospective_slot.event.set()
        prospective_slot._dirty = True
        prospective_slot._resumed_count = 0
        prospective_slot._pending_rewrite = True

        # The backing queue belongs to the discarded suffix too. Entries that
        # arrive AFTER this snapshot (a send diverted to the queue by the
        # reservation below) are not part of it and must survive the commit.
        discarded_queue = list(slot._queue)
        discarded_queue_ids = {item["id"] for item in discarded_queue}

        # Build the user row through the slot's normal append path without
        # publishing it to the live slot before persistence succeeds.
        redacted_content, _ = redact_exfiltration_urls(content)
        redacted_content, _ = redact_credentials(redacted_content)
        prospective_slot.append("user", redacted_content, "msg msg-u")
        msgs_snapshot = list(prospective_slot.messages)
        retired_question_ids = [
            question_id
            for question_id in slot._question_pending
            if question_id not in prospective_slot._question_pending
        ]

        # Reserve the slot BEFORE the awaits below. ``slot.running`` derives
        # from ``slot.task``, and the send path is not serialized on
        # ``slot._lock``: without a live task, a send arriving while either
        # durable boundary is pending observes an idle slot, appends its row
        # to ``slot.messages`` and dispatches a competing turn -- which the
        # commit below would then erase. Publishing the dispatch task here
        # (no await between the idle check above and this assignment) makes
        # such a send take the queue path instead; the entry is not in
        # ``discarded_queue``, so the commit preserves it and the replacement
        # turn's own teardown drain delivers it. On abort the task starts the
        # next queued turn itself, so a diverted send is never stranded.
        dispatch_ready = asyncio.Event()
        dispatch_commit = False

        async def _rewind_dispatch() -> None:
            await dispatch_ready.wait()
            if dispatch_commit:
                await _run_chat(
                    state,
                    slot,
                    redacted_content,
                    _directive_user_origin=not bool(request_app),
                )
                return
            # Rewind rejected. A send diverted to the queue by this
            # reservation has no drain trigger of its own (no turn ran), so
            # hand it to the canonical successor dispatch, which re-validates
            # holds before starting anything. Entries that were already
            # queued before the rewind keep waiting for their own trigger.
            if any(entry["id"] not in discarded_queue_ids for entry in slot._queue):
                if await _start_next_queued_turn(state, slot):
                    return
            state.push_slots_update()

        task = asyncio.create_task(_rewind_dispatch())
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("rewind _run_chat failed for %s", slot.key, exc_info=t.exception())

        task.add_done_callback(_on_done)

        # Durably clear the native resume sid before committing the edited
        # history. A failure leaves the original branch intact and dispatches
        # no replacement turn.
        try:
            if state.sessions is not None:
                try:
                    # ``skip_if_busy``: an inbound channel turn (a Slack reply
                    # on the linked session) holds the session semaphore while
                    # ``slot.running`` reads False, so the idle check above
                    # cannot see it -- an unconditional discard would tear
                    # down its provider mid-reply. The refusal is atomic with
                    # the busy probe inside the lifecycle service.
                    discarded = await state.sessions.discard_conversation(
                        session_key, skip_if_busy=True
                    )
                except Exception:
                    logger.warning(
                        "rewind: failed to discard ACP conversation for %s",
                        session_key,
                        exc_info=True,
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "rewind_prepare_failed",
                        },
                        status=503,
                    )
                if not discarded:
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "the session is busy with another reply; retry the edit",
                            "code": "rewind_session_busy",
                        },
                        status=409,
                    )
                try:
                    # The sid clear lands in the session map's debounced
                    # writer; a gateway exit before that write would reload
                    # the old sid on restart and resurrect the discarded
                    # conversation. Force the durability point HERE,
                    # endpoint-side, so the shared discard keeps its existing
                    # semantics for its other callers (chat_runner, channel
                    # handlers), which tolerate the debounce.
                    await state.sessions.aflush()
                except Exception:
                    logger.warning(
                        "rewind: failed to flush the cleared resume sid for %s",
                        session_key,
                        exc_info=True,
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "rewind_prepare_failed",
                        },
                        status=503,
                    )

            def _commit_live_state() -> None:
                """Adopt the prepared state on the live slot (synchronous).

                Shared by the normal success path and the cancellation path
                below: once the destructive rewrite has landed on disk, this
                is the only thing that keeps live state matching it. No await
                inside, so it is atomic on the event loop.
                """
                slot.messages = prospective_slot.messages
                # Remove only the entries captured in the pre-await snapshot:
                # an entry queued while the boundaries were pending belongs to
                # the NEW timeline and must survive for the teardown drain.
                slot._queue[:] = [
                    entry for entry in slot._queue if entry["id"] not in discarded_queue_ids
                ]
                slot._pending = prospective_slot._pending
                slot._question_pending = prospective_slot._question_pending
                slot.invalidate_source_links()
                slot._dirty = True
                slot._resumed_count = 0
                # Deliberately NOT copied from ``prospective_slot``: the
                # persistence witnesses (``_pending_rewrite``, ``_disk_*``,
                # ``_frozen_prefix_cache``). The save above ran on the LIVE
                # slot and stamped them with the post-rewrite truth
                # (``_pending_rewrite`` cleared, disk window/meta/mtime cache
                # matching the truncated file); the prospective copies are the
                # PRE-save values. Restoring those would re-arm
                # ``_pending_rewrite`` -- making the next flush repeat the
                # destructive rewrite and discard any cross-process append
                # (workflow/cron) that landed in between -- and would move the
                # monotone ``_disk_tail_ts`` floor backwards.
                if slot._pending:
                    slot.event.set()
                else:
                    slot.event.clear()
                if retired_question_ids and callable(slot._on_question_retired):
                    try:
                        slot._on_question_retired(slot.key, retired_question_ids)  # type: ignore[operator]
                    except Exception:
                        logger.debug(
                            "rewind: question-retirement announcement failed for slot %s",
                            slot.key,
                            exc_info=True,
                        )
                # Other open clients render queue cards from WebSocket events
                # rather than this slot's optimistic edit.
                for item in discarded_queue:
                    state.broadcast_ws("queue_cancel", {"slot": slot.key, "queue_id": item["id"]})
                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.rewind",
                    outcome="allowed",
                    source="dashboard",
                    resources=(
                        f"slot={slot.key},at_index={index},"
                        f"orphan_kiro_session={orphan_kiro_session_id or 'none'}"
                    ),
                )

            # The worker thread cannot be interrupted: once the rewrite starts
            # it WILL finish, whether or not this handler is still alive. A
            # client disconnect cancels the handler task, and a bare await
            # here would then abandon a completed destructive rewrite --
            # persisted history rewound, live state stale, edited prompt never
            # dispatched. Shield the save; on cancellation, wait for the
            # worker's real outcome and complete the matching commit (and let
            # the reserved dispatch task run the edited prompt) before
            # propagating the cancellation.
            save_task = asyncio.ensure_future(
                asyncio.to_thread(
                    _save_slot_to_history,
                    state,
                    slot,
                    msgs_snapshot,
                    expected_history_key=expected_history_key,
                )
            )
            try:
                saved = await asyncio.shield(save_task)
            except asyncio.CancelledError:
                landed = False
                try:
                    landed = bool(await save_task)
                except Exception:
                    landed = False
                if landed and slot_history_key(slot) == expected_history_key:
                    _commit_live_state()
                    dispatch_commit = True
                    logger.info(
                        "rewind: request cancelled after the rewrite landed for %s; "
                        "committed live state and dispatching the edited prompt",
                        slot.key,
                    )
                raise
            except Exception:
                logger.warning("rewind: failed to persist truncated history", exc_info=True)
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "rewind_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The save's own guards refused the write (the session was
                # permanently deleted, or the slot was rebound to another
                # transcript, while the write awaited its lock). Nothing was
                # persisted, so reporting success here would dispatch a turn
                # from state that exists only in memory.
                logger.warning(
                    "rewind: history save refused for %s (concurrent delete or rebind)",
                    slot.key,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "rewind_save_failed",
                    },
                    status=503,
                )

            # Both irreversible boundaries succeeded. Before adopting the
            # prepared state, confirm the slot still routes to the transcript
            # this rewind was authorized against: a concurrent rebinding (a
            # cron injection re-linking the slot mid-persistence) hydrates the
            # slot with ANOTHER conversation's state, and a late commit here
            # would silently replace it. The prospective copy froze the old
            # routing, so the save-side ``expected_history_key`` guard cannot
            # see the live slot move -- this loop-side check is the one that
            # can. No await between this check and the mutations below.
            if slot_history_key(slot) != expected_history_key:
                logger.warning(
                    "rewind: slot %s was rebound to another transcript during "
                    "persistence; refusing the commit",
                    slot.key,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "the conversation changed while saving; retry the edit",
                        "code": "rewind_slot_rebound",
                    },
                    status=503,
                )

            # The prepared state is now the live slot state. Keep the queue
            # cancellation and source-link invalidation with this commit
            # rather than leaking them during either await above.
            _commit_live_state()
            # BEFORE the await below: a cancellation landing during the
            # best-effort cleanup would otherwise abort the reserved dispatch
            # after the commit already happened, stranding a persisted edited
            # prompt that never runs.
            dispatch_commit = True

            # Best-effort cleanup of the orphaned kiro-cli session JSONL so it
            # does not show up in ``kiro-cli chat -l`` or the resume picker.
            # After the commit (and skipped on the cancellation path): purely
            # cosmetic, kiro-cli's own GC reclaims the file eventually.
            if orphan_kiro_session_id:
                await _delete_orphan_kiro_session(orphan_kiro_session_id)
        finally:
            # Wake the reserved dispatch task on every exit: it runs the
            # replacement turn on commit and the queue handoff on abort.
            dispatch_ready.set()

    state.push_slots_update()
    return web.json_response({"ok": True, "at_message_index": index + disk_older})
