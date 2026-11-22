"""An HTTPS endpoint signed with AWS SigV4 as the decision model, from the Decisions card.

The owner-only ``PUT /api/decisions/provider`` (``handlers/decisions.py``) takes
``{"preset": "aws_https", "url", "service", "region"[, "profile"]}`` and writes the
``aws-https://`` address :func:`signed_https_target` builds from those parts; the GET
names a configured address by them (:func:`configured_signed_https`).
``POST /api/decisions/provider/aws-https-test`` sends that endpoint one fixed
question and writes nothing.

The address is never taken from the body: each part is checked by
``impl_signed_https.build_endpoint`` against the pattern the client parses it with,
so the body can name an ``https`` host, port and path, a signing service, a region
and a profile, and nothing else. The ceiling's ``endpoints`` list is asked about the
``https://`` URL the request reaches before anything is written or sent.

Split from ``handlers/decisions.py`` for its size, as ``decisions_sagemaker`` is.
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
from kiro_crew.dashboard.handlers.decisions_sagemaker import ProviderRefused

#: The picker's id for "an HTTPS endpoint signed with SigV4"; also its allow-list id.
PRESET_SIGNED_HTTPS = "aws_https"
_REQUIRED = frozenset({"preset", "url", "service", "region"})
_OPTIONAL = frozenset({"profile", "model"})
#: ``timeout_ms`` written for such an endpoint the config did not already name: a
#: remote GPU model answers in about a second, a cold one slower.
SIGNED_HTTPS_TIMEOUT_MS = 5000
#: Budget for the owner's connection test.
SIGNED_HTTPS_TEST_TIMEOUT_MS = 10000

_CODE_INVALID = "decisions_provider_aws_https_invalid"
_CODE_UNKNOWN_PROFILE = "decisions_provider_aws_https_unknown_profile"
_CODE_UNAVAILABLE = "decisions_provider_aws_https_unavailable"


def configured_signed_https(endpoint: object) -> dict | None:
    """The parts of a valid ``aws-https://`` *endpoint*, else ``None``."""
    from kiro_crew.decisions import impl_signed_https

    if not impl_signed_https.is_signed_https_endpoint(endpoint):
        return None
    try:
        target = impl_signed_https.parse_endpoint(endpoint)
    except (impl_signed_https.SignedHttpsConfigError, ValueError):
        return None
    return {
        "url": target.url,
        "service": target.service,
        "region": target.region,
        "profile": target.profile,
    }


def _address(body: dict) -> str:
    """The ``aws-https://`` address a body names. Raises :class:`ProviderRefused`. Filesystem IO."""
    from kiro_crew.decisions import impl_sagemaker, impl_signed_https

    keys = set(body)
    if not _REQUIRED <= keys or keys - _REQUIRED - _OPTIONAL:
        raise ProviderRefused(
            400,
            _CODE_PROVIDER_INVALID_BODY,
            'an AWS-signed HTTPS body is {"preset": "aws_https", "url", "service", "region"'
            '[, "profile"][, "model"]}',
        )
    profile = body.get("profile", "")
    try:
        address = impl_signed_https.build_endpoint(
            body["url"], body["service"], body["region"], profile
        )
    except impl_signed_https.SignedHttpsAddressFieldError as exc:
        raise ProviderRefused(
            400, _CODE_INVALID, f"the {exc.field} is not valid", field=exc.field
        ) from None
    if not impl_signed_https.botocore_installed():
        raise ProviderRefused(
            409, _CODE_UNAVAILABLE, "boto3 is not installed; install 'boto3>=1.34,<2'"
        )
    # The same profile lookup the SageMaker option uses: names only, no credential.
    if profile and not impl_sagemaker.profile_exists(profile):
        raise ProviderRefused(
            400,
            _CODE_UNKNOWN_PROFILE,
            "no AWS profile by that name in the AWS config files",
            field="profile",
        )
    return address


def signed_https_target(body: dict) -> tuple[str, str, int]:
    """``(endpoint, model, timeout_ms)`` for an AWS-signed HTTPS *body*. Raises :class:`ProviderRefused`.

    A config that already names such an endpoint keeps its model and timeout, which
    the owner may have set by hand for it; otherwise the shipped model id and
    :data:`SIGNED_HTTPS_TIMEOUT_MS`. Filesystem IO.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
    from kiro_crew.decisions import gate as _gate
    from kiro_crew.decisions.types import is_model_id

    address = _address(body)
    cfg = live.snapshot() or KiroCrewConfig.load()
    provider = getattr(getattr(cfg, "decisions", None), "provider", None)
    model, timeout_ms = DECISION_PROVIDER_MODEL_DEFAULT, SIGNED_HTTPS_TIMEOUT_MS
    if configured_signed_https(_gate.configured_endpoint(cfg)) is not None:
        current_model = getattr(provider, "model", "")
        current_timeout = getattr(provider, "timeout_ms", None)
        if is_model_id(current_model):
            model = current_model
        if isinstance(current_timeout, int) and not isinstance(current_timeout, bool):
            if current_timeout > 0:
                timeout_ms = current_timeout
    # A model id typed on the card wins over both; it is checked like any model id.
    raw_model = body.get("model")
    if raw_model is not None:
        if not isinstance(raw_model, str) or not is_model_id(raw_model.strip()):
            raise ProviderRefused(400, _CODE_INVALID, "the model is not valid", field="model")
        model = raw_model.strip()
    return address, model, timeout_ms


OP_SIGNED_HTTPS_TEST = "decisions_provider_aws_https_test"

#: What the connection test reports for each failure, by the client's own error
#: class, most specific first. Each value is a fixed word the card translates.
_TEST_KINDS: tuple[tuple[str, str], ...] = (
    ("SignedHttpsCredentialsError", "credentials"),
    ("SignedHttpsUnavailable", "unavailable"),
    ("SignedHttpsHttpError", "http_error"),
    ("TimeoutError", "timeout"),
    ("ProviderProtocolError", "protocol"),
)


def _test_kind(exc: BaseException) -> str:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return next((kind for name, kind in _TEST_KINDS if name in names), "failed")


def _test_target(body: object) -> tuple[str, str]:
    """``(address, model)`` the connection test sends to. Raises :class:`ProviderRefused`.

    The same body and checks as the provider PUT, and refused like it when the
    fleet withdraws the hosted side, when the ``providers`` list does not allow this
    kind of endpoint, or when the ``models`` / ``endpoints`` lists refuse it: the
    test leaves the machine too. Filesystem IO.
    """
    from kiro_crew.decisions.capability import (
        PROVIDER_SIGNED_HTTPS,
        is_decisions_denied,
        targets_denied,
    )
    from kiro_crew.decisions.impl_signed_https import policy_endpoint

    if not isinstance(body, dict) or body.get("preset") != PRESET_SIGNED_HTTPS:
        raise ProviderRefused(
            400,
            _CODE_PROVIDER_INVALID_BODY,
            'body must be {"preset": "aws_https", "url", "service", "region"[, "profile"]}',
        )
    address, model, _ = signed_https_target(body)
    if is_decisions_denied(local=False, provider=PROVIDER_SIGNED_HTTPS) or targets_denied(
        point=None, model=model, endpoint=policy_endpoint(address)
    ):
        raise ProviderRefused(
            403, _CODE_CAPABILITY_DENIED, "this decision model is not allowed by your administrator"
        )
    return address, model


async def api_decisions_provider_signed_https_test(request: web.Request) -> web.Response:
    """POST /api/decisions/provider/aws-https-test -- ask a signed HTTPS endpoint one fixed question.

    The body is the one the provider PUT takes, so the owner can test an endpoint
    before choosing it; nothing is written. One yes/no question with fixed text is
    signed and sent through the decision client itself. No message, memory or
    screenshot is sent, which is why it needs no consent. The answer is
    ``{"ok", "latency_ms", "error", "message"}``: ``error`` is a fixed word, and
    ``message`` is the client's own fixed text, which never quotes the request, the
    response or a credential.
    """
    import time
    from types import SimpleNamespace

    denied = await _deny_non_owner(request, OP_SIGNED_HTTPS_TEST)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        await _audit(
            request,
            operation=OP_SIGNED_HTTPS_TEST,
            outcome="denied",
            error="invalid_json",
            resources="decisions.provider",
        )
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    try:
        address, model = await asyncio.to_thread(_test_target, body)
    except ProviderRefused as refused:
        await _audit(
            request,
            operation=OP_SIGNED_HTTPS_TEST,
            outcome="denied",
            error=refused.code,
            resources="decisions.provider",
        )
        return refused.response()
    from kiro_crew.decisions.impl_signed_https import SignedHttpsError, SignedHttpsProvider
    from kiro_crew.decisions.impl_systemone import ProviderProtocolError
    from kiro_crew.decisions.types import Noul

    client = SignedHttpsProvider(
        SimpleNamespace(endpoint=address, model=model, timeout_ms=SIGNED_HTTPS_TEST_TIMEOUT_MS)
    )
    question = Noul(id="connection_test", prompt="Is this message a connection test?")
    started = time.monotonic()
    error, message = "", ""
    try:
        await client.ask("Connection test from the Kiro Crew settings page.", [question])
    except SignedHttpsError as exc:
        error, message = _test_kind(exc), str(exc)
    except asyncio.TimeoutError:
        error, message = "timeout", "no answer within the test budget"
    except ProviderProtocolError:
        error, message = "protocol", "the endpoint answered, but not with a System One answer"
    except Exception:
        error, message = "failed", "the connection test failed"
    latency_ms = int((time.monotonic() - started) * 1000)
    parts = configured_signed_https(address) or {}
    await _audit(
        request,
        operation=OP_SIGNED_HTTPS_TEST,
        outcome="error" if error else "allowed",
        error=error,
        resources=(
            f"decisions.provider aws_https={parts.get('url', '')}"
            f" service={parts.get('service', '')} region={parts.get('region', '')}"
            f" latency_ms={latency_ms}"
        ),
    )
    return web.json_response(
        {"ok": not error, "latency_ms": latency_ms, "error": error, "message": message}
    )
