"""One identity-carrying delivery per appended row.

The dashboard's redelivery guard keys on ``meta.mid`` and DECLINES mid-less
frames rather than guessing, so any ``chat_message`` frame that ships without
the appended row's mid renders as a NEW bubble whenever the same row reaches
the client through a second door. A family of call sites appended a row and
then hand-built a second, mid-less ``broadcast_ws("chat_message", ...)`` frame
-- double-delivering the row with an identity the client cannot match
(``slot.append`` already delivers one mid-carrying frame via ``_on_message``
whenever no HTTP stream reader is active).

``append_and_surface`` is the one door: append (identity minted, single
delivery) plus a manual frame ONLY when ``_has_reader`` suppresses append's
own callback -- and that frame carries the row's ``ts`` + ``meta`` (mid
included), so over-delivery is recognisable instead of duplicating.

Red-before-green: with the fix reverted (sites hand-building unconditional
mid-less frames), test_compaction_notice_delivers_exactly_once and
test_no_reader_no_manual_frame fail on the extra/mid-less frame.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard.state import _ChatSlot, append_and_surface, row_mid


class _StateStub:
    """Records broadcast_ws calls; quacks enough for append_and_surface."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, Any]] = []

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        self.frames.append((msg_type, data))

    def chat_frames(self) -> list[dict]:
        return [d for t, d in self.frames if t == "chat_message"]


def _slot_with_callback(key: str = "s1") -> tuple[_ChatSlot, list[dict]]:
    """A slot whose _on_message deliveries are captured (the SSE door)."""
    slot = _ChatSlot(key)
    delivered: list[dict] = []
    slot._on_message = lambda _key, msg: delivered.append(msg)
    return slot, delivered


class TestAppendAndSurface:
    def test_no_reader_no_manual_frame(self) -> None:
        """Without a reader, append's own delivery is the ONLY one."""
        state = _StateStub()
        slot, delivered = _slot_with_callback()
        msg = append_and_surface(state, slot, "assistant", "hello", "msg msg-a")  # type: ignore[arg-type]
        assert state.chat_frames() == []  # no hand-built second frame
        assert len(delivered) == 1  # append's single identity-carrying delivery
        assert row_mid(delivered[0]) == row_mid(msg)
        assert row_mid(msg)  # identity was minted

    def test_reader_suppressed_frame_carries_identity(self) -> None:
        """With a reader, exactly one manual frame -- carrying the row's mid + ts."""
        state = _StateStub()
        slot, delivered = _slot_with_callback()
        slot._has_reader_flag = True
        msg = append_and_surface(state, slot, "assistant", "hello", "msg msg-a")  # type: ignore[arg-type]
        assert delivered == []  # append's callback suppressed by the reader
        frames = state.chat_frames()
        assert len(frames) == 1
        frame = frames[0]
        assert frame["meta"]["mid"] == row_mid(msg)
        assert frame["ts"] == msg["ts"]
        assert frame["slot"] == slot.key
        assert frame["cls"] == "msg msg-a"

    def test_user_row_broadcast_user_delivers_once(self) -> None:
        """A non-locally-typed user row (channel mirror, Go label) delivers once."""
        state = _StateStub()
        slot, delivered = _slot_with_callback()
        msg = append_and_surface(
            state, slot, "user", "Go", "msg msg-u", broadcast_user=True  # type: ignore[arg-type]
        )
        assert state.chat_frames() == []
        assert len(delivered) == 1
        assert row_mid(delivered[0]) == row_mid(msg)

    def test_user_row_default_stays_optimistic(self) -> None:
        """Without broadcast_user, a user row is NOT broadcast (composer rendered it)."""
        state = _StateStub()
        slot, delivered = _slot_with_callback()
        append_and_surface(state, slot, "user", "typed here", "msg msg-u")  # type: ignore[arg-type]
        assert state.chat_frames() == []
        assert delivered == []

    def test_extra_fields_ride_the_reader_frame(self) -> None:
        """extra= (e.g. kind=compaction) lands on the manual frame only."""
        state = _StateStub()
        slot, _ = _slot_with_callback()
        slot._has_reader_flag = True
        msg = append_and_surface(
            state,
            slot,
            "assistant",
            "compacted",
            "msg msg-a",
            meta={"kind": "compaction"},
            extra={"kind": "compaction"},  # type: ignore[arg-type]
        )
        frame = state.chat_frames()[0]
        assert frame["kind"] == "compaction"
        assert frame["meta"]["kind"] == "compaction"
        assert frame["meta"]["mid"] == row_mid(msg)  # mid minted alongside caller meta


class TestConvertedSites:
    def test_compaction_notice_delivers_exactly_once(self) -> None:
        """The compaction chokepoint does not double-deliver (site-level red-before).

        Old code: slot.append (one _on_message delivery) + an unconditional
        hand-built mid-less frame = two renderable copies. New: one delivery,
        mid-carrying.
        """
        from kiro_crew.dashboard.chat_utils import _append_compaction_notice

        state = _StateStub()
        slot, delivered = _slot_with_callback()
        _append_compaction_notice(state, slot, "✅ Conversation compacted.")  # type: ignore[arg-type]
        total = len(delivered) + len(state.chat_frames())
        assert total == 1, f"expected exactly one delivery, got {total}"
        only = (delivered + state.chat_frames())[0]
        meta = only.get("meta")
        assert isinstance(meta, dict) and meta.get("mid"), "delivery must carry meta.mid"
        assert meta.get("kind") == "compaction"

    def test_manual_frame_meta_carries_mid(self) -> None:
        """A deliberate manual frame ships the appended row's mid (the retired crew dispatcher was the first such site)."""
        # Mirror the site's exact sequence: append(broadcast=False) then a frame
        # built from the APPENDED row's meta (not the pre-append dict).
        state = _StateStub()
        slot, delivered = _slot_with_callback()
        meta = {"crew_reply": True}
        row = slot.append("assistant", "fwd", "msg msg-a", broadcast=False, meta=meta)
        frame_meta = row.get("meta") if isinstance(row.get("meta"), dict) else meta
        state.broadcast_ws(
            "chat_message",
            {
                "slot": slot.key,
                "role": "assistant",
                "content": "fwd",
                "cls": "msg msg-a",
                "meta": frame_meta,
                "kind": "k",
            },
        )
        assert delivered == []  # broadcast=False honored
        frame = state.chat_frames()[0]
        assert frame["meta"].get("mid") == row_mid(row)
        assert frame["meta"].get("crew_reply") is True


class TestRecapNotice:
    """The recap chokepoint mirrors the compaction one: tagged, redacted,
    empty-dropped, single identity-carrying delivery."""

    def test_recap_notice_is_tagged_and_delivered_once(self) -> None:
        from kiro_crew.dashboard.chat_utils import _append_recap_notice

        state = _StateStub()
        slot, delivered = _slot_with_callback()
        _append_recap_notice(state, slot, "Goal: X. Next: Y.")  # type: ignore[arg-type]
        assert len(delivered) == 1
        msg = delivered[0]
        assert msg.get("meta", {}).get("kind") == "recap"
        assert msg.get("meta", {}).get("mid")
        assert msg.get("content", "").startswith("🧭")
        assert "Recap — session so far: " in msg.get("content", "")
        assert "Goal: X. Next: Y." in msg.get("content", "")
        assert state.chat_frames() == []  # no hand-built duplicate frame

    def test_recap_notice_redacts_at_the_chokepoint(self) -> None:
        # Defense-in-depth: even a caller that skipped the mapping layer's
        # redaction cannot push credential text to the surface.
        from kiro_crew.dashboard.chat_utils import _append_recap_notice

        state = _StateStub()
        slot, delivered = _slot_with_callback()
        _append_recap_notice(  # type: ignore[arg-type]
            state, slot, "resume; found key AKIAIOSFODNN7EXAMPLE in output"
        )
        assert len(delivered) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in delivered[0].get("content", "")

    def test_recap_notice_empty_text_appends_nothing(self) -> None:
        from kiro_crew.dashboard.chat_utils import _append_recap_notice

        state = _StateStub()
        slot, delivered = _slot_with_callback()
        _append_recap_notice(state, slot, "   ")  # type: ignore[arg-type]
        assert delivered == []
        assert state.chat_frames() == []

    def test_recap_notice_same_text_dedupes_against_the_tail(self) -> None:
        # An unchanged recap re-emitted on every resume must not stack rows;
        # DIFFERENT text still appends (newer information wins).
        from kiro_crew.dashboard.chat_utils import _append_recap_notice

        state = _StateStub()
        slot, delivered = _slot_with_callback()
        _append_recap_notice(state, slot, "Goal: X. Next: Y.")  # type: ignore[arg-type]
        _append_recap_notice(state, slot, "Goal: X. Next: Y.")  # type: ignore[arg-type]
        assert len(delivered) == 1
        _append_recap_notice(state, slot, "Goal: X. Next: Z.")  # type: ignore[arg-type]
        assert len(delivered) == 2


def test_recap_dispatch_never_counts_as_visible_output() -> None:
    """The recap branch must not set _produced_visible_output: it fires at
    stream start on exactly the first prompt after a resume, and counting
    it would suppress the empty-response recovery ladder for that turn —
    the user gets the recap, then silence, and the give-up notice that says
    to re-send is skipped."""
    import inspect
    import re

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    m = re.search(
        r"elif event\.kind == EVENT_SESSION_RECAP:(.*?)(?=\n            elif )",
        src,
        re.DOTALL,
    )
    assert m, "recap dispatch branch not found"
    assert "_append_recap_notice(" in m.group(1)
    assert "_produced_visible_output = True" not in m.group(1)
