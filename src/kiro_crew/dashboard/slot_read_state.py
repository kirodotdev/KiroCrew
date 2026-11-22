"""The person's read time for each session, kept on the gateway in ``chat_read_state.json``.

The file is ``{"epoch": <ts>, "reads": {<slot>: <ts>}}`` in the fenced ``pinned-order``
directory. Rows carry a slot's written read as ``last_read_ts`` and the written ``epoch``
as ``read_epoch``: a floor under which no reply badges, never a read. Reads only move forward.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard import pinned_session_order as pso
from kiro_crew.history import latest_transcript_ts, transcript_sort_key
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

READ_STATE_FILE = "chat_read_state.json"

#: Past it the oldest reads are dropped; the live slot cap is far below it.
MAX_READ_STATE_KEYS = pso.MAX_PINNED_ORDER_KEYS

#: A stored time is a transcript timestamp; this bounds a hand-edited file.
MAX_READ_TS_CHARS = 64

#: Byte ceiling for the file: every key and time at its bound, JSON-escaped.
MAX_READ_STATE_FILE_BYTES = (
    MAX_READ_STATE_KEYS * (pso.MAX_PINNED_ORDER_KEY_CHARS * 12 + MAX_READ_TS_CHARS + 8) + 256
)

SAVE_DELAY_SECONDS = 1.0


def store_path(home: Path) -> Path:
    return home / pso.PINNED_ORDER_DIR / READ_STATE_FILE


def valid_read_ts(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_READ_TS_CHARS
        and latest_transcript_ts(value) is not None
    )


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= pso.MAX_PINNED_ORDER_KEY_CHARS


def load_stored(path: Path) -> tuple[str | None, dict[str, str]]:
    """The stored ``(epoch, reads)``; ``(None, {})`` for a missing or unusable file.

    Any ``OSError`` other than ``FileNotFoundError`` is raised, so an unreadable home is never overwritten.
    """
    try:
        data = pso.read_store_file(path, MAX_READ_STATE_FILE_BYTES)
    except FileNotFoundError:
        return None, {}
    if data is None:
        return None, {}
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("%s has malformed content: %s", path.name, exc)
        return None, {}
    if not isinstance(raw, dict):
        logger.warning("%s is not an object; ignoring it", path.name)
        return None, {}
    epoch = raw.get("epoch") if valid_read_ts(raw.get("epoch")) else None
    reads_raw = raw.get("reads")
    reads: dict[str, str] = {}
    if isinstance(reads_raw, dict):
        reads = {k: v for k, v in reads_raw.items() if _valid_key(k) and valid_read_ts(v)}
    return epoch, _capped(reads)


def save(path: Path, epoch: str, reads: dict[str, str]) -> None:
    """Replace the stored read state in one atomic, owner-only write."""
    mkdirs_owner_only(path.parent)
    payload = json.dumps({"epoch": epoch, "reads": reads})
    atomic_write(path, payload, fsync=True, restrict_to_owner=True)


def _capped(reads: dict[str, str]) -> dict[str, str]:
    if len(reads) <= MAX_READ_STATE_KEYS:
        return reads
    newest = sorted(reads.items(), key=lambda item: transcript_sort_key(item[1]))
    return dict(newest[-MAX_READ_STATE_KEYS:])


def _newer(a: str | None, b: str) -> str:
    """The later of *a* and *b* as instants; *a* wins a tie. *b* is known to parse."""
    if a is None:
        return b
    return b if transcript_sort_key(b) > transcript_sort_key(a) else a


class ReadState:
    """The read times in memory and their coalesced write; rows and echoes see only ``persisted``."""

    def __init__(self) -> None:
        self.reads: dict[str, str] = {}
        self.persisted: dict[str, str] = {}
        self.epoch: str | None = None
        self.written_epoch: str | None = None
        self.loaded = False
        self._dirty = False
        self._save_lock = asyncio.Lock()
        self._save_task: asyncio.Task[None] | None = None
        self._waiting: dict[str, Callable[[str], None]] = {}

    def advance(self, slot: str, read_ts: object) -> str | None:
        """Keep *read_ts* for *slot* when it is newer; return the stored time, or ``None``."""
        if not _valid_key(slot):
            return None
        stored = self.reads.get(slot)
        if not valid_read_ts(read_ts):
            return stored
        assert isinstance(read_ts, str)
        nxt = _newer(stored, read_ts)
        if nxt != stored:
            self.reads[slot] = nxt
            if len(self.reads) > MAX_READ_STATE_KEYS:
                self.reads = _capped(self.reads)
                self._waiting = {k: v for k, v in self._waiting.items() if k in self.reads}
            self._dirty = True
        if self._dirty:
            self._schedule_save()
        return nxt

    def after_persisted(self, slot: str, publish: Callable[[str], None]) -> None:
        """Call *publish* with *slot*'s read once the disk holds it; a newer read replaces the wait."""
        current = self.reads.get(slot)
        if current is None:
            return
        if self.loaded and self.persisted.get(slot) == current:
            publish(current)
            return
        self._waiting[slot] = publish

    def row_read_ts(self, slot: str) -> str | None:
        """A slot row's ``last_read_ts``: its written read, or ``None``. Never the epoch."""
        if not self.loaded or self.written_epoch is None:
            return None
        return self.persisted.get(slot)

    def row_read_epoch(self) -> str | None:
        """A slot row's ``read_epoch``: the written epoch, or ``None`` until it is on disk."""
        if not self.loaded:
            return None
        return self.written_epoch

    def adopt(self, epoch: str | None, stored: dict[str, str]) -> bool:
        """Merge the file's contents under reads that landed first; ``True`` when it must be written."""
        dirty = epoch is None
        self.epoch = epoch or datetime.now(timezone.utc).isoformat()
        self.written_epoch = epoch
        merged = dict(stored)
        for slot, ts in self.reads.items():
            nxt = _newer(merged.get(slot), ts)
            if nxt != merged.get(slot):
                merged[slot] = nxt
                dirty = True
        self.reads = _capped(merged)
        self.persisted = _capped(dict(stored))
        self.loaded = True
        self._dirty = dirty
        self._publish_written()
        return dirty

    def _publish_written(self) -> None:
        for slot, publish in list(self._waiting.items()):
            current = self.reads.get(slot)
            if current is None:
                del self._waiting[slot]
            elif self.persisted.get(slot) == current:
                del self._waiting[slot]
                publish(current)

    def _schedule_save(self) -> None:
        if not self.loaded or (self._save_task is not None and not self._save_task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.create_task(self._save_later())

    async def _save_later(self) -> None:
        await asyncio.sleep(SAVE_DELAY_SECONDS)
        while self._dirty and await self.save_now():
            pass

    async def save_now(self) -> bool:
        """Write the state and publish what it covers; ``False`` (still dirty) when the write failed.

        Writes run one at a time, each snapshotting under the lock, so an older one never lands last.
        """
        from kiro_crew.config.paths import data_home

        async with self._save_lock:
            epoch = self.epoch
            if epoch is None:
                return False
            snapshot = dict(self.reads)
            self._dirty = False
            try:
                await asyncio.to_thread(save, store_path(data_home()), epoch, snapshot)
            except OSError:
                self._dirty = True
                logger.warning("could not write %s", READ_STATE_FILE, exc_info=True)
                return False
            self.written_epoch = epoch
            self.persisted = snapshot
            self._publish_written()
            return True


_STATES: "weakref.WeakKeyDictionary[Any, ReadState]" = weakref.WeakKeyDictionary()


def read_state_of(state: Any) -> ReadState:
    found = _STATES.get(state)
    if found is None:
        found = _STATES[state] = ReadState()
    return found


async def load_read_state_after_listen(state: Any) -> None:
    """Read the stored read times once the gateway serves, then publish them on the rows."""
    from kiro_crew.config.paths import data_home

    reads = read_state_of(state)
    if reads.loaded:
        if reads.written_epoch is None and await reads.save_now():
            state.push_slots_update()
        return
    try:
        epoch, stored = await asyncio.to_thread(load_stored, store_path(data_home()))
    except OSError:
        logger.warning("could not read %s", READ_STATE_FILE, exc_info=True)
        return
    if reads.adopt(epoch, stored) and not await reads.save_now():
        return
    state.push_slots_update()
