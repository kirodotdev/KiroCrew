"""WebSocket handler for recording sessions.

``/api/ws/recording`` accepts binary audio frames (16 kHz Int16 LE mono PCM)
plus JSON control messages (start, pause, resume, stop), and emits ready,
partial, final, level, and error events back to the client.

``start`` takes an optional ``meeting_id``. When present, the recording's
``audio.wav`` and ``transcript_local.json`` are written into that meeting's
directory, resolved through the store an app registered (see
:mod:`kiro_crew.recording.recovery`) -- this package is core and never turns a
client-supplied id into a path itself. When the id cannot be placed the start is
REFUSED, because a client that named a meeting expects a file at the end of it.
When ``meeting_id`` is absent nothing is persisted, which is the dictation and
voice-note case.

Hardening mirrors ``stt_stream.py``:
- Origin check (``check_origin(require=True)``)
- Per-frame size caps (binary and text separately)
- Credential and exfiltration-URL redaction on partials and finals
- Paired start/end audit events on every exit path, end emitted before close
- Concurrency cap (design: 1 simultaneous recording session)
- Duration cap when the STT provider is AWS Transcribe (billing bound)
- Loopback guard (refuse non-loopback clients when require_local_gateway is set)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.urls import is_loopback
from kiro_crew.recording.recovery import get_meeting_store
from kiro_crew.recording.session import (
    InvalidTransitionError,
    RecordingSession,
    SessionState,
    active_session_count,
    register_session,
    unregister_session,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cap per-binary-frame size (128 KiB). At 16 kHz Int16 mono, 100 ms = 3200
# bytes; 128 KiB covers ~20 seconds in a single frame, far beyond any
# reasonable chunk cadence.
_MAX_BINARY_FRAME_BYTES = 128 * 1024

# Cap text-frame size. Valid control messages are short JSON objects
# (e.g. ``{"type":"start","title":"Weekly sync"}``). 1 KiB is generous.
_MAX_TEXT_FRAME_BYTES = 1024

# Maximum concurrent recording sessions (design spec: 1).
_MAX_CONCURRENT_SESSIONS = 1

# Cap session duration (seconds) when the STT provider is AWS Transcribe.
# Transcribe bills per audio-second ($0.024/min), so an abandoned or
# malicious connection left open could rack up unbounded cost. Local
# providers (whisper, mlx, faster) are unbounded — they have no billing.
_MAX_STREAM_DURATION_SECS = 300

# RMS level reporting interval — emit at most one level event per this many
# seconds. Too-frequent events flood the WebSocket and the frontend; too-rare
# ones make the meter feel laggy. 200 ms ≈ 5 Hz.
_LEVEL_INTERVAL_SECS = 0.2

# Resource string used in audit events.
_AUDIT_RESOURCE = "/api/ws/recording"


# ---------------------------------------------------------------------------
# Audit helpers — mirror the stt_stream.py pattern.
# ---------------------------------------------------------------------------


def _emit_end_audit(caller: str, *, outcome: str) -> None:
    """Log ``recording_session_end`` defensively.

    All exit paths must emit this so the audit trail shows no unmatched
    ``recording_session_start`` entries. Never raises.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="recording_session_end",
            outcome=outcome,
            resources=_AUDIT_RESOURCE,
        )
    except Exception:
        logger.exception("Failed to emit recording_session_end SEL audit")


async def _close_and_end_audit(ws: web.WebSocketResponse, caller: str, *, outcome: str) -> None:
    """Emit ``recording_session_end``, then close *ws*, on an early-return path.

    Order matters: audit first, close second.  ``WebSocketResponse.close()``
    awaits the peer's close acknowledgement under its own timeout, so a client
    that already went away would otherwise hold the end event back and leave an
    unmatched start in the audit trail.  Emitting first makes the audit
    independent of the peer.

    ``_emit_end_audit`` never raises, so the close is always reached.
    """
    _emit_end_audit(caller, outcome=outcome)
    try:
        await ws.close()
    except Exception:
        logger.exception("Failed to close recording WebSocket on early return")


def _emit_guard_audit(caller: str, *, outcome: str) -> None:
    """Log ``recording_session_rejected`` on guard-path rejections.

    Must never raise — otherwise the intended HTTP error is replaced by a 500.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="recording_session_rejected",
            outcome=outcome,
            resources=_AUDIT_RESOURCE,
        )
    except Exception:
        logger.exception("Failed to emit recording_session_rejected SEL audit")


# ---------------------------------------------------------------------------
# RMS computation
# ---------------------------------------------------------------------------


def _compute_rms(pcm_data: bytes) -> float:
    """Compute RMS level from 16-bit signed LE PCM data.

    Returns a float in [0.0, 1.0] representing the normalized RMS level.
    """
    n_samples = len(pcm_data) // 2
    if n_samples == 0:
        return 0.0
    # Unpack as signed 16-bit little-endian
    samples = struct.unpack(f"<{n_samples}h", pcm_data[: n_samples * 2])
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / n_samples) / 32768.0
    return min(1.0, rms)


# ---------------------------------------------------------------------------
# Storage resolution
# ---------------------------------------------------------------------------


def _resolve_storage_dir(meeting_id: str) -> Optional[Path]:
    """Where this recording's files belong, or ``None`` if it cannot be placed.

    This package is core and must not import an app, so the client-supplied
    ``meeting_id`` is never turned into a path here. It is handed to the store an
    app registered (see :mod:`kiro_crew.recording.recovery`), which owns the
    validation and the containment check -- for Meetings that is
    ``safe_meeting_id`` followed by ``contain``.

    Every failure is ``None``: no store registered, an id the store rejected, or
    an unwritable directory. Never raises, so a storage problem cannot take the
    socket down with it.
    """
    store = get_meeting_store()
    if store is None:
        logger.warning(
            "recording: meeting_id %r supplied but no meeting store is registered",
            meeting_id[:120],
        )
        return None
    try:
        return store.resolve_meeting_dir(meeting_id)
    except Exception:
        logger.exception("recording: meeting store failed to resolve a directory")
        return None


# ---------------------------------------------------------------------------
# Transcript emission helper
# ---------------------------------------------------------------------------


async def _emit_transcript(
    ws: web.WebSocketResponse,
    text: str,
    *,
    is_final: bool,
    session: RecordingSession,
) -> None:
    """Emit a redacted transcript event and persist the segment.

    Both partials and finals pass through ``redact_exfiltration_urls`` and
    ``redact_credentials`` before they leave the process — a partial displayed
    in the browser is an external surface even though it is immediately replaced.
    """
    if ws.closed:
        return
    redacted, _ = redact_exfiltration_urls(text)
    redacted, _ = redact_credentials(redacted)
    event_type = "final" if is_final else "partial"
    try:
        await ws.send_json({"type": event_type, "text": redacted})
    except Exception:
        return
    # Persist the segment via the session's debounced persister
    await session.add_transcript_segment(redacted, is_final=is_final)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


async def api_ws_recording(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws/recording — audio recording WebSocket endpoint.

    Client sends binary PCM frames and JSON control messages.
    Server emits JSON events: ready, partial, final, level, error.
    """
    # --- Guard: origin check ---
    if not check_origin(request, require=True):
        _emit_guard_audit(request.remote or "unknown", outcome="forbidden")
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    # --- Guard: loopback ---
    # Recording streams meeting audio to whatever host runs the Gateway.
    # When require_local_gateway is true (default), refuse connections from
    # non-loopback addresses — the operator must explicitly acknowledge remote
    # capture by setting recording.require_local_gateway = false.
    cfg = KiroCrewConfig.load()
    if cfg.recording.require_local_gateway:
        remote = request.remote or ""
        if not is_loopback(remote):
            _emit_guard_audit(remote or "unknown", outcome="forbidden_non_loopback")
            raise web.HTTPForbidden(
                text="Recording refused: gateway is not on a loopback address. "
                "Set recording.require_local_gateway = false to allow remote capture."
            )

    # --- Guard: concurrency cap ---
    if active_session_count() >= _MAX_CONCURRENT_SESSIONS:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        raise web.HTTPServiceUnavailable(text="too many concurrent recording sessions")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=_MAX_BINARY_FRAME_BYTES)
    await ws.prepare(request)

    caller = request.remote or "dashboard"

    # --- Audit: session start ---
    try:
        sel().log_api_access(
            caller=caller,
            operation="recording_session_start",
            outcome="ok",
            resources=_AUDIT_RESOURCE,
        )
    except Exception:
        logger.exception("Failed to emit recording_session_start SEL audit")
        try:
            await ws.send_json({"type": "error", "message": "audit subsystem unavailable"})
        except Exception:
            pass
        await _close_and_end_audit(ws, caller, outcome="error")
        return ws

    # --- Duration cap: only for AWS Transcribe (billing bound) ---
    enforce_deadline = cfg.stt.provider == "transcribe"

    session: Optional[RecordingSession] = None
    last_level_time: float = 0.0
    deadline_task: Optional[asyncio.Task[None]] = None
    _deadline_fired = False

    async def _enforce_deadline() -> None:
        """Close the socket after the billing-cap duration elapses.

        Fires only when the STT provider is AWS Transcribe. Mirrors the
        stt_stream.py pattern: an idle-but-alive client never trips a
        message-driven check, so a dedicated task is the only reliable
        mechanism.

        Sets ``_deadline_fired`` before closing so the finally block can
        detect timeout even if the task is still awaiting the peer close
        acknowledgement (``ws.close()`` awaits the peer).
        """
        nonlocal _deadline_fired
        await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
        _deadline_fired = True
        if not ws.closed:
            try:
                await ws.send_json({"type": "error", "message": "max recording duration exceeded"})
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

    try:
        if enforce_deadline:
            deadline_task = asyncio.create_task(_enforce_deadline())

        # Wait for control messages and audio frames.
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if len(msg.data.encode("utf-8", errors="replace")) > _MAX_TEXT_FRAME_BYTES:
                    logger.warning(
                        "Oversized text frame (%d bytes) on %s — closing",
                        len(msg.data),
                        _AUDIT_RESOURCE,
                    )
                    try:
                        await ws.send_json({"type": "error", "message": "text frame too large"})
                    except Exception:
                        pass
                    break

                try:
                    ctrl = json.loads(msg.data)
                except ValueError:
                    continue  # Ignore non-JSON text frames

                if not isinstance(ctrl, dict):
                    continue

                msg_type = ctrl.get("type", "")

                if msg_type == "start":
                    if session is not None:
                        # Already started — send error but don't break
                        try:
                            await ws.send_json(
                                {
                                    "type": "error",
                                    "message": "session already started",
                                }
                            )
                        except Exception:
                            pass
                        continue

                    # Optional: bind this recording to an app's meeting so the WAV
                    # and live transcript land in that meeting's directory. Omitted
                    # for a recording that persists nothing (dictation, a voice
                    # note), which is why absent is allowed and empty is not.
                    storage_dir: Optional[Path] = None
                    raw_meeting_id = ctrl.get("meeting_id")
                    if raw_meeting_id is not None:
                        if not isinstance(raw_meeting_id, str) or not raw_meeting_id.strip():
                            try:
                                await ws.send_json(
                                    {
                                        "type": "error",
                                        "message": "meeting_id must be a non-empty string",
                                    }
                                )
                            except Exception:
                                pass
                            continue
                        storage_dir = _resolve_storage_dir(raw_meeting_id)
                        if storage_dir is None:
                            # Refuse rather than record into the void. A client that
                            # named a meeting expects a file at the end of it, and
                            # starting anyway would produce a recording that silently
                            # persisted nothing.
                            try:
                                await ws.send_json(
                                    {
                                        "type": "error",
                                        "message": "recording storage unavailable",
                                    }
                                )
                            except Exception:
                                pass
                            continue

                    session = RecordingSession(
                        storage_dir=storage_dir,
                        title=ctrl.get("title", ""),
                        language=ctrl.get("language", "en"),
                    )

                    registered = await register_session(session)
                    if not registered:
                        try:
                            await ws.send_json(
                                {
                                    "type": "error",
                                    "message": "too many concurrent recording sessions",
                                }
                            )
                        except Exception:
                            pass
                        session = None
                        break

                    try:
                        await session.start()
                    except InvalidTransitionError as exc:
                        logger.warning("Recording start failed: %s", exc)
                        await unregister_session(session.meeting_id)
                        try:
                            await ws.send_json({"type": "error", "message": str(exc)})
                        except Exception:
                            pass
                        session = None
                        break

                    try:
                        await ws.send_json(
                            {
                                "type": "ready",
                                "meeting_id": session.meeting_id,
                            }
                        )
                    except Exception:
                        pass

                elif msg_type == "pause":
                    if session is None:
                        continue
                    try:
                        await session.pause()
                    except InvalidTransitionError:
                        pass  # Ignore invalid transitions silently

                elif msg_type == "resume":
                    if session is None:
                        continue
                    try:
                        await session.resume()
                    except InvalidTransitionError:
                        pass

                elif msg_type == "stop":
                    if session is not None:
                        try:
                            await session.stop()
                        except InvalidTransitionError:
                            pass
                    break

                # Unknown control types are ignored (forward-compat).

            elif msg.type == WSMsgType.BINARY:
                if session is None or session.state != SessionState.RECORDING:
                    continue  # Discard audio before start or while paused

                pcm_data = msg.data

                # Write audio to WAV
                await session.write_audio(pcm_data)

                # Compute and emit RMS level at a throttled rate
                now = asyncio.get_event_loop().time()
                if now - last_level_time >= _LEVEL_INTERVAL_SECS:
                    last_level_time = now
                    rms = _compute_rms(pcm_data)
                    try:
                        await ws.send_json({"type": "level", "rms": round(rms, 4)})
                    except Exception:
                        break

            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    finally:
        # Cancel the deadline task if it hasn't fired yet.
        if deadline_task is not None:
            deadline_task.cancel()

        # Clean up the session
        if session is not None:
            # If still recording/paused, stop it
            if session.state in (SessionState.RECORDING, SessionState.PAUSED):
                try:
                    await session.stop()
                except (InvalidTransitionError, Exception):
                    pass
            await session.close()
            await unregister_session(session.meeting_id)

        # Audit BEFORE close — same rationale as stt_stream.py: ws.close()
        # awaits the peer's close ack under its own timeout, so a client that
        # already went away would otherwise hold the end event back.
        _emit_end_audit(caller, outcome="timeout" if _deadline_fired else "ok")
        if not ws.closed:
            try:
                await ws.close()
            except Exception:
                logger.exception("Failed to close recording WebSocket during cleanup")

    return ws
