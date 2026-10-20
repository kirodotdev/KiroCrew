"""Which browser builds Playwright has downloaded for the installed ``playwright-cli``.

Passive filesystem evidence only, so :func:`kiro_crew.browser_cli.install.detect` stays free
of a subprocess that would start a browser to answer: where the CLI downloads browsers
(:func:`_browsers_cache_dir`), the revision its ``browsers.json`` requires, the host key that
sends a ``revisionOverrides`` download to its own directory, and the per-engine state
:func:`browser_status` and :func:`browsers_present` report.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.browser_cli.install_launcher import _cli_package_dirs, _manifest_for_cli_package

# Browser binaries live in Playwright's own cache, keyed by platform, and
# ``PLAYWRIGHT_BROWSERS_PATH`` overrides it. Probing this directory keeps
# ``detect()`` free of a subprocess that would launch a browser to answer.
_BROWSERS_CACHE_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def _playwright_env(name: str) -> str | None:
    """Read *name* the way playwright-core's ``getFromENV`` does.

    The process environment first, then npm's ``npm_config_<name>`` and
    ``npm_package_config_<name>`` projections. An installer child inherits the
    gateway's environment (:func:`cli_env`), so these are the values it sees.
    """
    lowered = name.lower()
    for key in (name, f"npm_config_{lowered}", f"npm_package_config_{lowered}"):
        value = os.environ.get(key)
        if value is not None:
            return value
    return None


def _default_cache_root() -> Path | None:
    """playwright-core's ``computeDefaultCacheDirectory`` for this platform."""
    if platform_compat.IS_LINUX:
        xdg = os.environ.get("XDG_CACHE_HOME")
        return Path(xdg) if xdg else Path.home() / ".cache"
    if platform_compat.IS_MACOS:
        return Path.home() / "Library" / "Caches"
    if platform_compat.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    return None


def _browsers_cache_dir() -> Path | None:
    """The browser registry directory the installed CLI downloads into.

    Mirrors ``registryDirectory`` in the playwright-core the CLI pins
    (``lib/coreBundle.js`` of ``playwright-core@1.64.0-alpha-1789764292000``,
    served to ``@playwright/cli@0.1.21``; ``lib/server/registry/index.js`` in
    1.58 has the same logic):

    * ``PLAYWRIGHT_BROWSERS_PATH=0`` means ``<playwright-core>/.local-browsers``,
      the package-local registry, so it is resolved against the SERVING core
      package rather than read as a directory named ``0``;
    * any other non-empty value is the directory itself;
    * otherwise ``ms-playwright`` under the platform cache root, which honours
      ``XDG_CACHE_HOME`` on Linux and falls back to ``~/AppData/Local`` on
      Windows when ``LOCALAPPDATA`` is unset;
    * a relative result is resolved against ``INIT_CWD`` or the working
      directory, which for the installer child is the gateway's own.

    ``None`` when the location cannot be determined (an unknown platform, or
    ``0`` with no attributable core package), which reads back as "unknown",
    never as a missing browser.
    """
    override = _playwright_env(_BROWSERS_CACHE_ENV)
    result: Path | None
    if override == "0":
        manifest = _browsers_manifest_path()
        result = manifest.parent / ".local-browsers" if manifest is not None else None
    elif override:
        result = Path(override)
    else:
        root = _default_cache_root()
        result = root / "ms-playwright" if root is not None else None
    if result is None:
        return None
    if not result.is_absolute():
        base = _playwright_env("INIT_CWD") or os.getcwd()
        result = Path(os.path.abspath(Path(base) / result))
    return result


# The engines Playwright downloads, in the order the panel lists them. A fixed
# tuple, not free input: it is what validates the engine name before it reaches
# argv (see `install_browser`), which is what keeps that spawn benign.
BROWSER_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")
_DEFAULT_BROWSER_ENGINE = BROWSER_ENGINES[0]


#: Per-engine download state reported by :func:`browser_status`.
STATUS_DOWNLOADED = "downloaded"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

#: The file playwright-core writes into a browser directory once extraction has
#: finished (``browserDirectoryToMarkerFilePath`` in the registry and in
#: ``oopDownloadBrowserMain``). The directory exists from the start of the
#: download, so its presence alone does not mean a browser is there.
_INSTALLATION_MARKER = "INSTALLATION_COMPLETE"


def _cached_browser_names(cache: Path) -> set[str] | None:
    """Directory names in Playwright's browser cache, or ``None`` if unreadable.

    An absent cache directory is an empty set: nothing was ever downloaded,
    which is a confirmed absence rather than an unknown.
    """
    try:
        return {child.name for child in cache.iterdir() if child.is_dir()}
    except (FileNotFoundError, NotADirectoryError):
        return set()
    except OSError:
        return None


def _download_complete(directory: Path) -> bool | None:
    """Whether *directory* carries the completion marker; ``None`` if unreadable."""
    try:
        return (directory / _INSTALLATION_MARKER).is_file()
    except OSError:
        return None


def _browsers_manifest_path() -> Path | None:
    """Locate the installed ``playwright-core/browsers.json``, or ``None``.

    Resolution is anchored on the ``@playwright/cli`` package
    (:func:`_cli_package_dirs`) rather than walked up from the launcher toward
    the filesystem root. The anchor is the correctness property, not a
    shortcut: an unbounded walk passes through ``$HOME`` on the standalone
    layout, where a single unrelated ``~/node_modules/playwright-core`` would
    supply a revision from a DIFFERENT install. That reports a **working**
    browser broken, and keeps reporting it after the download the panel offers,
    because the gate goes on reading the foreign manifest. Requiring the
    manifest to be served to the CLI package makes a foreign tree unreachable.

    ``None`` when no manifest can be attributed to a CLI package, which callers
    treat as "revision unknown" and answer with the documented presence-only
    fallback.
    """
    for package in _cli_package_dirs():
        manifest = _manifest_for_cli_package(package)
        if manifest is not None:
            return manifest
    return None


def _required_revisions() -> dict[str, str] | None:
    """Required revision per engine, read from ``browsers.json``, or ``None``.

    ``None`` means the required revision cannot be determined -- the manifest is
    absent, unreadable, or not the shape this expects. Callers treat that as
    "cannot confirm a revision" and fall back to the older presence-only check
    rather than turning a working browser into a reported-broken one.

    Keyed by the manifest's own engine names (``chromium``,
    ``chromium-headless-shell``, ``firefox``, ``webkit``, ...). Only entries with
    a string ``name`` and ``revision`` are kept, so a malformed row is skipped
    rather than crashing the read.
    """
    path = _browsers_manifest_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return None
    revisions: dict[str, str] = {}
    for entry in browsers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        revision = entry.get("revision")
        if isinstance(name, str) and isinstance(revision, str):
            revisions[name] = revision
    return revisions or None


def _revision_overrides() -> dict[str, dict[str, str]]:
    """Per-engine ``revisionOverrides`` from ``browsers.json`` (host platform -> revision).

    playwright-core downloads an overridden revision into
    ``<engine>_<hostPlatform>_special-<revision>`` (``readDescriptors``) on the
    hosts an override names, such as WebKit on older Debian and Ubuntu. Empty
    when there is no manifest or no override.
    """
    path = _browsers_manifest_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for entry in browsers:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        raw = entry.get("revisionOverrides")
        if isinstance(raw, dict):
            kept = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
            if kept:
                overrides[entry["name"]] = kept
    return overrides


def _playwright_arch() -> str | None:
    """Node's ``os.arch()`` name for this machine, for the architectures Playwright ships."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None


def _playwright_host_platform() -> str:
    """playwright-core's ``hostPlatform`` key for this host, or ``"<unknown>"``.

    A port of ``calculatePlatform`` in playwright-core's
    ``lib/server/utils/hostPlatform.js`` (1.58 and the 1.64 build the CLI pins),
    because the key decides which directory ``revisionOverrides`` sends the
    installer to: a host the override names gets
    ``<engine>_<key>_special-<rev>``, every other host the plain directory. One
    known divergence: on macOS Playwright appends ``-arm64`` when a CPU model
    names Apple, and this reads the machine type, which differs only for an
    x64 interpreter under Rosetta.
    """
    override = os.environ.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "")
    if override:
        return override
    if platform_compat.IS_MACOS:
        try:
            major = int(platform.release().split(".")[0])
        except ValueError:
            return "<unknown>"
        if major < 18:
            return "mac10.13"
        if major == 18:
            return "mac10.14"
        if major == 19:
            return "mac10.15"
        key = f"mac{min(major - 9, 15)}"
        return f"{key}-arm64" if _playwright_arch() == "arm64" else key
    if platform_compat.IS_WINDOWS:
        return "win64"
    if not platform_compat.IS_LINUX:
        return "<unknown>"
    arch = _playwright_arch()
    if arch is None:
        return "<unknown>"
    suffix = f"-{arch}"
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    distro = release.get("ID", "").lower()
    version = release.get("VERSION_ID", "")
    try:
        major_version = int(version.split(".")[0])
    except ValueError:
        major_version = 0
    if distro in ("ubuntu", "pop", "neon", "tuxedo"):
        if major_version < 20:
            return f"ubuntu18.04{suffix}"
        if major_version < 22:
            return f"ubuntu20.04{suffix}"
        if major_version < 24:
            return f"ubuntu22.04{suffix}"
        if major_version < 26:
            return f"ubuntu24.04{suffix}"
        return f"ubuntu{version}{suffix}"
    if distro == "linuxmint":
        if major_version <= 20:
            return f"ubuntu20.04{suffix}"
        if major_version == 21:
            return f"ubuntu22.04{suffix}"
        return f"ubuntu24.04{suffix}"
    if distro in ("debian", "raspbian"):
        if version in ("11", "12", "13"):
            return f"debian{version}{suffix}"
        if version == "":
            return f"debian13{suffix}"
    return f"ubuntu24.04{suffix}"


def _cache_dir_name_for(engine: str, revision: str) -> str:
    """The cache directory name that satisfies *engine* at *revision*.

    Playwright names the directory ``<engine>-<revision>`` (``chromium-1232``),
    with hyphens in the engine name turned into underscores. The engines in
    :data:`BROWSER_ENGINES` contain none, so the name is used as-is.
    """
    return f"{engine}-{revision}"


def _status_of(complete: list[bool | None]) -> str:
    """Fold the completion readings of candidate directories into one status."""
    if any(value is True for value in complete):
        return STATUS_DOWNLOADED
    if any(value is None for value in complete):
        return STATUS_UNKNOWN
    return STATUS_MISSING


def browser_status() -> dict[str, str]:
    """Per-engine download state for the revision the installed CLI needs.

    ``downloaded`` means a directory for the required revision holds
    playwright-core's completion marker (:data:`_INSTALLATION_MARKER`). It is
    passive filesystem evidence: nothing is launched, so it is not a claim that
    the browser starts. ``missing`` means the cache was read and no complete
    build is there, which includes a directory an interrupted download left
    behind. ``unknown`` means the answer could not be read -- an unknown cache
    location or an unreadable cache or marker.

    Revision matching is exact: playwright-core launches only the revision
    bound to its own version, so a stale ``chromium-1208`` left over from before
    a CLI upgrade is not the ``chromium-1232`` the upgraded CLI needs. When the
    required revision cannot be determined (no attributable manifest -- see
    :func:`_required_revisions`), any complete ``<engine>-*`` or
    ``<engine>_<host>_special-*`` build counts, the documented presence-only
    fallback that keeps missing metadata from turning a working browser into a
    reported-broken one.
    """
    cache = _browsers_cache_dir()
    names = _cached_browser_names(cache) if cache is not None else None
    if names is None or cache is None:
        return {engine: STATUS_UNKNOWN for engine in BROWSER_ENGINES}
    required = _required_revisions()
    overrides = _revision_overrides() if required is not None else {}
    host = _playwright_host_platform() if overrides else ""
    result: dict[str, str] = {}
    for engine in BROWSER_ENGINES:
        revision = required.get(engine) if required is not None else None
        if revision is None:
            # Presence-only: any complete build of this engine counts, including
            # the ``<engine>_<host>_special-<rev>`` directory playwright-core's
            # revisionOverrides writes on some hosts. Without a revision there is
            # nothing to match it against, so it is as good as the plain one; the
            # headless shell (``chromium_headless_shell-*``) still does not count.
            candidates = sorted(
                n for n in names if n.startswith(f"{engine}-") or _is_special_build_name(n, engine)
            )
            result[engine] = _status_of([_download_complete(cache / n) for n in candidates])
            continue
        # Exactly one directory satisfies the engine on this host, the one the
        # installer writes: the special override directory when the override
        # names this host's exact platform key, the plain one otherwise.
        host_revision = overrides.get(engine, {}).get(host)
        if host_revision is not None:
            wanted = f"{engine}_{host}_special-{host_revision}"
        else:
            wanted = _cache_dir_name_for(engine, revision)
        status = _status_of([_download_complete(cache / wanted)] if wanted in names else [])
        if status == STATUS_MISSING and _has_complete_special_build(cache, names, engine):
            # A special directory exists only because playwright-core's own
            # `calculatePlatform` chose it, so a complete one this host key did
            # not predict is evidence that the port in `_playwright_host_platform`
            # is stale relative to the installed playwright-core (the CLI pins
            # `@playwright/cli@latest`), not that the cache is empty. Read that as
            # `unknown` rather than a confident `missing`.
            status = STATUS_UNKNOWN
        elif status == STATUS_MISSING and host_revision is not None:
            # The converse: the port says this host takes the special build, but
            # the installer wrote (and completed) the plain required-revision
            # directory instead. Same rule: the port may be wrong about this host,
            # so the plain build is evidence for `unknown`, not `missing`.
            plain = _cache_dir_name_for(engine, revision)
            if plain in names and _download_complete(cache / plain) is True:
                status = STATUS_UNKNOWN
        result[engine] = status
    return result


def _is_special_build_name(name: str, engine: str) -> bool:
    """Whether *name* is an ``<engine>_<host>_special-<rev>`` cache directory."""
    return name.startswith(f"{engine}_") and "_special-" in name


def _has_complete_special_build(cache: Path, names: set[str], engine: str) -> bool:
    """Whether some complete ``<engine>_<host>_special-<rev>`` directory is in *cache*."""
    return any(
        _is_special_build_name(name, engine) and _download_complete(cache / name) is True
        for name in names
    )


def browsers_present(status: dict[str, str] | None = None) -> dict[str, bool]:
    """Which engines are ``downloaded`` per :func:`browser_status`.

    Reported per engine rather than as one boolean so the panel can offer each
    download separately: a user who wants to check a page in Firefox should not
    have to discover that "browser installed" only ever meant Chromium. An
    ``unknown`` engine is ``False`` here; callers that must tell unknown from
    missing read :func:`browser_status` instead. *status* reuses a reading the
    caller already took.
    """
    reading = status if status is not None else browser_status()
    return {engine: reading.get(engine) == STATUS_DOWNLOADED for engine in BROWSER_ENGINES}


def _browser_present(status: dict[str, str] | None = None) -> bool:
    """Whether a downloaded Chromium build exists in Playwright's cache.

    Chromium only, and that narrowness is the point: it is the engine
    ``attach``/``--extension`` supports, so a cache holding solely Firefox or
    WebKit does not make the capability work. This stays the single
    ``browser_ok`` capability gate even though `browsers_present` reports all
    three, because the other two are extras rather than prerequisites.
    """
    return browsers_present(status).get("chromium", False)
