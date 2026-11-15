"""The Kiro telemetry destination the public build ships for product metrics.

The public build's telemetry provider (``platform.defaults.DefaultTelemetryProvider``)
returns :func:`destinations` after the operator's own entries. It is a destination
built in code: config cannot remove or repoint it, and ``label_sources`` lists it
in Settings > Privacy and in ``kirocrew telemetry status`` as included by the build.

When it exports (:func:`exported`, called by the recorder build):

* local recording is on, by default or by choice. It is the one destination that
  exports under the recording default: an operator's own entries still need
  ``telemetry.enabled: true`` written or ``KIROCREW_TELEMETRY=1``
  (``consent_pins.outbound_consented``);
* AND the person has been shown the privacy notice that names it
  (:func:`notice_shown`): the dashboard's Privacy chapter at
  :data:`NOTICE_VERSION` naming it (Keep on writes
  ``dashboard.kiro_telemetry_ack_version``, as does ``kirocrew telemetry
  enable``), or, on a start with no dashboard, the one-line notice naming it
  printed on a terminal (``metrics/notice.py`` stamps that separately). A
  ``telemetry.enabled: true`` written under an older notice, which said metrics
  stay on this machine, does not count.

Every switch that stops recording stops it: ``telemetry.enabled: false`` or any
non-bool, ``kirocrew telemetry disable``, ``KIROCREW_TELEMETRY=0``,
``DO_NOT_TRACK``, an upgrade that had turned the heartbeat off. A policy
``capabilities.telemetry`` ``otlp_hosts`` list can refuse its host.

It is not returned at all in an LTS build (the admin's policy decides where
metrics go, with or without a collector of its own), on a CI runner, or under a
non-default ``KIROCREW_HOME`` (a dev home, a pod, a worktree preview): the same
hosts the daily heartbeat does not count.

Turning recording ON never needs a config-file edit because of it: the notice
already discloses it, so the dashboard and ``kirocrew telemetry enable`` refuse
only for the operator's own destinations (:func:`egress_needs_written_opt_in`).

What it sends, narrowed by the core (``metrics/destination_filter.py``):

* ``allow=PRODUCT_METRICS`` -- the reviewed list; every other metric is dropped.
* ``point_attributes={"ClientType": "kiro-crew"}`` -- the receiving side routes
  each data point on it and refuses one without it.
* ``resource_allow=RESOURCE_ALLOW`` -- no install id, pid, OS type or ``host.*`` key.
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
#: install id), ``process.pid``, ``os.type`` (the Privacy panel says the operating
#: system is never sent) and every ``host.*`` key stay on the machine.
RESOURCE_ALLOW = frozenset(
    {
        "service.name",
        "service.version",
        "process.runtime.name",
        "process.runtime.version",
        "kirocrew.harness",
        "kirocrew.process.role",
    }
)

HEADER_MACHINE_ID = "x-kiro-machineid"

#: The privacy notice version that names this destination
#: (``beacon.PRIVACY_NOTICE_VERSION`` when it shipped). Nothing is exported until
#: the person was shown it; see :func:`notice_shown`.
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
    if not beacon.is_default_home():
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


def is_builtin(dest: object) -> bool:
    """Whether *dest* is this destination (not an operator entry of the same name)."""
    return any(dest == own for own in destinations())


def notice_shown(full_cfg: Any = None) -> bool:
    """Whether the person was shown the notice that names this destination.

    ``dashboard.kiro_telemetry_ack_version``, which the dashboard chapter and
    ``kirocrew telemetry enable`` write only when their text named this
    destination (``privacy_ack_version`` alone does not count: a chapter shown
    under a non-default home never named it), or the version whose Kiro telemetry
    line a start with no dashboard printed on a terminal
    (``notice.KIRO_NOTICE_STAMP_FILE``). Fails closed.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.metrics.notice import KIRO_NOTICE_STAMP_FILE, stamped_version

        cfg = full_cfg if full_cfg is not None else KiroCrewConfig.load()
        acked = cfg.dashboard.kiro_telemetry_ack_version
        if isinstance(acked, int) and acked >= NOTICE_VERSION:
            return True
        return stamped_version(KIRO_NOTICE_STAMP_FILE) >= NOTICE_VERSION
    except Exception as exc:  # noqa: BLE001 - unreadable means "not shown"
        logger.debug("kiro_export: notice state unreadable (%s)", type(exc).__name__)
        return False


#: Whether the last recorder build kept this destination, for the disclosure of
#: a recorder kept running while consent cannot be read (``live_recorder_state``).
_last_build_kept = False


def last_build_kept() -> bool:
    """Whether the last recorder build in this process exports to this destination."""
    return _last_build_kept


def exported(dests: "tuple[OtlpDestination, ...]", full_cfg: Any, outbound: Any) -> tuple:
    """The destinations a recorder build may export to, recording being on.

    This one once its notice was shown; every other one only with the written
    opt-in *outbound* stands for.
    """
    global _last_build_kept
    shown: "bool | None" = None
    kept = []
    for dest in dests:
        if not is_builtin(dest):
            if outbound:
                kept.append(dest)
            continue
        if shown is None:
            shown = notice_shown(full_cfg)
        if shown:
            kept.append(dest)
    _last_build_kept = bool(shown)
    return tuple(kept)


def egress_needs_written_opt_in(cfg: Any) -> bool:
    """Whether turning recording on would export somewhere the notice did not name.

    What the dashboard's ``telemetry.enabled`` switch and ``kirocrew telemetry
    enable`` refuse on: the operator's own destinations (or a build's other ones),
    not this one. Raises like ``provider.otlp_egress_active`` when posture cannot
    be established; callers treat that as egress.
    """
    from kiro_crew.metrics import provider

    if not provider.otlp_egress_active(cfg):
        return False
    rows = provider.otlp_egress_listing(cfg)
    # Egress with no row to inspect cannot be shown to be this one: refuse.
    return not rows or any(not is_builtin(row.destination) for row in rows)


def listed(rows: Any) -> bool:
    """Whether a posture listing (``otlp_egress_listing``) includes this destination."""
    return any(is_builtin(getattr(row, "destination", None)) for row in rows or ())


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
