"""The verified download (seam level 18): ``ctx.verified_download`` and its SDK accessor.

What is pinned: the names and the context seam are level 18; every refusal (no or a
malformed sha256, a cap out of range, a url that is not https, a destination outside
the app's own data directory, a url the public-address vet refuses) happens before any
fetch; the transfer is the shared engine pinned to the vetted addresses, on the
same-host redirect policy with no proxy and no cross-host switch; the bytes install
only when they match, owner-only, inside the data directory; and the address pin
dials the approved addresses in turn and never the name. No test reaches the network.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import socket
import ssl
import stat
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import asset_downloader, link_unfurl, pinned_fs, platform_compat
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.download_sdk import VerifiedDownloadError, VerifiedDownloadSDK
from kiro_crew.testing.links import make_dir_link

_PAYLOAD = b"".join(bytes([i % 251]) for i in range(4096))
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_URL = "https://cdn.example.com/releases/tool-1.0.tar.gz"
_ADDRESSES = ("192.0.2.10", "192.0.2.11")
#: The SDK's staging name for ``tool.tar.gz``: a random token between the name and
#: the suffix.
_STAGING_RE = re.compile(r"\.tool\.tar\.gz\.[0-9a-f]{32}\.kc-dl")


def _vetted(url: str = _URL) -> link_unfurl.VettedUrl:
    return link_unfurl.VettedUrl(
        url=url,
        scheme="https",
        host="cdn.example.com",
        wire_host="cdn.example.com",
        port=443,
        ip=_ADDRESSES[0],
        addresses=_ADDRESSES,
        domain="cdn.example.com",
    )


class _Response:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.status = 200
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _MidBodyResponse(_Response):
    """Serves the body, but runs *during* first: a raise, or a call, on the second read.

    The staging file exists by then, so this is the shape of a transfer that stops (or
    is joined by another) part way through.
    """

    def __init__(self, data: bytes, during) -> None:  # noqa: ANN001 - exception or callable
        super().__init__(data)
        self._during = during
        self._reads = 0

    def read(self, n: int) -> bytes:
        self._reads += 1
        if self._reads == 2 and self._during is not None:
            during, self._during = self._during, None
            if isinstance(during, BaseException):
                raise during
            during()
        return super().read(n)


class _Interrupted(BaseException):
    """An exit that is not an ``Exception``, the shape an interrupt takes."""


@pytest.fixture
def network(monkeypatch):
    """The vet answers with :func:`_vetted`; the opener serves ``state.body`` or raises.

    ``state.during`` (an exception or a callable), when set, is used once: by the next
    response, on its second read.
    """
    state = SimpleNamespace(
        vetted=[], openers=[], requests=[], body=_PAYLOAD, error=None, during=None
    )

    def vet(url: str) -> link_unfurl.VettedUrl:
        state.vetted.append(url)
        return _vetted(url)

    def open_(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open signature
        state.requests.append(request.full_url)
        if state.error is not None:
            raise state.error
        if state.during is not None:
            during, state.during = state.during, None
            return _MidBodyResponse(state.body, during)
        return _Response(state.body)

    def opener(*args, **kwargs):
        state.openers.append((args, kwargs))
        return SimpleNamespace(open=open_)

    monkeypatch.setattr("kiro_crew.link_unfurl.vet_unfurl_url", vet)
    monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", opener)
    return state


@pytest.fixture
def downloads(tmp_path) -> tuple[VerifiedDownloadSDK, Path]:
    data = tmp_path / "apps" / "demo-app" / "data"
    data.mkdir(parents=True)
    return VerifiedDownloadSDK("demo-app", data), data


# ── the names and the seam ──────────────────────────────────────────────────


def test_the_names_are_level_eighteen_and_the_errors_are_the_sdk_class() -> None:
    for name in ("verified_download", "VerifiedDownloadError"):
        assert name in sdk.__all__ and name in dir(sdk), name
        assert sdk._EXPORT_LEVELS[name] == 18 <= sdk.SDK_LEVEL, name
    assert sdk.VerifiedDownloadError is VerifiedDownloadError


def test_ctx_verified_download_is_built_for_the_contexts_own_app(tmp_path) -> None:
    ctx = build_app_context("demo-app", tmp_path, permissions={})
    assert isinstance(ctx.verified_download, VerifiedDownloadSDK)
    assert ctx.verified_download.app_name == "demo-app"
    assert ctx.verified_download._data_dir == os.fspath(tmp_path)


def test_the_accessor_serves_the_spawned_apps_own_data_dir(monkeypatch) -> None:
    from kiro_crew.apps.manager_records import app_dir

    monkeypatch.setenv("KIROCREW_APP_NAME", "demo-app")
    downloader = sdk.verified_download()
    assert downloader.app_name == "demo-app"
    assert downloader._data_dir == os.fspath(app_dir("demo-app") / "data")


@pytest.mark.parametrize("value", [None, "", "   ", "../other-app", "Demo App"])
def test_the_accessor_refuses_without_a_gateway_set_app_name(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
    else:
        monkeypatch.setenv("KIROCREW_APP_NAME", value)
    with pytest.raises(LookupError):
        sdk.verified_download()


def test_the_accessor_takes_no_app_name() -> None:
    import inspect

    assert list(inspect.signature(sdk.verified_download).parameters) == []


def test_fetch_offers_no_redirect_hash_or_target_switch() -> None:
    import inspect

    parameters = inspect.signature(VerifiedDownloadSDK.fetch).parameters
    assert list(parameters) == ["self", "url", "dest", "sha256", "max_bytes", "on_progress"]
    assert parameters["sha256"].default is inspect.Parameter.empty
    assert parameters["max_bytes"].default is inspect.Parameter.empty


# ── refusals, before any fetch ──────────────────────────────────────────────


def _refused(downloads, network, error, **overrides) -> None:
    downloader, data = downloads
    call = {"url": _URL, "dest": "tool.tar.gz", "sha256": _SHA, "max_bytes": 1 << 20}
    call.update(overrides)
    with pytest.raises(error):
        downloader.fetch(call.pop("url"), call.pop("dest"), **call)
    assert network.vetted == [] and network.openers == [] and network.requests == []
    assert list(data.iterdir()) == []


@pytest.mark.parametrize("sha256", [None, "", "abc", _SHA[:-1], _SHA + "0", "g" * 64, 123])
def test_a_missing_or_malformed_sha256_is_refused(downloads, network, sha256) -> None:
    _refused(downloads, network, ValueError, sha256=sha256)


@pytest.mark.parametrize(
    "max_bytes", [0, -1, True, 1.5, "4096", None, asset_downloader.DEFAULT_MAX_BYTES + 1]
)
def test_a_cap_outside_its_range_is_refused(downloads, network, max_bytes) -> None:
    _refused(downloads, network, ValueError, max_bytes=max_bytes)


@pytest.mark.parametrize(
    "url", ["http://cdn.example.com/x", "file:///etc/passwd", "ftp://cdn.example.com/x", "", 7]
)
def test_a_url_that_is_not_https_is_refused(downloads, network, url) -> None:
    _refused(downloads, network, ValueError, url=url)


@pytest.mark.parametrize("dest", ["", ".", "..", "sub/..", "../escape.bin", "a\x00b"])
def test_a_destination_that_is_no_file_inside_the_data_dir_is_refused(
    downloads, network, dest
) -> None:
    _refused(downloads, network, ValueError, dest=dest)


def test_an_absolute_destination_outside_the_data_dir_is_refused(
    downloads, network, tmp_path
) -> None:
    _refused(downloads, network, ValueError, dest=str(tmp_path / "outside.bin"))


@pytest.mark.parametrize("dest", [7, None, b"tool.bin"])
def test_a_destination_that_is_not_a_path_is_refused(downloads, network, dest) -> None:
    _refused(downloads, network, TypeError, dest=dest)


def test_a_link_inside_the_data_dir_that_leads_out_is_refused(downloads, network, tmp_path) -> None:
    # A junction on Windows, a symlink elsewhere: the escape is refused on every OS.
    downloader, data = downloads
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_link(data / "vendor", outside)
    with pytest.raises(ValueError):
        downloader.fetch(_URL, "vendor/tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert network.vetted == [] and network.requests == []
    assert list(outside.iterdir()) == []


def test_a_non_callable_progress_hook_is_refused(downloads, network) -> None:
    _refused(downloads, network, TypeError, on_progress="not callable")


def test_a_url_the_vet_refuses_raises_the_gateway_class_and_fetches_nothing(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads

    def refuse(url: str) -> link_unfurl.VettedUrl:
        raise link_unfurl.UnfurlRejected("blocked_url")

    monkeypatch.setattr("kiro_crew.link_unfurl.vet_unfurl_url", refuse)
    with pytest.raises(sdk.UrlRejectedError) as raised:
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert raised.value.code == "blocked_url"
    assert network.openers == [] and list(data.iterdir()) == []


# ── the transfer ────────────────────────────────────────────────────────────


def test_a_matching_body_installs_inside_the_data_dir_over_a_pinned_connection(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    (data / "vendor").mkdir()
    progress: list[tuple[int, int]] = []
    locked: list[str] = []
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: locked.append(os.fspath(p)))
    got = downloader.fetch(
        _URL,
        "vendor/tool.tar.gz",
        sha256=_SHA.upper(),
        max_bytes=1 << 20,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert got == Path(os.path.realpath(data)) / "vendor" / "tool.tar.gz"
    assert got.read_bytes() == _PAYLOAD
    assert network.vetted == [_URL]
    assert network.requests == [_URL]
    ((args, kwargs),) = network.openers
    assert args == ()
    assert kwargs == {"pin": ("cdn.example.com", 443, _ADDRESSES)}
    assert progress and progress[-1][0] == len(_PAYLOAD)
    assert sorted(p.name for p in got.parent.iterdir()) == ["tool.tar.gz"]
    # The file is locked to the owner on every OS. POSIX does it through the pinned
    # directory descriptor (fchmod 0600). Windows has no descriptor-relative chmod, so
    # the owner-only DACL lockdown is asked for the staging file, which the install
    # rename carries to the final name; the DACL write itself is covered by the
    # platform_compat owner-only tests (the conftest stub skips it elsewhere).
    if platform_compat.IS_WINDOWS:
        (staging,) = [Path(p) for p in locked]
        assert staging.parent == got.parent and _STAGING_RE.fullmatch(staging.name)
    else:
        assert stat.S_IMODE(got.stat().st_mode) == 0o600


def test_an_absolute_destination_inside_the_data_dir_installs(downloads, network) -> None:
    downloader, data = downloads
    got = downloader.fetch(_URL, str(data / "tool.tar.gz"), sha256=_SHA, max_bytes=1 << 20)
    assert got.read_bytes() == _PAYLOAD


def test_a_mismatched_body_installs_nothing(downloads, network) -> None:
    downloader, data = downloads
    network.body = _PAYLOAD[:-1] + b"\x00"
    with pytest.raises(VerifiedDownloadError, match="sha256 mismatch"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert list(data.iterdir()) == []


def test_a_body_over_the_cap_is_abandoned(downloads, network) -> None:
    downloader, data = downloads
    with pytest.raises(VerifiedDownloadError, match="ceiling"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1024)
    assert list(data.iterdir()) == []


def test_a_refused_redirect_installs_nothing_and_names_the_hosts(downloads, network) -> None:
    downloader, data = downloads
    network.error = asset_downloader.RedirectRefused(
        _URL, "https://elsewhere.example/tool.tar.gz", "the transfer stays on its host"
    )
    with pytest.raises(VerifiedDownloadError) as raised:
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert "elsewhere.example" in raised.value.reason
    assert "/tool.tar.gz" not in raised.value.reason
    assert list(data.iterdir()) == []


# ── the pinned folder is the folder that was checked, on every platform ─────


def test_a_missing_folder_is_refused_and_nothing_is_created(downloads, network) -> None:
    _refused(downloads, network, ValueError, dest="newdir/tool.tar.gz")


@pytest.mark.parametrize(
    ("dest", "below"),
    [
        # An ancestor of the folder swapped: the pin's last component is real, so only
        # the handle's own path shows where it landed.
        ("vendor/sub/tool.tar.gz", "sub"),
        # The folder itself swapped: the pin refuses the link at its last component.
        ("vendor/tool.tar.gz", ""),
    ],
)
def test_a_folder_swapped_for_a_link_during_the_vet_writes_nothing_outside(
    monkeypatch, downloads, network, tmp_path, dest, below
) -> None:
    downloader, data = downloads
    (data / "vendor" / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    victim = outside / below / "tool.tar.gz"
    victim.write_bytes(b"a file outside the data directory")
    before = sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*"))

    def vet_then_swap(url: str) -> link_unfurl.VettedUrl:
        # After the destination was checked and before the folder is pinned: a junction
        # on Windows, a symlink elsewhere.
        os.rename(data / "vendor", data / "vendor-moved")
        make_dir_link(data / "vendor", outside)
        network.vetted.append(url)
        return _vetted(url)

    monkeypatch.setattr("kiro_crew.link_unfurl.vet_unfurl_url", vet_then_swap)
    with pytest.raises(VerifiedDownloadError):
        downloader.fetch(_URL, dest, sha256=_SHA, max_bytes=1 << 20)
    assert network.vetted == [_URL] and network.requests == []
    assert victim.read_bytes() == b"a file outside the data directory"
    assert sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*")) == before


def test_an_unreadable_folder_handle_path_refuses_before_any_request(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    monkeypatch.setattr("kiro_crew.pinned_fs.fd_real_path", lambda fd: None)
    with pytest.raises(VerifiedDownloadError, match="checked"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert network.requests == [] and list(data.iterdir()) == []


def test_a_staging_file_outside_the_pinned_folder_installs_nothing(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    real = pinned_fs.fd_real_path

    def elsewhere_for_files(fd: int) -> str | None:
        # The folder's handle answers truly; the staging file's says it is elsewhere.
        answer = real(fd)
        if answer is not None and stat.S_ISREG(os.fstat(fd).st_mode):
            return os.path.join(os.path.dirname(answer), "elsewhere", os.path.basename(answer))
        return answer

    monkeypatch.setattr("kiro_crew.pinned_fs.fd_real_path", elsewhere_for_files)
    with pytest.raises(VerifiedDownloadError, match="not in the pinned download directory"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert list(data.iterdir()) == []


# ── no file of the app's is touched but the destination ─────────────────────

#: Files an app may already keep beside its download: the engine's old staging name,
#: and one named like the SDK's own staging files.
_NEIGHBOURS = {
    "tool.tar.gz.part": b"the app's own partial download",
    ".tool.tar.gz.0123456789abcdef0123456789abcdef.kc-dl": b"named like a staging file",
}


def _plant_neighbours(data: Path) -> None:
    for name, body in _NEIGHBOURS.items():
        (data / name).write_bytes(body)


def _assert_only(data: Path, *extra: str) -> None:
    """Exactly the neighbours, byte for byte, plus *extra*: no staging file is left."""
    assert sorted(p.name for p in data.iterdir()) == sorted([*_NEIGHBOURS, *extra])
    for name, body in _NEIGHBOURS.items():
        assert (data / name).read_bytes() == body, name


@pytest.mark.parametrize("failure", ["digest", "cap", "transport", "redirect", "mid-body"])
def test_a_failed_fetch_touches_no_other_file(downloads, network, failure) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    max_bytes = 1 << 20
    if failure == "digest":
        network.body = _PAYLOAD[:-1] + b"\x00"
    elif failure == "cap":
        max_bytes = 1024
    elif failure == "transport":
        network.error = urllib.error.URLError("unreachable")
    elif failure == "redirect":
        network.error = asset_downloader.RedirectRefused(
            _URL, "https://elsewhere.example/tool.tar.gz", "the transfer stays on its host"
        )
    else:
        network.during = ConnectionResetError("the connection was reset")
    with pytest.raises(VerifiedDownloadError):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=max_bytes)
    _assert_only(data)


def test_an_interrupted_fetch_removes_only_its_own_staging_file(downloads, network) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    network.during = _Interrupted()
    with pytest.raises(_Interrupted):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    _assert_only(data)


def test_a_successful_fetch_touches_no_other_file(downloads, network) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    got = downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert got.read_bytes() == _PAYLOAD
    _assert_only(data, "tool.tar.gz")


def test_two_fetches_at_once_use_their_own_staging_files(downloads, network) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    during: list[list[str]] = []

    def second_fetch() -> None:
        # Runs while the first fetch holds its staging file, part way through its body.
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
        during.append(sorted(p.name for p in data.iterdir()))

    network.during = second_fetch
    got = downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert got.read_bytes() == _PAYLOAD
    (listing,) = during
    first_staging = [n for n in listing if _STAGING_RE.fullmatch(n) and n not in _NEIGHBOURS]
    assert len(first_staging) == 1, listing
    assert "tool.tar.gz" in listing
    _assert_only(data, "tool.tar.gz")


def test_the_engine_refuses_an_existing_exclusive_staging_name_and_keeps_it(
    network, tmp_path
) -> None:
    taken = tmp_path / ".x.bin.taken.kc-dl"
    taken.write_bytes(b"someone else's file")
    ok, reason = asset_downloader.download_to(
        tmp_path / "x.bin", _URL, sha256=_SHA, staging=taken, exclusive_staging=True
    )
    assert ok is False and "staging" in reason
    assert taken.read_bytes() == b"someone else's file"
    assert sorted(p.name for p in tmp_path.iterdir()) == [taken.name]


@pytest.mark.parametrize("options", [{}, {"resume": True}])
def test_exclusive_staging_needs_its_own_name_and_no_resume(network, tmp_path, options) -> None:
    staging = None if not options else tmp_path / ".x.bin.mine.kc-dl"
    ok, reason = asset_downloader.download_to(
        tmp_path / "x.bin",
        _URL,
        sha256=_SHA,
        staging=staging,
        exclusive_staging=True,
        **options,
    )
    assert ok is False and "exclusive staging" in reason
    assert network.requests == [] and list(tmp_path.iterdir()) == []


# ── every exit removes the staging file this transfer created ───────────────


class _FaultyWriter:
    """The real staging writer, with chosen calls failing as a full disk fails them.

    ``close`` releases the real descriptor first and then raises, as a buffered
    writer's close does when its flush fails.
    """

    def __init__(self, real, *, write=None, flush=None, close=None) -> None:  # noqa: ANN001
        self._real = real
        self._write, self._flush, self._close = write, flush, close

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def closed(self) -> bool:
        return self._real.closed

    def write(self, data: bytes) -> int:
        if self._write is not None:
            raise self._write
        return self._real.write(data)

    def flush(self) -> None:
        if self._flush is not None:
            raise self._flush
        self._real.flush()

    def close(self) -> None:
        self._real.close()
        if self._close is not None:
            raise self._close


def _faulty_staging(monkeypatch, **faults) -> None:
    real_create = asset_downloader._ExclusiveStaging.create
    monkeypatch.setattr(
        asset_downloader._ExclusiveStaging,
        "create",
        lambda self: _FaultyWriter(real_create(self), **faults),
    )


def _no_space(name: str) -> type[OSError]:
    """A distinct ``OSError`` subclass per call site, so the reason names the first one."""
    return type(name, (OSError,), {})


@pytest.mark.parametrize("first", ["write", "flush"])
def test_a_failed_write_or_flush_with_a_failing_close_leaves_no_staging_file(
    monkeypatch, downloads, network, first
) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    original = _no_space(f"{first.capitalize()}NoSpace")
    later = _no_space("CloseNoSpace")
    _faulty_staging(
        monkeypatch,
        **{first: original(errno.ENOSPC, "No space left on device")},
        close=later(errno.ENOSPC, "No space left on device"),
    )
    with pytest.raises(VerifiedDownloadError) as raised:
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert original.__name__ in raised.value.reason
    assert later.__name__ not in raised.value.reason
    _assert_only(data)


def test_a_failed_fsync_leaves_no_staging_file(monkeypatch, downloads, network) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    original = _no_space("SyncNoSpace")

    def failing_fsync(fd: int) -> None:
        raise original(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(asset_downloader, "_fsync", failing_fsync)
    with pytest.raises(VerifiedDownloadError) as raised:
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert original.__name__ in raised.value.reason
    _assert_only(data)


def test_a_failed_rename_leaves_no_staging_file(monkeypatch, downloads, network) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    original = _no_space("RenameNoSpace")

    def failing_replace(self, src: str, dst: str) -> None:  # noqa: ANN001
        raise original(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(asset_downloader.PinnedTargetDir, "replace", failing_replace)
    with pytest.raises(VerifiedDownloadError) as raised:
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    assert original.__name__ in raised.value.reason
    _assert_only(data)


def test_an_interrupt_during_close_still_removes_the_staging_file(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    _faulty_staging(
        monkeypatch,
        write=_no_space("WriteNoSpace")(errno.ENOSPC, "No space left on device"),
        close=_Interrupted(),
    )
    with pytest.raises(_Interrupted):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    _assert_only(data)


def test_a_refusal_right_after_the_create_removes_the_file_it_created(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    _plant_neighbours(data)

    def refuse(fd: int, target_dir, name: str) -> None:  # noqa: ANN001
        raise asset_downloader.StagingRefused("refused after the create")

    monkeypatch.setattr(asset_downloader, "_refuse_unless_named_file", refuse)
    with pytest.raises(VerifiedDownloadError, match="refused after the create"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    _assert_only(data)


def test_an_identity_refusal_after_the_rename_installs_nothing(
    monkeypatch, downloads, network
) -> None:
    downloader, data = downloads
    _plant_neighbours(data)
    real_install = asset_downloader.install_verified

    def install_unverified(where, staging, name, *, verified, restrict_to_owner):  # noqa: ANN001
        # The reopened final name is not the inode the digest was computed on.
        real_install(where, staging, name, verified=(0, 0), restrict_to_owner=restrict_to_owner)

    monkeypatch.setattr(asset_downloader, "install_verified", install_unverified)
    with pytest.raises(VerifiedDownloadError, match="replaced during the transfer"):
        downloader.fetch(_URL, "tool.tar.gz", sha256=_SHA, max_bytes=1 << 20)
    _assert_only(data)


# ── the address pin in the shared engine ────────────────────────────────────


def test_a_pinned_opener_keeps_the_same_host_policy_and_uses_no_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    opener = asset_downloader.build_opener(pin=("cdn.example.com", 443, _ADDRESSES))
    handlers = opener.handlers
    assert any(isinstance(h, asset_downloader._PinnedHTTPSHandler) for h in handlers)
    assert any(isinstance(h, asset_downloader._SameHostRedirectHandler) for h in handlers)
    assert not any(isinstance(h, asset_downloader._HttpsOnlyRedirectHandler) for h in handlers)
    proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies and all(h.proxies == {} for h in proxies)


def test_a_pinned_opener_refuses_cross_host_redirects() -> None:
    with pytest.raises(ValueError):
        asset_downloader.build_opener(
            pin=("cdn.example.com", 443, _ADDRESSES), allow_cross_host_redirects=True
        )


def test_the_engine_refuses_a_pin_with_cross_host_redirects_before_opening(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "kiro_crew.asset_downloader.build_opener", lambda *a, **k: pytest.fail("opened")
    )
    ok, reason = asset_downloader.download_to(
        tmp_path / "x.bin",
        _URL,
        sha256=_SHA,
        pinned_addresses=_ADDRESSES,
        allow_cross_host_redirects=True,
    )
    assert ok is False and "cross-host" in reason
    assert list(tmp_path.iterdir()) == []


def _pinned_handler() -> asset_downloader._PinnedHTTPSHandler:
    return asset_downloader._PinnedHTTPSHandler(
        asset_downloader.make_ssl_context(), "cdn.example.com", 443, _ADDRESSES
    )


@pytest.mark.parametrize(
    "host", ["elsewhere.example", "cdn.example.com:8443", "192.0.2.10", "cdn.example.com.evil"]
)
def test_a_connection_to_any_other_origin_is_refused_before_a_socket(host) -> None:
    with pytest.raises(urllib.error.URLError):
        _pinned_handler()._connection(host)


def test_the_pinned_connection_dials_the_approved_addresses_never_the_name(monkeypatch) -> None:
    dialed: list[tuple[str, int]] = []
    sentinel = object()

    def create_connection(address, timeout=None, source_address=None):
        dialed.append(address)
        if address[0] == _ADDRESSES[0]:
            raise ConnectionRefusedError("first node down")
        return sentinel

    monkeypatch.setattr(asset_downloader.socket, "create_connection", create_connection)
    handler = _pinned_handler()
    conn = handler._connection("cdn.example.com")
    assert conn.host == "cdn.example.com"
    got = conn._create_connection(("cdn.example.com", 443), socket.getdefaulttimeout(), None)
    assert got is sentinel
    assert dialed == [(_ADDRESSES[0], 443), (_ADDRESSES[1], 443)]


def test_the_pinned_connection_raises_when_no_approved_address_answers(monkeypatch) -> None:
    def refuse(address, timeout=None, source_address=None):
        raise ConnectionRefusedError(address[0])

    monkeypatch.setattr(asset_downloader.socket, "create_connection", refuse)
    conn = _pinned_handler()._connection("cdn.example.com")
    with pytest.raises(ConnectionRefusedError, match=_ADDRESSES[1]):
        conn._create_connection(("cdn.example.com", 443), socket.getdefaulttimeout(), None)


def _context(**overrides) -> SimpleNamespace:
    """A stand-in SSL context: only the two settings the pin reads, never a real one."""
    return SimpleNamespace(
        **{"verify_mode": ssl.CERT_REQUIRED, "check_hostname": True, **overrides}
    )


def test_the_pinned_connection_verifies_the_certificate_and_the_host_name() -> None:
    conn = _pinned_handler()._connection("cdn.example.com")
    assert isinstance(conn, asset_downloader._PinnedHTTPSConnection)
    assert conn._context.verify_mode == ssl.CERT_REQUIRED
    assert conn._context.check_hostname is True
    opener = asset_downloader.build_opener(pin=("cdn.example.com", 443, _ADDRESSES))
    (handler,) = [h for h in opener.handlers if isinstance(h, asset_downloader._PinnedHTTPSHandler)]
    built = handler._connection("cdn.example.com")
    assert built._context.verify_mode == ssl.CERT_REQUIRED
    assert built._context.check_hostname is True


@pytest.mark.parametrize(
    "unverified",
    [
        {"verify_mode": ssl.CERT_OPTIONAL},
        {"check_hostname": False},
    ],
)
def test_a_context_that_does_not_verify_is_refused_everywhere(unverified) -> None:
    context = _context(**unverified)
    with pytest.raises(ValueError, match="verifies"):
        asset_downloader.build_opener(context=context, pin=("cdn.example.com", 443, _ADDRESSES))
    with pytest.raises(ValueError, match="verifies"):
        asset_downloader._PinnedHTTPSHandler(context, "cdn.example.com", 443, _ADDRESSES)
    with pytest.raises(ValueError, match="verifies"):
        asset_downloader._PinnedHTTPSConnection(
            "cdn.example.com", addresses=_ADDRESSES, context=context, timeout=None
        )


def test_a_caller_cannot_hand_the_pinned_connection_its_own_context() -> None:
    handler = _pinned_handler()
    for options in ({"context": _context(check_hostname=False)}, {"check_hostname": False}):
        with pytest.raises(TypeError):
            handler._connection("cdn.example.com", **options)


def test_the_handler_forwards_no_connection_argument_to_urllib(monkeypatch) -> None:
    handler = _pinned_handler()
    seen: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        handler, "do_open", lambda http_class, req, **kwargs: seen.append((http_class, kwargs))
    )
    handler.https_open(object())
    assert seen == [(handler._connection, {})]
