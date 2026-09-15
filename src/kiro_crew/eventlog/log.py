"""One member's append-only log file: header line, then one Event per line.

File layout (``<members_root>/<slug>/log.jsonl``)::

    {"type":"member","version":1,"id":<slug>,"name":<name>,"createdAt":<epoch ms>}
    {"type":<event type>,"seq":0,"time":<epoch ms>,"data":{...}}
    {"type":<event type>,"seq":1,"time":<epoch ms>,"data":{...}}
    ...

``seq`` is the zero-based index of the event after the header. A gap or an
unparseable line INSIDE the committed region is corruption. A torn trailing
line (no newline, or an unparseable last line with no newline) is a partial
write and is repaired by truncating to the last committed byte.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from kiro_crew.eventlog.types import (
    HEADER_TYPE,
    HEADER_VERSION,
    Event,
    is_known_event_type,
)


class LogCorrupt(Exception):
    """A committed region of a member log is unreadable.

    Raised for a gap in ``seq`` or an unparseable line that is followed by a
    newline (i.e. fully committed, not a torn trailing write). Carries the
    1-based line number of the offending line.
    """

    def __init__(self, path: Path, line_no: int, detail: str) -> None:
        self.path = path
        self.line_no = line_no
        super().__init__(f"{path}: line {line_no}: {detail}")


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemberLog:
    """Append-only log for one member, guarded by a per-instance lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.header: dict | None = None
        self.events: list[Event] = []
        self.committed_bytes: int = 0
        self._loaded = False

    # ---- creation ---------------------------------------------------------
    def create(self, name: str) -> None:
        """Materialise the log with its header atomically, or do nothing.

        Uses a temp file + ``os.link``/rename so a reader never sees a
        header-less file, and fsyncs the containing directory so the rename is
        durable. A no-op when the log already exists.
        """
        with self._lock:
            if self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            header = {
                "type": HEADER_TYPE,
                "version": HEADER_VERSION,
                "id": self.path.parent.name,
                "name": name,
                "createdAt": _now_ms(),
            }
            line = json.dumps(header, ensure_ascii=False) + "\n"
            tmp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}.{_now_ms()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            # Atomic materialisation: link into place if possible, else rename.
            try:
                os.link(tmp, self.path)
            except (FileExistsError, OSError):
                # Either another writer won the race (FileExistsError) or the
                # filesystem does not support hard links; rename is atomic too.
                if self.path.exists():
                    os.unlink(tmp)
                else:
                    os.replace(tmp, self.path)
            finally:
                if os.path.lexists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            self._fsync_dir(self.path.parent)
            # Force a reload on next access.
            self._loaded = False

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dfd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    # ---- load -------------------------------------------------------------
    def load(self) -> None:
        """Parse header + events, repairing a torn trailing line in place."""
        with self._lock:
            self._load_locked()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_locked()

    def _load_locked(self) -> None:
        header: dict | None = None
        events: list[Event] = []
        committed = 0

        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self.header = None
            self.events = []
            self.committed_bytes = 0
            self._loaded = True
            return

        # Split keeping track of byte offsets so we can find the committed
        # length (the end of the last line terminated by "\n").
        pos = 0
        line_no = 0
        n = len(raw)
        torn = False
        while pos < n:
            nl = raw.find(b"\n", pos)
            if nl == -1:
                # Trailing bytes with no newline: a torn write. Everything
                # before `pos` is committed; repair by truncating here.
                torn = True
                break
            line_bytes = raw[pos:nl]
            line_end = nl + 1  # inclusive of the newline
            line_no += 1

            text = line_bytes.strip()
            if not text:
                # Blank line inside the committed region. Header must be first
                # non-blank; blank lines between events are tolerated (the
                # legacy activity writer frames with leading newlines).
                pos = line_end
                committed = line_end
                continue

            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                if line_end == n:
                    # Unparseable AND last line — but it WAS newline-terminated,
                    # so it is committed, not torn: corruption.
                    raise LogCorrupt(self.path, line_no, "unparseable committed line")
                raise LogCorrupt(self.path, line_no, "unparseable committed line")

            if header is None:
                if not isinstance(obj, dict) or obj.get("type") != HEADER_TYPE:
                    raise LogCorrupt(self.path, line_no, "first line is not a member header")
                header = obj
                pos = line_end
                committed = line_end
                continue

            if not isinstance(obj, dict):
                raise LogCorrupt(self.path, line_no, "event line is not an object")
            expected_seq = len(events)
            if obj.get("seq") != expected_seq:
                raise LogCorrupt(
                    self.path,
                    line_no,
                    f"seq {obj.get('seq')!r} != expected {expected_seq}",
                )
            events.append(
                {
                    "type": obj.get("type"),
                    "seq": expected_seq,
                    "time": obj.get("time"),
                    "data": obj.get("data") if isinstance(obj.get("data"), dict) else {},
                }
            )
            pos = line_end
            committed = line_end

        if header is None and committed == 0 and torn:
            # A torn header (partial first line, never committed). Repair to
            # an empty file; there is no valid log yet.
            self._truncate(committed)
            self.header = None
            self.events = []
            self.committed_bytes = 0
            self._loaded = True
            return

        if header is None:
            # Empty file (0 bytes) already handled; a non-empty file with no
            # header line means the whole thing was blank lines.
            self.header = None
            self.events = []
            self.committed_bytes = committed
            self._loaded = True
            return

        if torn:
            self._truncate(committed)

        self.header = header
        self.events = events
        self.committed_bytes = committed
        self._loaded = True

    def _truncate(self, size: int) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY)
        except OSError:
            return
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

    # ---- append -----------------------------------------------------------
    def append(self, type: str, data: dict) -> Event:
        """Append one event, fsync, and return it. Rolls back on any error."""
        if not is_known_event_type(type):
            raise ValueError(f"unknown event type {type!r}")
        # Validate serialisability before touching the file.
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event data is not JSON-serializable: {exc}") from exc
        json.loads(payload)  # cheap round-trip check

        with self._lock:
            self._ensure_loaded()
            if self.header is None:
                raise LogCorrupt(self.path, 0, "cannot append to a log with no header")
            seq = len(self.events)
            event: Event = {
                "type": type,
                "seq": seq,
                "time": _now_ms(),
                "data": data,
            }
            line = json.dumps(event, ensure_ascii=False) + "\n"

            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                size_before = os.fstat(fd).st_size
            except OSError:
                os.close(fd)
                raise
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            except Exception:
                # Roll the file back to what was durably committed.
                try:
                    os.ftruncate(fd, size_before)
                    os.fsync(fd)
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)

            self.events.append(event)
            self.committed_bytes += len(line.encode("utf-8"))
            return event

    # ---- read -------------------------------------------------------------
    def history(self, before: int | None, limit: int) -> list[Event]:
        """Newest-first page of events with ``seq < before`` (or all)."""
        with self._lock:
            self._ensure_loaded()
            evs = self.events
            if before is not None:
                evs = [e for e in evs if e["seq"] < before]
            newest_first = list(reversed(evs))
            if limit is not None and limit >= 0:
                return newest_first[:limit]
            return newest_first

    def events_after(self, after: int, limit: int) -> list[Event]:
        """Oldest-first page of events with ``seq > after``, at most *limit*.

        The catch-up read of the contribution protocol's §3: a consumer that
        folded up to ``after`` asks for what came next, in the order it must fold
        it. Distinct from :meth:`history`, which pages BACKWARDS for a timeline
        view -- folding a newest-first page would apply a later event before an
        earlier one.
        """
        with self._lock:
            self._ensure_loaded()
            out = [e for e in self.events if e["seq"] > after]
            if limit is not None and limit >= 0:
                return out[:limit]
            return out

    def last_seq(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self.events) - 1

    def all_events(self) -> list[Event]:
        """A copy of every event, oldest-first (for priming projections)."""
        with self._lock:
            self._ensure_loaded()
            return list(self.events)

    def exists(self) -> bool:
        return self.path.exists()
