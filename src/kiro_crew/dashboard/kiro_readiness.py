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
  readiness-gated".

The latch describes a KIRO-CLI sign-in. The destructive reruns and
``/v1/chat/completions`` therefore apply it only while the session they act on
authenticates through kiro-cli's identity store
(:func:`backend_signs_in_via_kiro_cli` over :func:`selected_backend`): on a
claude-agent-acp or codex-acp session it describes a binary that session never
spawns. The poll-driven spawn sites run kiro-cli themselves, so they pass
``signs_in_via_kiro_cli=True`` and stay gated whatever the selected harness.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

from aiohttp import web

from kiro_crew.acp_backends import backends_retired_by_host_logout
from kiro_crew.config import KiroCrewConfig
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.members import select_provider_backend

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


async def selected_backend(
    session_key: str | None = None, *, state: object | None = None
) -> str | None:
    """Return the backend the relevant new session would use, or ``None``.

    When *state* exposes a session manager with an installed provider factory
    and its paired concrete backend fields, those fields are authoritative: the
    destructive reruns that pass state rebuild through that factory, while a
    newer disk config may still be waiting for the watcher to install it. Any
    unrecognized state shape falls through to the existing off-loop config read,
    which remains authoritative for callers that spawn directly from config.

    With a *session_key* the answer goes through
    ``members.select_provider_backend`` -- the ONE per-session selection gate
    the factory itself calls (harness-parity H3/H13) -- so a ``member-*``
    session is judged by ``agent.member_acp_backend`` exactly as the factory
    routes it, denied-or-unknown value degrading to kiro included. Judging it by
    ``agent.acp_backend`` instead gets the member thread wrong in both
    directions: a kiro default holds a Claude-routed member rerun behind a
    kiro-cli sign-in it never needs, and a Claude default lets a KAS-routed
    member rerun rewrite its history on a signed-out kiro-cli, which is the
    very rewrite the gate exists to refuse. Without a key (the poll-driven
    spawn sites, which act for the gateway rather than for one session) the
    configured default is the answer, as before.

    Outside destructive reruns, read ONCE per request and pass the answer along.
    A handler that reads the backend for its own branch and then lets the gate
    read it again has two snapshots, and a ``PATCH agent.acp_backend`` landing
    between them lets the kiro-cli branch proceed on the first while the gate
    stands aside on the second -- one unauthenticated spawn, which is exactly
    the browser-opening event the gate exists to prevent. Destructive reruns
    deliberately re-derive this verdict under the slot lock immediately before
    their first irreversible write. A factory switch after that write can still
    leave the rerun failing against the replacement backend after native or
    durable history state has changed.
    """

    sessions = getattr(state, "sessions", None)
    factory = getattr(sessions, "_provider_factory", None)
    installed_cfg = getattr(sessions, "_cfg", None)
    installed_agent = getattr(installed_cfg, "agent", None)
    installed_backend = getattr(installed_agent, "acp_backend", None)
    installed_member_backend = getattr(installed_agent, "member_acp_backend", None)
    if (
        factory is not None
        and isinstance(installed_backend, str)
        and isinstance(installed_member_backend, str)
    ):
        return select_provider_backend(
            session_key,
            installed_member_backend,
            installed_backend,
        )

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.warning(
            "Could not read agent.acp_backend; keeping the kiro-cli readiness gate",
            exc_info=True,
        )
        return None
    agent = getattr(cfg, "agent", None)
    backend = getattr(agent, "acp_backend", "")
    if not isinstance(backend, str):
        return None
    if session_key is None:
        return backend
    member_backend = getattr(agent, "member_acp_backend", None)
    if not isinstance(member_backend, str):
        return None
    return select_provider_backend(session_key, member_backend, backend)


def backend_signs_in_via_kiro_cli(backend: str | None) -> bool:
    """Whether *backend* authenticates through kiro-cli's identity store.

    Answered as POSITIVE membership in ``backends_retired_by_host_logout()``
    (harness-parity H5/H6): kiro-cli and KAS resolve every access token from that
    store, so a kiro-cli sign-in verdict is a verdict about their sessions. A
    harness outside the set (claude-agent-acp, codex-acp) signs in some other way,
    and the readiness probe -- ``kiro-cli --version`` then ``whoami`` -- says
    nothing about whether its sessions can run.

    ``None`` (unknown: config unreadable, or a provider that is not an ACP
    harness) answers ``True``, failing CLOSED toward the gate: the kiro-cli latch
    keeps governing exactly as it did before this question existed. Guessing
    "foreign harness" on a broken config would un-gate the browser-opening spawn
    on the one host where nothing else can be trusted either.
    """

    if backend is None:
        return True
    return backend in backends_retired_by_host_logout()


async def live_session_signs_in_via_kiro_cli(state: object, session_key: str) -> bool | None:
    """Whether a LIVE session on *session_key* signs in through kiro-cli, or ``None``.

    A live session keeps the backend it was started on: ``PATCH
    agent.acp_backend`` refreshes the defaults new sessions are built with and
    deliberately retires nothing in flight (``handlers/core.py``). So for a
    caller that CONTINUES an existing session -- regenerate does, it neither
    discards nor rebuilds -- the configured default is the wrong thing to gate
    on after a switch: the turn will run on the old harness, and if that is a
    signed-out kiro-cli the history is rewritten before the auth failure lands.

    The answer is the provider's OWN declaration, ``uses_kiro_identity_store``
    (harness-parity H14) -- the same membership the gate derives for a
    configured backend through :func:`backend_signs_in_via_kiro_cli`, read from
    the one place the session layer already reads it, so the id is never
    re-derived here. Peeks only, never creates: ``state.sessions._sessions`` is
    read the way ``chat_runner`` reads it for ``/compact``. Anything that is not
    a live session carrying a ``bool`` declaration answers ``None`` -- a mocked
    manager, a key with no session, a provider that declares nothing -- and the
    caller then resolves the backend its next session will use.

    "Live" means the manager's own ``is_provider_alive`` answers ``True``, the
    test ``chat_turn/slash_commands._slash_command_backend`` uses. A row whose
    process has exited is not reused: ``get_or_create`` evicts it and builds the
    turn's session on the installed factory, so a dead foreign row must not
    answer for a kiro-cli successor. Anything but ``True`` (dead, unknown, no
    probe, a probe that raised) answers ``None``, failing toward the gate.
    """

    manager = getattr(state, "sessions", None)
    sessions = getattr(manager, "_sessions", None)
    if not isinstance(sessions, dict):
        return None
    declared = getattr(
        getattr(sessions.get(session_key), "provider", None), "uses_kiro_identity_store", None
    )
    if not isinstance(declared, bool):
        return None
    probe = getattr(manager, "is_provider_alive", None)
    if not callable(probe):
        return None
    try:
        alive = probe(session_key)
        if inspect.isawaitable(alive):
            alive = await alive
    except Exception:
        logger.debug("live-session liveness probe failed for %s", session_key, exc_info=True)
        return None
    return declared if alive is True else None


def refuse_rerun_after_signed_out_turn(slot: object) -> web.Response | None:
    """503 for a foreign-harness rerun on a slot whose last turn ended signed out.

    The kiro-cli latch says nothing about a foreign harness, so the replacement
    turn is the sign-in authority there, and a rerun truncates durably before it
    runs. The one sign-in fact Crew does hold for that harness is the slot's own
    last outcome: ``_last_turn_auth_required`` is set when a turn ends
    ``AcpAuthRequired`` and rewritten when the next turn ends. When it is set the
    rerun would cut history only to fail the same way, so refuse before the
    first destructive write. An ordinary send stays ungated: it mutates nothing
    up front, and its turn clears the flag once the harness is signed in again.
    """
    if getattr(slot, "_last_turn_auth_required", False) is True:
        return web.json_response(
            {
                "error": "The last turn here ended because the coding agent is signed "
                "out. Sign in, then send a message before rewriting this conversation.",
                "code": "harness_sign_in_required",
            },
            status=503,
        )
    return None


async def rerun_signs_in_via_kiro_cli(state: object, session_key: str) -> bool:
    """Continuing-rerun verdict: live session, then installed pair, then config."""

    live_verdict = await live_session_signs_in_via_kiro_cli(state, session_key)
    if live_verdict is not None:
        return live_verdict
    return backend_signs_in_via_kiro_cli(await selected_backend(session_key, state=state))


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
    signs_in_via_kiro_cli: bool | None = None,
    session_key: str | None = None,
    state: object | None = None,
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
    * **``POST /v1/chat/completions``** — no transcript the caller reads. For a
      kiro-backed turn the gate answers 503 before the turn starts; on backends
      it stands aside for, a turn that ends ``AcpAuthRequired`` is translated
      into the OpenAI ``authentication_error`` envelope (HTTP 401 blocking, an
      SSE ``error`` event once streaming has begun), so an SDK client always
      sees a real error rather than an empty 200.

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

    The latch governs ONLY a harness that signs in through kiro-cli
    (:func:`backend_signs_in_via_kiro_cli`). *signs_in_via_kiro_cli* is the
    caller's verdict for the session it acts on: a caller continuing a live
    session passes that session's own verdict
    (:func:`live_session_signs_in_via_kiro_cli`), and a caller acting for one
    session resolves ``backend_signs_in_via_kiro_cli(await selected_backend(key,
    state=state))`` itself, so a ``member-*`` session is judged by the backend
    the factory routes it to. Destructive reruns pass *session_key* and *state*
    instead at their mutation boundary, so this gate re-selects the installed
    factory backend there. The two poll-driven spawn sites pass ``True``: they
    spawn kiro-cli themselves whatever harness the sessions use, so the latch
    is about their own spawn.

    For a session outside the identity-store set this returns ``None`` without
    consulting the service: the reruns and ``/v1/chat/completions`` then start
    their turn on that harness, whose own ACP attempt is the authority for its
    sign-in, exactly as for an ordinary send.

    Every production caller passes *signs_in_via_kiro_cli* or *session_key* +
    *state* explicitly -- the bare call is for this gate's own unit tests only,
    and
    ``test_kiro_spawn_readiness_gate.test_every_production_caller_hands_the_gate_its_verdict_or_backend``
    fails on a call site in the package that omits all three.
    """

    if signs_in_via_kiro_cli is None:
        signs_in_via_kiro_cli = backend_signs_in_via_kiro_cli(
            await selected_backend(session_key, state=state)
        )
    if signs_in_via_kiro_cli:
        if await kiro_verified_ready(_service(request), max_age_secs=max_age_secs):
            _clear_refusal_warning()
            return None
        _warn_refused_once(_log_safe_path(request))
        return web.json_response(_KIRO_NOT_READY_RESPONSE, status=503)
    logger.debug(
        "%s: kiro-cli readiness gate not applicable to the selected backend",
        _log_safe_path(request),
    )
    return None
