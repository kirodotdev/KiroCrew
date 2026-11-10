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
* ``app-secrets/<app>`` -- the secret the gateway issued the app, which trades for its
  app token (``dashboard.token_auth_tokens.validate_app_secret``) and keys the proxy's
  HMAC. ``member-records/`` is created and masked before every spawn
  (``sandbox_precreate``), so an app installed while a sandbox runs still gets its
  secret where that sandbox cannot look. The app's own backend gets it as
  ``KIROCREW_PROXY_SECRET`` (``apps/backend.py``).

Inside a sandbox the directory is masked, so a read there finds no record: an
approved set reads as empty, no disable is seen and no secret is found. Authorization
is decided in the gateway, which reads them all.

This module imports nothing from the ``apps.manager`` family, so its lowest owner can
call it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import member_records
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

logger = logging.getLogger(__name__)

#: Record kinds under ``member-records/``; all are in ``member_records.RECORD_KINDS``.
APP_GRANTS_KIND = "app-grants"
APP_DISABLED_KIND = "app-disabled"
APP_SECRETS_KIND = "app-secrets"

#: Written in ``app-grants/`` once every app's set has been carried over from the app
#: tree. Once it exists a missing record means nothing is approved.
GRANTS_IMPORT_MARKER = ".imported-from-app-tree"
#: Held from the import's marker check to its marker write, across threads and processes.
_GRANTS_IMPORT_LOCK = ".importing"
#: Held across a check-then-issue of an app secret (:func:`app_secrets_lock`).
_SECRETS_LOCK = ".issuing"


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


def app_secret_path(app_name: str) -> Path:
    """Where *app_name*'s secret lives. Raises ``ValueError`` for an unsafe name."""
    return _record_path(APP_SECRETS_KIND, app_name, "")


def read_app_secret(app_name: str) -> str:
    """The secret the gateway issued *app_name*, or ``""`` when it has none.

    A name this store cannot hold has none. Any other read fault raises, so a caller
    deciding whether to mint or sign refuses rather than reading it as absent.
    """
    try:
        path = app_secret_path(app_name)
    except ValueError:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_app_secret(app_name: str, secret: str) -> None:
    """Store *secret* as *app_name*'s, owner-only. Raises ``OSError`` or ``ValueError``."""
    path = app_secret_path(app_name)
    mkdirs_owner_only(path.parent)
    atomic_write(path, secret, restrict_to_owner=True)


@contextlib.contextmanager
def app_secrets_lock() -> Iterator[None]:
    """Serialize a check-then-issue of any app's secret, across threads and processes.

    Innermost: nothing else is acquired while it is held.
    """
    directory = member_records.records_dir(APP_SECRETS_KIND)
    mkdirs_owner_only(directory)
    fd = open_create_or_existing(
        directory / _SECRETS_LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def issue_app_secret_if_absent(app_name: str, legacy_file: Path | None = None) -> str:
    """*app_name*'s stored secret, first issuing a NEW one when it has none.

    With *legacy_file* (the ``.app_secret`` an older build wrote in the app directory),
    one is issued only when that file exists. The file is never copied: a sandbox can
    read and replace it, so the app gets a secret only the gateway knows, and once the
    store holds one the file is removed (never followed through a link). Called where
    nothing holds a secret yet (a backend about to be spawned, the owner's enable, boot
    registration). Under :func:`app_secrets_lock`, so concurrent callers all get the
    same secret. Returns ``""`` when none is issued. Raises ``OSError``/``ValueError``.
    """
    with app_secrets_lock():
        stored = read_app_secret(app_name)
        if not stored and (legacy_file is None or legacy_file.is_file()):
            stored = secrets.token_hex(32)
            write_app_secret(app_name, stored)
    if stored and legacy_file is not None:
        remove_legacy_secret(legacy_file)
    return stored


def remove_legacy_secret(legacy_file: Path) -> None:
    """Remove the ``.app_secret`` an older build left, which trades for nothing.

    Called once the store holds the app's secret, so a dead credential does not stay
    readable in every sandbox. A link is removed, never followed. A failure leaves only
    that dead file, so it is logged, never raised.
    """
    try:
        legacy_file.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove %s", legacy_file, exc_info=True)


def forget_app_secret(app_name: str) -> None:
    """Remove *app_name*'s secret, once its uninstall committed. Raises ``OSError``."""
    with contextlib.suppress(ValueError):
        app_secret_path(app_name).unlink(missing_ok=True)
