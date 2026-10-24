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
    safe_read_file_bytes_nolink,
)
from kiro_crew.security import _PATH_RESOLVE_TIMEOUT_SECS
from kiro_crew.workspace_cli_settings import (
    CLI_JSON_REFUSED,
    LockedCliSettings,
    carry_effort_ownership,
    pinned_cli_settings_fd,
    publish_locked_cli_settings,
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


def _settings_with_mtime(
    path: Path | LockedCliSettings, *, may_pause: bool = True
) -> tuple[dict[str, Any], int | None]:
    """Read one Kiro ``cli.json`` and the whole-second mtime of the file version read.

    The mtime is what :func:`carry_effort_ownership` validates Kiro Crew's effort
    ownership record against when the document is written back; it is taken by a
    no-follow stat right after the read, and is ``None`` for an absent file or
    when the stat fails, which voids the record (the conservative side).

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

    def read_once() -> tuple[bytes, int | None] | object | None:
        nonlocal locked_changed_while_read, locked_refusal
        locked_refusal = None
        locked_changed_while_read = False
        if isinstance(path, LockedCliSettings):
            settings_fd = pinned_cli_settings_fd(path)
            if settings_fd is not None:
                try:
                    result = read_pinned_cli_json(settings_fd, cli_json, max_bytes=MAX_FILE_BYTES)
                except OSError as exc:
                    locked_refusal = exc
                    return None
                if result is None:
                    return _ABSENT_SETTINGS
                raw, before_read, after_read = result
                before_version = (
                    before_read.st_dev,
                    before_read.st_ino,
                    before_read.st_size,
                    before_read.st_mtime_ns,
                    before_read.st_ctime_ns,
                )
                after_version = (
                    after_read.st_dev,
                    after_read.st_ino,
                    after_read.st_size,
                    after_read.st_mtime_ns,
                    after_read.st_ctime_ns,
                )
                if before_version != after_version:
                    locked_changed_while_read = True
                    return None
                if len(raw) > MAX_FILE_BYTES:
                    raise FileTooLargeError(
                        f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap"
                    )
                return raw, int(after_read.st_mtime)
            if not os.path.lexists(cli_json):
                return _ABSENT_SETTINGS
            refusal = OSError(f"workspace cli.json overlay {cli_json} {CLI_JSON_REFUSED}")
            if platform_compat.is_link_or_junction(cli_json):
                locked_refusal = refusal
                return None
            raw_by_name = safe_read_file_bytes_nolink(
                str(cli_json),
                within_root=str(path.settings_dir),
                max_bytes=MAX_FILE_BYTES,
                within_root_is_canonical=True,
            )
            if raw_by_name is None:
                locked_refusal = refusal
                return None
            try:
                file_mtime: int | None = int(os.stat(cli_json, follow_symlinks=False).st_mtime)
            except OSError:
                file_mtime = None
            return raw_by_name, file_mtime
        if not cli_json.exists():
            return _ABSENT_SETTINGS
        raw_by_name = safe_read_file_bytes(str(cli_json))
        if raw_by_name is None:
            return None
        try:
            file_mtime = int(os.stat(cli_json, follow_symlinks=False).st_mtime)
        except OSError:
            file_mtime = None
        return raw_by_name, file_mtime

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
        return {}, None
    content, file_mtime = raw
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {cli_json}")
    return data, file_mtime


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
    # RuntimeError: Path.resolve() reports a symlink loop that way before Python 3.13.
    except (OSError, ValueError, RuntimeError, RecursionError, FileTooLargeError):
        logger.warning(
            "skill projection: Kiro settings unreadable; keeping inherited resources",
            exc_info=True,
        )
        return True
    return _inheritance_preference(local, global_settings)[0]


def _restore_inheritance(
    settings: LockedCliSettings, local: dict[str, Any], file_mtime: int | None
) -> None:
    """Undo only our overlay; a changed or removed native setting wins.

    *file_mtime* is the mtime of the file version *local* was read from, so the
    write carries Kiro Crew's effort ownership record exactly as it stood.
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
    stamp_ns = carry_effort_ownership(local, file_mtime)
    publish_locked_cli_settings(
        settings, settings_json_text(local), write=atomic_write, mtime_ns=stamp_ns
    )
