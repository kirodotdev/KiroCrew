"""Delete Codex's own record of one session.

codex-acp runs ``codex app-server``, which writes a rollout log of every thread
under ``CODEX_HOME`` (``~/.codex`` when unset):
``sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl``. Two other names
hold the same conversation. The ``local_thread_store_compression`` feature
rewrites an idle rollout in place as ``.jsonl.zst``, and ``thread/archive``
moves the file to ``archived_sessions/<same name>``. codex-acp's
``session/delete`` sends that archive to a client without the AIR session index,
so it keeps the text on disk.

A disposable session (incognito or temporary, a pruned subagent run, a released
continuable conversation) deletes every rollout file for its thread id in both
trees, the way the kiro-cli replay log is deleted. The root is the one the child
used. The child's environment is the gateway's with the session's ``extra_env``
(a cron job's ``env``, a workflow run's) merged over it, and ``agent.env``
refuses ``CODEX_HOME``. So the root is the gateway's ``CODEX_HOME`` unless that
``extra_env`` sets ``CODEX_HOME``, or sets ``HOME`` when no ``CODEX_HOME``
applies, and the cleanup reads the same merge. A subagent run records the root
at spawn in its cleanup identity, because the prune and ``spawn_release`` run
with no session at hand. A Codex record without it deletes nothing.

The root is the operator's location, so a link at the root or in a directory
that contains it is followed, as codex follows it. Such a link whose target is
missing makes the cleanup not done, since the store comes back with the target.
Inside the store nothing is followed: a linked ``sessions``,
``archived_sessions`` or date directory, or a leaf that is not a regular file,
deletes nothing. No directory is ever removed.

It sits behind the agent-SDK boundary so the subagent prune reaches Codex's
store, and Codex's provider label, without importing the ACP layer.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.acp.types import PROVIDER_LABEL_CODEX
from kiro_crew.providers.cleanup import _is_safe_path

logger = logging.getLogger(__name__)

#: ASCII digits only: ``str.isdigit`` also accepts ``"²"`` and other scripts.
_DIGITS = frozenset("0123456789")
_LOWER_HEX = frozenset("0123456789abcdef")
#: A Codex thread id as codex-acp hands it out: a lower-case hyphenated UUID,
#: 36 characters with ``-`` at these indexes. Anything else is refused before a
#: path is built.
_SESSION_ID_LENGTH = 36
_SESSION_ID_DASHES = frozenset((8, 13, 18, 23))
#: ``sessions/<year>/<month>/<day>/``, the date the rollout was created.
_DATE_DIRS = (("year", 4), ("month", 2), ("day", 2))
#: ``rollout-<YYYY-MM-DDTHH-MM-SS>-<thread id>.jsonl``, or ``.jsonl.zst`` once
#: compressed. The timestamp is digits except at these indexes.
_ROLLOUT_PREFIX = "rollout-"
_STAMP_LENGTH = 19
_STAMP_SEPARATORS = {4: "-", 7: "-", 10: "T", 13: "-", 16: "-"}
_ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")
_ACTIVE_TREE = "sessions"
_ARCHIVE_TREE = "archived_sessions"
_CODEX_HOME_ENV = "CODEX_HOME"
#: codex finds ``~`` from ``HOME`` on POSIX, and from the profile folder on Windows.
_HOME_ENV = "HOME"


def _is_session_id(value: object) -> bool:
    """Whether *value* is a lower-case hyphenated UUID."""
    return (
        isinstance(value, str)
        and len(value) == _SESSION_ID_LENGTH
        and all(
            char == "-" if index in _SESSION_ID_DASHES else char in _LOWER_HEX
            for index, char in enumerate(value)
        )
    )


def _is_date_dir(name: str, width: int) -> bool:
    """Whether *name* is exactly *width* ASCII digits."""
    return len(name) == width and all(char in _DIGITS for char in name)


def _is_stamp(text: str) -> bool:
    """Whether *text* is a rollout timestamp, ``YYYY-MM-DDTHH-MM-SS``."""
    return len(text) == _STAMP_LENGTH and all(
        char == _STAMP_SEPARATORS[index] if index in _STAMP_SEPARATORS else char in _DIGITS
        for index, char in enumerate(text)
    )


def _is_rollout_name(name: str, session_id: str) -> bool:
    """Whether *name* is a rollout file name for *session_id*."""
    if not name.startswith(_ROLLOUT_PREFIX):
        return False
    for suffix in _ROLLOUT_SUFFIXES:
        tail = f"-{session_id}{suffix}"
        if name.endswith(tail) and _is_stamp(name[len(_ROLLOUT_PREFIX) : -len(tail)]):
            return True
    return False


class _Refused(Exception):
    """The store holds a shape this module does not delete through."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


def is_codex_provider_label(provider_type: str | None) -> bool:
    """Whether *provider_type* (a session's provider label) names Codex.

    A positive comparison against Codex's own label (harness-parity H5).
    """
    return bool(provider_type) and provider_type == PROVIDER_LABEL_CODEX


def codex_home(env_overrides: Mapping[str, str] | None = None) -> Path | None:
    """The ``CODEX_HOME`` a Crew Codex session writes under, or ``None``.

    *env_overrides* is the session's ``extra_env``. The spawn merges it over the
    gateway's environment, so a name in it wins over ``os.environ``. The root is
    ``CODEX_HOME`` when set and not empty, else ``~/.codex``, where ``~`` is the
    overlay's ``HOME`` on POSIX when the overlay sets one. ``None`` when no home
    directory resolves, or when the value is relative: codex resolves a
    relative one from its own working directory, which the gateway cannot name.
    """
    overrides = env_overrides if isinstance(env_overrides, Mapping) else {}
    override: str | None
    if _CODEX_HOME_ENV in overrides:
        override = overrides[_CODEX_HOME_ENV]
    else:
        override = os.environ.get(_CODEX_HOME_ENV)
    if override:
        path = Path(override)
        return path if path.is_absolute() else None
    if _HOME_ENV in overrides and not platform_compat.IS_WINDOWS:
        home = Path(overrides[_HOME_ENV])
        return home / ".codex" if home.is_absolute() else None
    try:
        return Path.home() / ".codex"
    except (RuntimeError, KeyError):
        return None


def spawn_env_overrides(holder: object) -> Mapping[str, str] | None:
    """The ``extra_env`` that *holder*'s child process was spawned with, or ``None``.

    It is on the holder, on the ACP client a provider wraps (``client`` or
    ``_client``), or on the shared runtime that either one rides (``_runtime``).
    ``None`` when none of them carries one.
    """
    inner = getattr(holder, "client", None) or getattr(holder, "_client", None)
    for owner in (
        holder,
        getattr(holder, "_runtime", None),
        inner,
        getattr(inner, "_runtime", None),
    ):
        overlay = getattr(owner, "_extra_env", None)
        if isinstance(overlay, Mapping):
            return overlay
    return None


def recorded_codex_home(holder: object) -> str:
    """The root to record for *holder*'s Codex child, or ``""`` when it cannot be named.

    A subagent run records it at spawn. An empty value makes the later cleanup
    not done, so the run keeps its retry record.
    """
    overlay = spawn_env_overrides(holder)
    if overlay is None:
        return ""
    root = codex_home(overlay)
    return str(root) if root is not None else ""


def _root_link_dangles(root: Path) -> bool:
    """Whether *root*, or a directory that contains it, is a link whose target is gone.

    A store behind such a link is unreachable, not empty. Its rollouts come back
    when the target does (a volume mounts again), so the cleanup is not done.
    """
    if root.exists():
        return False
    return any(
        platform_compat.is_link_or_junction(path) and not path.exists()
        for path in (root, *root.parents)
    )


def _reason(exc: OSError) -> str:
    """An OSError's reason without the filename its ``str()`` would carry."""
    return exc.strerror or type(exc).__name__


def _subdirs(directory: Path, width: int, label: str) -> list[Path]:
    """The entries of *directory* named by *width* ASCII digits that are directories.

    A matching entry that is a link or junction refuses the whole cleanup: the
    rollout can be behind it, and following it is not allowed.
    """
    found: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not _is_date_dir(entry.name, width):
                continue
            path = Path(entry.path)
            if platform_compat.is_link_or_junction(path):
                raise _Refused(f"linked {label} directory")
            if entry.is_dir(follow_symlinks=False):
                found.append(path)
    return found


def _rollouts_in(directory: Path, session_id: str, root: Path) -> list[Path]:
    """The rollout files for *session_id* directly in *directory*.

    A matching name that is not a regular file, or that resolves outside
    *root*, refuses the whole cleanup.
    """
    found: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not _is_rollout_name(entry.name, session_id):
                continue
            path = Path(entry.path)
            if platform_compat.is_link_or_junction(path):
                raise _Refused("linked rollout")
            if not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                raise _Refused("rollout that is not a regular file")
            if not _is_safe_path(path, root):
                raise _Refused("rollout outside the store")
            found.append(path)
    return found


def _tree(root: Path, name: str) -> Path | None:
    """``root/name`` when it is a directory to search, ``None`` when it holds nothing."""
    tree = root / name
    if platform_compat.is_link_or_junction(tree):
        raise _Refused(f"linked {name} directory")
    return tree if tree.is_dir() else None


def _find_rollouts(root: Path, session_id: str) -> list[Path]:
    """Every rollout file for *session_id* under *root*'s two trees."""
    found: list[Path] = []
    active = _tree(root, _ACTIVE_TREE)
    if active is not None:
        level = [active]
        for label, width in _DATE_DIRS:
            level = [sub for parent in level for sub in _subdirs(parent, width, label)]
        for day in level:
            found.extend(_rollouts_in(day, session_id, root))
    archive = _tree(root, _ARCHIVE_TREE)
    if archive is not None:
        found.extend(_rollouts_in(archive, session_id, root))
    return found


def _scan(root: Path, session_id: str) -> list[Path] | None:
    """:func:`_find_rollouts`, or ``None`` after logging why the store was refused.

    Logged by a fixed label, never by path: a path carries the session id.
    """
    try:
        return _find_rollouts(root, session_id)
    except _Refused as refusal:
        logger.error("codex rollout cleanup: refusing a %s", refusal.label)
    except OSError as exc:
        logger.warning("codex rollout cleanup: cannot inspect the store (%s)", _reason(exc))
    return None


def delete_codex_session_files(
    session_id: str, env_overrides: Mapping[str, str] | None = None
) -> bool:
    """Delete every Codex rollout file for *session_id*.

    The root is :func:`codex_home` of *env_overrides*, the session's
    ``extra_env``. Returns whether the cleanup completed: ``True`` when no
    rollout for the id is left (including when none ever existed), ``False``
    when the id is not a lower-case UUID, no root resolves, the root or a
    directory that contains it is a link whose target is missing, the store
    holds a link or a non-file at a name this module would use, the store cannot
    be read, a delete failed, or a rollout is still there after the deletes. A
    refusal deletes nothing, so a caller that retries (the subagent prune) keeps
    its retry record and ``spawn_release`` answers ``not_disposed``.
    """
    if not _is_session_id(session_id):
        return False
    root = codex_home(env_overrides)
    if root is None:
        return False
    if _root_link_dangles(root):
        logger.warning("codex rollout cleanup: the store is behind a link whose target is missing")
        return False
    targets = _scan(root, session_id)
    if targets is None:
        return False
    succeeded = True
    for target in targets:
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            succeeded = False
            logger.warning("codex rollout cleanup: failed to delete a rollout (%s)", _reason(exc))
    if not succeeded or not targets:
        return succeeded
    # A live codex process can rewrite a rollout as ``.jsonl.zst`` between the
    # scan and the unlink, so only a second scan that finds nothing proves it.
    left = _scan(root, session_id)
    if left:
        logger.warning("codex rollout cleanup: a rollout was written during the cleanup")
    return left == []


__all__ = [
    "codex_home",
    "delete_codex_session_files",
    "is_codex_provider_label",
    "recorded_codex_home",
    "spawn_env_overrides",
]
