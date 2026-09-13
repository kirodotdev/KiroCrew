"""A ``linked_session_key`` rebind may not move the key an arm is settling onto.

The finding this covers: the settle resolves against a key, then the arm is transferred onto
it. A writer landing between the two leaves the arm owed to a binding nobody is on. Seven of
the eight writers are SYNCHRONOUS functions, so an asyncio lock is unavailable to them, and
one transfer already runs inside ``async with slot._lock`` where a second acquisition of a
non-reentrant lock never returns -- so the exclusion is a synchronous non-blocking guard that
parks a rebind and reports it, rather than a lock.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.chat_utils import (
    bind_linked_session_key,
    key_rebind_deferred,
    settling_key,
)


def _slot(key: str = "dashboard:test", linked: str = ""):
    slot = SimpleNamespace()
    slot.key = key
    slot.linked_session_key = linked
    slot._key_settling = 0
    slot._key_deferred = None
    return slot


class TestNoWriterCanMoveTheKeyWhileAnArmSettles:
    """The INVARIANT, asserted through behaviour rather than the writers' source text.

    What must hold is that a rebind cannot move the key an arm is settling onto. WHICH
    mechanism delivers that is not the invariant, so a TRIPWIRE on the slot catches any path
    that reaches the key inside a settling region, whatever route it took. A tree with no
    settling regions trips nothing and passes, so dismantling the arm store leaves this test
    correct rather than needing it rewritten.
    """

    def test_a_write_inside_a_settling_region_never_reaches_the_key(self):
        class Tripwire:
            """A slot that records every write that reaches the key itself."""

            def __init__(self):
                self.key = "dashboard:test"
                self._key_settling = 0
                self._key_deferred = None
                self._reached: list[str] = []
                self._linked = "slack:1111.0001"

            @property
            def linked_session_key(self):
                return self._linked

            @linked_session_key.setter
            def linked_session_key(self, value):
                if self._key_settling:
                    self._reached.append(value)
                self._linked = value

        slot = Tripwire()
        with settling_key(slot):
            bind_linked_session_key(slot, "cron:job-9")
            assert slot._reached == [], (
                "a writer moved the key while an arm was settling onto it, so the arm lands "
                f"on a binding nobody is on; reached={slot._reached}"
            )
        assert slot.linked_session_key == "cron:job-9", "the parked write was lost, not delayed"

    def test_no_key_writer_sits_in_a_module_that_serializes_nothing(self):
        """Completeness only: a NEW writer must not appear where nothing serializes it.

        Discovery is from source because an unexercised writer is invisible at runtime, but
        the assertion is a SUBSET -- writers may DISAPPEAR freely, which is what dismantling
        does, while one added in a module that reaches the key with no serialization in sight
        fails. The serialization point is resolved from the live object, so moving or renaming
        it does not read as a stray writer.
        """
        import pathlib
        import sys

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        assign = re.compile(r"\.linked_session_key\s*=(?!=)")
        point = pathlib.Path(sys.modules[bind_linked_session_key.__module__].__file__).resolve()

        strays = []
        seen_point = False
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not assign.search(text):
                continue
            serialized = "bind_linked_session_key" in text
            for n, line in enumerate(text.splitlines(), 1):
                if "self.linked_session_key" in line or not assign.search(line):
                    continue
                if path.resolve() == point:
                    seen_point = True
                    continue
                if serialized:
                    continue
                strays.append(f"{path.relative_to(root).as_posix()}:{n}: {line.strip()}")

        assert seen_point, (
            "no write was found inside the serialization point, so this pattern no longer "
            "matches the real thing and would report every module as clean"
        )
        assert not strays, (
            "these modules reach linked_session_key with nothing serializing them, so a "
            "rebind can move the key while an arm settles:\n  " + "\n  ".join(strays)
        )

    def test_the_slot_factory_binds_its_keyword_only_when_it_mints_the_slot(self, tmp_path):
        """The keyword surface cannot rebind a live slot, and no census above can see it.

        ``get_or_create_slot(linked_session_key=...)`` sets the key without ever naming the
        attribute, so the dotted-assignment census is structurally blind to it. What keeps it
        safe is not the guard but the factory's own early return for an EXISTING slot, which
        precedes the bind: the keyword therefore binds only the slot it mints, where no arm
        can yet be settling. Moving that bind above the early return would turn this surface
        into a rebind the census still reports as clean.
        """
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack:C123.456", linked_session_key="slack:C123.456")
        assert slot.linked_session_key == "slack:C123.456", (
            "the keyword did not bind the slot it minted, so a channel-born tab surfaces "
            "unbound and answers from a session no channel reads"
        )

        again = state.get_or_create_slot(slot.key, linked_session_key="cron:job-9")
        assert again is slot
        assert slot.linked_session_key == "slack:C123.456", (
            "the factory rebound a LIVE slot from its keyword, a surface no census here "
            "matches, so it can move the key while an arm settles onto the one it left"
        )


class TestARebindInsideTheSettlingRegionIsParkedNotApplied:
    """Held open, the guard must keep the key still and say a rebind arrived."""

    def test_a_writer_inside_the_region_cannot_move_the_key(self):
        slot = _slot(linked="slack:1111.0001")

        with settling_key(slot):
            landed = bind_linked_session_key(slot, "cron:job-9")
            assert not landed, "the writer reported the key landed inside the region"
            assert slot.linked_session_key == "slack:1111.0001", (
                "the key moved while an arm was settling onto it, so the arm is published "
                f"onto a binding nobody is on; key={slot.linked_session_key!r}"
            )
            assert key_rebind_deferred(slot), (
                "the settle cannot see that a rebind arrived, so it publishes onto the key "
                "it settled on even though that key is spent"
            )

        assert slot.linked_session_key == "cron:job-9", (
            "the parked rebind was LOST rather than delayed, so the slot never reaches the "
            f"session its writer bound it to; key={slot.linked_session_key!r}"
        )
        assert not key_rebind_deferred(slot), "the deferral outlived its region"

    def test_an_unrecognised_depth_assigns_rather_than_parking(self):
        """Parking is the direction that loses a bind, so an unknown shape must not park.

        A test double reports a truthy value for any attribute asked of it. Read as a depth
        that would park every bind on a slot that never unwinds a region, and the key is
        lost rather than delayed.
        """
        mock_slot = MagicMock()
        assert bind_linked_session_key(mock_slot, "cron:job-9"), (
            "a bind on a slot whose settling depth is not an int was PARKED, so it is lost "
            "on any slot that never unwinds a settling region"
        )
        assert mock_slot.linked_session_key == "cron:job-9"
        assert not key_rebind_deferred(mock_slot), (
            "an unrecognised parked value was read as a rebind, which refuses every settle "
            "on such a slot and leaves its arms unpublished forever"
        )

    def test_a_writer_outside_any_region_applies_immediately(self):
        slot = _slot(linked="slack:1111.0001")
        assert bind_linked_session_key(slot, "cron:job-9")
        assert slot.linked_session_key == "cron:job-9", (
            "an ordinary rebind was deferred with no settle in progress, which would strand "
            "every cron and workflow injection"
        )

    def test_the_regions_nest_without_releasing_early(self):
        slot = _slot(linked="slack:1111.0001")
        with settling_key(slot):
            with settling_key(slot):
                bind_linked_session_key(slot, "cron:inner")
            assert slot.linked_session_key == "slack:1111.0001", (
                "the inner region applied the parked key while the outer one was still "
                "settling, so the outer arm publishes onto a spent key"
            )
        assert slot.linked_session_key == "cron:inner"

    def test_the_guard_unwinds_on_an_exception(self):
        slot = _slot(linked="slack:1111.0001")
        with pytest.raises(RuntimeError):
            with settling_key(slot):
                bind_linked_session_key(slot, "cron:job-9")
                raise RuntimeError("the settle failed")
        assert (
            slot._key_settling == 0
        ), "the guard leaked, so every later rebind on this slot is parked forever"
        assert slot.linked_session_key == "cron:job-9", "the parked key was lost on the error path"


class TestTheSettleReportsUnsettledWhenARebindWasParked:
    """The settle must publish nothing when a rebind landed inside its own region."""

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_resolve_leaves_the_arm_unpublished(self):
        from kiro_crew.dashboard.chat_runner import _settle_arm_target

        slot = _slot(linked="slack:1111.0001")
        state = MagicMock()
        superseded: list = []
        transferred: list = []
        state.sessions.supersede_arm_for_new_slot = MagicMock(
            side_effect=lambda *a, **k: superseded.append((a, k))
        )
        state.sessions.transfer_retire_arm = MagicMock(side_effect=lambda *a: transferred.append(a))

        # The rebind lands DURING the resolve await, which is the window the finding names.
        async def resolve(key, cleared):
            bind_linked_session_key(slot, "cron:job-9")
            return "/workspace/resolved"

        state.sessions.resolve_arm_cwd = resolve

        denied, key, cwd, settled = await _settle_arm_target(
            state, slot, "slack:1111.0001", "", None
        )

        assert denied is None
        assert not settled, (
            "the settle reported SETTLED with a rebind already parked, so its caller "
            f"publishes an arm onto the spent key {key!r}"
        )
        assert not superseded, (
            "the unsettled retract spent an arm this caller never wrote, so a later claim "
            "stating no directory is served the superseded project with nothing to retire it"
        )
        assert (
            slot.linked_session_key == "cron:job-9"
        ), "the rebind was lost, so the slot never reaches the session it was bound to"
