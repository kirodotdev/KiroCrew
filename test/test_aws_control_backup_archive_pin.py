"""The backup archive binds its bytes to one descriptor: what it CHECKS is what it UPLOADS.

``test_aws_control_backup.py`` covers the two push paths with ``put_file`` mocked
out, so nothing there sees which file the upload would actually have opened. That
is the question these tests ask. The archive is staged in a directory a same-UID
process can write, so a NAME resolved once per step -- the entry-set fingerprint,
the size, the AWS CLI ``--body``, the body fingerprint -- is a separate answer per
step, and a process that replaces the file between two of them makes the upload
carry bytes nothing checked, with no recall once the object is sent.

The archive is opened ONCE through a checked descriptor
(``_open_pinned_archive_fd``: ``O_NOFOLLOW`` + ``S_ISREG`` + ``st_nlink == 1`` +
owner, with a Windows deny-write open) and the size, both fingerprints and the
upload body are read from THAT descriptor, so no step re-resolves the name. The
same inode pin is used on every platform the backup runs on -- Linux, macOS, the
BSDs, Windows.

So the whole-run tests here go through the REAL ``storage.put_file`` and stub the
subprocess chokepoint, reading the body the way the CLI child would. A swap
performed in the window immediately before that child resolves the body is what
separates a name-based upload from a descriptor-bound one.
"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.aws_control.backend import backup, storage

ACCOUNT = "111122223333"

#: The bytes a same-UID attacker plants over the staged archive just before upload.
PLANTED = b"SECRET-CREDENTIAL-BYTES-THAT-WERE-NEVER-CHECKED"


class _NoPread:
    """A stand-in ``os`` whose ``pread`` is removed, to exercise the fallback.

    ``_read_at`` prefers ``os.pread`` where it exists and falls back to a
    save/seek/read/restore otherwise. Windows has no ``pread``, so the fallback
    is a real production path and gets its own coverage here.
    """

    def __init__(self) -> None:
        self._real = os

    def __getattr__(self, name: str) -> Any:
        if name == "pread":
            raise AttributeError("pread")
        return getattr(self._real, name)


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
            if self.archive.exists():
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
    # Stage under the fixture's own tmp_path so nothing escapes into the real data
    # home. The narrowed design needs no sandbox mask to run -- the inode pin is the
    # hold, present on every platform -- so nothing here fakes a mask.
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
            # A platform without descriptor-pinned traversal refuses the sessions
            # backup outright. Assert the refusal rather than skip, so a regression
            # that let an unpinned walk through would redden.
            with pytest.raises(RuntimeError, match="openat|pinned|pin"):
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
        # The regression. On ``main``'s name-based upload the planted file is what
        # the CLI opens, so bytes nothing checked leave the host unrecallably. The
        # fix binds the upload to the descriptor opened before the swap, so the
        # planted bytes never leave.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # refusal already asserted
        assert swap.uploaded != PLANTED, "the planted bytes must not have been uploaded"
        assert swap.uploaded == swap.before_swap, "the uploaded bytes are the archive's"

    def test_the_uploaded_archive_still_holds_both_session_halves(
        self, _sessions_host, monkeypatch
    ):
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return
        names = _tar_member_prefixes(swap.uploaded)
        assert "crew" in names
        assert "cli" in names

    def test_the_recorded_fingerprint_describes_the_bytes_that_left(
        self, _sessions_host, monkeypatch, tmp_path
    ):
        # The body fingerprint the run records must be the digest of the bytes that
        # were actually uploaded -- both read the one pinned descriptor, so a swap
        # cannot make the record describe an object the bucket does not hold.
        swap = _SwapOnUpload()
        recorded: dict[str, str] = {}
        real_put = storage.put_file
        real_record = backup._record_run

        def watching_put(*args: Any, **kwargs: Any) -> str:
            if args[4].endswith(".tar.gz"):
                swap.note_archive(args[5])
            return real_put(*args, **kwargs)

        def capture_record(*args: Any, **kwargs: Any) -> Any:
            # positional (account, kind, key, size, body_fingerprint, version, ...)
            recorded["fingerprint"] = args[4]
            return real_record(*args, **kwargs)

        monkeypatch.setattr(storage, "_checked", swap)
        monkeypatch.setattr(backup.storage, "put_file", watching_put)
        monkeypatch.setattr(backup, "_record_run", capture_record)
        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup, "_publish_label"),
            mock.patch.object(backup, "_prune_remote_archives"),
        ):
            if not backup._CAN_PIN_TRAVERSAL:
                pytest.skip("sessions backup refuses without descriptor-pinned traversal")
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)
        # Assert against the product's own fingerprint of the uploaded bytes, so
        # the test states the invariant (the record describes what left) without
        # re-stating which algorithm the fingerprint uses.
        expected = tmp_path / "uploaded.bin"
        expected.write_bytes(swap.uploaded)
        assert recorded["fingerprint"] == backup._body_fingerprint(expected)


def _tar_member_prefixes(body: bytes) -> set[str]:
    import io

    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return {name.split("/", 1)[0] for name in tar.getnames() if name}


class TestPutFileBodySource:
    """What ``put_file`` sends: the name for a bare-path body, the descriptor for a
    handed-over ``body_fd``.

    The substitution refusals for the backup archive live on
    ``_open_pinned_archive_fd`` (``TestOpenPinnedArchiveFd``), which is where the
    archive callers pin the descriptor before handing it to ``put_file``. The bare
    path holds the body only by the name and is not covered here.
    """

    @pytest.fixture(autouse=True)
    def _no_network(self, _floor_monkeypatch):
        self.seen: dict[str, bytes] = {}

        def fake_checked(args, profile, *, action, timeout=30, extra_visible_dirs=(), **kw):
            self.seen["body"] = _body_the_child_would_read(args, kw.get("stdin_fd"))
            return "{}"

        _floor_monkeypatch.setattr(storage, "_checked", fake_checked)

    def test_a_bare_path_body_uploads_the_named_file(self, tmp_path):
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"the real bytes")
        storage.put_file("p", "r", "b", "backup", "k.tar.gz", str(f), account=ACCOUNT)
        assert self.seen["body"] == b"the real bytes"

    def test_a_handed_descriptor_uploads_the_bytes_the_descriptor_holds(self, tmp_path):
        # The archive path: the caller opens+checks the file and hands over body_fd.
        # The upload reads THAT descriptor, so a swap of the NAME after the open
        # cannot change what leaves.
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"descriptor bytes")
        fd = os.open(f, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            if platform_compat.IS_POSIX:
                # Swap the name after the descriptor is open: the descriptor still
                # reaches the original inode.
                f.unlink()
                f.write_bytes(PLANTED)
            storage.put_file(
                "p", "r", "b", "backup", "k.tar.gz", str(f), account=ACCOUNT, body_fd=fd
            )
        finally:
            os.close(fd)
        if platform_compat.IS_POSIX:
            assert self.seen["body"] == b"descriptor bytes"
            assert self.seen["body"] != PLANTED


class TestFingerprintsReadTheHeldInode:
    """Both fingerprints read the pinned descriptor at explicit offsets.

    A swap at the NAME after the descriptor is opened changes nothing the
    fingerprints see, because they never re-resolve the name.
    """

    def _pinned_fd(self, tmp_path, data: bytes) -> int:
        with tempfile.TemporaryDirectory(dir=tmp_path) as d:
            name = "archive.tar.gz"
            path = Path(d) / name
            with tarfile.open(path, "w:gz") as tar:
                blob = Path(d) / "m"
                blob.write_bytes(data)
                tar.add(blob, arcname="crew/m")
            dir_fd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                return backup._open_pinned_archive_fd(Path(d), dir_fd, name)
            finally:
                os.close(dir_fd)

    def test_the_body_digest_reads_the_descriptor_not_the_name(self, tmp_path):
        if not platform_compat.IS_POSIX:
            pytest.skip("dir_fd open is the POSIX arm")
        fd = self._pinned_fd(tmp_path, b"original")
        try:
            # The fd digest must be the product's digest of the bytes the fd holds.
            # Compare the two input modes of the one helper rather than restating
            # which algorithm it uses.
            same_bytes = tmp_path / "same.bin"
            same_bytes.write_bytes(_read_whole(fd))
            assert backup._body_fingerprint(fd=fd) == backup._body_fingerprint(same_bytes)
        finally:
            os.close(fd)

    def test_reading_from_a_descriptor_leaves_its_position_alone(self, tmp_path):
        if not platform_compat.IS_POSIX:
            pytest.skip("dir_fd open is the POSIX arm")
        fd = self._pinned_fd(tmp_path, b"original")
        try:
            os.lseek(fd, 7, os.SEEK_SET)
            backup._read_at(fd, 4, 0)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 7
        finally:
            os.close(fd)


class TestOffsetReadWhereThereIsNoPread:
    """``_read_at`` falls back to seek/read/restore where ``os.pread`` is absent."""

    def test_the_fallback_reads_the_same_bytes_at_an_offset(self, tmp_path, monkeypatch):
        f = tmp_path / "f"
        f.write_bytes(b"0123456789")
        fd = os.open(f, os.O_RDONLY)
        try:
            monkeypatch.setattr(backup, "os", _NoPread())
            assert backup._read_at(fd, 3, 4) == b"4567"[:3]
        finally:
            os.close(fd)

    def test_the_fallback_puts_the_callers_position_back(self, tmp_path, monkeypatch):
        f = tmp_path / "f"
        f.write_bytes(b"0123456789")
        fd = os.open(f, os.O_RDONLY)
        try:
            os.lseek(fd, 2, os.SEEK_SET)
            monkeypatch.setattr(backup, "os", _NoPread())
            backup._read_at(fd, 3, 6)
            assert _NoPread()._real.lseek(fd, 0, os.SEEK_CUR) == 2
        finally:
            os.close(fd)


class TestOpenPinnedArchiveFd:
    """The checks ``_open_pinned_archive_fd`` makes on the descriptor it returns."""

    def _dir(self, tmp_path):
        d = tmp_path / "staging"
        d.mkdir()
        dir_fd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        return d, dir_fd

    def test_a_link_planted_at_the_archive_name_is_refused(self, tmp_path):
        if not platform_compat.IS_POSIX:
            pytest.skip("O_NOFOLLOW symlink refusal is the POSIX arm")
        d, dir_fd = self._dir(tmp_path)
        try:
            secret = tmp_path / "secret"
            secret.write_bytes(b"owner-only")
            (d / "archive.tar.gz").symlink_to(secret)
            with pytest.raises(OSError):
                backup._open_pinned_archive_fd(d, dir_fd, "archive.tar.gz")
        finally:
            os.close(dir_fd)

    def test_a_hard_link_at_the_archive_name_is_refused(self, tmp_path):
        if not platform_compat.IS_POSIX:
            pytest.skip("st_nlink check is the POSIX arm")
        d, dir_fd = self._dir(tmp_path)
        try:
            secret = tmp_path / "secret"
            secret.write_bytes(b"owner-only")
            os.link(secret, d / "archive.tar.gz")
            with pytest.raises(ValueError):
                backup._open_pinned_archive_fd(d, dir_fd, "archive.tar.gz")
        finally:
            os.close(dir_fd)

    def test_an_ordinary_file_is_opened_and_is_a_regular_file(self, tmp_path):
        if not platform_compat.IS_POSIX:
            pytest.skip("dir_fd open is the POSIX arm")
        d, dir_fd = self._dir(tmp_path)
        try:
            (d / "archive.tar.gz").write_bytes(b"bytes")
            fd = backup._open_pinned_archive_fd(d, dir_fd, "archive.tar.gz")
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                assert info.st_nlink == 1
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)


class TestBackupRunsWithoutASealedMemfd:
    """Backup is NOT disabled on any platform.

    No capability predicate gates the
    sessions or snapshot kind on the presence of a sealable memfd or a sandbox mask:
    the inode pin works everywhere the backup already ran, so no platform loses the
    feature. These pin that contract.
    """

    def test_no_sealed_memfd_capability_predicate_remains(self):
        # No capability gate keys the kinds off a sealable memfd.
        assert not hasattr(storage, "can_hold_upload_body_from_creation")
        assert not hasattr(storage, "build_sealed_upload_body")

    def test_a_sessions_backup_completes_with_no_sealed_memfd(self, _sessions_host, monkeypatch):
        # Nothing fakes a sealable memfd or a sandbox mask: the run must still
        # complete and upload on this host, which is the no-regression guarantee.
        swap = _run_with_swap(monkeypatch)
        if swap is None:
            return  # a no-pinning platform refuses by design; asserted in the helper
        assert swap.uploaded, "the sessions backup must upload on a host with no sealed memfd"

    def test_a_snapshot_backup_is_not_gated_by_a_sandbox_mask(self, tmp_path, monkeypatch):
        # run_snapshot_backup does not refuse up front on an unmasked host.
        staging = tmp_path / "kc-aws-staging"
        staging.mkdir()
        monkeypatch.setattr(storage, "staging_root", lambda: staging)
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])

        def fake_snapshot(argv):
            out = Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz"
            with tarfile.open(out, "w:gz") as tar:
                blob = Path(argv[0]) / "m"
                blob.write_bytes(b"snap")
                tar.add(blob, arcname="snap/m")
            return 0

        uploaded: dict[str, bytes] = {}

        def fake_checked(args, profile, *, action, timeout=30, extra_visible_dirs=(), **kw):
            if "put-object" in args:
                uploaded["body"] = _body_the_child_would_read(args, kw.get("stdin_fd"))
            return "{}"

        with (
            mock.patch.object(backup, "snapshot_main", side_effect=fake_snapshot),
            mock.patch.object(backup.snapshot, "prepare_redacted_copy", return_value=None),
            mock.patch.object(storage, "_checked", fake_checked),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup, "_publish_label"),
            mock.patch.object(backup, "_prune_remote_archives"),
        ):
            record = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )
        assert record is not None
        assert uploaded.get("body"), "the snapshot backup must upload on an unmasked host"


class TestStagingRootIsPinnedBeforeMkdtemp:
    """``pinned_staging`` pins the staging ROOT before ``mkdtemp`` names a child.

    ``mkdtemp(dir=str(staging_root()))`` re-resolves the root by name. If the
    root is swapped for a link (or, on Windows, a reparse point to a remote
    share) between validation and this call, the child -- and the outbound
    access ``mkdtemp`` triggers -- lands under the planted target. Pinning the
    root first refuses a link at its name and blocks its rename for the scope,
    so the ordering is the whole guarantee and is what this pins.
    """

    def test_the_root_is_pinned_before_the_child_is_created(self, tmp_path, monkeypatch):
        root = tmp_path / "staging-root"
        root.mkdir()
        monkeypatch.setattr(storage, "staging_root", lambda: root)

        order: list[str] = []
        real_pin = storage.platform_compat.pin_directory
        real_mkdtemp = tempfile.mkdtemp

        def recording_pin(path):
            order.append(f"pin:{os.path.realpath(str(path))}")
            return real_pin(path)

        def recording_mkdtemp(*args, **kwargs):
            order.append("mkdtemp")
            return real_mkdtemp(*args, **kwargs)

        monkeypatch.setattr(storage.platform_compat, "pin_directory", recording_pin)
        monkeypatch.setattr(storage.tempfile, "mkdtemp", recording_mkdtemp)

        with storage.pinned_staging("probe-") as (path, dir_fd):
            assert dir_fd >= 0
            assert Path(path).is_dir()

        # The FIRST pin is the staging root, taken before mkdtemp names a child.
        assert order[0] == f"pin:{os.path.realpath(str(root))}", order
        assert order[1] == "mkdtemp", order
        # The child directory is pinned after it is created.
        assert order[2].startswith("pin:"), order

    def test_a_link_at_the_staging_root_name_is_refused(self, tmp_path, monkeypatch):
        if not platform_compat.IS_POSIX:
            pytest.skip("symlink-at-root refusal is asserted on the POSIX arm")
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        link_root = tmp_path / "link-root"
        try:
            link_root.symlink_to(real_root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("creating a symlink needs privilege on this host")
        monkeypatch.setattr(storage, "staging_root", lambda: link_root)

        with pytest.raises(Exception):
            with storage.pinned_staging("probe-"):
                pass
