"""Spoken replies: the per-conversation ``/voice`` toggle and the voice-note leg.

A turn whose answer has landed may also speak it (``telegram.voice_replies``, or the
conversation's own ``/voice on|off``). The text passes the shared display floor
before synthesis, and every synthesis or delivery failure is contained, because the
text answer has already gone out.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from kiro_crew.messaging.renderer import display_safe
from kiro_crew.voice_reply import synthesis_settings

if TYPE_CHECKING:
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

#: Answers shorter than this are not spoken. Speaking "Done." spends a message
#: and a notification to say less than the text bubble already did, and Telegram's
#: rate limit is per chat. Slack applies the same floor.
_VOICE_MIN_CHARS = 50


#: Container -> mime for the synthesizers that ship. Only OGG/Opus can take the
#: native voice-note bubble (``sendVoice``); anything else goes as ``sendAudio``,
#: which the client decides from the mime we declare here.
_AUDIO_MIMES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def _audio_mime(path: str) -> str:
    """Mime for a synthesized audio file, by extension.

    The extension is trustworthy HERE and nowhere else: this file was written by
    our own synthesizer into a temp dir, not named by the model. An inbound
    attachment is sniffed from its leading bytes instead.
    """
    return _AUDIO_MIMES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _read_bytes(path: str) -> bytes:
    """Read a synthesized audio file. Blocking; callers hand it to a thread."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        logger.warning("telegram: could not read synthesized audio", exc_info=True)
        return b""


def _voice_enabled(self: TelegramDispatcher, route: tuple[str, str]) -> bool:
    """Whether this conversation speaks its answers.

    Per-route ``/voice`` toggle first, then the configured default. Absent
    rather than pre-seeded so a later change to ``telegram.voice_replies``
    reaches every conversation the operator has not overridden.
    """
    pref = self._voice_pref.get(route)
    if pref is not None:
        return pref
    return bool(getattr(self._live_cfg().telegram, "voice_replies", False))


async def _handle_voice(
    self: TelegramDispatcher, route: tuple[str, str], chat_id: int, arg: str, thread: int | None
) -> None:
    """``/voice on|off`` — speak this conversation's answers, or stop.

    A bare ``/voice`` reports the current state rather than toggling: a
    toggle whose direction depends on state the user cannot see is how you end
    up turning voice ON in a room where you wanted it off.
    """
    want = arg.strip().lower()
    if want in ("on", "off"):
        self._voice_pref[route] = want == "on"
        state = "on" if want == "on" else "off"
        await self._reply(chat_id, f"🔊 Voice replies {state}.", thread=thread)
        return
    now = "on" if self._voice_enabled(route) else "off"
    await self._reply(
        chat_id,
        f"🔊 Voice replies are *{now}*. Use `/voice on` or `/voice off`.",
        thread=thread,
    )


async def _speak_reply(
    self: TelegramDispatcher, route: tuple[str, str], chat_id: int, text: str, thread: int | None
) -> None:
    """Synthesize *text* and send it as a voice/audio message. Never raises.

    Runs AFTER the text answer has landed, not instead of it: TTS depends on a
    local binary or a paid service, and an answer that only exists as audio is
    lost whenever either is unavailable. Sent silently, since the text reply
    already notified.

    Short answers are skipped — the same floor Slack applies. Speaking "Done."
    spends a message and a notification to say less than the text already did.

    Every failure is swallowed and logged: this is an enhancement on a turn
    that has already succeeded, so a TTS problem must not surface as a failed
    turn or re-post anything.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    if len(text) < _VOICE_MIN_CHARS or self.client is None:
        return
    # Through the shared display sink, like every other outbound Telegram text.
    # This leg bypasses the renderer, which is where a turn normally gets that
    # floor, and the driver's pass is BYTE-level: it sees `AKIA**IOSFODNN7...**`
    # as broken because the `**` sits inside the key. A synthesizer reads the
    # characters, not the markup, so the credential the byte pass missed would be
    # SPOKEN, and audio is the one egress a reader cannot un-see. Length is
    # checked first so a short answer costs no scan. Off-loop, because this is a
    # full credential/exfil pass over a whole answer.
    text = await asyncio.to_thread(display_safe, text)
    raw = getattr(self.cfg, "raw", {}) or {}
    section = raw.get("voice_reply") if isinstance(raw, dict) else None
    settings = synthesis_settings(section if isinstance(section, dict) else None)

    async def _deliver(path: str) -> bool:
        data = await asyncio.to_thread(_read_bytes, path)
        if not data:
            return False
        mid = await self.client.send_voice(  # type: ignore[union-attr]
            chat_id,
            data,
            filename=os.path.basename(path) or "reply.wav",
            mime=_audio_mime(path),
            message_thread_id=thread,
        )
        return mid is not None

    try:
        spoken = await facade.synthesize_and_deliver(_deliver, text, **settings)
    except Exception:
        logger.warning("telegram: voice reply failed for %s", route, exc_info=True)
        return
    if not spoken:
        logger.info("telegram: voice reply produced no audio for %s", route)
