"""The Kiro telemetry destination the public build ships for product metrics.

The public build's telemetry provider (``platform.defaults.DefaultTelemetryProvider``)
returns :func:`destinations` next to the operator's own entries, so an install
that has opted in to export sends the reviewed product metrics to the Kiro
telemetry endpoint. It is a destination built in code: config cannot remove or
repoint it, and ``label_sources`` lists it in Settings > Privacy and in
``kirocrew telemetry status`` as included by the build.

Every gate that stops any destination stops this one, unchanged:

* recording off -- ``telemetry.enabled: false``, ``kirocrew telemetry disable``,
  ``KIROCREW_TELEMETRY=0``, ``DO_NOT_TRACK`` -- builds no recorder at all;
* recording on only by default is not consent to export
  (``consent_pins.outbound_consented``), so a new install or an upgrade that never
  wrote ``telemetry.enabled: true`` sends nothing;
* the policy's ``capabilities.telemetry`` ``otlp_hosts`` list can refuse the host.

It is not returned at all (so the disclosure does not list it, and the Settings
switch and ``kirocrew telemetry enable`` keep working as they do with no
destination):

* until the person has acknowledged a privacy notice that names it
  (``dashboard.privacy_ack_version`` at least :data:`NOTICE_VERSION`). Every
  notice so far says metrics stay on this machine, so a ``telemetry.enabled:
  true`` written under one of them is consent to local recording only and must
  not start this export on upgrade (``consent_pins.outbound_consented`` says the
  same). The notice that names this destination bumps
  ``beacon.PRIVACY_NOTICE_VERSION`` to :data:`NOTICE_VERSION`;
* in an LTS build, where the admin's policy decides where metrics go;
* on a CI runner, or under a non-default ``KIROCREW_HOME`` (a dev home, a pod, a
  worktree preview): the same hosts the daily heartbeat does not count.

What it sends, narrowed by the core (``metrics/destination_filter.py``):

* ``allow=PRODUCT_METRICS`` -- the reviewed list; every other metric is dropped.
* ``point_attributes={"ClientType": "kiro-crew"}`` -- the receiving side routes
  each data point on it and refuses one without it.
* ``resource_allow=RESOURCE_ALLOW`` -- no install id, no pid, nothing under ``host.*``.
* ``temporality="delta"`` and ``process_env_credentials=False`` -- the
  process-wide OTel header, credential and TLS variables are for an operator's
  own collector and are never applied here.

Identity, added per request by the session: ``x-kiro-machineid``, a SHA-256 of
the existing install id under a fixed prefix, which the endpoint accepts for
anonymous ingestion. Never creates the id; without it no request is made. No
sign-in token or other credential is sent. The header value is never logged,
stored on the descriptor, or written to config.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import OtlpDestination

logger = logging.getLogger(__name__)

#: Non-secret label the core logs and the Privacy panel shows for this destination.
DESTINATION_NAME = "kiro-telemetry"

#: A constant: nothing in config or the environment can move it.
ENDPOINT = "https://telemetry.us-east-1.kiro.dev/v1/metrics"

CLIENT_TYPE_KEY = "ClientType"
CLIENT_TYPE_VALUE = "kiro-crew"

#: Resource keys this destination may receive. ``service.instance.id`` (the
#: install id), ``process.pid`` and every ``host.*`` key stay on the machine.
RESOURCE_ALLOW = frozenset(
    {
        "service.name",
        "service.version",
        "os.type",
        "process.runtime.name",
        "process.runtime.version",
        "kirocrew.harness",
        "kirocrew.process.role",
    }
)

HEADER_MACHINE_ID = "x-kiro-machineid"

#: The privacy notice version that names this destination. Nothing is returned
#: until the person has acknowledged it; see the module docstring.
NOTICE_VERSION = 3

_MACHINE_ID_PREFIX = "kirocrew-metrics-machine-id-v1:"

#: Set by the test suite (``test/conftest.py``) so no test can post to the real
#: endpoint. Not an environment variable on purpose: the off switches are the
#: telemetry controls above.
_suppressed_for_tests = False

_session: Any = None
_session_lock = threading.Lock()


def destinations() -> "tuple[OtlpDestination, ...]":
    """The Kiro telemetry destination as a one-element tuple, or ``()``.

    Cheap and side-effect free, as the provider seam requires: the session is
    built once per process and opens no socket until the first export.
    """
    from kiro_crew import beacon, edition

    if _suppressed_for_tests or edition.is_lts() or beacon.is_ci():
        return ()
    if not beacon.is_default_home() or not _notice_acknowledged():
        return ()
    from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
    from kiro_crew.platform.interfaces import OtlpDestination

    return (
        OtlpDestination(
            name=DESTINATION_NAME,
            endpoint=ENDPOINT,
            signals=frozenset({"metrics"}),
            session=_shared_session(),
            allow=PRODUCT_METRICS,
            point_attributes=MappingProxyType({CLIENT_TYPE_KEY: CLIENT_TYPE_VALUE}),
            resource_allow=RESOURCE_ALLOW,
            temporality="delta",
            process_env_credentials=False,
        ),
    )


def _notice_acknowledged() -> bool:
    """Whether the acknowledged privacy notice names this destination. Fails closed."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        acked = KiroCrewConfig.load().dashboard.privacy_ack_version
        return isinstance(acked, int) and acked >= NOTICE_VERSION
    except Exception as exc:  # noqa: BLE001 - unreadable means "not acknowledged"
        logger.debug("kiro_export: notice acknowledgement unreadable (%s)", type(exc).__name__)
        return False


def _shared_session() -> Any:
    global _session
    with _session_lock:
        if _session is None:
            _session = build_session()
        return _session


# ── identity ────────────────────────────────────────────────────────────────


def machine_id_hash() -> str:
    """SHA-256 of the existing install id under a fixed prefix, or ``""``."""
    try:
        from kiro_crew import beacon

        install_id = beacon.install_id(create=False)
    except Exception as exc:  # noqa: BLE001 - unreadable means "no id"
        logger.debug("kiro_export: install id unreadable (%s)", type(exc).__name__)
        return ""
    if not install_id:
        return ""
    return hashlib.sha256((_MACHINE_ID_PREFIX + install_id).encode("utf-8")).hexdigest()


def identity_headers() -> "dict[str, str]":
    """Headers for one export; ``{}`` means the batch is not sent."""
    machine_id = machine_id_hash()
    return {HEADER_MACHINE_ID: machine_id} if machine_id else {}


def _is_endpoint(url: object) -> bool:
    return str(url).rstrip("/") == ENDPOINT.rstrip("/")


def build_session() -> Any:
    """The transport for this destination. Opens no socket until first use.

    The core hands the exporter a copy with redirects off and ``.netrc`` blocked
    (``metrics/otlp_session.py``); this class adds the identity on each request,
    sends nothing to any URL but :data:`ENDPOINT`, and pins standard TLS
    verification with no client certificate.
    """
    import requests

    class KiroTelemetrySession(requests.Session):
        def request(self, method, url, *args, **kwargs):  # type: ignore[override]
            identity = identity_headers() if _is_endpoint(url) else {}
            if not identity:
                logger.debug("kiro_export: no machine id or not this endpoint; batch not sent")
                return _not_sent(url)
            headers = dict(kwargs.get("headers") or {})
            headers.update(identity)
            kwargs["headers"] = headers
            kwargs["allow_redirects"] = False
            kwargs["verify"] = True
            kwargs["cert"] = None
            return super().request(method, url, *args, **kwargs)

    def _not_sent(url: object) -> "requests.Response":
        response = requests.Response()
        response.status_code = 401
        response.reason = "not sent"
        response.url = str(url)
        response._content = b""
        return response

    return KiroTelemetrySession()
