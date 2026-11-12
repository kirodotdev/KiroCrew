"""Crew-owned OAuth for a DCR Connection card (RFC 7591), on KAS behind the preview.

The pre-registered path in :mod:`kiro_crew.connections.crew_oauth` uses an
operator's client and fixed endpoints. A DCR card has neither, so at sign-in:

* :func:`discover` walks the MCP authorization chain from the registry
  ``mcp_url``: the 401 challenge's ``resource_metadata``, else the well-known
  protected-resource documents, then the authorization-server metadata. The
  issuer must equal the one COMMITTED in the registry (``issuer_changed``
  otherwise), the AS must offer S256 and a ``registration_endpoint``, and no
  endpoint is ever guessed. Every fetch is SSRF-vetted, pinned, HTTPS:443, no
  redirects, size-bounded (:mod:`kiro_crew.mcp_oauth_discovery`).
* :func:`prepare` registers Crew's own client once per card and keeps it in the
  vault (``CONNECTIONS_<SLUG>_OAUTH_CLIENT``). It registers again when the stored
  client's redirect or issuer differs, its secret has expired, or after the token
  endpoint answered ``invalid_client`` (:func:`forget_client`).
* every authorize, token and refresh call carries ``resource`` (RFC 8707); the
  client authenticates the way the AS metadata allows (``none`` first, else
  Basic, else form post); ``offline_access`` is asked for when the AS lists it;
  the callback's ``iss`` is checked (RFC 9207).
* all DCR sign-ins share one loopback callback port (:data:`DCR_CALLBACK_PORT`,
  reserved in the registry validator), and ``state`` picks the pending sign-in.
  A gateway with a configured public HTTPS ``dashboard.url`` (never a request's
  ``Host``) registers its own route instead (:func:`gateway_callback`), so a
  pod's browser lands back on the gateway with no paste.
* :func:`revoke` is RFC 7009 when the AS advertises a ``revocation_endpoint``,
  else ``False`` (local delete only; the card keeps its manual revoke link).
  Disconnect keeps the registered client.

Providers stay data: nothing here names a vendor. Secrets never reach a log.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew.connections.crew_oauth import CrewOAuthError, Tokens, TokenStore, _refresh_locks
from kiro_crew.connections.registry import CALLBACK_PATH, DCR_CALLBACK_PORT, get_provider
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.mcp_discovery_probe_state import MCP_CLIENT_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

#: The ``client_name`` Crew registers under, unless the card names its own.
CLIENT_NAME = "Kiro Crew"
_DISCOVERY_TIMEOUT_SECS = 20.0
_HTTP_TIMEOUT_SECS = 20.0
_MAX_ANSWER_BYTES = 64 * 1024
#: Client authentication methods Crew can perform, in order of preference.
_AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")


def _public_origin() -> str:
    """The gateway's configured public HTTPS origin, or ``""``. Blocking.

    ``dashboard.url`` only. Never the request's ``Host``: a redirect a caller
    could steer would hand the code to its host.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.urls import dashboard_origin, is_loopback

    try:
        configured = KiroCrewConfig.load().dashboard.url
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the loopback
        return ""
    origin = dashboard_origin(configured) if isinstance(configured, str) else ""
    host = urllib.parse.urlsplit(origin).hostname or ""
    return origin if origin.startswith("https://") and host and not is_loopback(host) else ""


def redirect_uri() -> str:
    """The redirect DCR registers: the gateway's public route, else the loopback. Blocking."""
    from kiro_crew.dashboard.token_auth_paths import CREW_OAUTH_CALLBACK_PATH

    origin = _public_origin()
    if origin:
        return origin + CREW_OAUTH_CALLBACK_PATH
    return f"http://127.0.0.1:{DCR_CALLBACK_PORT}{CALLBACK_PATH}"


def client_entry_name(slug: str) -> str:
    """The vault entry holding *slug*'s registered client."""
    return f"CONNECTIONS_{slug.upper().replace('-', '_')}_OAUTH_CLIENT"


@dataclass(frozen=True)
class Client:
    """Crew's registration at one card's AS, plus the endpoints it was found with."""

    client_id: str
    client_secret: str | None
    secret_expires_at: float
    auth_method: str
    redirect_uri: str
    issuer: str
    resource: str
    authorize: str
    token: str
    revoke: str | None
    iss_param: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Client | None:
        try:
            doc = json.loads(raw)
            client = cls(**doc)
        except (ValueError, TypeError):
            return None
        return client if client.client_id and client.auth_method in _AUTH_METHODS else None

    def usable(self, issuer: str, now: float, redirect: str) -> bool:
        """Whether this registration still fits: same redirect and issuer, secret alive."""
        alive = not self.secret_expires_at or self.secret_expires_at > now
        return alive and self.redirect_uri == redirect and self.issuer == issuer


class ClientStore:
    """One vault entry per card, written compare-and-set like the token pair."""

    def __init__(self, vault: Any | None = None) -> None:
        if vault is None:
            from kiro_crew.config.paths import config_dir
            from kiro_crew.secrets import SecretVault

            vault = SecretVault(config_dir())
        self._vault = vault

    def read(self, slug: str) -> tuple[Client | None, str | None]:
        held = self._vault.get_with_revision(client_entry_name(slug))
        if held is None:
            return None, None
        return Client.from_json(held[0].reveal()), held[1]

    def commit(self, slug: str, client: Client, expected: str | None) -> str | None:
        return self._vault.set_if_revision_sync(client_entry_name(slug), client.to_json(), expected)

    def delete(self, slug: str) -> None:
        self._vault.delete_sync(client_entry_name(slug))


@dataclass(frozen=True)
class Plan:
    """What one sign-in needs: the client and the scope to ask for."""

    client: Client
    scope: str


# --- HTTP: every call vetted and pinned --------------------------------------


async def _session(url: str) -> tuple[Any, str]:
    """A session that can reach only *url*'s vetted addresses, and the URL to use."""
    import aiohttp

    from kiro_crew.link_unfurl import pinned_connector
    from kiro_crew.mcp_oauth_discovery import _vet_discovery_url

    vetted = await asyncio.to_thread(_vet_discovery_url, url)
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    return aiohttp.ClientSession(connector=pinned_connector(vetted), timeout=timeout), vetted.url


async def _post(
    url: str,
    *,
    form: dict[str, str] | None = None,
    body: Any = None,
    auth: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """POST *url*; ``(status, JSON object or {}, WWW-Authenticate)``. Never logs a value."""
    import aiohttp

    from kiro_crew.link_unfurl import UnfurlRejected

    try:
        session, target = await _session(url)
        async with session:
            async with session.post(
                target,
                data=form,
                json=body,
                auth=auth,
                headers={"Accept": "application/json", **(headers or {})},
                allow_redirects=False,
            ) as resp:
                challenge = resp.headers.get("WWW-Authenticate", "")
                status = resp.status
                # Read to EOF: one read() returns only what has arrived so far.
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(16 * 1024):
                    raw.extend(chunk)
                    if len(raw) > _MAX_ANSWER_BYTES:
                        break
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, UnfurlRejected):
        # The code the proxy already reads as transient ("try again").
        raise CrewOAuthError("token_unreachable") from None
    try:
        doc = json.loads(bytes(raw)) if len(raw) <= _MAX_ANSWER_BYTES else None
    except ValueError:
        doc = None
    return status, doc if isinstance(doc, dict) else {}, challenge


def _challenge_params(header: str) -> dict[str, str]:
    """``resource_metadata`` / ``scope`` from a ``Bearer`` challenge."""
    if not header.lower().startswith("bearer"):
        return {}
    text, i, n = header[6:], 0, len(header) - 6
    params: dict[str, str] = {}
    while True:
        while i < n and text[i] in " ,":
            i += 1
        start = i
        while i < n and (text[i].isalpha() or text[i] == "_"):
            i += 1
        name = text[start:i]
        while i < n and text[i] == " ":
            i += 1
        if not name or i >= n or text[i] != "=":
            return params
        i += 1
        while i < n and text[i] == " ":
            i += 1
        value: list[str] = []
        if i < n and text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(text[i])
                i += 1
            if i >= n:
                return params  # no closing quote
            i += 1
        else:
            while i < n and text[i] not in " ,":
                value.append(text[i])
                i += 1
        params[name.lower()] = "".join(value)


# --- discovery ---------------------------------------------------------------


def _endpoint(doc: dict[str, Any], key: str) -> str:
    """A metadata endpoint: absolute HTTPS on 443 to a public host, else ``ValueError``."""
    from kiro_crew.connections.l0_probe import _https_url

    url = _https_url(doc.get(key), key)
    if urllib.parse.urlsplit(url).port not in (None, 443):
        raise ValueError(f"{key} must use port 443")
    return url


def _as_metadata_urls(issuer: str) -> list[str]:
    """The MCP authorization spec's order: OAuth path-insert, OIDC path-insert, OIDC path-append."""
    from kiro_crew.connections.l0_probe import _authorization_metadata_url
    from kiro_crew.mcp_oauth_discovery import _oidc_metadata_url

    parts = urllib.parse.urlsplit(issuer)
    path = parts.path.rstrip("/")
    oidc_insert = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/.well-known/openid-configuration" + path, "", "")
    )
    urls = [_authorization_metadata_url(issuer), oidc_insert, _oidc_metadata_url(issuer)]
    return list(dict.fromkeys(urls))


async def _discover(
    provider: dict[str, Any], *, need_dcr: bool = True
) -> tuple[dict[str, Any], str, str]:
    """``(AS metadata, issuer, scope)`` for a card, pinned to its committed issuer.

    A provider without ``l0_expectations`` (an owner-added server) commits no
    issuer: it follows the one its url advertises, as the harness would, because
    the url itself is the owner's sealed record. *need_dcr* False skips the
    ``registration_endpoint`` requirement, for a client the owner registered.
    """
    from kiro_crew.connections.l0_probe import (
        _read_resource_metadata,
        _resource_metadata_urls,
        _validate_issuer,
        same_issuer,
    )
    from kiro_crew.mcp_oauth_discovery import _FETCH_ERRORS, _fetch_json

    mcp_url = str(provider["mcp_url"])
    expected = (provider.get("l0_expectations") or {}).get("authorization_server")
    committed = _validate_issuer(expected) if expected else None
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_CLIENT_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1"},
        },
    }
    try:
        status, _, header = await _post(
            mcp_url,
            body=initialize,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MCP_CLIENT_PROTOCOL_VERSION,
            },
        )
    except CrewOAuthError:
        # No challenge: the well-known documents below still name the issuer.
        status, header = 0, ""
    challenge = _challenge_params(header) if status == 401 else {}
    candidates = [challenge["resource_metadata"]] if challenge.get("resource_metadata") else []
    advertised = None
    resource_doc: dict[str, Any] = {}
    for url in dict.fromkeys([*candidates, *_resource_metadata_urls(mcp_url)]):
        try:
            resource_doc = await _fetch_json(url)
            advertised = _read_resource_metadata(resource_doc, mcp_url)
            break
        except _FETCH_ERRORS:
            continue
    if advertised is None:
        raise CrewOAuthError("discovery_failed")
    if committed is None:
        committed = advertised
    elif not same_issuer(advertised, committed):
        # Never followed: the committed issuer moves only in a reviewed commit.
        raise CrewOAuthError("issuer_changed")
    for url in _as_metadata_urls(committed):
        try:
            doc = await _fetch_json(url)
            if not same_issuer(_validate_issuer(doc.get("issuer")), committed):
                raise CrewOAuthError("issuer_changed")
            for key in ("authorization_endpoint", "token_endpoint"):
                _endpoint(doc, key)
        except _FETCH_ERRORS:
            continue
        break
    else:
        raise CrewOAuthError("discovery_failed")
    methods = doc.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise CrewOAuthError("no_s256")
    try:
        if need_dcr:
            _endpoint(doc, "registration_endpoint")
    except ValueError:
        raise CrewOAuthError("dcr_unsupported") from None
    scopes = (
        (challenge.get("scope") or "").split()
        or [str(s) for s in provider.get("recommended_scopes") or []]
        or [str(s) for s in resource_doc.get("scopes_supported") or [] if isinstance(s, str)]
    )
    supported = doc.get("scopes_supported")
    if isinstance(supported, list) and "offline_access" in supported:
        scopes = [*dict.fromkeys([*scopes, "offline_access"])]
    return doc, committed, " ".join(scopes)


def _auth_method(doc: dict[str, Any]) -> str:
    """``none`` when listed, else the first secret method Crew can send."""
    listed = doc.get("token_endpoint_auth_methods_supported")
    # RFC 8414 §2: absent means client_secret_basic.
    listed = listed if isinstance(listed, list) else ["client_secret_basic"]
    for method in _AUTH_METHODS:
        if method in listed:
            return method
    raise CrewOAuthError("auth_method_unsupported")


def _endpoints(doc: dict[str, Any], issuer: str, resource: str) -> dict[str, Any]:
    try:
        revoke = _endpoint(doc, "revocation_endpoint") if doc.get("revocation_endpoint") else None
    except ValueError:
        revoke = None
    return {
        "issuer": issuer,
        "resource": resource,
        "authorize": _endpoint(doc, "authorization_endpoint"),
        "token": _endpoint(doc, "token_endpoint"),
        "revoke": revoke,
        "iss_param": doc.get("authorization_response_iss_parameter_supported") is True,
    }


async def _register(
    provider: dict[str, Any], doc: dict[str, Any], scope: str, redirect: str
) -> dict[str, Any]:
    """RFC 7591 registration; the fields :class:`Client` keeps. Raises a coded error."""
    method = _auth_method(doc)
    request: dict[str, Any] = {
        "client_name": provider.get("dcr_client_name") or CLIENT_NAME,
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
    }
    if scope:
        request["scope"] = scope
    status, answer, _ = await _post(_endpoint(doc, "registration_endpoint"), body=request)
    client_id = answer.get("client_id")
    if status not in (200, 201) or not isinstance(client_id, str) or not client_id:
        raise CrewOAuthError("registration_refused")
    granted = answer.get("token_endpoint_auth_method")
    method = granted if granted in _AUTH_METHODS else method
    secret = answer.get("client_secret")
    secret = secret if isinstance(secret, str) and secret else None
    if method != "none" and secret is None:
        raise CrewOAuthError("registration_refused")
    expires = answer.get("client_secret_expires_at")
    return {
        "client_id": client_id,
        "client_secret": secret,
        "secret_expires_at": float(expires) if isinstance(expires, (int, float)) else 0.0,
        "auth_method": method,
        "redirect_uri": redirect,
    }


async def prepare(slug: str, store: ClientStore | None = None) -> Plan:
    """Discover, then reuse or register Crew's client for *slug*. Raises a coded error."""
    provider = get_provider(slug)
    if provider is None:
        raise CrewOAuthError("client_not_configured")
    store = store or ClientStore()
    try:
        doc, issuer, scope = await asyncio.wait_for(
            _discover(dict(provider)), _DISCOVERY_TIMEOUT_SECS
        )
        endpoints = _endpoints(doc, issuer, str(provider["mcp_url"]))
    except asyncio.TimeoutError:
        raise CrewOAuthError("discovery_failed") from None
    except ValueError:
        raise CrewOAuthError("discovery_failed") from None
    redirect = await asyncio.to_thread(redirect_uri)
    return await _reuse_or_register(
        slug, dict(provider), doc, issuer, scope, endpoints, redirect, store
    )


async def _reuse_or_register(
    slug: str,
    provider: dict[str, Any],
    doc: dict[str, Any],
    issuer: str,
    scope: str,
    endpoints: dict[str, Any],
    redirect: str,
    store: ClientStore,
    stage: bool = False,
) -> Plan:
    """Reuse *slug*'s stored registration, or register a new one.

    *stage* (an owner-added server) stores nothing: the caller writes the client
    together with the pair its code swap returns (:func:`keep_client`), so a
    refused or abandoned sign-in leaves the working client and pair in place.
    """
    async with _refresh_locks.setdefault(slug, asyncio.Lock()):
        held, revision = await asyncio.to_thread(store.read, slug)
        if held is not None and held.usable(issuer, time.time(), redirect):
            client = replace(held, **endpoints)
            if (
                not stage
                and client != held
                and await asyncio.to_thread(store.commit, slug, client, revision) is None
            ):
                raise CrewOAuthError("client_raced")
            return Plan(client, scope)
        client = Client(**await _register(provider, doc, scope, redirect), **endpoints)
        if stage:
            _audit("connections_crew_oauth_register", "staged", slug)
            return Plan(client, scope)
        if await asyncio.to_thread(store.commit, slug, client, revision) is None:
            raise CrewOAuthError("client_raced")
        if held is not None or revision is not None:
            # Tokens issued to the old registration cannot be used with the new one.
            await asyncio.to_thread(TokenStore().delete, slug)
        _audit("connections_crew_oauth_register", "ok", slug)
        return Plan(client, scope)


def _loopback_redirect(value: Any) -> tuple[int, str] | None:
    """``(port, path)`` of an ``http://localhost|127.0.0.1:<port>/<path>`` redirect, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "http"
        or parts.hostname not in ("localhost", "127.0.0.1")
        or port is None
        or not 1024 <= port <= 65535
        or not parts.path.startswith("/")
        or parts.query
        or parts.fragment
    ):
        return None
    return port, parts.path


def own_listener(redirect: str) -> tuple[int, str] | None:
    """The ``(port, path)`` a sign-in binds itself, or ``None`` for the shared listener.

    The shared DCR loopback and the gateway's public route are served by
    :func:`listen`; any other loopback redirect is an owner's own registration.
    """
    if redirect.startswith("https://") or redirect == (
        f"http://127.0.0.1:{DCR_CALLBACK_PORT}{CALLBACK_PATH}"
    ):
        return None
    return _loopback_redirect(redirect)


async def prepare_custom(
    key: str,
    mcp_url: str,
    oauth: Any,
    store: ClientStore | None = None,
    scopes: list[str] | None = None,
) -> Plan:
    """The client for an owner-added server, stored under *key*. Raises a coded error.

    Its ``oauth`` block may name the owner's own client (``clientId``, optional
    ``clientSecret``, ``redirectUri``); without a ``clientId`` Crew registers its
    own by DCR, as for a card. *scopes* are the entry's own (either spelling),
    else ``oauth.oauthScopes``. Endpoints always come from discovery from
    *mcp_url*. A ``clientSecret`` is copied into the vault entry the refresh
    reads; it is never sent to a harness.
    """
    oauth = oauth if isinstance(oauth, dict) else {}
    from kiro_crew.mcp_oauth_discovery import _mcp_endpoint

    try:
        # Scheme, authority and path only, for discovery, ``resource`` and the
        # audit: a query-string credential never reaches the AS or the SEL.
        mcp_url = _mcp_endpoint(mcp_url)
    except ValueError:
        raise CrewOAuthError("discovery_failed") from None
    client_id = oauth.get("clientId") if isinstance(oauth.get("clientId"), str) else ""
    if scopes is None:
        scopes = oauth.get("oauthScopes")
    provider: dict[str, Any] = {
        "slug": key,
        "mcp_url": mcp_url,
        "recommended_scopes": (
            [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []
        ),
    }
    store = store or ClientStore()
    try:
        doc, issuer, scope = await asyncio.wait_for(
            _discover(provider, need_dcr=not client_id), _DISCOVERY_TIMEOUT_SECS
        )
        endpoints = _endpoints(doc, issuer, mcp_url)
    except (asyncio.TimeoutError, ValueError):
        raise CrewOAuthError("discovery_failed") from None
    if provider["recommended_scopes"]:
        # The owner's own scopes win over a challenge's: their app grants those.
        asked = provider["recommended_scopes"]
        extra = ["offline_access"] if "offline_access" in scope.split() else []
        scope = " ".join(dict.fromkeys([*asked, *extra]))
    given = oauth.get("redirectUri")
    if given is not None and _loopback_redirect(given) is None:
        # A redirect Crew cannot receive would strand the code at the vendor.
        raise CrewOAuthError("redirect_unsupported")
    redirect = given if isinstance(given, str) else await asyncio.to_thread(redirect_uri)
    if not client_id:
        return await _reuse_or_register(
            key, provider, doc, issuer, scope, endpoints, redirect, store, stage=True
        )
    secret = oauth.get("clientSecret")
    secret = secret if isinstance(secret, str) and secret else None
    client = Client(
        client_id=client_id,
        client_secret=secret,
        secret_expires_at=0.0,
        auth_method=_secret_method(doc) if secret else "none",
        redirect_uri=redirect,
        **endpoints,
    )
    # Stored only once its code swap succeeds (:func:`keep_client`): the pair a
    # declined consent leaves behind keeps refreshing with the client that issued it.
    return Plan(client, scope)


def keep_client(key: str, client: Client, tokens: Any, vault: Any | None = None) -> None:
    """Store an owner's client and the pair it just issued in one vault write. Blocking.

    The caller holds *key*'s refresh lock and has swapped a code with *client*.
    One write, so a failure keeps both previous entries: the old pair never sits
    beside a client that did not issue it.
    """
    from kiro_crew.connections.crew_oauth import token_entry_name

    if vault is None:
        vault = ClientStore()._vault
    vault.set_many_sync(
        {client_entry_name(key): client.to_json(), token_entry_name(key): tokens.to_json()}
    )


def _secret_method(doc: dict[str, Any]) -> str:
    """How an owner's confidential client authenticates: as the AS lists, else form post.

    shortcut: an AS that lists no method gets ``client_secret_post`` (GitHub
    publishes none and reads the form), not RFC 8414's Basic default; follow
    the default if a server is found that refuses the form.
    """
    listed = doc.get("token_endpoint_auth_methods_supported")
    if not isinstance(listed, list):
        return "client_secret_post"
    for method in _AUTH_METHODS[1:]:
        if method in listed:
            return method
    raise CrewOAuthError("auth_method_unsupported")


async def forget_client(slug: str, store: ClientStore | None = None) -> None:
    """Drop *slug*'s registration after ``invalid_client``: the next Connect registers again."""
    await asyncio.to_thread((store or ClientStore()).delete, slug)
    _audit("connections_crew_oauth_register", "dropped", slug, "invalid_client")


def authorize_url(plan: Plan, state: str, challenge: str) -> str:
    client = plan.client
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": client.resource,
    }
    if plan.scope:
        query["scope"] = plan.scope
    return f"{client.authorize}?{urllib.parse.urlencode(query)}"


def iss_ok(client: Client, iss: str | None) -> bool:
    """RFC 9207: a present ``iss`` must name the issuer; required when the AS says so."""
    from kiro_crew.connections.l0_probe import same_issuer

    if not iss:
        return not client.iss_param
    return same_issuer(iss, client.issuer)


# --- token endpoint ----------------------------------------------------------


def _client_auth(client: Client, form: dict[str, str]) -> Any:
    """Add the client's credentials to *form*; the Basic header when that is the method."""
    import aiohttp

    if client.auth_method == "client_secret_basic" and client.client_secret:
        # RFC 6749 §2.3.1: both halves form-encoded before Basic.
        quote = urllib.parse.quote_plus
        return aiohttp.BasicAuth(quote(client.client_id), quote(client.client_secret))
    form["client_id"] = client.client_id
    if client.auth_method == "client_secret_post" and client.client_secret:
        form["client_secret"] = client.client_secret
    return None


async def _token(client: Client, form: dict[str, str]) -> dict[str, Any]:
    form = {**form, "resource": client.resource}
    auth = _client_auth(client, form)
    status, doc, _ = await _post(client.token, form=form, auth=auth)
    error = doc.get("error")
    if status >= 400 or error:
        raise CrewOAuthError(
            str(error)[:64] if isinstance(error, str) and error else f"token_http_{status}"
        )
    return doc


async def exchange(client: Client, code: str, verifier: str) -> dict[str, Any]:
    """Swap the callback's code for tokens."""
    return await _token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.redirect_uri,
            "code_verifier": verifier,
        },
    )


async def refresh(
    slug: str, refresh_token: str, store: ClientStore | None = None
) -> dict[str, Any]:
    """One refresh with the stored client; ``needs_auth`` when none is stored."""
    client, _ = await asyncio.to_thread((store or ClientStore()).read, slug)
    if client is None:
        raise CrewOAuthError("needs_auth")
    return await _token(client, {"grant_type": "refresh_token", "refresh_token": refresh_token})


async def revoke(slug: str, tokens: Tokens, store: ClientStore | None = None) -> bool:
    """RFC 7009 revoke when advertised; ``False`` without an endpoint. Never raises."""
    try:
        client, _ = await asyncio.to_thread((store or ClientStore()).read, slug)
        if client is None or not client.revoke:
            return False
        form = {
            "token": tokens.refresh or tokens.access,
            "token_type_hint": "refresh_token" if tokens.refresh else "access_token",
        }
        auth = _client_auth(client, form)
        status, _, _ = await _post(client.revoke, form=form, auth=auth)
        return status == 200
    except Exception:  # noqa: BLE001 -- best effort, as the pre-registered revoke
        return False


# --- the one shared callback listener ----------------------------------------

Handler = Callable[[Any], Awaitable[Any]]
#: ``state`` -> (handler, slug).
_pending: dict[str, tuple[Handler, str]] = {}
_listener: dict[str, Any] = {}
_listener_lock = LoopBoundLock()
_STALE = "This sign-in link is not the current one."
_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Kiro Crew</title></head>"
    "<body><p>{}</p></body></html>"
)


async def _route(request: Any) -> tuple[Any, str]:
    """Hand the callback to its pending sign-in; ``(answer, slug or "")``.

    The sign-in answers once (``SignIn._callback``) and leaves after
    :data:`~kiro_crew.connections.crew_oauth.SIGN_IN_TTL_SECS` (``SignIn.finish``, then :func:`unlisten`).
    """
    from aiohttp import web

    state = request.query.get("state", "").encode()
    for key, (handler, slug) in list(_pending.items()):
        # Bytes: compare_digest raises on a non-ASCII str.
        if secrets.compare_digest(key.encode(), state):
            return await handler(request), slug
    return web.Response(status=400, text=_STALE), ""


async def _loopback_route(request: Any) -> Any:
    return (await _route(request))[0]


async def gateway_callback(request: Any) -> Any:
    """``GET`` /api/connections/oauth/callback: a vendor's redirect back to this gateway.

    Token auth lets it through (the redirect carries no Crew credential); the
    pending ``state`` is the only admission. The page is fixed text, never input.
    """
    from aiohttp import web

    answer, slug = await _route(request)
    outcome = "ok" if answer.status == 200 else "refused"
    _audit("connections_crew_oauth_callback", outcome, slug or "unknown")
    return web.Response(
        status=answer.status,
        text=_PAGE.format(html.escape(answer.text or "")),
        content_type="text/html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def listen(state: str, handler: Handler, *, slug: str = "", loopback: bool = True) -> int:
    """Route callbacks carrying *state* to *handler* until :func:`unlisten`.

    *loopback* binds the shared port on first use; a public redirect needs no
    listener of its own, since :func:`gateway_callback` serves it.
    """
    from aiohttp import web

    async with _listener_lock:
        if not loopback:
            _pending[state] = (handler, slug)
            return 0
        if "runner" not in _listener:
            app = web.Application()
            app.router.add_get(CALLBACK_PATH, _loopback_route)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", DCR_CALLBACK_PORT).start()
            except OSError:
                await runner.cleanup()
                raise CrewOAuthError("callback_port_busy") from None
            _listener["runner"] = runner
        _pending[state] = (handler, slug)
        return int(_listener["runner"].addresses[0][1])


async def unlisten(state: str) -> None:
    """Forget *state*; the port closes with the last pending sign-in."""
    async with _listener_lock:
        _pending.pop(state, None)
        if not _pending and "runner" in _listener:
            await _listener.pop("runner").cleanup()


# --- custody flip cleanup ----------------------------------------------------


async def drop_harness_grants(slug: str, project_dirs: tuple[Path, ...] | None) -> None:
    """After Crew's sign-in, delete the harnesses' own grant for this URL. Never raises.

    Both grants are keyed by URL, so another entry naming the same URL uses them
    too. They go only when the census, over the open *project_dirs* as Disconnect
    reads it, finds no other entry sharing the URL, under the MCP config lock.
    ``None``: the open projects are unknown, so both stay. KAS's entries are
    deleted by name (:func:`kiro_crew.mcp_grant_kas.forget_kas_grant`), so Crew
    decrypts nothing.
    """
    provider = get_provider(slug)
    if provider is None or project_dirs is None:
        return
    url = str(provider["mcp_url"])
    try:
        from kiro_crew.connections.ownership import grant_sharers_map
        from kiro_crew.dashboard.handlers.mcp import _get_mcp_lock
        from kiro_crew.mcp_grant import revoke_local_grant
        from kiro_crew.mcp_grant_kas import forget_kas_grant

        async with _get_mcp_lock():
            sharing = (await grant_sharers_map([(slug, url)], project_dirs))[slug]
            if sharing.shared_with or sharing.census_incomplete:
                return
            await forget_kas_grant(url)
            await asyncio.to_thread(revoke_local_grant, url)
    except Exception:  # noqa: BLE001 -- cleanup is best effort
        logger.debug("harness grant cleanup failed for %s", slug, exc_info=True)


def _audit(operation: str, outcome: str, slug: str, detail: str = "") -> None:
    from kiro_crew.connections.crew_oauth import _audit as audit

    audit(operation, outcome, slug, detail)
