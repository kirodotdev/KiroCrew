"""ACP session compaction, kept separate from ACP framing and dispatch.

Owns summary request, interruption handling, lifecycle signals, and durable
session update. The ACP host still owns session intent and permissions.
"""
from __future__ import annotations

import time
from typing import Any

from .acp_session_state import Session
from .config import ConfigError, ModelSpec, ProviderSpec
from .providers import ProviderError, ProviderInterrupted, chat_completion_turn
from .reasoning_control import resolve_thinking_mode
from .session_events import compact_to_summary


class SessionCompactionMixin:
    @staticmethod
    def _compact_context_suffix(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("/compact"):
            return ""
        return stripped[len("/compact") :].strip()

    def _compact_session(
        self,
        session: Session,
        provider: ProviderSpec,
        model: ModelSpec,
        preserve_context: str,
    ) -> str:
        """Summarize and replace this session's model transcript.

        The operation is provider-native but tool-free.  It observes the same
        cancel event as an ordinary turn so ``session/cancel`` can abort an
        in-flight manual compaction instead of waiting for the upstream HTTP
        stream to finish.  The returned summary is useful only for lifecycle
        diagnostics; the durable source of truth is the append-only session event log.
        """
        if not session.messages:
            return ""

        transcript_parts: list[str] = []
        for message in session.messages:
            role = str(message.get("role") or "unknown")
            raw_content = message.get("content")
            if isinstance(raw_content, list):
                chunks: list[str] = []
                for block in raw_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunks.append(str(block.get("text") or ""))
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        chunks.append("[image attached]")
                content = " ".join(chunks)
            else:
                content = str(raw_content or "")
            transcript_parts.append(f"[{role}]\n{content}")
        transcript = "\n\n".join(transcript_parts)

        instruction = (
            "Summarize the conversation for future continuation. Preserve concrete facts, "
            "user preferences, decisions, unresolved tasks, important code/file names, and "
            "constraints. Remove repetition and conversational filler. Do not invent facts. "
            "Return only the compacted summary, with enough detail that another instance of "
            "the same model can continue the work."
        )
        if preserve_context:
            keep = preserve_context[:4000]
            instruction += f"\nThe host additionally asked that this context be preserved:\n{keep}"

        compact_messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": transcript},
        ]
        compact_thinking = resolve_thinking_mode(model, getattr(model, "thinking_preference", "auto"), "")
        compact_effort = "" if compact_thinking == "off" else session.effort
        turn = chat_completion_turn(
            provider,
            model.model_id,
            compact_messages,
            lambda _chunk: None,
            reasoning_effort=self._reasoning_wire_value(model, compact_thinking, compact_effort) or None,
            thinking_mode=compact_thinking,
            model_spec=model,
            cache_key=session.session_id + ":compact",
            tools=None,
            should_interrupt=session.cancel_event.is_set,
        )
        summary = turn.text.strip()
        if not summary:
            raise ProviderError("compaction returned an empty summary")

        with session.state_lock:
            compact_to_summary(
                session,
                {
                    "role": "system",
                    "content": "Compacted conversation summary:\n" + summary,
                },
            )
            session.updated_at = time.time()
        return summary

    def _emit_summarization_info(
        self,
        session: Session,
        kind: str,
        *,
        summary: str = "",
        detail: str = "",
    ) -> None:
        """Emit the CLI V3 replacement for legacy compaction-status frames."""
        kiro: dict[str, Any] = {"kind": kind}
        if summary:
            # Keep lifecycle updates bounded. The full durable summary remains in
            # the session store and is replayed through normal session history.
            kiro["summary"] = summary[:4000]
        if detail:
            kiro["detail"] = detail[:2000]
        self.notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "session_info_update",
                    "_meta": {"kiro": kiro},
                },
            },
        )

    def _run_manual_compaction(
        self,
        session: Session,
        *,
        preserve_context: str = "",
    ) -> bool:
        """Run one manual compaction while owning the session turn lock.

        Current Kiro V3's typed extension reports ``success: false`` when a
        session is already busy/compacting. Unknown sessions are rejected by the
        RPC handler before reaching here.
        """
        if not session.turn_lock.acquire(blocking=False):
            return False
        try:
            session.cancel_event.clear()
            config = self._config()
            resolved = self._resolved_model(config, session)
            if resolved is None:
                self._emit_summarization_info(session, "summarization_started")
                self._emit_summarization_info(
                    session, "summarization_failed",
                    detail=f"model {session.model!r} is no longer configured",
                )
                return False
            provider, model = resolved
            summary, _detail = self._run_compaction(session, provider, model, preserve_context)
            return summary is not None
        finally:
            session.turn_lock.release()

    def _run_compaction(
        self,
        session: Session,
        provider: ProviderSpec,
        model: ModelSpec,
        preserve_context: str,
    ) -> tuple[str | None, str]:
        """Run the shared compaction body and emit the summarization lifecycle.

        Both manual-compaction entry points (the legacy ``/compact`` prompt and
        the typed ``_kiro/session/compact`` extension) used to inline the same
        started -> compact -> persist -> completed/failed sequence with slightly
        different failure ``detail`` strings. The sequence lives here once so the
        two callers only differ in how they report their own result (JSON-RPC
        response vs. boolean), not in compaction behaviour or failure wording.

        Returns ``(summary, "")`` on success, or ``(None, detail)`` on failure
        where ``detail`` is ``"cancelled"`` for an interruption or the error text
        otherwise.
        """
        self._emit_summarization_info(session, "summarization_started")
        try:
            summary = self._compact_session(session, provider, model, preserve_context)
        except ProviderInterrupted:
            self._emit_summarization_info(session, "summarization_failed", detail="cancelled")
            return None, "cancelled"
        except (ConfigError, ProviderError) as exc:
            self._emit_summarization_info(session, "summarization_failed", detail=str(exc))
            return None, str(exc)
        self._persist_session(session)
        self._emit_summarization_info(session, "summarization_completed", summary=summary)
        return summary, ""

