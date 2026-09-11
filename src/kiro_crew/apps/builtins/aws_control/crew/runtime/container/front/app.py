"""The FastAPI application: ``build_app(settings)``.

Responsibilities, and only these:

* Strip the optional crew route prefix and classify on the STRIPPED path. A
  deployment that addresses a crew by a path segment (``/c/<crew>/...``) cannot
  always rewrite the path before the task sees it, so the prefix arrives here;
  nothing in this repository sets one, and it is empty by default. Classifying the
  prefixed path made control routes read as customer routes in an earlier build, so
  classification is done once, after stripping.
* Keep the customer surface (a single turn endpoint) separate from the owner's
  control surface. The split is fail-closed: only ``GET /health`` and the turn
  endpoint are customer routes; everything else is control and requires the
  control secret. A customer cannot relabel a control route as a customer one,
  because classification runs on the stripped path, and cannot forge the control
  secret, which they do not hold.
* Register a bare ``/health`` in addition to the prefixed one, so liveness can be
  probed without knowing the crew's prefix. It returns ``{"status": "ok"}`` and no
  internals, because it is the one route that answers without the control secret.
  No consumer in this repository calls it -- the image deliberately declares no
  ``HEALTHCHECK`` (``Dockerfile``) -- so it stands or falls with whatever probes the
  task.
* Ensure the addressed conversation's transcript is on disk before the turn
  reaches the backend. Boot restores no transcripts at all, so this is where a
  returning caller's conversation comes back. The lock and
  the fetch are one scope (``transcript.prepared_turn``) built at a single site,
  so the streamed and non-streamed transports cannot drift apart on it.
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from container import common
from container.common import audit
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import backend, transcript
from .slotlock import SlotSerializer

#: Hard ceiling on a control request body. The control routes carry small JSON
#: objects -- ids, flags, a slot key -- so a megabyte is already far more than
#: any real caller sends, and the point of the number is that there IS one.
_MAX_CONTROL_BODY_BYTES = 1024 * 1024

logger = logging.getLogger("smc.front.app")

# Customer surface, evaluated on the stripped path.
CUSTOMER_TURN_PATH = "/v1/chat/completions"
HEALTH_PATH = "/health"

# The header the API Gateway control integration sets to the value of
# SMC_CONTROL_SECRET. ``common`` is the single definition and this is an alias for
# readability at the use sites below, so the deploy integration matches whatever
# ``common.CONTROL_SECRET_HEADER`` says rather than a name chosen here. An earlier comment
# here asserted the reverse, which was true while this line spelled the string out and
# stopped being true the moment it became an alias.
CONTROL_SECRET_HEADER = common.CONTROL_SECRET_HEADER

# Read timeout is disabled: a turn (and a stream) can legitimately run long. The
# connect timeout stays short so a dead backend fails fast rather than hanging.
_BACKEND_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)


def strip_prefix(path: str, prefix: str) -> str:
    """Remove the configured route prefix from an incoming path.

    A path not under the prefix (the bare ``/health``, chiefly) is returned
    unchanged so it can still be classified. Unchanged means classified as control
    unless it is one of the two customer routes, so an unmatched path is refused
    rather than admitted.
    """
    if not prefix:
        return path
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix) :]
    return path


def _audit_control_decision(
    request: Request, stripped_path: str, *, granted: bool, settings: common.Settings
) -> None:
    """Record BOTH outcomes of the control-authorization decision, before responding.

    Emitted for a grant as well as a deny. The deny is the half that gets forgotten and
    the half that answers "who tried", and a log holding only grants records exactly the
    events nobody needed to investigate.

    Before the response, not after, so a failure between the decision and the reply cannot
    produce an unaudited grant. And a failure to RECORD refuses the request rather than
    passing: see ``common.audit``, which mirrors what
    ``kiro_crew.sel.SecurityEventLog.log_tool_invocation(critical=True)`` exists for.
    Raises ``AuditUnavailable``; the caller turns that into a 503.
    """
    audit.emit(
        audit.control_decision_event(
            granted=granted,
            method=request.method,
            path=stripped_path,
            header_present=common.CONTROL_SECRET_HEADER in request.headers,
            crew_name=settings.crew_name,
        )
    )


def _control_authorized(request: Request, settings: common.Settings) -> bool:
    """True only if the request carries the exact control secret.

    Fails closed when no control secret is configured, and compares in constant
    time. A client-supplied header cannot pass without knowing the secret, and
    the secret never appears on the customer path.
    """
    expected = settings.control_secret
    if not expected:
        return False
    provided = request.headers.get(CONTROL_SECRET_HEADER)
    if provided is None:
        return False
    # Compared as BYTES. `hmac.compare_digest` on str raises TypeError for any non-ASCII
    # character -- measured: "ab\u00e9" raises "comparing strings with non-ASCII characters
    # is not supported" -- so a caller could turn a 403 into an unhandled 500 by putting one
    # accented letter in the header. Encoding first gives every possible header value a
    # verdict, and keeps the comparison constant-time for equal lengths.
    #
    # surrogatepass, because a header that arrived as lone surrogates must still compare
    # rather than raise on the way in.
    return hmac.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def judge_addressed_crew(
    payload: dict[str, Any], deployed_crew: str
) -> tuple[str | None, JSONResponse | None]:
    """Resolve which crew the request addresses, or refuse.

    ``model`` is the OpenAI-shaped field that names the crew. It is OPTIONAL in the
    schema and REQUIRED in effect: a request that names nobody is refused rather
    than answered by whatever agent the backend would otherwise pick.

    That distinction is the whole point. Kiro Crew maps ``model`` to an agent name
    (``dashboard/openai_compat.py:237``) and validates it against a NAME REGEX, not
    against the set of agents that exist, so an unrecognised name does not fail --
    it lands on the installed default. A deployment whose crew was never installed
    therefore answers every turn with a stock agent and looks entirely healthy, which
    is exactly what this deployment did before the crew moved into the image. A
    default answer to an unaddressed request is the failure, not the fallback.

    Forwarding the customer's string was also an authorization hole: any agent
    present in the container was reachable by naming it. The caller may only ADDRESS
    this deployment's crew; the value that reaches the backend is set from
    ``SMC_CREW_NAME`` rather than copied from the request.

    Returns ``(crew, None)`` when the request may proceed, or ``(None, response)``.
    """
    if not deployed_crew:
        # The supervisor refuses to boot without a crew, so this is unreachable in a
        # correct deployment. Fail closed anyway: the alternative is accepting any
        # name because we do not know our own.
        return None, JSONResponse(
            {"detail": "service unavailable", "code": "crew_not_configured"},
            status_code=503,
        )

    raw = payload.get("model")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, JSONResponse(
            {
                "detail": (
                    'no crew was addressed: set "model" to the crew you are '
                    "calling. This service does not answer on behalf of an "
                    "unnamed crew."
                ),
                "code": "crew_not_addressed",
            },
            status_code=400,
        )
    if not isinstance(raw, str):
        return None, JSONResponse(
            {"detail": '"model" must be a string naming a crew', "code": "bad_request"},
            status_code=400,
        )

    if raw.strip() != deployed_crew:
        # Naming a crew that is not here is a 404: the address is wrong, not the
        # request. Echoing the deployed name is deliberate -- it is not a secret
        # (it is in the URL prefix) and withholding it makes a typo unfixable.
        return None, JSONResponse(
            {
                "detail": (
                    f"this deployment serves the crew {deployed_crew!r}, not " f"{raw.strip()!r}"
                ),
                "code": "crew_not_served_here",
            },
            status_code=404,
        )

    return deployed_crew, None


class BadForwardField(ValueError):
    """A payload field cannot be reduced to its backend contract type.

    Carried out of :func:`_forward_body` and turned into a 400 by the handler, so
    the type check lives with the field it guards and is unit-testable without an
    ASGI round trip.

    ``detail`` is the ONLY text that reaches the caller, and every value assigned to
    it must be written for that audience. Returning ``str(exc)`` would not be,
    which works while there is one raise site with an authored message and stops
    working the first time someone writes
    ``raise BadForwardField(f"bad field: {inner_exc}")`` -- the message would then
    carry whatever the inner exception says about the container's internals, and no
    reviewer of that line would be looking at the response path. Naming the channel
    puts the decision at the raise site, where the author can see it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _require_stream_bool(raw: Any) -> bool:
    """``stream`` must be a real boolean; anything else is refused.

    ``bool("false")`` is ``True``, so silently coercing would turn a client that
    sent the STRING ``"false"`` -- meaning off -- into a streamed response. A
    client that says ``"false"`` and means it has misunderstood the field, and a
    silent coercion hides that, so we refuse rather than guess. Absent means the
    default (non-streamed), which is the documented shape of the field.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    raise BadForwardField(
        f'"stream" must be a boolean (true/false), not {type(raw).__name__}. '
        'It is not interpreted loosely because the string "false" is truthy, '
        "so a coercion would stream a turn the caller asked not to stream."
    )


def _forward_body(payload: dict[str, Any], crew: str) -> tuple[dict[str, Any], str, bool]:
    """Reduce a customer payload to exactly the backend's contract fields.

    Only ``model``, ``messages``, ``id`` and ``stream`` are forwarded; any other
    field a client sends is dropped. ``id`` is the slot id used for
    serialization; absent, the backend mints one and there is nothing to
    serialize on.

    ``model`` is set from the DEPLOYED crew name, never copied from the payload:
    the request has already been checked to address this crew, and re-using the
    caller's string would let a differently-cased or padded value reach the
    backend's agent resolver.
    """
    stream = _require_stream_bool(payload.get("stream"))
    body: dict[str, Any] = {"stream": stream}
    body["model"] = crew
    # The ``id`` is forwarded as the caller sent it (after the shape and type
    # checks below) and is NOT bound to an authenticated principal, because this
    # process has no caller identity to bind: reaching the task is an authorised
    # call in the owner's own account, decided before the request arrives, and
    # nothing passes the result of that decision through to here. A binding
    # written against an absent identity would fail OPEN, which is the worse of
    # the two failure directions.
    #
    # The review's fix reads "bind forwarded IDs to the authenticated principal OR
    # enforce single-principal access". The second is what is implemented, in three
    # places that cover three different ways the pairing can be reached:
    #   * ``build_app`` -- refuses to START unless the deployment has declared
    #     ``SMC_SINGLE_PRINCIPAL``, whether or not a backup bucket is configured. This is
    #     the one that holds for an image run by any path at all, including none of the
    #     two below.
    #   * the deploy track's CloudFormation template refuses the stack when persistent
    #     memory lacks a single-principal trust domain, so a console or pipeline deploy
    #     cannot skip it, and its driver refuses the same pairing at parse time so an
    #     owner learns before step 0 rather than six steps in.
    # That track is not in this repository, which is why the first one is the one that
    # matters here: it needs nothing outside this image to be true.
    # In the default ``chatbot`` mode no history is retained, so a reused id resumes
    # nothing regardless.
    for key in ("messages", "id"):
        if key in payload:
            body[key] = payload[key]

    # The slot id drives the on-demand transcript fetch and the per-slot lock. A
    # NON-STRING id must never become one. ``bool("false")``'s sibling here is
    # ``str(123) == "123"``: a folded integer is a perfectly legal SHAPE, so it
    # sails through ``is_fetchable_slot_id`` and gets fetched -- and only THEN
    # does the backend reject the integer id, leaving the task holding
    # ``dashboard_123``, a conversation it never served. That is the same
    # isolation hole the punctuation door (``dashboard:cust-1``) opened, entered
    # through the type door instead.
    #
    # We choose "fetch nothing" over "reject outright at the front". The raw
    # ``id`` is still forwarded above, so the backend remains the one authority
    # that judges legality (its grammar is not importable here, and duplicating
    # it is exactly what the shape-test guard was built to avoid). An empty
    # ``slot_id`` maps to ``FetchOutcome("no_slot")`` -- no key, no lock keyed on
    # a fabricated string. And the direction is the safe one: declining to fetch
    # for a non-string id costs nothing (a refused turn writes nothing), whereas
    # fetching for it would put another customer's transcript on this disk.
    raw_id = payload.get("id")
    slot_id = raw_id if isinstance(raw_id, str) else ""
    return body, slot_id, stream


def _get_client(app: FastAPI) -> httpx.AsyncClient:
    """Lazily create the backend client inside the running loop, once."""
    client = getattr(app.state, "backend_client", None)
    if client is None:
        # ``trust_env=False`` because every request this client makes goes to
        # ``http://127.0.0.1:<backend_port>`` (``Settings.backend_base_url``), and
        # ambient environment is wrong for a loopback call in both directions it
        # reads. Proxies: an ``HTTP_PROXY`` / ``ALL_PROXY`` inherited by the task
        # would route the front's internal turn traffic -- whole conversations --
        # through that proxy instead of to the process next door. Trust store:
        # httpx eagerly builds an SSL context from ``SSL_CERT_FILE`` even for a
        # client that never speaks TLS, so a stale value crashes construction with
        # FileNotFoundError (which is how CI found this). This is NOT
        # ``verify=False``: verification stays on, so pointing the backend at an
        # https URL later still validates its certificate.
        client = httpx.AsyncClient(timeout=_BACKEND_TIMEOUT, trust_env=False)
        app.state.backend_client = client
    return client


def _get_transcript_reader(
    app: FastAPI, settings: common.Settings
) -> transcript.TranscriptReader | None:
    """The read-only S3 reader for on-demand transcripts, built once, or None.

    None means no bucket is configured, which is a supported deployment: the
    crew runs without durable conversations. It is NOT treated as a failed fetch,
    because nothing was ever uploaded, so nothing is missing. The distinction is
    said out loud once at startup rather than per turn (see ``build_app``).

    Lazy, like the backend client: constructing a boto3 client at import or at
    build time would put AWS on the path of every test that builds the app.
    """
    if not settings.backup_bucket:
        return None
    reader = getattr(app.state, "transcript_reader", None)
    if reader is None:
        reader = transcript.S3TranscriptReader(settings.backup_bucket)
        app.state.transcript_reader = reader
    return reader


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    client = getattr(app.state, "backend_client", None)
    if client is not None:
        await client.aclose()


def build_app(
    settings: common.Settings,
    *,
    transcript_reader: transcript.TranscriptReader | None = None,
) -> FastAPI:
    """The front process's app.

    ``transcript_reader`` is the injection seam for the on-demand transcript
    fetch. Production leaves it None and gets a lazily built
    :class:`~container.front.transcript.S3TranscriptReader`; a test passes a fake
    with a ``get`` and no credentials. It is keyword-only so the public seam
    named in ``docs/system-specs/modules/aws-control.md`` (``build_app(settings)``) still holds.
    """
    # Before the app exists, so it cannot be missing for a request. Under uvicorn's own
    # logging configuration this process's module loggers resolve to WARNING with no
    # reachable handler, so an INFO audit call writes nothing and raises nothing -- the
    # control audit would be inert exactly in production, where it is the only record of
    # who reached the control surface. Configured here rather than in ``__main__`` because
    # every path that can serve a request goes through this function.
    audit.configure_sink()
    app = FastAPI(
        title="share-my-crew front",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.backend_client = None
    app.state.transcript_reader = transcript_reader
    serializer = SlotSerializer()

    # An unclaimed trust domain is refused HERE, at startup, and NOT conditioned on a
    # backup bucket.
    #
    # The bucket was the condition in the first version of this guard and it is the wrong
    # one. What a bucket decides is whether transcripts are DURABLE; what the caller's
    # ``id`` decides is which conversation this turn joins. Those are different questions.
    # With no bucket the fetch does nothing, but the id still selects a slot on the shared
    # serializer and the backend still returns the live session behind it, so a second
    # caller reusing a first caller's id lands in that conversation. Gating on the bucket
    # left exactly that case open, and a no-bucket multi-caller deployment is an ordinary
    # one rather than a corner.
    #
    # So the real condition is the one that is always true here: this route accepts a
    # caller-supplied id and this process cannot bind it to a principal. It has no caller
    # identity to bind -- reaching the task is an authorised call in the owner's own
    # account, decided before the request arrives, and its result is not passed through --
    # and a binding written against an absent identity fails OPEN. So it answers the
    # question it CAN answer, whether the deployment has vouched that only one principal
    # reaches this task, and declines to serve customer turns at all without that answer.
    #
    # Refusing at startup rather than per turn, for the reason ``require_api_key`` refuses
    # at startup: a container that answers its port while mixing two callers' conversations
    # looks healthy and is not.
    if not settings.single_principal:
        raise common.ConfigError(
            "the deployment has not declared a single-principal trust domain "
            "(SMC_SINGLE_PRINCIPAL). The customer turn route accepts the caller's own slot "
            "id and this process has no caller identity to bind it to, so one caller "
            "reusing another's id joins that conversation -- with a backup bucket by having "
            "the transcript restored here, and without one by landing on the live session "
            "the id already names. Set SMC_SINGLE_PRINCIPAL=1 if exactly one principal "
            "reaches this task. Refusing to start."
        )

    if not settings.backup_bucket:
        # Said once, here, rather than on every turn. A crew with no bucket has
        # no durable conversations at all: nothing is uploaded, so an absent
        # transcript is expected rather than a restore that failed.
        logger.warning(
            "no backup bucket configured: transcripts are never fetched, so a "
            "conversation does not survive a task replacement"
        )

    def health() -> JSONResponse:
        # No internals: a customer reaches this. Just liveness.
        return JSONResponse({"status": "ok"})

    @app.get(HEALTH_PATH)
    async def health_bare() -> JSONResponse:
        return health()

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def gateway(full_path: str, request: Request):
        stripped = strip_prefix("/" + full_path, settings.route_prefix)
        method = request.method

        if method == "GET" and stripped == HEALTH_PATH:
            return health()

        # Customer surface is a tiny allowlist; everything else is control.
        is_customer_turn = method == "POST" and stripped == CUSTOMER_TURN_PATH
        if not is_customer_turn:
            granted = _control_authorized(request, settings)
            try:
                _audit_control_decision(request, stripped, granted=granted, settings=settings)
            except audit.AuditUnavailable as exc:
                # The decision is not acted on either way. Serving the grant would be the
                # unaudited grant the record exists to prevent; serving the deny would be
                # a denial nobody can later account for. 503 rather than 403, because what
                # failed is this container's ability to record, not the caller's
                # authorisation -- and a caller told "forbidden" would keep retrying a
                # thing that is not their fault.
                logger.error("control request refused: %s", exc)
                return JSONResponse(
                    {
                        "detail": "control decision could not be audited",
                        "code": "control_audit_unavailable",
                    },
                    status_code=503,
                )
            if not granted:
                return JSONResponse(
                    {"detail": "control access denied", "code": "control_forbidden"},
                    status_code=403,
                )
            # Authorised, and there is nothing here to run yet: the front process
            # serves two customer routes and no control operation. The gate stays in
            # front of the 404 on purpose, so adding a control route later is adding a
            # handler rather than also remembering to authorise it -- and so an
            # unauthorised caller cannot learn which control paths exist by reading
            # which ones 404.
            return JSONResponse(
                {
                    "detail": "control route not served by the front process",
                    "code": "control_not_implemented",
                },
                status_code=404,
            )

        # Bounded while READING, not after. ``request.json()`` buffers the whole body
        # first, so a large upload is already resident by the time anything could reject
        # it -- and a Content-Length check alone is not the bound, because the header is
        # the client's claim about a body it has not sent yet. Counting the chunks is what
        # makes the ceiling real.
        # Named for what it holds, not ``body``: a ``body`` further down this function is
        # the decoded dict handed to ``_forward_body``, and reusing the name made mypy
        # report three type errors in the forwarding path rather than here.
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                raw_body += chunk
                if len(raw_body) > _MAX_CONTROL_BODY_BYTES:
                    return JSONResponse(
                        {
                            "detail": (f"request body exceeds {_MAX_CONTROL_BODY_BYTES} bytes"),
                            "code": "body_too_large",
                        },
                        status_code=413,
                    )
        except Exception:
            return JSONResponse(
                {"detail": "request body could not be read", "code": "bad_request"},
                status_code=400,
            )
        try:
            payload = json.loads(bytes(raw_body).decode("utf-8"))
        except Exception:
            return JSONResponse(
                {"detail": "request body must be JSON", "code": "bad_request"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "request body must be a JSON object", "code": "bad_request"},
                status_code=400,
            )

        crew, refusal = judge_addressed_crew(payload, settings.crew_name)
        if refusal is not None:
            return refusal
        # `judge_addressed_crew` returns (crew, None) or (None, response) -- a
        # correlation no signature can express, so a checker reads `crew` as
        # possibly None here. State the invariant rather than widen
        # `_forward_body`, whose `crew: str` is what stops the deployed crew
        # name from being replaced by the caller's string.
        assert crew is not None
        try:
            body, slot_id, stream = _forward_body(payload, crew)
        except BadForwardField as exc:
            return JSONResponse(
                {"detail": exc.detail, "code": "bad_request"},
                status_code=400,
            )
        client = _get_client(app)
        reader = _get_transcript_reader(app, settings)

        # ONE construction site for the turn scope, so the per-slot lock and the
        # on-demand transcript fetch cannot differ between the two transports.
        # The streamed turn enters it inside its generator (the slot stays busy
        # for the life of the stream); the non-streamed turn enters it here.
        scope = transcript.prepared_turn(serializer, settings, slot_id, reader)

        if stream:
            return backend.forward_stream(client, settings, body, scope)

        try:
            async with scope:
                return await backend.forward_completion(client, settings, body)
        except common.BackendSecretUnavailable:
            return JSONResponse(
                {"detail": "service unavailable", "code": "backend_unavailable"},
                status_code=503,
            )
        except transcript.TranscriptUnavailable:
            # Refuse rather than answer with an empty conversation. A customer
            # whose history looks forgotten is worse than an error, and serving
            # the turn would also overwrite that history in S3 once a durability
            # writer exists (it replaces whole objects).
            logger.error("turn refused: transcript unavailable", exc_info=True)
            return JSONResponse(
                {
                    "detail": "conversation is temporarily unavailable",
                    "code": transcript.TranscriptUnavailable.code,
                },
                status_code=503,
            )

    return app
