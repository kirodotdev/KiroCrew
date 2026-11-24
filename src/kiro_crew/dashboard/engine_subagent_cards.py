"""What the Activity card of a KAS engine sub-agent shows beyond a native card's.

With the ``kas_subagents`` preview on, a person's dashboard chat on the default
agent runs its in-turn sub-agents inside the KAS engine (``acp/kas_engine_subagents``).
Such a child is a native card (``chat_turn/native_cards``), and the engine reports more about
it than the card's roster row carries, and less than a ``spawn_run`` child's card
shows. This module holds that difference, and only a card of such a turn takes it
(:func:`turn_runs_engine_children`); every other native card is drawn as it was.

What the engine reports and the card shows: the task the child was given, each
tool call the child makes, and why a child that did not complete ended. What it does
not report, and the card therefore never shows a figure for: credits, tokens, context
use and the model of one child, and a way to stop one child while the turn goes on.

A card of such a turn carries ``engine: true`` on its ``subagent_spawn``,
``subagent_done`` and replayed frames, so the page can say those two things in words.

Every string such a card, its approval card or its record shows is model-written and
is cut to a display bound. Each of those cuts goes through :func:`shown_cut`, or
:func:`cut_unsplit` for a text already shown, and nowhere else.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.agent_sdk.kas_subagent_runs import NATIVE_CHILD_ROSTER_CAP, liveness_key
from kiro_crew.security import redact
from kiro_crew.security.redaction_switch import always_scan, credential_pass_bypassed

#: The engine's status for a dispatch it has not started. It may be waiting on the
#: dispatch's approval card, so no card is drawn and no clock runs until it starts.
ENGINE_NOT_STARTED = "pending"
#: What a card says a child ended with when the engine gave a failed status and no words.
ENGINE_FAILED_NO_REASON = "failed (the engine gave no reason)"
#: How many of a card's child tool calls are remembered, to tell a call's later
#: frames from a new call. The page's own timeline keeps as many.
ENGINE_TOOL_CALLS_KEPT = 50
#: The longest tool title a card's frame carries, as for every other native card.
ENGINE_TOOL_TITLE_MAX = 80
#: How many dispatches' tasks one turn keeps for their approval cards: as many as the
#: native-child roster admits, so every dispatch it holds can show its task. Each
#: task is the roster row's own string, bounded where the row is written.
DISPATCH_TASKS_KEPT = NATIVE_CHILD_ROSTER_CAP
#: The longest task a dispatch's approval card shows.
APPROVAL_TASK_MAX = 600
#: The longest task and agent name a card shows, and the longest reason for a child
#: that failed (:func:`shown_reason`): within the bound the card's own sync clips an
#: error to, so that clip never cuts it a second time.
ENGINE_TASK_MAX = 2000
ENGINE_AGENT_MAX = 512
ENGINE_REASON_MAX = 200
#: The longest asking sub-agent's name an approval card shows.
ASKER_MAX = 80
#: How far past a display bound :func:`shown_cut` reads. The scan that tells whether
#: a cut splits a credential has to see the credential whole, so this is the reach of
#: every scan made here: the longest tail the stream redactor holds back for one
#: (a JWT). A credential that runs further past a bound than this is cut like text.
SHOWN_LOOKAHEAD = 4096


def shown_cut(text: str, bound: int) -> str:
    """*text* as this chat shows it, cut to *bound* characters, splitting no credential.

    The one place a model-written string of an engine card is cut for display. It
    reads at most ``bound + SHOWN_LOOKAHEAD`` characters, however long *text* is.

    With credential redaction in force the window is redacted whole and then cut.
    Where the owner's setting has it stand down (the owner's own chat turn), the text
    stays as written, and the cut is the hazard: every other reader's copy is redacted
    after it, and a credential the cut split does not read as one any more. So the cut
    is checked against a scan in full (:func:`cut_unsplit`), and only a text it would
    split is stored masked. The owner then reads what they wrote, cut, in every other
    case.
    """
    shown = redact(text[: bound + SHOWN_LOOKAHEAD])
    if not credential_pass_bypassed():
        return shown[:bound]
    return cut_unsplit(shown, bound)


def shown_reason(text: str) -> str:
    """Why a child failed, as this chat shows it: at most :data:`ENGINE_REASON_MAX` characters.

    :func:`shown_cut`'s rule, with the one thing the card's own error clip does for
    every native card: a request id the engine's formatter put last is kept, behind
    an elision mark, and the text before it is cut to make room. The id is the match
    of that clip's own pattern in the text as shown, so with redaction in force it is
    read after the pass. Where the pass stands down, the text as written is clipped
    that way only if a scan in full splits nothing before the id and leaves the id
    as it is; otherwise the masked text is clipped instead.
    """
    shown = redact(text)
    head, request_id = _reason_parts(shown)
    if head == shown or not credential_pass_bypassed():
        return _reason(head, request_id)
    stands, masked = _unsplit(shown, len(head), request_id)
    return _reason(head, request_id) if stands else _reason(*_reason_parts(masked))


def _reason_parts(shown: str) -> tuple[str, str]:
    """``(text kept, request id kept after it)`` of *shown* clipped to :data:`ENGINE_REASON_MAX`."""
    # The card's own clip keeps this pattern (``chat_turn.native_cards``). Read off the
    # runner, which composes that module and is the one import path to it; imported
    # here because the runner loads this module.
    from kiro_crew.dashboard import chat_runner

    if len(shown) <= ENGINE_REASON_MAX:
        return shown, ""
    last = getattr(chat_runner, "_RE_TRAILING_REQUEST_ID").search(shown)
    request_id = last.group(0).strip() if last else ""
    room = ENGINE_REASON_MAX - len(request_id) - 4  # the elision mark and a space
    if not request_id or room <= 0:
        return shown[:ENGINE_REASON_MAX], ""
    return shown[:room], request_id


def _reason(head: str, request_id: str) -> str:
    return f"{head}... {request_id}" if request_id else head


def cut_unsplit(shown: str, bound: int) -> str:
    """*shown*, a text already as this chat shows it, cut to *bound*, splitting no credential.

    The cut stands when a scan in full masks nothing differently in the whole text
    than in the cut one. Otherwise the cut fell inside something the scan masks, or
    removed what made it read as a credential, and the result is the masked text cut
    instead. Whatever the owner's setting reads: a text redacted when it was shown
    passes unchanged, and one shown as written is the case this exists for.
    """
    if len(shown) <= bound:
        return shown
    stands, masked = _unsplit(shown, bound)
    return shown[:bound] if stands else masked[:bound]


def _unsplit(shown: str, bound: int, kept_last: str = "") -> tuple[bool, str]:
    """Whether cutting *shown* to *bound* splits nothing a scan in full masks, and that
    scan's text. With *kept_last*, text kept from the end of *shown* beside the cut,
    the scan must also leave that as it is."""
    with always_scan():
        masked = redact(shown)
        stands = masked.startswith(redact(shown[:bound])) and masked.rstrip().endswith(kept_last)
    return stands, masked


def turn_runs_engine_children(turn: Any) -> bool:
    """Whether this turn's native sub-agents are the engine's own under the ``kas_subagents`` preview.

    What the turn's session registered (:func:`session_runs_engine_children`), read
    once per turn. Any failure to read it is False, which draws the cards as they were.
    """
    known = turn._engine_children
    if known is None:
        known = turn._engine_children = session_runs_engine_children(turn.slot)
    return bool(known)


def session_runs_engine_children(slot: Any) -> bool:
    """Whether the session behind *slot*'s turn registered the engine's sub-agent tool.

    The session's own record of what it mounted, not the preview as it reads now: a
    chat keeps the sub-agent path it was opened with, and the switch reaches only a
    session registered after it moved. The record is the one the session's permission
    stamps read (``acp.session_handle_permissions._shipped_helper_mounted``): the
    agent the registration mounted Kiro Crew's helper for
    (``AcpSessionHandle.kas_shipped_helper_parent``) is the agent the session runs
    now (``kas_projected_agent``). Read off the slot's live client, as the turn's end
    reads the same handle. A session that registered no mount, one moved to another
    agent, another harness and a slot with no turn all answer False. Never raises.
    """
    try:
        handle = getattr(getattr(slot, "_acp_client", None), "_handle", None)
        agent = getattr(handle, "kas_projected_agent", "")
        mounted_for = getattr(handle, "kas_shipped_helper_parent", "")
        return isinstance(agent, str) and bool(agent) and mounted_for == agent
    except Exception:  # noqa: BLE001 - an unreadable session keeps everything as it was
        return False


class EngineRow(dict[str, Any]):
    """A roster row of a KAS engine turn's own sub-agent, as its card reads it.

    A type, not a key: only :func:`engine_rows` builds one, so no row read off a
    harness's wire can carry the mark.
    """


def engine_rows(rows: object, tracker: dict[str, dict[Any, Any]]) -> list[EngineRow]:
    """The roster *rows* of an engine turn, each as the card reads it.

    For each row the session handle kept (``acp.session_handle_native_children``):

    * a dispatch the engine has not started, with no card yet, is left out, so no
      card is drawn and no clock runs for it;
    * the agent and the task are the ones the dispatch named (``target`` and
      ``prompt``), not the row's title, which is ``Sub-agent: <agent>`` for every
      child;
    * a failed status with no message carries the engine's reason (``failure``), or
      :data:`ENGINE_FAILED_NO_REASON`.

    The row holds those three as the engine sent them. Each is cut here for display
    (:func:`shown_cut`, :func:`shown_reason`), once: the agent and the task for a row
    with no card yet, which the card's sync is about to draw, and the reason for a
    card not yet closed.
    A roster repeats every row on every frame, and a card that is drawn reads none
    of them again, so a repeated row costs no scan.
    """
    shown: list[EngineRow] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        held = row.get("status")
        status = held if isinstance(held, dict) else {}
        stage = str(status.get("type") or "").lower()
        drawn = tracker.get(str(row.get("sessionId") or ""))
        if stage == ENGINE_NOT_STARTED and drawn is None:
            continue
        card = EngineRow(row)
        target = row.get("target")
        prompt = row.get("prompt")
        if drawn is None and isinstance(target, str) and target.strip():
            card["agentName"] = shown_cut(target, ENGINE_AGENT_MAX)
        if drawn is None and isinstance(prompt, str) and prompt.strip():
            card["initialQuery"] = shown_cut(prompt, ENGINE_TASK_MAX)
        closed = isinstance(drawn, dict) and bool(drawn.get("done"))
        if stage in ("failed", "error") and not closed and not str(status.get("message") or ""):
            failure = row.get("failure")
            reason = failure if isinstance(failure, str) and failure.strip() else ""
            card["status"] = {**status, "message": shown_reason(reason) or ENGINE_FAILED_NO_REASON}
        shown.append(card)
    return shown


def remember_dispatch_tasks(tasks: dict[str, str], rows: object) -> None:
    """Keep, in *tasks*, the task each roster row's dispatch named, by its call id.

    A dispatch's approval request names the call (``toolCall.toolCallId``) and not
    the task, which only its dispatch frame carries (``rawInput.prompt``), so the
    card reads it here (:func:`approval_card_fields`). Bounded by
    :data:`DISPATCH_TASKS_KEPT` calls; a turn's map is new each turn.
    """
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        call, prompt = row.get("dispatch_call"), row.get("prompt")
        if not isinstance(call, str) or not call or not isinstance(prompt, str) or not prompt:
            continue
        if call not in tasks and len(tasks) >= DISPATCH_TASKS_KEPT:
            continue
        tasks[call] = prompt


def approval_card_fields(turn: Any, event: Any) -> dict[str, str]:
    """What an approval card of an engine turn shows beyond any other card's. Display only.

    ``subagent_task``: the task a sub-agent dispatch waiting on this card was given.
    ``asked_by_subagent``: the sub-agent whose call this is, when the engine named it
    on the request (``AcpEvent.requesting_subagent``). Both are model-written and
    held as the engine sent them; each is cut here to what the card shows
    (:func:`shown_cut`, :data:`APPROVAL_TASK_MAX` and :data:`ASKER_MAX`), so the
    card's own builder (``chat_turn.permission_prompt``), which redacts and bounds
    every field it shows, cuts neither again. Neither is read by any
    gate, grant or audit. Outside an engine turn, or when the engine reported
    neither, this is empty and the card is as it was.
    """
    if not turn_runs_engine_children(turn):
        return {}
    fields: dict[str, str] = {}
    # The roster row keeps the dispatch's id as its liveness key; look it up the same way.
    call = liveness_key(getattr(event, "tool_call_id", ""))
    task = (getattr(turn, "_engine_dispatch_tasks", None) or {}).get(call) if call else None
    if isinstance(task, str) and task.strip():
        fields["subagent_task"] = shown_cut(task, APPROVAL_TASK_MAX)
    asker = getattr(event, "requesting_subagent", "")
    if isinstance(asker, str) and asker.strip():
        fields["asked_by_subagent"] = shown_cut(asker, ASKER_MAX)
    return fields


def child_tool_frame(info: dict[str, Any], tool_call_id: str, title: str) -> dict[str, Any] | None:
    """The ``subagent_tool`` fields for one frame of an engine child's tool call.

    ``None`` when the frame says nothing new: a call sends several frames, and only
    its first, or one that renames it, changes what the card lists. *info* is the
    card's tracker record; it remembers the last ``ENGINE_TOOL_CALLS_KEPT`` calls and
    counts every call it saw. *title* is the call's title as the turn holds it; it
    is cut here to :data:`ENGINE_TOOL_TITLE_MAX` (:func:`shown_cut`).
    """
    # The id is backend-authored and bounded only by the frame cap: held, matched and
    # broadcast as its liveness key (as sent up to the bound, its digest past it).
    key = liveness_key(tool_call_id)
    tool = shown_cut(title, ENGINE_TOOL_TITLE_MAX)
    if not key or not tool:
        return None
    seen: dict[str, str] = info.setdefault("tool_calls", {})
    known = seen.get(key)
    if known == tool:
        return None
    if known is None:
        info["tool_count"] = int(info.get("tool_count") or 0) + 1
        if len(seen) >= ENGINE_TOOL_CALLS_KEPT:
            del seen[next(iter(seen))]
    seen[key] = tool
    info["last_tool"] = tool
    frame: dict[str, Any] = {
        "tool": tool,
        "tool_call_id": key,
        "tool_count": info["tool_count"],
    }
    if known is not None:
        frame["is_update"] = True
    return frame


__all__ = [
    "APPROVAL_TASK_MAX",
    "ASKER_MAX",
    "DISPATCH_TASKS_KEPT",
    "ENGINE_AGENT_MAX",
    "ENGINE_FAILED_NO_REASON",
    "ENGINE_NOT_STARTED",
    "ENGINE_REASON_MAX",
    "ENGINE_TASK_MAX",
    "ENGINE_TOOL_CALLS_KEPT",
    "ENGINE_TOOL_TITLE_MAX",
    "EngineRow",
    "SHOWN_LOOKAHEAD",
    "approval_card_fields",
    "child_tool_frame",
    "cut_unsplit",
    "engine_rows",
    "remember_dispatch_tasks",
    "session_runs_engine_children",
    "shown_cut",
    "shown_reason",
    "turn_runs_engine_children",
]
