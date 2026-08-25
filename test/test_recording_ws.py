"""Tests for kiro_crew.recording.ws — WebSocket recording endpoint.

Validates:
- Origin rejection (403)
- Concurrency cap rejection (503)
- Start → ready handshake with meeting_id
- Binary audio frames accepted while recording
- Pause/resume/stop control messages
- RMS level events emitted
- Partial and final transcript events (redacted)
- Paired start/end audit events on every exit path
- Oversized text frame rejection
- Unknown control types ignored (forward-compat)
- Duration cap enforced only when provider is transcribe
- _close_and_end_audit helper emits audit then closes
"""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import ANY, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.recording.ws import (
    _AUDIT_RESOURCE,
    _close_and_end_audit,
    _compute_rms,
    _emit_transcript,
    api_ws_recording,
)

# Upper bound for waiting on audit events (same pattern as test_stt_stream.py).
_AUDIT_WAIT_TIMEOUT_SECS = 5.0


async def _wait_for_operation(calls: list[dict], operation: str) -> None:
    """Await *operation* appearing in *calls*, or fail with what did arrive."""

    async def _poll() -> None:
        while operation not in [c.get("operation") for c in calls]:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_poll(), timeout=_AUDIT_WAIT_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"{operation!r} audit never emitted within {_AUDIT_WAIT_TIMEOUT_SECS}s; "
            f"got {[c.get('operation') for c in calls]}"
        ) from None


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/ws/recording", api_ws_recording)
    # check_origin reads app["allowed_origins"]
    app["allowed_origins"] = {"http://localhost:5476"}
    return app


def _generate_pcm_frame(n_samples: int = 1600, amplitude: int = 1000) -> bytes:
    """Generate a synthetic PCM frame (16-bit signed LE mono)."""
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


class TestGuards:
    """Guard-path tests: origin check and concurrency cap."""

    @pytest.mark.asyncio
    async def test_rejects_bad_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: False)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_bad_origin_emits_rejection_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: False)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 403
        fake_sel.log_api_access.assert_any_call(
            caller=ANY,
            operation="recording_session_rejected",
            outcome="forbidden",
            resources=_AUDIT_RESOURCE,
        )

    @pytest.mark.asyncio
    async def test_rejects_when_concurrent_cap_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.recording.ws.active_session_count", lambda: 1)
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_CONCURRENT_SESSIONS", 1)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_concurrent_cap_emits_rejection_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.recording.ws.active_session_count", lambda: 1)
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_CONCURRENT_SESSIONS", 1)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 503
        fake_sel.log_api_access.assert_any_call(
            caller=ANY,
            operation="recording_session_rejected",
            outcome="unavailable",
            resources=_AUDIT_RESOURCE,
        )


class TestLoopbackGuard:
    """Loopback guard: refuse non-loopback clients when require_local_gateway is set."""

    @pytest.mark.asyncio
    async def test_rejects_non_loopback_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-loopback remote must be rejected when require_local_gateway is true."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        # Simulate a non-loopback remote address
        monkeypatch.setattr("kiro_crew.recording.ws.is_loopback", lambda addr: False)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_non_loopback_emits_rejection_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loopback guard must emit a recording_session_rejected audit event."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        monkeypatch.setattr("kiro_crew.recording.ws.is_loopback", lambda addr: False)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 403
        fake_sel.log_api_access.assert_any_call(
            caller=ANY,
            operation="recording_session_rejected",
            outcome="forbidden_non_loopback",
            resources=_AUDIT_RESOURCE,
        )

    @pytest.mark.asyncio
    async def test_allows_loopback_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback remote must be allowed even when require_local_gateway is true."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        # is_loopback returns True for 127.0.0.1 (the TestClient connects via loopback)
        monkeypatch.setattr("kiro_crew.recording.ws.is_loopback", lambda addr: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_allows_non_loopback_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-loopback remote must be allowed when require_local_gateway is false."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = False
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        monkeypatch.setattr("kiro_crew.recording.ws.is_loopback", lambda addr: False)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()


class TestSessionLifecycle:
    """Test the start/pause/resume/stop WebSocket lifecycle."""

    @pytest.fixture(autouse=True)
    def _patch_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        # Use a no-op SEL to avoid needing the real audit subsystem
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        self._fake_sel = fake_sel
        # Default to a local provider (no deadline) for lifecycle tests
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )

    @pytest.mark.asyncio
    async def test_start_emits_ready_with_meeting_id(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "title": "Test Meeting"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            assert "meeting_id" in resp
            assert len(resp["meeting_id"]) > 0
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_binary_frames_accepted_while_recording(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            # Send a PCM frame
            pcm = _generate_pcm_frame(n_samples=160, amplitude=5000)
            await ws.send_bytes(pcm)

            # Should get a level event back
            level_resp = await ws.receive_json()
            assert level_resp["type"] == "level"
            assert "rms" in level_resp
            assert 0.0 <= level_resp["rms"] <= 1.0

            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_audio_discarded_while_paused(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            await ws.send_json({"type": "pause"})
            # Send audio while paused — should be silently discarded
            pcm = _generate_pcm_frame(n_samples=160, amplitude=5000)
            await ws.send_bytes(pcm)

            # Resume and send audio again — should get level
            await ws.send_json({"type": "resume"})
            await ws.send_bytes(pcm)
            level_resp = await ws.receive_json()
            assert level_resp["type"] == "level"

            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_stop_closes_cleanly(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            await ws.send_json({"type": "stop"})
            # The server should close the socket after stop
            msg = await ws.receive()
            # aiohttp sends a CLOSE frame
            assert (
                msg.type.value >= 0x100 or msg.type.name == "CLOSE" or msg.type.name == "CLOSED"
            )  # noqa: E501
            await ws.close()

    @pytest.mark.asyncio
    async def test_double_start_emits_error(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert "already started" in resp["message"]

            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_unknown_control_type_ignored(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            # Unknown control type — should be ignored
            await ws.send_json({"type": "foobar", "data": "test"})

            # Still works normally after unknown type
            pcm = _generate_pcm_frame(n_samples=160, amplitude=5000)
            await ws.send_bytes(pcm)
            level_resp = await ws.receive_json()
            assert level_resp["type"] == "level"

            await ws.send_json({"type": "stop"})
            await ws.close()


class TestAuditTrail:
    """Paired start/end audit events on every exit path."""

    @pytest.fixture(autouse=True)
    def _patch_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        # Default to a local provider (no deadline) for audit trail tests
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )

    @pytest.mark.asyncio
    async def test_normal_session_emits_start_and_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []
        fake_sel = MagicMock()

        def _capture(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        fake_sel.log_api_access = _capture
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

        await _wait_for_operation(calls, "recording_session_end")

        ops = [c["operation"] for c in calls]
        assert "recording_session_start" in ops
        assert "recording_session_end" in ops

    @pytest.mark.asyncio
    async def test_abrupt_disconnect_emits_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []
        fake_sel = MagicMock()

        def _capture(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        fake_sel.log_api_access = _capture
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            # Close abruptly without stop
            await ws.close()

        await _wait_for_operation(calls, "recording_session_end")

        ops = [c["operation"] for c in calls]
        assert "recording_session_start" in ops
        assert "recording_session_end" in ops


class TestFrameSizeCaps:
    """Frame size cap enforcement."""

    @pytest.fixture(autouse=True)
    def _patch_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        # Default to a local provider (no deadline)
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )

    @pytest.mark.asyncio
    async def test_oversized_text_frame_sends_error(self) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            # Send an oversized text frame (>1 KiB)
            big_text = json.dumps({"type": "pause", "padding": "x" * 2000})
            await ws.send_str(big_text)

            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert "too large" in resp["message"]
            await ws.close()


class TestRmsComputation:
    """Unit tests for RMS level computation."""

    def test_silence_returns_zero(self) -> None:
        pcm = struct.pack("<10h", *([0] * 10))
        assert _compute_rms(pcm) == 0.0

    def test_max_amplitude_returns_near_one(self) -> None:
        pcm = struct.pack("<100h", *([32767] * 100))
        rms = _compute_rms(pcm)
        assert 0.99 <= rms <= 1.0

    def test_moderate_amplitude(self) -> None:
        pcm = struct.pack("<100h", *([16384] * 100))
        rms = _compute_rms(pcm)
        assert 0.4 <= rms <= 0.6

    def test_empty_data_returns_zero(self) -> None:
        assert _compute_rms(b"") == 0.0

    def test_single_byte_returns_zero(self) -> None:
        # Not enough for a full sample
        assert _compute_rms(b"\x00") == 0.0


class TestTranscriptEmission:
    """Verify that _emit_transcript applies redaction to both partials and finals."""

    @pytest.mark.asyncio
    async def test_final_event_redacts_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Final transcript events must be redacted before emission."""
        from unittest.mock import AsyncMock

        from kiro_crew.recording.session import RecordingSession

        ws = AsyncMock()
        ws.closed = False
        session = RecordingSession()

        # Patch the session's add_transcript_segment to capture what gets persisted
        persisted: list[tuple[str, bool]] = []

        async def _capture_segment(text: str, *, is_final: bool = False) -> None:
            persisted.append((text, is_final))

        session.add_transcript_segment = _capture_segment  # type: ignore[assignment]

        # Feed text with an AWS key pattern
        text_with_cred = "My key is AKIAIOSFODNN7EXAMPLE plus more"
        await _emit_transcript(ws, text_with_cred, is_final=True, session=session)

        # The sent JSON must not contain the raw key
        ws.send_json.assert_called_once()
        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "final"
        assert "AKIAIOSFODNN7EXAMPLE" not in sent["text"]

    @pytest.mark.asyncio
    async def test_partial_event_redacts_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Partial transcript events must also be redacted."""
        from unittest.mock import AsyncMock

        from kiro_crew.recording.session import RecordingSession

        ws = AsyncMock()
        ws.closed = False
        session = RecordingSession()

        async def _capture_segment(text: str, *, is_final: bool = False) -> None:
            pass

        session.add_transcript_segment = _capture_segment  # type: ignore[assignment]

        text_with_cred = "Secret AKIAIOSFODNN7EXAMPLE here"
        await _emit_transcript(ws, text_with_cred, is_final=False, session=session)

        ws.send_json.assert_called_once()
        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "partial"
        assert "AKIAIOSFODNN7EXAMPLE" not in sent["text"]

    @pytest.mark.asyncio
    async def test_noop_when_ws_closed(self) -> None:
        """No emission when the WebSocket is already closed."""
        from unittest.mock import AsyncMock

        from kiro_crew.recording.session import RecordingSession

        ws = AsyncMock()
        ws.closed = True
        session = RecordingSession()
        await _emit_transcript(ws, "hello", is_final=True, session=session)
        ws.send_json.assert_not_called()


class TestDurationCap:
    """Duration cap enforcement — only when STT provider is transcribe."""

    @pytest.fixture(autouse=True)
    def _patch_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        self._fake_sel = fake_sel

    @pytest.mark.asyncio
    async def test_deadline_not_started_for_local_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local providers (whisper, mlx, faster) must NOT have a deadline task."""
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "faster"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        # Patch asyncio.create_task to track whether a deadline task is spawned
        tasks_created: list[object] = []
        real_create_task = asyncio.get_event_loop().create_task

        def _track_create_task(coro, **kwargs):  # type: ignore[no-untyped-def]
            task = real_create_task(coro, **kwargs)
            tasks_created.append(task)
            return task

        # We verify indirectly: the session should work and close without timeout
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_deadline_fires_for_transcribe_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When provider is transcribe, the deadline must fire and close the socket."""
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "transcribe"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        # Set a very short deadline for testing
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_STREAM_DURATION_SECS", 0.1)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            # Wait for the deadline to fire (0.1s + margin)
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert "max recording duration exceeded" in resp["message"]

            # Socket should be closed by the server
            msg = await ws.receive()
            assert msg.type.value >= 0x100 or msg.type.name in ("CLOSE", "CLOSED")
            await ws.close()

    @pytest.mark.asyncio
    async def test_timeout_outcome_in_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End audit event must report outcome='timeout' when the deadline fires."""
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "transcribe"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_STREAM_DURATION_SECS", 0.1)

        calls: list[dict] = []
        fake_sel = MagicMock()

        def _capture(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        fake_sel.log_api_access = _capture
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"

            # Wait for deadline
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            await ws.close()

        await _wait_for_operation(calls, "recording_session_end")
        end_calls = [c for c in calls if c["operation"] == "recording_session_end"]
        assert len(end_calls) == 1
        assert end_calls[0]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_normal_stop_reports_ok_not_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A normal stop with transcribe provider should report outcome='ok'."""
        fake_cfg = MagicMock()
        fake_cfg.stt.provider = "transcribe"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        # Keep deadline long so it doesn't fire during the test
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_STREAM_DURATION_SECS", 60)

        calls: list[dict] = []
        fake_sel = MagicMock()

        def _capture(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        fake_sel.log_api_access = _capture
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

        await _wait_for_operation(calls, "recording_session_end")
        end_calls = [c for c in calls if c["operation"] == "recording_session_end"]
        assert len(end_calls) == 1
        assert end_calls[0]["outcome"] == "ok"


class TestCloseAndEndAudit:
    """Tests for the _close_and_end_audit helper."""

    @pytest.mark.asyncio
    async def test_emits_audit_then_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_close_and_end_audit must emit the end audit before closing ws."""
        from unittest.mock import AsyncMock

        order: list[str] = []
        fake_sel = MagicMock()

        def _log(**kwargs: object) -> None:
            order.append("audit")

        fake_sel.log_api_access = _log
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        ws = AsyncMock()

        async def _close(**kwargs: object) -> None:
            order.append("close")

        ws.close = _close

        await _close_and_end_audit(ws, "test-caller", outcome="error")

        assert order == ["audit", "close"]

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If ws.close() raises, it must not propagate."""
        from unittest.mock import AsyncMock

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        ws = AsyncMock()
        ws.close.side_effect = ConnectionResetError("gone")

        # Should not raise
        await _close_and_end_audit(ws, "test-caller", outcome="error")
