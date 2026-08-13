"""Runtime sub-question queue for Research Lab v2 recursive exploration.

Pure, side-effect-free queue logic over a plain dict so it is trivially
testable; thin ``load_queue``/``save_queue`` helpers persist it to the campaign
dir as ``subquestion_queue.json``. In Stage 5 the agent generates candidate
sub-questions from intermediate findings each round, enqueues the top-K here,
and the cycle loop dequeues them for investigation after the initial questions
are done — recursing until the done/success condition or ``max_cycles``.

Queue shape::

    {
      "pending":  [Item, ...],   # awaiting investigation, priority-ordered
      "analyzed": [Item, ...],   # already investigated (kept for dedup + provenance)
    }

Item::

    {id, text, depth, parent_id, priority, origin, status}

This module deliberately performs NO agent calls and NO model work — it is the
state model only. Ranking/relevance and budget come from the caller (Stage 5/6).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

QUEUE_FILENAME = "subquestion_queue.json"
_WS_RE = re.compile(r"\s+")


def new_queue() -> dict:
    """An empty queue with both buckets present."""
    return {"pending": [], "analyzed": []}


def _norm(text: str) -> str:
    """Dedup key: lowercased, whitespace-collapsed, stripped."""
    return _WS_RE.sub(" ", str(text).strip().lower())


def _known_keys(queue: dict) -> set:
    """Normalized text keys already present (pending OR analyzed)."""
    keys: set = set()
    for bucket in ("pending", "analyzed"):
        for it in queue.get(bucket, []):
            if isinstance(it, dict) and it.get("text"):
                keys.add(_norm(it["text"]))
    return keys


def _new_item_id() -> str:
    return "q" + uuid.uuid4().hex[:8]


def enqueue(
    queue: dict,
    candidates: list,
    *,
    parent_id: str | None = None,
    depth: int = 0,
    max_admit: int | None = None,
) -> list[dict]:
    """Admit new sub-questions into ``pending``.

    ``candidates`` are dicts ``{"text", "priority"?}`` or plain strings.
    De-duplicates (case/whitespace-insensitive) against everything already in
    the queue AND within the batch. Admits at most ``max_admit`` of the
    survivors, highest ``priority`` first (stable for ties). Returns the items
    actually admitted (so the caller can log/emit them).
    """
    known = _known_keys(queue)
    admitted: list[dict] = []
    seen_in_batch: set = set()
    # (priority, original-order, text) — sort by priority desc, stable on order.
    norm_cands: list[tuple[float, int, str]] = []
    for order, c in enumerate(candidates):
        if isinstance(c, dict):
            text = str(c.get("text", "")).strip()
            priority = float(c.get("priority", 0.0))
        else:
            text = str(c).strip()
            priority = 0.0
        if text:
            norm_cands.append((priority, order, text))
    norm_cands.sort(key=lambda t: (-t[0], t[1]))
    for priority, _order, text in norm_cands:
        if max_admit is not None and len(admitted) >= max_admit:
            break
        key = _norm(text)
        if key in known or key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        admitted.append({
            "id": _new_item_id(),
            "text": text,
            "depth": int(depth),
            "parent_id": parent_id,
            "priority": priority,
            "origin": "emergent",
            "status": "pending",
        })
    queue.setdefault("pending", []).extend(admitted)
    return admitted


def dequeue_top_k(queue: dict, k: int) -> list[dict]:
    """Remove and return up to ``k`` highest-priority pending items.

    Ties broken by current position (stable). Items are removed from
    ``pending``; the caller investigates them, then calls ``mark_analyzed``.
    """
    if k <= 0:
        return []
    pending = queue.get("pending", [])
    ordered = sorted(
        range(len(pending)),
        key=lambda i: (-float(pending[i].get("priority", 0.0)), i),
    )
    take = ordered[:k]
    take_set = set(take)
    out = [pending[i] for i in take]
    queue["pending"] = [it for idx, it in enumerate(pending) if idx not in take_set]
    return out


def mark_analyzed(queue: dict, items: list[dict]) -> None:
    """Record ``items`` as analyzed (for dedup + provenance)."""
    analyzed = queue.setdefault("analyzed", [])
    for it in items:
        if isinstance(it, dict):
            rec = dict(it)
            rec["status"] = "analyzed"
            analyzed.append(rec)


def pending_count(queue: dict) -> int:
    return len(queue.get("pending", []))


def analyzed_count(queue: dict) -> int:
    return len(queue.get("analyzed", []))


def is_known(queue: dict, text: str) -> bool:
    """True if ``text`` (normalized) is already pending or analyzed."""
    return _norm(text) in _known_keys(queue)


def normalize(text: str) -> str:
    """Public dedup-key normalizer (lowercased, whitespace-collapsed, stripped).

    Exposed so callers can dedup candidate sub-questions against an external
    checklist (e.g. the campaign's existing ``sub_questions``) before enqueue.
    """
    return _norm(text)


def next_depth(queue: dict) -> int:
    """Depth to stamp on the NEXT admitted round (1-based, increases per round).

    Returns 1 when nothing has been analyzed yet, else ``max(analyzed depth)+1``
    so each successive exploration round is one level deeper — letting the
    caller decay priority by depth so deeper leads rank lower.
    """
    depths = [
        int(it.get("depth", 0))
        for it in queue.get("analyzed", [])
        if isinstance(it, dict)
    ]
    return (max(depths) + 1) if depths else 1


# --- thin persistence (campaign dir) ---

def load_queue(campaign_dir: Path) -> dict:
    """Load the queue from the campaign dir; empty queue on missing/corrupt."""
    p = Path(campaign_dir) / QUEUE_FILENAME
    if not p.exists():
        return new_queue()
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return new_queue()
    if not isinstance(data, dict):
        return new_queue()
    data.setdefault("pending", [])
    data.setdefault("analyzed", [])
    return data


def save_queue(campaign_dir: Path, queue: dict) -> None:
    """Atomically persist the queue to the campaign dir."""
    p = Path(campaign_dir) / QUEUE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(queue, indent=2))
    tmp.replace(p)
