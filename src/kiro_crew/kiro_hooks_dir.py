"""Every path entry a hooks directory depends on, from one walker.

Crew and kiro-cli read the user's hook files from a directory named by a spelling:
``$HOME/.kiro/hooks``, kiro-cli's ``$KIRO_HOME/hooks``, and the ``agent.kiro_hooks_dir``
setting while Crew accepts it. Which directory a spelling reaches depends on every name
on the way: each directory, each symlink, each name a ``..`` follows (present or not).
Change any of them and the spelling reaches another directory.

:func:`walk` is the one place that answers both questions, what a spelling reaches and
which entries decide it. Crew's resolver for the setting
(``agent_materialization.kiro_hooks.resolve_kiro_hooks_dir``) takes its answer from it,
and the sandbox seal pins, creates and seals from it, so the two cannot disagree. It
follows :func:`os.path.realpath` (what ``Path.resolve`` does): a missing name is kept
as it is written and a later ``..`` removes it, so a missing name before a ``..`` is an
entry too, and must exist for the seal to hold it.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path

#: Links followed before a walk gives up, as the kernel does with ``ELOOP``.
_MAX_LINK_HOPS = 40


@dataclass(frozen=True)
class Walk:
    """What :func:`walk` found: the directory reached and every entry on the way."""

    #: The path the spelling reaches, as ``os.path.realpath`` would answer.
    resolved: str
    #: Every name the walk stepped on, in order, as an absolute path to that name: real
    #: directories, symlinks, missing names. The reached path is included only when the
    #: walk stepped on it before a later ``..`` climbed back to it.
    entries: tuple[str, ...]
    #: The entries that did not exist when walked.
    missing: tuple[str, ...]


def walk(path: str) -> Walk:
    """Walk *path* one name at a time, the way ``os.path.realpath`` resolves it.

    A relative *path* is joined onto the current directory, as ``Path.resolve`` joins
    it. Nothing is collapsed before the walk: a ``..`` applies to the directory reached
    so far, after the link before it is followed. Raises ``ValueError`` for a link loop
    or a chain longer than :data:`_MAX_LINK_HOPS`, and for a path with a NUL byte, and
    ``OSError`` when a name cannot be looked up for another reason (``EACCES``,
    ``ENAMETOOLONG``): what is there is then unknown.
    """
    found, error = _walk(path)
    if error is not None:
        raise error
    return found


def walk_until_error(path: str) -> Walk:
    """:func:`walk`, but a walk that fails returns the entries it stepped on before.

    A loop or a NUL byte is how a reader rejects a spelling, and an agent that replaces
    one of the entries before it can make the spelling resolve: so they are guarded
    like any other entry. ``resolved`` is then where the walk stopped. A folder the
    walk may not search hides the names below it, which can be neither created nor
    pinned; :func:`unreadable_owned_dir` names it, and the readers refuse instead.
    """
    return _walk(path)[0]


class HooksDirUnreadable(Exception):
    """A hooks directory, or a folder on the way to it, cannot be listed or searched.

    A reader that met this would otherwise read "no hooks", or answer another
    directory, and an agent can cause it on Linux by changing the mode of a folder it
    owns (a pin holds a name in place, not its mode). So every reader raises this
    instead, and the caller refuses: Crew's permission path blocks the turn's tool
    calls, an agent-config rebuild keeps the config it had, and a sandbox refuses to
    start. Not an ``OSError``, so no handler that reads an OS error as "no hooks"
    catches it on the way.
    """

    def __init__(self, directory: str) -> None:
        super().__init__(
            f"Kiro Crew cannot read the hooks folder or a folder on its path ({directory}); "
            "restore its permissions"
        )
        self.directory = directory


def walk_or_refuse(path: str) -> Walk:
    """:func:`walk`, raising :class:`HooksDirUnreadable` for an unsearchable owned folder."""
    found, error = _walk(path)
    if error is None:
        return found
    if isinstance(error, PermissionError) and _owned(found.resolved):
        raise HooksDirUnreadable(found.resolved) from error
    raise error


def list_or_refuse(directory: Path) -> list[Path]:
    """The entries of *directory*, raising :class:`HooksDirUnreadable` when a folder this
    uid owns stops it being listed (:func:`unreadable_owned_dir`). Any other ``OSError``
    propagates as it is."""
    try:
        return list(directory.iterdir())
    except PermissionError as exc:
        blocker = unreadable_owned_dir(str(directory), listing=True)
        if blocker is not None:
            raise HooksDirUnreadable(blocker) from exc
        raise


def unreadable_owned_dir(spelling: str, *, listing: bool) -> str | None:
    """The folder this uid owns that stops *spelling* from being read, else ``None``.

    The walk stops at a folder it may not search (``EACCES``); with *listing*, the
    reached directory itself is also listed. Only a folder this uid owns counts: its
    mode is one an agent running as this uid can change, so an unreadable one is
    treated as an error by every reader. A folder another user owns cannot be changed
    from a sandbox and keeps the reader's usual answer. Never raises.
    """
    found, error = _walk(spelling)
    if isinstance(error, PermissionError):
        return found.resolved if _owned(found.resolved) else None
    if error is not None or not listing:
        return None
    try:
        with os.scandir(found.resolved):
            pass
    except PermissionError:
        return found.resolved if _owned(found.resolved) else None
    except OSError:
        return None
    return None


def _owned(path: str) -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return False  # Windows: no sandbox of Crew's runs there
    try:
        return os.stat(path).st_uid == getuid()
    except OSError:
        return False


def _walk(path: str) -> tuple[Walk, OSError | ValueError | None]:
    if "\0" in path:
        return Walk(resolved=path, entries=(), missing=()), ValueError("path contains a NUL byte")
    if os.path.isabs(path):
        absolute = path
    else:
        try:
            absolute = os.path.join(os.getcwd(), path)
        except OSError as exc:  # the process's working directory was removed
            return Walk(resolved=path, entries=(), missing=()), exc
    entries: dict[str, None] = {}
    missing: dict[str, None] = {}
    resolved, pending = _anchor_and_names(absolute)
    hops = 0
    # The name the walk ended on, while nothing (a ``..``, a link) came after it.
    final: str | None = None
    while pending:
        name = pending.pop(0)
        if name in ("", "."):
            continue
        final = None
        if name == "..":
            resolved = os.path.dirname(resolved)
            continue
        current = os.path.join(resolved, name)
        entries[current] = None
        try:
            value = os.readlink(current)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOENT, errno.ENOTDIR):
                # EACCES, ENAMETOOLONG, ELOOP: the walk cannot say what is there
                return Walk(resolved, tuple(entries), tuple(missing)), exc
            if not os.path.lexists(current):
                missing[current] = None
            resolved = final = current
            continue
        hops += 1
        if hops > _MAX_LINK_HOPS:
            error = ValueError(f"too many links resolving {path!r}")
            return Walk(resolved, tuple(entries), tuple(missing)), error
        anchor, names = _anchor_and_names(value)
        pending[:0] = names
        resolved = anchor or os.path.dirname(current)
    if final is not None and list(entries)[-1] == final:
        # The reached directory itself, last stepped on: it is sealed, not guarded.
        # One a ``..`` climbed back to was stepped on before and stays an entry.
        entries.pop(final)
        missing.pop(final, None)
    return Walk(resolved=resolved, entries=tuple(entries), missing=tuple(missing)), None


def _anchor_and_names(path: str) -> tuple[str, list[str]]:
    """The drive and root *path* starts from (``""`` when relative), and its names.

    On Windows the anchor is the drive with its separator (``C:\\``, ``\\\\server\\share\\``),
    either separator splits names, and a ``\\\\?\\`` prefix ``readlink`` returns is dropped,
    as ``ntpath.realpath`` drops it.
    """
    if os.sep == "\\" and path.startswith("\\\\?\\"):
        path = "\\\\" + path[8:] if path[4:8].upper() == "UNC\\" else path[4:]
    drive, root, tail = os.path.splitroot(path)
    if os.altsep:
        tail = tail.replace(os.altsep, os.sep)
    return drive + (os.sep if root else ""), tail.split(os.sep)


def _default_spelling() -> str:
    """``$HOME/.kiro/hooks``, where Crew and kiro-cli read hooks by default."""
    return os.path.join(os.path.expanduser("~"), ".kiro", "hooks")


def _kiro_home_spelling() -> str | None:
    """``$KIRO_HOME/hooks`` as written, while Crew accepts the override, else ``None``."""
    from kiro_crew.config.paths import kiro_home

    written = _kiro_home_written()
    if written is None:
        return None
    try:
        if walk(os.path.dirname(written)).resolved != str(kiro_home()):
            return None
    except (OSError, ValueError):
        return None
    return written


def _kiro_home_written() -> str | None:
    """``$KIRO_HOME/hooks`` as written, ``~`` expanded, whenever the variable is set."""
    override = os.environ.get("KIRO_HOME")
    if not override:
        return None
    return os.path.join(os.path.expanduser(override), "hooks")


def _configured_spelling() -> str | None:
    """``agent.kiro_hooks_dir`` as written, ``~`` expanded, while Crew reads it.

    ``None`` when the setting is unset or not a string, or when Crew's resolver
    rejected it and answers the default instead.
    """
    from kiro_crew import agent as agent_mod

    written = _configured_written()
    if written is None:
        return None
    try:
        if walk(written).resolved != str(agent_mod.resolved_kiro_hooks_dir()):
            return None
    except (OSError, ValueError):
        return None
    return written


def _configured_written() -> str | None:
    """``agent.kiro_hooks_dir`` as written, ``~`` expanded, whether Crew accepts it or not.

    ``None`` when the setting is unset or not a string. Read the way the resolver reads
    it (BOM-tolerant, gateway-endorsed).
    """
    from kiro_crew import agent as agent_mod

    mc_cfg = agent_mod._load_json(agent_mod._mc_config_path()) or {}
    agent_cfg = mc_cfg.get("agent") if isinstance(mc_cfg, dict) else None
    raw = agent_cfg.get("kiro_hooks_dir") if isinstance(agent_cfg, dict) else None
    if not (isinstance(raw, str) and raw):
        return None
    return os.path.expanduser(raw)


def hooks_dir_spellings() -> list[str]:
    """Every spelling a reader of the user's hooks uses, as written. Never raises.

    The default ``$HOME/.kiro/hooks``, kiro-cli's ``$KIRO_HOME/hooks`` while Crew
    accepts that override, and the ``agent.kiro_hooks_dir`` setting while Crew reads
    it, relative or not.
    """
    spellings: list[str] = []
    for source in (_default_spelling, _kiro_home_spelling, _configured_spelling):
        try:
            spelling = source()
        except Exception:  # noqa: BLE001 (an unreadable source adds no spelling)
            continue
        if spelling:
            spellings.append(spelling)
    return list(dict.fromkeys(spellings))


def hooks_dir_walks() -> list[Walk]:
    """:func:`walk` of every :func:`hooks_dir_spellings` entry. Never raises."""
    walks: list[Walk] = []
    for spelling in hooks_dir_spellings():
        try:
            walks.append(walk(spelling))
        except (OSError, ValueError):
            continue
    return walks


def guarded_spellings() -> list[tuple[str, bool]]:
    """Every spelling a reader may use, accepted or not, and whether a reader accepts it.

    :func:`hooks_dir_spellings` lists only the spellings Crew accepts. Whether it
    accepts ``agent.kiro_hooks_dir`` or ``KIRO_HOME`` is decided by the same entries
    the walk steps on (a link out of ``$HOME``, to a sensitive directory, or into a
    loop), so an agent that replaces one could turn a rejected spelling into an
    accepted one naming its own directory. Each written spelling is listed here
    whether accepted or not, so its entries are guarded either way. Never raises.
    """
    accepted = set(hooks_dir_spellings())
    spellings: dict[str, bool] = {}
    for source in (_default_spelling, _kiro_home_written, _configured_written):
        try:
            spelling = source()
        except Exception:  # noqa: BLE001 (an unreadable source adds no spelling)
            continue
        if spelling:
            spellings[spelling] = spellings.get(spelling, False) or spelling in accepted
    return list(spellings.items())


def guard_walk(spelling: str, accepted: bool) -> Walk:
    """The entries to guard and create for *spelling*: :func:`walk_until_error`'s.

    A rejected spelling's reached name is an entry too, since nothing seals it and
    replacing it re-points the spelling, unless it is ``$HOME`` or a folder above it,
    which no write inside ``$HOME`` can replace. An accepted spelling's reached
    directory is sealed read-only instead. Never raises.
    """
    found = walk_until_error(spelling)
    if accepted or not _replaceable_from_home(found.resolved):
        return found
    missing = found.missing
    if not os.path.lexists(found.resolved):
        missing = (*missing, found.resolved)
    return Walk(found.resolved, (*found.entries, found.resolved), missing)


def _replaceable_from_home(path: str) -> bool:
    if not path or "\0" in path or not os.path.isabs(path):
        return False
    home = os.path.realpath(os.path.expanduser("~"))
    return path != home and not (home + os.sep).startswith(path.rstrip(os.sep) + os.sep)


def configured_kiro_hooks_dir_spellings() -> list[str]:
    """The configured directory's paths for the file-edit gate: reached, and as written.

    The written form is included only when it is absolute and has no ``.`` or ``..``
    name, so it names the same directory as a path. ``[]`` while Crew does not read
    the setting. Never raises.
    """
    try:
        written = _configured_spelling()
        if written is None:
            return []
        spellings = [walk(written).resolved]
    except HooksDirUnreadable:
        # Crew refuses to read the setting until the folder is searchable again; the
        # written form stays fenced meanwhile.
        written = _configured_written() or ""
        spellings = []
    except Exception:  # noqa: BLE001 (no readable setting: Crew reads the fenced default)
        return []
    if not written:
        return []
    if os.path.isabs(written) and os.path.normpath(written) == written.rstrip(os.sep):
        spellings.insert(0, written.rstrip(os.sep) or os.sep)
    return list(dict.fromkeys(spellings))
