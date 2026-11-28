"""A chat's history search cache (``kiro_crew.chat_search_cache``, SQLite FTS5) and
the search window that answers from it (``dashboard/chat_history_cached.py``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import chat_search_cache as csc
from kiro_crew import history_files
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.dashboard import chat_history_cached as chc
from kiro_crew.dashboard import chat_history_search as shs
from kiro_crew.dashboard.handlers.sessions import message_query
from kiro_crew.history import ConversationLog
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite has no FTS5")

KEY = "dashboard:chat-1"
SEARCH = "/api/chat/slots/{slot}/history/search"
COUNTS = "/api/chat/slots/{slot}/history/counts"


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    return ConversationLog(base_dir=tmp_path)


def _decoded(log, monkeypatch) -> list[int]:
    """Count the transcript rows every page read hands back."""
    seen = [0]
    real = log.read_messages_chained_page

    def counted(*args, **kwargs):
        page = real(*args, **kwargs)
        seen[0] += len(page.messages)
        return page

    monkeypatch.setattr(log, "read_messages_chained_page", counted)
    return seen


def _store(log) -> csc.ChatSearchCache:
    return csc.store_for(log._dir)


def _update(log, key: str = KEY) -> csc.Snapshot:
    return _store(log).update(log, key, chc.cache_entry)


def _cached(log, snap, q: str | None = None, **kw) -> dict:
    kw.setdefault("kind", "")
    kw.setdefault("since", None)
    kw.setdefault("until", None)
    kw.setdefault("before", None)
    kw.setdefault("limit", 30)
    return chc.search_cached(_store(log), snap, query=message_query(q) if q else None, **kw)


def _counts(log, snap, q: str | None, kind: str = "") -> dict:
    return _cached(log, snap, q, kind=kind)["counts"]


def _rewrite(log, rows: list[dict], key: str = KEY) -> None:
    """Replace the transcript file the way the dashboard save does: a new file."""
    path = log._path(key)
    head = path.read_text(encoding="utf-8").splitlines()[0]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join([head, *(json.dumps(r) for r in rows)]) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log._invalidate_cache(key)


def _rows(log) -> list[dict]:
    return list(log.read_messages(KEY))


def _plums(log, n: int) -> None:
    for i in range(n):
        log.append(KEY, "user", f"plum note {i}")


def test_new_rows_are_appended_without_reading_the_old_ones_again(log, monkeypatch):
    _plums(log, csc.VERIFY_ROWS + 50)
    first = _update(log)
    assert _counts(log, first, "plum")[""] == csc.VERIFY_ROWS + 50

    decoded = _decoded(log, monkeypatch)
    log.append(KEY, "assistant", "a fresh pear reply")
    later = _update(log)

    # The window it checks, row 0 and the one new row, never the whole chat again.
    assert decoded[0] <= csc.VERIFY_ROWS + 2
    assert _counts(log, later, "pear")[""] == 1
    assert _counts(log, later, "plum")[""] == csc.VERIFY_ROWS + 50


def test_a_save_that_rewrites_the_same_rows_does_not_rebuild(log, monkeypatch):
    _plums(log, csc.VERIFY_ROWS + 50)
    _update(log)
    _rewrite(log, _rows(log))

    decoded = _decoded(log, monkeypatch)
    snap = _update(log)

    assert decoded[0] <= csc.VERIFY_ROWS + 1
    assert _counts(log, snap, "plum")[""] == csc.VERIFY_ROWS + 50


@pytest.mark.parametrize(
    "change", ["first row edited", "rows taken back and refilled", "fewer rows"]
)
def test_a_rewrite_that_changed_old_rows_rebuilds_the_cache(log, monkeypatch, change):
    _plums(log, csc.VERIFY_ROWS + 50)
    _update(log)
    rows = _rows(log)
    if change == "first row edited":
        rows[0] = {**rows[0], "content": "a cherry instead"}
    elif change == "rows taken back and refilled":
        rows = rows[:10] + [{**r, "content": "a cherry instead"} for r in rows[10:]] + rows[:5]
    else:
        rows = rows[:-5]
    _rewrite(log, rows)

    decoded = _decoded(log, monkeypatch)
    snap = _update(log)

    assert decoded[0] >= len(rows)
    expected = sum("cherry" in r["content"] for r in rows)
    assert _counts(log, snap, "cherry")[""] == expected
    assert _counts(log, snap, "plum")[""] == sum("plum" in r["content"] for r in rows)


def test_an_edited_recent_row_is_read_again(log):
    _plums(log, 20)
    _update(log)
    rows = _rows(log)
    rows[-3] = {**rows[-3], "content": "now about a cherry"}
    _rewrite(log, rows)

    snap = _update(log)

    assert _counts(log, snap, "cherry")[""] == 1
    assert _counts(log, snap, "plum")[""] == 19


def test_the_text_is_redacted_before_it_is_matched_or_kept(log):
    log.append(KEY, "user", "the plum bot")
    rows = _rows(log)
    # Written past the append path, which would redact it on the way in: a
    # transcript can hold one (an older row, another writer).
    rows[0] = {**rows[0], "content": f"my token is {SAMPLE_SLACK_BOT_TOKEN} for the plum bot"}
    _rewrite(log, rows)
    snap = _update(log)
    part = SAMPLE_SLACK_BOT_TOKEN[8:24]

    assert _counts(log, snap, part)[""] == 0
    assert _counts(log, snap, "plum")[""] == 1
    path = _store(log).path(KEY)
    stored = b"".join(
        p.read_bytes() for p in (path, path.with_name(path.name + "-wal")) if p.exists()
    )
    assert part.encode() not in stored


def test_a_short_query_reads_the_stored_text_and_still_matches(log):
    log.append(KEY, "user", "see ab here")
    log.append(KEY, "assistant", "中文搜索 works")
    log.append(KEY, "user", "nothing")
    snap = _update(log)

    assert [h["index"] for h in _cached(log, snap, "ab")["hits"]] == [0]
    assert [h["index"] for h in _cached(log, snap, "中文")["hits"]] == [1]
    assert [h["index"] for h in _cached(log, snap, "中文 works")["hits"]] == [1]


def _mixed(log) -> None:
    """A chat with every shape the gate and the per-request filters read."""
    log.append(KEY, "user", "plum one", mid="u0")
    log.append(KEY, "assistant", "plum pie https://example.com/a", mid="a1")
    log.append(KEY, "user", "a plum picture", mid="u2", extra_meta={"files": ["/tmp/p.png"]})
    log.append(KEY, "user", "plum -pear and pear", mid="u3")
    log.append(KEY, "assistant", "ticket pr 4411 merged", mid="a4")
    log.append(KEY, "assistant", "pull/44110 is another", mid="a5")
    log.append(
        KEY,
        "user",
        "steer plum",
        mid="u6",
        extra_meta={"steer": True, "steerState": "superseded", "supersededBy": "u7"},
    )
    log.append(KEY, "user", "steer plum", mid="u7")
    log.append(KEY, "assistant", "中文 plum 搜索", mid="a8")


@pytest.mark.parametrize(
    "q, kind",
    [
        ("plum", ""),
        ("plum", "images"),
        ("plum", "links"),
        ("plum", "pinned"),
        ("plum -pear", ""),
        ('"plum pie"', ""),
        ("picture", ""),
        ("pr 4411", ""),
        ("中文", ""),
        ("搜索 plum", ""),
        (None, "images"),
        (None, "pinned"),
    ],
)
def test_the_cache_answers_what_the_paged_scan_answers(log, q, kind):
    _mixed(log)
    pinned = shs.Pinned([{"mid": "u2"}, {"mid": "a1"}])
    snap = _update(log)
    query = message_query(q) if q else None

    cached = _cached(log, snap, q, kind=kind, pinned=pinned, limit=50)
    scanned = shs.search_history(
        log,
        KEY,
        query=query,
        kind=kind,
        since=None,
        until=None,
        before=None,
        limit=50,
        pinned=pinned,
    )

    assert cached["hits"] == scanned["hits"]
    assert cached["counts"] == scanned["counts"]
    assert cached["complete"] is True


def test_hits_page_below_the_last_one(log):
    _plums(log, 40)
    snap = _update(log)

    first = _cached(log, snap, "plum", limit=30)
    rest = _cached(log, snap, "plum", limit=30, before=first["next_before"])

    assert first["next_before"] == first["hits"][-1]["index"] == 10
    assert [h["index"] for h in rest["hits"]] == list(range(9, -1, -1))
    assert rest["next_before"] is None
    assert first["counts"] == rest["counts"]


def test_the_least_recently_searched_caches_go_first_past_the_disk_cap(log):
    store = _store(log)
    keys = ["dashboard:a", "dashboard:b", "dashboard:c"]
    for key in keys:
        log.append(key, "user", "plum " * 200)
        _update(log, key)
    # Closed first: the close checkpoints the WAL, which moves the mtime.
    store.close_all()
    for n, key in enumerate(keys):
        os.utime(store.path(key), (1_000_000 + n, 1_000_000 + n))

    def size(key):
        base = store.path(key)
        return sum(p.stat().st_size for p in base.parent.glob(base.name + "*"))

    sizes = {key: size(key) for key in keys}
    # "a" was searched again last.
    os.utime(store.path("dashboard:a"), (2_000_000, 2_000_000))

    left = store.evict(cap=sizes["dashboard:a"] + sizes["dashboard:c"])

    assert not store.path("dashboard:b").exists()
    assert store.path("dashboard:a").exists() and store.path("dashboard:c").exists()
    assert left == sizes["dashboard:a"] + sizes["dashboard:c"]


def test_a_chat_whose_cache_alone_passes_the_cap_is_not_kept(log, monkeypatch):
    _plums(log, 20)
    # Smaller than any cache file: the first committed page passes it.
    monkeypatch.setattr(csc, "DISK_CAP_BYTES", 1024)
    store = _store(log)

    with pytest.raises(csc.CacheTooLarge):
        _update(log)
    assert list(store.dir.glob(store.path(KEY).name + "*")) == []
    # Not built again: the next update refuses before reading any row.
    decoded = _decoded(log, monkeypatch)
    with pytest.raises(csc.CacheTooLarge):
        _update(log)
    assert decoded == [0]
    assert not store.path(KEY).exists()


def test_rows_read_again_that_pass_the_cap_remove_the_cache(log, monkeypatch):
    _plums(log, 20)
    _update(log)
    store = _store(log)
    stem = csc._safe_key(KEY)
    monkeypatch.setattr(csc, "DISK_CAP_BYTES", store._disk_size(stem) + 64 * 1024)
    rows = _rows(log)
    # No new row: only the verify window's re-read commits the longer text.
    long_text = " ".join(f"w{i}x{i * 7919 % 100003}" for i in range(40_000))
    rows[-3] = {**rows[-3], "content": long_text}
    _rewrite(log, rows)
    decoded = _decoded(log, monkeypatch)

    with pytest.raises(csc.CacheTooLarge):
        _update(log)

    assert decoded[0] <= csc.VERIFY_ROWS
    assert list(store.dir.glob(store.path(KEY).name + "*")) == []
    assert store.too_large(KEY)


def test_a_deleted_chat_takes_its_search_cache_with_it(log):
    log.append(KEY, "user", "plum")
    _update(log)
    path = _store(log).path(KEY)
    assert path.exists()

    assert log.delete_session(KEY)

    assert list(path.parent.glob(path.name + "*")) == []


def test_a_dropped_cache_is_not_written_back_by_its_build(log):
    log.append(KEY, "user", "plum")
    snap = _update(log)

    assert csc.drop(log._dir, [KEY])

    with pytest.raises(csc.CacheDropped):
        _cached(log, snap, "plum")
    assert not _store(log).path(KEY).exists()


def _cached_then_rotated(log, monkeypatch) -> list:
    """A cache built while ``tamarind`` was live, then a rotation that archives it."""
    log.append(KEY, "user", "tamarind is the word only the first message holds")
    _update(log)
    monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
    monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    for i in range(20):
        log.append(KEY, "user", f"filler message number {i:02d} with enough text to rotate")
    # The rotation's archive write starts the hourly sweep on its own thread.
    sweep = history_files._cleanup_thread
    if sweep is not None:
        sweep.join(FALLBACK_TIMEOUT_SECS)
        assert not sweep.is_alive()
    segments = sorted((log._dir / "archive").glob(f"{csc._safe_key(KEY)}__*.jsonl"))
    assert any(b"tamarind" in seg.read_bytes() for seg in segments), "fixture must rotate"
    assert all("tamarind" not in m.get("content", "") for m in _rows(log))
    assert _holding(_store(log), "tamarind")
    for seg in segments:
        os.utime(seg, (1_000_000, 1_000_000))
    monkeypatch.setattr(history_files, "_last_cleanup", 0.0)
    return segments


def test_retention_drops_the_cache_that_holds_the_rows_it_expired(log, monkeypatch):
    segments = _cached_then_rotated(log, monkeypatch)

    assert history_files._cleanup_old_archives(retention_days=7, base=log._dir) == len(segments)

    assert not any(seg.exists() for seg in segments)
    assert _holding(_store(log), "tamarind") == []


def test_a_cache_that_cannot_be_dropped_keeps_the_expired_segments(log, monkeypatch):
    segments = _cached_then_rotated(log, monkeypatch)
    monkeypatch.setattr(csc.ChatSearchCache, "drop", lambda self, names, trashed=False: False)

    assert history_files._cleanup_old_archives(retention_days=7, base=log._dir) == 0

    assert all(seg.exists() for seg in segments)


def _app(log, key: str = KEY) -> web.Application:
    """The search routes, with the slot named ``KEY`` showing transcript *key*."""
    slot = SimpleNamespace(
        key=key, _app="", linked_session_key=key, workspace=None, _history_key=key
    )
    app = web.Application()
    app["state"] = SimpleNamespace(conversation_log=log, _slots={KEY: slot}, _chat_pins=[])
    app.router.add_get(SEARCH, shs.api_chat_slot_history_search)
    app.router.add_get(COUNTS, shs.api_chat_slot_history_counts)
    return app


async def _get(client, route: str, query: str) -> dict:
    resp = await client.get(route.format(slot=KEY) + "?" + query)
    assert resp.status == 200
    return await resp.json()


@pytest.mark.asyncio
async def test_every_tab_shows_the_same_counts_for_the_whole_chat(log, monkeypatch):
    # The cache must answer: no wall-clock window decides it.
    monkeypatch.setattr(chc, "BUILD_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    for i in range(120):
        if i % 3 == 0:
            log.append(KEY, "user", f"plum picture {i}", extra_meta={"files": [f"/p/{i}.png"]})
        else:
            log.append(KEY, "assistant", f"plum link {i} https://example.com/{i}")
    _update(log)
    async with TestClient(TestServer(_app(log))) as client:
        replies = [
            await _get(client, SEARCH, f"q=plum&kind={kind}")
            for kind in ("", "images", "links", "files")
        ]

    assert all(r["complete"] for r in replies)
    assert all(r["counts"] == replies[0]["counts"] for r in replies)
    assert (replies[0]["counts"][""], replies[0]["counts"]["links"]) == (120, 80)
    assert len(replies[0]["hits"]) == 30
    assert replies[0]["next_before"] == replies[0]["hits"][-1]["index"]


@pytest.mark.asyncio
async def test_a_cursor_past_the_chat_is_its_end_in_the_cache(log, monkeypatch):
    monkeypatch.setattr(chc, "BUILD_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    _plums(log, 3)
    _update(log)

    def no_scan(*args, **kwargs):
        raise AssertionError("the cache must answer")

    monkeypatch.setattr(shs, "search_history", no_scan)
    async with TestClient(TestServer(_app(log))) as client:
        # Wider than SQLite's 64-bit integer.
        body = await _get(client, SEARCH, f"q=plum&before={2**63}")

    assert [h["index"] for h in body["hits"]] == [2, 1, 0]


@pytest.mark.asyncio
async def test_counts_come_from_the_cache_without_the_row_walk(log, monkeypatch):
    _plums(log, 50)
    # A walk this short would stop before the chat's first row.
    monkeypatch.setattr(shs, "COUNT_BUDGET_ROWS", 10)
    monkeypatch.setattr(chc, "BUILD_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    _update(log)
    async with TestClient(TestServer(_app(log))) as client:
        body = await _get(client, COUNTS, "q=plum")

    assert body["complete"] is True
    assert body["counts"][""] == 50
    assert body["total"] == 50


@pytest.mark.asyncio
async def test_a_first_search_never_waits_for_the_build_and_starts_exactly_one(log, monkeypatch):
    _plums(log, 40)
    release = threading.Event()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    builds = []
    real = csc.ChatSearchCache.update

    def blocked(self, *args, **kwargs):
        builds.append(args[1])
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(FALLBACK_TIMEOUT_SECS)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(csc.ChatSearchCache, "update", blocked)
    # No bound on the wait: a route that waited for this build would never answer.
    monkeypatch.setattr(chc, "BUILD_WAIT_SECS", None)
    try:
        async with TestClient(TestServer(_app(log))) as client:
            first = await asyncio.wait_for(
                _get(client, SEARCH, "q=plum&limit=5"), FALLBACK_TIMEOUT_SECS
            )
            counts = await asyncio.wait_for(_get(client, COUNTS, "q=plum"), FALLBACK_TIMEOUT_SECS)
            build = chc._updates[f"{_store(log).dir}\0{KEY}"]
            # The build thread may not have reached the patched update yet.
            await asyncio.wait_for(entered.wait(), FALLBACK_TIMEOUT_SECS)
            started = list(builds)
            release.set()
            await asyncio.wait_for(asyncio.shield(build), FALLBACK_TIMEOUT_SECS)
            after = await _get(client, SEARCH, "q=plum&limit=5")
    finally:
        release.set()

    assert started == [KEY]
    assert first["indexing"] is True and "complete" not in first
    assert [h["index"] for h in first["hits"]] == [39, 38, 37, 36, 35]
    assert first["counts"][""] == 5
    assert counts["indexing"] is True and counts["counts"][""] == 40
    assert after["complete"] is True and "indexing" not in after
    assert after["counts"][""] == 40


async def _built(client, log, query: str = "q=plum") -> dict:
    """The first search (from the scan), then the build it started, finished."""
    first = await _get(client, SEARCH, query)
    build = chc._updates.get(f"{_store(log).dir}\0{KEY}")
    if build is not None:
        await asyncio.wait_for(asyncio.shield(build), FALLBACK_TIMEOUT_SECS)
        assert build.exception() is None
    return first


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_row_still_builds_and_is_found(log, monkeypatch):
    log.append(KEY, "user", "placeholder")
    _rewrite(
        log,
        [
            {"role": "user", "content": "a plum \ud83d pie", "ts": "2026-11-01T10:00:00+00:00"},
            {
                "role": "assistant",
                "content": "plum reply \udc00 here",
                "ts": "2026-11-01T10:00:01+00:00",
                "meta": {"mid": "m\ud83d"},
            },
        ],
    )
    monkeypatch.setattr(chc, "BUILD_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    async with TestClient(TestServer(_app(log))) as client:
        first = await _built(client, log)
        after = await _get(client, SEARCH, "q=plum")
        counts = await _get(client, COUNTS, "q=plum")

    assert [h["index"] for h in first["hits"]] == [1, 0]
    assert after["complete"] is True
    assert [h["index"] for h in after["hits"]] == [1, 0]
    assert counts["complete"] is True and counts["counts"][""] == 2


@pytest.mark.asyncio
async def test_a_cache_that_cannot_answer_falls_back_to_the_scan(log, monkeypatch):
    _plums(log, 3)
    _update(log)

    def broken(*args, **kwargs):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(chc, "search_cached", broken)
    async with TestClient(TestServer(_app(log))) as client:
        body = await _get(client, SEARCH, "q=plum")
        counts = await _get(client, COUNTS, "q=plum")

    assert [h["index"] for h in body["hits"]] == [2, 1, 0]
    assert counts["counts"][""] == 3


def test_deleting_a_tab_predecessor_drops_the_caches_that_read_it(log):
    first, later = "dashboard:chat-1-100", "dashboard:chat-2-200"
    log.append(first, "user", "the quincefruit note", tab_id="tabchain0001")
    log.append(later, "user", "a plum note", tab_id="tabchain0001")
    snap = _update(log, later)
    assert _counts(log, snap, "quincefruit")[""] == 1

    assert log.delete_session(first)

    cache_dir = _store(log).dir
    leftover = [p.name for p in cache_dir.iterdir() if b"quincefruit" in p.read_bytes()]
    assert leftover == []
    assert _counts(log, _update(log, later), "quincefruit")[""] == 0


FIRST, LATER = "dashboard:chat-1-100", "dashboard:chat-2-200"


def _tab_pair(log) -> None:
    """Two transcripts of one tab: ``LATER``'s chain reads ``FIRST``'s row first."""
    log.append(FIRST, "user", "the quincefruit note", tab_id="tabchain0001")
    log.append(LATER, "user", "a plum note", tab_id="tabchain0001")


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run() -> None:
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def _holding(store, text: str) -> list[str]:
    return [p.name for p in store.dir.iterdir() if text.encode() in p.read_bytes()]


def _bump(store, key: str) -> None:
    """What :meth:`ChatSearchCache.drop` does before its sweep: bump *key*'s gen."""
    stem = csc._safe_key(key)
    with store._lock:
        store._gen[stem] = store._gen.get(stem, 0) + 1


def test_a_drop_between_a_builds_read_and_its_commit_voids_the_build(log, monkeypatch):
    _tab_pair(log)
    store = _store(log)
    monkeypatch.setattr(csc, "BUILD_PAGE_ROWS", 1)
    inside, release = threading.Event(), threading.Event()
    real_insert = csc._insert

    def held(conn, entries):
        real_insert(conn, entries)
        if any("quincefruit" in (entry[3] or "") for entry in entries):
            inside.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr(csc, "_insert", held)
    decoded = _decoded(log, monkeypatch)
    thread, out = _in_thread(lambda: _update(log, LATER))
    assert inside.wait(FALLBACK_TIMEOUT_SECS)
    # The drop's gen bump lands while the page holding FIRST's row is not committed.
    _bump(store, FIRST)
    release.set()
    thread.join(FALLBACK_TIMEOUT_SECS)

    assert isinstance(out.get("error"), csc.CacheDropped)
    # Stopped by the check after that page's commit, before the next page.
    assert decoded == [1]
    assert not store.path(LATER).exists()
    assert _holding(store, "quincefruit") == []
    monkeypatch.setattr(csc, "_insert", real_insert)
    assert log.delete_session(FIRST)
    assert _counts(log, _update(log, LATER), "quincefruit")[""] == 0


def test_a_drop_after_a_build_committed_a_page_sweeps_that_file(log, monkeypatch):
    _tab_pair(log)
    store = _store(log)
    monkeypatch.setattr(csc, "BUILD_PAGE_ROWS", 1)
    pages, inside, release = [0], threading.Event(), threading.Event()

    def pause() -> None:
        pages[0] += 1
        if pages[0] == 2:  # FIRST's row is committed; the next page is not read
            inside.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)

    thread, out = _in_thread(lambda: store.update(log, LATER, chc.cache_entry, pause))
    assert inside.wait(FALLBACK_TIMEOUT_SECS)
    assert _holding(store, "quincefruit")
    try:
        assert csc.drop(log._dir, [FIRST])
        assert not store.path(LATER).exists()
        assert _holding(store, "quincefruit") == []
    finally:
        release.set()
        thread.join(FALLBACK_TIMEOUT_SECS)

    assert isinstance(out.get("error"), csc.CacheDropped)
    assert log.delete_session(FIRST)
    assert _counts(log, _update(log, LATER), "quincefruit")[""] == 0


def test_a_drop_bumps_the_gens_before_it_sweeps_the_files(log, monkeypatch):
    _tab_pair(log)
    store = _store(log)
    entered, release = threading.Event(), threading.Event()
    real_in_flight = log.delete_in_flight

    def in_flight(key):
        # The build has copied its chain's gens and opened no file yet.
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)
        return real_in_flight(key)

    monkeypatch.setattr(log, "delete_in_flight", in_flight)
    thread, out = _in_thread(lambda: _update(log, LATER))
    assert entered.wait(FALLBACK_TIMEOUT_SECS)
    real_holders = store._holders

    def sweep_then_build(stems):
        # The sweep finds no file of LATER; the whole build runs after it.
        found = real_holders(stems)
        release.set()
        thread.join(FALLBACK_TIMEOUT_SECS)
        return found

    monkeypatch.setattr(store, "_holders", sweep_then_build)
    try:
        assert store.drop([FIRST])
    finally:
        release.set()
        thread.join(FALLBACK_TIMEOUT_SECS)

    assert isinstance(out.get("error"), csc.CacheDropped)
    assert not store.path(LATER).exists()
    assert _holding(store, "quincefruit") == []


def _cherry_first(log) -> None:
    """Rewrite row 0, so the next update rebuilds the cache from an empty table."""
    rows = _rows(log)
    rows[0] = {**rows[0], "content": "a cherry instead"}
    _rewrite(log, rows)


def test_a_search_inside_the_file_when_a_rebuild_starts_reads_the_old_table(log, monkeypatch):
    _plums(log, 20)
    old = _update(log)
    store = _store(log)
    _cherry_first(log)
    inside, release, rebuilding = threading.Event(), threading.Event(), threading.Event()
    real_reading, real_use = store.reading, store._use

    @contextlib.contextmanager
    def held(snap):
        with real_reading(snap) as conn:
            inside.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)
            yield conn

    monkeypatch.setattr(store, "reading", held)
    search, found = _in_thread(lambda: _cached(log, old, "plum"))
    assert inside.wait(FALLBACK_TIMEOUT_SECS)

    def use(key, gen):
        if threading.current_thread() is not search:
            rebuilding.set()
        return real_use(key, gen)

    monkeypatch.setattr(store, "_use", use)
    build, built = _in_thread(lambda: _update(log))
    try:
        # The rebuild has started; it needs the file the search holds.
        assert rebuilding.wait(FALLBACK_TIMEOUT_SECS)
    finally:
        release.set()
        search.join(FALLBACK_TIMEOUT_SECS)
        build.join(FALLBACK_TIMEOUT_SECS)

    assert found["result"]["complete"] is True
    assert (found["result"]["counts"][""], found["result"]["total"]) == (20, 20)
    monkeypatch.setattr(store, "reading", real_reading)
    assert _counts(log, built["result"], "plum")[""] == 19


@pytest.mark.asyncio
async def test_a_search_during_a_rebuild_never_answers_partial_counts(log, monkeypatch):
    _plums(log, 20)
    monkeypatch.setattr(csc, "BUILD_PAGE_ROWS", 5)
    old = _update(log)
    store = _store(log)
    _cherry_first(log)
    paused, release = threading.Event(), threading.Event()

    def pause() -> None:
        # Called after the reset commit, before the first page is read.
        paused.set()
        assert release.wait(FALLBACK_TIMEOUT_SECS)

    build, built = _in_thread(lambda: store.update(log, KEY, chc.cache_entry, pause))
    try:
        assert paused.wait(FALLBACK_TIMEOUT_SECS)
        with pytest.raises(csc.CacheDropped):
            _cached(log, old, "plum")
        # The route took its snapshot before the reset and reads it after.
        monkeypatch.setattr(store, "holds", lambda _key: True)
        monkeypatch.setattr(store, "current", lambda _log, _key: old)
        monkeypatch.setattr(chc, "start_build", lambda _log, _key: False)
        async with TestClient(TestServer(_app(log))) as client:
            body = await _get(client, SEARCH, "q=plum")
            counts = await _get(client, COUNTS, "q=plum")
    finally:
        release.set()
        build.join(FALLBACK_TIMEOUT_SECS)

    assert "complete" not in body
    assert body["counts"][""] == 19 and counts["counts"][""] == 19
    assert _counts(log, built["result"], "plum")[""] == 19


def test_a_drop_leaves_the_other_caches_unwritten(log, monkeypatch):
    others = ["dashboard:chat-7", "dashboard:chat-8"]
    for key in others:
        log.append(key, "user", "plum")
        _update(log, key)
    store = _store(log)
    store.close_all()
    old = 1_600_000_000
    for i, key in enumerate(others):
        os.utime(store.path(key), (old + i, old + i))
    opened = []
    real_open = csc._Handle.open

    def counted(self):
        opened.append(self.path.name)
        return real_open(self)

    monkeypatch.setattr(csc._Handle, "open", counted)

    assert csc.drop(log._dir, ["dashboard:unrelated"])

    assert opened == []
    assert [store.path(key).stat().st_mtime for key in others] == [old, old + 1]


def test_reopening_a_cache_does_not_write_it(log):
    log.append(KEY, "user", "plum")
    _update(log)
    store = _store(log)
    store.close_all()
    old = 1_600_000_000
    os.utime(store.path(KEY), (old, old))

    with store._use(KEY, 0) as conn:
        assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 1
    store.close_all()

    assert store.path(KEY).stat().st_mtime == old


def test_a_variant_switched_far_back_is_seen_on_the_next_search(log):
    log.append(KEY, "user", "placeholder")
    rows = [
        {"role": "user", "content": "ask", "ts": "2026-11-01T10:00:00+00:00"},
        {
            "role": "assistant",
            "content": "the old pear answer",
            "ts": "2026-11-01T10:00:01+00:00",
            "variants": [{"content": "the old pear answer"}, {"content": "the new fig answer"}],
            "variant_idx": 0,
        },
        *(
            {"role": "user", "content": f"plum {i}", "ts": "2026-11-01T10:00:02+00:00"}
            for i in range(csc.VERIFY_ROWS + 50)
        ),
    ]
    _rewrite(log, rows)
    snap = _update(log)
    assert _counts(log, snap, "pear")[""] == 1

    rows[1] = {**rows[1], "content": "the new fig answer", "variant_idx": 1}
    _rewrite(log, rows)
    snap = _update(log)

    assert _counts(log, snap, "fig")[""] == 1
    assert _counts(log, snap, "pear")[""] == 0


def test_no_cache_is_built_while_the_chat_is_being_deleted(log):
    log.append(KEY, "user", "plum")

    with log.delete_in_flight_window(KEY), pytest.raises(csc.CacheDropped):
        _update(log)

    assert not _store(log).path(KEY).exists()


@pytest.mark.asyncio
async def test_builds_run_on_their_own_thread(log, monkeypatch):
    _plums(log, 3)
    threads = []
    real = csc.ChatSearchCache.update

    def recorded(self, *args, **kwargs):
        threads.append(threading.current_thread().name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(csc.ChatSearchCache, "update", recorded)
    async with TestClient(TestServer(_app(log))) as client:
        await _built(client, log)

    assert len(threads) == 1 and threads[0].startswith("chat-search-cache")


@pytest.mark.asyncio
async def test_an_up_to_date_chat_answers_while_another_chat_builds(log, monkeypatch):
    other = "dashboard:other"
    log.append(other, "user", "a pear note")
    _plums(log, 3)
    _update(log)
    release = threading.Event()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    builds = []
    real = csc.ChatSearchCache.update

    def blocked(self, *args, **kwargs):
        builds.append(args[1])
        if args[1] == other:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(FALLBACK_TIMEOUT_SECS)
        return real(self, *args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("the cache must answer")

    monkeypatch.setattr(csc.ChatSearchCache, "update", blocked)
    try:
        async with TestClient(TestServer(_app(log))) as client:
            assert chc.start_build(log, other)
            await asyncio.wait_for(entered.wait(), FALLBACK_TIMEOUT_SECS)
            monkeypatch.setattr(shs, "search_history", no_scan)
            body = await _get(client, SEARCH, "q=plum")
            counts = await _get(client, COUNTS, "q=plum")
    finally:
        release.set()

    # Chat KEY was answered from its cache, never queued behind the other build.
    assert builds == [other]
    assert body["complete"] is True and "indexing" not in body
    assert [h["index"] for h in body["hits"]] == [2, 1, 0]
    assert counts["complete"] is True and "indexing" not in counts
    assert counts["counts"][""] == 3


@pytest.mark.asyncio
async def test_a_chat_too_large_to_cache_starts_no_build_and_shows_no_indexing(log, monkeypatch):
    _plums(log, 20)
    # Smaller than any cache file: the first committed page passes it.
    monkeypatch.setattr(csc, "DISK_CAP_BYTES", 1024)
    builds = []
    real = csc.ChatSearchCache.update

    def recorded(self, *args, **kwargs):
        builds.append(args[1])
        return real(self, *args, **kwargs)

    monkeypatch.setattr(csc.ChatSearchCache, "update", recorded)
    async with TestClient(TestServer(_app(log))) as client:
        first = await _get(client, SEARCH, "q=plum")
        build = chc._updates.get(f"{_store(log).dir}\0{KEY}")
        if build is not None:
            with pytest.raises(csc.CacheTooLarge):
                await asyncio.wait_for(asyncio.shield(build), FALLBACK_TIMEOUT_SECS)
        # None: the build ended, and was forgotten, before the reply was read.
        assert _store(log).too_large(KEY)
        later = await _get(client, SEARCH, "q=plum")
        counts = await _get(client, COUNTS, "q=plum")

    # The first reply's build could still finish; none after it can.
    assert first["indexing"] is True
    assert "indexing" not in later and "indexing" not in counts
    assert builds == [KEY]
    assert f"{_store(log).dir}\0{KEY}" not in chc._updates
    assert [h["index"] for h in later["hits"]][:3] == [19, 18, 17]
    assert counts["counts"][""] == 20


def test_a_build_waits_between_pages_while_a_paged_scan_runs(monkeypatch):
    monkeypatch.setattr(chc, "SCAN_YIELD_SECS", FALLBACK_TIMEOUT_SECS)
    seen: list[int] = []
    build = threading.Thread(target=lambda: (chc._yield(), seen.append(chc._scans)))
    with chc.scanning():
        build.start()
        # The build parks on the scans it waits for, and reads nothing yet.
        wait_until(lambda: len(chc._scans_done._waiters) == 1)
        assert seen == []
    build.join(FALLBACK_TIMEOUT_SECS)

    assert seen == [0]
