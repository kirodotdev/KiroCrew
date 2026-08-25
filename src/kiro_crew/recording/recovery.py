"""Crash recovery for unfinished recording sessions.

A meeting whose metadata status is still ``recording`` at Gateway startup
indicates an abnormal termination while a session was active. This module
detects those meetings and exposes them, so a caller can offer the user a
choice: resume (keep the partial data and move on to post-processing) or
discard (mark failed, leaving the files on disk).

Storage lives in the app, not here. This package is core, so it must not
import an app package -- doing so would make the recording socket unusable
without that specific app installed, and would invert the dependency the
app registry is built on. Instead an app supplies a :class:`MeetingStore`
through :func:`register_meeting_store` at startup, and this module holds
only the four operations recovery actually needs.

A true live-audio resume is impossible after a process restart: the capture
stream belongs to the browser tab that is gone. "Resume" therefore means
"keep what was captured and continue processing it", never "carry on
recording".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

# Artifact names a session writes into its meeting directory. Recovery only
# probes for their presence, so a partial file still counts as "something was
# captured" -- which is the honest thing to tell the user.
AUDIO_FILENAME = "audio.wav"
LIVE_TRANSCRIPT_FILENAME = "transcript_local.json"


class MeetingStore(Protocol):
    """The slice of an app's meeting store that crash recovery depends on.

    Structural, not inherited: an app satisfies this by having the four
    methods, and does not import anything from here to do so.

    A meeting is a plain dict. Recovery reads ``id``, ``status``, ``title``,
    ``date`` and ``storage_path``, and treats every one of them as optional
    so a half-written metadata file cannot raise during startup scanning.
    """

    def list_meetings(self, root: Optional[Path] = None) -> list[dict[str, Any]]:
        """All meetings, newest first."""
        ...

    def get_meeting(self, meeting_id: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
        """One meeting, or ``None`` when it does not exist."""
        ...

    def update_meeting(
        self, meeting_id: str, patch: dict[str, Any], root: Optional[Path] = None
    ) -> Optional[dict[str, Any]]:
        """Merge ``patch`` into the meeting's metadata; return the result."""
        ...

    def resolve_meeting_dir(self, meeting_id: str, root: Optional[Path] = None) -> Optional[Path]:
        """The meeting's directory, or ``None`` when it cannot be resolved."""
        ...


_store: Optional[MeetingStore] = None


def register_meeting_store(store: MeetingStore) -> None:
    """Install the store recovery reads through.

    Called once by the owning app at startup. Registering twice replaces the
    previous store rather than raising: a gateway restart inside one process
    (as the tests do) must not be a fatal condition.
    """
    global _store
    _store = store


def get_meeting_store() -> Optional[MeetingStore]:
    """The registered store, or ``None`` when no app has installed one.

    Optional rather than raising, because the recording socket has a legitimate
    unregistered path: a session with no ``meeting_id`` (dictation, a voice note)
    persists nothing and needs no store. Recovery, which cannot work without one,
    uses :func:`_require_store` instead.
    """
    return _store


def _require_store() -> MeetingStore:
    if _store is None:
        raise RuntimeError(
            "no meeting store registered with kiro_crew.recording.recovery -- "
            "the owning app must call register_meeting_store() during startup"
        )
    return _store


@dataclass
class UnfinishedSession:
    """A meeting left in ``recording`` state after a crash."""

    meeting_id: str
    title: str
    date: str
    storage_path: str
    has_audio: bool
    has_live_transcript: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the API response."""
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "date": self.date,
            "storage_path": self.storage_path,
            "has_audio": self.has_audio,
            "has_live_transcript": self.has_live_transcript,
        }


def detect_unfinished_sessions(root: Optional[Path] = None) -> list[UnfinishedSession]:
    """Scan for meetings left in ``recording`` status.

    Returns them newest-first, following the store's own ordering, so the
    caller can offer resume or discard.
    """
    store = _require_store()
    results: list[UnfinishedSession] = []
    for meeting in store.list_meetings(root):
        if meeting.get("status") != "recording":
            continue

        meeting_id = meeting.get("id", "")
        mdir = store.resolve_meeting_dir(meeting_id, root)
        has_audio = False
        has_live_transcript = False
        if mdir is not None:
            has_audio = (mdir / AUDIO_FILENAME).is_file()
            has_live_transcript = (mdir / LIVE_TRANSCRIPT_FILENAME).is_file()

        results.append(
            UnfinishedSession(
                meeting_id=meeting_id,
                title=meeting.get("title", ""),
                date=meeting.get("date", ""),
                storage_path=meeting.get("storage_path", ""),
                has_audio=has_audio,
                has_live_transcript=has_live_transcript,
            )
        )

    if results:
        logger.info(
            "Detected %d unfinished recording session(s) from a previous run",
            len(results),
        )

    return results


def _transition_unfinished(
    meeting_id: str, to_status: str, root: Optional[Path], verb: str
) -> Optional[dict[str, Any]]:
    """Move an unfinished session to ``to_status``.

    Refuses anything not currently ``recording``, so a replayed request
    cannot drag a finished meeting backwards.
    """
    store = _require_store()
    meeting = store.get_meeting(meeting_id, root)
    if meeting is None or meeting.get("status") != "recording":
        return None

    updated = store.update_meeting(meeting_id, {"status": to_status}, root)
    if updated is not None:
        logger.info("%s unfinished session %s -> %s", verb, meeting_id, to_status)
    return updated


def resume_session(meeting_id: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Keep the partial capture and let post-processing continue.

    Moves the meeting to ``transcribing`` so batch transcription and
    summarization can run over whatever was captured. Returns the updated
    metadata, or ``None`` when the meeting is missing or is not in
    ``recording`` status.
    """
    return _transition_unfinished(meeting_id, "transcribing", root, "Resumed")


def discard_session(meeting_id: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Mark an unfinished session failed, keeping its files.

    The directory and its contents stay on disk so nothing is irreversibly
    lost; ``failed`` only hides the meeting from the active recording UI.
    Returns the updated metadata, or ``None`` when the meeting is missing or
    is not in ``recording`` status.
    """
    return _transition_unfinished(meeting_id, "failed", root, "Discarded")
