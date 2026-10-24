"""Secrets vault management API handlers for the dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import (
    CRED_FEISHU_APP_ID,
    CRED_JIRA_API_TOKEN,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_OWNER_ID,
    CRED_WAKATIME_API_KEY,
    CRED_WECOM_BOT_ID,
    MANAGED_VAULT_FIXED_CONSUMERS,
    VAULT_CHANNEL_CREDENTIAL_KEYS,
    EnvFileWideEncodingError,
    KiroCrewConfig,
    config_dir,
    jira_global_token_applicable,
    jira_host_token_name,
    normalize_jira_host,
    read_env_text,
)
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.dashboard.source_providers.gerrit import (
    GLOBAL_CREDENTIAL_NAME as GERRIT_GLOBAL_CREDENTIAL_NAME,
)
from kiro_crew.dashboard.source_providers.gerrit import (
    host_credential_name as gerrit_host_credential_name,
)
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


_MANAGED_KIND_JIRA_HOST_TOKEN = "jira_host_token"
# A Gerrit ``username:HTTP password`` pair for one listed host. One kind for both
# the lone-host global name and the per-host names: every row names its host, so
# the panel labels it by host and the hex-suffixed vault name stays a detail.
_MANAGED_KIND_GERRIT_HTTP_CREDENTIAL = "gerrit_http_credential"
# A Connections pre-registered OAuth client secret (``connections/oauth_clients``).
_MANAGED_KIND_CONNECTIONS_CLIENT_SECRET = "connections_client_secret"


def _connections_client_secret_owned(name: str) -> web.Response | None:
    """Refuse a generic vault write to a Connections client secret.

    Those entries are one half of a two-store record whose other half lives in
    ``config.json``, and every change to them must also rebuild the agent spec
    and withdraw the slug's in-flight mint -- the OAuth-client routes do all of
    that under one lock. A write through this generic route would change the
    vault half alone and leave the runtime projection on the old credential, so
    the panel lists these entries but does not edit them. Returns the 409 to
    send, or ``None`` when the name is not one of ours.
    """
    from kiro_crew.connections.oauth_clients import managed_client_secret_names

    slug = managed_client_secret_names().get(name)
    if slug is None:
        return None
    return web.json_response(
        {
            "error": "this secret belongs to a Connections OAuth app; change it under "
            "Settings → OAuth Apps",
            "code": "managed_by_connections",
            "slug": slug,
        },
        status=409,
    )


# The ``.env`` keys the channel settings panels write: every channel token in
# VAULT_CHANNEL_CREDENTIAL_KEYS plus the channel identifiers. All of them are
# credential keys, so a ``secret://NAME`` line on any of them resolves NAME from
# the vault when the channel starts.
_CHANNEL_ENV_KEYS: frozenset[str] = frozenset(
    VAULT_CHANNEL_CREDENTIAL_KEYS
    | {
        CRED_OWNER_ID,
        CRED_WECOM_BOT_ID,
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_TENANT_ID,
        CRED_FEISHU_APP_ID,
    }
)


def _channel_referenced_names() -> frozenset[str] | None:
    """The vault names a channel key's ``.env`` line or environment value references.

    Parses ``.env`` the way ``load_credentials`` does, and also reads the
    process environment, whose value for a key overrides the file's. A missing
    file, and a UTF-16/32 file the credential readers treat as unset, reference
    nothing. Returns ``None`` when the file exists but cannot be read, so the
    caller fails closed. Blocking file IO: call via ``asyncio.to_thread``.
    """
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

    try:
        text = read_env_text(Path(config_dir()) / ".env")
    except FileNotFoundError:
        text = ""
    except EnvFileWideEncodingError:
        text = ""
    except (OSError, UnicodeDecodeError):
        return None
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs.append((key.strip(), value.strip()))
    pairs.extend((key, os.environ.get(key, "")) for key in _CHANNEL_ENV_KEYS)
    return frozenset(
        value[len(SECRET_URI_PREFIX) :]
        for key, value in pairs
        if key in _CHANNEL_ENV_KEYS and value.startswith(SECRET_URI_PREFIX)
    )


async def _channel_credential_owned(request: web.Request, name: str) -> web.Response | None:
    """Refuse a remote vault write to a name a channel credential resolves from.

    Every channel settings panel accepts a save only from the machine running
    the gateway (``is_direct_local_request``), so a remote or tunnelled session
    cannot plant a channel token or take a channel down. A channel token lives
    in the vault behind its ``.env`` reference, so this generic route must apply
    the same gate to it: to a name in ``VAULT_CHANNEL_CREDENTIAL_KEYS`` (where the
    channel save stores the token) and to any name a channel key's ``.env``
    line or environment value references. A ``.env`` that cannot be read is
    treated as referencing every name. Returns the 409 to send, or ``None``
    when the write may proceed.
    """
    if is_direct_local_request(request):
        return None
    if name not in VAULT_CHANNEL_CREDENTIAL_KEYS:
        referenced = await asyncio.to_thread(_channel_referenced_names)
        if referenced is not None and name not in referenced:
            return None
    return web.json_response(
        {
            "error": "this secret holds a channel credential, which a remote session "
            "cannot change; change it under Settings → Channels on the machine "
            "running the gateway",
            "code": "managed_by_channels",
        },
        status=409,
    )


def _managed_secret_config() -> tuple[list[str], bool, bool, bool, list[str]]:
    """Return managed-consumer config without making vault listing fail."""
    try:
        config = KiroCrewConfig.load()
    except Exception:
        # Secret names remain available when unrelated config is broken, while
        # the response carries a non-fatal hint so the UI does not misrepresent
        # "config unreadable" as "no managed integrations enabled".
        logger.debug("Could not load managed-secret configuration", exc_info=True)
        return ([], False, False, True, [])
    entries = config.dashboard.jira_auth
    hosts = [normalized for entry in entries if (normalized := normalize_jira_host(entry.host))]
    # Already normalized by the ``dashboard.gerrit_hosts`` coercer.
    gerrit_hosts = sorted(set(getattr(config.dashboard, "gerrit_hosts", ())))
    degraded: frozenset[str] = getattr(config, "degraded_sections", frozenset())
    managed_config_error = bool(degraded & {DEGRADED_WHOLE_CONFIG, "dashboard", "wakatime"})
    return (
        hosts,
        jira_global_token_applicable(entries),
        bool(config.wakatime.enabled),
        managed_config_error,
        gerrit_hosts,
    )


def _managed_secret_catalog(
    names: list[str],
    jira_hosts: list[str],
    jira_global_applicable: bool,
    wakatime_enabled: bool,
    gerrit_hosts: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Describe managed vault names the current integration config can consume.

    One configured host may use the global token. Multiple hosts must use their
    per-host names. A single host's per-host name is also listed when already
    stored, because the runtime gives it precedence over the global fallback.
    Gerrit follows the same rule, with every row carrying its host so the user
    never has to derive the hex-suffixed name. Values and configured state are
    not duplicated here: callers already receive the complete ``names``
    membership list in the same response.
    """
    name_set = set(names)
    catalog: list[dict[str, str]] = []
    if wakatime_enabled:
        catalog.append(
            {
                "name": CRED_WAKATIME_API_KEY,
                "kind": MANAGED_VAULT_FIXED_CONSUMERS[CRED_WAKATIME_API_KEY],
            }
        )
    if jira_global_applicable and jira_hosts:
        per_host_name = jira_host_token_name(jira_hosts[0])
        if per_host_name not in name_set:
            catalog.append(
                {
                    "name": CRED_JIRA_API_TOKEN,
                    "kind": MANAGED_VAULT_FIXED_CONSUMERS[CRED_JIRA_API_TOKEN],
                }
            )

    for host in sorted(set(jira_hosts)):
        name = jira_host_token_name(host)
        if not jira_global_applicable or name in name_set:
            catalog.append(
                {
                    "name": name,
                    "kind": _MANAGED_KIND_JIRA_HOST_TOKEN,
                    "host": host,
                }
            )
    # Gerrit: the lone listed host is served by the global name (unless its
    # per-host name is already stored, which the provider prefers); two or more
    # hosts each get their per-host name, and the global one stops applying.
    gerrit_global_applicable = len(gerrit_hosts) == 1
    for host in gerrit_hosts:
        name = gerrit_host_credential_name(host)
        if gerrit_global_applicable and name not in name_set:
            name = GERRIT_GLOBAL_CREDENTIAL_NAME
        catalog.append({"name": name, "kind": _MANAGED_KIND_GERRIT_HTTP_CREDENTIAL, "host": host})
    # Connections OAuth client secrets. Listed only when STORED: the entry field
    # lives on Settings → OAuth Apps, so the Secrets panel's job is to label an
    # existing entry with its owner (not offer an empty slot), and keep a cleanup
    # from reading it as a stray user secret. ``host`` carries the provider slug,
    # the same way the Jira rows carry their host.
    from kiro_crew.connections.oauth_clients import managed_client_secret_names

    for name, slug in sorted(managed_client_secret_names().items()):
        if name in name_set:
            catalog.append(
                {"name": name, "kind": _MANAGED_KIND_CONNECTIONS_CLIENT_SECRET, "host": slug}
            )
    return catalog


def _unused_stored_secrets(
    names: list[str],
    jira_hosts: list[str],
    jira_global_applicable: bool,
    wakatime_enabled: bool,
    gerrit_hosts: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Classify stored integration names that runtime configuration will not use."""
    name_set = set(names)
    unused: list[dict[str, str]] = []
    if CRED_WAKATIME_API_KEY in name_set and not wakatime_enabled:
        unused.append({"name": CRED_WAKATIME_API_KEY, "reason": "wakatime_disabled"})
    if CRED_JIRA_API_TOKEN in name_set and jira_hosts:
        if not jira_global_applicable:
            unused.append({"name": CRED_JIRA_API_TOKEN, "reason": "jira_multi_host"})
        elif jira_host_token_name(jira_hosts[0]) in name_set:
            unused.append({"name": CRED_JIRA_API_TOKEN, "reason": "jira_host_precedence"})
    if GERRIT_GLOBAL_CREDENTIAL_NAME in name_set and gerrit_hosts:
        if len(gerrit_hosts) > 1:
            unused.append({"name": GERRIT_GLOBAL_CREDENTIAL_NAME, "reason": "gerrit_multi_host"})
        elif gerrit_host_credential_name(gerrit_hosts[0]) in name_set:
            unused.append(
                {"name": GERRIT_GLOBAL_CREDENTIAL_NAME, "reason": "gerrit_host_precedence"}
            )
    return unused


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
    # A bare enqueue: SEL is warmed at gateway startup (sel.warm_sel_singleton).
    # Guarded because a FAILED warm leaves construction to retry here
    # and possibly raise — the audit must never change the outcome.
    caller = str(request.get("user") or "unknown")
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
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
    names = sorted(await asyncio.to_thread(vault.list_names))
    (
        jira_hosts,
        jira_global_applicable,
        wakatime_enabled,
        managed_config_error,
        gerrit_hosts,
    ) = await asyncio.to_thread(_managed_secret_config)
    payload: dict[str, object] = {
        "names": names,
        "managed": _managed_secret_catalog(
            names,
            jira_hosts,
            jira_global_applicable,
            wakatime_enabled,
            gerrit_hosts,
        ),
    }
    unused = _unused_stored_secrets(
        names,
        jira_hosts,
        jira_global_applicable,
        wakatime_enabled,
        gerrit_hosts,
    )
    if unused:
        payload["unused"] = unused
    if managed_config_error:
        payload["managed_error"] = True
    return web.json_response(payload)


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
    owned = _connections_client_secret_owned(name)
    if owned is not None:
        return owned
    owned = await _channel_credential_owned(request, name)
    if owned is not None:
        return owned

    vault = SecretVault(config_dir())
    # The revision names THIS write (a digest of its ciphertext, never the
    # value), so a caller that may delete it later can delete exactly it.
    revision = await vault.set_with_revision(name, value)
    logger.info("Vault entry '%s' stored via dashboard", _sanitize_for_log(name))
    return web.json_response({"ok": True, "name": name, "revision": revision})


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
    owned = _connections_client_secret_owned(name)
    if owned is not None:
        return owned
    owned = await _channel_credential_owned(request, name)
    if owned is not None:
        return owned

    vault = SecretVault(config_dir())
    # `vault.delete` is a no-op when the name is absent, so an unconditional
    # `{"ok": true}` would report success for a mistyped name that was never
    # stored — the SecretsPanel then re-fetches and still shows the old entry,
    # leaving the user to think a delete failed silently. Check membership first
    # and return 404 so a missing name is an explicit, actionable error.
    names = await asyncio.to_thread(vault.list_names)
    if name not in names:
        return web.json_response({"error": "Secret not found", "code": "not_found"}, status=404)
    # ``?if_revision=`` deletes only the entry that write stored (a change
    # card's Undo), never a value saved over it since.
    if_revision = request.query.get("if_revision")
    if if_revision is not None:
        if not await vault.delete_if_revision(name, if_revision):
            return web.json_response(
                {"error": "The secret changed since it was saved", "code": "changed"},
                status=409,
            )
    else:
        await vault.delete(name)
    logger.info("Vault entry '%s' deleted via dashboard", _sanitize_for_log(name))
    return web.json_response({"ok": True, "name": name})


def setup_secrets_routes(app: web.Application) -> None:
    """Register /api/secrets routes on the dashboard app."""
    app.router.add_get("/api/secrets", api_secrets_list)
    app.router.add_post("/api/secrets", api_secrets_set)
    app.router.add_delete("/api/secrets/{name}", api_secrets_delete)
