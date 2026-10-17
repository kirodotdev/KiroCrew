"""Cloud backup retention: the count's writer, its gate and the last-sweep status.

``set_retention_keep`` and the round trip that makes the feature reachable, state that
is not UTF-8, the status field, a count changed during the listing, the one gate the
writer and the sweep share, and every terminal sweep state reaching the status read.

Part of the retention suite; ``test_aws_control_backup_retention.py`` says which AWS layer
each kind of test fakes, and no test reaches a network, a profile or a bucket.
"""

from __future__ import annotations

import datetime as dt
import inspect
import itertools
import json
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import aws_control_backup_retention_helpers
import pytest
from aws_control_backup_retention_helpers import (
    ACCOUNT,
    BUCKET,
    INSTALL,
    PROFILE,
    REGION,
    _archives,
    _key_of,
    _prune,
    _write_account,
)

from kiro_crew.apps.builtins.aws_control.backend import backup, storage
from kiro_crew.deploy.engine import AWSError

# The shared fixtures, bound here by name so pytest registers them for this module.
audit = aws_control_backup_retention_helpers.audit
drive = aws_control_backup_retention_helpers.drive
state = aws_control_backup_retention_helpers.state


#: The real gate, captured before the ``drive`` fixture stubs it out, for the tests
#: that need its own refusal shapes rather than a stand-in for them.
_REAL_AUTHORIZE_UPLOAD = backup._authorize_upload


class TestSetRetentionKeep:
    """The writer, and the round trip that makes the feature reachable at all.

    A switch nothing can set removes the harm for nobody, so the end-to-end path
    matters more than the reader in isolation: write a count, and the very next sweep
    must prune to it; clear it, and the next sweep must keep everything again.
    """

    def test_writing_a_count_turns_the_next_sweep_on(self, drive, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        # Off first, asserted rather than assumed, so the change below is the cause.
        assert _prune(drive)["keep"] == "off"
        assert drive.deleted == []
        backup.set_retention_keep(ACCOUNT, 2)
        result = _prune(drive)
        assert result["keep"] == 2
        assert result["retired"] == 2

    def test_clearing_it_turns_the_next_sweep_back_off(self, drive, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 2)
        backup.set_retention_keep(ACCOUNT, None)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        result = _prune(drive)
        assert result["keep"] == "off"
        assert drive.deleted == []

    def test_clearing_removes_the_key_rather_than_storing_a_sentinel(
        self, state, tmp_path, monkeypatch
    ):
        # One representation of off, so nothing downstream has to know a second one.
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        backup.set_retention_keep(ACCOUNT, 3)
        backup.set_retention_keep(ACCOUNT, None)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert backup.RETENTION_KEEP_STATE_KEY not in doc["accounts"][ACCOUNT]

    def test_it_does_not_touch_the_nightly_grant(self, state, tmp_path, monkeypatch):
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        backup.set_nightly(ACCOUNT, True)
        backup.set_retention_keep(ACCOUNT, 4)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["accounts"][ACCOUNT]["nightly"] is True
        assert doc["accounts"][ACCOUNT][backup.RETENTION_KEEP_STATE_KEY] == 4

    def test_true_is_refused_at_the_write_as_well_as_the_read(self, state, tmp_path, monkeypatch):
        # The route validates too, but this key's whole safety rests on the bool screen,
        # so a direct caller must not be able to store keep=1 by sending a flag.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, True)

    def test_a_non_integer_is_refused(self, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, "3")

    def test_an_out_of_range_count_is_refused_rather_than_clamped(
        self, state, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, 0)
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, -1)
        # No upper bound: a count larger than the archives that exist keeps all of
        # them, so refusing it would refuse an operator over nothing.
        backup.set_retention_keep(ACCOUNT, 10_000)
        assert backup.retention_keep(ACCOUNT) == 10_000

    def test_the_public_reader_reports_the_effective_count(self, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        assert backup.retention_keep(ACCOUNT) is None
        backup.set_retention_keep(ACCOUNT, 6)
        assert backup.retention_keep(ACCOUNT) == 6


class TestNonUtf8StateIsUnreadableNotAnException:
    """Bytes that are not UTF-8 are the "exists but could not be read" case.

    `read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which is a `ValueError`
    and NOT an `OSError`, so a reader catching only `OSError` lets it escape. That
    matters most at the sweep, which resolves its count before entering its own
    best-effort handler and whose call site promises retention cannot fail the run: an
    escaping decode error would report a backup already off-host as failed.

    Both readers treat it as unreadable, and neither overwrites the file. The sweep
    answers `({}, False)` and keeps every archive; the read-modify-write abandons the
    mutation. Repair-on-write covers a document that DECODED and then failed to parse,
    which these bytes never did.
    """

    def _corrupt(self, tmp_path, monkeypatch):
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        # Invalid UTF-8 rather than merely invalid JSON: 0x80 is a continuation byte
        # with no lead byte, so the decode fails before any parse is attempted.
        path.write_bytes(b'{"accounts": {"x": "\x80\x81"}}')
        return path

    def test_the_checked_reader_answers_unreadable(self, tmp_path, monkeypatch):
        self._corrupt(tmp_path, monkeypatch)
        assert backup._read_state_checked() == ({}, False)

    def test_the_sweep_keeps_everything_instead_of_raising(self, drive, tmp_path, monkeypatch):
        self._corrupt(tmp_path, monkeypatch)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        result = backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(drive.rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        # Returning at all is half the assertion: the defect was an exception escaping.
        assert result["keep"] == "off"
        assert result["skipped"] == "the retention setting could not be read"
        assert drive.deleted == []

    def test_the_sweep_still_audits_the_unreadable_state_as_a_failure(
        self, drive, tmp_path, monkeypatch, audit
    ):
        # The escaping exception skipped this branch entirely, so an operator whose
        # configured count stopped being readable got no audit entry at all.
        self._corrupt(tmp_path, monkeypatch)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(drive.rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "could not be read" in audit[0]["error"]

    def test_the_write_path_abandons_the_mutation_and_leaves_the_bytes_alone(
        self, tmp_path, monkeypatch
    ):
        # Repair-on-write is for a document that PARSED to nothing usable. These bytes
        # never reached the parser, so publishing over them would replace every
        # account's toggles, retention count and run history -- including the
        # `upload_versions` records the sweep's ownership test reads -- on the strength
        # of a document nobody read.
        path = self._corrupt(tmp_path, monkeypatch)
        before = path.read_bytes()
        with pytest.raises(OSError) as caught:
            backup.set_retention_keep(ACCOUNT, 3)
        assert isinstance(caught.value, backup._StateUnreadable)
        assert path.read_bytes() == before

    def test_the_abandoning_error_stays_an_oserror_for_the_existing_handler(
        self, tmp_path, monkeypatch
    ):
        # `set_nightly` lets this reach its own handler, which is written against
        # `OSError`, so the decode case must not arrive as a bare `ValueError`.
        self._corrupt(tmp_path, monkeypatch)
        with pytest.raises(OSError):
            backup.set_nightly(ACCOUNT, True)

    def test_a_document_that_decoded_but_did_not_parse_is_still_repaired(
        self, tmp_path, monkeypatch
    ):
        # The other side of the split, so the change above does not quietly widen into
        # abandoning every corrupt file: this one decoded, so it is the corrupt kind.
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        path.write_text("{not json at all", encoding="utf-8")
        backup.set_retention_keep(ACCOUNT, 3)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["accounts"][ACCOUNT][backup.RETENTION_KEEP_STATE_KEY] == 3

    def test_a_decode_error_is_not_swallowed_as_a_bare_value_error(self, tmp_path, monkeypatch):
        # The catch names UnicodeDecodeError, not ValueError, so a surprising
        # ValueError from the read stays loud instead of reading as "unreadable".
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text("{}", encoding="utf-8")

        def _boom(*_a, **_k):
            raise ValueError("not a decode problem")

        monkeypatch.setattr(Path, "read_text", _boom)
        with pytest.raises(ValueError):
            backup._read_state_checked()


class TestTheStatusFieldDoesNotClaimAConsumerItDoesNotHave:
    """`retentionKeep` is readable over HTTP and nothing renders it.

    The count is set and read by raw request only. A docstring promising a console
    control sends the next reader looking for a panel that is not there, and makes the
    shipped surface sound larger than it is.
    """

    def test_the_reader_does_not_claim_a_console_renderer(self):
        doc = backup.retention_keep.__doc__ or ""
        assert "console renders" not in doc
        # The absence is stated rather than merely unclaimed, so the gap is disclosed
        # where someone deciding whether to build the panel will find it.
        assert "NO\n    console renderer ships" in doc or "NO console renderer" in doc

    def test_the_count_survives_a_round_trip_without_any_frontend(self, tmp_path, monkeypatch):
        # Why the field stays despite having no renderer: without it the route is
        # write-only and an operator cannot confirm what was stored.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 4)
        assert backup.retention_keep(ACCOUNT) == 4
        backup.set_retention_keep(ACCOUNT, None)
        assert backup.retention_keep(ACCOUNT) is None


class TestARetentionChangeDuringTheListingStopsTheDelete:
    """The count is re-read at the delete, not trusted from before the listing.

    `list_object_versions` is a network round trip after a build that may have taken
    minutes. An owner who switches retention off inside that window has chosen to keep
    these versions, and the deletion is permanent, so a count read before the listing
    cannot authorize a delete that happens after it. The consent gate beside it already
    works this way.
    """

    def _flip_during_listing(self, new_value):
        """Return a patched lister that changes the stored count as it serves rows."""
        original = backup.storage.list_object_versions

        def flipping(*args, **kwargs):
            rows = original(*args, **kwargs)
            backup.set_retention_keep(ACCOUNT, new_value)
            return rows

        return flipping

    def test_switching_retention_off_mid_sweep_deletes_nothing(
        self, drive, state, monkeypatch, audit
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(None))
        result = _prune(drive)
        assert drive.deleted == []
        assert result["retired"] == 0
        # An owner changing their mind is not a failure of the sweep.
        assert [entry["outcome"] for entry in audit] == ["successful"]

    def test_raising_the_count_mid_sweep_deletes_nothing(self, drive, state, monkeypatch):
        # keep=1 selected the two older archives; keep=3 protects them, so the
        # candidate set computed under the old count is stale.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(3))
        result = _prune(drive)
        assert drive.deleted == []
        assert "changed before deletion" in result["skipped"]

    def test_lowering_the_count_mid_sweep_still_deletes_the_selected_set(
        self, drive, state, monkeypatch
    ):
        # The other direction is safe and must NOT refuse: keep=1 authorizes everything
        # keep=2 selected, so refusing here would be a sweep that never runs whenever
        # the setting happens to be touched.
        state(2)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(1))
        result = _prune(drive)
        assert drive.deleted != []
        assert result["retired"] >= 1

    def test_the_delete_is_not_serialized_behind_the_run_lock(self):
        # The fix must not be "hold `_run_lock` through the purge": that lock also
        # serializes `last_runs`, so a status read would stall for the length of a
        # network deletion. Matching the ACQUISITION rather than the bare name, which
        # the comment explaining this decision also contains.
        source = inspect.getsource(backup._prune_remote_archives)
        assert "with _run_lock" not in source


class TestTheWriterAndTheSweepShareOneGate:
    """A lock only one side takes protects nothing.

    The sweep's final count check and its delete are one critical section. That is
    worth nothing unless `set_retention_keep` takes the same lock, so this pins the
    writer's half by BEHAVIOUR -- it must not be able to complete while the gate is
    held -- rather than by reading the source for a lock name.
    """

    def test_setting_the_count_waits_for_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        finished = threading.Event()

        def write():
            backup.set_retention_keep(ACCOUNT, 3)
            finished.set()

        with backup._RETENTION_GATE:
            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            # Held: the writer must be parked, not racing ahead of a sweep that has
            # already checked the count and is about to call S3.
            # flake-ok: pre-existing 0.5 s window on the absent finish while the gate is held, moved verbatim; tracked for a de-flake pass
            assert not finished.wait(timeout=0.5)
        # Released: it proceeds, so the wait above was the lock and not a dead thread.
        assert finished.wait(timeout=5)
        worker.join(timeout=5)
        assert backup.retention_keep(ACCOUNT) == 3

    def test_the_gate_is_not_the_run_lock(self):
        # `_run_lock` also serializes `last_runs`, so sharing it would stall a status
        # read for the length of a purge. They must be different objects.
        assert backup._RETENTION_GATE is not backup._run_lock

    def test_consent_is_rechecked_with_the_gate_already_held(self, drive, state, monkeypatch):
        # Acquiring these locks can wait on another purge, so a consent check taken
        # before the wait can be stale by the time S3 is called. Asserting the gate is
        # ALREADY held when authorization runs is what shows the check moved inside
        # rather than merely being called twice.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        held: list[bool] = []
        real = backup._authorize_upload

        def recording(*args, **kwargs):
            held.append(backup._RETENTION_GATE.locked())
            return real(*args, **kwargs)

        monkeypatch.setattr(backup, "_authorize_upload", recording)
        _prune(drive)
        # The sweep authorizes before the listing too, so the LAST one is the one that
        # must be inside: that is the one immediately before the delete. Asserting the
        # FIRST one is OUTSIDE is what makes this non-vacuous -- the two calls differ in
        # lock state within a single run, so `True` is not something every call returns.
        assert held[0] is False
        assert held[-1] is True
        assert len(held) >= 2
        assert drive.deleted != []

    def test_the_delete_takes_the_same_sidecar_lock_the_state_writes_take(
        self, drive, state, monkeypatch
    ):
        # A `threading.Lock` orders threads and nothing else. This module documents a
        # second install sharing one bucket and one state file by design, so ordering
        # against another PROCESS needs the state file's own sidecar lock -- the same
        # path `_locked_state_update` takes. Asserting the path is what shows the two
        # are the same lock rather than two locks that merely both exist.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        opened: list[str] = []
        real = backup.open_lock_file

        def recording(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(backup, "open_lock_file", recording)
        _prune(drive)
        expected = str(backup._state_path().with_suffix(".lock"))
        assert expected in opened
        # And it is held for the delete, not merely opened somewhere in the sweep.
        assert drive.deleted != []

    def test_one_call_writes_the_state_exactly_once(self, tmp_path, monkeypatch):
        # A SECOND write outside the gate is not merely redundant: with a concurrent
        # clear it can land afterwards and restore the count, turning retention back on
        # so a later push deletes archives the operator had just stopped protecting.
        # Counting the writes is what catches that; asserting the final value does not,
        # because both writes store the same thing when nothing races them.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real = backup._locked_state_update
        calls = []

        def counting(mutate):
            calls.append(mutate)
            return real(mutate)

        monkeypatch.setattr(backup, "_locked_state_update", counting)
        backup.set_retention_keep(ACCOUNT, 3)
        assert len(calls) == 1

    def test_clearing_also_writes_exactly_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 3)
        real = backup._locked_state_update
        calls = []

        def counting(mutate):
            calls.append(mutate)
            return real(mutate)

        monkeypatch.setattr(backup, "_locked_state_update", counting)
        backup.set_retention_keep(ACCOUNT, None)
        assert len(calls) == 1
        assert backup.retention_keep(ACCOUNT) is None


def _last(kind: str = backup.KIND_SNAPSHOT) -> dict[str, Any]:
    """The status read's record of how ``kind``'s last sweep ended."""
    return backup.retention_last_sweep(ACCOUNT)[kind]


class TestEveryTerminalSweepStateReachesTheStatusRead:
    """How the last sweep ended is served, and a refusal is served like any other end.

    The SEL event and a log line are not where an operator reads whether retention is
    working; the status read is. Without this record it serves the configured count
    and nothing about whether a sweep acted on it, so a sweep refused every night reads
    exactly like a working one: retention on, the bill not falling, and nothing in the
    app saying why. Unlike the unclaimed pair, this record is written on the refusing
    paths too, because the refusal is the fact.
    """

    def test_nothing_is_served_before_any_sweep_has_ended(self, state):
        # An absent kind means "no sweep has ended", never "nothing happened".
        state(3)
        assert backup.retention_last_sweep(ACCOUNT) == {}

    def test_a_sweep_that_retired_archives_says_how_many_and_when(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        out = _prune(drive)
        last = _last()
        assert (last["result"], last["skipped"]) == ("successful", "")
        # The same two counts the audit event carries, so the two can be read together.
        assert (last["retired"], last["versions"]) == (out["retired"], out["versions"]) == (2, 2)
        # Parsed rather than merely truthy: the stamp is what tells a reader whether
        # this is last night's sweep or one from a month ago.
        assert dt.datetime.fromisoformat(last["at"]).tzinfo is not None

    def test_a_sweep_with_nothing_to_retire_is_published_as_a_measured_zero(self, drive, state):
        state(3)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        last = _last()
        assert (last["result"], last["skipped"], last["retired"]) == ("successful", "", 0)

    def test_retention_not_enabled_is_published_as_a_successful_sweep(self, drive, state):
        # The ordinary state of an unconfigured install: nothing to do, done.
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        _prune(drive)
        assert drive.deleted == []
        last = _last()
        assert (last["result"], last["skipped"]) == ("successful", "retention is not enabled")

    def test_a_count_somebody_wrote_and_the_sweep_cannot_use_is_published_as_failed(
        self, drive, state
    ):
        # The anomaly half of "off": an operator configured something that is not in
        # force, which is exactly what the status read must stop hiding.
        state("3")
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        _prune(drive)
        assert drive.deleted == []
        last = _last()
        assert last["result"] == "failed"
        assert last["skipped"] == "the retention setting could not be read"

    @pytest.mark.parametrize(
        ("error", "result"),
        [
            (RuntimeError("S3 consent was withdrawn"), "refused"),
            # A credential to renew is not a consent somebody withdrew: the audit event
            # files it `failed`, and the status read must say the same thing.
            (AWSError("expired token for profile p"), "failed"),
        ],
        ids=["gate-refusal", "credential-failure"],
    )
    def test_an_authorization_gate_ending_matches_its_audit_result(
        self, drive, state, audit, monkeypatch, error, result
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        monkeypatch.setattr(
            backup, "_authorize_upload", lambda *a, **k: (_ for _ in ()).throw(error)
        )
        _prune(drive)
        assert drive.listed == []
        last = _last()
        assert (last["result"], last["skipped"]) == (result, "authorization refused")
        # A bare RuntimeError stands in for `_refuse_upload`, which files its own
        # `denied` event before raising, so only the credential failure is filed here.
        assert [entry["outcome"] for entry in audit] == ([] if result == "refused" else [result])
        # The error's own text stays in the audit event, where it is redacted for that
        # record, and out of a payload every poller reads.
        assert str(error) not in json.dumps(last)

    @pytest.mark.parametrize(
        ("error", "result"),
        [
            (RuntimeError("consent withdrawn"), "refused"),
            (AWSError("expired token for profile p"), "failed"),
        ],
        ids=["gate-refusal", "credential-failure"],
    )
    def test_authorization_lost_before_the_delete_matches_its_audit_result(
        self, drive, state, monkeypatch, error, result
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        seen = {"n": 0}

        def gate(*a: Any, **k: Any) -> None:
            seen["n"] += 1
            if seen["n"] > 1:
                raise error

        monkeypatch.setattr(backup, "_authorize_upload", gate)
        _prune(drive)
        assert drive.deleted == []
        last = _last()
        assert last["result"] == result
        assert last["skipped"] == "authorization withdrawn before deletion"
        assert last["retired"] == 0

    @pytest.mark.parametrize("at_gate", [1, 2], ids=["first-gate", "second-gate"])
    def test_a_shutdown_refusal_is_published_as_failed_like_its_audit(
        self, drive, state, audit, monkeypatch, at_gate
    ):
        # The gate's teardown refusal is the one `_refuse_upload` files as `failed`
        # rather than `denied`: a restart is not an access decision. The real gate
        # runs here up to the stop check -- everything before it is stubbed to pass --
        # and the app starts stopping just before the chosen gate call.
        state(1)
        backup.clear_stop()
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            "kiro_crew.deploy.engine._checked",
            lambda *a, **k: json.dumps({"Account": ACCOUNT}),
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda name: True)
        grant = mock.Mock(account=ACCOUNT)
        monkeypatch.setattr("kiro_crew.aws_consent.read_grant", lambda service: grant)
        monkeypatch.setattr("kiro_crew.aws_consent.is_granted", lambda *a, **k: (True, ""))
        real = _REAL_AUTHORIZE_UPLOAD
        calls = {"n": 0}

        def gate(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == at_gate:
                backup.signal_stop()
            real(*args, **kwargs)

        monkeypatch.setattr(backup, "_authorize_upload", gate)
        try:
            _prune(drive)
        finally:
            backup.clear_stop()
        assert drive.deleted == []
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "shutting down" in audit[0]["error"]
        assert _last()["result"] == "failed"

    def test_an_access_decision_during_a_shutdown_is_still_published_as_refused(
        self, drive, state, audit, monkeypatch
    ):
        # Disabling the app sets the stop flag AND makes the app-enabled check refuse,
        # which runs before the teardown check. The gate files that as `denied`, so the
        # published result must be `refused` -- reading the stop flag instead of the
        # refusal itself would relabel it.
        state(1)
        backup.clear_stop()
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            "kiro_crew.deploy.engine._checked",
            lambda *a, **k: json.dumps({"Account": ACCOUNT}),
        )
        enabled = {"on": True}
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda name: enabled["on"])
        grant = mock.Mock(account=ACCOUNT)
        monkeypatch.setattr("kiro_crew.aws_consent.read_grant", lambda service: grant)
        monkeypatch.setattr("kiro_crew.aws_consent.is_granted", lambda *a, **k: (True, ""))
        calls = {"n": 0}

        def gate(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                enabled["on"] = False
                backup.signal_stop()
            _REAL_AUTHORIZE_UPLOAD(*args, **kwargs)

        monkeypatch.setattr(backup, "_authorize_upload", gate)
        try:
            _prune(drive)
        finally:
            backup.clear_stop()
        assert drive.deleted == []
        assert [entry["outcome"] for entry in audit] == ["denied"]
        assert "disabled" in audit[0]["error"]
        assert _last()["result"] == "refused"

    def test_the_standing_record_is_never_older_than_the_one_it_replaced(
        self, drive, state, monkeypatch
    ):
        # Two same-kind sweeps in separate processes reach the sidecar lock in either
        # order. Here the first recorder is held at the lock while a second one commits,
        # then resumes. Whichever commits last is the record a reader sees, so it must
        # carry the later stamp -- a stamp taken before the wait would let it claim to be
        # older than the record it just overwrote.
        #
        # The clock is pinned to strictly increasing instants, one per read, so the
        # order asserted below is the order of the reads and not a property of the
        # host's clock resolution: two live reads can land in one tick.
        state(1)
        reads = itertools.count()
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.side_effect = lambda *a, **k: dt.datetime(
            2026, 1, 1, tzinfo=dt.timezone.utc
        ) + dt.timedelta(seconds=next(reads))
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        real = backup._locked_state_update
        overtaken: dict[str, str] = {}

        def overtaking(mutate: Any, *args: Any, **kwargs: Any) -> Any:
            if not overtaken:
                overtaken["at"] = ""
                backup._record_last_sweep(
                    ACCOUNT,
                    backup.KIND_SNAPSHOT,
                    {"skipped": "", "retired": 2, "versions": 2},
                    "successful",
                )
                overtaken["at"] = _last()["at"]
            return real(mutate, *args, **kwargs)

        monkeypatch.setattr(backup, "_locked_state_update", overtaking)
        backup._record_last_sweep(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            {"skipped": "authorization refused", "retired": 0, "versions": 0},
            "refused",
        )
        standing = _last()
        assert standing["result"] == "refused"
        assert dt.datetime.fromisoformat(standing["at"]) > dt.datetime.fromisoformat(
            overtaken["at"]
        )

    def test_a_listing_the_sweep_would_not_act_on_is_published_as_failed(self, drive, state):
        # The unclaimed pair is deliberately NOT written here, because the listing
        # cannot be trusted about what it omitted. The refusal itself is trustworthy.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive, newest=f"snapshots/{INSTALL}/absent.tar.gz")
        assert drive.deleted == []
        assert backup.retention_unclaimed(ACCOUNT) == {}
        last = _last()
        assert last["result"] == "failed"
        assert "does not show the archive" in last["skipped"]

    @pytest.mark.parametrize(
        ("new_count", "reason"),
        [
            (None, "retention is not enabled"),
            (3, "the retention count changed before deletion"),
        ],
        ids=["switched-off", "raised"],
    )
    def test_a_count_withdrawn_mid_sweep_is_published_with_its_reason(
        self, drive, state, monkeypatch, new_count, reason
    ):
        # An owner changing their mind is not a failure, matching the audit result.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        original = backup.storage.list_object_versions

        def flipping(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            rows = original(*args, **kwargs)
            backup.set_retention_keep(ACCOUNT, new_count)
            return rows

        monkeypatch.setattr(backup.storage, "list_object_versions", flipping)
        _prune(drive)
        assert drive.deleted == []
        last = _last()
        assert (last["result"], last["skipped"], last["retired"]) == ("successful", reason, 0)

    def test_a_count_garbled_mid_sweep_is_published_as_failed(self, drive, state, monkeypatch):
        # The anomaly half of the re-read, matching its `failed` audit: a value somebody
        # wrote and the sweep cannot use is not an owner changing their mind.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        original = backup.storage.list_object_versions

        def garbling(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            rows = original(*args, **kwargs)
            _write_account({backup.RETENTION_KEEP_STATE_KEY: "3"})
            return rows

        monkeypatch.setattr(backup.storage, "list_object_versions", garbling)
        _prune(drive)
        assert drive.deleted == []
        last = _last()
        assert last["result"] == "failed"
        assert last["skipped"] == "the retention setting could not be read"

    def test_a_failed_listing_is_published_as_cleanup_failed(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.list_error = AWSError("access denied for arn:aws:iam::111122223333:role/r")
        _prune(drive)
        last = _last()
        assert (last["result"], last["skipped"], last["versions"]) == (
            "failed",
            "cleanup failed",
            0,
        )
        assert "arn:aws" not in json.dumps(last)

    def test_a_purge_that_failed_halfway_says_what_it_erased(self, drive, state, monkeypatch):
        # Those versions are gone for good, so "failed, nothing erased" would be the one
        # shape a reader must not be handed.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            backup.storage,
            "delete_object_versions",
            lambda *a, **k: (_ for _ in ()).throw(
                storage.PartialVersionDelete(1, AWSError("throttled"))
            ),
        )
        _prune(drive)
        last = _last()
        assert (last["result"], last["skipped"]) == ("failed", "cleanup failed")
        assert (last["retired"], last["versions"]) == (0, 1)

    def test_a_refusal_replaces_an_earlier_success(self, drive, state, monkeypatch):
        # The reported defect in one test: a sweep that worked once and has refused
        # since must not keep reading as working.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        _prune(drive)
        assert _last()["result"] == "successful"
        monkeypatch.setattr(
            backup,
            "_authorize_upload",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("S3 consent was withdrawn")),
        )
        _prune(drive)
        last = _last()
        assert (last["result"], last["skipped"], last["retired"]) == (
            "refused",
            "authorization refused",
            0,
        )

    def test_each_kind_keeps_its_own_result(self, drive, state, monkeypatch):
        # Per kind because the sweep is: a refusal of the sessions sweep must not
        # overwrite what the snapshot sweep did, or the reverse.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        _prune(drive)
        monkeypatch.setattr(
            backup,
            "_authorize_upload",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("S3 consent was withdrawn")),
        )
        drive.rows = _archives(backup.KIND_SESSIONS, ["01", "02"])
        _prune(drive, backup.KIND_SESSIONS)
        assert _last(backup.KIND_SNAPSHOT)["result"] == "successful"
        assert _last(backup.KIND_SESSIONS)["result"] == "refused"

    def test_a_state_write_failure_does_not_fail_the_sweep(self, drive, state, monkeypatch):
        # Best-effort like the recorders beside it: the archive is already off-host, so
        # nothing this write can fail at may turn a successful backup into a failed one.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])

        def _boom(*a: Any, **k: Any) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(backup, "write_state", _boom)
        out = _prune(drive)
        assert out["retired"] == 2
        assert len(drive.deleted) == 2
        assert backup.retention_last_sweep(ACCOUNT) == {}

    def test_a_corrupted_record_reads_as_nothing_and_the_next_sweep_repairs_it(self, drive, state):
        # A polled endpoint must not raise on a hand-edited document.
        state(1)
        _write_account({backup.RETENTION_LAST_SWEEP_STATE_KEY: "not a map"})
        assert backup.retention_last_sweep(ACCOUNT) == {}
        _write_account({backup.RETENTION_LAST_SWEEP_STATE_KEY: {backup.KIND_SNAPSHOT: "nope"}})
        assert backup.retention_last_sweep(ACCOUNT) == {}
        _write_account({backup.RETENTION_LAST_SWEEP_STATE_KEY: "not a map"})
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        assert _last()["result"] == "successful"

    def test_the_reader_does_not_claim_a_console_renderer(self):
        doc = backup.retention_last_sweep.__doc__ or ""
        assert "NO console renderer" in doc
