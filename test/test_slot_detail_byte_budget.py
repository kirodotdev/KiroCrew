"""A slot-detail page is bounded by bytes as well as rows, and is revalidated and gzipped."""

from __future__ import annotations

import gzip
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers as slot_detail

NAME = "big"


def _state(tmp_path, monkeypatch, sizes: list[int]):
    """A slot whose row i holds ``sizes[i]`` characters of text (rows are `r<i>:` + padding)."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for i, size in enumerate(sizes):
        text = f"r{i}:" + "x" * size
        state.conversation_log.append(f"dashboard:{NAME}", "user", text)
        slot.append("user", text)
    slot.drain()
    return state


async def _walk(client, before: int | None = None) -> list[list[str]]:
    """Every page, newest first, followed by ``next_before`` the way the dashboard scrolls."""
    pages: list[list[str]] = []
    while True:
        query = "limit=500" + ("" if before is None else f"&before={before}")
        data = await (await client.get(f"/api/chat/slots/{NAME}?{query}")).json()
        pages.append([m["content"].split(":")[0] for m in data["messages"]])
        if not data["has_more"]:
            return pages
        before = data["next_before"]


class TestByteBudget:
    @pytest.mark.asyncio
    async def test_a_page_stops_at_the_byte_budget_and_points_at_the_first_row_sent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 5000)
        state = _state(tmp_path, monkeypatch, [2000] * 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get(f"/api/chat/slots/{NAME}?limit=500")).json()
        assert [m["content"].split(":")[0] for m in data["messages"]] == ["r28", "r29"]
        assert (data["total"], data["has_more"], data["next_before"]) == (30, True, 28)

    @pytest.mark.asyncio
    async def test_walking_next_before_reaches_every_row_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 5000)
        state = _state(tmp_path, monkeypatch, [2000] * 11)
        async with TestClient(TestServer(_make_app(state))) as client:
            pages = await _walk(client)
        seen = [row for page in reversed(pages) for row in page]
        assert seen == [f"r{i}" for i in range(11)]

    @pytest.mark.asyncio
    async def test_a_row_over_the_budget_is_sent_alone_never_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 1000)
        state = _state(tmp_path, monkeypatch, [3000] * 4)
        async with TestClient(TestServer(_make_app(state))) as client:
            pages = await _walk(client)
        assert pages == [["r3"], ["r2"], ["r1"], ["r0"]]

    @pytest.mark.asyncio
    async def test_an_oversize_row_behind_a_small_newest_row_arrives_on_the_next_page(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 2000)
        state = _state(tmp_path, monkeypatch, [100, 100, 6000, 100])
        async with TestClient(TestServer(_make_app(state))) as client:
            pages = await _walk(client)
        assert pages == [["r3"], ["r2"], ["r0", "r1"]]

    @pytest.mark.asyncio
    async def test_the_whole_history_read_is_not_trimmed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 5000)
        state = _state(tmp_path, monkeypatch, [2000] * 6)
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get(f"/api/chat/slots/{NAME}")).json()
        assert len(data["messages"]) == 6
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_a_page_under_the_budget_is_unchanged(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch, [50] * 12)
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get(f"/api/chat/slots/{NAME}?limit=10")).json()
        assert len(data["messages"]) == 10
        assert (data["has_more"], data["next_before"]) == (True, 2)

    @pytest.mark.asyncio
    async def test_a_window_from_the_start_of_the_transcript_reports_its_cut_prefix(
        self, tmp_path, monkeypatch
    ):
        """The initial-prompt reader asks for the OLDEST window (`before=5`), which is
        where the byte trim cuts. `next_before` must name the first row sent, so the
        reader can fetch the rows it did not get."""
        state = _state(tmp_path, monkeypatch, [20, 400_000, 400_000, 400_000, 400_000])
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get(f"/api/chat/slots/{NAME}?limit=50&before=5")).json()
            sent = [m["content"].split(":")[0] for m in data["messages"]]
            assert sent == ["r2", "r3", "r4"][-len(sent) :] and "r0" not in sent
            assert data["has_more"] is True
            assert data["next_before"] == 5 - len(sent)
            prefix = await (
                await client.get(f"/api/chat/slots/{NAME}?limit=50&before={data['next_before']}")
            ).json()
        assert [m["content"].split(":")[0] for m in prefix["messages"]][0] == "r0"

    def test_the_trim_loop_is_one_step_per_row_and_keeps_the_newest(self, monkeypatch):
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 100)
        frags: list[str | None] = ["a" * 60, None, "b" * 60, "c" * 500]
        assert slot_detail._oldest_over_byte_budget(frags) == 3
        assert slot_detail._oldest_over_byte_budget(["a" * 10, "b" * 10]) == 0
        # A trailing row that renders as nothing never leaves an empty page.
        assert slot_detail._oldest_over_byte_budget(["a" * 500, None]) == 0


class TestRevalidationAndGzip:
    @pytest.mark.asyncio
    async def test_an_unchanged_page_answers_304_with_no_body(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch, [50] * 5)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.get(f"/api/chat/slots/{NAME}?limit=100")
            etag = first.headers["ETag"]
            assert first.headers["Cache-Control"] == "private, no-cache"
            again = await client.get(
                f"/api/chat/slots/{NAME}?limit=100", headers={"If-None-Match": etag}
            )
            assert again.status == 304
            assert await again.read() == b""
            # A new row changes the tag, so a 304 can never hide it.
            state.conversation_log.append(f"dashboard:{NAME}", "user", "new")
            state._slots[NAME].append("user", "new")
            changed = await client.get(
                f"/api/chat/slots/{NAME}?limit=100", headers={"If-None-Match": etag}
            )
            assert changed.status == 200

    @pytest.mark.asyncio
    async def test_a_large_page_is_gzipped_on_the_wire(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch, [3000] * 20)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(
                f"/api/chat/slots/{NAME}?limit=100",
                headers={"Accept-Encoding": "gzip"},
                auto_decompress=False,
            )
            wire = await resp.read()
            plain = await client.get(f"/api/chat/slots/{NAME}?limit=100")
            raw = await plain.read()
        assert resp.headers["Content-Encoding"] == "gzip"
        assert len(wire) < len(raw) // 4
        assert json.loads(gzip.decompress(wire)) == json.loads(raw)

    @pytest.mark.asyncio
    async def test_a_cursor_read_is_bounded_by_bytes_and_its_echo_is_a_delta(
        self, tmp_path, monkeypatch
    ):
        """The dashboard's refreshes read with ``cursor=1``; they get the same byte bound."""
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 5000)
        state = _state(tmp_path, monkeypatch, [2000] * 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (await client.get(f"/api/chat/slots/{NAME}?limit=500&cursor=1")).json()
            cur = first["cursor"]
            echo = f"from={cur['from']}&since={cur['since']}&digest={cur['digest']}"
            nxt = await (await client.get(f"/api/chat/slots/{NAME}?limit=500&{echo}")).json()
        assert [m["content"].split(":")[0] for m in first["messages"]] == ["r28", "r29"]
        assert (first["next_before"], cur["from"], cur["since"]) == (28, 28, 29)
        assert nxt["delta"] == {"from": 28, "since": 29}
        assert [m["content"].split(":")[0] for m in nxt["messages"]] == ["r29"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_limit", [1, 3])
    async def test_an_echo_wider_than_the_held_cursor_serves_the_width_asked(
        self, tmp_path, monkeypatch, first_limit
    ):
        """A narrow (or byte-cut) held cursor never narrows a later, wider read."""
        monkeypatch.setattr(slot_detail, "SLOT_DETAIL_PAGE_BYTE_BUDGET", 50_000)
        state = _state(tmp_path, monkeypatch, [2000] * 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (
                await client.get(f"/api/chat/slots/{NAME}?limit={first_limit}&cursor=1")
            ).json()
            cur = first["cursor"]
            echo = f"from={cur['from']}&since={cur['since']}&digest={cur['digest']}"
            plain = await (await client.get(f"/api/chat/slots/{NAME}?limit=20")).json()
            nxt = await (await client.get(f"/api/chat/slots/{NAME}?limit=20&{echo}")).json()
        assert "delta" not in nxt
        assert nxt["messages"] == plain["messages"]
        assert nxt["next_before"] == plain["next_before"] == 10
