"""Watermark-based incremental reader for the structured event log.

The watermark is ``(shard filename, byte offset)`` — file position, not
``seq``, is the authoritative order: it is stable across writer processes and
survives a writer whose in-memory counter restarted. ``read_since`` never
re-yields consumed events and tolerates shards disappearing to ``prune``
(a vanished watermark shard resumes from the next shard by name).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kiro_crew.events.base import Event, Parsed, parse
from kiro_crew.events.log import _SHARD_SUFFIX, default_events_dir, is_shard_name

#: (shard filename, byte offset) — resume from here.
Watermark = tuple[str, int]


@dataclass(frozen=True)
class ReadItem:
    """One consumed event plus where it came from."""

    event: Event
    kind: str
    src: str
    seq: int
    shard: str


class EventReader:
    """Read events across daily shards in (shard, offset) order."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory if directory is not None else default_events_dir()

    def _shards(self) -> list[Path]:
        if not self._dir.exists():
            return []
        return sorted(
            p for p in self._dir.glob("*" + _SHARD_SUFFIX) if is_shard_name(p.name)
        )

    def read_since(
        self,
        watermark: Watermark | None = None,
        *,
        kind_prefix: str | None = None,
        key: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[ReadItem], Watermark | None]:
        """Consume events after *watermark*, oldest first.

        Returns ``(items, new_watermark)``. Filters (``kind_prefix`` matches
        ``kind.startswith``; ``key`` matches exactly) affect which events are
        *returned*, not which are *consumed*: the returned watermark always
        covers every line read, so filtered-out events are never re-visited.
        ``limit`` caps returned items and stops consumption at the line after
        the last returned one, keeping resumption exact. A ``None`` watermark
        starts from the beginning; ``None`` comes back unchanged when the log
        is empty.
        """
        wm_shard, wm_offset = watermark if watermark is not None else ("", 0)
        items: list[ReadItem] = []
        new_wm: Watermark | None = watermark

        for path in self._shards():
            if path.name < wm_shard:
                continue
            start = wm_offset if path.name == wm_shard else 0
            try:
                size = path.stat().st_size
                if start >= size:
                    continue
                with open(path, "rb") as fh:
                    fh.seek(start)
                    while True:
                        raw = fh.readline()
                        if not raw:
                            break
                        offset_after = fh.tell()
                        if not raw.endswith(b"\n"):
                            # Torn tail mid-write: leave it for the next read.
                            break
                        parsed = _parse_bytes(raw)
                        new_wm = (path.name, offset_after)
                        if parsed is None:
                            continue
                        if kind_prefix is not None and not parsed.kind.startswith(kind_prefix):
                            continue
                        if key is not None and parsed.event.key != key:
                            continue
                        items.append(
                            ReadItem(
                                event=parsed.event,
                                kind=parsed.kind,
                                src=parsed.src,
                                seq=parsed.seq,
                                shard=path.name,
                            )
                        )
                        if limit is not None and len(items) >= limit:
                            return items, new_wm
            except OSError:
                # A shard that was listed but cannot be statted/read is either
                # (a) deleted by a concurrent prune -- the next read's shard
                # listing will no longer contain it -- or (b) transiently
                # unreadable. Both cases get the same treatment: STOP here and
                # return the watermark as it stands, so nothing after the
                # failed shard is consumed and the shard itself is retried on
                # the next read. Skipping ahead would advance the watermark
                # past events that still exist.
                return items, new_wm
        return items, new_wm


def _parse_bytes(raw: bytes) -> Parsed | None:
    try:
        return parse(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
