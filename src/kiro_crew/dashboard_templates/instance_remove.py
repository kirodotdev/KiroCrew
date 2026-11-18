"""Take back the dashboard a ``crewmate.import`` card adopted, only while it is still that page.

A sibling of :mod:`kiro_crew.dashboard_templates.instance`, which owns the store, its
lock and its commit; kept out of the store module for its size alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import instance as _store

__all__ = ["remove_adopted"]


def _open_member_dashboard(slug: str) -> int | None:
    """The member's ``dashboard`` directory, opened one ``O_NOFOLLOW`` step at a time.

    ``None`` when it does not exist. A link anywhere below the data home is refused,
    and the walk starts at the descriptor of ``members``, so nothing is re-joined by name.
    The walk starts where :func:`~kiro_crew.dashboard_templates.instance._pinned_session_dir` starts it: at ``members``, whose
    parent is resolved. A data home that is itself a link (the default ``~/.kiro/crew`` on
    another disk) is the operator's own layout, and opening it with ``O_NOFOLLOW`` would
    refuse every Undo.
    """
    if _store.is_session_key(slug) or slug in (".", "..") or Path(slug).name != slug:
        raise _store.InstanceError("a member slug is one path segment")
    if not pinned_fs.supports_pinned_walk():
        raise _store.PinUnsupported(
            "this platform cannot remove a dashboard without following links"
        )
    try:
        fd = pinned_fs.open_dir_pinned(
            data_home() / "members", what="members folder", refusal=_store.InstanceError
        )
    except FileNotFoundError:
        return None
    try:
        for part in (slug, "dashboard"):
            child = os.open(part, pinned_fs.dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
    except FileNotFoundError:
        os.close(fd)
        return None
    except (OSError, ValueError) as exc:  # ValueError: a NUL character in the slug
        os.close(fd)
        raise _store.InstanceError("a linked dashboard path is not followed") from exc
    return fd


def remove_adopted(slug: str, template_id: str, adopted_version: int) -> bool:
    """Remove the dashboard an import card adopted, only while it is still that page.

    The page is removed only when the record still names *template_id* at exactly
    *adopted_version*: any later apply, edit or rollback is the owner's and stays, and
    ``False`` says so. The record and every retained version go through descriptors
    pinned at the member's own ``dashboard`` directory (a link anywhere on the way is
    refused), never by name, and the lock is held for the whole removal. A staged preview
    and the history file go with them, so a later adopt on the same slug starts again at
    version 1 with no row of the removed import; only the empty directory and its lock file
    stay. The ``versions`` folder is opened pinned before the first delete, so a link in its
    place refuses the removal with nothing lost. The import card adopts only onto a slug that
    held nothing, readable or not (``instance_adopt.adopt_if_empty``), so what is removed here
    is the history this import began: a record that was there before is never adopted over and
    never reaches this function.
    shortcut: a package page snapshot under ``crew-panels`` is left to its own pruning.

    Raises :class:`~kiro_crew.dashboard_templates.instance.PinUnsupported` where the walk cannot be pinned (native Windows): the
    caller leaves the page and goes on, as a plain Bot delete does.
    """
    pinned = _open_member_dashboard(slug)
    if pinned is None:
        return False
    try:
        with _store._locked(_store.instance_dir(slug), pinned_dir=pinned):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                with os.fdopen(os.open(_store._RECORD_FILE, flags, dir_fd=pinned), "rb") as handle:
                    raw = json.loads(handle.read().decode("utf-8"))
            except FileNotFoundError:
                return False
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _store.InstanceError("the dashboard record cannot be read") from exc
            template = raw.get("template") if isinstance(raw, dict) else None
            if not isinstance(template, dict) or template.get("id") != template_id:
                return False
            if raw.get("instance_version") != adopted_version:
                return False
            # Pin the versions folder BEFORE anything is deleted: a folder that cannot be
            # opened without following a link refuses the whole removal, nothing lost.
            try:
                versions_fd = os.open(_store._VERSIONS_SUBDIR, pinned_fs.dir_flags(), dir_fd=pinned)
            except FileNotFoundError:
                versions_fd = None
            except OSError as exc:
                raise _store.InstanceError(
                    "the dashboard versions folder is a link; not removed"
                ) from exc
            try:
                for leftover in (_store._PREVIEW_FILE, _store._HISTORY_FILE):
                    try:
                        os.unlink(leftover, dir_fd=pinned)
                    except FileNotFoundError:
                        pass
                if versions_fd is not None:
                    for name in os.listdir(versions_fd):
                        os.unlink(name, dir_fd=versions_fd)
                    os.rmdir(_store._VERSIONS_SUBDIR, dir_fd=pinned)
                # The record goes LAST: a removal that stops half way leaves the record
                # that still names the adopted page, so the Undo can be retried.
                os.unlink(_store._RECORD_FILE, dir_fd=pinned)
            except OSError as exc:
                raise _store.InstanceError("the dashboard could not be removed completely") from exc
            finally:
                if versions_fd is not None:
                    os.close(versions_fd)
            return True
    finally:
        os.close(pinned)
