"""A fresh project folder for each new dashboard chat (``dashboard.session_folder_root``).

When the root is set, a dashboard chat created with no explicit project gets a newly
created subfolder of the root as its project directory, so unrelated pieces of work do
not share one directory. Empty means off, and the chat keeps the default project.

Two rules hold here rather than at the call site:

* **The root is a project directory first.** It is validated by the folder API's own
  project-path validator (absolute, not sensitive, an existing directory) plus the
  data-home overlap pre-flight the project endpoint applies, so the setting can never
  name a directory the user could not pick by hand. A UNC-shaped root is refused before
  anything resolves it, because resolving a share reaches the network.
* **The root is re-validated on every use.** The config file can be edited by hand, so
  the value written through the settings page is not trusted at creation time: a root
  that has since become sensitive, overlapping or missing is refused there too, and the
  caller falls back to the default project.

The folder itself is made with ``os.mkdir`` and then locked to its owner with
``restrict_dir_to_owner`` (mode ``0o700`` on POSIX, an owner-only inherited DACL on
Windows). ``mkdir`` is
exclusive, so an existing name is never adopted; a random suffix keeps two chats created
on the same day apart. What landed is re-checked as a real directory at the path that
was asked for before it is returned.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import secrets
import stat

from kiro_crew import platform_compat
from kiro_crew.hooks import is_unc_shape

logger = logging.getLogger(__name__)

#: Longest root the settings page accepts. A path is bounded by the platform well
#: below this; the cap only keeps a pasted blob out of the config file.
SESSION_FOLDER_ROOT_MAX_CHARS = 1024

#: How many random names a create tries before giving up. Six hex characters give
#: 16.7 million names per day, so a collision is already rare; the retries make it
#: practically impossible rather than unlikely.
_NAME_ATTEMPTS = 8

#: Owner-only: a session folder holds that chat's work files.
_FOLDER_MODE = 0o700


class SessionFolderError(Exception):
    """A session folder could not be created; the message says why."""


def validate_session_folder_root(raw: str) -> tuple[str, str | None]:
    """Resolve and vet a session-folder root.

    Returns ``(resolved, None)`` for a usable root, ``("", None)`` for an empty value
    (the feature is off), and ``("", reason)`` for a refused one. Synchronous and
    filesystem-touching: callers on the event loop run it through ``asyncio.to_thread``.
    """
    value = raw.strip()
    if not value:
        return "", None
    # A NUL cannot name a path; every path call below would raise ValueError on it
    # instead of answering, so it is refused here, before anything resolves it.
    if "\x00" in value:
        return "", "Session folder root must not contain a NUL character"
    if is_unc_shape(value):
        return "", "Session folder root must be a local directory"
    # Lazy: chat_folders imports the handlers package, which imports this module.
    from kiro_crew.dashboard.chat_folders import (
        _folder_project_overlap_denied,
        _validate_project_dir,
    )

    resolved, err = _validate_project_dir(value)
    if err:
        return "", err
    conflict = _folder_project_overlap_denied(resolved)
    if conflict is not None:
        return "", conflict
    return resolved, None


def create_session_folder(raw_root: str, *, today: _dt.date | None = None) -> str:
    """Create a new owner-only folder under *raw_root* and return its absolute path.

    The folder is named ``<YYYY-MM-DD>-<6 hex>``. Raises :class:`SessionFolderError`
    when the root is refused or the folder cannot be made; nothing is created then.
    Synchronous: callers on the event loop run it through ``asyncio.to_thread``.
    """
    root, err = validate_session_folder_root(raw_root)
    if err:
        raise SessionFolderError(err)
    if not root:
        raise SessionFolderError("Session folder root is not set")
    stamp = (today or _dt.date.today()).isoformat()
    for _ in range(_NAME_ATTEMPTS):
        path = os.path.join(root, f"{stamp}-{secrets.token_hex(3)}")
        try:
            os.mkdir(path, _FOLDER_MODE)
        except FileExistsError:
            continue
        except OSError as exc:
            raise SessionFolderError(f"Could not create a session folder: {exc.strerror}") from exc
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise SessionFolderError(f"Could not create a session folder: {exc.strerror}") from exc
        # The root was resolved, so the new child resolves to itself unless something
        # replaced it between the create and this check.
        if not stat.S_ISDIR(st.st_mode) or os.path.realpath(path) != path:
            raise SessionFolderError("The new session folder was replaced while being created")
        # `mkdir`'s mode is honoured on POSIX only; this also gives the folder an
        # owner-only, inherited DACL on Windows. A folder that cannot be locked
        # down is removed (it is still empty) rather than handed out as private.
        try:
            platform_compat.restrict_dir_to_owner(path)
        except OSError as exc:
            try:
                os.rmdir(path)
            except OSError:
                logger.warning("Could not remove unrestricted session folder %s", path)
            raise SessionFolderError(
                f"Could not make the session folder private: {exc.strerror or exc}"
            ) from exc
        return path
    raise SessionFolderError("Could not find a free session folder name")
