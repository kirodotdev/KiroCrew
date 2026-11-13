"""Pins the operator's Layer B permission gate on the sessions archive.

Covers the default withhold, the per-account grant in the fenced state directory, the
``conversations/`` scope marker, the SEL events, and the ``conversations_skipped``
reasons that hold back the retention sweep.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from aws_control_backup_helpers import (
    _SCOPED,
    ACCOUNT,
    _SessionsArchiveLayerBGateHarness,
)

from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.config import loader

# Layer B in the sessions archive -- the operator's standing permission


class TestSessionsArchiveLayerBGatePermission(_SessionsArchiveLayerBGateHarness):
    """The kiro-cli half rides only when the operator has permitted it.

    Layer B is the byte-exact unredacted model context window, so the archive needs
    the same operator opt-in as the file export. Mid-build changes are covered in
    ``test_aws_control_backup_layer_b_mid_build.py``.
    """

    # -- the block path ----------------------------------------------------

    def test_layer_b_is_withheld_by_default(self, tmp_path, monkeypatch):
        """No permission recorded: the transcript rides and the kiro-cli half does not."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        # No state file: the permission is read through the real function, not stubbed.

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        # Stated, not inferred from an absent key: this is what tells a reader the
        # archive cannot resume a session with full fidelity.
        assert record["layer_b"] is False

    def test_a_non_boolean_permission_withholds(self, tmp_path, monkeypatch):
        """A value this function cannot understand must not widen what leaves."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        # "true" is a string, and `bool("true")` is True -- the exact coercion that
        # would turn a hand-mangled store into an unintended grant.
        self._store(tmp_path, "true")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_an_unreadable_store_withholds_rather_than_failing(self, tmp_path, monkeypatch):
        """A state file that cannot be parsed withholds; the backup still runs."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        (tmp_path / "backup.json").write_text("{ not json", encoding="utf-8")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the allow path ----------------------------------------------------

    def test_the_permission_lets_both_halves_ride(self, tmp_path, monkeypatch):
        """With the recorded permission the archive carries Layer B, byte-exact."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["layer_b"] is True

    # -- the terminal conversation export rides the SAME permission ---------

    def test_withholding_also_withholds_the_conversation_export(self, tmp_path, monkeypatch):
        """A withheld run carries no `conversations/` root, store present or not.

        The export holds what the model saw, unredacted: the same data class as
        Layer B, and an uploaded object cannot be un-sent.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        # No permission recorded: read through the real function, not stubbed.

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_the_permission_lets_the_conversation_export_ride(self, tmp_path, monkeypatch):
        """With the permission the `conversations/` root rides and is recorded."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == sorted(
            [
                "crew/t.jsonl",
                "cli/abc.json",
                "cli/abc.jsonl",
                backup._CONVERSATIONS_DB_ARCNAME,
                backup._CONVERSATIONS_MANIFEST_ARCNAME,
            ]
        )
        assert record["layer_b"] is True

    def test_an_emitted_but_empty_export_is_not_discarded_as_nothing_to_archive(
        self, tmp_path, monkeypatch
    ):
        """Zero rows but a real `conversations/` root still uploads.

        An empty table still emits two members, so the "no session files to archive"
        guard must count members, not rows. Both transcript halves are empty here.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        empty_cli = tmp_path / "cli_sessions"
        empty_cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: empty_cli)
        self._synthetic_store(tmp_path, monkeypatch, rows=0)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == sorted(
            [backup._CONVERSATIONS_DB_ARCNAME, backup._CONVERSATIONS_MANIFEST_ARCNAME]
        )
        # The archive carries a conversations root, so the record must say so even
        # though no row and no cli file rode.
        assert record["layer_b"] is True

    def test_a_store_refused_for_a_link_is_not_reported_as_absent(self, tmp_path, monkeypatch):
        """A candidate this host HAS but the lookup refuses reports a reason.

        A symlinked home refuses the store on every run. Only "no store" is safe to
        prune on: a refused store may sit in an earlier archive this one lacks.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: False)
        monkeypatch.setattr(
            backup, "_kiro_cli_conversation_db", lambda: (None, "store_rejected_link")
        )

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_rejected_link"
        assert prune.call_count == 0

    def test_a_declined_sweep_still_files_its_audit_event(self, tmp_path, monkeypatch):
        """Suppressing the sweep suppresses the DELETION, never the audit.

        Every terminal sweep outcome files a SEL event, and a decline is one. The event
        shows an auditor why archives pile up past the keep count.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            with mock.patch.object(backup, "_audit_retention") as audit:
                record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_relocated_outside_fence"
        assert prune.call_count == 0
        assert audit.call_count == 1
        # The event must name why, or an auditor sees a sweep that did nothing and
        # cannot tell it from one that was never configured.
        assert audit.call_args.kwargs["result"] == "failed"
        assert "store_relocated_outside_fence" in audit.call_args.kwargs["error"]
        # The reason travels as `error`; the event's outcome payload is unchanged.
        assert audit.call_args.args[1]["skipped"] == ""

    def test_a_declined_sweep_is_published_to_the_status_read(self, tmp_path, monkeypatch):
        """A decline replaces the sessions row's last result instead of leaving it stale.

        A decline never reaches `_prune_remote_archives`, so without its own write a
        nightly decline would keep reading as the last success.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        stale = {
            "result": "successful",
            "skipped": "",
            "retired": 2,
            "versions": 2,
            "at": "2026-01-01T00:00:00+00:00",
        }
        doc = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        doc["accounts"][ACCOUNT][backup.RETENTION_LAST_SWEEP_STATE_KEY] = {
            backup.KIND_SESSIONS: stale
        }
        (tmp_path / "backup.json").write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            self._run_capturing_names(monkeypatch)

        assert prune.call_count == 0
        last = backup.retention_last_sweep(ACCOUNT)[backup.KIND_SESSIONS]
        assert last["result"] == "failed"
        assert "store_relocated_outside_fence" in last["skipped"]
        assert last["retired"] == 0
        assert last["at"] != stale["at"]

    def test_a_relocated_store_is_reported_not_silently_skipped(self, tmp_path, monkeypatch):
        """A store the environment re-roots is skipped AND named in the run record.

        Skipping is the security decision; skipping silently would let the operator
        believe the archive holds conversations it does not.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True)
        # Stub the flag, not `XDG_DATA_HOME`: on macOS the env var never sets it.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        record, names = self._run_capturing_names(monkeypatch)

        # The refusal is reported, never escalated into a failed backup.
        assert names == ["cli/abc.json", "crew/t.jsonl"]
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_a_run_that_skipped_nothing_carries_no_skip_key(self, tmp_path, monkeypatch):
        """No relocation means no `conversations_skipped` key at all.

        The key's presence is itself the signal.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        record, names = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record

    @staticmethod
    def _run_twice_over_an_unchanged_tree(monkeypatch):
        """Run the sessions backup twice. Returns both records.

        The fakes must echo the uploaded version and length: `_unchanged_baseline`
        HEADs the key, and without proof the second run would upload again.
        """
        uploaded: dict[str, int] = {}

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return None
            uploaded[key] = Path(local_path).stat().st_size
            return "v1"

        def fake_head(profile, region, bucket, section, key, *, account):
            size = uploaded.get(key)
            return None if size is None else {"ContentLength": size, "VersionId": "v1"}

        records = []
        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            mock.patch.object(backup.storage, "head_object_meta", side_effect=fake_head),
        ):
            for _ in range(2):
                records.append(
                    backup.run_sessions_backup(
                        ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                    )
                )
        return records

    def test_the_skip_key_survives_an_unchanged_run(self, tmp_path, monkeypatch):
        """An unchanged run must not erase the coverage facts of the run before it.

        `_record_skip` REPLACES the run slot, and the unchanged branch is the ordinary
        nightly case, so a dropped field would claim full coverage over a gap.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True)
        # Stub the flag, not `XDG_DATA_HOME`: on macOS the env var never sets it.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        first, second = self._run_twice_over_an_unchanged_tree(monkeypatch)

        assert first["conversations_skipped"] == "store_relocated_outside_fence"
        assert second.get("uploaded") is False, "second run should take the skip branch"
        assert second["conversations_skipped"] == "store_relocated_outside_fence"
        assert second["layer_b"] is True

    def test_a_stale_store_at_the_fixed_anchor_does_not_mask_a_relocation(
        self, tmp_path, monkeypatch
    ):
        """A leftover at the fixed anchor must not be exported once the env has moved.

        The relocation check must run BEFORE the fixed-candidate lookup. Here both a
        readable store at the fixed anchor and an active relocation exist.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        # A perfectly readable store at the fixed anchor -- the stale leftover.
        self._synthetic_store(tmp_path, monkeypatch, rows=5)
        self._store(tmp_path, True)
        # And an active relocation. Stub the flag, not `XDG_DATA_HOME`: on macOS the
        # env var never sets it.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"], "the stale store must not be exported"
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_an_unreadable_store_is_named_in_the_record(self, tmp_path, monkeypatch):
        """A store that cannot be read is reported, not silently skipped.

        The archive still uploads and reports success, so the record is the only place
        the operator sees the gap.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # Not a SQLite file at all, so the read raises inside the export.
        junk = tmp_path / "data.sqlite3"
        junk.write_bytes(b"this is not a database\n")
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (junk, ""))

        record, names = self._run_capturing_names(monkeypatch)

        assert not [n for n in names if n.startswith("conversations/")]
        assert record["conversations_skipped"] == "store_unreadable"

    def test_an_incomplete_export_does_not_let_retention_retire_the_older_archive(
        self, tmp_path, monkeypatch
    ):
        """An export that came up short must not trigger the retention sweep.

        At `keep=1` the sweep would erase the one archive still holding the
        conversations; the gap recovers next run, the deleted versions do not.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        junk = tmp_path / "data.sqlite3"
        junk.write_bytes(b"not a database\n")
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (junk, ""))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_unreadable"
        assert prune.call_count == 0

    def test_a_relocation_also_stops_retention(self, tmp_path, monkeypatch):
        """A relocation suppresses the sweep too, for the same reason as the others.

        The flag comes from this process's environment, so a daemon run and a shell run
        can disagree; an earlier archive may still hold the conversations.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # Stub the flag, not `XDG_DATA_HOME`: on macOS the env var never sets it.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_relocated_outside_fence"
        assert prune.call_count == 0

    def test_a_discovery_failure_reports_and_does_not_fail_the_backup(self, tmp_path, monkeypatch):
        """An exception resolving the store reports a skip; the archive still uploads.

        Discovery reaches `Path.home()`, which can raise. Retention is suppressed too,
        since an earlier archive may hold conversations this one does not.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        def _boom():
            raise RuntimeError("home directory cannot be determined")

        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", _boom)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["conversations_skipped"] == "store_discovery_failed"
        assert prune.call_count == 0

    def test_a_run_that_reported_nothing_still_prunes(self, tmp_path, monkeypatch):
        """The suppression must not over-reach: no reason means retention still runs.

        Only a run that READ everything the host holds may stay reasonless, so this one
        exports a real store. A no-store run is not this case; see
        `test_an_absent_store_also_stops_retention`.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record
        assert prune.call_count == 1
        # The in-lock re-check is not armed: this run carried the conversations itself,
        # and arming it would make every wide run refuse its own sweep.
        assert prune.call_args.kwargs["recheck_conversations_retained"] is False

    def test_a_grant_without_a_scope_marker_withholds_the_conversations(
        self, tmp_path, monkeypatch
    ):
        """A grant with no scope marker covers the `cli` half only.

        That consent named this product's session files, not every kiro-cli use on the
        host, so the export waits for a re-confirmation. The `cli` half still rides.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        # Recorded as the grant's scope, never as a skip reason, which would suppress
        # the sweep on every install with an unscoped grant.
        assert record["layer_b_scope"] == "cli"
        assert "conversations_skipped" not in record
        # Pruning is safe: an archive from an unscoped grant never held conversations.
        assert prune.call_count == 1

    def test_a_scoped_grant_exports_the_conversations(self, tmp_path, monkeypatch):
        """A grant carrying the scope marker authorizes the export, and it runs."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert any(name.startswith("conversations/") for name in names)
        # No scope line: the grant reaches the payload, so there is nothing to explain.
        assert "layer_b_scope" not in record
        assert "conversations_skipped" not in record

    def test_a_legacy_record_can_never_read_as_the_wider_scope(self, tmp_path):
        """Every stored shape but the exact marker reads as `cli`-only.

        A value this code does not understand must not widen what leaves the machine,
        so the marker is compared exactly, never by truthiness.
        """
        for stored, covered in (
            (_SCOPED, True),
            (None, False),
            ("cli", False),
            ("conversations", False),
            ("CLI+CONVERSATIONS", False),
            (True, False),
            (["cli", "conversations"], False),
            ({"cli": True}, False),
        ):
            self._store(tmp_path, True, scope=stored)
            assert backup.layer_b_grant_covers_conversations(ACCOUNT) is covered, stored

        # The grant itself still governs: a withheld permission covers nothing, however
        # the scope reads.
        self._store(tmp_path, False)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_enable_that_names_no_scope_never_widens(self, tmp_path):
        """Only a caller that NAMES the scope gets it, transition or not.

        A stale client's first enable shows the operator the narrower description, so
        stamping on the off-to-on transition would grant the whole host unasked.
        """
        # A fresh enable naming nothing. This is the stale-client case.
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

        # Named, and only then.
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        # An unrecognised name records the narrower grant rather than failing.
        backup.set_sessions_layer_b(ACCOUNT, False)
        backup.set_sessions_layer_b(ACCOUNT, True, scope="cli+everything")
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_unrecognised_scope_clears_an_already_wide_marker(self, tmp_path):
        """Naming a scope this code does not know is not the same as naming none.

        An absent field leaves the marker alone; a named but unknown scope asked for
        something else, so an already-wide grant must not stay wide.
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        backup.set_sessions_layer_b(ACCOUNT, True, scope="cli+everything")
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_enable_that_names_no_scope_does_not_narrow_either(self, tmp_path):
        """A grant the operator did make survives a caller that forgot to repeat it.

        Absent means "no request to widen", not "request to narrow".
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

    def test_an_unscoped_enable_clears_a_marker_left_on_a_disabled_grant(self, tmp_path):
        """A marker is only valid for the grant that was in force when it was written.

        The two keys are stored independently, so ``enabled=false`` can sit beside a
        marker. An enable that RE-ESTABLISHES the grant clears it; only an already-on
        grant keeps it (``test_an_enable_that_names_no_scope_does_not_narrow_either``).
        """
        self._store(tmp_path, False)
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_unscoped_enable_over_a_corrupted_grant_clears_the_marker(self, tmp_path):
        """A stored value the reader will not accept as ON is a transition, not a keep.

        The reader requires exactly ``True``, so "was it already on" must too.
        """
        self._store(tmp_path, "yes")
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_a_withdrawal_reports_withheld_even_with_a_marker_present(self, tmp_path, monkeypatch):
        """A withdrawn grant covers nothing, whatever a marker says.

        The disable branch removes the marker, which hides this case; calling the audit
        with a marker still stored shows whether `covered` reads the grant too.
        """
        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        monkeypatch.setattr(
            backup,
            "_locked_state_update",
            lambda mutate: mutate({"accounts": {ACCOUNT: {}}}),
        )
        backup._audit_layer_b_grant(
            ACCOUNT, False, backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )

        grants = [e for e in events if e["operation"] == "aws_control.backup_layer_b_grant"]
        assert len(grants) == 1
        assert "grant=withdrawn" in grants[0]["resources"]
        assert "conversations=withheld" in grants[0]["resources"]

    def test_the_grant_write_is_audited_in_both_directions(self, tmp_path, monkeypatch):
        """A widening and a narrowing both file a SEL event naming what was decided.

        The route's own event does not say which way the decision went, and a review
        needs the revocation as much as the grant.
        """
        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        backup.set_sessions_layer_b(ACCOUNT, False)

        grants = [e for e in events if e["operation"] == "aws_control.backup_layer_b_grant"]
        assert len(grants) == 2
        assert "grant=granted" in grants[0]["resources"]
        assert "conversations=allowed" in grants[0]["resources"]
        assert "grant=withdrawn" in grants[1]["resources"]
        assert "conversations=withheld" in grants[1]["resources"]

    def test_the_scope_decision_reaches_the_sel_event(self, tmp_path, monkeypatch):
        """The audit must answer the consent question without the run record.

        `layer_b=allowed` alone cannot tell a run that shipped the conversations from
        one that withheld them.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)

        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        self._run_capturing_names(monkeypatch)

        decisions = [e for e in events if e["operation"] == "aws_control.backup_layer_b_decision"]
        assert len(decisions) == 1
        assert "layer_b=allowed" in decisions[0]["resources"]
        assert "conversations=withheld" in decisions[0]["resources"]

    def test_the_writer_stamps_the_scope_and_a_disable_drops_it(self, tmp_path):
        """A named scope is stored under the exact on-disk key, and a disable drops it.

        Dropping it means a later enable cannot inherit a scope from a withdrawn grant.
        """
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        # Pinned as a LITERAL: it is an on-disk contract, and a changed constant would
        # turn every stored grant legacy.
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert state["accounts"][ACCOUNT][backup.SESSIONS_LAYER_B_SCOPE_KEY] == "cli+conversations"

        backup.set_sessions_layer_b(ACCOUNT, False)
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert backup.SESSIONS_LAYER_B_SCOPE_KEY not in state["accounts"][ACCOUNT]

    def test_the_scope_line_survives_an_unchanged_run(self, tmp_path, monkeypatch):
        """An unchanged run must not erase the scope line either.

        `_record_skip` REPLACES the run slot, so a field it does not forward is gone.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True, scope=None)

        first, second = self._run_twice_over_an_unchanged_tree(monkeypatch)

        assert first["layer_b_scope"] == "cli"
        assert second["layer_b_scope"] == "cli"
        assert second["uploaded"] is False

    def test_a_conversation_bearing_run_records_the_fact_and_still_prunes(
        self, tmp_path, monkeypatch
    ):
        """An archive that carries conversations is recorded, and pruning still runs.

        The fact tells a LATER run an older archive may be the only copy. This run
        holds the conversations itself, so retiring older ones loses nothing.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            self._run_capturing_names(monkeypatch)

        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert state["accounts"][ACCOUNT][backup.SESSIONS_CONVERSATIONS_RETAINED_KEY] is True
        assert prune.call_count == 1

    def test_a_run_with_no_conversations_keeps_an_older_archive_that_has_them(
        self, tmp_path, monkeypatch
    ):
        """The scope can narrow with nothing wrong, and the sweep must not retire the copy.

        A narrowed scope sets no skip reason, so only the retained-conversations fact
        stops the sweep. The reader is stubbed: this pins what the caller does with it.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        # A legacy grant: Layer B on, scope not covering conversations, so this run
        # carries none and reports no skip reason.
        self._store(tmp_path, True, scope=None)
        monkeypatch.setattr(backup, "a_retained_archive_carries_conversations", lambda _a: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert not any(name.startswith("conversations/") for name in names)
        assert "conversations_skipped" not in record
        assert prune.call_count == 0

    def test_a_run_with_no_conversations_and_none_retained_still_prunes(
        self, tmp_path, monkeypatch
    ):
        """The second condition must not over-suppress either.

        An install that never carried conversations has nothing for an older archive to
        hold; suppressing here would freeze retention on every unscoped install.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)
        monkeypatch.setattr(backup, "a_retained_archive_carries_conversations", lambda _a: False)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record
        assert prune.call_count == 1

    def test_an_absent_store_also_stops_retention(self, tmp_path, monkeypatch):
        """No store anywhere suppresses the sweep, because an earlier archive may hold.

        A store wiped, dropped by a reinstall or on an unmounted volume was present when
        the earlier archive was written. The resolver's own absence report is pinned in
        test_aws_control_backup_conversations.py, since this class stubs it.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: False)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, "store_absent"))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_absent"
        assert prune.call_count == 0

    def test_conversation_rows_alone_are_recorded_as_layer_b(self, tmp_path, monkeypatch):
        """An empty kiro-cli directory still records Layer B when rows rode.

        The record describes the ARCHIVE: `conversations/` rows are unredacted context
        even with no `cli/` member.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        empty_cli = tmp_path / "cli_sessions"
        empty_cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: empty_cli)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert not [n for n in names if n.startswith("cli/")]
        assert backup._CONVERSATIONS_DB_ARCNAME in names
        assert record["layer_b"] is True

    # -- the permission is NOT reachable from agent-writable config ---------

    def test_an_agent_writable_config_key_cannot_grant_layer_b(self, tmp_path, monkeypatch):
        """``config.json`` must not be able to self-grant this permission.

        Any auto-approved agent shell can write ``config.json``, so a prompt-injected
        agent could grant itself the upload. Every spelling the gate might read is set.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        monkeypatch.setattr(
            loader,
            "_raw_config",
            lambda: {
                "dashboard": {
                    "backup_include_layer_b": True,
                    "sessions_include_layer_b": True,
                    backup.SESSIONS_LAYER_B_KEY: True,
                }
            },
        )

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_the_export_permission_does_not_grant_the_backup(self, tmp_path, monkeypatch):
        """Two destinations, two decisions: the export key must not carry here.

        A downloaded file can be handed to another person; this archive lands in a
        bucket the operator owns. Sharing one answer would mean an operator who
        enabled the file export silently enabled unredacted context into their
        bucket as well.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        monkeypatch.setattr(
            loader, "_raw_config", lambda: {"dashboard": {"export_include_layer_b": True}}
        )

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the grant is per account ------------------------------------------

    def test_a_grant_for_one_account_does_not_carry_to_another(self, tmp_path, monkeypatch):
        """The risk this permission prices is the destination bucket.

        Granting for one account must not grant for another the operator adds
        later, so the answer is stored per account like the nightly bit beside it.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True, account="999999999999")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the permission lives behind the agent file floor -------------------

    def test_the_permission_lives_inside_the_fenced_state_directory(self):
        """The store must sit on the keystone floor, not in agent-writable config.

        ``STATE_DIR_LEAF`` is registered in ``security._CREW_SECRET_LEAVES``, so an
        agent's file tools and shell forms refuse every path in there -- which is
        what makes this a permission rather than a preference. A move out of that
        directory would silently un-protect it, so the placement is pinned.
        """
        from kiro_crew import security

        assert backup.STATE_DIR_LEAF in security._CREW_SECRET_LEAVES
        # Read through ``app_data_dir`` rather than ``_state_path``, which this
        # class's fixture redirects into ``tmp_path``. The leaf is always
        # '/'-joined (a catalog key, not a local path), so compare against the
        # posix form or this fails on Windows.
        assert backup.app_data_dir(backup.APP_NAME).as_posix().endswith(backup.STATE_DIR_LEAF)

    # -- the writer ---------------------------------------------------------

    def test_the_writer_records_both_directions(self, tmp_path):
        """``set_sessions_layer_b`` is the recorded answer the reader reads back."""
        assert backup.sessions_layer_b_enabled(ACCOUNT) is False
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        backup.set_sessions_layer_b(ACCOUNT, False)
        assert backup.sessions_layer_b_enabled(ACCOUNT) is False

    def test_the_writer_keeps_the_nightly_bit_beside_it(self, tmp_path):
        """Recording one permission must not publish over the other.

        Both live in the same account sub-dict, and the write is a
        read-modify-write of the whole document, so a base that dropped the
        sibling would silently disable unattended backups.
        """
        backup.set_nightly(ACCOUNT, True)
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.nightly_enabled(ACCOUNT) is True
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True

    # -- the permission is read once ---------------------------------------

    def test_the_archive_and_its_record_come_from_the_same_read(self, tmp_path, monkeypatch):
        """The record echoes the read that decided the contents, never a later one.

        A restore trusts the record. The revocation recheck may REFUSE on a later
        answer, but never rewrites what the record says about a shipped archive.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        calls: list[int] = []

        # The first two answers are the contents decision and the revocation
        # recheck. The third exists only to be wrong: anything that reads the
        # permission again to fill the RECORD would pick up this False.
        answers = iter([True, True, False, False])

        def _once(account):
            calls.append(1)
            return next(answers)

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", _once)

        record, names = self._run_capturing_names(monkeypatch)

        assert len(calls) == 2
        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["layer_b"] is True

    # -- the record describes the archive, not the permission ---------------

    def test_a_permitted_run_with_no_cli_files_records_no_layer_b(self, tmp_path, monkeypatch):
        """Granted but nothing to add: the record must say what the archive holds.

        An empty kiro-cli directory is ordinary on a fresh install; a record taken from
        the permission would file a crew-only archive as carrying Layer B.
        """
        self._skip_without_pinning()
        cli = self._both_halves(tmp_path, monkeypatch)
        for leftover in cli.iterdir():
            leftover.unlink()
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        # The permission is genuinely granted, so this is not the withhold path:
        # the cli tree was walked and simply had nothing in it.
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the snapshot record keeps its shape --------------------------------

    def test_a_snapshot_record_carries_no_layer_b_key(self, tmp_path, monkeypatch):
        """The question does not arise for a snapshot, so its record does not answer it."""
        record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x/a.tar.gz", 1, "f")
        assert "layer_b" not in record
