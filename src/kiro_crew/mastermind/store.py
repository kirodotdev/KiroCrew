"""The Mastermind tree on disk: two append-only JSON-lines logs and the views.

``messages.jsonl`` holds one record per message (``{"i", "kind", "text",
"date"}``) and ``nodes.jsonl`` one per summary written (``{"l", "i", "text"}``).
Nothing is rewritten: a record is appended once and read back by the byte
offset an index kept at load. ``views.json`` holds the views' lines and is
replaced whole (it is derived: losing it costs a re-merge, never a message).

A crash can leave a torn last line. A load cuts each log back to its last
whole record before anything is appended after it, so a torn write loses that
one record and nothing after it: the messages log is cut at its first record
that is torn, unreadable or out of order (a message's index is its line's
position), the nodes log at a torn last line.

Not thread-safe: one worker owns a store (``service.py``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets
from kiro_crew.mastermind.constants import MESSAGES_FILE, NODES_FILE, VIEWS_FILE

logger = logging.getLogger(__name__)


@dataclass
class _Log:
    """One append-only log: its file and each record's byte offset."""

    path: Path
    offsets: list[int] = field(default_factory=list)


class TreeStore:
    """Reads and appends the tree's logs under one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._messages = _Log(directory / MESSAGES_FILE)
        self._nodes = _Log(directory / NODES_FILE)

    # Loading

    def load_messages(self) -> list[dict[str, Any]]:
        """Index the messages log; return each message's metadata, text omitted."""
        out: list[dict[str, Any]] = []
        records, end = self._scan(self._messages)
        for offset, rec in records:
            if not isinstance(rec, dict) or rec.get("i") != len(out):
                logger.warning("mastermind: messages log cut at message %d (bad record)", len(out))
                end = offset
                break
            self._messages.offsets.append(offset)
            out.append({"kind": rec.get("kind", ""), "date": rec.get("date", "")})
        self._cut(self._messages, end)
        return out

    def load_nodes(self) -> dict[tuple[int, int], int]:
        """Index the nodes log: each node's ``(lv, i)`` to its record's offset.

        An unreadable whole line is skipped: a node is only a cache of its
        source, which is built again when missing."""
        index: dict[tuple[int, int], int] = {}
        records, end = self._scan(self._nodes)
        for offset, rec in records:
            if (
                isinstance(rec, dict)
                and isinstance(rec.get("l"), int)
                and isinstance(rec.get("i"), int)
            ):
                index[(rec["l"], rec["i"])] = offset
        self._cut(self._nodes, end)
        return index

    @staticmethod
    def _cut(log: _Log, end: int) -> None:
        """Truncate *log* at *end*, the byte after its last trusted record."""
        if log.path.exists() and log.path.stat().st_size > end:
            os.truncate(log.path, end)

    def _scan(self, log: _Log) -> tuple[list[tuple[int, Any]], int]:
        """Each whole line's offset and record (None: unreadable or over the
        record cap), and the byte after the last whole line."""
        if not log.path.exists():
            return [], 0
        out: list[tuple[int, Any]] = []
        end = 0
        with open(log.path, "rb") as fh:
            for start, stop, raw in bounded_raw_records_with_offsets(
                fh, log.path, label="mastermind"
            ):
                if raw is not None and not raw.endswith(b"\n"):
                    break  # a torn last line
                end = stop
                try:
                    out.append((start, json.loads(raw) if raw is not None else None))
                except ValueError:
                    out.append((start, None))
        return out, end

    # Reading

    def message(self, i: int) -> dict[str, Any] | None:
        if not 0 <= i < len(self._messages.offsets):
            return None
        return self._read(self._messages.path, self._messages.offsets[i])

    def node_text(self, offset: int) -> str | None:
        rec = self._read(self._nodes.path, offset)
        text = rec.get("text") if isinstance(rec, dict) else None
        return text if isinstance(text, str) else None

    @staticmethod
    def _read(path: Path, offset: int) -> dict[str, Any] | None:
        with open(path, "rb") as fh:
            fh.seek(offset)
            raw = fh.readline()
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        return rec if isinstance(rec, dict) else None

    # Appending

    def append_message(self, rec: dict[str, Any]) -> None:
        self._messages.offsets.append(self._append(self._messages.path, rec))

    def append_node(self, lv: int, i: int, text: str) -> int:
        """Append node ``(lv, i)``; return its offset."""
        return self._append(self._nodes.path, {"l": lv, "i": i, "text": text})

    @staticmethod
    def _append(path: Path, rec: dict[str, Any]) -> int:
        # A lone surrogate (a broken emoji pasted in) cannot be encoded; it is
        # replaced rather than losing the whole record.
        text = json.dumps(rec, ensure_ascii=False)
        line = (text + "\n").encode("utf-8", errors="replace")
        with open(path, "ab") as fh:
            offset = fh.tell()
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return offset

    def clear(self) -> None:
        """Forget every message and summary."""
        for log in (self._messages, self._nodes):
            log.offsets.clear()
            if log.path.exists():
                os.truncate(log.path, 0)
        (self.directory / VIEWS_FILE).unlink(missing_ok=True)

    def export_lines(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every message and node record, for an export."""
        return (
            [r for _, r in self._scan(self._messages)[0] if isinstance(r, dict)],
            [r for _, r in self._scan(self._nodes)[0] if isinstance(r, dict)],
        )

    # Views

    def load_views(self) -> dict[str, Any]:
        path = self.directory / VIEWS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_views(self, data: dict[str, Any]) -> None:
        atomic_write(self.directory / VIEWS_FILE, json.dumps(data))
