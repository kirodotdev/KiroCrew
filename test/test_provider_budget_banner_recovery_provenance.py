"""Provider-budget-banner recovery provenance, retry ownership and stop hooks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers


class TestProviderBudgetBannerRecoveryProvenance(BannerRecoveryHelpers):
    """Recovery and transient-retry rows keep their provenance, owner and visible tail."""

    @pytest.mark.parametrize("approval_mode", ["yolo", "session-trust"])
    @pytest.mark.asyncio
    async def test_banner_recovery_queues_under_auto_approve_like_the_transient_retry(
        self, tmp_path, monkeypatch, approval_mode
    ):
        """The approval mode does not gate the banner recovery.

        A banner-only turn gave no answer; its continuation re-drives the same
        request a post-token 5xx retry re-drives, under the same modes. YOLO and
        session trust therefore queue it and it runs, with no notice asking for a
        manual Continue.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the requested change.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="write_file",
                    tool_kind="write",
                    tool_call_id="tc-banner-under-trust",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Change applied.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        if approval_mode == "yolo":
            state._yolo = True
        else:
            slot._trust = True
        changed = tmp_path / "adversarial.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "Apply the change from the design note")
        await self._drain_bg(state)

        assert calls == 2
        assert slot._posttoken_retry_used is True
        assert not slot._queue
        assert self._assistant_texts(slot) == ["Applying the requested change.", "Change applied."]
        assert not any(
            "Auto-continue" in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "notice"
        )

    @pytest.mark.asyncio
    async def test_banner_recovery_survives_an_unrelated_prior_turn_stop(
        self, tmp_path, monkeypatch
    ):
        """With the stop counter elevated by an EARLIER turn's Stop and no
        intervention since the recovery was enqueued, the recovery must still run.
        Proves the enqueue-site stop-gen snapshot: without it the drain would read
        a stale zero, spuriously purge, and drop a legitimate recovery."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        captured: list[str] = []

        async def _stream(message):
            captured.append(message)
            if len(captured) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the fix.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="write_file",
                    tool_kind="write",
                    tool_call_id="tc-1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Finished safely.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # A Stop from an EARLIER, unrelated turn left the monotonic counter high.
        slot._stop_generation = 5
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "fix the bug")
        await self._drain_bg(state)

        # No Stop or user input since the recovery was enqueued -> it runs.
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert any("Finished safely." in text for text in self._assistant_texts(slot))

    @pytest.mark.asyncio
    async def test_prestream_retry_keeps_provider_owner_when_trust_changes_during_backoff(
        self, tmp_path, monkeypatch
    ):
        """The requeue of a provider recovery keeps its owner tag through the
        backoff, and a trust grant during the wait changes nothing about its
        dispatch: the approval mode does not gate the banner recovery."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                raise AcpError("transient provider failure", transient=True)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resumed under trust.")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("provider-owner-prestream-retry")
        slot._titled = True
        slot._posttoken_retry_used = True
        observed_owner: list[bool] = []
        start_next = chat_runner._start_next_queued_turn

        async def _grant_trust(_delay):
            slot._trust = True

        async def _capture_then_drain(*args, **kwargs):
            observed_owner.append(
                any(
                    has_recovery_provenance(item, RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
                    for item in slot._queue
                )
            )
            return await start_next(*args, **kwargs)

        with (
            patch.object(chat_runner, "_recovery_delay", _grant_trust),
            patch.object(chat_runner, "_start_next_queued_turn", _capture_then_drain),
        ):
            await _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _synthetic_recovery_turn=True,
                _current_message=self._provider_recovery_row(),
            )
            await self._drain_bg(state)

        assert observed_owner == [True]
        assert len(prompts) == 2
        assert slot._queue == []
        assert "Resumed under trust." in self._assistant_texts(slot)
        assert not any(
            "auto-approve" in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "notice"
        )

    @pytest.mark.asyncio
    async def test_post_token_transient_retry_mints_its_owner_and_keeps_the_tail(
        self, tmp_path, monkeypatch
    ):
        """The transient owner is reachable from the production retry, not only
        from a hand-built row. A turn streams output and then hits a transient
        5xx. The post-token one-shot queues the CONTINUE under
        ``RecoveryProvenance.TRANSIENT_RETRY``, the drain copies the tag onto the
        replayed row, and the retry turn it owns keeps a banner-shaped tail after
        visible output, where an ordinary turn strips that tail and queues a
        recovery. Nothing in this test writes the tag by hand."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        answer = "You have 8154 weighted tokens left"
        calls = 0
        captured: list = []

        async def _no_backoff(_delay):
            return None

        async def _stream(message):
            nonlocal calls
            calls += 1
            captured.append(message)
            if calls == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Partial work.")
                raise AcpError(
                    "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
                )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resumed work.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-posttoken-retry",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answer)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("posttoken-transient-owner")
        slot._titled = True

        with patch.object(chat_runner, "_recovery_delay", _no_backoff):
            await _run_chat(state, slot, "Continue the report")
            await self._drain_bg(state)

        # The production one-shot re-prompted the live session exactly once with
        # the fixed continuation (its marker line is neutralised on the way out,
        # so the instruction body is what the client sees).
        assert calls == 2
        assert "Continue from where it stopped" in captured[1]
        assert "Continue the report" not in captured[1]
        injected = [m for m in slot.messages if m.get("role") == "inject"]
        assert [m.get("content") for m in injected] == [_POSTTOKEN_RECOVER_MSG]
        # The replayed row carries the owner the retry path minted, and not the
        # provider-budget owner that shares its text.
        assert has_recovery_provenance(injected[0], RecoveryProvenance.TRANSIENT_RETRY)
        assert not has_recovery_provenance(injected[0], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
        # The owned retry keeps its banner-shaped tail; nothing was stripped and no
        # recovery was queued for it.
        assert self._assistant_texts(slot) == ["Partial work.", "Resumed work.", answer]
        assert not slot._queue
        assert not any(
            "internal status instead of an answer" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        )
        assert slot._posttoken_retry_used is True

    @pytest.mark.asyncio
    async def test_banner_recovery_precedes_stop_hook_without_replacing_visible_variant(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.dashboard.state import HOOK_CONTINUATION_RECOVERY_PREFIX
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the regenerated answer.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-regenerate-artifact",
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text="You have 8154 weighted tokens left",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        state._hook_store = MagicMock()
        state._hook_store.fire = AsyncMock(
            return_value=[
                SimpleNamespace(
                    exit_code=0,
                    stdout='{"decision":"block","reason":"run the gate"}',
                    stderr="",
                    hook_name="stop-gate",
                )
            ]
        )
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("banner-stop-hook")
        slot._titled = True
        slot._pending_variants = [{"content": "prior answer", "ts": "old-ts"}]
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        # Keep the queue observable instead of letting the finally drain dispatch it.
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "regenerate the answer")

        queued = [item["content"] for item in slot._queue]
        assert queued[0] == _POSTTOKEN_RECOVER_MSG
        assert queued[1] == f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nrun the gate"
        assert has_recovery_provenance(slot._queue[0], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
        assert not has_recovery_provenance(
            slot._queue[1], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT
        )
        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message["content"] for message in assistant] == ["Applying the regenerated answer."]
        assert [variant["content"] for variant in assistant[0]["variants"]] == [
            "prior answer",
            "Applying the regenerated answer.",
        ]
