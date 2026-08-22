"""Two hosts must not share a backup prefix, and our own tree databases are strict.

A hostname is not an identifier: machines built from one image answer the same name, and
identical name plus identical bundle filename is an identical S3 key. Under versioning the
loser becomes a noncurrent version the lifecycle rule expires, so one machine's backup
vanishes because another shares its name.
"""

from __future__ import annotations

import socket
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote


def _real_db(path: Path) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path.read_bytes()


class TestTheHostPrefixSurvivesAHostnameCollision:
    def test_the_same_hostname_on_two_machines_yields_different_keys(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "buildbox")
        dest = remote.Destination(
            bucket="b", region="us-west-2", account="123456789012", created_at="t"
        )

        monkeypatch.setattr(remote, "_machine_fingerprint", lambda: "aaaaaaaa")
        first = dest.key_for("kirocrew-snapshot-20260101T000000Z.tar.gz")
        monkeypatch.setattr(remote, "_machine_fingerprint", lambda: "bbbbbbbb")
        second = dest.key_for("kirocrew-snapshot-20260101T000000Z.tar.gz")

        assert first != second, (
            "same hostname and same bundle filename produced one key — one machine's "
            "backup would overwrite the other's"
        )

    def test_the_hostname_still_leads_so_an_operator_recognises_it(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "Workshop-01.corp.example")
        assert remote.host_id().startswith("workshop-01-")

    def test_the_raw_machine_id_is_never_published_in_the_key(self):
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                raw = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw:
                assert raw not in remote.host_id(), (
                    "the raw machine id reached the S3 key, which anyone able to list "
                    "the bucket can read"
                )
                return
        pytest.skip("no OS machine id on this host")

    def test_it_is_stable_across_calls(self):
        assert remote.host_id() == remote.host_id()

    def test_it_stays_within_the_safe_length(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "x" * 200)
        assert len(remote.host_id()) <= 63

    def test_the_fingerprint_actually_varies_with_the_machine(self, monkeypatch):
        """A constant fingerprint would collide for every host while looking correct.

        Patching the fingerprint helper in the key test cannot catch that, so this drives
        the real helper with two different machine-id contents.
        """
        seen = []

        def fake_read_text(self, *a, **kw):
            return seen[-1]

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        # Hex with letters: a machine id is hex, and a long run of digits would trip the
        # repository's account-id sweep and desensitise it.
        seen.append("abcdefabcdefabcdefabcdefabcdefab")
        first = remote._machine_fingerprint()
        seen.append("fedcbafedcbafedcbafedcbafedcbafe")
        second = remote._machine_fingerprint()
        assert (
            first != second
        ), "the fingerprint ignores the machine id, so every host shares one prefix"

    def test_a_fingerprint_is_returned_even_when_the_home_is_unwritable(
        self, monkeypatch, tmp_path
    ):
        """No OS id and an unwritable home must still produce a usable prefix."""
        monkeypatch.setattr(Path, "read_text", _raise_oserror)
        monkeypatch.setattr(remote.platform_compat, "make_owner_only_dir", _raise_oserror_any)
        got = remote._machine_fingerprint()
        assert len(got) == 8 and all(c in "0123456789abcdef" for c in got), got


def _raise_oserror(self, *a, **kw):
    raise OSError("no")


def _raise_oserror_any(*a, **kw):
    raise OSError("no")


class TestOurOwnDatabasesInsideATreeAreStrict:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / "home"
        h.mkdir()
        monkeypatch.setattr(snap, "_mc_dir", lambda: h)
        monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
        return h

    def _bundle(self, tmp_path, knowledge_bytes: bytes, name: str) -> Path:
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        kdir = payload / "workspace" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / f"sound-{name}.db"))
        (kdir / name).write_bytes(knowledge_bytes)
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / f"{name}.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        return bundle

    def test_an_unopenable_knowledge_database_is_refused(self, home, tmp_path, capsys):
        """`knowledge.db` is as much ours as `memory.db`, so unopenable is a bad bundle."""
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live.db"))
        before = live.read_bytes()

        bundle = self._bundle(tmp_path, b"torn, not a database", "knowledge.db")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "knowledge.db" in out and "integrity check failed" in out, out
        assert live.read_bytes() == before

    def test_an_incidental_non_database_in_the_same_tree_is_still_allowed(
        self, home, tmp_path, capsys
    ):
        """Leniency has to survive for files that were never databases."""
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live2.db"))
        bundle = self._bundle(tmp_path, b"\x00 windows thumbnail cache", "Thumbs.db")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 0, capsys.readouterr().out

    def test_merge_says_when_it_keeps_an_existing_knowledge_database(self, home, tmp_path, capsys):
        """Merge legitimately keeps the local file; going silent is what misleads.

        The operator asked to merge their knowledge library. Reporting success while
        importing none of it reads as data loss, so the skip is stated outright.
        """
        kdir = home / "workspace" / "knowledge"
        kdir.mkdir(parents=True)
        local = _real_db(tmp_path / "local-knowledge.db")
        (kdir / "knowledge.db").write_bytes(local)
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live3.db"))

        bundle = self._bundle(
            tmp_path, _real_db(tmp_path / "incoming-knowledge.db"), "knowledge.db"
        )
        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "workspace/knowledge/knowledge.db" in out, out
        assert "NOT merged" in out, out
        assert (
            kdir / "knowledge.db"
        ).read_bytes() == local, "merge must keep the local database, not clobber it"

    def test_the_component_help_does_not_promise_a_row_merge(self):
        help_text = snap.COMPONENTS["memory"].help
        assert "workspace/knowledge/" in help_text
        assert "not row-merged" in help_text, (
            "the help advertises the knowledge tree, so it must not imply its database "
            "rows are merged"
        )

    def test_the_limitation_is_documented_where_merge_is_explained(self):
        """A limitation that ships as documentation has to stay true to the code.

        The risk of choosing "document it" over "fix it" is that the two drift, so the
        doc claim and the runtime warning are pinned together.
        """
        doc = (Path(snap.__file__).parent / "docs" / "snapshot-and-restore.md").read_text(
            encoding="utf-8"
        )
        assert "not row-merged" in doc, "the merge limitation is not documented"
        assert "workspace/knowledge/knowledge.db" in doc
        # The doc promises the restore says so on the spot; that promise is the warning.
        import inspect

        src = inspect.getsource(snap._report_unmerged_databases)
        assert (
            "NOT " in src and "--mode replace" in src
        ), "the doc says the restore reports the skip and names --mode replace"

    def test_the_declared_set_is_not_empty_and_is_tree_relative(self):
        assert snap.PRODUCT_TREE_DATABASES, "the strict set must not be empty"
        for rel in snap.PRODUCT_TREE_DATABASES:
            assert "/" in rel, f"{rel} is not inside a tree"
            assert not rel.startswith("/"), rel
            covered = any(
                rel.startswith(f"{tree}/")
                for trees in snap.COMPONENT_TREES.values()
                for tree in trees
            )
            assert covered, f"{rel} is not under any declared component tree"
