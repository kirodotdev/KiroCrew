"""The slot-detail incremental-read cursor (``slot_detail_delta``)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.slot_detail_delta import (
    DeltaCursor,
    DeltaQueryError,
    parse_delta_query,
    plan_page,
    span_digest,
)


def _state(tmp_path, monkeypatch, name: str, count: int):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    for i in range(count):
        state.conversation_log.append(f"dashboard:{name}", "user", f"msg {i}")
        slot.append("user", f"msg {i}")
    slot.drain()
    return state, slot


def _echo(cursor: dict) -> str:
    return f"from={cursor['from']}&since={cursor['since']}&digest={cursor['digest']}"


class TestCursorReads:
    @pytest.mark.asyncio
    async def test_cursor_read_serves_the_same_page_plus_a_cursor(self, tmp_path, monkeypatch):
        state, _ = _state(tmp_path, monkeypatch, "c", 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            plain = await (await client.get("/api/chat/slots/c?limit=10")).json()
            first = await (await client.get("/api/chat/slots/c?limit=10&cursor=1")).json()
        assert first["messages"] == plain["messages"]
        assert (first["total"], first["has_more"], first["next_before"]) == (30, True, 20)
        assert first["cursor"]["from"] == 20 and first["cursor"]["since"] == 29
        assert "delta" not in first
        assert "cursor" not in plain

    @pytest.mark.asyncio
    async def test_echoed_cursor_gets_only_the_new_rows(self, tmp_path, monkeypatch):
        state, slot = _state(tmp_path, monkeypatch, "d", 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (await client.get("/api/chat/slots/d?limit=10&cursor=1")).json()
            for i in range(30, 33):
                state.conversation_log.append("dashboard:d", "user", f"msg {i}")
                slot.append("user", f"msg {i}")
            slot.drain()
            resp = await client.get(f"/api/chat/slots/d?limit=10&{_echo(first['cursor'])}")
            nxt = await resp.json()
        assert resp.status == 200
        assert nxt["delta"] == {"from": 20, "since": 29}
        # The held last row (29) is always re-sent, then the three new ones.
        assert [m["content"] for m in nxt["messages"]] == ["msg 29", "msg 30", "msg 31", "msg 32"]
        # The window slid: the reader drops rows before the new start.
        assert (nxt["total"], nxt["next_before"], nxt["has_more"]) == (33, 23, True)
        assert nxt["cursor"]["from"] == 23 and nxt["cursor"]["since"] == 32

    @pytest.mark.asyncio
    async def test_a_row_edited_in_place_falls_back_to_the_full_page(self, tmp_path, monkeypatch):
        state, slot = _state(tmp_path, monkeypatch, "e", 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (await client.get("/api/chat/slots/e?limit=10&cursor=1")).json()
            # A row well inside the held span changes (an approval resolved, a
            # card patched): only the hash notices.
            target = slot.messages[25]
            slot.update_message(
                target["ts"], content="edited", mid=(target.get("meta") or {}).get("mid")
            )
            nxt = await (
                await client.get(f"/api/chat/slots/e?limit=10&{_echo(first['cursor'])}")
            ).json()
        # A full replacement page: no delta, and the edit is in it.
        assert "delta" not in nxt
        assert len(nxt["messages"]) == 10
        assert "edited" in [m["content"] for m in nxt["messages"]]

    @pytest.mark.asyncio
    async def test_a_forged_digest_is_a_full_page(self, tmp_path, monkeypatch):
        state, _ = _state(tmp_path, monkeypatch, "f", 30)
        async with TestClient(TestServer(_make_app(state))) as client:
            nxt = await (
                await client.get("/api/chat/slots/f?limit=10&from=20&since=29&digest=" + "0" * 32)
            ).json()
        assert "delta" not in nxt
        assert [m["content"] for m in nxt["messages"]] == [f"msg {i}" for i in range(20, 30)]

    @pytest.mark.asyncio
    async def test_a_cursor_without_limit_is_still_a_bounded_read(self, tmp_path, monkeypatch):
        state, _ = _state(tmp_path, monkeypatch, "g", 250)
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get("/api/chat/slots/g?cursor=1")).json()
        assert len(data["messages"]) == 200
        assert data["next_before"] == 50

    @pytest.mark.asyncio
    async def test_assistant_and_tool_rows_hash_the_same_on_every_poll(self, tmp_path, monkeypatch):
        """Rows a running turn writes must not churn the hash, or every poll is a full page."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t")
        slot.append("user", "go")
        slot.append("assistant", "## Plan\n\n**bold** with https://example.com/x")
        slot.append("tool", "✅ fs_read", "msg msg-tool", meta={"tool_call_id": "call-A"})
        slot.append("assistant", "done")
        slot.drain()
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (await client.get("/api/chat/slots/t?limit=10&cursor=1")).json()
            slot.append("assistant", "more")
            slot.drain()
            nxt = await (
                await client.get(f"/api/chat/slots/t?limit=10&{_echo(first['cursor'])}")
            ).json()
        assert nxt.get("delta") == {
            "from": first["cursor"]["from"],
            "since": first["cursor"]["since"],
        }
        assert [m["content"] for m in nxt["messages"]] == ["done", "more"]

    @pytest.mark.asyncio
    async def test_a_cursor_read_renders_about_one_page(self, tmp_path, monkeypatch):
        """Rows read only to reach the span are not rendered."""
        from kiro_crew.dashboard import wire_render

        state, slot = _state(tmp_path, monkeypatch, "r", 60)
        rendered: list[int] = []
        real = wire_render.prepare_message_rows

        def _spy(messages, *a, **kw):
            rendered.append(len(messages))
            return real(messages, *a, **kw)

        monkeypatch.setattr(wire_render, "prepare_message_rows", _spy)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await (await client.get("/api/chat/slots/r?limit=10&cursor=1")).json()
            for i in range(60, 62):
                state.conversation_log.append("dashboard:r", "user", f"msg {i}")
                slot.append("user", f"msg {i}")
            slot.drain()
            nxt = await (
                await client.get(f"/api/chat/slots/r?limit=10&{_echo(first['cursor'])}")
            ).json()
        assert nxt["delta"] is not None
        # The span starts at 50; the new window at 52: rows 50..61 are rendered,
        # not the 19 rows the read composed to reach the span.
        assert rendered == [10, 12]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "from=1&since=2",
            "from=x&since=2&digest=" + "0" * 32,
            "from=3&since=2&digest=" + "0" * 32,
            "from=1&since=2&digest=nothex",
            "cursor=1&before=5",
        ],
    )
    async def test_a_malformed_cursor_is_a_bad_request(self, tmp_path, monkeypatch, query):
        state, _ = _state(tmp_path, monkeypatch, "h", 5)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/h?limit=5&{query}")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_query_params"


class TestPlanPage:
    @staticmethod
    def _ids(fragments):
        return [None if f is None else f.encode() for f in fragments]

    def test_misaligned_fragments_serve_the_page_with_no_cursor(self):
        plan = plan_page(
            ["a", "b"], self._ids(["a", "b"]), page_start=0, total=3, window=10, cursor=None
        )
        assert plan.fragments == ["a", "b"] and plan.cursor is None and plan.delta is None

    def test_a_dropped_row_inside_the_span_issues_no_cursor(self):
        frags = ["a", None, "c"]
        plan = plan_page(frags, self._ids(frags), page_start=0, total=3, window=10, cursor=None)
        assert plan.cursor is None
        assert plan.fragments == ["a", "c"]

    def test_a_row_without_identity_inside_the_span_issues_no_cursor(self):
        plan = plan_page(
            ["a", "b", "c"], [b"a", None, b"c"], page_start=0, total=3, window=10, cursor=None
        )
        assert plan.cursor is None and plan.fragments == ["a", "b", "c"]

    def test_a_dropped_last_row_still_issues_a_cursor(self):
        frags = ["a", "b", None]
        plan = plan_page(frags, self._ids(frags), page_start=0, total=3, window=10, cursor=None)
        assert plan.cursor == {"from": 0, "since": 2, "digest": span_digest([b"a", b"b"], 0, 2)}

    def test_the_digest_is_bound_to_position(self):
        assert span_digest([b"a"], 0, 1) != span_digest([b"a"], 1, 2)

    def test_the_digest_frames_each_identity(self):
        assert span_digest([b"ab", b"c"], 0, 2) != span_digest([b"a", b"bc"], 0, 2)

    def test_an_empty_session_has_a_cursor_too(self):
        plan = plan_page([], [], page_start=0, total=0, window=10, cursor=None)
        assert plan.cursor is not None and plan.cursor["since"] == 0
        nxt = plan_page(
            ["a"],
            [b"a"],
            page_start=0,
            total=1,
            window=10,
            cursor=DeltaCursor(0, 0, plan.cursor["digest"]),
        )
        assert nxt.delta == {"from": 0, "since": 0} and nxt.fragments == ["a"]

    def test_a_cursor_behind_the_composed_page_is_a_full_page(self):
        cursor = DeltaCursor(0, 2, span_digest([b"a", b"b"], 0, 2))
        plan = plan_page(["b", "c"], [b"b", b"c"], page_start=1, total=3, window=2, cursor=cursor)
        assert plan.delta is None and plan.fragments == ["b", "c"]

    def test_parse_requires_all_three(self):
        assert parse_delta_query({}) == (None, False)
        assert parse_delta_query({"cursor": "1"}) == (None, True)
        with pytest.raises(DeltaQueryError):
            parse_delta_query({"from": "1", "digest": "0" * 32})


class TestRowIdentities:
    def test_identity_tracks_the_stored_row_not_the_object(self):
        from kiro_crew.dashboard.wire_render import prepare_message_rows

        rows = [{"role": "assistant", "content": "x", "cls": "msg msg-a"}]
        _, first = prepare_message_rows(rows, False, live_child="")
        _, again = prepare_message_rows([dict(rows[0])], False, live_child="")
        _, edited = prepare_message_rows([{**rows[0], "content": "y"}], False, live_child="")
        assert first == again and first[0] is not None
        assert edited != first

    def test_oauth_and_chunk_rows_have_no_identity(self):
        from kiro_crew.dashboard.wire_render import prepare_message_rows

        rows = [
            {"role": "mcp_oauth", "content": "sign in", "cls": "msg", "meta": {}},
            {"role": "chunk", "content": "partial", "cls": "msg"},
        ]
        _, ids = prepare_message_rows(rows, True, live_child="")
        assert ids == [None, None]

    def test_owner_view_rows_have_no_identity(self, monkeypatch):
        from kiro_crew.dashboard import wire_render

        monkeypatch.setattr(wire_render, "credential_pass_bypassed", lambda: True)
        _, ids = wire_render.prepare_message_rows(
            [{"role": "assistant", "content": "x", "cls": "msg msg-a"}], False, live_child=""
        )
        assert ids == [None]

    @pytest.mark.parametrize("use_memo", [True, False])
    def test_an_edit_mid_render_cannot_pair_a_fragment_with_another_rows_identity(
        self, monkeypatch, use_memo
    ):
        """The event loop edits rows in place while the worker thread renders them."""
        from kiro_crew.dashboard import wire_render

        wire_render.clear_render_memo()
        live = {"role": "assistant", "content": "before", "cls": "msg msg-a"}
        real = wire_render._render_row

        def _render_then_edit(m, running, live_child):
            out = real(m, running, live_child)
            live["content"] = "after"
            return out

        monkeypatch.setattr(wire_render, "_render_row", _render_then_edit)
        fragments, ids = wire_render.prepare_message_rows(
            [live], False, live_child="", use_memo=use_memo
        )
        monkeypatch.setattr(wire_render, "_render_row", real)
        _, before_ids = wire_render.prepare_message_rows(
            [{"role": "assistant", "content": "before", "cls": "msg msg-a"}],
            False,
            live_child="",
            use_memo=use_memo,
        )
        assert "before" in fragments[0]
        assert ids == before_ids
