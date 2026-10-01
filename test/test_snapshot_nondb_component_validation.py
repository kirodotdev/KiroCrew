"""Non-database components are validated too, and an un-redacted backup says what it
carries.

Two gaps that share a root: a rule was implemented for the case that prompted it and not
for the others it applies to equally. Databases were validated but component JSON was not.
A backup is deliberately un-redacted, but nothing told the operator what that includes.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


def _real_db(path: Path) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


def _bundle(tmp_path, crons: bytes, name: str = "b") -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "crons.json").write_bytes(crons)
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"crons": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / f"{name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


class TestComponentJsonIsValidatedBeforeInstall:
    def test_an_unparseable_crons_file_is_refused(self, home, tmp_path, capsys):
        """Its reader treats an unreadable file as no jobs, so this would discard silently."""
        bundle = _bundle(tmp_path, b"{ this is not json", name="broken")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "crons"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "crons.json" in out and "could not be read as JSON" in out, out
        assert not (home / "crons.json").exists()

    def test_a_json_array_is_refused_because_the_reader_expects_an_object(
        self, home, tmp_path, capsys
    ):
        """Well-formed JSON is not enough: an array takes the reader's empty branch."""
        bundle = _bundle(tmp_path, b'[{"id": "a"}]', name="array")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "crons"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "not an" in out and "object" in out, out
        assert not (home / "crons.json").exists()

    def test_a_sound_crons_file_still_restores(self, home, tmp_path, capsys):
        bundle = _bundle(tmp_path, b'{"jobs": []}', name="ok")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "crons"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "crons.json").is_file()

    def test_merge_skips_a_crons_file_of_the_wrong_shape_rather_than_installing_it(
        self, home, tmp_path
    ):
        """A crons file that parses into the wrong shape must never reach the merge reader.

        Superseded mechanism: an earlier revision pre-flighted this in
        `_refuse_corrupt_source_databases` and raised `SourceComponentUnsound`. The M1
        base guards it on the merge side -- `_usable_cron_shape` classifies the shape and
        `_merge_crons` skips an unusable file and continues.

        That hand-off is conditional, and this test must not assert it unconditionally. The
        merger only runs when a live copy EXISTS (`if dst.is_file(): _merge_crons(...)`); the
        sibling `else` copies the bundle's file in verbatim, with no shape guard anywhere
        downstream. So the pre-flight may stand aside only for the case whose guard is real,
        and must refuse when it is the thing that installs -- both directions are asserted
        below rather than the one that happened to hold.
        """
        # An existing destination: the merger's own guard covers it, so the pre-flight
        # stands aside and one unreadable component does not fail every other one.
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"jobs": ["x"]}', encoding="utf-8")
        (home / "crons.json").write_text('{"jobs": []}', encoding="utf-8")
        snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

        # An ABSENT destination: merge copies verbatim, nothing downstream checks the shape,
        # so standing aside would install it. The pre-flight has to refuse here.
        (home / "crons.json").unlink()
        with pytest.raises(snap.SourceComponentUnsound):
            snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)
        (home / "crons.json").write_text('{"jobs": []}', encoding="utf-8")

        # ...and it is the reader's shape guard that rejects it before any field access.
        import json as _json

        assert (
            snap._usable_cron_shape(_json.loads('{"jobs": ["x"]}'), payload / "crons.json") is False
        )
        assert (
            snap._usable_cron_shape(_json.loads('{"jobs": {"a": 1}}'), payload / "crons.json")
            is False
        )

        # Unparseable JSON is still the pre-flight's to refuse (an object reader can't
        # even reach), so it stands aside for the merger there too.
        (payload / "crons.json").write_bytes(b"{ broken")
        snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

    def test_merge_validates_the_index_when_a_missing_memory_db_drags_it_along(
        self, home, tmp_path
    ):
        """`memory_index.db` is copied whenever the live `memory.db` is absent.

        Keying validation on the index's OWN destination let a corrupt index overwrite a
        healthy one, because the copy is triggered by the other file's absence.
        """
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "sound.db"))
        (payload / "memory_index.db").write_bytes(b"corrupt index")

        # A healthy local index exists, but no local memory.db -> both get copied.
        (home / "memory_index.db").write_bytes(_real_db(tmp_path / "localidx.db"))
        assert not (home / "memory.db").exists()
        with pytest.raises(snap.SourceComponentUnsound):
            snap._refuse_corrupt_source_databases(payload, ["memory"], mc_for_merge=home)

        # With a local memory.db present, merge copies neither, so the index is left alone.
        (home / "memory.db").write_bytes(_real_db(tmp_path / "localmem.db"))
        snap._refuse_corrupt_source_databases(payload, ["memory"], mc_for_merge=home)

    def test_the_declared_set_covers_the_readers_that_fail_empty(self):
        for name in snap.COMPONENT_JSON_OBJECTS:
            assert name.endswith(".json"), name
        declared = {f for files in snap.CORE_FILES.values() for f in files}
        assert (
            snap.COMPONENT_JSON_OBJECTS <= declared
        ), "every entry must be a real component file, or it is never checked"
        assert "crons.json" in snap.COMPONENT_JSON_OBJECTS


class TestAnUnredactedBackupSaysWhatItCarries:
    def test_it_names_the_uncertified_components(self, capsys):
        snap._report_unresolved_payload(["memory", "config"])
        out = capsys.readouterr().out
        assert "uncertified for sharing" in out.lower(), out
        assert "config" in out and "memory" in out, out

    def test_it_does_not_claim_a_redaction_state_it_no_longer_owns(self, capsys):
        """Whether the OUTBOUND copy is redacted is decided later and reported there.

        This notice runs before the outbound copy is even produced, so asserting "NOT
        redacted" here would state the outcome of a decision that has not been made --
        and it was wrong the moment redaction became the default.
        """
        snap._report_unresolved_payload(["memory", "config"])
        out = capsys.readouterr().out
        assert "NOT redacted" not in out, out
        # It must still be honest about the copy it CAN speak for: the local one.
        assert "local disk" in out, out

    def test_it_stays_quiet_when_nothing_uncertified_rides(self, capsys, monkeypatch):
        snap._report_unresolved_payload([])
        assert capsys.readouterr().out == ""

    def test_it_runs_before_the_outbound_copy_is_produced(self):
        import inspect

        src = inspect.getsource(snap.prepare_redacted_copy)
        assert src.index("_report_unresolved_payload(") < src.index("_redacted_upload_copy(")

    def test_no_component_is_certified_share_safe_yet(self):
        """The disclosure's premise: nothing has been cleared for another person's hands."""
        assert all(spec.policy is snap.SecretPolicy.UNRESOLVED for spec in snap.COMPONENTS.values())


def _config_bundle(tmp_path: Path, record: bytes, name: str) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260102T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    # The record travels only beside its map (a record-only bundle installs no
    # record at all); a map with no Slack links keeps these tests about the
    # record's own shape.
    (payload / "session_map.json").write_bytes(b'{"dashboard:d1": {"sid": "s1"}}')
    (payload / "slack_workspace.json").write_bytes(record)
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / f"{name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


class TestSlackWorkspaceRecordIsValidatedBeforeInstall:
    """The record's reader accepts one shape and treats every other as damage it
    refuses the Slack boot on. An object-only check would let a restore install
    a file that parses and yet takes Slack down while the restore reports
    success; the install path asks the record's own shape check."""

    @pytest.mark.parametrize(
        "record, defect",
        [
            (b'{"team_id": 123}', "'team_id' is not a string"),
            (b'{"team_id": "T0A", "pending": "T0B"}', "'pending' is not an object"),
            (
                b'{"team_id": "T0A", "pending": {"team_id": "", "swept": []}}',
                "'pending.team_id' is not a non-empty string",
            ),
            (
                b'{"team_id": "T0A", "pending": {"team_id": "T0B", "swept": "rows"}}',
                "'pending.swept' is not a list",
            ),
            (
                b'{"team_id": "T0A", "pending": {"team_id": "T0B", "swept": [1]}}',
                "'pending.swept[0]': row is not an object",
            ),
            (
                b'{"team_id": "T0A", "pending": {"team_id": "T0B", "swept": '
                b'[{"key": "k", "slack_thread_ts": "1.1", "slack_channel_id": {"id": 1}}]}}',
                "'pending.swept[0]': 'slack_channel_id' is not a string or null",
            ),
        ],
    )
    def test_a_record_the_reader_would_refuse_is_not_installed(
        self, home, tmp_path, capsys, record: bytes, defect: str
    ):
        bundle = _config_bundle(tmp_path, record, name="bad-record")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "slack_workspace.json" in out and defect in out, out
        assert not (home / "slack_workspace.json").exists()

    @pytest.mark.parametrize(
        "record",
        [
            b'{"team_id": "T0A"}',
            b'{"team_id": ""}',
            b'{"team_id": "T0A", "pending": {"team_id": "T0B", "swept": '
            b'[{"key": "k", "slack_thread_ts": "1.1", "slack_channel_id": "C1"}]}}',
        ],
    )
    def test_a_sound_record_restores(self, home, tmp_path, capsys, record: bytes):
        bundle = _config_bundle(tmp_path, record, name="ok-record")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "slack_workspace.json").read_bytes() == record

    def test_a_marker_over_the_row_cap_is_not_installed(self, home, tmp_path, capsys):
        import json

        from kiro_crew.slack.workspace_record import SLACK_SWITCH_MARKER_MAX_ROWS

        rows = [
            {"key": f"d:{i}", "slack_thread_ts": f"{i}.1"}
            for i in range(SLACK_SWITCH_MARKER_MAX_ROWS + 1)
        ]
        record = json.dumps(
            {"team_id": "T0A", "pending": {"team_id": "T0B", "swept": rows}}
        ).encode()
        bundle = _config_bundle(tmp_path, record, name="over-cap")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert f"more than {SLACK_SWITCH_MARKER_MAX_ROWS} rows" in out, out
        assert not (home / "slack_workspace.json").exists()

    def test_the_validator_mirrors_the_readers_shape(self):
        """Every shape the gateway reader refuses, the restore validator names,
        and every shape it accepts passes -- the two must not drift apart."""
        import json

        from kiro_crew.slack.gateway import _load_slack_workspace_record

        cases = [
            {"team_id": 123},
            {"team_id": "T0A", "pending": "T0B"},
            {"team_id": "T0A", "pending": {"team_id": "", "swept": []}},
            {"team_id": "T0A", "pending": {"team_id": "T0B", "swept": "rows"}},
            {"team_id": "T0A", "pending": {"team_id": "T0B", "swept": [1]}},
            {
                "team_id": "T0A",
                "pending": {
                    "team_id": "T0B",
                    "swept": [{"key": "k", "slack_thread_ts": "1.1", "slack_channel_id": 5}],
                },
            },
            {"team_id": "T0A", "pending": {"team_id": "T0B", "swept": [{"key": "k"}]}},
            {"team_id": "T0A"},
            {"team_id": ""},
            {
                "team_id": "T0A",
                "pending": {
                    "team_id": "T0B",
                    "swept": [{"key": "k", "slack_thread_ts": "1.1", "slack_channel_id": None}],
                },
            },
        ]
        import tempfile

        for parsed in cases:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "slack_workspace.json"
                p.write_text(json.dumps(parsed), encoding="utf-8")
                reader_refuses = _load_slack_workspace_record(p) is None
            validator_refuses = snap.COMPONENT_JSON_VALIDATORS["slack_workspace.json"](parsed)
            assert reader_refuses == (validator_refuses is not None), parsed


_SLACK_MAP = b'{"dashboard:d1": {"sid": "s1", "slack_thread_ts": "1.1", "slack_channel_id": "C1"}}'
_NO_SLACK_MAP = b'{"dashboard:d1": {"sid": "s1"}}'


def _map_bundle(tmp_path: Path, session_map: bytes, name: str, record: bytes | None) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260103T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "session_map.json").write_bytes(session_map)
    if record is not None:
        (payload / "slack_workspace.json").write_bytes(record)
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / f"{name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


class TestLegacyMapWithoutWorkspaceRecordIsRefusedOverABoundHome:
    """`slack_workspace.json` joined the config component after the session map
    did. A bundle from before then carries Slack links with no record of the
    workspace they belong to; restored over a home whose live record names a
    workspace, the links would be kept as that workspace's (the handshake sees
    no switch) and route its traffic into another workspace's threads. Refused
    before the live map moves -- and only in exactly that combination."""

    def test_refused_and_nothing_moves(self, home, tmp_path, capsys):
        (home / "slack_workspace.json").write_text('{"team_id": "T0B"}', encoding="utf-8")
        (home / "session_map.json").write_bytes(_NO_SLACK_MAP)
        bundle = _map_bundle(tmp_path, _SLACK_MAP, "legacy", record=None)
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "1 Slack conversation link(s)" in out and "T0B" in out, out
        assert "Refusing to restore" in out, out
        assert (home / "session_map.json").read_bytes() == _NO_SLACK_MAP
        assert (home / "slack_workspace.json").read_text(encoding="utf-8") == '{"team_id": "T0B"}'

    @pytest.mark.parametrize(
        "session_map, record, live_record",
        [
            # the record travels with the map: the connect path sees any switch
            (_SLACK_MAP, b'{"team_id": "T0A"}', '{"team_id": "T0B"}'),
            # no Slack binding to re-home
            (_NO_SLACK_MAP, None, '{"team_id": "T0B"}'),
            # first-boot home: no identity for the links to be kept under
            (_SLACK_MAP, None, None),
            (_SLACK_MAP, None, '{"team_id": ""}'),
        ],
    )
    def test_every_other_combination_restores(
        self, home, tmp_path, capsys, session_map, record, live_record
    ):
        if live_record is not None:
            (home / "slack_workspace.json").write_text(live_record, encoding="utf-8")
        bundle = _map_bundle(tmp_path, session_map, "ok", record=record)
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "session_map.json").read_bytes() == session_map

    def test_merge_over_a_live_map_is_not_an_install(self, home, tmp_path, capsys):
        """Merge leaves a present destination alone, so the bundle's links never
        reach the home and there is nothing to refuse."""
        (home / "slack_workspace.json").write_text('{"team_id": "T0B"}', encoding="utf-8")
        (home / "session_map.json").write_bytes(_NO_SLACK_MAP)
        bundle = _map_bundle(tmp_path, _SLACK_MAP, "merge", record=None)
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "session_map.json").read_bytes() == _NO_SLACK_MAP

    def test_an_empty_bundle_record_protects_nothing(self, home, tmp_path, capsys):
        """``{"team_id": ""}`` is the "no identity ever recorded" state: installed
        beside the links it makes boot's first-record branch keep them under
        the home's workspace, exactly as no record would -- so it is not the
        record that lets the links through. (No writer produces one, but the
        gateway's own "repair or remove" guidance makes it a plausible hand
        repair on a home later snapshotted.)"""
        (home / "slack_workspace.json").write_text('{"team_id": "T0B"}', encoding="utf-8")
        bundle = _map_bundle(tmp_path, _SLACK_MAP, "empty-record", record=b'{"team_id": ""}')
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Slack conversation link(s)" in out and "empty one" in out, out
        assert (home / "slack_workspace.json").read_text(encoding="utf-8") == '{"team_id": "T0B"}'
        assert not (home / "session_map.json").exists()

    @pytest.mark.parametrize("record", [None, b'{"team_id": "T0A"}'])
    def test_merge_onto_a_bound_home_without_a_map_is_refused(self, home, tmp_path, capsys, record):
        """Merge installs per file: the map lands (no live one), the bundle's
        record does NOT (a live one exists), so a record in the bundle changes
        nothing about what the home ends up with."""
        (home / "slack_workspace.json").write_text('{"team_id": "T0B"}', encoding="utf-8")
        bundle = _map_bundle(tmp_path, _SLACK_MAP, "merge-fresh", record=record)
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Slack conversation link(s)" in out, out
        assert not (home / "session_map.json").exists()

    def test_merge_installs_the_record_only_with_its_map(self, home, tmp_path, capsys):
        """A home with a live map and no record (every pre-record install until
        its first recording boot) must not take a bundle's record alone: the
        next handshake would read it as the former identity and sweep every
        live link. The record lands only where the map lands."""
        (home / "session_map.json").write_bytes(_SLACK_MAP)  # live links, no record
        bundle = _map_bundle(tmp_path, _NO_SLACK_MAP, "rec-only", record=b'{"team_id": "T0A"}')
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert not (home / "slack_workspace.json").exists()
        assert (home / "session_map.json").read_bytes() == _SLACK_MAP
        assert "skipped (its session map is not being restored)" in out, out

        # And a record the reader would refuse is not what stops such a merge:
        # it is not going to be installed, so it is not validated either.
        bundle = _map_bundle(tmp_path, _NO_SLACK_MAP, "rec-bad", record=b'{"team_id": 1}')
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert not (home / "slack_workspace.json").exists()

    @pytest.mark.parametrize("live_record", [None, '{"team_id": "T0MINE"}'])
    def test_replace_never_installs_the_record_without_its_map(
        self, home, tmp_path, capsys, live_record
    ):
        """Replace keeps a live core file the bundle does not carry, so a
        record-only bundle would land a foreign identity beside the home's
        surviving map. The record is skipped; the live map and the live record
        (if any) stay as they were."""
        (home / "session_map.json").write_bytes(_SLACK_MAP)
        if live_record is not None:
            (home / "slack_workspace.json").write_text(live_record, encoding="utf-8")
        payload = tmp_path / "kirocrew-snapshot-20260104T000000Z"
        payload.mkdir()
        (payload / "config.json").write_text("{}", encoding="utf-8")
        (payload / "slack_workspace.json").write_text('{"team_id": "T0FOREIGN"}', encoding="utf-8")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "record-only.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "skipped (its session map is not in this snapshot)" in out, out
        assert (home / "session_map.json").read_bytes() == _SLACK_MAP
        if live_record is None:
            assert not (home / "slack_workspace.json").exists()
        else:
            assert (home / "slack_workspace.json").read_text(encoding="utf-8") == live_record
        assert (home / "config.json").read_text(encoding="utf-8") == "{}"  # the rest installs

    def test_merge_onto_a_fresh_home_installs_map_and_record_together(self, home, tmp_path, capsys):
        bundle = _map_bundle(tmp_path, _SLACK_MAP, "both", record=b'{"team_id": "T0A"}')
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "config"]
            + unpinnable_argv()
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "session_map.json").read_bytes() == _SLACK_MAP
        assert (home / "slack_workspace.json").read_bytes() == b'{"team_id": "T0A"}'

    @pytest.mark.parametrize(
        "raw, count",
        [
            ({}, 0),
            ([], 0),
            ({"dashboard:d": {"sid": "s"}}, 0),
            ({"dashboard:d": {"sid": "s", "slack_thread_ts": ""}}, 0),
            ({"dashboard:d": {"sid": "s", "slack_thread_ts": "1.1"}}, 1),
            ({"slack:1.1": {"sid": "s"}}, 1),
            ({"1.1": "sid-legacy"}, 1),
            ({"a": {"sid": "s", "slack_thread_ts": "1.1"}, "b": {"sid": "t"}, "c": "u"}, 2),
        ],
    )
    def test_link_count_names_every_shape_the_loader_binds(self, raw, count):
        from kiro_crew.slack.workspace_record import session_map_slack_link_count

        assert session_map_slack_link_count(raw) == count


class TestTheRecordAndTheMapAreStagedFromOneGeneration:
    """Staging copies the workspace record BEFORE the session map, which pairs
    a record naming a workspace only with links written under it -- but only if
    the record did not move while the map was copied. A gateway that writes its
    first record, or commits a workspace switch, between the two copies would
    otherwise leave a bundle whose record names one workspace and whose rows
    were written under another. The record is read again after the map (into a
    probe name the bundle never carries); when the two reads differ, the map is
    copied again under the record as it now stands, bounded; a record that
    keeps moving fails the snapshot."""

    PROBE = "slack_workspace.json.pairing-probe"

    def _stage(self, tmp_path, monkeypatch, on_map_copy, *, initial_record=None):
        import tarfile

        from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

        from kiro_crew import pinned_fs
        from kiro_crew.snapshot import snapshot_main

        home = tmp_path / "home"
        _setup_fake_kirocrew(home)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "session_map.json").write_bytes(_SLACK_MAP)
        record = home / "slack_workspace.json"
        if initial_record is not None:
            record.write_text(initial_record, encoding="utf-8")

        real_copy = pinned_fs.copy_file_pinned
        copies: list[str] = []

        def _copy(src, dst, *a, **k):
            name = Path(str(dst)).name
            if name in ("session_map.json", "slack_workspace.json", self.PROBE):
                copies.append(name)
            out = real_copy(src, dst, *a, **k)
            if name == "session_map.json":
                on_map_copy(record, copies.count("session_map.json"))
            return out

        monkeypatch.setattr(pinned_fs, "copy_file_pinned", _copy)
        out = tmp_path / "out"
        rc = snapshot_main([str(out), "--components", "config"] + unpinnable_argv())
        if rc != 0:
            return rc, None, copies
        tarball = sorted(out.glob("kirocrew-*.tar.gz"))[-1]
        into = tmp_path / "unpacked"
        into.mkdir()
        with tarfile.open(str(tarball)) as t:
            t.extractall(into, filter=lambda m, _d="": m)
        payload = next(d for d in into.iterdir() if d.name.startswith("kirocrew-"))
        return rc, payload, copies

    def test_a_first_record_written_between_the_copies_is_staged_with_a_recopied_map(
        self, tmp_path, monkeypatch
    ):
        def _first_record_lands(record, nth_map_copy):
            if nth_map_copy == 1:
                record.write_text('{"team_id": "T0FIRST"}', encoding="utf-8")

        rc, payload, copies = self._stage(tmp_path, monkeypatch, _first_record_lands)
        assert rc == 0
        # Map (record absent), probe finds the record: adopt it, copy the map
        # again beneath it, probe once more -- stable.
        assert copies == ["session_map.json", self.PROBE, "session_map.json", self.PROBE]
        assert (payload / "session_map.json").read_bytes() == _SLACK_MAP
        assert (payload / "slack_workspace.json").read_text(encoding="utf-8") == (
            '{"team_id": "T0FIRST"}'
        )
        assert not (payload / self.PROBE).exists()

    def test_a_switch_committed_between_the_copies_recopies_the_map_under_the_new_record(
        self, tmp_path, monkeypatch
    ):
        def _switch_lands(record, nth_map_copy):
            if nth_map_copy == 1:
                # The gateway adopts workspace B and sweeps the map.
                record.write_text('{"team_id": "T0B"}', encoding="utf-8")
                (record.parent / "session_map.json").write_bytes(b"{}")

        rc, payload, copies = self._stage(
            tmp_path, monkeypatch, _switch_lands, initial_record='{"team_id": "T0A"}'
        )
        assert rc == 0
        assert copies == [
            "slack_workspace.json",
            "session_map.json",
            self.PROBE,
            "session_map.json",
            self.PROBE,
        ]
        # Never A's rows under B's record: the map is the one B left.
        assert (payload / "slack_workspace.json").read_text(
            encoding="utf-8"
        ) == '{"team_id": "T0B"}'
        assert (payload / "session_map.json").read_bytes() == b"{}"

    def test_a_stable_record_costs_one_probe_and_no_recopy(self, tmp_path, monkeypatch):
        rc, payload, copies = self._stage(
            tmp_path, monkeypatch, lambda r, n: None, initial_record='{"team_id": "T0A"}'
        )
        assert rc == 0
        assert copies == ["slack_workspace.json", "session_map.json", self.PROBE]
        assert (payload / "slack_workspace.json").read_text(
            encoding="utf-8"
        ) == '{"team_id": "T0A"}'

    def test_a_record_that_keeps_moving_fails_the_snapshot(self, tmp_path, monkeypatch, capsys):
        from kiro_crew import snapshot

        def _keeps_switching(record, nth_map_copy):
            record.write_text('{"team_id": "T0GEN%d"}' % nth_map_copy, encoding="utf-8")

        rc, payload, copies = self._stage(
            tmp_path, monkeypatch, _keeps_switching, initial_record='{"team_id": "T0A"}'
        )
        assert rc != 0 and payload is None
        # The first copy plus one re-copy per failed pass, then the refusal.
        assert copies.count("session_map.json") == snapshot._SLACK_PAIRING_ATTEMPTS + 1
        assert not list((tmp_path / "out").glob("kirocrew-*.tar.gz"))
