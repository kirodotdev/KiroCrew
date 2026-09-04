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
import re
from typing import Any, Callable, Optional

from kiro_crew.dashboard.chat_utils import dashboard_slot_key
from kiro_crew.dashboard.state import DashboardState, append_and_surface, row_mid
from kiro_crew.history import append_if_absent_off_loop
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

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


def _summarize(snapshot: dict) -> str:
    """Build the chat message body from a terminal run snapshot."""
    name = snapshot.get("name") or snapshot.get("run_id", "")
    status = snapshot.get("status", "")
    run_id = snapshot.get("run_id", "")
    lines = ["[Workflow completion event]", f"Workflow `{name}` ({run_id}) → **{status}**"]
    if status == "finished":
        result = snapshot.get("result")
        try:
            body = json.dumps(result, indent=2, default=str)
        except Exception:  # noqa: BLE001
            body = str(result)
        lines.append("\nResult:\n```json\n" + body[:4000] + "\n```")
        # Surface any artifact file paths the run produced, so the chat agent can
        # open/act on them directly instead of digging through the result blob.
        artifacts: list[str] = []
        _collect_artifact_paths(result, artifacts)
        if artifacts:
            lines.append("\nArtifacts (open with your file tools):")
            lines.extend(f"- `{p}`" for p in artifacts[:20])
            if len(artifacts) > 20:
                lines.append(f"- … and {len(artifacts) - 20} more")
    elif snapshot.get("error"):
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
    if error_count:
        lines.append(f"{error_count} agent call(s) failed; each reason is under `agent_errors`.")
    lines.append(
        f"\nUse workflow_result('{run_id}') for the full event stream, or "
        f"workflow_rerun_subtree('{run_id}', …) to restart from a step."
    )
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
            slot = state.get_or_create_slot(name=f"workflow-{run_id}")
            if not getattr(slot, "linked_session_key", ""):
                slot.linked_session_key = session_key
            slot.title = f"Workflow: {snapshot.get('name') or run_id}"

        # Dedup: don't double-inject the same result on a re-fire.
        already = any(m.get("content") == msg for m in getattr(slot, "messages", []))
        if not already:
            # Carry the window copy's minted ``meta.mid`` (read off the append's
            # return via ``row_mid``) onto the durable copy below: one logical
            # message, one identity, so the bounded-read identity walk
            # recognises the persisted row instead of re-appending the
            # injection. append_and_surface delivers the live copy through
            # exactly one identity-carrying door — the old unconditional
            # explicit frame here carried no ``meta.mid``, so the client
            # rendered the same result twice whenever append's own broadcast
            # also fired (#5981 family).
            window_mid = row_mid(
                append_and_surface(
                    state,
                    slot,
                    "assistant",
                    msg,
                    "msg msg-a",
                    extra={"kind": "workflow_result"},
                )
            )
            # Persist so a follow-up chat turn has the result as context.
            try:
                if state.conversation_log is not None:
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
