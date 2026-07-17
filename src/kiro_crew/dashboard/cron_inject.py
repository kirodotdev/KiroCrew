"""Cron result injection into dashboard chat slots.

Extracted from handlers/cron.py to break the circular import between
gateway.py and dashboard.handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.cron import CronJob


def inject_cron_result_to_dashboard(
    state: DashboardState, job: "CronJob", result_text: str,
    history: list[dict[str, Any]] | None = None,
) -> None:
    """Inject cron result into linked dashboard chat slot (shared by to-chat and auto-inject)."""
    slot_name = f"cron-{job.id}"
    slot = state.get_or_create_slot(name=slot_name, agent=job.agent_id or "")
    safe_name, _ = redact_exfiltration_urls(job.name)
    safe_name, _ = redact_credentials(safe_name)
    slot.title = f"Cron: {safe_name}"
    if not slot.linked_session_key:
        slot.linked_session_key = f"cron:{job.id}"
        if history is None:
            messages = state.conversation_log.read_messages(f"cron:{job.id}") if state.conversation_log else []
        else:
            messages = history
        hydrate_slot_from_history(slot, messages)
    if result_text:
        safe_result, _ = redact_exfiltration_urls(result_text)
        safe_result, _ = redact_credentials(safe_result)
        context = f"# Cron Job Result: {safe_name}\n\n{safe_result}"
        if not any(msg.get("content") == context for msg in slot.messages):
            slot.append("assistant", context, "msg msg-a")
            # Persist the result to the canonical ConversationLog under the
            # linked session key so a dashboard follow-up turn has it as
            # context. The cron execution path (gateway stream_and_collect)
            # streams text into job.last_result but never writes the dashboard
            # conversation_log, and slot.append only updates the in-memory
            # slot. Without this, chat_runner.build_session_replay reads an
            # empty cron:{id} log and the follow-up agent opens with no memory
            # of the result the user is looking at. Writing to the stable
            # linked key (cron:{id}) fixes both persistent and stateless crons
            # (the slot always links to cron:{id} regardless of the per-run
            # execution key).
            log_key = f"cron:{job.id}"
            if state.conversation_log is not None:
                existing = state.conversation_log.read_messages(log_key)
                if not any(m.get("content") == context for m in existing):
                    state.conversation_log.append(
                        log_key, "assistant", context, agent=job.agent_id or None
                    )
    state.push_slots_update()


def hydrate_slot_from_history(slot: Any, messages: list[dict[str, Any]]) -> None:
    """Load last 50 messages from pre-loaded history into a new slot."""
    for msg in messages[-50:]:
        role = msg.get("role", "assistant")
        content = msg.get("content", "")
        if not content:
            continue
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
        if any(m.get("content") == content for m in slot.messages):
            continue
        slot.append(role, content, f"msg msg-{'a' if role == 'assistant' else 'u'}", broadcast=False)
