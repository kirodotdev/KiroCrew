"""Dashboard chat: slot detail pagination over HTTP — request validation and the bounded reader."""

from __future__ import annotations

import logging
import sys
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

# ── Slot detail pagination (HTTP) ──


class TestSlotDetailPagination:
    @pytest.mark.asyncio
    async def test_default_returns_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        for i in range(10):
            slot.append("user", f"msg {i}")
        slot.drain()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test")
            data = await resp.json()
            assert data["total"] == 10
            assert len(data["messages"]) == 10
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_pagination_with_before(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        log = state.conversation_log
        for i in range(300):
            log.append("dashboard:test", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test?limit=200")
            data = await resp.json()
            assert data["has_more"] is True
            assert len(data["messages"]) == 200
            assert data["total"] == 300
            # The cursor for the next page, in the raw index space this slice was
            # taken in. The client cannot derive it from the response body, whose
            # rows have already been collapsed by _prepare_messages.
            assert data["next_before"] == 100

            resp = await client.get(f"/api/chat/slots/test?limit=200&before={data['next_before']}")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["has_more"] is False
            assert data["next_before"] == 0
            assert data["messages"][0]["content"] == "msg 0"

    @pytest.mark.asyncio
    async def test_empty_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/empty")
            data = await resp.json()
            assert data["total"] == 0
            assert data["messages"] == []
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nonexistent")
            assert resp.status == 404

    async def _slot_with_history(self, tmp_path, monkeypatch, name, count=10):
        """A slot with *count* messages on disk and in memory."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name)
        for i in range(count):
            state.conversation_log.append(f"dashboard:{name}", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()
        return state

    @pytest.mark.asyncio
    async def test_non_integer_limit_is_a_bad_request(self, tmp_path, monkeypatch):
        """A junk limit is the client's mistake, so it must not surface as a 500."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "badlimit")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/badlimit?limit=abc")
            assert resp.status == 400
            body = await resp.json()
            assert "limit" in body["error"]
            assert body["code"] == "invalid_query_params"

    @pytest.mark.asyncio
    async def test_non_integer_before_is_a_bad_request(self, tmp_path, monkeypatch):
        state = await self._slot_with_history(tmp_path, monkeypatch, "badbefore")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/badbefore?before=xyz")
            assert resp.status == 400
            body = await resp.json()
            assert "before" in body["error"]
            assert body["code"] == "invalid_query_params"

    @pytest.mark.asyncio
    async def test_limit_below_one_is_rejected_not_clamped_up(self, tmp_path, monkeypatch):
        """limit below 1 is rejected, not an empty page with has_more true forever."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "zerolimit")
        async with TestClient(TestServer(_make_app(state))) as client:
            for bad in ("0", "-1", "-5"):
                resp = await client.get(f"/api/chat/slots/zerolimit?limit={bad}")
                assert resp.status == 400, f"limit={bad} should be rejected"
                body = await resp.json()
                assert "messages" not in body
                assert body["code"] == "limit_out_of_range"

    @pytest.mark.asyncio
    async def test_before_zero_remains_valid(self, tmp_path, monkeypatch):
        """A real caller sends before=0 on first page, so it must not be rejected."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "beforezero")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/beforezero?limit=5&before=0")
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == []
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_bounded_request_still_returns_the_same_page(self, tmp_path, monkeypatch):
        """Regression guard: threading the disk read must not alter the result."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "bounded", count=30)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/bounded?limit=10")
            data = await resp.json()
            assert data["total"] == 30
            assert len(data["messages"]) == 10
            assert data["messages"][0]["content"] == "msg 20"
            assert data["messages"][-1]["content"] == "msg 29"
            assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_bounded_request_never_calls_full_chained_reader(self, tmp_path, monkeypatch):
        """The bounded branch reads only ranges, and every disk read leaves the loop.

        The thread assertion is the off-loop ratchet for this branch: it fails if
        any of the archive probe, the page composition, or the re-probe is inlined
        onto the event loop.
        """
        from kiro_crew.dashboard import chat_handlers

        state = await self._slot_with_history(tmp_path, monkeypatch, "bounded-reader", count=30)

        def full_read_forbidden(_key):
            raise AssertionError("bounded slot detail called the full chained reader")

        monkeypatch.setattr(state.conversation_log, "read_messages_chained", full_read_forbidden)
        seen: list[int] = []
        original_page = chat_handlers._bounded_slot_page
        original_probe = state.conversation_log.read_rotated_messages_chained

        def recording_page(*args, **kwargs):
            seen.append(threading.get_ident())
            return original_page(*args, **kwargs)

        def recording_probe(*args, **kwargs):
            seen.append(threading.get_ident())
            return original_probe(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_bounded_slot_page", recording_page)
        monkeypatch.setattr(
            state.conversation_log, "read_rotated_messages_chained", recording_probe
        )
        loop_thread = threading.get_ident()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/bounded-reader?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert [message["content"] for message in data["messages"]] == [
            f"msg {i}" for i in range(20, 30)
        ]
        assert len(seen) >= 3, "expected the archive probe, the page composition, and the re-probe"
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_overdeep_json_row_fails_closed_on_bounded_page(self, tmp_path, monkeypatch):
        """A row nested past the JSON parser's depth is a 503, never a 500."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "deep-json", count=30)
        path = state.conversation_log._path("dashboard:deep-json")
        with open(path, "ab") as handle:
            handle.write(b"[" * 200_000 + b"\n")
        state.conversation_log._invalidate_cache("dashboard:deep-json")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/deep-json?limit=10")
            assert resp.status == 503, await resp.text()
            body = await resp.json()
            assert body["code"] == "history_corpus_unreadable"

    @pytest.mark.asyncio
    async def test_overlong_integer_row_fails_closed_on_bounded_page(self, tmp_path, monkeypatch):
        """A row ``json.loads`` refuses for a non-syntax reason is a 503, not a shorter transcript.

        An integer literal past ``sys.get_int_max_str_digits()`` raises a plain
        ``ValueError`` (not ``JSONDecodeError``). The full reader skips only
        ``JSONDecodeError`` and fails closed on this; the bounded reader must not
        skip the row instead, or every cursor above it shifts.
        """
        state = await self._slot_with_history(tmp_path, monkeypatch, "big-int", count=30)
        path = state.conversation_log._path("dashboard:big-int")
        digits = str(sys.get_int_max_str_digits() + 1)
        with open(path, "ab") as handle:
            handle.write(b'{"role": "user", "content": "x", "n": ' + b"9" * int(digits) + b"}\n")
        state.conversation_log._invalidate_cache("dashboard:big-int")
        with pytest.raises(ValueError):
            state.conversation_log.read_messages_chained("dashboard:big-int")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/big-int?limit=10")
            assert resp.status == 503, await resp.text()
            body = await resp.json()
            assert body["code"] == "history_corpus_unreadable"

    @pytest.mark.asyncio
    async def test_undecodable_transcript_bytes_fail_closed_on_bounded_page(
        self, tmp_path, monkeypatch
    ):
        """Invalid UTF-8 past the first page is a 503, never a shorter transcript."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "bad-utf8", count=30)
        path = state.conversation_log._path("dashboard:bad-utf8")
        corrupt = path.read_bytes().replace(b'"msg 25"', b'"msg 2\xff"', 1)
        assert b"\xff" in corrupt
        path.write_bytes(corrupt)
        state.conversation_log._invalidate_cache("dashboard:bad-utf8")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/bad-utf8?limit=10")
            assert resp.status == 503, await resp.text()
            body = await resp.json()
            assert body["code"] == "history_corpus_unreadable"

    @pytest.mark.asyncio
    async def test_splitlines_boundary_row_uses_full_reader_without_retrying(
        self, tmp_path, monkeypatch
    ):
        """A raw U+2028 row is deterministic; answer from the full reader, once."""
        from kiro_crew.dashboard import chat_handlers

        state = await self._slot_with_history(tmp_path, monkeypatch, "u2028-row", count=30)
        path = state.conversation_log._path("dashboard:u2028-row")
        with open(path, "ab") as handle:
            handle.write(b'{"role": "user", "content": "a\xe2\x80\xa8b"}\n')
        state.conversation_log._invalidate_cache("dashboard:u2028-row")
        attempts: list[int] = []
        original = chat_handlers._bounded_slot_page

        def counting(*args, **kwargs):
            attempts.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_bounded_slot_page", counting)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/u2028-row?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert len(attempts) == 1, "a splitlines-only boundary is deterministic; do not retry"
        full = state.conversation_log.read_messages_chained("dashboard:u2028-row")
        assert data["total"] == len(full)
        assert [m["content"] for m in data["messages"]] == [m["content"] for m in full[-10:]]

    @pytest.mark.asyncio
    async def test_oversized_row_uses_full_reader_without_retrying(self, tmp_path, monkeypatch):
        """A deterministic over-cap row must not be re-scanned per retry attempt."""
        from kiro_crew import history_projection
        from kiro_crew.jsonl_util import OversizedRecord

        state = await self._slot_with_history(tmp_path, monkeypatch, "big-row", count=30)
        attempts: list[int] = []

        def always_oversized(*_args, **_kwargs):
            attempts.append(1)
            raise OversizedRecord("record over cap")

        monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", always_oversized)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/big-row?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert len(attempts) == 1, "an oversized row is deterministic; do not retry the scan"
        assert data["total"] == 30
        assert [m["content"] for m in data["messages"]] == [f"msg {i}" for i in range(20, 30)]

    @pytest.mark.asyncio
    async def test_prefix_counter_mismatch_uses_full_reader_without_retry_or_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        """Raw/durable prefix counters disagreeing is deterministic for the snapshot."""
        from kiro_crew.dashboard import chat_handlers

        state = await self._slot_with_history(tmp_path, monkeypatch, "prefix-skew", count=30)
        slot = state.get_or_create_slot("prefix-skew")
        # Force the two counters apart the way a half-finished flush leaves them.
        slot._disk_older_durable_count = slot._disk_older_count + 1
        attempts: list[int] = []
        original = chat_handlers._bounded_slot_page

        def counting(*args, **kwargs):
            attempts.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_bounded_slot_page", counting)
        with caplog.at_level(logging.DEBUG, logger=chat_handlers.logger.name):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/prefix-skew?limit=10")
                assert resp.status == 200, await resp.text()
                data = await resp.json()

        assert len(attempts) == 1, "a counter mismatch is deterministic; do not retry"
        assert data["total"] == 30
        assert [m["content"] for m in data["messages"]] == [f"msg {i}" for i in range(20, 30)]
        bounded = [
            r
            for r in caplog.records
            if r.name == chat_handlers.logger.name and "bounded slot history" in r.getMessage()
        ]
        assert [r.levelno for r in bounded] == [logging.DEBUG], [
            (r.levelname, r.getMessage()) for r in bounded
        ]

    @pytest.mark.asyncio
    async def test_never_flushed_slot_pages_without_retry_or_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        """A fresh tab with no transcript file is a stable empty durable revision."""
        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("fresh-tab")
        slot.append("user", "hello")
        assert not state.conversation_log._path("dashboard:fresh-tab").exists()

        with caplog.at_level(logging.DEBUG, logger=chat_handlers.logger.name):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/fresh-tab?limit=10")
                assert resp.status == 200, await resp.text()
                data = await resp.json()

        assert [m["content"] for m in data["messages"]] == ["hello"]
        assert data["total"] == 1
        failed = [r for r in caplog.records if "bounded slot history read failed" in r.getMessage()]
        assert failed == [], [r.getMessage() for r in failed]

    @pytest.mark.asyncio
    async def test_memory_only_slot_pagination_serves_the_resident_window(
        self, tmp_path, monkeypatch
    ):
        """No conversation log (memory-only gateway): paginate from the resident window, never 500."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = None
        slot = state.get_or_create_slot("memonly")
        for i in range(30):
            slot.append("user", f"msg {i}")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/memonly?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["total"] == 30
            assert [m["content"] for m in data["messages"]] == [f"msg {i}" for i in range(20, 30)]
            assert data["has_more"] is True

            older = await client.get("/api/chat/slots/memonly?limit=10&before=10")
            assert older.status == 200, await older.text()
            body = await older.json()
            assert [m["content"] for m in body["messages"]] == [f"msg {i}" for i in range(0, 10)]
            assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_rotation_racing_bounded_page_falls_back_to_archive_reader(
        self, tmp_path, monkeypatch
    ):
        """An archive appearing mid-request must not be hidden by the bounded page."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "rotate-race", count=30)
        log = state.conversation_log
        archived = [
            {"role": "user", "content": f"old {i}", "ts": "2026-09-03T00:00:00+00:00"}
            for i in range(5)
        ]
        probes: list[int] = []

        def rotated_probe(_key):
            # Empty before the bounded read (no archive yet), populated after it:
            # the size rotation landed while the page was being composed.
            probes.append(len(probes))
            return [] if len(probes) == 1 else list(archived)

        full_reads: list[str] = []
        real_full = log.read_messages_chained_full

        def full_reader(key):
            full_reads.append(key)
            return list(archived) + real_full(key)

        monkeypatch.setattr(log, "read_rotated_messages_chained", rotated_probe)
        monkeypatch.setattr(log, "read_messages_chained_full", full_reader)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/rotate-race?limit=10&before=12")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert len(probes) >= 2, "archive state must be re-probed after composition"
        assert full_reads, "an archive that appeared mid-request must route to the full reader"
        assert data["total"] == 35
        assert [message["content"] for message in data["messages"]] == [
            f"old {i}" for i in range(2, 5)
        ] + [f"msg {i}" for i in range(7)]
        assert data["has_more"] is True
        assert data["next_before"] == 2

    @pytest.mark.asyncio
    async def test_raw_and_durable_prefix_mismatch_uses_full_reader(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("mixed-prefix")
        state.conversation_log.append("dashboard:mixed-prefix", "done", "")
        state.conversation_log.append("dashboard:mixed-prefix", "user", "msg 1")
        state.conversation_log.append("dashboard:mixed-prefix", "user", "msg 2")
        slot.append("user", "msg 2")
        slot.drain()
        slot._disk_older_count = 2
        slot._disk_older_durable_count = 1

        real = state.conversation_log.read_messages_chained_page
        bounded_calls = 0

        def counting_page(*args, **kwargs):
            nonlocal bounded_calls
            bounded_calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", counting_page)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/mixed-prefix?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert bounded_calls == 0
        assert data["total"] == 2
        assert [message["content"] for message in data["messages"]] == [
            "msg 1",
            "msg 2",
        ]

    @pytest.mark.asyncio
    async def test_bounded_composition_retries_on_chain_revision_change(
        self, tmp_path, monkeypatch
    ):
        state = await self._slot_with_history(tmp_path, monkeypatch, "revision-race", count=30)
        log = state.conversation_log
        real = log.read_messages_chained_page
        inserted = False

        def append_between_ranges(key, *, limit, before=None, expected_revision=None):
            nonlocal inserted
            page = real(
                key,
                limit=limit,
                before=before,
                expected_revision=expected_revision,
            )
            if not inserted:
                inserted = True
                log.append(key, "assistant", "foreign mid-read")
            return page

        monkeypatch.setattr(log, "read_messages_chained_page", append_between_ranges)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/revision-race?limit=50")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [message["content"] for message in data["messages"]]
        assert contents == [f"msg {i}" for i in range(30)] + ["foreign mid-read"]

    @pytest.mark.asyncio
    async def test_probe_losing_the_revision_race_is_retried_not_answered_by_a_full_read(
        self, tmp_path, monkeypatch
    ):
        """Rows landing while the total/revision probe reads send it round again.

        The probe's own two unpinned attempts both race a writer. It must hand that
        to the snapshot retry rather than re-read the whole transcript to answer.
        """
        state = await self._slot_with_history(tmp_path, monkeypatch, "probe-race", count=30)
        log = state.conversation_log
        projection = log._read_projection
        real_once = projection._read_messages_chained_page_once
        raced = 0

        def racing_once(key, *, limit, before):
            nonlocal raced
            result = real_once(key, limit=limit, before=before)
            if limit == 0 and raced < 2:
                raced += 1
                log.append(key, "assistant", f"foreign {raced}")
            return result

        corpus_reads: list[str] = []
        real_corpus = projection.read_messages_chained

        def counting_corpus(key):
            corpus_reads.append(key)
            return real_corpus(key)

        monkeypatch.setattr(projection, "_read_messages_chained_page_once", racing_once)
        monkeypatch.setattr(projection, "read_messages_chained", counting_corpus)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/probe-race?limit=50")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert raced == 2
        assert corpus_reads == [], "the probe must not read the whole transcript"
        contents = [message["content"] for message in data["messages"]]
        assert contents[-2:] == ["foreign 1", "foreign 2"]
        assert [c for c in contents if c.startswith("msg ")] == [f"msg {i}" for i in range(30)]

    @pytest.mark.asyncio
    async def test_cursor_branch_reads_bounded_page_off_the_loop_thread(
        self, tmp_path, monkeypatch
    ):
        """The bounded reader must run away from the loop serving other requests."""
        state = await self._slot_with_history(tmp_path, monkeypatch, "offloop")
        log = state.conversation_log
        real = log.read_messages_chained_page
        seen: list[int] = []

        def recording(key, *, limit, before=None, expected_revision=None):
            seen.append(threading.get_ident())
            return real(
                key,
                limit=limit,
                before=before,
                expected_revision=expected_revision,
            )

        monkeypatch.setattr(log, "read_messages_chained_page", recording)
        loop_thread = threading.get_ident()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/offloop?limit=5")
            assert resp.status == 200
        assert seen, "read_messages_chained_page was never called"
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_unlimited_branch_returns_whole_history_off_the_loop_thread(
        self, tmp_path, monkeypatch
    ):
        """The no-limit branch reassembles disk+memory and still claims has_more false."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("whole")
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:whole", "user", f"msg {i}")
        for i in range(6, 10):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._disk_older_count = 6

        real = log.read_messages_chained
        seen: list[int] = []

        def recording(key):
            seen.append(threading.get_ident())
            return real(key)

        monkeypatch.setattr(log, "read_messages_chained", recording)
        loop_thread = threading.get_ident()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/whole")
            assert resp.status == 200
            data = await resp.json()
        assert [m["content"] for m in data["messages"]] == [f"msg {i}" for i in range(10)]
        assert data["total"] == 10
        assert data["has_more"] is False
        assert seen, "read_messages_chained was never called"
        assert loop_thread not in seen

    @pytest.mark.asyncio
    async def test_message_arriving_during_the_disk_read_is_not_dropped(
        self, tmp_path, monkeypatch
    ):
        """The awaited read is a suspension point, so the tail is re-read after it.

        The client replaces its message list with this response, so a message that
        lands mid-read must not be silently absent. Appending from inside the
        patched read reproduces exactly that window deterministically.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("midread")
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:midread", "user", f"msg {i}")
        for i in range(6, 10):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._disk_older_count = 6

        real = log.read_messages_chained

        def appends_while_reading(key):
            result = real(key)
            slot.append("assistant", "arrived mid-read")
            slot.drain()
            return result

        monkeypatch.setattr(log, "read_messages_chained", appends_while_reading)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/midread")
            assert resp.status == 200
            data = await resp.json()
        contents = [m["content"] for m in data["messages"]]
        assert "arrived mid-read" in contents, "message that landed during the await was dropped"
        assert contents == [f"msg {i}" for i in range(10)] + ["arrived mid-read"]


# ── Full pagination walk (simulates infinite scroll) ──


class TestFullPaginationWalk:
    @pytest.mark.asyncio
    async def test_walk_all_pages(self, tmp_path, monkeypatch):
        """Simulate frontend infinite scroll — walk backwards through all messages."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("walk")
        log = state.conversation_log
        total_msgs = 450

        for i in range(total_msgs):
            log.append("dashboard:walk", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            all_collected: list[str] = []
            before = None
            pages = 0

            while True:
                url = "/api/chat/slots/walk?limit=100"
                if before is not None:
                    url += f"&before={before}"
                resp = await client.get(url)
                data = await resp.json()
                msgs = data["messages"]
                all_collected = [m["content"] for m in msgs] + all_collected
                pages += 1

                if not data["has_more"]:
                    break
                before = data["total"] - len(all_collected)

            assert len(all_collected) == total_msgs
            assert all_collected[0] == "msg 0"
            assert all_collected[-1] == f"msg {total_msgs - 1}"
            assert pages > 1

    @pytest.mark.asyncio
    async def test_walk_with_trimmed_memory(self, tmp_path, monkeypatch):
        """Pagination with before uses disk — can access all messages."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("trim")
        log = state.conversation_log

        for i in range(400):
            log.append("dashboard:trim", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/trim?limit=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][-1]["content"] == "msg 399"

            # Pagination: disk has all 400
            resp = await client.get("/api/chat/slots/trim?limit=200&before=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False
