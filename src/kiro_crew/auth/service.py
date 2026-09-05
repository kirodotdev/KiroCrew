"""KasLoginService — orchestrates interactive KAS-mode login for the dashboard API.

Ties the token store, install-shape detection, and the device-code login flow into
the small state machine the HTTP handlers expose: status -> begin -> poll -> (token
saved) / logout. The service owns the pending-login table because the device flow is
inherently two requests apart (begin returns the user code, poll observes approval),
and the DeviceAuthorization codes must never leave the gateway process — the browser
only ever sees the verification URI.

Polling is single-shot by design: the dashboard drives the cadence, so a slow or
abandoned login never pins a server-side task. Expiry is enforced locally from the
authorization's own deadline, which also bounds how long an abandoned entry lives.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from kiro_crew.auth.login import builder_id, control_plane, device, portal
from kiro_crew.auth.login.endpoints import (
    BUILDER_ID_REGION,
    BUILDER_ID_START_URL,
    USER_AGENT,
    social_service_url,
)
from kiro_crew.auth.shape import Transport, select_transport
from kiro_crew.auth.store import KasToken, SocialProvider, TokenStore, TokenStoreError

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}


class UnknownLoginError(Exception):
    """The login_id does not match any pending device authorization."""


class MissingStartUrlError(Exception):
    """An IdC login was begun without the company start URL it requires."""


class LoopbackUnavailableError(Exception):
    """A loopback login cannot start here: wrong transport or no free callback port.

    The dashboard treats this as 'fall back to the device flow now', so it is a
    coded 409 rather than a failure.
    """


class InvalidRegionError(Exception):
    """A caller-supplied region is outside the AWS region-name grammar.

    The region is interpolated into the SSO-OIDC and control-plane hostnames
    (``https://oidc.<region>.amazonaws.com``), so anything beyond the strict
    grammar would let a crafted value (e.g. ``evil.com/``) redirect client
    registration, device codes, and token exchange to an attacker host.
    """


# AWS region names: two-letter partition, one or more dash-joined words, numeric
# suffix (us-east-1, us-gov-west-1, ap-southeast-3). Anchored and character-tight
# so no URL metacharacter ('/', '@', ':', '.') can survive into the hostname.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")


@dataclass
class _PendingLogin:
    """A social device authorization awaiting user approval, keyed by login_id."""

    auth: device.DeviceAuthorization
    provider: SocialProvider


@dataclass
class _PendingOidcLogin:
    """An SSO-OIDC device authorization (Builder ID / IdC) awaiting approval.

    Carries the dynamically-registered client because the token poll needs its
    credentials, and the identity/provider pair because they decide both the store
    entry and KAS's governance classification. ``resolve_profile`` marks the IdC
    path, whose token is unusable until a profile ARN is attached.
    """

    client: builder_id.RegisteredClient
    auth: builder_id.DeviceAuthorization
    region: str
    identity: str
    provider: str
    resolve_profile: bool


# How long the loopback listener waits for the portal to redirect back. Long enough
# for a full Google/GitHub sign-in with 2FA; short enough that a browser that can
# never reach this machine's loopback (tunnel, tailnet, phone) degrades to the
# device flow within one sitting instead of holding an allowlisted port all day.
LOOPBACK_TIMEOUT_SECS = 300.0


@dataclass
class _PendingLoopbackLogin:
    """A loopback (PKCE) social login whose listener task is running in-process.

    ``task`` owns the bound callback port for its whole life: it serves the redirect,
    exchanges the code and persists the token, then resolves. Polling only inspects
    the task; cancelling it is what releases the port early.
    """

    task: asyncio.Task[KasToken]
    provider: SocialProvider
    expires_at: datetime


def _parse_provider(provider_str: str) -> SocialProvider:
    """Map a caller-supplied provider name to the wire enum (case-insensitive)."""
    normalized = (provider_str or "").strip().lower()
    for member in SocialProvider:
        if member.value.lower() == normalized or member.name.lower() == normalized:
            return member
    raise ValueError(f"unknown social provider: {provider_str!r}")


_PendingKind = _PendingLogin | _PendingOidcLogin | _PendingLoopbackLogin


def _social_from_login_option(login_option: str) -> SocialProvider | None:
    """Map the portal callback's ``login_option`` (``google``/``github``) to the enum.

    ``None`` for an empty or unrecognised value, so the caller can fall back to the
    provider the user clicked rather than fail an otherwise-valid callback.
    """
    try:
        return _parse_provider(login_option)
    except ValueError:
        return None


def _expires_at(entry: _PendingKind) -> datetime:
    if isinstance(entry, _PendingLoopbackLogin):
        return entry.expires_at
    return entry.auth.expires_at


def _release(entry: _PendingKind) -> None:
    """Drop an entry's live resources; only a loopback login holds any (its port)."""
    if isinstance(entry, _PendingLoopbackLogin) and not entry.task.done():
        entry.task.cancel()


class KasLoginService:
    """Login orchestration for the KAS-mode auth subsystem.

    Handlers stay stateless; all mutable login state (the pending-authorization
    table and the shared HTTP session) lives here, guarded by one asyncio.Lock so
    concurrent dashboard tabs cannot corrupt the table.
    """

    def __init__(
        self,
        store: TokenStore,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._store = store
        self._session = session
        self._pending: dict[str, _PendingKind] = {}
        self._lock = asyncio.Lock()

    async def _http(self) -> aiohttp.ClientSession:
        # Lazy: constructing the session at gateway boot would bind it to a loop the
        # service may never run on; first use always happens on the serving loop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        # Release every held callback port first: a listener that outlives the
        # service would keep an allowlisted port bound with nobody to poll it.
        async with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for entry in entries:
            _release(entry)
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def status(self) -> dict[str, Any]:
        """Current auth state + which login transport this install shape should use."""
        # File reads happen off-loop: the store is tiny but sits on whatever disk the
        # data home lives on, and status is polled by the dashboard.
        token = await asyncio.to_thread(self._store.resolve)
        transport = select_transport()
        return {
            "authenticated": token is not None,
            "provider": token.provider if token else "",
            "identity": token.identity if token else "",
            "transport": transport.value,
        }

    async def begin_device(
        self, provider_str: str, *, start_url: str = "", region: str = ""
    ) -> dict[str, Any]:
        """Start a device-code login; returns what the user needs to approve it.

        ``google``/``github`` run the Kiro-proxied social flow; ``builder_id`` and
        ``idc`` run the standard AWS SSO-OIDC device flow (``idc`` additionally
        requires the company's ``start_url`` and takes an optional ``region``).
        Raises ValueError for an unrecognized provider, MissingStartUrlError for an
        IdC begin without a start URL, and DeviceAuthError / BuilderIdAuthError when
        the auth service rejects the authorization request.
        """
        normalized = (provider_str or "").strip().lower()
        if normalized in ("builder_id", "idc"):
            cleaned_region = (region or "").strip()
            if cleaned_region and not _REGION_RE.fullmatch(cleaned_region):
                raise InvalidRegionError(f"not an AWS region name: {cleaned_region[:64]!r}")
            region = cleaned_region
        if normalized == "builder_id":
            return await self._begin_oidc(
                start_url=BUILDER_ID_START_URL,
                region=region or BUILDER_ID_REGION,
                identity="builder_id",
                provider="BuilderId",
                resolve_profile=False,
            )
        if normalized == "idc":
            cleaned = (start_url or "").strip()
            if not cleaned:
                raise MissingStartUrlError("idc login requires the company start URL")
            return await self._begin_oidc(
                start_url=cleaned,
                region=region or BUILDER_ID_REGION,
                identity="identity_center",
                provider="Enterprise",
                resolve_profile=True,
            )
        provider = _parse_provider(provider_str)
        session = await self._http()
        auth = await device.initiate_device_authorization(provider, session=session)
        return await self._register_pending(
            _PendingLogin(auth=auth, provider=provider),
            user_code=auth.user_code,
            verification_uri_complete=auth.verification_uri_complete,
            expires_at=auth.expires_at,
        )

    async def _begin_oidc(
        self, *, start_url: str, region: str, identity: str, provider: str, resolve_profile: bool
    ) -> dict[str, Any]:
        """Register a fresh SSO-OIDC client and start its device authorization."""
        session = await self._http()
        client = await builder_id.register_client(region, session=session)
        auth = await builder_id.start_device_authorization(
            client, region=region, start_url=start_url, session=session
        )
        return await self._register_pending(
            _PendingOidcLogin(
                client=client,
                auth=auth,
                region=region,
                identity=identity,
                provider=provider,
                resolve_profile=resolve_profile,
            ),
            user_code=auth.user_code,
            verification_uri_complete=auth.verification_uri_complete,
            expires_at=auth.expires_at,
        )

    async def _register_pending(
        self,
        pending: _PendingKind,
        *,
        user_code: str,
        verification_uri_complete: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        # Opaque handle so the deviceCode (the secret half of the flow) never
        # travels back to the browser; the poll endpoint accepts only this id.
        login_id = secrets.token_urlsafe(16)
        async with self._lock:
            # Evict pending logins whose device code already expired: an abandoned
            # login (UI cancel / "start over" resets client state without telling
            # the server) is otherwise never polled again, so repeated
            # start-and-abandon would grow this process-lifetime dict without bound.
            now = datetime.now(timezone.utc)
            expired = [lid for lid, entry in self._pending.items() if _expires_at(entry) <= now]
            for lid in expired:
                _release(self._pending.pop(lid))
            self._pending[login_id] = pending
        return {
            "login_id": login_id,
            "user_code": user_code,
            "verification_uri_complete": verification_uri_complete,
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        }

    async def poll_device(self, login_id: str) -> dict[str, Any]:
        """One non-blocking poll of a pending login.

        The caller loops, not us: {status: pending|authorized|expired|error},
        plus provider on authorized. Raises UnknownLoginError for a stale/foreign id.
        """
        async with self._lock:
            pending = self._pending.get(login_id)
        if pending is None:
            raise UnknownLoginError(login_id)

        if isinstance(pending, _PendingLoopbackLogin):
            return await self._poll_loopback(login_id, pending)

        if datetime.now(timezone.utc) >= _expires_at(pending):
            await self._forget(login_id)
            return {"status": "expired"}

        if isinstance(pending, _PendingOidcLogin):
            return await self._poll_oidc(login_id, pending)

        session = await self._http()
        url = f"{social_service_url()}/oauth/device/poll"
        payload = {"deviceCode": pending.auth.device_code, "clientId": USER_AGENT}
        async with session.post(url, json=payload, headers=_HEADERS) as resp:
            if resp.status != 200:
                # Transient service hiccup: the flow's own expiry bounds retries,
                # so report pending rather than killing an approvable login.
                body = await resp.text()
                logger.warning("device poll HTTP %s: %s", resp.status, body)
                return {"status": "pending"}
            try:
                data = await resp.json()
            except (aiohttp.ClientError, ValueError):
                # Malformed 200 body: treat as a transient hiccup, not a crash;
                # the flow's expiry still bounds the caller's retries.
                logger.warning("device poll returned undecodable body", exc_info=True)
                return {"status": "pending"}
        if not isinstance(data, dict):
            logger.warning("device poll returned non-object body: %r", type(data).__name__)
            return {"status": "pending"}

        status = data.get("status", "")
        if status == "authorization_pending":
            return {"status": "pending"}
        if status == "expired_token":
            await self._forget(login_id)
            return {"status": "expired"}
        if status == "authorized":
            try:
                token = device._token_from_poll(data, pending.provider)
            except device.DeviceAuthError as err:
                # Approved but unusable (e.g. no profile ARN) — surface as error,
                # and drop the entry so the dashboard restarts cleanly.
                logger.warning("authorized device poll rejected: %s", err)
                await self._forget(login_id)
                return {"status": "error"}
            return await self._persist_and_finish(login_id, token)
        # invalid_token and anything unrecognized: unrecoverable for this login_id.
        logger.warning("device poll returned status %r", status)
        await self._forget(login_id)
        return {"status": "error"}

    async def _poll_oidc(self, login_id: str, pending: _PendingOidcLogin) -> dict[str, Any]:
        """One non-blocking poll of an SSO-OIDC (Builder ID / IdC) pending login."""
        session = await self._http()
        try:
            token = await builder_id.poll_token_once(
                pending.client,
                pending.auth,
                region=pending.region,
                identity=pending.identity,
                provider=pending.provider,
                session=session,
            )
        except builder_id.BuilderIdAuthError as err:
            # expired_token and terminal rejections: unrecoverable for this login_id.
            logger.warning("oidc device poll failed: %s", err)
            await self._forget(login_id)
            expired = "expired" in str(err)
            return {"status": "expired" if expired else "error"}
        if token is None:
            return {"status": "pending"}
        if pending.resolve_profile:
            # An IdC token is unusable without a profile ARN (the store itself drops
            # it), so resolution failures must end the login loudly, not save junk.
            try:
                profiles = await control_plane.list_available_profiles(
                    token.access_token, region=pending.region, session=session
                )
            except control_plane.ControlPlaneError as err:
                logger.warning("could not resolve IdC profile ARN: %s", err)
                await self._forget(login_id)
                return {"status": "error"}
            if not profiles:
                logger.warning("IdC login has no available Kiro profiles")
                await self._forget(login_id)
                return {"status": "error"}
            if len(profiles) > 1:
                # Multi-profile selection UX is a documented follow-up; until then
                # the first profile wins, and the choice is visible in the log.
                logger.info(
                    "IdC login has %d profiles; using %r",
                    len(profiles),
                    profiles[0].profile_name,
                )
            token.profile_arn = profiles[0].arn
        return await self._persist_and_finish(login_id, token)

    async def _persist_and_finish(self, login_id: str, token: KasToken) -> dict[str, Any]:
        """Save an approved token; the shared terminal step of every poll flavor.

        Runs under the pending lock, save included, and only while the login is
        still registered. ``cancel`` takes the same lock, so it either runs first
        (the entry is gone and nothing is ever written — the poll answers
        ``UnknownLoginError``) or after the write has landed. A cancel can never
        return while a credential the user abandoned is still on its way to disk,
        and two polls of one approved login persist once. The write is one small
        file, so other logins wait milliseconds.
        """
        async with self._lock:
            if login_id not in self._pending:
                raise UnknownLoginError(login_id)
            result = await self._save_token(token)
            self._pending.pop(login_id, None)
        return result

    async def _save_token(self, token: KasToken) -> dict[str, Any]:
        """Write an approved token to the store and shape the poll's terminal answer.

        Takes no lock, so ``_persist_and_finish`` can run it while holding
        ``self._lock`` to keep the login cancellable until the write lands.
        """
        try:
            await asyncio.to_thread(self._store.save, token)
        except TokenStoreError as err:
            # Disk full / read-only store: the login was approved but we cannot
            # persist it. Report error (not authorized); the caller drops the
            # pending entry so a retry starts a fresh flow rather than looping.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the OSError only, never the token value
            logger.warning("could not persist approved KAS token: %s", err)
            # The one poll error a dashboard must NOT retry on another transport:
            # the store itself is unwritable, so every flavor would fail the same way.
            return {"status": "error", "code": "token_store_failed"}
        return {"status": "authorized", "provider": token.provider}

    async def begin_loopback(self, provider_str: str) -> dict[str, Any]:
        """Start a loopback (PKCE) social login; returns the portal URL to open.

        Only Google/GitHub run through Kiro's portal; Builder ID / IdC stay on the
        device flow (as in kiro-cli). Binding one of the Cognito-allowlisted ports
        happens HERE, synchronously, so an all-ports-busy host fails the begin with a
        coded error the dashboard can turn into an immediate device-flow fallback —
        rather than a listener that never answers.

        Raises ValueError for a non-social provider, LoopbackUnavailableError when
        the transport is not loopback for this install shape or no callback port
        can be bound.
        """
        provider = _parse_provider(provider_str)
        if select_transport() is not Transport.LOOPBACK:
            raise LoopbackUnavailableError("loopback transport not available on this install")
        try:
            # bind() is a syscall on the serving loop; tiny for loopback, but the
            # gateway's no-blocking-call rule holds regardless of expected cost.
            sock, port = await asyncio.to_thread(portal.bind_allowed_port)
        except portal.PortalAuthError as err:
            raise LoopbackUnavailableError(str(err)) from err
        verifier = portal.generate_code_verifier()
        state = portal.generate_state()
        auth_url = portal.build_auth_url(port, state, portal.generate_code_challenge(verifier))
        task = asyncio.create_task(
            self._run_loopback(sock, port, provider, state, verifier),
            name=f"kas-loopback-{port}",
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=LOOPBACK_TIMEOUT_SECS)
        result = await self._register_pending(
            _PendingLoopbackLogin(task=task, provider=provider, expires_at=expires_at),
            user_code="",
            verification_uri_complete=auth_url,
            expires_at=expires_at,
        )
        # The dashboard opens this URL itself: the browser is the user's, not ours.
        result["auth_url"] = auth_url
        result["port"] = port
        return result

    async def _run_loopback(
        self,
        sock: Any,
        port: int,
        provider: SocialProvider,
        state: str,
        verifier: str,
    ) -> KasToken:
        """Serve the callback and exchange the code. Owns the socket.

        Returns the token WITHOUT persisting it: the poll persists through
        ``_save_token`` under the pending lock, only while the login is still
        registered, so a cancelled login ("use a code instead", start over) can
        never leave a credential behind that the user did not see land.
        """
        try:
            callback = await portal.wait_for_callback(
                sock, port, state, timeout_secs=LOOPBACK_TIMEOUT_SECS
            )
            redirect_uri = portal.rebuild_redirect_uri(
                port, callback["path"], callback["login_option"]
            )
            # The portal page is where the user actually picks Google vs GitHub;
            # the button they clicked here only chose the transport. Trust the
            # callback's login_option so the stored provider matches the account.
            chosen = _social_from_login_option(callback["login_option"]) or provider
            session = await self._http()
            return await portal.exchange_code(
                chosen, callback["code"], verifier, redirect_uri, session=session
            )
        finally:
            # The listener normally closes the socket with its runner; closing an
            # already-closed socket is a no-op, so this only matters for the path
            # where serving never started (cancel racing setup).
            await asyncio.to_thread(sock.close)

    async def _poll_loopback(self, login_id: str, pending: _PendingLoopbackLogin) -> dict[str, Any]:
        """Report the listener task's state; terminal outcomes drop the entry.

        A timeout or a listener failure carries ``code: loopback_failed`` so the
        dashboard can degrade to the device flow for the same provider without a
        detour through an error screen.
        """
        if not pending.task.done():
            if datetime.now(timezone.utc) < pending.expires_at:
                return {"status": "pending"}
            # Deadline passed but the listener has not unwound yet: same outcome as
            # its own timeout, reported now rather than one poll later.
            await self._forget(login_id)
            return {"status": "expired", "code": "loopback_timeout"}
        # Classify the terminal answer under the pending lock so two polls of one
        # finished task agree on who owns it; the success path then hands off to
        # ``_persist_and_finish``, which re-checks registration under the same
        # lock before writing, so a cancel landing in between wins and nothing
        # is persisted.
        async with self._lock:
            if self._pending.get(login_id) is not pending:
                raise UnknownLoginError(login_id)
            if pending.task.cancelled():
                self._pending.pop(login_id, None)
                return {"status": "error", "code": "loopback_cancelled"}
            err = pending.task.exception()
            if err is not None:
                self._pending.pop(login_id, None)
        if err is None:
            return await self._persist_and_finish(login_id, pending.task.result())
        if isinstance(err, portal.PortalTimeoutError):
            return {"status": "expired", "code": "loopback_timeout"}
        logger.warning("loopback login failed: %s", err)
        return {"status": "error", "code": "loopback_failed", "error": str(err)}

    async def cancel(self, login_id: str) -> None:
        """Abandon a pending login, releasing its callback port if it holds one.

        Idempotent: an unknown or already-finished id is a no-op, because the
        dashboard cancels on every 'start over' / 'use a code instead' click.
        """
        await self._forget(login_id)

    async def logout(self, identity: str) -> None:
        """Delete the stored token for one identity kind.

        Raises ValueError for an identity outside the known kinds (the store's own
        path guard), so a typo can never unlink an arbitrary file.
        """
        await asyncio.to_thread(self._store.delete, identity)

    async def _forget(self, login_id: str) -> None:
        async with self._lock:
            entry = self._pending.pop(login_id, None)
        if entry is not None:
            _release(entry)
