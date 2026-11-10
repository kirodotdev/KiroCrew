"""The owner's decisions about each installed app, kept where the app cannot write them.

An app's own directory (``apps/<name>/``) is writable from the sandbox its code runs in,
and the directory itself can be renamed aside and recreated. So nothing there can carry
a decision only the owner may make. Two such decisions live here instead, under
``<data home>/member-records/``, which the OS sandbox masks in every mode and the agent
file tools refuse (:mod:`kiro_crew.member_records`):

* ``app-grants/<app>.json`` -- which of the app's declared staged permission entries
  (``permissions.api``, ``permissions.events`` and the point families) the owner
  approved. ``manager_records`` reads and writes it through
  ``_read_approved_grants`` / ``_write_approved_grants``.
* ``app-disabled/<app>`` -- present while the owner's disable of the app stands. Every
  read of an app's ``installed.json`` reports the app disabled while it exists, whatever
  the file says; only the owner-authenticated enable and an uninstall remove it.

Inside a sandbox the directory is masked, so a read there finds neither record: an
approved set reads as empty and no disable is seen. Authorization is decided in the
gateway, which reads both.

This module imports nothing from the ``apps.manager`` family, so its lowest owner can
call it.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import member_records
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

#: Record kinds under ``member-records/``; both are in ``member_records.RECORD_KINDS``.
APP_GRANTS_KIND = "app-grants"
APP_DISABLED_KIND = "app-disabled"

#: Written in ``app-grants/`` once every app's set has been carried over from the app
#: tree. Once it exists a missing record means nothing is approved.
GRANTS_IMPORT_MARKER = ".imported-from-app-tree"
#: Held from the import's marker check to its marker write, across threads and processes.
_GRANTS_IMPORT_LOCK = ".importing"


def _record_path(kind: str, app_name: str, suffix: str) -> Path:
    """``member-records/<kind>/<app><suffix>``, containment-checked. Raises ``ValueError``."""
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(f"unsafe app name: {app_name!r}")
    root = member_records.records_dir(kind).resolve()
    target = (root / f"{app_name}{suffix}").resolve()
    if target.parent != root:
        raise ValueError(f"app name {app_name!r} escapes {root}")
    return target


def grants_path(app_name: str) -> Path:
    """Where *app_name*'s approved set lives. Raises ``ValueError`` for an unsafe name."""
    return _record_path(APP_GRANTS_KIND, app_name, ".json")


def grants_imported() -> bool:
    """Whether the one-time carry-over from the app trees has finished."""
    return (member_records.records_dir(APP_GRANTS_KIND) / GRANTS_IMPORT_MARKER).is_file()


def mark_grants_imported() -> None:
    directory = member_records.records_dir(APP_GRANTS_KIND)
    mkdirs_owner_only(directory)
    atomic_write(directory / GRANTS_IMPORT_MARKER, "1\n")


@contextlib.contextmanager
def grants_import_lock() -> Iterator[None]:
    """Serialize the one-time import: one importer at a time, in any thread or process."""
    directory = member_records.records_dir(APP_GRANTS_KIND)
    mkdirs_owner_only(directory)
    fd = open_create_or_existing(
        directory / _GRANTS_IMPORT_LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def write_grants_text(app_name: str, text: str | None) -> None:
    """Store *app_name*'s set as *text*; ``None`` removes it."""
    path = grants_path(app_name)
    if text is None:
        path.unlink(missing_ok=True)
        return
    mkdirs_owner_only(path.parent)
    atomic_write(path, text)


def read_grants_text(app_name: str) -> str | None:
    """The stored set's text, or ``None`` when there is none. Other read faults raise."""
    try:
        return grants_path(app_name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _disabled_path(app_name: str) -> Path:
    return _record_path(APP_DISABLED_KIND, app_name, "")


def owner_disabled(app_name: str) -> bool:
    """Whether the owner's disable of *app_name* stands.

    A name this store cannot hold has no record. A record that cannot be checked for
    any other reason reads as standing, so a fault never turns an app back on.
    """
    try:
        path = _disabled_path(app_name)
    except ValueError:
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def set_owner_disabled(app_name: str, disabled: bool) -> None:
    """Record, or end, the owner's disable of *app_name*."""
    path = _disabled_path(app_name)
    if disabled:
        mkdirs_owner_only(path.parent)
        atomic_write(path, "1\n")
    else:
        path.unlink(missing_ok=True)


def snapshot_app(app_name: str) -> tuple[str | None, bool]:
    """Both records of *app_name*, for :func:`restore_app` after a failed uninstall."""
    return read_grants_text(app_name), owner_disabled(app_name)


def restore_app(app_name: str, snapshot: tuple[str | None, bool]) -> None:
    """Put back what :func:`snapshot_app` read; the disable first, so a fault keeps it."""
    grants_text, disabled = snapshot
    set_owner_disabled(app_name, disabled)
    write_grants_text(app_name, grants_text)


def forget_grants(app_name: str) -> None:
    """Remove the approved set, for an uninstall: a later app under the name starts clean.

    The disable record stays until the uninstall commits (``uninstall_app`` removes
    it then), so the app is never re-enabled while its tree is still on disk.
    """
    with contextlib.suppress(ValueError):
        grants_path(app_name).unlink(missing_ok=True)
