"""Browser credentials for Kiro's own Chromium: an edition's sign-in, put in by the gateway.

Some sites the agent is asked to open sit behind a company sign-in the agent cannot
do itself (a device-bound login, a hardware key). Kiro's Chromium starts with an
empty profile, so every such site is a login wall. An edition that already holds the
user's session for its own sites supplies it through
``PlatformContext.browser_credentials``
(:class:`~kiro_crew.platform.interfaces.BrowserCredentialProvider`); the public
build's Default supplies nothing, so the stock browser is unchanged.

**Opt-in.** Used only while ``browser.credentials.enabled`` is on, and only for
cookies on a domain in BOTH the provider's ``domains()`` and
``browser.credentials.domains``; an unset key takes the provider's
``default_settings()`` (:func:`effective_settings`).

**When.** The supervisor applies them through the relay's own CDP connection
(``Storage.setCookies`` on the browser target) after the browser is up and BEFORE
the daemon starts, so no agent op runs before they land. Every relaunch goes through
the same start. Before an agent ``navigate`` (or new tab) the supervisor re-pulls
when the provider's ``fingerprint()`` or the ``browser.credentials`` settings moved
since the last apply: a sign-in the person re-ran, or a switch they flipped, reaches
the next page. It also re-pulls once the earliest ``expires`` the provider gave for a
cookie it set has passed (:meth:`PlacedCookies.next_expiry`).

**Stale cookies go.** :class:`PlacedCookies` remembers, per browser context, which
provider cookies the gateway set and their ``expires``; never a value. At each
re-apply a remembered cookie whose ``expires`` passed, or that a pull which succeeded
leaves out, is overwritten with a past ``expires``, which deletes it. Only
cookies the gateway set are touched.

**Session cookies.** Every cookie is set without ``expires``. Chromium still
writes a session cookie into the profile's ``Cookies`` file, and a delete it has
not yet committed is lost when it stops, so the supervisor deletes every cookie it
set before it stops Kiro's Chromium (:meth:`PlacedCookies.delete_all`) and, once
the browser is gone, removes their rows from the file itself
(:func:`purge_from_profile`). shortcut: a browser that crashes or is killed while
the gateway is down keeps them on disk until the next start's apply overwrites or
drops them.

**Where never.** The user's own Chrome or Edge: those are the person's profile. A
start on either records ``skipped`` and applies nothing.

**Values never leave.** Nothing here returns, logs or raises a cookie value. The
status (:class:`CredentialStatus`) carries the provider's name, the applied count,
domains and fixed reason codes, plus the provider's own ``needs_refresh`` and
message; a provider exception and a CDP error are reduced to a code. The supervisor
writes one SEL row per apply that set anything: provider name, count, domains. The
value travels in exactly one place: the ``Storage.setCookies`` params the relay
writes to the browser.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from kiro_crew.browser_harness.budgets import CREDENTIAL_FINGERPRINT_S
from kiro_crew.browser_harness.credential_files import CredentialSourceError
from kiro_crew.config.sections_browser import CREDENTIAL_DOMAIN_PATTERN
from kiro_crew.platform.interfaces import BrowserCredentialTarget as Target

logger = logging.getLogger(__name__)

#: How long one provider call (``domains()``, ``cookies()`` or ``status()``) may
#: take. Each runs in a worker thread; past this the call reads as
#: ``provider_timeout`` and the browser starts without the cookies.
PROVIDER_TIMEOUT_SECS = 15.0
#: How long ``fingerprint()`` may take. It runs before every agent navigation, so it
#: must be cheap (a ``stat``); past this the navigation goes ahead with what the
#: browser already holds.
FINGERPRINT_TIMEOUT_SECS = CREDENTIAL_FINGERPRINT_S
#: Ceiling on the provider's status message, in characters.
STATUS_MESSAGE_CAP = 200

#: A host name a provider may declare: two or more DNS labels, lowercase. The same
#: shape the ``browser.credentials.domains`` write accepts.
_DECLARED_DOMAIN = re.compile(CREDENTIAL_DOMAIN_PATTERN)
#: Characters a cookie field may not carry (C0 controls and DEL): a domain lands in
#: the SEL row, so a newline must never reach it.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAME_SITE = frozenset({"Strict", "Lax", "None"})

STATE_NONE = "none"  # no provider
STATE_APPLIED = "applied"
STATE_SKIPPED = "skipped"  # the browser in use is the person's own
STATE_ERROR = "error"
STATE_PENDING = "pending"  # enabled; no Kiro Chromium holds the current settings yet
STATE_DISABLED = "disabled"  # a provider exists; browser.credentials.enabled is off
STATE_NEEDS_REFRESH = "needs_refresh"  # the provider's source is missing or expired

#: Reason codes that mean "the person has to sign in again", not "something broke".
_NEEDS_REFRESH_CODES = frozenset({"source_missing", "source_expired"})


class _Relay(Protocol):
    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CredentialSettings:
    """``browser.credentials``, resolved: is the provider used, and for which domains."""

    enabled: bool = False
    domains: tuple[str, ...] = ()


@dataclass
class CredentialStatus:
    """The ``credentials`` block of ``GET /api/browser/status``. Holds no value."""

    provider: str | None = None
    enabled: bool = False
    domains: tuple[str, ...] = ()
    state: str = STATE_NONE
    reason: str | None = None
    applied: int = 0
    applied_domains: tuple[str, ...] = ()
    #: Cookies landed per context written, in the order asked; one entry per context.
    applied_per_context: tuple[int, ...] = ()
    #: Cookies this apply deleted, over every context: expired, or left out.
    removed: int = 0
    applied_at: float | None = None
    provider_status: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "domains": list(self.domains),
            "state": self.state,
            "reason": self.reason,
            "applied": self.applied,
            "applied_per_context": list(self.applied_per_context),
            "removed": self.removed,
            "applied_at": self.applied_at,
            "provider_status": self.provider_status,
        }


def cookie_host(domain: str) -> str:
    """The host a cookie's ``domain`` names: lowercase, one leading dot removed."""
    host = domain.strip().lower()
    return host[1:] if host.startswith(".") else host


def normalize_cookie(raw: Any, now: float) -> dict[str, Any] | None:
    """A provider cookie as a CDP ``Network.CookieParam`` SESSION cookie, or ``None``.

    ``None`` for anything that cannot be placed or is already expired: no name, a
    domain that is not a host name, a non-string field, a control character, a
    non-boolean flag. ``expires``
    is only read to drop an expired cookie; it is never passed on.
    """
    if not isinstance(raw, Mapping):
        return None
    fields = [raw.get("name"), raw.get("domain"), raw.get("value", ""), raw.get("path") or "/"]
    strings = [t for t in fields if isinstance(t, str) and not _CONTROL.search(t)]
    if len(strings) != len(fields):
        return None
    name, domain, value, path = strings
    # The domain must name a host the way a declaration does: it lands in the SEL row.
    if not name or not _DECLARED_DOMAIN.fullmatch(cookie_host(domain)):
        return None
    secure, http_only = raw.get("secure", False), raw.get("httpOnly", False)
    if not isinstance(secure, bool) or not isinstance(http_only, bool):
        return None
    expires = raw.get("expires")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and 0 < expires <= now:
        return None
    same_site = raw.get("sameSite")
    same_site = same_site if same_site in _SAME_SITE else "Lax"
    return {
        "name": name,
        "value": value,
        "domain": domain.strip().lower(),
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
        # Chromium rejects SameSite=None without Secure; Lax is what it would
        # have treated the cookie as anyway.
        "sameSite": "Lax" if same_site == "None" and not secure else same_site,
    }


def normalize_declared_domains(domains: Iterable[Any]) -> tuple[str, ...]:
    """The declared domains that are well-formed host names, sorted.

    A single label (``com``, ``localhost``), an IP literal, a wildcard or anything
    with a port is dropped: a declaration that broad would let a provider set
    cookies for hosts it does not serve.
    """
    out: set[str] = set()
    for raw in domains:
        if not isinstance(raw, str):
            continue
        host = cookie_host(raw)
        if _DECLARED_DOMAIN.fullmatch(host) and not host.replace(".", "").isdigit():
            out.add(host)
    return tuple(sorted(out))


def domain_allowed(domain: str, declared: Sequence[str]) -> bool:
    """Is *domain* one of *declared*, or a subdomain of one?"""
    host = cookie_host(domain)
    return any(host == d or host.endswith("." + d) for d in declared)


def _covers_any(domain: str, hosts: Sequence[str]) -> bool:
    """Would a cookie on *domain* be sent to one of *hosts*? A cookie set with an
    explicit ``domain`` (dot or not) is a domain cookie, sent to every subdomain."""
    host = cookie_host(domain)
    return any(h == host or h.endswith("." + host) for h in hosts)


def provider_name(provider: Any) -> str | None:
    """The provider's display name, or ``None`` (the Default, or a broken provider)."""
    try:
        name = provider.name()
    except Exception:  # noqa: BLE001 - a broken provider reads as no provider
        logger.warning("browser credentials: provider name() raised; treated as absent")
        return None
    # Printable only: it lands in Settings and in the SEL row's text.
    return _printable(name, 80)


def effective_settings(section: Any, provider: Any) -> CredentialSettings:
    """Resolve ``browser.credentials`` against the provider's edition defaults.

    *section* is the parsed ``browser.credentials`` (a mapping or a dataclass with
    ``enabled`` and ``domains``). A key the owner left unset (``enabled`` ``None``,
    ``domains`` empty) takes the provider's ``default_settings()`` value; with
    neither, the provider is OFF and no domain is allowed.
    """
    if dataclasses.is_dataclass(section) and not isinstance(section, type):
        section = dataclasses.asdict(section)
    section = section if isinstance(section, Mapping) else {}
    enabled, domains = section.get("enabled"), section.get("domains") or None
    defaults: Mapping[str, Any] = {}
    if provider_name(provider) is not None and (enabled is None or domains is None):
        try:
            got = provider.default_settings()
        except Exception:  # noqa: BLE001 - absent or broken defaults mean "off"
            got = None
        defaults = got if isinstance(got, Mapping) else {}
    if not isinstance(enabled, bool):
        enabled = defaults.get("enabled") is True
    if not isinstance(domains, (list, tuple)):
        domains = defaults.get("domains")
    return CredentialSettings(
        enabled=enabled,
        domains=normalize_declared_domains(domains if isinstance(domains, (list, tuple)) else ()),
    )


async def provider_fingerprint(provider: Any) -> str | None:
    """The provider's ``fingerprint()``: a short string that changes when its source does.

    ``None`` when there is no provider, or the call fails or passes
    :data:`FINGERPRINT_TIMEOUT_SECS`: that means "cannot tell".
    """
    if provider_name(provider) is None:
        return None
    try:
        got = await asyncio.wait_for(
            asyncio.to_thread(provider.fingerprint), FINGERPRINT_TIMEOUT_SECS
        )
    except Exception:  # noqa: BLE001 - cannot tell is not a failure
        return None
    return str(got)[:256] if got is not None else None


def _printable(text: Any, cap: int) -> str | None:
    if not isinstance(text, str):
        return None
    clean = "".join(ch for ch in text if ch.isprintable()).strip()
    return clean[:cap] or None


async def _provider_status(provider: Any) -> dict[str, Any] | None:
    """The provider's own ``status()``, bounded, reduced to what Settings shows.

    ``needs_refresh`` (bool) and ``message`` (edition-authored, printable, at most
    :data:`STATUS_MESSAGE_CAP` characters). A raise, a timeout or a non-mapping is
    ``None``: it runs inside a browser start, which must not wait on it.
    """
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(provider.status), PROVIDER_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 - a broken or hung status is no status
        return None
    if not isinstance(raw, Mapping):
        return None
    return {
        "needs_refresh": raw.get("needs_refresh") is True,
        "message": _printable(raw.get("message"), STATUS_MESSAGE_CAP),
    }


def _expires(raw: Any) -> float | None:
    """A provider cookie's ``expires`` (unix seconds), or ``None`` for none."""
    got = raw.get("expires") if isinstance(raw, Mapping) else None
    if isinstance(got, (int, float)) and not isinstance(got, bool) and got > 0:
        return float(got)
    return None


_CookieKey = tuple[str, str, str]


def _key(cookie: Mapping[str, Any]) -> _CookieKey:
    # A host-only cookie names its host by ``url`` alone (``sign_in_guard``).
    domain = cookie.get("domain") or cookie_host(
        urlsplit(str(cookie.get("url", ""))).hostname or ""
    )
    return cookie["name"], domain, cookie["path"]


class PlacedCookies:
    """The provider cookies the gateway set in one browser, per context. Holds no value.

    Each entry keeps the cookie's CDP params minus ``value``, which is what deleting
    it needs, and the ``expires`` the provider gave (``None`` for none). Lives as
    long as the browser it describes: a new browser starts with a new one.
    """

    def __init__(self) -> None:
        self._by_context: dict[str, dict[_CookieKey, tuple[dict[str, Any], float | None]]] = {}
        #: An expired cookie was deleted and the pull that would renew it has not
        #: finished (it timed out or was cancelled): the next navigation re-pulls.
        self._renewal_due = False

    def retain(self, contexts: Iterable[str]) -> None:
        """Forget the contexts not in *contexts*: a closed chat's context is gone."""
        keep = set(contexts)
        self._by_context = {c: v for c, v in self._by_context.items() if c in keep}

    def next_expiry(self) -> float | None:
        """When the next re-pull is due: the earliest ``expires`` of any remembered
        cookie, ``0.0`` while a renewal is owed, or ``None``."""
        if self._renewal_due:
            return 0.0
        times = [e for held in self._by_context.values() for _, e in held.values() if e]
        return min(times) if times else None

    def _expired(self, context: str, now: float) -> list[_CookieKey]:
        held = self._by_context.get(context, {})
        return [k for k, (_, e) in held.items() if e is not None and e <= now]

    def _omitted(self, context: str, supplied: set[_CookieKey]) -> list[_CookieKey]:
        return [k for k in self._by_context.get(context, {}) if k not in supplied]

    async def _delete(self, relay: _Relay, context: str, keys: list[_CookieKey]) -> int:
        """Delete *keys* from *context*; forget each only once its delete landed.

        A past ``expires`` deletes the cookie, and nothing is written to disk. A
        delete Chromium refused stays remembered, so the next apply tries again.
        """
        held = self._by_context.get(context, {})
        gone = [{**held[k][0], "value": "", "expires": 1} for k in keys]
        landed = await _set_cookies(relay, gone, context)
        for cookie in landed:
            held.pop(_key(cookie), None)
        return len(landed)

    def keys(self) -> list[_CookieKey]:
        """Every remembered cookie, as ``(name, domain, path)``, in any context."""
        return sorted({k for held in self._by_context.values() for k in held})

    async def delete_all(self, relay: _Relay) -> int:
        """Delete every remembered cookie in every context: the browser is stopping."""
        removed = 0
        for context in list(self._by_context):
            removed += await self._delete(relay, context, list(self._by_context[context]))
        return removed

    def _record(
        self, context: str, landed: list[dict[str, Any]], expires: dict[_CookieKey, float | None]
    ) -> None:
        held = self._by_context.setdefault(context, {})
        for cookie in landed:
            params = {k: v for k, v in cookie.items() if k != "value"}
            held[_key(cookie)] = (params, expires.get(_key(cookie)))


async def _provider_cookies(
    provider: Any, target: Any, settings: CredentialSettings
) -> tuple[list[tuple[dict[str, Any], float | None]], str | None]:
    """The provider's cookies both lists allow, as session cookies with the ``expires``
    the provider gave; and an error code."""
    try:
        declared = normalize_declared_domains(
            await asyncio.wait_for(asyncio.to_thread(provider.domains), PROVIDER_TIMEOUT_SECS)
        )
        raw = await asyncio.wait_for(
            asyncio.to_thread(provider.cookies, target), PROVIDER_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        return [], "provider_timeout"
    except CredentialSourceError as exc:
        # A missing or refused source file is a state the person can fix.
        return [], exc.code
    except Exception as exc:  # noqa: BLE001 - its text could carry a value; reduced to a code
        # Logs an exception class name or a state and two counts, never a cookie value.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.warning("browser credentials: provider failed (%s)", type(exc).__name__)
        return [], "provider_failed"
    if not isinstance(raw, (list, tuple)):
        return [], "provider_bad_shape"
    from kiro_crew.browser_harness.sign_in_guard import declared_hosts

    # A provider that signs per origin never puts a sign-in host's cookie here.
    sign_in_hosts = declared_hosts(provider)
    now = time.time()
    kept = [
        (c, _expires(item))
        for c, item in ((normalize_cookie(item, now), item) for item in raw)
        # BOTH lists: what the edition says it serves AND what the owner allowed.
        if c is not None
        and domain_allowed(c["domain"], declared)
        and domain_allowed(c["domain"], settings.domains)
        and not _covers_any(c["domain"], sign_in_hosts)
    ]
    return kept, ("source_expired" if raw and not kept else None)


async def _set_cookies(
    relay: _Relay, cookies: list[dict[str, Any]], browser_context_id: str = ""
) -> list[dict[str, Any]]:
    """``Storage.setCookies``; on a refused batch, one at a time. Returns those that landed.

    With *browser_context_id* the cookies go into that browser context only, never
    into the default one the person's tabs share.
    """
    if not cookies:
        return []
    scope = {"browserContextId": browser_context_id} if browser_context_id else {}
    try:
        await relay.send("Storage.setCookies", {"cookies": cookies, **scope})
        return cookies
    except Exception:  # noqa: BLE001 - one cookie Chromium refuses fails the batch; find it
        pass
    landed = []
    for cookie in cookies:
        try:
            await relay.send("Storage.setCookies", {"cookies": [cookie], **scope})
            landed.append(cookie)
        except Exception:  # noqa: BLE001 - left out; the CDP text is not repeated
            pass
    return landed


async def apply(
    relay: _Relay,
    *,
    provider: Any,
    target: Target,
    settings: CredentialSettings,
    contexts: Sequence[str] = ("",),
    placed: PlacedCookies | None = None,
) -> CredentialStatus:
    """Write the provider's cookies into the browser behind *relay*, if enabled.

    *contexts* are the browser contexts to fill, ``""`` being the default one and
    any other a per-chat context (``browser.isolation``); the provider is asked
    ONCE and the same cookies go into each. ``applied`` counts the first context's
    landing; ``applied_per_context`` keeps every context's. With *placed*, each
    context's landed cookies are remembered there. The ones it already held whose
    ``expires`` passed are deleted first, before the provider is asked, and so even
    while the switch is off; after a pull that succeeded, the ones it leaves out are
    deleted too (``removed``). A failed pull deletes nothing more: a hiccup must not
    sign anyone out.
    Never raises for a provider or a CDP failure: the browser is still
    useful without the cookies, so the outcome is a status, not an exception.
    Cancellation propagates.
    """
    name = provider_name(provider)
    status = CredentialStatus(provider=name, enabled=settings.enabled, domains=settings.domains)
    if target.kind != "kiro_chromium":
        status.state, status.reason = STATE_SKIPPED, "user_browser"
        return status
    if name is None:
        return status
    if placed is not None:
        # Before the pull, from memory alone: a slow provider or a switched-off one
        # never keeps an expired cookie in the browser.
        now = time.time()
        for context in contexts:
            status.removed += await placed._delete(relay, context, placed._expired(context, now))
        placed._renewal_due = placed._renewal_due or (bool(status.removed) and settings.enabled)
    if not settings.enabled:
        status.state, status.reason = STATE_DISABLED, "disabled"
        return status
    pulled, error = await _provider_cookies(provider, target, settings)
    cookies = [c for c, _ in pulled]
    expires = {_key(c): e for c, e in pulled}
    # ``source_expired`` is a pull that worked and found nothing still valid.
    supplied = set(expires) if error in (None, "source_expired") else None
    landed: list[dict[str, Any]] = []
    per_context: list[int] = []
    for index, context in enumerate(contexts):
        here = await _set_cookies(relay, cookies, context)
        per_context.append(len(here))
        if index == 0:
            landed = here
        if placed is not None:
            if supplied is not None:
                omitted = placed._omitted(context, supplied)
                status.removed += await placed._delete(relay, context, omitted)
            placed._record(context, here, expires)
    if placed is not None and supplied is not None and "" in contexts:
        # Paid only by a pull that worked across the whole browser: a failed pull,
        # or one chat's new context being seeded, still owes it.
        placed._renewal_due = False
    status.applied = len(landed)
    status.applied_per_context = tuple(per_context)
    status.applied_domains = tuple(sorted({cookie_host(c["domain"]) for c in landed}))
    status.provider_status = await _provider_status(provider)
    status.reason = error
    status.applied_at = time.time()
    if (status.provider_status or {}).get("needs_refresh") or error in _NEEDS_REFRESH_CODES:
        status.state = STATE_NEEDS_REFRESH
    elif error and not status.applied:
        status.state = STATE_ERROR
    else:
        status.state = STATE_APPLIED
    # Logs an exception class name or a state and two counts, never a cookie value.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info(
        "browser credentials: %s (%d/%d, %d removed)",
        status.state,
        status.applied,
        len(cookies),
        status.removed,
    )
    return status


__all__ = [
    "purge_from_profile",
    "CredentialSettings",
    "CredentialStatus",
    "PlacedCookies",
    "Target",
    "apply",
    "cookie_host",
    "domain_allowed",
    "effective_settings",
    "normalize_cookie",
    "normalize_declared_domains",
    "provider_fingerprint",
    "provider_name",
]


#: Where Chromium keeps the profile's cookie database, by release.
_COOKIE_FILES = (("Default", "Cookies"), ("Default", "Network", "Cookies"))


def purge_from_profile(profile: Path, keys: Iterable[_CookieKey]) -> int:
    """Remove *keys*' rows from Kiro's stopped Chromium's cookie files; the rows removed.

    Only with the browser gone: Chromium holds the file open while it runs. A row
    matches on name, path and host, with or without the domain cookie's leading
    dot. A file that is missing, a link or not a cookie database is left alone.
    """
    import sqlite3

    wanted = [
        (name, path, cookie_host(domain), "." + cookie_host(domain)) for name, domain, path in keys
    ]
    removed = 0
    if not wanted:
        return 0
    for parts in _COOKIE_FILES:
        db = profile.joinpath(*parts)
        if db.is_symlink() or not db.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db), timeout=1)
            try:
                with conn:
                    for name, path, host, dotted in wanted:
                        removed += conn.execute(
                            "DELETE FROM cookies WHERE name = ? AND path = ? AND host_key IN (?, ?)",
                            (name, path, host, dotted),
                        ).rowcount
            finally:
                conn.close()
        except sqlite3.Error:
            logger.warning("browser: could not clear the provider cookies from %s", db.name)
    return removed
