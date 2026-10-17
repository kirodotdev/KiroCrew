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


@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink"])
def test_case_alias_reads_follow_filesystem_identity(tmp_path, kind):
    """Case-sensitive volumes refuse a missing alias; insensitive ones read it."""
    directory = tmp_path / "CasePack"
    directory.mkdir()
    source = directory / "Report.txt"
    source.write_bytes(b"safe")
    alias = tmp_path / "casepack" / "report.TXT"
    if alias.exists():
        assert os.path.samestat(source.stat(), alias.stat())
        assert _read(kind, alias) == ("safe" if kind == "text" else b"safe")
        assert (
            hooks.safe_read_file_bytes_nolink(str(alias), within_root=str(alias.parent)) == b"safe"
        )
    elif kind == "text":
        with pytest.raises(FileNotFoundError):
            _read(kind, alias)
    else:
        assert _read(kind, alias) is None


@pytest.fixture
def darwin_spelling(tmp_path, monkeypatch):
    """Simulate kernel spelling/platform, keeping real descriptors and inodes."""
    from types import SimpleNamespace

    from kiro_crew import pinned_fs

    directory = tmp_path / "casepack"
    directory.mkdir()
    source = directory / "report.txt"
    source.write_bytes(b"safe")
    real_witness = hooks._fd_real_path

    def kernel_spelling(fd):
        path = real_witness(fd)
        return path.replace("casepack", "CasePack") if path else None

    monkeypatch.setattr(hooks, "sys", SimpleNamespace(**{**vars(hooks.sys), "platform": "darwin"}))
    monkeypatch.setattr(hooks, "_fd_real_path", kernel_spelling)
    # Windows cannot perform a POSIX walk. Substitute only that platform seam;
    # its own no-reparse opener still supplies a real file descriptor.
    if not pinned_fs.supports_pinned_walk():
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)

        def open_parent(parent, name, *, flags, mode, what, refusal):
            pin = pinned_fs.pin_parent(parent, what=what, refusal=refusal)
            try:
                return platform_compat.open_file_no_reparse(
                    os.path.join(parent, name), nonblocking=True
                )
            finally:
                os.close(pin)

        monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", open_parent)

        def pin_parent(path, **kwargs):
            if platform_compat.first_linked_ancestor(path) is not None:
                raise OSError("linked ancestor")
            return platform_compat.pin_directory(path)

        monkeypatch.setattr(pinned_fs, "pin_parent", pin_parent)
    descriptors = []
    real_open = pinned_fs.open_in_pinned_parent
    real_pin = pinned_fs.pin_parent

    def capture_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    def capture_pin(*args, **kwargs):
        fd = real_pin(*args, **kwargs)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", capture_open)
    monkeypatch.setattr(pinned_fs, "pin_parent", capture_pin)
    yield source
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink"])
def test_darwin_kernel_case_spelling_keeps_real_descriptor(darwin_spelling, kind):
    source = darwin_spelling
    assert _read(kind, source) == ("safe" if kind == "text" else b"safe")
    assert hooks.safe_read_file_bytes_nolink(str(source), within_root=str(source.parent)) == b"safe"


@pytest.mark.parametrize(
    "failure", ["different_inode", "missing", "no_walk", "leaf_link", "ancestor_link"]
)
def test_darwin_alias_requires_nofollow_identity(darwin_spelling, monkeypatch, failure):
    from kiro_crew import pinned_fs

    source = darwin_spelling.resolve()
    canonical = str(source)
    fd = platform_compat.open_file_no_reparse(source, nonblocking=True)
    moved = source.parent.with_name("moved")
    try:
        if failure == "different_inode":
            other = source.with_name("other.txt")
            other.write_bytes(b"other inode")
            canonical = str(other)
        elif failure == "missing":
            canonical = str(source.with_name("missing.txt"))
        elif failure == "no_walk":
            monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        elif failure == "leaf_link":
            # Pin the opener's link-refusal result; real directory links below
            # cover no-follow traversal without requiring Windows symlink rights.
            def refuse_leaf(*args, **kwargs):
                raise OSError("link at leaf")

            monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", refuse_leaf)
        else:
            os.close(fd)
            fd = -1
            source.parent.rename(moved)
            make_dir_link(source.parent, moved)
            fd = platform_compat.open_file_no_reparse(moved / source.name, nonblocking=True)
        assert not hooks._darwin_case_alias_matches(
            fd, canonical, canonical.replace("casepack", "CasePack")
        )
    finally:
        if fd >= 0:
            os.close(fd)
        if platform_compat.is_link_or_junction(source.parent):
            platform_compat.unlink_link_or_junction(source.parent)


@pytest.mark.parametrize(
    "failure", ["outside", "root_unknown", "root_swap", "sensitive", "hardlink", "oversize"]
)
def test_darwin_alias_keeps_read_boundaries(darwin_spelling, monkeypatch, failure):
    from kiro_crew import pinned_fs

    source = darwin_spelling
    root = source.parent
    if failure == "outside":
        root = root.with_name("casepack-other")
        root.mkdir()
    elif failure == "root_unknown":
        witness = hooks._fd_real_path
        monkeypatch.setattr(
            hooks,
            "_fd_real_path",
            lambda fd: None if hooks._stat.S_ISDIR(os.fstat(fd).st_mode) else witness(fd),
        )
    elif failure == "root_swap":
        real_pin = pinned_fs.pin_parent

        def refuse_root(path, **kwargs):
            if kwargs.get("what") == "read root":
                raise OSError("root became a link")
            return real_pin(path, **kwargs)

        monkeypatch.setattr(pinned_fs, "pin_parent", refuse_root)
    elif failure == "sensitive":
        real_sensitive = hooks.is_sensitive_path
        monkeypatch.setattr(
            hooks, "is_sensitive_path", lambda path: "CasePack" in path or real_sensitive(path)
        )
    elif failure == "hardlink":
        os.link(source, source.with_name("alias.txt"))
    else:
        with pytest.raises(hooks.FileTooLargeError):
            hooks.safe_read_file_bytes_nolink(str(source), within_root=str(root), max_bytes=3)
        return
    _forbid_content_read(monkeypatch)
    assert hooks.safe_read_file_bytes_nolink(str(source), within_root=str(root)) is None


def test_darwin_spelling_reaches_theme_destination_guard(darwin_spelling, tmp_path, monkeypatch):
    import json

    from kiro_crew.dashboard import theme_validate
    from kiro_crew.dashboard.handlers import themes

    monkeypatch.setattr(theme_validate, "config_dir", lambda: tmp_path / "cfg")
    themes_root = theme_validate._themes_dir()
    themes_root.mkdir(parents=True)
    destination = themes_root / "casepack"
    darwin_spelling.parent.rename(destination)
    source = destination / "subpack"
    source.mkdir()
    manifest = {"name": "casepack", "level": 0, "formatVersion": 1}
    variables = {
        mode: {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"}
        for mode in ("dark", "light")
    }
    (source / "theme.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "variables.json").write_text(json.dumps(variables), encoding="utf-8")
    before = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    theme, error, status = themes._do_install("local", {"path": str(source)})

    assert theme is None and status == 400
    assert error is not None and "inside the install destination" in error
    assert {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == before
    assert list(themes_root.iterdir()) == [destination]


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize(
    "checkpoint",
    [
        "file",
        pytest.param(
            "parent",
            marks=pytest.mark.skipif(
                not platform_compat.IS_POSIX, reason="POSIX directory-fd replacement"
            ),
        ),
    ],
)
def test_darwin_replace_checks_both_containment_witnesses(
    darwin_spelling, monkeypatch, verified, checkpoint
):
    import hashlib

    source = darwin_spelling.resolve()
    witness = hooks._fd_real_path

    def spelling(fd):
        value = witness(fd)
        is_dir = hooks._stat.S_ISDIR(os.fstat(fd).st_mode)
        if checkpoint == "parent" and not is_dir:
            return value.replace("CasePack", "casepack") if value else None
        return value

    # Each half must independently admit a kernel-spelled alias of the root.
    # The root witness follows the selected half's spelling as well.
    if checkpoint == "parent":
        monkeypatch.setattr(hooks, "_fd_real_path", spelling)
    if verified:
        result = hooks.verified_replace_file_nolink(
            str(source),
            "updated",
            hashlib.sha256(b"safe").hexdigest(),
            max_bytes=32,
            within_root=str(source.parent),
        )
        assert result == "ok"
    else:
        assert hooks.safe_write_file_nolink(str(source), "updated", str(source.parent))
    assert source.read_bytes() == b"updated"
    assert list(source.parent.iterdir()) == [source]


#: Windows ``normcase`` folds case lexically, so a case-only spelling difference
#: is admitted before the macOS pinned walk and that walk is never reached there.
_NORMCASE_FOLDS_CASE = os.path.normcase("A") != "A"


def _outbox_request(monkeypatch, source):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import files

    monkeypatch.setattr(files.config_loader, "outbox_dir", lambda: source.parent)
    monkeypatch.setattr(files, "_sel", MagicMock())
    monkeypatch.setattr(files, "redact", lambda text: text)
    request = MagicMock()
    request.app = {"state": MagicMock(_slots={})}
    request.headers = {}
    request.match_info = {"filename": source.name}
    monkeypatch.setattr(
        files,
        "read_bounded_json",
        AsyncMock(return_value=({"path": str(source), "filename": source.name}, None)),
    )
    return request


def _install_barrier(monkeypatch, checkpoint, source, pause):
    """Pause the reader at one filesystem step every platform performs.

    ``resolve`` pauses the request-path resolution. ``witness`` pauses the
    kernel-path witness of the opened regular file, then records whether the
    macOS pinned walk ran and on which thread.
    """
    from pathlib import Path

    from kiro_crew import pinned_fs

    walk_threads = []
    if checkpoint == "resolve":
        real_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == source:
                pause()
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        return walk_threads
    real_witness = hooks._fd_real_path

    def file_witness(fd):
        if hooks._stat.S_ISREG(os.fstat(fd).st_mode):
            pause()
        return real_witness(fd)

    real_open = pinned_fs.open_in_pinned_parent

    def open_witness(*args, **kwargs):
        import threading

        walk_threads.append(threading.get_ident())
        return real_open(*args, **kwargs)

    monkeypatch.setattr(hooks, "_fd_real_path", file_witness)
    monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", open_witness)
    return walk_threads


def _assert_pinned_walk(walk_threads, loop_thread, checkpoint):
    if checkpoint != "witness":
        return
    if _NORMCASE_FOLDS_CASE:
        assert walk_threads == [], "lexical case folding must precede the pinned walk"
    else:
        assert walk_threads, "the case alias must reach the pinned walk"
        assert loop_thread not in walk_threads


@pytest.mark.parametrize("endpoint", ["notify", "download"])
@pytest.mark.parametrize("checkpoint", ["resolve", "witness"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_outbox_reader_keeps_loop_live_and_closes_after_cancel(
    darwin_spelling, monkeypatch, endpoint, checkpoint, cancel
):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from kiro_crew.dashboard.handlers import files

    source = darwin_spelling.resolve()
    request = _outbox_request(monkeypatch, source)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    descriptors = _track_open(monkeypatch)

    def pause():
        # Fail before blocking if the syscall reaches the event-loop thread.
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "test did not release the filesystem worker"

    walk_threads = _install_barrier(monkeypatch, checkpoint, source, pause)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(files.executors, "path_transfer_executor", lambda: pool)
        task = asyncio.create_task(getattr(files, f"api_outbox_{endpoint}")(request))
        handshake = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                [task, handshake], timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                pytest.fail("reader completed before the filesystem handshake")
            assert handshake in done
            heartbeat = loop.create_future()
            loop.call_soon(heartbeat.set_result, True)
            assert await asyncio.wait_for(heartbeat, 10)
            assert not task.done()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            release.set()
            if not cancel:
                response = await asyncio.wait_for(task, 10)
                assert response.status == 200
                if endpoint == "download":
                    assert response.body == b"safe"
                _assert_pinned_walk(walk_threads, loop_thread, checkpoint)
        finally:
            release.set()
            handshake.cancel()
            await asyncio.gather(handshake, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)
            # A cancelled HTTP task does not own the worker's descriptors.
            # This serial-pool barrier proves that worker cleanup has completed.
            await asyncio.wait_for(asyncio.wrap_future(pool.submit(lambda: None)), 10)
    _assert_closed(descriptors)


@pytest.mark.parametrize("endpoint", ["notify", "download"])
@pytest.mark.parametrize("checkpoint", ["resolve", "witness"])
@pytest.mark.asyncio
async def test_on_loop_outbox_reader_trips_the_barrier(
    darwin_spelling, monkeypatch, endpoint, checkpoint
):
    """Negative control: a reader run on the event loop fails the same barrier."""
    import threading

    from kiro_crew.dashboard.handlers import files

    source = darwin_spelling.resolve()
    request = _outbox_request(monkeypatch, source)
    loop_thread = threading.get_ident()
    reached = []

    def pause():
        reached.append(threading.get_ident())
        assert threading.get_ident() != loop_thread

    _install_barrier(monkeypatch, checkpoint, source, pause)

    async def inline_probe(fn, *args, transfer=False):
        return fn(*args)

    monkeypatch.setattr(files, "_run_path_probe", inline_probe)
    with pytest.raises(AssertionError):
        await getattr(files, f"api_outbox_{endpoint}")(request)
    assert reached == [loop_thread]


@pytest.mark.parametrize("kind", ["bytes", "prefix", "text", "nolink"])
def test_normalization_only_alias_is_refused(darwin_spelling, monkeypatch, kind):
    """The macOS exception is exact case only: NFC and NFD spellings stay refused."""
    import unicodedata

    from kiro_crew import pinned_fs

    source = darwin_spelling.with_name(unicodedata.normalize("NFC", "r\u00e9port.txt"))
    darwin_spelling.rename(source)
    decomposed = unicodedata.normalize("NFD", source.name)
    assert decomposed != source.name
    real_witness = hooks._fd_real_path

    def nfd_spelling(fd):
        path = real_witness(fd)
        return path.replace(source.name, decomposed) if path else None

    walks = []
    real_open = pinned_fs.open_in_pinned_parent

    def record_walk(*args, **kwargs):
        walks.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(hooks, "_fd_real_path", nfd_spelling)
    monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", record_walk)
    _forbid_content_read(monkeypatch)
    _assert_refused(kind, source)
    assert walks == [], "a normalization-only alias is not a case-fold candidate"


@pytest.mark.parametrize("verified", [False, True])
def test_case_alias_replace_follows_filesystem_identity(tmp_path, verified):
    import hashlib

    root = tmp_path / "CasePack"
    root.mkdir()
    source = root / "Report.txt"
    source.write_bytes(b"safe")
    alias = tmp_path / "casepack" / "report.TXT"
    exists = alias.exists()
    if exists:
        assert os.path.samestat(source.stat(), alias.stat())
    if verified:
        result = hooks.verified_replace_file_nolink(
            str(alias),
            "updated",
            hashlib.sha256(b"safe").hexdigest(),
            max_bytes=32,
            within_root=str(alias.parent),
        )
        assert result == ("ok" if exists else "refused")
    else:
        assert hooks.safe_write_file_nolink(str(alias), "updated", str(alias.parent)) == exists
    assert source.read_bytes() == (b"updated" if exists else b"safe")


@pytest.mark.parametrize(
    "checkpoint",
    [
        "file",
        pytest.param(
            "parent",
            marks=pytest.mark.skipif(
                not platform_compat.IS_POSIX, reason="POSIX directory-fd replacement"
            ),
        ),
    ],
)
@pytest.mark.parametrize(
    "failure", ["outside", "root_unknown", "root_link", "sensitive", "hardlink"]
)
def test_darwin_replace_refusal_preserves_original(
    darwin_spelling, monkeypatch, checkpoint, failure
):
    from kiro_crew import pinned_fs

    source = darwin_spelling.resolve()
    root = source.parent
    witness = hooks._fd_real_path
    pins = []
    real_pin = pinned_fs.pin_parent

    def pin(path, **kwargs):
        if failure == "root_link":
            raise OSError("root became a link")
        fd = real_pin(path, **kwargs)
        pins.append(fd)
        return fd

    def spelling(fd):
        value = witness(fd)
        if fd in pins and failure == "root_unknown":
            return None
        if checkpoint == "parent" and not hooks._stat.S_ISDIR(os.fstat(fd).st_mode):
            return value.replace("CasePack", "casepack") if value else None
        return value

    monkeypatch.setattr(pinned_fs, "pin_parent", pin)
    monkeypatch.setattr(hooks, "_fd_real_path", spelling)
    if failure == "outside":
        root = root.with_name("casepack-other")
        root.mkdir()
    elif failure == "sensitive":
        real_sensitive = hooks.is_sensitive_path
        monkeypatch.setattr(
            hooks, "is_sensitive_path", lambda path: "CasePack" in path or real_sensitive(path)
        )
    elif failure == "hardlink":
        os.link(source, source.with_name("alias.txt"))
    before = {p.name: p.read_bytes() for p in source.parent.iterdir()}
    assert not hooks.safe_write_file_nolink(str(source), "must not land", str(root))
    assert {p.name: p.read_bytes() for p in source.parent.iterdir()} == before


@pytest.mark.parametrize("endpoint", ["notify", "download"])
@pytest.mark.parametrize("failure", ["busy", "malformed", "unreadable", "oversize"])
@pytest.mark.asyncio
async def test_outbox_offload_preserves_refusals(monkeypatch, endpoint, failure):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import executors
    from kiro_crew.dashboard.handlers import files

    request = MagicMock()
    request.app = {"state": MagicMock(_slots={})}
    request.headers = {}
    request.match_info = {"filename": "report.txt"}
    monkeypatch.setattr(files, "_sel", MagicMock())
    monkeypatch.setattr(
        files,
        "read_bounded_json",
        AsyncMock(return_value=({"path": "report.txt", "filename": "report.txt"}, None)),
    )
    errors = {
        "busy": executors.CronQueueTimeout(2),
        "malformed": ValueError("malformed path"),
        "unreadable": OSError("unreadable path"),
        "oversize": hooks.FileTooLargeError("file exceeds limit"),
    }
    monkeypatch.setattr(executors, "run_in_cron_pool", AsyncMock(side_effect=errors[failure]))
    handler = getattr(files, f"api_outbox_{endpoint}")
    if failure in {"malformed", "unreadable"}:
        with pytest.raises(type(errors[failure])):
            await handler(request)
    else:
        response = await handler(request)
        assert response.status == (503 if failure == "busy" else 413)
