"""The Linux launch catalog: desktop entries from root-owned system directories only.

``computer_launch_app`` on Linux resolves a NAME through this catalog and starts
the program itself; the upstream driver's ``launch_app`` is never called, because
it accepts a command line, tries a name as a shell command first and hands URLs
and paths to ``xdg-open``.

What makes a desktop entry launchable:

* it lives directly in one of :data:`SYSTEM_APPLICATION_DIRS`, and the file and
  every directory above it are owned by root and not writable by group or
  others. ``~/.local/share/applications`` (and ``XDG_DATA_HOME`` in general) is
  user-writable, so an agent shell could plant an entry there: it is never read;
* ``Type=Application``, not ``NoDisplay``/``Hidden``, not ``Terminal=true``, not a
  terminal emulator (typing into a terminal would sidestep the shell command
  floor every ``bash`` call passes);
* its ``Exec`` program resolves, on a fixed system ``PATH``, to a root-owned file
  that no group or other user can replace. Field codes (``%f``, ``%U`` ...) are
  removed, so no document, URL or argument from the caller is ever passed.
"""

from __future__ import annotations

import configparser
import os
import re
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.computer_use.types import (
    AmbiguousLaunchTarget,
    ComputerUseError,
    LaunchIdentity,
    NoSuchLaunchTarget,
)

SYSTEM_APPLICATION_DIRS: tuple[str, ...] = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    "/var/lib/snapd/desktop/applications",
)

#: Catalog directories whose entries are symlinks by design, and the root-owned
#: tree each link must resolve inside. Flatpak exports every app's ``.desktop``
#: file as a link into ``/var/lib/flatpak/app/<id>/current/active/export/``.
SYMLINKED_DIR_ROOTS: dict[str, str] = {
    "/var/lib/flatpak/exports/share/applications": "/var/lib/flatpak",
}

#: The PATH an Exec program is resolved on. Never the gateway's own PATH.
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/usr/games"

MAX_SUGGESTIONS = 5

_FIELD_CODE_RE = re.compile(r"%%|%[fFuUdDnNickvm]")

ERR_TERMINAL = (
    "'{name}' is a terminal; computer use does not open terminals, because typed text "
    "there would run as shell commands outside the command policy"
)
ERR_UNSAFE_PROGRAM = (
    "'{name}' cannot be launched: its program is not a root-owned file on the system "
    "path ({detail})"
)


@dataclass(frozen=True)
class DesktopEntry:
    name: str
    desktop_id: str
    argv: tuple[str, ...]
    terminal_emulator: bool
    path: str


def _secure_owner(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_uid == 0 and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _secure_chain(path: Path) -> bool:
    """*path* and each directory above it are root-owned and not group/other-writable."""
    current = path
    while True:
        if not _secure_owner(current):
            return False
        if current.parent == current:
            return True
        current = current.parent


def strip_field_codes(exec_line: str) -> "tuple[str, ...]":
    """``Exec`` split into argv with every ``%`` field code removed."""
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        return ()
    argv: list[str] = []
    for part in parts:
        cleaned = _FIELD_CODE_RE.sub(lambda m: "%" if m.group(0) == "%%" else "", part)
        if cleaned:
            argv.append(cleaned)
    return tuple(argv)


def parse_entry(path: Path, desktop_id: str) -> "DesktopEntry | None":
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    sec = parser["Desktop Entry"]
    if sec.get("Type", "") != "Application":
        return None
    if sec.get("NoDisplay", "").lower() == "true" or sec.get("Hidden", "").lower() == "true":
        return None
    name = sec.get("Name", "").strip()
    argv = strip_field_codes(sec.get("Exec", ""))
    if not name or not argv:
        return None
    categories = sec.get("Categories", "")
    terminal = sec.get("Terminal", "").lower() == "true" or "TerminalEmulator" in categories
    return DesktopEntry(
        name=name, desktop_id=desktop_id, argv=argv, terminal_emulator=terminal, path=str(path)
    )


def _entry_file(path: Path, link_root: "str | None") -> "Path | None":
    """The file to read for catalog entry *path*, or ``None`` to skip it.

    A plain root-owned file is read as is. A symlink is followed only in a
    directory listed in :data:`SYMLINKED_DIR_ROOTS`, and only when it resolves
    to a root-owned file inside that directory's tree whose every parent is
    root-owned and not group/other-writable.
    """
    if not path.is_symlink():
        return path if path.is_file() and _secure_owner(path) else None
    if link_root is None:
        return None
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    base = Path(link_root)
    if base not in target.parents or not target.is_file():
        return None
    if not _secure_owner(target) or not _secure_chain(target.parent):
        return None
    return target


def installed_apps(
    dirs: "tuple[str, ...]" = SYSTEM_APPLICATION_DIRS,
    link_roots: "dict[str, str] | None" = None,
) -> "tuple[DesktopEntry, ...]":
    roots = SYMLINKED_DIR_ROOTS if link_roots is None else link_roots
    seen: dict[str, DesktopEntry] = {}
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir() or not _secure_chain(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            path = _entry_file(root / name, roots.get(directory))
            if path is None:
                continue
            desktop_id = name[: -len(".desktop")]
            if desktop_id in seen:
                continue
            entry = parse_entry(path, desktop_id)
            if entry is not None:
                seen[desktop_id] = entry
    return tuple(seen.values())


def resolve_program(program: str) -> str:
    """The absolute, root-owned program *program* names, or raise ``ComputerUseError``."""
    if os.path.isabs(program):
        candidate: "str | None" = program
    else:
        candidate = shutil.which(program, path=SYSTEM_PATH)
    if not candidate:
        raise ValueError("not found on the system path")
    real = Path(os.path.realpath(candidate))
    if not real.is_file() or not os.access(real, os.X_OK):
        raise ValueError("not an executable file")
    if not _secure_chain(real):
        raise ValueError("owned by a non-root user or writable by others")
    return str(real)


def _matches(entry: DesktopEntry, wanted: str) -> bool:
    return wanted in (
        entry.name.casefold(),
        entry.desktop_id.casefold(),
        os.path.basename(entry.argv[0]).casefold(),
    ) or entry.desktop_id.casefold().endswith("." + wanted)


def resolve_target(
    query: str, *, catalog: "tuple[DesktopEntry, ...] | None" = None
) -> "tuple[str, str]":
    """Resolve *query* to ``(argv joined by NUL, display name)``, or raise."""
    entries = installed_apps() if catalog is None else catalog
    wanted = query.strip().casefold()
    exact = [e for e in entries if _matches(e, wanted)]
    if not exact:
        prefixed = [e for e in entries if e.name.casefold().startswith(wanted)]
        if len(prefixed) > 1:
            names = ", ".join(sorted(e.name for e in prefixed)[:MAX_SUGGESTIONS])
            raise AmbiguousLaunchTarget(names, len(prefixed))
        exact = prefixed
    if not exact:
        near = sorted({e.name for e in entries if wanted in e.name.casefold()})
        raise NoSuchLaunchTarget(", ".join(near[:MAX_SUGGESTIONS]))
    entry = exact[0]
    if entry.terminal_emulator:
        raise ComputerUseError(ERR_TERMINAL.format(name=entry.name))
    try:
        program = resolve_program(entry.argv[0])
    except ValueError as exc:
        raise ComputerUseError(ERR_UNSAFE_PROGRAM.format(name=entry.name, detail=exc)) from exc
    return "\0".join((program, *entry.argv[1:])), entry.name


def target_identity(target: str, display: str) -> LaunchIdentity:
    return LaunchIdentity(display=display, key=os.path.basename(target.split("\0", 1)[0]))


def spawn_detached(target: str, env: "dict[str, str] | None" = None) -> None:
    """Start *target* (from :func:`resolve_target`) detached, with no extra arguments."""
    argv = target.split("\0")
    subprocess.Popen(  # noqa: S603 - argv is a vetted, root-owned desktop entry
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=os.path.expanduser("~"),
        env=env,
    )


__all__ = [
    "DesktopEntry",
    "SYSTEM_APPLICATION_DIRS",
    "installed_apps",
    "parse_entry",
    "resolve_program",
    "resolve_target",
    "spawn_detached",
    "strip_field_codes",
    "target_identity",
]
