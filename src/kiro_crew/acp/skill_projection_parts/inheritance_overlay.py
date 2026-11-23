"""Crew's overlay on Kiro's ``chat.disableInheritingDefaultResources`` in a workspace ``cli.json``.

The keys it writes, the credential-gated settings read with its retry pause, the
recorded inheritance preference behind :func:`inherits_default_resources`, and the
rollback that restores what the workspace had.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, on_event_loop
from kiro_crew.config.paths import kiro_home
from kiro_crew.hooks import (
    MAX_FILE_BYTES,
    FileTooLargeError,
    safe_read_file_bytes,
)
from kiro_crew.security import _PATH_RESOLVE_TIMEOUT_SECS
from kiro_crew.workspace_cli_settings import (
    CLI_JSON_REFUSED,
    CliJsonVersion,
    LockedCliSettings,
    carry_effort_ownership,
    cli_json_version,
    current_cli_json_version,
    pinned_cli_settings_fd,
    publish_locked_cli_settings,
    read_named_cli_json,
    read_pinned_cli_json,
    settings_json_text,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"


_ABSENT_SETTINGS = object()

#: Pauses before the retries of a refused ``cli.json`` read, in seconds; the
#: first attempt runs before any of them. A healthy file is refused transiently
#: in two ways: a writer renames a new file over the one that was opened (the
#: descriptor check then names the unlinked inode), which clears on an immediate
#: re-check, and a saturated sensitive-path resolver, which clears once a
#: candidate resolution has had its full budget. No other refusal changes with
#: time inside one call, so more pauses would only delay the fail-closed answer.
_SETTINGS_READ_RETRY_PAUSES: tuple[float, ...] = (0.0, _PATH_RESOLVE_TIMEOUT_SECS)

#: The pause primitive of :func:`_settings`, held as a module attribute so a test
#: can replace it without touching the shared ``time`` module.
_settings_read_sleep = time.sleep


def _settings(path: Path | LockedCliSettings, *, may_pause: bool = True) -> dict[str, Any]:
    """Read one Kiro ``cli.json`` through the credential gate; absence reads ``{}``.

    See :func:`_settings_with_mtime` for the retry and pause rules.
    """
    return _settings_with_mtime(path, may_pause=may_pause)[0]


def _refuse_changed_since_read(
    settings: LockedCliSettings, read_version: CliJsonVersion | None
) -> None:
    """Raise when ``cli.json`` differs from the version a rewrite was built from.

    The lock holds back Kiro Crew's own writers only, so an editor save can land
    between the read and the publish; publishing the stale document would undo
    that save and carry its effort ownership record back to valid.
    """
    if current_cli_json_version(settings) != read_version:
        raise OSError(
            f"workspace cli.json overlay {settings.cli_json} changed after it was read; "
            "left unchanged"
        )


def _settings_with_mtime(
    path: Path | LockedCliSettings, *, may_pause: bool = True
) -> tuple[dict[str, Any], int | None, CliJsonVersion | None]:
    """Read one Kiro ``cli.json``, the whole-second mtime and the version of the file read.

    The mtime is what :func:`carry_effort_ownership` validates Kiro Crew's effort
    ownership record against when the document is written back; it is taken by a
    no-follow stat right after the read, and is ``None`` for an absent file or
    when the stat fails, which voids the record (the conservative side). The
    version is what :func:`_refuse_changed_since_read` compares before a locked
    rewrite; it is ``None`` for an absent file and for an unlocked read.

    Off the event loop a refused read is retried on
    :data:`_SETTINGS_READ_RETRY_PAUSES`: one immediate re-check, then one pause
    and a final attempt. Every attempt re-runs the whole gate, so a path that
    stays refused still raises. On a thread running an event loop exactly one
    attempt runs, because each attempt is several bounded path resolutions and a
    pause there stalls every session the gateway serves.

    ``may_pause=False`` keeps the immediate re-check but drops the pause, for a
    caller holding a cross-process lock whose contenders give up after the same
    2 s: sleeping there would make a concurrent spawn lose its lock instead.

    A locked read refuses every unsafe shape: a link, a hardlink, a non-regular
    file, a descriptor whose real path escapes the settings directory, is
    sensitive or cannot be resolved, and a read that fails. Once retries are
    exhausted, a locked read raises ``OSError`` when its last attempt was
    refused or saw the file change. Every other exhausted read raises
    ``ValueError``.
    """
    cli_json = path.cli_json if isinstance(path, LockedCliSettings) else path
    locked_refusal: OSError | None = None
    locked_changed_while_read = False

    def read_once() -> tuple[bytes, int | None, CliJsonVersion | None] | object | None:
        nonlocal locked_changed_while_read, locked_refusal
        locked_refusal = None
        locked_changed_while_read = False
        if isinstance(path, LockedCliSettings):
            settings_fd = pinned_cli_settings_fd(path)
            try:
                if settings_fd is not None:
                    result = read_pinned_cli_json(settings_fd, cli_json, max_bytes=MAX_FILE_BYTES)
                else:
                    if platform_compat.is_link_or_junction(cli_json):
                        raise OSError(f"workspace cli.json overlay {cli_json} {CLI_JSON_REFUSED}")
                    result = read_named_cli_json(
                        cli_json, path.settings_dir, max_bytes=MAX_FILE_BYTES
                    )
            except OSError as exc:
                locked_refusal = exc
                return None
            if result is None:
                return _ABSENT_SETTINGS
            # The bytes and both versions come from one handle, so a save that lands
            # after the read cannot lend the stale bytes its own version.
            raw, before_read, after_read = result
            after_version = cli_json_version(after_read)
            if cli_json_version(before_read) != after_version:
                locked_changed_while_read = True
                return None
            if len(raw) > MAX_FILE_BYTES:
                raise FileTooLargeError(
                    f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap"
                )
            return raw, int(after_read.st_mtime), after_version
        if not cli_json.exists():
            return _ABSENT_SETTINGS
        raw_by_name = safe_read_file_bytes(str(cli_json))
        if raw_by_name is None:
            return None
        try:
            file_mtime = int(os.stat(cli_json, follow_symlinks=False).st_mtime)
        except OSError:
            file_mtime = None
        return raw_by_name, file_mtime, None

    raw = read_once()
    if raw is None and not on_event_loop():
        for pause in _SETTINGS_READ_RETRY_PAUSES:
            if pause and not may_pause:
                break
            if pause:
                _settings_read_sleep(pause)
            raw = read_once()
            if raw is not None:
                break
    if raw is None:
        if isinstance(path, LockedCliSettings):
            if locked_refusal is not None:
                raise locked_refusal
            if locked_changed_while_read:
                raise OSError(
                    f"workspace cli.json overlay {cli_json} changed while it was read; "
                    "left unchanged"
                )
        raise ValueError(f"Cannot read Kiro settings at {cli_json}")
    if not isinstance(raw, tuple):
        # The absence sentinel: the file does not exist.
        return {}, None, None
    content, file_mtime, file_version = raw
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {cli_json}")
    return data, file_mtime, file_version


def _inheritance_preference(
    local: dict[str, Any], global_settings: dict[str, Any]
) -> tuple[bool, Any, bool]:
    """Return ``(inherited, source, overlaid)`` for custom agents in one workspace.

    Once Crew's overlay is in place the native key reads ``true`` whatever the
    user chose, so the recorded preference decides: a ``global`` source follows
    the live global setting and any other source keeps the recorded value.
    Without the overlay, or after the user moves the native key off ``true``,
    the native key decides -- the workspace value when present, else the global
    one -- and only the literal ``true`` opts out.
    """
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
        source = "local" if _INHERIT_SETTING in local else "global"
        native = local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING))
        return native is not True, source, False
    if source == "global":
        return global_settings.get(_INHERIT_SETTING) is not True, source, True
    return inherited, source, True


#: What a Kiro ``cli.json`` that is present but cannot be read raises from
#: :func:`_settings`: a refused or failed read, a malformed or non-object
#: document, an oversized file, a symlink loop (``RuntimeError`` before Python
#: 3.13). Every reader in this package treats these the same way: no projection
#: decision is taken from the file and nothing is written to it.
_UNREADABLE_SETTINGS_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    RuntimeError,
    RecursionError,
    FileTooLargeError,
)


def inherits_default_resources(work_dir: str | os.PathLike[str] | None) -> bool:
    """Whether a custom agent started in *work_dir* inherits kiro-cli's default resources.

    Those defaults are global and workspace steering, ``AGENTS.md`` and the
    global and workspace skill trees. This is the read-only twin of the decision
    :func:`prepare_native_skill_projection` makes, for callers that must mirror
    what the native agent loads. Settings
    that cannot be read keep inheritance, which is how those callers behaved
    before they asked.
    """
    try:
        global_settings = _settings(kiro_home() / "settings" / "cli.json")
        local = (
            _settings(Path(work_dir) / ".kiro" / "settings" / "cli.json")
            if work_dir is not None
            else {}
        )
    except _UNREADABLE_SETTINGS_ERRORS:
        logger.warning(
            "skill projection: Kiro settings unreadable; keeping inherited resources",
            exc_info=True,
        )
        return True
    return _inheritance_preference(local, global_settings)[0]


def _restore_inheritance(
    settings: LockedCliSettings,
    local: dict[str, Any],
    file_mtime: int | None,
    file_version: CliJsonVersion | None,
) -> None:
    """Undo only our overlay; a changed or removed native setting wins.

    *file_mtime* is the mtime of the file version *local* was read from, so the
    write carries Kiro Crew's effort ownership record exactly as it stood, and
    *file_version* is that version, so a file changed since is left unchanged.
    """
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {settings.cli_json}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    _refuse_changed_since_read(settings, file_version)
    stamp_ns = carry_effort_ownership(local, file_mtime)
    publish_locked_cli_settings(
        settings,
        settings_json_text(local),
        write=atomic_write,
        before_publish=lambda: _refuse_changed_since_read(settings, file_version),
        mtime_ns=stamp_ns,
    )
