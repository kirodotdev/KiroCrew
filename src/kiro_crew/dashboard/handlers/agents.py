"""Agent configuration, themes, AIM integration, and agent CRUD handlers."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import agent_state, model_registry
from kiro_crew.agent_discovery import list_agents
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
    resolve_agent_config_path,
)
from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import (
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    _history_key_for,
    is_deprecated_model,
)
from kiro_crew.dashboard.handlers._shared import _capability_manager
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor, maintenance_executor

_MODEL_LIST_STDERR_TAIL_CHARS = 1000

logger = logging.getLogger(__name__)


def _err500(exc: BaseException) -> web.Response:
    """Return a generic 500 with a correlation id; log the detail server-side.

    Browser-facing 5xx bodies must not echo raw backend exception text
    (CWE-209). The short correlation id ties the sanitized client response to
    the full server-side log line (which retains the traceback).
    """
    corr = uuid.uuid4().hex[:12]
    logger.error("agents handler error [%s]", corr, exc_info=exc)
    return web.json_response({"error": "internal error", "id": corr}, status=500)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Custom Themes ──

_THEMES_DIR_NAME = "themes"
_THEME_NAME_MAX_LEN = 60
_THEME_SLUG_MAX_LEN = 40
_THEME_EMOJI_MAX_LEN = 4
_THEME_DEFAULT_EMOJI = "🎨"
_THEME_REQUIRED_VARS = ("--bg", "--text", "--accent")

# CSS variables that constitute a complete theme definition.
_THEME_CSS_VARS = (
    "--bg",
    "--bg-accent",
    "--bg-elevated",
    "--bg-hover",
    "--card",
    "--card-fg",
    "--card-hl",
    "--panel",
    "--panel-strong",
    "--chrome",
    "--text",
    "--text-strong",
    "--muted",
    "--muted-strong",
    "--border",
    "--border-strong",
    "--border-hover",
    "--accent",
    "--accent-hover",
    "--accent-subtle",
    "--accent-glow",
    "--ring",
    "--ok",
    "--ok-subtle",
    "--warn",
    "--warn-subtle",
    "--danger",
    "--danger-subtle",
    "--info",
    "--aim",
    "--aim-subtle",
    "--clarify",
    "--clarify-subtle",
    "--diff-add",
    "--diff-add-text",
    "--diff-del",
    "--diff-del-text",
    "--diff-hunk",
    "--diff-hunk-text",
    "--diff-meta-text",
    "--shadow-sm",
    "--shadow-md",
    "--shadow-lg",
)


def _themes_dir() -> Path:
    """Return the custom themes directory under config_dir()."""
    return config_dir() / _THEMES_DIR_NAME


# Positive allowlist: only characters that appear in legitimate CSS color,
# shadow, and length values.  This blocks semicolons, braces, backslashes,
# angle brackets, quotes, at-signs, colons, and everything else that could
# escape the CSS declaration context.
_CSS_VALUE_ALLOWED_RE = re.compile(r"^[a-zA-Z0-9#(),.\- %/]+$")

# Function denylist for dangerous CSS functions whose individual characters
# pass the allowlist above (e.g. url(), expression(), image(), image-set()).
_CSS_DANGEROUS_FUNC_RE = re.compile(
    r"url\s*\(|expression\s*\(|image\s*\(|image-set\s*\(",
    re.IGNORECASE,
)

# Set of allowed CSS variable names (mirrors frontend ALLOWED_CSS_VARS).
_THEME_CSS_VARS_SET: frozenset[str] = frozenset(_THEME_CSS_VARS)


def _sanitize_css_value(value: str) -> str | None:
    """Validate a single CSS value using a positive character allowlist.

    Returns the trimmed value if safe, or None if rejected.
    """
    if not isinstance(value, str):
        return None
    if len(value) > 200:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not _CSS_VALUE_ALLOWED_RE.match(trimmed):
        return None
    if _CSS_DANGEROUS_FUNC_RE.search(trimmed):
        return None
    return trimmed


def _validate_theme_data(data: dict) -> str | None:
    """Validate a theme JSON object. Returns error string or None.

    Validates keys against ``_THEME_CSS_VARS_SET`` allowlist.
    Unknown keys are rejected.
    """
    if not isinstance(data, dict):
        return "theme must be a JSON object"
    name = data.get("name", "")
    if not isinstance(name, str):
        return "name must be a string"
    name = name.strip()
    if not name:
        return "name is required"
    if len(name) > _THEME_NAME_MAX_LEN:
        return f"name too long (max {_THEME_NAME_MAX_LEN} chars)"
    emoji = data.get("emoji", "")
    if not isinstance(emoji, str):
        return "emoji must be a string"
    for mode in ("dark", "light"):
        mode_data = data.get(mode, {})
        if not isinstance(mode_data, dict):
            return f"'{mode}' must be a JSON object"
        for required_var in _THEME_REQUIRED_VARS:
            if required_var not in mode_data:
                return f"'{mode}' is missing required" f" variable '{required_var}'"
        for key, val in mode_data.items():
            if key not in _THEME_CSS_VARS_SET:
                return f"'{mode}' key '{key}' is not a recognized theme variable"
            if _sanitize_css_value(val) is None:
                return f"'{mode}' variable '{key}' has an invalid value"
    return None


def _strip_to_allowed_vars(mode_data: dict[str, str]) -> dict[str, str]:
    """Return only the allowed CSS vars with sanitized values.

    Defense-in-depth: even after validation, re-filter before writing
    so only known variables with clean values reach disk.
    """
    result: dict[str, str] = {}
    for key, val in mode_data.items():
        if key not in _THEME_CSS_VARS_SET:
            continue
        clean = _sanitize_css_value(val)
        if clean is not None:
            result[key] = clean
    return result


def _slugify_theme_name(name: str) -> str:
    """Convert a theme name to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9\-]", "-", name.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:_THEME_SLUG_MAX_LEN] or "custom"


async def api_themes(request: web.Request) -> web.Response:
    """GET /api/themes — list all custom themes, sorted by creation date."""
    themes_path = _themes_dir()
    result: list[dict[str, Any]] = []
    if themes_path.is_dir():
        for f in themes_path.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                result.append(
                    {
                        "slug": f.stem,
                        "name": data.get("name", f.stem),
                        "emoji": data.get("emoji", "🎨"),
                        "created_at": data.get("created_at", ""),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue
    # Sort by created_at (oldest first), falling back to name
    result.sort(key=lambda t: t.get("created_at") or "9999")
    return web.json_response({"themes": result})


async def api_themes_create(request: web.Request) -> web.Response:
    """POST /api/themes — create a new custom theme."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    err = _validate_theme_data(body)
    if err:
        return web.json_response({"error": err}, status=400)

    name = body["name"].strip()
    slug = _slugify_theme_name(name)
    emoji = (
        body.get("emoji", _THEME_DEFAULT_EMOJI).strip()[:_THEME_EMOJI_MAX_LEN]
        or _THEME_DEFAULT_EMOJI
    )

    themes_path = _themes_dir()
    themes_path.mkdir(parents=True, exist_ok=True)
    target = themes_path / f"{slug}.json"
    if target.exists():
        return web.json_response({"error": f"theme '{slug}' already exists"}, status=409)

    theme_data = {
        "name": name,
        "slug": slug,
        "emoji": emoji,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dark": _strip_to_allowed_vars(body.get("dark", {})),
        "light": _strip_to_allowed_vars(body.get("light", {})),
    }
    target.write_text(json.dumps(theme_data, indent=2) + "\n", encoding="utf-8")
    return web.json_response({"ok": True, "slug": slug, "theme": theme_data})


async def api_theme_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/themes/{slug} — get, update, or delete a custom theme."""
    slug = request.match_info["slug"]
    # Sanitize slug to prevent path traversal
    safe_slug = re.sub(r"[^a-z0-9\-]", "", slug)
    if not safe_slug or safe_slug != slug:
        return web.json_response({"error": "invalid theme slug"}, status=400)

    target = _themes_dir() / f"{safe_slug}.json"

    if request.method == "DELETE":
        if not target.exists():
            return web.json_response({"error": "not found"}, status=404)
        target.unlink()
        return web.json_response({"ok": True})

    if request.method == "PUT":
        if not target.exists():
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        err = _validate_theme_data(body)
        if err:
            return web.json_response({"error": err}, status=400)
        name = body["name"].strip()
        emoji = (
            body.get("emoji", _THEME_DEFAULT_EMOJI).strip()[:_THEME_EMOJI_MAX_LEN]
            or _THEME_DEFAULT_EMOJI
        )
        # Preserve created_at from existing file
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        theme_data = {
            "name": name,
            "slug": safe_slug,
            "emoji": emoji,
            "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
            "dark": _strip_to_allowed_vars(body.get("dark", {})),
            "light": _strip_to_allowed_vars(body.get("light", {})),
        }
        target.write_text(json.dumps(theme_data, indent=2) + "\n", encoding="utf-8")
        return web.json_response({"ok": True, "theme": theme_data})

    # GET
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return web.json_response({"error": "failed to read theme"}, status=500)
    return web.json_response(data)


# ── Agent Config ──


def _auto_install_agent() -> None:
    """Re-install agent config to kiro-cli so changes take effect immediately."""
    try:
        from kiro_crew.agent import install_agent  # noqa: F811

        install_agent()
        logger.info("Auto-applied agent config via dashboard")
    except Exception:
        logger.debug("Auto-apply agent config failed", exc_info=True)


def _find_agent_config() -> Path:
    """Find agents/defaults.json — delegates to centralized resolver."""
    return resolve_agent_config_path()


def _installed_agent_config() -> Path:
    """Return the installed agent config path (~/.kiro/agents/kirocrew.json).

    This is the live config that kiro-cli reads.  Dashboard MCP toggle
    and sync operations write here — NOT to agents/defaults.json.
    """
    from kiro_crew.agent import AGENT_FILENAME, KIRO_AGENTS_DIR  # noqa: F811

    return KIRO_AGENTS_DIR / AGENT_FILENAME


async def api_agent_config(request: web.Request) -> web.Response:
    """GET/PUT /api/agent/config — read or write the installed agent config.

    Reads/writes ``~/.kiro/agents/kirocrew.json`` — the live config that
    kiro-cli actually uses at runtime.  Falls back to ``agents/defaults.json``
    if the installed config doesn't exist yet.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    installed_path = _h._installed_agent_config()
    defaults_path = _h._find_agent_config()
    # Prefer installed config (what kiro-cli reads); fall back to defaults
    agent_config_path = installed_path if installed_path.is_file() else defaults_path

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        config = body.get("config")
        if not isinstance(config, dict):
            return web.json_response({"error": "config must be an object"}, status=400)
        try:
            # Track tools the user intentionally removed from shipped defaults
            # so they don't reappear on upgrade.  Stored in ~/.kirocrew/config.json
            # (NOT kirocrew.json — kiro-cli rejects unknown fields).
            # Per-key dict so removing from allowedTools only doesn't affect tools.
            from kiro_crew.agent import get_shipped_tools  # noqa: F811

            shipped = get_shipped_tools()
            removed_per_key: dict[str, list[str]] = {}
            for key in ("tools", "allowedTools"):
                diff = sorted(set(shipped.get(key, [])) - set(config.get(key, [])))
                if diff:
                    removed_per_key[key] = diff
            mc_cfg_path = _h.config_path()  # type: ignore[operator]
            try:
                mc_cfg = (
                    json.loads(mc_cfg_path.read_text(encoding="utf-8"))
                    if mc_cfg_path.exists()
                    else {}
                )
            except Exception:
                mc_cfg = {}
            if removed_per_key:
                mc_cfg["removedTools"] = removed_per_key
            else:
                mc_cfg.pop("removedTools", None)
            mc_cfg_path.write_text(json.dumps(mc_cfg, indent=2) + "\n", encoding="utf-8")
            installed_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            # Restart kiro-cli sessions so new config takes effect
            await _h._reset_all_sessions(request)
            return web.json_response({"ok": True, "applied": True})
        except Exception as exc:
            return _err500(exc)
    # GET
    try:
        data = json.loads(agent_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    return web.json_response(data)


async def api_default_agent(request: web.Request) -> web.Response:
    """GET/PUT /api/config/default-agent — read or set the default agent."""
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        name = body.get("agent", "")
        path = _h.config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        data.setdefault("agent", {})["default_agent"] = name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return web.json_response({"ok": True, "default_agent": name})
    cfg = KiroCrewConfig.load()
    return web.json_response({"default_agent": cfg.agent.default_agent})


# ── Config Schema ──


async def api_config_schema(request: web.Request) -> web.Response:
    """GET /api/config/schema — return config schema entries."""
    entries = SCHEMA_REGISTRY

    # Filter by tags (comma-separated, intersection)
    tags_param = request.query.get("tags", "").strip()
    if tags_param:
        requested_tags = {t.strip() for t in tags_param.split(",") if t.strip()}
        entries = [e for e in entries if set(e.tags) & requested_tags]

    # Filter out deprecated entries when deprecated=false
    dep_param = request.query.get("deprecated", "").strip().lower()
    if dep_param == "false":
        entries = [e for e in entries if not e.deprecated]

    # Serialize, masking sensitive defaultValues and converting dataclass
    # defaults to None (they aren't JSON-serializable).
    result = []
    for entry in entries:
        d = config_entry_to_dict(entry)
        if entry.sensitive or dataclasses.is_dataclass(d.get("defaultValue")):
            d["defaultValue"] = None
        result.append(d)

    return web.json_response({"entries": result})


_CAPABILITY_UNAVAILABLE = "capability manager not available"


async def api_capability_mcp_list(request: web.Request) -> web.Response:
    """GET /api/capability/mcp — list installed MCP servers (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_mcp())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_install(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/install — install an MCP server via the capability manager."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_mcp(server_id)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            _sync_mcp_to_agent(server_id, True)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/uninstall — uninstall an MCP server via the capability manager."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_mcp(server_id)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            _sync_mcp_to_agent(server_id, False, remove=True)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_list(request: web.Request) -> web.Response:
    """GET /api/capability/skills — list installed skill packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_skills())
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_install(request: web.Request) -> web.Response:
    """POST /api/capability/skills/install — install a skill package.

    Takes only ``package``; any version/source resolution is owned by the
    edition's capability manager (no Amazon-internal version-set field is
    exposed on the public API).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_skill(package)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        # Regenerate agent config to pick up new skill paths. install_agent()
        # does filesystem-heavy config rebuilding — offload it so it never
        # blocks the asyncio event loop (chat/heartbeat) under a slow FS.
        from kiro_crew.agent import install_agent  # noqa: F811

        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/skills/uninstall — uninstall a skill package."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_skill(package)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        from kiro_crew.agent import install_agent  # noqa: F811

        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_agents_installed(request: web.Request) -> web.Response:
    """GET /api/agents/installed — list all installed kiro-cli agents.

    kirocrew is always first; kirocrew-lite is excluded.
    """

    # list_agents() does glob + per-file resolve(strict=True) + read_bytes +
    # json.loads over ~/.kiro/agents — blocking filesystem work that, on a large
    # agents dir (network home, many project-registry agents), can stall the
    # event loop past the loop-stall watchdog when a browser loads the dashboard.
    # Offload to the discovery pool, same as /api/skills.
    def _collect() -> list[Any]:
        agents = list(list_agents())
        agents.sort(key=lambda a: (0 if a.name == "kirocrew" else 1, a.name))
        return agents

    agents = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _collect)
    return web.json_response([a.to_dict() for a in agents])


def _normalize_model_key(name: str) -> str:
    """Canonical key for de-duping CC model ids across spelling variants.

    The claude-agent-acp adapter advertises dashed ids (``claude-opus-4-7``)
    while curated/config entries may use dotted versions (``claude-opus-4.7``);
    case can also differ. Without normalization the same model surfaces twice in
    the dropdown (one curated row + one advertised row). Lowercase and fold
    ``.``→``-`` so equivalent ids collapse to one entry. ``default`` and ``auto``
    both mean "let the backend pick", so they map to a single key too.
    """
    key = (name or "").strip().lower().replace(".", "-")
    if key in ("default", "auto"):
        return "auto"
    return key


def _advertised_cc_models(request: web.Request) -> list[dict]:
    """Map the first active CC provider's advertised models to the API shape.

    claude-agent-acp captures its real versioned list at session init (see
    AcpClient._capture_available_models). Backend provider ids are mapped back to
    canonical registry keys (``from_provider_id``) so they dedup cleanly against
    the registry rows in :func:`_cc_models` and the wire value stays canonical.
    A provider id with no registry entry passes through unchanged (forward-compat
    for models the registry doesn't list yet). Returns ``[]`` when no session has
    initialized or the backend advertised nothing.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in providers:
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            advertised = getter()
        except Exception:
            continue
        if advertised:
            return [
                {
                    "model_name": model_registry.from_provider_id(
                        m.get("modelId", ""), "claude_code"
                    ),
                    "display_name": m.get("name", "") or m.get("modelId", ""),
                    "description": m.get("description", ""),
                }
                for m in advertised
                if m.get("modelId")
            ]
    return []


def _cc_models(request: web.Request, configured_default: str = "") -> list[dict]:
    """Assemble the CC model dropdown: canonical registry first, then adapter extras.

    Merge order (deduped by model_name, first wins):
      1. The canonical model registry (model_registry.display_list) — canonical
         versioned+capability keys (opus-4.8-1m, …) with registry display names,
         shown FIRST so users always see clean, current defaults. These are the
         wire values; the backend translates them to provider ids at the factory.
      2. The live backend's advertised models NOT already covered — appended,
         de-duped, so nothing the adapter offers is hidden (its set can be stale).
    The configured default is force-included so the active model is always
    selectable even if neither source lists it.
    """
    advertised = _advertised_cc_models(request)
    registry_rows = model_registry.display_list("claude_code")
    merged: list[dict] = []
    seen: set[str] = set()
    for entry in (*registry_rows, *advertised):
        name = entry.get("model_name", "")
        key = _normalize_model_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    # Guarantee the configured default is present (e.g. a custom cc_model the
    # backend doesn't advertise) so the selected model never vanishes. Resolve it
    # to its canonical key first (it may be stored as a provider id or alias) so a
    # default that already maps to a registry row does NOT produce a duplicate.
    if configured_default:
        canonical_default = model_registry.from_provider_id(
            model_registry.to_provider_id(configured_default, "claude_code"), "claude_code"
        )
        # Skip a blank canonical key: cc_model="auto" round-trips to "" (auto's
        # provider id is empty), and _normalize_model_key("")=="" is never in
        # `seen` (which holds "auto"), so without the `if key` guard — the same
        # one the merge loop above uses — a blank-named row would be inserted as
        # the first/selected dropdown option. The "auto" registry row already
        # covers this case.
        key = _normalize_model_key(canonical_default)
        if key and key not in seen:
            merged.insert(
                0,
                {
                    "model_name": canonical_default,
                    "display_name": canonical_default,
                    "description": "Configured default",
                },
            )
    # Enrich every row with a context_window via the central authority so the CC
    # dropdown carries the same field the kiro branch does (the frontend picker
    # + tooltip read it uniformly). None -> reference (never a silent 200k).
    for entry in merged:
        if "context_window" not in entry:
            name = entry.get("model_name", "")
            entry["context_window"] = (
                model_registry.model_window(name) or model_registry.REFERENCE_WINDOW_TOKENS
            )
    return merged


async def api_models(request: web.Request) -> web.Response:
    """GET /api/models — list available models from the live kiro-cli ACP session."""
    try:
        from kiro_crew.acp.client import _resolve_kiro_bin, _resolve_ssh_auth_sock  # noqa: F811
        from kiro_crew.env import augmented_path  # noqa: F811
        from kiro_crew.sandbox import (  # noqa: F811
            cgroup_scope_argv,
            resource_limit_preexec,
            wrap_argv,
        )

        kiro_bin = _resolve_kiro_bin()
        if not kiro_bin:
            # Degraded (binary not resolved yet), NOT a genuine "zero models"
            # result. Return 503 so the client retries instead of caching an
            # empty list — a cached [] renders an empty picker that only a
            # manual page refresh recovers from.
            return web.json_response({"error": "kiro binary not resolved"}, status=503)
        argv = [kiro_bin, "chat", "--list-models", "--format", "json", "--no-interactive"]
        # Mirror AcpClient._spawn() sandbox: wrap_argv + env + process isolation.
        # Note: AcpClient._spawn() is for interactive ACP sessions (stdin/stdout
        # pipes); this is a one-shot read-only command, so we replicate the
        # sandbox setup directly.  See the security-controls rule.
        argv, cleanup = wrap_argv(argv)
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
        try:
            env = {**os.environ}
            env["PATH"] = augmented_path(env.get("PATH", ""))
            _resolve_ssh_auth_sock(env)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
                preexec_fn=resource_limit_preexec(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()
                # A cold CLI spawn exceeded the timeout. This is the common
                # cause of the "picker is empty until I refresh" symptom: a
                # slow first `--list-models` spawn used to return [] (HTTP 200),
                # which the client cached as a successful empty result. Return
                # 503 instead so React Query retries with backoff and the
                # picker self-heals without a manual refresh.
                logger.warning("api_models: --list-models timed out; returning 503")
                return web.json_response({"error": "model list timed out"}, status=503)
        finally:
            if cleanup and callable(cleanup):
                cleanup()

        if proc.returncode != 0:
            from kiro_crew.platform import redact_via_context  # noqa: F811

            stderr_tail = stderr.decode(errors="replace").strip()
            stderr_tail = redact_via_context(stderr_tail)[-_MODEL_LIST_STDERR_TAIL_CHARS:]
            logger.warning(
                "api_models: --list-models exited %s: %s; returning 503",
                proc.returncode,
                stderr_tail or "<no stderr>",
            )
            return web.json_response({"error": "model list command failed"}, status=503)

        if not stdout.strip():
            logger.warning("api_models: --list-models returned empty output; returning 503")
            return web.json_response({"error": "model list returned empty output"}, status=503)

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "api_models: --list-models returned invalid JSON (%s); returning 503",
                exc,
            )
            return web.json_response({"error": "model list returned invalid JSON"}, status=503)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            logger.warning("api_models: --list-models returned an invalid payload; returning 503")
            return web.json_response(
                {"error": "model list returned an invalid payload"}, status=503
            )
        models = data["models"]
        # Seed the central window authority from kiro's authoritative structured
        # 'context_window_tokens' field (keyed by model_id/model_name). This is
        # the ONE place these rows enter the system; every other consumer (the
        # ACP backfill, the context-budget scaler, the live meter) then resolves
        # through model_registry.model_window() rather than re-reading kiro. The
        # in-memory update is synchronous (cheap dict mutation); only the disk
        # persist is offloaded to an executor so the event loop never blocks on
        # filesystem I/O (no blocking call on the event loop).
        #
        # NOTE: this fork keeps kiro's bare-dotted ids as the picker WIRE FORMAT
        # (guarded by _model_rejected_reason / api_chat_slot_model, which rejects
        # canonical registry keys the ACP CLI can't accept). Upstream instead
        # canonicalizes the dropdown to registry keys + translates back at the
        # factory; that is an INCOMPATIBLE alternative to this fork's guard, so
        # the model_name-canonicalization / dedup half of the upstream change is
        # deliberately NOT ported here. The window seeding above IS ported — it
        # is the load-bearing benefit (real GPT/DeepSeek/Qwen windows for the
        # backfill) and is independent of the wire-format choice.
        if model_registry.refresh_kiro_windows(models):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), model_registry.persist_kiro_windows
            )
        models = [m for m in models if not is_deprecated_model(m.get("model_name", ""))]
        return web.json_response(models)
    except Exception:
        # Spawn failure, JSON parse error, etc. — degraded, not "zero models".
        # 503 so the client retries instead of caching an empty picker.
        logger.warning("api_models failed; returning 503 for client retry", exc_info=True)
        return web.json_response({"error": "model list unavailable"}, status=503)


async def api_effort_levels(request: web.Request) -> web.Response:
    """GET /api/effort-levels — list available reasoning effort levels.

    Per-slot: when a ``?slot=`` query param resolves to a live ACP provider,
    return the levels that slot's CURRENT model reported (ACP escalation order),
    so concurrent slots on different models/backends each see their own set and
    a model switch is reflected immediately. Falls back to the process-global
    ordered list (cold start / no live provider / provider without the getter).
    """
    slot = request.query.get("slot")
    if slot:
        try:
            state: DashboardState = request.app["state"]
            provider = state.sessions.get_provider(_history_key_for(slot))
            getter = getattr(provider, "get_valid_effort_levels", None) if provider else None
            if callable(getter):
                levels = getter()
                if levels:
                    return web.json_response(levels)
        except (KeyError, AttributeError):
            pass
    return web.json_response(get_reasoning_effort_ordered())


async def api_slash_commands(request: web.Request) -> web.Response:
    """GET /api/slash-commands — list available slash commands (provider-aware)."""
    cfg = KiroCrewConfig.load()
    if cfg.agent.provider == "claude_code":
        state: DashboardState = request.app["state"]
        cc_commands: list[str] = []
        for provider in state.sessions.active_providers():
            cmds = getattr(provider, "_slash_commands", [])
            if cmds:
                cc_commands = cmds
                break
        if not cc_commands:
            cc_commands = [
                "compact",
                "clear",
                "context",
                "help",
                "init",
                "review",
                "security-review",
                "usage",
            ]
        result = [
            {"name": f"/{c}", "description": SLASH_COMMAND_DESCRIPTIONS.get(f"/{c}", "")}
            for c in cc_commands
        ]
        result.append({"name": "/side", "description": SLASH_COMMAND_DESCRIPTIONS.get("/side", "")})
        return web.json_response(result)

    return web.json_response(
        [
            {"name": c, "description": SLASH_COMMAND_DESCRIPTIONS.get(c, "")}
            for c in sorted(_SLASH_COMMANDS)
        ]
    )


async def api_agent_detail(request: web.Request) -> web.Response:
    """GET/DELETE/PATCH /api/agents/detail/{name} — view, delete, or update agent config."""
    name = request.match_info["name"]
    from kiro_crew.agent import KIRO_AGENTS_DIR  # noqa: F811

    # Parse body early so JSONDecodeError returns 400, not 404 from the file loop.
    patch_body = None
    if request.method == "PATCH":
        try:
            patch_body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)

    for f in KIRO_AGENTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("name") == name or f.stem == name:
                if request.method == "DELETE":
                    if f.name in (
                        "kirocrew.json",
                        "kirocrew-lite.json",
                    ):
                        return web.json_response({"error": "cannot delete kirocrew"}, status=400)
                    f.unlink()
                    agent_state.prune(data.get("name") or name)
                    state: DashboardState = request.app["state"]
                    state.push_refresh("agents")
                    return web.json_response({"ok": True})
                if request.method == "PATCH" and patch_body is not None:
                    async with _get_config_lock():
                        data = json.loads(f.read_text(encoding="utf-8"))
                        agent_name = data.get("name") or name
                        if "model" in patch_body:
                            # Stored verbatim (canonical key); translated to a
                            # provider id at the config.loader factory boundary.
                            data["model"] = patch_body["model"] or None
                            if data["model"] is None:
                                data.pop("model", None)
                                # Cleared/auto: resume tracking the shipped
                                # default (re-synced by _refresh_dynamic_fields).
                                agent_state.set_model_managed(agent_name, True)
                            else:
                                # Explicit pick: freeze it against default bumps.
                                agent_state.set_model_managed(agent_name, False)
                        # Never persist KiroCrew bookkeeping into the kiro spec —
                        # kiro-cli rejects unknown fields and drops the agent.
                        data.pop("model_managed", None)
                        data.pop("cc_model", None)
                        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    state = request.app["state"]
                    state.push_refresh("agents")
                    return web.json_response({"ok": True, "model": data.get("model", "")})
                return web.json_response(data)
        except (json.JSONDecodeError, OSError):
            continue
    # "default" is the built-in agent with no config file
    if name == "default":
        if request.method != "GET":
            return web.json_response({"error": "cannot modify built-in default agent"}, status=400)
        return web.json_response({"name": "default", "model": ""})

    return web.json_response({"error": "not found"}, status=404)


async def api_capability_agents_list(request: web.Request) -> web.Response:
    """GET /api/capability/agents — list installed agent packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_agents())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_registry(request: web.Request) -> web.Response:
    """GET /api/capability/mcp/registry — browse available MCP servers from the registry.

    The capability manager owns registry-output parsing and returns entries
    directly (conventional keys: id, installed, title, tier, description); the
    core passes them through verbatim.
    """
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response({"servers": await mgr.registry()})
    except Exception as exc:
        return _err500(exc)


# ── KiroCrew Agent CRUD API ──


async def api_kirocrew_agents(request: web.Request) -> web.Response:
    """GET /api/agents — list all KiroCrew agent definitions, most-used first."""
    cfg = KiroCrewConfig.load()
    agents = [
        {"name": name, **dataclasses.asdict(agent_cfg)} for name, agent_cfg in cfg.agents.items()
    ]

    # Reorder by usage frequency (most-used first). Derived read-only from chat
    # history; degrade to config-insertion order on any failure so the dropdown
    # never breaks or drops agents when history is unreadable.
    state: DashboardState | None = request.app.get("state")
    conversation_log = state.conversation_log if state else None
    if conversation_log:
        try:
            usage = await asyncio.to_thread(conversation_log.agent_usage)
            # Default missing agents to (0, 0) — keeps the sort key total and
            # deterministic (never negates None); never-used agents collapse to
            # their config-insertion index and form a stable bottom block.
            sorted_agents = sorted(
                enumerate(agents),
                key=lambda item: (
                    -usage.get(item[1]["name"], (0, 0.0))[0],
                    -usage.get(item[1]["name"], (0, 0.0))[1],
                    item[0],
                ),
            )
            agents = [a for _, a in sorted_agents]
        except Exception:
            logger.warning("Failed to sort agents by usage; using config order", exc_info=True)

    return web.json_response(
        {
            "agents": agents,
            "default_agent": cfg.default_agent,
        }
    )


_config_lock: asyncio.Lock | None = None
_config_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_config_lock() -> asyncio.Lock:
    """Return a config lock bound to the current event loop (Python 3.10 compat)."""
    global _config_lock, _config_lock_loop
    loop = asyncio.get_running_loop()
    if _config_lock is None or _config_lock_loop is not loop:
        _config_lock = asyncio.Lock()
        _config_lock_loop = loop
    return _config_lock


async def api_kirocrew_agents_sync(request: web.Request) -> web.Response:
    """POST /api/agents/sync — auto-sync AIM-installed agents into config.json."""
    async with _get_config_lock():
        return await _do_agents_sync(request)


async def _do_agents_sync(request: web.Request) -> web.Response:

    cfg = KiroCrewConfig.load()
    synced: list[str] = []
    pruned: list[str] = []
    try:
        discovered_agents = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), lambda: list(list_agents())
        )
        discovered_names = {a.name for a in discovered_agents}

        # Add new agents
        from kiro_crew.agent import KIRO_AGENTS_DIR  # noqa: F811

        mc_kiro_agents = {a.kiro_agent for a in cfg.agents.values()}
        for disc in discovered_agents:
            if (
                disc.name not in mc_kiro_agents
                and disc.name not in cfg.agents
                and disc.source != "kirocrew"
            ):
                # EXECUTABLE INVARIANT enforcement (mirrors the seam-boundary
                # LIVENESS bound in platform.capability_bound —
                # BoundedCapabilityManager): a builtin_agents() row MUST be
                # spawnable. The core can only verify the on-disk case
                # (~/.kiro/agents/<name>.json); an edition may also make a
                # row ACP-resolvable WITHOUT an on-disk file, so we WARN rather
                # than hard-drop — dropping a legitimately ACP-resolvable agent
                # would itself be a correctness bug. The warning turns an
                # otherwise silent spawn-time (ACP session/set_mode) failure into
                # an actionable log line pointing at the offending seam row.
                if not (KIRO_AGENTS_DIR / f"{disc.name}.json").exists():
                    logger.warning(
                        "syncing agent %r (source=%s) with no on-disk config at "
                        "%s — if it is not ACP-resolvable it will persist into "
                        "config.json and fail at spawn (builtin_agents EXECUTABLE "
                        "INVARIANT)",
                        disc.name,
                        disc.source,
                        KIRO_AGENTS_DIR / f"{disc.name}.json",
                    )
                cfg.agents[disc.name] = KiroCrewAgentConfig(
                    kiro_agent=disc.name,
                    description=disc.description,
                    source=disc.source,
                )
                synced.append(disc.name)

        # Prune agents whose kiro_agent file no longer exists on disk.
        # Only prune package-installed agents (never user-created or kirocrew-owned).
        # Skip pruning if scan returned nothing -- likely a transient issue.
        # Invariant: for package-sourced entries, kiro_agent == dict key == agent name.
        # ("aim" retained for backward-compat with configs written before the rename.)
        if discovered_names:
            for name, agent_cfg in list(cfg.agents.items()):
                if agent_cfg.source in ("package", "aim") and (
                    agent_cfg.kiro_agent not in discovered_names
                ):
                    del cfg.agents[name]
                    pruned.append(name)
    except Exception:
        logger.warning("Failed to scan installed agents", exc_info=True)
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="failure",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync failure", exc_info=True)
        return web.json_response({"ok": False, "error": "sync failed", "synced": []}, status=500)

    if synced or pruned:
        try:
            cfg.save()
        except Exception:
            logger.warning("Failed to save config after agent sync", exc_info=True)
            try:
                _sel().log_api_access(
                    caller=request.get("user", "dashboard"),
                    operation="agent.auto_sync",
                    outcome="failure",
                    source="agent_sync",
                )
            except Exception:
                logger.warning("SEL logging failed for config save failure", exc_info=True)
            return web.json_response(
                {"ok": False, "error": "config save failed", "synced": []}, status=500
            )
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="success",
                source="agent_sync",
                resources=", ".join(synced + [f"-{p}" for p in pruned]),
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync success", exc_info=True)
    else:
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="noop",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync noop", exc_info=True)

    if pruned:
        logger.info("Pruned %d stale AIM agents: %s", len(pruned), ", ".join(pruned))

    return web.json_response({"ok": True, "synced": synced, "pruned": pruned})


async def api_kirocrew_agents_create(request: web.Request) -> web.Response:
    """POST /api/agents — create a new KiroCrew agent."""

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "Agent name is required"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)
        cfg.agents[name] = KiroCrewAgentConfig(
            kiro_agent=body.get("kiro_agent", "kirocrew"),
            workspace=body.get("workspace", "default"),
            memory_store=body.get("memory_store", "default"),
            description=body.get("description", ""),
            source=body.get("source", "kirocrew"),
        )
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_kirocrew_agent_update(request: web.Request) -> web.Response:
    """PUT /api/agents/{name} — update a KiroCrew agent."""

    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        agent = cfg.agents[name]
        changed: list[str] = []
        if "kiro_agent" in body:
            agent.kiro_agent = body["kiro_agent"]
            changed.append("kiro_agent")
        if "workspace" in body:
            agent.workspace = body["workspace"]
            changed.append("workspace")
        if "memory_store" in body:
            agent.memory_store = body["memory_store"]
            changed.append("memory_store")
        if "description" in body:
            agent.description = body["description"]
            changed.append("description")
        if "source" in body:
            agent.source = body["source"]
            changed.append("source")
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.update",
        outcome="success",
        source="dashboard",
        resources=f"{name} ({','.join(changed)})",
    )
    return web.json_response({"ok": True, "name": name})


async def api_kirocrew_agent_delete(request: web.Request) -> web.Response:
    """DELETE /api/agents/{name} — delete a KiroCrew agent."""

    name = request.match_info["name"]
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if name == cfg.default_agent:
            return web.json_response(
                {"error": f"Cannot delete default agent '{name}'. Change default_agent first."},
                status=409,
            )
        del cfg.agents[name]
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})


# ── Agent metadata (Phase 1: Agents as Skills) ──────────────────────


def _regen_conductor() -> None:
    """Regenerate conductor skill after metadata or agent roster changes."""
    try:
        cfg = KiroCrewConfig.load()
        if not cfg.agent.conductor_skill:
            return
        from kiro_crew.conductor_skill import generate_conductor_skill  # noqa: F811
        from kiro_crew.skills import SkillsLoader  # noqa: F811

        generate_conductor_skill(SkillsLoader())
    except Exception:
        logger.exception("Failed to regenerate conductor skill")


async def api_agent_metadata_get(request: web.Request) -> web.Response:
    """GET /api/agent-metadata/{name} — read agent routing metadata."""
    name = request.match_info["name"]
    from kiro_crew.agent_metadata import load  # noqa: F811

    content = load(name)
    return web.json_response({"name": name, "content": content})


async def api_agent_metadata_put(request: web.Request) -> web.Response:
    """PUT /api/agent-metadata/{name} — write agent routing metadata."""
    caller = request.get("user", "")
    if not caller:
        try:
            _sel().log_api_access(
                caller="anonymous",
                operation="agent_metadata.put",
                outcome="denied",
                source="dashboard",
                resources="unauthenticated",
            )
        except Exception:
            logger.warning("SEL logging failed", exc_info=True)
        return web.json_response({"error": "authentication required"}, status=401)
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = body.get("content", "").strip()
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    from kiro_crew.agent_metadata import save  # noqa: F811

    save(name, content)
    _regen_conductor()
    try:
        _sel().log_api_access(
            caller=caller, operation="agent_metadata.put", outcome="ok", resources=name
        )
    except Exception:
        logger.warning("SEL logging failed", exc_info=True)
    return web.json_response({"ok": True, "name": name})


async def api_agent_metadata_delete(request: web.Request) -> web.Response:
    """DELETE /api/agent-metadata/{name} — delete agent routing metadata."""
    caller = request.get("user", "")
    if not caller:
        try:
            _sel().log_api_access(
                caller="anonymous",
                operation="agent_metadata.delete",
                outcome="denied",
                source="dashboard",
                resources="unauthenticated",
            )
        except Exception:
            logger.warning("SEL logging failed", exc_info=True)
        return web.json_response({"error": "authentication required"}, status=401)
    name = request.match_info["name"]
    from kiro_crew.agent_metadata import delete  # noqa: F811

    delete(name)
    _regen_conductor()
    try:
        _sel().log_api_access(
            caller=caller, operation="agent_metadata.delete", outcome="ok", resources=name
        )
    except Exception:
        logger.warning("SEL logging failed", exc_info=True)
    return web.json_response({"ok": True, "name": name})
