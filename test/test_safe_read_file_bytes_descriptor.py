"""Byte reads authorize the opened file before consuming its content."""

import os

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import hooks, platform_compat, portability


def _assert_closed(descriptors):
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def _read(kind, path):
    if kind == "text":
        return hooks.safe_read_file(str(path))
    if kind == "prefix":
        return hooks.safe_read_prefix(str(path), 4)
    if kind == "nolink":
        return hooks.safe_read_file_bytes_nolink(str(path))
    if kind == "identity":
        info = path.stat()
        return hooks.safe_read_file_bytes_with_identity(str(path), {(info.st_dev, info.st_ino)})
    return hooks.safe_read_file_bytes(str(path))


def _assert_refused(kind, path):
    if kind in {"text", "identity"}:
        with pytest.raises(PermissionError):
            _read(kind, path)
    else:
        assert _read(kind, path) is None


def _track_open(monkeypatch):
    descriptors = []
    real_open = platform_compat.open_file_no_reparse

    def capture(path, *, nonblocking=False):
        assert nonblocking
        fd = real_open(path, nonblocking=nonblocking)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", capture)
    return descriptors


def _forbid_content_read(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Refused descriptor reached the content reader")

    monkeypatch.setattr(hooks.os, "fdopen", forbidden)


@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink", "identity"])
def test_directory_swap_cannot_replace_the_authorized_file(tmp_path, monkeypatch, kind):
    original = tmp_path / "approved"
    original.mkdir()
    (original / "report.txt").write_bytes(b"approved content")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_bytes(b"outside content must not be read")
    moved = tmp_path / "moved"
    requested = original / "report.txt"
    real_open = platform_compat.open_file_no_reparse
    descriptors = []

    def swap_then_open(path, *, nonblocking=False):
        assert path == os.path.realpath(requested)
        assert nonblocking
        original.rename(moved)
        make_dir_link(original, outside)
        fd = real_open(path, nonblocking=nonblocking)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", swap_then_open)
    _forbid_content_read(monkeypatch)
    try:
        _assert_refused(kind, requested)
        _assert_closed(descriptors)
        assert (moved / "report.txt").read_bytes() == b"approved content"
        assert (outside / "report.txt").read_bytes() == b"outside content must not be read"
    finally:
        if platform_compat.is_link_or_junction(original):
            platform_compat.unlink_link_or_junction(original)


@pytest.mark.parametrize("failure", ["unknown_path", "sensitive_path", "nonregular"])
@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink"])
def test_unverifiable_or_refused_descriptor_is_closed_without_reading(
    tmp_path, monkeypatch, failure, kind
):
    source = tmp_path / "report.txt"
    source.write_bytes(b"unchanged")
    descriptors = _track_open(monkeypatch)
    _forbid_content_read(monkeypatch)
    if failure == "unknown_path":
        monkeypatch.setattr(hooks, "_fd_real_path", lambda _fd: None)
    elif failure == "sensitive_path":
        real_witness = hooks._fd_real_path

        def sensitive_after_open(fd):
            resolved = real_witness(fd)
            monkeypatch.setattr(hooks, "is_sensitive_path", lambda _path: True)
            return resolved

        monkeypatch.setattr(hooks, "_fd_real_path", sensitive_after_open)
    else:
        monkeypatch.setattr(hooks._stat, "S_ISREG", lambda _mode: False)
    _assert_refused(kind, source)
    _assert_closed(descriptors)


@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink", "identity"])
def test_ordinary_authorized_reads_remain_available(tmp_path, monkeypatch, kind):
    directory = tmp_path / "files"
    directory.mkdir()
    payload = b"raw\r\nbytes\x00\x1a"
    (directory / "report.bin").write_bytes(payload)
    path = directory / "report.bin"
    descriptors = _track_open(monkeypatch)
    expected = payload[:4] if kind == "prefix" else payload
    if kind == "text":
        expected = payload.decode().replace("\r\n", "\n")
    assert _read(kind, path) == expected
    _assert_closed(descriptors)


@requires_symlinks
@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink", "identity"])
def test_preexisting_benign_leaf_link_still_resolves(tmp_path, kind):
    source = tmp_path / "source.txt"
    source.write_bytes(b"safe")
    link = tmp_path / "alias.txt"
    link.symlink_to(source)
    try:
        assert _read(kind, link) == ("safe" if kind == "text" else b"safe")
    finally:
        link.unlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO open contract")
@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink", "identity"])
def test_fifo_refusal_never_waits_for_a_writer(tmp_path, monkeypatch, kind):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    real_open = os.open
    canonical = os.path.realpath(fifo)

    def require_nonblocking(path, flags, *args, **kwargs):
        if os.fspath(path) == canonical:
            # A regression must fail before a blocking FIFO open can hang CI.
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", require_nonblocking)
    descriptors = _track_open(monkeypatch)
    _forbid_content_read(monkeypatch)
    _assert_refused(kind, fifo)
    _assert_closed(descriptors)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO open contract")
@pytest.mark.parametrize("reader", ["copy", "internal", "export"])
def test_remaining_nonregular_admissions_never_wait_for_a_writer(tmp_path, monkeypatch, reader):
    if reader == "internal":
        home = tmp_path / "home"
        parent = home / ".aws" / "sso" / "cache"
        parent.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        relative = ".aws/sso/cache/probe.pipe"
        monkeypatch.setitem(hooks._INTERNAL_READ_ALLOWLIST, "fifo.probe", relative)
        fifo = home / relative
    else:
        fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    canonical = os.path.realpath(fifo)
    real_open = os.open
    opened_flags = []

    def require_nonblocking(path, flags, *args, **kwargs):
        if os.fspath(path) == canonical:
            # Fail before a blocking FIFO open can hang the test worker.
            assert flags & os.O_NONBLOCK
            opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", require_nonblocking)
    if reader == "copy":
        destination = tmp_path / "copies"
        destination.mkdir()
        assert hooks.safe_copy_file_nolink(str(fifo), str(destination)) is None
        assert list(destination.iterdir()) == []
    elif reader == "internal":
        outcomes = []
        monkeypatch.setattr(
            hooks,
            "_emit_internal_read_audit",
            lambda read_id, outcome: outcomes.append((read_id, outcome)) or True,
        )
        assert hooks.safe_read_file_internal("fifo.probe") is None
        assert outcomes == [("fifo.probe", "not_regular")]
    else:
        assert portability._open_verified(str(fifo), os.path.realpath(tmp_path)) is None
    assert len(opened_flags) == 1


def test_size_refusal_still_closes_the_verified_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "report.txt"
    source.write_bytes(b"too large")
    descriptors = _track_open(monkeypatch)
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 4)
    with pytest.raises(hooks.FileTooLargeError):
        hooks.safe_read_file_bytes(str(source))
    _assert_closed(descriptors)
