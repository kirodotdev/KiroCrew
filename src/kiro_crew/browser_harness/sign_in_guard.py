"""Keep Kiro's Chromium off an edition's sign-in hosts, and sign a site in for one origin.

Some sites send the browser to a company sign-in service (the "sign-in host") that
the agent cannot use itself. An edition that can sign a site in out of band has two
OPTIONAL provider methods (:class:`~kiro_crew.platform.interfaces.BrowserCredentialProvider`,
read with ``getattr``): ``sign_in_hosts()``, the hosts the browser must never reach,
and ``sign_in_for_origin(target, origin, sign_in_url, *, site_cookies)``, which
answers the cookies the SITE sets for that one origin once signed in. The public
Default has neither, so nothing here runs there.

**When.** :func:`declared_hosts` is non-empty only for a provider with both methods
and a valid host; the supervisor turns the guard on after each credential apply on
Kiro's own Chromium with ``browser.credentials.enabled`` on, and off otherwise.

**Where.** ``Fetch.enable`` on the relay's own session of every agent tab (the one
the relay adopts for focus emulation, ``CdpRelay._adopt_once``), for the tab's life:
a site's token can run out at any time and the page then sends itself to sign in.
Two kinds of pattern (:func:`patterns`): every ``Document`` response, to catch a 3xx
whose ``Location`` is a sign-in host; and every request to a sign-in host, of any
resource type. Both are failed, so the browser never sends a request to a sign-in
host and the sign-in host never sets a cookie in it. Only a tab's main frame is
signed; a frame is blocked only.

**Which origin.** The origin of the URL whose response redirected (or, for a
script that went to the sign-in host itself, the page the tab is on), never the
sign-in URL. It must be ``https``, in BOTH ``domains()`` and
``browser.credentials.domains``, not a sign-in host, and either the host the agent's
own ``navigate`` asked for (:meth:`SignInGuard.begin`) or a host already signed in
this browser context (a renewal). Anything else is ``origin_not_requested``.

**Which cookies.** Each one the provider returns must name ``origin``'s host
exactly (or no domain): it is written host-only (``url=origin``), ``secure``, as a
session cookie, into that tab's browser context, then read back by name. A cookie
for any other host is dropped (``cross_host`` when none is left), so a sign-in for
one site never reaches another. After each op and each sign every cookie on a
sign-in host is deleted (:meth:`SignInGuard.sweep`).

**Values never leave.** The agent and the SEL row get the host, the outcome and a
fixed reason code; never a URL, a cookie name or a value.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from kiro_crew.browser_harness import credentials
from kiro_crew.browser_harness.budgets import SIGN_IN_PROVIDER_S
from kiro_crew.config.sections_browser import CREDENTIAL_DOMAIN_PATTERN
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: The two optional provider methods, read with ``getattr``.
HOSTS_METHOD = "sign_in_hosts"
SIGN_METHOD = "sign_in_for_origin"

OUTCOME_SIGNED_IN = "signed_in"
OUTCOME_NOT_SIGNED = "not_signed"
OUTCOME_HELD = "held"

#: The codes a provider may answer; anything else reads as ``other``.
PROVIDER_REASONS = frozenset(
    {
        "not_signed_in",
        "device_check_failed",
        "signer_missing",
        "signer_timeout",
        "refused",
        "other",
    }
)

#: What the agent is told per reason. Generic: an edition names its own service
#: in the provider's ``message``.
REASON_TEXT = {
    "not_signed_in": "ask the person to sign in again on this machine",
    "device_check_failed": "signing in again will not help; the person must fix the device check",
    "signer_missing": "this machine cannot sign in to this site by itself",
    "signer_timeout": "the sign-in timed out; try again later",
    "refused": "this site cannot be signed in to automatically",
    "other": "could not sign in automatically",
    "origin_not_requested": "the page was sent to sign in for a site the agent did not open",
    "provider_timeout": "the sign-in timed out; try again later",
    "provider_failed": "could not sign in automatically",
    "provider_bad_shape": "could not sign in automatically",
    "cross_host": "the sign-in answered for another site, so nothing was set",
    "readback_missing": "the browser did not keep the sign-in",
    "still_redirected": "the site asked to sign in again right after signing in",
    "nav_failed": "signed in, but the page was not opened again; open it yourself",
    "moved_on": "signed in; the page was left where the last action took it",
    "resubmit": "signed in; the form was not sent, so send it again",
}

#: A host signed in a context this recently that bounces again is not re-signed.
RENEW_WINDOW_S = 30.0
#: Outcomes kept per tab until an op reads them.
_OUTCOME_CAP = 8
_MESSAGE_CAP = 200
_HOST = re.compile(CREDENTIAL_DOMAIN_PATTERN)


def declared_hosts(provider: Any) -> tuple[str, ...]:
    """The provider's sign-in hosts, exact and lowercase; ``()`` without both methods."""
    hosts_of, sign = getattr(provider, HOSTS_METHOD, None), getattr(provider, SIGN_METHOD, None)
    if not callable(hosts_of) or not callable(sign):
        return ()
    try:
        raw = hosts_of()
    except Exception:  # noqa: BLE001 - a broken declaration is no declaration
        logger.warning("browser sign-in: provider %s() raised; none used", HOSTS_METHOD)
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    out = set()
    for host in raw:
        if isinstance(host, str):
            host = host.strip().lower()
            if _HOST.fullmatch(host) and not host.replace(".", "").isdigit():
                out.add(host)
    return tuple(sorted(out))


def patterns(hosts: Sequence[str]) -> list[dict[str, str]]:
    """The ``Fetch.enable`` patterns: every document response, every sign-in-host request."""
    return [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"}] + [
        # Any scheme and port; ``classify`` keeps the exact host, so a longer name
        # this wildcard also matches goes on.
        {"urlPattern": f"*://{host}*", "requestStage": "Request"}
        for host in hosts
    ]


def host_of(url: Any) -> str:
    """*url*'s host, lowercase, one trailing dot dropped (``host.`` is ``host``)."""
    try:
        host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    except ValueError:
        return ""
    return _bare(host)


def _bare(host: str) -> str:
    """*host* without one trailing dot: ``host.`` names the same host."""
    return host[:-1] if host.endswith(".") else host


def origin_of(url: Any) -> str:
    """``https://host[:port]`` of an ``https`` *url*, else ``""``."""
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        port = parts.port if parts is not None else None
    except ValueError:
        return ""
    if parts is None or parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    host = parts.hostname.lower()
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def classify(event: Mapping[str, Any], hosts: Sequence[str]) -> tuple[str, str]:
    """``("continue" | "block" | "sign", sign_in_url)`` for one ``Fetch.requestPaused``."""
    request = event.get("request") or {}
    url = str(request.get("url") or "")
    status = event.get("responseStatusCode")
    if status is None:
        if host_of(url) not in hosts:
            return "continue", ""
        return ("sign" if event.get("resourceType") == "Document" else "block"), url
    if isinstance(status, int) and 300 <= status < 400:
        location = _header(event, "location")
        to = _join(url, location) if location else ""
        if to and host_of(to) in hosts:
            return "sign", to
    return "continue", ""


def _join(url: str, location: str) -> str:
    """*location* resolved against *url*; ``""`` for one ``urljoin`` cannot parse."""
    try:
        return urljoin(url, location)
    except ValueError:
        return ""


def _header(event: Mapping[str, Any], name: str) -> str:
    for item in event.get("responseHeaders") or []:
        if isinstance(item, Mapping) and str(item.get("name", "")).lower() == name:
            return str(item.get("value") or "")
    return ""


def _set_cookie_pairs(event: Mapping[str, Any]) -> list[dict[str, str]]:
    """``name=value`` of each ``Set-Cookie`` on the paused response (CDP joins them by newline)."""
    out = []
    for item in event.get("responseHeaders") or []:
        if not isinstance(item, Mapping) or str(item.get("name", "")).lower() != "set-cookie":
            continue
        for line in str(item.get("value") or "").split("\n"):
            pair = line.split(";", 1)[0]
            if "=" in pair:
                name, value = pair.split("=", 1)
                if name.strip():
                    out.append({"name": name.strip(), "value": value.strip()})
    return out


def fence(raw: Any, origin: str) -> list[dict[str, Any]]:
    """The provider's cookies that name *origin*'s host exactly, as host-only CookieParams."""
    host = host_of(origin)
    if not host or not isinstance(raw, (list, tuple)):
        return []
    now = time.time()
    out = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        domain = item.get("domain")
        if domain is not None and (not isinstance(domain, str) or domain.strip().lower() != host):
            continue
        cookie = credentials.normalize_cookie({**item, "domain": host, "secure": True}, now)
        if cookie is None:
            continue
        cookie.pop("domain")
        out.append({**cookie, "url": origin})
    return out


async def _never_denied(_keys: list[str]) -> bool:
    return False


#: A session whose ``Fetch.enable`` was sent but not answered: it may intercept.
_UNKNOWN: tuple[str, ...] = ("",)


@dataclass
class SignInGuard:
    """The interception for one browser connection. Holds no cookie value.

    *relay* is the :class:`~kiro_crew.browser_harness.relay.CdpRelay`; *provider*
    the edition's; *hosts* :func:`declared_hosts`; *allowed* the effective
    ``browser.credentials.domains`` and *declared* the provider's ``domains()``.
    """

    relay: Any
    provider: Any
    hosts: tuple[str, ...]
    allowed: tuple[str, ...]
    declared: tuple[str, ...]
    headless: bool = False
    #: Whether the person holds *target* (takeover); the supervisor wires the live hub.
    held: Callable[[str], bool] = lambda _target: False
    #: ``browser.credentials`` as it is NOW (the supervisor's reader), asked before the
    #: provider and again before a write, so a switch turned off or a domain removed
    #: since the last apply stops a sign.
    settings_of: Callable[[Any], credentials.CredentialSettings] | None = None
    #: Whether the ceiling denies browsing for any of these session keys.
    browse_denied: Callable[[list[str]], Awaitable[bool]] = field(default=_never_denied)
    #: Off while ``reconcile`` has unhooked it; a sign in flight then writes nothing.
    active: bool = True
    #: Bumped by every ``reconcile``: a sign that began under older settings is dropped.
    generation: int = 0
    #: The sign-in hosts each session's ``Fetch`` holds now, by session id (``None`` is
    #: the browser target): what has really been sent, never what is wanted. A session
    #: not here holds none.
    _fetch: dict[str | None, tuple[str, ...]] = field(default_factory=dict)
    #: Serializes every ``Fetch.enable`` / ``Fetch.disable`` this guard sends.
    _fetch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The hosts interception should hold; ``()`` is off.
    _want: tuple[str, ...] = ()
    #: Whether the last :func:`converge` landed all of :attr:`_want`. Until it does,
    #: the supervisor runs it again (``refresh_credentials_if_source_changed``).
    settled: bool = False
    #: The cookies a sign set, per context; deleted before the browser stops.
    placed: credentials.PlacedCookies = field(default_factory=credentials.PlacedCookies)
    #: Tab -> the hosts the agent asked for in the op running on it.
    _requested: dict[str, set[str]] = field(default_factory=dict)
    #: Tabs an agent ``navigate`` runs on: that op opens the page again, not the guard.
    _navigating: set[str] = field(default_factory=set)
    #: The host a ``tabs new`` asked for, and its caller; the next adopted tab gets them.
    _expect_new: str = ""
    _expect_caller: str = ""
    _expect_gate: Callable[[str], bool] | None = None
    _expect_url: str = ""
    #: Tab -> the runtime session key of the last op's caller on it (governance's key).
    _callers: dict[str, str] = field(default_factory=dict)
    #: Tab -> the URL gate of the last op's caller on it (``OpContext.url_allowed``): a
    #: renewal signs and re-opens only a page that caller may open.
    _gates: dict[str, Callable[[str], bool]] = field(default_factory=dict)
    #: Tab -> origins of URLs its caller's gate passed (the op's request, the tab's page):
    #: a same-origin sign-in hop (``/login``) is that origin's, whatever its path.
    _gated: dict[str, set[str]] = field(default_factory=dict)
    #: Tab -> how many ops have started on it: a renewal re-opens only if none did since.
    _ops: dict[str, int] = field(default_factory=dict)
    _new_tabs: list[str] = field(default_factory=list)
    _signed: dict[tuple[str, str], float] = field(default_factory=dict)
    _inflight: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = field(default_factory=dict)
    #: Tabs whose page the guard is re-opening: their landing is judged by the gate.
    _reopening: set[str] = field(default_factory=set)
    #: Tabs waiting on each sign in ``_inflight``.
    _waiters: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    _pending: dict[str, set[asyncio.Task[None]]] = field(default_factory=dict)
    _outcomes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # ── the relay's hooks ──

    async def on_adopted(self, target_id: str, session_id: str) -> None:
        """Turn interception on for one agent tab's resident session."""
        # A popup a page opened is its opener's chat's until an op runs on it.
        opener = self.relay.opener_of(target_id)
        if opener in self._callers:
            self.note_caller(target_id, self._callers[opener], self._gates.get(opener))
            # Only origins the opener's own gate passed: a popup's same-origin login hop.
            self._gated.setdefault(target_id, set()).update(self._gated.get(opener, ()))
        # A ``tabs new`` tab has no opener; a popup never takes that op's host or chat.
        if self._expect_new and not opener:
            self._requested[target_id] = {self._expect_new}
            self._new_tabs.append(target_id)
            self.note_caller(target_id, self._expect_caller, self._expect_gate)
            self._pass_gate(target_id, self._expect_url)
            self._expect_new = ""
        # Nothing while the guard is being turned off; else the hosts it holds.
        await self._set_fetch(session_id, self.hosts if self.active or self._want else ())

    async def _set_fetch(self, session_id: str | None, hosts: tuple[str, ...]) -> None:
        """Make *session_id* hold *hosts* (``()``: none), unless it already does.

        One transition at a time for the whole guard (an adoption's enable and a
        reconcile's disable never interleave). Before an enable is sent the session
        is marked unknown, so an enable that is cut or still unanswered is never
        read as "holds nothing": the next run disables or re-enables it."""
        async with self._fetch_lock:
            if self._fetch.get(session_id, ()) == hosts:
                return
            if hosts:
                wanted = patterns(hosts) if session_id is not None else patterns(hosts)[1:]
                self._fetch[session_id] = _UNKNOWN
                await self.relay.send("Fetch.enable", {"patterns": wanted}, session_id)
                self._fetch[session_id] = hosts
            else:
                await self.relay.send("Fetch.disable", {}, session_id)
                self._fetch.pop(session_id, None)

    async def on_browser_paused(self, msg: dict[str, Any]) -> None:
        """A request the browser-wide block paused: a target with no guarded session yet
        (a popup before the relay adopts it, a worker, a cross-process frame). Only
        sign-in-host patterns reach here; each is failed, none is signed."""
        event = msg.get("params") or {}
        request_id = event.get("requestId")
        if not isinstance(request_id, str):
            return
        blocked = host_of((event.get("request") or {}).get("url")) in self.hosts
        method = "Fetch.failRequest" if blocked else "Fetch.continueRequest"
        params = {
            "requestId": request_id,
            **({"errorReason": "BlockedByClient"} if blocked else {}),
        }
        try:
            await self.relay.send(method, params)
        except Exception:  # noqa: BLE001 - a request whose target closed needs no answer
            logger.debug("browser sign-in: browser-wide %s not answered", method, exc_info=True)

    async def on_event(self, target_id: str, session_id: str, msg: dict[str, Any]) -> None:
        if msg.get("method") != "Fetch.requestPaused":
            return
        event = msg.get("params") or {}
        request_id = event.get("requestId")
        if not isinstance(request_id, str):
            return
        action, sign_in_url = classify(event, self.hosts)
        if action == "continue" and self._off_gate_landing(target_id, event):
            # A page the guard re-opened went on to one its caller may not open.
            await self._answer(
                session_id,
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
            )
            return
        if action == "continue":
            await self._answer(session_id, "Fetch.continueRequest", {"requestId": request_id})
            return
        task = None
        if action == "sign" and event.get("frameId") == target_id:
            # Registered before the request is answered, so an op's ``settle`` that
            # runs as soon as its navigation fails still sees this sign.
            # Taken now, before any await: an op that starts meanwhile is a move.
            bounce = (self._ops.get(target_id, 0), target_id in self._navigating)
            task = asyncio.ensure_future(
                self._sign(target_id, session_id, event, sign_in_url, bounce)
            )
            pending = self._pending.setdefault(target_id, set())
            pending.add(task)
            task.add_done_callback(pending.discard)
        reason = "Aborted" if event.get("responseStatusCode") is not None else "BlockedByClient"
        await self._answer(
            session_id, "Fetch.failRequest", {"requestId": request_id, "errorReason": reason}
        )
        if task is not None:
            await task

    async def _answer(self, session_id: str, method: str, params: dict[str, Any]) -> None:
        try:
            await self.relay.send(method, params, session_id)
        except Exception:  # noqa: BLE001 - a request whose tab closed needs no answer
            logger.debug("browser sign-in: %s not answered", method, exc_info=True)

    # ── what an op sees ──

    def begin(self, target_id: str, requested_url: str) -> None:
        """An agent ``navigate`` to *requested_url* starts on *target_id*."""
        self._requested[target_id] = {host_of(requested_url)}
        self._navigating.add(target_id)
        self._pass_gate(target_id, requested_url)

    def _pass_gate(self, target_id: str, url: str) -> None:
        if self._gate_allows(target_id, url):
            self._gated.setdefault(target_id, set()).add(origin_of(url))

    def allow(self, target_id: str, requested_url: str) -> None:
        """*requested_url*'s host may be signed on *target_id*; the guard re-opens it."""
        self._requested.setdefault(target_id, set()).add(host_of(requested_url))
        self._pass_gate(target_id, requested_url)

    def expect_new(
        self,
        requested_url: str,
        caller_key: str = "",
        gate: Callable[[str], bool] | None = None,
    ) -> None:
        """An agent ``tabs new`` to *requested_url* starts; its tab is adopted next."""
        self._expect_new, self._expect_caller = host_of(requested_url), caller_key
        self._expect_gate, self._expect_url = gate, requested_url

    def note_caller(
        self,
        target_id: str,
        caller_key: str,
        gate: Callable[[str], bool] | None = None,
        *,
        moves: bool = True,
    ) -> None:
        """An op by *caller_key* starts on *target_id*: a renewal there is judged against
        it and its URL *gate*, and one already in flight leaves the page an op that
        *moves* it (not a read-only one) takes it to."""
        if moves:
            self._ops[target_id] = self._ops.get(target_id, 0) + 1
        if gate is not None:
            self._gates[target_id] = gate
            # Passes are the old gate's: the new one judges again (a revoked site).
            self._gated.pop(target_id, None)
        if caller_key:
            if self._callers.get(target_id, caller_key) != caller_key:
                self._gated.pop(target_id, None)  # another caller's passes are not this one's
            self._callers[target_id] = caller_key

    def take_new(self) -> list[str]:
        """The tabs a ``tabs new`` opened since the last call."""
        self._expect_new = ""
        tabs, self._new_tabs = self._new_tabs, []
        return tabs

    def end(self, target_id: str) -> None:
        """The op on *target_id* ended (or was cut by a takeover): its signs end with it,
        so none re-opens a page the person has moved on from."""
        self._requested.pop(target_id, None)
        self._navigating.discard(target_id)
        for task in self._pending.pop(target_id, set()):
            task.cancel()

    async def settle(self, target_id: str) -> None:
        """Wait (bounded) for a sign in flight on *target_id*'s tab."""
        tasks = set(self._pending.get(target_id, ()))
        if tasks:
            await asyncio.wait(tasks, timeout=SIGN_IN_PROVIDER_S + 2)

    def take(self, target_id: str) -> list[dict[str, Any]]:
        """The outcomes on *target_id* since the last take."""
        return self._outcomes.pop(target_id, [])

    # ── signing ──

    async def _sign(
        self,
        target_id: str,
        session_id: str,
        event: Mapping[str, Any],
        sign_in_url: str,
        bounce: tuple[int, bool],
    ) -> None:
        try:
            info = await self.relay.send("Target.getTargetInfo", {"targetId": target_id})
        except Exception:  # noqa: BLE001 - a tab already gone has nothing to sign
            return
        tab = info.get("targetInfo") or {}
        # The relay's own name for the tab's context: ``""`` is the default one.
        context = self.relay.context_of(target_id)
        response = event.get("responseStatusCode") is not None
        page_url = str((event.get("request") or {}).get("url") if response else tab.get("url"))
        origin = origin_of(page_url)
        key = (context, host_of(origin))
        ops_at_bounce, during = bounce
        self._pass_gate(target_id, str(tab.get("url")))  # the page the tab shows
        if not (
            self._gate_allows(target_id, page_url)
            or origin_of(page_url) in self._gated.get(target_id, ())
        ):
            # The caller may not open this page (an app's own sites): nothing is signed.
            told = {
                "outcome": OUTCOME_NOT_SIGNED,
                "provider": credentials.provider_name(self.provider),
                "host": host_of(origin) or "-",
                "reason": "origin_not_requested",
            }
            _audit(told)
            self._record(target_id, told)
            return
        shared = self._inflight.get(key)
        if shared is None:
            shared = asyncio.ensure_future(
                self._audited(self._sign_once(target_id, key, origin, sign_in_url, event, page_url))
            )
            self._inflight[key] = shared
        self._waiters.setdefault(key, []).append(target_id)
        try:
            outcome = dict(await asyncio.shield(shared))
        except asyncio.CancelledError:
            if len(self._waiters[key]) == 1:
                # The last tab waiting on it was cut (``end``, a stop): the sign writes
                # nothing after that. Another tab still waiting keeps it going.
                shared.cancel()
            raise
        finally:
            self._waiters[key].remove(target_id)
            if not self._waiters[key]:
                del self._waiters[key]
                if self._inflight.get(key) is shared:
                    del self._inflight[key]
        # The shared sign is no tab's: each tab is held, or not, on its own.
        if outcome["outcome"] == OUTCOME_HELD and not self._held(target_id):
            outcome = {**outcome, "outcome": OUTCOME_NOT_SIGNED, "reason": "other"}
        # A second tab that bounced while the first one's sign ran shares its cookies,
        # and is opened again the same way.
        reopen = outcome["outcome"] == OUTCOME_SIGNED_IN and target_id not in self._navigating
        if reopen and not await self._allowed_now(target_id, outcome["host"]):
            # A tab that shared another tab's sign is judged against its own caller.
            outcome = {**outcome, "outcome": OUTCOME_NOT_SIGNED, "reason": "origin_not_requested"}
        elif reopen and self._held(target_id):
            outcome["outcome"] = OUTCOME_HELD  # signed, but the person's page is left alone
        elif reopen and self._ops.get(target_id, 0) != ops_at_bounce:
            # An op ran on the tab while it was signed (a click, a navigate): its page
            # stands. A page's own redirect or an error page the block left is no move.
            outcome["reason"] = "moved_on"
        elif reopen and str((event.get("request") or {}).get("method") or "GET") != "GET":
            # A form post that bounced: a GET would drop what it sent. The cookies stand;
            # the agent sends it again.
            outcome["reason"] = "resubmit"
        elif reopen and self.active:
            try:
                # The page itself when the caller's gate allows it; else the same-origin
                # page the tab showed (a login hop's start), which now carries the cookies.
                # Neither: ``nav_failed``, the page is the agent's to open.
                shown = str(tab.get("url"))
                url = next(
                    (
                        u
                        for u in (page_url, shown if origin_of(shown) == origin else "")
                        if u and self._gate_allows(target_id, u)
                    ),
                    "",
                )
                if not url:
                    raise LookupError("no page the caller may open")
                self._reopening.add(target_id)
                went = await self.relay.send("Page.navigate", {"url": url}, session_id)
                if isinstance(went, Mapping) and went.get("errorText"):
                    raise RuntimeError("the page did not load")  # as ``ops.navigate_to``
            except Exception:  # noqa: BLE001 - the cookies stand; the page is the agent's
                outcome["reason"] = "nav_failed"
        if during:
            outcome["during_navigate"] = True  # the op's navigate re-runs (``sign_in``)
        self._record(target_id, outcome)

    @staticmethod
    async def _audited(sign: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """One SEL row per sign, however many tabs wait on it."""
        outcome = await sign
        _audit(outcome)
        return outcome

    def _live_waiter(self, key: tuple[str, str], first: str) -> str | None:
        """A tab still waiting on the sign for *key* that the person does not hold."""
        return next((t for t in self._waiters.get(key, [first]) if not self._held(t)), None)

    async def _authorizer(
        self, key: tuple[str, str], first: str, origin: str, page_url: str
    ) -> tuple[str | None, bool]:
        """``(tab, any_unheld)``: a waiting tab the person does not hold whose own
        caller may sign *origin* now (governance, settings, URL gate), else ``None``."""
        for tab in list(self._waiters.get(key, [first])):
            if self._held(tab):
                continue
            gated = self._gate_allows(tab, page_url) or origin in self._gated.get(tab, ())
            if gated and await self._allowed_now(tab, key[1]) and not self._held(tab):
                return tab, True
        # Asked after the awaits above: a takeover during them counts.
        return None, self._live_waiter(key, first) is not None

    async def _sign_once(
        self,
        target_id: str,
        key: tuple[str, str],
        origin: str,
        sign_in_url: str,
        event: Mapping[str, Any],
        page_url: str,
    ) -> dict[str, Any]:
        """The provider's work for *key*, shared by every tab waiting on it; authorised
        on behalf of a waiting tab the person does not hold."""
        context, host = key
        name = credentials.provider_name(self.provider)
        told = {"outcome": OUTCOME_NOT_SIGNED, "provider": name, "host": host or "-"}
        # Held is asked after every await: a takeover can land during any of them.
        who, unheld = await self._authorizer(key, target_id, origin, page_url)
        if not unheld:
            return {**told, "outcome": OUTCOME_HELD, "reason": None}
        if (
            who is None
            or not self.active
            or not (
                host in self._requested.get(target_id, ())
                or host in self._requested.get(who, ())
                or (context, host) in self._signed
            )
        ):
            return {**told, "reason": "origin_not_requested"}
        signed_at = self._signed.get((context, host))
        if signed_at is not None and time.monotonic() - signed_at < RENEW_WINDOW_S:
            return {**told, "reason": "still_redirected"}
        generation = self.generation
        cookies, reason, message = await self._ask(target_id, context, origin, sign_in_url, event)
        if reason is not None:
            return {**told, "reason": reason, **({"message": message} if message else {})}
        # Asked again after the provider's wait: a takeover, the switch turned off, or
        # a domain removed meanwhile wins over the answer. The awaited read first,
        # then the flags it could have raced with.
        who, unheld = await self._authorizer(key, target_id, origin, page_url)
        if not unheld:
            return {**told, "outcome": OUTCOME_HELD, "reason": None}
        if who is None or not self.active or generation != self.generation:
            return {**told, "reason": "origin_not_requested"}
        if not cookies:
            return {**told, "reason": "other"}
        if not await self._write(context, cookies, host):
            return {**told, "reason": "readback_missing"}
        self._signed[(context, host)] = time.monotonic()
        await self.sweep()
        return {**told, "outcome": OUTCOME_SIGNED_IN, "reason": None, "count": len(cookies)}

    def _gate_allows(self, target_id: str, url: str) -> bool:
        """The tab caller's URL gate on *url*; no gate recorded is the op route's own."""
        gate = self._gates.get(target_id)
        try:
            return gate is None or bool(gate(url))
        except Exception:  # noqa: BLE001 - a gate that cannot answer refuses
            return False

    def _held(self, target_id: str) -> bool:
        """The person holds *target_id*, or a tab it was opened from: a popup the
        live hub has not claimed yet is its opener's chat's."""
        seen: set[str] = set()
        while target_id and target_id not in seen:
            if self.held(target_id):
                return True
            seen.add(target_id)
            target_id = self.relay.opener_of(target_id)
        return False

    def _off_gate_landing(self, target_id: str, event: Mapping[str, Any]) -> bool:
        """While a page the guard re-opened loads: is this main-frame document one its
        caller's URL gate refuses? The re-open ends at the first final answer."""
        if target_id not in self._reopening or event.get("frameId") != target_id:
            return False
        if event.get("resourceType") != "Document" or event.get("responseStatusCode") is None:
            return False
        status = event.get("responseStatusCode")
        if not (isinstance(status, int) and 300 <= status < 400):
            self._reopening.discard(target_id)
        url = str((event.get("request") or {}).get("url") or "")
        if self._gate_allows(target_id, url):
            return False
        self._reopening.discard(target_id)
        return True

    def _sign_in_cookie(self, domain: str) -> bool:
        """A cookie on a sign-in host, or a domain cookie one would be sent that no
        listed site owns (a parent domain outside both domain lists)."""
        host = _bare(credentials.cookie_host(domain))
        if host in self.hosts:
            return True
        listed = credentials.domain_allowed(host, self.declared) and credentials.domain_allowed(
            host, self.allowed
        )
        dotted = domain.strip().startswith(".")  # host-only: never sent to a subdomain
        return dotted and not listed and credentials._covers_any(_bare(domain.strip()), self.hosts)

    def _allowed(self, host: str) -> bool:
        """May *host* be signed at all: not a sign-in host, in both domain lists."""
        return (
            bool(host)
            and host not in self.hosts
            and credentials.domain_allowed(host, self.declared)
            and credentials.domain_allowed(host, self.allowed)
        )

    async def _allowed_now(self, target_id: str, host: str) -> bool:
        """:meth:`_allowed`, against the settings as they are now when a reader is wired,
        and only while the governance ceiling allows browsing (the gateway's and the
        owning chat's): a sign runs outside any op's own gate."""
        if target_id not in self._callers:
            return False  # no chat to judge it for: nothing is signed on its behalf
        keys = ["", self._callers[target_id]]
        if not self._allowed(host) or await self.browse_denied(keys):
            return False
        if self.settings_of is None:
            return True
        try:
            now = await asyncio.to_thread(self.settings_of, self.provider)
        except Exception:  # noqa: BLE001 - settings that cannot be read allow nothing
            return False
        return now.enabled and credentials.domain_allowed(host, now.domains)

    async def _ask(
        self,
        target_id: str,
        context: str,
        origin: str,
        sign_in_url: str,
        event: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """The provider's answer, fenced: ``(cookies, reason, message)``."""
        site_cookies = tuple(
            await self._jar_pairs(context, host_of(origin)) + _set_cookie_pairs(event)
        )
        reason = "navigate" if target_id in self._requested else "renew"
        target = credentials.Target("kiro_chromium", self.headless, reason)
        method = getattr(self.provider, SIGN_METHOD)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(method, target, origin, sign_in_url, site_cookies=site_cookies),
                SIGN_IN_PROVIDER_S,
            )
        except asyncio.TimeoutError:
            return [], "provider_timeout", None
        except Exception as exc:  # noqa: BLE001 - its text could carry a secret; reduced to a code
            logger.warning("browser sign-in: provider failed (%s)", type(exc).__name__)
            return [], "provider_failed", None
        if not isinstance(answer, Mapping) or not isinstance(answer.get("ok"), bool):
            return [], "provider_bad_shape", None
        if not answer["ok"]:
            code = answer.get("reason")
            message = answer.get("message")
            text = None
            if isinstance(message, str):
                # Redacted whole first: a cut could split a secret past the pattern.
                clean = redact_credentials(redact_exfiltration_urls(message)[0])[0]
                text = (
                    "".join(ch for ch in clean if ch.isprintable()).strip()[:_MESSAGE_CAP] or None
                )
            return [], (code if code in PROVIDER_REASONS else "other"), text
        raw = answer.get("cookies")
        kept = fence(raw, origin)
        if raw and not kept:
            # All dropped: for naming another host, or (none did) for being malformed.
            elsewhere = any(
                isinstance(c, Mapping)
                and c.get("domain") is not None
                and str(c.get("domain")).strip().lower() != host_of(origin)
                for c in raw
            )
            return [], ("cross_host" if elsewhere else "provider_bad_shape"), None
        return kept, None, None

    async def _jar_pairs(self, context: str, host: str) -> list[dict[str, str]]:
        scope = {"browserContextId": context} if context else {}
        try:
            got = await self.relay.send("Storage.getCookies", scope)
        except Exception:  # noqa: BLE001 - no jar read is an empty one
            return []
        return [
            {"name": str(c.get("name")), "value": str(c.get("value", ""))}
            for c in got.get("cookies") or []
            if isinstance(c, Mapping) and credentials.cookie_host(str(c.get("domain", ""))) == host
        ]

    async def _write(self, context: str, cookies: list[dict[str, Any]], host: str) -> bool:
        """Set *cookies* into *context*, then read them back by name and host."""
        # Remembered BEFORE the write: a stop that cancels it mid-reply still deletes
        # and purges whatever landed.
        self.placed._record(context, cookies, {})
        landed = await credentials._set_cookies(self.relay, cookies, context)
        if len(landed) != len(cookies):
            return False
        scope = {"browserContextId": context} if context else {}
        try:
            got = await self.relay.send("Storage.getCookies", scope)
        except Exception:  # noqa: BLE001 - no read-back is a missing one
            return False
        # Name, path AND value: an older cookie of the same name, which the site
        # rejects, must not read as the new one. Compared here, never logged.
        held = {
            (c.get("name"), c.get("path") or "/", c.get("value"))
            for c in got.get("cookies") or []
            if isinstance(c, Mapping) and c.get("domain") == host
        }
        return all((c["name"], c["path"], c["value"]) in held for c in cookies)

    def _record(self, target_id: str, outcome: dict[str, Any]) -> None:
        held = self._outcomes.setdefault(target_id, [])
        held.append(outcome)
        del held[:-_OUTCOME_CAP]

    # ── clean-up ──

    async def sweep(self) -> int:
        """Delete every cookie on a sign-in host, in every context the relay knows."""
        removed = 0
        for context in ("", *self.relay.session_context_ids()):
            scope = {"browserContextId": context} if context else {}
            try:
                got = await self.relay.send("Storage.getCookies", scope)
            except Exception:  # noqa: BLE001 - best effort; the next op sweeps again
                continue
            gone = [
                {
                    "name": c["name"],
                    "value": "",
                    # A host-only cookie (no leading dot) is named by its URL: a
                    # ``domain`` would address a domain cookie instead.
                    **(
                        {"domain": c["domain"]}
                        if c["domain"].startswith(".")
                        else {"url": f"https://{c['domain']}"}
                    ),
                    "path": c.get("path") or "/",
                    # A ``__Host-`` / ``__Secure-`` cookie is only replaced by a secure one.
                    "secure": c.get("secure") is True,
                    "expires": 1,
                }
                for c in got.get("cookies") or []
                if isinstance(c, Mapping)
                and isinstance(c.get("name"), str)
                and isinstance(c.get("domain"), str)
                and self._sign_in_cookie(c["domain"])
            ]
            removed += len(await credentials._set_cookies(self.relay, gone, context))
        return removed


async def _keys_browse_denied(keys: list[str]) -> bool:
    """The ceiling denies browsing for any of *keys* (``""`` is the gateway's own)."""
    from kiro_crew.browser_harness import governance

    for key in keys:
        if await governance.browse_denied_async(key or governance.HOST_KEY, audit=False):
            return True
    return False


def _tab_held(target_id: str) -> bool:
    """Whether the person holds *target_id* (a takeover), read from the live hub."""
    try:
        from kiro_crew.browser_harness.live_hub import get_live_hub

        hub = get_live_hub()
        key = hub.owner_of_target(target_id)
        return key is not None and hub.in_control(key)
    except Exception:  # noqa: BLE001 - no hub is no takeover
        return False


def prepare(
    current: SignInGuard | None,
    relay: Any,
    provider: Any,
    settings: credentials.CredentialSettings,
    choice: Any,
    settings_of: Callable[[Any], credentials.CredentialSettings] | None = None,
) -> SignInGuard | None:
    """The guard for *relay*, with what it should do set but nothing sent yet.

    Synchronous, so the caller holds the guard before any await that a timeout can
    cut. ``None`` when no guard exists and none is wanted.
    """
    guard = current if current is not None and current.relay is relay else None
    hosts: tuple[str, ...] = ()
    if choice.kind == "kiro_chromium" and settings.enabled and credentials.provider_name(provider):
        hosts = declared_hosts(provider)
    if guard is None and not hosts:
        return None
    if guard is None:
        guard = SignInGuard(relay, provider, hosts, settings.domains, (), choice.headless)
        guard.held, guard.browse_denied = _tab_held, _keys_browse_denied
    guard.generation += 1
    guard.provider, guard.settings_of, guard.allowed = provider, settings_of, settings.domains
    guard._want, guard.settled = hosts, False
    # On only once every session holds the hosts; off at once (signs stop).
    guard.active = bool(hosts) and guard.active
    if hosts:
        guard.hosts = hosts
    return guard


async def converge(guard: SignInGuard) -> bool:
    """Send each session the ``Fetch`` state *guard* wants; whether all of it landed.

    Each session is sent only what it does not hold yet, and recorded only once the
    browser answered, so a run cut short (a timeout, a cancel) or a refused send is
    simply done again by the next run. The relay's hooks stay on until every
    session's interception is off, so a request still paused is always answered.
    """
    relay, want = guard.relay, guard._want
    if want:
        relay.on_tab_adopted, relay.on_tab_event = guard.on_adopted, guard.on_event
        relay.on_browser_paused = guard.on_browser_paused
        try:
            guard.declared = credentials.normalize_declared_domains(
                await asyncio.wait_for(
                    asyncio.to_thread(guard.provider.domains), credentials.PROVIDER_TIMEOUT_SECS
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no domains: nothing is signed, all still blocked
            guard.declared = ()
    sessions: list[str | None] = [None, *relay.adopted_sessions().values()]
    for gone in [s for s in guard._fetch if s not in sessions]:
        del guard._fetch[gone]
    done = True
    for sid in sessions:
        try:
            await guard._set_fetch(sid, want)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - not landed: the next run sends it again
            done = False
    if want:
        guard.active = done or guard.active
    elif done:
        relay.on_tab_adopted = relay.on_tab_event = relay.on_browser_paused = None
    guard.settled = done
    return done


async def reconcile(
    current: SignInGuard | None,
    relay: Any,
    provider: Any,
    settings: credentials.CredentialSettings,
    choice: Any,
    settings_of: Callable[[Any], credentials.CredentialSettings] | None = None,
) -> SignInGuard | None:
    """:func:`prepare` then :func:`converge`: the guard for *relay*, on, retuned or off."""
    guard = prepare(current, relay, provider, settings, choice, settings_of)
    if guard is not None:
        await converge(guard)
    return guard


def live(guard: SignInGuard | None, relay: Any) -> SignInGuard | None:
    """*guard* while it intercepts on *relay* (the connected browser's), else ``None``."""
    if guard is None or relay is None or guard.relay is not relay:
        return None
    return guard if guard.active and relay.on_tab_event is not None else None


async def halt(guard: SignInGuard | None, relay: Any) -> SignInGuard | None:
    """Before *relay*'s browser stops: no sign of *guard* lands after this returns.

    Turns it off, cancels every sign in flight and waits for them, so the cookies it
    remembers are all the cookies it set. ``None`` when it is not *relay*'s guard.
    """
    if guard is None or guard.relay is not relay:
        return None
    guard.active, guard.generation = False, guard.generation + 1
    tasks = [*guard._inflight.values(), *(t for held in guard._pending.values() for t in held)]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=SIGN_IN_PROVIDER_S)
    return guard


def told(outcomes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The op result's ``sign_in`` block: the last outcome, with the agent's text."""
    if not outcomes:
        return None
    last = dict(outcomes[-1])
    last.pop("count", None)
    last.pop("during_navigate", None)
    reason = last.get("reason")
    if reason:
        last["note"] = last.pop("message", None) or REASON_TEXT.get(reason, REASON_TEXT["other"])
    return last


def _audit(outcome: Mapping[str, Any]) -> None:
    """One SEL row per sign: provider, host, outcome, reason, count. Never a URL or value."""

    def clean(text: Any) -> str:
        return "".join(ch for ch in str(text or "") if ch.isprintable() and ch != " ")[:253] or "-"

    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation="browser_harness.sign_in",
            outcome=clean(outcome.get("outcome")),
            source="browser_api",
            resources=(
                f"provider={clean(outcome.get('provider'))} host={clean(outcome.get('host'))} "
                f"reason={clean(outcome.get('reason'))} cookies={int(outcome.get('count') or 0)}"
            ),
        )
    except Exception:  # noqa: BLE001 - an audit failure never stops the browser
        logger.warning("browser sign-in: SEL write failed", exc_info=True)


__all__ = [
    "HOSTS_METHOD",
    "OUTCOME_HELD",
    "OUTCOME_NOT_SIGNED",
    "OUTCOME_SIGNED_IN",
    "PROVIDER_REASONS",
    "REASON_TEXT",
    "SIGN_METHOD",
    "SignInGuard",
    "classify",
    "declared_hosts",
    "fence",
    "origin_of",
    "halt",
    "live",
    "patterns",
    "reconcile",
    "told",
]
