"""Crew-owned OAuth for Connections on KAS (``dashboard.feature_previews.crew_oauth``).

The gateway runs the OAuth chain itself instead of handing it to the harness
(``docs/request-for-change`` RFC "Crew-owned OAuth for Connections", phase 1):

* :class:`SignIn` builds the consent URL (PKCE S256 + ``state``) and holds a
  one-shot callback listener on the provider's registered loopback port. The
  verifier lives in memory only.
* the code is swapped for tokens with the operator's ``client_secret``; the pair
  is stored in the user vault (:class:`TokenStore`), never in config, a spec, a
  transcript, a log or the sandbox.
* :func:`access_token` refreshes before expiry with a single-flight lock per
  connection. GitHub refresh tokens are single-use, so the new pair is committed
  to the vault (compare-and-set on the revision that was read) BEFORE the new
  access token is used, and a failed refresh never deletes the stored pair.
* :func:`disconnect` revokes upstream (best effort) and deletes the entry. A
  Google card whose grant another held Google card shares only deletes its entry.
* a DCR card (:data:`DCR_SLUGS`) has no operator client: Crew discovers its
  endpoints and registers its own client (:mod:`kiro_crew.connections.crew_oauth_dcr`).

Off by default (Settings > Feature previews). With the flag off :func:`manages` is
False everywhere and
nothing in this module runs. Standard library + aiohttp only; imported lazily.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The switch: the feature preview ``dashboard.feature_previews.crew_oauth``
#: (Settings > Feature previews), the only key read: a hand-edited
#: ``connections.crew_oauth`` turns nothing on.
FLAG_KEY = "crew_oauth"
#: Google's endpoints. Its revoke ends the user's whole grant for the client, every
#: product that client serves, so the slugs below share ``grant_group``.
_GOOGLE: dict[str, Any] = {
    "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
    "token": "https://oauth2.googleapis.com/token",
    "revoke": "https://oauth2.googleapis.com/revoke",
    "revoke_form": ("token",),
    # Without both Google returns no refresh token (or none on a repeat consent).
    "authorize_params": {"access_type": "offline", "prompt": "consent"},
    # ``openid`` makes the token endpoint return an id_token whose ``sub`` names
    # the account, so two cards share a grant only on the same account.
    "extra_scopes": ("openid",),
    "grant_group": "google",
    # Google revokes across every client of one Cloud project, so sharing is
    # judged by the project number that leads each client id.
    "grant_by_project": True,
}
#: The visible DCR cards Crew can own; their endpoints come from discovery.
DCR_SLUGS = frozenset({"atlassian", "notion", "linear", "stripe", "vercel", "gitlab", "ticktick"})

#: Per-provider OAuth endpoints, fixed per vendor (not discovered via RFC 8414).
#: ``revoke_form`` names the form fields of a POST revoke (``token`` is the refresh
#: token, else the access token); without it the revoke is GitHub's token-scoped
#: DELETE. ``authorize_params`` are added to the consent URL. Slugs with the same
#: ``grant_group``, client id and account share one upstream grant.
#: ``extra_scopes`` are appended to the registry's scopes.
_ENDPOINTS: dict[str, dict[str, Any]] = {
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        # Token-scoped: revokes only Crew's own token, never the whole app grant
        # another connection of the same account may share.
        "revoke": "https://api.github.com/applications/{client_id}/token",
    },
    "asana": {
        "authorize": "https://app.asana.com/-/oauth_authorize",
        "token": "https://app.asana.com/-/oauth_token",
        "revoke": "https://app.asana.com/-/oauth_revoke",
        "revoke_form": ("token", "client_id", "client_secret"),
    },
    **{
        slug: _GOOGLE
        for slug in (
            "google-drive",
            "google-docs",
            "google-sheets",
            "google-slides",
            "gmail",
            "google-calendar",
        )
    },
}
#: The slugs Crew can own: those with endpoints, and the DCR cards.
SUPPORTED_SLUGS = frozenset(_ENDPOINTS) | DCR_SLUGS
#: Prefix of the key an owner-added remote MCP server's grant is held under.
CUSTOM_PREFIX = "custom-"
_CUSTOM_DIGEST_CHARS = 20
#: Refresh this long before the recorded expiry.
_REFRESH_SKEW_SECS = 120.0
#: How long one consent may stay open.
SIGN_IN_TTL_SECS = 600.0
_HTTP_TIMEOUT_SECS = 20.0
#: Refresh errors that mean the stored refresh token is dead (after one re-read).
_DEAD_GRANT_ERRORS = frozenset({"bad_refresh_token", "invalid_grant", "unauthorized_client"})

_refresh_locks: dict[str, asyncio.Lock] = {}
_startup_locks: dict[str, asyncio.Lock] = {}
_group_locks: dict[str, asyncio.Lock] = {}


def _group_lock(slug: str) -> asyncio.Lock:
    """The lock every slug of *slug*'s grant group shares (the slug's own without one)."""
    group = str(_ENDPOINTS.get(slug, {}).get("grant_group") or slug)
    return _group_locks.setdefault(group, asyncio.Lock())


class CrewOAuthError(Exception):
    """A failure with a coarse, value-free code (``needs_auth``, ``exchange_failed``…)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def enabled() -> bool:
    """Whether the preview (:data:`FLAG_KEY`) is ``true``. Blocking.

    Reads the loaded config (``config.local.json`` overlay included), the same
    value the dashboard's switch shows.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.edition_locks import feature_previews

    try:
        previews = feature_previews(KiroCrewConfig.load().dashboard.feature_previews)
    except Exception:  # noqa: BLE001 -- an unreadable config reads as off
        return False
    return isinstance(previews, dict) and previews.get(FLAG_KEY) is True


def serves_backend(backend: str) -> bool:
    """Whether a session on *backend* reaches Crew's sign-in through the bridge.

    Never kiro-cli: it runs the vendor's OAuth itself, so its native grant keeps
    working beside a bridged session.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CREW_OAUTH_BRIDGE

    return backend in ACP_BACKENDS_CREW_OAUTH_BRIDGE


def _chat_backend() -> str | None:
    """The configured chat backend, ``None`` when the config cannot be read. Blocking."""
    from kiro_crew.config import KiroCrewConfig

    try:
        return str(KiroCrewConfig.load().agent.acp_backend)
    except Exception:  # noqa: BLE001 -- an unreadable config keeps today's custody
        return None


def _chat_backend_bridged() -> bool:
    """Whether ordinary chats run on a bridged backend. Blocking."""
    backend = _chat_backend()
    return backend is not None and serves_backend(backend)


def strips_spec_secret(slug: str) -> bool:
    """Whether the agent spec may drop *slug*'s pre-registered client secret. Blocking.

    The spec is what kiro-cli reads, and kiro-cli runs the vendor's OAuth itself,
    so the secret is dropped only when nobody reads it: chats on KAS (which
    discards the block), or a deployment where no kiro-cli session can start.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, selectable_backends

    if not custody_on(slug):
        return False
    return _chat_backend() == ACP_BACKEND_KAS or ACP_BACKEND_KIRO not in selectable_backends()


def custom_key(name: str, url: str) -> str:
    """The key an owner-added server's grant is held under: its name AND url.

    A re-pointed url gets a new key, so a token issued for one url is never sent
    to another.
    """
    digest = hashlib.sha256(f"{name}\n{url}".encode("utf-8", "surrogatepass")).hexdigest()
    return CUSTOM_PREFIX + digest[:_CUSTOM_DIGEST_CHARS]


def is_custom_key(slug: str) -> bool:
    """Whether *slug* is a :func:`custom_key`, never a registry slug."""
    tail = slug[len(CUSTOM_PREFIX) :]
    return (
        slug.startswith(CUSTOM_PREFIX)
        and len(tail) == _CUSTOM_DIGEST_CHARS
        and all(c in "0123456789abcdef" for c in tail)
    )


def _held_key(slug: str) -> bool:
    return slug in SUPPORTED_SLUGS or is_custom_key(slug)


def custody_on(slug: str) -> bool:
    """Whether Crew's sign-in may serve *slug* on this gateway at all. Blocking.

    The flag alone, whatever the chat backend: which session reaches it is
    :func:`serves_backend`'s question, asked per session.
    """
    return _held_key(slug) and enabled()


def _is_registry_url(url: Any) -> bool:
    """Whether *url* is a registry provider's MCP endpoint. Blocking.

    Such an entry belongs to that provider's card (its client, port and vault
    key), whatever name it was added under: the owner-server path never claims it.
    """
    from kiro_crew.connections.registry import get_all_registry_providers

    if not isinstance(url, str):
        return False
    wanted = url.rstrip("/")
    return any(str(p["mcp_url"]).rstrip("/") == wanted for p in get_all_registry_providers())


def signs_in_custom(entry: Any) -> bool:
    """Whether an owner-added server's dashboard sign-in runs through Crew. Blocking.

    The caller has proved *entry* is the owner's recorded remote server. Needs the
    flag and a bridged chat backend, as a card's Connect does; an entry that sends
    its own credential header keeps its own path.
    """
    return (
        isinstance(entry, dict)
        and entry.get("type") != "sse"
        and not _is_registry_url(entry.get("url"))
        and not has_credential_header(entry.get("headers"))
        and enabled()
        and _chat_backend_bridged()
    )


def _custom_slug(name: str, entry: Any) -> str:
    """The :func:`custom_key` when Crew holds this owner-added entry's grant, else ``""``.

    Signing in through Crew is the opt-in: until then the entry keeps the
    harness's own OAuth. The owner record (sealed ``config.json``) must name this
    exact url, so a server an agent added or re-pointed is never bridged.
    """
    from kiro_crew.config.loader import read_config_for_update
    from kiro_crew.connections.owner_servers import is_owner_server, is_signable_name, remote_url

    url = remote_url(entry)
    # The bridge speaks streamable HTTP only: a legacy SSE server keeps its own path.
    if (
        url is None
        or entry.get("type") == "sse"
        or not is_signable_name(name)
        or _is_registry_url(url)
    ):
        return ""
    key = custom_key(name, url)
    if holds_grant(key) is not True:
        return ""
    try:
        config = read_config_for_update()
    except Exception:  # noqa: BLE001 -- an unreadable record proves nothing
        return ""
    return key if is_owner_server(config, name, entry) else ""


def manages(slug: str) -> bool:
    """Whether Crew owns *slug*'s OAuth for the chat backend: Connect, status. Blocking.

    Needs the flag AND a bridged chat backend: on kiro-cli the native grant keeps
    working as today, so the Connections card must not take custody away from it.
    """
    return custody_on(slug) and _chat_backend_bridged()


#: Header-name words that mark a header as carrying a credential.
_CREDENTIAL_HEADER_WORDS = ("auth", "token", "key", "secret", "cookie", "session")


def has_credential_header(headers: Any) -> bool:
    """Whether *headers* carries its own credential (``Authorization``, an API key…)."""
    if not isinstance(headers, dict):
        return False
    return any(
        isinstance(k, str) and any(w in k.lower() for w in _CREDENTIAL_HEADER_WORDS)
        for k in headers
    )


def routed_slug(name: str, entry: Any, provider_for_server: Any, backend: str | None = None) -> str:
    """The slug when Crew's sign-in serves this entry, else ``""``. Blocking.

    A registry provider's own entry (slug AND url match), or an owner-added
    remote server whose grant Crew holds (:func:`_custom_slug`), for a session on
    *backend* (:func:`custody_on` and :func:`serves_backend`), or for the chat
    backend (:func:`manages`) when none is given. An entry that sends its own
    credential header keeps its own path.
    """
    if has_credential_header(entry.get("headers")):
        return ""
    if name in SUPPORTED_SLUGS:
        provider = (
            _dcr_provider(name, entry) if name in DCR_SLUGS else provider_for_server(name, entry)
        )
        if provider is None:
            return ""
        slug = str(provider["slug"])
    else:
        slug = _custom_slug(name, entry)
        if not slug:
            return ""
    if backend is None:
        return slug if manages(slug) else ""
    if not (custody_on(slug) and serves_backend(backend)):
        return ""
    # A session is bridged only when Crew can hold the grant: the card offers
    # Crew's Connect (a bridged chat backend), or Crew already holds the pair.
    # Otherwise the bridge could only ever answer "reconnect it".
    return slug if _chat_backend_bridged() or holds_grant(slug) is True else ""


def bridges_chat_sessions() -> bool:
    """Whether an ordinary chat session may be bridged. Blocking.

    A warm-pool process is projected before it has a session key, so it carries
    no bridge; the pool is skipped while this holds.
    """
    return enabled() and _chat_backend_bridged()


def _dcr_provider(name: str, entry: Any) -> Any:
    """The DCR registry provider *entry* is an instance of (slug AND url), else ``None``."""
    from kiro_crew.connections.registry import get_provider, is_preregistered

    provider = get_provider(name)
    if provider is None or is_preregistered(provider) or entry.get("url") != provider["mcp_url"]:
        return None
    return provider


def configured_entry_routed(slug: str) -> bool | None:
    """:func:`routed_slug` for *slug*'s configured entry; ``None`` with none. Blocking."""
    from kiro_crew.connections.oauth_clients import provider_for_server
    from kiro_crew.mcp_discovery import list_servers

    for server in list_servers():
        if server.name == slug and server.url:
            headers = server.headers if isinstance(server.headers, dict) else {}
            entry = {"url": server.url, "headers": headers}
            return bool(routed_slug(slug, entry, provider_for_server))
    return None


def owns(slug: str) -> bool:
    """Whether Crew's sign-in serves *slug* on this gateway: Connect, status. Blocking."""
    return manages(slug) and configured_entry_routed(slug) is not False


def holds_grant(slug: str) -> bool | None:
    """Whether Crew's vault still holds *slug*'s pair, whatever the flag says. Blocking.

    ``None`` when the vault could not be read: not a confirmed absence, so a
    Disconnect still attempts the cleanup and reports it if that fails.
    """
    if not _held_key(slug):
        return False
    try:
        return TokenStore().present(slug)
    except Exception:  # noqa: BLE001 -- unknown, never "absent"
        return None


def token_entry_name(slug: str) -> str:
    """The vault entry holding *slug*'s token pair."""
    return f"CONNECTIONS_{slug.upper().replace('-', '_')}_OAUTH_TOKENS"


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str | None
    expires_at: float | None
    scope: str
    #: The vendor's account id (OIDC ``sub``) when the provider returns one.
    account: str | None = None
    #: The client id that issued this pair (not the one configured now).
    client: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "access": self.access,
                "refresh": self.refresh,
                "expires_at": self.expires_at,
                "scope": self.scope,
                "account": self.account,
                "client": self.client,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> Tokens | None:
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        access = doc.get("access") if isinstance(doc, dict) else None
        if not isinstance(access, str) or not access:
            return None
        refresh = doc.get("refresh")
        expires = doc.get("expires_at")
        account = doc.get("account")
        client = doc.get("client")
        return cls(
            access=access,
            refresh=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=float(expires) if isinstance(expires, (int, float)) else None,
            scope=str(doc.get("scope") or ""),
            account=account if isinstance(account, str) and account else None,
            client=client if isinstance(client, str) and client else None,
        )

    def expiring(self, now: float) -> bool:
        # A pair with no refresh token (TickTick) has nothing to renew early: it is
        # served up to its recorded expiry.
        skew = _REFRESH_SKEW_SECS if self.refresh else 0.0
        return self.expires_at is not None and self.expires_at - skew <= now


def tokens_from_response(doc: dict[str, Any], now: float) -> Tokens:
    """A token-endpoint answer as :class:`Tokens`, or ``exchange_failed``."""
    access = doc.get("access_token")
    if not isinstance(access, str) or not access or doc.get("error"):
        raise CrewOAuthError(str(doc.get("error") or "exchange_failed")[:64])
    expires_in = doc.get("expires_in")
    refresh = doc.get("refresh_token")
    return Tokens(
        access=access,
        refresh=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=now + float(expires_in) if isinstance(expires_in, (int, float)) else None,
        scope=str(doc.get("scope") or ""),
        account=_id_token_subject(doc.get("id_token")),
    )


def _id_token_subject(id_token: Any) -> str | None:
    """The ``sub`` of an id_token, or ``None``. Never logged.

    Not signature-checked: the id_token came straight from the token endpoint over
    TLS, which OpenID Connect Core 3.1.3.7 accepts in place of the signature. It is
    used only to tell accounts apart, never to authenticate anyone.
    """
    import base64

    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    try:
        doc = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    sub = doc.get("sub") if isinstance(doc, dict) else None
    return sub if isinstance(sub, str) and sub else None


class TokenStore:
    """One vault entry per connection; every field commits in one atomic replace."""

    def __init__(self, vault: Any | None = None) -> None:
        if vault is None:
            from kiro_crew.config.paths import config_dir
            from kiro_crew.secrets import SecretVault

            vault = SecretVault(config_dir())
        self._vault = vault

    def read(self, slug: str) -> tuple[Tokens, str] | None:
        held = self._vault.get_with_revision(token_entry_name(slug))
        if held is None:
            return None
        value, revision = held
        tokens = Tokens.from_json(value.reveal())
        return (tokens, revision) if tokens is not None else None

    def present(self, slug: str) -> bool:
        return token_entry_name(slug) in self._vault.list_names()

    def commit(self, slug: str, tokens: Tokens, expected: str | None) -> str | None:
        """Write *tokens* only over *expected* (``None``: only if absent)."""
        return self._vault.set_if_revision_sync(token_entry_name(slug), tokens.to_json(), expected)

    def replace(self, slug: str, tokens: Tokens) -> None:
        """A fresh sign-in replaces whatever was stored."""
        self._vault.set_sync(token_entry_name(slug), tokens.to_json())

    def delete(self, slug: str) -> None:
        self._vault.delete_sync(token_entry_name(slug))

    async def delete_if(self, slug: str, revision: str) -> bool:
        """Delete only while the stored entry still has *revision*."""
        return bool(await self._vault.delete_if_revision(token_entry_name(slug), revision))


def _client(slug: str) -> Any:
    """The operator's resolved client (id + secret), or ``client_not_configured``."""
    from kiro_crew.config.loader import read_config_for_update
    from kiro_crew.config.paths import config_dir
    from kiro_crew.connections.oauth_clients import resolve_oauth_client
    from kiro_crew.connections.registry import get_provider
    from kiro_crew.secrets import SecretVault

    provider = get_provider(slug)
    resolved = (
        resolve_oauth_client(
            provider, config=read_config_for_update(), vault=SecretVault(config_dir())
        )
        if provider is not None
        else None
    )
    if resolved is None or not resolved.client_secret:
        raise CrewOAuthError("client_not_configured")
    return resolved


async def _post_token(slug: str, form: dict[str, str]) -> dict[str, Any]:
    """POST the token endpoint; the decoded JSON answer. Never logs a value."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _ENDPOINTS[slug]["token"],
                data=form,
                headers={"Accept": "application/json"},
                allow_redirects=False,
            ) as resp:
                if resp.status >= 400:
                    # Google and Asana answer a dead refresh token with a 400 whose
                    # body names the error (``invalid_grant``); keep that code.
                    try:
                        body = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        body = None
                    error = body.get("error") if isinstance(body, dict) else None
                    if isinstance(error, str) and error:
                        raise CrewOAuthError(error[:64])
                    raise CrewOAuthError(f"token_http_{resp.status}")
                try:
                    doc = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    raise CrewOAuthError("token_bad_answer") from None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        # Unreachable token endpoint: a coded failure every caller already handles.
        raise CrewOAuthError("token_unreachable") from None
    if not isinstance(doc, dict):
        raise CrewOAuthError("token_bad_answer")
    return doc


def authorize_url(slug: str, client_id: str, redirect: str, state: str, challenge: str) -> str:
    from kiro_crew.connections.registry import get_provider

    provider = get_provider(slug)
    scopes = (provider.get("recommended_scopes") if provider else None) or []
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scopes = [*scopes, *_ENDPOINTS[slug].get("extra_scopes", ())]
    if scopes:
        query["scope"] = " ".join(str(s) for s in scopes)
    query.update(_ENDPOINTS[slug].get("authorize_params", {}))
    return f"{_ENDPOINTS[slug]['authorize']}?{urllib.parse.urlencode(query)}"


class SignIn:
    """One pending consent: the listener, the verifier and ``state``, in memory only.

    Shaped for a mint row's ``client`` slot (:mod:`kiro_crew.connections.mint`):
    ``shutdown`` closes the listener, so the existing row lifecycle (cancel,
    supersede) reaches it. No process backs this row, so it offers no liveness
    probe; its own watcher writes the row's terminal state (granted, failed,
    expired after :data:`SIGN_IN_TTL_SECS`).
    """

    def __init__(self, slug: str, project_dirs: Any = None, custom: Any = None) -> None:
        self.slug = slug
        # An owner-added server (``{"slug": name, "mcp_url", "entry"}``) signed in
        # under its :func:`custom_key`; ``None`` for a registry card.
        self._custom = custom
        # The open checkouts the harness-grant census reads; ``None``: unknown.
        self._project_dirs = project_dirs
        self._state = secrets.token_urlsafe(32)
        # The one PKCE S256 implementation the product carries.
        from kiro_crew.auth.login.portal import generate_code_challenge, generate_code_verifier

        self._verifier = generate_code_verifier()
        self._challenge = generate_code_challenge(self._verifier)
        self._done: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._runner: Any = None
        self.url = ""
        self.bound_port = 0
        self._closing = False
        self._write: asyncio.Future[None] | None = None
        self._dcr: Any = None
        self._listening = False

    async def start(self) -> str:
        """Bind the callback listener and return the consent URL."""
        from kiro_crew.connections.registry import CALLBACK_PATH, get_provider, redirect_uri

        if self.slug in DCR_SLUGS or self._custom is not None:
            from kiro_crew.connections import crew_oauth_dcr as dcr

            if self._custom is None:
                self._dcr = await dcr.prepare(self.slug)
            else:
                from kiro_crew.mcp_utils import kiro_entry_scopes

                entry = self._custom["entry"]
                self._dcr = await dcr.prepare_custom(
                    self.slug,
                    str(self._custom["mcp_url"]),
                    entry.get("oauth"),
                    # Either spelling, as the custom-server editor stores them.
                    scopes=kiro_entry_scopes(entry, server=str(self._custom["slug"])),
                )
            self._redirect = self._dcr.client.redirect_uri
            own = dcr.own_listener(self._redirect)
            if own is not None:
                # The owner's app registered its own loopback redirect: bind that
                # port, for this one consent only.
                await self._bind(*own)
            else:
                # One port for every DCR sign-in (or the gateway's own public
                # route); ``state`` routes the callback here.
                self.bound_port = await dcr.listen(
                    self._state,
                    self._callback,
                    slug=self.slug,
                    loopback=not self._redirect.startswith("https://"),
                )
                self._listening = True
            self.url = dcr.authorize_url(self._dcr, self._state, self._challenge)
            return self.url
        provider = get_provider(self.slug)
        client = await asyncio.to_thread(_client, self.slug)
        redirect = redirect_uri(provider) if provider else None
        if not redirect:
            raise CrewOAuthError("client_not_configured")
        auth = provider["auth"]  # type: ignore[index]
        await self._bind(int(auth["redirect_port"]), CALLBACK_PATH)
        self._client = client
        self._redirect = redirect
        self.url = authorize_url(
            self.slug, client.client_id, redirect, self._state, self._challenge
        )
        return self.url

    async def _bind(self, port: int, path: str) -> None:
        """Hold this consent's own callback listener on loopback *port*."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get(path, self._callback)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # Loopback only, the exact host and port the operator registered, and only
        # while this one consent is pending (RFC 4.7).
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        try:
            await site.start()
        except OSError:
            await self._runner.cleanup()
            self._runner = None
            raise CrewOAuthError("callback_port_busy") from None
        self.bound_port = int(self._runner.addresses[0][1])

    async def _callback(self, request: Any) -> Any:
        from aiohttp import web

        state = request.query.get("state", "")
        code = request.query.get("code", "")
        # Bytes: compare_digest raises on a non-ASCII str, which would answer 500.
        if self._done.done() or not secrets.compare_digest(state.encode(), self._state.encode()):
            return web.Response(status=400, text="This sign-in link is not the current one.")
        if self._dcr is not None:
            from kiro_crew.connections.crew_oauth_dcr import iss_ok

            if not iss_ok(self._dcr.client, request.query.get("iss")):
                # RFC 9207 mix-up defence: the answer came from another server.
                self._done.set_exception(CrewOAuthError("issuer_mismatch"))
                return web.Response(status=400, text="This sign-in answer is not trusted.")
        if not code:
            self._done.set_exception(CrewOAuthError("consent_declined"))
            return web.Response(text="Sign-in was not completed. You can close this tab.")
        self._done.set_result(code)
        return web.Response(text="Signed in. You can close this tab and return to Kiro Crew.")

    async def finish(self, store: TokenStore | None = None) -> None:
        """Wait for the callback, swap the code, store the pair. Raises :class:`CrewOAuthError`."""
        try:
            code = await asyncio.wait_for(asyncio.shield(self._done), SIGN_IN_TTL_SECS)
        except asyncio.TimeoutError:
            raise CrewOAuthError("consent_expired") from None
        # The swap and the write outlive a cancel of this coroutine, so they are
        # kept and drained by shutdown(): a Disconnect that ends this sign-in never
        # reads the vault before the write it raced has landed.
        target = store or TokenStore()

        async def _swap_and_write() -> None:
            # Under the grant-group lock too: a sibling's Disconnect never revokes
            # the shared grant between this swap and its write.
            async with _group_lock(self.slug), _refresh_locks.setdefault(self.slug, asyncio.Lock()):
                # Cancelled while queued: never swap the code. Once swapped, the
                # pair is always stored, so a Disconnect draining this write
                # revokes it instead of leaving unseen tokens live upstream.
                if self._closing:
                    raise CrewOAuthError("cancelled")
                if self._dcr is not None:
                    doc = await self._dcr_exchange(code)
                    issued_to = self._dcr.client.client_id
                else:
                    doc = await _post_token(
                        self.slug,
                        {
                            "grant_type": "authorization_code",
                            "client_id": self._client.client_id,
                            "client_secret": self._client.client_secret,
                            "code": code,
                            "redirect_uri": self._redirect,
                            "code_verifier": self._verifier,
                        },
                    )
                    issued_to = self._client.client_id
                tokens = dataclasses.replace(
                    tokens_from_response(doc, time.time()), client=issued_to
                )
                if self._custom is not None:
                    from kiro_crew.connections.crew_oauth_dcr import keep_client

                    # The client and the pair it issued land in one vault write.
                    await asyncio.to_thread(
                        keep_client, self.slug, self._dcr.client, tokens, target._vault
                    )
                else:
                    await asyncio.to_thread(target.replace, self.slug, tokens)

        self._write = asyncio.ensure_future(_swap_and_write())
        await asyncio.shield(self._write)
        if self._dcr is not None:
            # One custody: the harness's own grant for this URL goes (RFC §4.2).
            from kiro_crew.connections.crew_oauth_dcr import drop_harness_grants

            await drop_harness_grants(self.slug, self._project_dirs)

    async def _dcr_exchange(self, code: str) -> dict[str, Any]:
        """The DCR code swap; a client the vendor does not know is forgotten."""
        from kiro_crew.connections.crew_oauth_dcr import ClientStore, exchange, forget_client

        try:
            return await exchange(self._dcr.client, code, self._verifier)
        except CrewOAuthError as exc:
            if exc.code in ("invalid_client", "unauthorized_client"):
                # The next Connect registers again instead of reusing the dead one.
                # Only the stored client is forgotten: an owner's replacement that
                # was never stored must not take the working one with it.
                stored = (await asyncio.to_thread(ClientStore().read, self.slug))[0]
                if self._custom is None or stored == self._dcr.client:
                    await forget_client(self.slug)
            raise

    async def shutdown(self) -> None:
        """Close the listener and wait for any token write already started."""
        self._closing = True
        if not self._done.done():
            self._done.cancel()
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        if self._listening:
            from kiro_crew.connections.crew_oauth_dcr import unlisten

            self._listening = False
            await unlisten(self._state)
        write = self._write
        if write is not None and not write.done():
            await asyncio.wait({write})


async def _refresh(slug: str, store: TokenStore, held: tuple[Tokens, str]) -> Tokens:
    """Rotate the pair once; commit before returning the new access token."""
    tokens, revision = held
    if not tokens.refresh:
        if tokens.expiring(time.time()):
            # Expired and unrenewable: drop it, so the card reads Not connected
            # and offers Connect again (the registered client stays).
            await store.delete_if(slug, revision)
        raise CrewOAuthError("needs_auth")
    from kiro_crew.connections import crew_oauth_dcr as dcr

    # A DCR card's or an owner-added server's client lives in the vault.
    discovered = slug in DCR_SLUGS or is_custom_key(slug)
    client = None if discovered else await asyncio.to_thread(_client, slug)
    try:
        if client is None:
            doc = await dcr.refresh(slug, tokens.refresh)
        else:
            doc = await _post_token(
                slug,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                },
            )
        fresh = tokens_from_response(doc, time.time())
    except CrewOAuthError as exc:
        # Another writer (a second gateway on this data home) may have rotated the
        # single-use token first: re-read once before calling the grant dead.
        again = await asyncio.to_thread(store.read, slug)
        if again is not None and again[1] != revision:
            return again[0]
        if client is None and exc.code == "invalid_client":
            # The vendor dropped Crew's registration: the next Connect registers
            # again, and the pair bound to the dead client goes with it.
            await dcr.forget_client(slug)
            await store.delete_if(slug, revision)
            raise CrewOAuthError("needs_auth") from None
        code = "needs_auth" if exc.code in _DEAD_GRANT_ERRORS else exc.code
        raise CrewOAuthError(code) from None
    # Google and Asana keep their refresh token; the account and issuing client
    # never change on a refresh.
    fresh = dataclasses.replace(
        fresh,
        refresh=fresh.refresh or tokens.refresh,
        account=fresh.account or tokens.account,
        client=tokens.client,
    )
    # Persist BEFORE use: the old refresh token is already spent server-side.
    if await asyncio.to_thread(store.commit, slug, fresh, revision) is None:
        again = await asyncio.to_thread(store.read, slug)
        if again is None:
            raise CrewOAuthError("needs_auth")
        return again[0]
    _audit("connections_crew_oauth_refresh", "ok", slug)
    return fresh


async def access_token(slug: str, *, force: bool = False, store: TokenStore | None = None) -> str:
    """A usable access token for *slug*; refreshes single-flight when it is expiring.

    *force* refreshes even before expiry (the vendor answered 401). Raises
    ``needs_auth`` when nothing usable is stored.
    """
    store = store or TokenStore()
    held = await asyncio.to_thread(store.read, slug)
    if held is None:
        raise CrewOAuthError("needs_auth")
    if not force and not held[0].expiring(time.time()):
        return held[0].access
    lock = _refresh_locks.setdefault(slug, asyncio.Lock())
    async with lock:
        current = await asyncio.to_thread(store.read, slug)
        if current is None:
            raise CrewOAuthError("needs_auth")
        if current[1] != held[1] or (not force and not current[0].expiring(time.time())):
            # Someone rotated it while this caller waited for the lock.
            return current[0].access
        try:
            return (await _refresh(slug, store, current)).access
        except CrewOAuthError as exc:
            _audit("connections_crew_oauth_refresh", "error", slug, exc.code)
            raise


#: How many replacement pairs one Disconnect chases before it gives up revoking.
_DISCONNECT_ROUNDS = 3


def _shares_upstream_grant(slug: str, store: TokenStore) -> bool:
    """Whether another held slug shares *slug*'s upstream grant. Blocking.

    Same ``grant_group``, the same issuing client id AND the same account, both as
    stored with each pair: revoking would end that one too. An unknown account or
    client, or anything unreadable, counts as not shared, so the revoke happens.
    """
    # A DCR card has no entry here: each registers its own client, so none shares.
    group = _ENDPOINTS.get(slug, {}).get("grant_group")
    if not group:
        return False
    try:
        held = store.read(slug)
        if held is None or not held[0].account or not held[0].client:
            return False
        mine = held[0]
        for other in sorted(SUPPORTED_SLUGS - {slug}):
            if _ENDPOINTS.get(other, {}).get("grant_group") != group:
                continue
            theirs = store.read(other)
            if (
                theirs is not None
                and theirs[0].account == mine.account
                and _issuer(other, theirs[0].client) == _issuer(slug, mine.client)
            ):
                return True
        return False
    except Exception:  # noqa: BLE001 -- unknown reads as not shared: revoke
        return False


def _issuer(slug: str, client: str | None) -> str | None:
    """What one upstream revoke reaches: the client id, or its Cloud project number."""
    if client and _ENDPOINTS[slug].get("grant_by_project"):
        return client.split("-", 1)[0]
    return client


async def _revoke(slug: str, tokens: Tokens) -> bool:
    """Upstream revoke of *tokens* in the vendor's shape; best effort, never raises."""
    import aiohttp

    if slug in DCR_SLUGS or is_custom_key(slug):
        from kiro_crew.connections.crew_oauth_dcr import revoke

        return await revoke(slug, tokens)
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
        form_fields = _ENDPOINTS[slug].get("revoke_form")
        # A token-only revoke (Google) needs no client, so a removed client record
        # never blocks it.
        client: Any = (
            await asyncio.to_thread(_client, slug)
            if not form_fields or set(form_fields) - {"token"}
            else None
        )
        if form_fields:
            values = {"token": tokens.refresh or tokens.access}
            if client is not None:
                values.update(client_id=client.client_id, client_secret=client.client_secret)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    _ENDPOINTS[slug]["revoke"],
                    data={k: values[k] for k in form_fields},
                    allow_redirects=False,
                ) as resp:
                    return resp.status == 200
        auth = aiohttp.BasicAuth(client.client_id, client.client_secret)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(
                _ENDPOINTS[slug]["revoke"].format(
                    client_id=urllib.parse.quote(client.client_id, safe="")
                ),
                json={"access_token": tokens.access},
                auth=auth,
                headers={"Accept": "application/vnd.github+json"},
                allow_redirects=False,
            ) as resp:
                return resp.status in (204, 404)
    except (CrewOAuthError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


async def disconnect(slug: str, store: TokenStore | None = None) -> dict[str, bool]:
    """Revoke upstream (best effort) and delete the stored pair.

    Holds the same per-connection lock refresh and sign-in writes take, and
    deletes only the revision it revoked: a pair that replaced it meanwhile is
    re-read and revoked too, so no stored token is dropped unrevoked. When another
    held slug shares the upstream grant (:func:`_shares_upstream_grant`) nothing is
    revoked and only this entry is deleted: ``shared`` is then True.
    """
    store = store or TokenStore()
    removed = False
    revoked = True
    seen = False
    # Slugs sharing a grant group disconnect one at a time, so two overlapping
    # Disconnects can never both see the other as the sharer and skip the revoke.
    async with (
        _group_lock(slug),
        _refresh_locks.setdefault(slug, asyncio.Lock()),
    ):
        if await asyncio.to_thread(_shares_upstream_grant, slug, store):
            # Another held card uses the same upstream grant: only the local copy goes.
            if await asyncio.to_thread(store.present, slug):
                await asyncio.to_thread(store.delete, slug)
                removed = True
            _audit("connections_crew_oauth_disconnect", "ok", slug, "revoke_kept_shared")
            return {"removed": removed, "revoked": False, "shared": True}
        for _ in range(_DISCONNECT_ROUNDS):
            held = await asyncio.to_thread(store.read, slug)
            if held is None:
                # An entry Crew cannot parse is never used, but must not outlive Disconnect.
                if await asyncio.to_thread(store.present, slug):
                    await asyncio.to_thread(store.delete, slug)
                    removed = True
                break
            seen = True
            revoked = await _revoke(slug, held[0]) and revoked
            if await store.delete_if(slug, held[1]):
                removed = True
                break
        else:
            # Still being replaced: drop it anyway and report the revoke as unproven.
            await asyncio.to_thread(store.delete, slug)
            removed, revoked = True, False
    revoked = revoked and seen
    _audit("connections_crew_oauth_disconnect", "ok" if revoked or not removed else "partial", slug)
    return {"removed": removed, "revoked": revoked}


def _audit(operation: str, outcome: str, slug: str, detail: str = "") -> None:
    """One SEL record and one gateway.log line; a provider and a coarse code, never a value."""
    logger.info(
        "Crew OAuth %s for %s: %s%s", operation, slug, outcome, f" ({detail})" if detail else ""
    )
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=f"provider:{slug}" + (f" reason={detail}" if detail else ""),
        )
    except Exception:  # noqa: BLE001 -- auditing never breaks the flow
        logger.debug("crew oauth audit failed", exc_info=True)


async def start_mint_row(
    slug: str, project_dirs: Any = None, custom: Any = None, on_granted: Any = None
) -> str:
    """Run a Crew-owned sign-in through the Connections card's mint row; the row token.

    The card's contract is unchanged: it polls ``GET /api/connections/mint`` for
    ``waiting`` + ``oauth_url``, then ``granted`` / ``failed`` / ``expired``. The row
    holds the :class:`SignIn` as its ``client`` and the wait as its ``watcher``,
    so Cancel, a superseding Connect and the dead-holder check all reach them.
    One start per provider at a time: a superseding Connect waits until the
    attempt it replaces has registered its listener, so it can dispose of it.

    *custom* is an owner-added server (``{"slug": name, "mcp_url", "entry"}``):
    its row is keyed by the server name the MCP Servers page polls, and its grant
    by *slug*, the :func:`custom_key`. *on_granted* (synchronous, no arguments,
    run on the loop) runs once the pair is stored.
    """
    async with _startup_locks.setdefault(slug, asyncio.Lock()):
        return await _start_mint_row(slug, project_dirs, custom, on_granted)


async def _start_mint_row(
    slug: str, project_dirs: Any, custom: Any = None, on_granted: Any = None
) -> str:
    from kiro_crew.connections import mint

    row = str(custom["slug"]) if custom is not None else slug
    token, prior = await mint.reserve_mint_row(row)
    if prior is not None:
        await mint._dispose_mint(prior)
    sign_in = SignIn(slug, project_dirs, custom)
    try:
        url = await sign_in.start()
    except CrewOAuthError as exc:
        await _set_row(row, token, {"state": "failed", "reason": f"crew_oauth_{exc.code}"})
        _audit("connections_crew_oauth_sign_in", "error", slug, exc.code)
        return token
    from kiro_crew.security import oauth_url_contains_credential

    # ``state`` and the PKCE challenge are this process's own random draws, which a
    # credential pattern can match by chance; the gate reads the link with one fixed
    # value of the same shape in their place, and every other part as it is.
    fixed = "A" * 43
    if sign_in._dcr is not None:
        from kiro_crew.connections.crew_oauth_dcr import authorize_url as dcr_authorize_url

        vetted = dcr_authorize_url(sign_in._dcr, fixed, fixed)
    else:
        vetted = authorize_url(slug, sign_in._client.client_id, sign_in._redirect, fixed, fixed)
    if custom is not None:
        # An owner-added server's consent host is on no builtin list. The sign-in
        # itself just discovered this endpoint from the owner-recorded url (the
        # challenge's ``resource_metadata`` included), so that verified result is
        # what the gate is told to admit.
        from kiro_crew.security import record_discovered_oauth_endpoint

        parts = urllib.parse.urlsplit(sign_in._dcr.client.authorize)
        await asyncio.to_thread(
            record_discovered_oauth_endpoint,
            parts.hostname or "",
            parts.path,
            # The query-free url discovery used, never the configured one.
            mcp_url=sign_in._dcr.client.resource,
        )
    if await asyncio.to_thread(oauth_url_contains_credential, vetted):
        await sign_in.shutdown()
        await _set_row(row, token, {"state": "failed", "reason": "mint_url_rejected"})
        return token
    watcher = asyncio.get_running_loop().create_task(
        _await_consent(row, token, sign_in, on_granted)
    )
    if not await _set_row(
        row, token, {"state": "waiting", "oauth_url": url, "client": sign_in, "watcher": watcher}
    ):
        watcher.cancel()
        await sign_in.shutdown()
    _audit("connections_crew_oauth_sign_in", "started", slug)
    return token


async def _set_row(row: str, token: str, fields: dict[str, Any]) -> bool:
    """Update mint row *row* only while it is still this attempt's."""
    from kiro_crew.connections import mint

    async with mint._mints_lock:
        entry = mint._mints.get(row)
        if entry is None or entry.get("token") != token:
            return False
        entry.update(fields)  # type: ignore[typeddict-item]
        return True


async def _await_consent(row: str, token: str, sign_in: SignIn, on_granted: Any = None) -> None:
    from kiro_crew.connections import mint

    slug = sign_in.slug
    outcome: dict[str, Any]
    try:
        await sign_in.finish()
        outcome = {"state": "granted"}
        _audit("connections_crew_oauth_sign_in", "ok", slug)
        if on_granted is not None:
            try:
                # On the loop, like the dashboard writer's own marks.
                on_granted()
            except Exception:  # noqa: BLE001 -- the grant is stored; a refresh is best effort
                logger.debug("post-grant hook failed for %s", slug, exc_info=True)
    except asyncio.CancelledError:
        await sign_in.shutdown()
        raise
    except CrewOAuthError as exc:
        state = "expired" if exc.code == "consent_expired" else "failed"
        outcome = {"state": state, "reason": f"crew_oauth_{exc.code}"}
        _audit("connections_crew_oauth_sign_in", "error", slug, exc.code)
    except Exception as exc:  # noqa: BLE001 -- background task; record, never raise
        outcome = {"state": "failed", "reason": "crew_oauth_" + type(exc).__name__.lower()[:32]}
        _audit("connections_crew_oauth_sign_in", "error", slug, type(exc).__name__)
    await sign_in.shutdown()
    async with mint._mints_lock:
        entry = mint._mints.get(row)
        if entry is not None and entry.get("token") == token:
            entry.pop("client", None)
            entry.pop("watcher", None)
            entry.pop("oauth_url", None)
            entry.update(outcome)  # type: ignore[typeddict-item]
