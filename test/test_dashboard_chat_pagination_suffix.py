"""Dashboard chat: a bounded page keeps the persisted suffix and the live rows intact."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


class TestSlotDetailPaginationPersistedSuffix:
    """TestSlotDetailPagination: the persisted suffix, approvals and live rows across a bounded read."""

    @pytest.mark.asyncio
    async def test_interleaved_foreign_row_does_not_duplicate_the_persisted_suffix(
        self, tmp_path, monkeypatch
    ):
        """A foreign row BETWEEN two window rows must not end the ordered scan.

        The save is non-destructive against a cross-process append: it preserves
        rows another writer added and merges them back in TIME order
        (``_interleave_foreign_lines``, ``chat_persistence.py:1337-1357``, whose
        docstring notes such rows "genuinely happened BETWEEN the window's turns").
        So the on-disk window region can read ``[window, foreign, window]``.

        An unmatched row there means "not mine", not "end of window". Stopping at it
        leaves every persisted row after it in the tail, and those rows are appended
        a second time — duplication of an already-persisted suffix.

        Built through the real save path so the interleave is produced by the code
        that really orders these rows, and guarded so it cannot pass vacuously if no
        interleave occurs.

        The window timestamps are set EXPLICITLY, far apart, rather than left to the
        clock. The foreign row takes its real "now" and must sort strictly between
        them. With three clock-minted timestamps the test is platform-fragile: on a
        coarse-clock platform the foreign append and the second window row land in
        the same tick, and a shared ``ts`` carried by exactly one unmatched window
        entry and one unmatched disk line is the save's UNAMBIGUOUS in-place-edit
        case (``chat_persistence.py:1573-1587``), so the foreign row is dropped and
        the interleave never happens. Do not collapse these back to clock defaults.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("interleave")
        key = "dashboard:interleave"

        slot.append("user", "w1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # Another writer appends between saves; its ts is "now", between the two
        # window rows below, so the merge must place it in the middle.
        state.conversation_log.append(key, "assistant", "foreign row")

        slot.append("user", "w2", ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()
        # This save preserves the foreign row and merges it back in time order.
        _save_slot_to_history(state, slot, force=True)

        disk = [m.get("content") for m in state.conversation_log.read_messages_chained(key)]
        assert disk == [
            "w1",
            "foreign row",
            "w2",
        ], f"fixture did not interleave the foreign row mid-window; disk={disk}"

        # A genuinely un-flushed turn after the interleave.
        slot.append("user", "w3", ts="2030-01-02T00:00:00.000000+00:00")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/interleave?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            contents.count("w2") == 1
        ), f"the persisted suffix after the foreign row was duplicated; got {contents}"
        assert "w3" in contents, f"the un-flushed turn was dropped; got {contents}"

    @pytest.mark.asyncio
    async def test_save_redacted_row_does_not_duplicate_the_persisted_suffix(
        self, tmp_path, monkeypatch
    ):
        """A row the SAVE redacted must still match its raw window copy.

        The redaction is asymmetric in a session that was never restored. The save
        redacts every non-user role (``chat_persistence.py:1282-1284``) while the
        window keeps the text verbatim (``state_parts/slot_transcript.py`` ``append``), so the disk
        side is redacted and the window side is raw. Redacting only the disk side cannot
        converge on that pair, because redacting an already-redacted body just
        reproduces it. The row then reads as un-flushed, the walk stops, and the
        persisted suffix is appended a second time.

        Driven through the real writers, and the fixture asserts both preconditions
        first: the disk window region is MIXED, which is what selects the ordered
        path, and the two sides genuinely differ by the redaction transform.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            redact_credentials,
            redact_exfiltration_urls,
        )

        secret = "key AKIAIOSFODNN7EXAMPLE here"
        redacted, _ = redact_exfiltration_urls(secret)
        redacted, _ = redact_credentials(redacted)
        assert redacted != secret, "fixture needs content the real redactor changes"

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("redactdup")
        key = "dashboard:redactdup"

        # Explicit stamps, far apart, so the foreign row's real "now" sorts
        # strictly between them and can equal neither. On a coarse-clock platform
        # (Windows ticks in ~15.6 ms steps) clock-minted stamps put the foreign
        # append and the second window row in the same tick, and a ``ts`` carried
        # by exactly one unmatched window entry and one unmatched disk line is the
        # save's UNAMBIGUOUS in-place-edit case, so the second save DROPS the
        # foreign line and the region is not mixed. Do not collapse these
        # back to clock defaults; the sibling interleave test pins them for the
        # same reason.
        slot.append("user", "q1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # An id-less foreign row is what puts this region on the ordered path.
        state.conversation_log.append(key, "assistant", "foreign row")

        slot.append("assistant", secret, ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert any(has_id) and not all(has_id), (
            f"fixture did not produce a MIXED disk window, so the ordered path "
            f"would not run; has_id={has_id}"
        )
        assert any(m["content"] == redacted for m in disk), (
            f"fixture did not persist the redacted form; disk=" f"{[m['content'] for m in disk]}"
        )
        assert any(
            m.get("content") == secret for m in slot.messages
        ), "fixture did not keep the window copy raw"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/redactdup?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            sum(1 for c in contents if "REDACTED" in c) == 1
        ), f"the save-redacted row was appended twice; got {contents}"

    @pytest.mark.asyncio
    async def test_redaction_equal_foreign_row_does_not_consume_an_unflushed_row(
        self, tmp_path, monkeypatch
    ):
        """Two DIFFERENT secrets redact alike; they must not be treated as one row.

        The redaction-equivalent compare exists for one shape only: a row and its
        own persisted copy, which differ by the transform because one side was
        redacted on save or load. That pair always carries the SAME ``ts`` — the
        save copies it verbatim (``chat_persistence.py:1288``) and so does the load
        (``:714``). So requiring the stamps to match costs the legitimate case
        nothing, and it stops a foreign row whose credential merely redacts to the
        same text from being consumed as the window's own.

        It cannot disturb a durable injection either: that pair is byte-identical,
        so it returns at the plain-equality branch above and never reaches here.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            redact_credentials,
            redact_exfiltration_urls,
        )

        def red(text: str) -> str:
            out, _ = redact_exfiltration_urls(text)
            out, _ = redact_credentials(out)
            return out

        # Two different credential KINDS rather than two near-identical keys: an
        # access-key id and a labelled secret-key assignment redact to the same tag,
        # so no second key-shaped literal is needed to make the bodies collide.
        mine = "key AKIAIOSFODNN7EXAMPLE here"
        theirs = "key aws_secret_access_key=x here"
        assert mine != theirs, "fixture needs two DISTINCT bodies"
        assert (
            red(mine) == red(theirs) != mine
        ), f"fixture needs two bodies that redact alike; got {red(mine)!r} vs {red(theirs)!r}"

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("redconflate")
        key = "dashboard:redconflate"

        slot.append("user", "q1", ts="2020-01-01T00:00:00.000000+00:00")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # A foreign, id-less row carrying a DIFFERENT secret. Id-less is what puts
        # this region on the ordered path.
        state.conversation_log.append(key, "assistant", theirs)

        # Our own un-flushed row, never saved, with a stamp that cannot collide.
        slot.append("assistant", mine, ts="2030-01-01T00:00:00.000000+00:00")
        slot.drain()

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert any(has_id) and not all(has_id), (
            f"fixture did not produce a MIXED disk window, so the ordered path "
            f"would not run; has_id={has_id}"
        )
        assert not any(
            m.get("content") == mine for m in disk
        ), "fixture expects our row to be un-flushed"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/redconflate?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert sum(1 for c in contents if "REDACTED" in c) == 2, (
            f"a foreign row that merely redacts alike consumed the un-flushed row; "
            f"got {contents}"
        )

    @pytest.mark.asyncio
    async def test_bounded_read_runs_the_tail_match_off_the_event_loop(self, tmp_path, monkeypatch):
        """The tail match must not run on the event loop.

        It walks the whole window against the whole disk window region and applies
        the redaction transform to both sides of every candidate compare, so on a
        large mixed or id-less history the cost is real. Running it inline blocks
        every other request on the loop, and the loop-stall watchdog treats a long
        enough block as a hung gateway. The disk read immediately above it already
        goes through ``asyncio.to_thread``; this makes the pair consistent.
        """
        import threading

        import kiro_crew.dashboard.chat_handlers as ch

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("offloop")
        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        original = ch._append_unflushed_tail_from_offset

        def spy(*args, **kwargs):
            seen["thread"] = threading.get_ident()
            return original(*args, **kwargs)

        monkeypatch.setattr(ch, "_append_unflushed_tail_from_offset", spy)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/offloop?limit=10")
            assert resp.status == 200, await resp.text()

        assert "thread" in seen, (
            "the tail match never ran, so this test asserts nothing -- the request "
            "did not reach it"
        )
        assert seen["thread"] != loop_thread, (
            "the tail match ran on the event loop thread; it must be dispatched to a "
            "worker so a large history cannot stall the loop"
        )

    @pytest.mark.asyncio
    async def test_interleaved_unmatched_row_is_not_dropped_by_the_id_walk(
        self, tmp_path, monkeypatch
    ):
        """An un-flushed row BEFORE a persisted one must still reach the response.

        Recording a prefix boundary on the id walk would fail here: on a match it set
        ``start = i + 1``, and a miss simply did not advance. So an un-flushed row
        followed by a matching row had the boundary moved PAST it, and the bounded
        response omitted it entirely — a drop, not a duplicate.

        Identity does not need a prefix. An id present in the disk window region
        proves that row reached disk, so the owed set is the rows whose id did NOT,
        taken in window order. That is a strict generalisation: when the persisted
        rows really are a prefix it gives the same answer.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("interleaveid")
        key = "dashboard:interleaveid"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        disk_mids = {
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        }
        assert len(disk_mids) == len(disk) and disk, (
            f"fixture needs EVERY disk row to carry an id so the id walk runs; "
            f"disk={len(disk)} mids={len(disk_mids)}"
        )

        # An un-flushed assistant row positioned BEFORE the already-persisted one.
        owed = {
            "role": "assistant",
            "content": "owed-turn",
            "cls": "msg msg-a",
            "ts": "2026-08-18T21:00:00+00:00",
            "meta": {"mid": "m-owedneverpersist"},
        }
        slot.messages.insert(1, owed)
        assert owed["meta"]["mid"] not in disk_mids, "fixture row must be un-persisted"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/interleaveid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert (
            "owed-turn" in contents
        ), f"the un-flushed row before a persisted row was dropped; got {contents}"
        assert contents.count("a1") == 1, f"the persisted row was duplicated; got {contents}"

    @pytest.mark.asyncio
    async def test_transient_window_row_does_not_truncate_the_id_prefix(
        self, tmp_path, monkeypatch
    ):
        """A transient row mid-window must not push persisted rows into the tail.

        Negative control for the test above. Transient roles are dropped by the
        save, and ``queued`` is outside ``_WIRE_ONLY_ROLES`` so it still gets an id
        minted — an id that can never appear on disk. Ending the walk at the first
        miss would therefore stop at this row and re-append every persisted row
        after it, which is the duplication this change exists to prevent.

        Driven through the real writers.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("transid")
        key = "dashboard:transid"

        slot.append("user", "q1")
        slot.append("queued", "queued-notice")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        assert [m.get("content") for m in disk] == ["q1", "a1"], (
            f"fixture expects the save to drop the transient row; disk="
            f"{[m.get('content') for m in disk]}"
        )
        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert queued and isinstance(
            queued[0].get("meta"), dict
        ), "fixture expects the queued row to carry a minted id"
        assert isinstance(
            queued[0]["meta"].get("mid"), str
        ), "fixture expects a string mid on the queued row"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/transid?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        contents = [m["content"] for m in data["messages"]]
        assert contents == ["q1", "a1"], (
            f"a transient row truncated the id prefix and duplicated a persisted "
            f"row; got {contents}"
        )

    @pytest.mark.asyncio
    async def test_pending_approval_survives_a_bounded_read(self, tmp_path, monkeypatch):
        """A live ``permission`` row must reach the bounded response.

        ``_TRANSIENT_ROLES`` documents itself as being about a *window-region disk
        line* (``chat_persistence.py:1320-1322``), and that is how
        ``chat_persistence.py:1571`` uses it. The owed-set loop applies it to
        in-memory ``slot.messages`` instead, which answers a different question:
        which rows does the client still need? A pending approval is actionable, and
        the client reads it straight out of the transcript
        (``store/chat/selectors.ts`` ``selectSlotPendingApproval``), so dropping it makes the
        approval bar vanish while the server is still waiting on an answer.

        Skipping it also bought nothing: ``permission`` is never persisted
        (``chat_persistence.py:1274`` returns ``None`` for it), so it can never have
        a disk counterpart for the id dedup to match.

        Single delivery is safe because the client REPLACES its transcript from this
        payload (``store/chat/slotSwitch.ts`` ``state.messages = next``) rather than appending,
        so returning the row once cannot double it.
        """
        import json as _json

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("perm")
        key = "dashboard:perm"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        # Same call shape the runner uses: the approval metadata rides in ``cls``.
        slot.append("permission", "Run a shell command?", _json.dumps({"request_id": "req-1"}))
        slot.drain()

        disk = state.conversation_log.read_messages_chained(key)
        mids = [
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        assert disk and len(mids) == len(disk), (
            f"fixture needs EVERY disk row to carry an id so the owed-set loop runs; "
            f"disk={len(disk)} mids={len(mids)}"
        )
        assert not any(
            m.get("role") == "permission" for m in disk
        ), "fixture expects the permission row to be un-persisted"
        assert any(
            m.get("role") == "permission" for m in slot.messages
        ), "fixture expects the permission row in the window"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/perm?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        roles = [m["role"] for m in data["messages"]]
        assert (
            roles.count("permission") == 1
        ), f"the pending approval prompt was dropped or duplicated; roles={roles}"
        contents = [m["content"] for m in data["messages"]]
        assert contents.count("a1") == 1, f"a persisted row was duplicated; got {contents}"

    @pytest.mark.asyncio
    async def test_resolved_permission_is_not_appended_after_later_turns(
        self, tmp_path, monkeypatch
    ):
        """A RESOLVED approval must not be re-appended after later persisted rows.

        ``permission`` is never persisted (``chat_persistence.py:1274`` returns
        ``None``), so a permission row in the window is ALWAYS owed. For a pending
        one that is right — it is the last row, and the client needs it. A resolved
        one is historical: the agent has continued past it and those later turns
        have reached disk, so putting it in the tail moves it AFTER them and the
        rendered order is wrong.

        Resolution is an in-place edit of the row's ``cls`` JSON
        (``state_parts/transcript_rows.py`` ``_mark_permission_resolved``), so the row stays in the
        window and its position cannot distinguish it.
        """
        import json as _json

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("permdone")
        key = "dashboard:permdone"

        slot.append("user", "q1")
        slot.append("permission", "Run a shell command?", _json.dumps({"request_id": "req-1"}))
        slot.mark_permission_resolved("req-1", "approved")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        disk = state.conversation_log.read_messages_chained(key)
        mids = [
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        ]
        assert disk and len(mids) == len(disk), (
            f"fixture needs EVERY disk row to carry an id so the owed-set loop runs; "
            f"disk={len(disk)} mids={len(mids)}"
        )
        assert not any(
            m.get("role") == "permission" for m in disk
        ), "fixture expects the permission row to be un-persisted"
        window_roles = [m.get("role") for m in slot.messages]
        assert "permission" in window_roles, "fixture expects the permission row in the window"
        assert window_roles.index("permission") < window_roles.index(
            "assistant"
        ), f"fixture expects the approval to sit BEFORE the later turn; {window_roles}"
        perm = next(m for m in slot.messages if m.get("role") == "permission")
        assert (
            _json.loads(perm["cls"]).get("resolved") == "approved"
        ), "fixture expects the permission row to be marked resolved"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/permdone?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant"], (
            f"a resolved approval was re-appended after later persisted rows, "
            f"corrupting transcript order; roles={roles}"
        )

    @pytest.mark.asyncio
    async def test_live_chunk_row_survives_a_bounded_read(self, tmp_path, monkeypatch):
        """A live ``chunk`` row must reach the bounded response.

        ``_prepare_messages`` does not discard chunk rows, it ACCUMULATES them and
        emits one ``streaming`` row (``chat_utils_display._prepare_messages_scoped``).
        That is the only way in-flight assistant text reaches the client through this endpoint,
        because the client filters raw ``chunk`` itself (``store/chat/wire.ts``
        ``SKIP_ROLES``). So skipping chunk rows in the owed set does not merely omit
        noise — it destroys the partial answer.

        Driven through the real producer's call shape (``chat_runner.py:3784``).
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("livechunk")
        key = "dashboard:livechunk"

        slot.append("user", "q1")
        slot.append("assistant", "a1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        partial = "partial answer so far"
        slot.append("chunk", partial, "chunk")

        disk = state.conversation_log.read_messages_chained(key)
        has_id = [
            isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str) for m in disk
        ]
        assert has_id and all(has_id), (
            f"fixture needs every disk row to carry an id so the owed-set path "
            f"runs; has_id={has_id}"
        )
        assert not any(
            m.get("content") == partial for m in disk
        ), "fixture expects the chunk row to be un-persisted"
        assert any(
            m.get("role") == "chunk" for m in slot.messages
        ), "fixture expects the chunk row to be in the window"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/livechunk?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        streamed = [m["content"] for m in data["messages"] if m.get("role") == "streaming"]
        assert streamed == [partial], (
            f"the live partial answer was dropped by the bounded read; "
            f"streaming rows={streamed}"
        )

    def test_window_snapshot_survives_a_trim_between_the_two_reads(self):
        """A trim landing mid-snapshot must not duplicate a persisted row.

        The scan runs in a worker thread, so an event-loop append can trim the
        front of the window and bump ``_disk_older_count``
        (``state_parts/slot_transcript.py`` ``append``) while the snapshot is being taken. Pairing a
        PRE-trim window with a POST-trim count shortens ``window_disk``, hides the
        trimmed rows' ids, and re-appends rows the disk read already returned.

        ``slot._lock`` is an ``asyncio.Lock`` and cannot be taken from this thread,
        so the fix is the bounded re-read ``_save_slot_to_history`` already uses
        (``chat_persistence.py:1711-1722``).
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        all_msgs = [
            {"role": "user", "content": "q1", "ts": "t1", "meta": {"mid": "m1"}},
            {"role": "assistant", "content": "a1", "ts": "t2", "meta": {"mid": "m2"}},
            {"role": "user", "content": "q2", "ts": "t3", "meta": {"mid": "m3"}},
        ]

        class TrimOnCopy(list):
            """Fires ONE trim after a copy has read every element."""

            def __init__(self, items):
                super().__init__(items)
                self.owner = None
                self.fired = False

            def __iter__(self):
                snapshot = list(super().__iter__())
                yield from snapshot
                if not self.fired:
                    self.fired = True
                    del self[:2]
                    self.owner._disk_older_count += 2

        window = TrimOnCopy(
            [
                {"role": "user", "content": "q1", "ts": "t1", "meta": {"mid": "m1"}},
                {"role": "assistant", "content": "a1", "ts": "t2", "meta": {"mid": "m2"}},
                {"role": "user", "content": "q2", "ts": "t3", "meta": {"mid": "m3"}},
                {"role": "assistant", "content": "owed", "ts": "t4"},
            ]
        )
        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        window.owner = slot

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]

        assert window.fired, "fixture never fired the trim, so nothing was raced"
        contents = [m["content"] for m in out]
        assert contents.count("q1") == 1 and contents.count("a1") == 1, (
            f"a trim between the two snapshot reads duplicated a persisted row; " f"got {contents}"
        )
