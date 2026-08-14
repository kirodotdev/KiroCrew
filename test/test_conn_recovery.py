"""Tests for connection-loss auto-recovery requeue behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import (
    _BUSY_RECOVER_MSG,
    _CONN_RECOVER_MSG,
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
    ResetCause,
    build_recovery_requeue,
    is_synthetic_payload_item,
    is_system_injection_item,
    payload_for_replay,
)
from kiro_crew.dashboard.state import (
    BUSY_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_PREFIX,
)


def test_requeue_after_emitted_turn_continues_without_replaying_original() -> None:
    original = "Build and deploy the service"

    requeued, payload = build_recovery_requeue(
        original, turn_emitted=True, cause=ResetCause.CONNECTION_LOST, message_is_synthetic=False
    )

    assert requeued == _CONN_RECOVER_MSG
    assert payload is RecoveryPayload.CONTINUATION
    assert requeued.startswith(CONN_RECOVERY_PREFIX)
    assert original not in requeued
    assert "continue" in requeued.lower()
    assert "do not restart" in requeued.lower()


def test_busy_reset_continues_without_claiming_the_connection_was_lost() -> None:
    """The marker labels the row, so it must name the cause that actually fired.

    Requeuing a continuation is correct for either cause -- replaying the request
    can repeat side effects whichever way the session died -- but a session that
    was merely busy was never disconnected, and the transcript renders whichever
    marker the continuation carries.
    """
    original = "Build and deploy the service"

    requeued, payload = build_recovery_requeue(
        original, turn_emitted=True, cause=ResetCause.SESSION_BUSY, message_is_synthetic=False
    )

    assert requeued == _BUSY_RECOVER_MSG
    assert payload is RecoveryPayload.CONTINUATION
    assert requeued.startswith(BUSY_RECOVERY_PREFIX)
    assert not requeued.startswith(CONN_RECOVERY_PREFIX)
    assert "connection" not in requeued.lower()
    assert original not in requeued
    assert "continue" in requeued.lower()
    assert "do not restart" in requeued.lower()


@pytest.mark.parametrize("cause", list(ResetCause))
def test_requeue_before_output_preserves_original_request(cause: ResetCause) -> None:
    original = "Build and deploy the service"

    assert build_recovery_requeue(
        original, turn_emitted=False, cause=cause, message_is_synthetic=False
    ) == (original, RecoveryPayload.ORIGINAL)


def test_connection_recovery_uses_structural_system_injection_provenance() -> None:
    item = {
        "content": _CONN_RECOVER_MSG,
        "kind": SYNTHETIC_RECOVERY_KIND,
    }

    assert is_system_injection_item(item) is True
    assert is_system_injection_item({"content": _CONN_RECOVER_MSG}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_recovery", "expected_role"),
    [
        (SYNTHETIC_RECOVERY_KIND, True, "inject"),
        ("", False, "user"),
    ],
)
async def test_queue_dispatch_preserves_recovery_provenance(
    tmp_path, monkeypatch, kind: str, expected_recovery: bool, expected_role: str
) -> None:
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    state.subagents = None
    slot = state.get_or_create_slot("recovery-dispatch")
    slot.queue_insert(0, _CONN_RECOVER_MSG, kind=kind)
    mock_run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", mock_run)

    assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert slot.task is not None
    await slot.task

    mock_run.assert_awaited_once_with(
        state,
        slot,
        _CONN_RECOVER_MSG,
        _synthetic_payload=expected_recovery,
    )
    assert slot.messages[-1]["role"] == expected_role


@pytest.mark.asyncio
async def test_cron_injection_preserves_pending_session_reset_notice(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    state.subagents = None
    slot = state.get_or_create_slot("cron-during-reset")
    slot._stopping = True
    cron_message = f'{CRON_NOTIFY_PREFIX}"daily"]\nrun report'
    slot.queue_insert(0, cron_message)
    mock_run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", mock_run)

    assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert slot.task is not None
    await slot.task

    assert slot._stopping is True
    assert not any(
        "Session reset — processing next message" in message.get("content", "")
        for message in slot.messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_synthetic"),
    [
        (RecoveryPayload.CONTINUATION, True),
        (RecoveryPayload.ORIGINAL, False),
    ],
)
async def test_dispatch_classifies_the_payload_not_the_recovery(
    tmp_path, monkeypatch, payload: str, expected_synthetic: bool
) -> None:
    """Whether the runner may mirror the text follows the PAYLOAD, not recovery-ness.

    ``build_recovery_requeue`` returns a runner-authored continuation once the turn
    emitted output, and the user's own message before that. Both are re-queued under
    the same recovery kind, because both must render as an inject row rather than a
    duplicate user bubble. Only the first is machine speech, so only the first may be
    withheld from a linked Slack/Telegram thread -- withholding the second hides a
    question the user really asked.

    Both cases carry the SAME text on purpose: classification must come from the
    entry's tag, so identical content classifying two ways is the point.
    """
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    state.subagents = None
    slot = state.get_or_create_slot("payload-classification")
    slot.queue_insert(
        0, "Build and deploy the service", kind=SYNTHETIC_RECOVERY_KIND, payload=payload
    )
    mock_run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", mock_run)

    assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert slot.task is not None
    await slot.task

    mock_run.assert_awaited_once_with(
        state,
        slot,
        "Build and deploy the service",
        _synthetic_payload=expected_synthetic,
    )
    # Provenance is unchanged by the split: either payload still renders as an
    # inject row, which is what stops the duplicate user bubble.
    assert slot.messages[-1]["role"] == "inject"


def test_untagged_recovery_entry_is_treated_as_machine_speech() -> None:
    """An untagged recovery entry falls back to the kind, erring toward suppression.

    A requeue site that omits the payload tag leaves the classification unknown. The
    two errors are not symmetric: mirroring runner text as if the user typed it
    misattributes machine orchestration, while withholding a mirror only loses an
    echo. Absent a tag, treat a recovery entry as machine speech.
    """
    assert is_synthetic_payload_item({"kind": SYNTHETIC_RECOVERY_KIND}) is True
    assert is_synthetic_payload_item({"kind": ""}) is False


@pytest.mark.parametrize("cause", list(ResetCause))
def test_a_second_failure_before_output_keeps_the_runners_text_machine_authored(
    cause: ResetCause,
) -> None:
    """A recovery turn that dies before emitting replays the RUNNER's text, not the user's.

    ``turn_emitted`` alone cannot say whose words these are: the replay branch returns
    ``message`` unchanged, and on a recovery turn that message is the continuation the
    runner wrote for the previous failure. Labelling it ORIGINAL sends internal
    orchestration to a linked thread as user speech.
    """
    text, payload = build_recovery_requeue(
        _CONN_RECOVER_MSG, turn_emitted=False, cause=cause, message_is_synthetic=True
    )

    assert text == _CONN_RECOVER_MSG
    assert payload is RecoveryPayload.CONTINUATION


def test_replayed_user_request_is_still_attributed_to_the_user() -> None:
    """The complement: over-correcting would hide a question the user really asked."""
    assert payload_for_replay(False) is RecoveryPayload.ORIGINAL
    assert payload_for_replay(True) is RecoveryPayload.CONTINUATION
