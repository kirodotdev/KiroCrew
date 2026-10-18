"""Pins the operator's Layer B permission gate on the sessions archive.

Covers ``TestSessionsArchiveLayerBGatePermission``: the default that withholds
Layer B, the recorded grant that lets the kiro-cli half ride, the scope marker the
``conversations/`` export also needs and ``set_sessions_layer_b`` stamps and clears,
the SEL events for the grant and the run decision, and the ``conversations_skipped``
reasons that hold back the retention sweep. Also pins a grant stored per account in
the fenced state directory, out of reach of ``config.json``, and a run record that
describes the archive rather than the permission. The shared harness comes from
``aws_control_backup_helpers.py``; the mid-build revocation and lock cases sit in
``test_aws_control_backup_layer_b_mid_build.py``.
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

# ---------------------------------------------------------------------------
# Layer B in the sessions archive -- the operator's standing permission
# ---------------------------------------------------------------------------


class TestSessionsArchiveLayerBGatePermission(_SessionsArchiveLayerBGateHarness):
    """The kiro-cli half rides only when the operator has permitted it.

    Layer B is the byte-exact unredacted model context window. The archive used
    to carry it because taring the directory reached it, not because any
    permission was consulted, while the file-export path required an operator
    opt-in for the same payload. These pin both directions of the gate and the
    reading a restore depends on.

    The shared fixture and helpers come from ``_SessionsArchiveLayerBGateHarness`` in
    ``aws_control_backup_helpers.py``. ``TestSessionsArchiveLayerBGateMidBuild`` in
    ``test_aws_control_backup_layer_b_mid_build.py`` covers a grant, revocation or scope
    withdrawal landing mid-build, and when an upload holds and releases the state lock.
    """

    # -- the block path ----------------------------------------------------

    def test_layer_b_is_withheld_by_default(self, tmp_path, monkeypatch):
        """No permission recorded: the transcript rides and the kiro-cli half does not."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        # No state file at all is the default an operator who never chose has, so
        # the permission is read through the real function rather than stubbed --
        # this pins the DEFAULT, which is the whole claim.

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

        The export reads the terminal's own conversation store, which is the same
        data class as Layer B -- what a model actually held, unredacted, not what
        was displayed with display-time redaction applied. An export that rode on
        the crew half's terms would put that content in the bucket of an install
        whose operator withheld exactly it, and an object already uploaded cannot
        be un-sent.

        Mutation-verified: drop the `if layer_b` guard at the export call site and
        this test reddens on the `conversations/` members; the permitted direction
        below still passes.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        # No permission recorded at all -- the default an operator who never chose
        # has, read through the real function rather than stubbed.

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

        The export carries a present-but-empty allowlisted table so a restore sees
        the real schema, which means rows and emitted members disagree: two members,
        zero rows. `run_sessions_backup` measures content with both, because
        measuring it by rows alone would let the "no session files to archive" guard
        throw away members the tar already holds.

        Both transcript halves are empty here, so rows alone cannot carry the run
        past that guard -- the members have to.

        MUTATION: guard on `count == 0` alone, or fold members into the row count,
        and this reddens with RuntimeError("no session files to archive").
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

        `_kiro_cli_conversation_db` declines a candidate whose leaf or ANY ancestor is
        a link, and the ancestor walk runs from `/` down and includes the home
        directory -- so an ordinary symlinked `~/.local/share`, or a symlinked home,
        takes that exit on every run. Returning a bare `None` for it made "I refused
        to read a store that is there" identical to "there is no store", and only the
        second is safe to prune on: the first means an earlier archive may hold
        conversations this one does not.

        MUTATION: return `None, ""` from the rejection branch and this reddens twice
        -- the reason is absent AND the sweep runs.
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

        `_audit_retention` is only reachable from inside `_prune_remote_archives`, so
        skipping the sweep without filing an event would leave the one path in the app
        that erases object versions permanently with no SEL record at all -- and that
        function's contract is that every terminal outcome files one, "including the
        ones that deleted nothing". A decline is a terminal outcome. It is also what
        keeps the accumulation visible: while the condition persists the archives pile
        up past the keep count, and one event per run naming the reason is how an
        auditor sees that instead of inferring it from a sweep that never ran.

        MUTATION: drop the `_audit_retention` call from the suppressed branch and this
        reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            with mock.patch.object(backup, "_audit_retention") as audit:
                self._run_capturing_names(monkeypatch)

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

        The status read serves how the last sweep ended. A decline never reaches
        `_prune_remote_archives`, so without its own write the sessions row would keep
        serving whatever the last sweep that RAN said -- and a decline repeated every
        night would read as the success before it.

        MUTATION: drop the `_record_last_sweep` call from the suppressed branch and
        this reddens on the stale success.
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

        Skipping it is the security decision (`test_an_env_relocated_store_is_not
        _consulted` in the conversations suite pins that). Skipping it SILENTLY is a
        separate failure: an operator whose store lives outside home would read a
        successful run and believe the archive holds their terminal conversations
        when it holds none. So the run record carries `conversations_skipped`.

        The detection is a path-set comparison and touches no file, which is why it
        is safe to run against a root outside the fence.

        MUTATION: drop the `_store_relocated_outside_the_fence()` branch and return a
        bare `_ConversationExport(0, 0)`, or stop threading `conversations_skipped`
        into the record, and this reddens on the missing key.
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
        # A relocation the resolver will not read: the env names a root away from
        # home, and no store exists at the fenced location.
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        record, names = self._run_capturing_names(monkeypatch)

        # The rest of the archive is still correct and still uploads -- the refusal
        # is reported, never escalated into a failed backup.
        assert names == ["cli/abc.json", "crew/t.jsonl"]
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_a_run_that_skipped_nothing_carries_no_skip_key(self, tmp_path, monkeypatch):
        """No relocation means no `conversations_skipped` key at all.

        Keeps the record's shape unchanged on the common path, so the key's presence
        is itself the signal rather than a value a reader has to interpret.

        MUTATION: always set the key (even empty) and this reddens.
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

        The second run can only take the unchanged-skip branch if the baseline probe
        can PROVE the first upload: `_unchanged_baseline` HEADs the recorded key and
        requires the version and length it recorded. So `put_file` returns a version
        and `head_object_meta` answers with what was actually uploaded -- a stub that
        returned nothing would send the second run down the upload path and the test
        would pass for the wrong reason.
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

        `_record_skip` REPLACES the run slot, so a field it does not forward is gone.
        The sessions path takes that branch whenever the tree has not moved, which is
        the ordinary nightly case -- so without forwarding, the first unchanged run
        would quietly restore an assertion of complete coverage over a run that had
        reported a gap.

        Runs the same backup twice. The second takes the unchanged-skip branch (it
        reports `uploaded` false), and the skip key must still be there.

        MUTATION: drop `conversations_skipped` from the `_record_skip` call and this
        reddens on the second record.
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
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
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

        The relocation check runs BEFORE the fixed-candidate lookup. Ordered the other
        way, a file still sitting at the old anchor after the environment says the
        store moved would be exported and the run would report complete coverage,
        because the relocation is only visible when the fixed lookup finds nothing.
        `identity_stores.selected_store` arbitrates this same leftover-in-the-abandoned-
        root state, so it is a state the codebase already expects.

        Here BOTH exist: a readable store at the fixed anchor AND an active relocation.

        MUTATION: move the relocation check back under `if db is None` and this reddens
        -- the stale store's members appear in the archive and the skip key is absent.
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
        # And an active relocation saying the real store lives elsewhere.
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"], "the stale store must not be exported"
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_an_unreadable_store_is_named_in_the_record(self, tmp_path, monkeypatch):
        """A store that cannot be read is reported, not silently skipped.

        The same standard the relocation path is held to. An operator whose store went
        unreadable for one run must be able to see that from the record, because the
        archive still uploads and still reports success on its transcript halves.

        MUTATION: return a bare `_ConversationExport(0, 0)` from the
        `except (OSError, sqlite3.Error)` branch and this reddens.
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

        Retention protects only the key THIS run uploaded, so at `keep=1` retiring the
        previous archive erases the one copy that still held the conversations, and
        `delete_object_versions` erases versions outright. The gap here recovers on the
        next successful run; the retired object does not.

        MUTATION: call `_prune_remote_archives` unconditionally and this reddens.
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

        The tempting argument is that a relocation is a standing property of the
        install, so no archive ever held those conversations and pruning loses nothing.
        That premise is false: the flag is read from THIS PROCESS's environment, and a
        daemon-launched run and a shell-launched run can disagree about
        `XDG_DATA_HOME` with the operator relocating nothing. So one run can write a
        complete archive and the next can omit the conversations, and pruning at
        `keep=1` would erase the only copy that held them with no recovery.

        MUTATION: restore the old exclusion -- gate the sweep on
        `conversations.skipped != "store_relocated_outside_fence"` -- and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_relocated_outside_fence"
        assert prune.call_count == 0

    def test_a_discovery_failure_reports_and_does_not_fail_the_backup(self, tmp_path, monkeypatch):
        """An exception resolving the store reports a skip; the archive still uploads.

        Both discovery calls reach `Path.home()`, which raises when the home directory
        cannot be determined. Unguarded, that exception would propagate out of
        `run_sessions_backup` and throw away a correct transcript archive over a
        missing sub-member -- the opposite of this function's stated best-effort
        contract.

        Also asserts retention is suppressed, since a discovery failure is transient
        and an earlier archive may hold conversations this one does not.

        MUTATION: remove the try/except around discovery and this reddens with the
        RuntimeError instead of returning a record.
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

        The sweep is suppressed by a predicate on `conversations_skipped`, so this is
        the other direction of that predicate and it needs its own test. The case that
        may stay reasonless is a run that READ everything the host holds, so this one
        exports a real store successfully: nothing an earlier archive could hold is
        missing from this archive, and retention must behave exactly as it did before
        this feature existed.

        A no-store run does NOT belong here, even though it also carries zero rows --
        see `test_an_absent_store_also_stops_retention`. A successful export is the
        case the claim is actually true of.

        MUTATION: suppress unconditionally and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record
        assert prune.call_count == 1
        # And the in-lock re-check is NOT armed for this run. It carried the
        # conversations itself, so it set the fact -- arming the re-check here would make
        # every wide run refuse its own sweep, and retention would never run again on the
        # installs that actually export conversations.
        #
        # MUTATION: pass `recheck_conversations_retained=True` unconditionally from the
        # sessions sweep and this reddens.
        assert prune.call_args.kwargs["recheck_conversations_retained"] is False

    def test_a_grant_without_a_scope_marker_withholds_the_conversations(
        self, tmp_path, monkeypatch
    ):
        """A grant recorded before the export existed covers the `cli` half only.

        The grant is one boolean with no scope in it, so reading it as also authorizing
        `conversations_v2` would ship every interactive kiro-cli use on the host
        off-host on a consent that named this product's session files. An object in a
        bucket cannot be recalled, so the export waits for a re-confirmation through
        the existing owner-gated writer.

        The `cli` half still rides, which is the half this change never touched.

        MUTATION: gate the export on `layer_b` alone instead of on the scope and this
        reddens -- a `conversations/` member appears.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        # Recorded as the GRANT's scope, never as a skip reason. A skip reason here
        # would suppress the retention sweep on every install that granted Layer B
        # before this export existed, all at once.
        assert record["layer_b_scope"] == "cli"
        assert "conversations_skipped" not in record
        # And pruning is SAFE: no released version wrote an archive carrying
        # conversations, so there is nothing an older archive holds that this run does
        # not. Verified against this PR's base and against main, whose sessions archive
        # has exactly the `crew` and `cli` roots.
        assert prune.call_count == 1

    def test_a_scoped_grant_exports_the_conversations(self, tmp_path, monkeypatch):
        """A grant carrying the scope marker authorizes the export, and it runs.

        This is the other direction of the same gate: re-confirming through the
        existing owner-gated writer stamps the scope, so the operator reaches the wider
        payload without a new endpoint or a new control.

        MUTATION: require any other scope literal and this reddens -- no
        `conversations/` member appears.
        """
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

        This is the whole point of the marker rather than an edge case in it. A stored
        value this code does not understand must not widen what leaves the machine,
        which is the posture `sessions_layer_b_enabled` already takes on an
        unparseable grant.

        MUTATION: accept a truthy marker instead of comparing it exactly, and the
        `"conversations"` and `True` rows redden.
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

        A transition test -- stamp when the grant goes from off to on -- closes an
        idempotent retry but not a FIRST enable from a client still rendering older
        copy: the operator reads the narrower description and the grant covers the
        whole host. The request itself is the only place the decision can be carried,
        so the scope must be named.

        MUTATION: stamp on any enable, or only on the off-to-on transition, and the
        first assertion reddens.
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

        An absent field is no statement about scope, so it leaves the marker alone. A
        caller that NAMED one said what it wanted and it was not the conversation
        export, so an already-wide grant must not stay wide for it -- otherwise the
        grant widens for a request that asked for something else entirely, which is the
        one thing this field exists to stop.

        MUTATION: collapse absent and unrecognised into one branch that leaves the
        marker alone, and this reddens.
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        backup.set_sessions_layer_b(ACCOUNT, True, scope="cli+everything")
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_enable_that_names_no_scope_does_not_narrow_either(self, tmp_path):
        """A grant the operator did make survives a caller that forgot to repeat it.

        Absent means "no request to widen", not "request to narrow". Treating a bare
        re-enable as a withdrawal would revoke a real consent on every idempotent retry
        from an older client, which the operator never asked for.

        MUTATION: drop the stored marker when no scope is named and this reddens.
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

    def test_an_unscoped_enable_clears_a_marker_left_on_a_disabled_grant(self, tmp_path):
        """A marker is only valid for the grant that was in force when it was written.

        The document can hold ``enabled=false`` together with a marker, because the two
        keys are stored independently and a state file is written by whichever build of
        this app is installed at the time. Preserving the marker on any
        unscoped enable then turns a bare ``{"enabled": true}`` into a host-wide grant
        the operator never named, and the archive that follows cannot be recalled.

        So an enable that RE-ESTABLISHES the grant clears it, and only an already-on
        grant may keep it -- which
        ``test_an_enable_that_names_no_scope_does_not_narrow_either`` pins separately.

        MUTATION: preserve the marker whenever no scope is named, and this reddens while
        that sibling test stays green.
        """
        self._store(tmp_path, False)
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_unscoped_enable_over_a_corrupted_grant_clears_the_marker(self, tmp_path):
        """A stored value the reader will not accept as ON is a transition, not a keep.

        The reader requires the grant to be exactly ``True``, so a corrupted value is
        OFF to every consumer. Deciding "was it already on" by truthiness instead would
        let such a document count as on and keep a marker the operator cannot be shown
        to have named, which is the unsafe direction for an off-host upload.

        MUTATION: compare the previous value by truthiness rather than ``is True``, and
        this reddens.
        """
        self._store(tmp_path, "yes")
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_a_withdrawal_reports_withheld_even_with_a_marker_present(self, tmp_path, monkeypatch):
        """A withdrawn grant covers nothing, whatever a marker says.

        `_audit_layer_b_grant` reads the resulting marker, and today the disable branch
        removes it, so the event happens to be truthful. That makes the event's
        correctness depend on a decision taken elsewhere in the same function rather
        than on the pair `layer_b_grant_covers_conversations` actually reads. An event
        reporting `conversations=allowed` for a withdrawal would misstate the one thing
        a consent review comes to it for.

        Driven through the writer with a marker already stored, so the audit sees the
        combination the coupling hides.

        MUTATION: derive `covered` from the marker alone and this reddens.
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

        The route's own event records the operation and the path, not which way the
        decision went, so learning what the grant became would mean reading the state
        file -- the on-disk dependency the decision audit exists to remove. A narrowing
        is filed on the same footing: a review reconstructing what an archive was
        allowed to carry needs the revocation as much as the grant.

        MUTATION: drop the `_audit_layer_b_grant` call, or file only the enable, and
        this reddens.
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

        That is the whole reason this event exists, so an event saying only
        `layer_b=allowed` describes a run that shipped the terminal conversations
        identically to one that withheld them. The run record carries `layer_b_scope`,
        but reading the scope from disk would put the audit back on the dependency it is
        here to remove.

        MUTATION: drop `conversations` from the event's `resources` and this reddens.
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

        No new endpoint and no new control: the owner-gated path that already records
        the decision carries the scope as a field of the same request. A disable removes
        the marker so a later enable cannot inherit a scope from a decision that was
        withdrawn.

        MUTATION: stop stamping a named scope and the second assertion reddens; stop
        dropping it on disable and the last reddens.
        """
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        # The stored value is pinned as a LITERAL, not through the constant, because it
        # is an on-disk contract. Comparing it to the constant only proves the writer
        # and the reader agree with each other -- they would still agree after the
        # literal changed, and every grant an operator had already re-confirmed would
        # then read as legacy and silently stop exporting conversations.
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert state["accounts"][ACCOUNT][backup.SESSIONS_LAYER_B_SCOPE_KEY] == "cli+conversations"

        backup.set_sessions_layer_b(ACCOUNT, False)
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert backup.SESSIONS_LAYER_B_SCOPE_KEY not in state["accounts"][ACCOUNT]

    def test_the_scope_line_survives_an_unchanged_run(self, tmp_path, monkeypatch):
        """An unchanged run must not erase the scope line either.

        `_record_skip` REPLACES the run slot, so a coverage fact it does not forward is
        gone -- the same trap `conversations_skipped` fell into. Without forwarding, the
        first ordinary nightly run would drop the one line explaining why a
        Layer-B-enabled archive holds no conversations.

        MUTATION: drop `layer_b_scope` from the `_record_skip` call and this reddens on
        the second record.
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

        Recording it is what lets a LATER run know an older archive is the only copy.
        This run prunes normally, and that is the point rather than an exception: the
        newest archive holds the conversations, so retiring older ones loses nothing --
        which is what keeps retention from freezing forever once the fact is set.

        MUTATION: stop passing `conversations_retained` and the first assertion reddens.
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

        An in-scope run uploads conversations; the grant's scope is then narrowed; the
        next run's archive omits them. That path sets NO skip reason -- a narrowed scope
        is a policy decline, not a failed read -- so the `skipped` suppression cannot see
        it, and at `keep=1` the sweep would retire the only archive holding them with no
        recovery.

        The reader is stubbed because this pins what the CALLER does with the fact; the
        fact's own recording is pinned by
        `test_a_conversation_bearing_run_records_the_fact_and_still_prunes`.

        MUTATION: remove the second condition and this reddens, while
        `test_an_absent_store_also_stops_retention` stays green -- the two conditions are
        independent.
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
        hold, so retention behaves exactly as it did before this feature existed. Without
        this direction the new condition would freeze retention on every install that
        has Layer B on and no conversation scope, which is the unbounded accumulation the
        suppression exists to prevent rather than an instance of it.

        MUTATION: suppress whenever this run carries no conversations, regardless of the
        fact, and this reddens.
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

        The tempting argument is that a host with no store has no conversations, so
        pruning loses nothing. It reasons about the store's state NOW, while retention
        decides the fate of an archive written EARLIER. A store wiped to clear
        corruption, dropped by a reinstall, or on a volume not mounted at nightly-run
        time was present when that archive was written, so at `keep=1` the sweep would
        erase the only copy holding those rows, and `delete_object_versions` leaves no
        recovery.

        MUTATION: carve this reason out of the caller's guard -- gate the sweep on
        `conversations.skipped != "store_absent"` -- and this reddens. That the resolver
        reports absence at all is pinned separately, by
        `test_a_host_with_no_store_reports_absence` in
        test_aws_control_backup_conversations.py, because this class stubs the resolver
        out by design.
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

        The record must describe the ARCHIVE. A permitted run on a host whose
        kiro-cli session directory is empty adds no `cli/` member but still carries
        unredacted terminal context under `conversations/`, so a record reading
        `layer_b=False` beside those rows would send a restore looking for a
        fidelity the object holds and describe one it does not.

        Mutation-verified: record `layer_b_files > 0` alone and this test reddens.
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

        ``config.json`` is writable by any auto-approved agent shell, so a
        permission honoured from there is one a prompt-injected agent can grant
        itself -- and the resulting upload of unredacted model context cannot be
        recalled. Every spelling this gate might read is offered here at once, so
        moving the read into ``config.json`` under any of them reddens this test.
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

        The archive and the run record must describe the same bytes. Deriving the
        record from a fresh read would let a write landing mid-build produce a tar
        that carries Layer B alongside a record saying it does not -- and the
        record is what a restore trusts. The revocation recheck before the upload
        may REFUSE on a later answer; it may not change what the record says about
        an archive that shipped.
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

        An absent or empty kiro-cli directory is an ordinary state on a fresh or
        CLI-idle install. The crew half alone carries the run past the empty-archive
        guard, so a record taken from the PERMISSION would file a crew-only archive
        as carrying Layer B. A run record is written once and nothing corrects it
        afterwards, so a restore reading that record would go looking for a fidelity
        the object does not hold.
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
