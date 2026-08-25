"""Tailnet mobile access: the guided path from this laptop to a phone.

The dashboard already had every *piece* of tailnet access — :mod:`kiro_crew.
dashboard.tailnet` resolves the MagicDNS name, :mod:`kiro_crew.dashboard.
tailnet_serve` publishes and withdraws ``tailscale serve``, and the CLI wires
them together as ``kirocrew tailnet up`` — and no way to get there from the UI.
An operator who wanted the dashboard on their phone had to know that Tailscale
existed, install it, sign in, find a config switch in a Security panel, restart
the gateway, run a CLI verb, and then mint a token by hand. Each step failed
with a message about the step, never about the sequence.

This module is the sequence. It answers one question — *what is the single next
thing to do?* — and it answers it as a **step**, not as a pile of booleans the
frontend has to re-derive. That is deliberate for the same reason
:func:`kiro_crew.dashboard.handlers.tailnet._derive_state` gives: one owner for
the state machine, so the card and the backend cannot disagree about what a host
with a name but no published serve means.

Three properties are load-bearing.

**This is a LIVE probe, and the existing status endpoint is not.**
``GET /api/tailnet/status`` deliberately reports the value resolved once at
startup, because that is what actually went into the fixed origin allowlist.
This endpoint reports what the machine can do *next*. The two must stay separate:
a name that resolves now but was absent at startup is exactly the boot race where
the origin is NOT trusted, and reporting it as ready would be the
"checked-but-never-ran shown as a clean result" defect. It gets its own step
(``restart_gateway``) instead. The one-click UI resumes after the replacement
gateway is ready, while the security boundary itself changes only at startup.

**The QR carries a live credential, so it is minted on demand and never cached.**
The payload is a URL with a session token in its query string. It is not logged,
not stored, and not returned by the status endpoint — only by an explicit POST.
Behind ``tailscale serve`` every request reaches the gateway from ``127.0.0.1``,
so per-device session pinning cannot distinguish the phone from anything else on
the tailnet (issue #1762): the token is the only real credential, which is why
the default TTL here is an hour rather than the 20-hour ceiling the CLI uses.

**Publishing is the consent for staying awake.** A phone loses the dashboard the
moment the laptop idles, so a published tailnet dashboard keeps the SYSTEM awake
(the display may still sleep). Enabling mobile access is the opt-in;
``dashboard.tailscale.keep_awake`` exists to opt back out of the awake half
without unpublishing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, NamedTuple

from aiohttp import web

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import tailnet, tailnet_serve
from kiro_crew.dashboard.boot_id import current_boot_id
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
    MAX_SESSION_TTL_SECS,
    generate_token,
    parse_duration,
)
from kiro_crew.qr import render_qr_data_uri

logger = logging.getLogger(__name__)

#: Where an operator without Tailscale is sent. The generic download page, not a
#: per-OS deep link: upstream redirects by user agent, and a hardcoded per-OS URL
#: is one more thing to rot when they reorganise the site.
TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download"

#: Default lifetime of the session a scanned QR opens. Deliberately far below the
#: 20-hour ceiling: behind ``tailscale serve`` the session cannot be pinned to the
#: scanning device (#1762), so this token is the only thing standing between the
#: tailnet and the dashboard. An hour is enough for a phone session and short
#: enough that a leaked link stops mattering quickly.
DEFAULT_QR_TTL_SECS = 3600

#: Hard ceiling this endpoint will mint, regardless of what the caller asks for.
#: Lower than ``MAX_SESSION_TTL_SECS`` for the reason above — the ceiling that
#: suits a CLI token typed on the operator's own machine is too generous for a
#: credential that travels as a scannable image.
MAX_QR_TTL_SECS = 12 * 3600

Step = Literal[
    "pinned",
    "install",
    "start_daemon",
    "sign_in",
    "enable_magicdns",
    "enable_https",
    "trust_off",
    "restart_gateway",
    "occupied",
    "publish",
    "ready",
]


def _derive_step(
    *,
    pinned: bool,
    probe: tailnet.DaemonProbe,
    trusted: bool,
    startup_host: str,
    published: bool | None,
) -> Step:
    """The one next action, derived HERE and nowhere else.

    Ordered by what blocks what, so the operator is never sent to a switch that
    cannot help yet. Each branch is a different remedy:

    1. ``pinned`` — an administrator's ceiling forbids tailnet access. Dead end;
       nothing below is actionable, and offering a toggle would be a lie.
    2. ``install`` / ``start_daemon`` / ``sign_in`` / ``enable_magicdns`` — the
       four ways there is no usable tailnet name, kept apart because "install
       Tailscale", "start it", "sign in" and "turn MagicDNS on" are four
       different errands.
    3. ``enable_https`` — the name exists but the tailnet has not granted
       certificate provisioning for it. This is a tailnet-wide administrator
       consent and cannot safely be performed by a gateway process.
    4. ``trust_off`` — a name exists, but the gateway is not configured to accept
       it as an origin, so publishing would produce a reachable dashboard that
       answers 403. Config first.
    5. ``restart_gateway`` — configured and resolvable NOW, but the running
       server does not trust this exact name (it may have booted before tailscaled
       or the node name may have changed). The fixed request boundary is rebuilt
       only by the formal gateway restart path.
    6. ``occupied`` — serve holds this port/mount for something that is not this
       dashboard, or its state could not be determined. Publishing would REPLACE
       it, so this refuses and the card renders the manual command
       (``kirocrew tailnet up``) for the operator to run deliberately.
    7. ``publish`` — everything is in place; one action left.
    8. ``ready`` — published and trusted.
    """
    if pinned:
        return "pinned"
    if not probe.installed:
        return "install"
    if not probe.reachable:
        return "start_daemon"
    if not probe.logged_in:
        return "sign_in"
    if not probe.name:
        return "enable_magicdns"
    # An already-published mapping is operational evidence stronger than a
    # possibly stale CertDomains snapshot. This exception also prevents a brief
    # control-plane propagation delay after first enablement from taking a
    # working QR away. For a new mapping, however, an explicit False is a hard
    # stop: the non-interactive gateway cannot grant tailnet-wide HTTPS consent.
    if published is not True and probe.https_enabled is False:
        return "enable_https"
    if not trusted:
        return "trust_off"
    if startup_host != probe.name:
        return "restart_gateway"
    if published is True:
        return "ready"
    # ``published is None`` is "could not tell", which is NOT "free". Publishing
    # over an unknown mount is the destructive direction, so an undetermined
    # state lands with the occupied case — same refusal, same manual escape.
    return "publish" if published is False else "occupied"


def _dashboard_port(request: web.Request) -> int:
    """The port this gateway is actually serving on.

    Read from the live app rather than from ``dashboard.url``: the configured URL
    is a statement of intent, and when its port was occupied the gateway moved.
    Publishing in front of a port this process is not listening on would hand
    ``tailscale serve`` whatever else holds it — the hazard ``kirocrew tailnet
    up`` refuses over, and the reason it prefers evidence to configuration.
    """
    port = request.app.get("port")
    try:
        return int(port or 0)
    except (TypeError, ValueError):
        return 0


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def _audit(request: web.Request, operation: str, outcome: str, resources: str) -> None:
    """Record a tailnet mobile-access decision in the security event log.

    Publishing changes what is reachable from every device on the tailnet, and a
    QR mint issues a credential — both are decisions, not inspections, so both
    leave a record. The read endpoint deliberately does NOT audit: it is polled
    by a card, and auditing a question would bury the decisions in noise.
    """
    try:
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation=operation,
            outcome=outcome,
            source="tailnet-mobile",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never break the action
        logger.debug("tailnet mobile audit write failed", exc_info=True)


async def _audit_async(
    request: web.Request,
    operation: str,
    outcome: str,
    resources: str,
) -> None:
    """Write SEL records off-loop, including the first cold initialization."""
    await asyncio.to_thread(_audit, request, operation, outcome, resources)


async def api_tailnet_mobile_status(request: web.Request) -> web.Response:
    """GET /api/tailnet/mobile — the guided state for the mobile-access card.

    Owner-only, like the mutations. Live, read-only, and never 500s: an unreadable config or an unreachable
        daemon is exactly when the operator wants this card, so every failure
        degrades into a step that names the remedy rather than into an error.
    """
    # Owner-only READ as well as write. This body carries the MagicDNS hostname,
    # whether the dashboard is currently published, and how many devices share
    # the tailnet — network facts about the operator's machine. Nothing consumes
    # it but the owner's own card, so refusing a non-owner costs nothing and
    # closes the disclosure that survived making the FRONTEND owner-only. The
    # renderer no longer needs an `is_owner` field: a refused read yields no
    # data, and the card already renders nothing without data.
    if request.get("app") != "" or not is_owner_dashboard_request(request):
        # A DENIED read is audited even though a successful one is not. The
        # docstring for `_audit` says reads are skipped because a polled question
        # would bury the decisions in noise — and that still holds for the 200
        # path, which the card hits every 30s. A refusal is not a question: it is
        # someone without owner rights reaching for this machine's network facts,
        # which is exactly the kind of event the SEL exists to carry. Mirrors the
        # denial audits the four mutating handlers already emit.
        await _audit_async(request, "tailnet.mobile.status", "denied", "not-owner")
        return web.json_response(
            {"error": "tailnet mobile access is owner-only", "code": "owner_only"},
            status=403,
        )
    port = _dashboard_port(request)
    live = await _live_state(request, port)
    probe = live.probe
    return web.json_response(
        {
            "step": live.step,
            "host": probe.name,
            "origin": f"https://{probe.name}" if probe.name else "",
            "installed": probe.installed,
            "reachable": probe.reachable,
            "logged_in": probe.logged_in,
            # The OTHER devices on this tailnet. Carried because publishing and
            # the QR both succeed on a tailnet of one, and the scan then fails in
            # the phone's browser with nothing on this machine to blame.
            "peer_count": probe.peer_count,
            "peers_online": probe.peers_online,
            "trusted": live.trusted,
            "startup_trusted": live.startup_host == probe.name,
            "published": live.published,
            "keep_awake": live.keep_awake,
            "governance_pinned": live.pinned,
            # Verbatim daemon/serve text, never a rephrasing. The classification
            # above is a best-effort hint; this is what Tailscale actually said,
            # and it is the only thing that stays correct if upstream rewords.
            "detail": live.serve_detail or probe.detail,
            "download_url": TAILSCALE_DOWNLOAD_URL,
            "qr_ttl_secs": DEFAULT_QR_TTL_SECS,
            "serve_port": tailnet_serve.SERVE_HTTPS_PORT,
            "dashboard_port": port,
        }
    )


class _LiveState(NamedTuple):
    """One reading of this machine's tailnet readiness, plus the derived step."""

    step: Step
    probe: tailnet.DaemonProbe
    published: bool | None
    serve_detail: str
    trusted: bool
    keep_awake: bool
    pinned: bool
    #: The name the RUNNING server resolved at startup. Empty means the origin is
    #: not trusted by this process yet, however resolvable the name is right now.
    startup_host: str


async def _live_state(request: web.Request, port: int) -> _LiveState:
    """Probe the machine and derive the single next step, for EVERY caller.

    Extracted so the status read and the QR mint cannot disagree about what this
    machine may currently do. They previously disagreed in the direction that
    matters: the card refused to offer a QR unless the derived step was ``ready``,
    while the mint endpoint re-checked two of ``_derive_step``'s seven
    preconditions by hand (a name exists; serve reports published) and silently
    admitted the other five. Every precondition the mint did not re-implement was
    a way to obtain a credential the card would never have offered — which is why
    this endpoint accumulated four separate blocking review findings, one per
    missed precondition, rather than one.

    ``_derive_step`` is documented as deriving the next action "HERE and nowhere
    else", so the fix is to honour that rather than to add a fifth hand-rolled
    check. Reading one function's answer is also the only version of this that
    stays correct when a step is added later.
    """
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        trusted = bool(cfg.dashboard.tailscale.enabled)
        keep_awake = bool(cfg.dashboard.tailscale.keep_awake)
    except Exception:
        logger.debug("tailnet mobile: config unreadable", exc_info=True)
        trusted = False
        keep_awake = True

    try:
        # No audit_tool: a polled read must not append an HMAC-chained SEL row
        # per refresh (see tailnet.is_governance_pinned_off).
        pinned = await asyncio.to_thread(tailnet.is_governance_pinned_off)
    except Exception:  # pragma: no cover - the probe is itself guarded
        logger.debug("tailnet mobile: governance probe unavailable", exc_info=True)
        pinned = False

    probe = tailnet.DaemonProbe(
        name="", installed=False, reachable=False, logged_in=False, detail=""
    )
    published: bool | None = None
    serve_detail = ""
    if not pinned:
        # Both are subprocess round trips; neither may run on the event loop.
        probe = await asyncio.to_thread(tailnet.probe_daemon)
        if probe.name and port:
            state = await asyncio.to_thread(tailnet_serve.serve_state, port)
            published = state.published
            serve_detail = state.detail

    startup_host = str(request.app.get("tailnet_host") or "")
    step = _derive_step(
        pinned=pinned,
        probe=probe,
        trusted=trusted,
        startup_host=startup_host,
        published=published,
    )
    return _LiveState(
        step=step,
        probe=probe,
        published=published,
        serve_detail=serve_detail,
        trusted=trusted,
        keep_awake=keep_awake,
        pinned=pinned,
        startup_host=startup_host,
    )


#: Why a QR cannot be minted in each non-``ready`` step, as ``(code, sentence)``.
#:
#: Every entry is a state in which a minted link would NOT open this dashboard, so
#: the credential would be spent on nothing — the refusal this endpoint's docstring
#: already promised and only partly delivered. ``pinned`` is the one that is a
#: security refusal rather than a usability one: an administrator's ceiling forbids
#: tailnet access, and a still-running publication from before the pin was applied
#: must not remain a source of fresh owner credentials.
#:
#: The four "no usable tailnet name" steps deliberately share ``no_name``: they
#: differ in which errand fixes them, which is the CARD's business, and the API
#: contract only needs to say why no code was made.
_QR_REFUSALS: dict[Step, tuple[str, str]] = {
    "pinned": (
        "governance_pinned",
        "An administrator's security policy pins tailnet access off, so no "
        "sign-in link can be issued for this machine.",
    ),
    "install": (
        "no_name",
        "This machine has no tailnet name right now, so there is nothing to " "point a phone at.",
    ),
    "trust_off": (
        "origin_not_trusted",
        "This dashboard is not configured to accept its own tailnet name as an "
        "origin, so a phone opening the link would be refused.",
    ),
    "restart_gateway": (
        "restart_required",
        "This running server has not loaded its validated tailnet origin yet. "
        "Restart Kiro Crew, then scan.",
    ),
    "publish": (
        "not_published",
        "The dashboard is not published on the tailnet, so a phone could not " "reach it.",
    ),
}
#: The other three "no usable tailnet name" steps answer exactly as ``install``, and
#: an undetermined serve state is not "free" — it refuses as not-yet-published.
#: Written as an update rather than a ``for`` loop so no loop variable is left bound
#: at module scope.
_QR_REFUSALS.update(
    {
        "start_daemon": _QR_REFUSALS["install"],
        "sign_in": _QR_REFUSALS["install"],
        "enable_magicdns": _QR_REFUSALS["install"],
        "enable_https": (
            "https_not_enabled",
            "This tailnet has not enabled HTTPS certificate provisioning for "
            "this machine, so a phone could not open a secure dashboard URL.",
        ),
        "occupied": _QR_REFUSALS["publish"],
    }
)


def _guard(request: web.Request) -> web.Response | None:
    """Refuse a mutation from a caller that must not make one.

    THREE independent gates. The owner gate is the load-bearing one.

    **Dashboard-user only.** ``request["app"]`` is set by the auth middleware on
    every authenticated path — ``""`` for a dashboard user, the app name for an
    app token — so anything else is refused. An app token is admitted by the
    middleware for whatever path prefixes its manifest ``permissions.api``
    declares, and ``_is_restricted_session`` cannot stop it: that predicate reads
    ``X-Session-Key``, which an app token does not carry, so it answers "not
    restricted". An ABSENT key is refused too: it means the middleware never ran.

    **Owner only.** Being a dashboard user is NOT enough, because a dashboard user
    is not necessarily *this* dashboard's owner. Telegram, Teams and Slack all
    hand a presigned dashboard link to any ALLOWED user, minting a token whose
    ``sub`` is that user's own id (``telegram/transport_dispatch``,
    ``teams/transport_dispatch``) — so a non-owner can legitimately hold a
    dashboard session. The QR endpoint mints ``generate_token(owner_id or
    "local-app")``, i.e. an OWNER-subject credential, and ``local-app`` is itself
    in ``_LOCAL_DASHBOARD_OWNER_SUBJECTS``. Without this gate any allowed
    messaging user could trade their own scoped session for an owner one — a
    privilege escalation, not merely an over-broad surface. ``core.py``'s
    identical mint is gated by loopback + a local-secret HMAC; this endpoint is
    reachable over the tailnet, so it needs the owner claim instead.
    ``is_owner_dashboard_request`` is reused rather than re-derived: a second copy
    of an authorization predicate is how one path comes to be guarded and its
    sibling not.

    **Not a restricted session.** An incognito/temporary slot must not publish or
    mint either.

    Returns the refusal, or ``None`` when the caller may proceed.
    """
    if request.get("app") != "":
        return web.json_response(
            {
                "error": "tailnet mobile access is a dashboard-user surface",
                "code": "app_token_not_allowed",
            },
            status=403,
        )
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        return web.json_response(
            {
                "error": "restricted session cannot change tailnet mobile access",
                "code": "restricted_session",
            },
            status=403,
        )
    if not is_owner_dashboard_request(request):
        return web.json_response(
            {"error": "tailnet mobile access is owner-only", "code": "owner_only"},
            status=403,
        )
    return None


async def api_tailnet_mobile_publish(request: web.Request) -> web.Response:
    """POST /api/tailnet/mobile/publish — put this dashboard on the tailnet.

    Delegates the whole decision to :func:`tailnet_serve.publish`, which owns the
    governance gate, the occupancy guard that refuses to overwrite someone else's
    mount, and the verbatim daemon error. Nothing about those is re-implemented
    here: a second copy is how one path comes to be guarded and its sibling not.
    """
    refusal = _guard(request)
    if refusal is not None:
        await _audit_async(request, "tailnet.mobile.publish", "denied", "restricted-session")
        return refusal
    port = _dashboard_port(request)
    if not port:
        await _audit_async(request, "tailnet.mobile.publish", "denied", "unknown-port")
        return web.json_response(
            {
                "ok": False,
                "code": "failed",
                "detail": (
                    "This gateway could not tell which port it is serving on, so it "
                    "refused to publish — `tailscale serve` would expose whatever "
                    "holds the port it guessed."
                ),
            },
            status=409,
        )
    result = await asyncio.to_thread(
        tailnet_serve.publish, port, audit_tool="tailnet_mobile_publish"
    )
    await _audit_async(
        request,
        "tailnet.mobile.publish",
        "success" if result.ok else "denied",
        f"port={port} code={result.code}",
    )
    # Branched instead of `status=200 if result.ok else 409`. A computed status is
    # invisible to the error-code contract scan (`dynamic_status`), which caps it
    # precisely because hoisting the status into an expression is how the gate
    # would otherwise be defeated while looking like ordinary refactoring. Two
    # literal returns say the same thing and stay checkable.
    if result.ok:
        return web.json_response(
            {"ok": True, "code": result.code, "detail": result.detail}, status=200
        )
    return web.json_response(
        {"ok": False, "code": result.code, "error": result.detail, "detail": result.detail},
        status=409,
    )


async def api_tailnet_mobile_unpublish(request: web.Request) -> web.Response:
    """POST /api/tailnet/mobile/unpublish — take it back off the tailnet.

    Withdrawal is deliberately NOT gated on governance (see
    :func:`tailnet_serve.unpublish`): a fail-closed policy probe returns "pinned"
    both for a real deny and for a ceiling it could not read, so gating the way
    OUT would let a transient policy-read failure leave the dashboard published
    with no supported way to remove it.
    """
    refusal = _guard(request)
    if refusal is not None:
        await _audit_async(request, "tailnet.mobile.unpublish", "denied", "restricted-session")
        return refusal
    port = _dashboard_port(request)
    result = await asyncio.to_thread(tailnet_serve.unpublish, port)
    await _audit_async(
        request,
        "tailnet.mobile.unpublish",
        "success" if result.ok else "denied",
        f"port={port} code={result.code}",
    )
    # Branched instead of `status=200 if result.ok else 409`. A computed status is
    # invisible to the error-code contract scan (`dynamic_status`), which caps it
    # precisely because hoisting the status into an expression is how the gate
    # would otherwise be defeated while looking like ordinary refactoring. Two
    # literal returns say the same thing and stay checkable.
    if result.ok:
        return web.json_response(
            {"ok": True, "code": result.code, "detail": result.detail}, status=200
        )
    return web.json_response(
        {"ok": False, "code": result.code, "error": result.detail, "detail": result.detail},
        status=409,
    )


async def api_tailnet_mobile_qr(request: web.Request) -> web.Response:
    """POST /api/tailnet/mobile/qr — mint a scannable, short-lived access link.

    Returns a PNG data URI and the URL it encodes. Both carry a **live session
    token**, so neither is logged, cached, or reachable from the polled status
    endpoint — a credential is handed out only in response to an explicit
    request for one.

    The token's subject mirrors ``/api/token/local`` (``owner_id`` falling back
    to ``local-app``) rather than inventing a new one. That is not cosmetic:
    ``local-app`` is in the recognised local-owner subject set that credential-
    backed routes admit, so a bespoke subject would produce a phone session that
    looks fine and is silently denied those routes.

    Refuses unless the derived step is ``ready``. That is the whole precondition
    set, read from ``_derive_step`` rather than re-checked here: a QR for a URL
    nothing answers is a support ticket, not a feature, and a QR issued under an
    administrator's tailnet pin is a credential the ceiling forbids.
    """
    refusal = _guard(request)
    if refusal is not None:
        await _audit_async(request, "tailnet.mobile.qr", "denied", "restricted-session")
        return refusal

    port = _dashboard_port(request)
    # Unconditional, unlike an earlier revision that nested this in `if port:`.
    # Both server entrypoints set ``app["port"]``, so an unresolved port is not a
    # reachable state today — but this handler's contract is that it refuses
    # unless the dashboard is actually published, and a gate that silently
    # vanishes when one input is falsy does not keep that promise. Its sibling
    # `publish` already refuses at port 0; the two must not disagree about
    # whether an unknown port is safe.
    if not port:
        await _audit_async(request, "tailnet.mobile.qr", "denied", "unknown-port")
        return web.json_response(
            {
                "error": (
                    "This gateway could not tell which port it is serving on, so it "
                    "could not confirm the dashboard is reachable — no code was made."
                ),
                "code": "unknown_port",
            },
            status=409,
        )

    # ONE gate, reading ``_derive_step``'s answer, instead of re-checking
    # individual preconditions here. Anything other than ``ready`` means a link
    # would not open this dashboard — or, for ``pinned``, must not be issued at
    # all — and the previous hand-rolled pair of checks admitted five of the seven
    # states that ``_derive_step`` already knows to stop at. ``pinned`` is the
    # security-relevant one: pinning the policy off does not tear down an existing
    # publication, so without this a still-live serve from before the pin remained
    # a source of fresh owner credentials over a tailnet the ceiling forbids.
    live = await _live_state(request, port)
    if live.step != "ready":
        code, sentence = _QR_REFUSALS.get(
            live.step,
            # Unreachable while `_QR_REFUSALS` covers every non-ready step (a test
            # pins that), but a future step must fail CLOSED here rather than mint.
            ("not_ready", "This machine is not ready to hand the dashboard to a phone."),
        )
        await _audit_async(request, "tailnet.mobile.qr", "denied", f"step={live.step}")
        detail = live.serve_detail or live.probe.detail
        return web.json_response(
            {"error": f"{sentence} {detail}".strip() if detail else sentence, "code": code},
            status=409,
        )
    host = live.probe.name

    ttl = DEFAULT_QR_TTL_SECS
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        raw_ttl = body.get("ttl")
        if isinstance(raw_ttl, str) and raw_ttl.strip():
            parsed = parse_duration(raw_ttl)
            if parsed:
                ttl = parsed
    # Clamped twice on purpose: this endpoint's own ceiling first, then the
    # global session ceiling, so neither a caller-supplied value nor a future
    # raise of MAX_QR_TTL_SECS can exceed what token_auth itself allows.
    ttl = min(ttl, MAX_QR_TTL_SECS, MAX_SESSION_TTL_SECS)

    state_obj = request.app.get("state")
    owner_id = str(getattr(state_obj, "owner_id", "") or "")
    # Two session shapes; the operator picks which by configuration. Both bound
    # the credential — they differ in WHAT bounds it.
    # Default — ``boot``: the session is scoped to this gateway PROCESS. The
    # refresh chain IS issued, so being idle no longer signs the phone out, and
    # both the access cookie and the chain carry the boot id, so a restart does.
    # This is the default because it matches what handing a phone a QR code
    # actually means: signed in while my gateway is up. A clock the operator
    # cannot see, which signs the phone out mid-use and yet keeps working after
    # the gateway is gone, matches nothing anyone asked for.
    #
    # It is a DIFFERENT bound, not a strictly tighter one: a gateway with long
    # uptime grants a correspondingly long session. What keeps that honest is
    # that the bound is something the operator can see and act on — `uptime`
    # answers "is my phone still signed in", and a restart is a hard revoke
    # needing no recorded state. The peer pin, the revocation counter and
    # `kirocrew logout` all still apply unchanged.
    #
    # Opt out — ``no_refresh``: no refresh chain is issued at the exchange, so
    # ``session_exp`` becomes a real ceiling and the phone re-scans when it
    # lapses. Kept as a supported shape for an operator who wants the credential
    # bounded by a clock regardless of process lifetime.
    #
    # Mutually exclusive on purpose: carrying both would mean a session that
    # neither refreshes nor lasts, which is worse than either.
    #
    # The TTL clamp above is untouched under both shapes. Rotation is what
    # extends a boot-bound session, so no ceiling and no security constant moves.
    # Read the session-shape choice here rather than widening ``_live_state``'s
    # tuple, which the status endpoint also consumes.
    #
    # An unreadable config falls back to the DEFAULT, not to the other shape.
    # "We could not read your override, so use the default" is the honest
    # reading; picking the timed shape instead would hand the phone a session
    # that expires on a clock the operator did not ask for, which presents as a
    # phone that randomly signs itself out. The fallback is not unbounded
    # either — a boot-bound session still ends at the next restart.
    try:
        _cfg = await asyncio.to_thread(KiroCrewConfig.load)
        _until_restart = bool(_cfg.dashboard.qr_session_until_restart)
    except Exception:
        logger.debug("tailnet mobile: config unreadable for session shape", exc_info=True)
        _until_restart = True

    if _until_restart:
        claims = {"boot": current_boot_id()}
    else:
        claims = {"no_refresh": "1"}
    token = generate_token(owner_id or "local-app", ttl_seconds=ttl, extra=claims)
    url = f"https://{host}/?token={token}"
    try:

        image = await asyncio.to_thread(render_qr_data_uri, url)
    except Exception:
        logger.debug("tailnet mobile QR encode failed", exc_info=True)
        await _audit_async(request, "tailnet.mobile.qr", "denied", "encode-failed")
        # Detail is in the server log above; the client body (rendered verbatim
        # into a localized UI) gets a generic message.
        return web.json_response(
            {
                "error": "Could not render the QR code",
                "code": "encode_failed",
            },
            status=500,
        )
    await _audit_async(request, "tailnet.mobile.qr", "success", f"ttl={ttl}")
    return web.json_response(
        {
            "url": url,
            "image": image,
            "ttl_secs": ttl,
            # The window in which the LINK must be opened, which is not the
            # session lifetime and is the thing that surprises people: the token
            # stops being redeemable long before the session it would have
            # created would have expired.
            "link_window_secs": LINK_WINDOW_SECS,
            "host": host,
        }
    )
