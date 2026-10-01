"""Local System One models the decision seam can use instead of hosted Jev.

A local model is an open-weight decision server the owner runs on this machine,
speaking the same ``/v1/systemone`` wire format Jev does, so :class:`JevOracle`
talks to it unchanged. This module owns three things:

* the PRESETS the dashboard offers, each with the numbers a reader needs to
  choose one -- how close it comes to Jev, how much memory it needs, how slow it
  is on a CPU;
* :func:`is_loopback_endpoint`, the one test for "this endpoint is on this
  machine", which ``impl_jev`` uses to withhold the Jev key and the provider
  route uses to decide whether a switch may carry consent across;
* :func:`endpoint_for`, which builds a preset's endpoint from a port, so no
  caller ever composes a local URL from caller-supplied text.

The quality numbers are the share of Jev's correct answers each model matched on
the same 231 public JevBench items (easy, original and hard tiers), measured on a
10-core CPU. Jev answered 200 of them correctly (48/48, 71/72, 81/111); the hard
tier gets its own number because it is where the models separate. They are
measurements of one host, not guarantees, and the card says so.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import yarl

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT
from kiro_crew.security import canonicalize_ip

#: Preset id that means "hosted Jev at the shipped default endpoint".
PRESET_JEV = "jev"

#: The path every System One server answers on.
SYSTEMONE_PATH = "/v1/systemone"

#: Local ports the provider route accepts. Below 1024 needs privileges no model
#: server should run with; the upper bound is the TCP limit.
PORT_MIN = 1024
PORT_MAX = 65535


@dataclass(frozen=True)
class LocalModel:
    """One local model the dashboard offers."""

    id: str
    name: str
    #: Model id sent in the request; local servers route on it or ignore it.
    model: str
    default_port: int
    #: Correct answers matched, as a percentage of Jev's, over all public items.
    jev_relative_pct: int
    #: The same ratio on the hard tier alone.
    hard_relative_pct: int
    #: Peak resident memory of the server, measured, in GB.
    peak_ram_gb: float
    #: Total machine memory at or above which the dashboard recommends this
    #: preset. The card picks the first preset, in :data:`LOCAL_MODELS` order,
    #: whose threshold the machine meets, and hosted Jev when none is met.
    recommended_total_ram_gb: int
    #: Median seconds per decision on 10 CPU cores.
    p50_secs: float
    #: 95th-percentile seconds per decision on 10 CPU cores.
    p95_secs: float
    #: ``provider.timeout_ms`` written when the preset is chosen. Each decision
    #: point still clamps its own wait, so a slow answer degrades to "no decision".
    timeout_ms: int
    #: Where the owner finds the setup steps.
    setup_doc: str
    #: The command that starts the server once it is installed.
    serve_command: str


_SETUP_DOC = (
    "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/decisions.md"
    "#running-a-model-on-this-machine"
)

#: Recommended first: the best quality at a memory cost most workstations have.
LOCAL_MODELS: tuple[LocalModel, ...] = (
    LocalModel(
        id="plumb-4b",
        name="Plumb-4B",
        model="plumb-4b",
        default_port=8102,
        jev_relative_pct=103,
        hard_relative_pct=109,
        peak_ram_gb=14.8,
        recommended_total_ram_gb=24,
        p50_secs=2.4,
        p95_secs=32.0,
        timeout_ms=5000,
        setup_doc=_SETUP_DOC,
        serve_command="python plumb_serve_cpu.py --port {port}",
    ),
    LocalModel(
        id="laya",
        name="Laya",
        model="english",
        default_port=8104,
        jev_relative_pct=67,
        hard_relative_pct=47,
        peak_ram_gb=6.0,
        recommended_total_ram_gb=12,
        p50_secs=0.17,
        p95_secs=0.51,
        timeout_ms=2000,
        setup_doc=_SETUP_DOC,
        serve_command="LAYA_HOST=127.0.0.1 LAYA_PORT={port} LAYA_DEVICE=cpu laya-serve",
    ),
)

_BY_ID = {m.id: m for m in LOCAL_MODELS}


def get(preset_id: object) -> LocalModel | None:
    """The preset named *preset_id*, or ``None`` for anything else."""
    return _BY_ID.get(preset_id) if isinstance(preset_id, str) else None


def endpoint_for(port: int) -> str:
    """The loopback endpoint of a local server on *port*. Raises on a bad port."""
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise ValueError("port out of range")
    return f"http://127.0.0.1:{port}{SYSTEMONE_PATH}"


def is_loopback_endpoint(endpoint: object) -> bool:
    """Whether *endpoint* is an http(s) URL on a LITERAL loopback address.

    Literal addresses only: ``localhost`` is a name, and a hosts file or resolver
    decides where a name goes, so it is not proof the request stays on this
    machine. User info in the authority is refused too, since it is how a URL
    that reads as loopback is made to parse as another host. A missing port is
    still local: it means the scheme's default port on the same address, and
    reading it as remote would hand the Jev key to whatever holds port 80 here.
    """
    if not isinstance(endpoint, str):
        return False
    try:
        parts = urlsplit(endpoint.strip())
        parts.port  # raises on an out-of-range or non-numeric port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return False
    # The host is read the way aiohttp dials it: ``yarl.URL.raw_host`` applies IDNA
    # normalisation, which turns ``127。0。0。1``, fullwidth dots and circled digits
    # into ``127.0.0.1``. Then the same canonicalisation the browser and link-unfurl
    # host checks use: every spelling the resolver reads as a number without a
    # lookup -- ``127.1``, ``0x7f.1``, ``2130706433``, ``[::ffff:127.0.0.1]`` --
    # becomes the dotted quad it reaches. A name is left as it is and so is never
    # resolved.
    try:
        dialed = yarl.URL(endpoint.strip()).raw_host or ""
        addr = ipaddress.ip_address(canonicalize_ip(dialed))
    except ValueError:
        return False
    # The unspecified address is dialled as this machine: connecting to 0.0.0.0 or
    # [::] reaches whatever listens on the local port, so it is local for the key.
    return addr.is_loopback or addr.is_unspecified


def _is_route_built(endpoint: object) -> bool:
    """Whether *endpoint* has exactly the shape :func:`endpoint_for` builds."""
    if not isinstance(endpoint, str):
        return False
    try:
        # ``urlsplit`` itself raises on an unbalanced ``[`` in a hand-written address.
        port = urlsplit(endpoint.strip()).port
    except ValueError:
        return False
    if port is None or not PORT_MIN <= port <= PORT_MAX:
        return False
    return endpoint.strip() == endpoint_for(port)


def active_id(endpoint: object, model: object) -> str:
    """Which preset the configured provider matches: a local id, ``jev``, or ``custom``.

    A preset is only ever the address the provider route builds itself; any other
    loopback spelling is a hand-written address and is reported as ``custom``, so
    the card keeps showing where it is set and that no key is sent to it.
    """
    if isinstance(endpoint, str) and endpoint.strip() == DECISION_PROVIDER_ENDPOINT_DEFAULT:
        return PRESET_JEV
    if _is_route_built(endpoint):
        for m in LOCAL_MODELS:
            if model == m.model:
                return m.id
    return "custom"


def as_payload(m: LocalModel) -> dict:
    """*m* as the JSON the dashboard reads."""
    return asdict(m)
