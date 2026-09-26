"""AWS Control backup -- the unchanged-check that stops a nightly re-uploading a copy.

Three layers, because each answers a question the others cannot:

* ``TestTreeFingerprint`` pins the MECHANISM on hand-built archives. This is where
  the "archives differ, decision does not" property lives, and where each
  normalization (the bundle's timestamped root, the manifest's ``created_at``) is
  shown to ignore exactly what it claims to and nothing more -- every exclusion gets
  a case where the value it drops is the ONLY difference, and a second case proving a
  neighbouring value still counts.
* ``TestRealBundleAssumption`` pins an ASSUMPTION about a module this one does not
  own. The skip rests on ``created_at`` being the only field ``snapshot.py`` rewrites
  on a rebuild; that is measured rather than assumed, and this builds two real
  bundles so the day a second volatile field appears CI goes red instead of the skip
  quietly never firing again.
* ``TestUnchangedBaseline`` and the two run-path classes pin the DECISION, in both
  directions. A guard that only ever refuses is indistinguishable from a feature that
  never runs, so every skip test has a sibling proving the same setup uploads when
  something really moved.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile
import time
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

from kiro_crew import pinned_fs
from kiro_crew.apps.builtins.aws_control.backend import backup

ACCOUNT = "111122223333"


def _pack(path: Path, entries: dict[str, bytes], *, dirs: tuple[str, ...] = ()) -> Path:
    """Write a ``tar.gz`` holding exactly *entries* (and any empty *dirs*)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f"{path.name}.stage"
    staging.mkdir()
    for name in dirs:
        (staging / name).mkdir(parents=True, exist_ok=True)
    for name, body in entries.items():
        member = staging / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(body)
    with tarfile.open(path, "w:gz") as tar:
        for child in sorted(staging.iterdir()):
            tar.add(str(child), arcname=child.name)
    return path


def _pack_member(path: Path, *, body: bytes, mode: int, name: str = "a.txt") -> Path:
    """Write one regular tar member with an exact name and permission mode.

    Built from a ``TarInfo`` rather than from a real file so the member's name is
    whatever the caller asks for, including a name the local filesystem could not
    hold. Every assertion over it then runs on every platform.
    """
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.size = len(body)
    with tarfile.open(path, "w:gz") as tar:
        tar.addfile(member, io.BytesIO(body))
    return path


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


class TestTreeFingerprint:
    def test_same_tree_packed_twice_differs_in_bytes_but_not_in_fingerprint(self, tmp_path):
        # The acceptance property, and the reason `_body_fingerprint` cannot answer
        # this question: a tar.gz embeds per-entry mtimes and a gzip stamp, so two
        # runs over one identical tree produce different BYTES. An archive-level
        # comparison would therefore report "changed" every single night and the skip
        # could never fire. Both halves are asserted here -- the differing bytes are
        # what make the matching fingerprint mean something.
        body = {"a.txt": b"hello", "nested/b.txt": b"world"}
        first = _pack(tmp_path / "one.tar.gz", body)
        time.sleep(1.1)  # tar mtimes are second-granular; force them apart
        second = _pack(tmp_path / "two.tar.gz", body)

        assert first.read_bytes() != second.read_bytes()
        assert backup._tree_fingerprint(first, volatile_root=False) == backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_a_surrogate_escaped_member_name_fingerprints_without_raising(self, tmp_path):
        # A session filename that is not valid UTF-8 is an ordinary byte sequence on
        # POSIX, and the session trees are agent-writable. `tarfile` decodes such a
        # name with errors="surrogateescape", so byte 0xff arrives as the lone
        # surrogate U+DCFF -- which is what this fixture spells directly, rather than
        # by planting a file the local filesystem may refuse to name. A strict encode
        # of that row raises OUTSIDE the guarded archive read, so it would propagate
        # out of the fingerprint and crash the whole run instead of falling back to
        # uploading.
        archive = _pack_member(
            tmp_path / "surrogate.tar.gz", body=b"x", mode=0o644, name="bad\udcff name.txt"
        )

        fingerprint = backup._tree_fingerprint(archive, volatile_root=False)

        assert len(fingerprint) == 64, fingerprint
        assert bytes.fromhex(fingerprint).hex() == fingerprint

    def test_two_member_names_differing_only_in_an_escaped_byte_do_not_collide(self, tmp_path):
        # Not raising is not enough: the encoding must still tell two such names
        # apart, or two different trees would share a fingerprint and the second
        # would never be uploaded.
        first = _pack_member(
            tmp_path / "one.tar.gz", body=b"x", mode=0o644, name="bad\udcfe name.txt"
        )
        second = _pack_member(
            tmp_path / "two.tar.gz", body=b"x", mode=0o644, name="bad\udcff name.txt"
        )

        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_permission_only_change_moves_the_fingerprint(self, tmp_path):
        first = _pack_member(tmp_path / "one.tar.gz", body=b"same", mode=0o600)
        second = _pack_member(tmp_path / "two.tar.gz", body=b"same", mode=0o640)

        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_identical_content_and_modes_share_a_fingerprint(self, tmp_path):
        first = _pack_member(tmp_path / "one.tar.gz", body=b"same", mode=0o640)
        second = _pack_member(tmp_path / "two.tar.gz", body=b"same", mode=0o640)

        assert backup._tree_fingerprint(first, volatile_root=False) == backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_changed_file_content_moves_the_fingerprint(self, tmp_path):
        # The other direction. Without this the test above is satisfied by a function
        # that returns a constant.
        first = _pack(tmp_path / "one.tar.gz", {"a.txt": b"hello"})
        second = _pack(tmp_path / "two.tar.gz", {"a.txt": b"hello!"})
        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_a_renamed_file_moves_the_fingerprint(self, tmp_path):
        # Content alone is not the identity: the same bytes at a different path is a
        # changed tree, so paths must be part of the digest.
        first = _pack(tmp_path / "one.tar.gz", {"a.txt": b"hello"})
        second = _pack(tmp_path / "two.tar.gz", {"b.txt": b"hello"})
        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_an_emptied_directory_moves_the_fingerprint(self, tmp_path):
        # An empty directory appears in no file's path, so a digest built only from
        # files would call this unchanged. Non-file members are recorded by name and
        # kind for exactly this case.
        first = _pack(tmp_path / "one.tar.gz", {"a.txt": b"hello"}, dirs=("spare",))
        second = _pack(tmp_path / "two.tar.gz", {"a.txt": b"hello"})
        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_volatile_root_ignores_the_timestamped_bundle_root(self, tmp_path):
        # The snapshot bundle's root directory is named `kirocrew-snapshot-<stamp>`,
        # so it changes every run. Left in the digest it would defeat the comparison
        # on its own, which is what `volatile_root=True` exists to prevent.
        first = _pack(
            tmp_path / "one.tar.gz", {"kirocrew-snapshot-20260101T000000Z/a.txt": b"hello"}
        )
        second = _pack(
            tmp_path / "two.tar.gz", {"kirocrew-snapshot-20260102T111111Z/a.txt": b"hello"}
        )
        assert backup._tree_fingerprint(first, volatile_root=True) == backup._tree_fingerprint(
            second, volatile_root=True
        )
        # And with the normalization off, the differing root is a real difference --
        # so the flag is doing the work rather than the two archives being equal
        # anyway.
        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_volatile_root_still_sees_content_below_the_root(self, tmp_path):
        # Stripping the root must not strip the tree. A blanket "ignore the first
        # segment" that also swallowed what sits under it would make every snapshot
        # look unchanged forever, which is the dangerous direction.
        first = _pack(tmp_path / "one.tar.gz", {"kirocrew-snapshot-20260101T000000Z/a.txt": b"x"})
        second = _pack(tmp_path / "two.tar.gz", {"kirocrew-snapshot-20260102T111111Z/a.txt": b"y"})
        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_sessions_roots_are_meaningful_and_are_not_stripped(self, tmp_path):
        # The sessions archive's roots are `crew` and `cli`, and which half a file
        # belongs to is real information -- the same bytes moving between them is a
        # changed tree. This is why that caller passes `volatile_root=False` instead
        # of this guessing from a name pattern.
        first = _pack(tmp_path / "one.tar.gz", {"crew/t.jsonl": b"x", "cli/other.log": b"y"})
        second = _pack(tmp_path / "two.tar.gz", {"cli/t.jsonl": b"x", "crew/other.log": b"y"})
        assert backup._tree_fingerprint(first, volatile_root=False) != backup._tree_fingerprint(
            second, volatile_root=False
        )

    def test_manifest_created_at_is_ignored(self, tmp_path):
        # The measured exclusion: two bundles built from one untouched tree differ in
        # this field alone, so it cannot be part of the digest or nothing ever matches.
        def bundle(root: str, created: str) -> dict[str, bytes]:
            manifest = {"version": 4, "created_at": created, "purpose": "backup"}
            return {
                f"{root}/MANIFEST.json": json.dumps(manifest).encode(),
                f"{root}/a.txt": b"hello",
            }

        first = _pack(tmp_path / "one.tar.gz", bundle("snap-1", "2026-01-01T00:00:00Z"))
        second = _pack(tmp_path / "two.tar.gz", bundle("snap-2", "2026-06-06T12:34:56Z"))
        assert backup._tree_fingerprint(first, volatile_root=True) == backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_other_manifest_fields_are_not_ignored(self, tmp_path):
        # The exclusion is ONE field, not the member. `purpose`, `staging` and
        # `version` are not derivable from the file set at all, so dropping the whole
        # manifest -- the obvious shortcut -- would silently stop noticing a bundle
        # that switched to an unpinned staging walk. `staging` is the case used here
        # because it is the one with a security meaning.
        def bundle(staging: str) -> dict[str, bytes]:
            manifest = {"version": 4, "created_at": "2026-01-01T00:00:00Z", "staging": staging}
            return {"snap/MANIFEST.json": json.dumps(manifest).encode(), "snap/a.txt": b"hello"}

        first = _pack(tmp_path / "one.tar.gz", bundle("pinned"))
        second = _pack(tmp_path / "two.tar.gz", bundle("unpinned"))
        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_manifest_key_order_does_not_matter(self, tmp_path):
        # The manifest is compared as parsed JSON with a canonical dump, so a
        # re-ordered but equivalent document is not a change.
        first = _pack(
            tmp_path / "one.tar.gz",
            {"snap/MANIFEST.json": b'{"version": 4, "purpose": "backup"}'},
        )
        second = _pack(
            tmp_path / "two.tar.gz",
            {"snap/MANIFEST.json": b'{"purpose": "backup", "version": 4}'},
        )
        assert backup._tree_fingerprint(first, volatile_root=True) == backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_an_unparseable_manifest_reads_as_changed_rather_than_matching(self, tmp_path):
        # A member that is not the JSON object this expects falls back to hashing its
        # raw bytes. That direction is the safe one: a parse failure must make the run
        # upload, never silently satisfy the comparison. Both archives carry
        # unparseable manifests, so a fallback that returned a constant would make
        # them match.
        first = _pack(tmp_path / "one.tar.gz", {"snap/MANIFEST.json": b"not json at all"})
        second = _pack(tmp_path / "two.tar.gz", {"snap/MANIFEST.json": b"also not json"})
        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_a_manifest_below_the_top_level_is_hashed_as_ordinary_content(self, tmp_path):
        # The normalization is scoped to the bundle's OWN manifest at the root. A file
        # that merely happens to be called MANIFEST.json deeper in the tree is the
        # operator's own content and its every byte counts -- otherwise a workspace
        # file with that name would become invisible to the backup.
        first = _pack(tmp_path / "one.tar.gz", {"snap/workspace/MANIFEST.json": b'{"a": 1}'})
        second = _pack(tmp_path / "two.tar.gz", {"snap/workspace/MANIFEST.json": b'{"a": 2}'})
        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_an_unreadable_archive_yields_no_fingerprint_rather_than_raising(self, tmp_path):
        # An archive that is not a gzip at all produces no fingerprint, and an empty
        # value can never match, so the run uploads. Deliberately not a refusal:
        # validating the payload is a different question from deciding whether to send
        # it, and raising here would decide the first one on the way past. Only the skip
        # is unavailable for such a payload.
        broken = tmp_path / "broken.tar.gz"
        broken.write_bytes(b"x")
        assert backup._tree_fingerprint(broken, volatile_root=True) == ""
        assert backup._tree_fingerprint(broken, volatile_root=False) == ""

    def test_a_missing_archive_yields_no_fingerprint(self, tmp_path):
        assert backup._tree_fingerprint(tmp_path / "absent.tar.gz", volatile_root=True) == ""


# ---------------------------------------------------------------------------
# The assumption about snapshot.py, pinned rather than trusted
# ---------------------------------------------------------------------------


def _manifest_field(archive: Path, field: str) -> Any:
    """Read one field out of a real bundle's ``MANIFEST.json``."""
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if member.name.endswith("MANIFEST.json"):
                body = tf.extractfile(member)
                assert body is not None
                return json.loads(body.read()).get(field)
    raise AssertionError(f"no MANIFEST.json in {archive}")


def _build_real_bundle(out: Path) -> Path:
    """Build one real snapshot bundle into *out* and return the archive it wrote.

    The staging mode is the platform's, not a preference. ``snapshot.py`` refuses to
    stage the data home where the platform cannot open a directory relative to a
    descriptor, because a by-name walk is exactly the mechanism an ancestor swapped
    mid-walk redirects; the documented way to proceed there is to ask for that walk
    explicitly, and the manifest records that it was asked for. So the flag is added
    where production requires it and nowhere else, which leaves the pinned walk under
    test on a platform that has one.

    Both sides of every comparison are built through here, so the staging mode is
    identical on either side. That matters because ``staging`` is a manifest field the
    fingerprint KEEPS: were one side pinned and the other not, the two would differ on
    the mode rather than on the tree.
    """
    out.mkdir(parents=True, exist_ok=True)
    argv = [str(out), "--keep", "1"]
    if not pinned_fs.supports_pinned_tree_walk():
        argv.append("--allow-unpinned-staging")
    assert backup.snapshot_main(argv) == 0
    return sorted(out.glob("kirocrew-snapshot-*.tar.gz"))[-1]


class TestRealBundleAssumption:
    def test_two_real_bundles_of_one_unchanged_tree_share_a_fingerprint(
        self, tmp_path, monkeypatch
    ):
        # The whole skip rests on a fact about a module this one does not own: of
        # everything `snapshot.py` writes into a bundle, only MANIFEST.json's
        # `created_at` changes when the tree has not. Prose cannot fail, so this
        # measures it against the real engine. The day a second volatile field is
        # added, this goes RED -- instead of the skip silently never firing again and
        # the nightly quietly going back to a full upload every night.
        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        (home / "workspace" / "note.md").write_text("hello\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        def build(out: Path) -> Path:
            return _build_real_bundle(out)

        first = build(tmp_path / "one")
        # The bundle name and its root directory are second-granular, so the two
        # builds have to land in different seconds for this to be the real case.
        time.sleep(1.1)
        second = build(tmp_path / "two")

        assert first.read_bytes() != second.read_bytes()
        # The mode is the platform's, not a blanket opt-in. Asserted universally: a
        # helper that always asked for the by-name walk would leave the pinned walk --
        # the one this platform actually runs -- untested, and still satisfy everything
        # else here.
        expected_mode = "pinned" if pinned_fs.supports_pinned_tree_walk() else "unpinned"
        assert _manifest_field(first, "staging") == expected_mode
        assert backup._tree_fingerprint(first, volatile_root=True) == backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_a_real_bundle_whose_tree_moved_does_not_match(self, tmp_path, monkeypatch):
        # The same engine, the same normalizations, one changed file. Without this the
        # test above is satisfied by a fingerprint that ignores the bundle entirely.
        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        (home / "workspace" / "note.md").write_text("hello\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        def build(out: Path) -> Path:
            return _build_real_bundle(out)

        first = build(tmp_path / "one")
        (home / "workspace" / "note.md").write_text("changed\n", encoding="utf-8")
        time.sleep(1.1)
        second = build(tmp_path / "two")

        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            second, volatile_root=True
        )

    def test_the_assumption_holds_where_the_platform_cannot_pin_the_walk(
        self, tmp_path, monkeypatch
    ):
        # Windows cannot open a directory relative to a descriptor, so production
        # refuses the pinned staging walk there and the run has to ask for the by-name
        # one. That is a different walk writing a different `staging` value into a
        # manifest field the fingerprint KEEPS, so the property is measured in that
        # mode rather than carried over from this one. Reporting the platform as
        # unable to pin is what the by-name path is reached by, on any host.
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        (home / "workspace" / "note.md").write_text("hello\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        first = _build_real_bundle(tmp_path / "one")
        time.sleep(1.1)
        second = _build_real_bundle(tmp_path / "two")

        # The precondition, asserted rather than assumed: a simulation that failed to
        # reach the by-name path would still satisfy everything below, because the
        # pinned walk agrees with itself too.
        assert _manifest_field(first, "staging") == "unpinned"
        assert _manifest_field(second, "staging") == "unpinned"

        # Unchanged tree agrees, and a moved one still disagrees -- a mode that made
        # every comparison equal would satisfy the first assertion on its own.
        assert first.read_bytes() != second.read_bytes()
        assert backup._tree_fingerprint(first, volatile_root=True) == backup._tree_fingerprint(
            second, volatile_root=True
        )

        (home / "workspace" / "note.md").write_text("changed\n", encoding="utf-8")
        time.sleep(1.1)
        third = _build_real_bundle(tmp_path / "three")
        assert backup._tree_fingerprint(first, volatile_root=True) != backup._tree_fingerprint(
            third, volatile_root=True
        )


# ---------------------------------------------------------------------------
# The decision -- every branch that must upload
# ---------------------------------------------------------------------------


class TestUnchangedBaseline:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # The baseline probe takes its own authorization before the HEAD, so without a
        # stub the real gate runs a live STS check, raises, and every branch below
        # returns None for the wrong reason -- the accepting case would fail while the
        # refusing cases passed, which is the shape that hides a broken guard.
        self.authz = mock.Mock()
        monkeypatch.setattr(backup, "_authorize_upload", self.authz)
        yield

    def _seed(self, *, tree: str = "t1", key: str = "snapshots/i/a.tar.gz", size: int = 10) -> None:
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, size, "fp", "v1", tree=tree)

    def _ask(self, tree: str = "t1") -> Optional[dict[str, Any]]:
        return backup._unchanged_baseline(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            tree,
            "p",
            "us-west-2",
            "bkt",
            caller=backup.CALLER_SCHEDULED,
        )

    def test_a_proven_present_matching_archive_is_a_baseline(self):
        # The one accepting case. Without it every test below passes on a function
        # that returns None unconditionally.
        self._seed()
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "v1"},
        ):
            assert self._ask() is not None

    def test_no_prior_run_uploads(self):
        # A fresh install has no baseline at all.
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            assert self._ask() is None
        # And it did not spend a round trip finding that out.
        head.assert_not_called()

    def test_a_prior_run_with_no_tree_fingerprint_uploads(self):
        # An install upgraded into this feature has records written before the field
        # existed. Unknown is not a pass -- the same rule this module already applies
        # to an empty body fingerprint -- so it uploads once and gets a baseline.
        self._seed(tree="")
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            assert self._ask(tree="t1") is None
        head.assert_not_called()

    def test_a_run_with_no_computed_fingerprint_uploads(self):
        # The mirror of the above: if this run could not produce a fingerprint, there
        # is nothing to compare and an empty value must never match an empty record.
        self._seed(tree="")
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            assert self._ask(tree="") is None
        head.assert_not_called()

    def test_a_moved_tree_uploads(self):
        self._seed(tree="t1")
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            assert self._ask(tree="t2") is None
        head.assert_not_called()

    def test_a_prior_run_with_no_key_uploads(self):
        self._seed(key="")
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            assert self._ask() is None
        head.assert_not_called()

    def test_a_recorded_archive_that_is_gone_uploads(self):
        # Acceptance: a recorded key whose object is gone must force a full upload.
        # This is not an exotic case -- retention deletes old archives by design, so
        # a baseline ageing out of the keep window is ordinary. Skipping against it
        # would leave the drive holding nothing for this kind while every run
        # reported success.
        self._seed()
        with mock.patch.object(backup.storage, "head_object_meta", return_value=None):
            assert self._ask() is None

    def test_a_head_that_could_not_be_answered_uploads(self):
        # `head_object_meta` raises rather than folding a throttle, a timeout or an
        # owner-pin refusal into "absent". Unproven is not proven, so it uploads.
        self._seed()
        with mock.patch.object(
            backup.storage, "head_object_meta", side_effect=backup.AWSError("throttled")
        ):
            assert self._ask() is None

    def test_an_archive_of_a_different_length_uploads(self):
        # Acceptance: a partial or replaced archive is never the baseline a later run
        # skips against. This drive is reachable by several installs by design -- the
        # same reason `_body_fingerprint` exists -- so an object truncated or
        # overwritten at a recorded key must not count as the archive we wrote.
        self._seed(size=10)
        with mock.patch.object(
            backup.storage, "head_object_meta", return_value={"ContentLength": 4, "VersionId": "v1"}
        ):
            assert self._ask() is None

    def test_a_head_that_reports_no_length_uploads(self):
        # An odd or empty HEAD response proves nothing about the stored bytes.
        self._seed()
        with mock.patch.object(backup.storage, "head_object_meta", return_value={}):
            assert self._ask() is None

    def test_an_archive_of_a_different_version_uploads(self):
        # Identity is the VERSION, not the length. One drive is reachable by several
        # installs by design, so a co-writer can overwrite a recorded key -- and an
        # overwrite that happens to match the recorded byte length would pass a
        # length-only check. The skip would then hold, uploads would stop while the tree
        # was unchanged, and a restore would fetch the foreign current version, fail the
        # fingerprint, and be left depending on `_recover_recorded_version` finding our
        # noncurrent bytes still on the drive -- a narrower guarantee than simply having
        # uploaded. Same length, different version, must upload.
        self._seed(size=10)
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "someone-elses"},
        ):
            assert self._ask() is None

    def test_a_head_that_names_no_version_uploads(self):
        # An unversioned bucket, or a HEAD that named none: nothing shows the object now
        # at that key is the one this install wrote, so the skip is unavailable there.
        self._seed()
        with mock.patch.object(
            backup.storage, "head_object_meta", return_value={"ContentLength": 10}
        ):
            assert self._ask() is None

    def test_a_prior_run_with_no_recorded_version_uploads(self):
        # The mirror: this install recorded no version for the archive, so even a HEAD
        # that names one cannot be compared against anything. Retention already reads a
        # missing version as "do not touch"; this reads it as "cannot prove".
        backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 10, "fp", "", tree="t1"
        )
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "v1"},
        ):
            assert self._ask() is None

    def test_a_prior_run_with_a_null_recorded_version_uploads(self):
        # Versioning suspension reuses "null" for each overwrite, so equality cannot
        # prove the current bytes are the archive this install wrote.
        backup._record_run(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            "snapshots/i/a.tar.gz",
            10,
            "fp",
            "null",
            tree="t1",
        )
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "null"},
        ):
            assert self._ask() is None

    def test_a_null_version_is_classified_as_unprovable(self):
        # The stored side has no behaviour of its own to pin. A stored "null" beside any
        # real recorded version already fails the equality check below, and the only
        # input where null-ness DECIDES the outcome is both sides being "null", which
        # the test above owns. So this pins the classification directly, and asserts the
        # upload alongside it as the sibling that shows equality refuses it too.
        self._seed()
        assert backup._is_provable_version_id("null") is False
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "null"},
        ):
            assert self._ask() is None

    def test_two_empty_versions_do_not_match_each_other(self):
        # The fail-open the emptiness guard exists to close, and the only case that
        # distinguishes it from the equality check below it: with NO recorded version and
        # a HEAD that names an empty one, a bare `stored != recorded` finds "" == "" and
        # skips on two absences. Unknown is not a pass, so emptiness is refused before
        # the comparison is reached.
        backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 10, "fp", "", tree="t1"
        )
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": ""},
        ):
            assert self._ask() is None

    def test_the_baseline_probe_is_authorized_under_its_own_operation(self):
        # The HEAD is a request to a paid service on the operator's account, taken after
        # a build that runs for minutes, so consent can be withdrawn in between. It is
        # gated -- and under its OWN operation name, so an audit reader can tell a
        # metadata probe from a refused archive PUT.
        self._seed()
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "v1"},
        ):
            assert self._ask() is not None
        assert self.authz.call_count == 1
        assert self.authz.call_args.kwargs["operation"] == backup.SEL_OP_BASELINE_PROBE
        assert self.authz.call_args.kwargs["caller"] == backup.CALLER_SCHEDULED
        # The probe carries no kind's payload -- it is a HEAD of an archive already in
        # the bucket, and writes nothing -- so no per-kind grant governs it. Naming a
        # kind here would subject a read to that kind's grant and turn a withdrawn
        # snapshot consent into a refused PROBE, which this module answers by
        # uploading: the nightly would re-upload an unchanged tree every night, the
        # exact cost this baseline exists to avoid. `None` is the real answer the
        # parameter documents, not an opt-out, and the parameter has no default
        # precisely so this site must state it.
        assert self.authz.call_args.kwargs["payload_kind"] is None

    def test_a_refused_probe_uploads_rather_than_skipping(self):
        # Consent withdrawn during the build: the gate raises and the run uploads. It
        # must not fall through to a skip, which would be a decision resting on an
        # authorization the account has revoked.
        self._seed()
        self.authz.side_effect = RuntimeError("consent withdrawn")
        with mock.patch.object(backup.storage, "head_object_meta") as head:
            with pytest.raises(RuntimeError, match="consent withdrawn"):
                self._ask()
        head.assert_not_called()

    def test_a_run_that_cannot_skip_spends_no_probe(self):
        # The gate sits AFTER the local checks, so a run with no baseline, a moved tree
        # or no recorded key does not spend an STS round trip discovering it cannot skip.
        with mock.patch.object(backup.storage, "head_object_meta"):
            assert self._ask() is None
        assert self.authz.call_count == 0

    def test_a_baseline_is_per_kind(self):
        # The snapshot and sessions archives are different trees under different
        # prefixes; one kind's baseline must never satisfy the other's comparison.
        self._seed(tree="t1")
        with mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": 10, "VersionId": "v1"},
        ):
            assert (
                backup._unchanged_baseline(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "t1",
                    "p",
                    "us-west-2",
                    "bkt",
                    caller=backup.CALLER_SCHEDULED,
                )
                is None
            )


# ---------------------------------------------------------------------------
# The locked skip record
# ---------------------------------------------------------------------------


class TestRecordSkipCompareAndSet:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    @staticmethod
    def _seed(key: str, tree: str) -> dict[str, Any]:
        return backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, key, 10, f"fp-{key}", f"v-{key}", tree=tree
        )

    def test_a_matching_baseline_records_the_skip(self):
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert skipped is not None
        assert skipped["uploaded"] is False
        assert skipped["key"] == baseline["key"]
        assert skipped["version"] == baseline["version"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == skipped

    def test_a_moved_baseline_refuses_the_skip(self):
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        newer = self._seed("snapshots/i/new.tar.gz", "new-tree")
        # A stale baseline is refused on the record's own identity. The stamp cannot
        # carry that: two writes inside one clock tick share `at` to the microsecond,
        # which is why a precondition on the stamp is not portable. The key is asserted
        # instead, because it differs whenever a real upload has landed.
        assert newer["key"] != baseline["key"]

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert skipped is None, "a stale baseline must refuse the skip"

    def test_a_moved_baseline_sharing_one_clock_tick_refuses_the_skip(self):
        # A coarse clock gives two successive writes the same `at` value, so the stamp
        # comparison passes and the refusal cannot rest on it. Pinning the stamps equal
        # by hand reproduces on every platform what Windows produces on its own. The
        # record's `(process, sequence)` pair still differs here, and so does the key,
        # so either term refuses: what this pins is that an equal stamp alone is never
        # accepted as proof.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        newer = self._seed("snapshots/i/new.tar.gz", "new-tree")
        collided = json.loads(backup._state_path().read_text(encoding="utf-8"))
        for entry in collided.get("accounts", {}).values():
            for run in entry.get("runs", {}).values():
                run["at"] = baseline["at"]
        backup._state_path().write_text(json.dumps(collided), encoding="utf-8")
        current = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert current["at"] == baseline["at"], "the tick collision must be in place"
        assert current["key"] == newer["key"], "the slot must still hold the newer run"

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert skipped is None, "an equal stamp is not proof the slot is still the baseline's"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == newer["key"]

    def test_a_second_skip_against_the_same_baseline_refuses(self):
        # Two runs can both find the tree unchanged. The first skip carries the
        # baseline's own key, so the slot's key still matches the stale baseline, and
        # both writes can land inside one clock tick and then carry the same `at`. So
        # neither of those terms can see this, and only `(process, sequence)` can --
        # which is what makes this reachable through two REAL skips on every platform
        # rather than by editing the slot by hand.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        first = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")
        assert first is not None
        assert first["key"] == baseline["key"], "the key must be the term that cannot help"
        assert first["sequence"] != baseline["sequence"], "the sequence must be the one that can"

        second = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert second is None, "a baseline already consumed by a skip must refuse a second"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == first

    def test_a_second_skip_sharing_one_clock_tick_refuses(self):
        # The Windows condition exactly: a first skip has already replaced the slot, it
        # copied the baseline's key, and its fresh stamp landed inside the same clock
        # tick so `at` collides too. Every term except the sequence therefore matches,
        # which makes this the one state that isolates it. The collision is pinned by
        # hand because a fine clock will not produce it.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        first = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")
        assert first is not None
        collided = json.loads(backup._state_path().read_text(encoding="utf-8"))
        for entry in collided.get("accounts", {}).values():
            for run in entry.get("runs", {}).values():
                run["at"] = baseline["at"]
        backup._state_path().write_text(json.dumps(collided), encoding="utf-8")
        current = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert current["at"] == baseline["at"], "the tick collision must be in place"
        assert current["key"] == baseline["key"], "the key must also match"
        assert current["process"] == baseline["process"], "the process must also match"
        assert current["sequence"] != baseline["sequence"], "only the sequence may differ"

        second = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert second is None, "only the sequence can refuse this, and it must"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == current

    def test_a_matching_sequence_from_another_process_refuses_the_skip(self):
        # `sequence` counts one process's own writes, so two processes can both sit at
        # the same number -- the manual-run-racing-the-nightly-loop case `_stamp`'s own
        # docstring names. Pairing it with `process` is what makes it an identity, and
        # it is the pairing `_run_is_newer` already requires for the same reason.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        foreign = json.loads(backup._state_path().read_text(encoding="utf-8"))
        for entry in foreign.get("accounts", {}).values():
            for run in entry.get("runs", {}).values():
                run["process"] = "another-host:99999"
        backup._state_path().write_text(json.dumps(foreign), encoding="utf-8")
        current = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert current["sequence"] == baseline["sequence"], "the sequence must collide"
        assert current["at"] == baseline["at"], "every other term must still match"
        assert current["key"] == baseline["key"], "every other term must still match"
        assert current["process"] != baseline["process"], "the process must be the difference"

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert skipped is None, "a sequence from another process is not proof of identity"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == current

    def test_a_record_without_a_sequence_refuses_the_skip(self):
        # A record that carries a process but no sequence cannot prove the slot is still
        # the one the baseline came from: an absent sequence equals an absent sequence,
        # so comparing them would read that as identity. The skip is refused and the
        # caller uploads a full copy, which is the direction every other proof here
        # fails toward, and it is why there is no fallback to comparing `at` alone.
        # A record predating BOTH fields is refused by the process guard instead.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        legacy = json.loads(backup._state_path().read_text(encoding="utf-8"))
        for entry in legacy.get("accounts", {}).values():
            for run in entry.get("runs", {}).values():
                run.pop("sequence", None)
        backup._state_path().write_text(json.dumps(legacy), encoding="utf-8")
        stripped = dict(baseline)
        stripped.pop("sequence", None)
        current = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert "sequence" not in current, "the legacy shape must be in place"
        assert current["process"] == baseline["process"], "the process must still match"
        assert current["at"] == baseline["at"], "every other term must still match"
        assert current["key"] == baseline["key"], "every other term must still match"

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, stripped, "same-tree")

        assert skipped is None, "a record carrying no sequence cannot prove identity"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == current

    def test_a_refused_skip_leaves_the_newer_record_in_place(self):
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        newer = self._seed("snapshots/i/new.tar.gz", "new-tree")
        assert newer["key"] != baseline["key"]

        backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        current = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert current == newer, "a refused skip must not replace the newer run record"

    def test_an_empty_process_on_both_sides_refuses_the_skip(self):
        # An empty string equals itself, so a bare `current == expected` would read a
        # state document carrying no process as proof the slot is still the baseline's.
        # That is the same shape as a version id of "null": a sentinel that compares
        # equal to a sentinel proves nothing about identity. The sequence alone cannot
        # carry the refusal here, because it counts one process's writes and there is
        # no process left to scope it to.
        baseline = self._seed("snapshots/i/old.tar.gz", "same-tree")
        blanked = json.loads(backup._state_path().read_text(encoding="utf-8"))
        for entry in blanked.get("accounts", {}).values():
            for run in entry.get("runs", {}).values():
                run["process"] = ""
        backup._state_path().write_text(json.dumps(blanked), encoding="utf-8")
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["process"] == ""
        baseline["process"] = ""

        skipped = backup._record_skip(ACCOUNT, backup.KIND_SNAPSHOT, baseline, "same-tree")

        assert skipped is None, "an empty process is not proof the slot is still the baseline's"


# ---------------------------------------------------------------------------
# The run paths, both directions
# ---------------------------------------------------------------------------


def _fake_snapshot(body: bytes):
    """A snapshot engine that packs *body*, in a fresh timestamped bundle root."""
    counter = {"n": 0}

    def build(argv):
        counter["n"] += 1
        out = Path(argv[0])
        root = f"kirocrew-snapshot-2026010{counter['n']}T000000Z"
        _pack(out / f"{root}.tar.gz", {f"{root}/a.txt": body})
        return 0

    return build


class TestRunSnapshotBackupSkip:

    @pytest.fixture(autouse=True)
    def _snapshot_payload_can_be_held(self, monkeypatch):
        """These tests are about the snapshot LOGIC, not the platform gate.

        ``run_snapshot_backup`` refuses outright where the staging leaf has no
        sandbox mask, because the payload is produced by another module and cannot be
        held from creation there. That refusal has its own tests. Everything in this
        class is about what the snapshot path DOES once it runs -- retention, skips,
        fingerprints, records -- so it asserts the capability rather than inheriting
        whichever platform the suite happens to run on. Without this the same tests
        would measure behaviour on POSIX and measure the refusal on Windows.
        """
        monkeypatch.setattr(backup.storage, "body_bytes_can_be_held_from_creation", lambda: True)

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
        yield

    def _seed_first_run(self, body: bytes = b"hello") -> dict[str, Any]:
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(body)),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", return_value="v1"),
        ):
            return backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

    def _head_matching(self, first: dict[str, Any]):
        return mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": first["bytes"], "VersionId": first["version"]},
        )

    def test_an_unchanged_tree_uploads_nothing_and_says_so(self):
        # The acceptance criterion, end to end: nothing is pushed, nothing is even
        # authorized, and the run record states that it sent no bytes rather than
        # looking like an ordinary successful backup.
        first = self._seed_first_run()
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(b"hello")),
            mock.patch.object(backup, "_authorize_upload") as authz,
            mock.patch.object(backup.storage, "put_file") as put_file,
            self._head_matching(first),
        ):
            second = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

        put_file.assert_not_called()
        # Not "no authorization at all": the skip authorizes its one metadata probe. What
        # it must never present is an UPLOAD decision, because no bytes leave.
        assert authz.call_count == 1
        assert authz.call_args.kwargs["operation"] == backup.SEL_OP_BASELINE_PROBE
        assert second["uploaded"] is False
        assert first["uploaded"] is True
        # The baseline survives the skip. Without this the next run has a record
        # pointing at nothing and can never skip again -- and retention loses the
        # version it needs to retire the key.
        assert second["key"] == first["key"]
        assert second["version"] == first["version"]
        assert second["tree"] == first["tree"]

    def test_a_moved_baseline_falls_through_to_an_upload(self, caplog):
        first = self._seed_first_run()
        original_baseline = backup._unchanged_baseline

        def move_after_probe(*args, **kwargs):
            baseline = original_baseline(*args, **kwargs)
            assert baseline is not None
            newer = backup._record_run(
                ACCOUNT,
                backup.KIND_SNAPSHOT,
                "snapshots/other/new.tar.gz",
                12,
                "new-fingerprint",
                "new-version",
                tree="new-tree",
            )
            assert newer["key"] != baseline["key"]
            return baseline

        caplog.set_level("INFO", logger=backup.__name__)
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(b"hello")),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", return_value="v2") as put_file,
            mock.patch.object(backup, "_unchanged_baseline", side_effect=move_after_probe),
            mock.patch.object(backup, "_publish_label"),
            mock.patch.object(backup, "_prune_remote_archives"),
            self._head_matching(first),
        ):
            second = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

        assert second["uploaded"] is True, "a moved baseline must fall through to an upload"
        archive_pushes = [
            call
            for call in put_file.call_args_list
            if not call.args[4].endswith(backup.LABEL_OBJECT_NAME)
        ]
        assert len(archive_pushes) == 1
        assert "recorded baseline moved while the archive was being built" in caplog.text

    def test_a_changed_tree_still_uploads(self):
        # The conditional half. A guard that refused unconditionally would pass every
        # test above while having turned the backup off.
        self._seed_first_run(b"hello")
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(b"different")),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", return_value="v2") as put_file,
            mock.patch.object(
                backup.storage,
                "head_object_meta",
                return_value={"ContentLength": 10, "VersionId": "v1"},
            ),
        ):
            second = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

        assert second["uploaded"] is True
        archive_pushes = [
            call
            for call in put_file.call_args_list
            if not call.args[4].endswith(backup.LABEL_OBJECT_NAME)
        ]
        assert len(archive_pushes) == 1

    def test_a_skip_leaves_the_nightly_not_due(self):
        # A skip takes a FRESH stamp on purpose. `due_for_nightly` reads `at`, so a
        # skip that kept the old stamp would read as due on the very next wake and
        # rebuild the archive every few minutes for as long as the tree stayed
        # unchanged -- turning the saving into a busy loop.
        first = self._seed_first_run()
        backup.set_nightly(ACCOUNT, True)
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(b"hello")),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file") as put_file,
            self._head_matching(first),
        ):
            backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )
        put_file.assert_not_called()
        assert backup.due_for_nightly(ACCOUNT) is False

    def test_a_skip_runs_no_retention_sweep(self):
        # Retention retires copies by COUNT after a push. Running it on a run that
        # pushed nothing would let a stretch of unchanged nights walk the keep window
        # down and eventually delete the very archive the next skip has to prove is
        # present.
        first = self._seed_first_run()
        with (
            mock.patch.object(backup, "snapshot_main", side_effect=_fake_snapshot(b"hello")),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file"),
            mock.patch.object(backup, "_prune_remote_archives") as prune,
            mock.patch.object(backup, "_publish_label") as label,
            self._head_matching(first),
        ):
            backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )
        prune.assert_not_called()
        label.assert_not_called()

    def test_the_first_run_records_a_tree_fingerprint(self):
        # The baseline has to be written by the upload path or nothing can ever skip.
        record = self._seed_first_run()
        assert record["tree"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["tree"] == record["tree"]


class TestRunSessionsBackupSkip:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
        crew = tmp_path / "crew_home" / "sessions"
        cli = tmp_path / "cli_sessions"
        crew.mkdir(parents=True)
        cli.mkdir(parents=True)
        (crew / "t.jsonl").write_text("{}\n", encoding="utf-8")
        (cli / "replay.log").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        # Isolate the kiro-cli conversation export: this suite compares
        # the archive's fingerprint across runs, so it must not depend on whatever
        # live terminal store the test host happens to have.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))
        self.crew = crew
        yield

    def _run(self, extra=()):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(backup, "_authorize_upload"))
            put_file = stack.enter_context(
                mock.patch.object(backup.storage, "put_file", return_value="v1")
            )
            for ctx in extra:
                stack.enter_context(ctx)
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )
        return record, put_file

    @pytest.mark.skipif(
        not backup._CAN_PIN_TRAVERSAL,
        reason="the sessions path refuses outright without descriptor-pinned traversal",
    )
    def test_unchanged_session_trees_upload_nothing_then_a_change_uploads(self):
        # Both directions in one test because the second half's setup IS the first
        # half's outcome: the skip must hold while the trees are untouched, and must
        # stop holding the moment one file changes.
        first, _ = self._run()
        assert first["uploaded"] is True

        def head():
            return mock.patch.object(
                backup.storage,
                "head_object_meta",
                return_value={"ContentLength": first["bytes"], "VersionId": first["version"]},
            )

        second, put_file = self._run(extra=(head(),))
        assert second["uploaded"] is False
        put_file.assert_not_called()

        self.crew.joinpath("t.jsonl").write_text('{"new": true}\n', encoding="utf-8")
        third, put_file = self._run(extra=(head(),))
        assert third["uploaded"] is True
        put_file.assert_called()

    @pytest.mark.skipif(
        not backup._CAN_PIN_TRAVERSAL,
        reason="the sessions path refuses outright without descriptor-pinned traversal",
    )
    def test_a_moved_baseline_falls_through_to_an_upload(self):
        first, _ = self._run()
        original_baseline = backup._unchanged_baseline

        def move_after_probe(*args, **kwargs):
            baseline = original_baseline(*args, **kwargs)
            assert baseline is not None
            newer = backup._record_run(
                ACCOUNT,
                backup.KIND_SESSIONS,
                "sessions/other/new.tar.gz",
                12,
                "new-fingerprint",
                "new-version",
                tree="new-tree",
            )
            assert newer["key"] != baseline["key"]
            return baseline

        head = mock.patch.object(
            backup.storage,
            "head_object_meta",
            return_value={"ContentLength": first["bytes"], "VersionId": first["version"]},
        )
        moved = mock.patch.object(backup, "_unchanged_baseline", side_effect=move_after_probe)

        second, put_file = self._run(extra=(head, moved))

        assert second["uploaded"] is True, "a moved baseline must fall through to an upload"
        archive_pushes = [
            call
            for call in put_file.call_args_list
            if not call.args[4].endswith(backup.LABEL_OBJECT_NAME)
        ]
        assert len(archive_pushes) == 1


# ---------------------------------------------------------------------------
# Composition with retention -- the two halves have to move together
# ---------------------------------------------------------------------------


class _Drive:
    """A fake versioned drive that GROWS when an upload lands.

    The growth is the point. Retention's protection of the newest archive is only
    testable if the key the run just uploaded is actually in the listing retention
    reads, which is what it is in production. A fake that never added the new key
    would leave the candidate set free of it and the test would pass without the
    guarantee existing.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.deleted: list[tuple[str, str]] = []
        self.put_keys: list[str] = []
        # Recorded length per key, so the presence proof compares a stored size against
        # the size the run recorded rather than passing on a shared default.
        self.sizes: dict[str, int] = {}
        # Each upload gets a DISTINCT, increasing stamp. Retention orders candidates
        # newest-first and slices, so a fake handing every archive one timestamp leaves
        # the order tied and the slice arbitrary -- which silently produces "retention
        # deleted nothing" and passes assertions that were meant to observe a delete.
        self._clock = 0
        # Set to force the NEXT upload's stamp, for the case where the archive a run
        # just wrote does not sort newest.
        self.next_modified: Optional[str] = None

    @staticmethod
    def row(key: str, modified: str) -> dict[str, Any]:
        return {
            "key": key,
            "versionId": f"v-{key}",
            "modified": modified,
            "size": 10,
            "latest": True,
            "deleteMarker": False,
        }

    def put_file(self, profile, region, bucket, section, key, local_path, **kw):
        if key.endswith(backup.LABEL_OBJECT_NAME):
            return "v-label"
        self._clock += 1
        if self.next_modified is not None:
            modified, self.next_modified = self.next_modified, None
        else:
            modified = f"2026-02-{self._clock:02d}T00:00:00Z"
        self.put_keys.append(key)
        self.rows.append(self.row(key, modified))
        return f"v-{key}"

    def list_object_versions(self, profile, region, bucket, section, subpath, *, account):
        return [r for r in self.rows if str(r["key"]).startswith(f"{subpath}/")]

    def delete_object_versions(self, profile, region, bucket, section, versions, *, account):
        self.deleted.extend(versions)
        present = {key for key, _ in versions}
        self.rows = [r for r in self.rows if str(r["key"]) not in present]
        return len(versions)

    def head_object_meta(self, profile, region, bucket, section, key, *, account):
        for row in self.rows:
            if str(row["key"]) == key:
                return {
                    "ContentLength": self.sizes.get(key, 10),
                    "VersionId": str(row["versionId"]),
                }
        return None

    @property
    def deleted_keys(self) -> list[str]:
        return [key for key, _ in self.deleted]


INSTALL = "c" * 32


class TestRetentionComposition:
    """Retention retires copies; the unchanged-check skips sending one. Together they
    must not leave the run record pointing at an object that is gone.

    The composition matters in one direction specifically: the next skip proves its
    baseline by HEADing the key the last run RECORDED, and retention is the thing in
    this module that deletes keys. So the question is whether retention can ever
    delete the recorded key, and whether a run of skips can walk the keep window
    down to it.
    """

    @pytest.fixture(autouse=True)
    def _snapshot_payload_can_be_held(self, monkeypatch):
        """These tests are about the snapshot LOGIC, not the platform gate.

        ``run_snapshot_backup`` refuses outright where the staging leaf has no
        sandbox mask, because the payload is produced by another module and cannot be
        held from creation there. That refusal has its own tests. Everything in this
        class is about what the snapshot path DOES once it runs -- retention, skips,
        fingerprints, records -- so it asserts the capability rather than inheriting
        whichever platform the suite happens to run on. Without this the same tests
        would measure behaviour on POSIX and measure the refusal on Windows.
        """
        monkeypatch.setattr(backup.storage, "body_bytes_can_be_held_from_creation", lambda: True)

    @pytest.fixture(autouse=True)
    def _wiring(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup, "install_identity", lambda: {"id": INSTALL, "label": "box"})
        self.drive = _Drive([])
        monkeypatch.setattr(backup.storage, "put_file", self.drive.put_file)
        monkeypatch.setattr(backup.storage, "list_object_versions", self.drive.list_object_versions)
        monkeypatch.setattr(
            backup.storage, "delete_object_versions", self.drive.delete_object_versions
        )
        monkeypatch.setattr(backup.storage, "head_object_meta", self.drive.head_object_meta)
        self.authz = mock.Mock()
        monkeypatch.setattr(backup, "_authorize_upload", self.authz)
        monkeypatch.setattr(backup, "_publish_label", lambda *a, **k: None)
        self.snapshot_body = b"hello"
        monkeypatch.setattr(backup, "snapshot_main", lambda argv: self._build(argv))
        monkeypatch.setattr(backup.snapshot, "prepare_redacted_copy", lambda *a, **k: None)
        self._builds = 0
        yield

    def _build(self, argv) -> int:
        self._builds += 1
        out = Path(argv[0])
        root = f"kirocrew-snapshot-2026010{self._builds}T000000Z"
        _pack(out / f"{root}.tar.gz", {f"{root}/a.txt": self.snapshot_body})
        return 0

    def _run(self) -> dict[str, Any]:
        record = backup.run_snapshot_backup(
            ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
        )
        # The fake drive reports the length it stored; keep the recorded size in step
        # so the presence proof compares like with like rather than passing by luck.
        self.drive.sizes[str(record["key"])] = int(record["bytes"])
        return record

    def _seed_uploads(self, n: int) -> list[dict[str, Any]]:
        """``n`` real uploads, each with different content so none of them skips.

        Deliberately not hand-written listing rows. Retention only retires an archive
        whose recorded version id is still the key's CURRENT version
        (``_current_version_is_ours``), so a fabricated row this install never recorded
        uploading is not a candidate at all -- a sweep over seeded rows deletes nothing
        and every assertion below would pass while proving nothing.
        """
        records = []
        for i in range(n):
            self.snapshot_body = f"body-{i}".encode()
            records.append(self._run())
        return records

    def test_retention_never_deletes_the_archive_the_next_skip_proves(self):
        # The composition, asserted rather than reasoned. Retention is at its most
        # aggressive setting (keep 1) and has real candidates it recorded uploading, so
        # it deletes as much as it is ever allowed to. The key the LAST run recorded
        # must survive that, because it is exactly what the next run HEADs to decide
        # whether it may skip.
        backup.set_retention_keep(ACCOUNT, 1)
        records = self._seed_uploads(3)
        newest = records[-1]

        assert self.drive.deleted_keys, "retention deleted nothing, so this proves nothing"
        assert newest["key"] not in self.drive.deleted_keys
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == newest["key"]
        assert (
            self.drive.head_object_meta("p", "r", "b", "backup", newest["key"], account=ACCOUNT)
            is not None
        )

        # So an unchanged run now skips, against an archive retention has already had
        # every chance to remove.
        self.snapshot_body = b"body-2"
        after = self._run()
        assert after["uploaded"] is False
        assert after["key"] == newest["key"]

    def test_retention_spares_the_recorded_key_even_when_it_is_not_the_newest(self):
        # Retention drops the run's own key by NAME, separately from the count, and the
        # skip leans on that being the stronger of the two guarantees. A co-writer with
        # a skewed clock, or a future-dated object, can leave the archive a run just
        # wrote sorting OLDEST rather than newest -- so it lands inside the slice the
        # count would delete. If the protection came only from "keep the newest N", the
        # recorded key would be deletable here and the next skip would be proving a
        # deleted object.
        backup.set_retention_keep(ACCOUNT, 1)
        first = self._seed_uploads(1)[0]

        # The next archive stores with an ancient stamp, so it sorts last.
        self.drive.next_modified = "2000-01-01T00:00:00Z"
        self.snapshot_body = b"second"
        second = self._run()

        order = [str(r["key"]) for r in self.drive.rows]
        assert second["key"] in order, "the run's archive must be in the listing"
        # The count alone would have retired it: it is the oldest of the two and keep is
        # 1. It survives because the sweep drops it by name first.
        assert second["key"] not in self.drive.deleted_keys
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == second["key"]
        assert (
            self.drive.head_object_meta("p", "r", "b", "backup", second["key"], account=ACCOUNT)
            is not None
        )
        # And the skip that depends on it still works.
        self.snapshot_body = b"second"
        assert self._run()["uploaded"] is False
        # Nothing was retired in this arrangement: the by-name drop spared the run's own
        # archive and the count kept the other one. So one more ordinary upload is the
        # control proving the sweep in this fixture is live rather than inert -- it does
        # retire, and it still spares the key the new record points at.
        self.snapshot_body = b"third"
        third = self._run()
        assert third["uploaded"] is True
        assert self.drive.deleted_keys, "the sweep never deleted, so the spare proves nothing"
        assert third["key"] not in self.drive.deleted_keys
        assert first["key"] in self.drive.deleted_keys

    def test_a_skip_does_not_consume_a_retention_slot(self):
        # A skip adds no archive, so the keep window must not move under it. Four
        # consecutive unchanged runs: the drive's contents are identical before and
        # after, nothing new is deleted and nothing new is uploaded -- so a long stretch
        # of unchanged nights cannot walk the window down to the baseline the last one
        # still depends on.
        backup.set_retention_keep(ACCOUNT, 2)
        records = self._seed_uploads(2)

        rows_before = sorted(str(r["key"]) for r in self.drive.rows)
        deletes_before = list(self.drive.deleted_keys)
        puts_before = list(self.drive.put_keys)

        self.snapshot_body = b"body-1"
        for _ in range(4):
            assert self._run()["uploaded"] is False

        assert sorted(str(r["key"]) for r in self.drive.rows) == rows_before
        assert self.drive.deleted_keys == deletes_before
        assert self.drive.put_keys == puts_before
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == records[-1]["key"]

    def test_a_skip_spends_only_the_baseline_probe(self):
        # A skip is not a push, so it must reach none of the push-path gates: retention
        # takes its own authorization and so does each label PUT. What it does spend is
        # exactly ONE gate -- the baseline probe that authorizes the HEAD -- so this
        # pins the count AND which operation it names, rather than just "fewer".
        backup.set_retention_keep(ACCOUNT, 1)
        self._seed_uploads(1)
        assert self.authz.call_count > 0, "the upload path must authorize, or this proves nothing"

        self.authz.reset_mock()
        self.snapshot_body = b"body-0"
        assert self._run()["uploaded"] is False
        assert self.authz.call_count == 1
        assert self.authz.call_args.kwargs["operation"] == backup.SEL_OP_BASELINE_PROBE
        ops = [c.kwargs.get("operation") for c in self.authz.call_args_list]
        assert backup.SEL_OP_UPLOAD not in ops
        assert backup.SEL_OP_RETENTION not in ops

    def test_a_deleted_baseline_forces_a_full_upload_rather_than_a_skip(self):
        # The inverse, tied to retention's own delete shape rather than a synthetic
        # absent object: once the recorded archive is gone from the drive, an unchanged
        # tree must upload. This is the branch that stops a drive holding nothing for a
        # kind while every run reports success.
        backup.set_retention_keep(ACCOUNT, 1)
        first = self._seed_uploads(1)[0]

        self.drive.delete_object_versions(
            "p", "r", "b", "backup", [(first["key"], f"v-{first['key']}")], account=ACCOUNT
        )
        assert (
            self.drive.head_object_meta("p", "r", "b", "backup", first["key"], account=ACCOUNT)
            is None
        )

        self.snapshot_body = b"body-0"
        second = self._run()
        assert second["uploaded"] is True
        assert second["key"] != first["key"]

    def test_a_changed_tree_after_a_skip_uploads_and_sweeps_again(self):
        # The full cycle, so none of the above is satisfied by a run path that stopped
        # working after its first skip: upload, skip, then a real change uploads again
        # and retention resumes -- with the NEW recorded key spared and the old one
        # retired.
        backup.set_retention_keep(ACCOUNT, 1)
        first = self._seed_uploads(1)[0]
        self.snapshot_body = b"body-0"
        assert self._run()["uploaded"] is False

        self.snapshot_body = b"moved on"
        third = self._run()
        assert third["uploaded"] is True
        assert third["key"] != first["key"]
        assert third["key"] not in self.drive.deleted_keys
        assert first["key"] in self.drive.deleted_keys
        assert (
            self.drive.head_object_meta("p", "r", "b", "backup", third["key"], account=ACCOUNT)
            is not None
        )


# ---------------------------------------------------------------------------
# The docstring stops describing a state the module has left
# ---------------------------------------------------------------------------


class TestModuleDocstringIsCurrent:
    def test_the_future_work_sentence_is_gone(self):
        # The module's own docstring is a routing surface: a sentence naming this gap as
        # future work sends the next reader hunting for a check that is right there in
        # the file. So the docstring must not carry that sentence.
        assert backup.__doc__ is not None
        assert "is future\nwork" not in backup.__doc__
        assert "integration with the storage inventory is future" not in backup.__doc__

    def test_the_docstring_states_the_new_behaviour(self):
        # And says what it does instead, so the replacement is not merely a deletion.
        assert "Unchanged runs upload nothing" in (backup.__doc__ or "")
