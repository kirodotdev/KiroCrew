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

        That hand-off is conditional, and this test used to assert it unconditionally. The
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
