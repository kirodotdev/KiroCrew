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

Both halves describe a KIRO-CLI sign-in, so both apply only while the selected
harness authenticates through kiro-cli's identity store
(:func:`backend_signs_in_via_kiro_cli` over :func:`selected_backend`). On a
deployment whose
``agent.acp_backend`` is claude-agent-acp or codex-acp the latch describes a
binary the sessions never spawn: refusing on it locked every gated endpoint
behind a kiro-cli sign-in the operator had deliberately stopped needing.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from kiro_crew.acp_backends import backends_retired_by_host_logout
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

logger = logging.getLogger(__name__)

_KIRO_NOT_READY_RESPONSE = {
    "error": "Kiro CLI setup or sign-in is required before starting a session.",
    "code": "kiro_prerequisite_required",
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

# How stale a probe may be and still authorize a destructive or spawning call.
# Small enough that an external logout cannot linger behind this gate, large
# enough that a burst of callers collapses onto one probe.
_VERIFY_MAX_AGE_SECS = 30.0


async def selected_backend() -> str | None:
    """The configured ``agent.acp_backend``, or ``None`` when config is unreadable.

    Reads the SAME field the provider factory builds a session from (already
    normalized by ``resolve_selected_backend``), off the loop because
    ``KiroCrewConfig.load`` stats -- and on a cache miss re-reads --
    ``config.json``. The import is deferred: this module is imported by every
    gated handler, and ``kiro_crew.config`` is heavier than anything else it
    needs.

    Read ONCE per request and passed along. A handler that reads the backend for
    its own branch and then lets the gate read it again has two snapshots, and a
    ``PATCH agent.acp_backend`` landing between them lets the kiro-cli branch
    proceed on the first while the gate stands aside on the second -- one
    unauthenticated spawn, which is exactly the browser-opening event the gate
    exists to prevent.
    """

    from kiro_crew.config import KiroCrewConfig

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.warning(
            "Could not read agent.acp_backend; keeping the kiro-cli readiness gate",
            exc_info=True,
        )
        return None
    backend = getattr(getattr(cfg, "agent", None), "acp_backend", "")
    return backend if isinstance(backend, str) else None


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


def live_session_signs_in_via_kiro_cli(state: object, session_key: str) -> bool | None:
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
    caller then falls back to the configured backend, which is what a fresh
    session would get.
    """

    sessions = getattr(getattr(state, "sessions", None), "_sessions", None)
    if not isinstance(sessions, dict):
        return None
    declared = getattr(
        getattr(sessions.get(session_key), "provider", None), "uses_kiro_identity_store", None
    )
    return declared if isinstance(declared, bool) else None


async def kiro_session_ready(service: object) -> bool:
    """Return the service's latched readiness. Fails closed on a bad service."""

    if not isinstance(service, KiroPrerequisiteService):
        return False
    return await service.session_ready()


async def kiro_verified_ready(service: object) -> bool:
    """Return readiness backed by a probe that is FRESH ENOUGH to authorize on.

    The latch alone cannot authorize these callers. It is written at boot and
    narrowed only when a chat turn observes ``AcpAuthRequired``, so an external
    logout with no chat turn in between leaves it ``ready=True`` indefinitely —
    and every one of this gate's callers acts irreversibly on that answer
    (deletes history, or spawns a browser-opening ``kiro-cli``). "Probe at boot
    only" is the right rule for the send path, which risks nothing; it is the
    wrong rule for authorization.

    So this re-probes when the latch is older than
    ``_VERIFY_MAX_AGE_SECS``. That is bounded work — it happens only on a
    destructive rerun or a poll tick, never on the message hot path — and the
    service's own short cache collapses bursts (e.g. the three destructive
    routes, or several pollers firing together) into one probe.
    """

    if not isinstance(service, KiroPrerequisiteService):
        return False
    return await service.verified_ready(max_age_secs=_VERIFY_MAX_AGE_SECS)


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
    ``api_models`` sit BELOW this gate (``agents.py:1014`` onward vs the gate at
    ``:942``) and are never reached in that state, so one line per refused request is
    not "the same order of magnitude as its siblings" — it is 570 lines/hour against
    their none. The dashboard log ring is ``deque(maxlen=1000)``
    (``handlers/updates.py:1381``), which that rate churns end to end every ~1.8
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
    backend: str | None = None,
    signs_in_via_kiro_cli: bool | None = None,
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

    The latch governs ONLY a harness that signs in through kiro-cli
    (:func:`backend_signs_in_via_kiro_cli`). *backend* is the harness the caller
    is about to act on: a caller that already read the configured backend for
    its own branch passes that same snapshot so one request cannot authorize a
    spawn on two different configurations, and a caller that continues a live
    session passes that session's own verdict as *signs_in_via_kiro_cli*
    (:func:`live_session_signs_in_via_kiro_cli`), which survives a hot switch of
    the default and outranks *backend*. Left ``None``, the configured backend
    is read here. For a backend outside the identity-store set this returns
    ``None`` without consulting the service: the destructive
    reruns and ``/v1/chat/completions`` then start their turn on that harness,
    whose own ACP attempt is the authority for its sign-in, exactly as it is for
    an ordinary send. That leaves the ``/v1/chat/completions`` empty-200 case
    (an ``AcpAuthRequired`` card its collectors cannot see) open on those
    harnesses, which is the pre-existing gap the 503 papered over for kiro-cli
    alone — a permanent 503 on a working Claude Code install was the larger
    defect. The poll-driven spawn sites do NOT rely on this ``None``: they test
    the membership themselves and never resolve kiro-cli for a foreign harness,
    because the binary may still be installed and signed out on that host and the
    browser storm is what the spawn does, whatever the selected backend.
    """

    if signs_in_via_kiro_cli is None:
        if backend is None:
            backend = await selected_backend()
        signs_in_via_kiro_cli = backend_signs_in_via_kiro_cli(backend)
    if signs_in_via_kiro_cli:
        if await kiro_verified_ready(_service(request)):
            _clear_refusal_warning()
            return None
        _warn_refused_once(_log_safe_path(request))
        return web.json_response(_KIRO_NOT_READY_RESPONSE, status=503)
    logger.debug(
        "%s: kiro-cli readiness gate not applicable to the selected backend",
        _log_safe_path(request),
    )
    return None
