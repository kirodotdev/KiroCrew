"""Approval and question lifecycles behind the DashboardState facade."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

_Redactor = Callable[[str], tuple[str, object]]


def _redact(text: object, redact_url: _Redactor, redact_secret: _Redactor) -> str:
    value, _ = redact_url(str(text or ""))
    value, _ = redact_secret(value)
    return value


class ApprovalCoordinator:
    """Own registration, waiting, auditing, and resolution of approvals."""

    @staticmethod
    async def request(
        state: Any,
        approval_id: str,
        source: str,
        tool: str,
        *,
        tool_input: str,
        tool_purpose: str,
        slot: str,
        is_background: bool,
        redact_url: _Redactor,
        redact_secret: _Redactor,
    ) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        state._approval_futures[approval_id] = future
        state._pending_approvals[approval_id] = {
            "id": approval_id,
            "source": source,
            "tool": _redact(tool, redact_url, redact_secret),
            "tool_input": _redact(tool_input, redact_url, redact_secret),
            "tool_purpose": _redact(tool_purpose, redact_url, redact_secret),
            "slot": slot,
            "ts": time.time(),
        }
        state.broadcast_ws("approval", state._pending_approvals[approval_id])
        timeout = (
            state._BACKGROUND_APPROVAL_TIMEOUT_SECS if is_background else state._APPROVAL_TIMEOUT
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            state._pending_approvals.pop(approval_id, None)
            state._approval_futures.pop(approval_id, None)

    @staticmethod
    def audit_and_broadcast(
        state: Any,
        session_key: str,
        approval_id: str,
        approved: bool,
        decision: str,
        *,
        audit_provider: Callable[[], Any],
    ) -> None:
        try:
            audit_provider().log_tool_invocation(
                session_key=session_key,
                tool_name="approval_decision",
                outcome=decision or ("approved" if approved else "rejected"),
                request_id=approval_id,
                source="dashboard",
            )
        except Exception:
            state._log.warning("SEL audit failed for approval resolution", exc_info=True)
        try:
            payload: dict = {"id": approval_id, "approved": approved}
            if session_key and session_key != "state":
                payload["slot"] = session_key
            state.broadcast_ws("approval_resolved", payload)
        except Exception:
            state._log.warning("WS broadcast failed for approval resolution", exc_info=True)

    @staticmethod
    def resolve_state(state: Any, approval_id: str, approved: bool) -> bool:
        future = state._approval_futures.get(approval_id)
        if future and not future.done():
            future.set_result(approved)
            state._audit_and_broadcast_approval("state", approval_id, approved)
            return True
        return False

    @staticmethod
    def resolve(
        state: Any,
        approval_id: str,
        approved: bool,
        *,
        rejected_once: bool,
        permission_marker: Callable[[list[dict], str, str], bool],
    ) -> bool:
        if approved:
            decision = "approved"
        elif rejected_once:
            decision = "rejected_once"
        else:
            decision = "rejected"
        if state.resolve_state_approval(approval_id, approved):
            if rejected_once:
                state._log.warning(
                    "approval %s resolved at state level; decision %r downgraded to rejected",
                    approval_id,
                    decision,
                )
            return True
        for slot in state._slots.values():
            future = slot._approval_futures.get(approval_id)
            if future and not future.done():
                future.set_result(decision)
                if permission_marker(slot.messages, approval_id, decision):
                    # The periodic flush skips clean slots; the resolved marker
                    # must become durable before its future disappears.
                    slot._dirty = True
                state._audit_and_broadcast_approval(slot.key, approval_id, approved, decision)
                state.push_slots_update()
                return True
        return False


class QuestionCoordinator:
    """Own stateless cards and legacy blocking question futures."""

    @staticmethod
    def redact_questions(
        questions: list[dict],
        *,
        redact_url: _Redactor,
        redact_secret: _Redactor,
    ) -> list[dict]:
        safe_questions: list[dict] = []
        seen_questions: set[str] = set()
        for question in questions:
            safe_question = dict(question)
            for field in ("question", "header"):
                safe_question[field] = _redact(safe_question.get(field), redact_url, redact_secret)
            normalized = " ".join(str(safe_question.get("question") or "").split()).casefold()
            if normalized in seen_questions:
                raise ValueError(
                    "questions collapse to identical text after redaction; "
                    "rephrase so each question is distinguishable"
                )
            seen_questions.add(normalized)

            safe_options: list[dict] = []
            seen_labels: set[str] = set()
            for option in safe_question.get("options") or []:
                safe_option = dict(option)
                for field in ("label", "description"):
                    safe_option[field] = _redact(safe_option.get(field), redact_url, redact_secret)
                normalized_label = " ".join(str(safe_option.get("label") or "").split()).casefold()
                if normalized_label in seen_labels:
                    raise ValueError(
                        "option labels collapse to identical text after redaction; "
                        "rephrase so every option is distinguishable"
                    )
                seen_labels.add(normalized_label)
                safe_options.append(safe_option)
            safe_question["options"] = safe_options
            safe_questions.append(safe_question)
        return safe_questions

    @staticmethod
    async def post_card(state: Any, slot_key: str, questions: list[dict]) -> int:
        safe_questions = state._redact_questions(questions)
        card_id = f"card-{uuid.uuid4().hex[:16]}"
        # Register before an await so a user row racing a backpressured socket
        # can retire the card instead of leaving needs_input stuck afterward.
        state.mark_question_pending(
            slot_key,
            blocking=False,
            card_id=card_id,
            questions=safe_questions,
        )
        payload = {
            "slot": slot_key,
            "card_id": card_id,
            "questions": safe_questions,
            "ts": time.time(),
        }
        return int(await state.deliver_ws_owners("question_card", payload))

    @staticmethod
    def mark_pending(
        state: Any,
        slot_key: str,
        *,
        blocking: bool,
        card_id: str,
        questions: list[dict] | None,
    ) -> None:
        slot = state._slots.get(slot_key)
        if slot is None or not card_id:
            return
        if not blocking:
            # The UI renders one stateless card per slot, so a replacement must
            # retire the old stateless record while preserving blocking asks.
            for existing_id, record in list(slot._question_pending.items()):
                if not record.get("blocking"):
                    slot._question_pending.pop(existing_id, None)
        entry: dict = {"ts": time.time(), "blocking": blocking}
        if questions is not None:
            entry["questions"] = questions
        slot._question_pending[card_id] = entry
        state._push_slots()

    @staticmethod
    def clear_pending(
        state: Any,
        slot_key: str,
        *,
        blocking: bool | None,
        card_id: str | None,
    ) -> bool:
        slot = state._slots.get(slot_key)
        if slot is None or not slot._question_pending:
            return False
        retired = [
            current_id
            for current_id, record in slot._question_pending.items()
            if (card_id is None or current_id == card_id)
            and (blocking is None or bool(record.get("blocking")) == blocking)
        ]
        if not retired:
            return False
        for current_id in retired:
            slot._question_pending.pop(current_id, None)
        state._broadcast_question_retired(slot_key, retired)
        state._push_slots()
        return True

    @staticmethod
    def broadcast_retired(state: Any, slot_key: str, card_ids: list[str]) -> None:
        for card_id in card_ids:
            if not card_id:
                continue
            try:
                state.broadcast_ws_owners(
                    "question_card_resolved",
                    {"card_id": card_id, "slot": slot_key},
                )
            except Exception:
                state._log.warning("WS broadcast failed for card retirement", exc_info=True)

    @staticmethod
    def push_slots(state: Any) -> None:
        try:
            state.push_slots_update()
        except Exception:
            state._log.debug("push_slots_update failed after question status change", exc_info=True)

    @staticmethod
    async def request(
        state: Any,
        ask_id: str,
        slot_key: str,
        questions: list[dict],
        timeout: int | None,
    ) -> dict[str, str] | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str] | None] = loop.create_future()
        safe_questions = state._redact_questions(questions)
        payload = {
            "ask_id": ask_id,
            "slot": slot_key,
            "questions": safe_questions,
            "ts": time.time(),
        }
        state._pending_questions[ask_id] = payload
        state._question_futures[ask_id] = future
        state.mark_question_pending(slot_key, blocking=True, card_id=ask_id)
        state.broadcast_ws_owners("question_card", payload)

        window = timeout if timeout is not None else state._QUESTION_TIMEOUT_DEFAULT
        window = max(1, min(int(window), state._QUESTION_TIMEOUT_MAX))
        try:
            return await asyncio.wait_for(future, timeout=window)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            state._pending_questions.pop(ask_id, None)
            state._question_futures.pop(ask_id, None)
            state.clear_question_pending(slot_key, blocking=True, card_id=ask_id)
            try:
                state.broadcast_ws_owners("question_card_resolved", {"ask_id": ask_id})
            except Exception:
                state._log.warning("WS broadcast failed for question resolution", exc_info=True)

    @staticmethod
    def resolve(state: Any, ask_id: str, answers: dict[str, str] | None) -> bool:
        future = state._question_futures.get(ask_id)
        if future is None or future.done():
            return False
        future.set_result(answers)
        return True

    @staticmethod
    def cancel_for_slot(state: Any, slot_key: str) -> int:
        pending_ids = [
            ask_id
            for ask_id, payload in state._pending_questions.items()
            if payload.get("slot") == slot_key
        ]
        cancelled = 0
        for ask_id in pending_ids:
            if state.resolve_question(ask_id, None):
                cancelled += 1
        return cancelled
