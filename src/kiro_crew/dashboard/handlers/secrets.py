"""Secrets vault management API handlers for the dashboard."""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)

# C0 controls (\x00-\x1f), DEL + C1 controls (\x7f-\x9f), and the Unicode line
# (U+2028) / paragraph (U+2029) separators — EXCLUDING tab/LF/CR, which are given
# their familiar \n/\r/\t spellings before this pattern runs (listing them here
# would double-escape them). C1 bytes (e.g. CSI \x9b) drive a terminal and
# U+2028/U+2029 break a line in a Unicode-aware log viewer, so both are
# log-injection vectors that must be neutralized alongside C0/DEL.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")


def _sel():
    """Late-binding sel() for test monkeypatch compatibility (see agents.py)."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    """Return a 403 unless the caller is the configured dashboard owner.

    The `/api/secrets` routes read, overwrite and delete AES-256-GCM vault
    entries — machine-global, keystone-floor material that later sessions and
    other integrations consume. `token_auth_middleware` authenticates the
    request but does NOT restrict it to the owner: an app token or any other
    authenticated dashboard subject (e.g. a Slack-origin credential minted for
    a different subject) reaches these handlers. Gate them per-handler with the
    same predicate the sibling secret-adjacent handlers use
    (`agents.py`, `aws_consent.py`, `messaging.py`), and — matching those
    handlers — write a SEL audit record on the denial so a non-owner attempt on
    the vault is not silent. Returns the 403 to send, or ``None`` when the
    caller is the owner.
    """
    if is_owner_dashboard_request(request):
        return None
    # Off the loop: the FIRST sel() of a process CONSTRUCTS the log (trust-dir
    # creation, key validation — blocking file IO), so on a fresh gateway whose
    # first secrets request is non-owner this would otherwise stall every other
    # request. Same reasoning as agents._require_owner.
    caller = str(request.get("user") or "unknown")
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="non_owner_block",
            )
        )
    except Exception:  # pragma: no cover — audit must never change the outcome
        logger.debug("SEL audit for non-owner %s failed", operation, exc_info=True)
    return web.json_response(
        {"error": "owner authorization required", "code": "owner_only"},
        status=403,
    )


def _sanitize_for_log(value: str) -> str:
    r"""Neutralize control characters before a user-controlled value is logged.

    The secret ``name`` is free-form user input (request body or URL path
    segment) and only has leading/trailing whitespace trimmed, so interior
    control characters survive into the log sink and let a caller forge
    additional log lines / fake audit entries (CWE-117 log injection), or
    smuggle ANSI escape sequences (``\x1b[...``) into a terminal-backed viewer.

    Every C0 control character (``\x00``-``\x1f``), DEL and the C1 controls
    (``\x7f``-``\x9f``), and the Unicode line/paragraph separators
    (``\u2028`` / ``\u2029``) are escaped to a readable ``\xNN`` / ``\uNNNN``
    form so the value stays legible in the log but can no longer break onto a
    new line or drive a terminal. The common
    ``\n`` / ``\r`` / ``\t`` are given their familiar two-character spellings.
    The backslash is escaped first so the escapes themselves are unambiguous.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    def _escape(m: "re.Match[str]") -> str:
        cp = ord(m.group())
        return f"\\x{cp:02x}" if cp <= 0xFF else f"\\u{cp:04x}"

    return _CONTROL_CHAR_RE.sub(_escape, value)


async def api_secrets_list(request: web.Request) -> web.Response:
    """GET /api/secrets — list stored secret names (values are never exposed)."""
    denied = await _owner_only(request, "secrets_list")
    if denied is not None:
        return denied
    vault = SecretVault(config_dir())
    # `list_names` is synchronous: it reads the whole store off disk and parses
    # the JSON. Calling it directly would block the gateway's event loop for the
    # duration of that read, stalling every other request. `SecretVault.set` and
    # `.delete` already offload internally via `asyncio.to_thread`; this is the
    # one read path that does not, so it is wrapped here.
    names = await asyncio.to_thread(vault.list_names)
    return web.json_response({"names": sorted(names)})


async def api_secrets_set(request: web.Request) -> web.Response:
    """POST /api/secrets — store or update a secret.

    Body: {"name": "...", "value": "..."}
    """
    denied = await _owner_only(request, "secrets_set")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)

    # A JSON body can legally be an array, string or number, and `name`/`value`
    # can legally be any JSON type. Calling `.strip()` on a non-string raised
    # AttributeError and surfaced as an HTTP 500 on well-formed-but-wrong input,
    # so both the container and the field types are checked before use.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"},
            status=400,
        )

    name = body.get("name")
    value = body.get("value")

    if not isinstance(name, str):
        return web.json_response(
            {"error": "Secret name must be a string", "code": "invalid_name_type"},
            status=400,
        )
    if not isinstance(value, str):
        return web.json_response(
            {"error": "Secret value must be a string", "code": "invalid_value_type"},
            status=400,
        )

    name = name.strip()

    if not name:
        return web.json_response(
            {"error": "Secret name is required", "code": "missing_name"}, status=400
        )
    # Reject a value that is empty or whitespace-only, but store the ORIGINAL
    # value unchanged: a credential can legitimately contain leading/trailing
    # whitespace, and trimming it before storage would corrupt the secret and
    # break downstream authentication. Only the emptiness *check* strips.
    if not value.strip():
        return web.json_response(
            {"error": "Secret value is required", "code": "missing_value"}, status=400
        )

    vault = SecretVault(config_dir())
    await vault.set(name, value)
    logger.info("Vault entry '%s' stored via dashboard", _sanitize_for_log(name))
    return web.json_response({"ok": True, "name": name})


async def api_secrets_delete(request: web.Request) -> web.Response:
    """DELETE /api/secrets/{name} — delete a secret."""
    denied = await _owner_only(request, "secrets_delete")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    if not name:
        return web.json_response(
            {"error": "Secret name is required", "code": "missing_name"}, status=400
        )

    vault = SecretVault(config_dir())
    # `vault.delete` is a no-op when the name is absent, so an unconditional
    # `{"ok": true}` would report success for a mistyped name that was never
    # stored — the SecretsPanel then re-fetches and still shows the old entry,
    # leaving the user to think a delete failed silently. Check membership first
    # and return 404 so a missing name is an explicit, actionable error.
    names = await asyncio.to_thread(vault.list_names)
    if name not in names:
        return web.json_response({"error": "Secret not found", "code": "not_found"}, status=404)
    await vault.delete(name)
    logger.info("Vault entry '%s' deleted via dashboard", _sanitize_for_log(name))
    return web.json_response({"ok": True, "name": name})


def setup_secrets_routes(app: web.Application) -> None:
    """Register /api/secrets routes on the dashboard app."""
    app.router.add_get("/api/secrets", api_secrets_list)
    app.router.add_post("/api/secrets", api_secrets_set)
    app.router.add_delete("/api/secrets/{name}", api_secrets_delete)
