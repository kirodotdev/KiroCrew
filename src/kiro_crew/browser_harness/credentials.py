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
the next page. A cookie a re-pull omits stays until the browser closes.

**Session cookies.** Every cookie is set without ``expires``, so nothing is written
into Kiro's profile on disk and all of it is gone when the browser closes.

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
from typing import Any, Protocol

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


async def _provider_cookies(
    provider: Any, target: Any, settings: CredentialSettings
) -> tuple[list[dict[str, Any]], str | None]:
    """The provider's cookies both lists allow, as session cookies; and an error code."""
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
    now = time.time()
    kept = [
        c
        for c in (normalize_cookie(item, now) for item in raw)
        # BOTH lists: what the edition says it serves AND what the owner allowed.
        if c is not None
        and domain_allowed(c["domain"], declared)
        and domain_allowed(c["domain"], settings.domains)
    ]
    return kept, ("source_expired" if raw and not kept else None)


async def _set_cookies(relay: _Relay, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``Storage.setCookies``; on a refused batch, one at a time. Returns those that landed."""
    if not cookies:
        return []
    try:
        await relay.send("Storage.setCookies", {"cookies": cookies})
        return cookies
    except Exception:  # noqa: BLE001 - one cookie Chromium refuses fails the batch; find it
        pass
    landed = []
    for cookie in cookies:
        try:
            await relay.send("Storage.setCookies", {"cookies": [cookie]})
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
) -> CredentialStatus:
    """Write the provider's cookies into the browser behind *relay*, if enabled.

    Never raises for a provider or a CDP failure: the browser is still useful without
    the cookies, so the outcome is a status, not an exception. Cancellation propagates.
    """
    name = provider_name(provider)
    status = CredentialStatus(provider=name, enabled=settings.enabled, domains=settings.domains)
    if target.kind != "kiro_chromium":
        status.state, status.reason = STATE_SKIPPED, "user_browser"
        return status
    if name is None:
        return status
    if not settings.enabled:
        status.state, status.reason = STATE_DISABLED, "disabled"
        return status
    cookies, error = await _provider_cookies(provider, target, settings)
    landed = await _set_cookies(relay, cookies)
    status.applied = len(landed)
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
    logger.info("browser credentials: %s (%d/%d)", status.state, status.applied, len(cookies))
    return status


__all__ = [
    "CredentialSettings",
    "CredentialStatus",
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
