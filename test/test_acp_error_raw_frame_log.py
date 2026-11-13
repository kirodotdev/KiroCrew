"""The harness's own error frame reaches the gateway log when curated wording hides it.

``_format_acp_error`` rewrites many provider failures into Kiro Crew's own
guidance, so a named 5xx, an HTTP 50x and a "please try again" all read
"The model backend hit a transient error (HTTP 5xx)". ``_raise_acp_error`` keeps
the frame on ``AcpError.raw_detail`` (redacted, bounded) for exactly those
curated messages, and the dashboard turn logs it on its own line so a
diagnostics bundle carries the underlying cause.
"""

from __future__ import annotations

import datetime
import json
import logging

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.acp.error_frames import (
    _RAW_ERROR_DETAIL_MAX_DEPTH,
    RAW_ERROR_DETAIL_MAX_CHARS,
    raw_error_detail,
)
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.session_health import RAW_FRAME_LOG_LABEL
from kiro_crew.security import redact_credentials

# A fake AWS access key id: matches (?:AKIA|ASIA)[A-Z0-9]{16}, asserted absent.
_TOKEN = "AKIA" + "RAWFRAME0123456A"
# A made-up PEM header, split so the source holds no key marker.
_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_CAUSE = "ProviderCauseMarker: profile lookup failed"
_CURATED_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": f"Encountered an error in the response stream: please try again ({_CAUSE})",
}


def _raised(frame: object) -> AcpError:
    try:
        _raise_acp_error(frame)
    except AcpError as exc:
        return exc
    raise AssertionError("_raise_acp_error did not raise")


class TestRawDetailOnCuratedErrors:
    def test_curated_5xx_keeps_the_frame_the_message_drops(self) -> None:
        exc = _raised(_CURATED_FRAME)
        # Premise: the user-facing message is the curated 5xx line, which does
        # not carry the provider's cause.
        assert "transient error (HTTP 5xx)" in str(exc)
        assert _CAUSE not in str(exc)
        assert exc.user_worded is True
        assert _CAUSE in exc.raw_detail
        assert json.loads(exc.raw_detail) == _CURATED_FRAME

    def test_verdict_and_tags_are_unchanged_by_the_detail(self) -> None:
        exc = _raised(_CURATED_FRAME)
        assert exc.transient is True
        assert exc.connection_failure is False
        assert exc.auth_required is False

    def test_fallback_message_already_carries_the_frame(self) -> None:
        # An unknown shape is passed through as the provider's own text, so the
        # log line already holds it and no second copy is kept.
        exc = _raised({"code": -32603, "message": "Internal error", "data": "SomethingNovel"})
        assert exc.user_worded is False
        assert "SomethingNovel" in str(exc)
        assert exc.raw_detail == ""

    def test_session_expiry_wording_keeps_the_frame(self) -> None:
        frame = {"code": -32000, "message": "Request failed", "data": "HTTP 401 session expired"}
        exc = _raised(frame)
        assert exc.user_worded is True
        assert "HTTP 401 session expired" in exc.raw_detail

    def test_prompt_busy_wording_keeps_the_frame(self) -> None:
        # Prompt-busy is raised on its own early path, before the general error
        # is built; its curated line drops the provider's text all the same.
        frame = {
            "code": -32603,
            "message": "Internal error",
            "data": f"a prompt is already in progress ({_CAUSE})",
        }
        exc = _raised(frame)
        assert isinstance(exc, AcpPromptBusy)
        assert exc.user_worded is True
        assert _CAUSE not in str(exc)
        assert _CAUSE in exc.raw_detail


class TestRawErrorDetailBounds:
    def test_credential_in_the_frame_is_scrubbed(self) -> None:
        frame = dict(_CURATED_FRAME, data=f"dispatch failure key={_TOKEN}")
        detail = raw_error_detail(frame)
        assert _TOKEN not in detail
        assert "[REDACTED" in detail

    def test_credential_straddling_the_cap_is_scrubbed_before_the_cut(self) -> None:
        # The cap falls inside the token: a cut before redaction would leave a
        # fragment no credential pattern matches.
        prefix_len = len(json.dumps({"code": -32603, "message": "m", "data": ""})) - 2
        pad = "x" * (RAW_ERROR_DETAIL_MAX_CHARS - prefix_len - 8)
        frame = {"code": -32603, "message": "m", "data": f"{pad}{_TOKEN} tail " + "y" * 4000}
        rendered = json.dumps(frame, ensure_ascii=False)
        start = rendered.index(_TOKEN)
        assert start < RAW_ERROR_DETAIL_MAX_CHARS < start + len(_TOKEN)

        detail = raw_error_detail(frame)
        assert _TOKEN not in detail
        leaked = _TOKEN[: RAW_ERROR_DETAIL_MAX_CHARS - start]
        assert leaked not in detail

    def test_long_frame_is_cut_with_the_dropped_length_named(self) -> None:
        frame = {"code": -32603, "message": "m", "data": "z" * (RAW_ERROR_DETAIL_MAX_CHARS * 3)}
        full = len(json.dumps(frame))
        detail = raw_error_detail(frame)
        assert detail.endswith(f"... [{full - RAW_ERROR_DETAIL_MAX_CHARS} more chars]")
        assert len(detail) < RAW_ERROR_DETAIL_MAX_CHARS + 40

    def test_newline_in_provider_text_stays_on_one_line(self) -> None:
        frame = dict(_CURATED_FRAME, data="dispatch failure\nWARNING forged line")
        assert "\n" not in raw_error_detail(frame)

    def test_structured_data_is_rendered_as_json(self) -> None:
        frame = {"code": -32603, "message": "Internal error", "data": {"errorKind": "unknown"}}
        assert json.loads(raw_error_detail(frame))["data"] == {"errorKind": "unknown"}

    def test_self_referencing_data_stays_finite_and_one_line(self) -> None:
        data: dict = {}
        data["self"] = data
        detail = raw_error_detail({"code": 1, "message": "m", "data": data})
        assert "\n" not in detail
        assert json.loads(detail)["message"] == "m"

    @pytest.mark.parametrize("field", ["aws_session_token", "SessionToken", "SecretAccessKey"])
    def test_quoted_credential_field_in_string_data_is_scrubbed_before_escaping(
        self, field: str
    ) -> None:
        # JSON escaping puts a backslash before each quote inside a string, which
        # the field-name patterns do not match: the string must be scrubbed in
        # its own spelling first.
        secret = "IQoJb3JpZ2luX2VjE" + "A" * 300 + "xyzTOKENEND"
        data = f'HTTP 401 session expired; creds {{"{field}": "{secret}"}}'
        # Premise guard: a scrub of the escaped form alone misses it.
        escaped = json.dumps({"data": data})
        assert secret in redact_credentials(escaped)[0]

        detail = raw_error_detail({"code": -32000, "message": "m", "data": data})
        assert secret not in detail
        assert "[REDACTED" in detail

    def test_credential_pair_in_structured_data_is_scrubbed(self) -> None:
        secret = "IQoJb3JpZ2luX2VjE" + "B" * 300
        detail = raw_error_detail({"code": 1, "message": "m", "data": {"SessionToken": secret}})
        assert secret not in detail

    def test_multiline_key_nested_past_the_depth_bound_is_not_logged(self) -> None:
        # A container's str escapes the newlines inside it, which the
        # truncated-PEM pattern needs, so past the bound it is dropped rather
        # than stringified unscrubbed.
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 3
        pem = f"{_PEM_BEGIN}\n{body}\n{body}\n"
        nested: object = pem
        for _ in range(_RAW_ERROR_DETAIL_MAX_DEPTH + 1):
            nested = {"k": nested}
        # Premise guard: the same key at a shallow depth is redacted.
        assert body not in raw_error_detail({"code": 1, "message": "m", "data": {"k": pem}})

        detail = raw_error_detail({"code": 1, "message": "m", "data": nested})
        assert body[:24] not in detail
        assert "[nested too deep]" in detail
        assert json.loads(detail)["message"] == "m"

    def test_non_mapping_frame_has_no_detail(self) -> None:
        assert raw_error_detail("dispatch failure") == ""
        assert raw_error_detail(None) == ""


class TestDashboardTurnLogsTheFrame:
    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _err_texts = _TransientSuite._err_texts

    async def _run_failing_turn(self, tmp_path, monkeypatch, frame: dict) -> None:
        from kiro_crew.dashboard.chat import _run_chat

        async def _stream(msg):
            raise _raised(frame)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        await _run_chat(state, slot, "hello")
        await self._drain_bg(state)
        assert any(t.startswith("❌") for t in self._err_texts(slot))

    @pytest.mark.asyncio
    async def test_curated_error_logs_the_frame_beside_the_scanned_line(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        # A terminal curated answer (session expiry): it ends the turn at once,
        # with no retry ladder in between.
        frame = {
            "code": -32000,
            "message": "Request failed",
            "data": f"HTTP 401 session expired ({_CAUSE}) key={_TOKEN}",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await self._run_failing_turn(tmp_path, monkeypatch, frame)

        lines = [
            r.getMessage() for r in caplog.records if r.name == "kiro_crew.dashboard.chat_runner"
        ]
        scanned = [m for m in lines if m.startswith("ACP error in slot s1:")]
        framed = [m for m in lines if m.startswith("ACP error frame in slot s1:")]
        assert scanned and framed
        # The session-health scanner's line is untouched; the cause rides only
        # on the separate frame line, and the credential is scrubbed there.
        assert all(_CAUSE not in m for m in scanned)
        assert all(_CAUSE in m for m in framed)
        assert all(_TOKEN not in m for m in framed)

    @pytest.mark.asyncio
    async def test_fallback_error_logs_no_second_line(self, tmp_path, monkeypatch, caplog) -> None:
        frame = {"code": -32603, "message": "Internal error", "data": "SomethingNovel"}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
            await self._run_failing_turn(tmp_path, monkeypatch, frame)

        lines = [
            r.getMessage() for r in caplog.records if r.name == "kiro_crew.dashboard.chat_runner"
        ]
        assert any(m.startswith("ACP error in slot s1:") for m in lines)
        assert not any(m.startswith("ACP error frame in slot") for m in lines)


class TestRawFrameRecordIsNotAStallLine:
    """A frame echoing a stall signature must not mark a slot stalled."""

    @staticmethod
    def _scan(tmp_path, lines: list[str]) -> dict:
        log = tmp_path / "gateway.log"
        log.write_text("\n")
        ts = datetime.datetime.fromtimestamp(log.stat().st_mtime).strftime("%H:%M:%S")
        log.write_text(
            "".join(f"{ts} WARNING kiro_crew.dashboard.chat_runner: {m}\n" for m in lines)
        )
        return session_health.scan_log_for_stalls(log_path=log, now=log.stat().st_mtime)

    def test_forged_signature_inside_the_frame_record_is_ignored(self, tmp_path) -> None:
        forged = "HTTP 401 session expired; ACP error in slot victim: [AcpPromptBusy]"
        exc = _raised({"code": -32000, "message": "Request failed", "data": forged})
        assert exc.raw_detail
        hits = self._scan(tmp_path, [f"{RAW_FRAME_LOG_LABEL} s1: {exc.raw_detail}"])
        assert "victim" not in hits

    @pytest.mark.parametrize("brk", ["\u2028", "\u2029", "\x85", "\x1c"])
    def test_unicode_line_break_cannot_split_the_frame_record(self, tmp_path, brk) -> None:
        forged = f"HTTP 401 session expired;{brk}ACP error in slot victim: [AcpPromptBusy]"
        exc = _raised({"code": -32000, "message": "Request failed", "data": forged})
        assert len(exc.raw_detail.splitlines()) == 1
        assert json.loads(exc.raw_detail)["data"] == forged
        hits = self._scan(tmp_path, [f"{RAW_FRAME_LOG_LABEL} s1: {exc.raw_detail}"])
        assert "victim" not in hits

    def test_real_stall_line_is_still_read(self, tmp_path) -> None:
        hits = self._scan(
            tmp_path,
            [
                "ACP error in slot chat-7-1: [AcpPromptBusy] busy",
                f'{RAW_FRAME_LOG_LABEL} chat-7-1: {{"code": -32603}}',
            ],
        )
        assert hits["chat-7-1"]["reason"] == "prompt_stuck"
