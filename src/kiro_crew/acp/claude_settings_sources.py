"""Whether another Claude Code settings source defines ``availableModels``.

Claude Code's family-wildcard rule: an allowlist entry naming a specific model
disables that family's alias (``haiku``), which then resolves to the newest model
the list permits. Crew's own list is needed only to counter another source's list,
so the seed writer asks this module before writing one. It also reads the user's own
settings file for the project MCP approvals in it (:func:`read_user_settings`).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import platform_compat
from kiro_crew.user_json import loads_user_json

#: Ceiling on a Claude Code settings file read only to inspect a few keys.
CLAUDE_SETTINGS_MAX_BYTES = 1 << 20


def claude_home(environ: Mapping[str, str]) -> Path | None:
    """The home directory of a ``claude`` process started with *environ*, or None.

    None when no home resolves, or when it is relative: the ``claude`` process resolves
    that against its own working directory, a session's work dir, as it does a relative
    ``CLAUDE_CONFIG_DIR``.
    """
    value = environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    try:
        home = Path(value) if value else Path.home()
    except RuntimeError:
        return None
    return home if home.is_absolute() else None


def claude_config_dir(environ: Mapping[str, str]) -> Path | None:
    """The config directory a ``claude`` process started with *environ* reads.

    ``CLAUDE_CONFIG_DIR`` when it is set and not empty, else ``.claude`` in the home
    directory (:func:`claude_home`). None when no home resolves, or when the value is
    relative: Claude Code resolves that against its own working directory, a session's
    work dir, so it is no config directory of the user's that Crew can name.
    """
    override = environ.get("CLAUDE_CONFIG_DIR")
    if override:
        path = Path(override)
        return path if path.is_absolute() else None
    home = claude_home(environ)
    return home / ".claude" if home is not None else None


def read_claude_settings_bytes(path: Path, *, follow_links: bool) -> bytes | None:
    """The bytes of a Claude Code settings file, read bounded and non-blocking.

    ``O_NONBLOCK`` so a FIFO cannot block the open, a regular-file check on the
    opened descriptor so a link to a device is refused, and a size ceiling on the
    read. *follow_links* is False for a file a cloned repository controls: a link
    there is refused; on POSIX ``O_NOFOLLOW`` also closes the race between that
    check and the open, and on Windows, which has no such flag, the link check
    before the open is the only refusal.

    Raises ``FileNotFoundError`` when nothing is at *path*. Returns None when the
    bytes cannot be examined: a refused link, a non-regular or oversized file, or
    any other read error.
    """
    if not follow_links:
        try:
            if path.is_symlink():
                return None
        except OSError:
            return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if not follow_links:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > CLAUDE_SETTINGS_MAX_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= CLAUDE_SETTINGS_MAX_BYTES:
            chunk = os.read(fd, CLAUDE_SETTINGS_MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > CLAUDE_SETTINGS_MAX_BYTES:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _settings_object(path: Path, *, follow_links: bool) -> dict[str, Any] | None:
    """The JSON object in the settings file at *path*, or None.

    None when the file is absent, cannot be read or parsed, or is not an object.
    """
    try:
        raw = read_claude_settings_bytes(path, follow_links=follow_links)
    except FileNotFoundError:
        return None
    if raw is None:
        return None
    try:
        # Claude Code reads a file opening with the UTF-16LE byte-order mark as
        # UTF-16LE (Windows PowerShell 5.1 writes that by default) and any other
        # file as UTF-8; loads_user_json drops the decoded mark.
        encoding = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-8"
        data = loads_user_json(raw.decode(encoding))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def read_user_settings(config_dir: Path) -> dict[str, Any] | None:
    """The ``settings.json`` object in Claude Code config directory *config_dir*, or None.

    None when there is none to use. The file may be a symlinked dotfile, so a link is
    followed.
    """
    return _settings_object(config_dir / "settings.json", follow_links=True)


def _defines_available_models(path: Path, *, follow_links: bool = True) -> bool:
    """Whether *path* is a JSON object with an ``availableModels`` key.

    Any failure to read or parse the file counts as not defining it.
    """
    data = _settings_object(path, follow_links=follow_links)
    return data is not None and "availableModels" in data


def _managed_settings_files(managed_dir: Path) -> list[Path]:
    """``managed-settings.json``, then each drop-in in ``managed-settings.d/`` in name order.

    Mirrors Claude Code's own walk: a drop-in whose name starts with ``.`` or does
    not end in ``.json`` is skipped, and an unlistable drop-in directory adds nothing.
    """
    files = [managed_dir / "managed-settings.json"]
    drop_in = managed_dir / "managed-settings.d"
    try:
        with os.scandir(drop_in) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        return files
    files.extend(
        drop_in / name for name in names if name.endswith(".json") and not name.startswith(".")
    )
    return files


def other_sources_define_available_models(
    work_dir: Path,
    *,
    config_dir: Path | None = None,
    managed_dir: Path | None = None,
) -> bool:
    """Whether a settings source other than Crew's own local file defines ``availableModels``.

    Checks the user file, the project file, and the managed file plus its
    ``managed-settings.d/`` drop-ins that claude-agent-acp reads. The project's
    ``settings.local.json`` is not checked: when Crew wrote it, it holds Crew's own
    list, and when a project owns it, Crew either sends its settings inline with the
    project tier left out of the CLI or cannot apply them at all, so the CLI never
    loads that file beside Crew's list.
    Never raises: a missing, unreadable or malformed file counts as not defining
    the key.
    """
    user_dir = config_dir if config_dir is not None else claude_config_dir(os.environ)
    managed = (
        managed_dir
        if managed_dir is not None
        else platform_compat.claude_code_managed_settings_dir()
    )
    # The project file is the only one a cloned repository controls, so it alone
    # refuses links; user and managed files may be symlinked dotfiles.
    if user_dir is not None and _defines_available_models(user_dir / "settings.json"):
        return True
    project = Path(work_dir) / ".claude" / "settings.json"
    if _defines_available_models(project, follow_links=False):
        return True
    return any(_defines_available_models(path) for path in _managed_settings_files(managed))
