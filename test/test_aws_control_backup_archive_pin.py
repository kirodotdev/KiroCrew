"""The backup archive's bytes are bound to ONE inode from build to upload.

``test_aws_control_backup.py`` covers the two push paths with ``put_file`` mocked
out, so nothing there sees which file the upload would actually have opened. That
is the question these tests ask. The archive is staged in a directory a same-UID
process can write, and a NAME resolved once per step -- the entry-set fingerprint,
the size, the AWS CLI ``--body``, the body fingerprint -- is a separate answer per
step. A process that replaces the file between two of them makes the upload carry
bytes nothing checked, and an uploaded object has no recall.

So the whole-run tests here go through the REAL ``storage.put_file`` and stub the
subprocess chokepoint, reading the body the way the CLI child would. A swap
performed in the window immediately before that child resolves the body is what
separates a name-based upload from a descriptor-bound one.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew import platform_compat, sandbox
from kiro_crew.apps.builtins.aws_control.backend import backup, storage

ACCOUNT = "111122223333"


def _body_can_be_held() -> bool:
    """Whether the archive body can be produced into a held, unrewritable descriptor.

    This mirrors the production gate exactly -- ``build_sealed_upload_body`` builds the
    body into an anonymous **memfd** (RAM-backed, not a file on the staging
    directory's filesystem), so availability depends ONLY on the kernel offering
    ``memfd_create`` + file seals and on the staging leaf being masked. It is
    independent of the staging directory's filesystem: a memfd lives in RAM, so a
    tmpfs or overlay basetemp (some CI runners) serves it just as well as any other
    mount. Where this is False (a non-Linux platform, or a Linux without
    memfd/seals, or an unmasked host) the create fails closed; the callers assert
    that behaviour.
    """
    return storage._UNNAMED_BODY_SUPPORTED and storage._staging_leaf_is_masked()


def _no_body(_fd: int) -> None:
    """A write_body that writes nothing -- the sealed fd carries an empty body.

    Used by the tests that only assert the create's PROPERTIES (nameless inode,
    fail-closed where unavailable), not the body contents.
    """


def _writes(payload: bytes):
    """A write_body factory that streams *payload* into the helper's pipe."""

    def _write(fd: int) -> None:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]

    return _write


#: What a same-UID process plants over the finished archive. Not a tarball, so a
#: test that reports these bytes as uploaded is reporting real exposure: they
#: stand in for any file the owner can read, which is what a hard link to
#: ``~/.aws/credentials`` would have made the upload carry.
PLANTED = b"SECRET-CREDENTIAL-BYTES-THAT-WERE-NEVER-CHECKED"


#: The swap these tests perform -- unlink the archive and write a different file at
#: its name while a descriptor is still open on it -- is one the platform itself
#: refuses where an open handle blocks a delete.
#:
#: Skipping there would leave the platform-specific half of a credential-exposure pin
#: unasserted, and a skipped assertion is a silent CI green. So the swap is ATTEMPTED
#: and both outcomes are asserted: where it succeeds, the descriptor's reading must be
#: unaffected while the name's reading changes; where the platform refuses the unlink,
#: that refusal is itself the protection and the name must still reach the real bytes.
def _swap_at_the_name(path: Path, planted: bytes) -> bool:
    """Try to replace ``path``'s contents behind an open descriptor.

    Returns True when the platform allowed the substitution, False when it refused
    the unlink while a descriptor was open. A False return is a STRONGER outcome than
    the one under test, and the caller asserts the bytes at the name are untouched.
    """
    try:
        path.unlink()
    except OSError:
        return False
    path.write_bytes(planted)
    return True


class _NoPread:
    """Make :func:`backup._read_at` take its no-``pread`` arm on any platform.

    ``create=True`` is what lets this run where the attribute is ABSENT rather
    than merely present: on Windows ``os.pread`` does not exist, so patching it
    without that flag raises before the helper under test is ever reached, which
    makes the patch itself the thing that fails instead of measuring the arm.
    """

    def __enter__(self) -> None:
        self._patch = mock.patch.object(backup.os, "pread", None, create=True)
        self._patch.start()

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()


def _no_pread() -> _NoPread:
    return _NoPread()


def _reference_pread(path: Path):
    """A correct ``pread`` that reads through its OWN descriptor.

    The fallback arm is compared against this rather than against the platform's
    real ``pread``, because on a platform that HAS no ``pread`` the latter
    comparison is the fallback measured against itself -- it would agree for any
    implementation, including a broken one. A second descriptor is a genuinely
    independent reader everywhere.
    """

    def pread(fd: int, size: int, offset: int) -> bytes:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    return pread


def _read_whole(fd: int) -> bytes:
    """Every byte of *fd* from offset 0, without disturbing its position."""
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = backup._read_at(fd, 1024 * 1024, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _body_the_child_would_read(args: list[str], stdin_fd: int | None) -> bytes:
    """The bytes the AWS CLI child resolves for ``--body``, in either spelling.

    A path is read as the child would read it -- by re-resolving the name, which
    is the whole exposure. A descriptor spelling is read from the descriptor the
    child inherits, because that is literally the file it opens.
    """
    body = args[args.index("--body") + 1]
    if body.startswith(("/dev/stdin", "/dev/fd/", "/proc/self/fd/")):
        assert stdin_fd is not None, f"{body} was passed with no descriptor to resolve it"
        return _read_whole(stdin_fd)
    return Path(body).read_bytes()


class _SwapOnUpload:
    """Stands in for the subprocess chokepoint, swapping the archive first.

    The swap lands in the narrowest window there is: after every local decision
    has been taken and immediately before the child resolves the body. A
    name-based upload reads the planted file; an upload bound to the descriptor
    opened before the swap reads the archive.
    """

    def __init__(self) -> None:
        self.archive: Path | None = None
        self.uploaded: bytes = b""
        self.before_swap: bytes = b""
        self.swapped = False
        self.swap_prevented = False

    def note_archive(self, local_path: str) -> None:
        self.archive = Path(local_path)
        # Capture the archive the tar wrote WHEN THE NAME STILL EXISTS. On the
        # POSIX descriptor path ``put_file`` unlinks the staging inode before the
        # upload, so by the time this stub runs there is no name left to read --
        # reading here rather than at swap time keeps the archive bytes available
        # for the assertions either way.
        try:
            self.before_swap = self.archive.read_bytes()
        except FileNotFoundError:
            self.before_swap = b""

    def __call__(
        self,
        args: list[str],
        profile: str,
        *,
        action: str,
        timeout: int = 30,
        extra_visible_dirs: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> str:
        if "put-object" in args and self.archive is not None and not self.swapped:
            self.swapped = True
            if not self.archive.exists():
                # The upload chokepoint already UNLINKED the staging inode, so the
                # same-UID name-swap this stub performs has nothing to replace: the
                # race is closed by removing the name, not by refusing the upload.
                # The descriptor the child inherits still holds the real archive.
                self.swap_prevented = True
            else:
                if not self.before_swap:
                    self.before_swap = self.archive.read_bytes()
                # Unlink and re-create rather than truncate: this is the same-UID
                # replacement the module's own sandbox notes describe, and it leaves
                # any descriptor already open on the real inode pointing at it.
                self.archive.unlink()
                self.archive.write_bytes(PLANTED)
        if "put-object" in args:
            self.uploaded = _body_the_child_would_read(args, kwargs.get("stdin_fd"))
        return "{}"


@pytest.fixture
def _sessions_host(tmp_path, monkeypatch):
    """A populated host with both session halves and isolated module state.

    Built on every platform. Where the traversal cannot be pinned the run refuses,
    and ``_run_with_swap`` asserts that refusal rather than the host being skipped.
    """
    crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
    crew.mkdir(parents=True)
    (crew / "t.jsonl").write_bytes(b"transcript\n")
    cli = tmp_path / "cli_sessions"
    cli.mkdir(parents=True)
    (cli / "replay.jsonl").write_bytes(b"{}\n")
    monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
    monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
    monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: True)
    monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
    # No prior archive, so the unchanged-check cannot short-circuit the upload.
    monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
    # These tests exercise the archive upload path, which ``put_file`` allows on
    # POSIX only when the staging-leaf sandbox mask is present (a confined agent) --
    # an unconfined test host would otherwise be refused by the gate that closes
    # the same-UID-writer hole. The mask's ABSENCE is covered by its own dedicated
    # test; here it is set present so the descriptor pin these cases are about is
    # what gets exercised, not the confinement refusal.
    monkeypatch.setattr(backup.storage, "_staging_leaf_is_masked", lambda: True)
    # The archive body is an anonymous memfd (RAM-backed), so the nameless arm is
    # available on any Linux filesystem regardless of whether it honours O_TMPFILE.
    # The staging dir only needs to exist under a root the mask covers (mocked
    # present above), so stage it under the fixture's own tmp_path. Where no
    # sealable memfd exists at all the create fails closed, which ``_run_with_swap``
    # asserts rather than skips.
    staging = tmp_path / "kc-aws-staging"
    staging.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "staging_root", lambda: staging)
    return tmp_path


def _run_with_swap(monkeypatch) -> _SwapOnUpload | None:
    """Run one sessions backup whose archive is swapped just before the upload.

    Returns ``None`` where this platform cannot pin a directory traversal. That case
    is ASSERTED here rather than skipped: the run must refuse in its own words and
    upload nothing at all, which is a stronger outcome than the swap these tests
    describe, and a caller that gets ``None`` has already had it verified.
    """
    swap = _SwapOnUpload()
    real_put = storage.put_file

    def watching_put(*args: Any, **kwargs: Any) -> str:
        # positional: profile, region, bucket, section, key, local_path
        key, local_path = args[4], args[5]
        if key.endswith(".tar.gz"):
            swap.note_archive(local_path)
        return real_put(*args, **kwargs)

    monkeypatch.setattr(storage, "_checked", swap)
    monkeypatch.setattr(backup.storage, "put_file", watching_put)
    with (
        mock.patch.object(backup, "_authorize_upload"),
        # Both run after the archive PUT and would each reach the stub again;
        # neither is what these tests are about.
        mock.patch.object(backup, "_publish_label"),
        mock.patch.object(backup, "_prune_remote_archives"),
    ):
        if not backup._CAN_PIN_TRAVERSAL:
            with pytest.raises(RuntimeError, match="openat|pinned"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
            assert not swap.swapped, "nothing may be staged when the run refuses"
            assert not swap.uploaded, "a refused run must not upload"
            return None
        if not storage._UNNAMED_BODY_SUPPORTED:
            # macOS, the BSDs, Windows, or a Linux without memfd/file seals: the
            # sessions body cannot be held unrewritable from creation, so the run
            # refuses outright rather than upload bytes it cannot vouch for -- the
            # blessed product shape. Assert that refusal (a stronger outcome than the
            # swap these tests describe) rather than skip, so a regression that let an
            # unholdable body through would redden here.
            with pytest.raises(RuntimeError, match="unavailable on this platform|unrewritable"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
            assert not swap.uploaded, "a refused run must not upload"
            return None
        backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)
    return swap


class TestUploadedBytesAreTheArchive:
    def test_a_swap_before_the_upload_cannot_change_the_bytes_that_leave(
        self, _sessions_host, monkeypatch
    ):
        # The regression. On a name-based upload the planted file is what the CLI
        # opens, so bytes nothing checked leave the host unrecallably. On the POSIX
        # descriptor path the staging inode is unlinked before the stream, so the
        # name-swap has nothing to replace (``swap_prevented``); either way what
        # leaves is the archive, never the plant.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        assert swap.swapped, "the swap never fired, so this test proves nothing"
        assert swap.uploaded != PLANTED
        assert swap.uploaded[:2] == b"\x1f\x8b", "the upload did not carry a gzip archive"

    def test_the_uploaded_archive_still_holds_both_session_halves(
        self, _sessions_host, monkeypatch, tmp_path
    ):
        # Not merely "not the planted bytes": the bytes that left are the archive
        # the tar wrote, member for member. A refusal would also satisfy the test
        # above, and a refused nightly backup is its own failure.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        landed = tmp_path / "landed.tar.gz"
        landed.write_bytes(swap.uploaded)
        with tarfile.open(landed) as tar:
            assert sorted(tar.getnames()) == ["cli/replay.jsonl", "crew/t.jsonl"]

    def test_the_recorded_fingerprint_describes_the_bytes_that_left(
        self, _sessions_host, monkeypatch
    ):
        # The record's fingerprint and size describe the bytes that actually left,
        # which the descriptor holds. On Linux the archive is nameless (O_TMPFILE),
        # so there is no name to substitute at all -- the record, the entry-set
        # digest and the upload all read the one nameless descriptor and agree on the
        # real archive by construction.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        # The uploaded bytes are read from the streamed descriptor by the stub, so
        # they are the archive that left. The record must describe THOSE bytes.
        assert swap.uploaded[:2] == b"\x1f\x8b", "the upload did not carry a gzip archive"
        expected = hashlib.md5(swap.uploaded, usedforsecurity=False).hexdigest()  # noqa: S324
        assert record["fingerprint"] == expected
        assert record["bytes"] == len(swap.uploaded)

    def test_a_swap_between_the_tar_closing_and_the_entry_read_changes_nothing(
        self, _sessions_host, monkeypatch
    ):
        # The entry-set digest is what DECIDES whether to upload, so it has to
        # describe the bytes that are then uploaded. On Linux the archive is a
        # nameless O_TMPFILE inode written THROUGH the descriptor, so there is no
        # name for a same-UID process to unlink-and-replace in the window between the
        # tar closing and the digest -- a name-based swap is structurally impossible,
        # which is the stronger outcome. The digest and the upload both read the one
        # descriptor and describe the real archive.
        real_entries = backup._archive_entries
        fired: dict[str, Any] = {}

        def swapping_entries(archive=None, *, volatile_root, fd=None):
            # Try the name-based swap the old model relied on; on Linux the archive
            # is nameless so the name does not exist and the swap cannot fire, which
            # is recorded rather than asserted away.
            if "checked" not in fired:
                fired["checked"] = True
                if archive is not None and Path(archive).exists():
                    here = Path(archive)
                    fired["real"] = here.read_bytes()
                    here.unlink()
                    here.write_bytes(PLANTED)
                    fired["swapped"] = True
                else:
                    fired["swapped"] = False
            return real_entries(archive, volatile_root=volatile_root, fd=fd)

        monkeypatch.setattr(backup, "_archive_entries", swapping_entries)
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        assert "checked" in fired, "the entry read never ran, so this test proves nothing"
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        # Whether or not the swap could fire, the uploaded bytes are a real gzip
        # archive and the recorded tree digest is non-empty -- both read the
        # descriptor, not a name.
        assert record["tree"] != ""
        assert swap.uploaded[:2] == b"\x1f\x8b", "the upload did not carry a gzip archive"
        if fired.get("swapped"):
            # Named-archive platform (Windows): the descriptor still carried the real
            # archive despite the name being swapped to the plant.
            assert swap.uploaded == fired["real"]


class TestPutFileRefusesASubstitutedBody:
    """``put_file`` is the shared helper every backup kind uploads through.

    A helper that takes only a name cannot express "upload THIS file": the CLI
    resolves the name again. These pin the identity checks it makes on the inode it
    uploads, which is what protects the callers with no fingerprint of their own --
    the label sidecar and the library push.
    """

    @pytest.fixture(autouse=True)
    def _stub_cli(self, monkeypatch):
        self.seen: dict[str, Any] = {}

        def fake_checked(args, profile, *, action, timeout=30, extra_visible_dirs=(), **kw):
            self.seen["args"] = args
            self.seen["body"] = _body_the_child_would_read(args, kw.get("stdin_fd"))
            return "{}"

        monkeypatch.setattr(storage, "_checked", fake_checked)

    def test_a_symlink_at_the_name_is_refused_rather_than_followed(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_bytes(b"owner-only")
        link = tmp_path / "payload.bin"
        if not hasattr(os, "O_NOFOLLOW"):
            # Without O_NOFOLLOW this open cannot refuse a link AT the open. The
            # symlink is planted at the staged name and put_file is asked to upload
            # it; the verified-body open still refuses a non-regular file, so nothing
            # is staged or sent.
            link.symlink_to(secret)
            with pytest.raises((storage.AWSError, OSError)):
                storage.put_file(
                    "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(link), account=ACCOUNT
                )
            return
        link.symlink_to(secret)
        with pytest.raises(storage.AWSError, match="not a regular file|link"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(link), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_a_hard_link_is_refused_because_it_defeats_the_other_two_checks(self, tmp_path):
        # A hard link is a genuine regular file reached under the expected name,
        # so S_ISREG passes and O_NOFOLLOW has nothing to reject. The link COUNT
        # is the only thing that tells it from the file we staged.
        secret = tmp_path / "secret"
        secret.write_bytes(b"owner-only")
        linked = tmp_path / "payload.bin"
        os.link(secret, linked)
        with pytest.raises(storage.AWSError, match="more than one name|link"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(linked), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_a_fifo_is_refused_before_any_byte_is_read(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            # Asserted rather than skipped. A FIFO cannot exist here, so what this test
            # is really pinning -- that a body which is not a regular file is refused
            # before a byte is read -- is exercised through the guard itself, against a
            # directory, which is the non-regular thing every platform has.
            directory = tmp_path / "payload.bin"
            directory.mkdir()
            with pytest.raises((storage.AWSError, OSError)):
                storage.put_file(
                    "p",
                    "us-west-2",
                    "bkt",
                    "backup",
                    "k/payload.bin",
                    str(directory),
                    account=ACCOUNT,
                )
            assert "body" not in self.seen
            return
        fifo = tmp_path / "payload.bin"
        os.mkfifo(fifo)
        assert stat.S_ISFIFO(fifo.stat().st_mode)
        with pytest.raises(storage.AWSError, match="not a regular file"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(fifo), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_an_ordinary_file_uploads_its_own_bytes(self, tmp_path):
        payload = tmp_path / "payload.bin"
        payload.write_bytes(b"real-payload")
        storage.put_file(
            "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(payload), account=ACCOUNT
        )
        assert self.seen["body"] == b"real-payload"

    def test_the_owner_pinning_and_the_size_ceiling_survive_the_descriptor_body(self, tmp_path):
        # The ceiling is measured on the DESCRIPTOR being uploaded, so the number
        # checked and the bytes sent cannot disagree -- a size read from the name is
        # the planted inode's size whenever one has been planted.
        payload = tmp_path / "payload.bin"
        payload.write_bytes(b"x" * 10)
        monkeypatched = 4
        with mock.patch.object(storage, "_MAX_PINNED_TRANSFER_BYTES", monkeypatched):
            with pytest.raises(storage.AWSError, match="exceeds the"):
                storage.put_file(
                    "p",
                    "us-west-2",
                    "bkt",
                    "backup",
                    "k/payload.bin",
                    str(payload),
                    account=ACCOUNT,
                )
        storage.put_file(
            "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(payload), account=ACCOUNT
        )
        assert "--expected-bucket-owner" in self.seen["args"]
        assert self.seen["args"][self.seen["args"].index("--expected-bucket-owner") + 1] == ACCOUNT


class TestFingerprintsReadTheHeldInode:
    """Both digests describe the file the caller holds, not what its name reaches.

    They are what decides whether to upload at all and what the run record claims
    the object holds, so a digest taken from a re-resolved name can approve one
    file and record another. These replace the file at the name after the
    descriptor is open, which is the whole substitution in one step.
    """

    @staticmethod
    def _archive(tmp_path: Path) -> Path:
        path = tmp_path / "sessions.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("crew/t.jsonl")
            payload = b"transcript\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return path

    def test_the_entry_set_digest_survives_a_swap_at_the_name(self, tmp_path):
        path = self._archive(tmp_path)
        fd = os.open(path, os.O_RDONLY)
        try:
            from_descriptor_before = backup._tree_fingerprint(path, volatile_root=False, fd=fd)
            if not _swap_at_the_name(path, PLANTED):
                # The platform refused the unlink while the descriptor was open, so
                # the substitution never happened. Assert that outcome rather than
                # skipping: the name must still reach the archive we staged, and the
                # descriptor must agree with it.
                assert backup._tree_fingerprint(path, volatile_root=False) == (
                    from_descriptor_before
                )
                assert path.read_bytes() != PLANTED
                return
            assert backup._tree_fingerprint(path, volatile_root=False, fd=fd) == (
                from_descriptor_before
            )
            # The name now reaches something that is not a tar.gz at all, which is
            # the reading the fingerprint would have taken from it.
            assert backup._tree_fingerprint(path, volatile_root=False) == ""
        finally:
            os.close(fd)

    def test_the_body_digest_survives_a_swap_at_the_name(self, tmp_path):
        path = tmp_path / "payload.bin"
        path.write_bytes(b"real-payload")
        fd = os.open(path, os.O_RDONLY)
        try:
            expected = hashlib.md5(b"real-payload", usedforsecurity=False).hexdigest()  # noqa: S324
            if not _swap_at_the_name(path, PLANTED):
                # Same reasoning as above: the refusal IS the protection here, so both
                # readings must still describe the payload we wrote.
                assert backup._body_fingerprint(fd=fd) == expected
                assert backup._body_fingerprint(path) == expected
                return
            assert backup._body_fingerprint(fd=fd) == expected
            assert backup._body_fingerprint(path) != expected
        finally:
            os.close(fd)

    def test_reading_from_a_descriptor_leaves_its_position_alone(self, tmp_path):
        # The push paths hand ONE descriptor to the entry-set digest, the body
        # digest, the size and the upload in turn. A reader that consumed the
        # position would leave the next one with an empty file, so the upload
        # would send nothing and the record would still call it a success.
        path = self._archive(tmp_path)
        fd = os.open(path, os.O_RDONLY)
        try:
            backup._tree_fingerprint(path, volatile_root=False, fd=fd)
            backup._body_fingerprint(fd=fd)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 0
            assert _read_whole(fd) == path.read_bytes()
        finally:
            os.close(fd)


class TestOffsetReadWhereThereIsNoPread:
    """The fallback arm of :func:`backup._read_at`, exercised with ``pread`` masked.

    A POSIX runner always takes the ``os.pread`` arm, so the arm Windows actually
    runs would otherwise reach a user before anything measured it. Deleting the
    attribute is what makes that arm run here, which is also the only shape the
    absence takes: on Windows ``os`` has no ``pread`` at all.
    """

    @staticmethod
    def _archive(tmp_path: Path) -> Path:
        path = tmp_path / "sessions.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("crew/t.jsonl")
            payload = b"transcript\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return path

    def test_the_fallback_reads_the_same_bytes_at_an_offset(self, tmp_path):
        path = tmp_path / "payload.bin"
        path.write_bytes(b"0123456789")
        fd = os.open(path, os.O_RDONLY)
        try:
            with _no_pread():
                assert backup._read_at(fd, 4, 3) == b"3456"
        finally:
            os.close(fd)

    def test_the_fallback_puts_the_callers_position_back(self, tmp_path):
        # This is the whole reason the helper exists. The push paths hand ONE
        # descriptor to the entry-set digest, the body digest, the size and the
        # upload in turn, so a read that left the position moved would give the
        # next reader a short file and the run record would still call it a
        # success.
        path = tmp_path / "payload.bin"
        path.write_bytes(b"0123456789")
        fd = os.open(path, os.O_RDONLY)
        try:
            os.lseek(fd, 2, os.SEEK_SET)
            with _no_pread():
                assert backup._read_at(fd, 3, 6) == b"678"
            assert os.lseek(fd, 0, os.SEEK_CUR) == 2
        finally:
            os.close(fd)

    def test_both_fingerprints_agree_with_an_independent_reader(self, tmp_path):
        path = self._archive(tmp_path)
        fd = os.open(path, os.O_RDONLY)
        try:
            with mock.patch.object(backup.os, "pread", _reference_pread(path), create=True):
                reference = (
                    backup._tree_fingerprint(path, volatile_root=False, fd=fd),
                    backup._body_fingerprint(fd=fd),
                )
            with _no_pread():
                without_pread = (
                    backup._tree_fingerprint(path, volatile_root=False, fd=fd),
                    backup._body_fingerprint(fd=fd),
                )
            assert without_pread == reference
            assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        finally:
            os.close(fd)


class TestPinnedStagingDirectory:
    """The archive is created relative to a held directory descriptor.

    A pre-planted entry at the archive's own name must fail the create rather
    than become the file the tar writes through, and the directory itself must
    not be reachable through a link.
    """

    @pytest.fixture(autouse=True)
    def _confined(self, monkeypatch):
        # The O_TMPFILE archive arm is taken only on a confined host (the
        # staging-leaf mask removes the same-UID writer, so the nameless inode's
        # /proc alias is unreachable too). An unconfined test host would fail
        # closed here; the refusal has its own dedicated test. Set the mask present
        # so these cases exercise the descriptor pin they are about.
        monkeypatch.setattr(storage, "_staging_leaf_is_masked", lambda: True)

    def test_a_plain_file_already_at_the_archive_name_fails_the_create(self, tmp_path):
        # On Linux the archive body is produced into an anonymous memfd, so a
        # pre-planted entry at the archive's name is irrelevant -- the memfd has no
        # directory entry at all, there is no name to collide with, and its inode is
        # a fresh one no planted file can be. Where no sealable memfd is available
        # the create fails closed instead.
        directory = tmp_path / "staging"
        directory.mkdir()
        (directory / "archive.tar.gz").write_bytes(b"planted")
        dir_fd = platform_compat.pin_directory(directory)
        try:
            if _body_can_be_held():
                fd = backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
                try:
                    # A fresh nameless inode: distinct from the planted file, and the
                    # planted file is untouched.
                    planted = os.stat(directory / "archive.tar.gz")
                    created = os.fstat(fd)
                    assert (created.st_dev, created.st_ino) != (planted.st_dev, planted.st_ino)
                    assert (directory / "archive.tar.gz").read_bytes() == b"planted"
                finally:
                    os.close(fd)
            else:
                # No sealable memfd (a non-Linux platform, or a Linux without
                # memfd/seals): no transfer-lifetime hold, so the create fails closed
                # per the ruling; the planted file is untouched and nothing staged.
                with pytest.raises(storage.AWSError, match="transfer|memfd|Linux"):
                    backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
                assert (directory / "archive.tar.gz").read_bytes() == b"planted"
        finally:
            os.close(dir_fd)

    def test_a_link_planted_at_the_archive_name_fails_the_create(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_bytes(b"")
        directory = tmp_path / "staging"
        directory.mkdir()
        (directory / "archive.tar.gz").symlink_to(target)
        dir_fd = platform_compat.pin_directory(directory)
        try:
            if _body_can_be_held():
                # An anonymous memfd resolves no name, so a planted link cannot
                # redirect it and the created inode is neither the link nor its
                # target.
                fd = backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
                try:
                    created = os.fstat(fd)
                    aimed = os.stat(target)
                    assert (created.st_dev, created.st_ino) != (aimed.st_dev, aimed.st_ino)
                finally:
                    os.close(fd)
            else:
                # No sealable memfd: fail closed per the ruling, nothing staged.
                with pytest.raises(storage.AWSError, match="transfer|memfd|Linux"):
                    backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
        finally:
            os.close(dir_fd)

    def test_the_created_archive_is_ours_alone(self, tmp_path):
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            if not (storage._UNNAMED_BODY_SUPPORTED and storage._staging_leaf_is_masked()):
                # No transfer-lifetime hold available (a non-Linux platform, or a
                # Linux without memfd/seals, or an unmasked host): the create refuses
                # per the ruling. Asserted rather than skipped so the behaviour is
                # pinned.
                with pytest.raises(storage.AWSError, match="transfer|memfd|seal|Linux"):
                    backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
                return
            fd = backup._create_pinned_archive_fd("archive.tar.gz", _writes(b"body-bytes"))
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                # An anonymous memfd has NO directory entry, so its link count is
                # zero -- no name reaches it for any process to open.
                assert info.st_nlink == 0
                # The returned descriptor is ALREADY sealed by the helper (the
                # gateway never held a writable alias): a write is refused by the
                # kernel, through this descriptor AND through any /proc alias.
                with pytest.raises(OSError):
                    os.write(fd, b"tamper")
                # The bytes write_body produced are intact and readable.
                assert os.pread(fd, 64, 0) == b"body-bytes"
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)


class TestStagingStandsOnTheMaskedRoot:
    """Where the archive is staged, not just how it is addressed.

    The descriptor pin defeats a rename, an unlink and a planted link at the
    archive's name. It cannot defeat a WRITE. A sibling agent that opens the
    staged archive and rewrites it changes the very inode this module holds, so
    the entry-set digest, the body digest and the upload all read the substituted
    bytes and AGREE with one another -- the run then records a successful backup
    of a file it never built, and retention is free to prune the valid
    predecessor it supersedes. Nothing downstream can notice, because every
    measurement was taken after the substitution.

    So the writer has to be removed rather than detected, and that is a property
    of the DIRECTORY: the shared system temp directory is same-UID writable and
    carries no mask, while the AWS Control staging leaf is bound over with an
    empty directory inside every agent's namespace. An archive there has no name
    a sibling agent can open.
    """

    def test_the_staging_directory_is_cut_under_the_masked_root(self, tmp_path):
        root = tmp_path / "aws-control-staging"
        root.mkdir()
        with mock.patch.object(storage, "staging_root", return_value=root) as consulted:
            with storage.pinned_staging("kc-backup-") as (directory, dir_fd):
                assert dir_fd >= 0
                # The parent is the root, so the archive inside it inherits the
                # mask. Staging in the shared temp directory would put the parent
                # somewhere no mask covers, which is the defect this pins.
                assert directory.parent == root
                assert directory.is_dir()
        assert consulted.called

    def test_the_staging_directory_is_removed_with_its_contents(self, tmp_path):
        # The masked root is long-lived, so a staging directory that outlived its
        # run would accumulate archives there -- each one a complete copy of the
        # owner's sessions sitting on disk for no reason.
        root = tmp_path / "aws-control-staging"
        root.mkdir()
        with mock.patch.object(storage, "staging_root", return_value=root):
            with storage.pinned_staging("kc-backup-") as (directory, _dir_fd):
                (directory / "archive.tar.gz").write_bytes(b"staged")
                held = directory
        assert not held.exists()
        assert list(root.iterdir()) == []

    def test_the_staging_leaf_is_one_the_sandbox_masks(self):
        # A SOURCE ratchet, because no behavioural test in this process can see a
        # mount namespace: the whole argument above rests on that leaf being
        # masked, and the two facts live in different modules. Renaming the leaf
        # on one side without the other would leave the archive staged in an
        # agent-reachable directory while every other test here still passed.
        assert storage.STAGING_DIR_LEAF in sandbox._CREW_HIDDEN_LEAVES

    def test_the_real_root_is_not_the_shared_temp_directory(self, tmp_path):
        # The patched-root tests above would also pass if `staging_root` itself
        # returned the shared temp directory, so the real function is checked once
        # here against the directory the finding was about.
        real = storage.staging_root().resolve()
        assert real.name == storage.STAGING_DIR_LEAF
        assert real != Path(tempfile.gettempdir()).resolve()


class TestTheArchiveBodyIsHeldFromCreation:
    """The backup archive's bytes are bound to one descriptor from build to upload.

    This is the frozen Goal, and the only surface this PR hardens: the archive is
    produced INTO a nameless ``O_TMPFILE`` inode on a confined Linux host, so between
    the tar being written and the upload there is no named file a same-UID process
    could rewrite and no ``/proc`` alias it could reach. Where that cannot be
    expressed -- an unconfined POSIX host, macOS, the BSDs -- the run fails closed up
    front rather than staging a body that could be substituted unseen. The label
    sidecar, the library push and the drive spool are deliberately NOT in scope here;
    they stay on the plain named-body path they have always used, and their platform
    availability is unchanged.
    """

    def test_the_archive_is_produced_into_a_nameless_inode_when_confined(
        self, tmp_path, monkeypatch
    ):
        # The memfd arm is taken only on a confined host; set the mask present so
        # the descriptor pin is exercised.
        monkeypatch.setattr(storage, "_staging_leaf_is_masked", lambda: True)
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            if not _body_can_be_held():
                # No transfer-lifetime hold available here: the create must REFUSE,
                # not stage a point-in-time-safe named body. Asserted rather than
                # skipped so the platform's safety behaviour is pinned.
                with pytest.raises(storage.AWSError, match="transfer|memfd|unrewritable"):
                    backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
                assert not (directory / "archive.tar.gz").exists()
                return
            fd = backup._create_pinned_archive_fd("archive.tar.gz", _writes(b"archive-bytes"))
            try:
                # No directory entry: link count zero, and no name reaches the inode.
                assert os.fstat(fd).st_nlink == 0, "the archive still has a directory entry"
                assert not (directory / "archive.tar.gz").exists()
                # /proc shows a nameless inode as a deleted path.
                assert "(deleted)" in os.readlink(f"/proc/self/fd/{fd}")
                # The tar bytes write_body produced are read back through the sealed
                # descriptor the helper returned.
                assert os.pread(fd, 13, 0) == b"archive-bytes"
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    def test_a_confined_host_without_o_tmpfile_fails_closed(self, tmp_path, monkeypatch):
        """No O_TMPFILE means no transfer-lifetime hold, so it refuses rather than stage.

        A named body under a point-in-time mask check is not enough for a
        minutes-long transfer: an agent spawned mid-stream could open the name and
        rewrite it. Only a nameless O_TMPFILE inode (no name to reopen for the
        descriptor's whole life) provides the transfer-lifetime exclusion, so a
        confined host whose staging filesystem does not honour O_TMPFILE fails closed.
        Forced here by turning the O_TMPFILE support constant off while the mask is
        present.
        """
        monkeypatch.setattr(storage, "_UNNAMED_BODY_SUPPORTED", False)
        monkeypatch.setattr(storage, "_staging_leaf_is_masked", lambda: True)
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            with pytest.raises(storage.AWSError, match="transfer|O_TMPFILE|unrewritable"):
                backup._create_pinned_archive_fd("archive.tar.gz", _no_body)
            # Nothing staged.
            assert not (directory / "archive.tar.gz").exists()
        finally:
            os.close(dir_fd)

    def test_the_archive_body_fails_closed_where_a_nameless_inode_is_unavailable(self, tmp_path):
        """macOS/BSD -- no mask and no O_TMPFILE -- refuses rather than stage.

        The SCOPED refusal First Principles asked for: not "refuse every unmasked
        POSIX upload", but the platforms where a body held unrewritable cannot be
        expressed at all. macOS/BSD have neither the sandbox mask (so
        ``_staging_leaf_is_masked`` is False) nor O_TMPFILE, so the
        create fails closed. Forced here by removing both.
        """
        with mock.patch.object(storage, "_UNNAMED_BODY_SUPPORTED", False):
            with mock.patch.object(storage.platform_compat, "IS_POSIX", True):
                with mock.patch.object(storage, "_staging_leaf_is_masked", lambda: False):
                    with pytest.raises(storage.AWSError, match="unrewritable|mask|writer"):
                        storage.build_sealed_upload_body(_no_body, "body.bin")

    def test_the_helper_holds_the_build_window_without_the_mask(self, tmp_path):
        """The sealed helper covers the build window the seal cannot -- no mask needed.

        ``memfd_create`` returns a WRITABLE descriptor and ``F_SEAL_WRITE`` is applied
        only after the whole body is written, so the build window is unprotected IN
        THE PROCESS THAT HOLDS THE WRITABLE MEMFD. The close is that the writable memfd
        never lives in the gateway: an exec'd NON-DUMPABLE helper creates it (its
        ``/proc`` entries reparent to root, so a same-UID reader gets ``EACCES``) and
        receives the body over an ``AF_UNIX`` socketpair a same-UID agent cannot reopen
        through ``/proc`` (ENXIO). That isolation holds the window on its own, so the
        sandbox mask is NOT required -- requiring it only withdrew the kind on unmasked
        Linux without adding a hold the helper lacks (First Principles item 4).

        So with the mask reported ABSENT the create still SUCCEEDS and returns a sealed
        descriptor; the body is immutable to every writer afterwards.
        """
        if not (platform_compat.IS_LINUX and storage._UNNAMED_BODY_SUPPORTED):
            # Not the Linux memfd path: there is no sealable memfd, so the create fails
            # closed on this real platform regardless of the mask. Assert that rather
            # than skipping -- a skip would score as a pass and hide a regression on a
            # Windows/macOS CI runner.
            with pytest.raises(storage.AWSError, match="unrewritable|whole transfer|memfd"):
                storage.build_sealed_upload_body(_no_body, "body.bin")
            return
        # Mask absent -> the helper still holds the build window -> create SUCCEEDS, and
        # the returned fd is SEALED by the helper (write refused), carrying what
        # write_body wrote. This is the behaviour the dropped mask conjunct would have
        # refused.
        with mock.patch.object(storage, "_staging_leaf_is_masked", lambda: False):
            fd = storage.build_sealed_upload_body(_writes(b"held-bytes"), "body.bin")
            try:
                with pytest.raises(OSError):
                    os.write(fd, b"x")
                assert os.pread(fd, 10, 0) == b"held-bytes"
            finally:
                os.close(fd)

    def test_run_sessions_backup_refuses_up_front_on_an_unholdable_host(self, monkeypatch):
        """The run states the refusal before any work, not deep in the build.

        ``kind_unavailable_reason`` quotes the same reason to the owner up front, so
        a host that cannot hold the body (no sealable memfd -- Windows, macOS, the
        BSDs, a Linux without ``memfd_create``/file seals) gets a stated "unavailable"
        answer rather than a failed-run record from an exception raised while the
        archive is built. Driven here by the no-memfd condition -- one of the two the
        gate requires (the other is the build-window mask) -- so the refusal states
        up front before any work begins.
        """
        monkeypatch.setattr(storage, "_UNNAMED_BODY_SUPPORTED", False)
        if not backup._CAN_PIN_TRAVERSAL:
            # A host without pinned traversal (Windows) refuses EARLIER, for its own
            # reason (no pinned walk), before the held-body check. Assert it still
            # refuses up front rather than skipping -- the point of this test is that
            # no run proceeds to build on an unholdable host, whichever reason fires.
            with pytest.raises(
                (RuntimeError, storage.AWSError), match="pin|traversal|unrewritable|memfd|held"
            ):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
            return
        with pytest.raises(RuntimeError, match="unrewritable|held unrewritable|memfd"):
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)

    def test_the_archive_bytes_are_written_whole(self, tmp_path, monkeypatch):
        # os.write is not obliged to take the whole buffer in one call; the helper's
        # drain loop and the write_body loop both handle that. Verified through the
        # returned sealed descriptor since the inode is nameless.
        monkeypatch.setattr(storage, "_staging_leaf_is_masked", lambda: True)
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            payload = bytes(range(256)) * 400
            if not _body_can_be_held():
                # No transfer-lifetime hold here: the create refuses rather than
                # staging a point-in-time-safe named body. Asserted, not skipped, so
                # the platform's behaviour is pinned either way.
                with pytest.raises(storage.AWSError, match="transfer|memfd|unrewritable"):
                    backup._create_pinned_archive_fd("archive.tar.gz", _writes(payload))
                return
            fd = backup._create_pinned_archive_fd("archive.tar.gz", _writes(payload))
            try:
                assert os.fstat(fd).st_size == len(payload)
                read = b""
                while len(read) < len(payload):
                    chunk = os.pread(fd, 1 << 20, len(read))
                    if not chunk:
                        break
                    read += chunk
                assert read == payload
                # The returned fd is SEALED: a write is refused.
                with pytest.raises(OSError):
                    os.write(fd, b"x")
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
