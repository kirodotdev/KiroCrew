"""WebSocket handler for recording sessions.

``/api/ws/recording`` accepts binary audio frames (16 kHz Int16 LE mono PCM)
plus JSON control messages (start, pause, resume, stop), and emits ``ready``,
``level``, and ``error`` events back to the client. Audio goes IN; nothing
derived from it comes back out. Transcription is not this socket's job: the
browser already streams the same PCM to ``/api/ws/stt``, and the Meetings app
persists that transcript itself, so running a second recognizer here would
double the work (and, on AWS Transcribe, the bill) for a duplicate result.

``start`` takes an optional ``meeting_id``. When present, the recording's WAV
is written into that meeting's directory, resolved through the store an app
registered (see :mod:`kiro_crew.recording.recovery`) -- this package is core and
never turns a client-supplied id into a path itself. When the id cannot be
placed the start is REFUSED, because a client that named a meeting expects a
file at the end of it. When ``meeting_id`` is absent nothing is persisted,
which is the dictation and voice-note case. Every recording gets its own file
(``audio.wav``, then ``audio-2.wav``, ...): a second recording in the same
meeting never touches the first one's bytes.

Hardening mirrors ``stt_stream.py``:
- Origin check (``check_origin(require=True)``)
- Per-frame size caps (binary and text separately)
- Paired start/end audit events on every exit path, end emitted before close
- Concurrency cap (design: 1 simultaneous recording session)
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
from kiro_crew.dashboard.origin import (
    check_origin,
    is_direct_local_request,
    mark_audit_claimed,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.recording.recovery import get_meeting_store
from kiro_crew.recording.session import (
    MAX_CONCURRENT_SESSIONS,
    InvalidTransitionError,
    RecordingSession,
    SessionState,
    active_session_count,
    register_session,
    unregister_session,
)
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
# (e.g. ``{"type":"start","meeting_id":"event-42"}``). 1 KiB is generous.
_MAX_TEXT_FRAME_BYTES = 1024

# Ceiling on ONE recording's audio, independent of any STT provider. The
# per-frame and concurrency caps bound a message and a session count, not how
# long one session may stream, and a client that never sends ``stop`` would
# otherwise grow the file until the disk fills. Four hours of 16 kHz Int16 mono
# (32,000 bytes/s, ~461 MB) — the longest meeting the Meetings app allows — and
# well under the 4 GiB a RIFF size field can describe. On reaching it the
# recording is finalized normally, so what was captured is kept.
_MAX_RECORDING_SECS = 4 * 60 * 60
_MAX_RECORDING_BYTES = _MAX_RECORDING_SECS * 16_000 * 2

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
# WebSocket handler
# ---------------------------------------------------------------------------


async def api_ws_recording(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws/recording — audio recording WebSocket endpoint.

    Client sends binary PCM frames and JSON control messages.
    Server emits JSON events: ready, level, error.
    """
    # --- Guard: origin check ---
    if not check_origin(request, require=True):
        _emit_guard_audit(request.remote or "unknown", outcome="forbidden")
        # That record is the specific one; claim the request so the deny-audit
        # boundary does not add a second, generic entry for the same refusal.
        mark_audit_claimed(request)
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    # --- Guard: direct local request ---
    # Recording streams meeting audio to whatever host runs the Gateway.
    # When require_local_gateway is true (default), refuse connections from
    # remote clients even when a same-host proxy presents a loopback peer — the
    # operator must explicitly acknowledge remote capture by setting
    # recording.require_local_gateway = false.
    loop = asyncio.get_running_loop()
    cfg = await loop.run_in_executor(subprocess_executor(), KiroCrewConfig.load)
    if cfg.recording.require_local_gateway and not is_direct_local_request(request):
        remote = request.remote or ""
        _emit_guard_audit(remote or "unknown", outcome="forbidden_non_loopback")
        mark_audit_claimed(request)
        raise web.HTTPForbidden(
            text="Recording refused: gateway is not receiving a direct local request. "
            "Set recording.require_local_gateway = false to allow remote capture."
        )

    # --- Guard: concurrency cap ---
    if active_session_count() >= MAX_CONCURRENT_SESSIONS:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        mark_audit_claimed(request)
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

    session: Optional[RecordingSession] = None
    last_level_time: float = 0.0
    recorded_bytes = 0
    recording_failed = False

    try:
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
                        storage_dir = await loop.run_in_executor(
                            subprocess_executor(), _resolve_storage_dir, raw_meeting_id
                        )
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

                    session = RecordingSession(storage_dir=storage_dir)

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
                    except OSError:
                        # The exclusive create lost a race for the chosen name, or
                        # the directory went read-only between resolve and open.
                        # Either way no bytes were written and nothing existing
                        # was touched; refuse rather than record into the void.
                        logger.exception("Recording start failed: could not create the audio file")
                        await unregister_session(session.meeting_id)
                        try:
                            await ws.send_json(
                                {"type": "error", "message": "recording storage unavailable"}
                            )
                        except Exception:
                            pass
                        session = None
                        break

                    ready: dict[str, object] = {
                        "type": "ready",
                        "meeting_id": session.meeting_id,
                    }
                    # Which file this recording writes, so a client can tell a
                    # second recording apart from the first without listing the
                    # directory. Absent when nothing is persisted.
                    if session.audio_path is not None:
                        ready["audio_file"] = session.audio_path.name
                    try:
                        await ws.send_json(ready)
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

                # Enforce the per-recording ceiling: write what still fits (whole
                # samples only), then finalize and tell the client why it stopped.
                remaining = _MAX_RECORDING_BYTES - recorded_bytes
                if len(pcm_data) >= remaining:
                    head = pcm_data[: remaining - (remaining % 2)]
                    if head:
                        await session.write_audio(head)
                        recorded_bytes += len(head)
                    try:
                        await session.stop()
                    except InvalidTransitionError:
                        pass
                    try:
                        await ws.send_json(
                            {"type": "error", "message": "recording size limit reached"}
                        )
                    except Exception:
                        pass
                    break

                # Write audio to WAV
                await session.write_audio(pcm_data)
                recorded_bytes += len(pcm_data)

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

    except Exception:
        recording_failed = True
        logger.exception("Recording session failed")
        if not ws.closed:
            try:
                await ws.send_json({"type": "error", "message": "recording finalization failed"})
            except Exception:
                pass
    finally:
        if session is not None:
            try:
                if session.state in (SessionState.RECORDING, SessionState.PAUSED):
                    await session.stop()
                await session.close()
            except Exception:
                recording_failed = True
                logger.exception("Recording finalization failed during cleanup")
                if not ws.closed:
                    try:
                        await ws.send_json(
                            {"type": "error", "message": "recording finalization failed"}
                        )
                    except Exception:
                        pass
            finally:
                await unregister_session(session.meeting_id)

        # Audit BEFORE close — same rationale as stt_stream.py: ws.close()
        # awaits the peer's close ack under its own timeout, so a client that
        # already went away would otherwise hold the end event back.
        _emit_end_audit(caller, outcome="error" if recording_failed else "ok")
        if not ws.closed:
            try:
                await ws.close()
            except Exception:
                logger.exception("Failed to close recording WebSocket during cleanup")

    return ws
