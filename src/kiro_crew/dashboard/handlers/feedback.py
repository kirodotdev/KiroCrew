"""Server-side proxy to AWS Aperture's non-console feedback APIs.

Kiro Crew is self-hosted, open-source software: every install runs on its own
arbitrary origin (``localhost:5476``, a self-hosted domain, whatever a user
picks). Aperture's non-console APIs are browser-CORS-gated and allowlist a
finite, known set of origins — a model that assumes one controlled domain, not
an unbounded set of installs. A direct browser->Aperture fetch is therefore
never reliably reachable for this product, regardless of which domain any one
install happens to use.

This module moves the two calls the in-app session-pulse survey needs
(``SessionPulseSurveyCard.tsx``) to the Kiro Crew backend instead: the frontend
calls these same-origin routes, and the backend makes the actual Aperture
request server-to-server, where browser CORS does not apply at all.

Both the Aperture endpoint and the form namespace (category/name/version) are
hardcoded here, never accepted from the request — the client only ever
supplies the answer content. Following the ``kiro_usage_api.py`` sibling's full
acceptance criteria, the two outbound calls also disable redirects
(``allow_redirects=False``) so the body and identity can never be replayed to a
redirected host, and read the response through a byte cap so a hostile or
misconfigured endpoint cannot OOM the gateway.

**Egress consent.** The survey sends survey answers plus a stable per-install
id to a third party (Aperture), so it is not a display feature — it is
telemetry, and it rides the SAME consent gate as the usage beacon
(``beacon.telemetry_permitted``). When that gate says no (``KIROCREW_TELEMETRY_DISABLED``,
an enterprise ``capabilities.telemetry`` policy, ``telemetry.beacon_enabled``
off, CI, a non-default data home, or the first-run privacy disclosure not yet
acknowledged) the survey never reaches Aperture: eligibility fails closed and
submit is refused. This keeps the beacon family the
repo's only default-on egress family (``governance.md``) rather than adding a
second one.

**Identity.** The per-respondent id sent to Aperture is ``beacon.install_id()``
— the codebase's designated anonymous egress identity (a random per-install
uuid, not derived from any owner id or secret). It is stable across every
browser on one install, so a person is not counted once per browser; it is
per-install, so two people sharing one install are counted as one, an accepted
tradeoff for a lightweight session-pulse signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import web

from kiro_crew import beacon
from kiro_crew import sel as _sel_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import read_bounded_json, read_capped_response
from kiro_crew.dashboard.session_pulse_counter import (
    NEW_USER_SESSION_THRESHOLD,
    get_user_session_count,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_INGESTION_URL = "https://ingestion.aperture-public-api.feedback.console.aws.dev/form"
_PROMPT_URL = "https://prompt.aperture-public-api.feedback.console.aws.dev/form/prompt"

_FORM_CATEGORY = "KiroCrew"  # brand-ok: literal Aperture portal identifier
_FORM_NAME = "SessionFeedback"
_FORM_VERSION = "1.0.1"
# serviceId/reference are console-navigation concepts (they normally mirror a
# console navId); Aperture's guidance for a non-console form is to use the
# team/product name instead.
_SERVICE_ID = "KiroCrew"  # brand-ok: literal Aperture portal identifier

_REQUEST_TIMEOUT_SECONDS = 10

# Cap on the Aperture response body we read, mirroring kiro_usage_api.py's
# `_MAX_RESP_BYTES`. With redirects disabled the destination is fixed, but a
# hostile or misconfigured endpoint could still stream an unbounded body; the
# cap bounds what one request can buffer on the event loop.
_MAX_RESP_BYTES = 1_000_000

# The exact set of `responseValue`s Aperture's registered form template accepts
# for the rating question — the wire values in
# `website/src/components/sessionPulseWireValues.ts`. Validated server-side so
# `rating` is a genuine fixed enum (as the Security Posture panel claims) rather
# than an unredacted free-text egress channel keyed on a different field name.
_RATING_VALUES = frozenset({"Very Poor", "Poor", "Fair", "Good", "Excellent"})

# Question text and PII flags copied verbatim from the actual registered
# template (GET rendering.../form/template for category=KiroCrew,  # brand-ok: registered category id
# name=SessionFeedback, version=1.0.1) — since ingestion 400s on any
# text/type mismatch against the form-template, not just a semantic one.
_RATING_QUESTION = "How would you rate your experience with KiroCrew today?"  # brand-ok: verbatim registered template text, ingestion 400s on mismatch
_FEEDBACK_QUESTION = "Do you have additional feedback about this experience?"
_EMAIL_QUESTION = (
    "We may want to contact you about your feedback. "
    "Share your email to join our research panel. "
)


def _session_key(request: web.Request) -> str:
    """The caller's session key (``X-Session-Key`` header), for the SEL trail."""
    return request.headers.get("X-Session-Key") or ""


def _audit_feedback_denial(request: web.Request, tool: str) -> None:
    """SEL-audit a denied feedback permission decision.

    A security allow/deny must leave a decision record. The dashboard-user gate
    denies app tokens before the telemetry ladder (which audits its own
    verdict via ``audit_tool``) is ever reached, so that denial needs its own
    entry. Fail-safe: an audit failure must never turn a 403 into a 500.
    """
    try:
        _sel_mod.sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome="denied",
        )
    except Exception:  # pragma: no cover - audit must never break a request
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


def _require_dashboard_user(request: web.Request, tool: str = "feedback") -> web.Response | None:
    """403 unless this is a real dashboard user's request, else ``None``.

    Deny-by-default on the app claim, matching the ``request["app"] == ""``
    convention used by ``deny_non_dashboard_caller`` / ``kiro_prerequisite`` /
    ``chat_handlers``: the auth middleware sets ``request["app"]`` on every
    authenticated path (``""`` for a dashboard user, the app name for an app
    token), so an ABSENT key means the middleware did not run and must be
    refused rather than fall through.

    Without this an installed app whose manifest ``permissions.api`` covers
    ``/api/feedback`` could be recorded as a human respondent. The survey is a
    dashboard-user surface only. A denial is SEL-audited (``tool`` names the
    endpoint) so the security decision leaves its required record.
    """
    if request.get("app") != "":
        _audit_feedback_denial(request, tool)
        return web.json_response({"code": "forbidden"}, status=403)
    return None


def _telemetry_permitted(audit_tool: str = "") -> bool:
    """Whether this install currently permits outbound telemetry.

    The survey egresses to a third party, so it rides the beacon's consent
    ladder rather than introducing a second default-on egress path. Reads the
    same two config inputs the beacon and ``kirocrew telemetry status`` use.
    Passing a non-empty ``audit_tool`` records the allow/deny verdict in the
    SEL trail. Callers run this off the event loop (``asyncio.to_thread``) since
    it does config-file I/O and writes the audit record.
    """
    cfg = KiroCrewConfig.load()
    return beacon.telemetry_permitted(
        enabled=cfg.telemetry.beacon_enabled,
        acked=cfg.dashboard.privacy_acked,
        audit_tool=audit_tool,
    ).ok


def _survey_identity() -> str:
    """The anonymous per-install id sent to Aperture as ``userId``.

    ``beacon.install_id()`` is the codebase's designated egress identity: a
    random per-install uuid, safe to transmit, not derived from any owner id or
    the token-signing secret. Stable across browsers on one install.
    """
    return beacon.install_id()


async def _read_capped_text(resp: aiohttp.ClientResponse) -> str:
    """Read at most ``_MAX_RESP_BYTES`` from *resp*, raising if it exceeds the cap.

    Streams to EOF via ``read_capped_response`` and treats an over-cap body
    as a failure rather than buffering it, so a hostile endpoint cannot OOM
    the single-threaded gateway.
    """
    raw = await read_capped_response(resp, _MAX_RESP_BYTES)
    if len(raw) > _MAX_RESP_BYTES:
        raise ValueError("aperture response body exceeded cap")
    return raw.decode("utf-8", "replace")


def _customer_responses(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Aperture's ``customerResponses`` shape from the survey's answers.

    Question text and response types must match what's registered for this
    form in the Aperture portal, or ingestion rejects the submission with a
    400 (form-template/response mismatch).
    """
    rating = str(body.get("rating") or "").strip()
    if not rating:
        raise ValueError("missing rating")
    # `rating` is documented (security_posture.py) as a fixed frontend enum that
    # is intentionally NOT run through the redaction pass. Enforce that here so
    # the claim is true server-side: an unrecognized value is refused rather
    # than egressed verbatim, closing the "put a secret in the one unredacted
    # field" channel.
    if rating not in _RATING_VALUES:
        raise ValueError("invalid rating")
    responses: list[dict[str, Any]] = [
        {
            "question": _RATING_QUESTION,
            "pii": False,
            "response": {"responseType": "radio", "responseValue": rating},
        }
    ]
    # `feedback` is free text the user types; unlike `rating` (a fixed enum
    # from the frontend), it can contain anything they choose to paste or
    # describe, including a credential or an exfiltration-style URL. Redact
    # both before this leaves the host, matching the standard order used
    # everywhere else in this codebase (security.py).
    raw_feedback = str(body.get("feedback") or "").strip()
    feedback, _ = redact_exfiltration_urls(raw_feedback)
    feedback, _ = redact_credentials(feedback)
    if feedback:
        responses.append(
            {
                "question": _FEEDBACK_QUESTION,
                "pii": False,
                "response": {"responseType": "textArea", "responseValue": feedback},
            }
        )
    # Like `feedback`, `email` is user-typed free text -- someone could paste a
    # credential into it instead of a real address -- so it gets the same
    # redaction pass before leaving the host. `pii: True` below is a Aperture
    # disclosure flag (this field may legitimately contain identity data); it
    # does not substitute for content-safety redaction.
    email = str(body.get("email") or "").strip()
    email, _ = redact_exfiltration_urls(email)
    email, _ = redact_credentials(email)
    if email:
        responses.append(
            {
                "question": _EMAIL_QUESTION,
                "pii": True,
                "response": {"responseType": "text", "responseValue": email},
            }
        )
    return responses


async def api_feedback_submit(request: web.Request) -> web.Response:
    """POST /api/feedback/submit — forward a session-pulse survey response to Aperture.

    Body: ``{rating, feedback?, email?, sessionId, kiroCrewVersion}``.
    Never blocks the caller on Aperture trouble — any failure (network, 4xx,
    5xx) is reported as a coded, non-2xx JSON body and the frontend already
    treats submission failures as non-fatal to the chat experience.
    """
    denied = _require_dashboard_user(request, "feedback_submit")
    if denied is not None:
        return denied
    # Egress consent gate: refuse rather than send when telemetry is not
    # permitted (opted out, governance-pinned, CI, non-default home, or the
    # privacy disclosure not yet acknowledged). Off the event loop: it reads
    # config and writes the SEL audit record.
    if not await asyncio.to_thread(_telemetry_permitted, "feedback_submit"):
        return web.json_response({"code": "telemetry_disabled"}, status=403)

    # Bounded read BEFORE decoding: without a cap, one authenticated POST of a
    # multi-megabyte body runs the super-linear redactors synchronously on the
    # event loop and freezes every WebSocket, turn and cron tick. The shared
    # helper enforces the cap on the raw stream and returns a 413/400 response
    # for the caller to pass straight through.
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    if body is None:  # pragma: no cover - read_bounded_json returns one or the other
        return web.json_response({"code": "invalid_body"}, status=400)

    try:
        customer_responses = _customer_responses(body)
    except ValueError:
        return web.json_response({"code": "missing_rating"}, status=400)

    # `sessionId` and `kiroCrewVersion` are client-supplied and land in
    # `metadataList` verbatim, so a credential-bearing custom slot key (or a
    # doctored version string) would otherwise egress to Aperture unredacted --
    # the one channel `rating` (enum-gated) and `feedback`/`email` (redacted
    # above) already close. Run both through the same redaction pass, in the
    # same order (security.py). `userId` below is the server-derived install id
    # (beacon.install_id()), never client input, so it is not redacted here.
    session_id = str(body.get("sessionId") or "")
    session_id, _ = redact_exfiltration_urls(session_id)
    session_id, _ = redact_credentials(session_id)
    kiro_crew_version = str(body.get("kiroCrewVersion") or "")
    kiro_crew_version, _ = redact_exfiltration_urls(kiro_crew_version)
    kiro_crew_version, _ = redact_credentials(kiro_crew_version)
    # Off the event loop: install_id() may create the id file and set
    # owner-only permissions (a subprocess on Windows) on first use.
    user_id = await asyncio.to_thread(_survey_identity)

    payload = {
        "category": _FORM_CATEGORY,
        "name": _FORM_NAME,
        "version": _FORM_VERSION,
        "locale": "en_US",
        "customerResponses": customer_responses,
        # Order matters here, not just key/value/pii content: Aperture's
        # ingestion API validates metadataList against the form template
        # positionally rather than as an unordered set — the identical set of
        # keys in a different order 400s with the same "mismatch" error a
        # genuinely wrong key would. This order was empirically confirmed
        # against the template returned by
        # GET rendering.../form/template?category=KiroCrew&name=SessionFeedback&version=1.0.1,  # brand-ok: registered category id
        # whose own metadataList lists userId, sessionId, kiro_crew_version in
        # that order.
        "metadataList": [
            {"key": "userId", "value": user_id, "pii": True},
            {"key": "sessionId", "value": session_id, "pii": False},
            {"key": "kiro_crew_version", "value": kiro_crew_version, "pii": False},
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _INGESTION_URL,
                json=payload,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if 200 <= resp.status < 300:
                    return web.json_response({"ok": True})
                error_body = await _read_capped_text(resp)
                logger.warning(
                    "aperture ingestion rejected submission: http %s: %s",
                    resp.status,
                    error_body[:500],
                )
                return web.json_response({"code": "aperture_rejected"}, status=502)
    except Exception:
        logger.warning("aperture ingestion request failed", exc_info=True)
        return web.json_response({"code": "aperture_unreachable"}, status=502)


async def api_feedback_eligible(request: web.Request) -> web.Response:
    """GET /api/feedback/eligible — ask Aperture if this install is due.

    Aperture tracks per-user prompt/cooldown state server-side. A null
    response body means "not eligible yet"; any JSON body means eligible. On
    any failure (network, non-2xx) this fails CLOSED — ``{"eligible": false}``
    — so an unreachable Aperture never surfaces the survey.
    """
    denied = _require_dashboard_user(request, "feedback_eligible")
    if denied is not None:
        return denied
    # Egress consent gate: an opted-out install must not even reach Aperture.
    # Off the event loop (config I/O + SEL audit record).
    if not await asyncio.to_thread(_telemetry_permitted, "feedback_eligible"):
        return web.json_response({"eligible": False})

    # New-user window: never surface the survey until this install has started
    # at least NEW_USER_SESSION_THRESHOLD genuine user chats. The count is kept
    # durably server-side (session_pulse_counter), so a fresh install fails
    # closed here. Off the event loop: it reads a small state file.
    if await asyncio.to_thread(get_user_session_count) < NEW_USER_SESSION_THRESHOLD:
        return web.json_response({"eligible": False})

    # Off the event loop: install_id() may create/permission the id file.
    user_id = await asyncio.to_thread(_survey_identity)
    if not user_id:
        return web.json_response({"eligible": False})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _PROMPT_URL,
                headers={
                    "userid": user_id,
                    "category": _FORM_CATEGORY,
                    "name": _FORM_NAME,
                    "version": _FORM_VERSION,
                    "serviceid": _SERVICE_ID,
                    "content-type": "application/json",
                    "locale": "en_US",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.info("aperture prompt check failed: http %s", resp.status)
                    return web.json_response({"eligible": False})
                raw = await _read_capped_text(resp)
                data = json.loads(raw) if raw.strip() else None
                return web.json_response({"eligible": data is not None})
    except Exception:
        logger.warning("aperture prompt request failed", exc_info=True)
        return web.json_response({"eligible": False})


def setup_feedback_routes(app: web.Application) -> None:
    """Register the session-pulse survey's Aperture proxy routes."""
    app.router.add_post("/api/feedback/submit", api_feedback_submit)
    app.router.add_get("/api/feedback/eligible", api_feedback_eligible)
