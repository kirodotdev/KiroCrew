"""Text-to-speech on an edition-registered provider (``PlatformContext.speech``).

Split out of :mod:`kiro_crew.voice_reply`, which re-exports every public name here
and stays the import path callers use. A registered provider yields
``AudioChunk`` values; this module bounds and checks them, joins them into one
playable file, and hands the provider a read-only copy of the voice settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("kiro_crew.voice_reply")


def registered_voice_config(
    provider: str,
    *,
    voice_id: str = "",
    rate: str = "",
    pitch: str = "",
    system_voice: str = "",
    language_code: str = "",
) -> dict[str, str]:
    """The ``voice_config`` an edition TTS provider receives: the settings in force.

    A plain dict of strings, never the live settings object, so a provider can
    read the user's choices but cannot change them. ``language_code`` is the
    ``voice_reply.language_code`` BCP-47 tag, or ``""`` for the provider's default.
    """
    return {
        "provider": provider,
        "voice": voice_id,
        "rate": rate,
        "pitch": pitch,
        "system_voice": system_voice,
        "language_code": language_code,
    }


#: Upper bound on the audio one edition synthesis may return, so a provider that
#: never stops yielding cannot exhaust memory or the temp volume.
_MAX_REGISTERED_AUDIO_BYTES = 64 * 1024 * 1024
#: The two audio formats the dashboard plays, and so the two an edition may yield.
_MIME_MP3 = "audio/mpeg"
_MIME_WAV = "audio/wav"


async def iter_registered_chunks(
    provider: Any, text: str, voice_config: dict[str, str]
) -> "AsyncIterator[Any]":
    """Yield each well-formed audio chunk from *provider*, bounded in total size.

    A chunk with no bytes is skipped. A chunk whose ``mime`` is not ``audio/mpeg``
    or ``audio/wav`` (the two the dashboard plays), or differs from the first
    chunk's, ends the stream with :class:`VoiceSynthesisError`, as does passing
    the size bound. One format per stream is what lets :func:`join_audio` build
    the replay clip.
    """
    from kiro_crew.voice_reply import VoiceSynthesisError  # circular: it re-exports this module

    total = 0
    first_mime: str | None = None
    # Iterated on this loop; the provider contract allows no loop-bound state
    # across calls.
    stream = provider.synthesize(text, voice_config)
    try:
        async for chunk in stream:
            audio = getattr(chunk, "audio", None)
            mime = getattr(chunk, "mime", None)
            if not isinstance(audio, (bytes, bytearray)) or not audio:
                continue
            if mime not in (_MIME_MP3, _MIME_WAV) or first_mime not in (None, mime):
                raise VoiceSynthesisError(
                    "voice_synthesis_failed", "The voice provider returned an unsupported format."
                )
            first_mime = mime
            total += len(audio)
            if total > _MAX_REGISTERED_AUDIO_BYTES:
                raise VoiceSynthesisError(
                    "voice_synthesis_failed", "The voice provider returned too much audio."
                )
            yield chunk
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()


def join_audio(chunks: list[tuple[bytes, str]]) -> tuple[bytes, str] | None:
    """One playable file's bytes and suffix from *chunks* of a single format.

    MP3 frames concatenate as they are. WAV chunks are joined on their PCM, so
    each must share the first chunk's channels, width and rate. Mixed or
    mismatched chunks answer ``None``.
    """
    import io
    import wave

    mimes = {mime for _, mime in chunks}
    if len(mimes) != 1:
        return None
    if mimes == {_MIME_MP3}:
        return b"".join(audio for audio, _ in chunks), ".mp3"
    params = None
    frames: list[bytes] = []
    try:
        for audio, _ in chunks:
            with wave.open(io.BytesIO(audio), "rb") as w:
                shape = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = shape
                elif shape != params:
                    return None
                frames.append(w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return None
    assert params is not None
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(b"".join(frames))
    return out.getvalue(), ".wav"


async def _synthesize_registered(
    provider: Any, text: str, voice_config: dict[str, str]
) -> str | None:
    """Speak *text* on an edition provider into one temp file; ``None`` on failure.

    Same contract as the built-in branches: a temp file path the caller deletes,
    or ``None`` -- never an exception. The text is already redacted.
    """
    chunks: list[tuple[bytes, str]] = []
    try:
        async for chunk in iter_registered_chunks(provider, text, voice_config):
            chunks.append((bytes(chunk.audio), chunk.mime))
    except Exception:
        logger.warning("TTS provider %r failed", getattr(provider, "name", "?"), exc_info=True)
        return None
    if not chunks:
        return None
    # Off the loop: parsing and rewriting up to the size bound is a long copy.
    joined = await asyncio.to_thread(join_audio, chunks)
    if joined is None:
        logger.warning(
            "TTS provider %r returned audio that cannot be joined", getattr(provider, "name", "?")
        )
        return None
    audio, suffix = joined

    def _write() -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return path

    # Shielded: a cancelled caller would otherwise lose the path while the thread
    # still creates the file, leaving the spoken answer in the temp directory.
    writer = asyncio.ensure_future(asyncio.to_thread(_write))
    try:
        return await asyncio.shield(writer)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            path = await writer
            await asyncio.to_thread(_unlink_quietly, path)
        raise


def _unlink_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
