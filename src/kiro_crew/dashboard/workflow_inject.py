"""Inject a finished workflow run's result back into the originating chat.

When a background workflow run reaches a terminal state, the registry fires
``on_done`` → this injects a summary + result into the chat session that started
it, so the agent can continue the conversation with the workflow's output (the
whole point of the chat integration). Mirrors ``cron_inject`` — appends to the
linked dashboard slot and persists to the conversation log so a follow-up turn
has it as context. LLM-derived text is redacted before delivery.

If the run had no originating session (e.g. launched from the Workflows tab with
no chat link), the result simply isn't injected — the tab already shows it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.background_commands import DRIVER, describe_outcome
from kiro_crew.dashboard.chat_utils import dashboard_slot_key
from kiro_crew.dashboard.slot_ownership import app_holds_gateway_key
from kiro_crew.dashboard.state import (
    DashboardState,
    append_and_surface,
    load_window_off_loop,
    note_crew_log_class,
    row_mid,
)
from kiro_crew.history import append_if_absent_off_loop
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Matches absolute POSIX paths to a file with an extension (artifacts a workflow
# may have written, e.g. "/home/u/report.md"). Conservative: absolute only, must
# have a dotted file extension, so prose rarely false-matches.
_PATH_RE = re.compile(r"(?<![\w./])(/[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,8})\b")


def _collect_artifact_paths(value: object, out: list[str]) -> None:
    """Walk a JSON-able result collecting file-path-like strings (artifacts)."""
    if isinstance(value, str):
        for m in _PATH_RE.findall(value):
            if m not in out:
                out.append(m)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_artifact_paths(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_artifact_paths(v, out)


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


# The longest serialized result a completion message carries whole. A longer one is
# shown as a plain-text preview with a pointer to the complete value.
_MAX_RESULT_CHARS = 4000


def _summarize(snapshot: dict) -> str:
    """Build the chat message body from a terminal run snapshot."""
    if snapshot.get("driver") == DRIVER:
        return _summarize_command(snapshot)
    name = snapshot.get("name") or snapshot.get("run_id", "")
    status = snapshot.get("status", "")
    run_id = snapshot.get("run_id", "")
    lines = ["[Workflow completion event]", f"Workflow `{name}` ({run_id}) → **{status}**"]
    if status == "finished":
        result = snapshot.get("result")
        try:
            body, fence = json.dumps(result, indent=2, default=str), "json"
        except Exception:  # noqa: BLE001
            body, fence = str(result), "text"
        if len(body) <= _MAX_RESULT_CHARS:
            lines.append(f"\nResult:\n```{fence}\n{body}\n```")
        else:
            # A cut JSON document is not JSON, so the preview is never labelled json.
            lines.append(
                f"\nResult (truncated: first {_MAX_RESULT_CHARS} of {len(body)} characters;"
                f" the complete value is the `result` field of workflow_result('{run_id}')):"
                f"\n```text\n{body[:_MAX_RESULT_CHARS]}\n```"
            )
        # Surface any artifact file paths the run produced, so the chat agent can
        # open/act on them directly instead of digging through the result blob.
        artifacts: list[str] = []
        _collect_artifact_paths(result, artifacts)
        if artifacts:
            lines.append("\nArtifacts (open with your file tools):")
            lines.extend(f"- `{p}`" for p in artifacts[:20])
            if len(artifacts) > 20:
                lines.append(f"- … and {len(artifacts) - 20} more")
    # A returned result does not imply that its durable checkpoint succeeded.
    if snapshot.get("error"):
        lines.append(f"\nError: {snapshot['error']}")
    # A failed run is not necessarily an empty one: every agent call that completed
    # before the ceiling / cancel / crash is preserved on the record. Say so
    # explicitly — otherwise the reader assumes the whole run was lost and either
    # redoes the work or goes digging through the run JSON by hand.
    partial_count = snapshot.get("partial_result_count") or 0
    error_count = snapshot.get("agent_error_count") or 0
    if partial_count:
        lines.append(
            f"\n{partial_count} agent result(s) finished before the run ended and were "
            f"preserved — read them with `workflow_result('{run_id}')` under "
            "`partial_results` (keyed by agent call index)."
        )
    result_count = snapshot.get("agent_result_count") or 0
    if result_count:
        lines.append(
            f"\n{result_count} agent call result(s) recorded — read `agent_results` with "
            f"workflow_result('{run_id}'). Finished means the workflow function returned; "
            "required artifacts are not verified by this status."
        )
    if error_count:
        lines.append(f"{error_count} agent call(s) failed; each reason is under `agent_errors`.")
    lines.append(
        f"\nUse workflow_result('{run_id}') for the full event stream, or "
        f"workflow_rerun_subtree('{run_id}', …) to restart from a step."
    )
    return "\n".join(lines)


def _summarize_command(snapshot: dict) -> str:
    """Completion body for a background command run.

    Keeps the workflow completion header so the chat renders it as the same
    compact card; the body carries what the waiting agent needs to continue.
    """
    # The card's header parser takes a single-line, backtick-free name.
    name = " ".join(str(snapshot.get("name") or snapshot.get("run_id", "")).split())
    name = name.replace("`", "'")
    status = snapshot.get("status", "")
    run_id = snapshot.get("run_id", "")
    result = snapshot.get("result")
    result = result if isinstance(result, dict) else {}
    lines = ["[Workflow completion event]", f"Workflow `{name}` ({run_id}) → **{status}**"]
    if result:
        command = " ".join(str(result.get("command", "")).split()).replace("`", "'")
        lines.append(f"\nThe background command {describe_outcome(result)}.")
        lines.append(f"Command: `{command}`")
        tail = str(result.get("output_tail") or "")
        if tail:
            # A fence longer than any backtick run in the output, so command
            # output can never close it and pass as message text.
            longest = max((len(run) for run in re.findall(r"`+", tail)), default=0)
            fence = "`" * max(3, longest + 1)
            lines.append(f"\nLast lines of output:\n{fence}text\n{tail}\n{fence}")
        else:
            lines.append("\nThe command printed no output.")
        if result.get("log_path"):
            lines.append(f"Full output: `{result['log_path']}` (open with your file tools).")
    if snapshot.get("error"):
        lines.append(f"\nError: {snapshot['error']}")
    lines.append(f"\nUse workflow_result('{run_id}') for the run record.")
    return "\n".join(lines)


def _slot_key_from_session(session_key: str) -> str:
    """Map an originating session_key to the dashboard slot key it came from.

    A chat-launched run carries the session key of the chat that launched it,
    which for a channel-born tab is the channel's own key — so the slot name
    comes from the live tab rather than from stripping a ``dashboard:`` prefix
    such a key never had.

    A key that names no tab (a cron/legacy session, an already-bare slot key) is
    returned as-is: the caller looks it up and routes to the ``workflow-<id>``
    fallback slot on a miss.
    """
    return dashboard_slot_key(session_key) or session_key


def inject_workflow_result(
    state: DashboardState,
    run_id: str,
    snapshot: dict,
    *,
    on_injected: Optional[Callable[[Any, dict], None]] = None,
) -> bool:
    """Inject a terminal run's result into its ORIGINATING chat slot.

    The whole point of the chat integration: when a run finishes, its result must
    land in the SAME chat the user launched it from — appended to that slot AND
    broadcast live as a chat_message so it shows up without a manual fetch. Only if
    that slot no longer exists do we fall back to a dedicated ``workflow-<id>`` slot.

    ``on_injected(slot, snapshot)`` (optional) fires exactly once, only on a FRESH
    inject into the live ORIGINATING slot (never the ``workflow-<id>`` fallback, a
    dedup re-fire, or a UI-only run). The gateway uses it to auto-run an agent turn
    so the launching agent actually interprets the result — injecting the summary
    alone leaves it as a passive ``assistant`` message the model never acts on.

    Returns True if injected, False if there was no originating session to route to
    (e.g. a UI-only run). Best-effort — never raises.
    """
    session_key = (snapshot.get("session_key") or "").strip()
    if not session_key:
        return False  # no chat to route back to (e.g. UI-only run)

    try:
        msg = _redact(_summarize(snapshot))

        # 1. Prefer the ORIGINATING slot (the chat the user launched it from).
        slot = None
        target_slot_key = _slot_key_from_session(session_key)
        if target_slot_key:
            getter = getattr(state, "get_slot", None)
            if getter is not None:
                slot = getter(target_slot_key)
        # The originating chat is live iff we found its slot above; the auto-turn
        # only makes sense there (the fallback slot has no agent watching it).
        is_originating = slot is not None

        # 2. Fall back to a dedicated workflow slot only if the chat is gone.
        if slot is None:
            if snapshot.get("memory_mode", "persistent") != "persistent":
                return False
            # Never adopt an app-owned slot under the run's key: linking it would
            # hand the app the originating session's transcript.
            if app_holds_gateway_key(state, f"workflow-{run_id}", "workflow.inject_result"):
                return False
            slot = state.get_or_create_slot(name=f"workflow-{run_id}")
            if not getattr(slot, "linked_session_key", ""):
                slot.linked_session_key = session_key
                # A reused ``workflow-<run_id>`` slot object can still hold the
                # PRIOR run's in-memory dismissed set. Leaving it in place while
                # the slot now links a DIFFERENT transcript would let the next
                # union-save fold those foreign tombstones into the newly-linked
                # transcript and suppress unrelated links there — the same silent,
                # permanent cross-transcript corruption the cron bind path clears
                # against. Empty the set and mark it dismissed-unhydrated so the
                # union carries only the linked transcript's own on-disk line; a
                # later readable restore replaces it authoritatively.
                slot._dismissed_source_links = set()
                slot.invalidate_source_links()
                slot._dismissed_hydrated = False
                note_crew_log_class(state, slot)
                from kiro_crew.dashboard.transcript_binding import bind_transcript

                bind_transcript(state, slot)
            slot.title = f"Workflow: {snapshot.get('name') or run_id}"

        # Dedup: don't double-inject the same result on a re-fire.
        already = any(m.get("content") == msg for m in getattr(slot, "messages", []))
        if not already:
            # Carry the window copy's minted ``meta.mid`` (read off the append's
            # return via ``row_mid``) onto the durable copy below: one logical
            # message, one identity, so the bounded-read identity walk
            # recognises the persisted row instead of re-appending the
            # injection. append_and_surface delivers the live copy through
            # exactly one identity-carrying door — an unconditional explicit
            # frame here carries no ``meta.mid``, so the client renders the same
            # result twice whenever append's own broadcast also fires.
            window_mid = row_mid(
                append_and_surface(
                    state,
                    slot,
                    "assistant",
                    msg,
                    "msg msg-a",
                    meta={"kind": "workflow_result"},
                )
            )
            # Persist so a follow-up chat turn has the result as context.
            try:
                if (
                    state.conversation_log is not None
                    and snapshot.get("memory_mode", "persistent") == "persistent"
                    and getattr(slot, "memory_mode", "persistent") == "persistent"
                ):
                    # inject_workflow_result runs on the event loop (invoked from
                    # the workflow runner's on_done inside an asyncio task), so
                    # offload the lock-backed disk append to a worker thread —
                    # otherwise the on-loop _locked path drops it under any
                    # concurrent holder. The slot.append above already surfaces
                    # the result to the live UI; this is the durable replay copy.
                    #
                    # Use append_if_absent (not a plain append): slot.append has
                    # already put this message into the DIRTY in-memory slot, so
                    # a periodic slot save may serialize it to disk before this
                    # durable copy runs. A plain append would then write it a
                    # SECOND time and the workflow result would be replayed twice
                    # after a restart. append_if_absent does the existence check
                    # under the SAME per-session lock the slot save takes, so the
                    # write collapses to a no-op when the save already landed it.
                    append_if_absent_off_loop(
                        state.conversation_log, session_key, "assistant", msg, mid=window_mid
                    )
            except Exception:  # noqa: BLE001
                pass
            # Auto-run the launching agent on the fresh result, but ONLY in the
            # live originating chat (never the workflow-<id> fallback, which has
            # no agent watching it). The summary above is a passive ``assistant``
            # message the model won't act on by itself; ``on_injected`` lets the
            # gateway start an agent turn so the result is actually interpreted.
            if is_originating and on_injected is not None:
                try:
                    on_injected(slot, snapshot)
                except Exception:  # noqa: BLE001 - auto-turn is best-effort
                    pass
        # Nudge the UI / mark unread on the originating slot.
        try:
            state.broadcast_ws("workflow_result_injected", {"run_id": run_id, "slot": slot.key})
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001 - injection is best-effort
        return False


def _seed_turn_timeout(slot: Any, meta: Optional[dict]) -> None:
    """Give a fresh slot linked to an existing transcript that transcript's turn time limit.

    *meta* is the transcript's readable metadata line, or ``None`` when it could
    not be read. Unread, the slot holds no limit of its own and is marked
    unhydrated, so its saves carry the on-disk value forward instead of
    replacing it with "follow the global".
    """
    from kiro_crew.dashboard.slot_persistence.metadata_values import _validate_turn_timeout_secs

    if meta is None:
        slot.turn_timeout_secs = None
        slot._turn_timeout_hydrated = False
        return
    slot.turn_timeout_secs = _validate_turn_timeout_secs(meta.get("turn_timeout_secs"))
    slot._turn_timeout_hydrated = True


#: A boot-time delivery waits this long at most for the open tabs to come back:
#: a surface that restores none never sets ``open_slots_restored``.
_OPEN_TABS_WAIT_SECS = 60.0
_OPEN_TABS_POLL_SECS = 0.5


async def await_open_tabs(state: DashboardState, since: float) -> None:
    """Wait for this boot's open-tab restore, up to a bound past ``since`` (monotonic).

    A result delivered before its chat's tab is back lands in a fallback slot no
    agent watches, so the chat that waits on it never wakes.
    """
    import asyncio
    import time

    give_up = since + _OPEN_TABS_WAIT_SECS
    while not getattr(state, "open_slots_restored", True) and time.monotonic() < give_up:
        await asyncio.sleep(_OPEN_TABS_POLL_SECS)


#: How often a delivery re-checks whether its queued wake has run.
_WAKE_POLL_SECS = 1.0
#: How long a delivery waits on its wake before the card alone is the delivery: a
#: chat held for sign-in, or one that never goes idle, keeps the run's slot that long.
_WAKE_WAIT_SECS = 24 * 3600
#: The role the drain writes a ``BACKGROUND_COMMAND_KIND`` wake's row with.
_WAKE_ROW_ROLE = "inject"


class _WakeConsumption:
    """The runner's consumption reports for one wake, read by the delivery waiting on it."""

    __slots__ = ("consumed", "irreversible")

    def __init__(self) -> None:
        self.consumed = False
        self.irreversible = False

    def note(self, consumed: bool = True) -> None:
        # False retracts an earlier report: the runner re-queued the prompt verbatim.
        self.consumed = consumed

    def note_irreversible(self) -> None:
        # Output streamed or a tool ran: no retry can take this report back.
        self.irreversible = True


async def deliver_command_outcome(
    state: DashboardState,
    run_id: str,
    snapshot: dict,
    *,
    prompt: str,
    wake: Callable[[Any, Callable[[bool], None], Callable[[], None]], Optional[str]],
    dispatch: Callable[[Any, str], Awaitable[bool]],
) -> bool:
    """Post a background command's card, wake its chat, and say whether both are saved.

    The card is posted once (a repeat is dropped). ``wake`` queues ``prompt`` on the
    chat with the runner's two consumption callbacks and returns its queue id, or None
    when the chat cannot take a turn now; an idle chat has that entry run by
    ``dispatch``. The wake counts only once the runner reports its prompt consumed:
    its row is not enough, because a turn a signed-out CLI refuses writes one and
    runs nothing. True only once the slot is saved and its transcript carries the
    card and the wake's row, which is what the caller settles on.
    """
    import asyncio

    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key

    if not await inject_bound_workflow_result(state, run_id, snapshot):
        return False
    origin = snapshot.get("session_key") or ""
    slot = state.get_slot(_slot_key_from_session(origin))
    if slot is not None and effective_session_key(slot) == origin:
        consumption = _WakeConsumption()
        queue_id = wake(slot, consumption.note, consumption.note_irreversible)
        if queue_id is None:
            return False
        if getattr(slot, "memory_mode", "persistent") != "persistent":
            if not slot.running:
                await dispatch(slot, queue_id)
            return True
        outcome = await _until_wake_consumed(
            state, slot, prompt, queue_id, consumption, wake, dispatch
        )
        if outcome is None:
            return False
        if not outcome:
            # Nothing is left to wake (its user removed it or Stopped its turn), or
            # the wait ran out: the card is the delivery.
            prompt = ""
    else:
        # The card went to the fallback slot, which no agent watches.
        slot, prompt = state.get_slot(f"workflow-{run_id}"), ""
    log = getattr(state, "conversation_log", None)
    if log is None:
        return True
    if slot is None:
        return False
    await asyncio.to_thread(state.flush_slot_now, slot)
    card = _redact(_summarize(snapshot))
    return await asyncio.to_thread(_saved_with, log, slot_history_key(slot), card, prompt)


async def _until_wake_consumed(
    state: DashboardState,
    slot: Any,
    prompt: str,
    queue_id: str,
    consumption: _WakeConsumption,
    wake: Callable[[Any, Callable[[bool], None], Callable[[], None]], Optional[str]],
    dispatch: Callable[[Any, str], Awaitable[bool]],
) -> Optional[bool]:
    """Wait until the runner reports the wake queued as ``queue_id`` consumed.

    True once it has: at once for output or a tool call, which nothing retracts,
    and for a bare end of turn only once that turn has ended, since an empty reply
    retracts the report and re-queues the prompt before the turn ends. False when
    it left the queue with no turn (its user removed it), its turn ended unconsumed
    with no retry left (a Stop), or ``_WAKE_WAIT_SECS`` passed; a wake still queued
    then stays queued. None when the slot closed or was replaced, or the wake could
    not be queued again. A turn the CLI refused for sign-in leaves no retry, since
    only runner-authored kinds are re-queued then, so the wake is queued again and
    runs once the chat resumes.
    """
    import asyncio
    import time

    give_up = time.monotonic() + _WAKE_WAIT_SECS
    rows = _rows_carrying(slot, prompt)
    while True:
        if state.get_slot(slot.key) is not slot:
            return None
        if time.monotonic() >= give_up and not consumption.consumed:
            logger.info(
                "background command wake %s in %s not run after %ss; delivering the card",
                queue_id,
                slot.key,
                _WAKE_WAIT_SECS,
            )
            return False
        if consumption.irreversible:
            return True
        if not slot.running:
            if consumption.consumed:
                return True
            if _queued(slot, queue_id):
                await dispatch(slot, queue_id)
            elif _rows_carrying(slot, prompt) == rows:
                return False
            elif slot._last_turn_auth_required:
                if not any(prompt in str(entry.get("content") or "") for entry in slot._queue):
                    requeued = wake(slot, consumption.note, consumption.note_irreversible)
                    if requeued is None:
                        return None
                    queue_id, rows = requeued, _rows_carrying(slot, prompt)
            elif not any(entry.get("_on_consumed") is not None for entry in slot._queue):
                # A runner retry reports through the same callback; none is queued.
                return False
        await asyncio.sleep(_WAKE_POLL_SECS)


def _rows_carrying(slot: Any, prompt: str) -> int:
    """How many wake rows in ``slot`` carry ``prompt``."""
    return sum(
        1
        for m in getattr(slot, "messages", [])
        if m.get("role") == _WAKE_ROW_ROLE and prompt in str(m.get("content") or "")
    )


def _queued(slot: Any, queue_id: str) -> bool:
    # The live queue: its persisted projection stops at a cap, and a wake past it is still queued.
    return any(entry.get("id") == queue_id for entry in slot._queue)


def _saved_with(log: Any, key: str, card: str, prompt: str) -> bool:
    """Whether the transcript holds ``card`` and, when given, a wake row carrying ``prompt``.

    Blocking: it reads the transcript.
    """
    rows = log.read_messages(key)
    if not any(r.get("role") == "assistant" and r.get("content") == card for r in rows):
        return False
    return not prompt or any(
        r.get("role") == _WAKE_ROW_ROLE and prompt in str(r.get("content") or "") for r in rows
    )


async def inject_bound_workflow_result(
    state: DashboardState, run_id: str, snapshot: dict, *, on_injected=None
) -> bool:
    """Deliver using the run's captured identity without reselecting its memory."""
    import asyncio

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.execution_context import (
        bind_session_execution,
        execution_from_record,
        member_config_for_id,
        read_session_execution,
    )

    try:
        # The delivery appends to the originating tab's rows. A tab restored as a
        # sidebar row loads its window first, with the read off the loop; every
        # lookup below reads the slot again after this await.
        origin = snapshot.get("session_key", "")
        getter = getattr(state, "get_slot", None)
        if isinstance(origin, str) and origin and callable(getter):
            live = getter(_slot_key_from_session(origin))
            if live is not None:
                await load_window_off_loop(live)
        execution = execution_from_record(snapshot, required=False)
        if execution is None:
            if snapshot.get("memory_store") or snapshot.get("member_id"):
                return False
            return inject_workflow_result(state, run_id, snapshot, on_injected=on_injected)
        origin = snapshot.get("session_key", "")
        if not isinstance(origin, str) or not origin:
            return False
        # Capture the live target before awaiting config/metadata I/O. Recheck
        # that same slot after the await rather than selecting another member.
        slot = state.get_slot(_slot_key_from_session(origin))
        if slot is not None:
            admitted = await asyncio.to_thread(read_session_execution, origin)
            if (
                state.get_slot(slot.key) is not slot
                or effective_session_key(slot) != origin
                or admitted is None
                or admitted.store != execution.store
                or getattr(slot, "memory_store", "") != execution.store.legacy_name
            ):
                return False
            if getattr(slot, "memory_mode", "persistent") != execution.memory_mode:
                return False
        else:
            if execution.memory_mode != "persistent":
                return False
            agent = execution.template_id
            if execution.member_id is not None:
                config = await asyncio.to_thread(KiroCrewConfig.load)
                agent, _ = member_config_for_id(config, execution.member_id)
            fallback_name = f"workflow-{run_id}"
            slot = state.get_slot(fallback_name)
            if slot is not None and (
                getattr(slot, "linked_session_key", "") != origin
                or getattr(slot, "memory_store", "") != execution.store.legacy_name
                or getattr(slot, "memory_mode", "persistent") != execution.memory_mode
            ):
                return False
            # Bind the original identity when the parent is absent.
            # The new visible slot inherits it before any provider can start.
            await asyncio.to_thread(bind_session_execution, origin, execution)
            # The transcript's own turn time limit is read before a fresh slot
            # exists, so the slot never saves "follow the global" over it.
            limit_meta: dict = {}
            limit_readable = False
            log = getattr(state, "conversation_log", None)
            if slot is None and log is not None:
                try:
                    limit_meta, limit_readable = await asyncio.to_thread(
                        log.get_metadata_status, origin
                    )
                except Exception:  # noqa: BLE001 - an unread limit stays deferred below
                    limit_meta, limit_readable = {}, False
            if (
                state.get_slot(_slot_key_from_session(origin)) is not None
                or state.get_slot(fallback_name) is not slot
            ):
                return False
            if slot is None:
                slot = state.get_or_create_slot(
                    name=fallback_name,
                    agent=agent,
                    linked_session_key=origin,
                    memory_mode=execution.memory_mode,
                )
                slot.memory_store = execution.store.legacy_name
                _seed_turn_timeout(slot, limit_meta if limit_readable else None)
            on_injected = None
        delivered = dict(snapshot)
        delivered["memory_mode"] = execution.memory_mode
        return inject_workflow_result(state, run_id, delivered, on_injected=on_injected)
    except (OSError, ValueError, RuntimeError):
        return False
