"""The history search window answered from the chat's search cache.

``chat_history_search.search_history`` reads the transcript on every request and
counts only the rows one reply read. This module keeps, per chat, what each row
holds for a search (``kiro_crew.chat_search_cache``): the message's redacted text
case-folded (the form a query matches, indexed by FTS5 trigrams), its excerpt
source, its kinds as a bit mask, its time, its pin ids and the parts of its hit
that no query changes. A search then reads no transcript row, and its ``counts``
cover the whole chat, so every tab shows the same numbers.

A query whose every required term has at least three characters is looked up in
the trigram index; one with a shorter term (a CJK word, ``pr``) reads the stored
redacted text instead. When the lookup cannot decide a term on its own (an
exclusion, a number bounded by non-digits, a CJK adjacency floor), each candidate
is confirmed with ``needles_match_text``, the gate the paged scan applies. Steer
hiding, dates, ``before``, pins and the caller's artifact scope are applied per
request, never stored.

The first search of a chat in this process never waits for the cache: the paged
scan answers it with ``indexing: true``, and only then does :func:`start_build`
start the build in a worker thread, so the build does not slow that reply. A
request that arrives while it runs is answered the same way, and no second build
starts. A chat whose cache passed the disk cap alone starts no build and gets
no ``indexing``. Once this process holds the chat's cache, a cache that matches
the transcript answers at once, off the build thread, so it never waits behind
another chat's build; one with rows written since is caught up on the build
thread, and a request waits at most ``BUILD_WAIT_SECS`` for it; a longer update
keeps running and the paged scan answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kiro_crew import chat_search_cache as csc
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard import chat_history_search as chs
from kiro_crew.history_index import fold_texts, storable_text
from kiro_crew.history_projection import TranscriptRevisionChanged
from kiro_crew.history_search import needles_match_text
from kiro_crew.search_shown_text import shown_text

logger = logging.getLogger(__name__)

#: Seconds a request waits for its chat's cache before the paged scan answers it.
BUILD_WAIT_SECS = 0.25
#: Longest a build waits between two pages for the paged scans to finish: a
#: scan and a build both read and redact the chat, and the scan has a person
#: waiting. Past it the build reads one page anyway, so a busy chat still builds.
SCAN_YIELD_SECS = 5.0
#: Shortest term the trigram index can look up.
_MIN_TRIGRAM_CHARS = 3
#: Values bound per ``IN (...)`` list when pins are looked up.
_IN_CHUNK = 500
_KIND_BITS = {kind: 1 << i for i, kind in enumerate(chs.KINDS)}
#: The kinds a stored row carries; ``pinned`` is per caller, never stored.
_STORED = [kind for kind in chs.KINDS if kind != "pinned"]
_HTML = _KIND_BITS["html"]
# A superseded steer is hidden when a later row offers the same text with its
# successor id, or with no id (the chat's queue-pop rows).
_VISIBLE = (
    "NOT (r.steer IS NOT NULL AND EXISTS (SELECT 1 FROM rows l WHERE l.idx > r.idx"
    " AND l.succ IN (r.steer, substr(r.steer, 1, instr(r.steer, ':')))))"
)

_updates: dict[str, asyncio.Future] = {}
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_scans = 0
_scans_done = threading.Condition()


def _text_id(content: str) -> str:
    """The id a steer and its successor share for equal text; the text is not kept."""
    return hashlib.blake2b(content.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()


def cache_entry(msg: dict, index: int) -> tuple | None:
    """The ``rows`` record the cache keeps of transcript row *msg* at *index*, or None.

    Every text is redacted before it is kept: the query matches ``body``, the
    redacted message case-folded, never the raw row. Every string is made
    storable (``storable_text``), since SQLite refuses a lone surrogate.
    """
    record = _entry(msg, index)
    if record is None:
        return None
    return tuple(storable_text(v) if isinstance(v, str) else v for v in record)


def _entry(msg: dict, index: int) -> tuple | None:
    steer = succ = None
    content = msg.get("content")
    meta = msg.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    if msg.get("role") == "user" and isinstance(content, str):
        successor = meta.get("supersededBy")
        if (
            meta.get("steer") is True
            and meta.get("steerState") == "superseded"
            and isinstance(successor, str)
            and successor
        ):
            steer = f"{_text_id(content)}:{successor}"
        later = chs._successor_key(msg)
        if later is not None:
            succ = f"{_text_id(later[0])}:{later[1] or ''}"
    row = chs._read_row(msg)
    if row is None:
        if steer is None and succ is None:
            return None
        return (index, None, 0, None, None, None, 0, None, steer, succ, 0, 0, None, None)
    bits = 0
    for kind in row.kinds:
        bits |= _KIND_BITS[kind]
    # A slug redaction would change is no artifact id: it is never kept.
    art = [[shown_text(label), slug] for label, slug in row.art if shown_text(slug) == slug]
    mid = meta.get("mid")
    ts = msg.get("ts")
    return (
        index,
        chs._row_epoch(ts),
        bits,
        fold_texts([row.match]),
        row.shown,
        json.dumps(chs.static_hit(msg, index, row), ensure_ascii=False),
        len(row.html),
        json.dumps(art, ensure_ascii=False) if art else None,
        steer,
        succ,
        int(bool(row.match.strip())),
        1,
        mid if isinstance(mid, str) else None,
        ts if isinstance(ts, str) else None,
    )


def _lookup(query: Any) -> tuple[str | None, bool, list[list[str]]]:
    """``(MATCH expression, exact, substrings)`` for *query*.

    The expression ANDs every required term the trigram index can look up (each
    an OR of its spellings, each spelling its trigrams ANDed), so it finds a
    superset of the matching rows. A spelling longer than one trigram is also
    listed in *substrings*, which the query tests on the stored text in SQL
    (``instr``). It is exact, needing no confirmation in Python, when the two
    cover every term the gate reads.
    """
    parts: list[str] = []
    substrings: list[list[str]] = []
    exact = True
    for needle in query.needles:
        if not needle.required:
            # An exclusion or an adjacency term is read only by the gate.
            exact = exact and not (needle.excluded or needle.adjacency)
            continue
        spellings = [s for s in (needle.text, *needle.alts) if s]
        if not spellings or any(len(s) < _MIN_TRIGRAM_CHARS for s in spellings):
            exact = False
            continue
        exact = exact and not needle.digit_bounded
        parts.append("(" + " OR ".join(_term(s) for s in spellings) + ")")
        if any(len(s) > _MIN_TRIGRAM_CHARS for s in spellings):
            # Trigrams ANDed find a superset; the substring test makes it exact.
            substrings.append(spellings)
    return (" AND ".join(parts) or None), exact and bool(parts), substrings


def _quoted(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _term(text: str) -> str:
    """The MATCH expression of one spelling: its distinct trigrams ANDed (the
    table keeps no positions, so FTS5 refuses a phrase)."""
    grams = dict.fromkeys(text[i : i + _MIN_TRIGRAM_CHARS] for i in range(len(text) - 2))
    return "(" + " AND ".join(_quoted(g) for g in grams) + ")"


def _where(
    expr: str | None,
    substrings: list[list[str]],
    since: float | None,
    until: float | None,
    filter_only: bool,
) -> tuple[str, list]:
    clauses = ["r.msg = 1", _VISIBLE]
    params: list = []
    if expr is not None:
        clauses.append("r.idx IN (SELECT rowid FROM rows_fts WHERE rows_fts MATCH ?)")
        params.append(expr)
    for spellings in substrings:
        clauses.append("(" + " OR ".join("instr(r.body, ?) > 0" for _ in spellings) + ")")
        params.extend(spellings)
    if since is not None:
        clauses.append("(r.at IS NULL OR r.at >= ?)")
        params.append(since)
    if until is not None:
        clauses.append("(r.at IS NULL OR r.at < ?)")
        params.append(until)
    if filter_only:
        # With no query a row lists when it says something or carries a kind.
        clauses.append("(r.nonempty = 1 OR r.kinds != 1)")
    return " AND ".join(clauses), params


def _html_labels(art: str | None, artifact_kind: Callable[[str], str | None] | None) -> list[str]:
    if not art or artifact_kind is None:
        return []
    return [label or slug for label, slug in json.loads(art) if artifact_kind(slug) == "html"]


def _pinned_rows(conn: Any, where: str, params: list, pinned: chs.Pinned | None) -> set[int]:
    """The matched rows *pinned* holds, by ``mid`` or by ``ts``."""
    found: set[int] = set()
    if pinned is None:
        return found
    for column, values in (("mid", sorted(pinned.mids)), ("ts", sorted(pinned.times))):
        for at in range(0, len(values), _IN_CHUNK):
            chunk = values[at : at + _IN_CHUNK]
            marks = ", ".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT r.idx FROM rows r WHERE {where} AND r.{column} IN ({marks})",
                [*params, *chunk],
            ).fetchall()
            found.update(r[0] for r in rows)
    return found


def _hit(static: str, shown: str, html_files: int, labels: list[str], query: Any) -> dict[str, Any]:
    hit = json.loads(static)
    hit["snippet"] = chs.hit_snippet(shown, query)
    return chs.with_html_labels(hit, html_files, labels)


def search_cached(
    store: csc.ChatSearchCache,
    snap: csc.Snapshot,
    *,
    query: Any,
    kind: str,
    since: float | None,
    until: float | None,
    before: int | None,
    limit: int,
    artifact_kind: Callable[[str], str | None] | None = None,
    pinned: chs.Pinned | None = None,
) -> dict[str, Any]:
    """``search_history``'s reply, read from the cache; ``counts`` cover the whole chat.

    Hits come newest first from *before* down. ``next_before`` is the last hit's
    index when more hits are left below it, else None. Blocking IO: call it off
    the loop.
    """
    # As the paged scan does: a cursor past the chat is its end, and SQLite
    # cannot bind an integer wider than 64 bits.
    if before is not None:
        before = min(before, snap.total)
    expr, exact, substrings = _lookup(query) if query is not None else (None, True, [])
    where, params = _where(expr, substrings, since, until, query is None)
    with store.reading(snap) as conn:
        pins = _pinned_rows(conn, where, params, pinned)
        if exact:
            counts, picked = _exact(conn, where, params, kind, before, limit, artifact_kind, pins)
        else:
            counts, picked = _confirmed(
                conn, where, params, query, kind, before, limit, artifact_kind, pins
            )
        counts["pinned"] = len(pins)
        hits = [
            _hit(static, shown, html_files, labels, query)
            for static, shown, html_files, labels in picked[:limit]
        ]
    store.touch(snap.key)
    return {
        "hits": hits,
        "next_before": hits[-1]["index"] if len(picked) > limit and hits else None,
        "total": snap.total,
        "counts": counts,
        "complete": True,
    }


def _exact(
    conn: Any,
    where: str,
    params: list,
    kind: str,
    before: int | None,
    limit: int,
    artifact_kind: Callable[[str], str | None] | None,
    pins: set[int],
) -> tuple[dict[str, int], list]:
    """Counts from one ``SUM`` query, hits from one ordered read: the lookup
    decides every term, so no row's text is read."""
    sums = ", ".join(f"COALESCE(SUM((r.kinds >> {i}) & 1), 0)" for i in range(len(_STORED)))
    totals = conn.execute(f"SELECT {sums} FROM rows r WHERE {where}", params).fetchone()
    counts = dict(zip(_STORED, (int(n) for n in totals)))
    # A linked saved html artifact counts as HTML for the callers that may read it.
    for (art,) in conn.execute(
        f"SELECT r.art FROM rows r WHERE {where} AND r.art IS NOT NULL AND (r.kinds & ?) = 0",
        [*params, _HTML],
    ):
        if _html_labels(art, artifact_kind):
            counts["html"] += 1
    if limit <= 0 or (kind == "pinned" and not pins):
        return counts, []
    sql = f"SELECT r.idx, r.kinds, r.shown, r.static_hit, r.html_files, r.art FROM rows r WHERE {where}"
    args = list(params)
    if before is not None:
        sql += " AND r.idx < ?"
        args.append(before)
    if kind == "html":
        sql += " AND ((r.kinds & ?) != 0 OR r.art IS NOT NULL)"
        args.append(_HTML)
    elif kind not in ("", "pinned"):
        sql += " AND (r.kinds & ?) != 0"
        args.append(_KIND_BITS[kind])
    picked: list = []
    for idx, bits, shown, static, html_files, art in conn.execute(
        sql + " ORDER BY r.idx DESC", args
    ):
        labels = _html_labels(art, artifact_kind)
        if kind == "html" and not (bits & _HTML or labels):
            continue
        if kind == "pinned" and idx not in pins:
            continue
        picked.append((static, shown, html_files, labels))
        if len(picked) > limit:
            break
    return counts, picked


def _confirmed(
    conn: Any,
    where: str,
    params: list,
    query: Any,
    kind: str,
    before: int | None,
    limit: int,
    artifact_kind: Callable[[str], str | None] | None,
    pins: set[int],
) -> tuple[dict[str, int], list]:
    """Counts and hits from the stored redacted text, each candidate confirmed
    with the paged scan's own gate."""
    counts = dict.fromkeys(_STORED, 0)
    matched: list[int] = []
    want = _KIND_BITS[kind]
    rows = conn.execute(
        f"SELECT r.idx, r.kinds, r.art, r.body FROM rows r WHERE {where} ORDER BY r.idx DESC",
        params,
    )
    confirmed_pins: set[int] = set()
    for idx, bits, art, body in rows:
        if not needles_match_text(query.needles, body or "", query.adjacency_floor):
            continue
        if art and not bits & _HTML and _html_labels(art, artifact_kind):
            bits |= _HTML
        if idx in pins:
            confirmed_pins.add(idx)
            bits |= _KIND_BITS["pinned"]
        for name in _STORED:
            if bits & _KIND_BITS[name]:
                counts[name] += 1
        if limit > 0 and bits & want and (before is None or idx < before) and len(matched) <= limit:
            matched.append(idx)
    # The pins a candidate's text did not confirm are no hits.
    pins.intersection_update(confirmed_pins)
    picked = []
    for idx in matched:
        shown, static, html_files, art = conn.execute(
            "SELECT shown, static_hit, html_files, art FROM rows WHERE idx = ?", (idx,)
        ).fetchone()
        picked.append((static, shown, html_files, _html_labels(art, artifact_kind)))
    return counts, picked


def _sessions_dir(log: Any) -> Path | None:
    found = getattr(log, "_dir", None)
    return found if isinstance(found, Path) else None


def _marker(store: csc.ChatSearchCache, key: str) -> str:
    return f"{store.dir}\0{key}"


def _store_of(log: Any) -> csc.ChatSearchCache | None:
    sessions_dir = _sessions_dir(log)
    if sessions_dir is None or not csc.available():
        return None
    return csc.store_for(sessions_dir)


def _update_task(store: csc.ChatSearchCache, log: Any, key: str) -> asyncio.Future:
    """*key*'s running update, or a new one: at most one per chat at a time."""
    marker = _marker(store, key)
    task = _updates.get(marker)
    if task is None or task.done():
        # One dedicated thread runs every build, one at a time, so a build never
        # holds a default-executor thread that a search needs. It runs in a
        # fresh context, never in the request's: the text it stores is always
        # the redacted form.
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(_builds(), store.update, log, key, cache_entry, _yield)
        _updates[marker] = task
        task.add_done_callback(lambda done: _forget(marker, done))
    return task


async def cached_search(log: Any, key: str, **search: Any) -> dict[str, Any] | None:
    """The reply from *key*'s cache, brought up to date first (on the build
    thread only when it is behind the transcript).

    None when the cache cannot answer: the log keeps no sessions directory, this
    SQLite has no FTS5, this process has not built the chat's cache yet (the
    caller answers from the paged scan, then calls :func:`start_build`), the
    update failed, or it is still running after ``BUILD_WAIT_SECS``.
    """
    store = _store_of(log)
    if store is None or not store.holds(key):
        # A first search never waits for a build: the paged scan is faster.
        return None
    try:
        # An up-to-date cache answers without the build thread, which may be
        # busy building another chat.
        snap = await asyncio.to_thread(store.current, log, key)
        if snap is None:
            task = _update_task(store, log, key)
            finished, _pending = await asyncio.wait({task}, timeout=BUILD_WAIT_SECS)
            if not finished:
                return None
            snap = task.result()
        return await asyncio.to_thread(search_cached, store, snap, **search)
    except (
        OSError,
        ValueError,
        OverflowError,
        TranscriptRevisionChanged,
        csc.CacheDropped,
        sqlite3.Error,
    ):
        # ValueError and OverflowError cover a value SQLite cannot bind: a cache
        # that cannot answer never fails the request, the paged scan answers it.
        logger.warning(
            "history search: the search cache of %s could not answer", key, exc_info=True
        )
        return None


@contextlib.contextmanager
def scanning() -> Iterator[None]:
    """Mark a paged scan as running for the block: builds wait for it."""
    global _scans
    with _scans_done:
        _scans += 1
    try:
        yield
    finally:
        with _scans_done:
            _scans -= 1
            _scans_done.notify_all()


def _yield() -> None:
    with _scans_done:
        _scans_done.wait_for(lambda: _scans == 0, timeout=SCAN_YIELD_SECS)


def start_build(log: Any, key: str) -> bool:
    """Start bringing *key*'s cache up to date in a worker thread, unless an
    update already runs; whether one runs. Call it after the paged scan has
    answered, so the build does not slow that reply. A chat whose cache the
    store refuses (it passed the disk cap alone) starts nothing: no build of it
    can finish."""
    store = _store_of(log)
    if store is None or store.too_large(key):
        return False
    return not _update_task(store, log, key).done()


def _builds() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-search-cache")
        return _executor


def _forget(marker: str, task: asyncio.Future) -> None:
    if _updates.get(marker) is task:
        del _updates[marker]
    if task.cancelled():
        return
    # Read so a failed build that no request waited for is not reported as an
    # exception never retrieved; the next search answers from the scan and
    # builds again.
    failed = task.exception()
    if failed is not None and not isinstance(failed, csc.CacheDropped):
        logger.warning("history search: building a search cache failed", exc_info=failed)


__all__ = [
    "BUILD_WAIT_SECS",
    "cache_entry",
    "cached_search",
    "search_cached",
    "start_build",
]
