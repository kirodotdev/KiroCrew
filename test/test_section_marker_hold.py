"""The section-marker hold: a marker row waits in the slot's deferred-note hold for the
turn boundary it closes, and each flush seam says whether it lets markers out.

The ``section_marker`` tool that fills the hold lands separately; these tests put entries
in the hold directly, the way its applier does.
"""

import pytest

from kiro_crew.dashboard.session_directive_apply import SECTION_MARKER_ROLE


class TestAHeldMarkerIsDurableOnAFreshSlot:
    """The hold is merged into the slot's metadata line, which a fresh slot lacks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("written", [False, True], ids=["fresh-slot", "existing-line"])
    async def test_a_held_marker_on_a_fresh_slot_is_saved_when_the_merge_finds_no_line(
        self, monkeypatch, written
    ):
        """The merge writer lands only in an EXISTING metadata line, so on a fresh
        slot it reports ``written=False``. A restart before the next save then loses
        the marker, which is the case the persist exists for."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.slot_buffers import DeferredHoldOutcome

        outcome = DeferredHoldOutcome(written=written, evidence=MagicMock())
        monkeypatch.setattr(chat_runner, "persist_held_markers_sync", lambda *a: outcome)
        save = AsyncMock()
        monkeypatch.setattr(chat_runner, "save_slot_off_loop", save)
        state = MagicMock()
        slot = MagicMock(key="s", _active_turn_history_key="h")

        await chat_runner._persist_held_markers(state, slot, "before a re-queued turn", "x")

        if written:
            save.assert_not_awaited()
        else:
            save.assert_awaited_once_with(state, slot)


# ──────────────────── the REAL flush, not a stand-in ─────────────────────────


class TestRealFlushWritesTheMarkerRow:
    """``_FakeSlot.flush`` above reproduces the flush contract, which means it
    shares a blind spot with the code it stands in for: if the real
    ``flush_deferred_notes`` dropped ``role`` or ``meta``, every applier test
    would still pass. These exercise the real ``SlotBuffers.flush_deferred_notes``
    on a real ``_ChatSlot``, so the row shape is measured rather than assumed.
    """

    def _slot(self):
        from kiro_crew.dashboard.state import _ChatSlot

        return _ChatSlot("section-marker-flush")

    @staticmethod
    def _held(slot, **over):
        """A held entry authorized against the slot's LIVE session.

        The flush drops a held row whose recorded session differs, so an
        entry stamped with anything else is dropped before the write and every
        assertion below would pass vacuously on an empty transcript.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        entry = {
            "content": "— End of section: item-42 —",
            "cls": "",
            "context": None,
            "role": SECTION_MARKER_ROLE,
            "meta": {"label": "item-42"},
            "session": effective_session_key(slot),
        }
        entry.update(over)
        return entry

    def test_a_held_marker_is_written_with_its_role_and_label(self):
        slot = self._slot()
        slot._deferred_notes.append(self._held(slot))
        assert slot.flush_deferred_notes(markers_only=False) == 1
        row = slot.messages[-1]
        assert row["role"] == SECTION_MARKER_ROLE
        assert row["content"] == "— End of section: item-42 —"
        assert row["meta"]["label"] == "item-42"
        # Drained, so a second seam cannot re-write it.
        assert slot._deferred_notes == []
        assert slot.flush_deferred_notes(markers_only=False) == 0

    def test_a_marker_row_is_not_stamped_as_a_note(self):
        """``meta.noteSession`` is the surviving half of the /note wire contract:
        the frontend's ``isNoteRow`` returns true for ANY row carrying it, because
        ``cls`` does not survive the write path for a non-system role. Stamping it
        on a marker would make that predicate call the marker a note."""
        slot = self._slot()
        slot._deferred_notes.append(self._held(slot, content="— End of section —"))
        assert slot.flush_deferred_notes(markers_only=False) == 1
        assert "noteSession" not in slot.messages[-1]["meta"]

    def test_a_note_entry_keeps_its_prior_shape(self):
        """The negative control for the two above: the role/meta passthrough must
        not have changed what a /note entry — which supplies neither key — writes.
        A note is still an ``inject`` carrying ``reconcile-note`` and its
        ``noteSession`` stamp."""
        slot = self._slot()
        entry = self._held(slot, content="a note", cls="reconcile-note")
        del entry["role"]
        del entry["meta"]
        slot._deferred_notes.append(entry)
        assert slot.flush_deferred_notes(markers_only=False) == 1
        row = slot.messages[-1]
        assert row["role"] == "inject"
        assert row["cls"] == "reconcile-note"
        assert "noteSession" in row["meta"]


class TestTheFlushModeIsRequiredNotDefaulted:
    """A seam that says nothing must fail to CALL, not inherit flush-everything.

    The hold carries two element classes with opposite release policies and neither
    wrong choice fails visibly, so the guard has to be the signature: documentation and
    per-seam tests cannot reach a seam nobody has written yet.
    """

    def test_omitting_the_mode_is_a_type_error_on_both_layers(self):
        import inspect

        from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
        from kiro_crew.dashboard.state import _ChatSlot

        for owner, func in (
            ("_ChatSlot", _ChatSlot.flush_deferred_notes),
            ("SlotBufferCoordinator", SlotBufferCoordinator.flush_deferred_notes),
        ):
            param = inspect.signature(func).parameters["markers_only"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{owner}: not keyword-only"
            assert param.default is inspect.Parameter.empty, (
                f"{owner}: markers_only has default {param.default!r}; a seam that says "
                f"nothing would silently inherit it"
            )

    def test_every_production_seam_states_its_class(self):
        """Read off the tree, so a NEW seam added without a mode fails here too."""
        import pathlib

        from kiro_crew import dashboard

        root = pathlib.Path(dashboard.__file__).parent
        bare: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                code = line.split("#", 1)[0]
                if ".flush_deferred_notes()" in code:
                    bare.append(f"{path.relative_to(root).as_posix()}:{n}")
        assert not bare, f"seam(s) calling the flush with no mode: {bare}"

    #: Every production flush seam and the ``markers_only`` expression it passes, keyed
    #: by module and expression so drift does not churn it but a NEW seam must be added.
    #: Keyed by path RELATIVE to the dashboard package: the seams live in subpackages,
    #: and a bare filename cannot tell two same-named modules apart.
    EXPECTED_SEAMS = {
        "chat_api/handover_drain.py": ["markers_only=False"],
        # Both close the slot: the archive of a popped tab, and the commit of a close
        # whose turn outlived the cancel wait. No turn follows either one.
        "chat_api/slot_lifecycle.py": ["markers_only=False", "markers_only=False"],
        # Both run on an IDLE slot, ahead of the next directly started turn: the held
        # notes are owed to that turn and the marker precedes it, so all of it goes.
        "chat_api/slot_note.py": ["markers_only=False"],
        "slot_buffers.py": ["markers_only=False"],
        "chat_turn/queue_cycle.py": ["markers_only=will_synthesize"],
        "chat_turn/queue_drain.py": ["markers_only=structural_next"],
        "chat_turn/stop_recovery.py": [
            # The landing turn's pre-save flush. The empty-response re-queue is NOT
            # here: it persists the hold, because its reply is appended after it.
            "markers_only=True",
        ],
    }

    def test_the_seam_inventory_is_pinned_so_a_new_one_cannot_land_silently(self):
        """Neither wrong ``markers_only`` choice fails visibly at runtime.

        The required keyword forces an author to answer, but nothing made them answer
        CORRECTLY, and a seam that releases a marker too early relocates a boundary
        while a seam that withholds one drops it from the turn it belongs to. Pinning
        the inventory turns adding a seam into a deliberate edit here.
        """
        import pathlib
        import re

        from kiro_crew import dashboard

        root = pathlib.Path(dashboard.__file__).parent
        call = re.compile(r"\.flush_deferred_notes\(\s*(markers_only=[A-Za-z_][A-Za-z_0-9]*)")
        found: dict[str, list[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for line in path.read_text(encoding="utf-8").split("\n"):
                code = line.split("#", 1)[0]
                # The slot facade FORWARDS the caller's flag rather than deciding one,
                # so it is a pass-through, not a seam that makes a choice.
                if "markers_only=markers_only" in code:
                    continue
                for match in call.finditer(code):
                    found.setdefault(path.relative_to(root).as_posix(), []).append(match.group(1))

        normalised = {name: sorted(args) for name, args in found.items()}
        expected = {name: sorted(args) for name, args in self.EXPECTED_SEAMS.items()}
        assert normalised == expected, (
            "the flush-seam inventory moved. Record the new seam in EXPECTED_SEAMS "
            f"with the flag it must pass, and say why in its own comment: {normalised}"
        )


@pytest.mark.parametrize(
    "kind, holds",
    [
        ("synthetic_recovery", True),
        ("false_tool_blocker_replay", True),
        ("", False),
    ],
    ids=["continuation", "false-tool-blocker-replay", "user-turn"],
)
def test_the_queue_drain_holds_a_marker_for_every_recovery_kind(tmp_path, kind, holds):
    """Every recovery requeue still owes the reply a held marker closes, whichever
    kind carries it; only a real successor lets the boundary out."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import (
        FALSE_TOOL_BLOCKER_REPLAY_KIND,
        SYNTHETIC_RECOVERY_KIND,
    )
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    assert SYNTHETIC_RECOVERY_KIND == "synthetic_recovery"
    assert FALSE_TOOL_BLOCKER_REPLAY_KIND == "false_tool_blocker_replay"
    slot = _ChatSlot("drain-hold")
    slot.queue_insert(0, "next", kind=kind)
    assert chat_runner._next_queued_is_recovery_continuation(slot) is holds


@pytest.mark.parametrize(
    "kind, holds",
    [("synthetic_recovery", True), ("", False)],
    ids=["auth-held-continuation", "user-turn"],
)
@pytest.mark.asyncio
async def test_the_cycle_end_holds_a_marker_behind_a_queued_recovery(tmp_path, kind, holds):
    """An auth-required failure ends the cycle with its recovery held at the queue
    head; that recovery still owes the reply the marker closes."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("cycle-end-hold")
    slot._titled = True
    state._slots[slot.key] = slot
    slot.task = asyncio.get_running_loop().create_future()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.refresh_slot_source_status = MagicMock()
    state.push_refresh = MagicMock()
    slot._deferred_notes.append(TestRealFlushWritesTheMarkerRow._held(slot))
    slot.queue_insert(0, "next", kind=kind)

    with patch.object(chat_runner, "generate_session_summary", new=AsyncMock()):
        await chat_runner._finish_queue_cycle(state, slot, drain=False)
        await asyncio.sleep(0)

    written = [m for m in slot.messages if m.get("role") == SECTION_MARKER_ROLE]
    assert (written == []) is holds
    assert (len(slot._deferred_notes) == 1) is holds
