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

import ast
import contextlib
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

    def note_archive(self, local_path: str) -> None:
        self.archive = Path(local_path)

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
            # Read before replacing, so the assertions have the archive the tar
            # actually wrote rather than having to re-derive it.
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
        backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)
    return swap


class TestUploadedBytesAreTheArchive:
    def test_a_swap_before_the_upload_cannot_change_the_bytes_that_leave(
        self, _sessions_host, monkeypatch
    ):
        # The regression. On a name-based upload the planted file is what the CLI
        # opens, so bytes nothing checked leave the host unrecallably.
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
        # The record's fingerprint and size describe the archive, not whatever the
        # name reaches when they are taken: from separate resolutions of one name the
        # record can describe the substitute while the entry-set digest that
        # authorized the upload describes the archive.
        # Asserted against the bytes read BEFORE the swap, which is the archive the
        # tar wrote: comparing the record to the uploaded bytes alone passes when
        # both read the substitute, which is the state this closes.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        expected = hashlib.md5(swap.before_swap, usedforsecurity=False).hexdigest()  # noqa: S324
        assert swap.uploaded == swap.before_swap
        assert record["fingerprint"] == expected
        assert record["bytes"] == len(swap.before_swap)

    def test_a_swap_between_the_tar_closing_and_the_entry_read_changes_nothing(
        self, _sessions_host, monkeypatch
    ):
        # The entry-set digest is what DECIDES whether to upload, so it has to
        # describe the file that is then uploaded. This puts the substitution in
        # the window between the tar closing and that digest being taken: read
        # from the name, the digest describes the planted file -- which is not a
        # tarball, so it is empty; read from the descriptor, it describes the
        # archive.
        real_entries = backup._archive_entries
        fired: dict[str, Any] = {}

        def swapping_entries(archive=None, *, volatile_root, fd=None):
            if archive is not None and "real" not in fired and Path(archive).exists():
                here = Path(archive)
                fired["real"] = here.read_bytes()
                here.unlink()
                here.write_bytes(PLANTED)
            return real_entries(archive, volatile_root=volatile_root, fd=fd)

        monkeypatch.setattr(backup, "_archive_entries", swapping_entries)
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # the refusal on an unpinnable platform was asserted in the helper
        assert "real" in fired, "the swap never fired, so this test proves nothing"
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        # A digest taken from the planted file is empty, because it is not a
        # tar.gz -- so a non-empty digest is itself the evidence the read went
        # through the descriptor.
        assert record["tree"] != ""
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
            # Asserted rather than skipped: without O_NOFOLLOW this open cannot refuse
            # a link AT the open, so the claim to verify is that the substitution has
            # no way in here either. The staged name is created by this app with
            # CREATE_NEW semantics, so a link already sitting at it loses the create
            # instead of becoming the file written through.
            with pytest.raises(OSError):
                platform_compat.create_file_deny_write(str(secret))
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

    def test_a_plain_file_already_at_the_archive_name_fails_the_create(self, tmp_path):
        # Only O_EXCL refuses this one. A pre-planted REGULAR file is not a link,
        # so O_NOFOLLOW has nothing to reject, and without the exclusive create the
        # tar would open it and write through it -- which is a file this process
        # did not stage becoming the object that leaves the host.
        directory = tmp_path / "staging"
        directory.mkdir()
        (directory / "archive.tar.gz").write_bytes(b"planted")
        dir_fd = platform_compat.pin_directory(directory)
        try:
            with pytest.raises(FileExistsError):
                backup._create_pinned_archive_fd(directory, dir_fd, "archive.tar.gz")
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
            with pytest.raises(OSError):
                backup._create_pinned_archive_fd(directory, dir_fd, "archive.tar.gz")
        finally:
            os.close(dir_fd)

    def test_the_created_archive_is_ours_alone(self, tmp_path):
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            fd = backup._create_pinned_archive_fd(directory, dir_fd, "archive.tar.gz")
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                assert info.st_nlink == 1
                if platform_compat.IS_POSIX:
                    # Windows does not carry POSIX permission bits at all: it
                    # reports 0o666 for any writable file whatever mode the create
                    # asked for, so asserting 0o600 there measures the platform's
                    # stat emulation rather than this code. Least privilege on that
                    # platform comes from the directory's ACL, which the staging
                    # root inherits.
                    assert stat.S_IMODE(info.st_mode) == 0o600
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


class TestNoUploadBodyStagesInTheSharedTempRoot:
    """A ratchet over the whole backend, not one test per site.

    Four separate places staged an upload body with a bare ``tempfile`` call: the
    archive, the backup label sidecar, the library push and the drive upload
    spool. Each one defaults to the shared system temp root, which is same-UID
    writable and unmasked, so the bytes can be rewritten in place between being
    written and being uploaded -- and the descriptor the upload holds fixes which
    inode it sends, not that inode's contents.

    Fixing them one at a time leaves the next one to be found by a reviewer, so
    the invariant is asserted over the module instead: in this backend, a temp
    staging call names where it stages. Parsed rather than grepped because these
    calls span several lines, which a regex reads wrong in both directions.
    """

    #: The ``tempfile`` entry points that default to the shared temp root.
    _ROOTED_AT_TMPDIR = {
        "mkdtemp",
        "mkstemp",
        "TemporaryDirectory",
        "NamedTemporaryFile",
        "TemporaryFile",
    }

    def _offenders(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in self._ROOTED_AT_TMPDIR:
                continue
            owner = func.value
            if not isinstance(owner, ast.Name) or owner.id != "tempfile":
                continue
            if any(kw.arg == "dir" for kw in node.keywords):
                continue
            found.append(f"{path.name}:{node.lineno} tempfile.{func.attr}() with no dir=")
        return found

    def test_every_temp_staging_call_names_its_directory(self):
        backend = Path(storage.__file__).parent
        sources = sorted(backend.glob("*.py"))
        # Control: an empty file list would make this pass vacuously, and the
        # glob is the only thing standing between the assertion and no corpus.
        assert len(sources) >= 4, f"expected the backend's modules, got {sources}"
        offenders: list[str] = []
        for source in sources:
            offenders.extend(self._offenders(source))
        assert not offenders, (
            "these staging calls default to the shared system temp root, where a "
            "same-UID process can rewrite the body between the write and the upload: "
            f"{offenders}. Pass dir=storage.staging_root()."
        )

    def test_only_storage_reaches_the_staging_root_directly(self):
        """Naming the root is not enough -- the directory must also be PINNED.

        Staging under the masked root stops a sibling agent rewriting the body in
        place. It does not stop the other half: where no descriptor can be handed
        to the child, ``put_file`` gives the CLI a NAME, and a name is re-resolved
        at the child's open. Only holding the directory open makes that sound, and
        a caller that reaches ``staging_root()`` itself gets the root without the
        pin -- relocated, and still replaceable.

        So the root is storage's alone, and every other module reaches staging
        through :func:`storage.cut_pinned_staging` or :func:`storage.pinned_staging`,
        where the pin is not optional.
        """
        backend = Path(storage.__file__).parent
        sources = sorted(p for p in backend.glob("*.py") if p.name != "storage.py")
        # Control: the glob is all that stands between this and an empty corpus,
        # and the modules that stage bodies are the point.
        assert len(sources) >= 3, f"expected the backend's other modules, got {sources}"
        reached: list[str] = []
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                # The NAME, wherever it appears -- not only where it is called.
                # `asyncio.to_thread(storage.cut_pinned_staging, prefix)` hands the
                # function over by reference precisely so it runs on the worker
                # thread, and this module already does that. Matching only
                # ``ast.Call`` would let `to_thread(storage.staging_root)` take the
                # unpinned root while this stayed green.
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                else:
                    continue
                if name == "staging_root":
                    reached.append(f"{source.name}:{node.lineno}")
        assert not reached, (
            "these reach storage.staging_root(), which yields the masked root "
            "WITHOUT a pin on the directory, so on a platform that uploads by name the "
            f"path can still be re-pointed before the child opens it: {reached}. "
            "Use storage.pinned_staging() (or cut_pinned_staging() off the event loop)."
        )


class TestEveryUploadBodyIsHeldFromCreation:
    """A body written by NAME and uploaded by name is unheld in between.

    The relocation under the masked leaf answers this only where a mask exists,
    and it does not exist whenever no namespace is active -- ``agent.sandbox="off"``
    or a host with no backend -- nor anywhere on Windows. So the hold is taken when
    the file is created instead of being inferred from the platform.

    ``_verified_body_fd`` inside ``put_file`` is not that hold: it opens a file the
    caller already created and closed, so it describes what is at the name by the
    time it looks, which is after the window it would need to cover. For the library
    push the consequence is not abstract -- the credential and exfiltration scan runs
    on the in-memory string, so bytes substituted in that gap reach the bucket
    unscanned.
    """

    def test_the_body_is_created_through_the_deny_write_helper(self, tmp_path):
        with mock.patch.object(
            platform_compat,
            "create_file_deny_write",
            wraps=platform_compat.create_file_deny_write,
        ) as created:
            with storage.held_body(tmp_path, "body.json", b"held-from-birth") as (path, fd):
                # Held: the descriptor is open on the file while the caller uses it,
                # so there is no moment between the bytes existing and the upload in
                # which the file is closed and replaceable.
                assert fd >= 0
                assert os.fstat(fd).st_size == len(b"held-from-birth")
                assert path.read_bytes() == b"held-from-birth"
        assert created.called, "the body must be created through the deny-write helper"
        # The descriptor is released on exit; the file itself goes with the staging
        # directory, so the helper must not leave an fd behind per upload.
        with pytest.raises(OSError):
            os.fstat(fd)

    def test_the_bytes_are_written_whole(self, tmp_path):
        # os.write is not obliged to take the whole buffer in one call, and a body
        # truncated here would be uploaded and recorded as complete.
        payload = bytes(range(256)) * 400
        with storage.held_body(tmp_path, "big.bin", payload) as (path, fd):
            assert os.fstat(fd).st_size == len(payload)
        assert path.read_bytes() == payload

    def _name_only_put_file_calls(self, path: Path) -> list[str]:
        """Every upload in *path* that reaches ``put_file`` without a held body.

        Matches the NAME wherever it appears, not only a direct call. The drive
        upload spends its ``put_file`` as ``asyncio.to_thread(storage_mod.put_file,
        ...)``, where the only ``Call`` node is ``to_thread`` and the keywords belong
        to it -- so a call-shaped check reports that site clean while it uploads a
        body nothing holds. That is how it was missed here, and it is the same
        reference-versus-call hole the staging ratchet above was raised for.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            named_here = isinstance(node.func, ast.Attribute) and node.func.attr == "put_file"
            # The reference form: `put_file` handed to another call, which is then
            # the call carrying the keywords.
            passed_along = any(
                isinstance(a, ast.Attribute) and a.attr == "put_file" for a in node.args
            )
            if not (named_here or passed_along):
                continue
            if any(kw.arg == "body_fd" for kw in node.keywords):
                continue
            form = "put_file()" if named_here else "put_file passed by reference"
            found.append(f"{path.name}:{node.lineno} {form} with no body_fd=")
        return found

    def test_no_upload_passes_only_a_name(self):
        """A ratchet over the backend, because fixing one site leaves the next.

        Four call sites staged a body with ``write_text`` or a plain ``open`` and let
        ``put_file`` resolve the name itself: the backup label sidecar, the library
        push's two objects, and the drive upload spool. Asserted over the module so a
        fifth cannot be added quietly -- parsed rather than grepped, since these calls
        span several lines and one of them never spells ``put_file(`` at all.
        """
        backend = Path(storage.__file__).parent
        sources = sorted(p for p in backend.glob("*.py") if p.name != "storage.py")
        # Control: the glob is the only thing between this and an empty corpus.
        assert len(sources) >= 3, f"expected the backend's other modules, got {sources}"
        offenders: list[str] = []
        for source in sources:
            offenders.extend(self._name_only_put_file_calls(source))
        assert not offenders, (
            "these uploads pass a name with no held descriptor, so the body is "
            "closed and replaceable between the write and the upload: "
            f"{offenders}. Stage through storage.held_body() and pass body_fd."
        )

    def _undrained_acquisitions(self, path: Path) -> list[str]:
        """Every acquisition in *path* that hands over a pin or descriptor unprotected.

        ``cut_pinned_staging`` and ``cut_held_body`` RETURN what has to be released,
        so the release is written as a ``try``/``finally`` around the assignment. A
        cancellation delivered at the acquiring ``await`` never binds that
        assignment, so the ``finally`` is never entered and what the thread took
        stays taken: the directory pin and the deny-write descriptor for the
        process's life, and the staging directory with whatever the transfer wrote.

        Matched by NAME wherever it appears, for the reason the upload ratchet above
        is: these are spent as ``to_thread(storage_mod.cut_..., ...)``, where the only
        ``Call`` node is ``to_thread``. An acquisition is protected when the call
        carrying the name is ``_drained_cut``, which drains the thread and then
        releases what it returned before the cancellation continues.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        cuts = {"cut_pinned_staging", "cut_held_body"}
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            named = {
                a.attr
                for a in [node.func, *node.args]
                if isinstance(a, ast.Attribute) and a.attr in cuts
            }
            if not named:
                continue
            caller = node.func
            through_drain = (isinstance(caller, ast.Name) and caller.id == "_drained_cut") or (
                isinstance(caller, ast.Attribute) and caller.attr == "_drained_cut"
            )
            if through_drain:
                continue
            found.append(f"{path.name}:{node.lineno} {sorted(named)} acquired without _drained_cut")
        return found

    def test_no_acquisition_hands_over_a_pin_a_cancellation_can_orphan(self):
        """A ratchet, because the release half was protected first and this one was not.

        The close that ends the spool descriptor's life is shielded; the opens that
        begin it were not, which leaves the same descriptor unreachable by exactly the
        cleanup written for it. Asserted over the backend so the next acquisition
        cannot arrive bare.
        """
        backend = Path(storage.__file__).parent
        sources = sorted(p for p in backend.glob("*.py") if p.name != "storage.py")
        # Control: the glob is the only thing between this and an empty corpus.
        assert len(sources) >= 3, f"expected the backend's other modules, got {sources}"
        offenders: list[str] = []
        for source in sources:
            offenders.extend(self._undrained_acquisitions(source))
        assert not offenders, (
            "a cancellation at these awaits orphans the pin or descriptor the thread "
            f"returned, since the finally that frees it is never entered: {offenders}. "
            "Acquire through _drained_cut, which releases what it took."
        )

    def test_a_held_body_is_its_own_guard(self, tmp_path):
        """``put_file`` must not take a SECOND deny-write hold on a held body.

        On Windows the creation-time handle keeps ``GENERIC_WRITE``, and an open that
        does not share write cannot coexist with it -- so a second deny-write open
        raises a sharing violation before any AWS call and the upload never happens.
        Every body this app stages is created that way, so the guard would break all
        of them rather than protecting any.
        """
        with storage.held_body(tmp_path, "body.bin", b"held") as (path, fd):
            with mock.patch.object(platform_compat, "open_file_no_reparse") as second_open:
                with mock.patch.object(storage, "_CAN_PASS_BODY_DESCRIPTOR", False):
                    with mock.patch.object(storage, "_checked", return_value=""):
                        with mock.patch.object(storage, "_assert_uploadable_handle"):
                            with contextlib.suppress(Exception):
                                storage.put_file(
                                    "p",
                                    "us-east-1",
                                    "b",
                                    "drive",
                                    "k",
                                    str(path),
                                    account=ACCOUNT,
                                    body_fd=fd,
                                    body_fd_denies_write=True,
                                )
            assert not second_open.called, (
                "a held body must be its own guard; a second deny-write open fails "
                "against the live write handle on Windows"
            )

    def test_an_unheld_descriptor_still_takes_the_guard(self, tmp_path):
        # The other direction, so the flag cannot become a blanket exemption: a
        # descriptor opened somewhere put_file cannot see has made no promise, and
        # dropping its guard would silently reintroduce the replaceable body.
        body = tmp_path / "plain.bin"
        body.write_bytes(b"plain")
        fd = os.open(body, os.O_RDONLY)
        try:
            # A DUPLICATE, not the same descriptor: `put_file` closes the guard it
            # takes, and handing it this test's own fd would have it closed twice.
            with mock.patch.object(
                platform_compat, "open_file_no_reparse", side_effect=lambda *a, **k: os.dup(fd)
            ) as second_open:
                with mock.patch.object(storage, "_CAN_PASS_BODY_DESCRIPTOR", False):
                    with mock.patch.object(storage, "_checked", return_value=""):
                        with mock.patch.object(storage, "_assert_uploadable_handle"):
                            with mock.patch.object(storage, "_assert_same_open_file"):
                                with contextlib.suppress(Exception):
                                    storage.put_file(
                                        "p",
                                        "us-east-1",
                                        "b",
                                        "drive",
                                        "k",
                                        str(body),
                                        account=ACCOUNT,
                                        body_fd=fd,
                                    )
            assert second_open.called, "an unheld descriptor must still take its guard"
        finally:
            os.close(fd)
