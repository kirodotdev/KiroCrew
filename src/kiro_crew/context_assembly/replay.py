"""Transcript replay and recall projection: rows in, prompt text out.

The context builder reads a session's own transcript in :mod:`kiro_crew.context`
(the transcript-plumbing reads stay there) and hands the rows here. This module
owns what those rows become: the per-role row quotas, the delivery-identity merge
of a disk snapshot with the live window, the tail-heavy replay budget, the
thread-history fallback, and the cancelled- and interrupted-turn restores. It never
opens a log and never redacts; the caller applies redaction to what it returns.

New replay, recall and turn-restore projections belong here.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Set as AbstractSet

from kiro_crew.context_assembly import budget as _budgets

# Message roles included in session replay, thread-history compression, and the
# context-builder recent-message path. "inject" is included so cron results and
# /note breadcrumbs survive a session boundary and can still be recalled.
RECALL_ROLES: frozenset[str] = frozenset({"user", "assistant", "inject"})


# Strip Mode Identity blocks from injected context so cross-tab or history
# content from a different mode doesn't override the current prompt's identity.
_MODE_IDENTITY_RE = re.compile(r"## 🔒 Mode Identity.*?(?=\n## |\Z)", re.DOTALL)


_STOP_EVENT_CAP = 3  # max recent stop events to inject into LLM context
_STOP_EVENT_RESOLVED_STATES = frozenset({"stopped", "stop_failed_reset"})


# Regex patterns for noise compression in assistant messages
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_JSON_BLOB_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _compress_assistant_message(text: str) -> str:
    """Reduce low-signal noise from assistant messages on the fallback path.

    Code blocks over 2K chars are replaced with a head/tail excerpt that
    preserves function signatures, imports, and structure.  JSON blobs
    over 1K chars are replaced with a truncation marker.
    """

    def _replace_code_block(m: re.Match[str]) -> str:
        body = m.group(1)
        if len(body) <= 2000:
            return m.group(0)
        lines = body.strip().splitlines()
        if len(lines) > 15:
            kept = lines[:10] + [f"  ... ({len(lines) - 15} lines omitted)"] + lines[-5:]
        else:
            # Few lines but still over 2K — apply character-level truncation
            truncated_body = body[:2000]
            kept = truncated_body.splitlines()
            kept.append(f"  ... ({len(body) - 2000} chars truncated)")
        lang_line = m.group(0).split("\n", 1)[0]  # ```lang
        return lang_line + "\n" + "\n".join(kept) + "\n```"

    result = _CODE_BLOCK_RE.sub(_replace_code_block, text)

    def _replace_json(m: re.Match[str]) -> str:
        if len(m.group(0)) <= 1000:
            return m.group(0)
        return "[tool output truncated]"

    result = _JSON_BLOB_RE.sub(_replace_json, result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


# Roles that OPEN a turn in a dashboard transcript: the row an interrupted turn
# was answering. An ``inject`` row opens one only when its ``meta.injectKind`` is
# in the caller's *opener_inject_kinds* (a cron delivery, an app message, a
# synthesis); every other row between the opener and the resume -- tool cards,
# error and notice rows, a recovery inject such as an earlier Resume press -- is
# walked past.
_TURN_OPENER_ROLES = frozenset({"user", "nudge", "subagent"})


def _interrupted_opener_kind(row: dict) -> str:
    """A short name for the automated delivery that opened an interrupted turn."""
    role = row.get("role")
    if role == "nudge":
        return "a monitor loop cycle"
    if role == "subagent":
        return "a sub-agent completion"
    meta = row.get("meta")
    kind = meta.get("injectKind") if isinstance(meta, dict) else None
    return {
        "cron": "a scheduled job",
        "mcp_app": "an app message",
        "synthesis": "a sub-agent synthesis",
    }.get(kind if isinstance(kind, str) else "", "an automated message")


def build_interrupted_turn_preamble(
    messages: list[dict],
    current: dict | None = None,
    *,
    opener_inject_kinds: AbstractSet[str] = frozenset(),
    user_cap: int = 8000,
    assist_cap: int = 4000,
) -> str:
    """Restore the interrupted turn for a backend that natively resumed without it.

    kiro-cli appends a prompt to its session log only once the model has
    answered it. When the process serving a turn dies mid-answer -- a gateway
    restart, a crash, a recycled runtime -- that turn's request is never written,
    so ``session/load`` brings the conversation back WITHOUT it. A resumed
    session gets no Kiro Crew replay (the native history is trusted to be
    complete), so a Resume pressed on that turn reaches the model as a bare
    "finish the user's most recent request" with the request missing: the model
    answers the turn before it, or reports there is nothing to continue.

    *messages* is the slot's transcript window, which still holds the turn (the
    turn-in-flight marker restores its opener after a restart). Walk back from
    *current* -- the resume row this turn is running, excluded -- to the row that
    opened the interrupted turn, collecting the assistant text it had streamed.
    Returns "" when no opener is found. The caller mints the result AFTER the
    prompt's egress scrub (its markers are in ``_STRUCTURAL_MARKER_RES``); the
    payload is scrubbed here instead.
    """
    end = len(messages)
    if current is not None:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i] is current:
                end = i
                break
    opener_idx = -1
    for i in range(end - 1, -1, -1):
        role = messages[i].get("role")
        meta = messages[i].get("meta")
        if role in _TURN_OPENER_ROLES or (
            role == "inject"
            and isinstance(meta, dict)
            and meta.get("injectKind") in opener_inject_kinds
        ):
            opener_idx = i
            break
    if opener_idx < 0:
        return ""
    user_text = str(messages[opener_idx].get("content") or "").strip()
    if not user_text:
        return ""
    assistant_parts = [
        str(m.get("content") or "").strip()
        for m in messages[opener_idx + 1 : end]
        if m.get("role") == "assistant" and str(m.get("content") or "").strip()
    ]
    assistant_text = "\n".join(assistant_parts)
    if len(user_text) > user_cap:
        user_text = user_text[:user_cap] + "… [truncated]"
    if len(assistant_text) > assist_cap:
        assistant_text = assistant_text[:assist_cap] + "… [truncated]"
    # The frame is minted outside the prompt's egress scrub, so its payload is
    # scrubbed here: a transcript row must not carry a structural marker past it.
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    user_text = ctx._neutralize_structural_markers(user_text)
    assistant_text = ctx._neutralize_structural_markers(assistant_text)
    # Only a ``user`` row is the person's own words. Every other opener is an
    # automated delivery (a monitor loop's cycle, a sub-agent's completion, a
    # cron/app/synthesis inject) and must not be framed as something the user
    # typed -- see docs/system-specs/common/injected-messages.md.
    opener = messages[opener_idx]
    if opener.get("role") == "user":
        whose = "It is the user's most recent request"
        heading = "Interrupted request"
    else:
        whose = (
            f"It is an automated delivery ({_interrupted_opener_kind(opener)}), not "
            "something the user typed, and it is the work"
        )
        heading = "Interrupted automated delivery"
    lines = [
        "[INTERRUPTED TURN — context restore]",
        "The turn below was cut off when the agent process serving this "
        "conversation stopped, so the restored conversation may not include it. "
        f"{whose}, the one you are being asked to carry on with. Tool calls it "
        "made may already have taken effect.",
        "",
        f"{heading}:\n{user_text}",
    ]
    if assistant_text:
        lines += ["", f"Partial response before the interruption:\n{assistant_text}"]
    lines.append("[END INTERRUPTED TURN]")
    return "\n".join(lines)


# ── Provider-Agnostic Session Replay ──


_REPLAY_BUDGET_CHARS = (
    80_000  # 80K chars ≈ 20K tokens — fits alongside system context in 200K window
)

# Per-row ceiling for ``inject`` content inside a replay. Conversation rows are
# uncapped here: they are the signal the replay exists to carry. An inject row
# only has to say that a cron ran or a note was left, so a breadcrumb is enough,
# and without a ceiling one chatty producer spends the whole tail-heavy budget on
# itself and evicts real history. Sized above the p75 real inject row so typical
# breadcrumbs pass through whole and only the outsized dumps are clipped.
_REPLAY_INJECT_CAP_CHARS = 2_000

# Share of the replay budget ``inject`` rows may spend between them. Conversation
# keeps the rest, which the per-row ceiling above cannot guarantee: it clips one
# row's content while leaving the total unbounded, so a tail of capped inject rows
# could spend the whole budget and leave no room for a single user turn.
_REPLAY_INJECT_BUDGET_DIVISOR = 4

# Row quota for ``inject`` rows, kept SEPARATE from the conversation quota because
# the row bound is applied by the query before any budgeting runs. Derived from the
# share above: at the per-row ceiling this many rows exactly fill it, so admitting
# more could never surface additional content.
_REPLAY_INJECT_MAX_ROWS = (
    _REPLAY_BUDGET_CHARS // _REPLAY_INJECT_BUDGET_DIVISOR
) // _REPLAY_INJECT_CAP_CHARS

_REPLAY_CONVERSATION_MAX_ROWS = 500


def _replay_identity(row: dict) -> tuple | None:
    """Delivery identity, never a global text-equality deduplication key."""
    meta = row.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    for field in ("mid", "sendId"):
        value = meta.get(field)
        if isinstance(value, str) and value:
            return (field, value, row.get("role"))
    ts = row.get("ts")
    if ts:
        return ("legacy", ts, row.get("role"), row.get("content"))
    return None


def _merge_replay_rows(disk: list[dict], pending: list[dict], current: dict | None) -> list[dict]:
    """Reconcile one snapshot before quota selection, budgeting and formatting."""
    from kiro_crew.history import transcript_sort_key

    current_id = _replay_identity(current) if current is not None else None
    rows: list[dict] = []
    positions: dict[tuple, deque[int]] = defaultdict(deque)
    for row in disk:
        identity = _replay_identity(row)
        if current_id is not None and identity == current_id:
            continue
        if identity is not None:
            positions[identity].append(len(rows))
        rows.append(row)
    for row in pending:
        identity = _replay_identity(row)
        if row is current or (current_id is not None and identity == current_id):
            continue
        matches = positions.get(identity) if identity is not None else None
        if matches:
            rows[matches.popleft()] = row
        else:
            rows.append(row)
    # Timestamps from each writer share the transcript ordering contract. Keep
    # insertion order for legacy fixtures/rows with no timestamp at all.
    if rows and all(row.get("ts") for row in rows):
        rows.sort(key=lambda row: transcript_sort_key(row["ts"]))
    return rows


# Conversation rows admitted by the bounded recall sites. Mirrors ``recent()``'s
# own ``max_messages`` default so the fallback keeps the window it always had.
_RECALL_FALLBACK_MAX_ROWS = 20


def _quota_tail(messages: list[dict], *, conv_max: int, inject_max: int) -> list[dict]:
    """The newest rows under separate conversation and ``inject`` row quotas, oldest first.

    A role-filtered tail slice lets a run of ``inject`` rows longer than the bound be
    the entire read, so conversation disappears. Counting the two quotas separately
    lets notes reach the model without competing with user/assistant turns for the
    same slots.

    Rows are handed out with their image references stripped
    (:func:`~kiro_crew.image_refs.strip_image_refs`). A row's picture belonged to an
    earlier turn and cannot travel in a text vehicle, so the reference is the only
    thing that would arrive: either as a path the prompt builder re-inlines --
    resurrecting an image a compaction already dropped -- or, once the file is gone,
    as prose naming a picture the model cannot see.
    """
    from kiro_crew.image_refs import strip_image_refs

    kept: list[dict] = []
    conv = inj = 0
    for m in reversed(messages):
        role = m["role"]
        if role == "inject":
            if inj >= inject_max:
                continue
            inj += 1
        elif role in RECALL_ROLES:
            if conv >= conv_max:
                if inj >= inject_max:
                    break
                continue
            conv += 1
        else:
            continue
        kept.append({"role": role, "content": strip_image_refs(m["content"])})
    kept.reverse()
    return kept


def replay_text(messages: list[dict], model_window: int | None) -> str:
    """The tail-heavy replay of *messages* within the window-scaled replay budget.

    Keeps as many recent messages as fit, newest first. ``inject`` rows are clipped
    to a per-row ceiling and spend their own share of the budget, so conversation is
    never starved by breadcrumbs. Returned unredacted; the caller redacts.
    """
    # Replay is a separate, existing tail-history allowance, not background
    # capacity. Preserve small-window replay limits; larger windows cannot
    # enlarge it beyond the reference allowance.
    factor = max(
        0.2,
        min(1.0, _budgets._effective_window(model_window) / _budgets._REFERENCE_WINDOW_TOKENS),
    )
    replay_budget = round(_REPLAY_BUDGET_CHARS * factor)
    inject_cap = max(1, min(replay_budget, round(_REPLAY_INJECT_CAP_CHARS * factor)))

    # Reserved so conversation cannot be starved by breadcrumbs: inject rows spend
    # their own share and older ones are skipped, while the scan keeps looking for
    # user/assistant rows rather than stopping at the first inject row that spills.
    inject_budget = max(1, replay_budget // _REPLAY_INJECT_BUDGET_DIVISOR)

    # Build lines from most recent to oldest, stop when budget exhausted
    lines: list[str] = []
    total = 0
    inject_total = 0
    for m in reversed(messages):
        role = m["role"].title()
        content = m.get("content", "")
        if m["role"] == "inject" and len(content) > inject_cap:
            content = content[:inject_cap] + "…[truncated]"
        line = f"{role}: {content}"
        if m["role"] == "inject" and inject_total + len(line) > inject_budget and lines:
            continue
        if total + len(line) > replay_budget and lines:
            break
        lines.append(line)
        total += len(line) + 2  # +2 for separator
        if m["role"] == "inject":
            inject_total += len(line) + 2

    lines.reverse()
    return "\n\n".join(lines)


def thread_history_text(recent: list[dict], caps: _budgets._ResolvedCaps) -> str:
    """The fallback thread-history body for a fresh session, or ``""``.

    Spends ``caps.history_fallback`` newest-first, compressing assistant noise and
    clipping each row. Returned unredacted; the caller redacts and frames it.
    """
    budget = caps.history_fallback
    # Per-message cap scales WITH the section budget: keeping the
    # fixed 8k cap while the budget shrinks on a small window
    # meant one big recent message (~8k) could exceed the whole
    # scaled history budget and drop ALL history. Bounding it at
    # the budget guarantees at least the newest message fits.
    per_message_cap = min(
        caps.per_message,
        max(0, budget - len("Assistant: ") - len("…[truncated]")),
    )
    # The row quota alone cannot protect conversation here: this
    # loop spends the budget newest-first, and notes are the newest
    # rows, so a few large ones exhaust it before any user or
    # assistant turn is reached. Reserve a share for notes and skip
    # the ones that spill, exactly as the replay path does, so the
    # scan keeps looking for conversation instead of stopping.
    inject_cap = max(1, min(budget, _REPLAY_INJECT_CAP_CHARS))
    inject_budget = max(1, budget // _REPLAY_INJECT_BUDGET_DIVISOR)
    inject_spent = 0
    history_lines: list[str] = []
    for m in reversed(recent):
        content = _MODE_IDENTITY_RE.sub("", m["content"])
        if m["role"] == "assistant":
            content = _compress_assistant_message(content)
        row_cap = inject_cap if m["role"] == "inject" else per_message_cap
        if len(content) > row_cap:
            content = content[:row_cap] + "…[truncated]"
        line = f"{m['role'].title()}: {content}"
        if m["role"] == "inject" and inject_spent + len(line) > inject_budget and history_lines:
            continue
        if budget - len(line) < 0:
            break
        history_lines.append(line)
        budget -= len(line)
        if m["role"] == "inject":
            inject_spent += len(line)
    if not history_lines:
        return ""
    history_lines.reverse()
    return "\n".join(history_lines)


def stop_event_notes(messages: list[dict]) -> str:
    """Short system notes for the recent resolved ``stop_event`` rows in *messages*."""
    notes: list[str] = []
    for m in reversed(messages):
        if len(notes) >= _STOP_EVENT_CAP:
            break
        if m.get("role") != "system":
            continue
        content = m.get("content", "")
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(data, dict)
            and data.get("kind") == "stop_event"
            and data.get("state") in _STOP_EVENT_RESOLVED_STATES
        ):
            notes.append("[User stopped the previous turn mid-execution.]")
    if not notes:
        return ""
    notes.reverse()
    return "\n".join(notes) + "\n\n"


def cancelled_turn_preamble(recent: list[dict], *, user_cap: int, assist_cap: int) -> str:
    """The ``[PREVIOUS TURN WAS CANCELLED …]`` restore for *recent* rows, or ``""``.

    Scans backwards for a ``stop_event`` marker, then takes the user message
    immediately before it plus any assistant text in between.
    """
    if not recent:
        return ""
    # Look for a stop_event marker (dashboard writes these; Slack does not).
    # If present, it bounds the cancelled turn. Otherwise fall back to "last
    # user turn" — safe because (a) ``prev_turn_cancelled`` is a one-shot
    # flag consumed right before this function runs, and (b) callers persist
    # the NEW user message to ``conversation_log`` only AFTER the preamble
    # is built (see handler.py save_conversation_turn / chat.py _flush_segment),
    # so ``recent()`` at this moment contains only prior turns and the most
    # recent user entry is the cancelled one.
    stop_idx = -1
    for i in range(len(recent) - 1, -1, -1):
        if recent[i].get("role") != "system":
            continue
        content = recent[i].get("content", "")
        if not isinstance(content, str) or not content:
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("kind") == "stop_event":
                stop_idx = i
                break
        except (ValueError, TypeError):
            continue
    # Find the most recent user message. If a stop_event was found, the user
    # message must precede it; otherwise just take the latest user entry.
    search_end = stop_idx if stop_idx >= 0 else len(recent)
    user_idx = -1
    for i in range(search_end - 1, -1, -1):
        if recent[i].get("role") == "user":
            user_idx = i
            break
    if user_idx < 0:
        return ""
    # Collect any assistant text between user_idx and the boundary.
    boundary = stop_idx if stop_idx >= 0 else len(recent)
    user_text = (recent[user_idx].get("content") or "").strip()
    assistant_parts: list[str] = []
    for i in range(user_idx + 1, boundary):
        if recent[i].get("role") == "assistant":
            t = (recent[i].get("content") or "").strip()
            if t:
                assistant_parts.append(t)
    assistant_text = "\n".join(assistant_parts)
    if len(user_text) > user_cap:
        user_text = user_text[:user_cap] + "… [truncated]"
    if len(assistant_text) > assist_cap:
        assistant_text = assistant_text[:assist_cap] + "… [truncated]"
    lines = [
        "[PREVIOUS TURN WAS CANCELLED BY THE USER — context restore]",
        "The following user request was interrupted mid-response. "
        "Do not emit any standalone acknowledgment of the cancellation. "
        "Use this restored context silently and respond only to the current "
        "user request, referencing the interrupted work only when the "
        "current request depends on it.",
        "",
        f"Cancelled user request:\n{user_text}",
    ]
    if assistant_text:
        lines += ["", f"Partial assistant response before cancel:\n{assistant_text}"]
    lines.append("[END PREVIOUS TURN]")
    return "\n".join(lines)
