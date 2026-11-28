"""The verified download (seam level 20): one writer per destination folder.

What is pinned, one guarantee per test group:

* the names ship at level 20, ``ctx.verified_download`` and the backend SDK spelling
  are one call, and the backend SDK one takes no app name;
* one writer: a fetch into a folder another fetch holds is refused before the
  transport is opened, a fetch into another folder is not, and the lock is released
  and kept on disk afterwards;
* staging is created exclusively: a file already at the staging name is refused and
  left as it was, and a ``<dest>.part`` beside the destination is never touched;
* cleanup removes only what the fetch created: a name the fetch did not create, or
  one swapped out from under it, is left in place;
* verify before visible: a digest or size mismatch leaves the destination's old file
  as it was (same bytes, same inode) and no staging file behind;
* containment: an absolute, ``..`` or reserved ``dest`` is refused before anything is
  opened, a link or junction anywhere on the way refuses the fetch with nothing
  written outside, while a link above the data directory is resolved once, and an
  empty pinned folder turned into a link after the pin refuses it too; the lock, the
  staging file before its rename and the installed file must each sit, by the
  kernel's answer for the open handle, in the folder that was checked;
* the network is the engine's own: the opener is built with the same-host redirect
  policy and a certificate-verifying context, and a failure names the host only.

No network: ``asset_downloader.build_opener`` is replaced per test with a fake opener
that serves bytes from memory and records how it was built.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import os
import ssl
import stat
import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import asset_downloader, pinned_fs, platform_compat
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps import download_sdk
from kiro_crew.apps.backend_sdk import download as sdk_download
from kiro_crew.apps.context import AppContext
from kiro_crew.testing.links import make_dir_link

#: The real opener factory, captured before any test replaces it.
_REAL_BUILD_OPENER = asset_downloader.build_opener

_PAYLOAD = bytes(range(256)) * 64
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_URL = "https://downloads.example.com/engine-1.4.2.tar.gz?sig=s3cr3t-query"
_O_BINARY = getattr(os, "O_BINARY", 0)


class _Response:
    """One body served from memory; *during* runs once, on the first read."""

    def __init__(self, data: bytes, during: Callable[[], None] | None) -> None:
        self._data = data
        self._during = during
        self.status = 200
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int) -> bytes:
        if self._during is not None:
            during, self._during = self._during, None
            during()
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes = _PAYLOAD,
    *,
    during: Callable[[], None] | None = None,
    fail: BaseException | None = None,
) -> SimpleNamespace:
    """Replace the engine's opener factory; record how it was built and what it served."""
    state = SimpleNamespace(built=[], urls=[])

    def _open(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        state.urls.append(request.full_url)
        if fail is not None:
            raise fail
        hook, state.during = state.during, None
        return _Response(payload, hook)

    def _build_opener(*args: object, **kwargs: object) -> SimpleNamespace:
        state.built.append((args, kwargs))
        return SimpleNamespace(open=_open)

    state.during = during
    monkeypatch.setattr(asset_downloader, "build_opener", _build_opener)
    return state


def _data_dir(tmp_path: Path, *folders: str) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for folder in folders:
        (data / folder).mkdir(parents=True)
    return data


def _ctx(data: Path) -> AppContext:
    return AppContext(name="demo-app", data_dir=data)


def _identity(path: Path) -> tuple[int, int]:
    return asset_downloader.file_identity(os.lstat(path))


# --------------------------------------------------------------------------- #
# The names and the two spellings
# --------------------------------------------------------------------------- #


def test_the_names_ship_at_seam_level_nineteen() -> None:
    for name in ("verified_download", "VerifiedDownloadError"):
        assert name in sdk.__all__ and name in dir(sdk), name
        assert sdk._EXPORT_LEVELS[name] == 20 <= sdk.SDK_LEVEL, name
    routed = {name for name, module in sdk._LAZY_EXPORTS.items() if module == "download"}
    assert routed == set(sdk_download.__all__)
    assert sdk.VerifiedDownloadError is download_sdk.VerifiedDownloadError
    assert issubclass(sdk.VerifiedDownloadError, OSError)
    assert callable(getattr(_ctx(Path(".")), "verified_download", None))


def test_the_backend_sdk_spelling_takes_no_app_name() -> None:
    assert list(inspect.signature(sdk.verified_download).parameters) == [
        "url",
        "dest",
        "sha256",
        "size",
    ]
    assert list(inspect.signature(AppContext.verified_download).parameters) == [
        "self",
        "url",
        "dest",
        "sha256",
        "size",
    ]


def test_ctx_installs_the_verified_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path, "vendor")
    state = _serve(monkeypatch)

    path = _ctx(data).verified_download(
        _URL, "vendor/engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD)
    )

    assert path == Path(os.path.realpath(data)) / "vendor" / "engine.tar.gz"
    assert path.read_bytes() == _PAYLOAD
    assert sorted(os.listdir(data / "vendor")) == [download_sdk.LOCK_NAME, "engine.tar.gz"]
    assert state.urls == [_URL]


def test_an_uppercase_digest_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path)
    _serve(monkeypatch)
    path = _ctx(data).verified_download(_URL, "engine", sha256=_SHA.upper(), size=len(_PAYLOAD))
    assert path.read_bytes() == _PAYLOAD


def test_the_backend_sdk_spelling_writes_the_spawned_apps_own_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.manager import app_data_dir

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_APP_NAME", "demo-app")
    _serve(monkeypatch)

    path = sdk.verified_download(_URL, "engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD))

    assert path == Path(os.path.realpath(app_data_dir("demo-app"))) / "engine.tar.gz"
    assert path.read_bytes() == _PAYLOAD


@pytest.mark.parametrize("name", [None, "", "  ", "../other-app", "Other App"])
def test_the_backend_sdk_spelling_needs_the_name_the_gateway_set(
    name: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if name is None:
        monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
    else:
        monkeypatch.setenv("KIROCREW_APP_NAME", name)
    state = _serve(monkeypatch)
    with pytest.raises(LookupError):
        sdk.verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))
    assert state.urls == []


def test_it_refuses_to_block_an_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path)
    state = _serve(monkeypatch)

    async def _on_the_loop() -> Path:
        return _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))

    with pytest.raises(RuntimeError, match="worker thread"):
        asyncio.run(_on_the_loop())
    assert state.urls == [] and os.listdir(data) == []


# --------------------------------------------------------------------------- #
# One writer per destination folder
# --------------------------------------------------------------------------- #


def test_a_fetch_into_a_folder_another_fetch_holds_is_refused_before_any_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path, "vendor")
    state = _serve(monkeypatch)
    lock_fd = platform_compat.open_create_or_existing(
        data / "vendor" / download_sdk.LOCK_NAME, os.O_RDWR, 0o600
    )
    try:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            with pytest.raises(download_sdk.VerifiedDownloadError, match="another verified"):
                _ctx(data).verified_download(
                    _URL, "vendor/engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD)
                )
    finally:
        os.close(lock_fd)
    assert state.urls == []
    assert os.listdir(data / "vendor") == [download_sdk.LOCK_NAME]


def test_a_second_fetch_during_a_transfer_is_refused_and_the_first_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path, "vendor", "other")
    refusals: list[BaseException] = []
    elsewhere: list[Path] = []

    def _while_the_first_streams() -> None:
        for dest in ("vendor/engine.tar.gz", "vendor/another.bin"):
            try:
                _ctx(data).verified_download(_URL, dest, sha256=_SHA, size=len(_PAYLOAD))
            except download_sdk.VerifiedDownloadError as exc:
                refusals.append(exc)
        # Another folder has its own lock, so a fetch there runs.
        elsewhere.append(
            _ctx(data).verified_download(
                _URL, "other/engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD)
            )
        )

    state = _serve(monkeypatch, during=_while_the_first_streams)
    path = _ctx(data).verified_download(
        _URL, "vendor/engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD)
    )

    assert path.read_bytes() == _PAYLOAD
    assert len(refusals) == 2
    assert all("another verified download" in str(exc) for exc in refusals)
    assert [p.read_bytes() for p in elsewhere] == [_PAYLOAD]
    assert len(state.urls) == 2  # the first fetch and the one into the other folder
    assert sorted(os.listdir(data / "vendor")) == [download_sdk.LOCK_NAME, "engine.tar.gz"]


def test_the_lock_is_released_and_kept_after_a_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    _serve(monkeypatch)
    ctx = _ctx(data)
    ctx.verified_download(_URL, "a.bin", sha256=_SHA, size=len(_PAYLOAD))
    lock = data / download_sdk.LOCK_NAME
    kept = _identity(lock)
    _serve(monkeypatch)
    ctx.verified_download(_URL, "b.bin", sha256=_SHA, size=len(_PAYLOAD))
    assert _identity(lock) == kept
    assert lock.stat().st_size == 0


# --------------------------------------------------------------------------- #
# Exclusive staging, and cleanup of only the fetch's own files
# --------------------------------------------------------------------------- #


def test_a_file_already_at_the_staging_name_is_refused_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    staging = ".0123456789abcdef0123456789abcdef" + download_sdk.STAGING_SUFFIX
    monkeypatch.setattr(download_sdk, "_staging_name", lambda: staging)
    planted = data / staging
    planted.write_bytes(b"theirs")
    before = _identity(planted)
    _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError):
        _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))

    assert planted.read_bytes() == b"theirs" and _identity(planted) == before
    assert not (data / "engine").exists()


@pytest.mark.parametrize("ok", [True, False])
def test_a_part_file_beside_the_destination_is_never_touched(
    ok: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    part = data / "engine.tar.gz.part"
    part.write_bytes(b"the app's own partial")
    before = _identity(part)
    _serve(monkeypatch)
    sha = _SHA if ok else "0" * 64

    if ok:
        _ctx(data).verified_download(_URL, "engine.tar.gz", sha256=sha, size=len(_PAYLOAD))
    else:
        with pytest.raises(download_sdk.VerifiedDownloadError):
            _ctx(data).verified_download(_URL, "engine.tar.gz", sha256=sha, size=len(_PAYLOAD))

    assert part.read_bytes() == b"the app's own partial" and _identity(part) == before


def test_cleanup_removes_only_a_file_this_fetch_created(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | _O_BINARY
    with platform_compat.pinned_directory(folder) as pin:
        where = download_sdk._OwnFiles(
            Path(pin.path), pin.fileno(), relative=platform_compat.IS_POSIX
        )
        (folder / "theirs").write_bytes(b"x")
        where.unlink("theirs")
        assert (folder / "theirs").read_bytes() == b"x"

        os.close(where.open("mine", create, 0o600))
        where.unlink("mine")
        assert not (folder / "mine").exists()

        # Moved away, and another writer's file put at the name: that file is not ours.
        os.close(where.open("swapped", create, 0o600))
        os.replace(folder / "swapped", folder / "moved")
        (folder / "swapped").write_bytes(b"foreign")
        where.discard_own("swapped")
        assert (folder / "swapped").read_bytes() == b"foreign"

        where.discard_own("absent")


# --------------------------------------------------------------------------- #
# Verified before it is visible
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sha256", "size"),
    [
        ("0" * 64, len(_PAYLOAD)),
        (_SHA, len(_PAYLOAD) + 1),
        (_SHA, len(_PAYLOAD) - 1),
    ],
    ids=["digest", "body-shorter-than-size", "body-longer-than-size"],
)
def test_a_mismatch_leaves_the_destination_as_it_was(
    sha256: str, size: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    dest = data / "engine.tar.gz"
    dest.write_bytes(b"the working copy")
    before = _identity(dest)
    _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError):
        _ctx(data).verified_download(_URL, "engine.tar.gz", sha256=sha256, size=size)

    assert dest.read_bytes() == b"the working copy" and _identity(dest) == before
    assert sorted(os.listdir(data)) == [download_sdk.LOCK_NAME, "engine.tar.gz"]


def test_a_transport_failure_names_the_host_only_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    _serve(monkeypatch, fail=urllib.error.URLError(ConnectionRefusedError()))

    with pytest.raises(download_sdk.VerifiedDownloadError) as caught:
        _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))

    message = str(caught.value)
    # The engine's own redaction: the scheme and the host, never the path or the query.
    assert f" from {asset_downloader.redact_url(_URL)}:" in message
    assert "s3cr3t" not in message and "engine-1.4.2" not in message
    assert os.listdir(data) == [download_sdk.LOCK_NAME]


# --------------------------------------------------------------------------- #
# Arguments, refused before anything is opened
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sha256": ""},
        {"sha256": "a" * 63},
        {"sha256": "z" * 64},
        {"sha256": 123},
        {"size": 0},
        {"size": -1},
        {"size": True},
        {"size": 1.5},
        {"size": "16384"},
        {"size": asset_downloader.DEFAULT_MAX_BYTES + 1},
        {"url": "http://downloads.example.com/engine"},
        {"url": "file:///etc/passwd"},
        {"url": None},
        {"dest": os.path.join(os.path.abspath(os.sep), "engine")},
        {"dest": "../engine"},
        {"dest": "vendor/../../engine"},
        {"dest": ""},
        {"dest": "."},
        {"dest": "\x00"},
        {"dest": download_sdk.LOCK_NAME},
        {"dest": download_sdk.LOCK_NAME.upper()},
        {"dest": "engine" + download_sdk.STAGING_SUFFIX},
        {"dest": "engine."},
        {"dest": "engine "},
        {"dest": 7},
    ],
)
def test_a_refused_argument_opens_nothing(
    kwargs: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    state = _serve(monkeypatch)
    call = {"url": _URL, "dest": "engine", "sha256": _SHA, "size": len(_PAYLOAD), **kwargs}

    with pytest.raises((ValueError, TypeError)):
        _ctx(data).verified_download(
            call["url"], call["dest"], sha256=call["sha256"], size=call["size"]
        )

    assert state.urls == [] and os.listdir(data) == []


# --------------------------------------------------------------------------- #
# Containment in the data directory
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dest", ["escape/engine", "vendor/inner/engine"])
def test_a_link_on_the_way_refuses_the_fetch_and_writes_nothing_outside(
    dest: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path, "vendor")
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    make_dir_link(data / "escape", outside)
    make_dir_link(data / "vendor" / "inner", outside / "inner")
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="real folder"):
        _ctx(data).verified_download(_URL, dest, sha256=_SHA, size=len(_PAYLOAD))

    assert state.urls == []
    assert os.listdir(outside) == ["inner"] and os.listdir(outside / "inner") == []


@pytest.mark.parametrize("dest", ["missing/engine", "a-file/engine"])
def test_a_missing_folder_or_a_file_on_the_way_is_refused(
    dest: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    (data / "a-file").write_bytes(b"not a folder")
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="real folder"):
        _ctx(data).verified_download(_URL, dest, sha256=_SHA, size=len(_PAYLOAD))

    assert state.urls == [] and sorted(os.listdir(data)) == ["a-file"]


def test_a_link_above_the_data_directory_is_resolved_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    (real / "data").mkdir(parents=True)
    make_dir_link(tmp_path / "alias", real)
    _serve(monkeypatch)

    path = _ctx(tmp_path / "alias" / "data").verified_download(
        _URL, "engine", sha256=_SHA, size=len(_PAYLOAD)
    )

    assert path == Path(os.path.realpath(real / "data")) / "engine"
    assert (real / "data" / "engine").read_bytes() == _PAYLOAD


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


@pytest.mark.parametrize(
    ("dest", "below"),
    [
        # An ancestor of the folder swapped: the folder's own name is real.
        ("vendor/sub/engine", "sub"),
        # The folder itself swapped.
        ("vendor/engine", ""),
    ],
    ids=["ancestor", "folder"],
)
def test_a_folder_swapped_for_a_link_before_the_pin_writes_nothing_outside(
    dest: str, below: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path, "vendor/sub")
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    victim = outside / below / "engine"
    victim.write_bytes(b"a file outside the data directory")
    before = _tree(outside)
    pin_folder = download_sdk._pin_folder

    def _swap_then_pin(
        stack: contextlib.ExitStack, root: str, folder: tuple[str, ...]
    ) -> platform_compat.PinnedDirectory:
        # After dest was checked and before anything is pinned: a junction on Windows,
        # a symlink elsewhere.
        os.rename(data / "vendor", data / "vendor-moved")
        make_dir_link(data / "vendor", outside)
        return pin_folder(stack, root, folder)

    monkeypatch.setattr(download_sdk, "_pin_folder", _swap_then_pin)
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="real folder"):
        _ctx(data).verified_download(_URL, dest, sha256=_SHA, size=len(_PAYLOAD))

    assert state.urls == []
    assert victim.read_bytes() == b"a file outside the data directory"
    assert _tree(outside) == before


def _turn_into_a_link_in_place(folder: Path, target: Path) -> None:
    """Redirect the pinned, empty *folder* to *target*, as another process could.

    Windows: a mount-point reparse point is set on the folder itself through a second
    handle (``FSCTL_SET_REPARSE_POINT``), which the pin's share mode does not refuse;
    the folder becomes a junction in place. POSIX has no in-place conversion, and a
    pin does not stop a rename, so the folder is moved away and a symlink takes its
    name.
    """
    if not platform_compat.IS_WINDOWS:
        os.rename(folder, folder.with_name(folder.name + "-moved"))
        folder.symlink_to(target, target_is_directory=True)
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        str(folder),
        0x40000000,  # GENERIC_WRITE
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        printed = str(target).encode("utf-16-le")
        names = substitute + b"\0\0" + printed + b"\0\0"
        # MountPointReparseBuffer: substitute name at 0, print name after its NUL.
        body = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
        body += names
        request = struct.pack("<IHH", 0xA0000003, len(body), 0) + body  # IO_REPARSE_TAG_MOUNT_POINT
        buffer = ctypes.create_string_buffer(request, len(request))
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle,
            0x000900A4,  # FSCTL_SET_REPARSE_POINT
            buffer,
            len(request),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    finally:
        kernel32.CloseHandle(handle)


def test_an_empty_pinned_folder_turned_into_a_link_writes_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path, "vendor")
    outside = tmp_path / "outside"
    outside.mkdir()
    pin_folder = download_sdk._pin_folder

    def _pin_then_redirect(
        stack: contextlib.ExitStack, root: str, folder: tuple[str, ...]
    ) -> platform_compat.PinnedDirectory:
        # The folder is pinned and still empty: the lock is the next thing made in it.
        pinned = pin_folder(stack, root, folder)
        _turn_into_a_link_in_place(data / "vendor", outside)
        return pinned

    monkeypatch.setattr(download_sdk, "_pin_folder", _pin_then_redirect)
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="checked"):
        _ctx(data).verified_download(_URL, "vendor/engine.tar.gz", sha256=_SHA, size=len(_PAYLOAD))

    assert state.urls == []
    assert os.listdir(outside) == []


def _elsewhere_for(monkeypatch: pytest.MonkeyPatch, matches: Callable[[int, str], bool]) -> None:
    """Make the kernel's answer for the handles *matches* picks name another folder."""
    real = pinned_fs.fd_real_path

    def _answer(fd: int) -> str | None:
        answer = real(fd)
        if answer is None or not matches(fd, os.path.basename(answer)):
            return answer
        return os.path.join(os.path.dirname(answer), "elsewhere", os.path.basename(answer))

    monkeypatch.setattr(pinned_fs, "fd_real_path", _answer)


_HANDLES: dict[str, Callable[[int, str], bool]] = {
    "folder": lambda fd, name: stat.S_ISDIR(os.fstat(fd).st_mode),
    "lock": lambda fd, name: name == download_sdk.LOCK_NAME,
    "staging": lambda fd, name: name.endswith(download_sdk.STAGING_SUFFIX),
    "dest": lambda fd, name: name == "engine",
}


@pytest.mark.parametrize("handle", sorted(_HANDLES))
def test_a_handle_outside_the_checked_folder_refuses_the_fetch(
    handle: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    _elsewhere_for(monkeypatch, _HANDLES[handle])
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="folder that was checked"):
        _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))

    # The folder and the lock are refused at the lock, before the transport is opened;
    # the staging file before its rename and the installed file after the install, and
    # each time this fetch's own file is gone and nothing else is.
    assert state.urls == ([] if handle in ("folder", "lock") else [_URL])
    assert os.listdir(data) == [download_sdk.LOCK_NAME]


def test_an_unreadable_handle_path_refuses_before_any_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    monkeypatch.setattr(pinned_fs, "fd_real_path", lambda fd: None)
    state = _serve(monkeypatch)

    with pytest.raises(download_sdk.VerifiedDownloadError, match="folder that was checked"):
        _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))

    assert state.urls == [] and os.listdir(data) == [download_sdk.LOCK_NAME]


# --------------------------------------------------------------------------- #
# The network is the engine's own
# --------------------------------------------------------------------------- #


def test_the_transfer_uses_the_engines_same_host_redirect_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data_dir(tmp_path)
    state = _serve(monkeypatch)
    _ctx(data).verified_download(_URL, "engine", sha256=_SHA, size=len(_PAYLOAD))
    assert state.built == [((), {"allow_cross_host_redirects": False})]


def test_that_opener_verifies_the_certificate_and_keeps_redirects_on_the_host() -> None:
    opener = _REAL_BUILD_OPENER(allow_cross_host_redirects=False)
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    context = https[0]._context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert any(isinstance(h, asset_downloader._SameHostRedirectHandler) for h in opener.handlers)
