"""kiro-cli native sub-agent Activity cards and the native AskUserQuestion card a dashboard turn
shows."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        NATIVE_SUBAGENT_DONE_RESULT_CAP,
        NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
        ValidationError,
        _redact_tool_field,
        logger,
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
        validate_ask_user_question,
    )

# Native subagent cards carry a short error string only, so a long provider
# message is clipped. The request id is what identifies the failure
# server-side and the formatter appends it LAST, so a plain head-slice drops
# precisely the part worth keeping.
_MAX_NATIVE_CARD_ERROR = 200


_RE_TRAILING_REQUEST_ID = re.compile(r"\(request_id:\s*[0-9a-fA-F-]+\)\s*$")


def _clip_card_error(text: str, limit: int = _MAX_NATIVE_CARD_ERROR) -> str:
    """Clip *text* to *limit* characters, keeping any trailing request id."""
    if len(text) <= limit:
        return text
    match = _RE_TRAILING_REQUEST_ID.search(text)
    if not match:
        return text[:limit]
    suffix = match.group(0).strip()
    head = limit - len(suffix) - 4  # room for the elision marker and a space
    if head <= 0:
        return text[:limit]
    return f"{text[:head]}... {suffix}"


async def _post_native_question_card(state: Any, slot_key: str, tool_input: str) -> bool:
    """Post kiro-cli's native ``AskUserQuestion`` card through the server owner.

    The card goes through the SAME path the MCP ``ask_question`` directive uses
    (``DashboardState.post_question_card``): one minted ``card_id`` on the
    frame, one ``_question_pending`` record on the slot, one redaction pass. A
    live user row or a consumed steer then retires the record and broadcasts
    ``question_card_resolved`` exactly as for an MCP card, so the client keeps
    no lifecycle of its own for this kind.

    Schema validation stays here (``validate_ask_user_question`` is the
    tool-input contract); text redaction lives in the coordinator, which also
    rejects questions that collapse to the same text after redaction.

    Returns False -- and logs -- when the input is unusable. Nothing raised here
    may reach the turn: a malformed native card is a warning, not a turn error.
    """
    try:
        questions = validate_ask_user_question(json.loads(tool_input))
        await state.post_question_card(slot_key, questions, native=True)
    except (
        # ``json.JSONDecodeError`` and the coordinator's redaction-collision
        # refusal are both ValueError.
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ValidationError,
    ) as exc:
        logger.warning("AskUserQuestion validation failed: %s", exc)
        return False
    return True


# Native kiro-cli subagents (``use_subagent``) are surfaced in the Activity tab
# via the ``_kiro.dev/subagent/list_update`` notification (one card per
# sub-agent), handled by ``_native_subagent_sync`` below. The list_update gives
# authoritative per-sub-agent identity/status, so the ``subagent`` tool call's
# ``stages`` payload is not parsed.

_NATIVE_SUBAGENT_STALE_SECS = 120.0  # auto-close cards with no progress after 2 min


def _native_done_result(chunks: "list[str] | None") -> str:
    """Return the newest bounded native-card output with a truncation marker."""
    joined = "".join(chunks or [])
    if len(joined) <= NATIVE_SUBAGENT_DONE_RESULT_CAP:
        return joined
    return NATIVE_SUBAGENT_DONE_TRUNC_MARKER + joined[-NATIVE_SUBAGENT_DONE_RESULT_CAP:]


def _append_native_output(
    buf: list[str],
    text: str,
    total: int,
    cap: int = NATIVE_SUBAGENT_OUTPUT_TAIL,
    hard: int = NATIVE_SUBAGENT_OUTPUT_HARD,
) -> int:
    """Append output and collapse it to the newest tail past the hard ceiling."""
    buf.append(text)
    total += len(text)
    if total > hard:
        tail = "".join(buf)[-cap:]
        buf[:] = [tail]
        total = len(tail)
    return total


def _native_card_feed(card_output, card_id: str) -> str:
    """Build, bound, and redact native output at the broadcast boundary."""
    feed = _native_done_result((card_output or {}).get(card_id))
    if feed:
        feed, _ = redact_exfiltration_urls(feed)
        feed, _ = redact_credentials(feed)
    return feed


def _native_subagent_sync(state, slot, subagents, tracker, card_output=None) -> None:
    """Reconcile per-subagent Activity cards from a kiro-cli
    ``_kiro.dev/subagent/list_update`` notification.

    Native (``use_subagent``) crews run *inside* the parent kiro-cli session, so
    their internal tool calls are not attributable per sub-agent over standard
    ACP. But kiro-cli also emits this list (the same data its TUI shows) with one
    entry per sub-agent carrying ``sessionId``, ``sessionName``,
    ``role``/``agentName``, ``initialQuery`` and ``status``. We map each entry to
    its own Activity card (``native:<sessionId>``): spawned when first seen,
    completed when its status terminates. This yields one card per sub-agent
    (matching the spawn_run/spawn_sub_agents card model) with no agent-prompt
    changes.

    ``tracker`` is per-turn state: ``{session_id: {started, done, agent, task}}``.
    """
    if not isinstance(subagents, list):
        return
    _running = ("working", "running", "pending", "queued", "in_progress", "")
    now = time.time()
    # Track which sids are still reported by kiro-cli this update
    _seen_sids: set[str] = set()
    for sub in subagents:
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("sessionId") or "")
        if not sid:
            continue
        _seen_sids.add(sid)
        card_id = f"native:{_redact_tool_field(sid)}"
        _status_raw = sub.get("status")
        status = _status_raw if isinstance(_status_raw, dict) else {}
        stype = str(status.get("type") or "").lower()
        smsg = str(status.get("message") or "")
        if sid not in tracker:
            agent, _ = redact_exfiltration_urls(str(sub.get("role") or sub.get("agentName") or ""))
            agent, _ = redact_credentials(agent)
            task = redact_and_truncate(
                str(sub.get("initialQuery") or sub.get("sessionName") or ""), 2000
            )
            # Skip cards with empty task entirely — kiro-cli sometimes emits
            # list_update notifications where initialQuery/sessionName are both
            # empty. Showing an Activity card with no meaningful input is
            # confusing UX ("Starting..." with nothing to explain what it does).
            # Mark as done immediately so we don't re-process on next update.
            if not task.strip():
                tracker[sid] = {
                    "started": now,
                    "done": True,
                    "agent": agent,
                    "task": "",
                    "last_activity": now,
                }
                logger.debug(
                    "native subagent skipped (empty task): sid=%s slot=%s",
                    sid,
                    slot.key,
                )
                continue
            tracker[sid] = {
                "id": card_id,
                "started": now,
                "done": False,
                "agent": agent,
                "task": task,
                "last_activity": now,
                "last_tool": "",
            }
            # Register in state-level dict so DELETE /api/spawn/{id} can cancel native cards.
            _register_native_card(state, card_id, slot.key, sid)
            logger.debug(
                "native subagent spawn broadcast: id=%s agent=%s slot=%s",
                card_id,
                agent,
                slot.key,
            )
            state.broadcast_ws(
                "subagent_spawn",
                {"id": card_id, "slot": slot.key, "task": task, "agent": agent},
            )
        else:
            # Card is still being reported this update — keep it alive. Update
            # unconditionally (not just on non-empty smsg): a card reported
            # with empty status messages for >120s would otherwise be
            # auto-closed the instant it disappears from the list, since its
            # last_activity would still be the creation timestamp.
            tracker[sid]["last_activity"] = now
        info = tracker[sid]
        if info["done"]:
            continue
        if stype and stype not in _running:
            err = None
            if stype in ("failed", "error") and smsg:
                err, _ = redact_exfiltration_urls(smsg)
                err, _ = redact_credentials(err)
                err = _clip_card_error(err)
            info["done"] = True
            _feed = _native_card_feed(card_output, card_id)
            _elapsed = time.time() - info["started"]
            _result = _feed or "(output in chat)"
            info["elapsed"] = _elapsed
            info["error"] = err
            info["result"] = _result
            info["done_at"] = time.time()
            _unregister_native_card(state, card_id)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": card_id,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": err,
                    "task": info["task"],
                    "agent": info["agent"],
                    "result": _result,
                },
            )
        elif smsg and smsg.lower() != "running":
            # Surface a non-generic status message as the card's current tool.
            tool, _ = redact_exfiltration_urls(smsg)
            tool, _ = redact_credentials(tool)
            info["last_tool"] = tool[:80]
            state.broadcast_ws(
                "subagent_tool",
                {"id": card_id, "slot": slot.key, "tool": tool[:80]},
            )

    # Staleness timeout: auto-close cards that kiro-cli has stopped reporting and
    # that have had no activity for too long. This prevents native sub-agent
    # cards from staying stuck in "Starting..." indefinitely when kiro-cli
    # fails to emit a terminal status. Cards still present in the current
    # list_update are alive by definition and never timed out here.
    for sid, info in list(tracker.items()):
        if info.get("done"):
            continue
        if sid in _seen_sids:
            continue  # still reported by kiro-cli this update — not stale
        last_act = info.get("last_activity", info["started"])
        if now - last_act > _NATIVE_SUBAGENT_STALE_SECS:
            info["done"] = True
            _cid = f"native:{_redact_tool_field(sid)}"
            _feed = _native_card_feed(card_output, _cid)
            _elapsed = now - info["started"]
            _error = "timed out (no activity)"
            _result = _feed or "(no output received)"
            info["elapsed"] = _elapsed
            info["error"] = _error
            info["result"] = _result
            info["done_at"] = now
            _unregister_native_card(state, _cid)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": _cid,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": _error,
                    "task": info.get("task", ""),
                    "agent": info.get("agent", ""),
                    "result": _result,
                },
            )
            logger.info(
                "native subagent %s auto-closed: stale for %.0fs",
                _cid,
                now - last_act,
            )


def _register_native_card(state, card_id: str, slot_key: str, session_id: str) -> None:
    """Register a native subagent card in the state-level dict for cancel support."""
    if not hasattr(state, "_native_cards"):
        state._native_cards = {}  # card_id -> {slot, session_id, started}
    state._native_cards[card_id] = {
        "slot": slot_key,
        "session_id": session_id,
        "started": time.time(),
    }


def _unregister_native_card(state, card_id: str) -> None:
    """Remove a native subagent card from the state-level dict."""
    if hasattr(state, "_native_cards"):
        state._native_cards.pop(card_id, None)


def _native_subagent_close_all(state, slot, tracker, card_output=None) -> None:
    """Complete any still-open native subagent cards (turn-end safety net)."""
    for sid, info in tracker.items():
        if info.get("done"):
            continue
        info["done"] = True
        _cid = f"native:{_redact_tool_field(sid)}"
        _feed = _native_card_feed(card_output, _cid)
        _elapsed = time.time() - info.get("started", time.time())
        _result = _feed or "(output in chat)"
        info["elapsed"] = _elapsed
        info["error"] = None
        info["result"] = _result
        info["done_at"] = time.time()
        _unregister_native_card(state, _cid)
        state.broadcast_ws(
            "subagent_done",
            {
                "id": _cid,
                "slot": slot.key,
                "elapsed": _elapsed,
                "error": None,
                "task": info.get("task", ""),
                "agent": info.get("agent", ""),
                "result": _result,
            },
        )


def _retain_terminal_native(
    tracker: "dict[str, dict]",
    keep: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
    ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    now: "float | None" = None,
) -> "dict[str, dict]":
    """Retain recent terminal records for bounded post-turn reconnect replay."""
    current = time.time() if now is None else now
    terminal = [
        (sid, info)
        for sid, info in tracker.items()
        if info.get("done")
        and info.get("id")
        and (current - float(info.get("done_at") or 0.0)) <= ttl_secs
    ]
    if keep >= 0 and len(terminal) > keep:
        terminal.sort(key=lambda item: float(item[1].get("done_at") or 0.0), reverse=True)
        terminal = terminal[:keep]
    return {sid: info for sid, info in terminal}


def _safe_native_crew_debug_title(title: str) -> str:
    """Redact credentials/exfiltration URLs from an LLM-controlled native-crew
    tool title before it is logged. Control chars are escaped at the log call
    via %r."""
    safe, _ = redact_exfiltration_urls(title or "")
    safe, _ = redact_credentials(safe)
    return safe
