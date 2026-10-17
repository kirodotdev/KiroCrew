"""The on-device providers behind :func:`kiro_crew.transcribe.transcribe_audio`.

The resident whisper.cpp recogniser (a WAV read with the stdlib, or an ffmpeg
transcode to 16 kHz mono, and the language it is told) and the Apple SpeechAnalyzer
lane, which owns its own conversion in :mod:`kiro_crew.apple_speech`.

:mod:`kiro_crew.transcribe` stays the import path and the patch surface; see
:mod:`kiro_crew.transcribe_ffmpeg`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import wave
from typing import TYPE_CHECKING

from kiro_crew import stt
from kiro_crew.transcribe_ffmpeg import (
    _close_ffmpeg_for_execution,
    _create_ffmpeg_subprocess,
    _describe_ffmpeg_exit,
    _forced_demuxer_args,
    _input_suffix,
    _kill_and_reap,
    _resolve_ffmpeg_for_execution,
    _unlink_if_exists,
)

if TYPE_CHECKING:
    import numpy as np

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.transcribe")


# ---------------------------------------------------------------------------
# The local recogniser
# ---------------------------------------------------------------------------

#: Shape of a language code whisper understands: two or three ASCII letters
#: (ISO 639-1 / 639-3), never a region. Anything outside it is treated as unset.
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")


#: Suffixes read with the stdlib WAV reader before ffmpeg is considered. Only the
#: suffix is trusted to decide whether to *try*; the reader itself decides whether
#: the bytes are usable, so a mislabelled file falls through to the transcode.
_WAV_SUFFIXES = (".wav", ".wave")


#: Longest audio a batch transcription reads into memory. At 16 kHz float32 this
#: is 4 bytes per sample, so an hour is ~230 MB. The point is to bound a
#: pathological input (a multi-hour recording, a corrupt container ffmpeg decodes
#: forever), not to limit a real voice memo, which is seconds to minutes long.
_MAX_AUDIO_SECS = 3600


def _whisper_language(language_code: str) -> str:
    """Reduce a BCP-47 tag to the bare language whisper wants (``en-US`` -> ``en``).

    Whisper names its languages by ISO 639 code with no region, so a configured
    locale has to be cut down to its primary subtag. An empty, unrecognisably
    shaped, or ``auto`` value returns ``""``, which the recogniser reads as
    auto-detect: a mistyped setting must cost the user a detection pass, never a
    failed transcription. The ``str()`` covers a hand-edited ``config.json``
    holding a non-string, which ``or ""`` would let through because it only
    substitutes on a falsy value.
    """
    primary = str(language_code or "").strip().split("-")[0].split("_")[0].lower()
    return primary if _LANGUAGE_RE.match(primary) else ""


def _make_temp_wav() -> str:
    """Create and close a temporary WAV file without leaking its descriptor."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def _pcm_from_wav(audio_path: str) -> np.ndarray | None:
    """Read a 16 kHz WAV as mono float32, or None when it needs transcoding.

    The dashboard's audio worklet and the recogniser already agree on 16 kHz mono
    int16, so audio that arrives in that form needs no external tool at all. Any
    other rate or sample width returns None so the caller hands it to ffmpeg,
    because resampling correctly is ffmpeg's job and a naive stride would change
    the pitch the model hears.
    """
    try:
        with wave.open(audio_path, "rb") as wav:
            channels = wav.getnchannels()
            if wav.getframerate() != stt.SAMPLE_RATE_HZ or wav.getsampwidth() != 2 or channels < 1:
                return None
            frames_total = min(wav.getnframes(), _MAX_AUDIO_SECS * stt.SAMPLE_RATE_HZ)
            if channels == 1:
                return stt.pcm_from_int16(wav.readframes(frames_total))
            # Fold to mono in BYTE-bounded slices. A whole-file read would hold
            # the interleaved int16 buffer AND its float32 conversion at once —
            # around 1.5 GiB for a four-channel hour, enough to OOM the
            # gateway on an input the 512 MiB import cap admits. And the bound
            # must be BYTES, not seconds: a frame is ``channels * 2`` bytes,
            # so a fixed frame count lets the channel
            # count scale the transient without limit — a valid 256-channel
            # minute under the same cap would make a "60-second" slice
            # allocate ~0.5 GiB raw plus its float32 conversion.
            # 8 MiB of raw int16 per slice keeps the transient under a
            # few tens of MiB for ANY channel count, while the result stays
            # the same: the per-frame mean is local to each frame, and
            # ``readframes`` counts whole frames, so no frame is ever split
            # across slices.
            chunk_frames = max(1, (8 * 1024 * 1024) // (channels * 2))
            folded = []
            remaining = frames_total
            while remaining > 0:
                take = min(chunk_frames, remaining)
                raw = wav.readframes(take)
                if not raw:
                    break
                remaining -= take
                pcm = stt.pcm_from_int16(raw)
                # Drop a final frame the file cut in half before folding
                # channels, so the reshape cannot fail on a truncated
                # recording.
                usable = pcm.size - (pcm.size % channels)
                if usable <= 0:
                    continue
                folded.append(pcm[:usable].reshape(-1, channels).mean(axis=1, dtype=pcm.dtype))
    except (OSError, EOFError, wave.Error):
        # Not a readable PCM WAV (a compressed payload, a truncated header, a
        # mislabelled suffix). ffmpeg reads far more than the stdlib does, so this
        # is a "try the other route", not a failure.
        return None
    if not folded:
        return None
    if len(folded) == 1:
        return folded[0]
    import numpy as np  # runtime import: module-level numpy is typing-only here

    return np.concatenate(folded)


async def _pcm_via_ffmpeg(audio_path: str, timeout_secs: int) -> np.ndarray | None:
    """Transcode *audio_path* to 16 kHz mono and return it as float32 samples.

    A Slack voice memo arrives as ogg/Opus and the dashboard records webm,
    neither of which the stdlib reads. Desktop releases supply the decoder;
    source installs use a system FFmpeg from fixed platform paths. The recogniser
    accepts exactly one format, so the transcode targets it directly rather than
    leaving a rate conversion for later.
    """
    try:
        # BEFORE the decoder handle is resolved: a refusal here must not leak
        # the authenticated FFmpeg descriptor the seam would otherwise own.
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to decode %s", audio_path)
        return None
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        logger.error(
            "the audio decoder is unavailable for %s; reinstall the Kiro Crew "
            "desktop app or install system FFmpeg for a source install",
            audio_path,
        )
        return None
    try:
        tmp_wav = await asyncio.to_thread(_make_temp_wav)
    except BaseException:
        await _close_ffmpeg_for_execution(ffmpeg_bin, preserve_active_exception=True)
        raise
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-y",
                *demux_args,
                "-i",
                audio_path,
                "-ar",
                str(stt.SAMPLE_RATE_HZ),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                # Bounds the temp file as well as the later read, so a container
                # that decodes forever cannot fill the disk while it does.
                "-t",
                str(_MAX_AUDIO_SECS),
                tmp_wav,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to decode %s", ffmpeg_bin, audio_path)
            return None
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error("ffmpeg decode of %s timed out after %ds", audio_path, timeout_secs)
            return None
        except BaseException:
            # ``CancelledError`` is a ``BaseException``, so the ``TimeoutError``
            # arm never sees it and an abandoned request would leave the child
            # running. Stop AND reap it before the ``finally`` removes the temp:
            # Windows keeps the output file locked until the child fully exits,
            # and on POSIX a live child can race the removal.
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
            logger.error(
                "ffmpeg %s decoding %s",
                _describe_ffmpeg_exit(proc.returncode, tail),
                audio_path,
            )
            return None
        return await asyncio.to_thread(_pcm_from_wav, tmp_wav)
    finally:
        # Off the loop, and scheduled as its own task BEFORE anything is
        # awaited, so a repeat cancellation landing on an await abandons only
        # the wait while the removal still runs to completion in its worker
        # thread. ``shield`` keeps that cancellation out of the removal task;
        # the exception itself still reaches the awaiter.
        rm = asyncio.ensure_future(asyncio.to_thread(_unlink_if_exists, tmp_wav))
        try:
            # The authenticated handle must outlive the spawn: every
            # path reaching this ``finally`` has already reaped the child
            # (``communicate`` on success and on a nonzero exit, kill-and-reap
            # on timeout and on cancellation) or never spawned one, so the
            # staged image can be released now. ``preserve_active_exception``
            # is set only while an exception is genuinely in flight, so a
            # cleanup failure never masks the original error and a cancellation
            # landing on the close await of a success path still propagates.
            await _close_ffmpeg_for_execution(
                ffmpeg_bin,
                preserve_active_exception=sys.exc_info()[1] is not None,
            )
        finally:
            await asyncio.shield(rm)


async def _transcribe_local(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe with the resident whisper.cpp recogniser.

    Everything expensive is shared with every other voice surface: one loaded
    model per process, so a Slack voice memo decodes on the weights a dashboard
    dictation just warmed rather than loading its own copy.
    """
    # Off the loop: the first probe links the recogniser's native extension, and
    # this coroutine is awaited from the Slack path and the transcribe endpoint.
    available = await asyncio.to_thread(stt.availability)
    if not available.ok:
        logger.error("Local speech recognition unavailable: %s", available.detail)
        return None

    pcm: np.ndarray | None = None
    if _input_suffix(audio_path) in _WAV_SUFFIXES:
        pcm = await asyncio.to_thread(_pcm_from_wav, audio_path)
    if pcm is None:
        pcm = await _pcm_via_ffmpeg(audio_path, stt_config.timeout_secs)
    if pcm is None or pcm.size == 0:
        logger.error("No audio could be decoded from %s", audio_path)
        return None

    # ``timeout_secs`` bounds the transcode above AND, inside the engine, each
    # decode and each model load separately. What it deliberately does NOT bound is
    # the first-run model download: that happens before the engine takes its lock,
    # so a slow transfer cannot be mistaken for a wedged decode and abandoned
    # mid-flight. The decode measures a real-time factor of 0.007-0.011, so the
    # ceiling only ever fires on a genuinely stuck native call.
    #
    # Both bounds are passed on every call because the recogniser is a singleton:
    # they are re-applied to the live instance rather than fixed by whichever
    # surface reached it first, which is what stops a Slack voice memo from pinning
    # the operator's settings to the package defaults.
    text, result = await stt.transcribe_pcm(
        pcm,
        model_name=stt_config.model,
        language=_whisper_language(stt_config.language_code),
        idle_evict_secs=stt_config.idle_evict_secs,
        timeout_secs=stt_config.timeout_secs,
    )
    if not result.ok:
        logger.error("Local speech recognition unavailable: %s", result.detail)
        return None
    # ``transcribe_pcm`` has already applied the hallucination filter, which can
    # empty a transcript that was entirely caption boilerplate, and answers ``""``
    # for an inaudible buffer. Both arrive with ``result.ok`` true, so they are
    # a SUCCESSFUL decode of a recording with nothing to write down; returned as
    # ``""`` so the caller can tell that from the None every failure above
    # returns. A channel adapter still reports a memo it could not hear, and
    # nothing writes boilerplate into an agent's notes.
    return text


async def _transcribe_apple(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe with Apple's on-device SpeechAnalyzer (macOS 26+).

    Delegates to :mod:`kiro_crew.apple_speech`, which owns the Swift-helper seam.
    The framework needs a language *locale* rather than whisper's bare language
    code, so ``stt_config.effective_language_code`` supplies BCP-47 (e.g. ``en-US``)
    even when the stored preference is automatic. The helper falls back to another
    installed dialect of the same language before it refuses.

    A supported host needs no model download because the OS ships the assets, so
    a failure here is a real error rather than the missing-model state the local
    recogniser can be in on a first run.
    """
    from kiro_crew import apple_speech

    text, metrics = await apple_speech.transcribe(
        audio_path,
        locale=stt_config.effective_language_code,
        timeout_secs=stt_config.timeout_secs or apple_speech.DEFAULT_TIMEOUT_SECS,
    )
    if text is None:
        logger.error("Apple speech transcription failed: %s", metrics.get("error", "unknown"))
        return None
    logger.debug(
        "Apple speech: %.2fs for %.1fs of audio (locale=%s)",
        metrics.get("transcribe_secs", 0.0),
        metrics.get("audio_secs", 0.0),
        metrics.get("locale", "?"),
    )
    return text
