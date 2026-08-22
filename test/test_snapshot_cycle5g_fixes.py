"""Non-database components are validated too, the authorization decision is audited, and
an un-redacted backup says what it carries.

Three gaps that share a root: a rule was implemented for the case that prompted it and not
for the others it applies to equally. Databases were validated but component JSON was not.
The authorization CHECK was moved to the mutation site but its AUDIT stayed in the wrapper.
A backup is deliberately un-redacted, but nothing told the operator what that includes.
"""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote


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
        )
        assert rc == 0, capsys.readouterr().out
        assert (home / "crons.json").is_file()

    def test_merge_validates_the_shape_of_a_crons_file_it_will_not_install(self, home, tmp_path):
        """Merge parses the incoming crons file even though it never installs it.

        The two failure kinds are not the pre-flight's to treat alike. A file that parses
        into the wrong shape flows past the merger's own parse guard and reaches field
        access on an entry partway through the merge, so refusing here is the only place
        it is still free. A file that does not parse at all is the merger's to report, and
        it returns without writing live state, so nothing is lost or lost quietly.
        """
        (home / "crons.json").write_text('{"jobs": [{"id": "local"}]}', encoding="utf-8")
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)

        (payload / "crons.json").write_text('{"jobs": ["x"]}', encoding="utf-8")
        with pytest.raises(snap.SourceComponentUnsound):
            snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

        # And still refused when there is no local file to keep.
        (home / "crons.json").unlink()
        with pytest.raises(snap.SourceComponentUnsound):
            snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

        # Unparseable is the merger's own to report on this path, so the pre-flight
        # stands aside rather than failing the restore of every other component.
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


class TestTheAuthorizationDecisionIsAudited:
    def test_a_refusal_is_recorded_at_the_decision_not_the_wrapper(self, tmp_path, monkeypatch):
        """A direct library call makes a real decision, so the log has to see it."""
        events = []
        monkeypatch.setattr(remote, "_audit_authorization", lambda o, d: events.append((o, d)))
        monkeypatch.setattr(remote, "authorization_token_path", lambda: tmp_path / "absent")
        with pytest.raises(remote.DestinationError):
            remote.consume_authorization("123456789012", "us-west-2", "b")
        assert events and events[0][0] == "denied", events

    def test_a_grant_is_recorded_only_after_the_token_is_consumed(self, tmp_path, monkeypatch):
        """The wrapper's pre-flight cannot stand in: it runs before consumption."""
        token = tmp_path / "authorized.json"
        token.write_text('{"account": "123456789012", "region": "us-west-2"}', encoding="utf-8")
        order = []
        monkeypatch.setattr(remote, "authorization_token_path", lambda: token)
        monkeypatch.setattr(
            remote,
            "_audit_authorization",
            lambda o, d: order.append(("audit", o, token.exists())),
        )
        remote.consume_authorization("123456789012", "us-west-2", "b")
        assert order == [("audit", "completed", False)], order

    def test_the_audit_lives_beside_the_consumption(self):
        import inspect

        # The decision path spans two functions since the consume step moved inside the
        # cross-process lock, so both are read -- an audit that moved out of view is
        # still an audit, but one counted in only one of them would look deleted.
        src = inspect.getsource(remote.consume_authorization) + inspect.getsource(
            remote._consume_verified_authorization
        )
        assert src.count("_audit_authorization(") >= 5, (
            "every decision path -- missing, already consumed, unreadable, mismatched, "
            "granted -- must be recorded"
        )


class TestAnUnredactedBackupSaysWhatItCarries:
    def test_it_names_the_uncertified_components(self, capsys):
        snap._report_unresolved_payload(["memory", "config"])
        out = capsys.readouterr().out
        assert "uncertified for sharing" in out.lower(), out
        assert "config" in out and "memory" in out, out

    def test_it_does_not_claim_a_redaction_state_it_no_longer_owns(self, capsys):
        """Whether the OUTBOUND copy is redacted is decided later and reported there.

        This notice runs before the destination is even loaded, so asserting "NOT
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

    def test_it_runs_before_the_upload_writes_anything(self):
        import inspect

        src = inspect.getsource(snap._upload_bundle)
        assert src.index("_report_unresolved_payload(") < src.index("load_destination()")

    def test_no_component_is_certified_share_safe_yet(self):
        """The disclosure's premise: nothing has been cleared for another person's hands."""
        assert all(spec.policy is snap.SecretPolicy.UNRESOLVED for spec in snap.COMPONENTS.values())
        assert argparse  # import kept meaningful for the namespace-built callers above
