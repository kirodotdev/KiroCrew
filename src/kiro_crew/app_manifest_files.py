"""An installed app's ``app.json``: refused to file edits, sealed read-only at spawn.

The gateway reads ``<data home>/apps/<name>/app.json`` to decide the MCP servers,
tools and agents an app brings. Only the gateway's own install and update writes
it; an agent asks with ``request_server`` instead. The file-tool refusal matches
by place, without a listing. The sandbox seals existing regular manifests, with
folders guarded against rename. Other entries are skipped and logged.

No hashing and nothing on the gateway's boot path. Linux directory owner bits are
restored through held descriptors; a seal never stops a session from starting.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Iterator

from kiro_crew.apps.credential_masks import APPS_LEAF
from kiro_crew.platform_compat import effective_uid

if TYPE_CHECKING:
    from kiro_crew.security.paths_targets import _ResolvedRoots

logger = logging.getLogger(__name__)

#: An app's manifest, inside its installed folder.
MANIFEST_NAME = "app.json"


def app_manifest_write(
    path_str: str, candidates: set[str], roots: _ResolvedRoots | None = None
) -> bool:
    """Whether one of *candidates* (the spellings of *path_str*) is an installed app's manifest.

    That is ``app.json`` directly inside a folder of the data home's ``apps/``, at
    either data-home prefix or a relocated home, compared case-folded as the gate
    compares every target. *roots* is the gate decision's resolved anchors. A folder
    whose name starts with ``.`` holds no installed app (the lifecycle's move-aside
    names), so it is not matched.
    """
    if not path_str or not candidates:
        return False
    # Call-time imports: the security package imports this module at load.
    from kiro_crew.security.paths_lists import _CREW_HOME_PREFIXES
    from kiro_crew.security.paths_targets import _home_dir_targets

    apps_roots = _home_dir_targets(
        [f"{prefix}/{APPS_LEAF}" for prefix in _CREW_HOME_PREFIXES],
        roots=roots,
    )
    for cand in candidates:
        folded = os.path.normpath(cand).casefold()
        folder = os.path.dirname(folded)
        if (
            os.path.basename(folded) == MANIFEST_NAME
            and not os.path.basename(folder).startswith(".")
            and os.path.dirname(folder) in apps_roots
        ):
            return True
    return False


@contextmanager
def _restored_directory(parent_fd: int, name: str, path: str) -> Iterator[int | None]:
    """Hold a Linux directory, restore its owner bits, and verify the reading descriptor."""
    from kiro_crew.pinned_fs import dir_flags

    path_fd = -1
    dir_fd = None
    try:
        path_fd = os.open(
            name, getattr(os, "O_PATH") | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd
        )
        info = os.fstat(path_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != effective_uid():
            raise ValueError("not an owned directory")
        mode = stat.S_IMODE(info.st_mode)
        if mode & stat.S_IRWXU != stat.S_IRWXU:
            os.chmod(f"/proc/self/fd/{path_fd}", mode | stat.S_IRWXU)
            logger.warning("app manifest seal: restored owner access to %s", path)
        dir_fd = os.open(name, dir_flags(), dir_fd=parent_fd)
        opened = os.fstat(dir_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("directory identity changed")
    except (OSError, ValueError) as error:
        if dir_fd is not None:
            os.close(dir_fd)
            dir_fd = None
        if not isinstance(error, OSError) or error.errno != errno.ENOENT:
            log = logger.warning
            if isinstance(error, OSError) and error.errno == errno.ENOTDIR:
                try:
                    mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
                    if not stat.S_ISLNK(mode) and not stat.S_ISDIR(mode):
                        log = logger.info
                except OSError:
                    pass
            log("app manifest seal: skipped %s (%s)", path, error)
    finally:
        if path_fd >= 0:
            os.close(path_fd)
    try:
        yield dir_fd
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def app_manifest_leaves(home: str) -> tuple[str, ...]:
    """Existing regular manifests, with Linux owner-bit restoration through descriptors."""
    leaves: list[str] = []
    try:
        home = os.path.realpath(home)
        apps = os.path.join(home, APPS_LEAF)
        linux = sys.platform.startswith("linux")
        with ExitStack() as stack:
            apps_fd = None
            if linux:
                from kiro_crew.pinned_fs import open_dir_pinned

                parent_fd = open_dir_pinned(os.path.dirname(home), what="app manifest data home")
                stack.callback(os.close, parent_fd)
                home_fd = stack.enter_context(
                    _restored_directory(parent_fd, os.path.basename(home), home)
                )
                if home_fd is None:
                    return ()
                apps_fd = stack.enter_context(_restored_directory(home_fd, APPS_LEAF, apps))
                if apps_fd is None:
                    return ()
            names = sorted(os.listdir(apps_fd if apps_fd is not None else apps))
            for name in names:
                if name.startswith("."):
                    continue
                folder = os.path.join(apps, name)
                manifest = os.path.join(folder, MANIFEST_NAME)
                try:
                    with ExitStack() as app_stack:
                        app_fd = None
                        if apps_fd is not None:
                            app_fd = app_stack.enter_context(
                                _restored_directory(apps_fd, name, folder)
                            )
                            if app_fd is None:
                                continue
                        else:
                            folder_mode = os.lstat(folder).st_mode
                            if not stat.S_ISDIR(folder_mode):
                                log = logger.warning if stat.S_ISLNK(folder_mode) else logger.info
                                log(
                                    "app manifest seal: skipped %s (a link or not a folder)", folder
                                )
                                continue
                        mode = (
                            os.stat(MANIFEST_NAME, dir_fd=app_fd, follow_symlinks=False).st_mode
                            if app_fd is not None
                            else os.lstat(manifest).st_mode
                        )
                        if not stat.S_ISREG(mode):
                            logger.warning(
                                "app manifest seal: skipped %s (a link or not a file)", manifest
                            )
                            continue
                        leaves.append(f"{APPS_LEAF}/{name}/{MANIFEST_NAME}")
                except (OSError, ValueError) as error:
                    if not isinstance(error, OSError) or error.errno not in (
                        errno.ENOENT,
                        errno.ENOTDIR,
                    ):
                        logger.warning("app manifest seal: skipped %s (%s)", manifest, error)
    except Exception as error:
        if not isinstance(error, OSError) or error.errno != errno.ENOENT:
            logger.warning("app manifest seal: skipped %s (%s)", home, error)
    return tuple(leaves)


def app_manifest_targets(os_home: str) -> tuple[str, ...]:
    """Absolute spellings of the existing app manifests the sandbox can seal.

    Join each leaf under both data-home prefixes and a relocated home, keeping
    only regular files. A spelling that cannot be inspected is logged and skipped.
    """
    # Call-time imports: the sandbox modules import this one at spawn, not at load.
    from kiro_crew.config.paths import config_dir
    from kiro_crew.sandbox_leaves import _CREW_HOME_PREFIXES
    from kiro_crew.sandbox_tiers import _relocated_crew_targets

    try:
        leaves = app_manifest_leaves(str(config_dir()))
    except OSError as error:
        logger.warning("app manifest seal: skipped data home (%s)", error)
        return ()
    spellings = (
        *(os.path.join(os_home, prefix, leaf) for prefix in _CREW_HOME_PREFIXES for leaf in leaves),
        *_relocated_crew_targets(leaves),
    )
    targets: list[str] = []
    for target in dict.fromkeys(spellings):
        try:
            mode = os.lstat(target).st_mode
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                logger.warning("app manifest seal: skipped %s (%s)", target, error)
            continue
        if stat.S_ISREG(mode):
            targets.append(target)
            try:
                resolved = os.path.realpath(target)
                if resolved != target and stat.S_ISREG(os.lstat(resolved).st_mode):
                    targets.append(resolved)
            except OSError:
                pass
    return tuple(dict.fromkeys(targets))
