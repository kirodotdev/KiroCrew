"""Contract tests for the projection kernel's savepoints.

Each test pins ONE property the store or the checkpointed prime promises. They use
throwaway definitions rather than any client's real folds, for the same reason the
registry's own tests do: a member projection passing is evidence about that
projection, and would keep passing if a rule below were quietly relaxed.

The load-bearing one is the equality test -- a stale savepoint plus its tail reaching
exactly what a cold fold reaches. Everything else here protects that equality by
refusing a payload that would break it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.projection import (
    EMPTY_WATERMARK,
    DirectoryCheckpointStore,
    ProjectionRegistry,
    Savepoint,
)
from kiro_crew.projection import checkpoint as checkpoint_mod

IDENTITY = {"origin": "log-a", "first_seq": 1}


def _ev(seq: int, amount: int = 1) -> dict:
    return {"seq": seq, "amount": amount}


class _Summing:
    """Sums ``amount`` across events. JSON-serializable state, so it can be saved."""

    key = "summing"
    state_version = 3

    def __init__(self) -> None:
        self.applied: list[int] = []

    def init(self) -> dict:
        return {"total": 0, "count": 0}

    def apply(self, state: dict, event: dict) -> dict:
        self.applied.append(event["seq"])
        return {"total": state["total"] + event["amount"], "count": state["count"] + 1}

    def view(self, state: dict) -> dict:
        return dict(state)


def _registry() -> tuple[ProjectionRegistry, _Summing]:
    reg = ProjectionRegistry()
    defn = _Summing()
    reg.register(defn)
    return reg, defn


def _tail_from(events: list[dict]):
    """A client's tail reader: the events strictly after a watermark."""

    def reader(watermark: int):
        return [e for e in events if e["seq"] > watermark]

    return reader


class TestASavepointRoundTrips:
    def test_saved_state_and_watermark_come_back(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        reg, _ = _registry()
        for ev in (_ev(1), _ev(2, amount=5)):
            reg.drive("a", ev)

        saved = reg.savepoints("a", IDENTITY)
        assert len(saved) == 1
        assert store.save("a", saved[0]) is True

        back = store.load("a", "summing", state_version=3, identity=IDENTITY)
        assert back is not None
        assert back.watermark == 2
        assert back.state == {"total": 6, "count": 2}
        assert back.identity == IDENTITY

    def test_a_fold_that_consumed_nothing_is_not_saved(self, tmp_path):
        # A savepoint at the empty watermark saves no replay, so spending a write on
        # one is pure cost.
        reg, _ = _registry()
        assert reg.savepoints("a", IDENTITY) == []


class TestAStaleSavepointPlusItsTailEqualsAColdFold:
    def test_resuming_reaches_the_same_value_as_folding_from_scratch(self, tmp_path):
        events = [_ev(seq, amount=seq) for seq in range(1, 11)]

        cold, _ = _registry()
        cold.prime("a", events)
        expected = cold.snapshot("a")

        # Save deliberately STALE: only the first four events are in the payload.
        store = DirectoryCheckpointStore(tmp_path)
        warm_writer, _ = _registry()
        warm_writer.prime("a", events[:4])
        assert store.save("a", warm_writer.savepoints("a", IDENTITY)[0]) is True

        resumed, defn = _registry()
        floor = resumed.prime_checkpointed("a", store, IDENTITY, _tail_from(events))

        assert floor == 4, "resumed at the saved watermark"
        assert resumed.snapshot("a") == expected
        assert defn.applied == [5, 6, 7, 8, 9, 10], "only the tail was folded"

    def test_no_savepoint_folds_the_whole_log(self, tmp_path):
        events = [_ev(seq) for seq in range(1, 5)]
        store = DirectoryCheckpointStore(tmp_path)
        reg, defn = _registry()

        floor = reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))

        assert floor == EMPTY_WATERMARK
        assert defn.applied == [1, 2, 3, 4]
        assert reg.snapshot("a")["values"]["summing"]["count"] == 4


class TestARestoreFiresNoChangeCallbacks:
    """A restore is not news, so it emits nothing -- the same as a cold ``prime``.

    A client's callback is a network egress rather than a bookkeeping hook, so a
    restore that emits republishes a store's own history as live changes.
    """

    def test_resuming_past_a_savepoint_emits_nothing(self, tmp_path):
        events = [_ev(seq, amount=seq) for seq in range(1, 11)]
        store = DirectoryCheckpointStore(tmp_path)
        writer, _ = _registry()
        writer.prime("a", events[:4])
        assert store.save("a", writer.savepoints("a", IDENTITY)[0]) is True

        reg, defn = _registry()
        fired: list[tuple[str, int]] = []
        reg.set_on_change(lambda _store, key, _view, seq: fired.append((key, seq)))

        floor = reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))

        assert floor == 4
        assert defn.applied == [5, 6, 7, 8, 9, 10], "the tail still folds"
        assert fired == []

    def test_a_load_with_no_savepoint_emits_nothing(self, tmp_path):
        # Nothing on disk is the common case, not a corner: the floor drops to the
        # empty watermark, so the tail is the WHOLE log. A client attached at that
        # moment is the one that would receive a frame per historical change.
        events = [_ev(seq) for seq in range(1, 8)]
        store = DirectoryCheckpointStore(tmp_path)
        reg, defn = _registry()
        fired: list[tuple[str, int]] = []
        reg.set_on_change(lambda _store, key, _view, seq: fired.append((key, seq)))

        floor = reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))

        assert floor == EMPTY_WATERMARK
        assert defn.applied == [1, 2, 3, 4, 5, 6, 7], "the whole log still folds"
        assert fired == []
        assert reg.snapshot("a")["values"]["summing"]["count"] == 7

    def test_a_cold_prime_is_silent_on_the_same_events(self, tmp_path):
        # The parity the docstring claims, pinned directly: whichever path reaches the
        # value, neither reports it as a change.
        events = [_ev(seq) for seq in range(1, 8)]
        reg, _ = _registry()
        fired: list[tuple[str, int]] = []
        reg.set_on_change(lambda _store, key, _view, seq: fired.append((key, seq)))

        reg.prime("a", events)

        assert fired == []

    def test_a_live_event_after_the_restore_still_emits(self, tmp_path):
        # Silencing the restore must not silence what the callback exists for.
        events = [_ev(seq) for seq in range(1, 5)]
        store = DirectoryCheckpointStore(tmp_path)
        reg, _ = _registry()
        fired: list[tuple[str, int]] = []
        reg.set_on_change(lambda _store, key, _view, seq: fired.append((key, seq)))

        reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))
        assert fired == [], "the restore itself is silent"

        reg.drive("a", _ev(5))

        assert fired == [("summing", 5)], "a genuinely new event is still news"


class TestAStateVersionMismatchForcesAColdRebuild:
    def test_a_savepoint_written_by_another_state_shape_is_refused(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        assert store.save("a", Savepoint("summing", 2, 9, {"total": 99}, IDENTITY)) is True

        # The definition in this build declares state_version 3. Resuming version 2's
        # state onto version 3's logic would serve pre-change numbers for the store's
        # whole life, which is worse than one cold fold.
        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is None

    def test_the_registry_refolds_rather_than_resuming_a_mismatch(self, tmp_path):
        events = [_ev(seq) for seq in range(1, 6)]
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 2, 3, {"total": 99, "count": 99}, IDENTITY))

        reg, defn = _registry()
        floor = reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))

        assert floor == EMPTY_WATERMARK
        assert defn.applied == [1, 2, 3, 4, 5]
        assert reg.snapshot("a")["values"]["summing"] == {"total": 5, "count": 5}


class TestAnIdentityMismatchForcesAColdRebuild:
    def test_a_savepoint_from_a_different_log_is_refused(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 9, {"total": 99, "count": 9}, IDENTITY))

        # A unit deleted and recreated under the same id restarts its seqs, so once the
        # new log grows past the stored watermark a seq check alone would pass. The
        # identity block is what catches it.
        recreated = {"origin": "log-b", "first_seq": 1}
        assert store.load("a", "summing", state_version=3, identity=recreated) is None

    def test_a_retention_trim_is_caught_by_the_same_block(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 9, {"total": 99, "count": 9}, IDENTITY))

        trimmed = {"origin": "log-a", "first_seq": 400}
        assert store.load("a", "summing", state_version=3, identity=trimmed) is None

    def test_the_block_is_compared_verbatim_not_by_a_known_key_list(self, tmp_path):
        # The kernel never interprets an identity key, so a client can add a condition
        # of its own without this module changing. A payload missing that new key must
        # therefore be refused.
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 9, {"total": 9, "count": 9}, IDENTITY))

        extended = dict(IDENTITY) | {"schema": "v9"}
        assert store.load("a", "summing", state_version=3, identity=extended) is None

    def test_admit_can_refuse_what_equality_cannot(self, tmp_path):
        # The prefix digest and "the log is shorter than the savepoint" are evaluated
        # against live log state, so they cannot be equality against a stored constant.
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 9, {"total": 9, "count": 9}, IDENTITY))

        seen: list[dict] = []

        def refuse(stored, witness):
            seen.append(dict(stored))
            return False

        assert store.load("a", "summing", state_version=3, identity=IDENTITY, admit=refuse) is None
        assert seen == [IDENTITY], "admit is called with the STORED block"
        assert (
            store.load("a", "summing", state_version=3, identity=IDENTITY, admit=lambda *_: True)
            is not None
        )


class TestTheWitnessCarriesWhatEqualityCannotHold:
    """A value the caller cannot state before loading, so it is stored, not compared."""

    WITNESS = {"prefix_sha": "a" * 64, "prefix_records": 9}

    def _saved(self, tmp_path, witness) -> DirectoryCheckpointStore:
        store = DirectoryCheckpointStore(tmp_path)
        assert (
            store.save(
                "a",
                Savepoint("summing", 3, 9, {"total": 9, "count": 9}, IDENTITY, witness),
            )
            is True
        )
        return store

    def test_a_stored_witness_comes_back_on_the_savepoint(self, tmp_path):
        store = self._saved(tmp_path, self.WITNESS)

        back = store.load("a", "summing", state_version=3, identity=IDENTITY)

        assert back is not None
        assert back.witness == self.WITNESS

    def test_the_witness_is_left_out_of_the_equality_compare(self, tmp_path):
        # The whole reason it is a separate mapping: a caller cannot name the digest
        # before reading the file that holds it, so comparing it would refuse every
        # savepoint that carried one.
        store = self._saved(tmp_path, self.WITNESS)

        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is not None

    def test_admit_is_handed_the_stored_witness_beside_the_identity(self, tmp_path):
        store = self._saved(tmp_path, self.WITNESS)
        seen: list[tuple[dict, dict]] = []

        def accept(identity, witness):
            seen.append((dict(identity), dict(witness)))
            return True

        assert (
            store.load("a", "summing", state_version=3, identity=IDENTITY, admit=accept) is not None
        )
        assert seen == [(IDENTITY, self.WITNESS)]

    def test_a_witness_refused_by_admit_discards_the_savepoint(self, tmp_path):
        store = self._saved(tmp_path, self.WITNESS)

        def refuse(_identity, witness):
            return witness.get("prefix_sha") == "b" * 64

        assert store.load("a", "summing", state_version=3, identity=IDENTITY, admit=refuse) is None

    def test_a_payload_carrying_no_witness_loads_with_an_empty_one(self, tmp_path):
        # What a payload written before this field says: no evidence. A client whose
        # condition needs evidence refuses on the empty mapping, which is why absent
        # does not have to retire the envelope version.
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 9, {"total": 9, "count": 9}, IDENTITY))
        path = store.path_for("a", "summing")
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["witness"]
        path.write_text(json.dumps(raw), encoding="utf-8")

        seen: list[dict] = []

        def watch(_identity, witness):
            seen.append(dict(witness))
            return True

        back = store.load("a", "summing", state_version=3, identity=IDENTITY, admit=watch)

        assert back is not None
        assert back.witness == {}
        assert seen == [{}]

    def test_a_witness_that_is_not_a_mapping_is_refused(self, tmp_path):
        # ``admit`` is written against a mapping, so handing it another shape would
        # push this module's own parsing failure into the client's predicate.
        store = self._saved(tmp_path, self.WITNESS)
        path = store.path_for("a", "summing")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["witness"] = ["a" * 64, 9]
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is None

    def test_a_registry_savepoint_carries_an_empty_witness_until_a_client_fills_it(self):
        # The registry builds savepoints from its cells and knows nothing about a
        # client's evidence, so attaching one is the client's own step.
        reg, _ = _registry()
        reg.drive("a", _ev(1))

        assert reg.savepoints("a", IDENTITY)[0].witness == {}


class TestAReDeliveredEventAtOrBelowTheWatermarkIsANoOp:
    def test_replaying_the_tail_after_a_resume_changes_nothing(self, tmp_path):
        events = [_ev(seq) for seq in range(1, 7)]
        store = DirectoryCheckpointStore(tmp_path)
        writer, _ = _registry()
        writer.prime("a", events[:4])
        store.save("a", writer.savepoints("a", IDENTITY)[0])

        reg, defn = _registry()
        reg.prime_checkpointed("a", store, IDENTITY, _tail_from(events))
        settled = reg.snapshot("a")

        # The gateway is not a log's only writer, so a client may re-drive a range it
        # is unsure about. That has to cost nothing rather than double-count.
        for ev in events:
            reg.drive("a", ev)

        assert reg.snapshot("a") == settled
        assert defn.applied == [5, 6], "a re-delivered event was folded again"


class TestAFailedWriteLeavesMemoryAuthoritativeAndThePreviousFileIntact:
    def test_save_reports_false_and_does_not_touch_the_existing_payload(
        self, tmp_path, monkeypatch
    ):
        store = DirectoryCheckpointStore(tmp_path)
        reg, _ = _registry()
        for ev in (_ev(1), _ev(2)):
            reg.drive("a", ev)
        assert store.save("a", reg.savepoints("a", IDENTITY)[0]) is True

        path = store.path_for("a", "summing")
        assert path is not None
        before = path.read_text(encoding="utf-8")

        reg.drive("a", _ev(3))

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(checkpoint_mod, "atomic_write", boom)
        assert store.save("a", reg.savepoints("a", IDENTITY)[0]) is False

        # The old payload is still there and still loadable: a savepoint write goes
        # through atomic_write, so a reader sees the previous complete payload or the
        # new one, never a half-written file.
        assert path.read_text(encoding="utf-8") == before
        monkeypatch.undo()
        recovered = store.load("a", "summing", state_version=3, identity=IDENTITY)
        assert recovered is not None
        assert recovered.watermark == 2

        # And the IN-MEMORY fold is authoritative: it kept the third event.
        assert reg.snapshot("a")["values"]["summing"] == {"total": 3, "count": 3}
        assert reg.snapshot("a")["asOfSeq"] == 3

    def test_unserializable_state_costs_the_savepoint_and_nothing_else(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        assert store.save("a", Savepoint("summing", 3, 4, {"bad": object()}, IDENTITY)) is False
        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is None


class TestAPayloadThisBuildCannotTrustIsIgnored:
    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda raw: raw.__setitem__("v", 999), id="envelope-version"),
            pytest.param(lambda raw: raw.__setitem__("key", "other"), id="wrong-fold-name"),
            pytest.param(lambda raw: raw.__setitem__("watermark", "4"), id="watermark-not-int"),
            pytest.param(lambda raw: raw.__setitem__("watermark", True), id="watermark-bool"),
            pytest.param(lambda raw: raw.__setitem__("watermark", -2), id="watermark-below-empty"),
            pytest.param(lambda raw: raw.__setitem__("identity", "log-a"), id="identity-not-dict"),
            pytest.param(lambda raw: raw.pop("state"), id="state-absent"),
        ],
    )
    def test_each_bad_field_answers_none(self, tmp_path, mutate):
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 4, {"total": 4, "count": 4}, IDENTITY))
        path = store.path_for("a", "summing")
        assert path is not None
        raw = json.loads(path.read_text(encoding="utf-8"))
        mutate(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is None

    def test_a_truncated_file_answers_none(self, tmp_path):
        store = DirectoryCheckpointStore(tmp_path)
        store.save("a", Savepoint("summing", 3, 4, {"total": 4, "count": 4}, IDENTITY))
        path = store.path_for("a", "summing")
        assert path is not None
        path.write_text('{"v": 1, "key": "summ', encoding="utf-8")

        assert store.load("a", "summing", state_version=3, identity=IDENTITY) is None

    def test_a_name_that_could_escape_the_directory_is_refused(self, tmp_path):
        # The store turns a name into a PATH, so it refuses anything that could leave
        # the directory rather than trusting its caller to have validated it.
        store = DirectoryCheckpointStore(tmp_path)
        assert store.path_for("..", "summing") is None
        assert store.path_for("a", "../../etc/passwd") is None
        assert store.path_for("a/b", "summing") is None
        assert store.save("..", Savepoint("summing", 3, 1, {}, IDENTITY)) is False
