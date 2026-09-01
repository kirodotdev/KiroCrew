"""AgentCore Gateway attach — session/new inject + per-session inbound sidecar.

Public core never vends a token (``DefaultAgentIdentityProvider`` returns
``None``). A companion adapter supplies ``gateway_mcp_spec()`` and
``vend_gateway_inbound_token``. The Gateway is never written into
``~/.kiro/agents/kirocrew.json``: ``--agent`` loads that file for every
session, including one whose profile disabled ``capabilities.agentcore``.
``session_gateway_servers`` is the only contribution path and it honors
``session_key``.

Workload posture injects a localhost SigV4 proxy URL on ``session/new``.
kiro-cli never sees the unsigned Gateway hostname. Login posture leaves
Gateway out of the agent file until ``attach_gateway_inbound`` writes a
``0600`` session sidecar; session/new reads that sidecar. A companion
JWT becomes an ``Authorization`` header. Without one, the sidecar is
URL-only so kiro-cli can run its MCP OAuth challenge
(``_kiro.dev/mcp/oauth_request``). Token bytes never enter the agent
file, SEL, logs, or ``status()``.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.platform.context import (
    async_safe_context_call,
    current_context,
    safe_context_call,
)
from kiro_crew.platform.governance import agentcore_posture
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY, vet_and_audit
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal
from kiro_crew.security import allow_agentcore_consent_url
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# Owning-module constants (code-style): Gateway MCP name + inbound dir.
class GatewayCredentialError(Exception):
    """Leftover inbound credentials could not be dropped before session/new."""


GATEWAY_SERVER_NAME = "agentcore-gateway"
INBOUND_DIR_NAME = "agentcore-inbound"

# Exact interactive keys — ``cli_chat`` in ``cli_chat.py``. Unknown
# keys default to unattended; only these plus the dashboard / channel
# namespaces below bind as a human at the keyboard.
INTERACTIVE_SESSION_KEYS = frozenset({"cli_chat"})
# Exact keys, not prefixes — ``BACKGROUND_KEY`` / ``HEARTBEAT_KEY`` in
# session.py. Shared automation slots, never a human at a keyboard.
UNATTENDED_SESSION_KEYS = frozenset({"_bg", "_hb"})
# Known automation prefixes kept for docs and tests. ``is_unattended_session``
# does not use this as a denylist: a custom ``ctx.agent`` session name
# is unattended because it is *not* an interactive namespace.
# ``channel:`` is ``run_channel_agent`` (``channel:{id}:{agent}``),
# not a human ChannelTurn (those use ``slack:`` / ``discord:`` / …).
# ``meetings-`` is ``slot_key()`` (``meetings-{agent}-{meeting}``).
UNATTENDED_SESSION_PREFIXES = (
    "cron:",
    "taskrunner:",
    "subagent:",
    "hook:",
    "channel:",
    "meetings-",
    "wf:",
    "wf-pool:",
    "wf-author:",
    "wf-unpooled:",
)

# Companion ``status()`` keys. Display / policy only — never token material,
# and never written onto the sanitized Gateway spec (``_URL_ONLY_KEYS``).
STATUS_AUTHORIZATION_URL = "authorizationUrl"
STATUS_CREDENTIAL_KIND = "credentialKind"
STATUS_VAULTED_OWNER = "vaultedOwnerToken"
CREDENTIAL_KIND_M2M = "m2m"
CREDENTIAL_KIND_USER = "user"

# Inbound sidecar states. ``expired`` is the drain trigger: the file is
# gone and the live ACP child must be recycled so session/new cannot keep
# presenting a dead JWT.
SIDECAR_LIVE = "live"
SIDECAR_DENIED = "denied"
SIDECAR_EXPIRED = "expired"
SIDECAR_ABSENT = "absent"
REASON_EXPIRED = "expired"
REASON_OAUTH_CHALLENGE = "oauth_challenge"

# Spec keys that are bearer material or a place to hide it. Stripped
# from every session-inject spec so a companion extra cannot hand kiro-cli
# a bearer.
_SECRET_SPEC_KEYS = frozenset({"headers", "authorization", "Authorization"})

# Remote-MCP keys allowed on the session-inject spec (URL + transport).
_URL_ONLY_KEYS = frozenset({"url", "type", "timeout", "disabledTools", "autoApprove"})

# Hosts the SigV4 proxy may advertise. https is never loopback-listen.
_LOOPBACK_LISTEN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# kiro-cli 2.20.1 ``session/new`` deserializes an untagged ``McpServer``.
# HTTP requires ``type`` plus a ``headers`` array (empty is fine). A
# deny-only ``{name, disabled: true}`` is rejected, so retract uses a
# disabled HTTP element. Port 1 is never the SigV4 proxy.
ACP_HTTP_TYPE = "http"
ACP_DENIED_PLACEHOLDER_URL = "http://127.0.0.1:1/mcp"


def strip_secret_spec_keys(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Copy *spec* without header / Authorization keys."""
    return {key: value for key, value in spec.items() if key not in _SECRET_SPEC_KEYS}


def sanitize_gateway_spec(spec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a URL-only remote MCP spec, or ``None`` when it is not one.

    Requires a non-empty string ``url``. Drops ``headers`` / ``Authorization``
    so a companion extra cannot put a bearer on the session-inject spec.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    out: dict[str, Any] = {"url": url.strip()}
    for key in _URL_ONLY_KEYS:
        if key == "url" or key not in spec:
            continue
        out[key] = spec[key]
    return out


def _looks_interactive_namespace(session_key: str) -> bool:
    """True when *session_key* uses a dashboard or channel namespace spelling."""
    lowered = session_key.lower()
    if lowered.startswith(("dashboard:", "dashboard_")):
        return True
    from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES

    for ns in CHANNEL_SESSION_NAMESPACES:
        if lowered.startswith((f"{ns}:", f"{ns}_")):
            return True
    return False


def _human_turn_proven(session_key: str) -> bool:
    """True when a real human path staged this key or already attached it.

    A caller-chosen ``slack:forged`` / ``dashboard:…`` spelling is not
    enough: ``ctx.agent(session=…)`` can mint those prefixes. Staging
    proves a human only when both surface and raw ID are set —
    unbound dashboard / shared-room prepare stages ``(None, None)``.
    A live non-deny inbound sidecar is the durable proof after that
    bind is consumed.
    """
    staged = peek_staged_gateway(session_key)
    if staged is not None:
        surface, raw_id = staged
        # Unbound dashboard / shared-room prepare stages ``(None, None)``.
        # That is not a human bind — treating it as one skips the
        # unattended denial SEL event in ``session_gateway_servers``.
        if surface and raw_id:
            return True
    data = read_inbound_sidecar(session_key)
    if data is None or data.get("denied") is True:
        return False
    url = data.get("url")
    return isinstance(url, str) and bool(url)


def is_unattended_session(session_key: str) -> bool:
    """True unless *session_key* is a positively identified interactive surface.

    Unknown keys (empty, ``_host``, ``ctx.agent(..., session="custom")``)
    default to unattended so workload user/OBO cannot reach Gateway
    without a vaulted token. Human ChannelTurns use a channel namespace
    (``slack:`` / ``discord:`` / …); ``channel:`` is ``run_channel_agent``.
    Dashboard/channel *spellings* are interactive only when a matching
    staged human turn or live login sidecar proves the key.
    """
    if not session_key or session_key in UNATTENDED_SESSION_KEYS:
        return True
    if session_key in INTERACTIVE_SESSION_KEYS:
        return False
    if not _looks_interactive_namespace(session_key):
        return True
    return not _human_turn_proven(session_key)


def _adapter_status() -> dict[str, Any]:
    empty: dict[str, Any] = {}
    raw: Any = safe_context_call(
        lambda: current_context().agent_identity.status(),
        fallback=empty,
        log_message="agent_identity.status lookup failed; treating as empty",
    )
    return raw if isinstance(raw, dict) else {}


def _credential_kind() -> str:
    kind = str(_adapter_status().get(STATUS_CREDENTIAL_KIND) or "").strip().lower()
    if kind == CREDENTIAL_KIND_M2M:
        return CREDENTIAL_KIND_M2M
    return CREDENTIAL_KIND_USER


def _vaulted_owner_token() -> bool:
    return _adapter_status().get(STATUS_VAULTED_OWNER) is True


def _unattended_user_permitted() -> bool:
    """M2M may run unattended; user/OBO needs a still-valid vaulted owner token."""
    if _credential_kind() == CREDENTIAL_KIND_M2M:
        return True
    return _vaulted_owner_token()


def _consent_host_path(url: str) -> str:
    """Host+path only — never a query string (state / PKCE / code)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not host:
        return ""
    return f"{host}{path}"


def surface_consent_url(url: str | None) -> str | None:
    """Return *url* when it is allowlisted; SEL grant/deny. Never token bytes.

    An absent / empty URL is not a deny — there is nothing to surface.
    A present URL that fails the allowlist is refused (SEL denied).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    stripped = url.strip()
    host_path = _consent_host_path(stripped)
    if allow_agentcore_consent_url(stripped):
        sel().log_api_access(
            caller="system",
            operation="agentcore.consent_url",
            outcome="ok",
            source="agentcore_gateway",
            resources=host_path,
        )
        return stripped
    sel().log_api_access(
        caller="system",
        operation="agentcore.consent_url",
        outcome="denied",
        source="agentcore_gateway",
        resources=host_path or "unknown-host",
    )
    return None


def pending_consent_url() -> str | None:
    """Allowlisted companion ``authorizationUrl``, or ``None``.

    Capability / adapter / posture must all be on (same conjunct as Gateway
    attach). The URL is never taken from a tool argument or the model.
    """
    snap = consent_snapshot()
    return snap["url"] if snap["pending"] else None


def consent_snapshot() -> dict[str, Any]:
    """Pending 3LO URL after the allowlist, or a refused/absent snapshot.

    ``refused`` is True only when the companion published a URL that failed
    the allowlist — the dashboard maps that to 403 ``consent_host_refused``.

    Evaluated on ``HOST_SESSION_KEY`` so a ``surface:host`` profile that
    denies AgentCore cannot leak a live URL through the unknown surface
    an empty key would classify as.
    """
    if not _identity_on(HOST_SESSION_KEY):
        return {"pending": False, "url": None, "refused": False}
    raw = _adapter_status().get(STATUS_AUTHORIZATION_URL)
    if not isinstance(raw, str) or not raw.strip():
        return {"pending": False, "url": None, "refused": False}
    allowed = surface_consent_url(raw)
    if allowed is None:
        return {"pending": False, "url": None, "refused": True}
    return {"pending": True, "url": allowed, "refused": False}


def inbound_sidecar_path(session_key: str) -> Path:
    """Owner-only sidecar path for one session's inbound token.

    The filename is a digest of *session_key* so ``:`` / ``/`` in a key cannot
    escape the inbound directory.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return config_dir() / INBOUND_DIR_NAME / f"{digest}.json"


def _identity_on(session_key: str = "", *, agent: str = "") -> bool:
    """Adapter on AND capability permitted AND known posture. Fail closed."""
    adapter_on = bool(
        safe_context_call(
            lambda: current_context().agent_identity.enabled(),
            fallback=False,
            log_message="agent_identity.enabled lookup failed; treating as disabled",
        )
    )
    if not adapter_on:
        return False
    permitted = bool(
        safe_context_call(
            lambda: getattr(
                vet_and_audit(
                    "capabilities.agentcore",
                    "",
                    session_key=session_key,
                    agent=agent,
                    tool_name="agentcore.gateway_inject",
                    fail_closed=True,
                    log_warning=False,
                ),
                "permitted",
                False,
            ),
            fallback=False,
            log_message="agentcore governance lookup failed; treating as disabled",
        )
    )
    if not permitted:
        return False
    return bool(
        safe_context_call(
            lambda: agentcore_posture(current_context().governance) is not None,
            fallback=False,
            log_message="agentcore posture lookup failed; treating as disabled",
        )
    )


def gateway_requires_fresh_session(session_key: str = "", *, agent: str = "") -> bool:
    """True when ``session/new`` must see current Gateway inject / sidecar.

    Warm-pool processes already completed ``session/new`` at fill time
    with an empty key and no inbound sidecar. A pool hit cannot reuse
    that handshake when AgentCore is on (login JWT or workload SigV4).
    """
    return _identity_on(session_key, agent=agent)


def _current_posture() -> str | None:
    return safe_context_call(
        lambda: agentcore_posture(current_context().governance),
        fallback=None,
        log_message="agentcore posture lookup failed; no Gateway contribution",
    )


def _gateway_spec_from_adapter() -> dict[str, Any] | None:
    spec: dict[str, Any] | None = safe_context_call(
        lambda: current_context().agent_identity.gateway_mcp_spec(),
        fallback=None,
        log_message="gateway_mcp_spec lookup failed; no Gateway contribution",
    )
    if spec is not None:
        return sanitize_gateway_spec(spec)
    extras: dict[str, Any] = safe_context_call(
        lambda: current_context().mcp_tooling.extra_mcp_servers(),
        fallback={},
        log_message="extra_mcp_servers lookup failed; no Gateway fallback",
    )
    if not extras:
        return None
    return sanitize_gateway_spec(extras.get(GATEWAY_SERVER_NAME))


def clear_inbound_sidecar(session_key: str) -> None:
    """Remove this session's inbound sidecar if it exists.

    ``FileNotFoundError`` is success (already absent). Any other OS
    error raises :class:`GatewayCredentialError` so a caller cannot
    spawn a child that would reuse a leftover bearer.
    """
    path = inbound_sidecar_path(session_key)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GatewayCredentialError(f"could not drop inbound sidecar for {session_key}") from exc


def _write_owner_only_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write *payload* through the shared secret writer (symlink-safe)."""
    from kiro_crew.atomic_write import atomic_write

    platform_compat.make_owner_only_dir(path.parent)
    atomic_write(
        path,
        json.dumps(dict(payload), sort_keys=True),
        restrict_to_owner=True,
    )


def _live_inbound_token(token: InboundToken | None) -> InboundToken | None:
    """Return *token* only when it is still valid to present.

    ``session/new`` treats an expired sidecar as gone, then attach
    rewrites it and recycles. Writing an already-expired JWT is that
    loop. A missing or past ``expires_at`` is absent, not a bearer.
    """
    if token is None or token.expires_at <= time.time():
        return None
    return token


def inbound_sidecar_state(session_key: str) -> str:
    """Classify this session's inbound sidecar without mutating it.

    ``expired`` means a file is present and ``expires_at`` is in the past.
    Callers that must drain a live ACP transport key off this, not off a
    later ``read_inbound_sidecar`` miss (that helper deletes the file).
    """
    path = inbound_sidecar_path(session_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SIDECAR_ABSENT
    except (OSError, ValueError):
        logger.warning("agentcore inbound sidecar unreadable; treating as absent")
        return SIDECAR_ABSENT
    if not isinstance(raw, dict):
        return SIDECAR_ABSENT
    if raw.get("denied") is True:
        return SIDECAR_DENIED
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        return SIDECAR_EXPIRED
    return SIDECAR_LIVE


def read_inbound_sidecar(session_key: str) -> dict[str, Any] | None:
    """Load this session's inbound sidecar, or ``None`` if missing / expired."""
    state = inbound_sidecar_state(session_key)
    if state in (SIDECAR_ABSENT, SIDECAR_EXPIRED):
        if state == SIDECAR_EXPIRED:
            clear_inbound_sidecar(session_key)
        return None
    path = inbound_sidecar_path(session_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("agentcore inbound sidecar unreadable; treating as absent")
        return None
    return raw if isinstance(raw, dict) else None


async def drain_expired_gateway_transport(sessions: Any, session_key: str) -> bool:
    """Recycle a live ACP child whose inbound JWT has expired.

    Gateway is unpooled and injected on ``session/new``. An expired sidecar
    is not enough on its own: kiro-cli still holds the dead header until
    the child is gone. ``SessionManager.remove`` preserves the session map
    so the next turn cold-starts and ``session/load`` restores the
    conversation. Does not touch mcp_gateway pooled backends.

    Returns True when an expired sidecar was found and cleared.
    """
    if not session_key:
        return False
    if inbound_sidecar_state(session_key) != SIDECAR_EXPIRED:
        return False
    clear_inbound_sidecar(session_key)
    sel().log_api_access(
        caller="system",
        operation="agentcore.gateway_inbound",
        outcome="denied",
        source="agentcore_gateway",
        resources=f"session={session_key} reason={REASON_EXPIRED}",
    )
    await _recycle_live_session(sessions, session_key, why="expired")
    return True


def _sidecar_fingerprint(session_key: str) -> str:
    """Sidecar state plus bytes, or absent. Used to detect attach mutations."""
    path = inbound_sidecar_path(session_key)
    try:
        return f"{inbound_sidecar_state(session_key)}:{path.read_text(encoding='utf-8')}"
    except OSError:
        return f"{SIDECAR_ABSENT}:"


_STAGED_GATEWAY: contextvars.ContextVar[tuple[str, str | None, str | None, str] | None] = (
    contextvars.ContextVar("agentcore_staged_gateway", default=None)
)


def stage_session_gateway(
    session_key: str,
    surface: str | None = None,
    raw_id: str | None = None,
    *,
    agent: str = "",
) -> None:
    """Record this turn's Gateway bind for apply-after-acquire."""
    if session_key:
        _STAGED_GATEWAY.set((session_key, surface, raw_id, agent or ""))


def _staged_for(
    session_key: str,
) -> tuple[str, str | None, str | None, str] | None:
    staged = _STAGED_GATEWAY.get()
    if staged is None or staged[0] != session_key:
        return None
    return staged


def peek_staged_gateway(session_key: str) -> tuple[str | None, str | None] | None:
    """Return the staged bind when it names *session_key*, without consuming it."""
    staged = _staged_for(session_key)
    if staged is None:
        return None
    return staged[1], staged[2]


def take_staged_gateway(session_key: str) -> tuple[str | None, str | None] | None:
    """Pop the staged bind when it names *session_key*."""
    staged = peek_staged_gateway(session_key)
    if staged is None:
        return None
    _STAGED_GATEWAY.set(None)
    return staged


async def install_staged_gateway_sidecar(session_key: str) -> None:
    """Write the staged sidecar without consuming it or recycling a live child.

    Cold-start ``session/new`` reads the inbound sidecar during
    ``provider.start()``. This install runs under the per-key creation
    reservation so that read sees this turn's credentials, not a leftover
    bearer. Post-acquire :func:`apply_staged_session_gateway` still
    ``take()``s and recycles if the fingerprint later changes.
    """
    staged = _staged_for(session_key)
    surface, raw_id, agent = (None, None, "") if staged is None else staged[1:]
    await _apply_session_gateway(
        session_key,
        surface=surface,
        raw_id=raw_id,
        sessions=None,
        hold_lease=False,
        agent=agent,
    )


async def _recycle_live_session(
    sessions: Any, session_key: str, *, why: str, hold_lease: bool = False
) -> bool:
    """Drop an ACP child so the next session/new reads a fresh sidecar.

    Without *hold_lease*, never removes a session that currently holds
    its turn lease (``is_busy`` / ``reset(..., skip_if_busy=True)``).
    After ``get_or_create`` has acquired the semaphore the caller passes
    *hold_lease* so this turn can replace its own child. Returns False
    when a foreign live lease blocked the recycle.
    """
    if sessions is None:
        return True
    if not hold_lease:
        is_busy = getattr(sessions, "is_busy", None)
        if callable(is_busy) and is_busy(session_key):
            return False
    reset = getattr(sessions, "reset", None)
    if callable(reset):
        try:
            result = reset(session_key, skip_if_busy=not hold_lease)
            if inspect.isawaitable(result):
                await result
            # ``reset`` returns False when the key is already absent. That
            # is success: no live child remains. A busy foreign lease was
            # rejected above, so a False here is not a leftover bearer.
            return True
        except TypeError:
            logger.debug("session reset rejected skip_if_busy for %s", session_key)
        except Exception:
            logger.debug(
                "could not recycle session %s (%s)",
                session_key,
                why,
                exc_info=True,
            )
            return False
    remover = getattr(sessions, "remove", None)
    if not callable(remover):
        return True
    try:
        result = remover(session_key)
        if inspect.isawaitable(result):
            await result
        return True
    except Exception:
        logger.debug(
            "could not recycle session %s (%s)",
            session_key,
            why,
            exc_info=True,
        )
        return False


def acp_http_server(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    disabled: bool = False,
    name: str = GATEWAY_SERVER_NAME,
) -> dict[str, Any]:
    """ACP ``session/new`` HTTP element kiro-cli will deserialize.

    ``headers`` is always present (empty list when there is no bearer).
    """
    pairs = headers or {}
    shaped: dict[str, Any] = {
        "name": name,
        "type": ACP_HTTP_TYPE,
        "url": url,
        "headers": [{"name": str(key), "value": str(value)} for key, value in pairs.items()],
    }
    if disabled:
        shaped["disabled"] = True
    return shaped


def is_loopback_listen_url(url: str) -> bool:
    """True for the SigV4 proxy listen URL. Never the unsigned Gateway hostname."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_LISTEN_HOSTS


def _workload_live_proxy_servers(session_key: str = "", *, agent: str = "") -> list[dict[str, Any]]:
    """Inject the live loopback listen URL so session/new outranks a stale port.

    Workload attach clears the sidecar. The agent file never holds the
    Gateway; session/new is the only contribution. Never inject https —
    that would be the unsigned Gateway hostname.
    """
    if not _identity_on(session_key, agent=agent):
        return []
    if _current_posture() != "workload":
        return []
    sanitized = _gateway_spec_from_adapter()
    url = str((sanitized or {}).get("url") or "")
    if not is_loopback_listen_url(url):
        return []
    from kiro_crew.platform.agentcore_sigv4 import proxy_auth_headers

    headers = proxy_auth_headers(session_key, agent=agent)
    if not headers:
        return []
    return [acp_http_server(url, headers=headers)]


def session_gateway_servers(session_key: str, *, agent: str = "") -> list[dict[str, Any]]:
    """ACP ``mcpServers`` entries for this session's Gateway, or ``[]``.

    A deny sidecar retracts Gateway. A live login sidecar supplies the
    https URL plus optional ``Authorization``. Workload has no sidecar:
    when identity is on, inject the live loopback SigV4 listen URL so
    session/new outranks a stale agent-file port. Two sessions never
    share a sidecar path, so Gateway stays unpooled. *agent* is the crew
    agent whose task profile may deny the capability.
    """
    if not session_key:
        return []
    if not _identity_on(session_key, agent=agent):
        return []
    data = read_inbound_sidecar(session_key)
    if data is not None and data.get("denied") is not True:
        # Leftover human sidecar: do not inject the previous owner's JWT.
        # Unattended keys never inherit a login bearer. Workload posture
        # uses the SigV4 proxy, so a persisted login sidecar from a
        # prior posture must not reach session/new.
        if is_unattended_session(session_key) or _current_posture() == "workload":
            data = None
    if data is None:
        deny_user = not _unattended_user_permitted()
        # Workload user/OBO needs a vault even when the key *looks*
        # interactive: a forged ``slack:`` prefix must not skip this.
        if deny_user and (is_unattended_session(session_key) or _current_posture() == "workload"):
            principal = SessionPrincipal(
                surface="system",
                subject=session_key,
                session_key=session_key,
            )
            if _current_posture() == "login":
                _log_unattended_denied(principal, reason="login_unattended")
                return []
            _log_unattended_denied(principal, reason="user_without_vault")
            return [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]
        return _workload_live_proxy_servers(session_key, agent=agent)
    if data.get("denied") is True:
        # Session inject outranks the same-named agent-file entry (kiro-cli).
        # Workload user/OBO unattended retracts Gateway this way. kiro-cli
        # rejects ``{name, disabled: true}``; a disabled HTTP element is
        # the legal retract.
        return [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]
    url = data.get("url")
    headers = data.get("headers")
    if not isinstance(url, str) or not url:
        return []
    header_map = headers if isinstance(headers, dict) else None
    return [
        acp_http_server(
            url,
            headers=header_map,
            name=str(data.get("name") or GATEWAY_SERVER_NAME),
        )
    ]


def _authorization_value(token: InboundToken) -> str:
    """RFC 6750 ``Authorization: Bearer <token>``. Scheme field stays lowercase."""
    scheme = (token.scheme or "bearer").strip() or "bearer"
    if scheme.lower() == "bearer":
        scheme = "Bearer"
    return f"{scheme} {token.token}".strip()


def _log_unattended_denied(principal: SessionPrincipal, *, reason: str) -> None:
    sel().log_api_access(
        caller="system",
        operation="agentcore.unattended_denied",
        outcome="denied",
        source="agentcore_gateway",
        resources=(
            f"session={principal.session_key} subject={principal.subject} " f"reason={reason}"
        ),
    )


def _login_gateway_spec() -> dict[str, Any] | None:
    """HTTPS Gateway URL for a login sidecar — never the workload SigV4 proxy.

    ``gateway_mcp_spec()`` rewrites to ``127.0.0.1`` when env posture is
    still ``workload`` (CFN leftover / pre-restart). kiro-cli cannot run
    an OAuth challenge against that listener. Prefer the configured
    https URL; fall back to an adapter spec only when it is https.
    """
    from kiro_crew.platform.governance import agentcore_gateway_url

    real = agentcore_gateway_url(current_context().governance)
    if real.startswith("https://"):
        return sanitize_gateway_spec({"url": real})
    sanitized = _gateway_spec_from_adapter()
    url = str((sanitized or {}).get("url") or "")
    if url.startswith("https://"):
        return sanitized
    return None


def _write_unattended_deny_sidecar(principal: SessionPrincipal, *, reason: str) -> None:
    """Retract Gateway for this session without writing a token."""
    payload: dict[str, Any] = {
        "name": GATEWAY_SERVER_NAME,
        "denied": True,
        "reason": reason,
        "session_key": principal.session_key,
        "subject": principal.subject,
    }
    _write_owner_only_json(inbound_sidecar_path(principal.session_key), payload)
    _log_unattended_denied(principal, reason=reason)


async def attach_gateway_inbound(principal: SessionPrincipal, *, agent: str = "") -> Path | None:
    """Attach login-posture Gateway for this session, or withhold it.

    Workload posture clears any leftover sidecar (IAM inbound, no JWT)
    unless this is an unattended user/OBO session without a vaulted owner
    token — then a deny sidecar retracts the agent-file Gateway.
    Login posture writes a ``0600`` sidecar when a URL-only spec exists:
    a vend'd JWT becomes the ``Authorization`` header; otherwise the
    sidecar is URL-only so kiro-cli can start its MCP OAuth challenge.
    Unattended login sessions never attach. ``agent`` is the selected
    crew identity (not a field on ``SessionPrincipal``) so a task
    profile that denies AgentCore withholds even when the surface
    profile permits it.
    """
    if not await asyncio.to_thread(_identity_on, principal.session_key, agent=agent):
        clear_inbound_sidecar(principal.session_key)
        return None
    posture = _current_posture()
    unattended = is_unattended_session(principal.session_key)
    if posture == "workload":
        if not _unattended_user_permitted():
            await asyncio.to_thread(
                _write_unattended_deny_sidecar, principal, reason="user_without_vault"
            )
            return None
        clear_inbound_sidecar(principal.session_key)
        return None
    if posture != "login":
        clear_inbound_sidecar(principal.session_key)
        return None
    if unattended:
        clear_inbound_sidecar(principal.session_key)
        _log_unattended_denied(principal, reason="login_unattended")
        return None

    from kiro_crew.cloud import iam as cloud_iam

    # Public probe defaults False: "no mismatch detected", not "IAM inbound
    # is impossible". A companion must override the live check.
    if cloud_iam.probe_instance_invoke_gateway():
        sel().log_api_access(
            caller="system",
            operation="agentcore.posture_mismatch",
            outcome="denied",
            source="agentcore_gateway",
            resources="InvokeGateway succeeded under login posture; inbound withheld",
        )
        clear_inbound_sidecar(principal.session_key)
        return None

    async def _vend() -> InboundToken | None:
        return await current_context().agent_identity.vend_gateway_inbound_token(principal)

    sanitized = _login_gateway_spec()
    if sanitized is None:
        sel().log_api_access(
            caller="system",
            operation="agentcore.gateway_inbound",
            outcome="denied",
            source="agentcore_gateway",
            resources=(
                f"session={principal.session_key} subject={principal.subject}; " "Gateway withheld"
            ),
        )
        clear_inbound_sidecar(principal.session_key)
        return None

    token = await async_safe_context_call(
        _vend,
        fallback=None,
        log_message="vend_gateway_inbound_token failed; attaching URL-only Gateway",
    )
    token = _live_inbound_token(token)
    payload: dict[str, Any] = {
        "name": GATEWAY_SERVER_NAME,
        "url": sanitized["url"],
        "session_key": principal.session_key,
        "subject": principal.subject,
    }
    if token is not None:
        payload["headers"] = {"Authorization": _authorization_value(token)}
        payload["expires_at"] = token.expires_at
        payload["audience"] = token.audience
        reason = "bearer"
    else:
        # No companion JWT: kiro-cli presents the URL, Gateway returns 401
        # + WWW-Authenticate, and Crew already surfaces Authorize.
        payload["oauth_challenge"] = True
        reason = REASON_OAUTH_CHALLENGE
    path = inbound_sidecar_path(principal.session_key)
    await asyncio.to_thread(_write_owner_only_json, path, payload)
    sel().log_api_access(
        caller="system",
        operation="agentcore.gateway_inbound",
        outcome="ok",
        source="agentcore_gateway",
        resources=(
            f"session={principal.session_key} subject={principal.subject} " f"reason={reason}"
        ),
    )
    logger.debug("agentcore inbound sidecar written for session %s", principal.session_key)
    return path


async def apply_staged_session_gateway(sessions: Any, session_key: str) -> bool:
    """Attach or retract Gateway after the session lease is held.

    Returns True when the live ACP child was recycled and the caller
    must cold-start so ``session/new`` reads the new sidecar. Raises
    :class:`GatewayCredentialError` when leftover credentials cannot
    be dropped — the turn must not continue with the old bearer.
    """
    held = _staged_for(session_key)
    staged = take_staged_gateway(session_key)
    if staged is None:
        # AutoNudge / webhook get_or_create never stages. Retract a
        # leftover human bearer rather than keep it for the unattended
        # turn.
        return await _apply_session_gateway(
            session_key,
            surface=None,
            raw_id=None,
            sessions=sessions,
            hold_lease=True,
        )
    surface, raw_id = staged
    agent = "" if held is None else held[3]
    recycled = await _apply_session_gateway(
        session_key,
        surface=surface,
        raw_id=raw_id,
        sessions=sessions,
        hold_lease=True,
        agent=agent,
    )
    if recycled:
        # Recycle retries get_or_create in this same task after take()
        # cleared the ContextVar. Put the bind back so the retry
        # attaches the same principal instead of retracting it.
        stage_session_gateway(session_key, surface, raw_id, agent=agent)
    return recycled


async def prepare_session_gateway(
    session_key: str,
    *,
    surface: str | None = None,
    raw_id: str | None = None,
    sessions: Any = None,
    agent: str = "",
) -> Path | None:
    """Stage this turn's Gateway bind; attach only when no session lease exists.

    ``SessionManager.get_or_create`` installs the staged sidecar under a
    per-key creation reservation before ``provider.start()`` so
    ``session/new`` cannot read a leftover bearer. A successful creator
    then consumes the staged bind without re-vending (a second vend
    would change the fingerprint and recycle the child just started).
    Existing-session / won-race paths still apply after the lease
    (recycle if the fingerprint changed). Pre-acquire attach/recycle is
    what let concurrent
    channel messages overwrite each other's JWT and abort an in-flight
    stream. Callers without ``get_or_create`` (CLI, unit tests) attach here.
    ``agent`` is the selected crew identity so a task profile that denies
    AgentCore withholds the sidecar even when the surface profile permits it.
    """
    if not session_key:
        return None
    stage_session_gateway(session_key, surface, raw_id, agent=agent)
    if sessions is not None and callable(getattr(sessions, "get_or_create", None)):
        return None
    await _apply_session_gateway(
        session_key,
        surface=surface,
        raw_id=raw_id,
        sessions=sessions,
        hold_lease=False,
        agent=agent,
    )
    path = inbound_sidecar_path(session_key)
    return path if path.exists() else None


async def _apply_session_gateway(
    session_key: str,
    *,
    surface: str | None,
    raw_id: str | None,
    sessions: Any,
    hold_lease: bool,
    agent: str = "",
) -> bool:
    """Write or drop the sidecar. Return True when the live child was recycled."""
    before = _sidecar_fingerprint(session_key)
    if surface and raw_id:
        from kiro_crew.platform.agent_identity import (
            apply_principal_annotation,
            derive_session_principal,
        )

        principal = derive_session_principal(
            surface=surface, raw_id=raw_id, session_key=session_key
        )
        principal = await apply_principal_annotation(principal)
        try:
            await attach_gateway_inbound(principal, agent=agent)
        except GatewayCredentialError:
            raise
        except Exception:
            clear_inbound_sidecar(session_key)
            if is_unattended_session(session_key):
                try:
                    await asyncio.to_thread(
                        _write_unattended_deny_sidecar, principal, reason="attach_failed"
                    )
                except GatewayCredentialError:
                    raise
                except Exception:
                    logger.debug(
                        "unattended deny sidecar write failed for %s",
                        session_key,
                        exc_info=True,
                    )
            logger.debug(
                "prepare_session_gateway attach failed; Gateway stays absent for %s",
                session_key,
                exc_info=True,
            )
        if _sidecar_fingerprint(session_key) != before:
            recycled = await _recycle_live_session(
                sessions, session_key, why="attach_changed", hold_lease=hold_lease
            )
            if not recycled:
                raise GatewayCredentialError(
                    f"cannot recycle Gateway credentials for session {session_key}"
                )
            return True
        return False
    live_principal = None
    getter = getattr(sessions, "get_principal", None) if sessions is not None else None
    if callable(getter):
        live_principal = getter(session_key)
    leftover = inbound_sidecar_state(session_key) != SIDECAR_ABSENT or live_principal is not None
    if leftover:
        recycled = await _recycle_live_session(
            sessions, session_key, why="unbound", hold_lease=hold_lease
        )
        if not recycled:
            raise GatewayCredentialError(
                f"cannot retract leftover Gateway credentials for busy session {session_key}"
            )
    clear_inbound_sidecar(session_key)
    return leftover
