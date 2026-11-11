"""The HTTP session a metrics OTLP exporter sends through.

Every exporter gets a session that follows no redirect. The ``otlp_hosts`` policy
and the https rule check the configured endpoint only, and a 307/308 would resend
the batch and its headers to a host neither checked. ``requests`` raises
``TooManyRedirects`` before following one, which the exporter logs once, without
header values, and does not retry.

Imported by :mod:`kiro_crew.metrics.provider` at module scope, so nothing here
imports ``requests`` or the OTel SDK until a reader is built.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: The SDK's credential-provider variables, generic first (the order it reads them).
_CREDENTIAL_PROVIDER_VARS = (
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER",
)


def export_session(supplied: Any, own_only: bool) -> Any:
    """The no-redirect session for one destination's exporter.

    - *supplied* (a destination built in code with ``session``): a copy of it,
      so neither the redirect rule nor the exporter's header updates change the
      edition's object (:func:`session_copy`).
    - *own_only* (a ``telemetry.otlp_endpoints`` entry): a plain session of our
      own, so the exporter does not load the OTel credential-provider session:
      that variable is process-wide, like the header variables.
    - otherwise (the legacy ``telemetry.otlp_endpoint``): the session the
      exporter would pick for itself, the credential-provider one when that
      variable names one. The SDK's loader is private: when an SDK release drops
      it, the destination keeps exporting through a plain session and one
      WARNING says so (:func:`_legacy_session`).
    """
    import requests

    if supplied is not None:
        session = session_copy(supplied)
    elif own_only:
        session = requests.Session()
    else:
        session = _legacy_session()
    session.max_redirects = 0
    return session


def _legacy_session() -> Any:
    """The credential-provider session the SDK would load, else a plain one."""
    import requests

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            _load_session_from_envvar,
        )
    except ImportError:
        named = [var for var in _CREDENTIAL_PROVIDER_VARS if os.environ.get(var)]
        if named:
            logger.warning(
                "OTLP metrics exporter: this SDK has no credential-provider session "
                "loader, so the session %s names was NOT loaded; the legacy "
                "telemetry.otlp_endpoint exports with a plain session",
                named[0],
            )
        else:
            logger.warning(
                "OTLP metrics exporter: this SDK has no credential-provider session "
                "loader; the legacy telemetry.otlp_endpoint exports with a plain session"
            )
        return requests.Session()
    return (
        _load_session_from_envvar("OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER")
        or requests.Session()
    )


def session_copy(source: Any) -> Any:
    """A shallow copy of *source*: same class, same state, its own headers.

    The class is kept, so a subclass that signs requests by overriding
    ``request``/``send`` still signs them. Every attribute, the subclass's own
    included, is carried over as-is (``auth``, adapters and cookies stay the
    edition's objects, as when the session itself was handed over). Only
    ``headers`` is a new dict, because the exporter updates it at construction.
    ``max_redirects`` is a plain int, so the caller setting it on the copy leaves
    *source* alone.

    Not ``copy.copy``: ``requests.Session.__getstate__`` keeps only the
    attributes ``requests`` defines, which drops a subclass's own state.
    """
    session = object.__new__(type(source))
    session.__dict__.update(vars(source))
    session.headers = source.headers.copy()
    return session
