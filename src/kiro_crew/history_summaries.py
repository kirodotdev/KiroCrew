"""The derived-summary sidecar caches beside a transcript.

The one-line summary and the intent summary, each valid only for the content signature
and generation it was made from.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.history_files import _safe_key, _safe_mtime
from kiro_crew.history_locks import HistoryLockTimeout

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


def _summary_cache_path(self: ConversationLog, key: str) -> Path:
    """Sidecar path for a session's cached one-line summary."""
    return self._dir / ".summaries" / f"{_safe_key(key)}.json"


def get_cached_summary(self: ConversationLog, key: str) -> str | None:
    """Return the cached one-line summary for *key* if still valid.

    Summaries are cached in a sidecar file — never in the session JSONL —
    so summarizing a session never rewrites (and therefore never risks
    clobbering, via a read-modify-write race with :meth:`append`) its
    conversation log. The cache is valid only while the session file's
    mtime matches the signature recorded when the summary was generated;
    any real append advances the mtime and invalidates it.
    """
    try:
        data = json.loads(self._summary_cache_path(key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    summary = data.get("summary")
    sig = self.session_mtime(key)
    if (
        sig is not None
        and data.get("sig") == sig
        and data.get("gen", 0) == self.rotation_generation(key)
        and isinstance(summary, str)
    ):
        return summary
    return None


def set_cached_summary(
    self: ConversationLog, key: str, summary: str, sig: float, generation: int | None = None
) -> None:
    """Persist a derived one-line *summary* to the sidecar cache.

    Keyed by the session file's mtime *sig* so a later append invalidates
    it. Atomic and side-effect-free with respect to the session JSONL —
    no read-modify-write, hence no data-loss race with a concurrent
    :meth:`append`.

    *generation* is :meth:`rotation_generation` captured at the same moment
    as *sig*, and must come from the caller for the same reason *sig* does:
    summary generation holds no lock while the model call is in flight, and
    a rewrite landing in that window preserves the mtime while advancing the
    generation. Reading the generation HERE would stamp the new content's
    identity onto the old summary and bless it as fresh — the exact
    staleness the generation was added to catch. ``None`` reads it at write
    time, which is only safe when no snapshot preceded the call.
    """
    atomic_write(
        self._summary_cache_path(key),
        json.dumps(
            {
                "sig": sig,
                "gen": (self.rotation_generation(key) if generation is None else generation),
                "summary": summary,
            }
        ),
    )


def _intent_summary_cache_path(self: ConversationLog, key: str) -> Path:
    """Sidecar path for a session's cached intent-structured summary.

    Deliberately a different file from :meth:`_summary_cache_path`: the
    one-line summary and the intent summary have independent writers and
    independent triggers, and sharing one file would reintroduce the
    read-modify-write race the sidecar design exists to avoid.
    """
    return self._dir / ".intents" / f"{_safe_key(key)}.json"


def get_cached_intent_summary(self: ConversationLog, key: str) -> dict | None:
    """Return the cached intent summary payload for *key* if still valid.

    Same mtime-signature contract as :meth:`get_cached_summary`: any real
    append advances the session file's mtime and invalidates the cache,
    while metadata-only rewrites preserve it. Returns the whole payload so
    the caller can read ``generated_at`` for display.
    """
    try:
        raw = self._intent_summary_cache_path(key).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
        return None
    sig = self.session_mtime(key)
    if sig is None or data.get("sig") != sig:
        return None
    if data.get("gen", 0) != self.rotation_generation(key):
        return None
    return data


def read_intent_summary(self: ConversationLog, key: str) -> tuple[dict | None, bool]:
    """Return ``(payload, stale)`` for a session's intent summary.

    Unlike :meth:`get_cached_intent_summary`, this does not discard a
    payload whose signature does not match — it reports it as stale
    instead. The panel prefers showing the last known summary marked as
    out of date over showing nothing, because an empty panel reads as
    "this feature is broken" while a stale one reads as "not regenerated
    yet", which is the truth.
    """
    try:
        raw = self._intent_summary_cache_path(key).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, False
    if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
        return None, False
    sig = self.session_mtime(key)
    fresh = (
        sig is not None
        and data.get("sig") == sig
        and data.get("gen", 0) == self.rotation_generation(key)
    )
    return data, not fresh


def set_cached_intent_summary(
    self: ConversationLog, key: str, payload: dict, sig: float, generation: int | None = None
) -> bool:
    """Persist a derived intent summary *payload* to its sidecar cache.

    Writes only the sidecar, never the session JSONL, so generating a
    summary cannot clobber the transcript or advance its mtime (which would
    both invalidate every other derived cache and reorder ``list_sessions``).

    The write happens under ``_locked`` and only if the transcript still
    exists with the *same* signature the generation started from. Generation
    holds no lock while the model call is in flight (it can take tens of
    seconds), so a permanent :meth:`delete_session` can complete in that
    window -- removing the transcript AND this sidecar. An unconditional
    write here would then recreate the sidecar, resurrecting deleted
    conversation data after the user was told it was gone. The sig equality
    check also drops a summary that a mid-generation append has already made
    stale, rather than storing it as the latest word.

    Returns True when the payload was written, False when it was refused
    (transcript deleted or changed, or the lock could not be acquired).
    Callers run this off the event loop (``asyncio.to_thread``) because
    ``_locked`` blocks.
    """
    try:
        with self._locked(key):
            if _safe_mtime(self._path(key)) != sig:
                return False
            current_generation = self.rotation_generation(key)
            if generation is not None and current_generation != generation:
                # A rewrite landed while the model call was in flight. It
                # PRESERVED the mtime, so the check above cannot see it —
                # the generation is the only signal that the summary now
                # describes replaced content. Refuse for the same reason a
                # changed mtime is refused: storing it would record a known
                # stale payload as the latest word.
                return False
            atomic_write(
                self._intent_summary_cache_path(key),
                json.dumps(
                    {
                        **payload,
                        "sig": sig,
                        "gen": (current_generation if generation is None else generation),
                    }
                ),
            )
            return True
    except HistoryLockTimeout:
        logger.warning("set_cached_intent_summary: lock timeout, not writing key=%s", key)
        return False
