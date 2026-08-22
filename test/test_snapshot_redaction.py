"""The copy that leaves the host is redacted; the copy on local disk is not.

The boundary is the upload, not the archive. A local bundle sits on the machine that
already holds these secrets, so redacting it would destroy the only copy that restores
complete and buy nothing. What crosses the boundary is a separate, redacted copy.
"""

from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact, snapshot_remote


def _real_db(path: Path, secret: str | None = None) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE semantic_memory (key, value_json)")
    conn.execute("INSERT INTO semantic_memory VALUES (?, ?)", ("note", secret or "nothing secret"))
    conn.commit()
    conn.close()
    return path.read_bytes()


TOKEN = "8412345678:AAH9xSECRETtokenvalue_here12345"


@pytest.fixture(autouse=True)
def opted_in(monkeypatch):
    """Outbound redaction is OFF by default, so these tests must ask for it.

    What guards the bundle is the owner-only destination, re-verified at every upload.
    Redaction is an opt-in on top of that, so a test of the redaction PASS has to turn it
    on the way an operator would. `TestTheOptOut` patches this same function in its own
    bodies, which runs after this fixture and therefore still wins.
    """
    monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: True)


@pytest.fixture
def bundle(tmp_path):
    """A written archive carrying a bot token in config and a key inside memory."""
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True)
    (payload / "config.json").write_text(
        json.dumps({"telegram": {"bot_token": TOKEN}}), encoding="utf-8"
    )
    (payload / "memory.db").write_bytes(
        _real_db(tmp_path / "src.db", f"my key is ghp_{'A' * 36} ok")
    )
    (payload / "memory_index.db").write_bytes(_real_db(tmp_path / "idx.db"))
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
        encoding="utf-8",
    )
    out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Capture what upload() was handed, and under which object key."""
    calls: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        snapshot_remote,
        "load_destination",
        lambda: snapshot_remote.Destination(
            bucket="b", region="us-west-2", account="123456789012", created_at="t"
        ),
    )
    monkeypatch.setattr(snap, "_resolve_aws_profile", lambda *a, **k: ("p", "us-west-2"))

    def fake_upload(payload, dest, profile, *, key_name=None, **kw):
        # Copy it aside: the real path deletes the temp copy when the upload returns.
        keep = tmp_path / "uploaded.tar.gz"
        keep.write_bytes(Path(payload).read_bytes())
        calls.append((keep, key_name or Path(payload).name))
        return "s3://b/x"

    monkeypatch.setattr(snapshot_remote, "upload", fake_upload)
    return calls


def _read_member(archive: Path, suffix: str) -> bytes:
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            if m.name.endswith(suffix):
                f = tf.extractfile(m)
                assert f is not None
                return f.read()
    raise AssertionError(f"{suffix} not found in {archive}")


def _member_names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tf:
        return tf.getnames()


def _bundle_with(tmp_path: Path, extra: dict[str, bytes]) -> Path:
    """A minimal valid bundle carrying *extra* entries alongside a real manifest."""
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True)
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}), encoding="utf-8"
    )
    (payload / "memory.db").write_bytes(_real_db(tmp_path / "m.db"))
    for name, data in extra.items():
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


class TestTheOutboundCopyIsRedacted:
    def test_the_token_does_not_leave_the_host(self, bundle, sent, capsys):
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert sent, out
        uploaded, _key = sent[0]
        assert TOKEN not in _read_member(uploaded, "config.json").decode("utf-8")

    def test_a_key_pasted_into_memory_does_not_leave_either(self, bundle, sent, capsys):
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        uploaded, _ = sent[0]
        assert b"ghp_" + b"A" * 36 not in _read_member(uploaded, "memory.db")

    def test_the_uploaded_database_is_still_a_database(self, bundle, sent, tmp_path, capsys):
        """The whole reason redaction goes through SQL instead of over the bytes."""
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        uploaded, _ = sent[0]
        restored = tmp_path / "roundtrip.db"
        restored.write_bytes(_read_member(uploaded, "memory.db"))
        conn = snap.sqlite3.connect(str(restored))
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 1
        conn.close()

    def test_the_local_bundle_is_left_unredacted(self, bundle, sent, capsys):
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        assert TOKEN in _read_member(bundle, "config.json").decode(
            "utf-8"
        ), "the local archive was modified -- it must stay restorable"

    def test_the_object_keeps_the_original_name(self, bundle, sent, capsys):
        """A restore looks for the bundle's name, not a temp file's."""
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        _uploaded, key = sent[0]
        assert key == bundle.name, key

    def test_it_reports_what_it_changed(self, bundle, sent, capsys):
        snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert "Redacted the outbound copy" in out, out
        assert "config.json" in out, out
        assert "still restores complete" in out, out


class TestRedactionFailureRefusesRatherThanLeaking:
    def test_a_pass_that_cannot_run_uploads_nothing(self, bundle, sent, monkeypatch, capsys):
        """An IO failure inside the pass is a refusal, not a traceback.

        A traceback would leave the operator unable to tell a crash from a leak, and the
        branch that decides what to upload would never run at all.
        """

        def boom(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(snapshot_redact, "redact_bundle_for_egress", boom)
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Refusing to upload" in out, out
        assert "no space left on device" in out, out
        assert sent == [], "a failed redaction still published the bundle"

    def test_an_unreadable_bundle_refuses_with_a_reason(self, tmp_path, sent, capsys):
        """Refused before redaction is even reached, by the earlier re-read guard.

        Asserted as an outcome rather than a message, because which guard catches it is an
        ordering detail; that nothing is published is the property.
        """
        broken = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        broken.write_bytes(b"not a tarball")
        rc = snap._upload_bundle(broken, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "not uploading" in out or "Nothing was published" in out, out
        assert sent == [], "an unredactable bundle was uploaded anyway"


class TestTheOptOut:
    def test_disabling_redaction_uploads_the_original(self, bundle, sent, monkeypatch, capsys):
        # The opt-out is not a config field: it lives behind the keystone fence on the
        # backup directory, because a switch the agent can write is a switch the agent can
        # use to publish plaintext credentials through a sanctioned path.
        monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: False)
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Redaction is DISABLED" in out, out
        uploaded, _ = sent[0]
        assert TOKEN in _read_member(uploaded, "config.json").decode(
            "utf-8"
        ), "the opt-out must actually send the complete bundle"

    def test_an_oversized_bundle_is_refused_even_with_redaction_off(
        self, bundle, sent, monkeypatch, capsys
    ):
        """The upload's own bound is load-bearing exactly here.

        With redaction ON, the pass opens the archive and bounds it too, so the upload's
        own check is shadowed. Turning redaction OFF removes that second reader, and this
        is the only path left that keeps an unrestorable bundle out of the bucket.
        """
        # The opt-out is not a config field: it lives behind the keystone fence on the
        # backup directory, because a switch the agent can write is a switch the agent can
        # use to publish plaintext credentials through a sanctioned path.
        monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: False)
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 2)

        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "declares more than" in out, out
        assert sent == [], "an unrestorable bundle was uploaded with redaction off"

    def test_an_unreadable_app_config_does_not_change_the_switch(
        self, bundle, sent, monkeypatch, capsys
    ):
        """The switch is its own fenced file, not a field in the app config.

        So a config that cannot be read says nothing about redaction either way: with the
        operator opted in, the pass still runs.
        """
        from kiro_crew.config import loader as cfg_loader

        def boom(*a, **k):
            raise OSError("config unreadable")

        monkeypatch.setattr(cfg_loader.KiroCrewConfig, "load", staticmethod(boom))
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 0, out
        uploaded, _ = sent[0]
        assert TOKEN not in _read_member(uploaded, "config.json").decode("utf-8")


class TestNothingUnprovableLeavesTheHost:
    """Anything that cannot be shown redacted is removed, not shipped hoping it is fine."""

    def test_secret_only_files_are_left_out(self, tmp_path, sent, capsys):
        out = _bundle_with(tmp_path, {"telemetry_salt": b"saltvalue", "sel_hmac.key": b"k"})
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        uploaded, _ = sent[0]
        names = _member_names(uploaded)
        assert not any(n.endswith("telemetry_salt") for n in names), names
        assert not any(n.endswith("sel_hmac.key") for n in names), names

    def test_an_ordinary_source_file_is_redacted_not_deleted(self, tmp_path, sent, capsys):
        """A workspace holds arbitrary files; an unlisted suffix is not a reason to drop one.

        `tool.py` is text. Deciding by suffix classified it as opaque and removed it, so an
        off-host restore reported success while permanently lacking the operator's file.
        """
        out = _bundle_with(
            tmp_path,
            {
                "workspace/project/tool.py": f'TOKEN = "{TOKEN}"\nprint("hi")\n'.encode(),
                "workspace/data/rows.csv": b"a,b\n1,2\n",
                "workspace/page.html": b"<p>ok</p>",
            },
        )
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        uploaded, _ = sent[0]
        names = _member_names(uploaded)
        for keep in ("tool.py", "rows.csv", "page.html"):
            assert any(n.endswith(keep) for n in names), f"{keep} was dropped: {names}"
        # And it was actually redacted, not merely kept.
        assert TOKEN not in _read_member(uploaded, "tool.py").decode("utf-8")

    def test_a_file_that_cannot_be_read_refuses_rather_than_passing(
        self, tmp_path, sent, monkeypatch, capsys
    ):
        """Unreadable is not the same as clean, and it must not escape as a traceback.

        Patched rather than produced with permissions, because a mode-000 file is still
        readable by its owner on Windows -- the branch would go untested exactly where the
        platform differs.
        """
        out = _bundle_with(tmp_path, {"workspace/notes.md": b"hello"})
        real_read = Path.read_text

        def fail_on_notes(self, *a, **k):
            if self.name == "notes.md":
                raise OSError(5, "Input/output error")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", fail_on_notes)
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        text = capsys.readouterr().out
        assert rc == 1, text
        assert "Refusing to upload" in text, text
        assert "Traceback" not in text
        assert sent == [], "a bundle with an unreadable file was uploaded"

    def test_the_unreadable_signal_is_an_io_error(self):
        """So the upload path's IO handler reports it instead of letting it escape."""
        assert issubclass(snapshot_redact._FileUnreadable, OSError)

    def test_a_genuinely_opaque_file_refuses_the_upload_and_is_kept(self, tmp_path, sent, capsys):
        """Not text -> cannot be shown clean. Refuse; do not quietly drop the file.

        The bytes here are undecodable on purpose: a NUL-filled file IS valid UTF-8, so
        "looks binary" is not the test -- decoding is.
        """
        png = b"\x89PNG\r\n\x1a\n\xff\xd8\xff\xe0binary"
        out = _bundle_with(tmp_path, {"workspace/shot.png": png})
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        text = capsys.readouterr().out
        assert rc == 1, text
        assert "not text" in text, text
        assert "shot.png" in text, text
        assert "NOT removed" in text, text
        assert sent == [], "an unprovable bundle was uploaded"
        # The local bundle is untouched, so the operator still holds the complete copy.
        assert any(n.endswith("shot.png") for n in _member_names(out))

    def test_a_corrupt_product_database_refuses_rather_than_shipping_without_it(
        self, tmp_path, sent, capsys
    ):
        """Dropping the payload IS shipping a broken backup, so this refuses instead.

        This asserted that an unprovable product database was removed and the remainder
        uploaded. That produces an off-host copy which reports success and restores
        nothing, discovered only once the machine it came from is gone -- the exact
        failure the feature exists to prevent. The database the backup carries is not a
        file that may be silently omitted, so the upload is refused and names it, and the
        complete local archive is left untouched.
        """
        out = _bundle_with(
            tmp_path, {"workspace/knowledge/knowledge.db": b"this is not a database"}
        )
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 1
        assert sent == [], "a backup stripped of its own database was uploaded"
        assert "knowledge.db" in capsys.readouterr().out

    def test_a_corrupt_database_the_product_does_not_own_refuses_the_upload(
        self, tmp_path, sent, capsys
    ):
        """Deleting an operator's file to protect them from it is not a trade to make."""
        out = _bundle_with(tmp_path, {"workspace/projects/notes.db": b"not a database"})
        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 1
        assert sent == [], "an unprovable bundle was uploaded"
        assert "notes.db" in capsys.readouterr().out

    def test_the_uploaded_manifest_records_the_redaction(self, bundle, sent, capsys):
        """Restore's warning depends on this stamp, so the upload path must write it."""
        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        assert rc == 0, capsys.readouterr().out
        uploaded, _ = sent[0]
        mf = json.loads(_read_member(uploaded, "MANIFEST.json").decode("utf-8"))
        assert mf.get("redaction", {}).get("redacted") is True, mf
        assert mf["redaction"]["replacements"], mf

    def test_a_redaction_failure_publishes_nothing(self, tmp_path, sent, capsys):
        """Two roots is a state the pass refuses -- and a refusal must not upload."""
        stage = tmp_path / "two"
        for name in ("kirocrew-snapshot-20260101T000000Z", "kirocrew-snapshot-other"):
            (stage / name).mkdir(parents=True)
        out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            for child in sorted(stage.iterdir()):
                tf.add(str(child), arcname=child.name)

        rc = snap._upload_bundle(out, argparse.Namespace(aws_profile=None), ["memory"])
        text = capsys.readouterr().out
        assert rc == 1, text
        assert "Refusing to upload" in text, text
        assert sent == [], "a bundle that could not be redacted was uploaded anyway"


class TestRestoreAnnouncesARedactedBundle:
    def test_it_says_the_credentials_are_inert(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "components": {"memory": "unresolved"},
                    "redaction": {
                        "redacted": True,
                        "replacements": {"config.json": 1},
                        "dropped": ["telemetry_salt"],
                        "indexes_needing_rebuild": ["memory_index.db"],
                    },
                }
            ),
            encoding="utf-8",
        )
        snap._report_redacted_bundle(payload)
        out = capsys.readouterr().out
        assert "REDACTED before it left its host" in out, out
        assert "config.json: 1" in out, out
        assert "Re-enter those credentials" in out, out
        assert "rebuilding" in out, out

    def test_it_stays_quiet_for_an_ordinary_bundle(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
            encoding="utf-8",
        )
        snap._report_redacted_bundle(payload)
        assert capsys.readouterr().out == ""
