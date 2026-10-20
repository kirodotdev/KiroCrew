"""Voice memos in inbound Slack messages: download, transcription and the no-words note.

``_transcribe_with_reaction`` marks the message with a reaction while
``_transcribe_files`` downloads and transcribes its audio files, and
``_voice_memo_context`` adds one visible note per memo that produced no words.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New voice-memo work goes here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import TYPE_CHECKING

from kiro_crew.sel import sel
from kiro_crew.slack.files import (
    VOICE_MEMO_DURATION_UNVERIFIED,
    VOICE_MEMO_FAILED,
    VOICE_MEMO_TOO_LONG,
    VOICE_MEMO_UNAVAILABLE,
    is_voice_memo,
    voice_memo_notes,
)
from kiro_crew.transcribe import (
    audio_exceeds_secs,
    batch_duration_cap_secs,
    load_stt_config,
    transcribe_audio,
)

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.events")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Audio transcription helper
# ---------------------------------------------------------------------------

# What counts as a voice memo lives with the attachment adapter
# (``slack/files.py::is_voice_memo``) so the transcriber and the ingestion path
# cannot disagree about what is audio.


def _voice_memo_context(text: str, memos: int, transcribed: int, *, available: bool) -> str:
    """*text* plus one visible note per voice memo that produced no words.

    A memo whose transcription is unavailable or failed would otherwise be dropped
    in TOTAL silence: nothing appended to the prompt, so a voice-only message has
    no text at all and the turn never starts. The sender's send succeeded, so
    from their side that is indistinguishable from being ignored, and the agent
    is never told anything arrived. The note keeps both true: the turn runs,
    and it runs knowing a memo it cannot hear is what the user sent.

    The wording is the neutral half's (``slack/files.py`` pins it), so the same
    failure reads the same way whichever channel the memo came from.
    """
    missing = memos - transcribed
    if missing <= 0:
        return text
    note = VOICE_MEMO_UNAVAILABLE if not available else VOICE_MEMO_FAILED
    block = "\n\n".join(voice_memo_notes(missing, note))
    return f"{text}\n\n{block}" if text else block


async def _transcribe_with_reaction(
    slack_client: "SlackClientOps",
    channel: str,
    msg_ts: str,
    orch: "GatewayOrchestrator",
    files: list[dict],
) -> list[str]:
    """Transcribe audio files with a reaction indicator for user feedback."""
    _stt_reaction_added = False
    try:
        await slack_client.add_reaction(channel, msg_ts, "studio_microphone")
        _stt_reaction_added = True
    except Exception:
        logger.debug("Failed to add STT reaction", exc_info=True)

    try:
        transcripts = await _transcribe_files(orch, files)
    finally:
        if _stt_reaction_added:
            try:
                await slack_client.remove_reaction(
                    channel,
                    msg_ts,
                    "studio_microphone",
                )
            except Exception:
                logger.debug("Failed to remove STT reaction", exc_info=True)
    return transcripts


async def _transcribe_files(orch: "GatewayOrchestrator", files: list[dict]) -> list[str]:
    """Download and transcribe audio files, return list of transcription strings.

    What speech-to-text could hear, plus one pinned refusal note
    (:data:`VOICE_MEMO_TOO_LONG` / :data:`VOICE_MEMO_DURATION_UNVERIFIED`) per
    memo refused before transcription. A memo that produced nothing is reported
    by the caller, which knows how many arrived: see :func:`_voice_memo_context`.
    """
    results: list[str] = []
    for f in files:
        if not is_voice_memo(f):
            continue
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue
        dest: str | None = None
        try:
            raw_ft = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "webm"))
            suffix = "." + (raw_ft or "webm")
            fd, dest = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            assert orch.slack is not None
            assert dest is not None
            await orch.slack.download_file(url, dest)
            sel().log_api_access(
                caller="stt",
                operation="slack.download_file",
                outcome="success",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            stt_config = await asyncio.to_thread(load_stt_config)
            duration_cap = batch_duration_cap_secs(stt_config)
            if duration_cap is not None:
                exceeds = await audio_exceeds_secs(
                    dest, duration_cap, timeout_secs=stt_config.timeout_secs
                )
                if exceeds is not False:
                    note = VOICE_MEMO_DURATION_UNVERIFIED
                    error = "audio_duration_unverified"
                    if exceeds:
                        note = VOICE_MEMO_TOO_LONG.format(minutes=duration_cap // 60)
                        error = "audio_too_long"
                    results.append(note)
                    sel().log_api_access(
                        caller="stt",
                        operation="stt.transcribe",
                        outcome="denied",
                        source="transcribe",
                        resources=f.get("name", "?"),
                        error=error,
                    )
                    continue
            transcript = await transcribe_audio(dest, stt_config)
            sel().log_api_access(
                caller="stt",
                operation="stt.transcribe",
                outcome="success" if transcript else "empty",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            if transcript:
                results.append(transcript)
                logger.info("Transcribed voice memo: %d chars", len(transcript))
            else:
                logger.warning("Transcription returned empty for %s", f.get("name", "?"))
        except Exception:
            logger.exception("Failed to transcribe file %s", f.get("name", "?"))
            sel().log_api_access(
                caller="stt",
                operation="stt.transcribe",
                outcome="error",
                source="transcribe",
                resources=f.get("name", "?"),
                error="transcription_failed",
            )
        finally:
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    return results
