"""The AWS Systems Manager (SSM) path into a Crew: what this machine is, and what to run.

A teammate with AWS access to this machine opens a Session Manager port forward
and talks to a loopback listener (``dashboard.ssm_gate``). This module holds the
parts that need no server: finding this machine's managed-node id, noting whether
the listener runs, and building the command and link the owner sends.

Nothing here reads or writes the user's AWS configuration, credentials or
profiles, and nothing here starts a process.
"""

from __future__ import annotations

import http.client
import os
import re
import shlex
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

STATE_OFF = "off"
STATE_ACTIVE = "active"
STATE_UNAVAILABLE = "unavailable"

REASON_DISABLED = "disabled"
REASON_NOT_STARTED = "not_started"
REASON_NO_TARGET = "no_target"
REASON_PORT_IN_USE = "port_in_use"
REASON_BIND_FAILED = "bind_failed"
REASON_GOVERNED = "governed"

#: Operator overrides for a machine without instance metadata (a hybrid managed
#: node, or a container whose metadata hop limit is 1).
ENV_TARGET = "KIROCREW_SSM_TARGET"
ENV_REGION = "KIROCREW_SSM_REGION"

#: An EC2 instance id (``i-``) or a hybrid managed-node id (``mi-``).
_TARGET_RE = re.compile(r"\A(i|mi)-[0-9a-f]{8,17}\Z")
_REGION_RE = re.compile(r"\A[a-z]{2}(-[a-z]+)+-[0-9]\Z")

_IMDS_HOST = "169.254.169.254"
_IMDS_TIMEOUT_SECS = 1.0
_IMDS_TOKEN_TTL_SECS = "60"


@dataclass(frozen=True)
class Target:
    """The managed node a teammate's session connects to."""

    instance_id: str
    region: str = ""


@dataclass(frozen=True)
class Listener:
    """The SSM listener as the gateway started it."""

    state: str = STATE_OFF
    reason: str = REASON_NOT_STARTED
    port: int = 0
    target: Target | None = None

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE


_LOCK = threading.Lock()
_listener = Listener()


def note_listener(answer: Listener) -> None:
    """Record what the SSM listener resolved to at gateway start."""
    global _listener
    with _LOCK:
        _listener = answer


def listener() -> Listener:
    with _LOCK:
        return _listener


def _reset_for_tests() -> None:
    note_listener(Listener())


def valid_target(value: object) -> bool:
    return isinstance(value, str) and bool(_TARGET_RE.match(value))


def valid_region(value: object) -> bool:
    return isinstance(value, str) and bool(_REGION_RE.match(value))


def _imds_get(
    conn: http.client.HTTPConnection, method: str, path: str, headers: Mapping[str, str]
) -> str:
    conn.request(method, path, headers=dict(headers))
    response = conn.getresponse()
    body = response.read(256).decode("ascii", "replace").strip()
    if response.status != 200:
        raise OSError(f"instance metadata answered {response.status}")
    return body


def read_imds() -> Target | None:
    """This machine's instance id and region from IMDSv2, or ``None`` off EC2. Blocking."""
    conn = http.client.HTTPConnection(_IMDS_HOST, 80, timeout=_IMDS_TIMEOUT_SECS)
    try:
        token = _imds_get(
            conn,
            "PUT",
            "/latest/api/token",
            {"X-aws-ec2-metadata-token-ttl-seconds": _IMDS_TOKEN_TTL_SECS},
        )
        auth = {"X-aws-ec2-metadata-token": token}
        instance_id = _imds_get(conn, "GET", "/latest/meta-data/instance-id", auth)
        try:
            region = _imds_get(conn, "GET", "/latest/meta-data/placement/region", auth)
        except (OSError, http.client.HTTPException):
            region = ""
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if not valid_target(instance_id):
        return None
    return Target(instance_id, region if valid_region(region) else "")


def detect_target(
    environ: Mapping[str, str] | None = None,
    imds: Callable[[], Target | None] = read_imds,
) -> Target | None:
    """The managed node to connect to: the operator's override, else instance metadata. Blocking."""
    env = os.environ if environ is None else environ
    region = env.get(ENV_REGION) or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or ""
    region = region.strip()
    override = (env.get(ENV_TARGET) or "").strip()
    if override:
        if not valid_target(override):
            return None
        return Target(override, region if valid_region(region) else "")
    found = imds()
    if found is None:
        return None
    if not found.region and valid_region(region):
        return Target(found.instance_id, region)
    return found


def session_document(port: int) -> str:
    """The custom Session document an owner creates for this port (the guide gives its content).

    It fixes ``portNumber`` to the SSM port, so IAM can allow ``ssm:StartSession`` on this
    document alone and a teammate cannot forward any other port. The invite command names
    it, and the guide tells the owner to create exactly this name.
    """
    return f"Kiro-Crew-Shared-Crew-{port}"


def start_session_args(target: Target, port: int) -> list[str]:
    """The argument list a teammate runs to forward ``localhost:<port>`` to this machine's SSM port.

    The document and parameters come from the one builder every port forward uses.
    The command runs on the teammate's machine, so its head is plain ``aws`` rather
    than the path resolved for this one.
    """
    from kiro_crew.cloud.ssm import build_port_forward_argv

    return [
        "aws",
        *build_port_forward_argv(
            target.instance_id,
            port,
            port,
            region=target.region,
            document=session_document(port),
        )[1:],
    ]


def start_session_command(target: Target, port: int) -> str:
    return shlex.join(start_session_args(target, port))


def local_url(port: int) -> str:
    """Where the teammate's browser opens once the forward is up."""
    return f"http://localhost:{port}/"


def invite_url(port: int, credential: str) -> str:
    return f"{local_url(port)}?ssm_invite={credential}"


__all__ = [
    "ENV_REGION",
    "ENV_TARGET",
    "Listener",
    "REASON_BIND_FAILED",
    "REASON_DISABLED",
    "REASON_GOVERNED",
    "REASON_NOT_STARTED",
    "REASON_NO_TARGET",
    "REASON_PORT_IN_USE",
    "STATE_ACTIVE",
    "STATE_OFF",
    "STATE_UNAVAILABLE",
    "Target",
    "detect_target",
    "invite_url",
    "listener",
    "local_url",
    "note_listener",
    "read_imds",
    "session_document",
    "start_session_args",
    "start_session_command",
    "valid_region",
    "valid_target",
]
