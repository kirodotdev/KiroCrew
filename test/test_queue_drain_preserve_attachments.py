"""A queued send keeps its attachment lists through the drain.

A dispatched send persists the client's ``meta.files`` (and ``meta.dirs``) on
its user row, and the renderer resolves each ``[attached_file N] path`` marker
LOSSLESSLY against that list. A send that arrived while the slot was busy went
through the queue instead, and the queue entry carried only the containment
snapshot and the send id -- so the row the drain wrote had no list, the
renderer fell back to a whitespace-bounded capture of the marker text, and a
path with a space (``/tmp/My Report.pdf``) came back as ``/tmp/My``: an
attachment card that opens nothing.

These pins cover the producer (the busy-slot and sub-agent-hold branches stamp
the lists onto the entry), the leg the fix relies on (the drain's meta union
carries them onto the persisted row), the client's leg (the ``queue_pop`` frame
carries them, since no ``chat_message`` echo follows for a user row), the
validation (a malformed list is dropped rather than carried, because the lists
are indexed by marker number), and the merge rule (an attachment-bearing entry
drains alone, because a merged row has one meta for several texts and every
other entry's markers would resolve against the wrong list).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_delivery import (
    ATTACHMENT_LIST_MAX_ITEMS,
    ATTACHMENT_PATH_MAX_LEN,
    MAX_PENDING_STEERS,
    attachment_meta,
)

_PATH = "/tmp/My Report.pdf"
_WIRE = f"summarize this\n[attached_file 1] {_PATH}"
_DIR = "/home/u/my designs/"


async def _post_busy(state, slot_key: str, message: str, meta: dict | None):
    body: dict = {"message": message, "slot": slot_key}
    if meta is not None:
        body["meta"] = meta
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json=body)
        assert resp.status == 200
        payload = await resp.json()
        assert payload.get("queued") is True
        return payload


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


def _busy_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("busy-chat")
    slot._in_stage_execution = True  # force the busy queue path
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    return state, slot


def _seed_pending_steer_stores(slot, count):
    messages = [f"existing pending steer {i}" for i in range(count)]
    slot._pending_steers.extend(messages)
    slot._steer_delivery_ids.update(
        {message: f"delivery-{i}" for i, message in enumerate(messages)}
    )
    slot._steer_send_ids.update({message: f"send-{i}" for i, message in enumerate(messages)})
    slot._steer_user_origin.update({message: True for message in messages})
    slot._steer_admissions.update({message: {"constraints": []} for message in messages})
    slot._steer_attachment_meta.update(
        {message: {"files": [f"/tmp/file-{i}"]} for i, message in enumerate(messages)}
    )
    return messages


class TestAttachmentMeta:
    def test_reduces_to_the_two_ordered_lists(self):
        assert attachment_meta({"files": [_PATH], "dirs": [_DIR], "sendId": "s-1"}) == {
            "files": [_PATH],
            "dirs": [_DIR],
        }

    @pytest.mark.parametrize(
        "meta",
        [
            None,
            {},
            {"files": []},
            {"files": "not-a-list"},
            {"files": [_PATH, 42]},
            {"files": [_PATH, ""]},
            {"dirs": {"a": 1}},
        ],
    )
    def test_drops_anything_but_a_nonempty_list_of_paths(self, meta):
        """The lists are indexed by marker number on the render side, so one bad
        entry would shift every later marker onto the wrong path -- drop the
        whole list rather than carry a corrupt one."""
        assert attachment_meta(meta) == {}

    def test_keeps_the_good_list_when_the_other_is_bad(self):
        assert attachment_meta({"files": [_PATH], "dirs": "nope"}) == {"files": [_PATH]}

    def test_admits_a_list_at_the_bounds(self):
        """The bounds are inclusive: a legitimate send right at them is carried."""
        at_count = [f"/tmp/f{i}" for i in range(ATTACHMENT_LIST_MAX_ITEMS)]
        at_len = ["/" + "x" * (ATTACHMENT_PATH_MAX_LEN - 1)]
        assert attachment_meta({"files": at_count, "dirs": at_len}) == {
            "files": at_count,
            "dirs": at_len,
        }

    def test_refuses_a_list_over_the_count_bound_whole(self, caplog):
        """Retained lists are raw client input (`api_chat` reads its body
        uncapped), so the count is bounded where the list is RETAINED. Refused
        whole, not sliced: markers past a cut would resolve through the
        renderer's whitespace-bounded fallback. Said once in the log."""
        over = [f"/tmp/f{i}" for i in range(ATTACHMENT_LIST_MAX_ITEMS + 1)]
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_delivery"):
            assert attachment_meta({"files": over, "dirs": [_DIR]}) == {"dirs": [_DIR]}
        assert sum("over the" in r.getMessage() for r in caplog.records) == 1

    def test_refuses_a_list_carrying_an_overlong_path_whole(self, caplog):
        """A path cut in place is a different path, so one over-long entry
        refuses its list rather than truncating it."""
        too_long = "/" + "x" * ATTACHMENT_PATH_MAX_LEN
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_delivery"):
            assert attachment_meta({"files": [_PATH, too_long], "dirs": [_DIR]}) == {"dirs": [_DIR]}
        assert sum("over the" in r.getMessage() for r in caplog.records) == 1


class TestBusySlotQueueEntry:
    @pytest.mark.asyncio
    async def test_entry_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)

        await _post_busy(
            state, "busy-chat", _WIRE, {"files": [_PATH], "dirs": [_DIR], "sendId": "s-1"}
        )

        entry = next(i for i in slot._queue if i["content"] == _WIRE)
        assert entry["meta"].get("files") == [_PATH]
        assert entry["meta"].get("dirs") == [_DIR]
        # The id contract is untouched by the addition.
        assert entry["meta"].get("sendId") == "s-1"

    @pytest.mark.asyncio
    async def test_a_send_without_attachments_keeps_the_prior_entry_shape(
        self, tmp_path, monkeypatch
    ):
        """Pinned as ABSENT keys: an empty list on the row would still be a shape
        change for every meta-keyed consumer."""
        state, slot = _busy_state(tmp_path, monkeypatch)

        await _post_busy(state, "busy-chat", "plain text", {"sendId": "s-2"})

        entry = next(i for i in slot._queue if i["content"] == "plain text")
        assert "files" not in entry["meta"]
        assert "dirs" not in entry["meta"]


def _queue_push_frames(state) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]


class TestQueuePushFrameCarriesTheLists:
    """Every producer's ``queue_push`` frame names the entry's attachment lists.

    The card that frame draws is what a cancel restores the composer from on a
    tab that never held the send's own state (a reload, another tab). Without
    the lists the client parses the marker text by whitespace and a spaced
    path is truncated or left in the composer verbatim. The key is the
    ``meta`` the ``queue_pop`` frame already uses; absent, not empty, for an
    entry without attachments.
    """

    @pytest.mark.asyncio
    async def test_busy_slot_frame(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        receipt = await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH], "dirs": [_DIR]})
        (frame,) = _queue_push_frames(state)
        assert frame["queue_id"] == receipt["queue_id"]
        assert frame["content"] == _WIRE
        assert frame["meta"] == {"files": [_PATH], "dirs": [_DIR]}

    @pytest.mark.asyncio
    async def test_busy_slot_frame_without_attachments_has_no_meta_key(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", "plain text", {"sendId": "s-2"})
        (frame,) = _queue_push_frames(state)
        assert "meta" not in frame

    @pytest.mark.asyncio
    async def test_sub_agent_hold_frame(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        # The idle-slot hold: no turn is running, but children are.
        slot._in_stage_execution = False
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))
        receipt = await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH]})
        (frame,) = _queue_push_frames(state)
        assert frame["queue_id"] == receipt["queue_id"]
        assert frame["meta"] == {"files": [_PATH]}

    @pytest.mark.asyncio
    async def test_requeued_steer_frame(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        state, slot = _busy_state(tmp_path, monkeypatch)
        slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": _WIRE,
                    "steer": True,
                    "meta": {"files": [_PATH], "dirs": [_DIR]},
                },
            )
            assert resp.status == 200
        state.broadcast_ws.reset_mock()

        _requeue_unconsumed_steers(state, slot)
        (frame,) = _queue_push_frames(state)
        entry = next(i for i in slot._queue if i["content"] == _WIRE)
        assert frame["queue_id"] == entry["id"]
        assert frame["meta"] == {"files": [_PATH], "dirs": [_DIR]}


class TestDrainedRow:
    @pytest.mark.asyncio
    async def test_drained_row_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        """End to end: the list on the entry is worth nothing on its own -- the
        persisted row is what history replay reads back."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH]})

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows, "the drain must have written a user row for the queued send"
        meta = rows[-1].get("meta") or {}
        assert meta.get("files") == [_PATH]
        # Queue plumbing must not ride into the persisted row.
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        assert QUEUED_CONTAINMENT_META_KEY not in meta

    @pytest.mark.asyncio
    async def test_queue_pop_frame_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        """The client rebuilds the drained entry as a user row from the frame
        alone (no chat_message echo follows for a user row), so the lists must
        be ON the frame or the rebuilt row truncates the spaced path until the
        next reload."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH], "dirs": [_DIR]})

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        pops = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_pop"
        ]
        pop = next(p for p in pops if p.get("content") == _WIRE)
        assert pop.get("meta") == {"files": [_PATH], "dirs": [_DIR]}

    @pytest.mark.asyncio
    async def test_queue_pop_frame_without_attachments_has_no_meta_key(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", "plain text", None)

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        pops = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_pop"
        ]
        pop = next(p for p in pops if p.get("content") == "plain text")
        assert "meta" not in pop


class TestSteeredAttachments:
    @pytest.mark.asyncio
    async def test_pending_steer_cap_falls_back_without_retaining_anything(
        self, tmp_path, monkeypatch, caplog
    ):
        state, slot = _busy_state(tmp_path, monkeypatch)
        _seed_pending_steer_stores(slot, MAX_PENDING_STEERS)
        steer = AsyncMock(return_value=True)
        slot._acp_client = MagicMock(supports_steer=True, steer=steer)
        store_names = (
            "_pending_steers",
            "_steer_delivery_ids",
            "_steer_send_ids",
            "_steer_user_origin",
            "_steer_admissions",
            "_steer_attachment_meta",
        )
        before = {name: getattr(slot, name).copy() for name in store_names}
        overflow = "queue this instead of retaining steer metadata"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": overflow,
                    "steer": True,
                    "meta": {
                        "sendId": "s-cap-overflow",
                        "files": [_PATH],
                        "dirs": [_DIR],
                    },
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        steer.assert_not_awaited()
        for name, expected in before.items():
            assert getattr(slot, name) == expected
        queued = next(entry for entry in slot._queue if entry["content"] == overflow)
        assert queued["meta"]["files"] == [_PATH]
        assert queued["meta"]["dirs"] == [_DIR]
        cap_warnings = [
            record
            for record in caplog.records
            if "pending steer limit reached" in record.getMessage()
        ]
        assert len(cap_warnings) == 1

    @pytest.mark.asyncio
    async def test_pending_steer_below_cap_is_admitted(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        _seed_pending_steer_stores(slot, MAX_PENDING_STEERS - 1)

        async def accept(message):
            assert message == _WIRE
            assert len(slot._pending_steers) == MAX_PENDING_STEERS
            assert _WIRE in slot._steer_delivery_ids
            assert slot._steer_send_ids[_WIRE] == "s-cap-last"
            assert slot._steer_user_origin[_WIRE] is True
            assert _WIRE in slot._steer_admissions
            assert slot._steer_attachment_meta[_WIRE] == {
                "files": [_PATH],
                "dirs": [_DIR],
            }
            return True

        steer = AsyncMock(side_effect=accept)
        slot._acp_client = MagicMock(supports_steer=True, steer=steer)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": _WIRE,
                    "steer": True,
                    "meta": {
                        "sendId": "s-cap-last",
                        "files": [_PATH],
                        "dirs": [_DIR],
                    },
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        steer.assert_awaited_once_with(_WIRE)
        assert slot._pending_steers[-1] == _WIRE
        assert slot._steer_attachment_meta[_WIRE] == {
            "files": [_PATH],
            "dirs": [_DIR],
        }

    @pytest.mark.asyncio
    async def test_accepted_steer_keeps_attachments_on_row_and_live_echo(
        self, tmp_path, monkeypatch
    ):
        state, slot = _busy_state(tmp_path, monkeypatch)
        slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": _WIRE,
                    "steer": True,
                    "meta": {"files": [_PATH], "dirs": [_DIR]},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        assert _user_rows(slot)[-1]["meta"]["files"] == [_PATH]
        echo = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert echo["meta"] == {"files": [_PATH], "dirs": [_DIR]}
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        _settle_consumed_steers(slot, f"<user_message>\n{_WIRE}\n</user_message>")
        assert slot._steer_attachment_meta == {}

    @pytest.mark.asyncio
    async def test_steer_retains_nothing_for_a_list_over_the_bound(self, tmp_path, monkeypatch):
        """The pending-steer map is a retention site for raw client lists and is
        keyed by message text with no cap of its own, so the field bound is
        what keeps an entry's size in check: an over-bound list is admitted
        nowhere -- not the map, not the row, not the live echo."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        # Never consumed, so the entry (if admitted) stays pending past the RPC.
        slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))
        over = [f"/tmp/f{i}" for i in range(ATTACHMENT_LIST_MAX_ITEMS + 1)]

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": _WIRE,
                    "steer": True,
                    "meta": {"files": over},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        assert slot._pending_steers == [_WIRE]
        assert slot._steer_attachment_meta == {}
        assert "files" not in _user_rows(slot)[-1].get("meta", {})
        echo = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert "meta" not in echo

    @pytest.mark.asyncio
    async def test_unconsumed_steer_keeps_attachments_through_requeue_and_drain(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        state, slot = _busy_state(tmp_path, monkeypatch)
        slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "busy-chat",
                    "message": _WIRE,
                    "steer": True,
                    "meta": {"files": [_PATH], "dirs": [_DIR]},
                },
            )
            assert resp.status == 200

        _requeue_unconsumed_steers(state, slot)
        entry = next(i for i in slot._queue if i["content"] == _WIRE)
        assert entry["meta"]["files"] == [_PATH]
        assert entry["meta"]["dirs"] == [_DIR]
        assert slot._steer_attachment_meta == {}

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)
        assert _user_rows(slot)[-1]["meta"]["files"] == [_PATH]


class TestAttachmentEntriesDrainAlone:
    """Merging joins several texts under ONE meta. Each text indexes the lists
    by marker number, so a merged row would resolve every entry's
    ``[attached_file 1]`` against whichever list won -- a card that opens a
    different file. An attachment-bearing entry therefore ends a merge run and,
    at the head of the queue, pops alone."""

    def _slot(self, entries):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._queue = list(entries)
        return slot

    def test_carries_attachments_reads_the_two_lists(self):
        from kiro_crew.dashboard.chat_utils import carries_attachments

        assert carries_attachments({"meta": {"files": [_PATH]}}) is True
        assert carries_attachments({"meta": {"dirs": [_DIR]}}) is True
        assert carries_attachments({"meta": {"sendId": "s-1"}}) is False
        assert carries_attachments({"meta": {"files": []}}) is False
        assert carries_attachments({"meta": "nope"}) is False
        assert carries_attachments({}) is False

    def test_head_entry_with_attachments_pops_alone(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        slot = self._slot(
            [
                {"id": "a", "content": _WIRE, "meta": {"files": [_PATH]}},
                {"id": "b", "content": "and this", "meta": {}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == _WIRE
        assert [c["id"] for c in consumed] == ["a"]
        assert [i["id"] for i in slot._queue] == ["b"]

    def test_merge_run_stops_before_an_attachment_entry(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        other_wire = "read this\n[attached_file 1] /tmp/other.txt"
        slot = self._slot(
            [
                {"id": "a", "content": "first", "meta": {}},
                {"id": "b", "content": "second", "meta": {}},
                {"id": "c", "content": other_wire, "meta": {"files": ["/tmp/other.txt"]}},
                {"id": "d", "content": _WIRE, "meta": {"files": [_PATH]}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == "[2 queued messages merged]\n\nfirst\n\nsecond"
        assert [c["id"] for c in consumed] == ["a", "b"]
        # The two attachment entries stay queued, in order, each to drain alone
        # with its own list.
        assert [i["id"] for i in slot._queue] == ["c", "d"]
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == other_wire
        assert [c["id"] for c in consumed] == ["c"]
        assert consumed[0]["meta"]["files"] == ["/tmp/other.txt"]

    def test_plain_entries_still_merge(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        slot = self._slot(
            [
                {"id": "a", "content": "first", "meta": {"sendId": "s-1"}},
                {"id": "b", "content": "second", "meta": {"sendId": "s-2"}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == "[2 queued messages merged]\n\nfirst\n\nsecond"
        assert len(consumed) == 2


class TestAttachmentPathsAreRedacted:
    """A path is user-supplied text and reaches every client of the slot, so it
    takes the same redaction the message text does."""

    def test_credential_in_a_path_is_redacted_before_it_rides_the_entry(self):
        from kiro_crew.dashboard.chat_utils import redact_credentials

        leaky = "/tmp/exports/AKIAIOSFODNN7EXAMPLE-report.pdf"
        expected, changed = redact_credentials(leaky)
        assert changed, "fixture must contain something the redactor rewrites"
        out = attachment_meta({"files": [leaky, _PATH]})
        assert out["files"] == [expected, _PATH]

    def test_plain_paths_pass_through_unchanged(self):
        assert attachment_meta({"files": [_PATH], "dirs": [_DIR]}) == {
            "files": [_PATH],
            "dirs": [_DIR],
        }


class TestQueueEditPrunesAttachmentMeta:
    """An edit that removes a marker removes the file from the agent's prompt;
    the entry's list must follow, or the drained row shows a card for an
    attachment that was never delivered. The surviving markers are renumbered
    to the filtered list, because the renderer reads ``files[N-1]`` for marker
    ``N`` and, when the two disagree, falls back to a whitespace capture that
    truncates a spaced path."""

    def _slot_with_entry(self, content, meta):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        qid = slot.queue_append(content, meta=dict(meta))
        return slot, qid

    def _entry(self, slot, qid):
        return next(i for i in slot._queue if i["id"] == qid)

    def test_removing_the_first_marker_renumbers_the_spaced_survivor(self):
        other = "/tmp/other.txt"
        wire = f"read both\n[attached_file 1] {other}\n[attached_file 2] {_PATH}"
        slot, qid = self._slot_with_entry(wire, {"files": [other, _PATH], "sendId": "s-1"})

        assert slot.queue_edit_by_id(qid, f"read one\n[attached_file 2] {_PATH}") is True

        entry = self._entry(slot, qid)
        # Marker 2 became marker 1 and files[0] is its path: the renderer's
        # lossless branch (files[N-1] sits verbatim after the marker) holds, so
        # `/tmp/My Report.pdf` is never read by whitespace as `/tmp/My`.
        assert entry["content"] == f"read one\n[attached_file 1] {_PATH}"
        assert entry["meta"]["files"] == [_PATH]
        # Unrelated meta is untouched by the prune.
        assert entry["meta"]["sendId"] == "s-1"

    def test_removing_a_middle_marker_compacts_the_rest(self):
        a, b, c = "/tmp/a.txt", "/tmp/b c.txt", "/tmp/d.txt"
        wire = f"[attached_file 1] {a}\n[attached_file 2] {b}\n[attached_file 3] {c}"
        slot, qid = self._slot_with_entry(wire, {"files": [a, b, c]})
        assert slot.queue_edit_by_id(qid, f"[attached_file 1] {a}\n[attached_file 3] {c}") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == f"[attached_file 1] {a}\n[attached_file 2] {c}"
        assert entry["meta"]["files"] == [a, c]

    def test_removing_every_marker_removes_the_list(self):
        slot, qid = self._slot_with_entry(_WIRE, {"files": [_PATH]})
        assert slot.queue_edit_by_id(qid, "just the text now") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == "just the text now"
        assert "files" not in entry["meta"]

    def test_a_path_that_prefixes_a_kept_one_is_still_dropped(self):
        """`/tmp/report.pdf` is a substring of `/tmp/report.pdf.bak`; only the
        exact numbered marker with the path ending at whitespace or the end of
        the text keeps an entry alive."""
        a, b = "/tmp/report.pdf", "/tmp/report.pdf.bak"
        wire = f"[attached_file 1] {a}\n[attached_file 2] {b}"
        slot, qid = self._slot_with_entry(wire, {"files": [a, b]})
        assert slot.queue_edit_by_id(qid, f"[attached_file 2] {b}") is True
        entry = self._entry(slot, qid)
        assert entry["meta"]["files"] == [b]
        assert entry["content"] == f"[attached_file 1] {b}"

    def test_a_path_mentioned_in_prose_without_its_marker_is_dropped(self):
        slot, qid = self._slot_with_entry(_WIRE, {"files": [_PATH]})
        assert slot.queue_edit_by_id(qid, f"I removed {_PATH} from this message") is True
        entry = self._entry(slot, qid)
        assert "files" not in entry["meta"]

    def test_renumbering_leaves_marker_like_prose_with_a_longer_path_alone(self):
        """`[attached_file 2] /tmp/b` is a prefix of the caption text
        `[attached_file 2] /tmp/bak`; only the exact token bounded by whitespace
        or the end of the text is renumbered."""
        a, b = "/tmp/a.txt", "/tmp/b"
        prose = "note: the literal [attached_file 2] /tmp/bak is prose"
        wire = f"[attached_file 1] {a}\n[attached_file 2] {b}\n{prose}"
        slot, qid = self._slot_with_entry(wire, {"files": [a, b]})
        assert slot.queue_edit_by_id(qid, f"[attached_file 2] {b}\n{prose}") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == f"[attached_file 1] {b}\n{prose}"
        assert entry["meta"]["files"] == [b]

    def test_edit_that_keeps_every_marker_stores_the_text_verbatim(self):
        new = f"reworded caption\n[attached_file 1] {_PATH}"
        slot, qid = self._slot_with_entry(_WIRE, {"files": [_PATH]})
        assert slot.queue_edit_by_id(qid, new) is True
        entry = self._entry(slot, qid)
        assert entry["content"] == new
        assert entry["meta"]["files"] == [_PATH]

    def test_dirs_renumber_with_their_own_marker_word(self):
        d1, d2 = "/home/u/one/", _DIR
        wire = f"see [attached_dir 1] {d1} and [attached_dir 2] {d2}"
        slot, qid = self._slot_with_entry(wire, {"dirs": [d1, d2]})
        assert slot.queue_edit_by_id(qid, f"see [attached_dir 2] {d2}") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == f"see [attached_dir 1] {d2}"
        assert entry["meta"]["dirs"] == [d2]

    def test_prune_helper_tolerates_missing_or_malformed_meta(self):
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        assert prune_attachment_meta(None, "x", "x") == "x"
        meta = {"files": "nope", "dirs": [_DIR]}
        before = f"see [attached_dir 1] {_DIR}"
        assert prune_attachment_meta(meta, "text without the dir", before) == "text without the dir"
        assert meta == {"files": "nope"}

    def test_an_entry_the_old_text_never_named_survives_an_edit(self):
        """A caller can stamp `meta.files` on text that carries no marker for
        it. The edit did not remove that attachment (there was nothing to
        remove), so the list keeps it; otherwise the drained row would lose an
        attachment the user never touched."""
        slot, qid = self._slot_with_entry("no marker here", {"files": [_PATH]})
        assert slot.queue_edit_by_id(qid, "still no marker, reworded") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == "still no marker, reworded"
        assert entry["meta"]["files"] == [_PATH]

    def test_a_list_path_redacted_differently_from_the_text_survives_an_edit(self):
        """The list is redacted at enqueue (`attachment_meta`), the text is
        not: a credential-bearing path is spelled two ways. The old text never
        carried the list's spelling, so an edit that keeps the text's marker
        must not drop the entry."""
        from kiro_crew.dashboard.chat_delivery import attachment_meta

        raw = "/tmp/uploads/AKIAIOSFODNN7EXAMPLE-report.pdf"
        lists = attachment_meta({"files": [raw]})
        stored = lists["files"][0]
        assert stored != raw, "the fixture path must be one the redactor rewrites"
        wire = f"summarize\n[attached_file 1] {raw}"
        slot, qid = self._slot_with_entry(wire, lists)
        assert slot.queue_edit_by_id(qid, f"summarize briefly\n[attached_file 1] {raw}") is True
        entry = self._entry(slot, qid)
        assert entry["meta"]["files"] == [stored]
        assert entry["content"] == f"summarize briefly\n[attached_file 1] {raw}"

    def test_a_named_entry_is_still_dropped_next_to_an_unnamed_one(self):
        """Mixed list: `a` has its marker, `b` never did. Removing `a`'s marker
        drops `a` and keeps `b`; nothing to renumber since `b` has no token."""
        a, b = "/tmp/a.txt", "/tmp/b.txt"
        slot, qid = self._slot_with_entry(f"[attached_file 1] {a}", {"files": [a, b]})
        assert slot.queue_edit_by_id(qid, "dropped a") is True
        entry = self._entry(slot, qid)
        assert entry["content"] == "dropped a"
        assert entry["meta"]["files"] == [b]

    @pytest.mark.asyncio
    async def test_edit_endpoint_echoes_the_normalized_text(self, tmp_path, monkeypatch):
        """The row and the ``queue_edit`` frame carry the ENTRY's text (markers
        renumbered), not the request body, so every client shows the text the
        agent will actually receive."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("busy-chat")
        other = "/tmp/other.txt"
        wire = f"[attached_file 1] {other}\n[attached_file 2] {_PATH}"
        qid = slot.queue_append(wire, meta={"files": [other, _PATH]})
        slot.append("queued", wire, json.dumps({"queue_id": qid}))

        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit

        app = web.Application()
        app["state"] = state
        app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/busy-chat/queue/{qid}",
                    json={"content": f"[attached_file 2] {_PATH}"},
                )
                assert resp.status == 200

        expected = f"[attached_file 1] {_PATH}"
        frame = next(
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_edit"
        )
        assert frame["content"] == expected
        # The pruned, renumbered list rides the frame beside the renumbered
        # text: `[attached_file 1]` is now `files[0]`, so a client that
        # replaces the row's lists from this frame can still restore the
        # survivor exactly on a later cancel. Without it the row keeps the
        # pre-edit two-entry list, which indexes nothing in the new text.
        assert frame["meta"] == {"files": [_PATH]}
        row = next(m for m in slot.messages if m.get("role") == "queued")
        assert row["content"] == expected

    @pytest.mark.asyncio
    async def test_edit_frame_carries_no_meta_once_every_marker_is_gone(
        self, tmp_path, monkeypatch
    ):
        """An edit that drops the last marker drops the list; the frame says so
        by carrying no ``meta`` (the client reads absence as "no lists")."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("busy-chat")
        qid = slot.queue_append(_WIRE, meta={"files": [_PATH]})
        slot.append("queued", _WIRE, json.dumps({"queue_id": qid}))

        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit

        app = web.Application()
        app["state"] = state
        app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/busy-chat/queue/{qid}", json={"content": "just text"}
                )
                assert resp.status == 200

        frame = next(
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_edit"
        )
        assert frame == {"slot": "busy-chat", "queue_id": qid, "content": "just text"}
        assert "files" not in slot._queue[0]["meta"]
