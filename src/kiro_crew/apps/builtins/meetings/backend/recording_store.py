"""Meetings — the store adapter the core recording package reads through.

``kiro_crew.recording`` is core, so it must not import this app: doing so would
make ``/api/ws/recording`` unusable without Meetings installed and would invert
the dependency the app registry is built on. Core therefore declares a
structural :class:`kiro_crew.recording.recovery.MeetingStore` protocol and an
app fills it in at startup. This module is Meetings' implementation.

What it is used for today:

* :meth:`MeetingsRecordingStore.resolve_meeting_dir` — the recording socket asks
  where a meeting's ``audio.wav`` and ``transcript_local.json`` belong. This is
  the ONLY thing that turns the socket's client-supplied ``meeting_id`` into a
  path, and it does so exclusively through :func:`store.meeting_dir`, which runs
  ``safe_meeting_id`` and then ``contain``. A rejected id becomes ``None`` here
  rather than an exception, because the protocol is declared that way and core
  must not have to know this app's error type.

The other three methods exist because crash recovery
(``recording.recovery.detect_unfinished_sessions`` and friends) is written
against the full protocol. Recovery is not wired into the gateway yet — nothing
calls it — so those three are currently exercised only by their own tests.

One mismatch is deliberate and worth stating plainly: recovery looks for
meetings whose ``status`` is ``"recording"``, and this app's lifecycle has no
such status (``idle``/``active``/``paused``/``reviewing``/``ended``, gated by
``ALLOWED_TRANSITIONS``). So ``detect_unfinished_sessions`` will find nothing
until the two vocabularies are reconciled. Inventing a ``recording`` status here
would have to go through that state machine and is a change to the meeting
lifecycle, not to this adapter -- it is left for the crash-recovery work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.recording.recovery import register_meeting_store

logger = logging.getLogger("kirocrew.app.meetings")


class MeetingsRecordingStore:
    """Adapts :mod:`..store` to core's ``MeetingStore`` protocol.

    Structural conformance only -- nothing here inherits from the protocol, and
    core is not imported for typing purposes beyond the registration call.
    """

    def __init__(self, app: Optional[web.Application] = None) -> None:
        # Held so the test data-root override can be honoured. Read lazily rather
        # than captured at construction: ``_meetings_data_root`` is set on the
        # Application by the test helper, and registration order relative to that
        # is not something this adapter should depend on.
        self._app = app

    def _root(self, root: Optional[Path]) -> Optional[Path]:
        """An explicit *root* wins; otherwise the app's test override, else None.

        ``None`` means "the real app data dir" to every ``store`` function, which
        is the production case.
        """
        if root is not None:
            return root
        if self._app is None:
            return None
        injected = self._app.get("_meetings_data_root")
        return injected if isinstance(injected, Path) else None

    # ── the method the recording socket uses ────────────────────────────────

    def resolve_meeting_dir(self, meeting_id: str, root: Optional[Path] = None) -> Optional[Path]:
        """Where a recording's files belong, or ``None`` if the id is not usable.

        Every rejection collapses to ``None``: an illegal id (already SEL-audited
        by ``safe_meeting_id``), a path that escaped the data root (audited by
        ``contain``), or an unwritable directory. The caller's contract is only
        "a directory or nothing", and a recording that cannot be placed safely
        must not be placed at all.

        The directory is created here rather than left to the session, so a
        failure to create it is reported as "no directory" at resolve time
        instead of surfacing mid-recording on the first audio frame.
        """
        try:
            mdir = store.meeting_dir(meeting_id, self._root(root))
        except store.MeetingsPathError:
            # Already audited at the point of rejection; nothing to add.
            return None
        except Exception:  # pragma: no cover — defensive
            logger.warning("meetings: could not resolve a recording directory", exc_info=True)
            return None
        try:
            mdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("meetings: recording directory is not writable: %s", mdir)
            return None
        return mdir

    # ── the rest of the protocol (crash recovery) ──────────────────────────

    def list_meetings(self, root: Optional[Path] = None) -> list[dict[str, Any]]:
        """All meetings, with ``event_id`` surfaced as ``id``.

        The rename is the whole reason this is not just ``store.list_meetings``:
        this app's metadata calls the key ``event_id`` (a meeting is usually a
        calendar event), and core reads ``id``. Both are emitted so neither side
        has to care.
        """
        summaries = store.list_meetings(self._root(root))
        for summary in summaries:
            summary["id"] = summary.get("event_id", "")
        return summaries

    def get_meeting(self, meeting_id: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
        """One meeting's metadata, or ``None`` when it does not exist."""
        try:
            meta = store.read_meeting_meta(meeting_id, self._root(root))
        except store.MeetingsPathError:
            return None
        if meta is None:
            return None
        meta = dict(meta)
        meta["id"] = meta.get("event_id", meeting_id)
        return meta

    def update_meeting(
        self, meeting_id: str, patch: dict[str, Any], root: Optional[Path] = None
    ) -> Optional[dict[str, Any]]:
        """Merge *patch* into a meeting's metadata; return the result.

        Taken under ``store.meta_transaction()`` for the same reason every other
        metadata mutation is: the read and the write have to be in one critical
        section, or a concurrent request's write is clobbered and both report
        success. ``atomic_write`` does not help -- the write was always atomic,
        the read-modify-write around it was not.
        """
        resolved = self._root(root)
        try:
            with store.meta_transaction():
                meta = store.read_meeting_meta(meeting_id, resolved)
                if meta is None:
                    return None
                meta.update(patch)
                store.write_meeting_meta(meeting_id, meta, resolved)
        except store.MeetingsPathError:
            return None
        meta = dict(meta)
        meta["id"] = meta.get("event_id", meeting_id)
        return meta


def register(app: Optional[web.Application] = None) -> MeetingsRecordingStore:
    """Install this app as core's meeting store. Idempotent by replacement.

    Called from ``register_routes``. Registering twice replaces the previous
    store rather than raising, which is what lets a second gateway inside one
    process (as the tests do) start cleanly.
    """
    adapter = MeetingsRecordingStore(app)
    register_meeting_store(adapter)
    return adapter
