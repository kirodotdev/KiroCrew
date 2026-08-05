"""Manual MCP server management — add custom servers and edit specs.

Provides ``POST /api/mcp/custom`` (add one or more user-authored servers,
from the Add Custom form or a pasted ``mcpServers`` JSON block) and
``PUT /api/mcp/custom/{name}`` (replace an installed server's spec).

This is the manual-install path for servers not in any registry.  It
REUSES the mcp.json write helpers from ``handlers/mcp.py`` (the file
lock + ``_set_kirocrew_entry`` / ``_replace_kirocrew_spec``) so there is
exactly one code path that mutates MCP config on disk.

Consent stance (mirrors discover-install): servers land
DISABLED unless the request carries ``enable: true`` — which the UI only
sends when the user ticks "Enable immediately", making the tick itself
the consent act.  Editing a spec never changes the enabled state.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers import mcp as _mcp
from kiro_crew.dashboard.handlers.mcp import (
    _find_server_spec_anywhere,
    _get_kirocrew_entry,
    _get_mcp_lock,
    _is_valid_mcp_name,
    _replace_kirocrew_spec,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Cap on servers per POST — a pasted README block is a handful of entries;
# anything larger is malformed input, not a use case.
_MAX_SERVERS_PER_ADD = 20

# The only keys a v1 custom spec may carry.  Tight allowlist: unknown keys
# are rejected by name rather than silently dropped or passed through to
# the process spawner.
_STDIO_KEYS = {"command", "args", "env"}
_REMOTE_KEYS = {"url"}
_ALLOWED_SPEC_KEYS = _STDIO_KEYS | _REMOTE_KEYS


def _validate_spec(spec: object, carried_keys: frozenset[str] = frozenset()) -> str | None:
    """Return an error message for an invalid spec, or None when valid.

    A valid spec is an object with EITHER the stdio shape
    (``command`` + optional ``args``/``env``) OR the remote shape
    (``url``, http/https only) — exactly one of command/url.

    ``carried_keys`` are extra keys tolerated for THIS spec because they
    already exist on the entry being edited (e.g. ``disabledTools``,
    ``autoApprove``, ``headers`` written by other flows).  Without it a
    GET→PUT round-trip of an unmodified spec would 400, and stripping
    the key to get past validation would silently widen the agent's
    tool surface.  Fresh adds pass no carried keys — the tight
    allowlist still rejects genuinely unknown keys by name.
    """
    if not isinstance(spec, dict):
        return "spec must be an object"
    unknown = sorted(set(spec) - _ALLOWED_SPEC_KEYS - {"disabled"} - carried_keys)
    if unknown:
        return f"unknown spec key '{unknown[0]}'"

    has_command = "command" in spec
    has_url = "url" in spec
    if has_command and has_url:
        return "spec cannot have both 'command' and 'url'"
    if not has_command and not has_url:
        return "spec needs 'command' (stdio) or 'url' (remote)"

    if has_url:
        url = spec["url"]
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return "'url' must be an http(s) URL"
        for key in _STDIO_KEYS & set(spec):
            return f"'{key}' is not valid on a remote (url) server"
        return None

    command = spec["command"]
    if not isinstance(command, str) or not command.strip():
        return "'command' must be a non-empty string"
    args = spec.get("args", [])
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return "'args' must be a list of strings"
    env = spec.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        return "'env' must be an object of string values"
    return None


def _clean_spec(spec: dict) -> dict:
    """Normalized copy of a validated spec (drops empty optionals)."""
    if "url" in spec:
        return {"url": spec["url"]}
    out: dict = {"command": spec["command"].strip()}
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    return out


def _load_kirocrew_config_strict() -> dict | None:
    """Load ``<data home>/mcp.json`` distinguishing MISSING from BROKEN.

    Returns ``{}`` when the file doesn't exist (a fresh add is fine), the
    parsed object when it's valid, and ``None`` when the file exists but
    is malformed or unreadable.  The batch-add path must refuse to write
    in the None case: the lenient ``_load_json_or_empty`` coerces a broken
    file to ``{}``, and the subsequent atomic write would then silently
    replace EVERY previously configured server with just the new batch
    while reporting success.
    """
    try:
        text = _mcp._kirocrew_mcp_json().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
        return None
    return data


async def _rebuild_agent_config() -> None:
    """Best-effort agent-config rebuild so changes load on the next session."""
    try:
        # circular import: kiro_crew.agent imports dashboard handlers.
        from kiro_crew.agent import rebuild_agent_config

        await asyncio.to_thread(rebuild_agent_config)
    except Exception:
        logger.warning("rebuild_agent_config failed after MCP custom write", exc_info=True)


async def api_mcp_custom_add(request: web.Request) -> web.Response:
    """POST /api/mcp/custom — add one or more user-authored MCP servers.

    Body: ``{"servers": {name: spec, ...}, "enable": bool=false}``.
    Validates EVERY entry before writing ANY (no partial adds), and
    refuses to clobber existing servers (409 listing the conflicts).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    servers = body.get("servers")
    if not isinstance(servers, dict) or not servers:
        return web.json_response({"error": "'servers' must be a non-empty object"}, status=400)
    if len(servers) > _MAX_SERVERS_PER_ADD:
        return web.json_response(
            {"error": f"too many servers (max {_MAX_SERVERS_PER_ADD})"}, status=400
        )
    enable = body.get("enable", False)
    if not isinstance(enable, bool):
        return web.json_response({"error": "'enable' must be a boolean"}, status=400)

    # Validate everything up front — one bad entry fails the whole batch
    # so a pasted block never half-applies.
    cleaned: dict[str, dict] = {}
    for name, spec in servers.items():
        if not isinstance(name, str) or not _is_valid_mcp_name(name):
            return web.json_response(
                {"error": f"invalid server name '{str(name)[:64]}'"}, status=400
            )
        err = _validate_spec(spec)
        if err:
            return web.json_response({"error": f"server '{name}': {err}"}, status=400)
        cleaned[name] = _clean_spec(spec)

    async with _get_mcp_lock():
        # Load once, strictly: a malformed existing file must fail the
        # request, never be coerced to {} and overwritten (that would
        # destroy every previously configured server "successfully").
        data = _load_kirocrew_config_strict()
        if data is None:
            sel().log_api_access(
                caller="dashboard",
                operation="mcp_custom_add",
                outcome="error",
                source="dashboard",
                resources="mcp.json malformed — write refused",
            )
            return web.json_response(
                {
                    "error": "existing MCP config file is malformed — fix or"
                    f" remove {_mcp._kirocrew_mcp_json()} and retry"
                },
                status=500,
            )
        conflicts = sorted(n for n in cleaned if _find_server_spec_anywhere(n) is not None)
        if conflicts:
            sel().log_api_access(
                caller="dashboard",
                operation="mcp_custom_add",
                outcome="denied",
                source="dashboard",
                resources=f"custom:{','.join(conflicts)} collision",
            )
            return web.json_response(
                {"error": "name already in use", "conflicts": conflicts}, status=409
            )
        entries = data.setdefault("mcpServers", {})
        for name, spec in cleaned.items():
            entry = dict(spec)
            if not enable:
                entry["disabled"] = True
            entries[name] = entry
        _mcp._atomic_write(_mcp._kirocrew_mcp_json(), data)

    await _rebuild_agent_config()

    added = sorted(cleaned)
    sel().log_api_access(
        caller="dashboard",
        operation="mcp_custom_add",
        outcome="ok",
        source="dashboard",
        resources=f"custom:{','.join(added)} enabled={enable}",
    )
    return web.json_response({"ok": True, "added": added, "enabled": enable})


async def api_mcp_custom_get(request: web.Request) -> web.Response:
    """GET /api/mcp/custom/{name} — the raw editable spec of one server.

    The servers list endpoint intentionally omits ``env``; the edit modal
    must prefill from the FULL spec or a save would silently drop the
    user's env vars.  Reads the same scope PUT writes (404 otherwise).
    """
    name = request.match_info.get("name", "")
    if not _is_valid_mcp_name(name):
        return web.json_response({"error": f"invalid server name '{name[:64]}'"}, status=400)

    async with _get_mcp_lock():
        entry = _get_kirocrew_entry(name)
    if entry is None:
        return web.json_response({"error": f"server '{name}' not found"}, status=404)

    enabled = not entry.get("disabled", False)
    spec = {k: v for k, v in entry.items() if k != "disabled"}
    return web.json_response({"name": name, "spec": spec, "enabled": enabled})


async def api_mcp_custom_update(request: web.Request) -> web.Response:
    """PUT /api/mcp/custom/{name} — replace an installed server's spec.

    404 when the name isn't a KiroCrew-managed entry.  The server's
    enabled/disabled state is preserved — editing is not consent to run.

    Keys outside the editable allowlist that already exist on the entry
    (``disabledTools``, ``autoApprove``, ``headers``, …) round-trip: the
    GET payload includes them, an unmodified save is accepted, and their
    on-disk values are preserved verbatim.  They cannot be edited or
    removed through this endpoint — dropping ``disabledTools`` on a save
    would silently widen the agent's tool surface.
    """
    name = request.match_info.get("name", "")
    if not _is_valid_mcp_name(name):
        return web.json_response({"error": f"invalid server name '{name[:64]}'"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    submitted = body.get("spec")

    async with _get_mcp_lock():
        existing = _get_kirocrew_entry(name)
        if existing is None:
            # Not in <data home>/mcp.json — either unknown, or managed by an
            # external scope this endpoint deliberately does not mutate.
            return web.json_response({"error": f"server '{name}' not found"}, status=404)

        carried = {
            k: v
            for k, v in existing.items()
            if k not in _ALLOWED_SPEC_KEYS and k != "disabled"
        }
        err = _validate_spec(submitted, frozenset(carried))
        if err:
            return web.json_response({"error": err}, status=400)
        assert isinstance(submitted, dict)  # narrowed by _validate_spec
        for key, on_disk in carried.items():
            if key in submitted and submitted[key] != on_disk:
                return web.json_response(
                    {
                        "error": f"'{key}' is managed by other flows and cannot be"
                        " edited here — revert it to save"
                    },
                    status=400,
                )
        spec = _clean_spec(submitted)
        spec.update(carried)  # preserved verbatim, never dropped
        replaced = _replace_kirocrew_spec(name, spec)
    if not replaced:
        return web.json_response({"error": f"server '{name}' not found"}, status=404)

    await _rebuild_agent_config()

    sel().log_api_access(
        caller="dashboard",
        operation="mcp_custom_update",
        outcome="ok",
        source="dashboard",
        resources=f"custom:{name}",
    )
    return web.json_response({"ok": True, "name": name})
