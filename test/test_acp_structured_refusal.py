"""Structured model-side refusals: one ``RefusalInfo`` for every harness.

The Kiro service declines a turn on the ``_kiro.dev/metadata`` channel
(``stopReason: CONTENT_FILTERED`` + a ``refusal`` object) while the terminal
reads ``end_turn``; Anthropic's adapter says only ``stopReason: "refusal"``.
These tests pin that both are folded onto ``STOP_REASON_REFUSAL`` with a
``RefusalInfo`` attached, that the parser is opt-in by capability set, that
provider text is redacted, and that the dashboard card renders exactly the
fields the provider filled.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch import parse_metadata, parse_refusal
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    STOP_REASON_CONTENT_FILTERED_WIRE,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    AcpPromptStats,
    RefusalInfo,
)
from kiro_crew.dashboard.chat_runner import refusal_card_text

CANNED = (
    "The selected model cannot continue this conversation. Please select a different "
    "model, or start a new conversation, or rewind the current conversation to an "
    "earlier point and try a different approach."
)

INCIDENT_FRAME = {
    "sessionId": "sess-1",
    "stopReason": STOP_REASON_CONTENT_FILTERED_WIRE,
    "refusal": {"category": "CYBER", "explanation": CANNED, "recommendedModel": None},
}


@pytest.fixture(autouse=True)
def _clear_reported_fields():
    _dispatch._reported_metadata_fields.clear()
    yield
    _dispatch._reported_metadata_fields.clear()


class TestCapabilitySet:
    def test_kiro_and_kas_carry_the_envelope(self):
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_STRUCTURED_REFUSAL
        assert ACP_BACKEND_KAS in ACP_BACKENDS_STRUCTURED_REFUSAL

    def test_claude_and_codex_do_not(self):
        # Not "unsupported": they land on the same RefusalInfo with no fields.
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_STRUCTURED_REFUSAL
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_STRUCTURED_REFUSAL

    def test_every_shared_runtime_harness_is_a_member(self):
        # AcpSessionHandle reads the envelope unconditionally on the strength of
        # this subset relation; a runtime harness outside the set would have its
        # metadata guessed at.
        assert ACP_BACKENDS_ACP_RUNTIME <= ACP_BACKENDS_STRUCTURED_REFUSAL


class TestParseRefusal:
    def test_incident_frame(self):
        info = parse_refusal(INCIDENT_FRAME)
        assert info == RefusalInfo(category="CYBER", explanation=CANNED, recommended_model="")

    def test_ordinary_usage_frame_is_not_a_refusal(self):
        assert parse_refusal({"contextUsagePercentage": 12.0, "meteringUsage": []}) is None
        assert parse_refusal({}) is None

    def test_stop_reason_alone_is_a_refusal(self):
        info = parse_refusal({"stopReason": "content_filtered"})
        assert info is not None and info.category == ""

    def test_refusal_object_alone_is_a_refusal(self):
        # A future stop-reason spelling must not hide a reason the service sent.
        info = parse_refusal({"stopReason": "SOMETHING_NEW", "refusal": {"category": "X"}})
        assert info is not None and info.category == "X"

    def test_non_string_fields_are_empty_not_guessed(self):
        info = parse_refusal(
            {"stopReason": "CONTENT_FILTERED", "refusal": {"category": 7, "explanation": ["a"]}}
        )
        assert info == RefusalInfo()

    def test_explanation_is_redacted_and_capped(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        info = parse_refusal(
            {
                "stopReason": "CONTENT_FILTERED",
                "refusal": {"explanation": f"key {secret} " + "x" * 2000},
            }
        )
        assert info is not None
        assert secret not in info.explanation
        assert len(info.explanation) <= _dispatch._REFUSAL_EXPLANATION_MAX

    def test_log_names_category_but_never_explanation(self, caplog):
        with caplog.at_level(logging.INFO, logger=_dispatch.logger.name):
            parse_refusal(INCIDENT_FRAME)
        line = next(
            r.getMessage() for r in caplog.records if "content-filter refusal" in r.getMessage()
        )
        assert "CYBER" in line
        assert "cannot continue" not in line

    def test_envelope_keys_are_consumed_not_reported(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata(INCIDENT_FRAME)
        assert not [r for r in caplog.records if "unconsumed field" in r.getMessage()]

    def test_novel_refusal_key_is_reported_by_name_only(self, caplog):
        frame = {"stopReason": "CONTENT_FILTERED", "refusal": {"policyId": "p-secret"}}
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata(frame)
        line = next(r.getMessage() for r in caplog.records if "unconsumed field" in r.getMessage())
        assert "refusal.policyId:str" in line
        assert "p-secret" not in line


class TestTerminalFold:
    def test_metadata_refusal_rewrites_end_turn(self):
        stats = AcpPromptStats()
        stats.refusal = parse_refusal(INCIDENT_FRAME)
        reason, info = stats.terminal_refusal(STOP_REASON_END_TURN)
        assert reason == STOP_REASON_REFUSAL
        assert info is not None and info.category == "CYBER"

    def test_bare_refusal_stop_reason_passes_through_with_no_payload(self):
        # Anthropic's `refusal` carries no reason: the stop reason alone drives
        # the dashboard branch, and the card renders None as the bare card.
        reason, info = AcpPromptStats().terminal_refusal(STOP_REASON_REFUSAL)
        assert reason == STOP_REASON_REFUSAL
        assert info is None

    def test_ordinary_terminal_is_untouched(self):
        assert AcpPromptStats().terminal_refusal(STOP_REASON_END_TURN) == (
            STOP_REASON_END_TURN,
            None,
        )

    def test_refusal_does_not_survive_the_turn_boundary(self):
        stats = AcpPromptStats()
        stats.refusal = parse_refusal(INCIDENT_FRAME)
        assert stats.carry_over().refusal is None


class TestClientGate:
    """``AcpClient._track_metadata`` consults the parser only for members (H6)."""

    @staticmethod
    def _client(backend: str):
        from kiro_crew.acp import client as acp_client

        c = acp_client.AcpClient.__new__(acp_client.AcpClient)
        c._acp_backend = backend
        c.last_prompt_stats = AcpPromptStats()
        c._resolved_model_id = ""
        c._model = ""
        return c

    def test_kiro_records_the_refusal(self):
        from kiro_crew.acp.types import JsonRpcMessage

        c = self._client(ACP_BACKEND_KIRO)
        c._track_metadata(JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)))
        assert c.last_prompt_stats.refusal is not None
        assert c.last_prompt_stats.refusal.category == "CYBER"

    def test_claude_ignores_the_envelope(self):
        from kiro_crew.acp.types import JsonRpcMessage

        c = self._client(ACP_BACKEND_CLAUDE)
        c._track_metadata(JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)))
        assert c.last_prompt_stats.refusal is None


class TestCard:
    def test_full_kiro_card(self):
        text = refusal_card_text(parse_refusal(INCIDENT_FRAME))
        assert text.startswith("Response declined by the model.")
        assert "Content filter: cyber." in text
        assert CANNED in text
        assert "rephrasing" in text

    def test_recommended_model_replaces_the_rephrase_hint(self):
        text = refusal_card_text(RefusalInfo(recommended_model="m-2"))
        assert "suggests model 'm-2'" in text
        assert "rephrasing" not in text

    def test_bare_card_has_no_empty_labels(self):
        text = refusal_card_text(None)
        assert "Content filter" not in text
        assert "suggests model" not in text
        assert text.startswith("Response declined by the model.")


class TestEveryFieldIsScrubbed:
    """Round-2 GPT finding: ``category`` / ``recommendedModel`` reach the same
    log line and card as ``explanation``, so all three pass the two redactors."""

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.parametrize("field", ["category", "explanation", "recommendedModel"])
    def test_credential_in_any_field_is_redacted(self, field, caplog):
        with caplog.at_level(logging.INFO, logger=_dispatch.logger.name):
            info = parse_refusal(
                {"stopReason": "CONTENT_FILTERED", "refusal": {field: f"k {self.SECRET} tail"}}
            )
        assert info is not None
        assert self.SECRET not in (info.category + info.explanation + info.recommended_model)
        assert not any(self.SECRET in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("field", ["category", "recommendedModel"])
    def test_exfil_url_in_a_short_field_is_redacted(self, field):
        url = "https://evil.example/x?t=" + "a" * 40
        info = parse_refusal({"stopReason": "CONTENT_FILTERED", "refusal": {field: url}})
        assert info is not None
        joined = info.category + info.recommended_model
        assert "a" * 40 not in joined
        assert "[REDACTED" in joined


class TestProviderForwardsRefusal:
    def test_to_llm_event_carries_refusal(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
        from kiro_crew.providers.acp import AcpProvider

        src = AcpEvent(
            kind=EVENT_COMPLETE,
            stop_reason=STOP_REASON_REFUSAL,
            refusal=RefusalInfo(category="CYBER"),
        )
        out = AcpProvider._to_llm_event(src)
        assert out.refusal is src.refusal


class TestErrorFrameTerminal:
    """Round-2 Opus finding: a filter refusal can terminate as a bare -32603.
    The reason already arrived on metadata, so the error frame is the
    refusal's terminal -- surfaced as EVENT_COMPLETE, never raised."""

    @pytest.mark.asyncio
    async def test_client_yields_refusal_terminal_instead_of_raising(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)
        c._read_new_tool_results_sync = lambda: []

        async def _loop(req_id, timeout):
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield (
                "error",
                JsonRpcMessage(id=1, error={"code": -32603, "message": "Internal error"}),
            )

        c._prompt_loop = _loop
        events = [ev async for ev in c._dispatch_events(1, 5.0)]
        assert [ev.kind for ev in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_REFUSAL
        assert events[0].refusal is not None and events[0].refusal.category == "CYBER"

    @pytest.mark.asyncio
    async def test_error_without_a_refusal_still_raises(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)
        c._read_new_tool_results_sync = lambda: []

        async def _loop(req_id, timeout):
            yield (
                "error",
                JsonRpcMessage(id=1, error={"code": -32603, "message": "Internal error"}),
            )

        c._prompt_loop = _loop
        with pytest.raises(acp_client.AcpError):
            _ = [ev async for ev in c._dispatch_events(1, 5.0)]


class TestCardDoesNotRepeatStreamedText:
    """Round-2 Opus finding: the Kiro explanation streams as assistant text
    before the terminal, so the card must not print it a second time."""

    def test_explanation_omitted_when_already_streamed(self):
        info = parse_refusal(INCIDENT_FRAME)
        text = refusal_card_text(info, streamed_text="prefix " + CANNED + " suffix")
        assert CANNED not in text
        assert "Content filter: cyber." in text

    def test_explanation_kept_when_not_streamed(self):
        info = parse_refusal(INCIDENT_FRAME)
        assert CANNED in refusal_card_text(info, streamed_text="something else")


class TestRedactionSeesTheWholeValue:
    """Round-3 GPT finding: a pre-redaction slice can cut a secret so its prefix
    no longer matches and reaches the surface raw. The redactors run on the
    full value; only the result is capped."""

    def test_credential_beyond_the_cap_is_still_redacted(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        padding = "https://evil.example/" + "u" * 300 + " "
        value = padding * 3 + secret + " tail"
        assert len(value) > 4 * 128
        text = _dispatch._refusal_str(value, 128)
        assert secret not in text
        assert len(text) <= 128


class TestWorkerPoolPathFoldsRefusal:
    """Round-3 Opus finding: the string-returning ``_read_prompt_response``
    (worker-pool / background path) must fold the refusal too, and must not
    raise on the refusal's -32603 terminal."""

    @pytest.mark.asyncio
    async def test_error_terminal_returns_streamed_text_under_refusal(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)

        async def _loop(req_id, timeout):
            yield (
                "update",
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": CANNED},
                        }
                    },
                ),
            )
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield (
                "error",
                JsonRpcMessage(id=1, error={"code": -32603, "message": "Internal error"}),
            )

        c._prompt_loop = _loop
        out = await c._read_prompt_response(1, 5.0)
        assert out == CANNED
        assert c._last_stop_reason == STOP_REASON_REFUSAL

    @pytest.mark.asyncio
    async def test_end_turn_terminal_is_folded(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)

        async def _loop(req_id, timeout):
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield ("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN}))

        c._prompt_loop = _loop
        await c._read_prompt_response(1, 5.0)
        assert c._last_stop_reason == STOP_REASON_REFUSAL


class TestSendMessageStreamFoldsRefusal:
    """First Principles round: ``send_message_stream`` is the fourth prompt
    terminal reader; it folds too, so no path is left as a trap."""

    @pytest.mark.asyncio
    async def test_error_terminal_ends_under_refusal(self, tmp_path):
        from unittest.mock import AsyncMock

        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)
        c.ensure_ready = AsyncMock()
        c._send_prompt = AsyncMock(return_value=1)

        async def _loop(req_id, timeout):
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield (
                "error",
                JsonRpcMessage(id=1, error={"code": -32603, "message": "Internal error"}),
            )

        c._prompt_loop = _loop
        chunks = [ch async for ch in c.send_message_stream("hi")]
        assert chunks == []
        assert c._last_stop_reason == STOP_REASON_REFUSAL


class TestRefusalTerminalIsScoped:
    """Design Review round: only the bare -32603 after a recorded refusal is
    the refusal's terminal. Any other error keeps its own classification and
    the swallow is logged at WARNING."""

    REFUSAL = RefusalInfo(category="CYBER")

    def test_bare_internal_error_after_refusal_is_the_terminal(self, caplog):
        from kiro_crew.acp._dispatch import error_is_refusal_terminal

        with caplog.at_level(logging.WARNING, logger=_dispatch.logger.name):
            assert error_is_refusal_terminal(
                {"code": -32603, "message": "Internal error"}, self.REFUSAL
            )
        assert any("consumed as the terminal" in r.getMessage() for r in caplog.records)

    def test_no_recorded_refusal_means_ordinary_error(self):
        from kiro_crew.acp._dispatch import error_is_refusal_terminal

        assert not error_is_refusal_terminal({"code": -32603, "message": "Internal error"}, None)

    @pytest.mark.parametrize(
        "error",
        [
            {"code": -32603, "message": "Internal error", "data": "already in progress"},
            {"code": -32603, "message": "Internal error", "data": "The model 'x' is not available"},
            {"code": -32603, "message": "Internal error", "data": "ThrottlingException"},
            {"code": -32000, "message": "Internal error"},
            {"code": "not-a-code"},
            "not a dict",
        ],
    )
    def test_other_errors_after_a_refusal_keep_their_class(self, error):
        from kiro_crew.acp._dispatch import error_is_refusal_terminal

        assert not error_is_refusal_terminal(error, self.REFUSAL)

    @pytest.mark.asyncio
    async def test_prompt_busy_after_refusal_still_raises_prompt_busy(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)
        c._read_new_tool_results_sync = lambda: []

        async def _loop(req_id, timeout):
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield (
                "error",
                JsonRpcMessage(
                    id=1,
                    error={
                        "code": -32603,
                        "message": "Internal error",
                        "data": "already in progress",
                    },
                ),
            )

        c._prompt_loop = _loop
        with pytest.raises(acp_client.AcpPromptBusy):
            _ = [ev async for ev in c._dispatch_events(1, 5.0)]


class TestRefusalTerminalFlushesToolResults:
    """GPT round on 6a58089f9: the refusal terminal in ``_dispatch_events``
    must drain pending tool results before EVENT_COMPLETE, like ``complete``
    does, or a result from the tool that ran just before the filtered
    inference is dropped or leaks into the next turn."""

    @pytest.mark.asyncio
    async def test_pending_tool_results_precede_the_refusal_terminal(self, tmp_path):
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, JsonRpcMessage

        c = acp_client.AcpClient(work_dir=tmp_path)
        pending = [
            AcpEvent(kind="tool_result", tool_call_id="t1", tool_output="ok", tool_final=True)
        ]
        c._read_new_tool_results_sync = lambda: pending

        async def _loop(req_id, timeout):
            yield (
                "metadata",
                JsonRpcMessage(method="_kiro.dev/metadata", params=dict(INCIDENT_FRAME)),
            )
            yield (
                "error",
                JsonRpcMessage(id=1, error={"code": -32603, "message": "Internal error"}),
            )

        c._prompt_loop = _loop
        events = [ev async for ev in c._dispatch_events(1, 5.0)]
        assert [ev.kind for ev in events] == ["tool_result", EVENT_COMPLETE]
        assert events[0].tool_call_id == "t1"
        assert events[1].stop_reason == STOP_REASON_REFUSAL
