"""Write, clear and recover the workspace ``cli.json`` overlay a kiro-family harness reads at spawn.

The effort and Tool Search keys of ``<work_dir>/.kiro/settings/cli.json``: the file
writers and readers, and the ``AcpProvider`` members that apply the session's level and
Tool Search choice to it before every spawn.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_explicit_effort_default_applied``,
``_explicit_effort_default_pending``, ``_fence_explicit_effort_default_rewrite``.

Instance fields read: ``_client``, ``_tool_search``, ``_tool_search_min_pct``,
``_tool_search_min_tokens``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import stat
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp._dispatch import redact_backend_text
from kiro_crew.acp.client import DEFAULT_MODEL
from kiro_crew.acp.types import ACP_BACKENDS_KIRO_SLASH_COMMANDS, ACP_BACKENDS_TOOL_SEARCH_OVERLAY
from kiro_crew.agent_sdk.tool_search import (
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
    clamp_min_pct,
    clamp_min_tokens,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.effort import EFFORT_LEVELS, effort_settings_key
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import canonical_path_refusal
from kiro_crew.workspace_cli_settings import (
    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    EFFORT_OWNED_KEY,
    EFFORT_OWNED_STAMP_KEY,
    LockedCliSettings,
    admitted_workspace_cli_settings_dir,
    effort_ownership_stamp_matches,
    locked_workspace_cli_settings,
    owned_document_unchanged,
    stamp_effort_ownership,
    workspace_cli_settings_fence_key,
)

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")

#: Decoding for the workspace ``cli.json``. The file can be a person's own
#: workspace settings, and Windows editors save UTF-8 with a byte-order mark,
#: which ``json.loads`` refuses on decoded text. A file that does not parse is
#: left unchanged and the overlay write fails, so the reader drops one leading
#: mark and the overlay still applies. Writers emit plain UTF-8.
_CLI_JSON_ENCODING = "utf-8-sig"


_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS = (
    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS + CLI_SETTINGS_LOCK_TIMEOUT_SECS
)
# The key names live in ``workspace_cli_settings`` so every writer of the file,
# including the skill projection, carries the record under one rule.
_KIROCREW_EFFORT_OWNED_KEY = EFFORT_OWNED_KEY
_KIROCREW_EFFORT_OWNED_STAMP_KEY = EFFORT_OWNED_STAMP_KEY
_WARN_UNOWNED_MAX_ITEMS = 10
_WARN_UNOWNED_MAX_CHARS = 120
# A settings file is kilobytes; past this Kiro Crew does not read it, so every
# structure derived from it stays bounded.
_CLI_JSON_MAX_BYTES = 1024 * 1024
_OVERLAY_REFUSED = (
    "is a link, a hardlink, not a regular file inside the settings directory, or cannot be "
    "read; left unchanged"
)
_CliJsonVersion = tuple[int, int, int, int, int]
_UNKNOWN_CLI_JSON_VERSION = object()


class _OverlayTooLarge(ValueError):
    """Raised when a workspace cli.json overlay exceeds the read ceiling."""


class _OverlayParentNotAnObject(ValueError):
    """Raised when an explicit Default would replace a non-object overlay parent."""


def _log_atom(value: object) -> str:
    """Render a cli.json value as one bounded, single-line log token."""
    if isinstance(value, str):
        rendered = json.dumps(value[:_WARN_UNOWNED_MAX_CHARS], ensure_ascii=True)
    elif isinstance(value, (int, float, bool)) or value is None:
        rendered = json.dumps(value, ensure_ascii=True)
    elif isinstance(value, dict):
        rendered = f"<object with {len(value)} keys>"
    elif isinstance(value, list):
        rendered = f"<array of {len(value)} items>"
    else:
        rendered = f"<{type(value).__name__}>"
    if len(rendered) > _WARN_UNOWNED_MAX_CHARS:
        return rendered[: _WARN_UNOWNED_MAX_CHARS - 4] + '..."'
    return rendered


def _log_effort_level(value: object) -> str:
    """Render a cli.json effort leaf without exposing non-level file content."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True) if value in EFFORT_LEVELS else "<unrecognized>"
    if isinstance(value, dict):
        return f"<object with {len(value)} keys>"
    if isinstance(value, list):
        return f"<array of {len(value)} items>"
    return f"<{type(value).__name__}>"


def _log_model_key(value: object) -> str:
    """Render a cli.json model key after redacting the whole value before bounding it."""
    if not isinstance(value, str):
        return _log_atom(value)
    rendered = json.dumps(redact_backend_text(value), ensure_ascii=True)
    if len(rendered) > _WARN_UNOWNED_MAX_CHARS:
        return rendered[: _WARN_UNOWNED_MAX_CHARS - 4] + '..."'
    return rendered


def _owned_cli_effort(settings: dict[str, Any], file_mtime: int | None) -> dict[str, str]:
    """Return effort provenance only when it belongs to this file version."""
    if not effort_ownership_stamp_matches(settings, file_mtime):
        return {}
    record = settings.get(_KIROCREW_EFFORT_OWNED_KEY)
    if not isinstance(record, dict):
        return {}
    return {
        model: level
        for model, level in record.items()
        if isinstance(model, str) and model and isinstance(level, str) and level
    }


def _cli_effort_is_owned(active: object, recorded: str | None) -> bool:
    """Return whether a non-empty record names the identical active effort value."""
    return isinstance(recorded, str) and bool(recorded) and active == recorded


def _pinned_settings_fd(settings: LockedCliSettings) -> int | None:
    """The lock's settings descriptor when both the read and the publish can go through it."""
    if settings.settings_fd is None or not atomic_write_module.pinned_parent_replace_supported():
        return None
    return settings.settings_fd


def _cli_json_version(metadata: os.stat_result) -> _CliJsonVersion:
    """Return the fields that identify one observed version of ``cli.json``."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _current_cli_json_version(
    settings: LockedCliSettings, settings_fd: int | None
) -> _CliJsonVersion | None:
    """Stat the current ``cli.json`` name through the writer's selected path."""
    try:
        if settings_fd is None:
            metadata = os.stat(settings.cli_json, follow_symlinks=False)
        else:
            metadata = os.stat(
                "cli.json",
                dir_fd=settings_fd,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(
            f"workspace cli.json overlay {settings.cli_json} changed while the overlay "
            "was written; left unchanged"
        ) from exc
    return _cli_json_version(metadata)


def _publish_cli_overlay(
    settings: LockedCliSettings,
    document: dict[str, Any],
    read_document: dict[str, Any],
    file_mtime: int | None,
    read_version: _CliJsonVersion | None | object,
) -> None:
    """Publish one overlay into the settings directory the lock opened, where supported.

    *read_document* is a copy of the document :func:`_read_cli_overlay_document`
    returned under this lock, and *file_mtime* the mtime it returned with it. A
    document that equals the file read, apart from the stamp, while that file's
    record is valid is not written: the file keeps its bytes and its mtime, so the
    record stays valid and a same-level projection before every spawn leaves the
    file alone.

    A settings directory replaced or linked by name after the lock cannot receive the
    write. A ``cli.json`` changed after the read is left unchanged and the writer raises.
    A publish that would leave the file unchanged also refuses either change.
    A writer that changes the file after the version check can still race the rename;
    closing that window requires every writer to take the shared lock. Platforms without
    descriptor-relative writes use the by-name write floor.
    """
    settings_fd = _pinned_settings_fd(settings)
    if settings_fd is not None and not settings.holds_named_settings_dir():
        raise OSError("workspace CLI settings directory changed before the overlay was written")
    current_version = _current_cli_json_version(settings, settings_fd)
    if read_version is _UNKNOWN_CLI_JSON_VERSION or current_version != read_version:
        raise OSError(
            f"workspace cli.json overlay {settings.cli_json} changed while the overlay "
            "was written; left unchanged"
        )
    if owned_document_unchanged(document, read_document, file_mtime):
        return
    # The timestamp is applied to the staged inode before publication, so the
    # ownership record and the inode carrying it become visible together. A
    # document with no record carries no stamp and takes a natural mtime.
    stamp_ns = stamp_effort_ownership(document)
    content = json.dumps(document, indent=2)
    if settings_fd is None:
        atomic_write(settings.cli_json, content, mtime_ns=stamp_ns)
    else:
        atomic_write(
            settings.cli_json,
            content,
            parent_dir_fd=settings_fd,
            mtime_ns=stamp_ns,
        )
        if not settings.holds_named_settings_dir():
            raise OSError("workspace CLI settings directory changed while the overlay was written")


def _read_pinned_cli_json(
    settings_fd: int, cli_json: Path
) -> tuple[bytes, os.stat_result, os.stat_result] | None:
    """Read ``cli.json`` through the pinned settings directory.

    Returns the bytes with the opened file's metadata from before and after the read.
    """
    try:
        fd = os.open(
            "cli.json",
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=settings_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(f"workspace cli.json overlay {cli_json} {_OVERLAY_REFUSED}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError(f"workspace cli.json overlay {cli_json} {_OVERLAY_REFUSED}")
        opened_path = pinned_fs.fd_real_path(fd)
        if opened_path is None or canonical_path_refusal(opened_path) is not None:
            raise OSError(f"workspace cli.json overlay {cli_json} {_OVERLAY_REFUSED}")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(_CLI_JSON_MAX_BYTES + 1)
        return raw, opened, os.fstat(handle.fileno())


def _read_cli_overlay_document(
    settings: LockedCliSettings,
) -> tuple[dict[str, Any], int | None, _CliJsonVersion | None | object]:
    """Return the settings object an overlay writer merges into.

    The returned version is the file's metadata from before its bytes were read, so
    the guarded publish refuses after any later change, even one during the read. A
    missing file is an empty document. An existing file that cannot be read,
    is a link, is hardlinked, is non-regular, or escapes the workspace settings
    directory raises so the writer leaves its bytes untouched. An overlay past
    the ceiling is left unchanged and the write raises, as an unparseable one does.
    Where descriptor-relative writes are supported, the read and publish use
    the settings directory the lock opened, so a folder replaced or linked after
    the lock cannot receive the write and the writer raises. Elsewhere both go by
    name; on Windows the lock holds ``.kiro`` and ``settings`` open meanwhile, so
    neither can be renamed or deleted until the writer is done.
    """
    cli_json = settings.cli_json
    settings_fd = _pinned_settings_fd(settings)
    raw: bytes | None
    file_mtime: int | None = None
    file_version: _CliJsonVersion | None | object
    if settings_fd is not None:
        read_result = _read_pinned_cli_json(settings_fd, cli_json)
        if read_result is None:
            return {}, None, None
        raw, before_read, after_read = read_result
        file_mtime = int(after_read.st_mtime)
        file_version = _cli_json_version(before_read)
        if len(raw) > _CLI_JSON_MAX_BYTES:
            raise _OverlayTooLarge(
                f"workspace cli.json overlay {cli_json} is past the "
                f"{_CLI_JSON_MAX_BYTES}-byte ceiling; left unchanged"
            )
    else:
        if not os.path.lexists(cli_json):
            return {}, None, None
        if platform_compat.is_link_or_junction(cli_json):
            raise OSError(f"workspace cli.json overlay {cli_json} is a link; left unchanged")

        # The lock's folder is the one ``.kiro/settings`` resolved to inside the work dir,
        # so an accepted link reads and writes the same folder.
        settings_root = str(settings.settings_dir)
        try:
            file_version = _cli_json_version(os.stat(cli_json, follow_symlinks=False))
        except OSError:
            file_version = _UNKNOWN_CLI_JSON_VERSION
        try:
            raw = safe_read_file_bytes_nolink(
                str(cli_json),
                within_root=settings_root,
                max_bytes=_CLI_JSON_MAX_BYTES,
                within_root_is_canonical=True,
            )
        except FileTooLargeError as exc:
            raise _OverlayTooLarge(
                f"workspace cli.json overlay {cli_json} is past the "
                f"{_CLI_JSON_MAX_BYTES}-byte ceiling; left unchanged"
            ) from exc
        if raw is None:
            raise OSError(f"workspace cli.json overlay {cli_json} {_OVERLAY_REFUSED}")
        try:
            file_mtime = int(os.stat(cli_json, follow_symlinks=False).st_mtime)
        except OSError:
            # The read remains authoritative. A racing replacement or failed
            # metadata lookup only voids ownership, the conservative side.
            file_mtime = None

    try:
        existing = json.loads(raw.decode(_CLI_JSON_ENCODING))
    except Exception as exc:
        # Every failure to turn bytes into a settings object leaves the operator's
        # file unchanged.
        raise ValueError(
            f"workspace cli.json overlay {cli_json} could not be parsed as a settings document; "
            "left unchanged"
        ) from exc
    if not isinstance(existing, dict):
        raise ValueError(
            f"workspace cli.json overlay {cli_json} is not a JSON object; left unchanged"
        )
    return existing, file_mtime, file_version


def _write_cli_overlay(
    work_dir: Path,
    model: str,
    effort: str,
    *,
    timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    replace_unowned: bool = False,
) -> None:
    """Write a workspace cli.json overlay so kiro-cli applies effort at spawn.

    Path: ``<work_dir>/.kiro/settings/cli.json``. Workspace settings override
    the global ``~/.kiro/settings/cli.json``, so nothing here touches the user's
    global kiro settings. The overlay's scope is that WORK DIRECTORY, not a
    session: every session served by one runtime shares the work dir. Kiro-cli
    reads the file at spawn, while Kiro Crew reasserts a resolved level after
    spawn and after each fresh session on a warm runtime. Merge-safe and
    idempotent -- safe to call before every spawn.

    The effort value and Kiro Crew's exact-match ownership record live in the
    same document and are published by one atomic write. An existing matching
    value with no matching record is operator-owned and is never claimed. A
    differing operator-authored active-key value, or a non-object value a
    normal projection would replace, is kept. ``replace_unowned`` is reserved
    for the explicit Default action: it replaces this model's effort values in
    both family shapes while preserving their sibling keys, but refuses a
    non-object ``chat.modelDefaults``, model entry, or effort object and leaves
    the file unchanged. A stale or malformed record is dropped without changing
    any other setting; when no record is stale or malformed the file is
    untouched.

    The effort sub-key is family-specific (``effort_settings_key``): Claude
    models use ``output_config``, GPT models use ``reasoning`` -- kiro-cli
    silently ignores the wrong key, so the shape must match the model.

    An existing overlay that cannot be read, is a link, is hardlinked, is
    non-regular, escapes the workspace settings directory, exceeds the 1 MiB
    read ceiling, or cannot be parsed into a settings object is left byte-for-byte
    unchanged and the write raises: the caller reports a level as persisted only
    when the file holds it, and an operator's file is never reset to make room
    for one. A settings folder on a sensitive path, or one whose real path cannot
    be verified, is refused by the lock before anything beneath it is read, and
    the write raises. Where descriptor-relative writes are supported, the read and publish use
    the settings directory the lock opened, so a folder replaced or linked after
    the lock cannot receive the write and the writer raises. Elsewhere both go by
    name; on Windows the lock holds ``.kiro`` and ``settings`` open meanwhile, so
    neither can be renamed or deleted until the writer is done.
    """
    with locked_workspace_cli_settings(work_dir, timeout=timeout) as settings:
        existing, file_mtime, file_version = _read_cli_overlay_document(settings)
        read_document = copy.deepcopy(existing)

        key = effort_settings_key(model)
        model_defaults_present = "chat.modelDefaults" in existing
        model_defaults_raw = existing.get("chat.modelDefaults")
        model_defaults = model_defaults_raw if isinstance(model_defaults_raw, dict) else None
        model_present = model_defaults is not None and model in model_defaults
        model_cfg_raw = model_defaults.get(model) if model_defaults is not None else None
        model_cfg = model_cfg_raw if isinstance(model_cfg_raw, dict) else None
        active_key_present = model_cfg is not None and key in model_cfg
        current_effort_cfg_raw = model_cfg.get(key) if model_cfg is not None else None
        current_effort_cfg = (
            current_effort_cfg_raw if isinstance(current_effort_cfg_raw, dict) else None
        )
        current_effort = (
            current_effort_cfg.get("effort") if current_effort_cfg is not None else None
        )
        active_effort_present = current_effort_cfg is not None and "effort" in current_effort_cfg
        if replace_unowned:
            for path, present, value in (
                ("chat.modelDefaults", model_defaults_present, model_defaults_raw),
                (f"chat.modelDefaults.{model}", model_present, model_cfg_raw),
                (f"chat.modelDefaults.{model}.{key}", active_key_present, current_effort_cfg_raw),
            ):
                if present and not isinstance(value, dict):
                    raise _OverlayParentNotAnObject(
                        f"workspace cli.json overlay {settings.cli_json} has non-object {path}; "
                        "left unchanged"
                    )
        raw_owned = existing.get(_KIROCREW_EFFORT_OWNED_KEY)
        owned = _owned_cli_effort(existing, file_mtime)
        recorded_effort = owned.get(model)
        ownership_needs_normalization = (
            _KIROCREW_EFFORT_OWNED_KEY in existing and raw_owned != owned
        )
        replaces_operator_value = not replace_unowned and (
            (model_defaults is None and model_defaults_present)
            or (
                (model_defaults is not None and model_cfg is None and model_present)
                or (model_cfg is not None and current_effort_cfg is None and active_key_present)
                or (
                    active_effort_present
                    and not _cli_effort_is_owned(current_effort, recorded_effort)
                )
            )
        )
        if replaces_operator_value:
            if recorded_effort is not None or ownership_needs_normalization:
                owned.pop(model, None)
                if owned:
                    existing[_KIROCREW_EFFORT_OWNED_KEY] = owned
                else:
                    existing.pop(_KIROCREW_EFFORT_OWNED_KEY, None)
                _publish_cli_overlay(settings, existing, read_document, file_mtime, file_version)
            logger.debug(
                "ACP effort overlay kept operator-authored level: model=%s effort=%s",
                model,
                _log_effort_level(current_effort),
            )
            return

        if model_defaults is None:
            model_defaults = {}
        if model_cfg is None:
            model_cfg = {}
        if replace_unowned:
            for effort_key in ("output_config", "reasoning"):
                prior_effort_cfg = model_cfg.get(effort_key)
                if not isinstance(prior_effort_cfg, dict):
                    continue
                preserved_effort_cfg = dict(prior_effort_cfg)
                preserved_effort_cfg.pop("effort", None)
                if preserved_effort_cfg:
                    model_cfg[effort_key] = preserved_effort_cfg
                else:
                    model_cfg.pop(effort_key, None)
        current_effort_cfg = model_cfg.get(key)
        effort_cfg = dict(current_effort_cfg) if isinstance(current_effort_cfg, dict) else {}
        effort_cfg["effort"] = effort
        model_cfg[key] = effort_cfg
        model_defaults[model] = model_cfg
        existing["chat.modelDefaults"] = model_defaults
        owned[model] = effort
        if owned:
            existing[_KIROCREW_EFFORT_OWNED_KEY] = owned
        else:
            existing.pop(_KIROCREW_EFFORT_OWNED_KEY, None)

        _publish_cli_overlay(settings, existing, read_document, file_mtime, file_version)


# The thresholds and their clamps live in ``agent_sdk.tool_search`` so the
# wire channel (KAS) and the overlay channel (kiro-cli) read one definition; the
# names stay importable here because tests and the config mirror pin them.
_clamp_min_pct = clamp_min_pct
_clamp_min_tokens = clamp_min_tokens


def _write_tool_search_overlay(
    work_dir: Path,
    enabled: bool,
    min_pct: object = TOOL_SEARCH_DEFAULT_MIN_PCT,
    min_tokens: object = TOOL_SEARCH_DEFAULT_MIN_TOKENS,
) -> None:
    """Write kiro Tool Search settings into the workspace cli.json overlay.

    Path: ``<work_dir>/.kiro/settings/cli.json`` — the SAME overlay used for
    effort, with the same scope: the work directory, which every session on a
    runtime shares, read by kiro-cli only at spawn (see
    :func:`_write_cli_overlay`). Workspace settings override the global
    ``~/.kiro/settings/cli.json``, so this never mutates the user's global kiro
    settings.

    This file is the kiro-cli (Rust engine) channel only. KAS never opens it: its
    Tool Search setting rides the ACP ``initialize`` request instead (see
    :mod:`kiro_crew.agent_sdk.tool_search`), which is why the writer is
    scoped to ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY`` and not to every kiro-family
    host.

    Tool Search (https://kiro.dev/docs/cli/mcp/tool-search/) loads MCP tool
    specs on demand ("search-and-call") instead of sending every spec each
    turn. Deferral costs a round-trip: a deferred tool's spec is absent from the
    model's tool list, so the first direct call fails and the model has to load
    it with ``tool_search`` before retrying. That trade only pays once the specs
    are actually large, which is what *min_pct* (percent of the context window)
    and *min_tokens* express — kiro-cli activates deferral when EITHER is
    exceeded. Both are configurable (``agent.tool_search_min_pct`` /
    ``tool_search_min_tokens``) and default to kiro-cli's own thresholds; a
    small install therefore keeps its full tool specs and never pays the
    round-trip, while a heavy one still defers. Setting both to 0 restores
    unconditional deferral.

    The flag is written deterministically for BOTH true and false so the
    Kiro Crew toggle stays authoritative regardless of any value in the user's
    global kiro settings, and the thresholds are written EXPLICITLY rather than
    omitted — an earlier build forced them to 0, and leaving that behind would
    silently keep deferral unconditional on an already-configured machine.

    Merge-safe and idempotent — preserves the effort ``chat.modelDefaults`` keys
    and any other settings already present. kiro cli.json uses flat dotted keys
    (e.g. ``"chat.modelDefaults"``), so the Tool Search keys are written flat to
    match.

    An existing overlay that cannot be read, is a link, is hardlinked, is
    non-regular, escapes the workspace settings directory, cannot be parsed
    into a settings object, or exceeds the shared ceiling is left byte-for-byte
    unchanged and the write raises, exactly as :func:`_write_cli_overlay` does:
    the effort entries and operator settings in that file are never reset to
    make room for this flag. Where descriptor-relative writes are supported,
    the read and publish use the settings directory the lock opened, so a folder
    replaced or linked after the lock cannot receive the write and the writer
    raises. Elsewhere both go by name; on Windows the lock holds ``.kiro`` and
    ``settings`` open meanwhile, so neither can be renamed or deleted until the
    writer is done. The provider's caller logs the refused write and starts the
    session anyway.
    """
    with locked_workspace_cli_settings(work_dir) as settings:
        existing, file_mtime, file_version = _read_cli_overlay_document(settings)
        read_document = copy.deepcopy(existing)
        raw_owned = existing.get(_KIROCREW_EFFORT_OWNED_KEY)
        owned = _owned_cli_effort(existing, file_mtime)
        if _KIROCREW_EFFORT_OWNED_KEY in existing and raw_owned != owned:
            if owned:
                existing[_KIROCREW_EFFORT_OWNED_KEY] = owned
            else:
                existing.pop(_KIROCREW_EFFORT_OWNED_KEY, None)
        existing["toolSearch.enabled"] = bool(enabled)
        if enabled:
            existing["toolSearch.minPct"] = _clamp_min_pct(min_pct)
            existing["toolSearch.minTokens"] = _clamp_min_tokens(min_tokens)
        else:
            # Drop the thresholds when disabling so nothing is left behind that
            # would take effect if a later build flips the global default on.
            existing.pop("toolSearch.minPct", None)
            existing.pop("toolSearch.minTokens", None)
        _publish_cli_overlay(settings, existing, read_document, file_mtime, file_version)


class _BoundedCliEffortPairs:
    """Retain a bounded sample of file-derived effort pairs for one log line."""

    def __init__(self) -> None:
        self._sample: list[str] = []
        self._omitted = 0

    @property
    def sample_size(self) -> int:
        """Number of retained, rendered pairs."""
        return len(self._sample)

    @property
    def omitted(self) -> int:
        """Number of pairs omitted after the retained sample."""
        return self._omitted

    def add(self, model: object, level: object) -> None:
        """Retain one escaped pair or count it as omitted."""
        rendered = f"{_log_model_key(model)}={_log_effort_level(level)}"
        if len(self._sample) < _WARN_UNOWNED_MAX_ITEMS:
            self._sample.append(rendered)
        else:
            self._omitted += 1

    def __bool__(self) -> bool:
        return bool(self._sample) or self._omitted > 0

    def render(self) -> str:
        """Render the retained sample and its omitted tail count."""
        rendered = list(self._sample)
        if self._omitted:
            rendered.append(f"... {self._omitted} more")
        return ", ".join(rendered)


def _warn_unowned_cli_effort(
    cli_json: Path, model_defaults: dict[str, Any], model: str | None
) -> None:
    """Log the active-key effort values a Default or ``auto`` spawn may run.

    Called after the ownership sweep, so every value still under a model's
    active key is one Kiro Crew did not write: an operator's entry, or a level
    an older build projected without a record. ``None`` reads every model in
    the document because the ``auto`` spawn's model is unknown. One warning
    covers every remaining value; nothing is logged when none remains.
    """
    candidates = list(model_defaults) if model is None else [model]
    pairs = _BoundedCliEffortPairs()
    for candidate in candidates:
        model_cfg = model_defaults.get(candidate)
        if not isinstance(model_cfg, dict):
            continue
        effort_cfg = model_cfg.get(effort_settings_key(candidate))
        if not isinstance(effort_cfg, dict) or "effort" not in effort_cfg:
            continue
        pairs.add(candidate, effort_cfg["effort"])
    if not pairs:
        return
    if model is None:
        # The ``auto`` spawn: the picker clears an entry only for the model the
        # slot has selected, so the listed model has to be selected first.
        remedy = (
            "Remove it by selecting the listed model, picking a level and then Default in "
            "the effort picker, or by editing the file"
        )
    else:
        remedy = (
            "Remove it by picking a level and then Default in the effort picker, or by "
            "editing the file"
        )
    logger.warning(
        "ACP effort overlay %s holds an effort level Kiro Crew did not write (%s): "
        "this session is on Default, so it may run that level instead of the model's "
        "own default. %s",
        cli_json,
        pairs.render(),
        remedy,
    )


def _clear_cli_overlay_effort(
    work_dir: Path,
    model: str | None,
    *,
    timeout: float = CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    owned_only: bool = True,
    warn_unowned: bool = False,
) -> bool:
    """Remove matching effort projections from the workspace overlay.

    A normal clear removes only an active-key value that exactly matches Kiro
    Crew's record in the same cli.json document. An absent, malformed, stale,
    or mismatched record owns nothing. ``None`` applies that rule to every
    recorded model. ``owned_only=False`` is the explicit operator clear and
    removes either known effort-key shape for the requested model.

    ``warn_unowned`` is the pre-spawn projection's flag: after the sweep it
    logs the active-key values the document still holds for the model (every
    model for ``None``), because the spawn that follows may run one of them
    instead of the default. The values are read from the document already
    parsed under the lock; nothing is stored.

    Returns False only when the shared settings lock or an existing overlay
    cannot be read or written, is a link, is hardlinked, is non-regular, or
    escapes the workspace settings directory, when the settings folder is on a
    sensitive path or its real path cannot be verified, or when the settings
    folder is replaced during the write. The lock-free absence probe first admits the
    settings folder from link text; when admission refuses, no target is probed
    and the lock reports False whether or not that target contains ``cli.json``.
    An overlay past the ceiling returns
    False and is left unchanged. A missing file, or one that cannot be parsed
    into a settings object, names no effort for anyone, so it is left unchanged
    and is a successful no-op. Where descriptor-relative writes are supported,
    the read and publish use the settings directory the lock opened, so a folder
    replaced or linked after the lock cannot receive the write. Elsewhere both go
    by name; on Windows the lock holds ``.kiro`` and ``settings`` open meanwhile,
    so neither can be renamed or deleted until the writer is done.
    """
    admitted_settings_dir = admitted_workspace_cli_settings_dir(work_dir)
    if admitted_settings_dir is not None and not os.path.lexists(
        admitted_settings_dir / "cli.json"
    ):
        return True

    try:
        with locked_workspace_cli_settings(work_dir, timeout=timeout) as settings:
            cli_json = settings.cli_json
            try:
                data, file_mtime, file_version = _read_cli_overlay_document(settings)
            except _OverlayTooLarge:
                logger.warning(
                    "ACP effort overlay %s is past the %d-byte ceiling; left unchanged",
                    cli_json,
                    _CLI_JSON_MAX_BYTES,
                )
                return False
            except OSError:
                logger.debug("ACP effort overlay read failed", exc_info=True)
                return False
            except ValueError:
                # Every failure to turn bytes into a settings object names no
                # effort to clear, so the operator's bytes stay unchanged.
                return True
            read_document = copy.deepcopy(data)

            raw_owned = data.get(_KIROCREW_EFFORT_OWNED_KEY)
            owned = _owned_cli_effort(data, file_mtime)
            ownership_changed = _KIROCREW_EFFORT_OWNED_KEY in data and raw_owned != owned
            target_models = list(owned) if model is None else [model]
            original_model_defaults = data.get("chat.modelDefaults")
            if isinstance(original_model_defaults, dict):
                model_defaults = original_model_defaults
                model_defaults_was_dict = True
            else:
                model_defaults = {}
                model_defaults_was_dict = False
            overlay_changed = False
            removed_owned = _BoundedCliEffortPairs()

            for target_model in target_models:
                recorded_level = owned.pop(target_model, None)
                if recorded_level is not None:
                    ownership_changed = True
                model_cfg = model_defaults.get(target_model)
                if not isinstance(model_cfg, dict):
                    continue

                keys = (
                    ("output_config", "reasoning")
                    if not owned_only
                    else (effort_settings_key(target_model),)
                )
                for key in keys:
                    effort_cfg = model_cfg.get(key)
                    if not isinstance(effort_cfg, dict) or "effort" not in effort_cfg:
                        continue
                    if owned_only and not _cli_effort_is_owned(
                        effort_cfg.get("effort"), recorded_level
                    ):
                        continue
                    if owned_only:
                        removed_owned.add(target_model, effort_cfg["effort"])
                    effort_cfg.pop("effort")
                    overlay_changed = True
                    if not effort_cfg:
                        model_cfg.pop(key, None)
                if not model_cfg:
                    model_defaults.pop(target_model, None)

            if warn_unowned:
                _warn_unowned_cli_effort(cli_json, model_defaults, model)
            if not overlay_changed and not ownership_changed:
                return True
            if model_defaults:
                data["chat.modelDefaults"] = model_defaults
            elif model_defaults_was_dict:
                data.pop("chat.modelDefaults", None)
            if owned:
                data[_KIROCREW_EFFORT_OWNED_KEY] = owned
            else:
                data.pop(_KIROCREW_EFFORT_OWNED_KEY, None)
            _publish_cli_overlay(settings, data, read_document, file_mtime, file_version)
            if removed_owned:
                logger.info(
                    "ACP effort overlay %s removed Kiro Crew-owned level(s): %s; if you set a "
                    "level by hand to the same value, set it again",
                    cli_json,
                    removed_owned.render(),
                )
    except (OSError, RecursionError):
        logger.debug("ACP effort overlay clear failed", exc_info=True)
        return False
    return True


async def _await_overlay_write(write: Callable[[], object]) -> object:
    """Run a ``cli.json`` overlay write in a worker thread and wait for it even when cancelled.

    A cancelled caller re-raises only once the worker is done, however many times
    it is cancelled while it waits, so a teardown that follows (the shutdown
    reclaim of a disposable work dir) or a successor's projection never races a
    write still in progress. A worker that fails after the cancellation leaves
    only the cancellation to raise.
    """
    write_task = asyncio.ensure_future(asyncio.to_thread(write))
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        # Each further cancellation lands on a fresh shield, never on the worker.
        while not write_task.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(write_task)
        if not write_task.cancelled():
            write_task.exception()
        raise


# Set by start() around its pre-spawn projection, so the overlay worker (which
# copies the caller's context) keys on the spawn's model; a live projection in
# another task sees None and keys on the running session's model.
_EFFORT_OVERLAY_MODEL_OVERRIDE: ContextVar[str | None] = ContextVar(
    "effort_overlay_model_override", default=None
)


_EFFORT_OVERLAY_AUTO_CLEAR_WARNING = (
    "ACP effort overlay could not be checked or cleared for the auto "
    "model: this spawn may run a level another session projected "
    "instead of the default"
)
_EFFORT_OVERLAY_DEFAULT_CLEAR_WARNING = (
    "ACP effort overlay could not be checked or cleared for %s: this "
    "spawn may run a level another session projected instead of the "
    "default"
)


class _WorkspaceEffortOverlayFence:
    """Keep Default-branch spawn reads free of gateway-written levels.

    Windows share because each clears effort entries. A waiting window blocks new
    writes, so it waits only for the write already holding the fence and no level
    reaches the file before a Default runtime start returns.
    """

    def __init__(self) -> None:
        self._windows = 0
        self._pending_windows = 0
        self._write_held = False
        self._write_released = asyncio.Event()
        self._write_released.set()
        self._windows_released = asyncio.Event()
        self._windows_released.set()

    async def acquire(self, mode: Literal["window", "write"], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if mode == "window":
            if not self._windows and not self._pending_windows:
                self._windows_released.clear()
            self._pending_windows += 1
            pending = True
            try:
                while self._write_held:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return False
                    try:
                        await asyncio.wait_for(self._write_released.wait(), remaining)
                    except asyncio.TimeoutError:
                        return False
                self._pending_windows -= 1
                pending = False
                self._windows += 1
                return True
            finally:
                if pending:
                    self._pending_windows -= 1
                    if not self._windows and not self._pending_windows:
                        self._windows_released.set()
        while self._write_held or self._windows or self._pending_windows:
            event = self._write_released if self._write_held else self._windows_released
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        self._write_held = True
        self._write_released.clear()
        return True

    def release(self, mode: Literal["window", "write"]) -> None:
        if mode == "window":
            self._windows -= 1
            if not self._windows and not self._pending_windows:
                self._windows_released.set()
        else:
            self._write_held = False
            self._write_released.set()


_WORKSPACE_EFFORT_FENCES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, _WorkspaceEffortOverlayFence]
] = weakref.WeakKeyDictionary()


def _workspace_effort_overlay_fence(real_settings_dir: str) -> _WorkspaceEffortOverlayFence:
    """Get or create this loop's fence for an already-resolved settings directory.

    The key is the settings folder, not the work dir: the settings lock follows a
    ``.kiro`` or ``settings`` link that stays inside the work dir, so two work
    dirs can share one settings folder, and their windows and writes must then
    exclude each other.

    This stays synchronous and await-free because the get and create form one
    critical section on the loop. An await between them could give two coroutines
    separate fences for one directory, which would exclude nothing.
    """
    loop = asyncio.get_running_loop()
    fences = _WORKSPACE_EFFORT_FENCES.setdefault(loop, weakref.WeakValueDictionary())
    fence = fences.get(real_settings_dir)
    if fence is None:
        fence = _WorkspaceEffortOverlayFence()
        fences[real_settings_dir] = fence
    return fence


@asynccontextmanager
async def _hold_workspace_effort_overlay_fence(
    work_dir: Path, mode: Literal["window", "write"], timeout: float
) -> AsyncIterator[bool]:
    # Off the loop: even trusted work-directory resolution can pause on a slow mount.
    # The helper admits each settings link from lstat/readlink text before keying it.
    # Admission refusal uses the resolved work dir plus literal names; failure to
    # resolve the work dir still fails the start.
    real_settings_dir = await asyncio.to_thread(workspace_cli_settings_fence_key, work_dir)
    fence = _workspace_effort_overlay_fence(real_settings_dir)
    acquired = await fence.acquire(mode, timeout)
    try:
        yield acquired
    finally:
        if acquired:
            fence.release(mode)


def _apply_effort_overlay(
    self: AcpProvider,
    *,
    timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    replace_unowned: bool = False,
) -> bool:
    """Project this session's effort for the current model into the kiro overlay.

    Written only for the harnesses that READ it
    (``ACP_BACKENDS_KIRO_SLASH_COMMANDS`` — the kiro family takes effort from
    this file at spawn); adapter harnesses use a live set_config_option push
    instead. A no-op when a concrete model is not effort-capable. Called
    before every (re)spawn so resume/restart keeps the same level.

    A resolved level and its exact-match ownership record are written in one
    atomic cli.json update unless an operator-authored active-key value or
    non-object shape would be replaced. Those values stay in the file, and
    the session receives its own resolved level through the post-spawn
    ``/effort`` push. ``replace_unowned``, passed only by the explicit
    Default, replaces them instead (see ``_write_cli_overlay``). When none
    resolves for a concrete model, that model's entry is removed only when
    the in-file record names the exact active-key value still present. For
    ``auto``, the same rule is applied to every recorded model because the
    concrete model is unknowable until after kiro-cli reads the overlay.
    Before-spawn callers override the projection key with
    :attr:`_spawn_effort_model`, so a spawn with nothing pinned projects as
    ``auto`` whatever the last session served. Live callers key on
    :attr:`_effort_model`, the model the running session serves.
    Operator-authored entries and values edited after projection are
    preserved, and stale records are dropped. The removal admits the settings
    folder from link text before its lock-free ``cli.json`` probe, so an
    outside or remote target is not inspected; Default then reports that it
    could not change the file through the existing lock-failure path.

    KAS is written too, by membership, though no session takes its level
    from the file: kiro-cli's KAS engine never reads it, and construction
    does not read it back. A KAS session's level travels as the
    ``effortLevel`` config option instead.

    Membership rather than "not claude": the companion clear
    (``_clear_cli_overlay_effort`` in :meth:`change_effort`) is already
    membership-gated, so a negation here writes an overlay for a harness the
    clear will never reach — a stale file left in the user's workspace.
    """
    if self._client.backend not in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
        return True
    model_override = _EFFORT_OVERLAY_MODEL_OVERRIDE.get()
    model = self._effort_model if model_override is None else model_override
    work_dir = self._client._work_dir
    admitted_settings_dir = admitted_workspace_cli_settings_dir(work_dir)
    cli_json = admitted_settings_dir / "cli.json" if admitted_settings_dir is not None else None
    if model == DEFAULT_MODEL:
        if cli_json is not None and not os.path.lexists(cli_json):
            return True
        if not _clear_cli_overlay_effort(work_dir, None, timeout=timeout, warn_unowned=True):
            logger.warning(_EFFORT_OVERLAY_AUTO_CLEAR_WARNING)
            return False
        logger.debug("ACP effort ownership checked for the auto model")
        return True
    if not model or not self.supports_effort():
        return True
    level = self._resolve_effort()
    if not level:
        if cli_json is not None and not os.path.lexists(cli_json):
            return True
        if not _clear_cli_overlay_effort(
            work_dir,
            model,
            timeout=timeout,
            owned_only=not replace_unowned,
            warn_unowned=not replace_unowned,
        ):
            logger.warning(_EFFORT_OVERLAY_DEFAULT_CLEAR_WARNING, model)
            return False
        logger.debug("ACP effort ownership checked at the default: model=%s", model)
        return True
    try:
        _write_cli_overlay(
            work_dir,
            model,
            level,
            timeout=timeout,
            replace_unowned=replace_unowned,
        )
    except Exception:
        # A respawn reads the FILE, unless an operator-authored entry was
        # kept and the post-spawn push carries this session's level. A failed
        # write otherwise must not promise a level the next reset undoes.
        logger.warning("ACP effort overlay write failed", exc_info=True)
        return False
    logger.debug("ACP effort overlay applied: model=%s effort=%s", model, level)
    return True


def _effort_overlay_fence_mode(self: AcpProvider) -> Literal["window", "write"] | None:
    if self._client.backend not in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
        return None
    model = self._spawn_effort_model
    if model == DEFAULT_MODEL:
        return "window"
    if model and self.supports_effort():
        return "write" if self._resolve_effort() else "window"
    return None


def _log_effort_overlay_projection_skipped(self: AcpProvider, mode: Literal["write"]) -> None:
    model = self._spawn_effort_model
    if model == DEFAULT_MODEL:
        logger.warning(_EFFORT_OVERLAY_AUTO_CLEAR_WARNING)
    else:
        logger.warning(
            "ACP effort overlay write was skipped for %s; post-spawn effort "
            "reassertion will apply its resolved level",
            model,
        )


def _project_explicit_effort_default(self: AcpProvider) -> None:
    """Replace this model's entry whoever wrote it, and disarm once that succeeds.

    Runs in the overlay worker and records the outcome with the write, so a
    start cancelled while it waits still knows the file changed.
    """
    if self._apply_effort_overlay(replace_unowned=True):
        self._explicit_effort_default_applied = True
        self._explicit_effort_default_pending = False


def _spawn_effort_model(self: AcpProvider) -> str:
    """The model a projection before a spawn keys on: the pin, else ``auto``.

    With nothing pinned, kiro-cli picks its model after it reads the
    overlay, and the model the last session served need not be the one the
    next serves, so a projection keyed on it could leave another chat's
    level for the model the spawn picks. ``auto`` makes the projection
    consider every recorded model, as for an ``auto`` pin.
    """
    model = self._client._model
    return model if model and model != DEFAULT_MODEL else DEFAULT_MODEL


async def _write_effort_overlay(
    self: AcpProvider, write: Callable[[], object], timeout: float
) -> object | bool:
    if self._client.backend not in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
        return await _await_overlay_write(write)
    async with _hold_workspace_effort_overlay_fence(
        self._client._work_dir, "write", timeout
    ) as acquired:
        if not acquired:
            return False
        return await _await_overlay_write(write)


def arm_explicit_effort_default(
    self: AcpProvider, fence_rewrite: Callable[[], AbstractContextManager[object]]
) -> None:
    """Make the next start's projection replace this model's entry, once, inside ``fence_rewrite``."""
    self._explicit_effort_default_pending = True
    self._fence_explicit_effort_default_rewrite = fence_rewrite


def explicit_effort_default_applied(self: AcpProvider) -> bool:
    """Whether this provider replaced its pending Default overlay successfully."""
    return self._explicit_effort_default_applied


def _apply_tool_search_overlay(self: AcpProvider) -> None:
    """Write the kiro Tool Search setting into the workspace cli.json overlay.

    Tool Search is read from this file at spawn by kiro-cli's Rust engine
    only, so the write is scoped to ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY`` --
    narrower than ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``, because KAS shares
    the slash-command dialect but takes this setting over the wire instead
    (:meth:`_tool_search_settings`). A no-op as well when no toggle value was
    supplied (``self._tool_search is None``). Called before every (re)spawn
    so resume/restart keeps the same setting.
    """
    if self._client.backend not in ACP_BACKENDS_TOOL_SEARCH_OVERLAY:
        return
    if self._tool_search is None:
        return
    try:
        _write_tool_search_overlay(
            self._client._work_dir,
            self._tool_search,
            self._tool_search_min_pct,
            self._tool_search_min_tokens,
        )
        # The interpolated values are a bool and two integer thresholds, plus a
        # path. Semgrep matches on the word "tokens" in the message string, not
        # on the arguments; the setting names are kept verbatim so the log line
        # greps against the kiro settings it writes.
        logger.debug(  # nosemgrep: python-logger-credential-disclosure
            "ACP MCP Tool Search overlay applied: enabled=%s minPct=%s minTokens=%s (%s)",
            self._tool_search,
            self._tool_search_min_pct,
            self._tool_search_min_tokens,
            self._client._work_dir / ".kiro" / "settings" / "cli.json",
        )
    except Exception:
        logger.warning("ACP tool-search overlay write failed", exc_info=True)
