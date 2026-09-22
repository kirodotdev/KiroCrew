"""Contract tests for the projection kernel, written against the kernel alone.

These use throwaway definitions rather than any client's real folds. Each test pins
a rule the KERNEL enforces, and a member projection passing is evidence about that
projection rather than about the rule -- the member log's suite already covers the
member folds, and it would still pass if the rules below were quietly relaxed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.projection import ProjectionRegistry, attribute_seq


def _ev(seq: int, touches: bool = True) -> dict:
    """A mapping-shaped carrier: the seq, plus the one field the folds below read."""
    return {"seq": seq, "touches": touches}


class _Counting:
    """Counts events that touch it and returns state UNCHANGED for the rest.

    It records every ``apply`` call in ``applied``, which a pure fold would not do:
    the recording is how a test tells "folded and produced no change" apart from
    "never folded at all", and those are exactly what the watermark decides between.
    """

    key = "counting"
    state_version = 1

    def __init__(self) -> None:
        self.applied: list[int] = []

    def init(self) -> dict:
        return {"n": 0}

    def apply(self, state: dict, event: Any) -> dict:
        self.applied.append(event["seq"])
        if not event["touches"]:
            return state
        return {"n": state["n"] + 1}

    def view(self, state: dict) -> dict:
        return dict(state)


class _FreshEqual:
    """Always returns a NEW object EQUAL to the old one -- the rule's failure mode."""

    key = "fresh_equal"
    state_version = 1

    def init(self) -> dict:
        return {"n": 0}

    def apply(self, state: dict, event: Any) -> dict:
        return dict(state)

    def view(self, state: dict) -> dict:
        return dict(state)


@dataclass(frozen=True)
class _Entry:
    """An attribute-shaped carrier -- the shape a crew log entry has."""

    seq: int


class _LastSeq:
    """Folds an attribute-shaped carrier, to prove the kernel never subscripts one."""

    key = "last_seq"
    state_version = 1

    def init(self) -> dict:
        return {"last": -1}

    def apply(self, state: dict, event: Any) -> dict:
        return {"last": event.seq}

    def view(self, state: dict) -> dict:
        return dict(state)


class TestTheSameReferenceRuleDecidesTheChangeFeed:
    """``apply`` returning the SAME object is what "nothing changed" means here."""

    def test_returning_the_same_object_emits_nothing(self):
        reg = ProjectionRegistry()
        reg.register(_Counting())
        fired: list[int] = []
        reg.set_on_change(lambda store, key, view, seq: fired.append(seq))

        reg.drive("s", _ev(1, touches=False))

        assert fired == [], "an event the fold ignored still emitted a change"

    def test_a_fresh_but_equal_object_emits(self):
        # IDENTITY is what the registry compares, not equality -- so a fold that
        # rebuilds state it did not change spends a frame on every client watching
        # that store. Pinned because the cheap-looking "fix" is to compare with
        # ``==``, which would instead DROP a real change whose new state happens to
        # equal the old one, and nothing would raise either way.
        reg = ProjectionRegistry()
        reg.register(_FreshEqual())
        fired: list[int] = []
        reg.set_on_change(lambda store, key, view, seq: fired.append(seq))

        reg.drive("s", _ev(1, touches=False))

        assert fired == [1], "a new-but-equal state was treated as no change"


class TestAnEventAtOrBelowTheWatermarkIsANoOp:
    """Re-driving a seq a cell already folded costs that cell nothing."""

    def test_redriving_a_folded_seq_neither_folds_nor_emits(self):
        reg = ProjectionRegistry()
        defn = _Counting()
        reg.register(defn)
        fired: list[int] = []
        reg.set_on_change(lambda store, key, view, seq: fired.append(seq))

        reg.drive("s", _ev(5))
        assert reg.snapshot("s")["values"]["counting"] == {"n": 1}
        assert fired == [5]

        reg.drive("s", _ev(5))  # the same seq again
        reg.drive("s", _ev(3))  # and an older one

        assert defn.applied == [5], "an already-folded event was applied a second time"
        assert fired == [5], "a replayed event emitted a change"
        assert reg.snapshot("s")["values"]["counting"] == {"n": 1}
        assert reg.snapshot("s")["asOfSeq"] == 5

    def test_the_watermark_is_per_store(self):
        # Cells are keyed by (key, store), so one store reaching seq 9 must not make
        # another store's seq 1 look like a replay. A watermark held per DEFINITION
        # would silently drop every early event of every store but the first.
        reg = ProjectionRegistry()
        reg.register(_Counting())

        reg.drive("a", _ev(9))
        reg.drive("b", _ev(1))

        assert reg.snapshot("b")["values"]["counting"] == {"n": 1}
        assert reg.snapshot("b")["asOfSeq"] == 1


class TestTheKernelReadsSeqThroughTheClientsReader:
    """The kernel never reaches into a payload, so a carrier stays its owner's type."""

    def test_a_mapping_carrier_needs_no_reader(self):
        reg = ProjectionRegistry()
        reg.register(_Counting())

        reg.drive("s", _ev(2))

        assert reg.snapshot("s")["asOfSeq"] == 2

    def test_an_attribute_carrier_drives_with_attribute_seq(self):
        reg = ProjectionRegistry(seq_of=attribute_seq)
        reg.register(_LastSeq())

        reg.drive("s", _Entry(seq=7))

        assert reg.snapshot("s")["values"]["last_seq"] == {"last": 7}
        assert reg.snapshot("s")["asOfSeq"] == 7

    def test_an_attribute_carrier_gets_the_watermark_too(self):
        # The guard reads the seq through the same reader, so it must hold for a
        # carrier the kernel cannot subscript -- otherwise the second client would
        # lose idempotent replay, which is the property its resumed folds rest on.
        reg = ProjectionRegistry(seq_of=attribute_seq)
        reg.register(_LastSeq())

        reg.prime("s", [_Entry(seq=4)])
        reg.drive("s", _Entry(seq=4))

        assert reg.snapshot("s")["asOfSeq"] == 4
