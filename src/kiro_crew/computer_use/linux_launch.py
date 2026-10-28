"""Application catalog and launcher for the Linux computer-use driver.

The catalog is the freedesktop ``.desktop`` entries in SYSTEM directories only. That
restriction is the security property, not a simplification: a ``.desktop`` file names
an arbitrary command line, and ``~/.local/share/applications`` is writable by the
agent's own user. Accepting it would let the agent write ``Exec=<anything>`` and then
"launch an application" to run it. So an entry is accepted only when the file and its
directory are root-owned and not group/world writable, and the resolved executable is
root-owned too. That mirrors the Windows driver's refusal of user-writable install
roots. The model passes a NAME; the argv comes from the vetted entry, with every
freedesktop field code (``%f``, ``%U``, ...) stripped so no caller text reaches it.
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

from kiro_crew.computer_use import linux_x11
from kiro_crew.computer_use.types import (
    MAX_LAUNCH_SUGGESTIONS,
    AmbiguousLaunchTarget,
    LaunchIdentity,
    NoSuchLaunchTarget,
)

#: System application directories. User directories are excluded on purpose (see the
#: module docstring).
APPLICATION_DIRS: tuple[str, ...] = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    "/var/lib/snapd/desktop/applications",
)

_FIELD_CODE = re.compile(r"%[fFuUdDnNickvm%]")


@dataclass(frozen=True)
class InstalledApp:
    desktop_id: str
    name: str
    argv: tuple[str, ...]
    wm_class: str


def _root_owned(path: str) -> bool:
    """Owned by someone else (root, or root seen through a user namespace) and not writable by us.

    The property that matters is "the agent's own user cannot rewrite this". Inside a
    sandbox with a user namespace, root-owned files show the overflow uid (65534)
    rather than 0, so the check is "not ours and not writable", not ``uid == 0``.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_uid == os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return not os.access(path, os.W_OK)


def _parse(path: str) -> "InstalledApp | None":
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # case-sensitive keys
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    if entry.get("Type", "") != "Application":
        return None
    if (
        entry.get("NoDisplay", "false").lower() == "true"
        or entry.get("Hidden", "false").lower() == "true"
    ):
        return None
    name = entry.get("Name", "").strip()
    exec_line = entry.get("Exec", "").strip()
    if not name or not exec_line:
        return None
    try:
        argv = [tok for tok in shlex.split(_FIELD_CODE.sub("", exec_line)) if tok]
    except ValueError:
        return None
    if not argv:
        return None
    exe = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if not exe or not _root_owned(os.path.realpath(exe)):
        return None
    argv[0] = exe
    return InstalledApp(
        desktop_id=os.path.basename(path)[: -len(".desktop")],
        name=name,
        argv=tuple(argv),
        wm_class=entry.get("StartupWMClass", "").strip(),
    )


def installed_apps() -> tuple[InstalledApp, ...]:
    found: dict[str, InstalledApp] = {}
    for directory in APPLICATION_DIRS:
        if not os.path.isdir(directory) or not _root_owned(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".desktop"):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path) or not _root_owned(path):
                continue
            app = _parse(path)
            if app is not None and app.desktop_id not in found:
                found[app.desktop_id] = app
    return tuple(found.values())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


#: display name -> extra window-matching keys, filled at resolve time so ``find`` can
#: recognise the window of an app whose WM_CLASS differs from its display name.
_match_keys: dict[str, set[str]] = {}


def match_keys(display: str) -> set[str]:
    return {_norm(display)} | _match_keys.get(display, set())


def resolve_target(query: str) -> "tuple[str, str]":
    """``(desktop_id, display_name)`` for *query*. Exact, then unique prefix."""
    wanted = _norm(query)
    if not wanted:
        raise NoSuchLaunchTarget()
    apps = installed_apps()
    exact = [a for a in apps if wanted in (_norm(a.name), _norm(a.desktop_id))]
    if not exact:
        exact = [
            a
            for a in apps
            if _norm(a.name).startswith(wanted) or _norm(a.desktop_id).startswith(wanted)
        ]
    if len(exact) == 1:
        app = exact[0]
        keys = {_norm(app.desktop_id), _norm(os.path.basename(app.argv[0]))}
        if app.wm_class:
            keys.add(_norm(app.wm_class))
        _match_keys[app.name] = keys
        return app.desktop_id, app.name
    if len(exact) > 1:
        names = ", ".join(sorted({a.name for a in exact})[:MAX_LAUNCH_SUGGESTIONS])
        raise AmbiguousLaunchTarget(names, len(exact))
    near = [a.name for a in apps if wanted in _norm(a.name)]
    raise NoSuchLaunchTarget(", ".join(sorted(near)[:MAX_LAUNCH_SUGGESTIONS]))


def target_identity(desktop_id: str, display: str) -> LaunchIdentity:
    return LaunchIdentity(display=display, key=desktop_id)


def spawn_detached(desktop_id: str) -> None:
    """Start the vetted entry's argv, detached from the gateway. Raises OSError."""
    app = next((a for a in installed_apps() if a.desktop_id == desktop_id), None)
    if app is None:
        raise OSError(2, f"{desktop_id} is no longer installed")
    subprocess.Popen(  # noqa: S603 - argv comes from a root-owned .desktop entry
        list(app.argv),
        cwd=os.path.expanduser("~"),
        env=linux_x11.session_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
