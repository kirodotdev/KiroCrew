"""Whether another Claude Code settings source defines ``availableModels``.

Claude Code's family-wildcard rule: an allowlist entry naming a specific model
disables that family's alias (``haiku``), which then resolves to the newest model
the list permits. Crew's own list is needed only to counter another source's list,
so the seed writer asks this module before writing one.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.user_json import loads_user_json

#: Ceiling on a Claude Code settings file read only to inspect a few keys.
CLAUDE_SETTINGS_MAX_BYTES = 1 << 20


def claude_config_dir() -> Path | None:
    """The user's Claude config directory, or None when no home directory resolves.

    The resolution the ``claude`` CLI itself makes (``CLAUDE_CONFIG_DIR``, else
    ``~/.claude``), read in the gateway because the child inherits its environment.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    try:
        return Path.home() / ".claude"
    except RuntimeError:
        return None


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


def _settings_object(path: Path, *, follow_links: bool = True) -> dict[str, Any] | None:
    """The JSON object in the settings file at *path*, or None.

    Any failure to read or parse the file, and a file that is not an object,
    gives None.
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
    user_dir = config_dir if config_dir is not None else claude_config_dir()
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


#: The marker a Claude model id ends with to ask for the 1M-token window.
_WINDOW_MARKER = "[1m]"


def _split_window_marker(model_id: str) -> tuple[str, str]:
    """*model_id* without its window marker, and the marker (``""`` when it has none)."""
    if model_id.endswith(_WINDOW_MARKER):
        return model_id[: -len(_WINDOW_MARKER)], _WINDOW_MARKER
    return model_id, ""


def family_entry_covers(entry: str, model_id: str) -> bool:
    """Whether the ``availableModels`` entry *entry* also allows the newer *model_id*.

    The rule is Claude Code's own, from the ``availableModels`` comment in the
    claude-agent-sdk settings type (``sdk.d.ts``): 'version prefixes ("opus-4-5"
    allows that version and any model ID that extends it, so "claude-opus-5" also
    allows "claude-opus-5-5")'. So *model_id* is covered when it is *entry* plus a
    ``-`` and more.

    Two cases are left out on purpose, so this never widens what the CLI allows:

    - An entry with no digit is a family alias (``opus``), not a version prefix.
      Listing a specific id beside an alias changes which model the alias runs.
    - The two must agree on the window marker. The SDK text does not say whether
      ``claude-opus-5`` covers ``claude-opus-5-5[1m]``.
    """
    prefix, entry_window = _split_window_marker(entry)
    model, model_window = _split_window_marker(model_id)
    if entry_window != model_window or not any(ch.isdigit() for ch in prefix):
        return False
    return model.startswith(prefix + "-")


def user_models_a_family_entry_covers(
    *,
    config_dir: Path | None = None,
    managed_dir: Path | None = None,
) -> list[str]:
    """The user's ``modelOverrides`` keys that their own ``availableModels`` covers but does not list.

    claude-agent-acp builds its model picker from the literal ``availableModels``
    entries, one row each. The CLI reads the same list as a filter, where a
    version prefix also allows the newer members of that family
    (:func:`family_entry_covers`). So a user whose list has ``claude-opus-5`` and
    whose ``modelOverrides`` routes ``claude-opus-5-5`` can pick that model in
    the CLI and finds no row for it in the adapter's picker. The seed writer adds
    these keys to its own list, and the adapter's union then gives each a row.

    Only the user's own ``settings.json`` is read, and only names it holds are
    returned, in the order ``modelOverrides`` lists them. Returns ``[]`` when a
    managed file sets ``availableModelsMatch`` to ``"exact"``: under that
    setting an entry allows only its own version. Never raises.
    """
    user_dir = config_dir if config_dir is not None else claude_config_dir()
    if user_dir is None:
        return []
    settings = _settings_object(user_dir / "settings.json")
    if settings is None:
        return []
    entries = settings.get("availableModels")
    overrides = settings.get("modelOverrides")
    if not isinstance(entries, list) or not isinstance(overrides, dict):
        return []
    listed = [entry for entry in entries if isinstance(entry, str)]
    covered = [
        name
        for name in overrides
        if name not in listed and any(family_entry_covers(entry, name) for entry in listed)
    ]
    if not covered:
        return []
    managed = (
        managed_dir
        if managed_dir is not None
        else platform_compat.claude_code_managed_settings_dir()
    )
    for path in _managed_settings_files(managed):
        policy = _settings_object(path)
        if policy is not None and policy.get("availableModelsMatch") == "exact":
            return []
    return covered
