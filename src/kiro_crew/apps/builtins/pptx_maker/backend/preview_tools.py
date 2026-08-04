"""PPTX Maker — installing the app-managed preview tools.

Slide thumbnails need two binaries the engine shells out to by name, and a stock
machine has neither:

* ``pdftoppm`` (poppler) — rasterizes the PDF to per-page PNGs. **Provided here,
  on every OS, with no download**: ``pypdfium2`` is already a dependency of the
  engine's own venv, so this module only has to write a launcher named
  ``pdftoppm`` that runs :mod:`.pdftoppm_shim` inside that venv.
* ``soffice`` (LibreOffice) — converts the .pptx to PDF. **Not installable from
  here**, for the cost-and-coverage reasons recorded in
  ``docs/system-specs/modules/pptx-maker.md`` § Preview tools (the authority for
  this decision): it publishes only OS installers, none of them an
  unpack-and-run tree, and the Windows ``.msi``'s cab is LZX-compressed, which
  the Python stdlib cannot decompress at all. So it is reported with a per-OS
  instruction instead (:func:`soffice_hint`), and the user's own install is
  picked up automatically.

Why one bin dir: the engine resolves both tools with ``shutil.which()`` inside the
``sdpm`` MCP server that kiro-cli spawns, so whatever is installed must sit on THAT
process's ``PATH``. :func:`.paths.preview_tools_bin` is that single directory, and
:func:`.provision.mcp_tools_path` is what renders it into the agent config's
``mcpServers.sdpm.env.PATH`` — appended, so a real system tool still wins the
engine's own lookup.

Everything here is BLOCKING (filesystem) and must run through ``routes.off_loop``.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.pptx_maker.backend import paths
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger("kirocrew.app.pptx-maker")

#: The tool this module can provide, and the module that implements it.
PDFTOPPM = "pdftoppm"
#: The tool it deliberately cannot — a system package, reported with a hint.
SOFFICE = "soffice"
_SHIM_MODULE = "kiro_crew.apps.builtins.pptx_maker.backend.pdftoppm_shim"

#: Mode for the generated launcher: OWNER-ONLY rwx. It lives under the data home and
#: is executed by the gateway's own engine children, so nothing else needs to read or
#: run it — and a file the engine execs must not be group- or world-writable.
_LAUNCHER_MODE = 0o700

#: Per-OS instruction for the one tool that cannot be auto-installed. Shown by the
#: UI verbatim; the user runs it themselves, which is the documented contract for
#: a system package.
_SOFFICE_HINTS = {
    "darwin": "brew install --cask libreoffice",
    "linux": "sudo apt install libreoffice  (or: sudo dnf install libreoffice)",
    "win32": "winget install TheDocumentFoundation.LibreOffice",
}


def soffice_hint() -> str:
    """The install command for LibreOffice on this host.

    Returned as data for the UI to display, never executed here: running a package
    manager on the operator's behalf from a browser request is the host mutation
    this app refuses.
    """
    if sys.platform.startswith("linux"):
        return _SOFFICE_HINTS["linux"]
    return _SOFFICE_HINTS.get(sys.platform, "install LibreOffice for your platform")


def _engine_python() -> Path | None:
    """The engine venv interpreter that owns ``pypdfium2``, or ``None``.

    ``pypdfium2`` lives in the ENGINE's venv, not the gateway's, so the shim has to
    run under this interpreter. ``None`` means the engine is not provisioned yet,
    which the caller reports as "provision the engine first" rather than writing a
    launcher that could not work.
    """
    candidates = [paths.engine_python()]
    # The engine's venv layout is POSIX-shaped in `paths`; Windows venvs put the
    # interpreter under Scripts/ instead.
    if platform_compat.IS_WINDOWS:
        candidates.insert(0, paths.engine_root() / "mcp-local" / ".venv" / "Scripts" / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _launcher_paths() -> list[Path]:
    """Every launcher file installed for ``pdftoppm`` on this platform.

    Windows gets a ``.cmd`` because that is what ``shutil.which`` will resolve for
    a bare ``pdftoppm`` (it honours ``PATHEXT``), and an extensionless shell script
    is not executable there. POSIX gets the extensionless name poppler uses.
    """
    bin_dir = paths.preview_tools_bin()
    if platform_compat.IS_WINDOWS:
        return [bin_dir / f"{PDFTOPPM}.cmd"]
    return [bin_dir / PDFTOPPM]


def _launcher_body(python: Path, shim: Path) -> str:
    """The launcher script text.

    Both forms exec the engine's interpreter against the shim module FILE, not
    ``-m``: ``-m`` would need the gateway package importable from the engine's
    venv, which it deliberately is not. Arguments are forwarded verbatim so the
    engine's own ``argv`` reaches the shim unaltered.

    The engine venv's own console scripts cannot be reused for this — their
    shebangs point at the temporary staging directory the install swapped away, so
    they are unrunnable. Naming the interpreter and the file directly avoids that
    entirely.

    Both paths are QUOTED FOR THEIR SHELL, not merely wrapped in double quotes.
    ``KIROCREW_HOME`` is user-chosen, so either path can contain a character its
    shell would otherwise interpret: inside POSIX double quotes ``$`` and
    backticks still expand and ``\\`` still escapes, so a data home like
    ``/tmp/my $home/`` produced a launcher pointing at a path with the ``$home``
    segment silently deleted, and every invocation failed with "cannot execute".
    :func:`shlex.quote` (single quotes, with embedded single quotes escaped)
    removes every such expansion.
    """
    if platform_compat.IS_WINDOWS:
        # cmd.exe has no quoting function in the stdlib. Double quotes are the
        # right container for a path with spaces, and `%` is the only character
        # cmd expands inside them — escaped by doubling. A literal `"` cannot
        # appear in a Windows path at all, so there is nothing else to escape.
        # `%*` forwards every argument verbatim.
        return "@echo off\r\n" f'"{_cmd_escape(str(python))}" "{_cmd_escape(str(shim))}" %*\r\n'
    # `exec` keeps a single process so the engine's `subprocess.run` sees the
    # shim's real exit code, not a wrapper's. `"$@"` forwards argv unaltered.
    return f'#!/bin/sh\nexec {shlex.quote(str(python))} {shlex.quote(str(shim))} "$@"\n'


def _cmd_escape(value: str) -> str:
    """Escape *value* for use inside double quotes in a ``.cmd`` script.

    Only ``%`` needs it: cmd.exe expands ``%VAR%`` and ``%1`` even within double
    quotes, and doubling the sign is how a literal percent is written.
    """
    return value.replace("%", "%%")


def pdftoppm_installed() -> bool:
    """True when the managed ``pdftoppm`` launcher is present and executable."""
    for launcher in _launcher_paths():
        try:
            if not launcher.is_file():
                return False
        except OSError:
            return False
        if not platform_compat.IS_WINDOWS and not os.access(launcher, os.X_OK):
            return False
    return True


def install_pdftoppm() -> tuple[bool, str]:
    """Install the managed ``pdftoppm``. Returns ``(ok, message)``.

    Idempotent: rewrites the launcher every time, so re-running after an engine
    update re-points it at the current interpreter (the engine tree is replaced on
    each version bump, but its path is stable, so this is belt-and-braces).

    BLOCKING — call through ``routes.off_loop``.
    """
    python = _engine_python()
    if python is None:
        return False, "the presentation engine is not installed yet — install it first"

    shim = Path(__file__).resolve().parent / "pdftoppm_shim.py"
    if not shim.is_file():
        # Would mean a broken package install; reported rather than written blind.
        return False, "the bundled pdftoppm shim is missing from this install"

    bin_dir = paths.preview_tools_bin()
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create {bin_dir}: {exc}"

    body = _launcher_body(python, shim)
    for launcher in _launcher_paths():
        try:
            # `newline=""` disables universal-newline translation, so the line
            # endings written are exactly the ones `_launcher_body` chose: the
            # `.cmd` form already spells its own `\r\n`, which the default
            # translation would turn into `\r\r\n` on Windows.
            atomic_write(launcher, body, newline="")
            # chmod AFTER the write and before any caller can resolve it, so the
            # launcher is never observable without its exec bit.
            if not platform_compat.IS_WINDOWS:
                platform_compat.chmod_safe(launcher, _LAUNCHER_MODE)
        except OSError as exc:
            return False, f"cannot write {launcher.name}: {exc}"

    if not pdftoppm_installed():
        return False, "the pdftoppm launcher failed its post-install check"
    logger.info("pptx-maker: installed the managed pdftoppm launcher in %s", bin_dir)
    return True, "pdftoppm is ready"


def _mode_is_safe(path: Path) -> bool:
    """True when *path* is not group- or world-writable.

    The launcher is executed by the engine, so a writable one would be a way to
    run arbitrary code as the gateway user. Checked on the status path so a bad
    mode is visible rather than silently trusted.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return not bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def managed_status() -> dict[str, object]:
    """What the UI needs to describe the managed tools.

    BLOCKING (filesystem probes) — call through ``routes.off_loop``.
    """
    launchers = _launcher_paths()
    return {
        "pdftoppmInstalled": pdftoppm_installed(),
        "pdftoppmSecure": all(_mode_is_safe(p) for p in launchers if p.is_file()),
        "engineReady": _engine_python() is not None,
        "binDir": str(paths.preview_tools_bin()),
        "sofficeHint": soffice_hint(),
    }
