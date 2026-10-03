"""Registry that lets core recording resolve app-owned storage safely.

The recording socket is core and must not import an app package. An app instead
registers the one operation core needs: resolving an opaque meeting id to a
validated, contained directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol


class MeetingStore(Protocol):
    """The app-owned storage operation used by the recording socket."""

    def resolve_meeting_dir(self, meeting_id: str, root: Optional[Path] = None) -> Optional[Path]:
        """Return the meeting directory, or ``None`` when it is not usable."""
        ...


_store: Optional[MeetingStore] = None


def register_meeting_store(store: MeetingStore) -> None:
    """Install the store used to place app-bound recordings.

    A second registration replaces the first because an in-process gateway
    restart constructs a fresh app and therefore a fresh data-home binding.
    """
    global _store
    _store = store


def get_meeting_store() -> Optional[MeetingStore]:
    """Return the registered store, or ``None`` when no app installed one."""
    return _store
