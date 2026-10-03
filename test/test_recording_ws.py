"""Tests for kiro_crew.recording.ws — WebSocket recording endpoint.

Validates:
- Origin rejection (403)
- Concurrency cap rejection (503)
- Start → ready handshake with meeting_id
- Binary audio frames accepted while recording
- Pause/resume/stop control messages
- RMS level events emitted
- The socket emits only ready / level / error (audio in, nothing derived out)
- Paired start/end audit events on every exit path
- Oversized text frame rejection
- Unknown control types ignored (forward-compat)
- Recording persistence is independent of the STT provider
- _close_and_end_audit helper emits audit then closes
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from unittest.mock import ANY, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.recording import session as session_mod
from kiro_crew.recording.ws import (
    _AUDIT_RESOURCE,
    _close_and_end_audit,
    _compute_rms,
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
        monkeypatch.setattr("kiro_crew.recording.ws.MAX_CONCURRENT_SESSIONS", 1)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_concurrent_cap_emits_rejection_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.recording.ws.active_session_count", lambda: 1)
        monkeypatch.setattr("kiro_crew.recording.ws.MAX_CONCURRENT_SESSIONS", 1)
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
    """Local guard: refuse non-direct clients when require_local_gateway is set."""

    @pytest.mark.asyncio
    async def test_rejects_non_loopback_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A request the direct-local predicate rejects must be refused."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        monkeypatch.setattr("kiro_crew.recording.ws.is_direct_local_request", lambda request: False)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/recording")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_forwarded_loopback_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-host proxy cannot turn a remote client into a local recorder."""
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/ws/recording",
                headers={"X-Forwarded-For": "203.0.113.7"},
            )
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
        monkeypatch.setattr("kiro_crew.recording.ws.is_direct_local_request", lambda request: False)
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
        monkeypatch.setattr("kiro_crew.recording.ws.is_direct_local_request", lambda request: True)
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
        monkeypatch.setattr("kiro_crew.recording.ws.is_direct_local_request", lambda request: False)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            resp = await ws.receive_json()
            assert resp["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()


class TestAsyncSetup:
    """Blocking setup work must not run on aiohttp's event-loop thread."""

    @pytest.fixture(autouse=True)
    def _patch_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.recording.ws.is_direct_local_request", lambda request: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

    @pytest.mark.asyncio
    async def test_config_load_runs_off_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        event_loop_thread = threading.get_ident()
        called_from: list[int] = []
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"

        def _load(cls: object) -> object:
            called_from.append(threading.get_ident())
            return fake_cfg

        monkeypatch.setattr("kiro_crew.recording.ws.KiroCrewConfig.load", classmethod(_load))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

        assert called_from
        assert all(thread_id != event_loop_thread for thread_id in called_from)

    @pytest.mark.asyncio
    async def test_storage_resolution_runs_off_event_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        event_loop_thread = threading.get_ident()
        called_from: list[int] = []
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        fake_cfg.stt.provider = "whisper"
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )

        def _resolve(meeting_id: str):
            called_from.append(threading.get_ident())
            return tmp_path

        monkeypatch.setattr("kiro_crew.recording.ws._resolve_storage_dir", _resolve)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "meeting-1"})
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

        assert called_from
        assert all(thread_id != event_loop_thread for thread_id in called_from)


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
    async def test_finalization_failure_reports_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kwargs: calls.append(dict(kwargs))
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: fake_sel)

        async def _fail_stop(session) -> None:  # type: ignore[no-untyped-def]
            session._state = session_mod.SessionState.PROCESSING
            raise OSError("disk full")

        monkeypatch.setattr(session_mod.RecordingSession, "stop", _fail_stop)

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start"})
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_json({"type": "stop"})
            error = await ws.receive_json()
            assert error == {"type": "error", "message": "recording finalization failed"}
            await ws.close()

        await _wait_for_operation(calls, "recording_session_end")
        end_calls = [c for c in calls if c["operation"] == "recording_session_end"]
        assert end_calls[-1]["outcome"] == "error"

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


class TestNoTranscriptOnTheWire:
    """The recording socket carries audio in and only ready/level/error out.

    Transcription lives on ``/api/ws/stt``; this socket never emits a
    ``partial``/``final`` and holds no redactor, so there is no text egress here
    to register in ``security_posture.py``.
    """

    def test_socket_module_has_no_transcript_path(self) -> None:
        import inspect

        import kiro_crew.recording.ws as ws_mod

        source = inspect.getsource(ws_mod)
        assert "_emit_transcript" not in source
        assert "redact_credentials" not in source
        assert '"partial"' not in source and '"final"' not in source

    def test_concurrency_cap_has_one_source_of_truth(self) -> None:
        import kiro_crew.recording.ws as ws_mod

        assert ws_mod.MAX_CONCURRENT_SESSIONS is session_mod.MAX_CONCURRENT_SESSIONS
        assert not hasattr(ws_mod, "_MAX_CONCURRENT_SESSIONS")


class TestNoDurationCap:
    """Recording persistence is independent of STT provider limits."""

    @pytest.mark.asyncio
    async def test_a_recording_stops_at_its_size_ceiling_and_keeps_what_fit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import wave

        monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: MagicMock())
        fake_cfg = MagicMock()
        fake_cfg.recording.require_local_gateway = True
        monkeypatch.setattr(
            "kiro_crew.recording.ws.KiroCrewConfig.load",
            classmethod(lambda cls: fake_cfg),
        )
        monkeypatch.setattr("kiro_crew.recording.ws._resolve_storage_dir", lambda _m: tmp_path)
        # An odd ceiling proves the cut lands on a whole sample.
        monkeypatch.setattr("kiro_crew.recording.ws._MAX_RECORDING_BYTES", 1001)

        frame = _generate_pcm_frame(n_samples=160)  # 320 bytes
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "meeting-1"})
            assert (await ws.receive_json())["type"] == "ready"
            for _ in range(4):
                await ws.send_bytes(frame)
            errors = []
            while True:
                msg = await ws.receive()
                if msg.type.name != "TEXT":
                    break
                body = json.loads(msg.data)
                if body.get("type") == "error":
                    errors.append(body["message"])
            await ws.close()

        assert errors == ["recording size limit reached"]
        with wave.open(str(tmp_path / "audio.wav"), "rb") as wav:
            assert wav.getnframes() * wav.getsampwidth() == 1000

    def test_the_ceiling_covers_the_longest_meeting_and_fits_a_wav(self) -> None:
        import kiro_crew.recording.ws as ws_mod

        assert ws_mod._MAX_RECORDING_BYTES == 4 * 60 * 60 * 16_000 * 2
        assert ws_mod._MAX_RECORDING_BYTES < 2**32 - 44

    def test_recording_socket_has_no_duration_deadline(self) -> None:
        import inspect

        import kiro_crew.recording.ws as ws_mod

        source = inspect.getsource(ws_mod)
        assert "_MAX_STREAM_DURATION_SECS" not in source
        assert "_enforce_deadline" not in source
        assert "cfg.stt.provider" not in source


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
