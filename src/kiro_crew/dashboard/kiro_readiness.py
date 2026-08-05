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
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_KIRO_NOT_READY_RESPONSE = {
    "error": "Kiro CLI setup or sign-in is required before starting a session.",
    "code": "kiro_prerequisite_required",
}

# How stale a probe may be and still authorize a destructive or spawning call.
# Small enough that an external logout cannot linger behind this gate, large
# enough that a burst of callers collapses onto one probe.
_VERIFY_MAX_AGE_SECS = 30.0


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


def _service(request: web.Request) -> object:
    service = request.app.get("kiro_prerequisite_service")
    if service is None:
        service = getattr(request.app.get("state"), "kiro_prerequisite_service", None)
    return service


async def reject_if_kiro_unverified(request: web.Request) -> web.Response | None:
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
    stale latch must not block them (see the module docstring). A missing or
    invalid service fails closed here.

    These callers must not trust the latch in EITHER direction, so this uses
    :func:`kiro_verified_ready` — a stale ``ready=True`` is as dangerous as a
    stale ``ready=False`` here (it authorizes the history rewrite or the
    browser-opening spawn), and only these paths pay for the re-probe.
    """

    if await kiro_verified_ready(_service(request)):
        return None
    return web.json_response(_KIRO_NOT_READY_RESPONSE, status=503)
