"""Pick a cached Chromium build that can load against this host's libcups.

Chromium builds from revision ~1234 on carry a hard (``U``) dynamic reference to
``ippValidateAttributes``, a libcups function older distributions do not export
(Amazon Linux 2 ships cups-libs 1.6.3). With ``BIND_NOW`` the dynamic linker
refuses to start such a binary there: it exits 127 with ``undefined symbol:
ippValidateAttributes`` before Playwright can talk to it. Older builds reference
the symbol weakly (``w``), which resolves to NULL and starts fine.

:func:`compatible_chromium_executable` answers one question for the launch config
(:func:`kiro_crew.browser_cli.launch.desired_config`): should ``playwright-cli`` be
pointed at a different cached build than the one it would pick? It returns a path
only when all of these hold, and ``None`` otherwise:

1. the host is Linux;
2. the build the CLI would launch (the ``chromium`` revision its ``browsers.json``
   requires, or the newest cached build when that cannot be read) hard-requires
   the symbol;
3. the system libcups the dynamic linker resolves (``ldconfig -p``) does not
   export it -- so a host with a current libcups is never downgraded just because
   an older build is still cached;
4. some COMPLETE cached build (one carrying Playwright's install-complete marker)
   references it only weakly, or not at all -- an interrupted extraction is never
   a fallback, however new it is.

Every probe is ``nm -D`` or ``ldconfig -p`` resolved through
:func:`kiro_crew.platform_compat.trusted_system_bin`, a read of the dynamic symbol
table that never runs the binary. A probe that cannot run counts as "no evidence"
and leaves the config unchanged.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.browser_cli.install import (
    _browsers_cache_dir,
    _download_complete,
    _required_revisions,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The libcups function newer Chromium builds hard-require.
CUPS_SYMBOL = "ippValidateAttributes"

#: The libcups soname Chromium links against.
_LIBCUPS = "libcups.so.2"

#: Where a ``chromium-<rev>`` cache directory keeps its binary, newest layout first.
_CHROME_SUBPATHS: tuple[str, ...] = ("chrome-linux64/chrome", "chrome-linux/chrome")

_NM_TIMEOUT_SECS = 10


def _run_tool(name: str, args: list[str]) -> str | None:
    """Stdout of the trusted system tool *name*, or ``None`` if it could not run.

    ``None`` covers no trusted binary, a timeout, an OS error and a non-zero exit.
    """
    tool = platform_compat.trusted_system_bin(name)
    if tool is None:
        return None
    try:
        result = subprocess.run(
            [tool, *args],
            capture_output=True,
            timeout=_NM_TIMEOUT_SECS,
            check=False,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return str(result.stdout)


def _symbol_binding(binary: Path) -> str | None:
    """The ``nm -D`` binding letter of :data:`CUPS_SYMBOL` in *binary*.

    ``""`` when the symbol is not listed at all, ``None`` when the table could
    not be read (no trusted ``nm``, a timeout, or a non-zero exit).
    """
    stdout = _run_tool("nm", ["-D", str(binary)])
    if stdout is None:
        return None
    for line in stdout.splitlines():
        parts = line.split()
        # "                 U ippValidateAttributes" or
        # "0000000000047f20 T ippValidateAttributes"
        if len(parts) >= 2 and parts[-1] == CUPS_SYMBOL:
            return parts[-2]
    return ""


def _system_libcups() -> Path | None:
    """The ``libcups.so.2`` the dynamic linker resolves, or ``None``.

    Read from the linker's own cache (``ldconfig -p``), never from
    ``LD_LIBRARY_PATH``: that can name agent-writable directories, and a planted
    copy would decide whether the gateway rewrites the launch config. A
    qualified 64-bit entry (``(libc6,x86-64)``, ``(libc6,AArch64)``) is preferred
    over a bare ``(libc6)`` one, which on a multilib host is the 32-bit copy.
    """
    stdout = _run_tool("ldconfig", ["-p"])
    if stdout is None:
        return None
    qualified: list[Path] = []
    bare: list[Path] = []
    for line in stdout.splitlines():
        # "\tlibcups.so.2 (libc6,x86-64) => <path>"
        head, sep, target = line.partition("=>")
        fields = head.split()
        if not sep or not fields or fields[0] != _LIBCUPS:
            continue
        path = Path(target.strip())
        try:
            usable = path.is_absolute() and path.is_file()
        except OSError:
            usable = False
        if not usable:
            continue
        (qualified if "," in head else bare).append(path)
    found = qualified or bare
    return found[0] if found else None


def _libcups_lacks_symbol() -> bool:
    """Whether the system libcups is known NOT to export :data:`CUPS_SYMBOL`.

    ``False`` when it exports it, and also when there is no evidence either way
    (no libcups found, or its table unreadable): the fallback is a downgrade,
    so it needs a positive reading.
    """
    libcups = _system_libcups()
    if libcups is None:
        return False
    binding = _symbol_binding(libcups)
    if binding is None:
        return False
    # Exported means defined: any binding other than undefined (U) or weak-undefined (w/v).
    return binding in ("", "U", "w", "v")


def _cached_chromium_builds(cache: Path) -> list[tuple[int, Path]]:
    """Cached ``chromium-<rev>`` builds with a binary present, newest first."""
    builds: list[tuple[int, Path]] = []
    try:
        entries = list(cache.iterdir())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.startswith("chromium-"):
            continue
        try:
            revision = int(name.removeprefix("chromium-"))
        except ValueError:
            continue
        for sub in _CHROME_SUBPATHS:
            binary = entry / sub
            try:
                present = binary.is_file()
            except OSError:
                # An untraversable build (a shared cache with another owner's
                # directory) is skipped: it can be neither the default nor a fallback.
                present = False
            if present:
                builds.append((revision, binary))
                break
    builds.sort(key=lambda build: build[0], reverse=True)
    return builds


def compatible_chromium_executable() -> str | None:
    """Path of a cached Chromium that can start on this host, when the default cannot.

    See the module docstring for the four conditions. Runs ``nm`` subprocesses,
    so callers on the event loop MUST offload it.

    Never raises ``OSError``: this runs on the gateway's startup path, before
    :func:`kiro_crew.browser_cli.launch.write_config` reaches its own error
    handling, so a filesystem error anywhere in the probe means "no evidence"
    and the unchanged config, never a gateway that fails to start.
    """
    try:
        return _compatible_chromium_executable()
    except OSError as exc:
        logger.warning("could not check cached Chromium builds against libcups: %s", exc)
        return None


def _compatible_chromium_executable() -> str | None:
    if not platform_compat.IS_LINUX:
        return None
    cache = _browsers_cache_dir()
    if cache is None:
        return None
    builds = _cached_chromium_builds(cache)
    if not builds:
        return None
    required = (_required_revisions() or {}).get("chromium")
    default = next((binary for rev, binary in builds if str(rev) == required), None)
    if default is None:
        default = builds[0][1]
    if _symbol_binding(default) != "U":
        return None
    if not _libcups_lacks_symbol():
        return None
    for revision, binary in builds:
        if binary == default:
            continue
        # Only a finished download: a half-extracted build may lack the executable
        # bit or its libraries, and pinning it would fail every launch.
        if _download_complete(cache / f"chromium-{revision}") is not True:
            continue
        if _symbol_binding(binary) in ("", "w", "v"):
            logger.warning(
                "Chromium at %s needs %s, which this host's libcups does not export; "
                "pointing playwright-cli at the compatible build %s",
                default,
                CUPS_SYMBOL,
                binary,
            )
            return str(binary)
    logger.warning(
        "Chromium at %s needs %s, which this host's libcups does not export, and no "
        "cached Chromium build can start without it; the browser will not launch "
        "until cups-libs is upgraded or a compatible build is installed",
        default,
        CUPS_SYMBOL,
    )
    return None
