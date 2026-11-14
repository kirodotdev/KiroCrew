"""Write, clear and recover the workspace ``cli.json`` overlay a kiro-family harness reads at spawn.

The effort and Tool Search keys of ``<work_dir>/.kiro/settings/cli.json``: the file
writers and readers, and the ``AcpProvider`` members that apply the session's level and
Tool Search choice to it before every spawn.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_effort_overlay_recovered``, ``_effort_per_model``.

Instance fields read: ``_client``, ``_tool_search``, ``_tool_search_min_pct``,
``_tool_search_min_tokens``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.acp.types import ACP_BACKENDS_EFFORT_OVERLAY, ACP_BACKENDS_TOOL_SEARCH_OVERLAY
from kiro_crew.agent_sdk.tool_search import (
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
    clamp_min_pct,
    clamp_min_tokens,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.effort import effort_settings_key
from kiro_crew.workspace_cli_settings import (
    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    LockedCliSettings,
    locked_workspace_cli_settings,
    named_cli_json_metadata,
    pinned_cli_settings_fd,
    publish_locked_cli_settings,
    read_named_cli_json,
    read_pinned_cli_json,
)

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


#: Decoding for the workspace ``cli.json``. The file can be a person's own
#: workspace settings, and Windows editors save UTF-8 with a byte-order mark,
#: which ``json.loads`` refuses on decoded text. A file that does not parse is
#: left unchanged and the write raises, so every reader drops one leading mark
#: rather than refusing a file a person saved. Writers emit plain UTF-8.
_CLI_JSON_ENCODING = "utf-8-sig"

#: A settings file is kilobytes. Past this Kiro Crew neither reads it nor
#: rewrites it: the repository can track a ``cli.json`` of any size, and a
#: whole-file read and parse of it holds the shared settings lock for as long as
#: that takes and grows memory with the file. A larger file is left unread and
#: unchanged, so none of Kiro Crew's own keys land in it. The same ceiling bounds
#: the bytes a rewrite would publish, because an indented rewrite of a file just
#: under it can grow past it and the next read would then refuse the file this
#: module wrote.
_CLI_JSON_MAX_BYTES = 1024 * 1024


class _OverlayTooLarge(ValueError):
    """Raised when a workspace cli.json overlay exceeds the read ceiling.

    The ceiling applies to the file as read and to the bytes a rewrite would publish.
    """


_CliJsonVersion = tuple[int, int, int, int, int]


def _load_cli_json(cli_json: Path) -> object:
    """Parse *cli_json* after reading at most ``_CLI_JSON_MAX_BYTES`` bytes of it.

    Raises :class:`_OverlayTooLarge` for a larger file without reading the rest,
    ``OSError`` when it cannot be opened (an absent file included) and
    ``json.JSONDecodeError`` when it does not parse.
    """
    with cli_json.open("rb") as handle:
        raw = bounded_read(handle, _CLI_JSON_MAX_BYTES + 1)
    if len(raw) > _CLI_JSON_MAX_BYTES:
        raise _OverlayTooLarge(f"{cli_json} is larger than {_CLI_JSON_MAX_BYTES} bytes")
    return json.loads(raw.decode(_CLI_JSON_ENCODING))


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
            named = named_cli_json_metadata(settings.cli_json)
            if named is None:
                return None
            metadata = named
        else:
            metadata = os.stat("cli.json", dir_fd=settings_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(
            f"workspace cli.json overlay {settings.cli_json} changed while the overlay "
            "was written; left unchanged"
        ) from exc
    return _cli_json_version(metadata)


def _read_pinned_cli_json(
    settings_fd: int, cli_json: Path
) -> tuple[bytes, os.stat_result, os.stat_result] | None:
    """Read ``cli.json`` through the shared pinned settings reader."""
    return read_pinned_cli_json(settings_fd, cli_json, max_bytes=_CLI_JSON_MAX_BYTES)


def _read_named_cli_json(
    cli_json: Path, settings_dir: Path
) -> tuple[bytes, os.stat_result, os.stat_result] | None:
    """Read ``cli.json`` by name through the shared settings reader."""
    return read_named_cli_json(cli_json, settings_dir, max_bytes=_CLI_JSON_MAX_BYTES)


def _read_cli_overlay_document(
    settings: LockedCliSettings,
) -> tuple[dict[str, Any], _CliJsonVersion | None]:
    """Return the settings object an overlay writer merges into, and the version it read.

    The version is the file's metadata from before its bytes were read, so the
    guarded publish refuses after any later change, even one during the read. A
    missing file is an empty document. An existing file that cannot be read, is a
    link, is hardlinked, is non-regular, or escapes the workspace settings directory
    raises so the writer leaves its bytes untouched. An overlay past the ceiling is
    left unchanged and the write raises, as an unparseable or non-object one does.
    Where descriptor-relative writes are supported, the read and publish use the
    settings directory the lock opened, so a folder replaced or linked after the
    lock cannot receive the write and the writer raises. Elsewhere both go by name;
    on Windows the lock holds ``.kiro`` and ``settings`` open meanwhile, so neither
    can be renamed or deleted until the writer is done.
    """
    cli_json = settings.cli_json
    settings_fd = pinned_cli_settings_fd(settings)
    if settings_fd is not None:
        read_result = _read_pinned_cli_json(settings_fd, cli_json)
    else:
        if platform_compat.is_link_or_junction(cli_json):
            raise OSError(f"workspace cli.json overlay {cli_json} is a link; left unchanged")
        read_result = _read_named_cli_json(cli_json, settings.settings_dir)
    if read_result is None:
        return {}, None
    raw, before_read, _after_read = read_result
    file_version = _cli_json_version(before_read)
    if len(raw) > _CLI_JSON_MAX_BYTES:
        raise _OverlayTooLarge(
            f"workspace cli.json overlay {cli_json} is past the "
            f"{_CLI_JSON_MAX_BYTES}-byte ceiling; left unchanged"
        )
    try:
        existing = json.loads(raw.decode(_CLI_JSON_ENCODING))
    except Exception as exc:
        # Every failure to turn bytes into a settings object leaves the file unchanged.
        raise ValueError(
            f"workspace cli.json overlay {cli_json} could not be parsed as a settings document; "
            "left unchanged"
        ) from exc
    if not isinstance(existing, dict):
        raise ValueError(
            f"workspace cli.json overlay {cli_json} is not a JSON object; left unchanged"
        )
    return existing, file_version


def _publish_cli_overlay(
    settings: LockedCliSettings,
    document: dict[str, Any],
    read_version: _CliJsonVersion | None,
) -> None:
    """Publish one overlay into the settings directory the lock opened, where supported.

    A settings directory replaced or linked by name after the lock cannot receive the
    write. A ``cli.json`` changed after the read is left unchanged and the writer
    raises. A document whose bytes would exceed the read ceiling raises
    :class:`_OverlayTooLarge` before anything is written. A writer that changes the
    file after the version check can still race the rename; closing that window
    requires every writer to take the shared lock. Platforms without
    descriptor-relative writes use the by-name write floor.
    """
    settings_fd = pinned_cli_settings_fd(settings)
    if settings_fd is not None and not settings.holds_named_settings_dir():
        raise OSError("workspace CLI settings directory changed before the overlay was written")
    current_version = _current_cli_json_version(settings, settings_fd)
    if current_version != read_version:
        raise OSError(
            f"workspace cli.json overlay {settings.cli_json} changed while the overlay "
            "was written; left unchanged"
        )
    # Bytes, so no platform rewrites line endings: the file keeps LF on Windows
    # too, and the ceiling is measured on what lands on disk.
    content = json.dumps(document, indent=2).encode("utf-8")
    if len(content) > _CLI_JSON_MAX_BYTES:
        raise _OverlayTooLarge(
            f"workspace cli.json overlay {settings.cli_json} would exceed the "
            f"{_CLI_JSON_MAX_BYTES}-byte ceiling; left unchanged"
        )
    publish_locked_cli_settings(settings, content, write=atomic_write)


def _write_cli_overlay(
    work_dir: Path,
    model: str,
    effort: str,
    *,
    timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS,
) -> None:
    """Write a workspace cli.json overlay so kiro-cli applies effort at spawn.

    Path: ``<work_dir>/.kiro/settings/cli.json``. Workspace settings override
    the global ``~/.kiro/settings/cli.json``, so nothing here touches the user's
    global kiro settings. The overlay's scope is that WORK DIRECTORY, not a
    session: every session served by one runtime shares the work dir, so the
    file is shared by all of them, and kiro-cli reads it only at spawn. A write
    therefore reaches no session that is already running -- it lands on the next
    respawn, and then for every session that respawn serves. Merge-safe and
    idempotent — safe to call before every spawn.

    Its unit of state is therefore ``(work_dir, model)`` and never
    ``(session, model)``: two sessions on one runtime running the same model have
    one entry between them, and the last write is the one a respawn reads.
    Nothing reaches this writer except :class:`AcpProvider`, which is the object
    that spawns the runtime and so always owns it, which is why a live effort
    change is pushed to the running session separately
    (:meth:`AcpProvider.change_effort`) rather than through this file. A session
    that joins a runtime it did not spawn has no path here at all today; if one
    is added, it needs to say that its write cannot reach the running process.

    The effort sub-key is family-specific (``effort_settings_key``): Claude
    models use ``output_config``, GPT models use ``reasoning`` — kiro-cli
    silently ignores the wrong key, so the shape must match the model.

    Format::

        {"chat.modelDefaults": {"<model>": {"<key>": {"effort": "<level>"}}}}
    """
    with locked_workspace_cli_settings(work_dir, timeout=timeout) as settings:
        existing, read_version = _read_cli_overlay_document(settings)
        model_defaults = existing.get("chat.modelDefaults")
        if not isinstance(model_defaults, dict):
            model_defaults = {}
        model_cfg = model_defaults.get(model)
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        key = effort_settings_key(model)
        effort_cfg = model_cfg.get(key)
        if not isinstance(effort_cfg, dict):
            effort_cfg = {}
        effort_cfg["effort"] = effort
        model_cfg[key] = effort_cfg
        # Recovery checks output_config first, so leaving effort under both family
        # keys can resurrect a stale value after restart.
        other_key = "reasoning" if key == "output_config" else "output_config"
        other_effort_cfg = model_cfg.get(other_key)
        if isinstance(other_effort_cfg, dict) and "effort" in other_effort_cfg:
            other_effort_cfg.pop("effort")
            if not other_effort_cfg:
                model_cfg.pop(other_key, None)
        model_defaults[model] = model_cfg
        existing["chat.modelDefaults"] = model_defaults
        _publish_cli_overlay(settings, existing, read_version)


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
    """
    with locked_workspace_cli_settings(work_dir) as settings:
        existing, read_version = _read_cli_overlay_document(settings)
        existing["toolSearch.enabled"] = bool(enabled)
        if enabled:
            existing["toolSearch.minPct"] = _clamp_min_pct(min_pct)
            existing["toolSearch.minTokens"] = _clamp_min_tokens(min_tokens)
        else:
            # Drop the thresholds when disabling so nothing is left behind that
            # would take effect if a later build flips the global default on.
            existing.pop("toolSearch.minPct", None)
            existing.pop("toolSearch.minTokens", None)
        _publish_cli_overlay(settings, existing, read_version)


def _clear_cli_overlay_effort(work_dir: Path, model: str) -> bool:
    """Remove the effort entry for *model* from the workspace cli.json overlay.

    Merge-safe: leaves other models' settings intact and drops now-empty
    containers. No-op when the file or entry is absent.

    Returns whether the overlay holds no effort for *model* once this returns.
    False means the shared settings lock could not be taken, which with the
    ACTION ceiling means a stuck holder rather than the routine contention the
    startup ceiling sees -- projection's own critical section is sub-second. That
    distinction is what keeps this answer two-valued: a failure mode that happens
    by design would need a third state, and one that means "something is wrong"
    does not. BLOCKING on the event loop for that ceiling would freeze every
    session, so every caller must reach this off the loop.

    Every other exit is a success: an absent file, an absent entry, a removed
    entry and a malformed file all leave the overlay naming no effort for *model*.
    """
    try:
        with locked_workspace_cli_settings(
            work_dir, timeout=CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS
        ) as settings:
            try:
                data, read_version = _read_cli_overlay_document(settings)
            except _OverlayTooLarge:
                raise
            except ValueError:
                # A file that does not parse as a settings object names no effort
                # for any model, and ``_read_cli_overlay`` reads it as ``{}`` too,
                # so a respawn re-seeds nothing. The postcondition already holds,
                # and the file is left as the person wrote it.
                return True
            if not data:
                return True
            model_defaults = data.get("chat.modelDefaults")
            if not isinstance(model_defaults, dict):
                return True
            model_cfg = model_defaults.get(model)
            if isinstance(model_cfg, dict):
                # Clear whichever sub-key holds effort. Sweep both known shapes so a
                # model whose family key changed (or an overlay written by an older
                # build) is fully cleaned up, not just the current-family key.
                for key in ("output_config", "reasoning"):
                    effort_cfg = model_cfg.get(key)
                    if isinstance(effort_cfg, dict):
                        effort_cfg.pop("effort", None)
                        if not effort_cfg:
                            model_cfg.pop(key, None)
                if not model_cfg:
                    model_defaults.pop(model, None)
            _publish_cli_overlay(settings, data, read_version)
    except _OverlayTooLarge:
        # The file is left as it was and may still name a level for *model*:
        # the clear did not happen, which is the False this function answers.
        logger.warning("ACP effort overlay clear refused: workspace cli.json is too large")
        return False
    except OSError:
        logger.debug("ACP effort overlay clear failed", exc_info=True)
        return False
    return True


def _read_cli_overlay(work_dir: Path) -> dict[str, str]:
    """Recover ``{model: level}`` from an existing workspace cli.json overlay.

    Called on provider init so a server restart (which loses in-memory slot
    state) still reflects the effort kiro-cli will actually use. Returns ``{}``
    when the file is missing, malformed, or has no effort entries.
    """
    cli_json = work_dir / ".kiro" / "settings" / "cli.json"
    if not cli_json.exists():
        return {}
    try:
        data = _load_cli_json(cli_json)
    except (json.JSONDecodeError, OSError, _OverlayTooLarge):
        return {}
    if not isinstance(data, dict):
        return {}
    model_defaults = data.get("chat.modelDefaults")
    if not isinstance(model_defaults, dict):
        return {}
    out: dict[str, str] = {}
    for model, cfg in model_defaults.items():
        if not isinstance(cfg, dict):
            continue
        # Effort lives under output_config (Claude) or reasoning (GPT); read
        # whichever is present so recovery works for both families.
        for key in ("output_config", "reasoning"):
            eff_cfg = cfg.get(key)
            if isinstance(eff_cfg, dict):
                eff = eff_cfg.get("effort")
                if isinstance(eff, str) and eff:
                    out[model] = eff
                    break
    return out


async def _recover_effort_overlay(self: AcpProvider) -> None:
    """Fill the effort map from the levels the workspace overlay already holds.

    Once per provider, on its first start: after a gateway restart the map then
    names what kiro-cli will run. Caller-provided overrides win -- only gaps are
    filled. A later restart of the same provider does not read again, because the
    file is keyed by work dir and another chat's last pick would otherwise be
    adopted as this chat's own.

    The overlay's own readers only (``ACP_BACKENDS_EFFORT_OVERLAY``): read back by
    a harness that never opens the file (KAS), it would start a new chat at
    whatever level another chat in the same work dir picked last. Such a harness
    takes its level from the factory alone.

    The read is bounded (``_CLI_JSON_MAX_BYTES``) and runs in a worker thread, so
    the file is never opened on the event loop; the map is filled back on the loop.
    """
    if self._effort_overlay_recovered or self._client.backend not in ACP_BACKENDS_EFFORT_OVERLAY:
        return
    self._effort_overlay_recovered = True
    try:
        recovered = await asyncio.to_thread(_read_cli_overlay, self._client._work_dir)
    except Exception:
        logger.debug("ACP effort overlay recovery failed", exc_info=True)
        return
    for model, level in recovered.items():
        self._effort_per_model.setdefault(model, level)


def _apply_effort_overlay(
    self: AcpProvider, *, timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS
) -> bool:
    """Write the kiro workspace cli.json overlay for (current model, effort).

    Written only for the harnesses that READ it (``ACP_BACKENDS_EFFORT_OVERLAY``
    — kiro-cli takes effort from this file at spawn); every other harness takes
    its level live, KAS as the ``effortLevel`` config option. Also a no-op when
    the model is not effort-capable or no level resolves. Called from ``start()``
    before every (re)spawn so resume/restart keeps the same level, and never from
    the constructor, which runs on the event loop.

    KAS is not written: the file is shared by every session in the work dir and
    the first start reads it back, so a level one KAS chat left there would start
    the next one. A KAS slot's own level survives a gateway restart through the
    slot's metadata instead, which the factory hands back as its override.

    Membership rather than "not claude": the companion reads and clears (provider
    start-up recovery, :meth:`change_effort`, :meth:`clear_effort`) are gated on the
    same set, so a wider gate here writes an overlay for a harness they never
    reach — a stale file left in the user's workspace.
    """
    if self._client.backend not in ACP_BACKENDS_EFFORT_OVERLAY:
        return True
    model = self._effort_model
    level = self._resolve_effort()
    if not model or not level:
        return True
    try:
        _write_cli_overlay(self._client._work_dir, model, level, timeout=timeout)
    except Exception:
        # The FILE is what a respawn reads, so a caller that reports a live
        # change on a failed write promises a level the next reset undoes.
        logger.warning("ACP effort overlay write failed", exc_info=True)
        return False
    logger.debug("ACP effort overlay applied: model=%s effort=%s", model, level)
    return True


def _apply_tool_search_overlay(self: AcpProvider) -> None:
    """Write the kiro Tool Search setting into the workspace cli.json overlay.

    Tool Search is read from this file at spawn by kiro-cli's Rust engine
    only, so the write is scoped to ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY`` --
    narrower than ``ACP_BACKENDS_KIRO_FAMILY``, because KAS is in the kiro
    family but takes this setting over the wire instead
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
