"""Kiro's Chromium download (``browser_harness/chromium.py``) against an in-test archive."""

from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from kiro_crew.browser_harness import chromium

_PLATFORM = "linux64"
_EXE_REL = "chrome-linux64/chrome"


def _archive(*, with_symlink: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        exe = zipfile.ZipInfo(_EXE_REL)
        exe.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(exe, b"#!/bin/sh\necho chrome\n")
        zf.writestr("chrome-linux64/resources.pak", b"pak")
        if with_symlink:
            link = zipfile.ZipInfo("chrome-linux64/chrome-link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, b"chrome")
    return buf.getvalue()


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _opener_for(payload: bytes, *, fail_first: bool = False):
    calls: list[str] = []

    def opener(url: str, timeout: float) -> _Response:
        calls.append(url)
        if fail_first and len(calls) == 1:
            raise OSError("first host down")
        return _Response(payload)

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def test_cft_platform_maps_each_host() -> None:
    assert chromium.cft_platform("darwin", "arm64") == "mac-arm64"
    assert chromium.cft_platform("darwin", "x86_64") == "mac-x64"
    assert chromium.cft_platform("linux", "x86_64") == "linux64"
    assert chromium.cft_platform("linux", "aarch64") == "linux-arm64"
    assert chromium.cft_platform("win32", "AMD64") == "win64"
    assert chromium.cft_platform("linux", "mips") is None
    for key in chromium.CFT_PLATFORMS:
        assert chromium.executable_path(Path("/p"), key) is not None


def test_absent_prefix_reads_as_not_present(tmp_path: Path) -> None:
    state = chromium.chromium_state(tmp_path, _PLATFORM)
    assert state.present is False and state.executable is None
    assert state.version == chromium.CHROMIUM_VERSION


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")
def test_download_extracts_modes_symlinks_and_writes_the_marker_last(tmp_path: Path) -> None:
    stages: list[tuple[str, str]] = []
    opener = _opener_for(_archive(with_symlink=True))
    state = chromium.download_chromium(
        tmp_path, lambda s, d: stages.append((s, d)), platform=_PLATFORM, opener=opener
    )
    assert state.present is True
    exe = tmp_path / chromium.CHROMIUM_DIR_NAME / chromium.CHROMIUM_VERSION / _EXE_REL
    assert state.executable == exe
    assert os.access(exe, os.X_OK)
    assert (exe.parent / "chrome-link").is_symlink()
    assert os.readlink(exe.parent / "chrome-link") == "chrome"
    assert (exe.parent.parent / chromium.INSTALLATION_MARKER).read_text(
        encoding="utf-8"
    ) == chromium.CHROMIUM_VERSION
    assert [s for s, _ in stages] == [chromium.STAGE_DOWNLOADING, chromium.STAGE_EXTRACTING]
    assert opener.calls[0] == chromium.DOWNLOAD_URLS[0].format(
        version=chromium.CHROMIUM_VERSION, platform=_PLATFORM
    )


def test_the_second_source_is_tried_when_the_first_fails(tmp_path: Path) -> None:
    opener = _opener_for(_archive(with_symlink=False), fail_first=True)
    state = chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=opener)
    assert state.present is True
    assert len(opener.calls) == 2
    assert "cdn.playwright.dev" in opener.calls[1]


def test_every_source_failing_raises_and_leaves_no_version_directory(tmp_path: Path) -> None:
    def opener(url: str, timeout: float) -> _Response:
        raise OSError("down")

    with pytest.raises(chromium.ChromiumDownloadError):
        chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=opener)
    assert not chromium.version_dir(tmp_path).exists()
    assert chromium.chromium_state(tmp_path, _PLATFORM).present is False


def test_a_complete_install_is_left_alone(tmp_path: Path) -> None:
    first = _opener_for(_archive(with_symlink=False))
    chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=first)
    second = _opener_for(_archive(with_symlink=False))
    chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=second)
    assert second.calls == []


def test_an_archive_without_the_binary_is_refused(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("chrome-linux64/README", b"nothing here")
    with pytest.raises(chromium.ChromiumDownloadError):
        chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=_opener_for(buf.getvalue()))
    assert chromium.chromium_state(tmp_path, _PLATFORM).present is False


def test_an_archive_entry_that_escapes_is_refused(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape", b"x")
    with pytest.raises(chromium.ChromiumDownloadError):
        chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=_opener_for(buf.getvalue()))
    assert not (tmp_path / "escape").exists()


def test_only_https_sources_are_configured() -> None:
    assert all(url.startswith("https://") for url in chromium.DOWNLOAD_URLS)
    with pytest.raises(chromium.ChromiumDownloadError):
        chromium._default_opener("http://example.invalid/x.zip", 1.0)


def test_refresh_replaces_a_complete_install(tmp_path: Path) -> None:
    """Redownload: a copy whose marker is present is fetched again and replaced."""
    first = _opener_for(_archive(with_symlink=False))
    chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=first)
    damaged = chromium.executable_path(tmp_path, _PLATFORM)
    assert damaged is not None
    damaged.write_bytes(b"not a browser")
    second = _opener_for(_archive(with_symlink=False))
    state = chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=second, refresh=True)
    assert len(second.calls) == 1
    assert state.present is True
    assert damaged.read_bytes() != b"not a browser"


@pytest.mark.parametrize("failure", ["offline", "no_binary"])
def test_a_redownload_that_fails_keeps_the_working_copy(tmp_path: Path, failure: str) -> None:
    """Redownload while offline, or of a bad archive: the installed browser still runs."""
    chromium.download_chromium(
        tmp_path, platform=_PLATFORM, opener=_opener_for(_archive(with_symlink=False))
    )
    exe = chromium.executable_path(tmp_path, _PLATFORM)
    assert exe is not None
    before = exe.read_bytes()
    if failure == "offline":

        def opener(url: str, timeout: float) -> _Response:
            raise OSError("offline")

    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("chrome-linux64/resources.pak", b"pak")
        opener = _opener_for(buf.getvalue())
    with pytest.raises(chromium.ChromiumDownloadError):
        chromium.download_chromium(tmp_path, platform=_PLATFORM, opener=opener, refresh=True)
    assert exe.read_bytes() == before
    assert chromium.chromium_state(tmp_path, _PLATFORM).present is True
