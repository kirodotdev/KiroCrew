"""Who may use and change speech-to-text, and the audit trail of those decisions.

The machine-readable error codes the STT endpoints answer with, the detached tasks a
model download or prewarm runs as, the app-token refusal, the custom-model refusals
and the SEL records each refusal or disclosure writes.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.stt import models as stt_models

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


#: Machine-readable reasons on the STT endpoints' non-2xx bodies. The dashboard
#: renders localised text and cannot key off an English sentence, so the prose is
#: advisory and these are the contract. Codes the stt package already owns
#: (``stt_extra_missing``, ``stt_model_missing``, …) are forwarded unchanged.
_CODE_DASHBOARD_USER_REQUIRED = "dashboard_user_required"


_CODE_STT_UNAVAILABLE = "stt_unavailable"


_CODE_STT_MISSING_AUDIO = "stt_missing_audio_field"


_CODE_STT_AUDIO_TOO_LARGE = "stt_audio_too_large"


_CODE_STT_FAILED = "stt_transcription_failed"


#: A decoder fetch asked of a desktop release. Its own payload is the only decoder
#: it will run, so the remedy is reinstalling the app, not a download.
_CODE_STT_DECODER_BUNDLED = "stt_decoder_bundled"


#: Background model-download and prewarm tasks, held ONLY so the loop keeps a
#: strong reference: a task nobody references can be collected mid-await. Both
#: endpoints answer 202 and let the caller poll ``GET /api/stt/status``, because
#: the whole point of the pair is that a 148 MB fetch is not on the request the
#: user is waiting behind.
_stt_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_stt_background(coro: Coroutine[Any, Any, Any]) -> None:
    """Run *coro* detached, keeping a reference until it finishes."""
    task = asyncio.create_task(coro)
    _stt_background_tasks.add(task)
    task.add_done_callback(_stt_background_tasks.discard)


def _audit_stt_refusal(request: web.Request, operation: str, error: str) -> None:
    """Record a refused STT capability in SEL. Best-effort, never raises.

    An unwrapped SEL failure here would replace the intended 403 with a 500, which
    is the one outcome a refusal must never turn into.

    A direct enqueue, not a ``to_thread`` hop: the SEL singleton is warmed at
    startup (:func:`kiro_crew.sel.warm_sel_singleton`, awaited by both start
    paths), so the first-touch construction that would otherwise justify offloading
    this -- reading the HMAC key, the chain tip and the live-file identity from disk --
    never runs on a request. Ten handler sites already record their decision this
    way; ``members.py`` states the same reasoning. Still guarded, because a FAILED
    warm leaves that construction to retry on this thread and possibly raise, and
    an audit must never change the outcome it is recording.
    """
    try:
        _sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


def _audit_custom_model_disclosure(
    request: web.Request, cfg: KiroCrewConfig, *, served: bool
) -> None:
    """Record whether the custom-model pair was served to this caller. Never raises.

    The write path audits only its REFUSAL (:func:`_deny_non_owner_stt_custom_model`),
    which left the read side of the same authorization decision unrecorded: the same
    owner predicate decides whether ``GET /api/config/stt`` serves or withholds
    ``custom_model_path`` / ``custom_model_sha256``, and neither branch wrote an event.
    So a silent gap in owner-only custom-model access could not be reconstructed from
    the log afterwards -- a served pair is the exact value a caller would need to echo
    back on the write it may not make. Both outcomes are recorded, not just the denial:
    "nobody was served this" is only provable if the allowed branch logs too.

    Only when a pair is IN PLAY. With no custom file, no digest and a catalog model in
    force, the branch discloses two empty strings and there is no decision to audit --
    while this endpoint is fetched by the chat composer's voice hook
    (``useComposerVoice``) on mount, not just by the settings page, so an
    unconditional row per GET would bury the decisions that do carry a value.

    Best-effort like every other audit on this endpoint (``critical=False``): an
    unwrapped SEL failure here would turn a served config into a 500, and an audit
    must never change the outcome it is recording.
    """
    if not (cfg.stt.custom_model_path or cfg.stt.custom_model_sha256):
        if cfg.stt.model != stt_models.CUSTOM_MODEL:
            return
    try:
        _sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation="stt.custom_model.read",
            outcome="allowed" if served else "denied",
            source="dashboard",
            resources=request.path,
            error="" if served else "non-owner identity, pair withheld",
        )
    except Exception:
        logger.warning("SEL logging failed for stt.custom_model.read", exc_info=True)


def _audit_stt_custom_model_write_allowed(request: web.Request) -> None:
    """Record that the owner gate ADMITTED a custom-model write. Never raises.

    The gate below logged only its refusal, so the log answered "who was turned
    away" and not "who chose the weights". Those are not the same question, and only
    the second one names the file and digest the gateway went on to verify and
    load. A denial-only record also cannot distinguish an endpoint nobody exercised
    from one exercised successfully, which is the distinction an operator needs
    after the fact: the admitted write is the event with consequences.

    Unconditional, unlike :func:`_audit_custom_model_disclosure`'s "only when a pair
    is in play" gate. That gate exists because ``GET /api/config/stt`` is fetched on
    every chat mount by ``useComposerVoice``, so most reads carry no decision. This
    is the WRITE, reached only by a body that names ``custom`` or one of the two
    fields -- the presence test above already made it rare and deliberate.

    Same operation name and same best-effort contract as the refusal it pairs with,
    so both outcomes of one authorization decision read as one operation in SEL.
    """
    try:
        _sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation="stt.custom_model",
            outcome="allowed",
            source="dashboard",
            resources=request.path,
            error="",
        )
    except Exception:
        logger.warning("SEL logging failed for stt.custom_model", exc_info=True)


def _refuse_app_token(request: web.Request, operation: str) -> web.Response:
    """The audited 403 an app token gets on a dashboard-only capability."""
    _audit_stt_refusal(request, operation, "dashboard user required")
    return web.json_response(
        {"error": "dashboard user required", "code": _CODE_DASHBOARD_USER_REQUIRED},
        status=403,
    )


def _deny_app_token(request: web.Request, operation: str) -> web.Response | None:
    """Refuse an app token on the dashboard-only STT endpoints. 403 or None.

    These endpoints start a model download and warm a resident model inside the
    gateway, which is operator setup rather than something an app earns by naming
    a path. The live transcription surfaces (``/api/ws/stt``, ``POST
    /api/stt/transcribe``) are deliberately NOT gated this way: shipped apps reach
    them on an app token.

    Whole-handler. The per-FIELD sibling is
    :func:`_deny_non_owner_stt_custom_model`, for the config endpoint that an app
    may legitimately reach for everything except the custom model.

    An absent ``app`` key is refused along with a non-empty one, so an
    unauthenticated route can only ever fail closed here.
    """
    if request.get("app") == "":
        return None
    return _refuse_app_token(request, operation)


async def _deny_non_owner_stt_custom_model(request: web.Request, body: dict) -> web.Response | None:
    """Refuse a non-owner caller the custom-model fields of ``PUT /api/config/stt``.

    ``PUT /api/config/stt`` is app-reachable as a whole, so this is per-field
    rather than the blanket :func:`_deny_app_token` that ``stt.status``,
    ``stt.prepare`` and ``stt.prewarm`` use. Those three are denied because they
    fetch weights and load them into the gateway. The custom-model pair reaches
    exactly that outcome by a longer road: it names a file the gateway will verify
    against a digest THE SAME CALLER supplied, and load — and the transcription
    surfaces that trigger the load are deliberately app-reachable. So a caller
    allowed to write these fields would choose the weights every later utterance is
    transcribed through. That is a hole through the side door, and it is the whole
    reason this exists.

    The bar is the OWNER dashboard (``is_owner_dashboard_request``), not merely a
    caller holding a non-app token. An allow-listed messaging user who runs
    ``!dashboard`` is minted a dashboard session too, and its app claim is ``""``
    just like the operator's: "not an app" and "the operator" are different
    questions, and only the second one may choose the weights. Same predicate, and
    same denial tail, as every other owner-only mutation in the dashboard.

    Scope, and why each half is drawn where it is:

    - ``custom_model_path`` / ``custom_model_sha256`` are refused on PRESENCE.
      The GET omits them for everyone but the owner, so no other caller has a
      legitimate source for either value and none can be round-tripping one it
      was served.
    - ``model`` is refused on PRESENCE of ``custom`` too, unconditionally, NOT
      only when it would change the selection. An earlier version compared
      against ``cfg.stt.model`` to let a caller harmlessly write back a ``custom``
      already in force. That comparison cannot be made safely here: ``cfg`` is
      read by the caller BEFORE the shared config lock, so a non-owner who reads
      "custom is already active" and then writes inside the lock re-activates a
      custom model the owner had just turned off. The round-trip that carve-out
      protected is a convenience; the race it opened is an authorization bypass,
      so the convenience loses.

    403 rather than dropping the fields quietly, because a 200 would tell the
    caller its custom model had been stored.
    """
    # Non-dict bodies never reach here: the caller 400s them before this gate, so
    # every membership test below is well-defined. Presence is checked FIRST so an
    # ordinary write (language, toggles) is untouched for every caller this gate
    # does not concern.
    if not (
        body.get("model") == stt_models.CUSTOM_MODEL
        or "custom_model_path" in body
        or "custom_model_sha256" in body
    ):
        return None
    # Imported in the body, as `require_owner_dashboard_request` does: importing
    # `source_providers` at module scope pulls chat-state helpers that reach back
    # into sibling handler modules.
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if is_owner_dashboard_request(request):
        _audit_stt_custom_model_write_allowed(request)
        return None
    # Domain audit stays here under this endpoint's own operation name; only the
    # denial TAIL is shared, so a session that predates the configured owner gets
    # the standard 401 relabel instead of a 403 it cannot act on.
    _audit_stt_refusal(request, "stt.custom_model", "non-owner identity rejected")
    return _owner_denial_response(request, "the custom speech model is owner-only")


def _stt_custom_model_refusal(body: dict) -> web.Response | None:
    """A 400 for a custom-model field that may not be stored, else ``None``.

    Refused rather than skipped, because a skipped field answers 200 and the row
    the user just typed into resets from the response, with nothing to say why.
    ``""`` is always accepted: it clears the field, which is how a user goes back to
    a catalog model.

    The path is checked against the disk here -- inside the models directory, no
    ``..`` and no symlink out of it, present, a regular file -- with the same
    function every load runs, so a value this endpoint stores is one a load will
    accept until the file itself changes. Blocking I/O; the caller runs it off the
    loop.
    """
    if "custom_model_path" in body and body["custom_model_path"] != "":
        try:
            stt_models.custom_model_file(body["custom_model_path"])
        except stt_models.CustomModelPathError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    if "custom_model_sha256" in body and body["custom_model_sha256"] != "":
        if not stt_models.valid_custom_sha256(body["custom_model_sha256"]):
            return web.json_response(
                {
                    "error": "The SHA-256 must be 64 hexadecimal characters.",
                    "code": "custom_model_sha256_invalid",
                },
                status=400,
            )
    return None


def _stt_positive_int(body: dict, key: str, *, minimum: int, maximum: int) -> int | None:
    """*body*'s value for *key* when it is an int inside the range, else None.

    Both ends are required rather than defaulted, because a knob accepted here and
    then clamped by the config loader is worse than one refused: the value the user
    reads back would not be the value in force.

    ``bool`` is excluded explicitly because it subclasses ``int``: without the
    check a client sending a checkbox value would persist ``True`` as ``1``.
    """
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if minimum <= value <= maximum else None
