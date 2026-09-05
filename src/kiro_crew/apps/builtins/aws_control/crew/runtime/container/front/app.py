"""The FastAPI application: ``build_app(settings)``.

Responsibilities, and only these:

* Strip the crew route prefix (an ALB forward action cannot rewrite the path, so
  ``/c/<crew>/...`` arrives here) and classify on the STRIPPED path. Classifying
  the prefixed path made control routes read as customer routes in an earlier
  build, so classification is done once, after stripping.
* Keep the customer surface (a single turn endpoint) separate from the owner's
  control surface. The split is fail-closed: only ``GET /health`` and the turn
  endpoint are customer routes; everything else is control and requires the
  control secret. A customer cannot relabel a control route as a customer one,
  because classification runs on the stripped path, and cannot forge the control
  secret, which they do not hold.
* Register a bare ``/health`` in addition to the prefixed one, because the load
  balancer's target-group health check hits the container directly, with no
  prefix.
* Ensure the addressed conversation's transcript is on disk before the turn
  reaches the backend. Boot restores no transcripts (``EPHEMERAL-CONTRACT.md``),
  so this is where a returning customer's conversation comes back. The lock and
  the fetch are one scope (``transcript.prepared_turn``) built at a single site,
  so the streamed and non-streamed transports cannot drift apart on it.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from container import common
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import backend, transcript
from .slotlock import SlotSerializer

logger = logging.getLogger("smc.front.app")

# Customer surface, evaluated on the stripped path.
CUSTOMER_TURN_PATH = "/v1/chat/completions"
HEALTH_PATH = "/health"

# The header the API Gateway control integration sets to the value of
# SMC_CONTROL_SECRET. Its name is an S1 choice, not pinned by the shared base;
# see the note in the track's report so the deploy integration matches it.
CONTROL_SECRET_HEADER = common.CONTROL_SECRET_HEADER

# Read timeout is disabled: a turn (and a stream) can legitimately run long. The
# connect timeout stays short so a dead backend fails fast rather than hanging.
_BACKEND_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)


def strip_prefix(path: str, prefix: str) -> str:
    """Remove the configured route prefix from an incoming path.

    A path not under the prefix (the bare ``/health`` the LB probes, chiefly) is
    returned unchanged so it can still be classified.
    """
    if not prefix:
        return path
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix) :]
    return path


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
    return hmac.compare_digest(provided, expected)


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
    """


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
    # process has no caller identity to bind to: ``AWS_IAM`` guards the CONTROL
    # plane, the ALB is ``Scheme: internal``, and nothing upstream passes an
    # identity through to here. A binding written against an absent identity
    # would fail OPEN, which is the worse of the two failure directions.
    #
    # The review's fix reads "bind forwarded IDs to the authenticated principal OR
    # enforce single-principal access". The second is what is implemented, twice
    # and deliberately outside this file, since a check here could only refuse
    # what it cannot identify:
    #   * ``templates/crew.yaml`` -- a CloudFormation ``Rule`` refuses the stack
    #     when ``Memory=persistent`` lacks ``TrustDomain=single-principal``, so a
    #     console or pipeline deploy cannot skip it.
    #   * ``scripts/smc-deploy.sh`` -- the same refusal at parse time, so an owner
    #     learns before step 0 rather than six steps in.
    # In the default ``chatbot`` mode no history is retained, so a reused id
    # resumes nothing regardless.
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
    named in ``container/CONTRACT.md`` (``build_app(settings)``) still holds.
    """
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
            if not _control_authorized(request, settings):
                return JSONResponse(
                    {"detail": "control access denied", "code": "control_forbidden"},
                    status_code=403,
                )
            # The gate is S1's invariant; the container front implements no
            # control operations (the owner's control plane is off-box).
            return JSONResponse(
                {
                    "detail": "control route not served by the front process",
                    "code": "control_not_implemented",
                },
                status_code=404,
            )

        try:
            payload = await request.json()
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
                {"detail": str(exc), "code": "bad_request"},
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
            # the turn would also overwrite that history in S3 at the next
            # backup cycle (the sidecar replaces whole objects).
            logger.error("turn refused: transcript unavailable", exc_info=True)
            return JSONResponse(
                {
                    "detail": "conversation is temporarily unavailable",
                    "code": transcript.TranscriptUnavailable.code,
                },
                status_code=503,
            )

    return app
