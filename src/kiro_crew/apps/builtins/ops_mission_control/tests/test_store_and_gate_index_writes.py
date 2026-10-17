"""Tests for the dispatch index's read-modify-writes.

Proposal expiry holds the lock across read-check-write, a field-only update never carries a
stale status into a concurrent transition, and no write publishes the index over a read that
failed.
"""

import contextlib
import json
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.tests.store_and_gate_helpers import _HomeIsolated


class TestProposalExpiryIsAtomic(_HomeIsolated):
    """`expire_stale_proposals` must hold the lock across read-check-write.

    It read the index, tested each proposal, and wrote through `update_fields` — separate
    accesses with no lock held across them, so the heartbeat could read an expired draft, a
    concurrent request revise or decide it, and this stale write then stamp `expired` over the
    newer state: silently reverting an operator's decision. Same class as the `decide_proposal`
    race fixed one function up. Found in review.
    """

    def test_the_whole_sweep_is_locked(self):
        """Structural, and deliberately so.

        A behavioural test needs two threads inside one file lock, and every harness tried
        against the `decide_proposal` race produced one winner because the old write path took
        the lock itself — the concurrency was unobservable from the outside. What is actually
        assertable is that the sweep takes the lock and does not call the relocking helper.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        source = inspect.getsource(store.expire_stale_proposals)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn("_IndexLock()", code)
        # The STRICT reader specifically. `_read_index_unlocked` collapses a FAILED read to
        # an empty index, which is right for the board and wrong as the base of this
        # whole-document rewrite -- reading through it here would publish an empty index
        # over every incident on one transient EACCES.
        self.assertIn("_read_index_for_update()", code)
        self.assertNotIn(
            "_read_index_unlocked()",
            code,
            "the sweep rewrites the index from the lenient display read",
        )
        self.assertNotIn(
            "update_fields(",
            code,
            "the sweep re-enters the lock through update_fields instead of writing in place",
        )
        self.assertNotIn(
            "read_index()",
            code.replace("_read_index_for_update()", ""),
            "the sweep reads through the unlocked snapshot",
        )

    def test_expiry_still_marks_a_past_ttl_proposal(self):
        """The lock must not change what the sweep does."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/x", title="x broke"),
            operating_mode=models.MODE_PROPOSE,
        )
        assert inc is not None
        store.update_fields(
            inc.incident_id,
            proposed_action={
                "state": models.PROPOSAL_PENDING,
                "action": "ack",
                "expires_at": "2000-01-01T00:00:00Z",
            },
        )
        touched = store.expire_stale_proposals()
        self.assertEqual(touched, [inc.incident_id])
        reloaded = store.get_incident(inc.incident_id)
        assert reloaded is not None
        assert reloaded.proposed_action is not None
        self.assertEqual(reloaded.proposed_action["state"], models.PROPOSAL_EXPIRED)

    def test_a_future_ttl_proposal_is_untouched(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/y", title="y broke"),
            operating_mode=models.MODE_PROPOSE,
        )
        assert inc is not None
        store.update_fields(
            inc.incident_id,
            proposed_action={
                "state": models.PROPOSAL_PENDING,
                "action": "ack",
                "expires_at": "2999-01-01T00:00:00Z",
            },
        )
        self.assertEqual(store.expire_stale_proposals(), [])


class TestUpdateFieldsDoesNotCarryAStaleStatus(_HomeIsolated):
    """A field-only edit must not assert a status, or it can revert a concurrent transition.

    `update_fields` read the current status OUTSIDE `_IndexLock` and handed it back to
    `transition`. On a mutually-legal transition pair the grammar cannot catch —
    `investigating` <-> `needs_human` — a concurrent change between that read and the locked
    write was silently reverted: an operator's "waiting on you" state vanished with no error.
    The incident-index sibling of the proposal races. Found by auditing for the class.
    """

    def _claim_at(self, status):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/x", title="x"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, status)
        return inc.incident_id

    def test_a_field_edit_keeps_the_current_status(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        iid = self._claim_at(models.STATUS_NEEDS_HUMAN)
        store.update_fields(iid, diagnosis="root cause")
        after = store.get_incident(iid)
        assert after is not None
        self.assertEqual(after.status, models.STATUS_NEEDS_HUMAN)
        self.assertEqual(after.diagnosis, "root cause")

    def test_update_fields_reads_the_status_only_inside_the_lock(self):
        """Structural: `update_fields` must not resolve the status before calling
        `transition`. A behavioural race here needs two threads inside one file lock and
        produced a single winner every time it was tried against the proposal races, so the
        assertable property is that no pre-lock status read exists to be stale."""
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        source = inspect.getsource(store.update_fields)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn("_KEEP_STATUS", code)
        self.assertNotIn(
            ".status",
            code,
            "update_fields reads a status outside the lock; it must pass _KEEP_STATUS",
        )
        self.assertNotIn("get_incident(", code)


class TestTheIndexIsNeverPublishedOverAFailedRead(_HomeIsolated):
    """The BASE read of a read-modify-write may not fail open.

    ``_read_index_unlocked`` collapses every failure to ``{}``, which is right for
    the board, the counts and ``find_by_signal`` — a render must not fail on an
    index it could not load. It is wrong as the base of the six locked mutations,
    which rewrite the WHOLE document: there ``{}`` means "delete every incident on
    the board", and one transient EACCES/EIO published it.

    The index is not a view, it is the CLAIM LEDGER: ``claim`` is a compare-and-set
    against these rows. Emptied, every signal reads as unowned, so the next
    heartbeat re-claims alarms already being worked and opens a duplicate
    investigation of each — and in ``act`` mode a duplicate investigation is a
    second real write against the operator's production paging. The per-incident
    markdown logs survive on disk with nothing indexing them, and an open incident
    that is not listed is never swept, resolved, or answered.
    """

    def _unreadable_index(self):
        """Fail ONLY the index file's read, as a transient EACCES would.

        Scoped by path: a blanket ``read_text`` failure would also break home
        resolution and the per-incident logs, and the test would pass for the
        wrong reason.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        target = store.index_path()
        real_read_text = Path.read_text

        def _guarded(path_self, *args, **kwargs):
            if Path(path_self) == target:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", _guarded)

    def test_a_read_that_failed_never_truncates_the_index(self):
        """The durable harm, asserted directly: the incidents already claimed
        must still be on the board afterwards."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        first = store.claim(self._signal(native_id="alarm/one"), operating_mode=models.MODE_OBSERVE)
        second = store.claim(
            self._signal(native_id="alarm/two"), operating_mode=models.MODE_OBSERVE
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        before = store.index_path().read_bytes()

        with self._unreadable_index():
            with contextlib.suppress(OSError):
                store.claim(
                    self._signal(native_id="alarm/three"), operating_mode=models.MODE_OBSERVE
                )
            with contextlib.suppress(OSError):
                store.prune_closed(keep=0)

        self.assertEqual(
            store.index_path().read_bytes(),
            before,
            "a failed read was published back over the index",
        )
        self.assertEqual(
            sorted(store.read_index()),
            sorted([first.incident_id, second.incident_id]),
            "a failed read dropped incidents that were still claimed",
        )

    def test_an_unreadable_index_refuses_a_claim(self):
        """A claim is a compare-and-set. It cannot be decided against an index
        nobody read, and ``None`` here would read as "someone else owns it"."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.claim(self._signal(native_id="alarm/one"), operating_mode=models.MODE_OBSERVE)
        with self._unreadable_index():
            with self.assertRaises(OSError):
                store.claim(self._signal(native_id="alarm/two"), operating_mode=models.MODE_OBSERVE)

    def test_an_unreadable_index_refuses_a_transition(self):
        """``transition`` raises ``KeyError`` for an unknown incident, so on the
        lenient read a real incident became "unknown" — a 404 for a row that is
        on disk. The read failure must surface as itself."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        with self._unreadable_index():
            with self.assertRaises(OSError):
                store.transition(inc.incident_id, models.STATUS_INVESTIGATING)

    def test_a_missing_index_is_still_a_first_claim(self):
        """Absent is the one failure where ``{}`` is the truth. The guard must
        not turn the app's very first claim into an error."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        self.assertFalse(store.index_path().exists())
        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(inc)
        assert inc is not None
        self.assertIn(inc.incident_id, store.read_index())

    def test_a_corrupt_index_refuses_the_mutation_instead_of_replacing_it(self):
        """Refusal, not repair-on-write.

        The four merged siblings of this idiom (`library.py`, `shares.py`,
        `secrets.py`, `policy_store.py`) answer corruption the same way. Reading an
        unparseable document as empty has a real argument behind it -- nothing
        parsed, so there is nothing to merge into -- but "cannot merge into" is not
        "safe to destroy": a truncated index still holds most of its records
        verbatim, and the mutation would replace the file and take them with it.
        Refusing costs one skipped mutation and a visible error; replacing costs
        the records, silently.

        The fixture writes malformed JSON to the real index path, so the corruption
        is reached by the update reader itself rather than simulated -- and `claim`
        is used because it is the mutation with no prior lookup to short-circuit on.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.index_path().write_text('{"INV-1": {"incident_id": "INV-1"', encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)

    def test_a_corrupt_index_is_left_exactly_as_it_was(self):
        """The point of refusing: the salvageable bytes must survive on disk."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        truncated = '{"INV-1": {"incident_id": "INV-1", "status": "dispatched"'
        store.index_path().write_text(truncated, encoding="utf-8")

        with contextlib.suppress(json.JSONDecodeError):
            store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(
            store.index_path().read_text(encoding="utf-8"),
            truncated,
            "the refused mutation still overwrote the recoverable document",
        )

    def test_the_display_read_still_tolerates_corruption(self):
        """The asymmetry, pinned. Only the MUTATION base got strict.

        The board must still render on a corrupt index -- failing the route would
        turn a recoverable file into an unusable app, which is the opposite of the
        point. So `read_index` keeps collapsing to empty (and now logs why).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        store.index_path().write_text("{ not json", encoding="utf-8")
        self.assertEqual(store.read_index(), {})

    def test_a_skipped_entry_is_not_silently_deleted_by_the_next_mutation(self):
        """Normalization was the second door into the same data loss.

        `_coerce_index` skips a value that is not an object -- and skipped it with no log
        at all, unlike the unloadable-row case beside it. On the display read that is
        right: one bad row must not blank the board. On the update path it is the whole
        bug in miniature, because the mutation rewrites the file from what the reader
        returned, so a skipped row is deleted from disk permanently with no error and no
        parse failure to notice. Found in review (GPT 5.6) -- the strict reader had closed
        the malformed-JSON door and left this one open.

        The fixture uses a non-object VALUE rather than an unloadable one because that is
        the reachable half: `Incident.from_dict` coerces every field through `str()` and
        type guards, so it essentially never raises for a dict. Strict mode covers that
        path too, but this is the one a hand-edit or a merge artifact actually produces.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        raw["INV-BROKEN"] = "this row is not an object"
        store.index_path().write_text(json.dumps(raw), encoding="utf-8")
        before = store.index_path().read_text(encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            store.claim(self._signal(native_id="alarm/new"), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(
            store.index_path().read_text(encoding="utf-8"),
            before,
            "the mutation deleted a row it merely could not read",
        )
        self.assertIn("INV-BROKEN", store.index_path().read_text(encoding="utf-8"))

    def test_an_index_that_is_valid_json_but_not_an_object_refuses_the_mutation(self):
        """`[]` parses fine, so no JSONDecodeError fires -- but it is still not a document
        a whole-file rewrite may be based on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.index_path().write_text("[]", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(store.index_path().read_text(encoding="utf-8"), "[]")

    def test_both_corruption_doors_raise_the_named_type(self):
        """The reader's contract: every refusal is ONE exception type, either door.

        Review observed that `CorruptDocumentError` was a subclass no catcher distinguished
        (First Principles). That was fair, and it applied to the tests too -- every corruption
        test here asserts the BASE `json.JSONDecodeError`, so nothing would have noticed the
        reader handing back a bare base instance. This pins the type itself, so the subclass
        is the reader's contract rather than decoration at the raise site.

        Both doors are checked because they arrive differently: a bad byte stream comes from
        `json.loads` and is re-raised, while a bad shape is constructed in `_coerce_index`.
        Catchers written against `json.JSONDecodeError` keep working either way -- asserted
        here too, since that compatibility is the whole reason for subclassing.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for label, content in (
            ("unparseable bytes", "{ not json"),
            ("valid JSON, wrong shape", "[]"),
        ):
            with self.subTest(door=label):
                store.index_path().write_text(content, encoding="utf-8")
                with self.assertRaises(models.CorruptDocumentError):
                    store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
                # The base class still catches it; that is why the subclass is safe.
                store.index_path().write_text(content, encoding="utf-8")
                with self.assertRaises(json.JSONDecodeError):
                    store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)

    def test_a_row_whose_id_disagrees_with_its_key_refuses_the_mutation(self):
        """The one hole the round-trip rule structurally could not close.

        Both sides of the round trip preserve a key/`incident_id` mismatch faithfully, so
        `_lost` passes and `_unknown` passes. But the corruption manifests at WRITE time:
        `expire_stale_proposals` iterated `index.values()` and wrote back with
        `index[inc.incident_id] = ...`, so a row stored under `INV-1` whose id says `INV-9` was
        REKEYED on the next expiry sweep -- leaving `INV-1` behind as a duplicate, or
        overwriting a real `INV-9`. Found in review (GPT 5.6).

        This is a referential invariant of the document (the key IS the id), not another shape
        check, which is why it needs its own assertion rather than being folded into the
        equivalence rule.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        raw[good.incident_id]["incident_id"] = "INV-SOMEONE-ELSE"
        store.index_path().write_text(json.dumps(raw), encoding="utf-8")
        before = store.index_path().read_text(encoding="utf-8")

        with self.assertRaises(models.CorruptDocumentError):
            store.claim(self._signal(native_id="alarm/new"), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(store.index_path().read_text(encoding="utf-8"), before)

    def test_the_expiry_sweep_writes_rows_back_under_the_key_it_read(self):
        """Belt-and-braces for the same defect, at the write rather than the read.

        The reader now refuses an inconsistent document, but the function that REWRITES the
        index should not be the one relying on that. Pinned by driving a real expiry and
        asserting the key set is unchanged -- on a consistent document the old and new forms
        agree, so only a rekey could alter it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(native_id="alarm/prop"), operating_mode=models.MODE_ACT)
        assert inc is not None
        store.propose_action(
            inc.incident_id, action="silence", sink="pagerduty", note="n", duration_secs=1
        )
        keys_before = set(json.loads(store.index_path().read_text(encoding="utf-8")))

        store.expire_stale_proposals(now="2099-01-01T00:00:00Z")

        self.assertEqual(
            set(json.loads(store.index_path().read_text(encoding="utf-8"))),
            keys_before,
            "the expiry sweep moved a record to a different key",
        )

    def test_a_file_that_is_not_utf8_takes_the_corruption_path(self):
        """The sibling exception that slipped past every corruption clause.

        `read_text(encoding="utf-8")` raises `UnicodeDecodeError` on invalid bytes, so
        `json.loads` never runs. `UnicodeDecodeError` is a `ValueError` but NOT a
        `JSONDecodeError` -- so unwrapped it missed every `except json.JSONDecodeError` this
        change added and landed in the tolerant `except (KeyError, ValueError, OSError)` arms
        instead, silently swallowed. That is the exact accidental tolerance this change exists
        to close, reached through a different exception type. Found in review (GPT 5.6).

        Asserted as `CorruptDocumentError` rather than `UnicodeDecodeError`, because the point
        is that a corrupt byte stream is ONE condition regardless of which decoder noticed it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.index_path().write_bytes(b'{"INV-1": "\xff\xfe not utf8"}')
        before = store.index_path().read_bytes()

        with self.assertRaises(models.CorruptDocumentError):
            store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(store.index_path().read_bytes(), before)

    def test_the_display_read_survives_a_file_that_is_not_utf8(self):
        """The asymmetry holds for this door too: the board still renders."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        store.index_path().write_bytes(b'{"INV-1": "\xff\xfe not utf8"}')
        self.assertEqual(store.read_index(), {})

    def test_the_serializer_round_trips_every_shape_the_app_writes(self):
        """The availability property the round-trip refusal now depends on.

        Because a mutation refuses when `to_dict(from_dict(x))` does not preserve what `x`
        held, serializer idempotence stopped being merely a correctness concern and became an
        AVAILABILITY one: a field that does not survive its own round trip would make every
        claim, transition, sweep and decide refuse. Named in review (Design Review) as
        "serializer idempotence load-bearing for store availability", which is accurate, so
        this pins it on the shapes the app actually produces rather than leaving it implied.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        incident = store.claim(self._signal(), operating_mode=models.MODE_ACT)
        assert incident is not None
        store.propose_action(
            incident.incident_id, action="silence", sink="pagerduty", note="n", duration_secs=60
        )
        store.update_fields(
            incident.incident_id,
            diagnosis="d",
            ledger_matches=["L-1", "L-2"],
            last_action="silence",
            verification=models.VERIFY_PENDING,
            verify_after="2026-01-01T00:00:00Z",
        )
        store.transition(incident.incident_id, models.STATUS_INVESTIGATING)

        for key, original in json.loads(store.index_path().read_text(encoding="utf-8")).items():
            with self.subTest(incident=key):
                self.assertEqual(
                    models.Incident.from_dict(original).to_dict(),
                    original,
                    "a field the app writes does not survive its own round trip",
                )

    def test_a_field_from_a_newer_build_refuses_without_calling_the_file_corrupt(self):
        """Version skew must refuse the write and NOT be reported as corruption.

        `to_dict` is `asdict()` over the fields this build knows, so a row written by a newer
        build loses that key on the round trip. Refusing is right -- writing would strip data
        the newer build stored. But the file is FINE: the remedy is to move this instance
        forward, not to repair a document, so it must not be classified with corruption.

        The reachable path is a ROLLBACK on one machine. `ledger_sync` deliberately does not
        sync the index ("Only the ledger. NOT the dispatch index", because the index is
        last-writer-wins and syncing it would let two instances each believe they own an
        incident), so this is NOT two instances sharing a file -- an earlier version of this
        docstring said it was, which First Principles caught. Design Review found the case
        itself: without it, one rolled-back instance refuses EVERY mutation forever while
        telling the operator to fix a healthy file.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        raw[good.incident_id]["a_field_from_the_future"] = {"nested": True}
        store.index_path().write_text(json.dumps(raw), encoding="utf-8")
        before = store.index_path().read_text(encoding="utf-8")

        with self.assertRaises(models.UnknownFieldError):
            store.claim(self._signal(native_id="alarm/new"), operating_mode=models.MODE_OBSERVE)

        self.assertEqual(
            store.index_path().read_text(encoding="utf-8"),
            before,
            "the newer instance's field was stripped",
        )
        # It IS still refused like corruption -- every caller that refuses one refuses both.
        self.assertIsInstance(
            models.UnknownFieldError("m", "d", 0),
            models.CorruptDocumentError,
        )

    def test_a_malformed_nested_field_is_not_silently_replaced(self):
        """The deepest shape door, and the one my own earlier note pointed at without my seeing it.

        `Incident.from_dict` COERCES these three nested fields instead of raising: `signal`
        to an empty `Signal`, `ledger_matches` to `[]`, `proposed_action` to `None`. So a row
        whose `"signal"` is `[]` loads clean, and the next mutation writes the empty
        substitute back -- permanently discarding the source, native id, title and labels
        that were on disk, with no parse failure and nothing logged.

        Found in review (GPT 5.6). Two rounds earlier I recorded that `from_dict` "essentially
        never raises" and concluded its raising branch was near-dead code; the correct
        conclusion was that its TOLERANCE is a data-loss door one level below the two I had
        just closed.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None

        for field, bad in (
            ("signal", []),
            ("ledger_matches", {"not": "a list"}),
            ("proposed_action", "not an object"),
        ):
            with self.subTest(field=field):
                raw = json.loads(store.index_path().read_text(encoding="utf-8"))
                raw[good.incident_id][field] = bad
                store.index_path().write_text(json.dumps(raw), encoding="utf-8")
                before = store.index_path().read_text(encoding="utf-8")

                with self.assertRaises(models.CorruptDocumentError):
                    store.claim(
                        self._signal(native_id="alarm/new"), operating_mode=models.MODE_OBSERVE
                    )

                self.assertEqual(
                    store.index_path().read_text(encoding="utf-8"),
                    before,
                    f"a malformed {field} was replaced with an empty substitute",
                )

    def test_an_absent_nested_field_is_still_a_normal_record(self):
        """Absent is not corrupt: these keys are missing on pre-feature records."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        for field in ("ledger_matches", "proposed_action"):
            raw[good.incident_id].pop(field, None)
        store.index_path().write_text(json.dumps(raw), encoding="utf-8")

        second = store.claim(
            self._signal(native_id="alarm/new"), operating_mode=models.MODE_OBSERVE
        )
        self.assertIsNotNone(second)
        self.assertIn(good.incident_id, store.read_index())

    def test_the_display_read_logs_every_structural_degradation(self):
        """The PR claims the display reads log when they degrade; they did not, for shape.

        A non-object root returned `{}` and a non-object row hit a bare `continue`, both
        silent -- so the description's promise was wider than the code. Found in review
        (GPT 5.6). The board must still render, which is why these log rather than raise.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        store.index_path().write_text("[]", encoding="utf-8")
        with self.assertLogs("kiro_crew", level="WARNING") as caught:
            self.assertEqual(store.read_index(), {})
        self.assertTrue(any("root is not an object" in m for m in caught.output))

        store.index_path().write_text('{"INV-1": "not an object"}', encoding="utf-8")
        with self.assertLogs("kiro_crew", level="WARNING") as caught:
            self.assertEqual(store.read_index(), {})
        self.assertTrue(any("not an object" in m for m in caught.output))

    def test_the_display_read_still_tolerates_a_skipped_entry(self):
        """The asymmetry again: one unreadable row must not blank the whole board."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        good = store.claim(self._signal(native_id="alarm/keep"), operating_mode=models.MODE_OBSERVE)
        assert good is not None
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        raw["INV-BROKEN"] = "this row is not an object"
        store.index_path().write_text(json.dumps(raw), encoding="utf-8")

        self.assertEqual(sorted(store.read_index()), [good.incident_id])

    def test_a_raising_read_still_releases_the_index_lock(self):
        """ "Raise while holding `_IndexLock`" became reachable for the first time here.

        `_read_index_unlocked` never raised -- it swallowed every failure -- so before
        this change no mutation could leave the `with _IndexLock():` block by exception,
        and nothing had to be exception-safe about the release. Now the strict read can
        raise inside that block on every one of the six mutations.

        If the release were not exception-safe the app would WEDGE rather than error:
        every later mutation would block forever on a lock nobody holds, and `flock` is
        per-descriptor so the same process deadlocks against itself. That is a worse
        outcome than the data loss this PR fixes, and it is the one thing the change puts
        at risk that no existing test could reach. Asserted behaviourally -- a second
        mutation must complete -- so a regression shows up as a hang under CI's timeout
        rather than as a passing test.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None

        with self._unreadable_index():
            with self.assertRaises(OSError):
                store.transition(inc.incident_id, models.STATUS_INVESTIGATING)

        # The lock must be free again, on the very next attempt.
        # flake-ok: pre-existing reacquire only a release regression could wedge; moved verbatim
        moved = store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        self.assertEqual(moved.status, models.STATUS_INVESTIGATING)
        # ...and the index must still hold exactly the one incident, not a second copy.
        self.assertEqual(list(store.read_index()), [inc.incident_id])
