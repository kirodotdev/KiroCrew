"""Dashboard chat: the unflushed tail a page owes — matching, ordering and the loop snapshot."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


class TestSlotDetailPaginationUnflushedTail:
    """TestSlotDetailPagination: matching the unflushed tail against the disk window."""

    @pytest.mark.asyncio
    async def test_transient_window_row_does_not_duplicate_the_tail(self, tmp_path, monkeypatch):
        """A transient row in the window must not re-append rows already on disk.

        A save drops transient roles, so the disk read holds only persisted rows
        while the window holds both. Sizing the un-flushed tail by subtracting the
        two lengths therefore counts each transient row as one missing message and
        reaches that many rows too far back — rows the disk read already returned.

        Here everything persisted is on disk, so the correct tail is empty. The
        window carries one ``done`` row, which ``_prepare_messages`` drops from the
        response, so a duplicated row is directly visible in the content sequence.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("dup")
        log = state.conversation_log
        for i in range(4):
            log.append("dashboard:dup", "user", f"msg {i}")
        slot.append("user", "msg 0")
        slot.append("user", "msg 1")
        slot.append("done", "")
        slot.append("user", "msg 2")
        slot.append("user", "msg 3")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/dup?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == [
            f"msg {i}" for i in range(4)
        ], "un-flushed tail sized by a length subtraction re-appended a persisted row"
        assert data["total"] == 4

    @pytest.mark.asyncio
    async def test_genuinely_unflushed_tail_is_still_appended_once(self, tmp_path, monkeypatch):
        """Control for the test above: a real un-flushed tail must still arrive.

        Only the first two window rows reached disk, so the last two are owed to the
        client. A transient row sits between them, so a fix that merely stopped
        appending would pass the duplication test and fail this one.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("owed")
        log = state.conversation_log
        for i in range(2):
            log.append("dashboard:owed", "user", f"msg {i}")
        slot.append("user", "msg 0")
        slot.append("user", "msg 1")
        slot.append("done", "")
        slot.append("user", "msg 2")
        slot.append("user", "msg 3")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/owed?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == [f"msg {i}" for i in range(4)], "un-flushed tail was dropped"

    @pytest.mark.asyncio
    async def test_foreign_disk_row_does_not_consume_the_unflushed_tail(
        self, tmp_path, monkeypatch
    ):
        """A disk row absent from the window must not consume an owed turn.

        This is the OVER-count direction. A writer other than this slot's own save
        appends to the same transcript, so the chained disk read grows without any
        window row having been flushed. Sizing the boundary as
        ``len(all_msgs) - _disk_older_count`` therefore counts that foreign row as
        one more persisted window row, walks one row too far, and drops the
        genuinely un-flushed turn from a response the client uses as a replacement.
        Under-count fails safe with an empty tail; over-count loses a message.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("overcount")
        for i in range(3):
            slot.append("user", f"msg {i}")
        slot.drain()
        # Persist through the real save path, so the disk rows carry the window's
        # own message ids rather than a shape only a test helper produces.
        _save_slot_to_history(state, slot, force=True)
        # A writer that does not mirror into the window appends to the transcript.
        state.conversation_log.append("dashboard:overcount", "assistant", "foreign row")
        # A new turn arrives in memory and has not reached disk.
        slot.append("user", "new turn")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/overcount?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "new turn" in contents, f"over-count dropped the owed turn; got {contents}"

    @pytest.mark.asyncio
    async def test_non_string_message_id_does_not_crash_the_bounded_read(
        self, tmp_path, monkeypatch
    ):
        """A caller-supplied non-string ``meta.mid`` must not turn the read into a 500.

        ``meta`` on an inbound message comes from the HTTP caller and is checked
        only for being a dict, so its values keep whatever type arrived. A truthy
        non-string id is preserved rather than replaced, reaches disk, and is then
        hashed by the boundary match, where a list raises ``TypeError``.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("listmid")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hi",
                    "slot": "listmid",
                    "meta": {"mid": ["not-a-string"]},
                },
            )
            assert resp.status == 200
            slot = state.get_or_create_slot("listmid")
            slot.drain()
            # The id must be on disk as well as in the window: the boundary match
            # hashes both sides.
            _save_slot_to_history(state, slot, force=True)
            assert slot.messages[-1]["meta"]["mid"] == [
                "not-a-string"
            ], "fixture no longer reproduces a non-string id"

            resp = await client.get("/api/chat/slots/listmid?limit=10")
            assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    async def test_foreign_row_does_not_consume_the_tail_of_an_id_less_session(
        self, tmp_path, monkeypatch
    ):
        """The same over-count, on a session whose disk rows carry no message ids.

        The id match cannot help here: a session persisted before ids existed has
        none on disk, so the boundary falls back to counting. Sizing it as
        ``len(all_msgs) - _disk_older_count`` measures the whole file rather than
        this slot's own persisted rows, so a row appended by any other writer is
        counted as one more flushed window row and the owed turn is dropped.

        ``_disk_window_len`` is how many window rows are on disk, which is the
        quantity the subtraction was approximating, and a foreign append does not
        move it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("legacy")
        log = state.conversation_log
        # ConversationLog.append writes role/content/ts only, so these rows reach
        # disk with no ids -- what a pre-id session looks like.
        for i in range(3):
            log.append("dashboard:legacy", "user", f"msg {i}")
        for i in range(3):
            slot.append("user", f"msg {i}")
        slot.drain()
        # Mirror what the restore path leaves behind for such a session.
        slot._disk_older_count = 0
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        # A writer that does not mirror into the window appends to the transcript.
        log.append("dashboard:legacy", "assistant", "foreign row")
        slot.append("user", "new turn")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/legacy?limit=10")
            assert resp.status == 200
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "new turn" in contents, f"over-count dropped the owed turn; got {contents}"
        assert contents.count("msg 2") == 1, f"boundary walked short and duplicated; got {contents}"

    def test_id_less_boundary_matches_a_row_redacted_on_load(self):
        """A restored row keeps its ``ts`` verbatim but not always its content.

        Restore redacts non-user content before it enters the window while the disk
        row stays raw, so matching on content alone ends the run at that row and
        re-appends everything from there — rows the disk read already returned.

        The pair is taken from the REAL redactor rather than written by hand: an
        invented pair passes for the wrong reason, because a stamp match would carry
        it even when the transform would not.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        raw = "AKIAIOSFODNN7EXAMPLE"
        loaded, _ = redact_exfiltration_urls(raw)
        loaded, _ = redact_credentials(loaded)
        assert loaded != raw, "fixture no longer exercises a real redaction"

        all_msgs = [
            {"role": "user", "content": "ask", "ts": "t1"},
            {"role": "assistant", "content": raw, "ts": "t2"},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "ask", "ts": "t1"},
                {"role": "assistant", "content": loaded, "ts": "t2"},
                {"role": "user", "content": "new turn", "ts": "t3"},
            ],
            _disk_older_count=0,
        )

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert [m["content"] for m in out] == [
            "ask",
            raw,
            "new turn",
        ], "a row whose content was redacted on load was treated as un-flushed"

    def test_shared_stamp_foreign_row_does_not_swallow_an_unflushed_row(self):
        """A foreign row sharing a ``ts`` must not be accepted as the window's own.

        On a coarse clock two writers flooring off the same previous row both emit
        ``previous + 1µs`` (``history_schema.monotonic_transcript_ts``), so a foreign disk row and an
        un-flushed window row of the SAME role can carry an identical stamp while
        holding different messages. Accepting a stamp match on its own then treats
        the foreign row as the window row, the boundary advances past the un-flushed
        one, and the bounded response omits it — from a payload the client uses to
        REPLACE its transcript, so the message is simply gone from view.

        Content is the discriminator: these two rows differ by more than the load
        redaction, so nothing may match them.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        collided = "2026-08-18T12:00:00.000001+00:00"
        all_msgs = [
            {"role": "user", "content": "q", "ts": "2026-08-18T12:00:00+00:00"},
            {"role": "assistant", "content": "another writer's row", "ts": collided},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "q", "ts": "2026-08-18T12:00:00+00:00"},
                {"role": "assistant", "content": "owed reply", "ts": collided},
            ],
            _disk_older_count=0,
        )
        assert all_msgs[1]["ts"] == slot.messages[1]["ts"], "fixture lost the collision"
        assert all_msgs[1]["role"] == slot.messages[1]["role"], "fixture lost same-role"

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert "owed reply" in [
            m["content"] for m in out
        ], f"a shared stamp swallowed the un-flushed row; got {[m['content'] for m in out]}"

    @pytest.mark.asyncio
    async def test_repeated_caller_supplied_id_does_not_hide_an_unflushed_row(
        self, tmp_path, monkeypatch
    ):
        """A caller may repeat ``meta.mid``, and a set membership test over-reaches.

        ``meta`` on an inbound message is caller-supplied and an id is minted only
        when one is *absent*, so a client can post the same id twice. The first row
        reaches disk; the second is still only in the window. Testing membership
        against a *set* of disk ids matches both window rows, so the boundary walks
        past the row that was never persisted and the bounded read omits it — the
        silent-loss direction. One disk row is enough to reproduce it; two disk rows
        sharing an id are not required.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("dupmid")
        dup = "m-deadbeefdeadbeef"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "first", "slot": "dupmid", "meta": {"mid": dup}},
            )
            assert resp.status == 200
            slot = state.get_or_create_slot("dupmid")
            slot.drain()
            # Only the first row reaches disk, so the id is on disk exactly once.
            _save_slot_to_history(state, slot, force=True)

            resp = await client.post(
                "/api/chat",
                json={"message": "second", "slot": "dupmid", "meta": {"mid": dup}},
            )
            assert resp.status == 200
            slot.drain()
            mids = [m["meta"].get("mid") for m in slot.messages if isinstance(m.get("meta"), dict)]
            assert mids.count(dup) == 2, f"fixture no longer reproduces a repeated id; got {mids}"

            resp = await client.get("/api/chat/slots/dupmid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert "second" in contents, f"a repeated id hid the un-flushed row; got {contents}"
        assert contents.count("first") == 1, f"a row was duplicated; got {contents}"

    def test_frozen_prefix_id_does_not_fund_a_window_match(self):
        """An id occurrence in the frozen prefix must not count as a flushed window row.

        ``all_msgs[:_disk_older_count]`` is the frozen prefix — on-disk rows OLDER
        than the window, so none of them is in ``slot.messages``. Counting ids over
        the whole disk read lets such an occurrence fund a consumption for a window
        row that was never flushed, and the boundary then walks past it. Only the
        on-disk window region, ``all_msgs[_disk_older_count:]``, may fund a match.

        Reachable because a caller-supplied ``mid`` is preserved rather than re-minted
        (``state_parts/slot_transcript.py`` ``append``), which is what lets a redelivered row carry
        an id whose original has since been paged into the prefix.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        dup = "m-deadbeefdeadbeef"
        all_msgs = [
            {"role": "user", "content": "paged out", "ts": "t0", "meta": {"mid": dup}},
            {"role": "user", "content": "on disk", "ts": "t1", "meta": {"mid": "m-window1"}},
        ]
        slot = SimpleNamespace(
            messages=[
                {"role": "user", "content": "on disk", "ts": "t1", "meta": {"mid": "m-window1"}},
                {"role": "user", "content": "owed turn", "ts": "t2", "meta": {"mid": dup}},
            ],
            _disk_older_count=1,
        )
        # Guard the fixture: with no frozen prefix this case cannot arise at all and
        # the test would pass against the defect.
        assert slot._disk_older_count > 0, "fixture establishes no frozen prefix"

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert [m["content"] for m in out] == [
            "paged out",
            "on disk",
            "owed turn",
        ], "a frozen-prefix id funded a match and hid the owed turn"

    @pytest.mark.asyncio
    async def test_mixed_id_and_id_less_disk_window_does_not_duplicate_an_injection(
        self, tmp_path, monkeypatch
    ):
        """A disk window holding BOTH id-carrying and id-less rows must not duplicate.

        The dual-write injectors stamp both copies of an injection with one id, so
        a NEW injection does not produce this state — but transcripts written
        before the append path accepted an id hold exactly it, as does any caller
        that passes no id: earlier saved rows WITH ids plus a durable row WITHOUT
        one. The id-less ``append_if_absent`` call below is that legacy writer
        shape, and it must keep working unmigrated.

        Selecting id matching because SOME row carries an id then applies it to a row
        that structurally cannot match, so the injection is treated as un-flushed and
        appended a second time. Id matching is only sound when EVERY row in the disk
        window carries one; a mixed window belongs on the ordered path, which matches
        the fields both writers do record.

        Driven through the real writers rather than a hand-built mixed window, so it
        cannot encode a row shape neither writer emits.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("mixed")
        key = "dashboard:mixed"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        # Real save: these disk rows carry the window's own ids.
        _save_slot_to_history(state, slot, force=True)

        # The injector shape: window copy (id minted) plus durable copy (no meta).
        slot.append("assistant", "injected result")
        slot.drain()
        state.conversation_log.append_if_absent(key, "assistant", "injected result")

        disk = state.conversation_log.read_messages_chained(key)
        with_id = [
            m
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        without_id = [
            m
            for m in disk
            if not (isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str))
        ]
        assert with_id and without_id, (
            f"fixture did not produce a MIXED disk window; "
            f"with_id={len(with_id)} without_id={len(without_id)}"
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/mixed?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("injected result") == 1
        ), f"the durable injection was appended twice; got {contents}"

    @pytest.mark.asyncio
    async def test_durable_injection_carrying_the_window_rows_id_matches_by_identity(
        self, tmp_path, monkeypatch
    ):
        """Both copies of an injection carry ONE id, so the identity walk matches.

        The dual-write shape: the window copy is appended through ``slot.append``,
        which mints ``meta.mid`` and returns the row; the durable copy passes that
        same id to ``append_if_absent``. The on-disk window region then holds ONLY
        id-carrying rows, so the bounded read reconciles by identity rather than a
        body heuristic, and the injection appears exactly once.

        Guarded on the all-id premise: if the durable copy ever stops carrying the
        id, the fixture degrades to the mixed shape and the guard names that,
        instead of the count assertion passing by way of the ordered fallback.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("dualid")
        key = "dashboard:dualid"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        # Real save: these disk rows carry the window's own ids.
        _save_slot_to_history(state, slot, force=True)

        # The injector shape: one id minted in the window, the SAME id persisted
        # on the durable copy.
        window_row = slot.append("assistant", "injected result")
        slot.drain()
        mid = window_row["meta"]["mid"]
        assert (
            state.conversation_log.append_if_absent(key, "assistant", "injected result", mid=mid)
            is True
        )

        disk = state.conversation_log.read_messages_chained(key)
        region_mids = [
            m["meta"].get("mid") if isinstance(m.get("meta"), dict) else None for m in disk
        ]
        assert all(
            isinstance(x, str) and x for x in region_mids
        ), f"the durable copy dropped the id — the region is mixed, not all-id: {region_mids}"
        assert mid in region_mids, "the durable copy's id is not the window row's id"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/dualid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("injected result") == 1
        ), f"the identity walk failed to match the durable copy; got {contents}"

    @pytest.mark.asyncio
    async def test_hydrated_slot_over_an_all_id_transcript_is_not_served_twice(
        self, tmp_path, monkeypatch
    ):
        """Hydration must keep the disk rows' ids, or an all-id region doubles.

        With durable injection copies id-carrying, a cron transcript's window
        region can be ALL-id — the condition that selects the identity walk.
        ``hydrate_slot_from_history`` re-appends those rows into a fresh slot;
        if it dropped their ``meta``, every hydrated row would be re-minted a
        fresh id, no window id would match the disk, and the walk would mark
        the WHOLE history owed — a bounded read then serves every row twice
        until the next flush rewrites the file.
        """
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        key = "dashboard:cronhyd"

        # An all-id transcript: what a session looks like once every writer
        # (slot save and id-carrying durable appends) stamps meta.mid.
        state.conversation_log.append(key, "user", "q1", mid="m-disk-0000000001")
        state.conversation_log.append(key, "assistant", "a1", mid="m-disk-0000000002")
        state.conversation_log.append(key, "assistant", "cron result", mid="m-disk-0000000003")

        slot = state.get_or_create_slot("cronhyd")
        hydrate_slot_from_history(slot, state.conversation_log.read_messages(key))
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/cronhyd?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        for body in ("q1", "a1", "cron result"):
            assert contents.count(body) == 1, f"a hydrated row was served twice; got {contents}"


class TestUnflushedTailOrderingAndSnapshot:
    """The owed tail must land in window order, from a snapshot taken on the loop."""

    @staticmethod
    def _row(role: str, body: str, mid: str, ts: str) -> dict:
        return {
            "role": role,
            "content": body,
            "cls": f"msg msg-{role[0]}",
            "ts": ts,
            "meta": {"mid": mid},
        }

    def test_owed_row_merges_at_its_window_position_not_after_the_disk_slice(self):
        """A persisted row LATER in window order must not be rendered before an owed one.

        ``_flush_segment`` pulls a ``stop_event`` out of the trailing chunk run and
        re-appends it AFTER the finalized assistant row (chat_runner.py:2686-2687).
        So a stop that reached disk during streaming sits, in window order, behind a
        reply that is still owed. Concatenating the owed rows after the whole disk
        slice therefore renders the pair inverted -- the stop card above the prose it
        terminated.

        Window order is authoritative and every row in this arm carries an id, so the
        position is derivable without the body matching the other arm needs.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")

        window = [q, reply, stop]
        all_msgs = [q, stop]  # the reply has not been flushed yet
        slot = SimpleNamespace(messages=window, _disk_older_count=0)

        disk_mids = {m["meta"]["mid"] for m in all_msgs}
        assert reply["meta"]["mid"] not in disk_mids, "fixture needs the reply un-persisted"
        assert stop["meta"]["mid"] in disk_mids, "fixture needs the stop already on disk"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the owed reply BEFORE the persisted stop, else there "
            "is no inversion to detect"
        )

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("stopped") == 1 and bodies.count("done") == 1
        ), f"a row was dropped or duplicated; got {bodies}"
        assert bodies.index("done") < bodies.index("stopped"), (
            f"the persisted stop was rendered before the owed reply it follows in the "
            f"window; got {bodies}"
        )

    def test_tail_with_no_inversion_still_appends_after_the_disk_slice(self):
        """Negative control: with no inversion the result must be the old concatenation.

        Fails for the intended reason if the merge reorders rows it should leave alone
        -- every owed row here already follows every persisted one in window order, so
        the answer must be exactly ``all_msgs + owed``.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")

        all_msgs = [q, stop]
        slot = SimpleNamespace(messages=[q, stop, reply], _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        assert out == [q, stop, reply], (
            f"the no-inversion case must be untouched concatenation; got "
            f"{[m['content'] for m in out]}"
        )

    @pytest.mark.asyncio
    async def test_tail_snapshot_is_captured_on_the_loop_not_inside_the_worker(
        self, tmp_path, monkeypatch
    ):
        """The window snapshot must be taken before the offload, not inside the thread.

        ``_flush_segment`` assigns ``slot.messages = head`` and only THEN appends the
        finalized assistant row, so between those two statements the window is a
        transient chunk-free head missing that row. It is a plain ``def`` that is never
        handed to ``to_thread``, so the event loop cannot interleave with it -- but the
        threaded tail scan can land exactly there and snapshot the hole, and
        ``_disk_older_count`` is untouched by that mutation so the bounded re-read
        cannot detect it.

        Simulated deterministically: the mutation is applied at the moment the worker
        is entered. A snapshot captured on the loop predates it and keeps the row; one
        taken inside the worker sees the hole and drops the row from the response.
        """
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("loopsnap")
        key = "dashboard:loopsnap"

        slot.append("user", "q1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        disk = state.conversation_log.read_messages_chained(key)
        disk_mids = {
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        }
        assert disk and len(disk_mids) == len(disk), (
            f"fixture needs every disk row to carry an id so the id arm runs; "
            f"disk={len(disk)} mids={len(disk_mids)}"
        )

        owed = {
            "role": "assistant",
            "content": "finalized-reply",
            "cls": "msg msg-a",
            "ts": "2026-08-19T10:00:03+00:00",
            "meta": {"mid": "m-finalizedneverpersisted"},
        }
        slot.messages.append(owed)
        assert owed["meta"]["mid"] not in disk_mids, "fixture row must be un-persisted"

        original = ch._append_unflushed_tail_from_offset
        seen: dict[str, object] = {}

        def worker_entered(slot_arg, all_msgs_arg, **kwargs):
            # The worker is now running. Reproduce the mid-finalization window: the
            # finalized assistant row is not in slot.messages yet.
            seen["snapshot_kwarg"] = "snapshot" in kwargs
            slot_arg.messages = [
                m for m in slot_arg.messages if m.get("content") != "finalized-reply"
            ]
            return original(slot_arg, all_msgs_arg, **kwargs)

        monkeypatch.setattr(ch, "_append_unflushed_tail_from_offset", worker_entered)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/loopsnap?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert (
            seen.get("snapshot_kwarg") is not None
        ), "the tail match never ran, so this test asserts nothing"
        assert seen["snapshot_kwarg"] is True, (
            "the caller did not pass a snapshot, so the window is still read inside "
            "the worker thread where a mid-finalization window is observable"
        )
        contents = [m["content"] for m in data["messages"]]
        assert "finalized-reply" in contents, (
            f"the worker snapshotted the transient chunk-free window and dropped the "
            f"finalized row; got {contents}"
        )

    @staticmethod
    def _idless(role: str, body: str, ts: str, cls: str | None = None) -> dict:
        """A row with NO ``meta.mid`` -- forces the id-less body-matching arm."""
        r = {"role": role, "content": body, "ts": ts}
        if cls is not None:
            r["cls"] = cls
        return r

    def test_live_streamed_row_before_a_persisted_row_is_not_dropped(self):
        """An owed transient row must survive the suffix boundary in the id-less arm.

        The matcher skipped a transient row WITHOUT advancing ``start``; a later
        persisted match then set ``start = i + 1``, moving the boundary PAST the skipped
        row, and ``window[start:]`` excluded it. So a live streamed chunk sitting before
        a persisted ``stop_event`` was dropped from the bounded response entirely.

        A disk read omits transient roles, so such a row can never be matched and is
        always owed -- which is precisely the rule the id-based arm already applies.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")

        all_msgs = [q, stop]  # a disk read holds no transient rows
        window = [q, chunk, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(chunk) < window.index(stop), (
            "fixture must place the transient BEFORE the persisted row, else the "
            "suffix boundary never moves past it"
        )

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            "partial an" in bodies
        ), f"the live streamed row was dropped by the suffix boundary; got {bodies}"
        assert bodies.index("partial an") < bodies.index("stopped"), (
            f"the owed transient must keep its window position ahead of the persisted "
            f"row; got {bodies}"
        )

    def test_trailing_transient_row_is_emitted_exactly_once(self):
        """Negative control for the merge: an owed transient must not be emitted twice.

        Rows left in ``pending`` sit at or after ``start`` and are therefore already
        carried by ``window[start:]``. Emitting them again as well would re-append a
        live row -- the duplication class this change exists to avoid. Fails for the
        intended reason if the merge and the suffix both claim the same row.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:03+00:00")

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"the trailing transient was emitted more than once; got {bodies}"
        assert bodies == ["run it", "stopped", "partial an"], (
            f"a transient already after the last match must stay where it was; got " f"{bodies}"
        )

    def test_unowed_and_answered_rows_stay_excluded_from_the_idless_tail(self):
        """Negative control: only genuinely owed transients may be merged.

        ``done``/``queued`` are ``_UNOWED_WINDOW_ROLES`` and an answered ``permission``
        has already been dispositioned, so neither is owed. Merging transients
        unconditionally would re-append them, which is the same defect in the opposite
        direction from the drop above. A still-pending permission IS owed, so it is
        included -- that arm of the assertion is what stops the fix from over-narrowing
        into a second drop.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-19T15:00:01+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({}),
        )

        for extra, body, owed in (
            (done, "unowed-marker", False),
            (answered, "answered-approval", False),
            (pending, "pending-approval", True),
        ):
            slot = SimpleNamespace(messages=[q, extra, stop], _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            if owed:
                assert body in bodies, (
                    f"{extra['role']} row {body!r} is owed and must be merged; got " f"{bodies}"
                )
            else:
                assert body not in bodies, (
                    f"{extra['role']} row {body!r} is NOT owed and must not be "
                    f"re-appended; got {bodies}"
                )

    def test_unmatched_row_does_not_strand_the_persisted_rows_behind_it(self):
        """An owed row must not make the id-less arm re-emit later persisted rows.

        The arm forward-scans the disk slice for each window row. When a row was NOT
        found the loop would ``break`` outright, which left ``start`` pointing AT that
        row -- so ``window[start:]`` carried it AND every later window row, including
        rows the disk slice already holds. Those came back a second time, and out of
        order, which is the duplication this function exists to remove.

        The reachable shape is a ``stop_event`` flushed before reply finalization:
        ``_flush_segment`` re-appends the stop AFTER the finalized assistant row, so the
        window reads ``[... unflushed reply, flushed stop]``. The reply misses, and on
        the old code the persisted stop was emitted twice.

        The sibling id-carrying arm already holds an unmatched row and keeps walking, so
        this mirrors that rule rather than inventing a second one.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T16:00:00+00:00")
        reply = self._idless("assistant", "the reply", "2026-08-19T16:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T16:00:02+00:00")

        # The reply is still owed; the stop already reached disk ahead of it.
        all_msgs = [q, stop]
        window = [q, reply, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the UNMATCHED row before the persisted one, else the "
            "loop never abandons the rows behind it"
        )
        assert not any(
            m["content"] == "the reply" for m in all_msgs
        ), "fixture must keep the reply off disk, else there is nothing unmatched"

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]

        assert bodies.count("stopped") == 1, (
            f"the persisted stop was re-emitted because an earlier unmatched row "
            f"abandoned the scan; got {bodies}"
        )
        assert bodies == [
            "run it",
            "the reply",
            "stopped",
        ], f"output must follow window order with each row exactly once; got {bodies}"

    def test_unmatched_row_is_still_emitted_and_not_dropped(self):
        """Negative control: holding the unmatched row must not silently drop it.

        The fix stops an unmatched row from stranding the rows behind it. The opposite
        failure is just as silent -- discarding that row instead of retaining it would
        also stop the duplicate, and would pass a test that only counted the persisted
        row. So assert the owed row itself survives, in both the with-a-later-match and
        no-later-match shapes.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "ask", "2026-08-19T16:10:00+00:00")
        owed = self._idless("assistant", "owed body", "2026-08-19T16:10:01+00:00")
        persisted = self._idless("assistant", "on disk", "2026-08-19T16:10:02+00:00")

        # (a) a later row DOES match, so the owed row is flushed at that disk position
        slot = SimpleNamespace(messages=[q, owed, persisted], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, persisted])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"the owed row must be retained exactly once, not dropped; got {bodies}"
        assert bodies.index("owed body") < bodies.index(
            "on disk"
        ), f"the retained row must keep its window position; got {bodies}"

        # (b) NO later row matches, so the owed row can only arrive via the tail
        slot = SimpleNamespace(messages=[q, owed], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"with no later match the owed row must still be emitted once; got {bodies}"

    def test_trailing_unowed_and_answered_rows_are_excluded_from_the_idless_tail(self):
        """An unowed row in the LAST window position must not reach the tail.

        ``start`` only advances on a match, and an unowed row takes the transient branch
        and ``continue``s -- skipping ``start = i + 1``. So when such a row TRAILS the last
        match, a raw ``window[start:]`` re-admits it and undoes the exclusion the loop's own
        guard performed. The sibling id-carrying arm has no such hole because it applies
        both exclusions at the top of its loop.

        Two symptoms share that cause. A trailing ``done`` reaches the bounded response and
        ``_prepare_messages`` drops it while rendering, so a page whose only row is that
        ``done`` renders EMPTY and replaces the transcript. A trailing answered
        ``permission`` is instead re-ordered after every persisted row.

        The sibling test above puts the extra row in the MIDDLE, where the later match
        advances ``start`` PAST it -- so the exclusion it observes comes from ``start``
        overshooting, not from any filter, and it cannot see this defect. The whole body
        list is asserted rather than mere membership, because membership alone cannot see a
        mis-ordering.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:00:01+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-20T04:00:02+00:00")
        queued = self._idless("queued", "queued-marker", "2026-08-20T04:00:02+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-20T04:00:02+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )

        for extra, body in (
            (done, "unowed-marker"),
            (queued, "queued-marker"),
            (answered, "answered-approval"),
        ):
            window = [q, stop, extra]
            assert window[-1] is extra, (
                "fixture must place the unowed row LAST, else a later match advances "
                "start past it and the assertion passes without a filter"
            )
            slot = SimpleNamespace(messages=window, _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            assert body not in bodies, (
                f"trailing {extra['role']} row {body!r} was re-admitted by the unfiltered "
                f"tail slice; got {bodies}"
            )
            assert bodies == ["run it", "stopped"], (
                f"the bounded page must hold exactly the persisted rows in order; got " f"{bodies}"
            )

    def test_trailing_owed_rows_survive_the_idless_tail_filter(self):
        """Negative control: the tail filter must not over-narrow into a second drop.

        ``_UNOWED_WINDOW_ROLES`` is ``{done, queued}`` -- ``chunk`` and ``streaming`` are
        subtracted out, and a still-PENDING ``permission`` has not been dispositioned. All
        three are genuinely owed: a disk read carries no transient row, so they can never be
        matched, and dropping them loses a live streamed answer or an approval the user has
        yet to answer. That is the opposite defect from the leak, and it is the one an
        over-eager filter actually produces.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:10:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:10:01+00:00")
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-20T04:10:02+00:00",
            cls=json.dumps({}),
        )
        chunk = self._idless("chunk", "partial an", "2026-08-20T04:10:02+00:00")

        slot = SimpleNamespace(messages=[q, stop, pending], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert bodies == ["run it", "stopped", "pending-approval"], (
            f"a still-pending permission is owed and must survive the tail filter; got " f"{bodies}"
        )

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"a trailing live chunk is owed and must be emitted exactly once; got {bodies}"
        assert bodies == [
            "run it",
            "stopped",
            "partial an",
        ], f"the owed chunk must keep its trailing window position; got {bodies}"
