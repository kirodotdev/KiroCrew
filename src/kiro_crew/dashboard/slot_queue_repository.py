"""Queue storage and delivery-ledger operations for dashboard chat slots."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

# This is well above the slot queue's legitimate in-flight set.  Eviction only
# bounds orphaned bookkeeping; an evicted agent remains recoverable on restart.
MAX_PENDING_SUBAGENT_DELIVERIES = 128


def _delivery_key(content: str) -> str:
    """Return a compact identity that survives queue-entry ID replacement."""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:32]


#: Meta keys a queued send's attachment lists ride under, each with the marker
#: word its ``[<marker> N] path`` tokens use. ``files`` is the image-free list
#: ``[attached_file N]`` markers index into, ``dirs`` the folder list
#: ``[attached_dir N]`` markers index into. Defined here, on the queue entry's
#: own module, because every dashboard module that reads them sits downstream.
ATTACHMENT_META_KEYS: tuple[str, ...] = ("files", "dirs")
_ATTACHMENT_MARKERS: dict[str, str] = {"files": "attached_file", "dirs": "attached_dir"}


def _marker_spans(content: str, marker: str, index: int, path: str) -> list[tuple[int, int]]:
    """Every span of the exact ``[<marker> <index>] <path>`` token in *content*.

    The path must end at a whitespace or the end of the text: a bare substring
    test would keep ``/tmp/report.pdf`` alive through ``/tmp/report.pdf.bak``,
    and a bare replace would rewrite the ``[attached_file 2] /tmp/b`` prefix of
    ``[attached_file 2] /tmp/bak`` -- one is a removed attachment drawing a card
    again, the other is a caption silently altered.
    """
    token = f"[{marker} {index}] {path}"
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        at = content.find(token, start)
        if at < 0:
            return spans
        end = at + len(token)
        if end == len(content) or content[end].isspace():
            spans.append((at, end))
        start = at + 1


def _renumber_marker(content: str, marker: str, old: int, new: int, path: str) -> str:
    """Rewrite each exact ``[<marker> <old>] <path>`` token to index *new*."""
    replacement = f"[{marker} {new}] {path}"
    for at, end in reversed(_marker_spans(content, marker, old, path)):
        content = content[:at] + replacement + content[end:]
    return content


def prune_attachment_meta(meta: Any, content: str, previous: str) -> str:
    """Reconcile an entry's attachment lists with an edited *content*.

    An edit to a queued message can remove a ``[attached_file N] path`` marker;
    the agent then receives text without that path and never gets the file, so
    a list still naming it would make the drained row show a card for an
    attachment that was never delivered. Each list is filtered in place to the
    entries the edit did not remove (order kept; a list left empty is removed),
    and the surviving markers in the text are renumbered to the filtered list's
    positions. The renumbering is what keeps a spaced path lossless: the
    renderer reads ``files[N-1]`` for marker ``N`` and, when the two disagree,
    falls back to a whitespace-bounded capture of the marker text -- which
    would hand back ``/tmp/My`` for ``/tmp/My Report.pdf``.

    "Removed by the edit" means the exact numbered marker was in *previous*
    (the entry's text before this edit) and is not in *content*. An entry the
    previous text never named is out of the edit's reach and is kept as-is: a
    send can carry a list entry with no marker (a caller that stamps
    ``meta.files`` on markerless text; a path the list redacted while the text
    kept it verbatim, so the two spellings differ), and the edit did not take
    that attachment away from the agent -- dropping it would make the drained
    row lose an attachment the user never touched. Returns the content to
    store; it equals *content* whenever nothing was pruned.
    """
    if not isinstance(meta, dict):
        return content
    for key in ATTACHMENT_META_KEYS:
        raw = meta.get(key)
        if not isinstance(raw, list):
            continue
        marker = _ATTACHMENT_MARKERS[key]
        indexed = [(i + 1, p) for i, p in enumerate(raw) if isinstance(p, str) and p]
        kept = [
            (old, p)
            for old, p in indexed
            if _marker_spans(content, marker, old, p) or not _marker_spans(previous, marker, old, p)
        ]
        if len(kept) == len(indexed):
            continue
        for new, (old, p) in enumerate(kept, start=1):
            if new != old:
                content = _renumber_marker(content, marker, old, new, p)
        if kept:
            meta[key] = [p for _, p in kept]
        else:
            meta.pop(key, None)
    return content


class SlotQueueRepository:
    """Mutate the current facade-owned queue and delivery ledger.

    Every operation receives its owner explicitly.  Replay and cleanup paths
    replace ``_queue`` and ``_subagent_delivery_pending`` wholesale, so keeping
    either container on this repository would split the slot into two states.
    """

    def __init__(
        self,
        *,
        id_provider: Callable[[], str] | None = None,
        timestamp_provider: Callable[[], str] | None = None,
        delivery_key: Callable[[str], str] = _delivery_key,
        max_pending_deliveries: Callable[[], int] | None = None,
    ) -> None:
        self._id_provider = id_provider or (lambda: uuid.uuid4().hex[:12])
        self._timestamp_provider = timestamp_provider or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self._delivery_key = delivery_key
        self._max_pending_deliveries = max_pending_deliveries or (
            lambda: MAX_PENDING_SUBAGENT_DELIVERIES
        )

    def queue_append(
        self,
        owner: Any,
        content: str,
        kind: str = "",
        meta: dict | None = None,
        *,
        directive_user_origin: bool = False,
        principal_surface: str = "",
        principal_raw_id: str = "",
    ) -> str:
        """Append an entry and return its process-local queue ID."""
        queue_id = self._id_provider()
        item: dict[str, Any] = {
            "id": queue_id,
            "content": content,
            "kind": kind,
        }
        # Append deliberately retains the producer's metadata object: enqueue
        # sites can finish populating structured facts after constructing it.
        if meta:
            item["meta"] = meta
        if directive_user_origin:
            item["_directive_user_origin"] = True
        self._stamp_principal(item, principal_surface, principal_raw_id)
        owner._queue.append(item)
        owner._note_enqueue()
        return queue_id

    def note_enqueue(self, owner: Any) -> None:
        """Record queue activity beside, rather than inside, an entry."""
        # Queue dicts are compared wholesale on the wire and in persistence
        # tests; placing the clock there would make their shape time-dependent.
        owner._last_enqueue_ts = self._timestamp_provider()

    def queue_insert(
        self,
        owner: Any,
        index: int,
        content: str,
        kind: str = "",
        payload: str = "",
        meta: dict | None = None,
        on_consumed: Callable[[bool], None] | None = None,
        on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
        directive_user_origin: bool = False,
        principal_surface: str = "",
        principal_raw_id: str = "",
    ) -> str:
        """Insert one entry while preserving retry callbacks and provenance."""
        queue_id = self._id_provider()
        item: dict[str, Any] = {
            "id": queue_id,
            "content": content,
            "kind": kind,
            "payload": payload,
        }
        # Insert is the recovery path: its process-local retry entry owns a
        # snapshot, so later producer mutation must not rewrite queued facts.
        if meta:
            item["meta"] = dict(meta)
        if on_consumed is not None:
            item["_on_consumed"] = on_consumed
        if on_irreversibly_consumed is not None:
            item["_on_irreversibly_consumed"] = on_irreversibly_consumed
        if directive_user_origin:
            item["_directive_user_origin"] = True
        self._stamp_principal(item, principal_surface, principal_raw_id)
        owner._queue.insert(index, item)
        owner._note_enqueue()
        return queue_id

    def queue_pop(self, owner: Any, index: int = 0) -> dict[str, Any]:
        """Remove and return the exact entry at *index*."""
        return owner._queue.pop(index)

    def note_pending_subagent_delivery(
        self,
        owner: Any,
        content: str,
        agent_ids: list[str],
    ) -> None:
        """Remember which agents a queued completion still owes delivery."""
        if not content or not agent_ids:
            return
        key = self._delivery_key(content)
        owed = owner._subagent_delivery_pending.setdefault(key, [])
        owed.extend(agent_id for agent_id in agent_ids if agent_id not in owed)
        # Only the consuming row may settle an entry.  A turn tail can dequeue
        # its successor before the current settlement callback runs, so sweeping
        # merely because content left the queue would lose the successor's debt.
        while len(owner._subagent_delivery_pending) > self._max_pending_deliveries():
            owner._subagent_delivery_pending.pop(next(iter(owner._subagent_delivery_pending)))

    def owes_subagent_delivery(self, owner: Any, contents: list[str]) -> bool:
        """Return whether any named completion has unsettled delivery debt."""
        return any(
            self._delivery_key(content) in owner._subagent_delivery_pending for content in contents
        )

    def take_pending_subagent_deliveries(self, owner: Any, contents: list[str]) -> list[str]:
        """Claim delivery marks in consumed-row order and forget only those rows."""
        claimed: list[str] = []
        for content in contents:
            claimed.extend(owner._subagent_delivery_pending.pop(self._delivery_key(content), []))
        return claimed

    def queue_remove_by_id(self, owner: Any, queue_id: str) -> str | None:
        """Remove the matching entry and return its content."""
        for index, item in enumerate(owner._queue):
            if item["id"] == queue_id:
                del owner._queue[index]
                return item["content"]
        return None

    def queue_edit_by_id(
        self,
        owner: Any,
        queue_id: str,
        content: str,
        *,
        directive_user_origin: bool = False,
        principal_surface: str = "",
        principal_raw_id: str = "",
    ) -> bool:
        """Edit a user-owned entry without changing its identity or position."""
        for item in owner._queue:
            if item["id"] != queue_id:
                continue
            # Retry callbacks settle the exact automatic payload that failed;
            # moving them to replacement text would acknowledge the wrong work.
            if "_on_consumed" in item or "_on_irreversibly_consumed" in item:
                return False
            # The lists index the OLD text's markers; drop only what this edit
            # removed (named before, unnamed now) and renumber the survivors
            # (prune_attachment_meta). An entry the old text never named is
            # not the edit's to drop.
            previous = item.get("content")
            item["content"] = prune_attachment_meta(
                item.get("meta"), content, previous if isinstance(previous, str) else ""
            )
            if directive_user_origin:
                item["_directive_user_origin"] = True
            else:
                item.pop("_directive_user_origin", None)
            # Replacement text is a new speaker unless both identity fields
            # are present; a partial pair must not leave the old principal.
            if principal_surface and principal_raw_id:
                self._stamp_principal(item, principal_surface, principal_raw_id)
            else:
                item.pop("_principal_surface", None)
                item.pop("_principal_raw_id", None)
            return True
        return False

    @staticmethod
    def _stamp_principal(item: dict[str, Any], surface: str, raw_id: str) -> None:
        """Stamp queue-item provenance only when both identity fields are set."""
        if surface and raw_id:
            item["_principal_surface"] = surface
            item["_principal_raw_id"] = raw_id

    def queue_promote_by_id(self, owner: Any, queue_id: str) -> bool:
        """Move the exact matching entry to the front without rebuilding it."""
        for index, item in enumerate(owner._queue):
            if item["id"] == queue_id:
                owner._queue.insert(0, owner._queue.pop(index))
                return True
        return False
