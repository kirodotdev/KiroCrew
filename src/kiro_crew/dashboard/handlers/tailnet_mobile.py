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

**This is a LIVE daemon probe, while the existing status endpoint reports the
live request boundary.**  They must stay separate: a daemon name is not trusted
merely because it resolves.  The background recovery path first validates and
adds it to the running Origin/Host set; only then does this endpoint report the
name as trusted.  Until that happens the existing fail-closed restart step is
preserved, including for config changes that still require a restart.

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
from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
from kiro_crew.dashboard import tailnet, tailnet_serve
from kiro_crew.dashboard.boot_id import current_boot_id
from kiro_crew.dashboard.handlers._shared import _caller_bounds, _is_restricted_session
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.mobile_connect import mint_denied_reason
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
       ways there is no usable tailnet name, kept apart because "install
       Tailscale", "start it", "sign in" and "turn MagicDNS on" are different
       errands. ``start_daemon`` covers both a daemon that does not answer and
       one that answers as stopped (``BackendState "Stopped"``): the errand is
       the same, and the verbatim probe detail tells the two apart.
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
    # A stopped daemon (``BackendState "Stopped"``) answers status reads but the
    # tailnet is down: nothing can reach this host and serve writes cannot take
    # effect, so every later branch — including ``ready`` — would be a lie. Same
    # errand as an unreachable daemon (get Tailscale running), so it reuses that
    # step; the card renders the probe's detail, which names the stopped state
    # precisely. The step's "no tailnet name" copy holds because ``probe_daemon``
    # always returns ``name=""`` for the Stopped state.
    if probe.stopped:
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
            # A per-process marker lets the setup flow prove that a requested
            # restart reached the replacement gateway. Step text alone is not
            # sufficient: an already-ready pre-migration process remains
            # ``ready`` during its response-flush window and could otherwise
            # mint one last boot-bound QR before exiting.
            "boot_id": current_boot_id(),
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

    startup_host = tailnet.running_tailnet_origin(request.app)[0]
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


def _apply_mobile_setup_config(data: dict, login: str) -> tuple[bool, bool, bool]:
    """Apply the durable half of one-click mobile setup in place.

    Returns ``(changed, restart_required, persistent)``. The login comes from
    this machine's daemon status, not from the browser, and is ADDED to an
    existing allowlist rather than replacing it. That makes the explicit
    "Set up / Show QR" action sufficient to establish the identity bound a
    session needs to survive an update without silently removing identities an
    operator configured by hand.

    ``qr_session_until_restart=false`` is the one advanced opt-out preserved:
    it deliberately asks for a clock-bounded, non-refreshing session. Every
    default/ordinary shape is promoted to persistent once identity trust is
    ready. Trust/origin changes require a gateway restart because middleware
    snapshots them at boot; changing only the QR session shape does not.
    """
    dashboard = data.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("config section 'dashboard' is not an object")
    tailscale = dashboard.setdefault("tailscale", {})
    if not isinstance(tailscale, dict):
        raise ValueError("config section 'dashboard.tailscale' is not an object")

    changed = False
    restart_required = False

    def _set(section: dict, key: str, value: object, *, restart: bool = False) -> None:
        nonlocal changed, restart_required
        if section.get(key) == value:
            return
        section[key] = value
        changed = True
        restart_required = restart_required or restart

    _set(tailscale, "enabled", True, restart=True)

    raw_logins = tailscale.get("allowed_logins", [])
    if not isinstance(raw_logins, list):
        raise ValueError("config field 'dashboard.tailscale.allowed_logins' is not a list")
    allowed_logins = list(raw_logins)
    if login and not any(
        isinstance(entry, str) and entry.strip().lower() == login.lower()
        for entry in allowed_logins
    ):
        allowed_logins.append(login)
        _set(tailscale, "allowed_logins", allowed_logins, restart=True)
    if login:
        _set(tailscale, "trust_identity", True, restart=True)

    identity_ready = bool(
        tailscale.get("trust_identity") is True
        and any(isinstance(entry, str) and entry.strip() for entry in allowed_logins)
    )
    persistent = False
    if identity_ready and dashboard.get("qr_session_until_restart", True) is not False:
        _set(dashboard, "qr_session_until_restart", True)
        _set(dashboard, "qr_session_persist_across_restart", True)
        persistent = True

    return changed, restart_required, persistent


def _effective_mobile_setup(cfg: KiroCrewConfig, login: str) -> tuple[bool, list[str]]:
    """Validate the merged config that the next gateway boot will consume.

    The writer above intentionally edits ``config.json`` only, while
    ``config.local.json`` is a user-owned, higher-precedence overlay.  Returning
    success from the raw base write would therefore be a lie when that overlay
    disables identity trust or persistence.  The explicit timed-session opt-out
    remains valid; every other mismatch is returned as a dotted field name so
    the caller can explain exactly what the overlay must stop overriding.
    """
    dashboard = cfg.dashboard
    tailscale = dashboard.tailscale
    allowed_logins = tuple(
        entry for entry in getattr(tailscale, "allowed_logins", ()) if isinstance(entry, str)
    )
    conflicts: list[str] = []
    if getattr(tailscale, "enabled", False) is not True:
        conflicts.append("dashboard.tailscale.enabled")
    if getattr(tailscale, "trust_identity", False) is not True:
        conflicts.append("dashboard.tailscale.trust_identity")
    if not tailnet.login_allowed(login, allowed_logins):
        conflicts.append("dashboard.tailscale.allowed_logins")

    timed_opt_out = getattr(dashboard, "qr_session_until_restart", True) is False
    persistent = False
    if not timed_opt_out:
        if getattr(dashboard, "qr_session_persist_across_restart", False) is not True:
            conflicts.append("dashboard.qr_session_persist_across_restart")
        else:
            persistent = not conflicts
    return persistent, conflicts


def _running_tailnet_trust_ready(request: web.Request, login: str) -> bool:
    """Whether this process can enforce a persistent phone session right now.

    Asks ``enforces_identity`` rather than spelling the conjunction out again:
    the property exists so every site answering "is tailnet identity in force"
    answers the same way, and a fifth hand-written spelling is the drift it was
    added to prevent. Behaviour here is unchanged either way -- an unreadable
    policy carries an empty allowlist, so ``login_allowed`` already refused --
    but a future change to what enforcement means now reaches this site too.
    """
    trust = request.app.get("tailnet_trust")
    if not isinstance(trust, tailnet.TailnetTrust):
        return False
    return trust.enforces_identity and tailnet.login_allowed(login, trust.allowed_logins)


async def api_tailnet_mobile_configure(request: web.Request) -> web.Response:
    """POST /api/tailnet/mobile/configure — persist safe update-proof access.

    This is deliberately a dedicated owner-only mutation instead of four
    generic config PATCHes. The allowlist value is discovered from the local
    Tailscale daemon and all related fields land in one locked write, so a
    crash or concurrent settings save cannot leave ``trust_identity`` enabled
    with an empty list, or persistence enabled without its identity bound.
    """
    refusal = _guard(request)
    if refusal is not None:
        await _audit_async(request, "tailnet.mobile.configure", "denied", "restricted-session")
        return refusal

    pinned = await asyncio.to_thread(
        tailnet.is_governance_pinned_off, audit_tool="tailnet_mobile_configure"
    )
    if pinned:
        await _audit_async(request, "tailnet.mobile.configure", "denied", "governance-pinned")
        return web.json_response(
            {
                "error": "tailnet access is disabled by your administrator's security policy",
                "code": "governance_pinned",
            },
            status=403,
        )

    probe = await asyncio.to_thread(tailnet.probe_daemon)
    if not (probe.reachable and probe.logged_in and probe.name and probe.login):
        await _audit_async(request, "tailnet.mobile.configure", "denied", "daemon-not-ready")
        return web.json_response(
            {
                "error": (
                    "Tailscale must be signed in with MagicDNS and a readable local "
                    "login before persistent phone access can be configured."
                ),
                "code": "daemon_not_ready",
            },
            status=409,
        )

    result: dict[str, bool] = {}

    def _mutate(data: dict) -> dict | None:
        changed, restart_required, persistent = _apply_mobile_setup_config(data, probe.login)
        result.update(
            changed=changed,
            restart_required=restart_required,
            persistent=persistent,
        )
        return data if changed else None

    try:
        async with _get_config_lock():
            await asyncio.to_thread(update_config_locked, config_path(), mutate=_mutate)
    except ConfigReadError:
        await _audit_async(request, "tailnet.mobile.configure", "error", "config-read-failed")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_read_failed"}, status=500
        )
    except ValueError as exc:
        await _audit_async(request, "tailnet.mobile.configure", "error", "config-invalid")
        return web.json_response({"error": str(exc), "code": "config_invalid"}, status=500)
    except OSError:
        await _audit_async(request, "tailnet.mobile.configure", "error", "config-write-failed")
        return web.json_response(
            {"error": "failed to write config file", "code": "config_write_failed"}, status=500
        )

    effective = await asyncio.to_thread(KiroCrewConfig.load)
    persistent, conflicts = _effective_mobile_setup(effective, probe.login)
    result["persistent"] = persistent
    if conflicts:
        await _audit_async(
            request,
            "tailnet.mobile.configure",
            "denied",
            "config-local-override",
        )
        return web.json_response(
            {
                "error": (
                    "config.json now contains the safe phone-access settings, but "
                    "config.local.json overrides them; update or remove the listed "
                    "overrides, then retry so the gateway can load them"
                ),
                "code": "config_overlay_conflict",
                "fields": conflicts,
            },
            status=409,
        )

    if persistent and not _running_tailnet_trust_ready(request, probe.login):
        # A prior attempt may have written the base file but returned the
        # overlay conflict above. Once the user removes that overlay, a retry
        # sees no raw-file change; nevertheless this process still holds the
        # old startup trust snapshot and must restart before a require_peer
        # link can work. Comparing the LIVE snapshot keeps that retry honest.
        result["restart_required"] = True

    await _audit_async(
        request,
        "tailnet.mobile.configure",
        "success",
        "persistent" if result.get("persistent") else "boot-bound",
    )
    # The browser needs only the action it must take next. ``changed`` and
    # ``persistent`` remain server-side inputs to restart/audit decisions; a
    # successful 2xx response already carries the success bit.
    return web.json_response({"restart_required": bool(result.get("restart_required"))})


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

    Refuses unless the derived step is ``ready``. That is the whole machine-state
    precondition set, read from ``_derive_step`` rather than re-checked here: a
    QR for a URL nothing answers is a support ticket, not a feature, and a QR
    issued under an administrator's tailnet pin is a credential the ceiling
    forbids. On top of the machine state, the CALLER's own session bounds are
    enforced via ``_shared._caller_bounds`` (the same helper the mobile-link
    mint uses): the minted token never out-scopes the session authorizing it,
    and a caller with no lifetime left to lend is refused.
    """
    refusal = _guard(request)
    if refusal is not None:
        await _audit_async(request, "tailnet.mobile.qr", "denied", "restricted-session")
        return refusal

    # Governance chokepoint: minting a phone QR is the "tailnet_qr" method of
    # the capabilities.mobile_connect scope. The methods listing may already
    # hide this method, but omission is presentation only — the mint itself
    # re-runs the decision (fail-closed inside mint_denied_reason). Distinct
    # from capabilities.tailnet_origin (checked below via the derived step),
    # which governs the tailnet ORIGIN as a whole; this row governs the
    # phone-credential family across all methods.
    denied = await asyncio.to_thread(mint_denied_reason, "tailnet_qr")
    if denied:
        await _audit_async(request, "tailnet.mobile.qr", "denied", "governance-mobile-connect")
        return web.json_response({"error": denied, "code": "governance_denied"}, status=403)

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
    # Clamped by this endpoint's own ceiling first, then the global session
    # ceiling, so neither a caller-supplied value nor a future raise of
    # MAX_QR_TTL_SECS can exceed what token_auth itself allows. The caller's
    # own remaining lifetime is applied further down, after the last awaited
    # step before the mint, so it cannot go stale while this handler waits.
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
    # Mutually exclusive as the DEFAULT shapes on purpose: choosing both for an
    # unbounded caller would mean a session that neither refreshes nor lasts,
    # which is worse than either. A BOUNDED caller is different — its carried
    # claims are merged over the configured shape below, and a token carrying
    # both ``boot`` and ``no_refresh`` is then the honest intersection: the
    # session ends at whichever bound is hit first, which is exactly what
    # "never out-scope the caller" requires.
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
    # Read INDEPENDENTLY, each with its own conservative default, rather than as
    # one all-or-nothing block. Coupling them means a config object missing any
    # ONE attribute discards the other two — so adding this shape would silently
    # take the existing opt-out away from anyone whose config predates it, which
    # is the opposite of "an unreadable override falls back to the default".
    # Per-field is the faithful version of that rule.
    _until_restart = True
    _persist = False
    _identity_trusted = False
    try:
        _cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.debug("tailnet mobile: config unreadable for session shape", exc_info=True)
    else:
        _dash = getattr(_cfg, "dashboard", None)
        _until_restart = bool(getattr(_dash, "qr_session_until_restart", True))
        _persist = bool(getattr(_dash, "qr_session_persist_across_restart", False))
        _ts = getattr(_dash, "tailscale", None)
        _identity_trusted = bool(
            getattr(_ts, "trust_identity", False) and getattr(_ts, "allowed_logins", None)
        )

    # THIRD shape - ``persistent``: the refresh chain with NO boot claim, so one
    # scan survives a gateway restart and is bounded only by the chain's own
    # 30-day lifetime. Opt-in, because the boot bound it removes is a hard revoke
    # needing no recorded state, and that default was chosen deliberately.
    #
    # GATED on daemon-verified tailnet identity, and the gate is what makes this
    # offerable at all rather than merely convenient. Behind ``tailscale serve``
    # every request reaches the gateway from 127.0.0.1 (#1762), so with identity
    # trust off the pin is ``ip:127.0.0.1`` for every tailnet client and the
    # cookie is a bearer credential any of them could replay. A session that ends
    # at the next restart bounds that exposure; one that outlives the process does
    # not. With ``trust_identity`` plus an allowlist the session pins to a
    # verified peer instead, and the exposure is bounded by identity rather than
    # by uptime.
    #
    # Both refusals are LOUD. Turning the flag on and silently getting a
    # boot-bound session is the "checked but never ran, reported as a clean
    # result" defect: the operator would believe the phone survives restarts and
    # find out only by being signed out.
    if _persist and not _until_restart:
        logger.warning(
            "dashboard.qr_session_persist_across_restart is on but "
            "qr_session_until_restart is off, so the timed shape is in force and "
            "there is no refresh chain to carry across a restart. The phone "
            "session stays bounded by its TTL; turn qr_session_until_restart on "
            "to use the persistent shape."
        )
        _persist = False
    if _persist and not _identity_trusted:
        logger.warning(
            "dashboard.qr_session_persist_across_restart is on but tailnet "
            "identity trust is not configured, so the phone session stays bound "
            "to this gateway process. Behind `tailscale serve` every request "
            "arrives from 127.0.0.1, so without dashboard.tailscale.trust_identity "
            "plus a non-empty allowed_logins the session cannot be pinned to a "
            "verified peer and must not outlive the process."
        )
        _persist = False

    if _persist:
        # No ``boot``, so the chain rather than the process bounds this session,
        # and no ``no_refresh``, so the chain exists at all. ``require_peer`` is
        # what keeps that honest: this session's whole security argument is that
        # it is pinned to a daemon-verified tailnet identity, so the chain must
        # refuse to rotate whenever that identity cannot be established.
        #
        # An address pin is NOT an alternative here, and that is the whole reason
        # this claim exists rather than reusing the boot-bound rotation path.
        # Behind ``tailscale serve`` every request reaches the gateway from
        # 127.0.0.1, so ``ip:127.0.0.1`` is satisfied by every peer on the
        # tailnet - it would read as a pin in the audit trail while excluding
        # nobody. Refusing the rotation is the only bound that actually holds.
        shape: dict[str, str] = {"require_peer": "1"}
    elif _until_restart:
        shape = {"boot": current_boot_id()}
    else:
        shape = {"no_refresh": "1"}
    # The calling session's own bounds cap everything minted below — the same
    # invariant the sibling mobile-link mint enforces, read through the same
    # shared helper so the two surfaces cannot drift. A deliberately bounded
    # owner session (``no_refresh``, or a short remaining ``session_exp``) must
    # not trade itself for a boot-bound, refresh-chained credential on another
    # device: behind ``tailscale serve`` every request reaches the gateway from
    # 127.0.0.1, so the token cannot be device-pinned and its own bounds are the
    # only limit that holds. A caller with no lifetime left to lend is refused
    # outright — minting against it would hand out a credential that outlives
    # the session authorizing it.
    #
    # Read AFTER every awaited step above (the request body and the config
    # load), deliberately: the remaining lifetime is a wall-clock snapshot, and
    # a client that trickles the request body in controls how long this handler
    # waits — a snapshot taken before those awaits would let a caller in its
    # last seconds stretch the mint past its own expiry.
    carried, ttl_ceiling = _caller_bounds(request)
    if ttl_ceiling <= 0:
        await _audit_async(request, "tailnet.mobile.qr", "denied", "caller-session-expired")
        return web.json_response(
            {
                "error": (
                    "This session has no lifetime left to lend, so no sign-in "
                    "link can be issued. Sign in again, then scan."
                ),
                "code": "caller_session_expired",
            },
            status=403,
        )
    # The caller's remaining lifetime completes the clamp: a short-lived caller
    # asking for the default cannot exceed what the authorizing session itself
    # has left.
    ttl = min(ttl, ttl_ceiling)
    # The caller's carried bounds win on conflict and are never dropped: a
    # ``boot`` claim is carried verbatim rather than re-derived (the same rule
    # the link→session exchange follows), and a ``no_refresh`` caller stamps the
    # minted credential ``no_refresh`` regardless of the configured shape, so
    # the phone session never grows a refresh chain its authorizing session did
    # not have.
    #
    # This caps the PERSISTENT shape too, and that is correct rather than a gap:
    # a credential must not outlive the session that authorized it, so an owner
    # who is themselves signed in on a boot-bound session hands the phone a
    # boot-bound session as well, whatever the config says. Reaching the
    # persistent shape therefore also requires the authorizing session to be
    # unbounded - which the desktop local-bootstrap mint is, since
    # ``/api/token/local`` carries neither ``boot`` nor ``no_refresh``.
    claims = {**shape, **carried}
    caller_peer_key = claims.pop("peer_key", "")
    token = generate_token(
        owner_id or "local-app",
        ttl_seconds=ttl,
        peer_key=caller_peer_key,
        extra=claims,
    )
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
            # created would have expired. generate_token clamps the link-click
            # ``exp`` to the session TTL, so a short-lived caller's link dies
            # with the ttl it lent — report the live window, not the constant.
            "link_window_secs": min(LINK_WINDOW_SECS, ttl),
            "host": host,
        }
    )
