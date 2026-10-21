"""The Mastermind memory tree: messages, their summaries, and the views.

Node ``(0, i)`` is message ``i`` in at most ``NODE_BYTES``; node ``(lv, i)``
merges ``(lv-1, 2i)`` and ``(lv-1, 2i+1)``. A source that fits is its own node,
stored at once; a longer one is a *job*, summarized by the caller
(``summarizer.py``) and handed back with :meth:`MemoryTree.finish`.

A view lists nodes covering the chat to its end, oldest first; each message
appends its leaf. Past ``high`` bytes the view merges the adjacent siblings
whose parent is built and most due (oldest since its last message, in halves:
detail fades with age) down to half, so a view changes only at its end between
merges. Two views: the chat's, which every turn reads, and the summaries'
(``seen``), which a summary reads as context up to the first message not built.

Not thread-safe: one worker thread owns a tree (``service.py``).
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.mastermind.constants import (
    BYTES_PER_TOKEN,
    DEFAULT_WINDOW_TOKENS,
    NODE_BYTES,
    RECENT_BYTES,
    RECENT_MESSAGES,
    SEEN_HIGH_BYTES,
    VIEW_HIGH_BYTES,
    VIEW_MAX_BYTES,
    VIEW_WINDOW_SHARE,
    ZOOM_PAGE_CHARS,
)
from kiro_crew.mastermind.prompts import LEAF_TASK, MERGE_TASK, NOT_SUMMARIZED
from kiro_crew.mastermind.store import TreeStore

logger = logging.getLogger(__name__)

Part = tuple[int, int]


@dataclass
class View:
    """A view: its lines, the bytes of those built, a merge under way."""

    high: int
    parts: list[Part] = field(default_factory=list)
    size: int = 0
    batch: bool = False


@dataclass(frozen=True)
class Job:
    """A node to summarize: where it is, and the prompt that asks for it."""

    lv: int
    i: int
    source: str
    prompt: str


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def clip(text: str, limit: int = NODE_BYTES) -> str:
    """The longest prefix of *text* within *limit* UTF-8 bytes."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def flat(text: str) -> str:
    """One line: newlines and the whitespace around them become one space."""
    if "\n" not in text:
        return text
    return " ".join(chunk.strip() for chunk in text.split("\n") if chunk.strip())


def span(part: Part) -> tuple[int, int]:
    """The first and last message a node covers."""
    lv, i = part
    first = i << lv
    return first, first + (1 << lv) - 1


def ref(part: Part) -> str:
    """A node's zoom target, ``id+n``."""
    lv, i = part
    return f"{i << lv}+{1 << lv}"


def context_end(part: Part) -> int:
    """The message a node's summary reads its context up to (excluded)."""
    lv, i = part
    return i if lv == 0 else (i + 1) << lv


def halves(part: Part) -> list[Part]:
    lv, i = part
    return [] if lv == 0 else [(lv - 1, 2 * i), (lv - 1, 2 * i + 1)]


def view_high_for(window_tokens: int | None) -> int:
    """A turn view's high, in bytes, for a model with *window_tokens* of
    context (None: unknown): its share of the window, within the floor and the
    ceiling."""
    window = window_tokens if window_tokens and window_tokens > 0 else DEFAULT_WINDOW_TOKENS
    share = int(window * VIEW_WINDOW_SHARE * BYTES_PER_TOKEN)
    return max(VIEW_HIGH_BYTES, min(share, VIEW_MAX_BYTES))


def roots(count: int) -> list[Part]:
    """The tree's roots: one node per power of two in the count, largest first."""
    out: list[Part] = []
    first = 0
    for lv in range(max(count.bit_length() - 1, 0), -1, -1):
        if first + (1 << lv) <= count:
            out.append((lv, first >> lv))
            first += 1 << lv
    return out


class MemoryTree:
    """One chat's tree, kept under *directory*."""

    def __init__(
        self,
        directory: Path,
        *,
        high: int = VIEW_HIGH_BYTES,
        seen_high: int = SEEN_HIGH_BYTES,
    ) -> None:
        self.store = TreeStore(directory)
        self._messages = self.store.load_messages()
        # A node past the messages kept (their log was cut) summarizes messages
        # the chat does not hold.
        self._nodes = {
            p: off for p, off in self.store.load_nodes().items() if span(p)[1] < self.count
        }
        self._texts: dict[Part, str] = {}
        self._busy: set[Part] = set()
        self.view = View(high)
        self.seen = View(seen_high)
        self._ready: set[Part] = set()
        self._rebuild_ready()
        self._load_views()
        self._settle(at_message=True)

    # Messages

    @property
    def count(self) -> int:
        return len(self._messages)

    def append(self, kind: str, text: str, date: str | None = None) -> int:
        """Log a message; return its index. Its leaf is built or queued."""
        i = self.count
        when = date or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.append_message({"i": i, "kind": kind, "text": text, "date": when})
        self._messages.append({"kind": kind, "date": when})
        self._ready.add((0, i))
        self._settle(at_message=True)
        return i

    def clear(self) -> None:
        """Forget every message and summary."""
        self.store.clear()
        self._messages, self._nodes, self._texts = [], {}, {}
        self._busy.clear()
        self._ready.clear()
        self.view = View(self.view.high)
        self.seen = View(self.seen.high)

    def message(self, i: int) -> dict[str, Any] | None:
        return self.store.message(i) if 0 <= i < self.count else None

    @staticmethod
    def show(rec: dict[str, Any]) -> str:
        """A message as the tree and the model read it, tagged by its author."""
        return f"{rec.get('kind', '')}: {rec.get('text', '')}"

    # Nodes

    def built(self, part: Part) -> bool:
        return part in self._nodes

    def node(self, part: Part) -> str | None:
        """A node's text, or None if it is not built."""
        text = self._texts.get(part)
        if text is not None:
            return text
        offset = self._nodes.get(part)
        if offset is None:
            return None
        text = self.store.node_text(offset)
        if text is not None and self._held(part):
            self._texts[part] = text
        return text

    def source(self, part: Part) -> str | None:
        """What a node summarizes: its message, or its two halves' lines."""
        if part[0] == 0:
            rec = self.message(part[1])
            return None if rec is None else self.show(rec)
        texts = [self.node(half) for half in halves(part)]
        if any(t is None for t in texts):
            return None
        return "\n".join(flat(t) for t in texts if t is not None)

    def jobs(self, limit: int) -> list[Job]:
        """Up to *limit* summaries to write now, oldest first; marked busy."""
        out: list[Job] = []
        end = self._first_unbuilt()
        for part in sorted(self._ready - self._busy, key=lambda p: (context_end(p), -p[0])):
            if len(self._busy) >= limit:
                break
            text = self.source(part)
            if text is None:
                continue
            self._busy.add(part)
            out.append(Job(part[0], part[1], text, self._task(part, text, end)))
        return out

    def finish(self, lv: int, i: int, line: str) -> None:
        """Store a summary written for a job (clipped to fit, if it is not)."""
        part = (lv, i)
        self._busy.discard(part)
        if self.built(part) or span(part)[1] >= self.count:
            return
        self._store(part, clip(flat(line.strip())) or clip(flat(self.source(part) or "")))
        self._settle()

    def release(self, lv: int, i: int) -> None:
        """A job given up for now: it is offered again by a later :meth:`jobs`."""
        self._busy.discard((lv, i))

    def pending(self) -> int:
        """How many nodes wait to be built (ready ones; others follow them)."""
        return len(self._ready)

    def _task(self, part: Part, text: str, first_unbuilt: int) -> str:
        first, last = span(part)
        n = (1 << part[0]) // 2
        if part[0] == 0:
            task = LEAF_TASK.format(id=first)
        else:
            task = MERGE_TASK.format(a=f"{first}+{n}", b=f"{first + n}+{n}", id=first, end=last)
        upto = min(context_end(part), first_unbuilt)
        return self.render(upto, self.seen) + "\n" + task + text + "\n</input>"

    def _store(self, part: Part, text: str) -> None:
        self._nodes[part] = self.store.append_node(part[0], part[1], text)
        self._ready.discard(part)
        if self._held(part):
            self._texts[part] = text
        # A leaf enters a view unbuilt, counted as 0 bytes; a higher node enters
        # only by a merge, which counts it then.
        for view in (self.view, self.seen):
            if part[0] == 0 and part[1] < self._end(view):
                view.size += utf8_len(text)
        lv, i = part
        up = (lv + 1, i >> 1)
        if self.built((lv, i ^ 1)) and not self.built(up):
            self._ready.add(up)

    def _store_free(self) -> bool:
        """Build every ready node whose source fits; whether any was built."""
        built = False
        while True:
            free = [
                (p, t)
                for p in sorted(self._ready - self._busy, key=lambda p: (context_end(p), -p[0]))
                if (t := self.source(p)) is not None and utf8_len(t) <= NODE_BYTES
            ]
            if not free:
                return built
            for part, text in free:
                self._store(part, text)
            built = True

    def _rebuild_ready(self) -> None:
        """Ready: every leaf not built, and every unbuilt node whose halves are."""
        self._ready = {(0, i) for i in range(self.count) if (0, i) not in self._nodes}
        for lv, i in self._nodes:
            up = (lv + 1, i >> 1)
            if i % 2 == 0 and self.built((lv, i + 1)) and not self.built(up):
                self._ready.add(up)

    def _first_unbuilt(self) -> int:
        leaves = [i for lv, i in self._ready if lv == 0]
        return min(leaves) if leaves else self.count

    # Views

    def _settle(self, at_message: bool = False) -> None:
        self._store_free()
        self._fit(at_message)

    def _end(self, view: View) -> int:
        return span(view.parts[-1])[1] + 1 if view.parts else 0

    def _size(self, part: Part) -> int:
        text = self.node(part)
        return 0 if text is None else utf8_len(text)

    def _reach(self, view: View, end: int) -> None:
        while view.parts and span(view.parts[-1])[1] >= end:
            view.size -= self._size(view.parts.pop())
        for i in range(self._end(view), end):
            view.parts.append((0, i))
            view.size += self._size((0, i))

    def _fit(self, at_message: bool) -> None:
        """Each view to its end, merged as it passes its high.

        A merge starts when a view passes ``high`` and lands only whole (to half,
        or by ``high`` while parents are missing); one held up by missing parents
        goes on at the next message (``at_message``), never at a summary
        landing, so between messages a view changes only at its end.
        """
        before = (list(self.view.parts), self.view.batch, list(self.seen.parts), self.seen.batch)
        self._reach(self.view, self.count)
        self._reach(self.seen, self._first_unbuilt())
        for view in (self.view, self.seen):
            if view.size > view.high or (at_message and view.batch):
                parts, size = self._merge(view)
                if at_message or size <= max(view.high // 2, view.size - view.high):
                    view.parts, view.size = parts, size
                view.batch = view.size > view.high // 2
        self._texts = {p: t for p, t in self._texts.items() if self._held(p)}
        if before != (self.view.parts, self.view.batch, self.seen.parts, self.seen.batch):
            self._save_views()

    def _merge(self, view: View) -> tuple[list[Part], int]:
        """The view with its most due pairs merged down to half, as parents allow.

        A pair's due is ``(T + 1) / 2^lv - i`` (T the view's end): its age from its
        last message, in halves, so detail fades with age and a merge never
        rewrites the lines near the end. A heap over the view as a linked list
        keeps a view of thousands of lines fast.
        """
        end, low = self._end(view), view.high // 2
        parts: list[Part | None] = list(view.parts)
        nxt = list(range(1, len(parts) + 1))
        prv = list(range(-1, len(parts) - 1))
        heap: list[tuple[float, int, Part, Part]] = []

        def offer(k: int) -> None:
            if k < 0 or k >= len(parts) or nxt[k] >= len(parts):
                return
            a, b = parts[k], parts[nxt[k]]
            if a is None or b is None or a[1] % 2 or b != (a[0], a[1] + 1):
                return
            if not self.built((a[0] + 1, a[1] // 2)):
                return
            due = (end + 1) / (1 << a[0]) - a[1]
            heapq.heappush(heap, (-due, k, a, b))

        size = view.size
        for k in range(len(parts)):
            offer(k)
        while heap and size > low:
            _, k, a, b = heapq.heappop(heap)
            j = nxt[k]
            if j >= len(parts) or parts[k] != a or parts[j] != b:
                continue
            up = (a[0] + 1, a[1] // 2)
            size += self._size(up) - self._size(a) - self._size(b)
            parts[k], parts[j] = up, None
            nxt[k] = nxt[j]
            if nxt[j] < len(parts):
                prv[nxt[j]] = k
            offer(prv[k])
            offer(k)
        return [p for p in parts if p is not None], size

    def _held(self, part: Part) -> bool:
        """Whether a view holds *part* or a node under it, or will."""
        first = span(part)[0]
        for view in (self.view, self.seen):
            k = self._cover(view, first)
            if k == len(view.parts) or view.parts[k][0] <= part[0]:
                return True
        return False

    @staticmethod
    def _cover(view: View, msg: int) -> int:
        lo, hi = 0, len(view.parts)
        while lo < hi:
            mid = (lo + hi) // 2
            if span(view.parts[mid])[1] < msg:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _save_views(self) -> None:
        self.store.save_views(
            {
                "count": self.count,
                "view": {"parts": self.view.parts, "batch": self.view.batch},
                "seen": {"parts": self.seen.parts, "batch": self.seen.batch},
            }
        )

    def _load_views(self) -> None:
        saved = self.store.load_views()
        for name, view in (("view", self.view), ("seen", self.seen)):
            kept = saved.get(name) if isinstance(saved.get(name), dict) else None
            parts = kept.get("parts") if kept else None
            if not isinstance(parts, list) or not self._valid(parts):
                continue
            view.parts = [(int(lv), int(i)) for lv, i in parts]
            view.batch = bool(kept.get("batch")) if kept else False
            view.size = sum(self._size(p) for p in view.parts)

    def _valid(self, parts: list[Any]) -> bool:
        """Whether saved lines tile the chat from 0 with nodes that exist."""
        nxt = 0
        for p in parts:
            if not (isinstance(p, list) and len(p) == 2 and all(isinstance(x, int) for x in p)):
                return False
            part = (p[0], p[1])
            first, last = span(part)
            if first != nxt or last >= self.count or (part[0] > 0 and not self.built(part)):
                return False
            nxt = last + 1
        return True

    # Reading

    def render(
        self, end: int | None = None, view: View | None = None, budget: int | None = None
    ) -> str:
        """The view's lines before message *end*, inside ``<chat>`` tags.

        With a *budget* (bytes), only the newest lines that fit are kept and the
        older ones are named in one opening line. A view passes its high only
        while the parents its merge needs are not summarized yet (a burst of
        messages, an import), and a turn must not carry all of it meanwhile.
        """
        view = view or self.view
        stop = self.count if end is None else end
        lines = []
        for part in view.parts:
            if span(part)[1] >= stop:
                break
            text = self.node(part)
            lines.append(f"{ref(part)}|{flat(text) if text is not None else NOT_SUMMARIZED}")
        if budget is not None:
            kept, size = 0, 0
            for line in reversed(lines):
                size += utf8_len(line) + 1
                if size > budget:
                    break
                kept += 1
            if kept < len(lines):
                last = span(view.parts[len(lines) - kept - 1])[1]
                heads = ", ".join(ref(r) for r in roots(last + 1))
                note = (
                    f"(messages 0 to {last} are left out while their summaries are written; "
                    f"zoom these lines to read them: {heads})"
                )
                lines = [note] + lines[len(lines) - kept :]
        return "<chat>\n" + "".join(line + "\n" for line in lines) + "</chat>"

    def set_high(self, high: int) -> None:
        """Size the turn view to *high* bytes (the model's window changed). A
        smaller view merges as soon as a whole merge can land; a larger one keeps
        its lines and grows with the messages that come."""
        if high != self.view.high:
            self.view.high = high
            self._fit(at_message=False)

    def last_messages(self) -> str:
        """The last messages whole (newest first within the budget), oldest
        first, inside ``<recent>`` tags; empty when the chat is."""
        picked: list[str] = []
        budget = RECENT_BYTES
        for i in range(self.count - 1, max(self.count - RECENT_MESSAGES, 0) - 1, -1):
            rec = self.message(i) or {}
            line = f"{i}+0|{self.show(rec)}"
            if utf8_len(line) > budget:
                line = clip(line, budget)
            budget -= utf8_len(line)
            picked.append(line)
            if budget <= 0:
                break
        if not picked:
            return ""
        return "<recent>\n" + "\n".join(reversed(picked)) + "\n</recent>"

    def lines(self) -> list[dict[str, Any]]:
        """The turn view's lines, for the dashboard."""
        out: list[dict[str, Any]] = []
        for part in self.view.parts:
            text = self.node(part)
            first, _ = span(part)
            out.append(
                {
                    "id": first,
                    "n": 1 << part[0],
                    "ref": ref(part),
                    "text": flat(text) if text is not None else NOT_SUMMARIZED,
                    "built": text is not None,
                }
            )
        return out

    def export(self) -> dict[str, Any]:
        messages, nodes = self.store.export_lines()
        return {"format": "kirocrew-mastermind/1", "messages": messages, "nodes": nodes}

    def zoom(self, msg_id: int, n: int, at: int = 0) -> str:
        """Message *msg_id* whole (``n == 1``, paged from *at*), else the two
        lines under line ``msg_id+n``."""
        ok = n >= 1 and n & (n - 1) == 0 and msg_id >= 0 and msg_id % n == 0
        if not ok or msg_id + n > self.count:
            heads = ", ".join(ref(r) for r in roots(self.count)) or "none yet"
            return (
                f"No line {msg_id}+{n}: n is a power of 2 and id a multiple of n, within "
                f"the chat's {self.count} messages. The tree's roots: {heads}."
            )
        if n == 1:
            rec = self.message(msg_id) or {}
            text = str(rec.get("text", ""))
            page = text[at : at + ZOOM_PAGE_CHARS]
            more = at + ZOOM_PAGE_CHARS < len(text)
            tail = (
                f"\n[{len(text) - at - len(page)} more characters: zoom with at={at + len(page)}]"
            )
            return f"{msg_id}+0|{rec.get('kind', '')}: {page}" + (tail if more else "")
        part = (n.bit_length() - 1, msg_id // n)
        return "\n".join(f"{ref(h)}|{flat(self.node(h) or NOT_SUMMARIZED)}" for h in halves(part))

    def date(self, msg_id: int) -> str:
        if not 0 <= msg_id < self.count:
            return f"No message {msg_id}."
        return str(self._messages[msg_id].get("date") or "unknown")
