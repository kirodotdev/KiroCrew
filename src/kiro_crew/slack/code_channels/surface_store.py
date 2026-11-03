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
    #: When a canvas create of ours may have landed with its answer lost (0 when
    #: none): Slack's next "already exists" then names our canvas to adopt.
    canvas_create_pending_since: float = 0.0
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
#: The type each scalar field must have on disk (``context_bar`` is checked apart).
_SCALAR_TYPES: dict[str, type | tuple[type, ...]] = {
    f.name: ((int, float) if f.type in ("float", float) else str)
    for f in fields(SurfaceRecord)
    if f.name != "context_bar"
}


def _default_path() -> Path:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.slack.code_channels.store import STORE_DIRNAME

    return config_dir() / STORE_DIRNAME / STORE_FILENAME


def _from_dict(raw: Any) -> SurfaceRecord | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("channel_id"), str):
        return None
    data = {k: v for k, v in raw.items() if k in _FIELDS}
    bar = data.get("context_bar", [])
    # A bar this build can't read in full is an unreadable row, never a
    # silently shortened one that the next write would make permanent.
    if not isinstance(bar, list) or not all(
        isinstance(item, dict) and all(isinstance(v, str) for v in item.values()) for item in bar
    ):
        return None
    data["context_bar"] = tuple({str(k): v for k, v in item.items()} for item in bar)
    for name, kinds in _SCALAR_TYPES.items():
        if name in data and (not isinstance(data[name], kinds) or isinstance(data[name], bool)):
            return None  # a wrong-typed value is an unreadable row, never written back
    try:
        return SurfaceRecord(**data)
    except TypeError:
        return None


class SurfaceStoreUnavailable(OSError):
    """The surface file exists but couldn't be read: neither read nor write it."""


class SurfaceStore:
    """Thread-safe load/save of :class:`SurfaceRecord` (cheap; safe on the loop)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._records: dict[str, SurfaceRecord] = {}
        self._extras: dict[str, dict[str, Any]] = {}

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    def _ensure_loaded(self) -> bool:
        """Load the file once; False while it can't be read (retried on each call).

        A missing or empty file is a real, empty store. A file that exists but
        can't be read or parsed is NOT: treating it as empty would let the next
        write replace every record in it, so reads see nothing and writes
        refuse until a load succeeds.
        """
        if self._loaded:
            return True
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except Exception as exc:
            logger.warning("code channel surface store unreadable (will retry): %s", exc)
            return False
        records: dict[str, SurfaceRecord] = {}
        extras: dict[str, dict[str, Any]] = {}
        if text.strip():
            # Anything this build can't read in full is left as is: one write
            # would otherwise replace the whole file with the part it did read.
            try:
                data = json.loads(text)
            except ValueError as exc:
                logger.warning("code channel surface store corrupt (left as is): %s", exc)
                return False
            # ``{}`` is the empty document the sandbox pre-creates: a real empty store.
            rows = data.get("records", []) if isinstance(data, dict) else None
            version = data.get("version", 1) if isinstance(data, dict) else None
            if not isinstance(rows, list) or not isinstance(version, int) or version > 1:
                logger.warning("code channel surface store has an unknown shape (left as is)")
                return False
            for raw in rows:
                record = _from_dict(raw)
                if record is None:
                    logger.warning("code channel surface store has an unreadable row (left as is)")
                    return False
                records[record.channel_id] = record
                # Fields a newer build wrote are kept and written back untouched.
                unknown = {k: v for k, v in raw.items() if k not in _FIELDS}
                if unknown:
                    extras[record.channel_id] = unknown
        self._records = records
        self._extras = extras
        self._loaded = True
        return True

    def _commit(self, records: dict[str, SurfaceRecord], written: str = "") -> None:
        """Write *records*, then adopt them; a failed write raises and adopts nothing.

        Past :data:`MAX_RECORDS` the record just *written* always stays, and
        records pointing at something posted in Slack (the plan canvas, the
        wrap-up) go last: losing one strands that canvas or button for good.
        """
        ordered = sorted(
            records.values(),
            key=lambda r: (
                r.channel_id == written,
                bool(r.plan_canvas_id or r.summary_ts),
                r.updated_at,
            ),
            reverse=True,
        )
        staged = {r.channel_id: r for r in ordered[:MAX_RECORDS]}
        payload = {
            "version": 1,
            "records": [
                {**self._extras.get(r.channel_id, {}), **asdict(r)} for r in staged.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(payload, indent=1, ensure_ascii=False))
        self._records = staged

    def get(self, channel_id: str) -> SurfaceRecord | None:
        with self._lock:
            if not self._ensure_loaded():
                raise SurfaceStoreUnavailable(str(self.path))
            return self._records.get(channel_id)

    def update(self, channel_id: str, **changes: Any) -> SurfaceRecord:
        """Apply *changes* to the channel's record (made when missing); returns it."""
        unknown = set(changes) - _FIELDS
        if unknown:
            raise TypeError(f"unknown surface fields: {sorted(unknown)}")
        if "context_bar" in changes:
            changes["context_bar"] = tuple(dict(i) for i in changes["context_bar"])
        with self._lock:
            if not self._ensure_loaded():
                raise SurfaceStoreUnavailable(str(self.path))
            current = self._records.get(channel_id) or SurfaceRecord(channel_id=channel_id)
            stored = replace(current, **changes, updated_at=time.time())
            self._commit({**self._records, channel_id: stored}, written=channel_id)
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
