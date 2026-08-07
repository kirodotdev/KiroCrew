"""ACP provider — wraps existing AcpClient behind LLMProvider interface."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kiro_crew.acp.client import (
    _NOT_LOGGED_IN_MESSAGE,
    DEFAULT_MODEL,
    AcpAuthRequired,
    AcpClient,
    AcpError,
    advertised_model_ids,
    model_is_unusable,
)
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_COMPACTION_STATUS,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.effort import (
    EFFORT_LEVELS,
    effort_settings_key,
    model_supports_effort,
    resolve_effort_for_model,
)
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.providers.base import (
    CancelOutcome,
    LLMEvent,
    LLMProvider,
)
from kiro_crew.providers.cleanup import _is_safe_path

logger = logging.getLogger(__name__)


def _write_cli_overlay(work_dir: Path, model: str, effort: str) -> None:
    """Write a workspace cli.json overlay so kiro-cli applies effort at spawn.

    Path: ``<work_dir>/.kiro/settings/cli.json``. Workspace settings override
    the global ``~/.kiro/settings/cli.json`` so this only affects the slot's
    own session. Merge-safe and idempotent — safe to call before every spawn.

    The effort sub-key is family-specific (``effort_settings_key``): Claude
    models use ``output_config``, GPT models use ``reasoning`` — kiro-cli
    silently ignores the wrong key, so the shape must match the model.

    Format::

        {"chat.modelDefaults": {"<model>": {"<key>": {"effort": "<level>"}}}}
    """
    settings_dir = work_dir / ".kiro" / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    cli_json = settings_dir / "cli.json"
    try:
        existing = json.loads(cli_json.read_text(encoding="utf-8")) if cli_json.exists() else {}
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
    atomic_write(cli_json, json.dumps(existing, indent=2))  # atomic: readers never see a partial file (#426)


def _write_tool_search_overlay(work_dir: Path, enabled: bool) -> None:
    """Write kiro Tool Search settings into the workspace cli.json overlay.

    Path: ``<work_dir>/.kiro/settings/cli.json`` — the SAME per-session overlay
    used for effort. Workspace settings override the global
    ``~/.kiro/settings/cli.json`` so this only affects this slot's own kiro-cli
    session and never mutates the user's global kiro settings.

    Tool Search (https://kiro.dev/docs/cli/mcp/tool-search/) loads MCP tool
    specs on demand ("search-and-call") instead of sending every spec each
    turn. When *enabled* we also zero ``minPct``/``minTokens`` so deferral is
    always active whenever MCP tools are present — KiroCrew sessions always
    carry several MCP servers (well past kiro's default 50k-token trigger), but
    zeroing guarantees the search-and-call behavior rather than relying on the
    spec size crossing a threshold. The flag is written deterministically for
    BOTH true and false so the KiroCrew toggle stays authoritative regardless
    of any value in the user's global kiro settings.

    Merge-safe and idempotent — preserves the effort ``chat.modelDefaults`` keys
    and any other settings already present. kiro cli.json uses flat dotted keys
    (e.g. ``"chat.modelDefaults"``), so the Tool Search keys are written flat to
    match.
    """
    settings_dir = work_dir / ".kiro" / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    cli_json = settings_dir / "cli.json"
    try:
        existing = json.loads(cli_json.read_text(encoding="utf-8")) if cli_json.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["toolSearch.enabled"] = bool(enabled)
    if enabled:
        # Force always-on deferral regardless of tool-spec size.
        existing["toolSearch.minPct"] = 0
        existing["toolSearch.minTokens"] = 0
    else:
        # Drop the forced-on thresholds when disabling so we don't leave zeroed
        # thresholds behind that would silently force-activate Tool Search if a
        # later build flips the global default on.
        existing.pop("toolSearch.minPct", None)
        existing.pop("toolSearch.minTokens", None)
    atomic_write(cli_json, json.dumps(existing, indent=2))  # atomic: readers never see a partial file (#426)


def _clear_cli_overlay_effort(work_dir: Path, model: str) -> None:
    """Remove the effort entry for *model* from the workspace cli.json overlay.

    Merge-safe: leaves other models' settings intact and drops now-empty
    containers. No-op when the file or entry is absent.
    """
    cli_json = work_dir / ".kiro" / "settings" / "cli.json"
    if not cli_json.exists():
        return
    try:
        data = json.loads(cli_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    model_defaults = data.get("chat.modelDefaults")
    if not isinstance(model_defaults, dict):
        return
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
    try:
        atomic_write(cli_json, json.dumps(data, indent=2))  # atomic (#426)
    except OSError:
        logger.debug("ACP effort overlay clear failed", exc_info=True)


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
        data = json.loads(cli_json.read_text(encoding="utf-8"))
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


# ── F2 load-recovery: session/load retry past a stale native session lock ──
# A kiro-cli holder killed uncleanly (SIGKILL, crash, OOM, or a gateway-restart
# drain timeout) can leave its per-session lock held briefly. Re-issuing
# session/load a few times with exponential backoff lets the stale lock release
# so the new gateway resumes LOSSLESSLY. If the lock never clears we fall back
# to a fresh session + KiroCrew history replay (see _start_kiro_runtime_impl).
_RESUME_MAX_ATTEMPTS = 4  # total session/load attempts before fresh fallback
_RESUME_BACKOFF_BASE_S = 1.0  # backoff = base * 2**attempt → 1s, 2s, 4s between attempts


class AcpProvider(LLMProvider):
    """LLMProvider backed by ACP JSON-RPC over stdio (kiro-cli or claude-agent-acp)."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str | None = None,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        effort_per_model: dict[str, str] | None = None,
        effort_defaults: object = None,
        tool_search: bool | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_settings_mcp_json: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "work_dir": work_dir,
            "model": model,
            "sandbox_mode": sandbox_mode,
            "session_key": session_key,
            "channel_id": channel_id,
            "extra_env": extra_env,
            "acp_backend": acp_backend,
            "mcp_gateway_overlay": mcp_gateway_overlay,
            "mcp_gateway_settings_mcp_json": mcp_gateway_settings_mcp_json,
            "mcp_gateway_socket": mcp_gateway_socket,
            # Claude permission mode (Auto-mode/permission-UI parity). None on the
            # kiro-cli path — fully inert; a companion-registered backend threads
            # it.
            "permission_mode": permission_mode,
        }
        if agent:
            kwargs["agent"] = agent
        self._client = AcpClient(**kwargs)
        # F2 load-recovery: set True by _start_kiro_runtime_impl when a resume
        # falls back to a FRESH native session (the prior session's lock never
        # cleared). Signals SessionManager.get_or_create to replay KiroCrew's
        # conversation_log into the fresh session on the first prompt so the slot
        # is not context-free.
        self._history_replay_needed: bool = False
        # Terminal compaction status captured by compact() while draining its
        # prompt turn; consumed by wait_for_compaction() (see compact()).
        self._compact_result: dict | None = None
        # Per-model reasoning-effort. Resolution priority (see effort.py):
        # slot override > workspace defaults > None. The kiro backend applies
        # this via a workspace cli.json overlay read at spawn; the claude
        # backend applies it live via session/set_config_option (no overlay).
        self._effort_per_model: dict[str, str] = dict(effort_per_model or {})
        self._effort_defaults = effort_defaults
        # MCP Tool Search toggle (kiro-cli backend only). None = caller did not
        # specify (e.g. claude_code factory), so leave the overlay untouched.
        # True/False = write the kiro settings overlay deterministically so the
        # KiroCrew toggle is authoritative over any global kiro setting.
        self._tool_search = tool_search
        if not self.is_claude_backend:
            # Recover overlay-persisted levels (server-restart resilience) and
            # write the overlay BEFORE the first spawn so kiro-cli reads it on
            # session/new. Caller-provided overrides win — only fill gaps.
            try:
                for m, lvl in _read_cli_overlay(self._client._work_dir).items():
                    self._effort_per_model.setdefault(m, lvl)
            except Exception:
                logger.debug("ACP effort overlay recovery failed", exc_info=True)
            self._apply_effort_overlay()
            self._apply_tool_search_overlay()

    @property
    def client(self) -> AcpClient:
        """Expose underlying client for backward compat (e.g. is_ready check)."""
        return self._client

    @property
    def cwd(self) -> str:
        """Working directory this provider operates in.

        Overrides the ``LLMProvider`` default ("") so session_map can persist
        the real workspace path for both ACP backends. The work_dir lives on
        the underlying client (``self._client._work_dir``), not the provider.
        """
        return str(self._client._work_dir)

    @property
    def is_claude_backend(self) -> bool:
        """True when this ACP provider talks to claude-agent-acp (vs kiro-cli)."""
        return self._client.backend == ACP_BACKEND_CLAUDE

    @property
    def is_session_sharing_eligible(self) -> bool:
        """True when this provider can host multiplexed subagent sessions.

        Session sharing requires the kiro-cli backend (which supports N
        concurrent sessions per process via AcpRuntime demux). The Claude
        Code backend uses AcpClient (one process per session) and is never
        eligible, so subagents fall back to the legacy per-process path.
        """
        return not self.is_claude_backend

    async def _start_kiro_runtime(self) -> None:
        """Spawn an AcpRuntime + session; time the kiro cold-start split.

        Thin telemetry wrapper over ``_start_kiro_runtime_impl`` so the DEFAULT
        (kiro) backend's cold start is finally measured. ``AcpClient.ensure_ready``
        already emits ``kirocrew.session.startup.duration`` for the *claude* path
        only; the kiro path went through here with no duration metric. We reuse the
        same histogram, tagged ``backend=kiro`` + ``phase=<...>`` so the phase split
        (spawn+initialize vs session/new-the-MCP-toolset-load vs set_model) is
        visible. Best-effort — a telemetry failure never affects startup.
        """
        t0 = time.monotonic()
        phases: dict[str, float] = {}
        # Capture the session key BEFORE the impl runs: on the success path it
        # replaces self._client with an AcpSessionProvider whose _session_key
        # starts empty, so reading it at emit time would file every successful
        # cold start under channel=unknown — exactly the case being measured.
        meta: dict[str, object] = {"session_key": getattr(self._client, "_session_key", None)}
        outcome = "error"
        try:
            await self._start_kiro_runtime_impl(phases, meta)
            outcome = "ready"
        except AcpAuthRequired:
            outcome = "auth_required"
            raise
        finally:
            self._emit_kiro_startup_metric(t0, phases, outcome, meta)

    def _emit_kiro_startup_metric(
        self,
        t0: float,
        phases: dict[str, float],
        outcome: str,
        meta: dict[str, object] | None = None,
    ) -> None:
        """Emit the kiro cold-start histogram (total + per-phase). Best-effort.

        Every point from this path is a cold start: ``_start_kiro_runtime_impl``
        always constructs and spawns a fresh ``AcpRuntime``, and the warm
        fast-path returns before reaching here. The Telemetry aggregator splits
        cold from warm on ``spawned``, so it is emitted unconditionally true.

        ``channel`` records WHICH conversation source paid the cost and
        ``resumed`` which construction path ran (``session/load`` vs
        ``session/new``) — without them a duration cannot be attributed to a
        surface or a path, which is the whole diagnostic question for TTFT.
        """
        try:
            # circular import: importing get_recorder at module top would form a
            # config.loader -> ... -> metrics.provider -> config.loader cycle
            # (same reason as the lazy import in acp/client.py's ensure_ready).
            # Keep it lazy so provider is never loaded during config.loader's
            # import chain.
            from kiro_crew.metrics.provider import get_recorder

            # The key comes from meta, captured before startup replaced
            # self._client (see _start_kiro_runtime); the live client is only a
            # fallback for callers that emit without seeding meta.
            _meta = meta or {}
            session_key = _meta.get("session_key") or getattr(
                getattr(self, "_client", None), "_session_key", None
            )
            channel = telemetry_channel_of(session_key if isinstance(session_key, str) else None)
            base_attrs: dict[str, str | int | bool | float] = {
                "backend": "kiro",
                "outcome": outcome,
                "spawned": True,
                "channel": channel,
                "resumed": bool(_meta.get("resumed", False)),
            }
            rec = get_recorder()
            total_ms = (time.monotonic() - t0) * 1000.0
            rec.histogram(
                "kirocrew.session.startup.duration",
                total_ms,
                unit="ms",
                attrs={**base_attrs, "phase": "total"},
            )
            for phase, ms in phases.items():
                rec.histogram(
                    "kirocrew.session.startup.duration",
                    ms,
                    unit="ms",
                    attrs={**base_attrs, "phase": phase},
                )
            resume_outcome = _meta.get("resume_outcome")
            if resume_outcome:
                rec.counter(
                    "kirocrew.session.resume.outcome",
                    1,
                    attrs={
                        "outcome": str(resume_outcome),
                        "channel": channel,
                    },
                )
        except Exception:  # never let telemetry break session startup
            logger.debug("kiro startup metric emit failed", exc_info=True)

    async def _load_session_with_retry(
        self,
        runtime: AcpRuntime,
        session_file: str,
        resume_sid: str,
        work_dir: str | Path | None,
        agent: str | None,
    ) -> AcpSessionHandle | None:
        """Resume via session/load, retrying past a stale native session lock.

        F2 load-recovery (Phase 1). A kiro-cli holder killed uncleanly can leave
        its per-session lock held for a short window; re-issuing session/load a
        few times with exponential backoff lets the stale lock release so we
        resume LOSSLESSLY (full native history). Outcomes:

        * load succeeds            → return the handle (fast path: no sleep).
        * "active in another        → retry up to ``_RESUME_MAX_ATTEMPTS`` with
          process" lock held         backoff; return the handle if it clears.
        * lock never clears        → return ``None`` (caller falls back to a
                                       fresh session + history replay — Phase 2).
        * any OTHER load error     → return ``None`` immediately (retrying a
                                       genuine failure only wastes time).
        * runtime dies mid-retry   → return ``None`` (caller's respawn handles it).
        """
        for attempt in range(_RESUME_MAX_ATTEMPTS):
            try:
                handle = await runtime.load_session(
                    session_file,
                    resume_sid,
                    cwd=work_dir,
                    agent=agent or None,
                )
                if attempt:
                    logger.info(
                        "Resume of kiro session %s recovered on attempt %d/%d "
                        "(stale lock released)",
                        resume_sid,
                        attempt + 1,
                        _RESUME_MAX_ATTEMPTS,
                    )
                return handle
            except Exception as exc:
                if "active in another process" not in str(exc).lower():
                    # Genuine load failure — will not clear with time.
                    logger.warning(
                        "Failed to resume session %s, starting fresh",
                        resume_sid,
                        exc_info=True,
                    )
                    return None
                if not runtime.is_alive():
                    logger.warning(
                        "Runtime died while retrying resume of session %s; starting fresh",
                        resume_sid,
                    )
                    return None
                if attempt + 1 < _RESUME_MAX_ATTEMPTS:
                    backoff = _RESUME_BACKOFF_BASE_S * (2**attempt)
                    logger.info(
                        "Resume of kiro session %s refused (lock active in "
                        "another process); retry %d/%d in %.1fs",
                        resume_sid,
                        attempt + 1,
                        _RESUME_MAX_ATTEMPTS,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
        # Exhausted every attempt on a persistent lock. Loud, grep-able marker;
        # the caller migrates to a fresh session with KiroCrew history replay.
        logger.warning(
            "Resume of kiro session %s failed after %d attempts (lock still "
            "active in another process — a stale lock from an uncleanly-killed "
            "holder, or a rare same-gateway session-key alias miss); migrating "
            "to a fresh session with KiroCrew history replay",
            resume_sid,
            _RESUME_MAX_ATTEMPTS,
        )
        return None

    async def _start_kiro_runtime_impl(
        self, phases: dict[str, float], meta: dict[str, object]
    ) -> None:
        """Spawn an AcpRuntime and replace self._client with AcpSessionProvider.

        After this, self._client is an AcpSessionProvider that implements the
        same interface as AcpClient, so all downstream method calls work unchanged.

        ``phases`` is populated in-place with per-step wall-clock (ms) — spawn_init,
        session_new, set_model — for the startup histogram in the wrapper.
        """
        # Extract params from the AcpClient that was created in __init__
        # (it was never spawned — just used for config storage)
        work_dir = self._client._work_dir
        agent = getattr(self._client, "_agent", None) or ""
        sandbox_mode = getattr(self._client, "_sandbox_mode", "auto")
        extra_env = getattr(self._client, "_extra_env", None) or {}
        mcp_gateway_overlay = getattr(self._client, "_mcp_gateway_overlay", None)
        mcp_gateway_settings_mcp_json = getattr(
            self._client, "_mcp_gateway_settings_mcp_json", None
        )
        mcp_gateway_socket = getattr(self._client, "_mcp_gateway_socket", None)

        # Check for session resume
        resume_sid = getattr(self._client, "_resume_session_id", "")

        # Preserve the configured model so we can re-apply it once the session
        # is live. AcpClient sends session/set_model in its handshake; the runtime
        # path must do the same or a slot configured with a non-default kiro model
        # would silently run on the agent's default.
        configured_model = getattr(self._client, "_model", "") or ""

        runtime = AcpRuntime(
            work_dir=work_dir,
            agent=agent or "kirocrew",
            sandbox_mode=sandbox_mode,
            extra_env=extra_env,
            mcp_gateway_overlay=mcp_gateway_overlay,
            mcp_gateway_settings_mcp_json=mcp_gateway_settings_mcp_json,
            mcp_gateway_socket=mcp_gateway_socket,
        )
        _t_spawn = time.monotonic()
        try:
            await runtime.spawn()
        except AcpRuntimeError as exc:
            # kiro-cli can exit during initialize when not authenticated —
            # surface an actionable login prompt (parity with AcpClient) rather
            # than a generic runtime-death error.
            if runtime.saw_not_logged_in():
                raise AcpAuthRequired(_NOT_LOGGED_IN_MESSAGE) from exc
            raise
        finally:
            # subprocess launch + ACP `initialize` handshake
            phases["spawn_init"] = (time.monotonic() - _t_spawn) * 1000.0

        # Everything AFTER a successful spawn() must be guarded: until the
        # AcpSessionProvider is constructed and assigned to self._client, NOTHING
        # owns the spawned kiro-cli process, so any failure here would orphan it
        # until the gateway restarts. Kill the runtime on any failure path before
        # re-raising. BaseException (not Exception) so CancelledError/KeyboardInterrupt
        # also clean up. Once self._client = provider runs, the session lifecycle
        # owns the runtime and this guard has already returned.
        try:
            # Resume via session/load when a prior transcript exists, otherwise a
            # fresh session/new. Resume issues session/load DIRECTLY (no
            # session/new first) so it fully mirrors AcpClient and avoids the
            # double-context 'refusal' failure mode.
            # session/new is where kiro loads the full MCP toolset + system prompt
            # (the dominant cold-start cost) — time it as its own phase. The
            # resume attempt is timed separately as ``session_load``: the two are
            # different protocol calls with different costs, and folding them into
            # one phase hid the resume cost that dominates a rebuilt session.
            handle = None
            resumed = False
            if resume_sid:
                session_file = kiro_sessions_dir() / f"{resume_sid}.json"
                if session_file.exists():
                    # F2 load-recovery: retry past a stale "active in another
                    # process" lock (Phase 1); on persistent failure fall through
                    # to a fresh session/new below WITH KiroCrew history replay
                    # (Phase 2, self._history_replay_needed). This self-heals
                    # regardless of WHY the resume failed (SIGKILL'd holder,
                    # crash, OOM, drain timeout) — it never depends on the dead
                    # holder cooperating.
                    _t_load = time.monotonic()
                    try:
                        handle = await self._load_session_with_retry(
                            runtime,
                            str(session_file),
                            resume_sid,
                            work_dir,
                            agent,
                        )
                    finally:
                        phases["session_load"] = (time.monotonic() - _t_load) * 1000.0
                    resumed = handle is not None
                    meta["resume_outcome"] = "loaded" if resumed else "fallback_replay"
                    if handle is None:
                        self._history_replay_needed = True
                else:
                    logger.info("Session file missing for %s, skipping load", resume_sid)
                    meta["resume_outcome"] = "no_session_file"
            meta["resumed"] = resumed

            _t_sess = time.monotonic()
            _ran_session_new = handle is None

            if handle is None:
                # ── Fix B: respawn if runtime died during resume attempt ──
                # The orphan sweep may have killed the runtime's PID during the
                # session/load window (race condition). Calling create_session on
                # a dead runtime raises AcpRuntimeDead which bubbles to the user.
                # Instead, detect the dead runtime and transparently respawn.
                if not runtime.is_alive():
                    logger.warning(
                        "runtime died during resume; respawning for fresh start " "(PID was %s)",
                        runtime.pid,
                    )
                    try:
                        await runtime.kill()
                    except Exception:
                        pass
                    runtime = AcpRuntime(
                        work_dir=work_dir,
                        agent=agent or "kirocrew",
                        sandbox_mode=sandbox_mode,
                        extra_env=extra_env,
                        mcp_gateway_overlay=mcp_gateway_overlay,
                        mcp_gateway_settings_mcp_json=mcp_gateway_settings_mcp_json,
                        mcp_gateway_socket=mcp_gateway_socket,
                    )
                    try:
                        await runtime.spawn()
                    except AcpRuntimeError as exc:
                        if runtime.saw_not_logged_in():
                            raise AcpAuthRequired(_NOT_LOGGED_IN_MESSAGE) from exc
                        raise
                try:
                    handle = await runtime.create_session(
                        cwd=work_dir,
                        agent=agent or None,
                    )
                except AcpRuntimeError as exc:
                    if runtime.saw_not_logged_in():
                        raise AcpAuthRequired(_NOT_LOGGED_IN_MESSAGE) from exc
                    raise
            # session/new + MCP-toolset/system-prompt load done. Only recorded
            # when create_session actually ran: a successful resume skips that
            # block entirely, and reporting a near-zero session_new for it would
            # understate the phase's real distribution.
            if _ran_session_new:
                phases["session_new"] = (time.monotonic() - _t_sess) * 1000.0

            # Apply the configured model override (mirrors AcpClient handshake).
            # DEFAULT_MODEL ("auto") means "let kiro-cli pick per agent config".
            if configured_model and configured_model != DEFAULT_MODEL:
                # Withhold a model this account cannot run rather than sending
                # it. session/new has already reported what is on offer, and
                # this model was NOT picked for this turn — it comes from the
                # agent spec, the config default, or a slot value persisted
                # before entitlements were known. Sending it anyway is what put
                # a raw "-32603 ... model is not available" in the transcript on
                # every turn: kiro-cli ACCEPTS the id here (so the except below
                # never fires) and only the service rejects it, mid-prompt.
                # Leaving it unset keeps the session on the backend's own
                # default, so the turn succeeds.
                _advertised = advertised_model_ids(handle.available_models)
                if model_is_unusable(configured_model, _advertised):
                    logger.warning(
                        "Configured model %s is not available to this account; "
                        "leaving the session on the backend default (advertised: %s)",
                        configured_model,
                        ", ".join(_advertised),
                    )
                else:
                    _t_model = time.monotonic()
                    try:
                        await handle.set_model(configured_model)
                        logger.info("Kiro runtime model set: %s", configured_model)
                    except Exception:
                        logger.warning(
                            "Failed to set model %s on kiro runtime session",
                            configured_model,
                            exc_info=True,
                        )
                    finally:
                        phases["set_model"] = (time.monotonic() - _t_model) * 1000.0

            # Replace the placeholder AcpClient with the real AcpSessionProvider
            provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
            if resumed:
                provider.resumed = True
            self._client = provider  # type: ignore[assignment]
        except BaseException:
            # No provider owns the runtime yet — kill it so a failed session
            # setup doesn't leak an orphaned kiro-cli process. Best-effort:
            # the cleanup kill must not mask the original exception.
            try:
                await runtime.kill()
            except Exception:
                logger.debug(
                    "Cleanup kill of runtime after failed session setup failed",
                    exc_info=True,
                )
            raise
        logger.info(
            "Kiro provider started via AcpRuntime (PID %s, session %s, resumed=%s)",
            runtime.pid,
            handle.session_id,
            resumed,
        )

    def available_models(self) -> list[dict[str, str]]:
        """Models the backend advertised at session init (may be empty).

        Surfaced to the dashboard so the model dropdown reflects what the
        active backend actually offers (e.g. claude-agent-acp's versioned
        Claude list) rather than a hardcoded set.
        """
        return self._client.available_models()

    def get_valid_effort_levels(self) -> list[str]:
        """Effort levels the backend reported for the CURRENT model, in ACP order.

        Read live from the session's ``configOptions`` so the dashboard dropdown
        reflects exactly what this slot's active model supports (and updates on a
        model switch). Empty when the backend reported no effort selector.
        """
        return self._client.get_valid_effort_levels()

    # ── Reasoning-effort control ─────────────────────────────────────

    def supports_effort(self) -> bool:
        """True when the current model accepts a reasoning-effort level.

        Drives the dashboard effort dropdown: shown only when the active
        model is effort-capable (Opus/Sonnet), for both ACP backends.
        """
        return model_supports_effort(self._client._model)

    def _resolve_effort(self) -> str | None:
        """Resolve effort for the current model via the shared priority chain."""
        return resolve_effort_for_model(
            self._client._model,
            slot_overrides=self._effort_per_model,
            defaults=self._effort_defaults,
        )

    def _apply_effort_overlay(self) -> None:
        """Write the kiro workspace cli.json overlay for (current model, effort).

        No-op for the claude backend (it uses live set_config_option) and when
        the model is not effort-capable or no level resolves. Called before
        every (re)spawn so resume/restart keeps the same level.
        """
        if self.is_claude_backend:
            return
        model = self._client._model
        level = self._resolve_effort()
        if not model or not level:
            return
        try:
            _write_cli_overlay(self._client._work_dir, model, level)
            logger.debug("ACP effort overlay applied: model=%s effort=%s", model, level)
        except Exception:
            logger.warning("ACP effort overlay write failed", exc_info=True)

    def _apply_tool_search_overlay(self) -> None:
        """Write the kiro Tool Search setting into the workspace cli.json overlay.

        No-op for the claude backend (Tool Search is a kiro-cli feature) and
        when no toggle value was supplied (``self._tool_search is None``).
        Called before every (re)spawn so resume/restart keeps the same setting.
        """
        if self.is_claude_backend or self._tool_search is None:
            return
        try:
            _write_tool_search_overlay(self._client._work_dir, self._tool_search)
            logger.debug(
                "ACP MCP Tool Search overlay applied: enabled=%s (%s)",
                self._tool_search,
                self._client._work_dir / ".kiro" / "settings" / "cli.json",
            )
        except Exception:
            logger.warning("ACP tool-search overlay write failed", exc_info=True)

    async def _set_claude_effort(self, level: str) -> None:
        """Push an effort level to the claude backend, stepping down on reject.

        claude-agent-acp validates the value against the *current model's*
        ``supportedEffortLevels`` and throws ``Invalid value for config option
        effort: <level>`` (surfaced as an ``AcpError``) for anything the model
        does not advertise — e.g. ``"max"`` on a model whose ceiling is
        ``"xhigh"``. Rather than letting that bubble up (which makes the
        dashboard reset the whole session and silently drop to the adapter
        default), fall back down the effort ladder so the model still lands at
        the highest level it actually supports. Only the value-rejection error
        is retried; any other failure (transport, timeout) propagates.

        Older claude-agent-acp builds expose no ``effort`` config option at all
        and reject the push with ``Unknown config option: effort`` (a -32603
        Internal error). When the session advertises no effort selector, skip
        the push entirely — there is nothing to set, and attempting it would
        spam errors and trigger a session reset on every turn.
        """
        if not self._client.supports_config_option("effort"):
            logger.debug("claude-agent-acp exposes no 'effort' config option; skipping effort push")
            return
        # Descend from the requested level through lower levels (e.g.
        # max → xhigh → high). Never escalate above what was asked.
        try:
            start = EFFORT_LEVELS.index(level)
        except ValueError:
            await self._client.set_config_option("effort", level)
            return
        ladder = [lvl for lvl in reversed(EFFORT_LEVELS[: start + 1])]
        last_exc: Exception | None = None
        for candidate in ladder:
            try:
                await self._client.set_config_option("effort", candidate)
                if candidate != level:
                    logger.info(
                        "CC effort %r unsupported by model %s — applied %r instead",
                        level,
                        self._client._model,
                        candidate,
                    )
                return
            except AcpError as exc:
                msg = str(exc)
                if "unknown config option" in msg.lower():
                    # Adapter has no 'effort' option at all (older build) —
                    # nothing to set; skip silently rather than reset.
                    logger.debug("claude-agent-acp rejected 'effort' as unknown; skipping")
                    return
                if "config option effort" not in msg:
                    raise  # not a value-rejection — a real failure
                last_exc = exc
                continue
        # Every candidate rejected (unexpected) — surface the last error.
        if last_exc is not None:
            raise last_exc

    async def change_effort(self, level: str) -> bool:
        """Change effort live for the current model. Returns True on success.

        Persists the slot override and (kiro) rewrites the overlay so a later
        respawn keeps the level, then pushes the change to the running session:
        kiro via the ``/effort`` slash command, claude via
        ``session/set_config_option``. Returns False when the current model
        does not support effort.
        """
        model = self._client._model
        if not model_supports_effort(model):
            logger.info("change_effort skipped — model %s does not support effort", model)
            return False
        # Older claude-agent-acp builds advertise no 'effort' config option;
        # attempting to push would fail with 'Unknown config option' and reset
        # the session. Report unsupported so the dashboard leaves the UI as-is.
        if self.is_claude_backend and not self._client.supports_config_option("effort"):
            logger.info("change_effort skipped — claude-agent-acp build exposes no 'effort' option")
            return False
        # Accept any level the dynamic validation set knows about — ACP backends
        # can report levels beyond the canonical five (effort.py), and those are
        # already admitted by the dropdown, the API validator and persistence.
        # Gating here on the canonical-only is_valid_effort would raise for such
        # a level, which the caller swallows and turns into a full session reset.
        # The empty string ("use model default") is cleared via clear_effort(),
        # not change_effort(), so it remains invalid here.
        # circular import: chat_persistence → dashboard → session → providers.acp
        from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_values

        if not level or level not in get_reasoning_effort_values():
            raise ValueError(f"invalid effort level {level!r}")
        # Snapshot so a failed live push doesn't leave a poisoned override/
        # overlay that would re-push the rejected level on every respawn.
        _prev = self._effort_per_model.get(model)
        self._effort_per_model[model] = level
        self._apply_effort_overlay()
        try:
            if self.is_claude_backend:
                await self._set_claude_effort(level)
            else:
                await self._client.send_command("/effort", args={"level": level})
        except Exception:
            # Roll back to the prior state before propagating to the caller.
            if _prev is None:
                self._effort_per_model.pop(model, None)
                if not self.is_claude_backend:
                    _clear_cli_overlay_effort(self._client._work_dir, model)
            else:
                self._effort_per_model[model] = _prev
                self._apply_effort_overlay()
            logger.warning(
                "ACP effort live push failed (model=%s effort=%s) — rolled back", model, level
            )
            raise
        logger.info(
            "ACP effort live-changed: model=%s effort=%s backend=%s",
            model,
            level,
            "claude" if self.is_claude_backend else "kiro",
        )
        return True

    async def clear_effort(self) -> bool:
        """Clear the slot's effort override for the current model.

        Drops the per-model override (and the kiro overlay entry) so the model
        falls back to its own default.

        Returns True ONLY when a concrete default was applied LIVE to the
        running session (kiro with a resolvable workspace default). Returns
        False — signalling the caller to reset the session so a cold start
        re-resolves the true default — in the cases that cannot be reset live:
        - claude-agent-acp has no "reset to default" config value (there is no
          level to push; only a respawn drops the override), and
        - kiro with no workspace default (the model's built-in default is only
          re-applied on respawn once the overlay is cleared).
        Without this, the running session would silently keep its prior effort
        while the UI shows "default".
        """
        model = self._client._model
        if not model_supports_effort(model):
            return False
        self._effort_per_model.pop(model, None)
        if self.is_claude_backend:
            # No live "reset to default" — caller must reset the session.
            logger.info("ACP effort cleared (claude); session reset needed for default")
            return False
        # kiro: clear/rewrite the overlay so a respawn doesn't re-apply it.
        level = self._resolve_effort()  # workspace default, or None
        if level:
            self._apply_effort_overlay()
            await self._client.send_command("/effort", args={"level": level})
            logger.info("ACP effort cleared to workspace default %s (kiro)", level)
            return True
        # No default to push live — clear the overlay and let the caller reset
        # so kiro respawns at the model's built-in default.
        _clear_cli_overlay_effort(self._client._work_dir, model)
        logger.info("ACP effort cleared (kiro); session reset needed for built-in default")
        return False

    async def start(self) -> None:
        # Re-apply the overlay on every (re)start to cover resume / model swap.
        # (no-op for claude backend — that path applies effort live below.)
        self._apply_effort_overlay()
        self._apply_tool_search_overlay()

        if not self.is_claude_backend:
            # ── Kiro unified path: AcpRuntime + AcpSessionHandle ──
            # Spawn a runtime, create/resume a session, wrap in
            # AcpSessionProvider. One process hosts parent + all subagent
            # sessions (session sharing).
            await self._start_kiro_runtime()
        else:
            # ── CC path: legacy AcpClient (unchanged) ──
            await self._client.ensure_ready()

        await self._apply_initial_effort()

    async def _apply_initial_effort(self) -> None:
        """Apply the resolved effort to a fresh claude-agent-acp session.

        claude-agent-acp does NOT read ``CLAUDE_CODE_EFFORT_LEVEL`` from the
        environment — effort only takes hold via settings.json files or a live
        ``session/set_config_option``. So for the claude backend we push the
        resolved level once after the session is ready. Best-effort: a model
        that does not support effort, or an adapter that rejects the value,
        must not break session start. The kiro backend already gets effort
        from the cli.json overlay at spawn, so this is a no-op there.
        """
        if not self.is_claude_backend:
            return
        level = self._resolve_effort()
        if not level:
            return
        try:
            await self._set_claude_effort(level)
            logger.info("CC initial effort applied: model=%s effort=%s", self._client._model, level)
        except Exception:
            logger.warning(
                "CC initial effort apply failed (model=%s effort=%s)",
                self._client._model,
                level,
                exc_info=True,
            )

    async def shutdown(self) -> None:
        await self._client.shutdown()

    @staticmethod
    def _to_llm_event(e: Any) -> LLMEvent:
        return LLMEvent(
            kind=e.kind,
            text=e.text,
            tool_call_id=e.tool_call_id,
            title=e.title,
            tool_kind=e.tool_kind,
            tool_purpose=e.tool_purpose,
            context_usage_pct=e.context_usage_pct,
            stop_reason=e.stop_reason,
            request_id=e.request_id,
            options=e.options,
            tool_input=e.tool_input,
            tool_output=e.tool_output,
            tool_final=e.tool_final,
            usage=e.usage,
            raw_tool_params=e.raw_tool_params,
            server_name=e.server_name,
            oauth_url=e.oauth_url,
            subagents=e.subagents,
            sub_session_id=e.sub_session_id,
            is_shell=e.is_shell,
            # Canonical, non-model-authored tool identity (_meta.kiro). The
            # session-directive forgery gate in chat_runner keys on THESE, so
            # dropping them here silently discards every session-bound tool's
            # effect (the gate sees an empty server name and never records).
            tool_name=e.tool_name,
            mcp_server_name=e.mcp_server_name,
            diff_old_text=e.diff_old_text,
            diff_path=e.diff_path,
        )

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        async for e in self._client.stream_events(message):
            yield self._to_llm_event(e)

    async def stream_command(self, command: str) -> AsyncIterator[LLMEvent]:
        # claude-agent-acp does not implement the kiro-only
        # _kiro.dev/commands/execute method — route slash commands through
        # session/prompt, which the claude backend interprets natively for its
        # SDK-supported commands (/compact, /help, /model, /context, …).
        # Commands the SDK doesn't recognise (kiro-only ones like /agent,
        # /experiment, /hooks) flow through as conversational prompt text;
        # this is a softer failure mode than the previous -32601
        # "Method not found" hard error.
        if self.is_claude_backend:
            async for e in self._client.stream_events(command):
                yield self._to_llm_event(e)
            return
        async for e in self._client.stream_command(command):
            yield self._to_llm_event(e)

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        await self._client.approve_tool(request_id, always=always)

    async def reject_tool(self, request_id: str | int) -> None:
        await self._client.reject_tool(request_id)

    def context_usage_pct(self) -> float:
        return self._client.last_prompt_stats.context_pct

    def context_usage_unknown(self) -> bool:
        return self._client.last_prompt_stats.context_pct_unknown

    def context_window_tokens(self) -> int:
        return self._client.last_prompt_stats.context_window_tokens

    def context_used_tokens(self) -> int:
        return self._client.last_prompt_stats.context_used_tokens

    async def compact(self, context: str = "") -> None:
        """Trigger native /compact with optional context-preserving prompt."""
        if context:
            # Truncate to avoid overwhelming the compact prompt
            prompt = context[:4000] if len(context) > 4000 else context
            message = f"/compact Preserve this session context in the summary:\n{prompt}"
        else:
            message = "/compact"
        # BOTH backends send /compact through session/prompt.
        # - claude-agent-acp does not implement _kiro.dev/commands/execute;
        #   the claude backend handles the slash command natively in-prompt.
        # - kiro-cli DOES advertise commands/execute, but its STRING form
        #   (`"command": "/compact"`) makes kiro-cli 2.14.0 exit rc=0 without
        #   a response — live-probe confirmed for /compact AND /help, while
        #   the object form (`{"command": "help", "args": {}}`, used by
        #   /effort) works. The prompt transport is the path the dashboard's
        #   manual /compact and Slack's !compact have always used: kiro ACKs
        #   the prompt (end_turn) and then emits _kiro.dev/compaction/status
        #   started/completed, which wait_for_compaction() picks up.
        # Capture a terminal status emitted MID-TURN (before end_turn) while
        # draining — otherwise it would be consumed and lost, stranding a
        # subsequent wait_for_compaction() (task_executor, wecom, cli_chat)
        # until timeout even though the compact succeeded.
        self._compact_result = None
        async for event in self._client.stream_events(message):
            if event.kind == EVENT_COMPACTION_STATUS and event.text in (
                "completed",
                "failed",
            ):
                self._compact_result = {
                    "type": event.text,
                    "summary": event.title or "",
                }

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Cancel in-flight operation via ACP session/cancel."""
        if not self._client.has_active_turn():
            logger.debug("provider.cancel: no active turn, skip")
            return "no_turn"
        try:
            # Pass the ack budget so the client's read-grace window matches how
            # long we will actually wait below — otherwise a budget above the
            # 10s floor is silently capped and the turn is torn down early.
            await self._client.cancel_session(grace_secs=wait_ack_timeout)
        except AcpError:
            logger.debug("provider.cancel: cancel_session raised AcpError", exc_info=True)
            return "error"
        if wait_ack_timeout <= 0:
            # Fire-and-forget: cancel notification sent, caller does not
            # wait for the agent's ack. Return "acked" optimistically so
            # callers that don't care about confirmation stay happy. Any
            # caller that needs a real ack MUST pass a positive timeout.
            logger.debug("provider.cancel: wait_ack_timeout=0, returning acked")
            return "acked"
        try:
            reason = await self._client.wait_turn_done(timeout=wait_ack_timeout)
            logger.debug("provider.cancel: wait_turn_done returned reason=%r", reason)
            if reason in (STOP_REASON_CANCELLED, STOP_REASON_END_TURN):
                return "acked"
            logger.debug("Unexpected stop reason after cancel: %r", reason)
            return "timeout"
        except asyncio.TimeoutError:
            logger.debug("provider.cancel: wait_turn_done timed out after %.1fs", wait_ack_timeout)
            return "timeout"

    async def steer(self, message: str) -> bool:
        """Delegate a mid-turn steer to the inner client (kiro-cli
        ``_session/steer``). Fire-and-forget; returns False if not steerable."""
        return await self._client.steer(message)

    @property
    def supports_steer(self) -> bool:
        """True when the inner client supports mid-turn steer."""
        return bool(getattr(self._client, "supports_steer", False))

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict:
        """Wait for compaction completed/failed after stream ends.

        Consumes the result compact() captured mid-turn if there is one
        (kiro-cli may emit the terminal status before end_turn), otherwise
        delegates to the client's queue wait (async-after-end_turn case).
        On a cached ``completed``, still grace-drains the inner client for
        kiro's post-compaction metadata so the mid-turn path reports real
        numbers too — mirrors ``AcpSessionHandle.wait_for_compaction``.
        """
        cached = getattr(self, "_compact_result", None)
        if cached is not None:
            self._compact_result = None
            if cached.get("type") == "completed":
                drain = getattr(self._client, "_drain_post_compaction_metadata", None)
                if drain is not None:
                    await drain()
            return cached
        return await self._client.wait_for_compaction(timeout)

    async def new_conversation(self) -> None:
        """Clean-slate reset on the SAME warm process (no cold start).

        Delegates to the backing client — ``AcpSessionProvider.new_conversation``
        on the kiro path (``self._client`` is swapped to the session provider by
        ``start()``), which issues a fresh ``session/new`` on the already-running
        process, skipping subprocess spawn + the ``initialize`` handshake. This is
        the primitive a warm session pool uses to reuse one worker across
        independent tasks without leaking context between them. The dormant
        claude-backend seam would delegate to its own client-side reset the same
        way (the fork's ``AcpClient`` does not ship it — hence the type ignore).
        """
        await self._client.new_conversation()  # type: ignore[attr-defined]

    def is_alive(self) -> bool:
        return self._client.is_responsive()

    def is_process_alive(self) -> bool:
        """True if the underlying OS process has not exited (ignores I/O staleness)."""
        return self._client.is_process_alive()

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight (and not yet cancelled) on the client."""
        return bool(self._client) and self._client.has_active_turn()

    def has_unfinished_turn(self) -> bool:
        """True if the client reports a native turn that has NOT reached its
        done boundary — INDEPENDENT of cancel state (unlike
        :meth:`has_active_turn`).

        Used by the shutdown drain so an already-cancelled turn whose native ack
        has not yet arrived is still drained (waited on) before the process is
        killed — otherwise kiro-cli is killed with the native turn open and its
        session lock held (the empty-response-after-restart bug, #200).
        """
        return bool(self._client) and self._client.has_unfinished_turn()

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current native turn to reach its done boundary; returns
        the stop reason, or raises ``asyncio.TimeoutError``.

        Public wrapper over the inner client's ``wait_turn_done`` (which
        :meth:`cancel` already uses). The shutdown drain calls this on a turn
        that was already cancelled (so ``cancel`` returns ``"no_turn"``) but is
        still unfinished, to wait for the pending ack before teardown.
        """
        return await self._client.wait_turn_done(timeout=timeout)

    @property
    def exit_code(self) -> int | None:
        """Process exit code, or None if still running."""
        return self._client.exit_code

    def touch_activity(self) -> None:
        self._client.touch_activity()

    def runtime_info(self) -> tuple[int | None, str | None]:
        """Return (runtime_pid, gateway_socket_path) for abort propagation."""
        pid = getattr(self._client, "_pid", None)
        socket_path = getattr(self._client, "_mcp_gateway_socket", None)
        return (pid, socket_path)

    @property
    def session_id(self) -> str:
        """Return the kiro-cli session UUID."""
        return self._client._session_id if self._client and self._client._session_id else ""

    async def cleanup_session(self, session_id: str) -> None:
        """Delete kiro-cli session files (.json + .jsonl) at ~/.kiro/sessions/cli.

        (The claude seam's SDK transcript cleanup, ~/.claude/projects/, is
        re-added by the internal companion alongside its Claude backend.)
        """
        if not session_id:
            return
        sessions_dir = kiro_sessions_dir()
        for suffix in (".json", ".jsonl"):
            target = sessions_dir / f"{session_id}{suffix}"
            if not _is_safe_path(target, sessions_dir):
                logger.error("cleanup_session: path traversal blocked for %s", target)
                return
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.warning("cleanup_session: failed to delete %s", target, exc_info=True)


def is_claude_backend(provider: Any) -> bool:
    """Check if a provider is a Claude backend via the ACP adapter.

    Free function for use from modules that hold the provider as the
    ``LLMProvider`` ABC and can't reach the ``AcpProvider.is_claude_backend``
    property without an isinstance gate.
    """
    return isinstance(provider, AcpProvider) and provider.is_claude_backend
