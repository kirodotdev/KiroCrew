"""The crew record store: what it keeps, what it refuses, and what it writes atomically."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.microvm import states
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore


@pytest.fixture()
def store(tmp_path):
    return CrewStore(tmp_path / "microvm_crews.json")


class TestRoundTrip:
    def test_an_absent_file_reads_as_no_crews(self, store):
        assert store.load() == {}
        assert store.get("anything") is None

    def test_one_record_survives_a_round_trip(self, store):
        store.put(CrewRecord(tag="kc-abc123", microvm_id="mvm-1", crew_name="demo"))
        back = store.get("kc-abc123")
        assert back is not None
        assert back.microvm_id == "mvm-1"
        assert back.crew_name == "demo"

    def test_put_leaves_siblings_alone(self, store):
        store.put(CrewRecord(tag="a"))
        store.put(CrewRecord(tag="b"))
        store.put(CrewRecord(tag="a", microvm_id="mvm-a"))
        assert set(store.load()) == {"a", "b"}
        assert store.get("b").microvm_id == ""

    def test_delete_reports_whether_there_was_one(self, store):
        store.put(CrewRecord(tag="a"))
        assert store.delete("a") is True
        assert store.delete("a") is False

    def test_the_file_is_sorted_so_a_diff_is_readable(self, store):
        store.put(CrewRecord(tag="zeta"))
        store.put(CrewRecord(tag="alpha"))
        document = json.loads(store.path.read_text())
        assert [c["tag"] for c in document["crews"]] == ["alpha", "zeta"]


class TestTolerance:
    def test_a_corrupt_file_reads_as_no_crews(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json")
        assert store.load() == {}

    def test_a_json_scalar_reads_as_no_crews(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('"hello"')
        assert store.load() == {}

    def test_one_unreadable_record_does_not_hide_the_others(self, store):
        """One bad crew must not take the owner's whole roster down."""
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "crews": [
                        {"tag": "good", "state": states.RUNNING},
                        {"tag": "bad", "wall_at": "not a number"},
                    ],
                }
            )
        )
        loaded = store.load()
        assert set(loaded) == {"good"}

    def test_an_oversized_file_reads_as_no_crews(self, store, monkeypatch):
        from kiro_crew.cloud.microvm import record as record_module

        monkeypatch.setattr(record_module, "_MAX_FILE_BYTES", 10)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"version": 1, "crews": []}) + " " * 100)
        assert store.load() == {}


class TestFieldTypes:
    def test_a_record_with_no_tag_is_refused(self):
        assert CrewRecord.from_json({"state": states.RUNNING}) is None

    def test_an_unknown_state_is_refused(self):
        """A state the edge table has no row for cannot be moved out of."""
        assert CrewRecord.from_json({"tag": "a", "state": "sleepy"}) is None

    def test_a_string_field_of_the_wrong_type_drops_the_record(self):
        assert CrewRecord.from_json({"tag": "a", "microvm_id": 7}) is None

    def test_a_boolean_is_not_a_count(self):
        """``bool`` is an ``int`` subclass, so ``true`` would read as one restart."""
        assert CrewRecord.from_json({"tag": "a", "generation": True}) is None

    def test_an_unknown_key_is_dropped_not_refused(self):
        """A document from a newer build must still load on an older one."""
        record = CrewRecord.from_json({"tag": "a", "something_new": 1})
        assert record is not None and record.tag == "a"

    def test_an_int_is_accepted_where_a_float_is_declared(self):
        record = CrewRecord.from_json({"tag": "a", "wall_at": 1700000000})
        assert record is not None and record.wall_at == 1700000000.0


class TestApplyEvent:
    def test_a_launch_creates_the_record(self, store):
        record = store.apply_event("kc-new", states.EVENT_LAUNCH_STARTED)
        assert record.state == states.PENDING
        assert store.get("kc-new") is not None

    def test_an_illegal_event_is_not_stored(self, store):
        store.put(CrewRecord(tag="a", state=states.TERMINATED))
        with pytest.raises(states.IllegalTransition):
            store.apply_event("a", states.EVENT_ONLINE)
        assert store.get("a").state == states.TERMINATED

    def test_the_state_and_its_facts_land_in_one_write(self, store):
        """The window between two writes is where a state arrives without the
        facts that explain it, and the next reader cannot act on either."""
        store.put(CrewRecord(tag="a", state=states.PENDING))
        record = store.apply_event("a", states.EVENT_ONLINE, mi_id="mi-1", last_observed_at=1234.0)
        assert record.state == states.RUNNING
        on_disk = store.get("a")
        assert on_disk.mi_id == "mi-1"
        assert on_disk.last_observed_at == 1234.0


class TestCaps:
    def test_publishing_past_the_cap_is_refused(self, store, monkeypatch):
        from kiro_crew.cloud.microvm import record as record_module

        monkeypatch.setattr(record_module, "_MAX_RECORDS", 2)
        with pytest.raises(ValueError, match="cap is 2"):
            store.publish({str(i): CrewRecord(tag=str(i)) for i in range(3)})


class TestEffectiveState:
    def test_a_never_observed_crew_is_unknown(self):
        assert (
            CrewRecord(tag="a", state=states.RUNNING).effective_state(now=1000.0)
            == states.EFFECTIVE_UNKNOWN
        )

    def test_a_freshly_observed_crew_reports_its_state(self):
        record = CrewRecord(tag="a", state=states.RUNNING, last_observed_at=990.0)
        assert record.effective_state(now=1000.0) == states.RUNNING

    def test_evolve_does_not_mutate_the_original(self):
        record = CrewRecord(tag="a")
        other = record.evolve(microvm_id="mvm-1")
        assert record.microvm_id == ""
        assert other.microvm_id == "mvm-1"


class TestAWriteNeverErasesWhatItCouldNotRead:
    """An unreadable ledger must refuse the write, not publish one record over it.

    The tolerant read is right for a READER: raising would turn one bad byte into
    a dashboard showing nothing. It is wrong before a WRITE, because an
    unreadable file reads as ``{}``, and a writer that adds its one record to that
    publishes a store with every other crew missing -- taking with them the ETags
    their archives require. The archives survive and nothing can restore them.
    """

    def test_an_unparseable_store_refuses_a_put(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        with pytest.raises(CrewStoreUnreadable, match="not readable JSON"):
            CrewStore(path).put(CrewRecord(tag="newcrew"))

    def test_the_file_is_left_exactly_as_it_was(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        before = path.read_bytes()
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).put(CrewRecord(tag="newcrew"))
        assert path.read_bytes() == before

    def test_an_unparseable_store_refuses_an_event(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text('{"crews": "not a list"}')
        with pytest.raises(CrewStoreUnreadable, match="not the shape"):
            CrewStore(path).apply_event("c", "launch_recorded")

    def test_an_unparseable_store_refuses_a_delete(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("\x00\x01 binary")
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).delete("c")

    def test_a_store_that_never_existed_is_the_one_true_empty(self, tmp_path):
        """A file that was never written genuinely holds no crews, so a first
        write must not be refused."""
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        assert store.put(CrewRecord(tag="first")).tag == "first"
        assert set(store.load()) == {"first"}

    def test_a_readable_store_still_writes_and_keeps_the_others(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(CrewRecord(tag="one"))
        store.put(CrewRecord(tag="two"))
        assert set(store.load()) == {"one", "two"}

    def test_the_reader_stays_tolerant(self, tmp_path):
        """Unchanged, and deliberately: one bad byte must not blank the dashboard."""
        from kiro_crew.cloud.microvm.record import CrewStore

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        assert CrewStore(path).load() == {}


class TestAWriteRefusesAPartiallyUnreadableLedger:
    """One dropped record takes that crew's archive ETag with it.

    The tolerant read drops an entry it cannot parse and keeps the rest, which is
    right for a dashboard. A WRITE publishes exactly what loaded, so the dropped
    entry does not come back -- and what it held was the only thing that can read
    that crew's archive.
    """

    def test_one_unparseable_record_refuses_a_put(self, tmp_path):
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        good = CrewStore(path)
        good.put(CrewRecord(tag="keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        with pytest.raises(CrewStoreUnreadable, match="could not be parsed"):
            CrewStore(path).put(CrewRecord(tag="newcomer"))

    def test_the_keeper_is_still_on_disk_afterwards(self, tmp_path):
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="keeper", crew_name="keeper-crew"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).put(CrewRecord(tag="newcomer"))
        assert "keeper-crew" in path.read_text()

    def test_the_reader_still_shows_the_readable_ones(self, tmp_path):
        """Unchanged: one unreadable crew must not hide the others."""
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        assert set(CrewStore(path).load()) == {"keeper"}


class TestTwoWritersCannotLoseACrew:
    """The lane has two writers by design and they are in different processes.

    The gateway writes this file as it serves the owner's clicks; the ``tick``
    cron writes it on its own schedule in its own interpreter. ``publish``
    replaces the WHOLE document, so an atomic rename makes each write indivisible
    without making the pair of them ordered: both read the same document, each
    adds its own change, and the second rename discards the first. What it
    discards is a crew's row, and with it the archive ETag that is the only thing
    that can condition the next write on that archive.
    """

    @staticmethod
    def _cannot_be_locked(path) -> bool:
        """Whether this file's writer lock is held by somebody.

        A fresh ``open`` gets its own open file description, so POSIX ``flock``
        counts it as a competing holder even in this process -- which is exactly
        the property under test. ``wait=False`` makes it one attempt.
        """
        from kiro_crew import platform_lock_compat

        with open(str(path) + ".lock", "a+") as handle:
            try:
                with platform_lock_compat.file_lock(handle.fileno(), exclusive=True, wait=False):
                    return False
            except Exception:
                return True

    def test_a_put_holds_the_lock_while_it_publishes(self, tmp_path):
        path = tmp_path / "crews.json"
        store = CrewStore(path)
        held: list[bool] = []
        original = store.publish

        def publish(records):
            held.append(self._cannot_be_locked(path))
            original(records)

        store.publish = publish  # type: ignore[method-assign]
        store.put(CrewRecord(tag="kc-a"))
        assert held == [True], "the publish ran without the writer lock held"

    def test_an_apply_event_holds_the_lock_while_it_publishes(self, tmp_path):
        """The transition is decided inside the lock too: the event's legality
        depends on the state read, so deciding outside it can store a state no
        edge of the table allowed."""
        path = tmp_path / "crews.json"
        store = CrewStore(path)
        store.put(CrewRecord(tag="kc-a", state=states.RUNNING))
        held: list[bool] = []
        original = store.publish

        def publish(records):
            held.append(self._cannot_be_locked(path))
            original(records)

        store.publish = publish  # type: ignore[method-assign]
        store.apply_event("kc-a", states.EVENT_TERMINATED)
        assert held == [True]

    def test_a_delete_holds_the_lock_while_it_publishes(self, tmp_path):
        path = tmp_path / "crews.json"
        store = CrewStore(path)
        store.put(CrewRecord(tag="kc-a"))
        held: list[bool] = []
        original = store.publish

        def publish(records):
            held.append(self._cannot_be_locked(path))
            original(records)

        store.publish = publish  # type: ignore[method-assign]
        assert store.delete("kc-a") is True
        assert held == [True]

    def test_the_lock_is_released_when_the_write_is_refused(self, tmp_path):
        """A refusal must not leave the file locked for the next pass: the tick
        runs every minute and a held lock would turn one bad byte into a lane
        that never writes again."""
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewStoreUnreadable

        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).put(CrewRecord(tag="newcomer"))
        assert self._cannot_be_locked(path) is False

    def test_the_lock_file_is_not_the_document(self, tmp_path):
        """A sibling ``.lock`` rather than the record itself, because the Windows
        path locks a byte of the file it is given and the document is replaced by
        rename underneath it."""
        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="kc-a"))
        assert (tmp_path / "crews.json.lock").exists()
        assert set(CrewStore(path).load()) == {"kc-a"}

    def test_a_missing_parent_directory_is_created_for_the_lock(self, tmp_path):
        """The first write of a fresh install creates the directory, and the lock
        is taken BEFORE the publish that would otherwise have created it."""
        path = tmp_path / "nested" / "deeper" / "crews.json"
        CrewStore(path).put(CrewRecord(tag="kc-a"))
        assert set(CrewStore(path).load()) == {"kc-a"}


class TestPatchLiveFencesAStaleWriter:
    """``patch_live`` is the write a step takes AFTER waiting.

    ``put`` writes the record whole, so a caller that read a record, waited
    minutes, and then evolved the copy it read puts every field back as it was --
    including a ``state`` that a teardown moved on in the meantime. These are the
    three ways the crew a caller is acting for stops being the crew on disk.
    """

    def test_a_live_crew_of_the_same_generation_is_patched(self, store):
        store.put(CrewRecord(tag="c", state=states.RUNNING, generation=2))
        got = store.patch_live("c", generation=2, mi_id="mi-1")
        assert got is not None
        assert got.mi_id == "mi-1"
        assert store.get("c").mi_id == "mi-1"

    def test_a_row_that_is_gone_refuses(self, store):
        assert store.patch_live("c", generation=1, mi_id="mi-1") is None

    def test_a_terminal_row_refuses_and_is_left_alone(self, store):
        store.put(CrewRecord(tag="c", state=states.TERMINATED, generation=1))
        assert store.patch_live("c", generation=1, mi_id="mi-1") is None
        # The point of the fence: the terminal state survives the late write.
        assert store.get("c").state == states.TERMINATED
        assert store.get("c").mi_id == ""

    def test_a_newer_generation_refuses(self, store):
        """A relaunch took the tag. The row is live and non-terminal, and it
        belongs to a different VM -- which a state check alone cannot tell."""
        store.put(CrewRecord(tag="c", state=states.RUNNING, generation=3))
        assert store.patch_live("c", generation=2, mi_id="mi-1") is None
        assert store.get("c").mi_id == ""

    def test_the_changes_land_on_the_record_as_it_is_on_disk(self, store):
        """Not on the caller's copy. This is the whole difference from ``put``:
        the caller supplies the new facts and never the old ones."""
        store.put(CrewRecord(tag="c", state=states.RUNNING, generation=1))
        # Something else writes a fact while the caller holds its stale copy.
        store.put(store.get("c").evolve(endpoint="https://later"))

        got = store.patch_live("c", generation=1, mi_id="mi-1")

        assert got is not None
        assert got.mi_id == "mi-1", "the caller's own change was dropped"
        assert got.endpoint == "https://later", "the fence wrote a stale field back"


class TestTheStoreIsNotAgentWritable:
    """The file holds ids that a destructive AWS call consumes without asking.

    ``teardown`` reads a row's ``microvm_id`` and hands it to
    ``launcher.terminate`` -- no ``--tag`` to disagree with it and no
    describe-and-confirm step in front of it. So an agent that could rewrite one
    crew's row could point it at another crew's VM and have the owner's next
    delete destroy that one instead, along with its home. The repository already
    treats ``cloud_launch_state.json`` this way for a weaker version of the same
    harm, and this file gets the same three layers.
    """

    LEAF = "microvm_crews.json"

    def test_the_file_name_is_the_one_the_protection_lists_name(self):
        """Non-vacuity for every assertion below: they all key off the leaf name,
        so a rename that left the lists alone would pass them all silently."""
        from kiro_crew.cloud.microvm import record

        assert record._FILENAME == self.LEAF

    def test_a_write_is_refused_by_the_file_edit_gate(self):
        import os

        from kiro_crew.security import paths

        target = os.path.join(os.path.expanduser("~/.kirocrew"), self.LEAF)
        assert paths.is_sensitive_write_path(target), (
            "the store is writable through the agent's file-edit tool, so an agent "
            "can choose which VM the next teardown terminates"
        )

    def test_the_read_stays_open(self):
        """Write-protected, not read-masked -- the same asymmetry
        ``cloud_launch_state.json`` has. Classifying it sensitive for READS would
        mask a path the gateway itself resolves, and the harm here is the write."""
        import os

        from kiro_crew.security import paths

        target = os.path.join(os.path.expanduser("~/.kirocrew"), self.LEAF)
        assert not paths.is_sensitive_path(target)

    def test_a_sandboxed_shell_meets_a_kernel_seal_too(self):
        """The file-edit gate covers the agent's tool; only a kernel denial covers
        ``open(..., "w")`` from a sandboxed shell, however the write is spelled."""
        from kiro_crew import sandbox

        assert self.LEAF in sandbox._CREW_READONLY_LEAVES

    def test_the_name_is_pre_created_so_it_cannot_be_squatted(self):
        """``mount(2)`` cannot seal a name nothing occupies, and this file does not
        exist until the lane's first launch -- which is never, on most installs."""
        from kiro_crew import sandbox

        assert self.LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_a_foreign_harnesss_child_is_not_handed_the_cloud_inventory(self):
        """Withheld rather than readable, because no in-sandbox reader needs it: the
        engine and the crew-turn route are the only callers and both run in the
        gateway. What a read would hand over is every crew's VM id, node id and
        endpoint, which is reconnaissance for the write the seal exists to stop."""
        from kiro_crew import sandbox

        assert self.LEAF in sandbox._CREW_CHILD_WITHHELD_LEAVES
        assert self.LEAF not in sandbox._CREW_CHILD_READABLE_LEAVES


class TestThePrecreatedEmptyStoreIsWritable:
    """The sandbox pre-creates this file as ``{}``, and a launch must survive it.

    The leaf is on ``_CREW_PRECREATE_READONLY_FILE_LEAVES`` because ``mount(2)``
    cannot seal a name nothing occupies. The pre-create writes ``{}``, so ``{}``
    is the shape the FIRST launch on a sealed install finds -- and the write path
    has to accept it, not only the tolerant read. ``load`` already did; a
    ``crews``-list check in ``load_for_write`` did not, which made the one state
    the seal itself creates the one state a launch could not start from.
    """

    def test_an_empty_object_reads_as_no_crews_on_the_write_path(self, store):
        store.path.write_text("{}", encoding="utf-8")
        assert store.load_for_write() == {}

    def test_a_launch_can_write_into_the_precreated_store(self, store):
        """The chain that matters: pre-created ``{}`` -> ``provision`` -> ``put``."""
        store.path.write_text("{}", encoding="utf-8")
        got = store.put(CrewRecord(tag="c", state=states.PENDING, generation=1))
        assert got.tag == "c"
        assert store.get("c") is not None, "the first launch could not record its crew"

    def test_the_tolerant_read_agrees_with_it(self, tmp_path):
        """Criterion 1 for the pre-create list, which both readers must meet:
        an EMPTY document means what an ABSENT one means."""
        d = tmp_path
        absent = CrewStore(d / "gone.json")
        empty = CrewStore(d / "empty.json")
        empty.path.write_text("{}", encoding="utf-8")
        assert absent.load() == empty.load() == {}
        assert absent.load_for_write() == empty.load_for_write() == {}

    @pytest.mark.parametrize(
        "document",
        ['{"other": 1}', '{"crews": "nope"}', "[]", '{"crews": {}}', "3"],
        ids=["wrong-key", "crews-not-a-list", "array", "crews-a-dict", "scalar"],
    )
    def test_any_other_shape_is_still_refused(self, store, document):
        """Non-vacuity: only the exact pre-created ``{}`` is accepted. A document
        of some other shape may be another writer's file, and publishing over it
        would discard whatever it holds."""
        from kiro_crew.cloud.microvm.record import CrewStoreUnreadable

        store.path.write_text(document, encoding="utf-8")
        with pytest.raises(CrewStoreUnreadable):
            store.load_for_write()
