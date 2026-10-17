"""Jev questions over HTTP, using https://docs.typesafe.ai/api.

Each question type maps to the provider's own: a ``Choice`` sends a ``criteria``
map keyed by every option, each value its optional rubric text or ``null`` when
none is authored, a ``Noul`` sends its optional yes/no rubric as
``criteria.true`` / ``criteria.false``, and a ``Score`` sends its levels as the
ordered ``criteria`` array. Every answer must carry the type of the question
it answers and that type's own fields, finite and in range; anything else is a
protocol error. The gate validates answer domains again before anything is
consumed. Transport and protocol failures raise; the gate supplies fallback, not
retries.

The same client serves a local System One server (``decisions/local_models.py``):
an endpoint on a literal loopback address is sent no credential at all.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.decisions.local_models import active_id, is_loopback_endpoint
from kiro_crew.decisions.types import (
    SCORE_MAX_LEVELS,
    SCORE_MIN_LEVELS,
    Answer,
    Answers,
    Choice,
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
_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT
_SECRET_PREFIX = "secret://"
#: The response header carrying the provider's request id. Support asks for this
#: value; the vendor documents it under this name.
_REQUEST_ID_HEADER = "x-typesafe-request-id"
#: Longest ``model``/``request_id`` string kept on the metadata. Both are
#: provider-stamped, so this is far past any real value; it is the bound that
#: keeps a misbehaving provider from writing an unbounded field onto the row.
_META_STR_MAX = 128
#: The ONE vault entry this module will read. ``provider.api_key`` lives in the
#: agent-writable ``config.json``, so a reference that named any entry would let
#: a prompt-injected shell pick which of the operator's secrets is sent as the
#: bearer token. Only the dedicated Jev entry resolves; any other name is "no key".
VAULT_SECRET_NAME = "TYPESAFE_API_KEY"


class JevProtocolError(RuntimeError):
    """The provider answered, but not in a shape this module can read."""


class JevHttpError(RuntimeError):
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


def _to_wire(state: dict | str, model: str, questions: list[Question]) -> dict[str, Any]:
    """Map each question to the provider's wire shape for its type.

    The model id is checked here as well as by the gate's scrub: this is the one
    function that puts it on the wire, so the bound holds for any caller.
    """
    if not is_model_id(model):
        raise JevProtocolError("provider.model is not a model id")
    wire_questions: dict[str, Any] = {}
    for q in questions:
        wire_questions[q.id] = _question_to_wire(q)
    return {"state": state, "model": model, "questions": wire_questions}


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
            raise JevProtocolError("score question has an unsupported number of levels")
        return {"type": "score", "instructions": q.prompt, "criteria": list(q.levels)}
    raise JevProtocolError("unsupported question type")


def _from_wire(body: Any, questions: list[Question]) -> Answers:
    """Return every requested answer or raise; partial results are failures."""
    if not isinstance(body, dict):
        raise JevProtocolError("response is not an object")
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict):
        raise JevProtocolError("response has no 'answers' object")
    answers: Answers = {}
    for q in questions:
        raw = raw_answers.get(q.id)
        if not isinstance(raw, dict):
            raise JevProtocolError("no answer for question")
        answers[q.id] = _answer_from_wire(q, raw)
    return answers


def _answer_from_wire(q: Question, raw: dict) -> Answer:
    """Validate wire fields before constructing an answer, by question type."""
    if isinstance(q, Noul):
        return _noul_from_wire(q, raw)
    if isinstance(q, Score):
        return _score_from_wire(q, raw)
    if not isinstance(q, Choice):
        raise JevProtocolError("unsupported question type")
    if raw.get("type") != "choice":
        raise JevProtocolError("answer type does not match question")
    chosen = raw.get("choice")
    if not isinstance(chosen, str):
        raise JevProtocolError("answer has no 'choice' string")
    probabilities = raw.get("probabilities")
    probability = _as_float_or_none(
        probabilities.get(chosen) if isinstance(probabilities, dict) else None
    )
    if probability is None or not 0.0 <= probability <= 1.0:
        raise JevProtocolError("answer has no valid chosen probability")
    return Answer(
        id=q.id,
        value=chosen,
        p=probability,
        confidence=_as_float_or_none(raw.get("confidence")),
    )


def _noul_from_wire(q: Noul, raw: dict) -> Answer:
    """``{"type": "noul", "noul": <0..1>}``: the value IS the probability of yes."""
    if raw.get("type") != "noul":
        raise JevProtocolError("answer type does not match question")
    value = _as_float_or_none(raw.get("noul"))
    if value is None or not 0.0 <= value <= 1.0:
        raise JevProtocolError("answer has no valid 'noul' probability")
    return Answer(id=q.id, value=value, p=max(value, 1.0 - value))


def _score_from_wire(q: Score, raw: dict) -> Answer:
    """``{"type": "score", "score", "probabilities", "confidence"}``, checked against the levels.

    ``probabilities`` is keyed by level index as a string. It may name only this
    question's own levels and must name at least one, because ``p`` is the
    probability of the most likely level and an empty map carries none.
    """
    if raw.get("type") != "score":
        raise JevProtocolError("answer type does not match question")
    top = len(q.levels) - 1
    value = _as_float_or_none(raw.get("score"))
    if value is None or not 0.0 <= value <= top:
        raise JevProtocolError("answer has no valid 'score'")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        raise JevProtocolError("answer has no 'probabilities' object")
    allowed = {str(index) for index in range(top + 1)}
    level_ps: list[float] = []
    for level, raw_p in probabilities.items():
        if level not in allowed:
            raise JevProtocolError("probabilities name a level this question does not offer")
        level_p = _as_float_or_none(raw_p)
        if level_p is None or not 0.0 <= level_p <= 1.0:
            raise JevProtocolError("a level probability is not a finite number in 0..1")
        level_ps.append(level_p)
    confidence = _as_float_or_none(raw.get("confidence"))
    return Answer(id=q.id, value=value, p=max(level_ps), confidence=confidence)


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
    it never touches ``answers`` or ``state``, so no conversation text can reach it.
    """
    meta: dict[str, Any] = {}
    body = parsed if isinstance(parsed, dict) else {}
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


class JevOracle:
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

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """POST one request carrying every question. Raises on any failure.

        Error text never quotes the request or the response body: the gate logs
        the exception class only, and a provider message could echo the state.
        """
        if not questions:
            raise JevProtocolError("no questions to ask")
        headers = {"Content-Type": "application/json"}
        # A local model server gets NO credential. Whatever listens on a loopback
        # port is not TypeSafe, and handing it the Jev key would give that key to
        # any process on this machine that bound the port first.
        if not is_loopback_endpoint(self._endpoint):
            api_key = await asyncio.to_thread(resolve_api_key, self._api_key_setting)
            if not api_key:
                raise JevProtocolError("no api key configured")
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            _warn_keyless_custom_loopback(self._endpoint, self._model)

        import aiohttp

        body = _to_wire(state, self._model, questions)
        timeout_ms = _as_float_or_none(self._timeout_ms)
        timeout = aiohttp.ClientTimeout(total=max(0.001, (timeout_ms or 0.0) / 1000.0))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # No redirects, ever: consent is bound to THIS endpoint, and following a
            # 3xx would replay the body -- conversation text and skill descriptions
            # -- to whatever Location the server named. A 3xx is refused below as a
            # non-2xx, so the row says the provider misbehaved, not that we sent.
            async with session.post(
                self._endpoint,
                json=body,
                allow_redirects=False,
                headers=headers,
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise JevHttpError(f"HTTP {resp.status}")
                # Read inside the response context, before the body: the header is
                # the one piece of metadata that does not live in the parsed body.
                request_id = resp.headers.get(_REQUEST_ID_HEADER)
                # Bounded and chunked: `resp.text()` reads to EOF, and a single
                # `read(n)` may return short while more is coming, so only a
                # running total refuses on the real size instead of truncating.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        raise JevProtocolError(f"response exceeded {_MAX_RESPONSE_BYTES} bytes")
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="replace")
                import json

                try:
                    parsed = json.loads(text)
                except ValueError:
                    raise JevProtocolError("response is not JSON") from None
        answers = _from_wire(parsed, questions)
        # Only after a fully-valid parse: a row never carries metadata for a
        # response the gate is about to treat as an error. Built from the parsed
        # body plus the header, every value bounded, no conversation text.
        self.last_response_meta = _response_meta(parsed, request_id)
        return answers
