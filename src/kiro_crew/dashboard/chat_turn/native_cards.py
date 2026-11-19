"""Harness-native sub-agent Activity cards (kiro-cli's, KAS's and Claude Code's) and the native
AskUserQuestion card a dashboard turn shows, and the stream's native sub-agent arms
(``_on_subagent_list``, ``_on_subagent_activity``).

A row Crew built from a harness's background-task messages (a ``BackgroundCardRow``, a
Claude Code Workflow agent, ``acp.raw_sdk_workflow_cards``) carries four fields beyond
kiro-cli's shape, read off no other row: a ``label`` and a ``model`` the card shows, a
``result`` it ends with when no output reached it, and ``background``, an agent that
outlives the turn that drew it. Its card stays open past an end of turn Crew did not
cut short while the harness still runs it
(``_keep_background_open``), closes as interrupted if the harness loses it first
(``_close_lost_background``), is carried into the next turn's tracker
(``_carried_background_native``), is never closed for staleness, and never counts as
an active native crew. The harness's background-task messages reach a turn only through
the stream the turn marks as its own (``_expect_background_draw``), so a stream another
reader opens on the same client (a compaction, a channel) leaves them for the next turn."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.drivers.acp_vocab import (
    STOP_REASON_CANCEL_UNACKED,
    STOP_REASON_TIMEOUT,
    BackgroundCardRow,
    expect_dashboard_turn,
    live_background_card_ids,
    on_background_cards_lost,
)
from kiro_crew.dashboard.native_child_stop import STOP_REQUESTED
from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
)
from kiro_crew.security import redact, redact_and_truncate
from kiro_crew.validation import ValidationError, validate_ask_user_question

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        NATIVE_SUBAGENT_DONE_RESULT_CAP,
        NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
        STOP_REASON_CANCELLED,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
        _redact_tool_field,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

# Native subagent cards carry a short error string only, so a long provider
# message is clipped. The request id is what identifies the failure
# server-side and the formatter appends it LAST, so a plain head-slice drops
# precisely the part worth keeping.
_MAX_NATIVE_CARD_ERROR = 200


_RE_TRAILING_REQUEST_ID = re.compile(r"\(request_id:\s*[0-9a-fA-F-]+\)\s*$")

# Leads of the ACP handle's permission-rejection notice: the word current
# handles write and the U+26D4 no-entry sign older ones did.
_REJECT_NOTICE_LEADS = ("Blocked:", "\u26d4")  # emoji-ok: old saved lead


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


#: Bound on a card's label and model as shown.
_NATIVE_CARD_FIELD_MAX = 200


def _native_card_field(value: object, limit: int = _NATIVE_CARD_FIELD_MAX) -> str:
    """A backend-authored display field of a roster row, redacted and bounded."""
    text, _ = redact_exfiltration_urls(value if isinstance(value, str) else "")
    text, _ = redact_credentials(text)
    return text[:limit]


def _native_card_extras(info: dict) -> dict[str, str]:
    """The optional ``label`` and ``model`` a card's frames carry, when it has them."""
    return {key: info[key] for key in ("label", "model") if info.get(key)}


def _native_card_result(card_output, card_id: str, info: dict) -> str:
    """A closing card's result: its output, else the row's own result, else the marker."""
    return _native_card_feed(card_output, card_id) or info.get("row_result") or "(output in chat)"


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
            if isinstance(sub, BackgroundCardRow):
                tracker[sid]["background"] = sub.get("background") is True
                for key in ("label", "model"):
                    field_value = _native_card_field(sub.get(key))
                    if field_value:
                        tracker[sid][key] = field_value
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
                {
                    "id": card_id,
                    "slot": slot.key,
                    "task": task,
                    "agent": agent,
                    **_native_card_extras(tracker[sid]),
                },
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
            # The cancelled end of a child a person asked the harness to stop is
            # drawn as the card-only Stop draws it (``dashboard.native_child_stop``).
            stopped = bool(info.pop(STOP_REQUESTED, False)) and _is_cancelled_end(stype, smsg)
            if stype in ("failed", "error") and smsg and not stopped:
                err, _ = redact_exfiltration_urls(smsg)
                err, _ = redact_credentials(err)
                err = _clip_card_error(err)
            info["done"] = True
            row_result = sub.get("result") if isinstance(sub, BackgroundCardRow) else None
            if isinstance(row_result, str) and row_result.strip():
                info["row_result"] = redact_and_truncate(row_result, 2000)
            _elapsed = time.time() - info["started"]
            _result = _native_card_result(card_output, card_id, info)
            info["elapsed"] = _elapsed
            info["error"] = err
            info["result"] = _result
            info["done_at"] = time.time()
            if stopped:
                info["stopped"] = True
                info["outcome"] = "stopped"
            _unregister_native_card(state, card_id)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": card_id,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": err,
                    **({"stopped": True} if stopped else {}),
                    "task": info["task"],
                    "agent": info["agent"],
                    "result": _result,
                    **_native_card_extras(info),
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
        if info.get("done") or info.get("background"):
            # A background agent's end is the harness's to report.
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


def _is_cancelled_end(stype: str, smsg: str) -> bool:
    """Whether a roster status is a cancelled child's end: ``failed`` with ``cancelled``,
    how the claude client reports ``idle`` with stop reason ``cancelled``."""
    return stype == "failed" and smsg == "cancelled"


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


NATIVE_SUBAGENT_INTERRUPTED_ERROR = "interrupted (parent turn ended)"


def _native_subagent_close_all(
    state, slot, tracker, card_output=None, error: str | None = None
) -> None:
    """Complete any still-open native subagent cards (turn-end safety net).

    ``error`` is None only when the parent turn landed normally; a stopped,
    timed-out or crashed turn passes ``NATIVE_SUBAGENT_INTERRUPTED_ERROR`` so a
    child cut off mid-run does not show as a clean completion. A background card
    marked ``kept_open`` stays open: the harness still runs it, after a turn Crew
    did not cut short (``_keep_background_open``). One it does not run never
    reported its end, so it closes as interrupted whatever ``error`` says.
    """
    for sid, info in tracker.items():
        if info.get("done") or info.get("kept_open"):
            continue
        info["done"] = True
        _cid = f"native:{_redact_tool_field(sid)}"
        _elapsed = time.time() - info.get("started", time.time())
        _result = _native_card_result(card_output, _cid, info)
        _error = NATIVE_SUBAGENT_INTERRUPTED_ERROR if info.get("background") else error
        info["elapsed"] = _elapsed
        info["error"] = _error
        info["result"] = _result
        info["done_at"] = time.time()
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
                **_native_card_extras(info),
            },
        )


def _live_background_cards(turn: _ChatTurn) -> "frozenset[str]":
    """The background cards the slot's harness still runs; none when it cannot say."""
    try:
        return live_background_card_ids(turn.slot._acp_client)
    except Exception:  # noqa: BLE001 - an unanswered question keeps no card open
        logger.debug("live background cards could not be read", exc_info=True)
        return frozenset()


def _crew_cut_short(stop_reason: str) -> bool:
    """Whether Crew cut a turn short that ended with *stop_reason*: a Stop, or a recovery
    that cancelled the turn or killed its process. Either stops the harness's background
    work with the turn (Claude Code stops every background Workflow at a
    ``session/cancel``); an ending the harness chose itself (``end_turn``,
    ``max_tokens``, a refusal) leaves it running."""
    return stop_reason in (
        STOP_REASON_CANCELLED,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
        STOP_REASON_TIMEOUT,
        STOP_REASON_CANCEL_UNACKED,
    )


def _keep_background_open(turn: _ChatTurn, stop_reason: str, *, session_lost: bool = False) -> None:
    """Mark the open background cards ``_native_subagent_close_all`` leaves open.

    The ones the harness still runs, unless Crew cut the turn short
    (:func:`_crew_cut_short`) or the turn's session goes with it (*session_lost*). The
    cards kept open close as interrupted if the client loses their runs before they
    report their end (``_close_lost_background``).
    """
    background = [
        (sid, info)
        for sid, info in turn._native_tracker.items()
        if info.get("background") and not info.get("done")
    ]
    if not background:
        return
    cut_short = session_lost or _crew_cut_short(stop_reason)
    live = frozenset() if cut_short else _live_background_cards(turn)
    kept = False
    for sid, info in background:
        info["kept_open"] = sid in live
        kept = kept or info["kept_open"]
    if not kept:
        return
    state = turn.state
    slot = turn.slot
    try:
        on_background_cards_lost(slot._acp_client, lambda: _close_lost_background(state, slot))
    except Exception:  # noqa: BLE001 - a card left open closes at the next turn's end
        logger.debug("watching the background cards' runs failed", exc_info=True)


def _keep_background_open_at_release(turn: _ChatTurn) -> None:
    """Mark, by the rule a landing uses (:func:`_keep_background_open`), the open
    background cards the turn's release leaves open, however the turn ended.

    A turn that ended on an error (a provider error after the harness's own retries)
    got no landing, so it keeps open the cards the harness still runs. One cancelled
    (a Stop or the turn ceiling) keeps none, and neither does one whose session the
    release resets or whose conversation it discards (its process died, or a recovery
    replaces the session): the runs go with the session.

    A turn that landed already chose, by what the harness ran then. The run's end read
    since (the idle reader runs once the turn's read is done) is the next turn's to
    draw, so the release asks the client nothing: it only closes the cards kept open
    when the session goes or Crew cut the turn short.
    """
    stop_reason = getattr(turn, "_stop_reason", "") or ""
    session_lost = bool(
        getattr(turn, "needs_session_reset", False)
        or getattr(turn, "needs_conversation_discard", False)
    )
    if not getattr(turn, "_saw_terminal_event", False):
        _keep_background_open(turn, stop_reason, session_lost=session_lost)
    elif session_lost or _crew_cut_short(stop_reason):
        for info in turn._native_tracker.values():
            if info.get("background"):
                info["kept_open"] = False


def _expect_background_draw(client: object, expected: bool = True) -> None:
    """Mark the stream the dashboard turn opens next on *client*, its provider, as the
    one that draws what the harness's background work made (``acp.raw_sdk_tasks``), or,
    with *expected* false, take the mark back at the turn's release."""
    try:
        expect_dashboard_turn(getattr(client, "client", None), expected)
    except Exception:  # noqa: BLE001 - an unmarked turn leaves the work for the next one
        logger.debug("marking the turn's background draw failed", exc_info=True)


def _close_lost_background(state, slot) -> None:
    """Close *slot*'s open background cards as interrupted: the client lost their runs
    (its session was replaced or its process ended), so no end will come for them."""
    background = {
        sid: info for sid, info in slot._native_subagent_tracker.items() if info.get("background")
    }
    for info in background.values():
        info["kept_open"] = False
    _native_subagent_close_all(
        state,
        slot,
        background,
        slot._native_subagent_output,
        error=NATIVE_SUBAGENT_INTERRUPTED_ERROR,
    )


def _carried_background_native(tracker: "dict[str, dict]") -> "dict[str, dict]":
    """The background cards of the slot's last tracker, for the next turn's.

    Carried as they are: an open card keeps its start time, and the next turn's roster
    updates it rather than drawing it again, and only that turn's landing keeps it
    open. A closed one stays closed: a later roster naming it (the run's end, read
    after a Stop closed its cards) draws no second card.
    """
    carried = {
        sid: info for sid, info in tracker.items() if info.get("background") and info.get("id")
    }
    for info in carried.values():
        info["kept_open"] = False
    return carried


def _retain_terminal_native(
    tracker: "dict[str, dict]",
    keep: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
    ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    now: "float | None" = None,
) -> "dict[str, dict]":
    """Retain recent terminal records for bounded post-turn reconnect replay, and the
    open background cards a later turn carries on."""
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
    background = {
        sid: info
        for sid, info in tracker.items()
        if info.get("background") and not info.get("done") and info.get("id")
    }
    return {sid: info for sid, info in terminal} | background


def _safe_native_crew_debug_title(title: str) -> str:
    """Redact credentials/exfiltration URLs from an LLM-controlled native-crew
    tool title before it is logged. Control chars are escaped at the log call
    via %r."""
    safe, _ = redact_exfiltration_urls(title or "")
    safe, _ = redact_credentials(safe)
    return safe


async def _on_subagent_list(turn: _ChatTurn, event) -> None:
    """The session's native sub-agent list."""
    _native_card_output = turn._native_card_output
    _native_tracker = turn._native_tracker
    slot = turn.slot
    state = turn.state
    # kiro-cli per-subagent state (native use_subagent crews).
    # Reconcile one Activity card per sub-agent (spawn/done).
    logger.debug(
        "EVENT_SUBAGENT_LIST: %s subagents, slot=%s",
        len(event.subagents or []),
        slot.key,
    )
    _native_subagent_sync(state, slot, event.subagents, _native_tracker, _native_card_output)


async def _on_subagent_activity(turn: _ChatTurn, event) -> None:
    """A native sub-agent's activity."""
    _native_tc_card = turn._native_tc_card
    _native_tracker = turn._native_tracker
    slot = turn.slot
    # kiro-cli's _kiro.dev/session/update tags a sub-agent's inner
    # tool call with its sessionId. This ALWAYS arrives before the
    # corresponding flat tool_call/tool_call_update, so building the
    # toolCallId->card map here lets those flat events (which carry
    # the full tool title AND the real output) attribute to the
    # right sub-agent card.
    _sid = event.sub_session_id
    if _sid and event.tool_call_id:
        # A child's call whether or not it has a card: the directive refusal
        # reads this set, as the messaging driver's reads its own.
        turn._native_child_tc.add(event.tool_call_id)
    if _sid in _native_tracker and event.tool_call_id:
        _native_tc_card[event.tool_call_id] = f"native:{_redact_tool_field(_sid)}"
    # Permission-rejection notices (the handle's own "Blocked: …" lines,
    # or the U+26D4 lead an older handle wrote in its place,
    # e.g. drain-time rejects yielded at turn start) arrive BEFORE
    # any subagent_list populates the per-turn tracker — dropping
    # them here would leave the user watching a child tool fail
    # with no explanation. When the card cannot exist yet, persist
    # the explanation as a slot notice instead.
    if _sid not in _native_tracker and event.text and event.text.startswith(_REJECT_NOTICE_LEADS):
        # Stays English: the ACP handle composes this permission-rejection line
        # (its fixed reason fused with the redacted tool title) into
        # ``event.text`` at its source, and the event carries no structured
        # reason/title field to localize from. A token here needs a new field
        # on the ACP event, read by other consumers too, so it is not threaded
        # through here.
        _txt = redact(event.text)
        slot.append("notice", _txt, "msg msg-info")
    # Some kiro-cli builds also stream the sub-agent's own text via
    # agent_message_chunk on this channel — surface it on the card.
    if _sid in _native_tracker and event.text:
        _native_card_text(turn, _sid, event.text)


def _native_card_text(turn: _ChatTurn, sid: str, text: str) -> None:
    """Append a sub-agent's *text* to its open card's feed and stream it to the card."""
    _native_card_output = turn._native_card_output
    _native_card_output_len = turn._native_card_output_len
    _card_id = f"native:{_redact_tool_field(sid)}"
    _txt = redact(text)
    _native_card_output_len[_card_id] = _append_native_output(
        _native_card_output.setdefault(_card_id, []),
        _txt,
        _native_card_output_len.get(_card_id, 0),
    )
    turn.state.broadcast_ws(
        "subagent_chunk",
        {"id": _card_id, "slot": turn.slot.key, "text": _txt},
    )


def _native_flush_cut_short(turn: _ChatTurn) -> None:
    """Put the text a Claude child's redactor still holds on its card, for a turn cut short.

    A turn cancelled (Stop, the timeout ceiling, a closed tab) or ended with no
    completion (a provider error) while a card is still open gets no roster that
    would flush a child's tail, and its release closes the cards next. A turn that
    ends normally keeps the text held: a background child keeps running into the
    next turn, where its redactor still needs it. So does a turn that never opened
    a card. Claude's ``AcpClient`` and the runtime's ``AcpSessionProvider`` (for an
    announced codex child) hold child text; another client either lacks the method
    or holds nothing. It never suspends, as the release phase that calls it must not.
    """
    if getattr(turn, "_stop_reason", "") not in ("", STOP_REASON_CANCELLED):
        return
    if all(info.get("done") for info in turn._native_tracker.values()):
        return
    flush = getattr(turn.slot._acp_client, "flush_native_child_text", None)
    if not callable(flush):
        return
    try:
        held = list(flush())
    except Exception:
        logger.debug("native child text flush failed", exc_info=True)
        return
    for event in held:
        if event.sub_session_id in turn._native_tracker and event.text:
            _native_card_text(turn, event.sub_session_id, event.text)
