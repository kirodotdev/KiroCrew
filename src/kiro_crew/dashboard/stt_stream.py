"""Streaming STT WebSocket endpoint.

Forwards 16 kHz Int16 PCM audio chunks from the browser to AWS Transcribe
Streaming and relays partial + final transcripts back to the client.
Requires ``stt.provider == 'transcribe'`` and ``stt.streaming == true``.
Whisper users fall back to the existing batch path at ``/api/stt/transcribe``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

# Streaming-path dep. Declared in Config + setup.cfg, but keep the module
# importable without it so a stale-env gateway still starts and cli_doctor
# can diagnose. api_ws_stt() re-checks and returns a friendly WS error.
try:
    from amazon_transcribe.client import TranscribeStreamingClient
except ImportError:  # pragma: no cover — exercised by test_import_error_*
    TranscribeStreamingClient = None  # type: ignore[assignment,misc]

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.transcribe import _ProfileCredentialResolver

logger = logging.getLogger(__name__)

# Browser AudioWorklet downsamples to 16 kHz Int16 PCM mono.
STREAM_SAMPLE_RATE_HZ = 16000
# Cap per-WebSocket-frame size (128 KiB) — small enough to reject obvious
# abuse, large enough for any reasonable 16 kHz PCM chunk cadence.
_MAX_WS_MSG_SIZE = 128 * 1024
# Cap total session duration (seconds). Transcribe bills per audio-second
# ($0.024/min), so an abandoned or malicious connection left open could
# rack up unbounded cost. 5 min covers realistic dictation; longer
# sessions require explicit reconnect.
_MAX_STREAM_DURATION_SECS = 300
# Cap text-frame size — the only valid text frame is `{"type":"stop"}`
# (15 bytes). Reject obvious abuse without the 128 KiB binary cap.
_MAX_TEXT_FRAME_BYTES = 256
# Cap concurrent Transcribe streaming sessions per-process. Each open
# WebSocket spawns a billable Transcribe session; without a cap, a single
# user opening many tabs or an attacker past the origin check can
# multiply cost and exhaust the account's concurrent-stream quota.
# Safe as a plain int on the single-threaded asyncio loop.
_MAX_CONCURRENT_SESSIONS = 3
_active_sessions = 0


def _emit_end_audit(caller: str, *, outcome: str) -> None:
    """Log ``stt_stream_end`` defensively.

    All three exit paths (ImportError, Transcribe start-failure, normal
    ``finally``) must emit this event or the audit trail shows
    unmatched ``stt_stream_start`` entries. Never raise: a failing SEL
    call must not short-circuit the caller's ``return ws``.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="stt_stream_end",
            outcome=outcome,
            resources="/api/ws/stt",
        )
    except Exception:
        logger.exception("Failed to emit stt_stream_end SEL audit")


def _emit_guard_audit(caller: str, *, outcome: str) -> None:
    """Log ``stt_stream_rejected`` defensively on guard-path rejections.

    If ``sel()`` or ``log_api_access`` raises (e.g. SEL not initialized),
    the exception must not propagate — otherwise the intended
    ``HTTPForbidden``/``HTTPServiceUnavailable`` is replaced by a 500.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="stt_stream_rejected",
            outcome=outcome,
            resources="/api/ws/stt",
        )
    except Exception:
        logger.exception("Failed to emit stt_stream_rejected SEL audit")


def _make_handler(ws: web.WebSocketResponse):  # type: ignore[no-untyped-def]
    """Build a TranscriptResultStreamHandler that forwards events to ``ws``.

    Both partials and finals pass through ``redact_credentials`` and
    ``redact_exfiltration_urls`` before they leave the process — a
    partial flashed in the browser counts as an external surface even
    though it is replaced by the next partial and never persisted.
    """
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent

    class Handler(TranscriptResultStreamHandler):
        async def handle_transcript_event(self, event: TranscriptEvent) -> None:
            if ws.closed:
                return
            for result in event.transcript.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript
                redacted, _ = redact_exfiltration_urls(text)
                redacted, _ = redact_credentials(redacted)
                try:
                    if result.is_partial:
                        await ws.send_json({"type": "partial", "text": redacted})
                    else:
                        await ws.send_json({"type": "final", "text": redacted})
                except Exception:
                    # Client disconnected mid-send. Stop processing rather
                    # than flooding the log with per-event tracebacks.
                    return

    return Handler


async def api_ws_stt(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws/stt — streaming speech-to-text.

    Client sends binary PCM frames and text control messages
    (``{"type":"stop"}``). Server emits JSON events
    ``{"type":"partial"|"final"|"error"|"ready", ...}``.
    """
    if not check_origin(request, require=True):
        _emit_guard_audit(request.remote or "unknown", outcome="forbidden")
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    cfg = KiroCrewConfig.load()
    if not cfg.stt.enabled or cfg.stt.provider != "transcribe" or not cfg.stt.streaming:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        raise web.HTTPServiceUnavailable(text="streaming STT not enabled")

    global _active_sessions
    if _active_sessions >= _MAX_CONCURRENT_SESSIONS:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        raise web.HTTPServiceUnavailable(text="too many concurrent STT sessions")
    _active_sessions += 1
    try:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=_MAX_WS_MSG_SIZE)
        await ws.prepare(request)

        caller = request.remote or "dashboard"
        try:
            sel().log_api_access(
                caller=caller,
                operation="stt_stream_start",
                outcome="ok",
                resources="/api/ws/stt",
            )
        except Exception:
            # Mirror the ImportError / client-construction paths: if the
            # mandatory start audit fails, close the already-prepare()d WS
            # and emit the matching end audit so the trail stays balanced.
            logger.exception("Failed to emit stt_stream_start SEL audit")
            try:
                await ws.send_json({"type": "error", "message": "audit subsystem unavailable"})
            except Exception:
                pass
            await ws.close()
            _emit_end_audit(caller, outcome="error")
            return ws

        if TranscribeStreamingClient is None:
            # amazon-transcribe not installed at gateway startup. Module-top
            # import fell back to None so the gateway could boot; surface a
            # friendly error here and keep the audit trail balanced.
            await ws.send_json({"type": "error", "message": "amazon-transcribe not installed"})
            await ws.close()
            _emit_end_audit(caller, outcome="error")
            return ws

        try:
            profile = cfg.stt.transcribe_profile or None
            resolver = _ProfileCredentialResolver(profile) if profile else None
            client = TranscribeStreamingClient(
                region=cfg.stt.transcribe_region,
                credential_resolver=resolver,
            )
        except Exception:
            # Invalid profile, bad region, or constructor error. Without
            # this guard the already-prepare()d WS never closes and no
            # stt_stream_end audit emits — audit trail then shows an
            # unmatched stt_stream_start. Mirrors the start_stream path.
            logger.exception("Failed to create Transcribe client")
            await ws.send_json({"type": "error", "message": "failed to create transcription client"})
            await ws.close()
            _emit_end_audit(caller, outcome="error")
            return ws

        stream = None
        try:
            stream = await client.start_stream_transcription(
                language_code=cfg.stt.language_code,
                media_sample_rate_hz=STREAM_SAMPLE_RATE_HZ,
                media_encoding="pcm",
                # Stabilization=high tells Transcribe to commit each word
                # sooner, at the cost of slightly more downstream corrections.
                # For interactive dictation this trades accuracy on the last
                # 1-2 words for noticeably faster per-word rendering — the
                # right tradeoff for the dashboard input box.
                enable_partial_results_stabilization=True,
                partial_results_stability="high",
            )
        except Exception:
            logger.exception("Failed to start Transcribe stream")
            await ws.send_json({"type": "error", "message": "failed to start transcription"})
            await ws.close()
            _emit_end_audit(caller, outcome="error")
            return ws

        # Enforce the bill-cap with a dedicated task, not an in-loop check.
        # `async for msg in ws` only yields on client data; aiohttp handles
        # heartbeat ping/pong internally, so an idle-but-alive client would
        # never trip a message-driven deadline.
        async def _enforce_deadline() -> None:
            await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
            if not ws.closed:
                try:
                    await ws.send_json(
                        {"type": "error", "message": "max stream duration exceeded"}
                    )
                except Exception:
                    pass
                # Must match defensive style of send_json above: ws.close()
                # can raise on a broken transport. An unhandled exception
                # here would flag the task as done-with-error, spuriously
                # marking `timed_out=True` in the cleanup below and
                # emitting a "Task exception was never retrieved" warning.
                try:
                    await ws.close()
                except Exception:
                    pass

        # Wrap `send_json(ready)` + task creation in the cleanup `try`.
        # If any of these lines raises (most plausibly a client disconnect
        # during Transcribe cold-start), the finally must still run
        # `end_stream()` to release the Transcribe session — otherwise it
        # silently bills and counts against the concurrent-stream quota.
        handler_task = None
        deadline_task = None
        try:
            await ws.send_json({"type": "ready"})

            handler = _make_handler(ws)(stream.output_stream)
            handler_task = asyncio.create_task(handler.handle_events())
            deadline_task = asyncio.create_task(_enforce_deadline())

            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    try:
                        await stream.input_stream.send_audio_event(audio_chunk=msg.data)
                    except Exception:
                        logger.exception("Transcribe send_audio_event failed")
                        break
                elif msg.type == WSMsgType.TEXT:
                    if len(msg.data) > _MAX_TEXT_FRAME_BYTES:
                        logger.warning(
                            "Oversized text frame (%d bytes) on /api/ws/stt — closing",
                            len(msg.data),
                        )
                        break
                    try:
                        ctrl = json.loads(msg.data)
                    except ValueError:
                        continue  # ignore non-JSON text frames
                    if isinstance(ctrl, dict) and ctrl.get("type") == "stop":
                        break
                    # Unknown control frames are ignored (forward-compat).
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            # Deadline fires exactly once and never cancels itself, so done() ⇒ timeout.
            if deadline_task is not None:
                timed_out = deadline_task.done() and not deadline_task.cancelled()
                deadline_task.cancel()
            else:
                timed_out = False
            try:
                await stream.input_stream.end_stream()
            except Exception:
                # Log at WARNING — a leaked Transcribe session counts against
                # the account concurrent-stream limit and silently bills.
                logger.warning("Failed to end Transcribe stream cleanly", exc_info=True)
            if handler_task is not None:
                # 3s is generous for Transcribe's trailing finals after
                # end_stream() — the library normally flushes within
                # ~150ms. A longer wait trades user-visible UI lag for
                # tail-event completeness; given lastPartial-fallback on
                # the frontend, we err on the side of snappy close.
                try:
                    await asyncio.wait_for(asyncio.shield(handler_task), timeout=3)
                except asyncio.TimeoutError:
                    handler_task.cancel()
                    # Swallow the post-cancel InvalidStateError the
                    # amazon-transcribe awscrt pump raises when its
                    # response Future is cancelled mid-write. The session
                    # is already closing; surfacing it adds noise.
                    try:
                        await handler_task
                    except (asyncio.CancelledError, Exception):
                        pass
                except Exception:
                    # Surface mid-stream Transcribe errors (connection drop, credential
                    # expiry) so operators can see why transcription stopped instead of
                    # silently cancelling the task.
                    logger.exception("Transcribe handler task failed")
            if not ws.closed:
                # ws.close() can raise on a broken transport. If it did,
                # the uncaught exception would skip _emit_end_audit below,
                # leaving an unmatched stt_stream_start — exactly what the
                # surrounding cleanup is designed to prevent. Matches the
                # defensive pattern in _enforce_deadline.
                try:
                    await ws.close()
                except Exception:
                    pass
            _emit_end_audit(caller, outcome="timeout" if timed_out else "ok")

        return ws
    finally:
        _active_sessions -= 1
