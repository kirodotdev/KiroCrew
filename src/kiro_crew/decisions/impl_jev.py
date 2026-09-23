"""Jev Choice questions over HTTP, using https://docs.typesafe.ai/api.

Requests map options to nullable rubric text in ``criteria``. Responses must
carry the matching ``choice`` type and a finite probability for the chosen
option. The gate validates answer domains before a skill selection is consumed.
Transport and protocol failures raise; the gate supplies fallback, not retries.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.decisions.types import Answer, Answers, Choice, Question, is_model_id

logger = logging.getLogger(__name__)

# Bound the complete body, including chunked responses, before JSON parsing.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT
_SECRET_PREFIX = "secret://"
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
    """Map Choice options to the provider's criteria map.

    The model id is checked here as well as by the gate's scrub: this is the one
    function that puts it on the wire, so the bound holds for any caller.
    """
    if not is_model_id(model):
        raise JevProtocolError("provider.model is not a model id")
    wire_questions: dict[str, Any] = {}
    for q in questions:
        if not isinstance(q, Choice):
            raise JevProtocolError("unsupported question type")
        wire_questions[q.id] = {
            "type": "choice",
            "instructions": q.prompt,
            "criteria": {opt: None for opt in q.options},
        }
    return {"state": state, "model": model, "questions": wire_questions}


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
    """Validate wire fields before constructing an answer."""
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


def _as_float_or_none(raw: Any) -> float | None:
    """*raw* as a finite float, or ``None``. Never coerces: a bool or a string is not a number."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


class JevOracle:
    """Ask Jev for the Choice answers consumed by the decision gate."""

    def __init__(self, provider: Any) -> None:
        self._endpoint = str(getattr(provider, "endpoint", "") or _DEFAULT_ENDPOINT)
        self._model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
        self._api_key_setting = str(getattr(provider, "api_key", "") or "")
        self._timeout_ms = getattr(provider, "timeout_ms", 1000)

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """POST one request carrying every question. Raises on any failure.

        Error text never quotes the request or the response body: the gate logs
        the exception class only, and a provider message could echo the state.
        """
        if not questions:
            raise JevProtocolError("no questions to ask")
        api_key = await asyncio.to_thread(resolve_api_key, self._api_key_setting)
        if not api_key:
            raise JevProtocolError("no api key configured")

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
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise JevHttpError(f"HTTP {resp.status}")
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
        return _from_wire(parsed, questions)
