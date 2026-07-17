"""Inject a finished workflow run's result back into the originating chat (M6.4).

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

from kiro_crew.dashboard.state import DashboardState
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
    lines.append(
        f"\nUse workflow_result('{run_id}') for the full event stream, or "
        f"workflow_rerun_subtree('{run_id}', …) to restart from a step."
    )
    return "\n".join(lines)


def _slot_key_from_session(session_key: str) -> str:
    """Map an originating session_key to the dashboard slot key it came from.

    Chat-launched runs carry session_key ``dashboard:<slotKey>`` (the slot's
    history key — see ``_history_key_for``). The live slot is stored under the bare
    ``<slotKey>``, so strip the ``dashboard:``/``dashboard_`` prefix to find it.
    """
    sk = session_key
    if sk.startswith("dashboard:"):
        sk = sk[len("dashboard:"):]
    while sk.startswith("dashboard_"):
        sk = sk[len("dashboard_"):]
    return sk


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
            slot.append("assistant", msg, "msg msg-a")
            # Live: surface it in the open chat without a reload (mirrors how a
            # normal assistant turn is pushed). slot.append already broadcasts via
            # _pending flush, but an explicit chat_message guarantees the live UI
            # renders it into the originating conversation immediately.
            try:
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "assistant", "content": msg, "kind": "workflow_result"},
                )
            except Exception:  # noqa: BLE001
                pass
            # Persist so a follow-up chat turn has the result as context.
            try:
                if state.conversation_log is not None:
                    state.conversation_log.append(session_key, "assistant", msg)
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
