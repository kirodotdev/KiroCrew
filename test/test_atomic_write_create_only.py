"""Create-only atomic publication never exposes an inode other than the staged one."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import pinned_fs, platform_compat

_HAS_PROC_SELF_FD = os.path.isdir("/proc/self/fd")


def _pinned_parent_or_skip(directory: Path) -> int:
    if not aw.pinned_parent_replace_supported() or not pinned_fs.supports_pinned_walk():
        pytest.skip("platform without descriptor-relative atomic writes")
    return pinned_fs.open_dir_pinned(str(directory), what="test directory")


def _hide_proc_self_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    real_isdir = os.path.isdir

    def _without_proc_self_fd(path) -> bool:
        if os.fspath(path) == "/proc/self/fd":
            return False
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _without_proc_self_fd)


def _without_nofollow_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os,
        "supports_follow_symlinks",
        set(os.supports_follow_symlinks) - {os.link},
    )


def _is_staging_unlink(
    candidate, kwargs: dict[str, object], *, directory: Path, dir_fd: int | None
) -> bool:
    """Whether an unlink call addresses this write's staging name."""
    name = os.fspath(candidate)
    call_dir_fd = kwargs.get("dir_fd")
    if dir_fd is None:
        return (
            call_dir_fd is None
            and os.path.dirname(name) == str(directory)
            and name.endswith(".tmp")
        )
    return call_dir_fd == dir_fd and os.path.dirname(name) == "" and name.endswith(".tmp")


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_retries_transient_staging_unlink_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, pinned: bool
) -> None:
    """A scanner releasing the staging name lets cleanup finish without an orphan."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    destination = tmp_path / "upload.txt"
    real_unlink = os.unlink
    attempts = 0
    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None

    def _contended_unlink(candidate, *args, **kwargs):
        nonlocal attempts
        if not _is_staging_unlink(candidate, kwargs, directory=tmp_path, dir_fd=dir_fd):
            assert os.path.isabs(os.fspath(candidate)) or kwargs.get("dir_fd") is not None
            return real_unlink(candidate, *args, **kwargs)
        attempts += 1
        if attempts <= 2:
            raise PermissionError(errno.EACCES, "scanner holds staging file")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _contended_unlink)
    try:
        with caplog.at_level("WARNING", logger=aw.__name__):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    assert attempts == 3
    assert destination.read_bytes() == b"uploaded bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())
    assert "leaving the orphan behind" not in caplog.text


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_bounds_persistent_staging_unlink_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, pinned: bool
) -> None:
    """Exhausted cleanup warns but cannot turn a committed create into failure."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    destination = tmp_path / "upload.txt"
    real_unlink = os.unlink
    attempts = 0
    orphan: Path | None = None
    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None

    def _contended_unlink(candidate, *args, **kwargs):
        nonlocal attempts, orphan
        if not _is_staging_unlink(candidate, kwargs, directory=tmp_path, dir_fd=dir_fd):
            assert os.path.isabs(os.fspath(candidate)) or kwargs.get("dir_fd") is not None
            return real_unlink(candidate, *args, **kwargs)
        attempts += 1
        orphan = tmp_path / os.path.basename(os.fspath(candidate))
        raise PermissionError(errno.EACCES, "scanner holds staging file")

    monkeypatch.setattr(os, "unlink", _contended_unlink)
    try:
        with caplog.at_level("WARNING", logger=aw.__name__):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
        assert attempts == aw._REPLACE_MAX_ATTEMPTS
        assert destination.read_bytes() == b"uploaded bytes"
        assert orphan is not None and orphan.exists()
        destination.unlink()
        assert orphan.read_bytes() == b"uploaded bytes"
        assert "leaving the orphan behind" in caplog.text
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        if orphan is not None and orphan.exists():
            real_unlink(orphan)


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_does_not_retry_nonretryable_staging_unlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, pinned: bool
) -> None:
    """A non-sharing OSError reaches the terminal warning after one attempt."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    destination = tmp_path / "upload.txt"
    real_unlink = os.unlink
    attempts = 0
    orphan: Path | None = None
    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None

    def _bad_unlink(candidate, *args, **kwargs):
        nonlocal attempts, orphan
        if not _is_staging_unlink(candidate, kwargs, directory=tmp_path, dir_fd=dir_fd):
            assert os.path.isabs(os.fspath(candidate)) or kwargs.get("dir_fd") is not None
            return real_unlink(candidate, *args, **kwargs)
        attempts += 1
        orphan = tmp_path / os.path.basename(os.fspath(candidate))
        raise OSError(errno.EISDIR, "not a retryable sharing violation")

    monkeypatch.setattr(os, "unlink", _bad_unlink)
    try:
        with caplog.at_level("WARNING", logger=aw.__name__):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
        assert attempts == 1
        assert destination.read_bytes() == b"uploaded bytes"
        assert orphan is not None and orphan.exists()
        assert "leaving the orphan behind" in caplog.text
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        if orphan is not None and orphan.exists():
            real_unlink(orphan)


def _install_staging_name_swap(
    tmp_path: Path, protected: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Path], list[OSError]]:
    real_link = os.link
    real_supports_follow_symlinks = set(os.supports_follow_symlinks)
    swapped: list[Path] = []
    swap_refused: list[OSError] = []

    def _swap_visible_temp_then_link(src, dst, *args, **kwargs):
        staging = [entry for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")]
        assert len(staging) == 1, f"expected one staging file, found {staging}"
        held_staging = staging[0].with_suffix(".attacker-held")
        try:
            staging[0].rename(held_staging)
        except OSError as rename_err:
            # Windows refuses to rename a file whose descriptor is open
            # (ERROR_SHARING_VIOLATION), so the by-name swap this helper
            # simulates is impossible there while atomic_write holds the
            # staging fd. Record the refusal and let publication proceed
            # untouched -- the sharing lock IS the platform's protection.
            swap_refused.append(rename_err)
            return real_link(src, dst, *args, **kwargs)
        staging[0].symlink_to(protected)
        swapped.append(staging[0])
        try:
            return real_link(src, dst, *args, **kwargs)
        finally:
            held_staging.unlink()

    monkeypatch.setattr(os, "link", _swap_visible_temp_then_link)
    if real_link in real_supports_follow_symlinks:
        monkeypatch.setattr(
            os,
            "supports_follow_symlinks",
            (real_supports_follow_symlinks - {real_link}) | {_swap_visible_temp_then_link},
        )
    return swapped, swap_refused


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_publishes_open_staging_inode_not_swapped_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """A staging-name swap follows the strongest primitive this host provides.

    Linux publishes from the still-open descriptor, so the replaced temp name
    cannot select the published inode. Without that descriptor namespace,
    publication uses the staging name and verifies it against the open fd; a
    mismatch refuses the write without deleting any destination the link made.
    """
    protected = tmp_path / "protected.txt"
    protected.write_bytes(b"protected bytes")
    destination = tmp_path / "upload.txt"
    payload = b"uploaded bytes"
    swapped, swap_refused = _install_staging_name_swap(tmp_path, protected, monkeypatch)

    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None
    refusal: FileExistsError | None = None
    try:
        try:
            aw.atomic_write(destination, payload, create_only=True, parent_dir_fd=dir_fd)
        except FileExistsError as err:
            refusal = err
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    assert swapped or swap_refused, "premise: the swap was attempted before publication"
    if swap_refused:
        # The OS refuses to rename a staging file whose descriptor is open
        # (Windows sharing semantics), so the by-name swap cannot occur and
        # publication proceeds with the staged bytes.
        assert refusal is None
        assert destination.read_bytes() == payload
        assert not destination.is_symlink()
    elif _HAS_PROC_SELF_FD:
        assert refusal is None
        assert destination.read_bytes() == payload
        assert not destination.is_symlink()
        assert os.stat(destination).st_ino != os.stat(protected).st_ino
    elif os.link in os.supports_follow_symlinks:
        # macOS no-follow publication creates the destination symlink object,
        # which verification refuses and deliberately leaves in place.
        assert refusal is not None and "staging name changed" in str(refusal)
        assert destination.is_symlink()
    else:
        # CreateHardLinkW refuses the replaced symlink source before creating
        # a destination; the staging-identity check translates that EACCES into
        # the same verification refusal rather than a false unsupported-volume
        # fallback.
        assert refusal is not None and "staging name changed" in str(refusal)
        assert not os.path.lexists(destination)
    assert protected.read_bytes() == b"protected bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_without_proc_uses_nofollow_publish_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """Without /proc, a normal no-follow publish verifies and succeeds."""
    if os.link not in os.supports_follow_symlinks:
        pytest.skip("platform without follow_symlinks=False hard links")
    _hide_proc_self_fd(monkeypatch)
    destination = tmp_path / "upload.txt"

    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None
    try:
        aw.atomic_write(
            destination,
            b"uploaded bytes",
            create_only=True,
            parent_dir_fd=dir_fd,
        )
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    assert destination.read_bytes() == b"uploaded bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_without_proc_rejects_swapped_staging_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """No-follow publication refuses a swapped staging name without deleting it."""
    if os.link not in os.supports_follow_symlinks:
        pytest.skip("platform without follow_symlinks=False hard links")
    _hide_proc_self_fd(monkeypatch)
    protected = tmp_path / "protected.txt"
    protected.write_bytes(b"protected bytes")
    destination = tmp_path / "upload.txt"
    swapped, _swap_refused = _install_staging_name_swap(tmp_path, protected, monkeypatch)

    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None
    try:
        with pytest.raises(FileExistsError, match="staging name changed"):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    assert swapped, "premise: the staging name was swapped before publication"
    # Do not read through destination: the unverified object is deliberately left
    # in place, and proving it is still a symlink proves protected bytes were not
    # published as a regular file at this name.
    assert destination.is_symlink()
    assert protected.read_bytes() == b"protected bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


def test_create_only_linkless_fallback_rejects_swapped_staging_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-link rename fallback verifies the inode it actually published."""
    dir_fd = _pinned_parent_or_skip(tmp_path)
    destination = tmp_path / "upload.txt"
    substitute = tmp_path / "substitute.txt"
    substitute.write_bytes(b"substituted bytes")
    held_staging = tmp_path / "attacker-held"
    swapped: list[str] = []

    def _link_unsupported(*args, **kwargs):
        raise OSError(errno.EPERM, "linkless filesystem")

    def _publish_swapped_staging(src, dst, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        assert src_dir_fd == dir_fd == dst_dir_fd
        os.rename(src, held_staging.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.rename(substitute.name, src, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        swapped.append(os.fspath(src))

    monkeypatch.setattr(os, "link", _link_unsupported)
    monkeypatch.setattr(platform_compat, "RENAME_NOREPLACE_AVAILABLE", True)
    monkeypatch.setattr(platform_compat, "rename_noreplace", _publish_swapped_staging)

    try:
        with pytest.raises(FileExistsError, match="staging name changed"):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
        assert swapped, "premise: the staging name was swapped before the fallback publish"
        assert held_staging.read_bytes() == b"uploaded bytes"
        assert destination.read_bytes() == b"substituted bytes"
    finally:
        os.close(dir_fd)
        held_staging.unlink(missing_ok=True)

    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.parametrize("pinned", [False, True], ids=["by-name", "pinned-parent"])
def test_create_only_verification_never_unlinks_a_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """A regular file replacing the destination before verification survives refusal."""
    if os.link not in os.supports_follow_symlinks:
        pytest.skip("platform without follow_symlinks=False hard links")
    _hide_proc_self_fd(monkeypatch)
    destination = tmp_path / "upload.txt"
    real_lstat = os.lstat
    replaced = False

    dir_fd = _pinned_parent_or_skip(tmp_path) if pinned else None

    def _replace_destination_before_verify(candidate, *args, **kwargs):
        nonlocal replaced
        candidate_name = os.fspath(candidate)
        is_destination = (
            candidate_name == str(destination)
            if dir_fd is None
            else candidate_name == destination.name and kwargs.get("dir_fd") == dir_fd
        )
        if is_destination and not replaced:
            replaced = True
            destination.unlink()
            destination.write_bytes(b"other writer")
        return real_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", _replace_destination_before_verify)
    try:
        with pytest.raises(FileExistsError, match="staging name changed"):
            aw.atomic_write(
                destination,
                b"uploaded bytes",
                create_only=True,
                parent_dir_fd=dir_fd,
            )
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    assert replaced, "premise: a concurrent writer replaced the destination before verification"
    assert destination.read_bytes() == b"other writer"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.skipif(
    not _HAS_PROC_SELF_FD,
    reason="This test simulates the Windows no-/proc link shape from Linux; "
    "hosts without /proc exercise that shape natively in the platform test above",
)
def test_create_only_without_proc_or_nofollow_uses_plain_link_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows capability shape publishes through a plain hard link and verifies."""
    _hide_proc_self_fd(monkeypatch)
    real_link = os.link
    calls: list[dict[str, object]] = []

    def _record_plain_link(src, dst, *args, **kwargs):
        calls.append(kwargs)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", _record_plain_link)
    _without_nofollow_link(monkeypatch)
    destination = tmp_path / "upload.txt"

    aw.atomic_write(destination, b"uploaded bytes", create_only=True)

    assert calls and all("follow_symlinks" not in kwargs for kwargs in calls)
    assert destination.read_bytes() == b"uploaded bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.skipif(
    not _HAS_PROC_SELF_FD,
    reason="This test simulates Windows ERROR_ACCESS_DENIED from Linux; "
    "Windows exercises the same staging-swap refusal natively in the platform test above",
)
def test_windows_access_denied_for_replaced_staging_name_is_a_verification_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced source is a verification refusal, not an unsupported NTFS volume."""
    _hide_proc_self_fd(monkeypatch)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    destination = tmp_path / "upload.txt"
    swapped: list[Path] = []

    def _replace_staging_then_deny(src, dst, *args, **kwargs):
        staging = [entry for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")]
        assert len(staging) == 1, f"expected one staging file, found {staging}"
        held_staging = staging[0].with_suffix(".attacker-held")
        staging[0].rename(held_staging)
        staging[0].write_bytes(b"replacement inode")
        swapped.append(staging[0])
        try:
            raise PermissionError(errno.EACCES, "CreateHardLinkW refused replaced source")
        finally:
            held_staging.unlink()

    monkeypatch.setattr(os, "link", _replace_staging_then_deny)

    with pytest.raises(FileExistsError, match="staging name changed"):
        aw.atomic_write(destination, b"uploaded bytes", create_only=True)

    assert swapped, "premise: the staging name changed before CreateHardLinkW refused it"
    assert not os.path.lexists(destination)
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


@pytest.mark.skipif(
    not _HAS_PROC_SELF_FD,
    reason="This test simulates the Windows no-/proc link shape from Linux; "
    "hosts without /proc exercise that shape natively in the platform test above",
)
def test_create_only_without_proc_or_nofollow_rejects_swap_without_unlinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows capability shape refuses an unverifiable swap without deleting it."""
    _hide_proc_self_fd(monkeypatch)
    protected = tmp_path / "protected.txt"
    protected.write_bytes(b"protected bytes")
    destination = tmp_path / "upload.txt"
    swapped, _swap_refused = _install_staging_name_swap(tmp_path, protected, monkeypatch)
    _without_nofollow_link(monkeypatch)

    with pytest.raises(FileExistsError, match="staging name changed"):
        aw.atomic_write(destination, b"uploaded bytes", create_only=True)

    assert swapped, "premise: the staging name was swapped before publication"
    assert os.path.lexists(destination)
    assert protected.read_bytes() == b"protected bytes"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())
