"""``kirocrew.voice.*`` -- how dictation and spoken replies are used, and how they end.

Five counters, each answering one question a speech provider's own backend cannot:

=====================================  ===================================================
counter                                one increment per
=====================================  ===================================================
``kirocrew.voice.stt.started``         dictation session that passed its guards
                                       (``provider``)
``kirocrew.voice.stt.ended``           dictation session, at its single end
                                       (``provider``, ``outcome``, ``duration``,
                                       ``first_partial``, ``transcript``)
``kirocrew.voice.stt.errors``          ``error`` frame the socket reports, delivered
                                       or not (``provider``, ``code``)
``kirocrew.voice.unavailable``         dictation refused before a session started
                                       (``reason``)
``kirocrew.voice.tts.replies``         ``POST /api/voice/synthesize`` answered
                                       (``provider``, ``outcome``, ``first_audio``)
=====================================  ===================================================

The provider in use on each install is a gauge, not a counter:
``kirocrew.inventory.voice.provider`` (:mod:`kiro_crew.metrics.inventory_gauges`).

**Every attribute value is closed.** Each list is a tuple below, and anything
outside it maps to :data:`OTHER`. No transcript, reply text, voice id, language,
device name, error message or account is ever an attribute. A provider an edition
registers through the pluggable-speech seam is one bucket, :data:`PROVIDER_REGISTERED`:
its name is chosen by that edition, not by this project.

**Latency is a bucket attribute on a counter**, never a histogram: a product
destination takes counters and gauges only (``metrics/product_metrics.py``).
``first_partial`` is measured from the socket opening, so it includes model
warm-up and provider connect, which is the wait the person sees.

**One session, one end.** The dictation socket already balances its audit trail
(``stt_stream_end`` exactly once per ``stt_stream_start``, see
``dashboard/stt_stream._emit_end_audit``); :func:`stt_session_end` hangs off that
same call, so ``ended`` cannot double-count or go missing where the audit does not.
The session is carried in a :class:`contextvars.ContextVar` so the relay tasks the
socket handler spawns (each copies the handler's context) report to it without a
parameter threaded through every send site.

**Recording never changes what it measures.** Every function swallows its own
failure after a debug log; telemetry consent gates every emit, because
``emit_counter`` resolves a no-op recorder when telemetry is off.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

VOICE_STT_STARTED = "kirocrew.voice.stt.started"
VOICE_STT_ENDED = "kirocrew.voice.stt.ended"
VOICE_STT_ERRORS = "kirocrew.voice.stt.errors"
VOICE_UNAVAILABLE = "kirocrew.voice.unavailable"
VOICE_TTS_REPLIES = "kirocrew.voice.tts.replies"

OTHER = "other"
NONE = "none"

KIND_STT = "stt"
KIND_TTS = "tts"
KIND_VALUES: tuple[str, ...] = (KIND_STT, KIND_TTS)

#: Any provider an edition registered through the speech seam.
PROVIDER_REGISTERED = "registered"

#: Built-in dictation providers (``config.sections_stt._VALID_STT_PROVIDERS``).
STT_PROVIDER_VALUES: tuple[str, ...] = ("local", "apple", "transcribe", "codex", "off")

#: Built-in reply providers (``voice_reply.VALID_PROVIDERS``).
TTS_PROVIDER_VALUES: tuple[str, ...] = ("system", "polly")

#: How a dictation session ended: the ``stt_stream_end`` audit outcomes.
#: ``timeout`` is a session that reached its length limit, core's or the
#: provider's own.
STT_OUTCOME_VALUES: tuple[str, ...] = ("ok", "error", "timeout", "refused")

#: Session length, from socket open to end.
DURATION_VALUES: tuple[str, ...] = ("lt_10s", "10_60s", "1_5m", "5_15m", "15_60m", "gt_60m")
_DURATION_EDGES: tuple[tuple[float, str], ...] = (
    (10.0, "lt_10s"),
    (60.0, "10_60s"),
    (300.0, "1_5m"),
    (900.0, "5_15m"),
    (3600.0, "15_60m"),
)

#: Time to the first partial (dictation) or first audio chunk (replies).
LATENCY_VALUES: tuple[str, ...] = ("lt_500ms", "500ms_1s", "1_2s", "2_5s", "gt_5s", NONE)
_LATENCY_EDGES: tuple[tuple[float, str], ...] = (
    (0.5, "lt_500ms"),
    (1.0, "500ms_1s"),
    (2.0, "1_2s"),
    (5.0, "2_5s"),
)

#: Whether any non-empty transcript reached the client.
TRANSCRIPT_VALUES: tuple[str, ...] = ("nonempty", "empty")

#: Every code an ``error`` frame on ``/api/ws/stt`` can carry: the socket's own
#: (``dashboard/stt_stream._CODE_*``) and the recognisers' (``stt.engine`` /
#: ``stt.preflight`` / ``stt.decoder`` / ``stt.codex`` / ``stt.codex_rpc``). A
#: test reads those modules so a new code fails there rather than being counted
#: as ``other``.
STT_ERROR_CODES: tuple[str, ...] = (
    "stt_max_duration_exceeded",
    "stt_session_failed",
    "stt_streaming_disabled",
    "stt_consent_required",
    "stt_transcribe_vocabulary_rejected",
    "stt_aws_access_denied",
    "stt_transcribe_sdk_outdated",
    "stt_extra_missing",
    "stt_no_wheel_for_platform",
    "stt_import_failed",
    "stt_model_missing",
    "stt_decode_failed",
    "stt_unsupported_cpu",
    "stt_load_crashed",
    "stt_native_probe_crashed",
    "decoder_unsupported_platform",
    "decoder_download_disabled",
    "decoder_payload_unverified",
    "decoder_install_failed",
    "stt_codex_missing",
    "stt_codex_extra_missing",
    "stt_codex_cli_unresolved",
    "stt_codex_login_required",
    "stt_codex_voice_unavailable",
    "stt_codex_failed",
    "stt_codex_sandbox_unavailable",
)

#: Why ``/api/ws/stt`` refused before a session started.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    "forbidden_origin",
    "stt_disabled",
    "provider_off",
    "streaming_off",
    "provider_not_streaming",
    "busy",
)

#: How a reply request was answered.
TTS_OUTCOME_VALUES: tuple[str, ...] = (
    "ok",
    "cancelled",
    "busy",
    "invalid",
    "unavailable",
    "sandbox_refused",
    "error",
)


def _member(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def _bucket(seconds: Optional[float], edges: tuple[tuple[float, str], ...], top: str) -> str:
    if seconds is None:
        return NONE
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return OTHER
    for edge, label in edges:
        if seconds < edge:
            return label
    return top


def duration_bucket(seconds: Optional[float]) -> str:
    """*seconds* as a member of :data:`DURATION_VALUES` (``none`` when unknown)."""
    return _bucket(seconds, _DURATION_EDGES, "gt_60m")


def latency_bucket(seconds: Optional[float]) -> str:
    """*seconds* as a member of :data:`LATENCY_VALUES`; ``none`` when it never happened."""
    return _bucket(seconds, _LATENCY_EDGES, "gt_5s")


def provider_label(kind: str, name: Any) -> str:
    """A built-in provider as itself, an edition-registered one as ``registered``.

    Never raises; an unknown name is :data:`OTHER`. Registration is asked of the
    speech seam lazily, so importing this module stays cheap.
    """
    try:
        if not isinstance(name, str):
            return OTHER
        builtins = STT_PROVIDER_VALUES if kind == KIND_STT else TTS_PROVIDER_VALUES
        if name in builtins:
            return name
        from kiro_crew import speech_providers

        lookup = (
            speech_providers.stt_provider if kind == KIND_STT else speech_providers.tts_provider
        )
        return PROVIDER_REGISTERED if lookup(name) is not None else OTHER
    except Exception:
        logger.debug("voice provider label failed", exc_info=True)
        return OTHER


def _now() -> float:
    """The clock every reading here uses; a test replaces it to stay deterministic."""
    return time.monotonic()


def _emit(name: str, attrs: dict[str, str | int | bool | float]) -> None:
    """Add 1 to *name*; never raises."""
    try:
        from kiro_crew.metrics.events import emit_counter

        emit_counter(name, attrs)
    except Exception:  # telemetry must never break the measured path
        logger.debug("voice counter emit failed for %s", name, exc_info=True)


# -- dictation ----------------------------------------------------------------


@dataclass
class SttSession:
    """One dictation socket's readings, from guard pass to end."""

    provider: str
    started: float = field(default_factory=lambda: _now())
    first_partial: Optional[float] = None
    nonempty: bool = False
    ended: bool = False


_SESSION: "contextvars.ContextVar[Optional[SttSession]]" = contextvars.ContextVar(
    "kirocrew_voice_stt_session", default=None
)


def stt_session_begin(provider: Any) -> None:
    """Start tracking this socket's session and count it; never raises."""
    try:
        session = SttSession(provider=provider_label(KIND_STT, provider))
        _SESSION.set(session)
        _emit(VOICE_STT_STARTED, {"provider": session.provider})
    except Exception:
        logger.debug("voice stt begin failed", exc_info=True)


def observe_stt_frame(frame: Any) -> None:
    """Note one frame the socket is about to send; never raises.

    A ``partial`` or ``final`` with text marks the transcript non-empty (the first
    one also stamps time-to-first-text); an ``error`` counts its code.
    """
    try:
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        session = _SESSION.get()
        if kind in ("partial", "final"):
            if session is None:
                return
            text = frame.get("text")
            if isinstance(text, str) and text.strip():
                if session.first_partial is None:
                    session.first_partial = _now() - session.started
                session.nonempty = True
        elif kind == "error":
            _emit(
                VOICE_STT_ERRORS,
                {
                    "provider": session.provider if session is not None else NONE,
                    "code": _member(frame.get("code"), STT_ERROR_CODES),
                },
            )
    except Exception:
        logger.debug("voice stt frame observe failed", exc_info=True)


def stt_session_end(outcome: Any) -> None:
    """Count this socket's session as ended with *outcome*, once; never raises."""
    try:
        session = _SESSION.get()
        if session is None or session.ended:
            return
        session.ended = True
        _emit(
            VOICE_STT_ENDED,
            {
                "provider": session.provider,
                "outcome": _member(outcome, STT_OUTCOME_VALUES),
                "duration": duration_bucket(_now() - session.started),
                "first_partial": latency_bucket(session.first_partial),
                "transcript": "nonempty" if session.nonempty else "empty",
            },
        )
    except Exception:
        logger.debug("voice stt end failed", exc_info=True)


def guard_reason(stt_config: Any) -> str:
    """Why the streaming guard refused *stt_config*, as an :data:`UNAVAILABLE_REASONS` member."""
    try:
        if not stt_config.enabled:
            return "stt_disabled"
        if stt_config.provider == "off":
            return "provider_off"
        if not stt_config.streaming:
            return "streaming_off"
        return "provider_not_streaming"
    except Exception:
        return OTHER


def record_stt_unavailable(reason: Any) -> None:
    """One dictation socket refused before a session started; never raises."""
    _emit(VOICE_UNAVAILABLE, {"reason": _member(reason, UNAVAILABLE_REASONS)})


# -- spoken replies -----------------------------------------------------------


@dataclass
class TtsRequest:
    """One synthesize request's first-audio reading."""

    started: float = field(default_factory=lambda: _now())
    first_audio: Optional[float] = None


_TTS: "contextvars.ContextVar[Optional[TtsRequest]]" = contextvars.ContextVar(
    "kirocrew_voice_tts_request", default=None
)


def tts_request_begin() -> "contextvars.Token[Optional[TtsRequest]] | None":
    """Start timing this synthesize request; never raises."""
    try:
        return _TTS.set(TtsRequest())
    except Exception:
        logger.debug("voice tts begin failed", exc_info=True)
        return None


def note_tts_chunk() -> None:
    """Stamp the first ``voice_chunk`` this request broadcasts; never raises."""
    try:
        req = _TTS.get()
        if req is not None and req.first_audio is None:
            req.first_audio = _now() - req.started
    except Exception:
        logger.debug("voice tts chunk note failed", exc_info=True)


def tts_outcome(status: Any, code: Any) -> str:
    """The :data:`TTS_OUTCOME_VALUES` member for an HTTP *status* and error *code*."""
    if status == 200:
        return "ok"
    if status == 409:
        return "cancelled"
    if status == 429:
        return "busy"
    if status == 400:
        return "invalid"
    if isinstance(code, str):
        if code == "voice_unavailable":
            return "unavailable"
        if code.startswith("sandbox_"):
            return "sandbox_refused"
    return "error"


def tts_request_end(
    provider: Any,
    status: Any,
    code: Any,
    timing: "contextvars.Token[Optional[TtsRequest]] | None" = None,
) -> None:
    """Count one answered synthesize request; never raises."""
    try:
        req = _TTS.get()
        _emit(
            VOICE_TTS_REPLIES,
            {
                "provider": provider_label(KIND_TTS, provider),
                "outcome": tts_outcome(status, code),
                "first_audio": latency_bucket(req.first_audio if req is not None else None),
            },
        )
    except Exception:
        logger.debug("voice tts end failed", exc_info=True)
    finally:
        if timing is not None:
            try:
                _TTS.reset(timing)
            except Exception:
                pass
