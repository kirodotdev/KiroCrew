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
metrics go, with or without a collector of its own) or on a CI runner. A
non-default ``KIROCREW_HOME`` (a dev home, a pod, a worktree preview) gets it
like any other home; the daily heartbeat keeps its own default-home rule.

Turning recording ON never needs a config-file edit because of it: the notice
already discloses it, so the dashboard and ``kirocrew telemetry enable`` refuse
only for the operator's own destinations (:func:`egress_needs_written_opt_in`).

What it sends, narrowed by the core (``metrics/destination_filter.py``):

* ``allow=PRODUCT_METRICS`` -- the reviewed list; every other metric is dropped.
* ``point_attributes={"ClientType": "kiro-crew", "machineId": <hash>}`` -- the
  receiving side routes each data point on ``ClientType`` and refuses one
  without it. ``machineId`` is the same hash the header carries (below); the
  endpoint does not copy the header onto the points it stores, and a stored
  point with no identity is dropped before it reaches product analytics. It is
  left out while there is no install id, when no request is made anyway.
* ``resource_allow=RESOURCE_ALLOW`` -- no install id, pid, OS type or ``host.*`` key.
* ``temporality="delta"`` and ``process_env_credentials=False`` -- the
  process-wide OTel header, credential and TLS variables are for an operator's
  own collector and are never applied here.

Identity, added per request by the session: ``x-kiro-machineid``, a SHA-256 of
the existing install id under a fixed prefix, which the endpoint accepts for
anonymous ingestion. Never creates the id; without it no request is made. No
sign-in token or other credential is sent. The hash is never logged or written
to config; it is the only identity on the descriptor (the ``machineId`` point
attribute), and the raw install id never leaves the machine.
"""

from __future__ import annotations

import dataclasses
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

#: Point attribute carrying the same hash as :data:`HEADER_MACHINE_ID`.
MACHINE_ID_KEY = "machineId"

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
    built once per process and opens no socket until the first export, and the
    install id is only read, never created (:func:`machine_id_hash`).
    """
    from kiro_crew import beacon, edition

    if _suppressed_for_tests or edition.is_lts() or beacon.is_ci():
        return ()
    from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
    from kiro_crew.platform.interfaces import OtlpDestination

    point_attributes = {CLIENT_TYPE_KEY: CLIENT_TYPE_VALUE}
    machine_id = machine_id_hash()
    if machine_id:
        point_attributes[MACHINE_ID_KEY] = machine_id
    return (
        OtlpDestination(
            name=DESTINATION_NAME,
            endpoint=ENDPOINT,
            signals=frozenset({"metrics"}),
            session=_shared_session(),
            allow=PRODUCT_METRICS,
            point_attributes=MappingProxyType(point_attributes),
            resource_allow=RESOURCE_ALLOW,
            temporality="delta",
            process_env_credentials=False,
        ),
    )


def is_builtin(dest: object) -> bool:
    """Whether *dest* is this destination (not an operator entry of the same name).

    Compared without the ``machineId`` point attribute, which appears once the
    install id exists: a descriptor built before that and one built after are
    the same destination. The shared session still tells an operator entry apart.
    """
    probe = _without_machine_id(dest)
    return any(probe == _without_machine_id(own) for own in destinations())


def _without_machine_id(dest: object) -> object:
    attrs = getattr(dest, "point_attributes", None)
    if not attrs or MACHINE_ID_KEY not in attrs or not dataclasses.is_dataclass(dest):
        return dest
    kept = {key: value for key, value in attrs.items() if key != MACHINE_ID_KEY}
    return dataclasses.replace(dest, point_attributes=MappingProxyType(kept))  # type: ignore[type-var]


def notice_shown(full_cfg: Any = None) -> bool:
    """Whether the person was shown the notice that names this destination.

    ``dashboard.kiro_telemetry_ack_version``, which the dashboard chapter and
    ``kirocrew telemetry enable`` write only when their text named this
    destination (``privacy_ack_version`` alone does not count: a chapter whose
    text did not name it writes that too), or the version whose Kiro telemetry
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


def off_offer_due(full_cfg: Any = None) -> bool:
    """Whether recording is off only by a written ``telemetry.enabled: false``.

    Builds before recording was on by default wrote that ``false`` into every new
    config, so most such installs never chose it. Those people are shown the
    notice that names this destination once, in an off form that asks whether to
    turn recording on; until they do, only the heartbeat and app install receipts go. Not due for
    any other reason recording is off (``DO_NOT_TRACK``, ``KIROCREW_TELEMETRY``
    or another env pin, a policy, the heartbeat turned off, a ``config.local.json``
    entry), on a non-default home, where this destination is not listed (LTS, CI),
    when turning recording on would also export to a collector of the person's
    own, or once the current notice is acknowledged. Fails closed.
    """
    try:
        from kiro_crew import beacon
        from kiro_crew.config.loader import (
            KiroCrewConfig,
            _stored_agent_value,
            overlay_pins,
        )
        from kiro_crew.metrics.consent_pins import env_pin

        cfg = full_cfg if full_cfg is not None else KiroCrewConfig.load()
        # Its own home check, not only destinations(): dev homes export, but are not asked.
        if not destinations() or not beacon.is_default_home() or env_pin() is not None:
            return False
        # env_pin() lets the heartbeat env opt-out step aside once enabled is written; still not asked.
        if beacon.is_env_opted_out():
            return False
        if cfg.dashboard.privacy_ack_version >= beacon.PRIVACY_NOTICE_VERSION:
            return False
        if cfg.telemetry.beacon_enabled is not True or beacon.is_governance_pinned_off():
            return False
        if _stored_agent_value("enabled", section="telemetry") is not False:
            return False
        if overlay_pins("telemetry", "enabled"):
            return False
        return not egress_needs_written_opt_in(cfg.telemetry)
    except Exception as exc:  # noqa: BLE001 - unreadable means "not due"
        logger.debug("kiro_export: off notice state unreadable (%s)", type(exc).__name__)
        return False


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
