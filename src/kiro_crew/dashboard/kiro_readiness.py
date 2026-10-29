"""Shared pre-enqueue guard for Kiro-backed dashboard sessions.

Readiness is probed once at gateway start and then only on an explicit user
action (see ``kiro_prerequisite.KiroPrerequisiteService.session_ready``), so the
latched value can be arbitrarily stale. That splits the callers in two:

* **Ordinary sends are UNGATED.** A stale not-ready value must never block a
  send: the real ACP attempt is the authority, and it reports a signed-out CLI as
  an actionable ``AcpAuthRequired`` error in the chat transcript. Blocking on
  latched state was the stuck case — a user who signed in from a terminal stayed
  locked out until something re-probed. These handlers mutate nothing before the
  turn, so a failed turn costs only an error card.
* **Endpoints that act BEFORE the turn still BLOCK**
  (:func:`reject_if_kiro_unverified`) — the poll-driven ``kiro-cli`` spawn sites
  and the destructive reruns. Neither can rely on the ACP attempt as its
  authority: one has no turn at all, the other has already rewritten durable
  history by the time the turn fails. See
  ``docs/system-specs/modules/acp-client.md`` § "Poll-driven spawn sites are
  readiness-gated". The callers that only start a session turn are also
  authorized by Crew's own sign-in when that turn runs on the KAS engine bundle,
  which needs no kiro-cli (:func:`kas_crew_signin_ready`).
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

logger = logging.getLogger(__name__)

# The one 503 ``code`` every "Kiro CLI must be signed in" refusal carries, so a
# client can tell a sign-in wall from any other degraded answer. Public because a
# SECOND spawn site reaches the same verdict a different way: ``GET /api/models``
# detects an EXPIRED mid-session login from ``--list-models`` auth stderr (the
# readiness latch is stale-true there, so this gate's own refusal never fires) and
# must answer with the identical code so the dashboard shows one sign-in message.
KIRO_PREREQUISITE_REQUIRED_CODE = "kiro_prerequisite_required"
_KIRO_NOT_READY_RESPONSE = {
    "error": "Kiro CLI setup or sign-in is required before starting a session.",
    "code": KIRO_PREREQUISITE_REQUIRED_CODE,
}
_KIRO_NOT_READY_CODE = _KIRO_NOT_READY_RESPONSE["code"]

# When was the current refusal last reported at WARNING? ``None`` means "not yet, warn
# now". Module-level because the gate must work with no service at all (the fail-closed
# path has no object to hang state on), matching ``mcp_discovery``'s module-level
# warn-once ledgers. See :func:`_warn_refused_once` for why it is one cell rather than
# a per-path ledger.
_refusal_warned_at: float | None = None

# Longest an ongoing outage may stay silent at WARNING. Clearing on authorize
# (:func:`_clear_refusal_warning`) re-arms immediately when a gated caller OBSERVES the
# recovery, but nothing observes a recovery on a gateway whose dashboard is closed and
# whose pollers have stopped, and the flag would then still be set when the NEXT outage
# began — the subtler silence this whole mechanism exists to avoid. A floor makes the
# guarantee unconditional: no outage is ever quiet for longer than this, whether or not
# a recovery was ever seen. 30 minutes is ~2 lines/hour against a 1000-entry ring, four
# orders below what one line per refused request costs.
_REFUSAL_REWARN_SECS = 1800.0

# Indirected so a test can advance it without touching the ``time`` module globally,
# the same clock-injection shape ``KiroPrerequisiteService`` uses for its own staleness.
_clock = time.monotonic

# How stale a probe may be and still authorize a DESTRUCTIVE rerun. Small enough
# that an external logout cannot linger behind this gate before history is
# rewritten, large enough that a burst of callers collapses onto one probe. The
# reruns are rare and human-paced, so the tight bound costs nothing here.
_VERIFY_MAX_AGE_SECS = 30.0

# How stale a probe may be and still authorize the ``/api/sessions/usage``
# poll gate. The 30s bound above is a poison for it: that bound expires at almost
# every tick, so whichever request lands first after expiry runs a full
# ``whoami`` probe INLINE and waits seconds on it, which is the credit pill
# periodically hanging. The gate exists only to keep a signed-out gateway from
# spawning a browser-opening CLI on a timer, and that threat tolerates a wide
# window: the worst case of a longer bound is one such spawn up to this long
# after an external logout, where the tight bound is one every poll interval. A
# real logout is also caught sooner by the identity-change re-probe and by
# Refresh. The usage spawn is itself throttled to one fetch per
# ``_USAGE_REFRESH_SECS`` (600s), so the wide window cannot storm it; five
# minutes is comfortably longer than the poll interval, so a healthy latch
# authorizes many polls between probes and the periodic probe keeps the single
# service-wide latch warm for the tight-bound callers.
_POLL_GATE_MAX_AGE_SECS = 300.0


async def kiro_session_ready(service: object) -> bool:
    """Return the service's latched readiness. Fails closed on a bad service."""

    if not isinstance(service, KiroPrerequisiteService):
        return False
    return await service.session_ready()


async def kiro_verified_ready(
    service: object, *, max_age_secs: float = _VERIFY_MAX_AGE_SECS
) -> bool:
    """Return readiness backed by a probe no older than *max_age_secs*.

    The latch alone cannot authorize these callers. It is written at boot and
    narrowed only when a chat turn observes ``AcpAuthRequired``, so an external
    logout with no chat turn in between leaves it ``ready=True`` indefinitely —
    and every one of this gate's callers acts irreversibly on that answer
    (deletes history, or spawns a browser-opening ``kiro-cli``). "Probe at boot
    only" is the right rule for the send path, which risks nothing; it is the
    wrong rule for authorization.

    So this re-probes when the latch is older than *max_age_secs*. That is
    bounded work — it happens only on a destructive rerun or a poll tick, never
    on the message hot path — and the service's own short cache collapses bursts
    (e.g. the three destructive routes, or several pollers firing together) into
    one probe.

    The bound is the CALLER's to pass, because the two caller classes carry
    different freshness needs. A destructive rerun rewrites persisted history, so
    it reads on ``_VERIFY_MAX_AGE_SECS`` — tight, since a stale ``ready=True``
    there drops prior turns. A poll-driven spawn site only guards against a
    timed browser login, so it reads on ``_POLL_GATE_MAX_AGE_SECS`` — wide, since
    a tight bound there expires at every poll and forces the probe inline.
    """

    if not isinstance(service, KiroPrerequisiteService):
        return False
    return await service.verified_ready(max_age_secs=max_age_secs)


def _kas_bundle_turn_sync(session_key: str | None) -> bool:
    """True when the turn on *session_key* runs on Crew's own installed KAS engine bundle.

    That route spawns no kiro-cli at all (``acp/harness/kas.py``
    ``KasHarness.resolve_spawn``): the engine is the bundle's Node build and its
    credential is Crew's own sign-in, never kiro-cli's store. The harness takes
    that route when it is KAS and ``agent.kas_bundle`` is on, so the conditions
    are the same three, read per session:

    * the session's own backend is KAS, through ``select_provider_backend``, the
      gate the provider factory uses: a member thread runs on
      ``agent.member_acp_backend``, so the default backend alone does not name
      the harness its rerun will start on (``None`` is a new or unknown slot,
      which takes the default). ``kas_bundle.kas_bundle_route()`` is not used:
      it asks about the default backend only, and so refuses a member thread
      on KAS under a non-KAS default;
    * ``agent.kas_bundle`` on, the flag ``KasHarness`` itself reads;
    * an engine is installed, since a bundle spawn with nothing installed refuses.

    Blocking (config reads and a stat of the engine record); never raises.
    """
    try:
        from kiro_crew.agent_sdk import kas_bundle
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.crewmate_harness import crewmate_backend_pin
        from kiro_crew.members import select_provider_backend

        config = KiroCrewConfig.load()
        agent = config.agent
        if not agent.kas_bundle:
            return False
        # A crewmate's harness pin outranks the member backend, as it does for
        # the provider factory. A pin this gateway cannot start raises here, and
        # the except below then keeps the kiro-cli gate.
        backend = select_provider_backend(
            session_key,
            agent.member_acp_backend,
            agent.acp_backend,
            crew_backend=crewmate_backend_pin(config, session_key),
        )
        if backend == ACP_BACKEND_KAS:
            return kas_bundle.installed_bundle() is not None  # type: ignore[misc]
        return False
    except Exception as exc:  # noqa: BLE001 - an unreadable answer keeps the kiro-cli gate
        logger.debug("Kiro readiness: KAS bundle route probe failed (%s)", type(exc).__name__)
        return False


def turn_gate_target(state: object, slot_key: object) -> tuple[bool, str | None]:
    """``(harness_turn, session_key)`` for a gate in front of a turn on *slot_key*.

    The session is the one the slot's turns run on (``effective_session_key``);
    a slot that does not exist yet is ``None``, the default backend's session.
    A state or slot this cannot read answers ``(False, None)``: the caller keeps
    the kiro-cli gate rather than being judged as a default-backend session.
    Never raises, so the gate still answers before the handler touches state.
    """
    try:
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key

        slots = getattr(state, "_slots", None)
        if slots is None:
            return False, None
        slot = slots.get(slot_key) if slot_key else None
        return True, (effective_session_key(slot) if slot is not None else None)
    except Exception as exc:  # noqa: BLE001 - an unreadable slot keeps the kiro-cli gate
        logger.debug("Kiro readiness: turn session unreadable (%s)", type(exc).__name__)
        return False, None


def live_turn_route(state: object, session_key: str | None) -> bool | None:
    """Whether the LIVE session on *session_key* is Crew's KAS engine bundle.

    A session that already has a provider runs its next turn on that provider:
    ``SessionManager.get_or_create`` reuses it with no backend check, and
    changing ``agent.acp_backend`` retires only the background runtime. So the
    configured backend does not name the route of a live session's turn; the
    running process does, and this reads it from the spawn plan it recorded
    (``AcpRuntime._kas_engine_bundle``).

    * ``None``: no live session (no session map, no key, no provider). The turn
      cold-starts, and the configured selection decides its route.
    * ``True``: the live process is the engine bundle, so its turn spawns no
      kiro-cli.
    * ``False``: a live session on any other route, or one whose route cannot be
      read (another provider type, a runtime not spawned yet). It keeps the
      kiro-cli gate: a retained kiro-cli session would fail auth after the
      rerun had already rewritten history.

    Reads only in-memory state on the event loop. Never raises.
    """
    if session_key is None:
        return None
    try:
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return None
        provider = sessions.get_provider(session_key)
        if provider is None:
            return None
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

        # The session map holds the ``AcpProvider`` wrapper; the runtime lives on
        # the ``AcpSessionProvider`` it exposes as ``client``. A bare session
        # provider carries the runtime itself. Any other shape reads no runtime.
        inner = provider
        if getattr(provider, "_runtime", None) is None:
            inner = getattr(provider, "client", None)
        runtime = getattr(inner, "_runtime", None)
        on_kas = getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS
        return on_kas and getattr(runtime, "_kas_engine_bundle", False) is True
    except Exception as exc:  # noqa: BLE001 - an unreadable live session keeps the gate
        logger.debug("Kiro readiness: live session route unreadable (%s)", type(exc).__name__)
        return False


async def kas_crew_signin_ready(session_key: str | None, live_route: bool | None = None) -> bool:
    """True when the turn on *session_key* needs no kiro-cli and Crew's sign-in can serve it.

    *live_route* is :func:`live_turn_route` for that session. A live session is
    judged by the process it is running, never by the configuration: ``False``
    refuses the bypass, ``True`` skips the configured-route check. Only ``None``
    (a cold start) reads the configured selection.

    The turn-starting gate's counterpart of :func:`kiro_verified_ready` for a
    session that runs on the KAS engine bundle. There the kiro-cli probe answers
    a question the turn never asks (on a host with no kiro-cli it is never
    ready), so the authority is what the engine will ask Crew for:
    :class:`~kiro_crew.auth.provider.KasAuthProvider` resolves it from the vault
    and refreshes it under the identity's lock, the same call that answers the
    engine's ``_kiro/auth/getAccessToken``. A rejected or unattemptable refresh,
    or no stored identity, answers False, so a destructive rerun is still
    refused before history is rewritten. Fresh on every call: there is no latch
    to go stale. Never raises.
    """
    if live_route is False:
        return False
    if live_route is None and not await asyncio.to_thread(_kas_bundle_turn_sync, session_key):
        return False
    try:
        # Deferred: kiro_crew.auth pulls the ``cryptography`` wheel, which must
        # not load on the handlers' import path (see kiro_crew.acp.kas_host_auth).
        from kiro_crew.auth.bridge import default_token_store
        from kiro_crew.auth.provider import KasAuthProvider

        await KasAuthProvider(default_token_store(), allow_env_api_key=False).current()
    except Exception as exc:  # noqa: BLE001 - an unusable sign-in is not ready
        # Type only: a refresh error's message can carry response bytes.
        logger.debug("Kiro readiness: Crew sign-in cannot serve the turn (%s)", type(exc).__name__)
        return False
    return True


def _log_safe_path(request: object) -> str:
    """The request path, rendered so it cannot forge a log line.

    ``aiohttp``'s ``Request.path`` is URL-DECODED, so ``%0A`` in a path segment
    arrives as a real newline, and the dynamic-route pattern for ``{slot}``
    excludes only ``/{}`` — a newline matches it and reaches this gate. Logging
    that verbatim lets a caller append arbitrary lines to ``gateway.log``, which
    would defeat the entire reason the refusal is logged at all: the log is meant
    to be evidence, and forgeable evidence is worse than none.

    Scope of the threat, stated exactly, because the halves differ. Only
    U+000A/U+000D put a real byte ``0a``/``0d`` in the file, so only those forge a
    physical line there. ``%C2%85``/``%E2%80%A8``/``%E2%80%A9`` decode to
    U+0085/U+2028/U+2029, which are multi-byte UTF-8 and therefore leave the FILE
    one line — but they are ``str.splitlines()`` boundaries, so any Python consumer
    that re-splits the log sees the forged line, and a rendered view may break on
    them too. Invisible formatting characters forge nothing structurally and reorder
    what a human reads. Every route ends at the same place: a line the reader cannot
    trust.

    ``repr`` does the escaping rather than a hand-written character class, because a
    class narrow enough to write out is narrower than the threat. ``Cc``/``Cf``/``Zl``/
    ``Zp`` alone misses four categories that ``yarl``'s percent-decoding can deliver:
    ``%C2%A0`` and ``%E3%80%80`` are ``Zs``, ``%EE%80%80`` is ``Co``, ``%CD%B8`` is
    ``Cn``. ``str.__repr__`` escapes everything ``str.isprintable()`` rejects, which is
    all of those plus ``Cs``, and it takes that from the interpreter's own Unicode
    tables rather than from a list somebody has to remember to extend. ``Cs`` is not
    reachable here — ``yarl`` leaves an invalid escape like ``%ED%A0%80`` as literal
    text rather than decoding it to a lone surrogate — but it matters that ``repr``
    covers it anyway: a raw surrogate is a string a UTF-8 log handler cannot encode,
    and ``logging`` swallows handler errors, so the line would vanish. Losing the line
    is the one outcome this function exists to prevent.

    ESCAPED rather than rejected, unlike dev_fleet's ``reject_unsafe``: this runs
    on the fail-CLOSED branch, where raising would turn a correct 503 into a 500.
    Escaped rather than stripped, too — a caller who sent one of these is exactly
    what the reader wants to see, so the evidence is preserved, just rendered inert.
    Visible non-ASCII survives (``repr`` escapes only the unprintable), so a path is
    not ASCII-folded for the operators most likely to need to read it. The quotes
    ``repr`` adds are wanted: they delimit the value, so trailing whitespace or an
    empty path is visible instead of blending into the message. Non-``str``
    (attribute absent, or a request-like double) degrades to the placeholder for the
    same no-raise reason.
    """
    raw = getattr(request, "path", None)
    if not isinstance(raw, str):
        return "<unknown path>"
    return repr(raw)


def _warn_refused_once(path: str) -> None:
    """WARNING on the transition into refusing, DEBUG thereafter.

    Mirrors ``mcp_discovery._warn_probe_sandbox_unavailable_once``, and for the same
    reason. Every post-spawn failure branch in ``api_models`` logs a WARNING, so a
    reader who greps the log for that endpoint and finds nothing concludes it is
    healthy. A silent refusal here therefore inverts the diagnosis rather than merely
    withholding it. An absent line gets read as evidence; it is not. So the refusal
    must be visible.

    It must ALSO not be visible 570 times an hour. A signed-out gateway with an open
    dashboard polls ``/api/models`` every 8s and ``/api/sessions/usage`` every 30s,
    and every one of those refuses HERE — the sibling branches that log in
    ``api_models`` (``dashboard/handlers/agents.py``) sit BELOW its call to this gate
    and are never reached in that state, so one line per refused request is
    not "the same order of magnitude as its siblings" — it is 570 lines/hour against
    their none. The dashboard log ring is ``deque(maxlen=1000)``
    (``handlers/updates_streams.py::_log_ring``), which that rate churns end to end every ~1.8
    hours, evicting the genuine diagnostics an operator came to read. A line meant to
    make the log trustworthy must not be the thing that empties it.

    One line per outage is the honest amount: the condition reported is
    gateway-global ("the CLI is not verified ready"), not per-endpoint, so the flag
    is global too and the message names whichever caller hit it first. Deliberately
    NOT keyed on the path — that is caller-controlled, and a per-path ledger would
    grow without bound on request.
    """
    global _refusal_warned_at

    now = _clock()
    if _refusal_warned_at is not None and now - _refusal_warned_at < _REFUSAL_REWARN_SECS:
        logger.debug("%s refused with 503 %s (already reported)", path, _KIRO_NOT_READY_CODE)
        return
    _refusal_warned_at = now
    logger.warning(
        "%s refused with 503 %s: Kiro CLI is not verified ready. Further refusals log "
        "at DEBUG until it recovers or %.0fs elapse. Check the prerequisite snapshot "
        "for which condition — a missing binary, a sandbox refusal and a timed-out "
        "probe are three different failures.",
        path,
        _KIRO_NOT_READY_CODE,
        _REFUSAL_REWARN_SECS,
    )


def _clear_refusal_warning() -> None:
    """Forget the reported refusal once the gate authorizes again.

    Without this the first outage after boot would consume the only WARNING the
    process ever emits, and every LATER outage would be silent — the original
    defect back in a subtler form, which is exactly why
    ``mcp_discovery._clear_unresolvable`` exists alongside its warn-once.

    This half only fires when a gated caller OBSERVES the recovery, which is why it
    is not the whole answer: on a gateway whose dashboard is closed the pollers stop,
    nothing calls this, and the flag would still be set when the next outage began.
    ``_REFUSAL_REWARN_SECS`` bounds that case; this one makes an observed recovery
    re-arm immediately rather than waiting the floor out.
    """
    global _refusal_warned_at

    _refusal_warned_at = None


def _service(request: web.Request) -> object:
    service = request.app.get("kiro_prerequisite_service")
    if service is None:
        service = getattr(request.app.get("state"), "kiro_prerequisite_service", None)
    return service


async def reject_if_kiro_unverified(
    request: web.Request,
    *,
    max_age_secs: float = _VERIFY_MAX_AGE_SECS,
    harness_turn: bool = False,
    session_key: str | None = None,
    turn_reuses_session: bool = True,
) -> web.Response | None:
    """Return 503 for the endpoints that must fail closed on a stale latch.

    Two classes qualify, both because the ACP attempt cannot be their authority:

    * **Poll-driven ``kiro-cli`` spawn sites** (``/api/models``,
      ``/api/sessions/usage``) — they shell out on a timer with no turn to report
      into, and an unauthenticated spawn opens an interactive browser login (and
      ``kiro-cli chat`` hangs) on every poll interval.
    * **Destructive reruns** (regenerate, edit-resend, rewind) — they truncate
      and PERSIST session history *before* the background turn starts, so a
      signed-out install would drop prior turns while returning 200. There is no
      later error card that can undo a durable rewrite, so the check has to
      happen before the mutation.
    * **``POST /v1/chat/completions``** — no transcript the caller reads. Its
      collectors take only ``chunk``/``assistant`` roles, so an ``AcpAuthRequired``
      turn's ``error`` card is invisible and the request would answer 200 with
      empty content, which an SDK client cannot tell apart from a model that said
      nothing.

    Ordinary sends are deliberately NOT gated: they mutate nothing up front, so a
    stale latch must not block them (see the module docstring). That includes
    ``POST /api/chat/slots/{slot}/continue``, which only queues a synthetic
    continuation for the runner to dispatch — it is a send with a machine-written
    body, not a fourth class. A missing or invalid service fails closed here.

    These callers must not trust the latch in EITHER direction, so this uses
    :func:`kiro_verified_ready` — a stale ``ready=True`` is as dangerous as a
    stale ``ready=False`` here (it authorizes the history rewrite or the
    browser-opening spawn), and only these paths pay for the re-probe.

    *max_age_secs* is how stale the backing probe may be. The default fits the
    destructive reruns and ``/v1/chat/completions``, which carry the tight
    freshness need. The poll-driven spawn sites pass
    :data:`_POLL_GATE_MAX_AGE_SECS` instead: their only risk is a timed browser
    login, which a wide window still contains, and the tight default would expire
    at nearly every poll and run the probe inline — the latency this gate's own
    pollers would otherwise pay on each tick.

    *harness_turn* says the caller's only kiro-cli dependency is the session
    turn it starts (the destructive reruns and ``/v1/chat/completions``), and
    *session_key* names the session that turn runs on (``None`` for a slot that
    does not exist yet). Such a caller is also authorized by
    :func:`kas_crew_signin_ready`: when that session runs on the KAS engine
    bundle, its turn spawns no kiro-cli and is served by Crew's sign-in, so the
    kiro-cli probe is not its authority. It is checked
    first because it reads only local state unless the vault token needs a
    refresh, and it spares a host that never runs kiro-cli the probe. A caller
    that spawns kiro-cli itself (the ``/api/models`` catalog off the bundle
    route, the usage scrape) leaves it False and stays gated on kiro-cli.

    *turn_reuses_session* says whether that turn runs on the session's live
    provider. Regenerate and ``/v1/chat/completions`` reuse it, so a live
    session is judged by its own running process (:func:`live_turn_route`).
    Edit-resend and rewind discard the conversation before their turn, so the
    turn cold-starts from the current configuration; they pass False and are
    judged on the configured route only, whatever the discarded process was.
    """

    live_route = (
        live_turn_route(request.app.get("state"), session_key) if turn_reuses_session else None
    )
    if harness_turn and await kas_crew_signin_ready(session_key, live_route):
        _clear_refusal_warning()
        return None
    if await kiro_verified_ready(_service(request), max_age_secs=max_age_secs):
        _clear_refusal_warning()
        return None
    _warn_refused_once(_log_safe_path(request))
    return web.json_response(_KIRO_NOT_READY_RESPONSE, status=503)
