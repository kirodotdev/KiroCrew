"""A SageMaker endpoint as the decision model, from the Decisions card.

The owner-only ``PUT /api/decisions/provider`` (``handlers/decisions.py``) takes
``{"preset": "sagemaker", "endpoint_name", "region"[, "profile"]}`` and writes the
address :func:`sagemaker_target` builds from those parts; the GET names a
configured address by them (:func:`configured_sagemaker`).
``POST /api/decisions/provider/sagemaker-test`` sends that endpoint one fixed
question and writes nothing.

The address is never taken from the body: each part is checked by
``impl_sagemaker.build_endpoint`` against the pattern the client parses it with,
so a dashboard caller can name a SageMaker endpoint and nothing else.

Split from ``handlers/decisions.py`` for its size. That module imports this one
inside its handlers; this one imports the shared owner gate, audit writer and
error codes from it at module top.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.handlers.decisions import (
    _CODE_CAPABILITY_DENIED,
    _CODE_INVALID_JSON,
    _CODE_PROVIDER_INVALID_BODY,
    _audit,
    _deny_non_owner,
)

#: The picker's id for "a SageMaker endpoint". Not a ``local_models`` preset:
#: ``active_id`` keeps reporting such an address as ``custom`` to its other callers.
PRESET_SAGEMAKER = "sagemaker"
#: The fields a SageMaker body carries besides ``preset``; ``profile`` is optional.
_SAGEMAKER_REQUIRED = frozenset({"preset", "endpoint_name", "region"})
_SAGEMAKER_OPTIONAL = frozenset({"profile"})
#: ``timeout_ms`` written for a SageMaker endpoint the config did not already name:
#: a remote model on a GPU answers in about a second, a cold one slower.
SAGEMAKER_TIMEOUT_MS = 5000
#: Budget for the owner's connection test, longer than a decision's so a slow
#: first answer still reports a latency instead of a timeout.
SAGEMAKER_TEST_TIMEOUT_MS = 10000

_CODE_SAGEMAKER_INVALID = "decisions_provider_sagemaker_invalid"
_CODE_SAGEMAKER_UNKNOWN_PROFILE = "decisions_provider_sagemaker_unknown_profile"
_CODE_SAGEMAKER_UNAVAILABLE = "decisions_provider_sagemaker_unavailable"


def configured_sagemaker(endpoint: object) -> dict | None:
    """The three parts of a valid ``sagemaker://`` *endpoint*, else ``None``."""
    from kiro_crew.decisions import impl_sagemaker

    if not impl_sagemaker.is_sagemaker_endpoint(endpoint):
        return None
    try:
        target = impl_sagemaker.parse_endpoint(endpoint)
    except (impl_sagemaker.SageMakerConfigError, ValueError):
        # ``urlsplit`` raises ValueError on a hand-written address such as an
        # unbalanced ``[``; the picker reports it as ``custom`` rather than failing.
        return None
    return {
        "endpoint_name": target.endpoint_name,
        "region": target.region,
        "profile": target.profile,
    }


class ProviderRefused(Exception):
    """A provider body the route refuses with its own status and code."""

    def __init__(self, status: int, code: str, error: str, field: str = "") -> None:
        super().__init__(error)
        self.status, self.code, self.error, self.field = status, code, error, field

    def response(self) -> web.Response:
        # ``field`` is "" when the refusal is not about one field.
        return web.json_response(
            {"error": self.error, "code": self.code, "field": self.field}, status=self.status
        )


def _sagemaker_address(body: dict) -> str:
    """The ``sagemaker://`` address a SageMaker *body* names. Raises :class:`ProviderRefused`.

    The address is built here from three checked fields; no field is written
    verbatim, so the body cannot add a path, a host, a port or another query key.
    The profile must be one the local AWS config files name. Filesystem IO.
    """
    from kiro_crew.decisions import impl_sagemaker

    keys = set(body)
    if not _SAGEMAKER_REQUIRED <= keys or keys - _SAGEMAKER_REQUIRED - _SAGEMAKER_OPTIONAL:
        raise ProviderRefused(
            400,
            _CODE_PROVIDER_INVALID_BODY,
            'a SageMaker body is {"preset": "sagemaker", "endpoint_name", "region"[, "profile"]}',
        )
    profile = body.get("profile", "")
    try:
        address = impl_sagemaker.build_endpoint(body["endpoint_name"], body["region"], profile)
    except impl_sagemaker.SageMakerAddressFieldError as exc:
        raise ProviderRefused(
            400, _CODE_SAGEMAKER_INVALID, f"the {exc.field} is not valid", field=exc.field
        ) from None
    if not impl_sagemaker.boto3_installed():
        raise ProviderRefused(
            409, _CODE_SAGEMAKER_UNAVAILABLE, "boto3 is not installed; install 'boto3>=1.34,<2'"
        )
    if profile and not impl_sagemaker.profile_exists(profile):
        raise ProviderRefused(
            400,
            _CODE_SAGEMAKER_UNKNOWN_PROFILE,
            "no AWS profile by that name in the AWS config files",
            field="profile",
        )
    return address


def sagemaker_target(body: dict) -> tuple[str, str, int]:
    """``(endpoint, model, timeout_ms)`` for a SageMaker *body*. Raises :class:`ProviderRefused`.

    A config that already names a SageMaker endpoint keeps its model and timeout,
    which the owner may have set by hand for that endpoint; otherwise the shipped
    model id and :data:`SAGEMAKER_TIMEOUT_MS`. Filesystem IO.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
    from kiro_crew.decisions import gate as _gate
    from kiro_crew.decisions.types import is_model_id

    address = _sagemaker_address(body)
    cfg = live.snapshot() or KiroCrewConfig.load()
    provider = getattr(getattr(cfg, "decisions", None), "provider", None)
    model, timeout_ms = DECISION_PROVIDER_MODEL_DEFAULT, SAGEMAKER_TIMEOUT_MS
    if configured_sagemaker(_gate.configured_endpoint(cfg)) is not None:
        current_model = getattr(provider, "model", "")
        current_timeout = getattr(provider, "timeout_ms", None)
        if is_model_id(current_model):
            model = current_model
        if isinstance(current_timeout, int) and not isinstance(current_timeout, bool):
            if current_timeout > 0:
                timeout_ms = current_timeout
    return address, model, timeout_ms


OP_SAGEMAKER_TEST = "decisions_provider_sagemaker_test"

#: What the connection test reports for each failure, by the client's own error
#: class, most specific first. Each value is a fixed word the card translates.
_SAGEMAKER_TEST_KINDS: tuple[tuple[str, str], ...] = (
    ("SageMakerCredentialsError", "credentials"),
    ("SageMakerEndpointNotFound", "not_found"),
    ("SageMakerTimeout", "timeout"),
    ("SageMakerThrottled", "throttled"),
    ("SageMakerUnavailable", "unavailable"),
    ("SageMakerModelError", "model_error"),
    ("SageMakerProtocolError", "protocol"),
)


def _sagemaker_test_kind(exc: BaseException) -> str:
    """The fixed word for *exc*'s failure; ``failed`` for anything unnamed."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    return next((kind for name, kind in _SAGEMAKER_TEST_KINDS if name in names), "failed")


def _sagemaker_test_target(body: object) -> tuple[str, str]:
    """``(address, model)`` the connection test sends to. Raises :class:`ProviderRefused`.

    The same body and checks as a SageMaker provider PUT, and the model that PUT
    would write. Refused like the PUT when the fleet withdraws the hosted side, or
    when the decisions ceiling's ``models`` / ``endpoints`` lists refuse it: the
    test leaves the machine too. Filesystem IO.
    """
    from kiro_crew.decisions.capability import is_decisions_denied, targets_denied
    from kiro_crew.decisions.impl_sagemaker import policy_endpoint

    if not isinstance(body, dict) or body.get("preset") != PRESET_SAGEMAKER:
        raise ProviderRefused(
            400,
            _CODE_PROVIDER_INVALID_BODY,
            'body must be {"preset": "sagemaker", "endpoint_name", "region"[, "profile"]}',
        )
    address, model, _ = sagemaker_target(body)
    if is_decisions_denied(local=False) or targets_denied(
        point=None, model=model, endpoint=policy_endpoint(address)
    ):
        raise ProviderRefused(
            403, _CODE_CAPABILITY_DENIED, "this decision model is turned off by policy"
        )
    return address, model


async def api_decisions_provider_sagemaker_test(request: web.Request) -> web.Response:
    """POST /api/decisions/provider/sagemaker-test -- ask a SageMaker endpoint one fixed question.

    The body is the one a SageMaker provider PUT takes, so the owner can test an
    endpoint before choosing it; nothing is written. One yes/no question with fixed
    text is signed and sent through the decision client itself, so the answer says
    whether decisions would reach that endpoint with these credentials. No message,
    memory or screenshot is sent, which is why it needs no consent. The answer is
    ``{"ok", "latency_ms", "error", "message"}``: ``error`` is a fixed word, and
    ``message`` is the client's own fixed text, which never quotes the request, the
    response or a credential.
    """
    import time
    from types import SimpleNamespace

    denied = await _deny_non_owner(request, OP_SAGEMAKER_TEST)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        await _audit(
            request,
            operation=OP_SAGEMAKER_TEST,
            outcome="denied",
            error="invalid_json",
            resources="decisions.provider",
        )
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    try:
        address, model = await asyncio.to_thread(_sagemaker_test_target, body)
    except ProviderRefused as refused:
        await _audit(
            request,
            operation=OP_SAGEMAKER_TEST,
            outcome="denied",
            error=refused.code,
            resources="decisions.provider",
        )
        return refused.response()
    from kiro_crew.decisions.impl_sagemaker import SageMakerError, SageMakerOracle
    from kiro_crew.decisions.impl_systemone import ProviderProtocolError
    from kiro_crew.decisions.types import Noul

    oracle = SageMakerOracle(
        SimpleNamespace(endpoint=address, model=model, timeout_ms=SAGEMAKER_TEST_TIMEOUT_MS)
    )
    question = Noul(id="connection_test", prompt="Is this message a connection test?")
    started = time.monotonic()
    error, message = "", ""
    try:
        await oracle.ask("Connection test from the Kiro Crew settings page.", [question])
    except SageMakerError as exc:
        error, message = _sagemaker_test_kind(exc), str(exc)
    except ProviderProtocolError:
        # The answer reader's own errors can name question ids or values; this one
        # says only that the answer was not a System One answer.
        error, message = "protocol", "the endpoint answered, but not with a System One answer"
    except Exception:
        # Nothing says the endpoint answered, so this is not called a protocol error.
        error, message = "failed", "the connection test failed"
    latency_ms = int((time.monotonic() - started) * 1000)
    name = configured_sagemaker(address) or {}
    await _audit(
        request,
        operation=OP_SAGEMAKER_TEST,
        outcome="error" if error else "allowed",
        error=error,
        resources=(
            f"decisions.provider sagemaker={name.get('endpoint_name', '')}"
            f" region={name.get('region', '')} latency_ms={latency_ms}"
        ),
    )
    return web.json_response(
        {"ok": not error, "latency_ms": latency_ms, "error": error, "message": message}
    )
