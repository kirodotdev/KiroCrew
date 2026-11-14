"""Mood Music's own storage: moments, the current queue and like/skip feedback.

One small JSON document in the app's data directory, written atomically. Every
record is bounded (:data:`MAX_MOMENTS`, :data:`MAX_FEEDBACK`, :data:`MAX_QUEUE`)
so the file stays a few hundred KB at most. Callers validate input first
(``routes.py``); the store trusts the shapes it is handed.

A document that cannot be parsed is moved aside to ``state.json.bad`` before the
next write replaces it, so a damaged file is never silently overwritten.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

MAX_MOMENTS = 200
MAX_FEEDBACK = 200
MAX_QUEUE = 12

#: The two feedback verdicts, and ``""`` to clear one.
VERDICTS = ("liked", "skipped", "")


def _empty() -> dict[str, Any]:
    return {"moments": [], "queue": [], "feedback": [], "last_source": ""}


class MoodStore:
    """The app's one JSON document, behind a lock."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()

    # ── file ──

    def _read(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # Keep the damaged bytes rather than overwrite them on the next write.
            self._path.replace(self._path.with_name(self._path.name + ".bad"))
            logger.warning("mood music state was unreadable; moved aside")
            return _empty()
        state = _empty()
        for key in ("moments", "queue", "feedback"):
            if isinstance(data.get(key), list):
                state[key] = [r for r in data[key] if isinstance(r, dict)]
        if isinstance(data.get("last_source"), str):
            state["last_source"] = data["last_source"]
        return state

    def _write(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(state, ensure_ascii=False), fsync=True)

    # ── reads ──

    def snapshot(self) -> dict[str, Any]:
        """Everything the page shows: newest moments and feedback first."""
        with self._lock:
            return self._read()

    # ── writes ──

    def add_moment(
        self, mood: str, *, note: str = "", tags: list[str] | None = None
    ) -> dict[str, Any]:
        """Record how the user feels right now."""
        moment = {
            "id": uuid.uuid4().hex,
            "mood": mood,
            "note": note,
            "tags": list(tags or []),
            "songs": [],
            "at": self._now(),
        }
        with self._lock:
            state = self._read()
            state["moments"] = [moment, *state["moments"]][:MAX_MOMENTS]
            self._write(state)
        return moment

    def set_queue(
        self,
        picks: list[dict[str, Any]],
        *,
        source: str,
        moment_id: str = "",
        mood_tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Replace the queue with the agent's new picks.

        *source* names the reply the picks came from, so the page never queues
        the same reply twice. When *moment_id* names a moment, the picks and
        the mood tags are attached to it for the Moments tab.
        """
        queue = [{**pick, "id": uuid.uuid4().hex, "feedback": ""} for pick in picks[:MAX_QUEUE]]
        with self._lock:
            state = self._read()
            state["queue"] = queue
            state["last_source"] = source
            for moment in state["moments"]:
                if moment.get("id") == moment_id:
                    moment["songs"] = [{"title": p["title"], "artist": p["artist"]} for p in queue]
                    if mood_tags:
                        moment["tags"] = list(mood_tags)
                    break
            self._write(state)
        return queue

    def set_feedback(self, pick_id: str, verdict: str) -> dict[str, Any] | None:
        """Mark a queued pick liked or skipped (``""`` clears it); ``None`` if unknown."""
        with self._lock:
            state = self._read()
            pick = next((p for p in state["queue"] if p.get("id") == pick_id), None)
            if pick is None:
                return None
            pick["feedback"] = verdict
            rest = [f for f in state["feedback"] if f.get("pick_id") != pick_id]
            if verdict:
                entry = {
                    "pick_id": pick_id,
                    "title": pick.get("title", ""),
                    "artist": pick.get("artist", ""),
                    "tags": pick.get("tags", []),
                    "verdict": verdict,
                    "at": self._now(),
                }
                rest = [entry, *rest]
            state["feedback"] = rest[:MAX_FEEDBACK]
            self._write(state)
            return pick
