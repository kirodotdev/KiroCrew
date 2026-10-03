"""Meetings adapter for the core recording storage registry.

Core owns the WebSocket but cannot turn a client-supplied meeting id into a
path. This app adapter performs that one operation through ``meeting_dir``,
which validates the id and contains the resulting path under the app data root.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.recording.recovery import register_meeting_store

logger = logging.getLogger("kirocrew.app.meetings")


class MeetingsRecordingStore:
    """Resolve app-owned meeting directories for core recording."""

    def __init__(self, app: Optional[web.Application] = None) -> None:
        self._app = app

    def _root(self, root: Optional[Path]) -> Optional[Path]:
        if root is not None:
            return root
        if self._app is None:
            return None
        injected = self._app.get("_meetings_data_root")
        return injected if isinstance(injected, Path) else None

    def resolve_meeting_dir(self, meeting_id: str, root: Optional[Path] = None) -> Optional[Path]:
        """Return a validated, contained and writable meeting directory."""
        try:
            mdir = store.meeting_dir(meeting_id, self._root(root))
        except store.MeetingsPathError:
            return None
        except Exception:  # pragma: no cover - defensive
            logger.warning("meetings: could not resolve a recording directory", exc_info=True)
            return None
        try:
            mdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("meetings: recording directory is not writable: %s", mdir)
            return None
        return mdir


def register(app: Optional[web.Application] = None) -> MeetingsRecordingStore:
    """Register this app's storage resolver, replacing any prior app binding."""
    adapter = MeetingsRecordingStore(app)
    register_meeting_store(adapter)
    return adapter
