"""ACP / model-provider adapter: the provider stream's own error vocabulary.

Bedrock and the other backends behind ``kiro-cli`` report throttling
(``ThrottlingException``, ``TooManyRequestsException``, ``429``), spent
allowances (``monthly usage limit``), capacity rollouts (``the model 'X' is not
available``), 5xx and credential failures through the ACP JSON-RPC error
frame. :mod:`kiro_crew.acp.client` already owns ONE set of patterns for
those, shared by its user-facing formatter and its retry classifier, and
exposes them as ``classify_provider_error``; this adapter maps that verdict
onto the dependency vocabulary (the client module is imported lazily -- it is
large and imports nothing from ``taskq``) so a third copy cannot drift.

The scope is the provider/model pair when the error names one
(``provider:<model>``), else the caller's hint, else ``provider:acp``: a
throttle on one model must not park tasks that use another.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..dependency import (
    KIND_AUTH_FAILED,
    KIND_CONCURRENCY_EXCEEDED,
    KIND_DEPENDENCY_UNAVAILABLE,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_QUOTA_EXHAUSTED,
    KIND_RATE_LIMITED,
    DependencySignal,
)
from . import http as _http

SOURCE = "acp_provider"
SCOPE_DEFAULT = "provider:acp"

_RE_QUOTA_NAMED = re.compile(r"\bServiceQuotaExceededException\b")


def _client() -> Any:
    """The ACP client module through the SDK driver; ``None`` when unavailable."""
    from kiro_crew.agent_sdk.drivers.acp import provider_error_client

    return provider_error_client()


def scope_for(exc: BaseException, hint: str) -> str:
    """The caller's hint wins; else the rejected model names the scope."""
    if hint:
        return hint
    model = getattr(exc, "rejected_model", None) or getattr(exc, "model", None)
    if isinstance(model, str) and model:
        return f"provider:{model}"
    return SCOPE_DEFAULT


def classify_text(
    haystack: str,
    *,
    data: str | None = None,
    scope: str = SCOPE_DEFAULT,
    transient_hint: bool | None = None,
    now: float | None = None,
) -> DependencySignal | None:
    """Classify provider error TEXT (formatted or raw); ``data`` is the raw provider field.

    Precedence is ``acp.client.classify_provider_error``'s: usage-limit
    (quota, terminal without a reset) → malformed request (permanent) →
    model-unavailable → throttle → credential propagation (retryable) →
    auth (terminal) → connection / 5xx (unavailable) → ``transient_hint``.
    """
    client = _client()
    detail = haystack[:200]
    data_field = data if data is not None else haystack
    if client is not None:
        verdict = client.classify_provider_error(haystack, data=data_field)
        kind = verdict.kind
        if kind == client.PROVIDER_ERROR_USAGE_LIMIT:
            return DependencySignal(
                kind=KIND_QUOTA_EXHAUSTED, dependency_scope=scope, source=SOURCE, detail=detail
            )
        if kind == client.PROVIDER_ERROR_MALFORMED_REQUEST:
            return DependencySignal(
                kind=KIND_PERMANENT_PARAM_ERROR,
                dependency_scope=scope,
                source=SOURCE,
                detail=detail,
            )
        if kind == client.PROVIDER_ERROR_MODEL_UNAVAILABLE:
            return DependencySignal(
                kind=KIND_DEPENDENCY_UNAVAILABLE,
                dependency_scope=scope,
                source=SOURCE,
                detail=detail,
            )
        # A named quota exception is a concurrency ceiling, not a rate limit;
        # the client folds it into its throttle family, so it is told apart
        # here before that family is mapped.
        if _RE_QUOTA_NAMED.search(haystack):
            return DependencySignal(
                kind=KIND_CONCURRENCY_EXCEEDED,
                dependency_scope=scope,
                source=SOURCE,
                detail=detail,
            )
        if kind == client.PROVIDER_ERROR_THROTTLE:
            return DependencySignal(
                kind=KIND_RATE_LIMITED,
                dependency_scope=scope,
                source=SOURCE,
                retry_at=_retry_at_in_text(haystack, now),
                detail=detail,
            )
        if kind == client.PROVIDER_ERROR_CREDENTIAL_PROPAGATION:
            return DependencySignal(
                kind=KIND_DEPENDENCY_UNAVAILABLE,
                dependency_scope=scope,
                source=SOURCE,
                detail=detail,
            )
        if kind in (client.PROVIDER_ERROR_AUTH, client.PROVIDER_ERROR_SESSION_EXPIRED):
            return DependencySignal(
                kind=KIND_AUTH_FAILED, dependency_scope=scope, source=SOURCE, detail=detail
            )
        if kind in (client.PROVIDER_ERROR_CONNECTION, client.PROVIDER_ERROR_HTTP_5XX):
            return DependencySignal(
                kind=KIND_DEPENDENCY_UNAVAILABLE,
                dependency_scope=scope,
                source=SOURCE,
                detail=detail,
            )
    status = _http.status_of(_Text(haystack))
    if status is not None:
        signal = _http.signal_for_status(
            status, scope=scope, source=SOURCE, retry_at=None, detail=detail
        )
        if signal is not None:
            return signal
    if transient_hint is True:
        return DependencySignal(
            kind=KIND_DEPENDENCY_UNAVAILABLE, dependency_scope=scope, source=SOURCE, detail=detail
        )
    return None


class _Text(Exception):
    """Wraps a string so :func:`http.status_of` can read its status token."""


def _retry_at_in_text(haystack: str, now: float | None) -> float | None:
    match = _http._RE_RETRY_AFTER_TEXT.search(haystack)
    if match is None:
        return None
    base = time.time() if now is None else now
    return base + float(int(match.group(1)))


def classify_raw_error(error: object, *, scope: str = SCOPE_DEFAULT) -> DependencySignal | None:
    """A raw JSON-RPC ``{code, message, data}`` error frame -> signal."""
    if not isinstance(error, dict):
        return None
    data = str(error.get("data", "") or "")
    message = str(error.get("message", "") or "")
    client = _client()
    hint = (
        client.classify_provider_error(f"{data} {message}", data=data).retryable
        if client is not None
        else None
    )
    return classify_text(f"{data} {message}", data=data, scope=scope, transient_hint=hint)


def is_acp_error(exc: BaseException) -> bool:
    client = _client()
    if client is not None and isinstance(exc, client.AcpError):
        return True
    return type(exc).__name__.startswith("Acp") and hasattr(exc, "transient")


def classify(exc: BaseException, scope: str = "") -> DependencySignal | None:
    """Adapter entry: only :class:`AcpError` (and duck-typed ``Acp*`` exceptions)."""
    if not is_acp_error(exc):
        return None
    effective_scope = scope_for(exc, scope)
    if getattr(exc, "auth_required", False):
        return DependencySignal(
            kind=KIND_AUTH_FAILED,
            dependency_scope=effective_scope,
            source=SOURCE,
            detail=str(exc)[:200],
        )
    transient = getattr(exc, "transient", None)
    return classify_text(
        str(exc),
        scope=effective_scope,
        transient_hint=transient if isinstance(transient, bool) else None,
        now=_http.observed_now(exc),
    )


__all__ = [
    "SCOPE_DEFAULT",
    "SOURCE",
    "classify",
    "classify_raw_error",
    "classify_text",
    "is_acp_error",
    "scope_for",
]
