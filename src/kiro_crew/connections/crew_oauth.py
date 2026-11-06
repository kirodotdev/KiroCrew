"""Crew-owned OAuth for one Connection (MVP: GitHub on KAS, ``connections.crew_oauth``).

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
* :func:`disconnect` revokes upstream (best effort) and deletes the entry.

Off by default. With the flag off (or on Windows, where the attested Unix-socket
channel the proxy needs does not exist) :func:`manages` is False everywhere and
nothing in this module runs. Standard library + aiohttp only; imported lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: The ``config.json`` key under ``connections`` that turns this on.
FLAG_KEY = "crew_oauth"
#: The MVP covers exactly these slugs.
SUPPORTED_SLUGS = frozenset({"github"})

#: Per-provider OAuth endpoints. GitHub's are fixed (not discoverable via RFC 8414).
_ENDPOINTS: dict[str, dict[str, str]] = {
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        # Token-scoped: revokes only Crew's own token, never the whole app grant
        # another connection of the same account may share.
        "revoke": "https://api.github.com/applications/{client_id}/token",
    }
}
#: Refresh this long before the recorded expiry.
_REFRESH_SKEW_SECS = 120.0
#: How long one consent may stay open.
SIGN_IN_TTL_SECS = 600.0
_HTTP_TIMEOUT_SECS = 20.0
#: Refresh errors that mean the stored refresh token is dead (after one re-read).
_DEAD_GRANT_ERRORS = frozenset({"bad_refresh_token", "invalid_grant", "unauthorized_client"})

_refresh_locks: dict[str, asyncio.Lock] = {}
_startup_locks: dict[str, asyncio.Lock] = {}


class CrewOAuthError(Exception):
    """A failure with a coarse, value-free code (``needs_auth``, ``exchange_failed``…)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def enabled() -> bool:
    """Whether ``connections.crew_oauth`` is ``true`` on a POSIX gateway. Blocking."""
    if platform_compat.IS_WINDOWS:
        return False
    from kiro_crew.config.loader import read_config_for_update

    try:
        connections = (read_config_for_update() or {}).get("connections")
    except Exception:  # noqa: BLE001 -- an unreadable config reads as off
        return False
    return isinstance(connections, dict) and connections.get(FLAG_KEY) is True


def _chat_backend_is_kas() -> bool:
    """Whether ordinary chats run on KAS, the one harness this MVP serves. Blocking."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.config import KiroCrewConfig

    try:
        return KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS
    except Exception:  # noqa: BLE001 -- an unreadable config keeps today's custody
        return False


def manages(slug: str) -> bool:
    """Whether Crew owns *slug*'s OAuth on this gateway. Blocking.

    Needs the flag AND KAS as the chat backend: on kiro-cli the native grant keeps
    working as today, so Crew must not take custody away from it.
    """
    return slug in SUPPORTED_SLUGS and enabled() and _chat_backend_is_kas()


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


def routed_slug(name: str, entry: Any, provider_for_server: Any) -> str:
    """The slug when Crew's sign-in serves this entry, else ``""``. Blocking.

    A registry provider's own entry (slug AND url match) while :func:`manages`
    holds. An entry that sends its own credential header keeps its own path.
    """
    if name not in SUPPORTED_SLUGS or has_credential_header(entry.get("headers")):
        return ""
    provider = provider_for_server(name, entry)
    if provider is None or not manages(str(provider["slug"])):
        return ""
    return str(provider["slug"])


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
    if slug not in SUPPORTED_SLUGS:
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

    def to_json(self) -> str:
        return json.dumps(
            {
                "access": self.access,
                "refresh": self.refresh,
                "expires_at": self.expires_at,
                "scope": self.scope,
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
        return cls(
            access=access,
            refresh=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=float(expires) if isinstance(expires, (int, float)) else None,
            scope=str(doc.get("scope") or ""),
        )

    def expiring(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at - _REFRESH_SKEW_SECS <= now


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
    )


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
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        query["scope"] = " ".join(str(s) for s in scopes)
    return f"{_ENDPOINTS[slug]['authorize']}?{urllib.parse.urlencode(query)}"


class SignIn:
    """One pending consent: the listener, the verifier and ``state``, in memory only.

    Shaped for a mint row's ``client`` slot (:mod:`kiro_crew.connections.mint`):
    ``shutdown`` closes the listener, so the existing row lifecycle (cancel,
    supersede) reaches it. No process backs this row, so it offers no liveness
    probe; its own watcher writes the row's terminal state (granted, failed,
    expired after :data:`SIGN_IN_TTL_SECS`).
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
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

    async def start(self) -> str:
        """Bind the callback listener and return the consent URL."""
        from aiohttp import web

        from kiro_crew.connections.registry import CALLBACK_PATH, get_provider, redirect_uri

        provider = get_provider(self.slug)
        client = await asyncio.to_thread(_client, self.slug)
        redirect = redirect_uri(provider) if provider else None
        if not redirect:
            raise CrewOAuthError("client_not_configured")
        auth = provider["auth"]  # type: ignore[index]
        app = web.Application()
        app.router.add_get(CALLBACK_PATH, self._callback)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # Loopback only, the exact host and port the operator registered, and only
        # while this one consent is pending (RFC 4.7).
        site = web.TCPSite(self._runner, "127.0.0.1", int(auth["redirect_port"]))
        try:
            await site.start()
        except OSError:
            await self._runner.cleanup()
            self._runner = None
            raise CrewOAuthError("callback_port_busy") from None
        self.bound_port = int(self._runner.addresses[0][1])
        self._client = client
        self._redirect = redirect
        self.url = authorize_url(
            self.slug, client.client_id, redirect, self._state, self._challenge
        )
        return self.url

    async def _callback(self, request: Any) -> Any:
        from aiohttp import web

        state = request.query.get("state", "")
        code = request.query.get("code", "")
        # Bytes: compare_digest raises on a non-ASCII str, which would answer 500.
        if self._done.done() or not secrets.compare_digest(state.encode(), self._state.encode()):
            return web.Response(status=400, text="This sign-in link is not the current one.")
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
        doc = await _post_token(
            self.slug,
            {
                "client_id": self._client.client_id,
                "client_secret": self._client.client_secret,
                "code": code,
                "redirect_uri": self._redirect,
                "code_verifier": self._verifier,
            },
        )
        tokens = tokens_from_response(doc, time.time())
        if self._closing:
            raise CrewOAuthError("cancelled")
        # A worker-thread write outlives a cancel of this coroutine, so it is kept
        # and drained by shutdown(): a Disconnect that ends this sign-in never
        # reads the vault before the write it raced has landed.
        target = store or TokenStore()

        async def _locked_write() -> None:
            # Serialized with refresh and Disconnect on the per-connection lock.
            async with _refresh_locks.setdefault(self.slug, asyncio.Lock()):
                await asyncio.to_thread(target.replace, self.slug, tokens)

        self._write = asyncio.ensure_future(_locked_write())
        await asyncio.shield(self._write)

    async def shutdown(self) -> None:
        """Close the listener and wait for any token write already started."""
        self._closing = True
        if not self._done.done():
            self._done.cancel()
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        write = self._write
        if write is not None and not write.done():
            await asyncio.wait({write})


async def _refresh(slug: str, store: TokenStore, held: tuple[Tokens, str]) -> Tokens:
    """Rotate the pair once; commit before returning the new access token."""
    tokens, revision = held
    if not tokens.refresh:
        raise CrewOAuthError("needs_auth")
    client = await asyncio.to_thread(_client, slug)
    try:
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
        code = "needs_auth" if exc.code in _DEAD_GRANT_ERRORS else exc.code
        raise CrewOAuthError(code) from None
    if fresh.refresh is None:
        fresh = Tokens(fresh.access, tokens.refresh, fresh.expires_at, fresh.scope)
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


async def _revoke(slug: str, access: str) -> bool:
    """Token-scoped upstream revoke of *access*; best effort, never raises."""
    import aiohttp

    try:
        client = await asyncio.to_thread(_client, slug)
        auth = aiohttp.BasicAuth(client.client_id, client.client_secret)
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(
                _ENDPOINTS[slug]["revoke"].format(
                    client_id=urllib.parse.quote(client.client_id, safe="")
                ),
                json={"access_token": access},
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
    re-read and revoked too, so no stored token is dropped unrevoked.
    """
    store = store or TokenStore()
    removed = False
    revoked = True
    seen = False
    async with _refresh_locks.setdefault(slug, asyncio.Lock()):
        for _ in range(_DISCONNECT_ROUNDS):
            held = await asyncio.to_thread(store.read, slug)
            if held is None:
                # An entry Crew cannot parse is never used, but must not outlive Disconnect.
                if await asyncio.to_thread(store.present, slug):
                    await asyncio.to_thread(store.delete, slug)
                    removed = True
                break
            seen = True
            revoked = await _revoke(slug, held[0].access) and revoked
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
    """One SEL record; names the provider and a coarse code, never a value."""
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


async def start_mint_row(slug: str) -> str:
    """Run a Crew-owned sign-in through the Connections card's mint row; the row token.

    The card's contract is unchanged: it polls ``GET /api/connections/mint`` for
    ``waiting`` + ``oauth_url``, then ``granted`` / ``failed`` / ``expired``. The row
    holds the :class:`SignIn` as its ``client`` and the wait as its ``watcher``,
    so Cancel, a superseding Connect and the dead-holder check all reach them.
    One start per provider at a time: a superseding Connect waits until the
    attempt it replaces has registered its listener, so it can dispose of it.
    """
    async with _startup_locks.setdefault(slug, asyncio.Lock()):
        return await _start_mint_row(slug)


async def _start_mint_row(slug: str) -> str:
    from kiro_crew.connections import mint

    token, prior = await mint.reserve_mint_row(slug)
    if prior is not None:
        await mint._dispose_mint(prior)
    sign_in = SignIn(slug)
    try:
        url = await sign_in.start()
    except CrewOAuthError as exc:
        await _set_row(slug, token, {"state": "failed", "reason": f"crew_oauth_{exc.code}"})
        _audit("connections_crew_oauth_sign_in", "error", slug, exc.code)
        return token
    from kiro_crew.security import oauth_url_contains_credential

    if await asyncio.to_thread(oauth_url_contains_credential, url):
        await sign_in.shutdown()
        await _set_row(slug, token, {"state": "failed", "reason": "mint_url_rejected"})
        return token
    watcher = asyncio.get_running_loop().create_task(_await_consent(slug, token, sign_in))
    if not await _set_row(
        slug, token, {"state": "waiting", "oauth_url": url, "client": sign_in, "watcher": watcher}
    ):
        watcher.cancel()
        await sign_in.shutdown()
    _audit("connections_crew_oauth_sign_in", "started", slug)
    return token


async def _set_row(slug: str, token: str, fields: dict[str, Any]) -> bool:
    """Update *slug*'s row only while it is still this attempt's."""
    from kiro_crew.connections import mint

    async with mint._mints_lock:
        entry = mint._mints.get(slug)
        if entry is None or entry.get("token") != token:
            return False
        entry.update(fields)  # type: ignore[typeddict-item]
        return True


async def _await_consent(slug: str, token: str, sign_in: SignIn) -> None:
    from kiro_crew.connections import mint

    outcome: dict[str, Any]
    try:
        await sign_in.finish()
        outcome = {"state": "granted"}
        _audit("connections_crew_oauth_sign_in", "ok", slug)
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
        entry = mint._mints.get(slug)
        if entry is not None and entry.get("token") == token:
            entry.pop("client", None)
            entry.pop("watcher", None)
            entry.pop("oauth_url", None)
            entry.update(outcome)  # type: ignore[typeddict-item]
