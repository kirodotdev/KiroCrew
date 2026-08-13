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
from kiro_crew.mcp_discovery import MCP_REDACTED_HEADER_VALUE, redact_mcp_headers
from kiro_crew.mcp_provenance import MARKER_KEY
from kiro_crew.mcp_utils import (
    INTERNAL_CLIENT_ID_KEY,
    INTERNAL_SCOPES_KEY,
    KIRO_OAUTH_KEY,
    KIRO_SCOPES_KEY,
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
# ``scopes`` and ``clientId`` are OAuth hints carried to the runtime verbatim.
# Kiro Crew validates only their SHAPE and never interprets them: the runtime
# owns the authorization exchange, so scope narrowing and client registration
# are its decisions, not ours.  A clientId is a public OAuth identifier — the
# corresponding secret never lives in an MCP spec — so neither key is redacted.
# ``headers`` carries per-request HTTP headers (typically credentials) to the
# runtime verbatim.  Unlike the OAuth hints its VALUES are secrets: reads
# redact them (``redact_mcp_headers``), so a fresh spec may author headers but
# an existing entry's stored values are preserve-only on edit — they can only
# round-trip, never be rewritten through this surface.
_REMOTE_ONLY_KEYS = {"scopes", "clientId", "headers"}
_REMOTE_KEYS = {"url"} | _REMOTE_ONLY_KEYS
_ALLOWED_SPEC_KEYS = _STDIO_KEYS | _REMOTE_KEYS


def _validate_spec(spec: object, carried_keys: frozenset[str] = frozenset()) -> str | None:
    """Return an error message for an invalid spec, or None when valid.

    A valid spec is an object with EITHER the stdio shape
    (``command`` + optional ``args``/``env``) OR the remote shape
    (``url``, http/https only) — exactly one of command/url.

    ``carried_keys`` are extra keys tolerated for THIS spec because they
    already exist on the entry being edited (e.g. ``disabledTools``,
    ``autoApprove`` written by other flows, or stored ``headers`` whose
    values are preserve-only).  Without it a GET→PUT round-trip of an
    unmodified spec would 400, and stripping the key to get past
    validation would silently widen the agent's tool surface.  Fresh
    adds pass no carried keys — the tight allowlist still rejects
    genuinely unknown keys by name.
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
        # One shape rule per hint, applied to BOTH spellings. The wire spellings
        # are tolerated by name so an unmodified round trip still saves, but
        # tolerating a key is not the same as accepting any value in it -- without
        # this, the same field is accepted or rejected purely by which spelling
        # the caller used, and an invalid value reaches disk under a 200.
        for scopes_key in ("scopes", KIRO_SCOPES_KEY):
            if scopes_key in spec:
                scopes = spec[scopes_key]
                if not isinstance(scopes, list) or any(
                    not isinstance(scope, str) or not scope.strip() for scope in scopes
                ):
                    return f"'{scopes_key}' must be a list of non-empty strings"
        if "clientId" in spec:
            client_id = spec["clientId"]
            if not isinstance(client_id, str) or not client_id.strip():
                return "'clientId' must be a non-empty string"
        if KIRO_OAUTH_KEY in spec:
            oauth = spec[KIRO_OAUTH_KEY]
            if not isinstance(oauth, dict):
                return f"'{KIRO_OAUTH_KEY}' must be an object"
            # Only the hints are ours to shape-check; unrelated sub-keys
            # (``issuer``, ...) belong to the runtime and pass through.
            if INTERNAL_CLIENT_ID_KEY in oauth:
                nested_id = oauth[INTERNAL_CLIENT_ID_KEY]
                if not isinstance(nested_id, str) or not nested_id.strip():
                    return f"'{KIRO_OAUTH_KEY}.clientId' must be a non-empty string"
            if KIRO_SCOPES_KEY in oauth:
                nested = oauth[KIRO_SCOPES_KEY]
                if not isinstance(nested, list) or any(
                    not isinstance(scope, str) or not scope.strip() for scope in nested
                ):
                    return f"'{KIRO_OAUTH_KEY}.{KIRO_SCOPES_KEY}' must be a list of non-empty strings"
        if "headers" in spec:
            headers = spec["headers"]
            if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not k.strip() or not isinstance(v, str)
                for k, v in headers.items()
            ):
                return "'headers' must be an object of string values"
            # Names and values travel into raw HTTP requests; a CR/LF or other
            # control character is a header-injection vector, never a legitimate
            # credential byte.
            if any(
                any(ord(ch) < 0x20 or ch == "\x7f" for ch in name + value)
                for name, value in headers.items()
            ):
                return "'headers' must not contain control characters"
            # A pasted spec copied from a read response carries the redaction
            # marker where the real values were.  Writing the marker verbatim
            # would store a nonsense credential that fails auth with no hint
            # why; stored headers round-trip via carried_keys instead.
            if "headers" not in carried_keys and MCP_REDACTED_HEADER_VALUE in headers.values():
                return "'headers' values are redacted in copied specs — enter the real value"
        return None

    command = spec["command"]
    if not isinstance(command, str) or not command.strip():
        return "'command' must be a non-empty string"
    for key in sorted(_REMOTE_ONLY_KEYS & set(spec)):
        return f"'{key}' is not valid on a stdio (command) server"
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
    out: dict = {}
    if "url" in spec:
        out["url"] = spec["url"]
        if spec.get("headers"):
            out["headers"] = dict(spec["headers"])
        if spec.get("scopes"):
            out["scopes"] = list(spec["scopes"])
        if spec.get("clientId"):
            out["clientId"] = spec["clientId"]
        return out
    out["command"] = spec["command"].strip()
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    return out


def _resolve_oauth_hints(spec: dict, submitted: dict, existing: dict) -> str | None:
    """Let the submitted spec decide the OAuth hints, wire sibling included.

    Returns an error message when the submission tries to change an ``oauth``
    sub-key that is not one of the hints, or None on success.

    The store speaks ONE spelling (``scopes`` / ``clientId``); the wire spelling
    exists only in the emitted agent spec. But the scope-toggle preservation rule
    copies a global server's spec into the store verbatim, so an entry can already
    hold the wire form -- and those keys fall outside the editable set, so plain
    carried-key restoration puts them back untouched. With no internal key on disk
    the reader correctly falls through to that sibling, and the hint the user just
    removed keeps being requested while the API answers 200.

    So a wire hint is NOT foreign configuration to be carried: it is this entry's
    own hint in the other spelling. When the submitted spec states neither
    spelling of a hint, the field is cleared and the sibling goes with it; when it
    states either, that is authoritative. Sub-keys of ``oauth`` we never owned
    (``issuer``) always survive, matching the emit boundary's surgical edit.

    Tolerating the wire keys on input keeps an unmodified GET -> PUT round trip
    working; it does not make them a second editable vocabulary, because nothing
    here interprets them beyond "this hint is still stated".
    """
    if "url" not in spec:
        return None  # stdio entries carry no OAuth hints
    submitted_oauth = submitted.get(KIRO_OAUTH_KEY)
    prior = existing.get(KIRO_OAUTH_KEY)

    # Sub-keys that are neither hint belong to other flows, so they follow the
    # carried-key rule: matching what is already on disk is fine, anything else is
    # refused. A key with no prior value cannot be stored either -- accepting it
    # and writing nothing would turn an explicit refusal into a silent success.
    _prior_others = {
        k: v
        for k, v in (prior if isinstance(prior, dict) else {}).items()
        if k not in (INTERNAL_CLIENT_ID_KEY, KIRO_SCOPES_KEY)
    }
    if isinstance(submitted_oauth, dict):
        for k, value in submitted_oauth.items():
            if k in (INTERNAL_CLIENT_ID_KEY, KIRO_SCOPES_KEY):
                continue
            if k not in _prior_others or _prior_others[k] != value:
                return (
                    f"'{KIRO_OAUTH_KEY}.{k}' is managed by other flows and cannot be"
                    " edited here — revert it to save"
                )

    # The scopes hint has TWO wire spellings -- top-level and nested under
    # ``oauth`` -- and the reader falls through to either. Both therefore obey one
    # statement test, or a clear would drop the spelling it happened to check and
    # leave the other standing for the reader to resurrect.
    scopes_stated = (
        INTERNAL_SCOPES_KEY in submitted
        or KIRO_SCOPES_KEY in submitted
        or (isinstance(submitted_oauth, dict) and KIRO_SCOPES_KEY in submitted_oauth)
    )
    if not scopes_stated:
        spec.pop(KIRO_SCOPES_KEY, None)
    elif KIRO_SCOPES_KEY in submitted and INTERNAL_SCOPES_KEY not in submitted:
        spec[KIRO_SCOPES_KEY] = submitted[KIRO_SCOPES_KEY]

    # Rebuilt rather than carried: only sub-keys that are neither hint survive
    # untouched, and each hint is re-added solely when the submission states it in
    # a wire spelling AND leaves the internal one unstated. The internal spelling
    # is the one the store and the editor speak, so stating it settles the hint
    # outright -- copying a wire sibling back would leave the reader a fallback
    # that outlives the value it was told to use.
    siblings = dict(_prior_others)
    if isinstance(submitted_oauth, dict):
        if INTERNAL_CLIENT_ID_KEY in submitted_oauth and INTERNAL_CLIENT_ID_KEY not in submitted:
            siblings[INTERNAL_CLIENT_ID_KEY] = submitted_oauth[INTERNAL_CLIENT_ID_KEY]
        if KIRO_SCOPES_KEY in submitted_oauth and INTERNAL_SCOPES_KEY not in submitted:
            siblings[KIRO_SCOPES_KEY] = submitted_oauth[KIRO_SCOPES_KEY]
    if siblings:
        spec[KIRO_OAUTH_KEY] = siblings
    else:
        spec.pop(KIRO_OAUTH_KEY, None)
    return None


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
        # The secure store write DACLs its temp file via icacls on Windows —
        # a blocking subprocess that must not run on the event loop.
        await _mcp._offload_config_write(_mcp._atomic_write, _mcp._kirocrew_mcp_json(), data)

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
    """GET /api/mcp/custom/{name} — the editable spec of one server.

    The servers list endpoint intentionally omits ``env``; the edit modal
    must prefill from the full spec or a save would silently drop the
    user's env vars. Header names remain visible, but their values are
    preserve-only redaction markers. Reads the same scope PUT writes.
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
    if "headers" in spec:
        spec["headers"] = redact_mcp_headers(spec["headers"])
    return web.json_response({"name": name, "spec": spec, "enabled": enabled})


async def api_mcp_custom_update(request: web.Request) -> web.Response:
    """PUT /api/mcp/custom/{name} — replace an installed server's spec.

    404 when the name isn't a KiroCrew-managed entry.  The server's
    enabled/disabled state is preserved — editing is not consent to run.

    Keys outside the editable allowlist that already exist on the entry
    (``disabledTools``, ``autoApprove``, …) round-trip: the GET payload
    includes them, an unmodified save is accepted, and their on-disk
    values are preserved verbatim.  They cannot be edited or removed
    through this endpoint — dropping ``disabledTools`` on a save would
    silently widen the agent's tool surface.

    ``headers`` straddles that line: an entry WITHOUT stored headers may
    author them here like a fresh add, but once values exist on disk they
    join the carried set — reads redact header values, so accepting an
    edit would let the redaction markers overwrite the real credentials.
    Changing stored headers means removing and re-adding the server.

    The authorship marker is the one exception: it records that Kiro Crew
    wrote an entry into a file it does NOT own, so it has no meaning in the
    store.  An unmodified round trip still SAVES with it present — refusing
    would strand the entry — but it is dropped rather than written back.  That
    keeps "the editor never writes the marker" structurally true: a hand-added
    marker in the store cannot ride a save back out and volunteer the entry
    for management on a shared surface.  A FRESH add carries no tolerated
    keys, so a pasted block naming it is still rejected outright.
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
            # ``headers`` is in the editable allowlist for entries WITHOUT
            # stored values, but once values exist on disk they are carried:
            # reads redact them, so an editable headers key would let the
            # redaction markers overwrite the real credentials on save.  An
            # EMPTY stored map carries no credential, so it does not join the
            # carried set — the entry stays header-authorable.
            if (k not in _ALLOWED_SPEC_KEYS or (k == "headers" and v))
            and k != "disabled"
            and k != MARKER_KEY
            and k not in (KIRO_SCOPES_KEY, KIRO_OAUTH_KEY)
        }
        err = _validate_spec(
            submitted, frozenset(carried) | {KIRO_SCOPES_KEY, KIRO_OAUTH_KEY, MARKER_KEY}
        )
        if err:
            return web.json_response({"error": err}, status=400)
        assert isinstance(submitted, dict)  # narrowed by _validate_spec
        for key, on_disk in carried.items():
            visible_value = redact_mcp_headers(on_disk) if key == "headers" else on_disk
            if key in submitted and submitted[key] != visible_value:
                if key == "headers":
                    return web.json_response(
                        {
                            "error": "stored header values are hidden and"
                            " read-only here. To change them, remove this"
                            " server and re-add it with fresh headers — it"
                            " starts disabled and your tool restrictions"
                            " won't carry over, so re-apply them after"
                            " re-adding.",
                            "code": "stored_headers_not_editable",
                        },
                        status=400,
                    )
                return web.json_response(
                    {
                        "error": f"'{key}' is managed by other flows and cannot be"
                        " edited here — revert it to save"
                    },
                    status=400,
                )
        # A headers map is scoped to the HOST it was typed for. The carried-key
        # restore below puts the on-disk credential back verbatim, so accepting
        # a url change here would silently re-point that credential at a
        # different origin — the next probe would send it there. Dropping costs
        # a re-auth; forwarding a secret cannot be undone.
        if "headers" in carried and submitted.get("url", "") != existing.get("url", ""):
            return web.json_response(
                {
                    "error": "cannot change 'url' while stored header credentials"
                    " exist — header values are hidden and read-only here, and"
                    " they were issued for the current URL. Remove this server"
                    " and re-add it with the new URL and fresh headers.",
                    "code": "url_change_with_stored_headers",
                },
                status=400,
            )
        spec = _clean_spec(submitted)
        spec.update(carried)  # preserved verbatim, never dropped
        _oauth_err = _resolve_oauth_hints(spec, submitted, existing)
        if _oauth_err:
            return web.json_response(
                {"error": _oauth_err, "code": "oauth_field_not_editable"}, status=400
            )
        # Same offload rationale as the add path: the store write applies an
        # owner-only DACL via a subprocess on Windows.
        replaced = await _mcp._offload_config_write(_replace_kirocrew_spec, name, spec)
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
