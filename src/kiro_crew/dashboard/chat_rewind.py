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
- Truncates ``slot.messages[:at_message_index]``, kills + removes the
  current ACP session for the slot, deletes the orphaned kiro-cli session
  file (best-effort), persists the truncated history, and re-runs from
  the edited prompt against a fresh ACP session.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
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
        return web.json_response({"error": "not found"}, status=404)

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
            return web.json_response({"error": "not found"}, status=404)

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
                        chained = state.conversation_log.read_messages_chained(
                            slot_history_key(slot)
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
        orphan_kiro_session_id = ""
        if state.sessions is not None:
            try:
                orphan_kiro_session_id = state.sessions._session_map.get(session_key) or ""
            except Exception:
                logger.debug("rewind: failed to read session_map", exc_info=True)

        # Truncate slot messages to the snapshot — everything from the
        # edited message onward is discarded (intentional, by design).
        del slot.messages[index:]
        slot.invalidate_source_links()
        slot._dirty = True
        slot._resumed_count = 0
        # Window was truncated → next save MUST be the archive-safe rewrite path.
        # If the inline save below fails, the flag keeps the flush loop on the
        # rewrite path so the dropped tail is still archived.
        slot._pending_rewrite = True

        # Swap the ACP session: kill current process AND delete the
        # session_map entry so the next get_or_create cold-starts a fresh
        # kiro-cli session. ``remove`` (vs ``reset``) is the right call —
        # we want a clean slate, not a session/load resume.
        if state.sessions is not None:
            try:
                await state.sessions.remove(session_key)
            except Exception:
                logger.warning(
                    "rewind: failed to remove ACP session for %s",
                    session_key,
                    exc_info=True,
                )

        # Best-effort cleanup of the orphaned kiro-cli session JSONL so it
        # does not show up in ``kiro-cli chat -l`` or the resume picker.
        if orphan_kiro_session_id:
            await _delete_orphan_kiro_session(orphan_kiro_session_id)

        # Append the edited prompt to slot history so the truncated +
        # edited state is what gets persisted and what the freshly-spawned
        # ACP session sees on its first prompt.
        redacted_content, _ = redact_exfiltration_urls(content)
        redacted_content, _ = redact_credentials(redacted_content)
        slot.append("user", redacted_content, "msg msg-u")

        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("rewind: failed to persist truncated history", exc_info=True)

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

        task = asyncio.create_task(_run_chat(state, slot, redacted_content))
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("rewind _run_chat failed for %s", slot.key, exc_info=t.exception())

        task.add_done_callback(_on_done)

    state.push_slots_update()
    return web.json_response({"ok": True, "at_message_index": index + disk_older})
