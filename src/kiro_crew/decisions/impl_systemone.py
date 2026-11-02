"""Decision questions over System One HTTP, using https://docs.typesafe.ai/api.

The shipped hosted service is Jev by TypeSafe (:data:`HOSTED_TYPESAFE`); the
module was ``impl_jev`` before the provider registry, and that name and the
``Jev*`` class names remain as aliases for one release.

Each question type maps to the provider's own: a ``Choice`` sends a ``criteria``
map keyed by every option, each value its optional rubric text or ``null`` when
none is authored, a ``Noul`` sends its optional yes/no rubric as
``criteria.true`` / ``criteria.false``, and a ``Score`` sends its levels as the
ordered ``criteria`` array. Every answer must carry the type of the question
it answers and that type's own fields, finite and in range; anything else is a
protocol error. The gate validates answer domains again before anything is
consumed. Transport and protocol failures raise; the gate supplies fallback, not
retries.

The same client serves Cloudflare's Clef decision model on Workers AI, which speaks
this format inside a ``result`` envelope that ``_from_wire`` unwraps.

The same client serves a local System One server (``decisions/local_models.py``):
an endpoint on a literal loopback address is sent no credential at all.

:class:`SystemOneProvider` is the ``decisions.provider.DecisionProvider`` behind both
endpoint-lane registry entries (``systemone-hosted`` and ``systemone-local``);
the gate never names it, it builds whatever ``provider.select`` returns.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.decisions.local_models import active_id, is_loopback_endpoint
from kiro_crew.decisions.types import (
    IMAGE_FORMATS,
    IMAGE_MAX_BYTES,
    SCORE_MAX_LEVELS,
    SCORE_MIN_LEVELS,
    Answer,
    Answers,
    Box,
    Choice,
    ImageAsset,
    Noul,
    Question,
    Score,
    is_model_id,
)

logger = logging.getLogger(__name__)

#: Hand-written loopback addresses already warned about, so the withheld key is
#: said once per address rather than once per decision. Keyed on scheme, host and
#: port -- not the raw string, which a config writer could vary without end -- and
#: bounded, so no sequence of writes can grow it past a handful of entries.
_keyless_loopback_warned: set[str] = set()
_KEYLESS_WARNED_MAX = 32


def _warn_keyless_custom_loopback(endpoint: str, model: str) -> None:
    """Say once that a hand-written loopback address is sent no Jev key.

    A preset is a local model server and needs none. An address the owner wrote by
    hand may be a tunnel to hosted Jev, which then answers 401 and the decisions
    quietly stop; this line is what names the cause.
    """
    if active_id(endpoint, model) != "custom":
        return
    parts = urlsplit(endpoint.strip())
    key = f"{parts.scheme}://{parts.hostname}:{parts.port}"
    if key in _keyless_loopback_warned:
        return
    if len(_keyless_loopback_warned) >= _KEYLESS_WARNED_MAX:
        _keyless_loopback_warned.clear()
    _keyless_loopback_warned.add(key)
    logger.warning(
        "decisions: no Jev API key is sent to a loopback endpoint; a local proxy to "
        "hosted Jev must add the credential itself"
    )


# Bound the complete body, including chunked responses, before JSON parsing.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class SystemOneProfile:
    """The vendor facts of one hosted System One service, kept out of the client logic.

    *vault_secret* is the ONE vault entry the client reads for that service (see
    :data:`VAULT_SECRET_NAME`); *request_id_header* is the response header support
    asks for.
    """

    name: str
    endpoint: str
    model: str
    vault_secret: str
    request_id_header: str
    #: The opt-in header the service requires on a request carrying an image or a
    #: Box question.
    images_header: str = ""


#: Jev by TypeSafe, the shipped hosted service. Values unchanged from before the
#: profile existed; the module constants below are read from it.
HOSTED_TYPESAFE = SystemOneProfile(
    name="jev",
    endpoint="https://api.typesafe.ai/v1/systemone",
    model=DECISION_PROVIDER_MODEL_DEFAULT,
    vault_secret="TYPESAFE_API_KEY",
    request_id_header="x-typesafe-request-id",
    images_header="TypeSafe-Danger-Zone-Images",
)

_DEFAULT_ENDPOINT = HOSTED_TYPESAFE.endpoint
_DEFAULT_MODEL = HOSTED_TYPESAFE.model
_SECRET_PREFIX = "secret://"
#: The response header carrying the provider's request id. Support asks for this
#: value; the vendor documents it under this name.
_REQUEST_ID_HEADER = HOSTED_TYPESAFE.request_id_header
#: Longest ``model``/``request_id`` string kept on the metadata. Both are
#: provider-stamped, so this is far past any real value; it is the bound that
#: keeps a misbehaving provider from writing an unbounded field onto the row.
_META_STR_MAX = 128
#: The ONE vault entry this module will read. ``provider.api_key`` lives in the
#: agent-writable ``config.json``, so a reference that named any entry would let
#: a prompt-injected shell pick which of the operator's secrets is sent as the
#: bearer token. Only the dedicated Jev entry resolves; any other name is "no key".
VAULT_SECRET_NAME = HOSTED_TYPESAFE.vault_secret


# One persistent HTTP client per event loop, shared by every oracle. ``decide``
# builds a fresh ``SystemOneProvider`` for each decision, so a client owned by the
# oracle would be a connection per request: a hosted provider then pays its full
# TCP and TLS handshake on every decision (over a second to Cloudflare's API,
# against tens of milliseconds of inference), which is longer than the default
# ``timeout_ms``. The client speaks HTTP/2 where the server offers it (ALPN), so
# several decisions in flight ride ONE connection as separate streams instead of
# queueing for a pool slot; a plain-HTTP or HTTP/1.1-only server gets keep-alive
# HTTP/1.1. The pool is keyed by loop because the client's connections are bound
# to the loop they were opened on; a loop that has closed takes its client with it
# (see :func:`_session_for_running_loop`).
#
# Nothing per-endpoint or per-request lives on the client: the ``Authorization``
# header, the URL and the timeout are all passed on each request, and connections
# pool by origin, so a changed ``provider.endpoint`` simply opens a new connection
# and the old one idles out after ``_KEEPALIVE_SECS``. A credential is therefore
# never attached to a connection, only to the request sent over it.
_KEEPALIVE_SECS = 15.0
_POOL_LIMIT = 16
_sessions: dict[asyncio.AbstractEventLoop, Any] = {}
_sessions_lock = threading.Lock()


def _refuse_all_cookies() -> Any:
    """A cookie jar that stores nothing: a ``Set-Cookie`` is never replayed."""
    from http.cookiejar import CookieJar, DefaultCookiePolicy

    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _session_for_running_loop() -> Any:
    """The shared persistent client for the running loop, created on first use.

    Recreated when it was closed, and never shared across loops. Entries whose loop
    has closed are dropped here: no running loop can close their client, and the
    sockets go with the loop.

    The cookie jar refuses every cookie on purpose: a client that outlives a
    request would otherwise replay any ``Set-Cookie`` the provider (or something in
    front of it) sent on every later decision. ``trust_env`` is off so no proxy
    variable and no ``.netrc`` credential is read from the environment, and TLS
    verification uses the process's default context (the system trust store the
    gateway installs), not a bundled CA file.
    """
    import ssl

    import httpx

    loop = asyncio.get_running_loop()
    with _sessions_lock:
        for dead in [lp for lp in _sessions if lp.is_closed()]:
            _sessions.pop(dead)
        session = _sessions.get(loop)
        if session is None or session.is_closed:
            session = httpx.AsyncClient(
                http2=True,
                verify=ssl.create_default_context(),
                trust_env=False,
                follow_redirects=False,
                cookies=_refuse_all_cookies(),
                limits=httpx.Limits(
                    max_connections=_POOL_LIMIT,
                    max_keepalive_connections=_POOL_LIMIT,
                    keepalive_expiry=_KEEPALIVE_SECS,
                ),
            )
            _sessions[loop] = session
        return session


async def close_sessions() -> None:
    """Close every shared client; the gateway's shutdown hook calls this.

    The running loop's client is closed in place. A client on another loop that is
    still running is closed on that loop without waiting for it, and one on a
    closed loop is only dropped. Safe to call with nothing open, and a later
    ``ask`` simply makes a new client.
    """
    running = asyncio.get_running_loop()
    with _sessions_lock:
        held = list(_sessions.items())
        _sessions.clear()
    for loop, session in held:
        if loop is running:
            with contextlib.suppress(Exception):
                await session.aclose()
        elif not loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(session.aclose(), loop)


class ProviderProtocolError(RuntimeError):
    """The provider answered, but not in a shape this module can read."""


class ProviderHttpError(RuntimeError):
    """The provider answered with a non-2xx status."""


def resolve_api_key(raw: str) -> str:
    """Resolve the ONE vault reference this module honours; nothing else is a key.

    ``provider.api_key`` lives in the agent-writable ``config.json``, so its value
    is never itself a credential: a literal there would let a prompt-injected
    shell write any secret it can read -- an operator's credential from the
    environment, another provider's key -- into the bearer header of an external
    request. Only ``secret://TYPESAFE_API_KEY`` resolves, and only from the
    dashboard secrets vault, which the agent cannot read. An absent value, a
    literal, a foreign name or a vault failure all return empty; the caller
    refuses the request.

    WHERE the key goes is not decided here: the gate sends only while consent
    (``decisions.consent.permits``) holds for the configured endpoint, which the
    owner reviewed and recorded, so a redirected ``provider.endpoint`` is a
    refusal before this function is reached.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith(_SECRET_PREFIX):
        # A constant message, no argument: the value is agent-controlled text and
        # may itself be a secret, so nothing from it -- not even its length -- is
        # logged. The fix it names is the vault entry, spelled out in the help text
        # of the setting (``config.sections.DecisionProviderConfig.api_key``).
        logger.warning(
            "decisions: provider.api_key holds a literal value, which is not used; "
            "reference the dashboard vault entry instead (see the setting's help text)"
        )
        return ""
    name = value[len(_SECRET_PREFIX) :].strip()
    if name != VAULT_SECRET_NAME:
        if name:
            # The class of the refusal only; the requested name is agent-controlled
            # text and does not belong in a log line.
            logger.warning("decisions: a vault reference named an entry this seam does not read")
        return ""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    try:
        secret = SecretVault(config_dir()).get(VAULT_SECRET_NAME)
        return secret.reveal() if secret is not None else ""
    except Exception as exc:
        # Exception class only: neither vault paths nor entry names belong here.
        logger.warning("decisions: vault lookup for the provider failed: %s", type(exc).__name__)
        return ""


def _to_wire(
    state: dict | str,
    model: str,
    questions: list[Question],
    assets: Mapping[str, ImageAsset] | None = None,
) -> dict[str, Any]:
    """Map each question to the provider's wire shape for its type.

    The model id is checked here as well as by the gate's scrub: this is the one
    function that puts it on the wire, so the bound holds for any caller. Each
    image becomes ``assets[<id>] = {type: image, format, data_base64}``; the state
    names it by that id.
    """
    if not is_model_id(model):
        raise ProviderProtocolError("provider.model is not a model id")
    wire_questions: dict[str, Any] = {}
    for q in questions:
        wire_questions[q.id] = _question_to_wire(q)
    body: dict[str, Any] = {"state": state, "model": model, "questions": wire_questions}
    if assets:
        body["assets"] = {asset_id: _asset_to_wire(asset) for asset_id, asset in assets.items()}
    return body


def _asset_to_wire(asset: object) -> dict[str, Any]:
    """One image in the provider's shape, or raise for anything the service refuses."""
    if not isinstance(asset, ImageAsset) or asset.format not in IMAGE_FORMATS:
        raise ProviderProtocolError("unsupported image asset")
    if not asset.data or len(asset.data) > IMAGE_MAX_BYTES:
        raise ProviderProtocolError("image asset is empty or too large")
    return {
        "type": "image",
        "format": asset.format,
        "data_base64": base64.b64encode(asset.data).decode("ascii"),
    }


def _question_to_wire(q: object) -> dict[str, Any]:
    """One question in the provider's shape, or raise for a class it has none for."""
    if isinstance(q, Choice):
        # The map stays keyed by EVERY option, as the provider reads it: an option
        # with no authored rubric carries ``None`` -- today's shape -- so a point
        # that supplies nothing sends exactly what it sent before. A ``criteria``
        # entry for a name that is not a declared option is ignored, never sent,
        # so a stale key cannot smuggle text past the option domain.
        criteria = q.criteria or {}
        return {
            "type": "choice",
            "instructions": q.prompt,
            "criteria": {opt: criteria.get(opt) for opt in q.options},
        }
    if isinstance(q, Noul):
        wire: dict[str, Any] = {"type": "noul", "instructions": q.prompt}
        criteria = {
            key: text for key, text in (("true", q.true_means), ("false", q.false_means)) if text
        }
        if criteria:
            wire["criteria"] = criteria
        return wire
    if isinstance(q, Score):
        if not SCORE_MIN_LEVELS <= len(q.levels) <= SCORE_MAX_LEVELS:
            raise ProviderProtocolError("score question has an unsupported number of levels")
        return {"type": "score", "instructions": q.prompt, "criteria": list(q.levels)}
    if isinstance(q, Box):
        return {"type": "box", "instructions": q.prompt}
    raise ProviderProtocolError("unsupported question type")


def _from_wire(body: Any, questions: list[Question]) -> Answers:
    """Return every requested answer or raise; partial results are failures."""
    if not isinstance(body, dict):
        raise ProviderProtocolError("response is not an object")
    raw_answers = _response_body(body).get("answers")
    if not isinstance(raw_answers, dict):
        raise ProviderProtocolError("response has no 'answers' object")
    answers: Answers = {}
    for q in questions:
        raw = raw_answers.get(q.id)
        if not isinstance(raw, dict):
            raise ProviderProtocolError("no answer for question")
        answers[q.id] = _answer_from_wire(q, raw)
    return answers


def _response_body(body: dict) -> dict:
    """The object supplying answers and metadata, with the ordinary shape first.

    Cloudflare wraps the System One response in ``result``. A top-level answers
    object wins even beside a result sibling; a malformed top-level answers field
    allows the wrapped shape. Failure envelopes with ``result: null`` remain
    unreadable, and no provider error text is inspected.
    """
    if isinstance(body.get("answers"), dict):
        return body
    result = body.get("result")
    return result if isinstance(result, dict) else body


def _answer_from_wire(q: Question, raw: dict) -> Answer:
    """Validate wire fields before constructing an answer, by question type."""
    if isinstance(q, Noul):
        return _noul_from_wire(q, raw)
    if isinstance(q, Score):
        return _score_from_wire(q, raw)
    if isinstance(q, Box):
        return _box_from_wire(q, raw)
    if not isinstance(q, Choice):
        raise ProviderProtocolError("unsupported question type")
    if raw.get("type") != "choice":
        raise ProviderProtocolError("answer type does not match question")
    chosen = raw.get("choice")
    if not isinstance(chosen, str):
        raise ProviderProtocolError("answer has no 'choice' string")
    probabilities = raw.get("probabilities")
    probability = _as_float_or_none(
        probabilities.get(chosen) if isinstance(probabilities, dict) else None
    )
    if probability is None or not 0.0 <= probability <= 1.0:
        raise ProviderProtocolError("answer has no valid chosen probability")
    return Answer(
        id=q.id,
        value=chosen,
        p=probability,
        confidence=_as_float_or_none(raw.get("confidence")),
    )


def _noul_from_wire(q: Noul, raw: dict) -> Answer:
    """``{"type": "noul", "noul": <0..1>}``: the value IS the probability of yes."""
    if raw.get("type") != "noul":
        raise ProviderProtocolError("answer type does not match question")
    value = _as_float_or_none(raw.get("noul"))
    if value is None or not 0.0 <= value <= 1.0:
        raise ProviderProtocolError("answer has no valid 'noul' probability")
    return Answer(id=q.id, value=value, p=max(value, 1.0 - value))


def _score_from_wire(q: Score, raw: dict) -> Answer:
    """``{"type": "score", "score", "probabilities", "confidence"}``, checked against the levels.

    ``probabilities`` is keyed by level index as a string. It may name only this
    question's own levels and must name at least one, because ``p`` is the
    probability of the most likely level and an empty map carries none.
    """
    if raw.get("type") != "score":
        raise ProviderProtocolError("answer type does not match question")
    top = len(q.levels) - 1
    value = _as_float_or_none(raw.get("score"))
    if value is None or not 0.0 <= value <= top:
        raise ProviderProtocolError("answer has no valid 'score'")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        raise ProviderProtocolError("answer has no 'probabilities' object")
    allowed = {str(index) for index in range(top + 1)}
    level_ps: list[float] = []
    for level, raw_p in probabilities.items():
        if level not in allowed:
            raise ProviderProtocolError("probabilities name a level this question does not offer")
        level_p = _as_float_or_none(raw_p)
        if level_p is None or not 0.0 <= level_p <= 1.0:
            raise ProviderProtocolError("a level probability is not a finite number in 0..1")
        level_ps.append(level_p)
    confidence = _as_float_or_none(raw.get("confidence"))
    return Answer(id=q.id, value=value, p=max(level_ps), confidence=confidence)


def _box_from_wire(q: Box, raw: dict) -> Answer:
    """``{"type": "box", "box": {left, top, right, bottom}, "confidence"}``, fractions 0..1.

    A box with no confidence, an edge outside 0..1, or a zero or negative area is a
    protocol error: there is no answer a caller could act on.
    """
    if raw.get("type") != "box":
        raise ProviderProtocolError("answer type does not match question")
    box = raw.get("box")
    if not isinstance(box, dict):
        raise ProviderProtocolError("answer has no 'box' object")
    edges: dict[str, float] = {}
    for edge in ("left", "top", "right", "bottom"):
        value = _as_float_or_none(box.get(edge))
        if value is None or not 0.0 <= value <= 1.0:
            raise ProviderProtocolError("a box edge is not a fraction 0..1")
        edges[edge] = value
    if edges["left"] >= edges["right"] or edges["top"] >= edges["bottom"]:
        raise ProviderProtocolError("the box has no area")
    confidence = _as_float_or_none(raw.get("confidence"))
    if confidence is None or not 0.0 <= confidence <= 1.0:
        raise ProviderProtocolError("answer has no valid box confidence")
    return Answer(id=q.id, value=edges, p=confidence, confidence=confidence)


def _as_float_or_none(raw: Any) -> float | None:
    """*raw* as a finite float, or ``None``. Never coerces: a bool or a string is not a number."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _as_int_or_none(raw: Any) -> int | None:
    """*raw* as a non-negative int, or ``None``. A bool, a float or a string is not one.

    Token counts are whole and non-negative; anything else the provider puts in
    the field is dropped rather than written onto a row, so one malformed reply
    cannot plant a type a reader has to defend against.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


def _as_bounded_str_or_none(raw: Any) -> str | None:
    """*raw* as a clipped string, or ``None`` when it is not a non-empty string.

    ``model`` and ``request_id`` are provider-stamped text; this bounds them at
    the source so the row never carries an unbounded provider field even before
    the log's own clip.
    """
    if not isinstance(raw, str) or not raw:
        return None
    return raw[:_META_STR_MAX]


def _response_meta(parsed: Any, request_id: Any) -> dict[str, Any] | None:
    """The three provider-stamped fields worth a row, each bounded, or ``None``.

    ``model`` (``jev-latest`` is an alias that can move under a caller), the token
    ``usage`` the provider reports, and the ``x-typesafe-request-id`` header. Only
    the fields actually present are returned, and ``None`` when none are, so a row
    carries metadata only where the provider supplied some. Reads only these keys:
    it never includes ``answers`` or ``state``, so no conversation text can reach it.
    """
    meta: dict[str, Any] = {}
    body = _response_body(parsed) if isinstance(parsed, dict) else {}
    model = _as_bounded_str_or_none(body.get("model"))
    if model is not None:
        meta["provider_model"] = model
    usage = body.get("usage")
    if isinstance(usage, dict):
        input_tokens = _as_int_or_none(usage.get("input_tokens"))
        if input_tokens is not None:
            meta["input_tokens"] = input_tokens
        output_tokens = _as_int_or_none(usage.get("output_tokens"))
        if output_tokens is not None:
            meta["output_tokens"] = output_tokens
    rid = _as_bounded_str_or_none(request_id)
    if rid is not None:
        meta["request_id"] = rid
    return meta or None


class SystemOneProvider:
    """Ask Jev for the typed answers consumed by the decision gate."""

    def __init__(self, provider: Any) -> None:
        self._endpoint = str(getattr(provider, "endpoint", "") or _DEFAULT_ENDPOINT)
        self._model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
        self._api_key_setting = str(getattr(provider, "api_key", "") or "")
        self._timeout_ms = getattr(provider, "timeout_ms", 1000)
        #: Bounded metadata read off the LAST successful response: the ``model``
        #: the provider actually answered with (``jev-latest`` is an alias that can
        #: move under a caller), its token ``usage``, and the ``x-typesafe-request-id``
        #: header support asks for. ``None`` until a call succeeds; the gate reads it
        #: after ``ask`` returns and writes it onto the decision row, so a row can say
        #: which model version produced an answer or a near-miss. It never carries
        #: conversation text -- only the three provider-stamped fields.
        self.last_response_meta: dict[str, Any] | None = None

    async def ask(
        self,
        state: dict | str,
        questions: list[Question],
        *,
        assets: Mapping[str, ImageAsset] | None = None,
    ) -> Answers:
        """POST one request carrying every question. Raises on any failure.

        Error text never quotes the request or the response body: the gate logs
        the exception class only, and a provider message could echo the state.

        *assets* are the images the state names by id. A request carrying one, or
        a Box question, also carries the hosted profile's image opt-in header.
        """
        if not questions:
            raise ProviderProtocolError("no questions to ask")
        headers = {"Content-Type": "application/json"}
        if assets or any(isinstance(q, Box) for q in questions):
            headers[HOSTED_TYPESAFE.images_header] = "true"
        # A local model server gets NO credential. Whatever listens on a loopback
        # port is not TypeSafe, and handing it the Jev key would give that key to
        # any process on this machine that bound the port first.
        if not is_loopback_endpoint(self._endpoint):
            api_key = await asyncio.to_thread(resolve_api_key, self._api_key_setting)
            if not api_key:
                raise ProviderProtocolError("no api key configured")
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            _warn_keyless_custom_loopback(self._endpoint, self._model)

        import httpx

        body = _to_wire(state, self._model, questions, assets)
        timeout_ms = _as_float_or_none(self._timeout_ms)
        # Per request, not per client: the client is shared, and the bound covers
        # waiting for a pooled connection as well as the exchange itself.
        seconds = max(0.001, (timeout_ms or 0.0) / 1000.0)
        session = _session_for_running_loop()
        # No redirects, ever: consent is bound to THIS endpoint, and following a
        # 3xx would replay the body -- conversation text and skill descriptions
        # -- to whatever Location the server named. A 3xx is refused below as a
        # non-2xx, so the row says the provider misbehaved, not that we sent.
        try:
            async with asyncio.timeout(seconds):
                async with session.stream(
                    "POST",
                    self._endpoint,
                    json=body,
                    headers=headers,
                    timeout=httpx.Timeout(seconds),
                ) as resp:
                    if resp.status_code < 200 or resp.status_code >= 300:
                        raise ProviderHttpError(f"HTTP {resp.status_code}")
                    # Read inside the response context, before the body: the header
                    # is the one piece of metadata that does not live in the body.
                    request_id = resp.headers.get(_REQUEST_ID_HEADER)
                    # Bounded and chunked: only a running total refuses on the real
                    # size instead of truncating, and a chunked body is covered too.
                    # Decoded bytes (a gzip or br body is decompressed as it streams),
                    # so the cap bounds what is held, a compression bomb included.
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(_RESPONSE_CHUNK_BYTES):
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise ProviderProtocolError(
                                f"response exceeded {_MAX_RESPONSE_BYTES} bytes"
                            )
                        chunks.append(chunk)
        except httpx.TimeoutException:
            # The gate reads a timeout by its class; keep it one.
            raise asyncio.TimeoutError from None
        text = b"".join(chunks).decode("utf-8", errors="replace")
        import json

        try:
            parsed = json.loads(text)
        except ValueError:
            raise ProviderProtocolError("response is not JSON") from None
        answers = _from_wire(parsed, questions)
        # Only after a fully-valid parse: a row never carries metadata for a
        # response the gate is about to treat as an error. Built from the parsed
        # body plus the header, every value bounded, no conversation text.
        self.last_response_meta = _response_meta(parsed, request_id)
        return answers

    async def warm(self) -> None:
        """Open the connection a burst of decisions will ride, before the first one.

        A bodiless ``HEAD`` to the configured endpoint, with no credential, no
        state and no question: only the handshake is wanted, so its status is
        ignored. The gate calls this only where it would also send a decision.
        """
        session = _session_for_running_loop()
        seconds = max(0.001, (_as_float_or_none(self._timeout_ms) or 0.0) / 1000.0)
        import httpx

        with contextlib.suppress(httpx.HTTPError):
            await session.head(self._endpoint, timeout=httpx.Timeout(max(seconds, 2.0)))


#: Former names, kept for one release so an importer of the old spellings keeps
#: working. They are the same objects, so an ``isinstance`` or ``except`` on
#: either name matches both.
JevOracle = SystemOneProvider
JevProtocolError = ProviderProtocolError
JevHttpError = ProviderHttpError
