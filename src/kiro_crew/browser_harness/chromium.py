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

Two builds of that version exist. The full one (``chrome``) is what every host
gets, except a Linux host whose libcups is too old for it (:mod:`.libcups`:
Amazon Linux 2), where it cannot even start. That host gets the same version's
``chrome-headless-shell`` instead, which does not link libcups, in a version
directory of its own. :func:`wanted_build` decides; :func:`chromium_state` and
:func:`download_chromium` follow its answer, so the executable the supervisor
launches does too.
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import shutil
import ssl
import stat
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import _ssl_compat
from kiro_crew.browser_harness import libcups

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

#: The two Chrome for Testing builds, by the name each archive and its top
#: directory carry (``<build>-<platform>.zip`` extracting to ``<build>-<platform>/``).
BUILD_FULL = "chrome"
BUILD_HEADLESS_SHELL = "chrome-headless-shell"

#: Suffix of the headless shell's version directory, beside the full build's.
_HEADLESS_SHELL_DIR_SUFFIX = "-headless-shell"

#: Download hosts, tried in order. ``{version}``, ``{platform}`` and ``{build}`` are
#: filled in.
DOWNLOAD_URLS: tuple[str, ...] = (
    "https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/{build}-{platform}.zip",
    "https://cdn.playwright.dev/builds/cft/{version}/{platform}/{build}-{platform}.zip",
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

#: The same for ``chrome-headless-shell-<platform>``. Linux only: no other platform
#: is ever given the headless shell.
_HEADLESS_SHELL_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "linux64": ("chrome-headless-shell",),
    "linux-arm64": ("chrome-headless-shell",),
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
    build: str = BUILD_FULL

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "platform": self.platform,
            "build": self.build,
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
        if arm:
            return "mac-arm64"
        return "mac-x64" if x64 else None
    if system.startswith("linux"):
        if arm:
            return "linux-arm64"
        return "linux64" if x64 else None
    if system == "win32":
        return "win64" if x64 or arm else None
    return None


def chromium_root(prefix: Path) -> Path:
    """Every downloaded version lives under here, inside the read-only prefix."""
    return prefix / CHROMIUM_DIR_NAME


def wanted_build(platform: str | None) -> str:
    """The build this host runs: the headless shell where libcups cannot load the full one.

    Reads the filesystem only (:func:`.libcups.too_old_for_chrome`), so it is safe on
    the status path.
    """
    if platform in _HEADLESS_SHELL_EXECUTABLES and libcups.too_old_for_chrome():
        return BUILD_HEADLESS_SHELL
    return BUILD_FULL


def version_dir(prefix: Path, version: str = CHROMIUM_VERSION, build: str = BUILD_FULL) -> Path:
    suffix = _HEADLESS_SHELL_DIR_SUFFIX if build == BUILD_HEADLESS_SHELL else ""
    return chromium_root(prefix) / f"{version}{suffix}"


def executable_path(
    prefix: Path,
    platform: str | None,
    version: str = CHROMIUM_VERSION,
    build: str = BUILD_FULL,
) -> Path | None:
    """Where *build*'s binary sits, whether or not it is downloaded yet."""
    table = _HEADLESS_SHELL_EXECUTABLES if build == BUILD_HEADLESS_SHELL else _EXECUTABLES
    parts = table.get(platform or "")
    if parts is None:
        return None
    return version_dir(prefix, version, build) / f"{build}-{platform}" / Path(*parts)


def chromium_state(
    prefix: Path, platform: str | None = None, *, build: str | None = None
) -> ChromiumState:
    """Passive filesystem evidence for the pinned version: nothing is launched.

    Only the build this host runs counts (*build*, default :func:`wanted_build`): on
    a host that wants the headless shell, a full build left on disk reads as absent.
    """
    key = platform if platform is not None else cft_platform()
    build = build if build is not None else wanted_build(key)
    exe = executable_path(prefix, key, build=build)
    present = False
    if exe is not None:
        try:
            marker = version_dir(prefix, build=build) / INSTALLATION_MARKER
            present = exe.is_file() and marker.is_file()
        except OSError:
            present = False
    return ChromiumState(CHROMIUM_VERSION, key, present, exe if present else None, build)


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
    build = wanted_build(key)
    target = version_dir(prefix, build=build)
    root = chromium_root(prefix)
    _sweep_previous(root, target)
    current = chromium_state(prefix, key, build=build)
    if current.present and not refresh:
        return current
    root.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda _stage, _detail: None)
    with tempfile.TemporaryDirectory(
        prefix="chromium-", dir=root, ignore_cleanup_errors=True
    ) as tmp:
        archive = Path(tmp) / "chrome.zip"
        errors: list[str] = []
        certificate_failed = False
        for template in urls:
            url = template.format(version=CHROMIUM_VERSION, platform=key, build=build)
            notify(STAGE_DOWNLOADING, url)
            try:
                _fetch(url, archive, opener, should_stop)
                break
            except ChromiumDownloadStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - the next source is tried
                errors.append(f"{url}: {exc}")
                certificate_failed = certificate_failed or _is_certificate_failure(exc)
                archive.unlink(missing_ok=True)
        else:
            message = "; ".join(errors) or "no download source configured"
            note = _ssl_compat.system_trust_note() if certificate_failed else None
            if note:
                message = f"{message} ({note})"
            raise ChromiumDownloadError(message)
        notify(STAGE_EXTRACTING, str(target))
        staging = Path(tmp) / "extract"
        _extract(archive, staging)
        exe = executable_path(prefix, key, build=build)
        if exe is None or not (staging / exe.relative_to(target)).is_file():
            raise ChromiumDownloadError(f"archive did not contain {exe}")
        aside = _swap_in(staging, target, root)
        (target / INSTALLATION_MARKER).write_text(CHROMIUM_VERSION, encoding="utf-8")
    if aside is not None:
        # The new copy is whole and marked: the old one goes, best effort. A file
        # something still holds open is left for the next install's sweep.
        shutil.rmtree(aside.parent, ignore_errors=True)
    state = chromium_state(prefix, key, build=build)
    if not state.present:
        raise ChromiumDownloadError(
            f"archive did not contain {executable_path(prefix, key, build=build)}"
        )
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
    # An explicit context, so the CAs this download trusts come from the platform
    # trust store whether or not the startup prelude managed to patch ``ssl``.
    context = _ssl_compat.system_trust_context()
    # The scheme is checked to be https just above, so no file:// URL reaches here.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    return urllib.request.urlopen(
        url, timeout=timeout, context=context
    )  # noqa: S310 - https pinned above


def _is_certificate_failure(exc: BaseException) -> bool:
    """Whether *exc*, or what it wraps (urllib's ``URLError.reason``), is a failed certificate check."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        for linked in (getattr(current, "reason", None), current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                stack.append(linked)
    return False


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
