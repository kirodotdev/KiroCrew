"""Remote-crew execution binding: a local session whose turns run on a peer.

Covers the three layers the feature is made of, in the order a turn traverses
them: the ``_ChatSlot`` binding and its round-trip through history metadata, the
in-band mirror that lets an SSE stream carry a WebSocket turn's full vocabulary,
and the relay that replays a peer's stream into the local slot.

The relay tests drive :func:`relay_remote_turn` with a hand-written byte stream
rather than a tunnel: the peer's wire format is the contract under test, and a
fake iterator pins it exactly while keeping the test free of aiohttp, SSH and a
second gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

import kiro_crew
from kiro_crew.dashboard import remote_mirror, remote_relay
from kiro_crew.dashboard.remote_relay import (
    RemoteTurnError,
    create_peer_slot,
    ensure_version_parity,
    forward_peer_selection,
    forward_peer_stop,
    iter_sse_records,
    parse_sse_record,
    relay_remote_turn,
)
from kiro_crew.dashboard.state import _ChatSlot

#: Every async test here needs the loop; the repo runs pytest-asyncio in strict
#: mode, so the marker is explicit rather than inferred from the coroutine.
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_mirror():
    """No test may leak a mirror registration into the next one."""
    remote_mirror.reset_for_tests()
    yield
    remote_mirror.reset_for_tests()


def _remote_slot(key: str = "chat-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.executor = "remote"
    slot.instance_id = "nobita"
    slot.remote_slot = "peer-chat-9"
    return slot


class _FakeRequest(dict):
    """The two things the owner gate reads off a request, and nothing else.

    A ``dict`` subclass rather than a namespace because the gate distinguishes an
    ``app`` claim of ``""`` from an ABSENT one (``"app" not in request`` means the
    auth middleware never ran, which must refuse), so membership has to behave
    like the real mapping.
    """

    def __init__(self, state, *, app_name: str = "", user: str = "local-app") -> None:
        super().__init__({"app": app_name, "user": user})
        self.app = {"state": state}


def _owner_request(state, **kwargs) -> _FakeRequest:
    """A request the owner gate admits, for the pick tests that are not ABOUT it.

    ``local-app`` is a local bootstrap subject, so with no configured ``owner_id``
    it passes ``is_owner_dashboard_request`` — the same default and the same reason
    as :func:`_create_app`'s.
    """
    return _FakeRequest(state, **kwargs)


async def _stream(*records: bytes) -> AsyncIterator[bytes]:
    for record in records:
        yield record


def _sse(row: dict) -> bytes:
    return f"data: {json.dumps(row)}\n\n".encode()


# ── The binding ────────────────────────────────────────────────────────────────


class TestRemoteBinding:
    def test_a_fresh_slot_runs_locally(self):
        assert _ChatSlot("chat-1").executor == "local"
        assert _ChatSlot("chat-1").is_remote is False

    @pytest.mark.parametrize("missing", ["instance_id", "remote_slot"])
    def test_a_half_present_binding_is_not_remote(self, missing):
        """A marker without its target must NOT read as a remote slot.

        This is the fail-closed direction that matters: if ``is_remote`` said
        True the dispatch would try a peer it cannot name, and if the *marker*
        alone were ignored the turn would silently run on THIS machine — work the
        user asked a named crew to do. Neither is acceptable, so the property is
        False and ``api_chat`` refuses on the marker instead.
        """
        slot = _remote_slot()
        setattr(slot, missing, "")
        assert slot.is_remote is False
        assert slot.executor == "remote"  # the marker is preserved for the refusal

    def test_the_binding_is_projected_on_every_slot(self):
        local = _ChatSlot("chat-1").to_dict()
        assert local["executor"] == "local"
        assert local["instance_id"] == ""
        remote = _remote_slot().to_dict()
        assert remote["executor"] == "remote"
        assert remote["instance_id"] == "nobita"

    def test_the_peers_slot_key_is_never_projected(self):
        """It is a peer-side identifier with no browser consumer — keep it server-side."""
        assert "remote_slot" not in _remote_slot().to_dict()


# ── The in-band mirror ─────────────────────────────────────────────────────────


class TestRelayMirror:
    def test_frames_are_not_mirrored_until_a_reader_attaches(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert slot._pending == []

    def test_an_attached_reader_receives_mirrored_frames(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        owned = remote_mirror.attach(slot.key)
        try:
            state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        finally:
            remote_mirror.detach(slot.key, owned)
        assert len(slot._pending) == 1
        row = slot._pending[0]
        assert row["cls"] == "relay:tool_call"
        assert json.loads(row["content"])["tool"] == "fs_read"

    @pytest.mark.parametrize("event", sorted(remote_mirror.MIRROR_SKIP_EVENTS))
    def test_already_in_band_frames_are_never_mirrored(self, tmp_path, event):
        """The denylist is what stops every chunk arriving twice.

        Each of these frame types has a transcript row or its own wire frame
        already on the SSE stream, so mirroring them would double the content.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        owned = remote_mirror.attach(slot.key)
        try:
            state.broadcast_ws(event, {"slot": slot.key, "content": "x"})
        finally:
            remote_mirror.detach(slot.key, owned)
        assert slot._pending == []

    def test_a_frame_for_another_slot_is_not_mirrored(self, tmp_path):
        state = _make_state(tmp_path)
        watched = state.get_or_create_slot("chat-1")
        other = state.get_or_create_slot("chat-2")
        owned = remote_mirror.attach(watched.key)
        try:
            state.broadcast_ws("tool_call", {"slot": other.key, "tool": "fs_read"})
        finally:
            remote_mirror.detach(watched.key, owned)
        assert watched._pending == []
        assert other._pending == []

    def test_a_second_reader_cannot_truncate_the_first(self, tmp_path):
        """Overlapping relay readers on one slot must not cut each other off."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        first = remote_mirror.attach(slot.key)
        second = remote_mirror.attach(slot.key)
        assert first is True and second is False
        remote_mirror.detach(slot.key, second)  # the non-owner leaves
        # Asserted through the mirror's only output — a frame still lands for the
        # reader that is still attached.
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert len(slot._pending) == 1
        remote_mirror.detach(slot.key, first)
        state.broadcast_ws("tool_call", {"slot": slot.key, "tool": "fs_read"})
        assert len(slot._pending) == 1  # nothing added after the owner left


# ── SSE framing ────────────────────────────────────────────────────────────────


class TestSseFraming:
    def test_records_split_on_the_blank_line_not_the_newline(self):
        """A JSON payload may contain an escaped newline; splitting on \\n cuts it."""
        buffer = bytearray()
        payload = json.dumps({"type": "chunk", "content": "line one\nline two"})
        records = list(iter_sse_records(buffer, f"data: {payload}\n\n".encode()))
        assert len(records) == 1
        assert parse_sse_record(records[0])["content"] == "line one\nline two"

    def test_a_record_split_across_chunks_is_reassembled(self):
        buffer = bytearray()
        assert list(iter_sse_records(buffer, b'data: {"type": "chunk", "cont')) == []
        records = list(iter_sse_records(buffer, b'ent": "hi"}\n\n'))
        assert [parse_sse_record(r)["content"] for r in records] == ["hi"]

    def test_the_terminator_is_reported_as_a_sentinel(self):
        assert parse_sse_record(b"data: [DONE]") == {"__done__": True}

    def test_a_keepalive_comment_is_not_a_row(self):
        assert parse_sse_record(b": keepalive") is None

    def test_an_undecodable_record_is_dropped_not_raised(self):
        assert parse_sse_record(b"data: {not json") is None

    def test_an_unterminated_record_is_refused_rather_than_buffered_forever(self):
        buffer = bytearray()
        with pytest.raises(RemoteTurnError):
            list(iter_sse_records(buffer, b"data: " + b"x" * (8 * 1024 * 1024 + 16)))


# ── The version gate ───────────────────────────────────────────────────────────


class TestVersionParity:
    async def test_an_equal_version_passes(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        await ensure_version_parity(mgr, "nobita")  # does not raise

    async def test_a_different_version_is_refused_naming_both_sides(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "0.5.9"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "0.5.9" in str(excinfo.value)
        assert kiro_crew.__version__ in str(excinfo.value)

    async def test_a_patch_skew_in_the_same_series_passes(self):
        """0.6.0 and 0.6.3 share the frame vocabulary, so a patch skew is allowed.

        The gate fences the ``major.minor`` SERIES, not the full string: a patch
        release does not move the wire contract, so refusing it only forced a
        lockstep upgrade with no safety it bought.
        """
        from kiro_crew.apps.version import parse_version

        major, minor = parse_version(kiro_crew.__version__)[:2]
        peer = f"{major}.{minor}.{minor + 999}"  # same major.minor, far-off patch
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, peer))
        await ensure_version_parity(mgr, "nobita")  # does not raise

    async def test_a_different_minor_is_refused(self):
        """A feature release (minor bump on 0.x) moves the vocabulary — refuse it."""
        from kiro_crew.apps.version import parse_version

        major, minor = parse_version(kiro_crew.__version__)[:2]
        peer = f"{major}.{minor + 1}.0"
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, peer))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "major.minor" in str(excinfo.value)

    async def test_a_non_semver_build_id_falls_back_to_strict_equality(self, monkeypatch):
        """When a side is a packaging build id, series parity is unprovable.

        ``parse_version`` raises ``ValueError`` on a non-semver build id, so the
        gate falls back to full-string equality rather than optimistically pass —
        an equal build id runs, any difference is refused.
        """
        monkeypatch.setattr(kiro_crew, "__version__", "build-2026.09.04-abc123", raising=False)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "build-2026.09.04-abc123"))
        await ensure_version_parity(mgr, "nobita")  # identical build id → passes

        mgr.peer_version = AsyncMock(return_value=(True, "build-2026.09.03-def456"))
        with pytest.raises(RemoteTurnError):
            await ensure_version_parity(mgr, "nobita")

    async def test_an_oversized_numeric_version_is_refused_not_crashed(self):
        """A peer-controlled version of thousands of digits must not 500 the create.

        CPython caps ``int(str)`` at 4300 digits, so parsing a version with a
        longer numeric run raises ``ValueError``. That must be caught and turned
        into the ordinary refusal (an unprovable series → strict equality →
        mismatch), never propagate out of ``ensure_version_parity`` as an HTTP 500
        (GPT/opus #8543).
        """
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "9" * 5000 + ".0.0"))
        with pytest.raises(RemoteTurnError):
            await ensure_version_parity(mgr, "nobita")

    async def test_an_unknown_version_is_a_mismatch_not_a_pass(self):
        """A peer too old to report its version cannot be proven equal.

        The optimistic reading — attempt it and see — is what the gate exists to
        prevent: the two ends exchange an unversioned frame vocabulary, so a skew
        surfaces as a session that half works.
        """
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(False, "capability_peer_too_old"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "older Kiro Crew" in str(excinfo.value)

    async def test_an_unreachable_peer_is_refused_with_a_different_remedy(self):
        """ "Too old" and "unreachable" need different advice, so they differ.

        A stale credential or a dropped tunnel is fixed by reconnecting; telling
        that user to update their crew sends them to the wrong place.
        """
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(False, "capability_no_credential"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "Reconnect the crew" in str(excinfo.value)


# ── The relay ──────────────────────────────────────────────────────────────────


class TestRelayReplay:
    async def test_chunks_replay_as_local_chat_chunk_frames(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "He", "cls": "chunk"}),
                _sse({"type": "chunk", "content": "llo", "cls": "chunk"}),
                b"data: [DONE]\n\n",
            ),
        )
        chunk_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        assert [c.args[1]["content"] for c in chunk_calls] == ["He", "llo"]
        # Local sequence numbers, not the peer's: the frontend orders within the
        # LOCAL slot, and a second relayed turn would restart the peer's count.
        assert [c.args[1]["seq"] for c in chunk_calls] == [1, 2]
        assert all(c.args[1]["slot"] == slot.key for c in chunk_calls)

    async def test_a_mirrored_frame_is_rebroadcast_under_the_local_key(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:tool_call",
                        "cls": "relay:tool_call",
                        "content": json.dumps(
                            {"slot": "peer-chat-9", "tool": "fs_read", "tool_call_id": "t1"}
                        ),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )
        tool_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "tool_call"]
        assert len(tool_calls) == 1
        # Rewriting the slot identifier is the whole translation — this is what
        # lets the unmodified frontend consumption path render a peer's turn.
        assert tool_calls[0].args[1]["slot"] == slot.key
        assert tool_calls[0].args[1]["tool"] == "fs_read"

    async def test_the_peers_user_row_is_not_replayed(self, tmp_path):
        """The local side already appended the user's message before dispatch."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "user", "content": "hi", "cls": "msg msg-u"}),
                b"data: [DONE]\n\n",
            ),
        )
        assert [m["role"] for m in slot.messages] == []

    async def test_the_finalized_assistant_row_replaces_its_chunks(self, tmp_path):
        """Keeping both would render the answer twice, once streamed once final."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "He", "cls": "chunk"}),
                _sse({"type": "chunk", "content": "llo", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "Hello", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )
        assert [(m["role"], m["content"]) for m in slot.messages] == [("assistant", "Hello")]
        # The window rewrite is only half the release: append put the SAME dict
        # in the pending queue, so a leak there would strand every token.
        assert [r for r in slot._pending if r.get("role") == "chunk"] == []

    async def test_a_stop_row_between_chunks_and_final_does_not_strand_them(self, tmp_path):
        """A mid-stream stop must not leave the streamed deltas rendering twice.

        A stop pressed on the peer relays a ``system`` stop_event row that lands
        between the trailing chunks and the finalized ``assistant`` row (its dict
        ``cls`` is stripped crossing the relay, so it is a plain ``system`` row).
        The finalize walk must step OVER it and still drop the chunk deltas, or the
        answer renders twice — once streamed, once finalized (opus #7693).
        """
        state = _make_state(tmp_path)
        slot = _remote_slot()
        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "He", "cls": "chunk"}),
                _sse({"type": "chunk", "content": "llo", "cls": "chunk"}),
                _sse({"type": "system", "content": "🛑 stopped", "cls": "msg msg-sys"}),
                _sse({"type": "assistant", "content": "Hello", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )
        # The stop row is kept; the streamed chunks are dropped; the finalized
        # answer appears exactly once.
        assert [(m["role"], m["content"]) for m in slot.messages] == [
            ("system", "🛑 stopped"),
            ("assistant", "Hello"),
        ]
        assert [r for r in slot._pending if r.get("role") == "chunk"] == []

    async def test_a_turn_always_ends_with_chat_done(self, tmp_path):
        """Without it the composer stays blocked and the session looks hung."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        await relay_remote_turn(state, slot, "hi", chunks=_stream(b"data: [DONE]\n\n"))
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    @pytest.mark.parametrize(
        "chunks_factory",
        [
            pytest.param(lambda: _stream(b"data: [DONE]\n\n"), id="completed"),
            pytest.param(lambda: _stream(), id="truncated"),
        ],
    )
    async def test_the_turn_is_persisted_before_the_composer_unblocks(
        self, tmp_path, monkeypatch, chunks_factory
    ):
        """The rows this turn produced must be saved, and saved BEFORE ``chat_done``.

        Everything ``_apply_row`` appends lands in the in-memory window only. The
        periodic flush skips non-dirty slots and runs on its own schedule, so a
        crash after the composer unblocks but before that flush loses a transcript
        this machine owns — the peer keeps no copy to re-read. The local turn path
        saves explicitly for the same reason; the relay replaces that path rather
        than wrapping it, so it has to do the same thing itself.

        Ordering is the assertion, not merely that a save happened: ``chat_done``
        is what tells the user the turn is finished, so a save after it is a save
        the user is already entitled to assume completed. Both a completed stream
        and a truncated one are checked — the truncation writes an error row, which
        is equally worth keeping.
        """
        state = _make_state(tmp_path)
        order: list[str] = []
        state.broadcast_ws = MagicMock(side_effect=lambda event, *a, **k: order.append(event))
        saved: list[str] = []

        async def _fake_save(_state, saved_slot, **kwargs):
            order.append("save")
            saved.append(saved_slot.key)
            return True

        monkeypatch.setattr(remote_relay, "save_slot_off_loop", _fake_save)
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi", chunks=chunks_factory())

        # Two saves: the in-flight marker persisted BEFORE the stream opens (so a
        # gateway crash mid-turn is detectable on reload) and the durability save
        # in the finally. Both are this slot, and the LAST save still lands before
        # chat_done — the ordering the composer relies on.
        assert saved == [slot.key, slot.key]
        last_save = max(i for i, e in enumerate(order) if e == "save")
        assert last_save < order.index("chat_done")

    async def test_a_failing_stream_yields_an_error_row_and_still_finishes(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        async def _boom() -> AsyncIterator[bytes]:
            yield _sse({"type": "chunk", "content": "par", "cls": "chunk"})
            raise ConnectionResetError("tunnel died")

        await relay_remote_turn(state, slot, "hi", chunks=_boom())
        assert slot.messages[-1]["role"] == "error"
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_a_stream_that_ends_without_the_terminator_is_not_a_success(self, tmp_path):
        """EOF is not completion, and the difference is invisible to the reader.

        A dropped tunnel, a peer that died mid-turn, or a proxy that closed the
        response early all end the iterator cleanly with no ``[DONE]``. Treating
        that as a finished turn broadcast ``chat_done`` over a transcript that
        stops mid-sentence, so a truncated answer read as a complete one with
        nothing anywhere saying otherwise.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(_sse({"type": "chunk", "content": "half an ans", "cls": "chunk"})),
        )

        assert slot.messages[-1]["role"] == "error"
        assert "incomplete" in slot.messages[-1]["content"]
        # Still unblocked: a truncation is reported, not left hanging.
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_an_empty_stream_is_not_a_success(self, tmp_path):
        """The degenerate case of the same defect: nothing arrived at all."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi", chunks=_stream())

        assert slot.messages[-1]["role"] == "error"

    async def test_rows_after_the_terminator_are_not_replayed(self, tmp_path):
        """The terminator still ends the replay, exactly as the early return did.

        Tracking it with a flag instead of returning must not turn ``[DONE]`` into
        a mere marker: anything a peer sends after it belongs to no turn here.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "done", "cls": "chunk"}),
                b"data: [DONE]\n\n",
                _sse({"type": "chunk", "content": "after", "cls": "chunk"}),
            ),
        )

        chunk_calls = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        assert [c.args[1]["content"] for c in chunk_calls] == ["done"]
        assert all(m["role"] != "error" for m in slot.messages)


# ── Metadata round-trip ────────────────────────────────────────────────────────


class TestBindingPersistence:
    def test_the_binding_survives_a_save_and_rehydrate(self, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        # Drop the in-memory slot so the rehydrate reads from disk rather than
        # returning the live object it would otherwise find in the registry.
        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert restored.executor == "remote"
        assert restored.instance_id == "nobita"
        assert restored.remote_slot == "peer-chat-9"
        assert restored.is_remote is True

    def test_the_empty_window_merge_persists_a_complete_binding(self, tmp_path):
        """The window is empty for the whole gap before the first relayed row.

        A peer-bound newborn has no messages until the relay appends one, so the
        empty-window metadata merge is the only writer its binding sees. If that
        path skips the three fields, a restart inside that gap brings the session
        back as an ordinary local one and the next turn runs here instead of on
        the crew the user picked.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        # Materialize the metadata line first: the merge only ever updates an
        # existing record, so a slot that never had one has nothing to reconcile.
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        slot.messages.clear()
        _save_slot_to_history(state, slot, force=True)

        meta = state.conversation_log.get_metadata("dashboard:chat-1")
        assert meta.get("executor") == "remote"
        assert meta.get("instance_id") == "nobita"
        assert meta.get("remote_slot") == "peer-chat-9"

    def test_the_empty_window_merge_writes_no_half_binding(self, tmp_path):
        """The empty-window merge must not persist a half binding it holds in memory.

        A complete binding round-trips through save unchanged; an in-memory slot
        that is only partially bound (target missing) is not written as a ``remote``
        marker, so the merge is never the writer of a marker-without-target. (An
        on-disk file that IS truncated to that shape now fails CLOSED on rehydrate —
        see ``test_an_incomplete_stored_binding_fails_closed_not_open`` below.)
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"  # no remote_slot: the peer open never landed
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)

        slot.messages.clear()
        _save_slot_to_history(state, slot, force=True)

        meta = state.conversation_log.get_metadata("dashboard:chat-1")
        assert "executor" not in meta
        assert "instance_id" not in meta

    def test_an_incomplete_stored_binding_fails_closed_not_open(self, tmp_path):
        """A truncated write / hand-edit that keeps the ``remote`` marker but loses
        its target must FAIL CLOSED, not resurrect as a local session.

        The old behaviour dropped the marker and came back local — but a local slot
        runs its next turn on THIS machine, which for a session the user bound to a
        remote crew is silent wrong-host execution (GPT #7693). The marker is kept
        instead, so the incomplete-binding guard in ``api_chat`` (409
        ``remote_binding_incomplete``) and the ``_run_chat`` chokepoint refuse the
        send with a message the user can act on.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("assistant", "hello", "msg msg-a")
        _save_slot_to_history(state, slot, force=True)
        path = state.conversation_log._path("dashboard:chat-1")
        lines = path.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["executor"] = "remote"  # marker only, no instance_id / remote_slot
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n")

        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        # Marker preserved (fail closed); the binding is incomplete so is_remote is
        # False, which is exactly what the incomplete-binding guard keys on.
        assert restored.executor == "remote"
        assert restored.is_remote is False


# ── Authorization before the peer is touched ───────────────────────────────────


def _create_app(state, *, app_name: str = "", user: str = "local-app"):
    """The real create handler behind a TestServer, optionally app-authenticated.

    Middleware sets ``request["app"]`` and ``request["user"]`` in production;
    setting them in a wrapper keeps the handler itself — and therefore the
    ordering under test — real.

    *user* defaults to ``local-app`` because the binding's first gate is a
    POSITIVE owner assertion (``is_owner_dashboard_request``): with no configured
    owner_id only the local dashboard subjects pass it, so a bare truthy user is
    an authenticated NON-owner and is refused. That is what
    ``test_a_non_owner_identity_cannot_bind_a_session_to_a_crew`` drives.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        request["user"] = user
        return await api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", handler)
    return app


class TestBindingAuthorization:
    """A refused create must not have already written to the peer.

    ``create_peer_slot`` is a write on ANOTHER machine, spending the owner's
    tunnel credential. Both gates here are about ORDER, not about the verdict:
    the handler already refused these callers, but only after opening a session
    over there and — on the existing-slot path — after stamping the binding onto
    a slot it does not own. Asserting the verdict alone would pass with the side
    effects intact, so every test asserts the peer was never called.
    """

    @pytest.fixture
    def peer(self, monkeypatch):
        """A spy standing in for the peer write, so a call is observable."""
        spy = AsyncMock(return_value="peer-chat-9")
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.create_peer_slot", spy)
        return spy

    async def test_a_non_owner_identity_cannot_bind_a_session_to_a_crew(self, tmp_path, peer):
        """Authenticated is not the same as authorized on this route.

        A messaging identity admitted by an allow-list holds a dashboard
        credential whose subject is not the owner and whose ``app`` claim is
        EMPTY — so the app gate passes it. The peer write it would reach spends
        the OWNER's manager-held tunnel credential, which is why the gate is a
        positive owner assertion rather than the absence of an app claim.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        app = _create_app(state, user="slack:U123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots", json={"instance_id": "nobita"})
            assert resp.status == 403
        peer.assert_not_awaited()

    async def test_an_unbound_create_is_untouched_by_the_owner_gate(self, tmp_path, peer):
        """The gate gates the BINDING, not the endpoint.

        A plain local create carries no ``instance_id``, reaches no peer and
        spends no tunnel credential, so a non-owner dashboard caller keeps the
        session-creation it has always had. Scoping the gate to the peer path is
        the whole reason it sits inside ``if instance_id:``.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, user="slack:U123"))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "chat-local"})
            assert resp.status == 200
        peer.assert_not_awaited()

    async def test_an_app_token_cannot_bind_a_session_to_a_crew(self, tmp_path, peer):
        """Binding is a human act from the composer's crew picker.

        An app token has no surface for it, so the request is refused outright
        rather than allowed to spend the user's peer credential unattended.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post("/api/chat/slots", json={"instance_id": "nobita"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        peer.assert_not_awaited()

    async def test_an_app_token_refusal_names_no_slot(self, tmp_path, peer):
        """One code for both refusal reasons, so it cannot be an existence oracle."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "slot_not_found"
            assert "chat-1" not in json.dumps(body)
        peer.assert_not_awaited()

    async def test_an_already_bound_slot_is_not_rebound(self, tmp_path, peer):
        """`name` can address an EXISTING slot, and rebinding it is destructive.

        The old binding's peer session keeps running with no local reader, and
        the caller silently takes over a session someone else placed on a crew.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "shizuka"
        slot.remote_slot = "peer-chat-1"
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_already_bound"
        peer.assert_not_awaited()
        # The original binding is intact — the refusal changed nothing.
        assert slot.instance_id == "shizuka"
        assert slot.remote_slot == "peer-chat-1"

    async def test_an_existing_local_slot_is_not_converted_to_remote(self, tmp_path, peer):
        """The destructive half of the same hole: local -> remote is not a create.

        The transcript stays here while EXECUTION moves to an empty peer slot, so
        the next turn runs with none of the conversation on screen. A binding is
        only ever stamped at birth, so the whole existing-slot path is refused.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "the context that would stop being in play", "msg msg-u")
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_already_bound"
        peer.assert_not_awaited()
        # Still an ordinary local session, and still the one the user was reading.
        assert slot.executor == "local"
        assert slot.instance_id == ""
        assert slot.is_remote is False
        assert len(slot.messages) == 1

    async def test_a_dashboard_caller_binds_a_new_slot(self, tmp_path, peer):
        """The allowed path, so the gates above are not just refusing everything."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["executor"] == "remote"
        assert body["instance_id"] == "nobita"
        # The peer's slot key is bound server-side but never projected.
        assert state._slots["chat-1"].remote_slot == "peer-chat-9"
        peer.assert_awaited_once()

    async def test_a_peer_that_refuses_leaves_no_local_slot_bound(self, tmp_path, monkeypatch):
        """Peer first, local slot second: a failure over there creates nothing here."""
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.create_peer_slot",
            AsyncMock(side_effect=RemoteTurnError("crew is on an older version")),
        )
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 502
            body = await resp.json()
        assert body["code"] == "remote_bind_failed"
        assert "older version" in body["error"]
        assert "chat-1" not in state._slots

    async def test_an_app_token_may_still_create_an_ordinary_local_slot(self, tmp_path, peer):
        """The gate is scoped to `instance_id`, not a blanket app refusal."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state, app_name="notes"))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "chat-1"})
            assert resp.status == 200
            assert (await resp.json())["executor"] == "local"
        peer.assert_not_awaited()

    @pytest.mark.parametrize(
        "body, code",
        [
            ({"mode": "bogus"}, "invalid_mode"),
            ({"memory_mode": "bogus"}, None),
            # A crew-bound create carrying ANY non-plain mode is refused before the
            # peer write (F3): the mode guard fires ahead of the crew-capable-name
            # check, so a crew-bound session can never host mode-specific work that
            # would run on THIS machine.
            ({"name": "...", "mode": "crew"}, "remote_mode_unsupported"),
        ],
        ids=["mode", "memory_mode", "remote_mode_unsupported"],
    )
    async def test_a_request_refused_for_its_body_never_reaches_the_peer(
        self, tmp_path, peer, body, code
    ):
        """A 400 must not cost the user a session on the crew.

        These three validations read only the request body, so nothing forced
        them to run after the peer write — and running them after it meant a
        malformed request opened a session over there, returned 400, and left it
        orphaned with no local slot pointing at it to ever release it. Every
        refusal an operator can trigger by hand is therefore reachable without
        touching the peer at all.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={**body, "instance_id": "nobita"})
            assert resp.status == 400
            if code:
                assert (await resp.json())["code"] == code
        peer.assert_not_awaited()

    async def test_a_member_name_never_reaches_the_peer(self, tmp_path, peer):
        """A ``member-`` name is reserved for the DM-thread endpoint.

        ``get_or_create_slot`` rejects it with a ValueError that becomes a 409 —
        but that check ran AFTER the peer write, so a
        ``{"instance_id": …, "name": "member-…"}`` create opened a peer session
        and only then 409'd locally, orphaning the peer slot with nothing here to
        release it (opus #7693). The reservation now runs among the pre-peer
        gates, so the refusal is reachable without touching the crew.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "member-alice", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_slot_reserved"
        peer.assert_not_awaited()
        assert "member-alice" not in state._slots

    @pytest.mark.parametrize("bad_name", [123, ["a"], {"k": "v"}], ids=["int", "list", "dict"])
    async def test_a_non_string_name_never_reaches_the_peer(self, tmp_path, peer, bad_name):
        """A name the slot store cannot key on must not cost a peer session.

        `get_or_create_slot` normalizes the key with string operations, so a
        non-string name raised out of the handler as a 500 — and it did so AFTER
        the peer write, which is the one refusal shape that leaves a session
        running over there with nothing local to release it. Coercing the name up
        front makes the request either succeed or fail before the peer is touched.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": bad_name, "instance_id": "nobita"}
            )
            # Either outcome is acceptable; an unhandled 500 is not.
            assert resp.status != 500
        if peer.await_count:
            # If the peer WAS reached, the create must have completed — a bound
            # slot exists locally, so the peer session is not orphaned.
            assert any(s.is_remote for s in state._slots.values())

    async def test_a_name_taken_while_the_peer_was_awaited_is_not_rebound(
        self, tmp_path, monkeypatch
    ):
        """The existing-slot gate is a check; `create_peer_slot` is an await.

        A concurrent create can take the name INSIDE that window, so
        `get_or_create_slot` hands back a session that already existed and has a
        transcript. Stamping the binding onto it is the destructive half the gate
        exists to prevent — the transcript stays here while execution moves to an
        empty peer slot, so the next turn runs with none of the conversation on
        screen. The race must lose to the existing session, not to the binding.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)

        async def _create_peer_slot_racing(state_arg, instance_id, **kwargs):
            # Stand in for the peer round-trip: while it is "in flight", another
            # caller mints the very name this request checked was free.
            slot = state_arg.get_or_create_slot("chat-1")
            slot.append("user", "the context that would stop being in play", "msg msg-u")
            return "peer-chat-9"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.create_peer_slot", _create_peer_slot_racing
        )
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_already_bound"

        # The session the racing caller created is untouched: still local, still
        # holding its message, never pointed at the peer.
        raced = state._slots["chat-1"]
        assert raced.executor == "local"
        assert raced.instance_id == ""
        assert raced.remote_slot == ""
        assert raced.is_remote is False
        assert len(raced.messages) == 1

    async def test_an_invalid_folder_project_never_reaches_the_peer(
        self, tmp_path, peer, monkeypatch
    ):
        """Same ordering rule for the one refusal that needs a folder read.

        Resolving a folder's project directory is I/O, not a body check, so it
        was the refusal most easily left on the far side of the peer write.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        state._folders.append({"id": "f1", "name": "Folder"})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._resolve_folder_project_dir",
            lambda folders, folder_id: ("", "no such directory"),
        )
        async with TestClient(TestServer(_create_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"instance_id": "nobita", "folder_id": "f1"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "folder_project_invalid"
        peer.assert_not_awaited()


class TestBoundCreateDefaults:
    """What a bound session records when the user picked nothing.

    A local create stamps THIS machine's default agent so the sidebar names what
    will actually answer. For a peer-bound create that reasoning inverts: the
    default names a crew from this machine's roster, the peer applies its own, and
    stamping ours would make the shelf advertise one crew while another answered.
    """

    @pytest.fixture
    def peer(self, monkeypatch):
        spy = AsyncMock(return_value="peer-chat-9")
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.create_peer_slot", spy)
        return spy

    @pytest.fixture
    def local_default(self, monkeypatch):
        """A config whose default agent exists only on THIS machine."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load",
            staticmethod(
                lambda: SimpleNamespace(
                    default_agent="local-only-crew",
                    dashboard=SimpleNamespace(default_project=""),
                )
            ),
        )

    async def test_a_bound_create_records_no_agent(self, tmp_path, peer, local_default):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (
                await client.post(
                    "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
                )
            ).json()

        assert body["agent"] == ""
        # And nothing was asked of the peer either: an omitted agent is how the
        # peer is told to apply its own default.
        assert peer.await_args.kwargs == {"agent": "", "model": ""}

    async def test_a_local_create_still_stamps_the_default(self, tmp_path, peer, local_default):
        """The counterpart, so the skip above is scoped to the binding."""
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (await client.post("/api/chat/slots", json={"name": "chat-1"})).json()

        assert body["agent"] == "local-only-crew"

    async def test_an_explicit_pick_is_recorded_and_forwarded(self, tmp_path, peer, local_default):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_create_app(state))) as client:
            body = await (
                await client.post(
                    "/api/chat/slots",
                    json={"name": "chat-1", "instance_id": "nobita", "agent": "peer-crew"},
                )
            ).json()

        assert body["agent"] == "peer-crew"
        assert peer.await_args.kwargs["agent"] == "peer-crew"


class TestRemotePickApplication:
    """Mirror AFTER the peer took the pick, never before.

    The local field is what the header renders and what the next turn reports, so
    writing it first would leave the user looking at a pick the peer refused.
    """

    @pytest.fixture
    def forward(self, monkeypatch):
        """Returns ``{}``: the peer's accepted state, empty when it reports none."""
        spy = AsyncMock(return_value={})
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.forward_peer_selection", spy)
        return spy

    async def test_an_agent_pick_mirrors_the_workspace_the_peer_committed(
        self, tmp_path, monkeypatch
    ):
        """The peer resolves the agent against ITS bindings and answers with the
        workspace that resolution chose — the same derivation the local switch
        does. Ignoring it left this slot naming a workspace the machine running
        the turns had already moved off, so the header and the persisted record
        disagreed with the only side that executes anything.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "agent": "reviewer", "workspace": "peer-ws"}),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "stale-local"

        resp = await _apply_remote_pick(
            _owner_request(state), state, slot, "agent", {"agent": "reviewer"}
        )

        assert resp.status == 200
        assert slot.agent == "reviewer"
        assert slot.workspace == "peer-ws"

    async def test_a_peer_that_names_no_workspace_changes_nothing(self, tmp_path, forward):
        """Only a non-empty string is taken, so an older or terser peer is safe:
        a missing field leaves the local value alone rather than blanking it."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "kept"

        await _apply_remote_pick(_owner_request(state), state, slot, "agent", {"agent": "reviewer"})

        assert slot.workspace == "kept"

    async def test_only_an_agent_pick_moves_the_workspace(self, tmp_path, monkeypatch):
        """A model or effort pick does not re-resolve bindings, so a `workspace`
        key in its reply is not a commitment this slot should adopt."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "workspace": "not-a-commitment"}),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.workspace = "kept"

        await _apply_remote_pick(_owner_request(state), state, slot, "model", {"model": "opus"})

        assert slot.workspace == "kept"

    async def test_a_failed_persist_rearms_the_flush_and_still_reports_success(
        self, tmp_path, forward
    ):
        """A swallowed write must not be silently final.

        The peer committed the pick, so the local write is the side that failed:
        marking the slot dirty makes the periodic flush retry it. The response
        stays 2xx on purpose — the pick DID apply on the machine that runs the
        turns, so reporting failure would roll the header back to a value the peer
        no longer holds.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        state.conversation_log = MagicMock()
        state.conversation_log.update_metadata.side_effect = OSError("history lock timeout")
        slot = _remote_slot()
        slot._dirty = False

        resp = await _apply_remote_pick(
            _owner_request(state), state, slot, "model", {"model": "opus"}
        )

        assert resp.status == 200
        assert slot.model == "opus"
        assert slot._dirty is True

    async def test_the_mirrored_workspace_is_persisted_with_the_agent(self, tmp_path, monkeypatch):
        """One write, or a restart restores the pair inconsistent — an agent from
        the peer next to a workspace the peer never agreed to."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "workspace": "peer-ws"}),
        )
        state = _make_state(tmp_path)
        state.conversation_log = MagicMock()
        slot = _remote_slot()

        await _apply_remote_pick(_owner_request(state), state, slot, "agent", {"agent": "reviewer"})

        written = state.conversation_log.update_metadata.call_args.args[1]
        assert written == {"agent": "reviewer", "workspace": "peer-ws"}

    async def test_an_accepted_pick_is_mirrored_on_the_slot(self, tmp_path, forward):
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()

        resp = await _apply_remote_pick(
            _owner_request(state), state, slot, "agent", {"agent": "reviewer"}
        )

        assert resp.status == 200
        assert json.loads(resp.body.decode()) == {
            "ok": True,
            "agent": "reviewer",
            "remote": True,
        }
        assert slot.agent == "reviewer"

    async def test_a_refused_pick_leaves_the_slot_untouched(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(side_effect=RemoteTurnError("that crew has no such agent")),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.agent = "coder"

        resp = await _apply_remote_pick(
            _owner_request(state), state, slot, "agent", {"agent": "reviewer"}
        )

        assert resp.status == 502
        body = json.loads(resp.body.decode())
        assert body["code"] == "remote_pick_failed"
        assert "no such agent" in body["error"]
        assert slot.agent == "coder"

    async def test_a_model_pick_bumps_the_pick_generation(self, tmp_path, forward):
        """Same reason the local path bumps it: an explicit pick has to outrank
        the model-fallback restore probe, which would otherwise treat the pick as
        automatic backfill and override it."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        before = slot._model_pick_gen

        await _apply_remote_pick(_owner_request(state), state, slot, "model", {"model": "opus"})

        assert slot.model == "opus"
        assert slot._model_pick_gen == before + 1

    @pytest.mark.parametrize(
        "control,value",
        [
            ("agent", "reviewer"),
            ("model", "opus"),
            ("workspace", "peer-ws"),
            ("reasoning_effort", "high"),
        ],
    )
    async def test_an_accepted_pick_is_persisted_immediately(
        self, tmp_path, forward, control, value
    ):
        """The peer committed the value when it answered; the two ends must agree.

        Leaving this to the periodic flush opens a window in which a restart
        restores a local field the crew no longer agrees with — and the crew is the
        side that runs the next turn. A purely local pick can only ever disagree
        with itself, which is why the local model/effort/workspace routes can leave
        it to the flush and this one cannot.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"

        await _apply_remote_pick(_owner_request(state), state, slot, control, {control: value})

        assert state.conversation_log.get_metadata("dashboard:chat-1").get(control) == value

    async def test_a_refused_pick_persists_nothing(self, tmp_path, monkeypatch):
        """The write is downstream of the peer's acceptance, like the mirror."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(side_effect=RemoteTurnError("that crew has no such agent")),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"

        await _apply_remote_pick(_owner_request(state), state, slot, "agent", {"agent": "reviewer"})

        assert "agent" not in state.conversation_log.get_metadata("dashboard:chat-1")

    async def test_a_workspace_pick_does_not_rewrite_the_local_project(self, tmp_path, forward):
        """``project`` is a path on THIS machine (file search, @-mentions).

        Deriving it from the peer's workspace name would write a local directory
        that has nothing to do with the workspace the turn actually runs in.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.project = "/local/project"

        await _apply_remote_pick(
            _owner_request(state), state, slot, "workspace", {"workspace": "peer-ws"}
        )

        assert slot.workspace == "peer-ws"
        assert slot.project == "/local/project"


# ── Talking to the peer ────────────────────────────────────────────────────────


class _FakeUpstream:
    """The async-context-manager reply shape ``proxy_request`` yields."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = SimpleNamespace(read=AsyncMock(return_value=body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _mgr_returning(status: int, body: bytes):
    """An instances manager whose proxy answers once with *status* / *body*."""
    mgr = MagicMock()
    mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
    mgr.proxy_request = MagicMock(return_value=_FakeUpstream(status, body))
    return mgr


class TestCreatePeerSlot:
    async def test_the_peers_slot_key_is_returned(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b'{"key": "peer-chat-9"}')

        assert await create_peer_slot(state, "nobita") == "peer-chat-9"

    async def test_no_agent_is_sent_to_the_peer(self, tmp_path):
        """The peer has its own roster, its own default and its own projects.

        Naming this machine's default would either fail there or silently bind a
        different crew than the name implies; letting the peer choose is the
        whole point of the session running on it.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita")

        _, kwargs = mgr.proxy_request.call_args
        assert json.loads(kwargs["data"]) == {}

    async def test_an_explicit_pick_rides_the_create(self, tmp_path):
        """A pick made in the crew picker comes from the PEER's roster.

        It travels with the create rather than in a follow-up call: a second
        round-trip can fail after the peer session already exists, which would
        leave a bound session running a crew the user did not choose.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita", agent="reviewer", model="opus")

        _, kwargs = mgr.proxy_request.call_args
        assert json.loads(kwargs["data"]) == {"agent": "reviewer", "model": "opus"}

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"agent": "reviewer"}, {"agent": "reviewer"}),
            ({"model": "opus"}, {"model": "opus"}),
            ({"agent": "", "model": ""}, {}),
        ],
    )
    async def test_only_the_named_picks_are_sent(self, tmp_path, kwargs, expected):
        """An empty pick is OMITTED, not sent as "".

        Sending ``{"agent": ""}`` is a different request from sending nothing: the
        peer would read it as an explicit blank rather than "apply your default".
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        state.instances_manager = mgr

        await create_peer_slot(state, "nobita", **kwargs)

        assert json.loads(mgr.proxy_request.call_args.kwargs["data"]) == expected

    async def test_a_gateway_without_instances_refuses(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = None

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "not available" in str(excinfo.value)

    async def test_a_version_skewed_peer_is_never_asked_to_open_a_slot(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"key": "peer-chat-9"}')
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError):
            await create_peer_slot(state, "nobita")
        mgr.proxy_request.assert_not_called()

    async def test_a_refusing_peer_reports_its_status(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(503, b"{}")

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "503" in str(excinfo.value)

    async def test_an_unreachable_peer_says_so_without_leaking_the_cause(self, tmp_path):
        """The transcript is a surface the user copies out of.

        Only the exception TYPE reaches the log; the message names no port, no
        credential and no library internals.
        """
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("port 17777 refused"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "17777" not in str(excinfo.value)
        assert "Reconnect" in str(excinfo.value)

    async def test_an_oversized_reply_is_refused_rather_than_decoded(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"x" * (64 * 1024 + 8))

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "oversized" in str(excinfo.value)

    async def test_a_malformed_reply_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"{not json")

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "malformed" in str(excinfo.value)

    @pytest.mark.parametrize("body", [b"{}", b'{"key": ""}', b'{"key": 7}', b"[]"])
    async def test_a_reply_that_names_no_slot_is_refused(self, tmp_path, body):
        """A missing key would otherwise bind the session to the empty string."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, body)

        with pytest.raises(RemoteTurnError) as excinfo:
            await create_peer_slot(state, "nobita")
        assert "did not name it" in str(excinfo.value)


class TestForwardPeerSelection:
    """A header pick has to land on the machine that answers the turn.

    The pickers for a bound session are populated from the PEER's rosters, so a
    pick applied only locally would name an agent, model or workspace the peer
    never hears about — the header would show one thing and the reply would come
    from another, with nothing reporting a problem.
    """

    @pytest.mark.parametrize(
        "control,body,segment",
        [
            ("agent", {"agent": "reviewer"}, "agent"),
            ("model", {"model": "opus"}, "model"),
            ("workspace", {"workspace": "main"}, "workspace"),
            ("reasoning_effort", {"reasoning_effort": "high"}, "reasoning-effort"),
        ],
    )
    async def test_each_control_reaches_the_peers_own_slot(self, tmp_path, control, body, segment):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b'{"ok": true}')
        state.instances_manager = mgr

        await forward_peer_selection(state, _remote_slot(), control, body)

        args, kwargs = mgr.proxy_request.call_args
        # The PEER's slot key, and the peer's route spelling — the local key
        # addresses nothing over there, and `reasoning_effort` is hyphenated in
        # the URL while the body key is not.
        assert args[2] == f"api/chat/slots/peer-chat-9/{segment}"
        assert json.loads(kwargs["data"]) == body

    async def test_the_peers_accepted_state_is_returned_not_discarded(self, tmp_path):
        """What the peer ACCEPTED is not always what was asked for.

        Its ``/agent`` route resolves the agent against its own bindings and
        answers with the workspace that resolution chose, so the caller needs the
        body to mirror it. Dropping it was how the two ends came to disagree.
        """
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(
            200, b'{"ok": true, "agent": "reviewer", "workspace": "peer-ws"}'
        )

        accepted = await forward_peer_selection(
            state, _remote_slot(), "agent", {"agent": "reviewer"}
        )

        assert accepted["workspace"] == "peer-ws"

    @pytest.mark.parametrize(
        "body",
        [b"not json at all", b'"a bare string"', b"", b"[]"],
        ids=["unparseable", "not-an-object", "empty", "array"],
    )
    async def test_a_reply_that_is_not_an_object_yields_nothing_to_mirror(self, tmp_path, body):
        """The pick SUCCEEDED over there — a reply this side cannot read must not
        turn it into a failure. An empty dict degrades to "nothing to mirror"."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, body)

        assert await forward_peer_selection(state, _remote_slot(), "model", {"model": "opus"}) == {}

    async def test_an_oversized_success_reply_is_not_decoded(self, tmp_path):
        """Same byte ceiling the refusal path enforces, and for the same reason:
        a hostile or broken peer must not be able to make the hub decode an
        unbounded body just because it answered 200."""
        from kiro_crew.dashboard.remote_relay import _MAX_PEER_SLOT_REPLY_BYTES

        state = _make_state(tmp_path)
        padding = b" " * (_MAX_PEER_SLOT_REPLY_BYTES + 1)
        state.instances_manager = _mgr_returning(200, b'{"workspace": "peer-ws"}' + padding)

        assert await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "r"}) == {}

    async def test_an_unlisted_control_is_a_programming_error(self, tmp_path):
        """A closed map, because ``control`` is interpolated into a proxied URL.

        Every caller passes a literal, so reaching this is a bug rather than a
        peer problem — and a soft failure here would read to the user as an
        offline crew.
        """
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        with pytest.raises(ValueError):
            await forward_peer_selection(state, _remote_slot(), "project", {"project": "/etc"})
        mgr.proxy_request.assert_not_called()

    async def test_a_version_skewed_peer_is_never_asked_to_apply_a_pick(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError):
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        mgr.proxy_request.assert_not_called()

    async def test_the_peers_own_refusal_is_what_the_user_reads(self, tmp_path):
        """Only the peer knows WHY: the agent was deleted there, the model is not
        available to that account, the workspace already has messages."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(409, b'{"error": "agent is pinned there"}')

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        assert str(excinfo.value) == "agent is pinned there"

    @pytest.mark.parametrize(
        "body",
        [
            b"{}",
            b"not json",
            b'{"error": 7}',
            b'["error"]',
            # Explicit id: pytest derives a node id from the VALUE, and a 64 KiB
            # body produces one that overflows Windows' 32767-character
            # environment limit — the shard dies with ValueError before the test
            # body runs, and takes the next test's teardown down with it.
            pytest.param(b"x" * (64 * 1024 + 8), id="oversized"),
        ],
    )
    async def test_an_unusable_refusal_falls_back_to_the_status(self, tmp_path, body):
        """A peer reply is untrusted: only a short ``error`` STRING is surfaced."""
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(500, body)

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "model", {"model": "x"})
        assert "500" in str(excinfo.value)

    async def test_an_overlong_peer_message_is_truncated(self, tmp_path):
        state = _make_state(tmp_path)
        detail = json.dumps({"error": "y" * 900}).encode()
        state.instances_manager = _mgr_returning(400, detail)

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "model", {"model": "x"})
        assert len(str(excinfo.value)) == 200

    async def test_an_unreachable_peer_says_so_without_leaking_the_cause(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("port 17777 refused"))
        state.instances_manager = mgr

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})
        assert "17777" not in str(excinfo.value)
        assert "Reconnect" in str(excinfo.value)

    async def test_a_gateway_without_instances_refuses(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = None

        with pytest.raises(RemoteTurnError):
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "x"})


class TestForwardPeerStop:
    async def test_a_local_slot_is_not_forwarded(self, tmp_path):
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(200, b"{}")

        assert await forward_peer_stop(state, _ChatSlot("chat-1"), False) is False

    async def test_an_accepted_stop_reports_true(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        assert await forward_peer_stop(state, _remote_slot(), False) is True
        args, kwargs = mgr.proxy_request.call_args
        # The PEER's slot key, not the local one: the local key means nothing
        # over there and would stop a different session or none at all.
        assert args[2] == "api/chat/slots/peer-chat-9/stop"
        assert kwargs["params"] is None

    async def test_a_forced_stop_carries_the_flag(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = _mgr_returning(200, b"{}")
        state.instances_manager = mgr

        await forward_peer_stop(state, _remote_slot(), True)

        assert mgr.proxy_request.call_args.kwargs["params"] == {"force": "true"}

    async def test_a_refused_stop_reports_false_rather_than_raising(self, tmp_path):
        """The caller turns False into an actionable error for the user.

        Raising here would surface as a generic 500 on a stop press, which reads
        as "the button is broken" rather than "the crew is unreachable".
        """
        state = _make_state(tmp_path)
        state.instances_manager = _mgr_returning(502, b"{}")

        assert await forward_peer_stop(state, _remote_slot(), False) is False

    async def test_an_unreachable_peer_reports_false(self, tmp_path):
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.proxy_request = MagicMock(side_effect=ConnectionResetError("tunnel died"))
        state.instances_manager = mgr

        assert await forward_peer_stop(state, _remote_slot(), False) is False


class TestPeerTurnRequest:
    async def test_the_turn_asks_the_peer_to_mirror_its_frames(self, tmp_path):
        """Without ``relay=1`` the reply carries the prose and no tool activity."""
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))

        class _Streaming(_FakeUpstream):
            def __init__(self):
                super().__init__(200, b"")
                self.content = SimpleNamespace(iter_any=self._iter)

            async def _iter(self):
                yield b"data: [DONE]\n\n"

        mgr.proxy_request = MagicMock(return_value=_Streaming())
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()

        await relay_remote_turn(state, _remote_slot(), "hi")

        args, kwargs = mgr.proxy_request.call_args
        assert args[2] == "api/chat"
        assert kwargs["params"] == {"relay": "1"}
        # The PEER's slot key, and only the message — see the known gap in the PR.
        assert json.loads(kwargs["data"]) == {"message": "hi", "slot": "peer-chat-9"}

    async def test_a_peer_that_refuses_the_turn_becomes_an_error_row(self, tmp_path):
        """The refusal must reach the transcript, not just the log.

        A silent failure leaves the user looking at a session that accepted
        their message and then said nothing.
        """
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))
        mgr.proxy_request = MagicMock(return_value=_FakeUpstream(503, b"{}"))
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi")

        assert slot.messages[-1]["role"] == "error"
        assert "503" in slot.messages[-1]["content"]
        assert [c.args[0] for c in state.broadcast_ws.call_args_list][-1] == "chat_done"

    async def test_a_version_skew_found_at_send_time_is_a_transcript_error(self, tmp_path):
        """The gate is re-checked per turn: a peer can be updated mid-session."""
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "0.0.1"))
        mgr.proxy_request = MagicMock()
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi")

        assert slot.messages[-1]["role"] == "error"
        assert "0.0.1" in slot.messages[-1]["content"]
        mgr.proxy_request.assert_not_called()


class TestRowReplayDetails:
    async def test_a_thinking_row_is_replayed_as_reasoning(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "thinking", "content": "hmm", "cls": "thinking"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [("thinking", "hmm")]
        assert [c.args[0] for c in state.broadcast_ws.call_args_list].count("chat_thinking") == 1

    async def test_a_rows_meta_survives_the_replay(self, tmp_path):
        """Tool rows carry their identity in `meta`; dropping it breaks folding."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "tool",
                        "content": "fs_read",
                        "cls": "tool",
                        "meta": {"tool_call_id": "t1"},
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        # A subset check, not equality: ``append`` mints a local ``mid`` into the
        # same dict, and the peer's id is what the frontend folds tool rows by.
        assert slot.messages[-1]["meta"]["tool_call_id"] == "t1"

    async def test_a_non_string_content_is_carried_as_json(self, tmp_path):
        """A structured row must not vanish because it is not a string."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": {"a": 1}, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert json.loads(slot.messages[-1]["content"]) == {"a": 1}

    async def test_a_row_with_no_type_is_ignored(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(_sse({"content": "orphan"}), b"data: [DONE]\n\n"),
        )

        assert slot.messages == []

    async def test_a_mirrored_frame_keyed_by_key_is_also_rewritten(self, tmp_path):
        """`slot_title` and `session_summary` name the session in `key`."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:slot_title",
                        "content": json.dumps({"key": "peer-chat-9", "title": "Deploy"}),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        titles = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "slot_title"]
        assert titles[0].args[1] == {"key": slot.key, "title": "Deploy"}

    @pytest.mark.parametrize("payload", ["{not json", '"a string"', "[1, 2]"])
    async def test_an_undecodable_mirrored_frame_is_dropped_not_raised(self, tmp_path, payload):
        """One bad frame must not end an otherwise healthy turn."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "relay:tool_call", "content": payload}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["chat_done"]
        assert slot.messages == []

    async def test_the_done_sentinel_stops_the_replay(self, tmp_path):
        """Rows after the terminator belong to no turn and must not be appended."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                b"data: [DONE]\n\n",
                _sse({"type": "assistant", "content": "late", "cls": "msg msg-a"}),
            ),
        )

        assert slot.messages == []

    async def test_a_record_with_no_data_line_is_not_a_row(self):
        assert parse_sse_record(b"event: ping\nid: 7") is None

    async def test_a_garbled_record_does_not_end_the_turn(self, tmp_path):
        """The rest of the stream is still worth replaying."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                b"data: {not json\n\n",
                b": keepalive\n\n",
                _sse({"type": "assistant", "content": "Hello", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [("assistant", "Hello")]

    async def test_only_the_trailing_run_of_chunks_is_replaced(self, tmp_path):
        """An earlier segment's finished message must survive the next finalize.

        The scan walks back from the end and stops at the first non-chunk row, so
        a two-segment turn keeps both answers instead of collapsing to the last.
        """
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "One", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "One", "cls": "msg msg-a"}),
                _sse({"type": "chunk", "content": "Two", "cls": "chunk"}),
                _sse({"type": "assistant", "content": "Two", "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert [(m["role"], m["content"]) for m in slot.messages] == [
            ("assistant", "One"),
            ("assistant", "Two"),
        ]


# A peer row carrying both shapes the local pass catches: a credential and a
# URL that would carry it off-box. The peer redacts its own copy with the same
# pass, so in the healthy case the local one is a no-op — these tests are about
# what happens when it is NOT, which is the only case that matters for a
# guarantee that has to hold on THIS side of the wire.
_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TAINTED = f"board updated {_SECRET} see http://evil.example.com/x?d={_SECRET}"


#: A syntactically valid AWS access-key id, used only as redaction bait. The peer
#: is a separate machine, so any of its strings can carry one that no local pass
#: has ever seen — that is the whole reason this boundary re-runs the chain.
_BAIT = "AKIAIOSFODNN7EXAMPLE"


class TestPeerStringRedaction:
    """Every peer-controlled string is scrubbed, not just a turn's message text.

    The relayed row content was covered from the start; these are the four
    siblings that reach a local surface by a different route — a frame's CSS
    class, the peer's refusal message, a capability label, and an accepted
    workspace name that is also PERSISTED. Each crosses the same trust boundary,
    so each runs the same chain.
    """

    async def test_a_frames_class_is_redacted_like_its_content(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "ok", "cls": f"chunk {_BAIT}"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert _BAIT not in json.dumps([list(c.args) for c in state.broadcast_ws.call_args_list])
        assert _BAIT not in json.dumps(slot.messages)

    async def test_a_peers_refusal_message_is_redacted_before_the_clamp(self, tmp_path):
        """Redacted BEFORE the 200-char clamp, or a credential survives by
        sitting past it."""
        state = _make_state(tmp_path)
        body = json.dumps({"error": "x" * 190 + " " + _BAIT}).encode()
        state.instances_manager = _mgr_returning(409, body)

        with pytest.raises(RemoteTurnError) as excinfo:
            await forward_peer_selection(state, _remote_slot(), "agent", {"agent": "r"})

        assert _BAIT not in str(excinfo.value)

    async def test_a_mirrored_workspace_is_redacted_before_it_is_persisted(
        self, tmp_path, monkeypatch
    ):
        """This one is both rendered AND written to history, so an unscrubbed
        credential here outlives the session."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={"ok": True, "workspace": f"ws-{_BAIT}"}),
        )
        state = _make_state(tmp_path)
        state.conversation_log = MagicMock()
        slot = _remote_slot()

        await _apply_remote_pick(_owner_request(state), state, slot, "agent", {"agent": "reviewer"})

        assert _BAIT not in slot.workspace
        written = state.conversation_log.update_metadata.call_args.args[1]
        assert _BAIT not in json.dumps(written)


def _assert_redacted(text: str) -> None:
    assert _SECRET not in text
    assert "[REDACTED: credential]" in text
    # The exfil marker names the host by design, so assert the marker rather
    # than the absence of the domain substring.
    assert "[REDACTED: suspicious URL to evil.example.com]" in text


class TestRelayedRedaction:
    """A relayed row reaches the same local surfaces as a locally-run one.

    The transcript, the WebSocket and the ConversationLog are shared by both
    paths, so the redaction pair a local turn applies before ``append`` has to
    be applied to peer-supplied text too — a peer that skips it (older build,
    different config, or simply compromised) must not be able to write raw
    credentials into this machine's history.
    """

    async def test_an_assistant_row_is_redacted_before_it_is_stored(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": _TAINTED, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_streamed_chunk_is_redacted_on_the_wire(self, tmp_path):
        """The chunk frame is its own broadcast, not a by-product of ``append``."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": _TAINTED, "cls": "chunk"}),
                b"data: [DONE]\n\n",
            ),
        )

        chunks = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_chunk"]
        _assert_redacted(chunks[0].args[1]["content"])
        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_thinking_row_is_redacted_on_both_surfaces(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "thinking", "content": _TAINTED, "cls": "thinking"}),
                b"data: [DONE]\n\n",
            ),
        )

        thinking = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_thinking"]
        _assert_redacted(thinking[0].args[1]["content"])
        _assert_redacted(slot.messages[-1]["content"])

    async def test_a_rows_meta_is_redacted_too(self, tmp_path):
        """`meta` holds the tool input — the field most likely to carry a secret."""
        state = _make_state(tmp_path)
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "tool",
                        "content": "fs_read",
                        "cls": "tool",
                        "meta": {"tool_call_id": "t1", "tool_input": _TAINTED},
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        meta = slot.messages[-1]["meta"]
        _assert_redacted(meta["tool_input"])
        # The recursive pass leaves non-matching strings byte-identical, so the
        # row's identity still folds.
        assert meta["tool_call_id"] == "t1"

    async def test_a_mirrored_frames_nested_strings_are_redacted(self, tmp_path):
        """The mirror is a denylist, so the frame shape is open-ended."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:tool_result",
                        "content": json.dumps(
                            {
                                "slot": "peer-chat-9",
                                "output": _TAINTED,
                                "nested": {"lines": [_TAINTED]},
                            }
                        ),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        results = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "tool_result"]
        payload = results[0].args[1]
        _assert_redacted(payload["output"])
        _assert_redacted(payload["nested"]["lines"][0])
        # Redaction runs BEFORE the key rewrite, so the local key still lands.
        assert payload["slot"] == slot.key

    async def test_clean_prose_is_passed_through_unchanged(self, tmp_path):
        """Both redactors return their input when nothing matches."""
        state = _make_state(tmp_path)
        slot = _remote_slot()
        prose = "Ratio 12:34 on port 8080 — see https://github.com/acme/widgets/pull/12"

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "assistant", "content": prose, "cls": "msg msg-a"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert slot.messages[-1]["content"] == prose


class TestPeerVersionIsNotAnEchoChannel:
    """The version-mismatch message quotes a string only the PEER controls.

    ``peer_version`` validates the reply as a non-empty ``str`` and nothing more,
    so the value interpolated into the refusal is arbitrary peer text on a path
    that ends in the user's transcript. It is scrubbed and bounded there for the
    same reason every other peer string is.
    """

    async def test_a_credential_in_the_peers_version_is_redacted(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, f"0.5.9 {_BAIT}"))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        message = str(excinfo.value)
        assert _BAIT not in message
        assert "[REDACTED: credential]" in message
        # Still actionable: the local version is what tells the user which end to
        # move, so redacting the peer's half must not cost them that.
        assert kiro_crew.__version__ in message

    async def test_an_oversized_peer_version_is_bounded(self):
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, "9" * 5000))
        with pytest.raises(RemoteTurnError) as excinfo:
            await ensure_version_parity(mgr, "nobita")
        assert "9" * 65 not in str(excinfo.value)


class TestMirroredClearCannotDestroyLocalRows:
    """A ``slot_clear`` arriving over the relay must not empty THIS transcript.

    The mirror is a denylist, so ``slot_clear`` rides it — and it is the one
    mirrored frame whose replay DESTROYS local data. Nothing about the frame
    proves the user asked for the clear: the peer decides when to send it, so
    honouring it hands whatever is on the far end of the tunnel the power to empty
    the slot's rows and (through ``_dirty``) have the emptied transcript written
    back over the user's history. That is unrecoverable and triggered off-machine,
    so the frame is dropped outright — broadcast included, which keeps the open
    view showing exactly what is persisted rather than a blank that reappears on
    the next reload.
    """

    async def test_a_mirrored_clear_leaves_the_local_rows_and_the_dirty_flag_alone(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        slot.append("user", "before the clear", "msg msg-u")
        slot.append("assistant", "also before", "msg msg-a")
        slot._dirty = False
        remote_mirror.attach(slot.key)

        await relay_remote_turn(
            state,
            slot,
            "/clear",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:slot_clear",
                        "cls": "relay:slot_clear",
                        "content": json.dumps({"slot": "peer-chat-9"}),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        assert [m["content"] for m in slot.messages] == ["before the clear", "also before"]
        # Not marked dirty either: a peer frame must not schedule a write of state
        # it was not allowed to change.
        assert slot._dirty is False
        # And not forwarded to the browser, so the view cannot show a clear the
        # stored transcript never took.
        assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["chat_done"]

    async def test_an_ordinary_mirrored_frame_leaves_the_rows_alone(self, tmp_path):
        """The clear branch must key on the frame type, not fire on every frame."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        slot.append("user", "keep me", "msg msg-u")
        remote_mirror.attach(slot.key)

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "relay:slot_title",
                        "cls": "relay:slot_title",
                        "content": json.dumps({"key": "peer-chat-9", "title": "t"}),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        assert [m["content"] for m in slot.messages] == ["keep me"]


class TestRelayedTurnDoesNotDrainLocally:
    """The relay must never hand a queued message to the LOCAL turn runner.

    A message sent during a relayed turn is queued and not executed — a known,
    documented gap. What these pin is the thing that is NOT allowed while it
    stands: routing that message through ``_start_next_queued_turn``, which has
    no ``is_remote``/``executor`` branch and dispatches ``_run_chat``, so the
    follow-up the user aimed at the crew would run on this machine instead.
    Losing a message is bad; silently running it on the wrong host with local
    tools and local credentials is worse, so the local drain stays out.
    """

    async def test_a_queued_message_is_not_handed_to_the_local_runner(self, tmp_path, monkeypatch):
        """``_start_next_queued_turn`` dispatches ``_run_chat``, so it stays unused.

        Its body carries no ``is_remote``/``executor`` branch, so calling it here
        would execute the queued message on the hub — the one outcome
        ``executor == "remote"`` exists to prevent. The message stays queued
        instead; that gap is documented at the call site.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        slot = _remote_slot()
        slot._queue.append({"id": "q1", "content": "the second send"})

        started = AsyncMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", started)

        await relay_remote_turn(state, slot, "hi", chunks=_stream(b"data: [DONE]\n\n"))

        started.assert_not_awaited()
        # Still queued, not silently discarded: whatever closes this gap has the
        # message to work with.
        assert [q["content"] for q in slot._queue] == ["the second send"]

    async def test_an_empty_queue_leaves_the_turn_end_untouched(self, tmp_path):
        """The drain must not fire the local finaliser on the common path.

        ``_finish_queue_cycle`` would append a ``done`` row and broadcast a second
        ``chat_done``; this arm already emitted its own terminator, so an empty
        queue has to be a no-op rather than a second turn end.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(state, slot, "hi", chunks=_stream(b"data: [DONE]\n\n"))

        assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["chat_done"]
        assert [m["cls"] for m in slot.messages] != ["done"]

    async def test_a_failed_relay_does_not_fall_back_to_the_local_runner(
        self, tmp_path, monkeypatch
    ):
        """A dead tunnel is the most tempting moment to run it here. Still no.

        The error paths are swallowed into an error row, so control does reach the
        tail — and a peer that just died is exactly when a local fallback looks
        like a kindness. It is not: the user asked a named crew for this work.
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        slot = _remote_slot()
        slot._queue.append({"id": "q1", "content": "the second send"})
        started = AsyncMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", started)

        async def _boom() -> AsyncIterator[bytes]:
            raise RuntimeError("tunnel died")
            yield b""  # pragma: no cover - unreachable, makes this a generator

        await relay_remote_turn(state, slot, "hi", chunks=_boom())

        assert slot.messages[-1]["cls"] == "msg msg-err"
        started.assert_not_awaited()


def _send_app(state, *, app_name: str = "", user: str = "local-app"):
    """The real ``api_chat`` handler behind a TestServer, owner-authenticated.

    Same shape as :func:`_create_app`: middleware sets ``request["app"]`` and
    ``request["user"]`` in production, so a wrapper sets them here and leaves the
    handler — and therefore the branch ordering under test — real. *user* defaults
    to a local bootstrap subject for the same reason ``_create_app``'s does: with
    no configured ``owner_id`` those are the only subjects the positive owner
    assertion admits, so any other truthy value is an authenticated NON-owner.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat import api_chat

    async def handler(request: web.Request) -> web.StreamResponse:
        request["app"] = app_name
        request["user"] = user
        return await api_chat(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/send", handler)
    return app


class TestBusyRemoteSlotRefusesInsteadOfQueueing:
    """A second send during a relayed turn must be REFUSED, not queued.

    A remote-bound slot reaches no queue drain: the drain lives inside
    ``_run_chat``, and ``relay_remote_turn`` replaces it rather than wrapping it.
    Queueing there answers ``queued: true`` for a message nothing will ever run,
    so the honest report is a refusal the user can act on. Draining it locally is
    the one thing that must NOT happen — that executes the crew's work on this
    machine (pinned separately in ``TestRelayedTurnDoesNotDrainLocally``).
    """

    async def _busy_remote_slot(self, state):
        slot = _remote_slot()
        # `running` is `task is not None and not task.done()`, so a real pending
        # task is what puts the slot on the busy branch.
        slot.task = asyncio.create_task(asyncio.sleep(30))
        state._slots[slot.key] = slot
        return slot

    async def test_a_second_send_is_refused_with_remote_turn_busy(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = await self._busy_remote_slot(state)
        try:
            async with TestClient(TestServer(_send_app(state))) as client:
                # ``api_chat`` resolves the slot from ``body["slot"]``, not the
                # path — naming it only in the URL auto-creates a fresh, idle slot
                # and the busy branch is never reached.
                resp = await client.post(
                    "/api/chat/send",
                    json={"slot": slot.key, "message": "the second send"},
                )
                assert resp.status == 409
                payload = await resp.json()
                assert payload["code"] == "remote_turn_busy"
            # Nothing queued: a queue entry here is a message with no runner.
            assert slot._queue == []
        finally:
            slot.task.cancel()

    async def test_a_busy_LOCAL_slot_still_queues(self, tmp_path):
        """The refusal is scoped to remote slots; local queueing is untouched.

        A local slot's queue IS drained by ``_run_chat``, so refusing there would
        be a regression in the ordinary path rather than a fix.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = _ChatSlot("chat-local-1")
        slot.task = asyncio.create_task(asyncio.sleep(30))
        state._slots[slot.key] = slot
        try:
            async with TestClient(TestServer(_send_app(state))) as client:
                resp = await client.post(
                    "/api/chat/send", json={"slot": slot.key, "message": "queue me"}
                )
                assert resp.status == 200
                payload = await resp.json()
                assert payload.get("queued") is True
            assert [q["content"] for q in slot._queue] == ["queue me"]
        finally:
            slot.task.cancel()


class TestRemotePicksAreSerialised:
    """Two concurrent header picks must not interleave their transaction.

    Each pick suspends at the tunnel await. Without a lock their peer write and
    their metadata write can complete in opposite orders, so a restart restores a
    value the crew does not hold — the local record naming one pick while the side
    that runs the next turn took the other.
    """

    async def test_a_second_pick_waits_for_the_first_transaction(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.conversation_log = None  # isolate the lock, not the persistence path
        slot = _remote_slot()

        order: list[str] = []
        release = asyncio.Event()

        async def _slow_forward(_state, _slot, control, body):
            order.append(f"enter:{body[control]}")
            # The first call parks here, which is where an unlocked second call
            # would slip past and land its own mutation first.
            await release.wait()
            order.append(f"exit:{body[control]}")
            return {}

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection", _slow_forward
        )

        first = asyncio.create_task(
            _apply_remote_pick(_owner_request(state), state, slot, "model", {"model": "a"})
        )
        await asyncio.sleep(0)  # let `first` reach the await inside the lock
        second = asyncio.create_task(
            _apply_remote_pick(_owner_request(state), state, slot, "model", {"model": "b"})
        )
        await asyncio.sleep(0)

        # The second pick has NOT entered the forward: it is queued on the lock.
        assert order == ["enter:a"]

        release.set()
        await asyncio.gather(first, second)

        # Whole transactions, one after the other — never interleaved.
        assert order == ["enter:a", "exit:a", "enter:b", "exit:b"]
        assert slot.model == "b"

    async def test_the_lock_is_not_the_message_window_lock(self, tmp_path):
        """`slot._lock` must stay free while a pick is in flight.

        Its own declaration forbids holding it across a multi-second network
        await, and a pick is exactly that. Holding it here would stall every
        message-window edit on the tunnel round-trip.
        """
        slot = _remote_slot()
        assert slot._remote_pick_lock is not slot._lock
        assert not slot._lock.locked()


# ── One authorization chokepoint for every peer-directed operation ─────────────


#: The three functions in this diff that actually reach the bound peer. Anything
#: that can call one of them can spend the owner's tunnel credential on the
#: owner's connected machine, so each needs an owner identity behind it.
_PEER_SINKS = frozenset({"relay_remote_turn", "forward_peer_stop", "forward_peer_selection"})

_OWNER_GATE = "deny_non_owner_remote_operation"

#: Functions that reach a peer sink WITHOUT calling the gate in their own body.
#: Each value names where their authorization lives instead, and an unlisted one
#: is a new ungated path to the owner's crew — which is the whole point of pinning
#: this set rather than the individual call sites.
_GATED_BY_THEIR_ROUTE = {
    # Takes a slot, not a request, and is also reached from session_control (whose
    # own docstring makes every non-route caller establish its own authority), so
    # the guard lives in the route that holds the credential.
    "stop_slot_turn": "api_chat_slot_stop",
    # The pick transaction's body, split out only so "the lock covers every early
    # return" stays checkable at a glance. Its one caller is the chokepoint, which
    # gates before taking the lock.
    "_apply_remote_pick_locked": "_apply_remote_pick",
}


def _chat_handlers_functions():
    """``{name: ast.FunctionDef}`` for every top-level def in ``chat_handlers``."""
    import ast
    import inspect

    from kiro_crew.dashboard import chat_handlers

    tree = ast.parse(inspect.getsource(chat_handlers))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node) -> set[str]:
    import ast

    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


class TestEveryPeerDirectedOperationIsOwnerGated:
    """The gap this closes, and the reason it is closed structurally.

    ``_deny_cross_app_slot_access`` returns ``None`` for every caller with an
    empty ``request["app"]`` — that is its contract, "dashboard users pass". But
    ``send_dashboard_link`` mints a dashboard token with ``app=""`` for an
    allow-listed messaging identity, so a NON-owner holds exactly that shape.
    Against a local slot that is the reach the link grants by design; against a
    peer-bound slot it is the owner's SSH tunnel and the owner's connected
    machine, which is the harm the create and capabilities gates were added to
    prevent.

    Per-branch guards were the wrong answer here: the send, the stop and the four
    header picks are six branches, and the reviewer handed them out one at a time.
    So the checks below pin the INVARIANT — every function that reaches the peer
    is gated, or is named as gated by its route — not six copies of one line.
    """

    def test_every_function_reaching_the_peer_is_gated_or_named(self):
        """A new ungated path to the owner's crew fails HERE, not in review.

        Adding one means either calling the gate or adding a justified entry to
        ``_GATED_BY_THEIR_ROUTE`` — a deliberate act with a written reason, which
        is the property a copied guard cannot give.
        """
        functions = _chat_handlers_functions()
        ungated = {}
        for name, node in functions.items():
            called = _called_names(node)
            if not (called & _PEER_SINKS):
                continue
            if _OWNER_GATE in called or name in _GATED_BY_THEIR_ROUTE:
                continue
            ungated[name] = sorted(called & _PEER_SINKS)
        assert ungated == {}, f"peer-directed but ungated: {ungated}"

    def test_each_deferred_gate_really_lives_in_the_route_it_names(self):
        """``_GATED_BY_THEIR_ROUTE`` must not become a place to park a gap."""
        functions = _chat_handlers_functions()
        for deferred, route in _GATED_BY_THEIR_ROUTE.items():
            assert deferred in functions, deferred
            assert route in functions, route
            assert _OWNER_GATE in _called_names(functions[route]), route

    def test_all_four_pick_routes_hand_their_request_to_the_chokepoint(self):
        """The gate is inside ``_apply_remote_pick``, so it needs the request.

        Every pick route reaches the peer through that one function, which is what
        makes a fifth control's authorization structural instead of a line the
        next handler can forget — but only while each call site actually passes
        the request it holds.
        """
        import ast

        functions = _chat_handlers_functions()
        sites = [
            (name, call)
            for name, node in functions.items()
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_apply_remote_pick"
        ]
        assert len(sites) == 4, [name for name, _ in sites]
        for name, call in sites:
            first = call.args[0]
            assert isinstance(first, ast.Name) and first.id == "request", name

    async def test_a_non_owner_cannot_send_to_a_bound_crew(self, tmp_path, monkeypatch):
        """403 before the relay, so the tunnel is never spent."""
        from aiohttp.test_utils import TestClient, TestServer

        relay = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.relay_remote_turn", relay)
        state = _make_state(tmp_path)
        slot = _remote_slot()
        state._slots[slot.key] = slot

        app = _send_app(state, user="slack:U123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "hi"})
            assert resp.status == 403
        relay.assert_not_called()

    async def test_a_non_owner_keeps_its_reach_on_a_LOCAL_slot(self, tmp_path):
        """The gate is scoped to the BINDING, exactly like the create path's.

        A dashboard link is meant to let an allow-listed identity chat; refusing
        that would be a regression in the ordinary path rather than a fix.
        """
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = _ChatSlot("chat-local-1")
        state._slots[slot.key] = slot

        app = _send_app(state, user="slack:U123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/send", json={"slot": slot.key, "message": "hi", "stream": False}
            )
            assert resp.status != 403

    async def test_a_non_owner_cannot_stop_a_bound_crew(self, tmp_path, monkeypatch):
        """The stop travels over the owner's tunnel and aborts the owner's turn."""
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat import api_chat_slot_stop

        forwarded = AsyncMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.forward_peer_stop", forwarded)
        state = _make_state(tmp_path)
        slot = _remote_slot()
        state._slots[slot.key] = slot

        async def handler(request: web.Request) -> web.Response:
            request["app"] = ""
            request["user"] = "slack:U123"
            return await api_chat_slot_stop(request)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/stop", handler)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/stop")
            assert resp.status == 403
        forwarded.assert_not_awaited()

    async def test_a_non_owner_cannot_reconfigure_a_bound_crew(self, tmp_path, monkeypatch):
        """Reconfiguring is the same credential spend as sending.

        Driven through the chokepoint itself: all four routes reach the peer here,
        so one refusal covers agent, model, reasoning effort and workspace, and
        the AST check above is what keeps that true.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        forwarded = AsyncMock(return_value={})
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.forward_peer_selection", forwarded)
        state = _make_state(tmp_path)
        slot = _remote_slot()
        slot.model = "kept"

        resp = await _apply_remote_pick(
            _FakeRequest(state, user="slack:U123"), state, slot, "model", {"model": "theirs"}
        )

        assert resp.status == 403
        forwarded.assert_not_awaited()
        # Not mirrored locally either: a refused pick must leave the header alone.
        assert slot.model == "kept"

    async def test_the_pick_chokepoint_still_lets_the_owner_through(self, tmp_path, monkeypatch):
        """The gate must not be a blanket refusal of the feature it protects."""
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
            AsyncMock(return_value={}),
        )
        state = _make_state(tmp_path)
        slot = _remote_slot()

        resp = await _apply_remote_pick(
            _owner_request(state), state, slot, "model", {"model": "ok"}
        )

        assert resp.status == 200
        assert slot.model == "ok"

    async def test_a_missing_app_claim_is_refused_on_a_bound_slot(self, tmp_path, monkeypatch):
        """An ABSENT ``app`` key means the auth middleware never ran.

        Deny-by-default there, rather than reading it as the empty dashboard
        claim: the peer path is the last place to guess about identity.
        """
        from kiro_crew.dashboard.chat_handlers import _apply_remote_pick

        forwarded = AsyncMock(return_value={})
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.forward_peer_selection", forwarded)
        state = _make_state(tmp_path)
        slot = _remote_slot()

        unauthenticated = _FakeRequest(state)
        del unauthenticated["app"]
        resp = await _apply_remote_pick(unauthenticated, state, slot, "model", {"model": "x"})

        assert resp.status == 403
        forwarded.assert_not_awaited()


class TestRelayedContextUsageFeedsTheMeterNotTheTranscript:
    """A peer ``context_usage`` reading must not be persisted as a chat row.

    It is in ``MIRROR_SKIP_EVENTS``, so it arrives as its own wire frame with the
    bare role ``context_usage`` rather than a ``relay:`` mirror. Falling through
    to the generic append wrote the raw JSON payload into the user's transcript
    once per reading, while the header meter — which reads the WS event — never
    moved.
    """

    async def test_a_relayed_reading_broadcasts_and_appends_nothing(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse(
                    {
                        "type": "context_usage",
                        "content": json.dumps(
                            {"slot": "peer-chat-9", "pct": 41.5, "used_tokens": 8300}
                        ),
                    }
                ),
                b"data: [DONE]\n\n",
            ),
        )

        # No raw JSON row in the history.
        assert slot.messages == []
        usage = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "context_usage"]
        assert len(usage) == 1
        payload = usage[0].args[1]
        assert payload["pct"] == 41.5
        # Rewritten to the LOCAL key: the peer's names a slot that does not exist
        # here, and the meter is keyed by slot.
        assert payload["slot"] == slot.key

    async def test_an_unparseable_reading_is_dropped_not_appended(self, tmp_path):
        """A missing reading leaves the meter at its last value; a JSON row in the
        transcript would be permanent."""
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "context_usage", "content": "not json at all"}),
                b"data: [DONE]\n\n",
            ),
        )

        assert slot.messages == []
        assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["chat_done"]


# ── F1: restart is surfaced, not silent ─────────────────────────────────────────


def _mode_app(state, *, app_name: str = "", user: str = "local-app"):
    """The real ``api_chat_slot_mode`` handler behind a TestServer.

    Same wrapper shape as :func:`_send_app`: the auth middleware's
    ``request["app"]``/``request["user"]`` are set here so the handler — and the
    guard ordering under test — runs unchanged.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat_folders import api_chat_slot_mode

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        request["user"] = user
        return await api_chat_slot_mode(request)

    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/mode", handler)
    return app


class TestRemoteSessionLockedWhileTunnelDown:
    """F1: a remote turn must not be dispatched into a tunnel that is not up.

    A gateway that just restarted has not re-established its instance tunnels
    yet. Firing a turn into a half-open or absent tunnel loses it — the peer
    never receives it, or answers into a stream nothing reads. The send path
    locks the session with a visible 409 until the tunnel is CONNECTED.
    """

    def test_peer_is_connected_reads_the_tunnel_state_defensively(self):
        from kiro_crew.dashboard.remote_relay import peer_is_connected

        up = SimpleNamespace(
            status=lambda _id: SimpleNamespace(state=SimpleNamespace(value="connected"))
        )
        down = SimpleNamespace(
            status=lambda _id: SimpleNamespace(state=SimpleNamespace(value="disconnected"))
        )
        no_status = SimpleNamespace(status=lambda _id: None)

        def _raises(_id):
            raise RuntimeError("manager blew up")

        raising = SimpleNamespace(status=_raises)

        assert peer_is_connected(up, "nobita") is True
        # Everything that is not a live CONNECTED tunnel reads as "not ready".
        assert peer_is_connected(down, "nobita") is False
        assert peer_is_connected(no_status, "nobita") is False
        assert peer_is_connected(raising, "nobita") is False
        assert peer_is_connected(None, "nobita") is False

    async def test_a_send_is_refused_while_the_tunnel_is_down(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = _remote_slot()
        state._slots[slot.key] = slot
        # Tunnel down, and the peer must not be reached at all while locked.
        state.instances_manager = SimpleNamespace(
            status=lambda _id: SimpleNamespace(state=SimpleNamespace(value="disconnected")),
            proxy_request=MagicMock(
                side_effect=AssertionError("peer reached while the session was locked")
            ),
        )
        async with TestClient(TestServer(_send_app(state))) as client:
            resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "hi"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_not_connected"
        # Nothing queued AND nothing recorded: the guard runs before the user-row
        # append, so a refused send leaves no local row. Were it recorded, the
        # user's retry would append a SECOND row while only the retry reached the
        # peer, diverging the local and peer transcripts (GPT #7693).
        assert slot._queue == []
        assert slot.messages == []


class TestInterruptedTurnSurvivesRestart:
    """F1: a turn in flight when the gateway crashes comes back marked, not silent."""

    async def test_relay_clears_the_in_flight_marker_when_the_turn_ends(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        await relay_remote_turn(
            state,
            slot,
            "hi",
            chunks=_stream(
                _sse({"type": "chunk", "content": "done", "cls": "chunk"}),
                b"data: [DONE]\n\n",
            ),
        )

        # A completed turn leaves no marker — the finally clears it before the
        # durability save, so a reload has nothing to recover.
        assert slot._relay_in_flight is False

    async def test_cancellation_preserves_the_in_flight_marker(self, tmp_path):
        """A cancelled relay is NOT a terminal outcome — the marker must survive.

        A graceful restart or the CHAT_TURN_TIMEOUT ceiling cancels the relay task
        while the peer keeps running detached. Clearing the marker (as the finally
        does on a terminal outcome) would let a reload present the truncated
        transcript as complete; instead the marker stays set so the interruption
        row is recovered, and no ``chat_done`` unblocks a turn that is not finished
        (GPT #7693).
        """
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()

        async def _cancel_mid_stream():
            raise asyncio.CancelledError()
            yield b""  # pragma: no cover - unreachable, makes this a generator

        with pytest.raises(asyncio.CancelledError):
            await relay_remote_turn(state, slot, "hi", chunks=_cancel_mid_stream())

        assert slot._relay_in_flight is True
        assert not any(c.args[0] == "chat_done" for c in state.broadcast_ws.call_args_list)

    def test_a_crash_mid_turn_comes_back_with_an_interrupted_row(self, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        slot.append("user", "run something long", "msg msg-u")
        # The gateway "crashed" here: the marker is set and persisted, the finally
        # that would clear it never ran.
        slot._relay_in_flight = True
        _save_slot_to_history(state, slot, force=True)

        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert restored.messages[-1]["role"] == "error"
        assert "interrupted" in restored.messages[-1]["content"].lower()
        # The runtime marker is NOT re-armed, so the next flush persists the row
        # with the flag cleared and a second restart cannot append it twice.
        assert restored._relay_in_flight is False

    def test_a_completed_turn_leaves_no_interrupted_row(self, tmp_path):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        slot.append("assistant", "all done", "msg msg-a")  # marker stays False
        _save_slot_to_history(state, slot, force=True)

        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert [m["role"] for m in restored.messages] == ["assistant"]

    def test_the_marker_is_cleared_on_disk_when_a_relay_completes(self, tmp_path):
        """A completed relay must ERASE the marker it wrote at start.

        The marker is written only while a relay runs and omitted once it ends.
        If ``relay_in_flight`` is not slot-owned, the ``true`` written at relay
        start is carried forward past a clean completion, so a later restart
        appends a false interruption row every time (GPT #7693). Owning the key
        makes its absence on the completion save clear the on-disk value.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.executor = "remote"
        slot.instance_id = "nobita"
        slot.remote_slot = "peer-chat-9"
        slot.append("user", "run something long", "msg msg-u")

        # Relay start: marker set and persisted.
        slot._relay_in_flight = True
        _save_slot_to_history(state, slot, force=True)
        # Relay end: the finally clears the marker, then persists again.
        slot.append("assistant", "all done", "msg msg-a")
        slot._relay_in_flight = False
        _save_slot_to_history(state, slot, force=True)

        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        # No interruption row despite the marker having been written mid-turn.
        assert [m["role"] for m in restored.messages] == ["user", "assistant"]
        assert restored._relay_in_flight is False


class TestRelayMirrorIsDetachedIfPrepareFails:
    """A relay reader that dies before ``prepare`` must not leak the mirror.

    ``remote_mirror.attach`` runs before dispatch; the matching ``detach`` lives
    only in the streaming loop's ``finally``. ``await resp.prepare(request)`` sits
    between them, so a peer that vanished at that instant would raise and skip the
    finally, stranding the slot in the process-global ``_MIRRORED`` set forever —
    every later frame then mirrors onto a queue nothing drains. The handler drops
    ownership on that error path so the set comes back clean.
    """

    async def test_a_prepare_failure_leaves_no_mirror_registration(self, tmp_path, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        slot = _remote_slot()
        state._slots[slot.key] = slot
        state.instances_manager = SimpleNamespace(
            status=lambda _id: SimpleNamespace(state=SimpleNamespace(value="connected"))
        )

        # Keep the dispatched turn inert: the leak is about the reader's own
        # lifecycle, not the peer round-trip.
        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.relay_remote_turn", _noop)

        async def _boom(self, _request):
            raise ConnectionResetError("reader vanished before prepare")

        monkeypatch.setattr(web.StreamResponse, "prepare", _boom)

        async with TestClient(TestServer(_send_app(state))) as client:
            # The reader is gone before a byte is sent; aiohttp drops the
            # connection, so the client raises rather than seeing a status. The
            # HTTP outcome is not the point — the server-side cleanup is.
            with contextlib.suppress(Exception):
                await client.post(
                    "/api/chat/send?relay=1",
                    json={"slot": slot.key, "message": "hi"},
                )

        # The whole assertion: attach happened, prepare raised, and detach still
        # ran — so the slot is not stranded as permanently mirrored.
        assert slot.key not in remote_mirror._MIRRORED


class TestPreStreamRefusalRollsBackTheUserRow:
    """A refusal before the peer receives the turn must not persist the prompt.

    ``api_chat`` appends the user row and THEN dispatches the relay. A pre-stream
    refusal (version skew, non-2xx, connection error) means the peer got nothing,
    so the row must be rolled back or a retry duplicates local history the peer
    never saw (GPT #7693). A mid-stream truncation is different — the peer IS
    running the turn — so that row stays.
    """

    async def test_a_pre_stream_refusal_removes_the_user_row(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        slot.append("user", "do it", "msg msg-u")  # as api_chat does, pre-dispatch
        before_total = slot.total_messages

        async def _raise_before_yield():
            raise RemoteTurnError("crew is on an older version")
            yield b""  # pragma: no cover - unreachable, makes this a generator

        await relay_remote_turn(state, slot, "do it", chunks=_raise_before_yield())

        # The unsent user row is gone (no duplicate on retry); only the error row
        # remains, and the message count nets out (-user +error).
        assert [m["role"] for m in slot.messages] == ["error"]
        assert slot.total_messages == before_total

    async def test_a_mid_stream_truncation_keeps_the_user_row(self, tmp_path):
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        slot.append("user", "do it", "msg msg-u")

        # A chunk streams (peer received + started) then the stream ends with no
        # [DONE] terminator — a truncation, not a pre-stream refusal.
        await relay_remote_turn(
            state,
            slot,
            "do it",
            chunks=_stream(_sse({"type": "chunk", "content": "partial", "cls": "chunk"})),
        )

        # The peer is running the turn, so the user row is preserved; an error row
        # is appended for the truncation.
        assert any(m["role"] == "user" for m in slot.messages)
        assert slot.messages[-1]["role"] == "error"

    async def test_a_zero_byte_2xx_stream_keeps_the_user_row(self, tmp_path):
        """A 2xx response that closes before a byte is ACCEPTANCE, not refusal.

        The peer answered 2xx and now owns the turn; the body then closing with
        zero bytes is a truncation of a turn the peer is running, not a
        pre-acceptance refusal. The earlier ``received_bytes`` gate saw no byte
        and dropped the prompt the peer had accepted (GPT #7693, this round). The
        empty acceptance sentinel ``_peer_turn_chunks`` emits right after the 2xx
        is what keeps the row here. This drives the REAL peer path, not an
        injected stream, so it locks the sentinel too.
        """
        state = _make_state(tmp_path)
        mgr = MagicMock()
        mgr.peer_version = AsyncMock(return_value=(True, kiro_crew.__version__))

        class _EmptyButAccepted(_FakeUpstream):
            def __init__(self):
                super().__init__(200, b"")
                self.content = SimpleNamespace(iter_any=self._iter)

            async def _iter(self):
                return
                yield b""  # pragma: no cover - makes this an async generator

        mgr.proxy_request = MagicMock(return_value=_EmptyButAccepted())
        state.instances_manager = mgr
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        slot.append("user", "do it", "msg msg-u")  # as api_chat does, pre-dispatch

        await relay_remote_turn(state, slot, "do it")

        # The prompt the peer accepted survives; a truncation error row is added.
        assert any(m["role"] == "user" for m in slot.messages)
        assert slot.messages[-1]["role"] == "error"


class TestRelayCarriesToolRowMeta:
    """A tool row's durable ``meta`` must survive the relay, minus the peer's mid."""

    def test_build_stream_chunk_carries_a_tool_rows_meta(self):
        from kiro_crew.dashboard.chat_utils import _build_stream_chunk

        row = {
            "role": "tool_call",
            "content": "x",
            "cls": "tool",
            "meta": {"tool": "fs_read", "call_id": "c1", "mid": "peer-mid"},
        }
        # Relay drain carries the row meta; the ordinary SSE stream does not.
        relayed = json.loads(_build_stream_chunk(row, include_row_meta=True))
        assert relayed["meta"]["tool"] == "fs_read"
        assert relayed["meta"]["call_id"] == "c1"
        assert "meta" not in json.loads(_build_stream_chunk(row))

    def test_apply_row_keeps_tool_meta_but_drops_the_peers_mid(self, tmp_path):
        from kiro_crew.dashboard.remote_relay import _apply_row, _ChunkSequencer

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        _apply_row(
            state,
            slot,
            {
                "type": "tool_result",
                "content": "ok",
                "cls": "tool",
                "meta": {"tool": "fs_read", "call_id": "c1", "mid": "peer-mid"},
            },
            _ChunkSequencer(),
        )
        row = slot.messages[-1]
        assert row["meta"]["tool"] == "fs_read"
        assert row["meta"]["call_id"] == "c1"
        # The peer's delivery id is not adopted; a local mid is minted instead.
        assert row["meta"].get("mid") != "peer-mid"


class TestRelayedSendToBusyPeerSlotIsRefused:
    """F2: on the peer, a busy slot must 409 a ``relay=1`` send, not queue it.

    A relayed turn runs this handler on the PEER against the peer's own slot — an
    ordinary LOCAL slot there, so ``is_remote`` is False. If it is busy and the
    send falls through to the queue, it drains later WITHOUT the ``relay=1``
    mirror and its answer never reaches the owner. Refusing makes the owner's
    reader raise and surface a reconnect prompt instead.
    """

    async def test_a_relayed_send_to_a_busy_local_slot_is_refused(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = _ChatSlot("chat-peer-1")  # a plain LOCAL slot, as it is on the peer
        slot.task = asyncio.create_task(asyncio.sleep(30))
        state._slots[slot.key] = slot
        try:
            async with TestClient(TestServer(_send_app(state))) as client:
                resp = await client.post(
                    "/api/chat/send?relay=1",
                    json={"slot": slot.key, "message": "relayed"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "remote_turn_busy"
            # Not queued: a queue entry here drains without the mirror.
            assert slot._queue == []
        finally:
            slot.task.cancel()


class TestRemoteSessionIsPlainChatOnly:
    """F3: a crew-bound session can never carry a non-plain mode.

    Create-time rejection is pinned by
    ``test_a_request_refused_for_its_body_never_reaches_the_peer``. This covers
    the sibling: the post-create mode switch that would otherwise reopen it.
    """

    async def test_a_remote_slot_cannot_switch_to_a_non_plain_mode(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        state = _make_state(tmp_path)
        slot = _remote_slot("chat-1")
        state._slots[slot.key] = slot
        async with TestClient(TestServer(_mode_app(state))) as client:
            resp = await client.patch("/api/chat/slots/chat-1/mode", json={"mode": "crew"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "remote_mode_unsupported"
        # The mode is left plain — the switch changed nothing.
        assert slot.mode == ""


def _turn_action_app(state, *, app_name=""):
    """The four turn-RESTARTING endpoints behind one TestServer.

    ``api_chat`` relays a plain send, but regenerate / edit-resend / rewind /
    continue carry no remote branch: each dispatches ``_run_chat`` (directly or
    via the queue) and three of them truncate + persist history first. On a bound
    slot that runs the crew's turn on THIS machine and rewrites the LOCAL
    transcript the peer never sees. The auth middleware mirrors production owner
    claims; readiness is stubbed so the guard under test is what returns.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_continue
    from kiro_crew.dashboard.chat_regenerate import (
        api_chat_slot_edit_resend,
        api_chat_slot_regenerate,
    )
    from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

    @web.middleware
    async def _auth(request, handler):
        request["app"] = app_name
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


class TestBoundSlotRefusesTurnRestartingActions:
    """A crew-bound slot must REFUSE every turn-restarting action, not run it.

    Only ``api_chat`` (plain send) knows how to relay. Regenerate, edit-resend,
    rewind, and continue dispatch a local turn, so on a bound slot they would run
    the crew's work on this machine and diverge the transcripts — and three of
    them truncate LOCAL history before dispatch, an edit the peer never sees. All
    four refuse with ``409 remote_action_unsupported`` until they learn to relay.
    """

    async def _bound_slot(self, state):
        slot = _remote_slot("chat-1")
        # A real turn to prove the refusal does NOT truncate it.
        slot.append("user", "first question", "msg msg-u")
        slot.append("assistant", "first answer", "msg msg-a")
        state._slots[slot.key] = slot
        return slot

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/chat/slots/chat-1/regenerate", None),
            ("/api/chat/slots/chat-1/edit-resend", {"index": 0, "content": "edited"}),
            ("/api/chat/slots/chat-1/rewind", {"at_message_index": 0, "content": "edited"}),
            ("/api/chat/slots/chat-1/continue", None),
        ],
    )
    async def test_each_action_is_refused_and_nothing_is_truncated(
        self, tmp_path, monkeypatch, path, body
    ):
        from aiohttp.test_utils import TestClient, TestServer

        # Readiness is orthogonal to this guard; stub it so a 503 latch cannot
        # mask the 409 under test (continue is not readiness-gated).
        async def _ok(_request):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified", _ok)
        monkeypatch.setattr("kiro_crew.dashboard.chat_rewind.reject_if_kiro_unverified", _ok)

        state = _make_state(tmp_path)
        slot = await self._bound_slot(state)
        before = list(slot.messages)

        async with TestClient(TestServer(_turn_action_app(state))) as client:
            resp = await client.post(path, json=body)
            assert resp.status == 409, f"{path} did not refuse a bound slot"
            assert (await resp.json())["code"] == "remote_action_unsupported"

        # The transcript is untouched: no truncation, no dispatched turn.
        assert slot.messages == before
        assert slot.task is None

    def test_the_helper_refuses_an_incomplete_binding_too(self):
        """Keyed on ``executor``, not ``is_remote`` — a half-open binding is still
        refused, never silently run locally."""
        from kiro_crew.dashboard.remote_relay import remote_bound_refusal

        # Fully bound.
        assert remote_bound_refusal(_remote_slot("chat-1")) is not None
        # Bound intent, incomplete triple (is_remote is False): still refused.
        half = _ChatSlot("chat-2")
        half.executor = "remote"  # instance_id / remote_slot absent
        assert half.is_remote is False
        assert remote_bound_refusal(half) is not None
        # A plain local slot passes through.
        assert remote_bound_refusal(_ChatSlot("chat-3")) is None

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/chat/slots/chat-1/rewind", {"at_message_index": 0, "content": "edited"}),
            ("/api/chat/slots/chat-1/continue", None),
        ],
    )
    async def test_a_foreign_app_gets_the_anti_enumeration_404_not_the_409(
        self, tmp_path, monkeypatch, path, body
    ):
        """The remote refusal must not precede the app-ownership 404.

        Continue and Rewind gate app ownership with a 404 that is deliberately
        indistinguishable from a missing slot (CWE-204). If the crew-bound 409
        fired first, a foreign app could tell a remote slot apart from a missing
        one — so the ownership 404 has to win (GPT #7693).
        """
        from aiohttp.test_utils import TestClient, TestServer

        async def _ok(_request):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_rewind.reject_if_kiro_unverified", _ok)

        state = _make_state(tmp_path)
        await self._bound_slot(state)  # remote slot in state, _app unset (owner-only)

        # A foreign app token: not the slot's owner, so ownership must 404.
        async with TestClient(TestServer(_turn_action_app(state, app_name="notes"))) as client:
            resp = await client.post(path, json=body)
            assert resp.status == 404, f"{path} leaked a remote slot to a foreign app"
            assert (await resp.json())["code"] == "slot_not_found"


class TestRemoteSlotNeverRunsLocally:
    """The chokepoint invariant: whatever the caller, `_run_chat` never executes a
    crew-bound slot on this machine.

    Every dispatch entry point is supposed to refuse or relay a remote slot before
    reaching the local runner, but they are many; this guard in `_run_chat` itself
    is what makes running a bound slot locally impossible regardless of caller
    (GPT #7693) — so a new entry point cannot silently reintroduce the divergence.
    """

    async def test_run_chat_refuses_a_remote_slot_before_any_execution(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _remote_slot()
        state._slots[slot.key] = slot

        await _run_chat(state, slot, "run this here")

        # An error row, a chat_done to unblock the composer, and NO assistant
        # output — the turn never executed locally.
        assert slot.messages[-1]["role"] == "error"
        assert "remote crew" in slot.messages[-1]["content"]
        assert any(c.args[0] == "chat_done" for c in state.broadcast_ws.call_args_list)
        assert not any(m["role"] == "assistant" for m in slot.messages)
