"""Write, clear and recover the workspace ``cli.json`` overlay a kiro-family harness reads at spawn.

The effort and Tool Search keys of ``<work_dir>/.kiro/settings/cli.json``: the file
writers and readers, and the ``AcpProvider`` members that apply the session's level and
Tool Search choice to it before every spawn.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields read: ``_client``, ``_tool_search``, ``_tool_search_min_pct``,
``_tool_search_min_tokens``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.acp.types import ACP_BACKENDS_EFFORT_OVERLAY, ACP_BACKENDS_TOOL_SEARCH_OVERLAY
from kiro_crew.agent_sdk.tool_search import (
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
    clamp_min_pct,
    clamp_min_tokens,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.effort import effort_settings_key
from kiro_crew.workspace_cli_settings import (
    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    CLI_SETTINGS_LOCK_TIMEOUT_SECS,
    workspace_cli_settings_lock,
)

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


#: Decoding for the workspace ``cli.json``. The file can be a person's own
#: workspace settings, and Windows editors save UTF-8 with a byte-order mark,
#: which ``json.loads`` refuses on decoded text. A refused file reads as empty
#: here, and the overlay writers then replace it with only Kiro Crew's keys, so
#: every reader drops one leading mark. Writers emit plain UTF-8.
_CLI_JSON_ENCODING = "utf-8-sig"


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
    with workspace_cli_settings_lock(work_dir, timeout=timeout) as cli_json:
        try:
            existing = (
                json.loads(cli_json.read_text(encoding=_CLI_JSON_ENCODING))
                if cli_json.exists()
                else {}
            )
        except (json.JSONDecodeError, OSError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
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
        atomic_write(
            cli_json, json.dumps(existing, indent=2)
        )  # atomic: readers never see a partial file


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
    with workspace_cli_settings_lock(work_dir) as cli_json:
        try:
            existing = (
                json.loads(cli_json.read_text(encoding=_CLI_JSON_ENCODING))
                if cli_json.exists()
                else {}
            )
        except (json.JSONDecodeError, OSError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["toolSearch.enabled"] = bool(enabled)
        if enabled:
            existing["toolSearch.minPct"] = _clamp_min_pct(min_pct)
            existing["toolSearch.minTokens"] = _clamp_min_tokens(min_tokens)
        else:
            # Drop the thresholds when disabling so nothing is left behind that
            # would take effect if a later build flips the global default on.
            existing.pop("toolSearch.minPct", None)
            existing.pop("toolSearch.minTokens", None)
        atomic_write(
            cli_json, json.dumps(existing, indent=2)
        )  # atomic: readers never see a partial file


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
        with workspace_cli_settings_lock(
            work_dir, timeout=CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS
        ) as cli_json:
            if not cli_json.exists():
                return True
            try:
                data = json.loads(cli_json.read_text(encoding=_CLI_JSON_ENCODING))
            except json.JSONDecodeError:
                # A malformed file names no effort for any model, and
                # ``_read_cli_overlay`` reads it as ``{}`` too, so a respawn
                # re-seeds nothing. The postcondition already holds.
                return True
            except OSError:
                # The file EXISTS and this call holds the lock, so a read that
                # fails here is transient IO (a Windows sharing violation, say),
                # NOT an absent entry: the level may well still be on disk.
                # Reporting success would be the silent stale reload this
                # function's own return value exists to prevent.
                logger.debug("ACP effort overlay read failed", exc_info=True)
                return False
            if not isinstance(data, dict):
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
            atomic_write(cli_json, json.dumps(data, indent=2))  # atomic
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
        data = json.loads(cli_json.read_text(encoding=_CLI_JSON_ENCODING))
    except (json.JSONDecodeError, OSError):
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


def _apply_effort_overlay(
    self: AcpProvider, *, timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS
) -> bool:
    """Write the kiro workspace cli.json overlay for (current model, effort).

    Written only for the harnesses that READ it (``ACP_BACKENDS_EFFORT_OVERLAY``
    — kiro-cli takes effort from this file at spawn); every other harness takes
    its level live, KAS as the ``effortLevel`` config option. Also a no-op when
    the model is not effort-capable or no level resolves. Called before every
    (re)spawn so resume/restart keeps the same level.

    KAS is not written: the file is shared by every session in the work dir and
    construction reads it back, so a level one KAS chat left there would start
    the next one. A KAS slot's own level survives a gateway restart through the
    slot's metadata instead, which the factory hands back as its override.

    Membership rather than "not claude": the companion reads and clears (provider
    construction, :meth:`change_effort`, :meth:`clear_effort`) are gated on the
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
