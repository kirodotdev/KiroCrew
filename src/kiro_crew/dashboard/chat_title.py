"""Title generation — auto-title, rename, plan rephrase."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.context_management import extract_plan_metadata, rephrase_plan
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import NEW_SESSION_TITLE, DashboardState, _ChatSlot
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

# Max turns to attempt auto-titling before giving up
_TITLE_MAX_ATTEMPTS = 5

# Only a small amount of user text can influence a 200-character title prompt.
# Allow enough bounded source for every dashboard attachment to precede it, then
# cap the retained text separately after generated references are removed.
_TITLE_TEXT_LIMIT = 16_384
_TITLE_MAX_ATTACHMENT_FILES = 20
_TITLE_MAX_ATTACHMENT_PATH_LENGTH = 4_096
_TITLE_SOURCE_SCAN_LIMIT = _TITLE_TEXT_LIMIT + _TITLE_MAX_ATTACHMENT_FILES * (
    _TITLE_MAX_ATTACHMENT_PATH_LENGTH + 32
)

# Titling is a trivial 3-6 word task, so run it on the cheapest/fastest model
# (Haiku) rather than the kirocrew-lite default (Opus 4.6 on the kiro-cli path).
# Applied per-session via set_model so heavier background work (compaction,
# optimizer) keeps the lite agent's default model. Best-effort: a failed
# override just falls back to the session's default model.
_TITLE_MODEL = "claude-haiku-4.5"

# Per-word delay for the word-by-word title reveal animation. LLM chunk
# streaming arrives in a sub-second burst (too fast to perceive), so the reveal
# is paced deterministically instead.
_TITLE_REVEAL_STEP_SECS = 0.09

_TITLE_PROMPT_TEMPLATE = (
    "You are a session naming agent. Name ONLY the conversation delimited below; "
    "ignore any earlier conversation, prior task, or context from this session's "
    "history — it is unrelated.\n\n"
    "If the delimited topic is clear: reply with ONLY a short title (3-6 words). "
    "No quotes, no punctuation.\n"
    "If NO (too vague, just greetings, or unclear topic): reply with exactly SKIP\n\n"
    "===== CONVERSATION TO NAME =====\n"
    "{transcript}\n"
    "===== END CONVERSATION ====="
)


def _strip_markdown_images(content: str, *, drop_trailing_partial: bool = False) -> str:
    """Remove dashboard-generated image blocks in one forward pass.

    Dashboard image references use the fixed ``![image](path)`` form on their
    own lines. Requiring that shape preserves escaped and code-quoted Markdown
    written by the user while balanced-parenthesis tracking handles filenames
    such as ``screenshot(1).jpg`` without regex backtracking.
    """
    prefix = "![image]("
    chunks: list[str] = []
    cursor = 0
    while True:
        image_start = content.find(prefix, cursor)
        if image_start < 0:
            chunks.append(content[cursor:])
            break

        if image_start > 0 and content[image_start - 1] != "\n":
            chunks.append(content[cursor : image_start + 1])
            cursor = image_start + 1
            continue

        index = image_start + len(prefix)
        depth = 1
        while index < len(content) and depth and content[index] not in "\r\n":
            char = content[index]
            if char == "\\" and index + 1 < len(content):
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1

        if depth or (index < len(content) and content[index] not in "\r\n"):
            if drop_trailing_partial and index == len(content):
                chunks.append(content[cursor:image_start])
                break
            chunks.append(content[cursor : image_start + 1])
            cursor = image_start + 1
            continue

        chunks.append(content[cursor:image_start])
        chunks.append(" ")
        cursor = index

    return "".join(chunks)


def _strip_attached_file_tokens(
    content: str,
    attached_files: tuple[str, ...] = (),
    *,
    drop_trailing_partial: bool = False,
) -> str:
    """Remove dashboard-generated ``[attached_file N] path`` references.

    Current dashboard messages store paths in token-index order, making each
    lookup constant-time. The whitespace-delimited fallback preserves support
    for older messages without metadata.
    """
    prefix = "[attached_file "
    chunks: list[str] = []
    cursor = 0
    while True:
        token_start = content.find(prefix, cursor)
        if token_start < 0:
            chunks.append(content[cursor:])
            break

        if token_start > 0 and not content[token_start - 1].isspace():
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        index = token_start + len(prefix)
        digits_start = index
        while index < len(content) and content[index].isdigit():
            index += 1
        digit_count = index - digits_start
        if not 1 <= digit_count <= 2 or not content.startswith("] ", index):
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        token_index = int(content[digits_start:index])
        path_start = index + 2
        expected_path = (
            attached_files[token_index - 1] if 1 <= token_index <= len(attached_files) else ""
        )
        path_end = path_start
        if expected_path and content.startswith(expected_path, path_start):
            candidate_end = path_start + len(expected_path)
            if candidate_end == len(content) or content[candidate_end].isspace():
                path_end = candidate_end
        elif (
            drop_trailing_partial
            and expected_path
            and expected_path.startswith(content[path_start:])
        ):
            path_end = len(content)

        if path_end == path_start:
            while path_end < len(content) and not content[path_end].isspace():
                path_end += 1
        if path_end == path_start:
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        chunks.append(content[cursor:token_start])
        chunks.append(" ")
        cursor = path_end

    return "".join(chunks)


def _message_attachment_paths(message: dict[str, Any]) -> tuple[str, ...]:
    """Return bounded, index-preserving paths from dashboard message metadata."""
    meta = message.get("meta")
    if not isinstance(meta, dict):
        return ()
    files = meta.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(
        path if isinstance(path, str) and 0 < len(path) <= _TITLE_MAX_ATTACHMENT_PATH_LENGTH else ""
        for path in files[:_TITLE_MAX_ATTACHMENT_FILES]
    )


def _title_text(content: str, attached_files: tuple[str, ...] = ()) -> str:
    """Return bounded message text suitable for title generation.

    A bounded allowance large enough for every accepted attachment is sanitized
    first, so generated paths cannot crowd later user text out of the retained
    title input. The normalized user text is capped separately.
    """
    source_was_truncated = len(content) > _TITLE_SOURCE_SCAN_LIMIT
    content = content[:_TITLE_SOURCE_SCAN_LIMIT]
    if content.startswith("[BROWSE] "):
        content = content[len("[BROWSE] ") :]
    content = _strip_markdown_images(content, drop_trailing_partial=source_was_truncated)
    content = _strip_attached_file_tokens(
        content,
        attached_files,
        drop_trailing_partial=source_was_truncated,
    )
    return " ".join(content.split())[:_TITLE_TEXT_LIMIT]


def _build_title_prompt(messages: list[dict[str, Any]]) -> str | None:
    """Build a title generation prompt from conversation messages."""
    lines: list[str] = []
    for m in messages[:10]:
        role = m.get("role", "")
        content = _title_text(m.get("content", ""), _message_attachment_paths(m))
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content[:200]}")
    if not lines:
        return None
    return _TITLE_PROMPT_TEMPLATE.format(transcript="\n".join(lines))


def _reset_auto_run_for_new_plan(slot: "_ChatSlot") -> None:
    """Clear auto-run state so a new plan requires fresh user approval."""
    session_dir = config_dir() / "sessions" / slot.key
    if session_dir.exists():
        for f in session_dir.glob("stage_*_result.md"):
            try:
                f.unlink()
            except OSError:
                pass
    slot._orch_tracker = None
    slot._auto_run = False


def _extract_and_redact_plan_metadata(text: str) -> tuple[list[str], str, list[list[str]]]:
    """Extract stage titles, goal, and descriptions from plan text, redacted."""
    titles, goal, descriptions = extract_plan_metadata(text)
    titles = [redact_credentials(redact_exfiltration_urls(t)[0])[0] for t in titles]
    if goal:
        goal = redact_credentials(redact_exfiltration_urls(goal)[0])[0]
    descriptions = [
        [redact_credentials(redact_exfiltration_urls(d)[0])[0] for d in stage_descs]
        for stage_descs in descriptions
    ]
    return titles, goal, descriptions


async def _rephrase_plan_lite(
    state: DashboardState,
    text: str,
    issues: list[str],
    *,
    might_not_be_plan: bool = False,
) -> str | None:
    """Rephrase a plan using the cheap background session (kirocrew-lite)."""

    try:
        bg, _new, _resumed = await state.sessions.get_or_create(BACKGROUND_KEY)
    except Exception:
        logger.warning("Failed to get background session for plan rephrase", exc_info=True)
        return None
    try:
        result = await rephrase_plan(text, issues, bg, might_not_be_plan=might_not_be_plan)
    finally:
        state.sessions.release(BACKGROUND_KEY)
        # Recycle the shared BG session if it's accumulated too much context.
        # Without this, repeated dashboard plan-rephrases bloat the kiro-cli
        # child until a mid-stream recycle eventually kills an in-flight call,
        # blocking every chat queued behind the BG session for minutes.
        await state.sessions.recycle_background()
    if result:
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
    return result


def _clean_title(s: str) -> str:
    """Normalize a (partial or final) LLM title: trim whitespace and wrapping
    quotes/period."""
    return s.strip().strip('"').strip("'").strip(".")


async def _reveal_title(state: DashboardState, slot: _ChatSlot, title: str) -> None:
    """Animate a title in word-by-word so it visibly types out in the sidebar.

    Raw LLM chunk streaming arrives in a sub-second burst (too fast to see), so
    this paces a deterministic reveal instead. Pushes lightweight ``slot_title``
    events (``full=False``); the caller does the final full push. Nothing here
    is persisted — the caller persists the complete title once.
    """
    words = title.split()
    if len(words) <= 1:
        return
    acc: list[str] = []
    for w in words[:-1]:  # last word arrives with the caller's final push
        acc.append(w)
        slot.title = " ".join(acc)
        state.push_slot_title(slot.key, slot.title, full=False)
        await asyncio.sleep(_TITLE_REVEAL_STEP_SECS)


async def _generate_title_via_kiro(
    state: DashboardState,
    messages: list[dict[str, Any]],
) -> str:
    """Generate a title using the shared background kiro-cli session."""

    prompt = _build_title_prompt(messages)
    if not prompt:
        logger.debug("Title generation skipped — no usable messages")
        return ""

    logger.debug("Title generation prompt (%d chars)", len(prompt))
    session = await state.sessions.get_bg_session()
    text = ""
    try:
        # Run titling on a fast/cheap model. Best-effort: if the backend can't
        # switch (older kiro-cli, non-kiro provider), fall through on the
        # session's default model.
        _set_model = getattr(session, "set_model", None)
        if _set_model is not None:
            try:
                await _set_model(_TITLE_MODEL)
            except Exception:
                logger.debug("Title model override to %s failed; using default", _TITLE_MODEL)
        async for event in session.prompt(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await session.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break
    finally:
        await session.destroy()
    title = _clean_title(text)
    if not title or title.upper() == "SKIP":
        logger.info("Title generation returned SKIP/empty — topic not clear yet")
        return ""
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    logger.info("Title generated: %r", title[:80])
    return title[:80]


async def _persist_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Save the slot title to the conversation history file.

    ``set_title`` -> ``update_metadata`` enters ``_locked`` (cross-process flock
    acquire + ``os.close``). Those are blocking-on-loop-prohibited, so the write
    is dispatched to a worker thread rather than run on the event-loop thread
    where a wedged peer could freeze chat/WS/heartbeat.
    """

    if state.conversation_log:
        history_key = _history_key_for(slot.key)
        try:
            await asyncio.to_thread(
                state.conversation_log.set_title, history_key, slot.title
            )
            logger.debug("Persisted title %r for slot %s", slot.title, slot.key)
        except Exception:
            logger.debug("Failed to persist title for slot %s", slot.key)


def _fallback_title_from_messages(messages: list[dict[str, Any]]) -> str:
    """Fallback title used only when the LLM can't title the chat: the first
    user message, cleaned and truncated to ~60 chars with an ellipsis.

    Trims back to a word boundary so the cut isn't mid-word. Short messages are
    returned whole (no ellipsis). Returns ``NEW_SESSION_TITLE`` if there's no
    usable user text, so the caller always has something to show.
    """
    first = next(
        (
            text
            for m in messages
            if m.get("role") == "user"
            and (text := _title_text(m.get("content", ""), _message_attachment_paths(m)))
        ),
        "",
    )
    first, _ = redact_exfiltration_urls(first)
    first, _ = redact_credentials(first)
    first = " ".join(first.split())
    if not first:
        return NEW_SESSION_TITLE
    if len(first) <= 60:
        return first
    cut = first[:60].rstrip()
    # Trim a dangling partial word so the ellipsis reads cleanly.
    if " " in cut:
        cut = cut[: cut.rindex(" ")].rstrip()
    return f"{cut}…"


async def _maybe_auto_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Background task: attempt to LLM-title a slot.

    Fired on the first message send (so the title lands during the first turn,
    from just the user's message) and again after a response completes as a
    retry. Idempotent: no-ops once titled and guards against concurrent
    attempts via ``slot._title_in_flight``. Untitled slots display as
    "New Session…" via ``_ChatSlot.display_title`` until this lands. If the LLM
    returns SKIP/empty after the assistant has responded (a definitive
    failure), the title falls back to the truncated first message with an
    ellipsis (see ``_fallback_title_from_messages``).

    Runs for EVERY ``memory_mode``, temporary included. Titling reads only the
    slot's own messages and prompts the shared ``_bg`` session, so it neither
    reads stored memory nor writes any — the two things a temporary session
    actually forbids. It used to bail on ``slot.blocks_reads``, which left
    temporary tabs stuck on "New Session…" forever; that guard was an
    over-broad proxy for "ephemeral" (the manual
    ``api_chat_slot_generate_title`` endpoint never had it). The title is
    persisted the same way for every mode because ``_save_slot_to_history``
    already writes ``meta_line["title"]`` for temporary slots regardless of
    this path — those sessions keep a transcript on disk for tab recovery.
    """
    if slot._titled:
        return
    if slot._title_in_flight:
        # Preserve the end-of-turn retry if the on-send attempt is still
        # running. The active attempt will consume it after releasing the guard.
        if any(m.get("role") == "assistant" and m.get("content") for m in slot.messages):
            slot._title_retry_pending = True
        return
    user_count = sum(1 for m in slot.messages if m.get("role") == "user")
    if user_count < 1 or user_count > _TITLE_MAX_ATTEMPTS:
        if user_count > _TITLE_MAX_ATTEMPTS and not slot._titled:
            # Gave up after repeated attempts — fall back to the truncated
            # first message with an ellipsis.
            slot.title = _fallback_title_from_messages(slot.messages)
            slot._titled = True
            await _persist_title(state, slot)
            state.push_slot_title(slot.key, slot.title)
        return
    slot._title_in_flight = True
    messages = list(slot.messages)
    attempt_has_assistant = any(m.get("role") == "assistant" and m.get("content") for m in messages)
    logger.info("Auto-title: attempting for slot %s (turn %d)", slot.key, user_count)

    cancelled = False
    try:
        title = await _generate_title_via_kiro(state, messages)
        logger.info("Auto-title: kiro returned %r for slot %s", title, slot.key)
        if title:
            # Animate the title in word-by-word, then finalize with the
            # complete title (full push + persist).
            await _reveal_title(state, slot, title)
            slot.title = title
            slot._titled = True
            await _persist_title(state, slot)
            state.push_slot_title(slot.key, title)
        else:
            # LLM returned SKIP/empty. Show the truncated fallback name right
            # away rather than leaving "New Session…" until the full turn ends
            # — otherwise the name lags the whole response for messages the LLM
            # won't title from the user text alone. Lock it (_titled=True) only
            # once the assistant has responded and the LLM still SKIP'd (a
            # definitive failure); on the on-send attempt leave it unlocked so
            # the end-of-turn retry can still upgrade the truncation to a real
            # LLM title.
            slot.title = _fallback_title_from_messages(slot.messages)
            slot._titled = attempt_has_assistant
            await _persist_title(state, slot)
            state.push_slot_title(slot.key, slot.title)
            logger.info(
                "Auto-title: fell back to truncated message for slot %s (locked=%s)",
                slot.key,
                attempt_has_assistant,
            )
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        logger.warning("Auto-title failed for slot %s", slot.key, exc_info=True)
    finally:
        slot._title_in_flight = False
        retry_pending = slot._title_retry_pending
        slot._title_retry_pending = False
        if retry_pending and not slot._titled and not cancelled:
            await _maybe_auto_title(state, slot)


async def api_chat_slot_generate_title(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/generate-title — manually trigger title generation."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    logger.info("Manual title generation requested for slot %s", name)
    fallback_is_placeholder = False
    try:
        title = await _generate_title_via_kiro(state, slot.messages)
    except Exception:
        logger.debug("Title generation failed for slot %s", name, exc_info=True)
        title = _fallback_title_from_messages(slot.messages)
        fallback_is_placeholder = title == NEW_SESSION_TITLE

    if title and not fallback_is_placeholder:
        slot.title = title
        slot._titled = True
        await _persist_title(state, slot)
        state.push_slot_title(slot.key, title)

    return web.json_response({"ok": True, "title": "" if fallback_is_placeholder else title})


async def api_chat_slot_rename(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/title — rename a chat session."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON"}, status=400)
    title = body.get("title", "").strip()[:200]
    if not title:
        return web.json_response({"error": "title required"}, status=400)
    slot.title = title
    slot._titled = True
    await _persist_title(state, slot)
    state.push_slot_title(slot.key, title)
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_rename",
        outcome="allowed",
        source="dashboard",
        resources=slot.key,
    )
    return web.json_response({"ok": True, "title": title})
