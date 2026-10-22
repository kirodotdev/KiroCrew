"""What the agent surface remembers about each code channel.

One small JSON document, ``routing/slack-code-channel-surface.json`` under the
Kiro Crew data home, keyed by channel id. It holds pointers and
digests only: the agent's own context-bar items, the plan canvas id, the digest of
the command set last registered, and which message is the current wrap-up. The
wrap-up and archive STAMPS live on the binding (``Binding.wrapped_up_at`` /
``archived_at``), not here. Never message
content beyond the agent's short context-bar labels.

Every write rewrites the file atomically under a lock, and the in-memory copy
(authoritative for reads) changes only once that write succeeded: a failed write
raises and changes nothing. A missing or unreadable file reads as empty -- it
never crashes the gateway -- and the oldest records are dropped past
:data:`MAX_RECORDS`. Blocking: coroutines use :func:`get_async` /
:func:`update_async`.

The gateway acts on these pointers for a channel (which canvas to update, which
message's Archive button is live), so no agent may write it. It sits beside the
core's binding record in the data home's ``routing`` directory, which the
security floors already fence as a whole: agent file tools refuse it, every
sandbox masks the directory (and the temp file ``atomic_write`` publishes
through), and it is pre-created so the mask holds on a fresh install. Only the
gateway opens it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

STORE_FILENAME = "slack-code-channel-surface.json"
MAX_RECORDS = 1000


@dataclass(frozen=True)
class SurfaceRecord:
    """One code channel's agent-surface state."""

    channel_id: str
    #: The agent's own context-bar items (``key``/``label``/``icon``/``url``),
    #: merged after the workspace's repo/branch/PR/CI items on every publish.
    context_bar: tuple[dict[str, str], ...] = ()
    #: The channel canvas this gateway created for the plan ("" before one).
    plan_canvas_id: str = ""
    #: The canvas view tab attached for it ("" before one).
    plan_view_id: str = ""
    #: Digest of the command set last registered with Slack.
    commands_digest: str = ""
    #: A Slack answer that switched commands off for this channel.
    commands_off: str = ""
    #: When a ``/wrap-up`` asked the agent for a summary that has not been posted.
    wrapup_pending_since: float = 0.0
    wrapup_requested_by: str = ""
    #: The ts of the wrap-up message carrying the Archive button.
    summary_ts: str = ""
    #: The session the wrap-up was posted for; a replaced session's button is stale.
    summary_session_key: str = ""
    updated_at: float = field(default=0.0)


_FIELDS = {f.name for f in fields(SurfaceRecord)}


def _default_path() -> Path:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.slack.code_channels.store import STORE_DIRNAME

    return config_dir() / STORE_DIRNAME / STORE_FILENAME


def _from_dict(raw: Any) -> SurfaceRecord | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("channel_id"), str):
        return None
    data = {k: v for k, v in raw.items() if k in _FIELDS}
    bar = data.get("context_bar")
    data["context_bar"] = tuple(
        {str(k): str(v) for k, v in item.items()}
        for item in (bar if isinstance(bar, list) else [])
        if isinstance(item, dict)
    )
    try:
        return SurfaceRecord(**data)
    except TypeError:
        return None


class SurfaceStore:
    """Thread-safe load/save of :class:`SurfaceRecord` (cheap; safe on the loop)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._records: dict[str, SurfaceRecord] = {}

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("code channel surface store unreadable, starting empty: %s", exc)
            return
        for raw in data.get("records", []) if isinstance(data, dict) else []:
            record = _from_dict(raw)
            if record is not None:
                self._records[record.channel_id] = record

    def _commit(self, records: dict[str, SurfaceRecord]) -> None:
        """Write *records*, then adopt them; a failed write raises and adopts nothing."""
        ordered = sorted(records.values(), key=lambda r: r.updated_at, reverse=True)
        staged = {r.channel_id: r for r in ordered[:MAX_RECORDS]}
        payload = {"version": 1, "records": [asdict(r) for r in staged.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(payload, indent=1, ensure_ascii=False))
        self._records = staged

    def get(self, channel_id: str) -> SurfaceRecord | None:
        with self._lock:
            self._ensure_loaded()
            return self._records.get(channel_id)

    def update(self, channel_id: str, **changes: Any) -> SurfaceRecord:
        """Apply *changes* to the channel's record (made when missing); returns it."""
        unknown = set(changes) - _FIELDS
        if unknown:
            raise TypeError(f"unknown surface fields: {sorted(unknown)}")
        if "context_bar" in changes:
            changes["context_bar"] = tuple(dict(i) for i in changes["context_bar"])
        with self._lock:
            self._ensure_loaded()
            current = self._records.get(channel_id) or SurfaceRecord(channel_id=channel_id)
            stored = replace(current, **changes, updated_at=time.time())
            self._commit({**self._records, channel_id: stored})
            return stored


_STORE: SurfaceStore | None = None


def store() -> SurfaceStore:
    """The process-wide store."""
    global _STORE
    if _STORE is None:
        _STORE = SurfaceStore()
    return _STORE


def set_store(value: SurfaceStore | None) -> None:
    """Replace the process-wide store (tests)."""
    global _STORE
    _STORE = value


async def get_async(channel_id: str) -> SurfaceRecord | None:
    """:meth:`SurfaceStore.get` off the event loop."""
    return await asyncio.to_thread(store().get, channel_id)


async def update_async(channel_id: str, **changes: Any) -> SurfaceRecord:
    """:meth:`SurfaceStore.update` off the event loop (raises when the write fails)."""
    return await asyncio.to_thread(lambda: store().update(channel_id, **changes))
