"""Kiro's own Chromium: the headless fallback for a host with no usable Chrome.

A Chrome for Testing build, pinned to one version, downloaded into the read-only
harness prefix (``<home>/browser-harness/chromium/<version>``) so a sandboxed process
can neither swap the binary nor read the profile beside it. The download logic moved
here from ``browser_cli/install_browsers.py``: the platform key, the marker file that
says an extraction finished (a directory exists from the first byte of a download, so
its presence alone proves nothing), and the per-platform executable path -- the table
Playwright's registry uses for the same builds.

Sources, in order: Google's own Chrome for Testing bucket, then Playwright's CDN,
which mirrors it and is the only host of the ``linux-arm64`` build. Both are HTTPS;
neither publishes a digest, so the archive is trusted the way the old Playwright
download was. The version pin is what makes a bump a reviewed change.
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The Chrome for Testing version Kiro Crew downloads. The same build Playwright
#: 1.64's ``chromium-1244`` ships, so hosts that proved it already ran it.
CHROMIUM_VERSION = "154.0.8037.0"

#: Directory under the harness prefix that holds every downloaded version.
CHROMIUM_DIR_NAME = "chromium"

#: Written into a version directory once extraction has finished. Same convention
#: as Playwright's ``INSTALLATION_COMPLETE``: without it, a directory an
#: interrupted download left behind would read as a browser.
INSTALLATION_MARKER = "INSTALLATION_COMPLETE"

#: Chrome for Testing's platform keys.
CFT_PLATFORMS: tuple[str, ...] = ("linux64", "linux-arm64", "mac-arm64", "mac-x64", "win64")

#: Download hosts, tried in order. ``{version}`` and ``{platform}`` are filled in.
DOWNLOAD_URLS: tuple[str, ...] = (
    "https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/chrome-{platform}.zip",
    "https://cdn.playwright.dev/builds/cft/{version}/{platform}/chrome-{platform}.zip",
)

#: Executable inside the extracted ``chrome-<platform>`` directory, per platform.
_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "linux64": ("chrome",),
    "linux-arm64": ("chrome",),
    "mac-arm64": (
        "Google Chrome for Testing.app",
        "Contents",
        "MacOS",
        "Google Chrome for Testing",
    ),
    "mac-x64": ("Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
    "win64": ("chrome.exe",),
}

#: Stages reported to the progress callback during a download.
STAGE_DOWNLOADING = "downloading_browser"
STAGE_EXTRACTING = "extracting_browser"

#: Network budget for one archive. Chrome for Testing is ~170 MB; a slow link gets
#: minutes, a dead one does not get forever.
DOWNLOAD_TIMEOUT_SECS = 20 * 60
_READ_TIMEOUT_SECS = 60.0
_CHUNK_BYTES = 1 << 20

#: Zip external attributes: the file-type bits and the symlink type.
_ZIP_MODE_SHIFT = 16
_ZIP_TYPE_MASK = 0o170000
_ZIP_SYMLINK = 0o120000
_ZIP_PERM_MASK = 0o7777

ProgressCallback = Callable[[str, str], None]


class ChromiumDownloadError(RuntimeError):
    """No source delivered the pinned build, or the archive did not extract."""


class ChromiumDownloadStopped(ChromiumDownloadError):
    """The install was stopped while the archive was downloading."""


@dataclass(frozen=True)
class ChromiumState:
    """What is on disk for the pinned version."""

    version: str
    platform: str | None
    present: bool
    executable: Path | None

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "platform": self.platform,
            "present": self.present,
            "executable": str(self.executable) if self.executable else None,
        }


def cft_platform(system: str | None = None, machine: str | None = None) -> str | None:
    """Chrome for Testing's key for this host, or ``None`` for one it has no build for."""
    system = system or sys.platform
    machine = (machine or _platform.machine()).lower()
    arm = machine in ("arm64", "aarch64")
    x64 = machine in ("x86_64", "amd64")
    if system == "darwin":
        return "mac-arm64" if arm else ("mac-x64" if x64 else None)
    if system.startswith("linux"):
        return "linux-arm64" if arm else ("linux64" if x64 else None)
    if system == "win32":
        return "win64" if x64 or arm else None
    return None


def chromium_root(prefix: Path) -> Path:
    """Every downloaded version lives under here, inside the read-only prefix."""
    return prefix / CHROMIUM_DIR_NAME


def version_dir(prefix: Path, version: str = CHROMIUM_VERSION) -> Path:
    return chromium_root(prefix) / version


def executable_path(
    prefix: Path, platform: str | None, version: str = CHROMIUM_VERSION
) -> Path | None:
    """Where the pinned build's binary sits, whether or not it is downloaded yet."""
    parts = _EXECUTABLES.get(platform or "")
    if parts is None:
        return None
    return version_dir(prefix, version) / f"chrome-{platform}" / Path(*parts)


def chromium_state(prefix: Path, platform: str | None = None) -> ChromiumState:
    """Passive filesystem evidence for the pinned version: nothing is launched."""
    key = platform if platform is not None else cft_platform()
    exe = executable_path(prefix, key)
    present = False
    if exe is not None:
        try:
            present = exe.is_file() and (version_dir(prefix) / INSTALLATION_MARKER).is_file()
        except OSError:
            present = False
    return ChromiumState(CHROMIUM_VERSION, key, present, exe if present else None)


def download_chromium(
    prefix: Path,
    progress: ProgressCallback | None = None,
    *,
    platform: str | None = None,
    urls: tuple[str, ...] = DOWNLOAD_URLS,
    opener: Callable[[str, float], object] | None = None,
    refresh: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> ChromiumState:
    """Download and extract the pinned build. BLOCKING: run it off the event loop.

    A complete install is left alone unless *refresh* is set (the owner pressed
    Redownload: the copy on disk is replaced even when its marker says it is
    whole, which is how a damaged executable is repaired). An incomplete
    directory is replaced either way. The archive is downloaded and extracted
    into a temp directory beside the target, and the executable checked there,
    before the installed copy is touched: a failed or stopped download leaves
    the working browser in place. The marker is written last.
    """
    key = platform if platform is not None else cft_platform()
    if key is None:
        raise ChromiumDownloadError("no Chrome for Testing build exists for this platform")
    target = version_dir(prefix)
    root = chromium_root(prefix)
    _sweep_previous(root, target)
    current = chromium_state(prefix, key)
    if current.present and not refresh:
        return current
    root.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda _stage, _detail: None)
    with tempfile.TemporaryDirectory(
        prefix="chromium-", dir=root, ignore_cleanup_errors=True
    ) as tmp:
        archive = Path(tmp) / "chrome.zip"
        errors: list[str] = []
        for template in urls:
            url = template.format(version=CHROMIUM_VERSION, platform=key)
            notify(STAGE_DOWNLOADING, url)
            try:
                _fetch(url, archive, opener, should_stop)
                break
            except ChromiumDownloadStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - the next source is tried
                errors.append(f"{url}: {exc}")
                archive.unlink(missing_ok=True)
        else:
            raise ChromiumDownloadError("; ".join(errors) or "no download source configured")
        notify(STAGE_EXTRACTING, str(target))
        staging = Path(tmp) / "extract"
        _extract(archive, staging)
        exe = executable_path(prefix, key)
        if exe is None or not (staging / exe.relative_to(target)).is_file():
            raise ChromiumDownloadError(f"archive did not contain {exe}")
        aside = _swap_in(staging, target, root)
        (target / INSTALLATION_MARKER).write_text(CHROMIUM_VERSION, encoding="utf-8")
    if aside is not None:
        # The new copy is whole and marked: the old one goes, best effort. A file
        # something still holds open is left for the next install's sweep.
        shutil.rmtree(aside.parent, ignore_errors=True)
    state = chromium_state(prefix, key)
    if not state.present:
        raise ChromiumDownloadError(f"archive did not contain {executable_path(prefix, key)}")
    return state


#: Prefix of the directory an old copy is moved into during a swap, beside the
#: version directories and outside the download's temp directory.
_PREVIOUS_PREFIX = ".previous-"


def _sweep_previous(root: Path, target: Path) -> None:
    """Deal with old copies an earlier swap left behind. Best effort.

    When *target* is missing (a swap whose restore failed), the old copy is put back
    first, so the only working browser is never the one swept. Every other leftover
    is deleted.
    """
    try:
        leftovers = sorted(p for p in root.iterdir() if p.name.startswith(_PREVIOUS_PREFIX))
    except OSError:
        return
    for path in leftovers:
        kept = path / target.name
        if not target.exists() and kept.is_dir():
            try:
                kept.rename(target)
            except OSError:
                continue
        shutil.rmtree(path, ignore_errors=True)


def _swap_in(staging: Path, target: Path, root: Path) -> Path | None:
    """Put *staging* at *target*; the installed copy is moved aside, never half-deleted.

    Returns where the old copy now is (``None`` when there was none), for the caller
    to delete once the new copy is marked. The old copy is renamed out of the way
    first, into a directory of its own under *root*. On Windows that rename is
    refused while the browser runs from it (its executable and libraries are open),
    and then nothing has been touched. A failed final rename puts the old copy back;
    if even that fails, the old copy is left where it is and the error names it.
    """
    aside: Path | None = None
    if target.exists():
        holder = Path(tempfile.mkdtemp(prefix=_PREVIOUS_PREFIX, dir=root))
        aside = holder / target.name
        try:
            target.rename(aside)
        except OSError as exc:
            shutil.rmtree(holder, ignore_errors=True)
            if isinstance(exc, PermissionError):
                raise ChromiumDownloadError(
                    f"the installed browser is in use and cannot be replaced; close it and retry ({exc})"
                ) from exc
            raise ChromiumDownloadError(
                f"could not move the installed browser aside: {exc}"
            ) from exc
    try:
        staging.rename(target)
    except OSError as exc:
        if aside is not None and not target.exists():
            try:
                aside.rename(target)
            except OSError as restore:
                raise ChromiumDownloadError(
                    f"could not install the new browser ({exc}); the previous copy is at {aside} ({restore})"
                ) from exc
        raise ChromiumDownloadError(f"could not install the new browser: {exc}") from exc
    return aside


def _fetch(
    url: str,
    dest: Path,
    opener: Callable[[str, float], object] | None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    open_url = opener or _default_opener
    with open_url(url, _READ_TIMEOUT_SECS) as response, dest.open("wb") as out:  # type: ignore[attr-defined]
        while True:
            # A stopped install returns within one chunk read, not after the archive.
            if should_stop is not None and should_stop():
                raise ChromiumDownloadStopped("install stopped")
            chunk = response.read(_CHUNK_BYTES)
            if not chunk:
                break
            out.write(chunk)


def _default_opener(url: str, timeout: float) -> object:
    if not url.startswith("https://"):
        raise ChromiumDownloadError(f"refusing a non-HTTPS download: {url}")
    # The scheme is checked to be https just above, so no file:// URL reaches here.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - https pinned above


def _extract(archive: Path, dest: Path) -> None:
    """Unzip preserving POSIX modes and symlinks, which ``extractall`` discards.

    Chrome's macOS bundle carries ``Versions/Current`` style links and every
    binary needs its execute bit; a plain ``extractall`` would write the link
    target's path as a file and leave nothing runnable.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            out = (dest / info.filename).resolve() if info.filename else root
            if root != out and root not in out.parents:
                raise ChromiumDownloadError(f"archive entry escapes its directory: {info.filename}")
            mode = info.external_attr >> _ZIP_MODE_SHIFT
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            if (mode & _ZIP_TYPE_MASK) == _ZIP_SYMLINK and os.name != "nt":
                link_target = zf.read(info).decode("utf-8")
                if out.is_symlink() or out.exists():
                    out.unlink()
                os.symlink(link_target, out)
                continue
            with zf.open(info) as src, out.open("wb") as fh:
                shutil.copyfileobj(src, fh, _CHUNK_BYTES)
            perm = mode & _ZIP_PERM_MASK
            if perm and os.name != "nt":
                os.chmod(out, perm | stat.S_IRUSR | stat.S_IWUSR)
