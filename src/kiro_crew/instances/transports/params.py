"""Validated, transport-specific connection parameters for one instance.

Lives here rather than in ``ssh_tunnel_manager`` so the transports can return it
without importing the manager that calls them. ``ssh_tunnel_manager`` re-exports
it as ``_TransportParams``, the name its existing callers and tests already use.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.cloud.connect import FARGATE_TURN_PATH
from kiro_crew.instances.registry import SSM_TRANSPORT_METHODS

_LOOPBACK = "127.0.0.1"


@dataclass
class TransportParams:
    """Validated, transport-specific connection parameters for one instance.

    Resolved once by :meth:`SshTunnelManager._resolve_transport` — which now
    delegates to the resolved transport's ``validate`` — so the connect /
    rebuild / self-heal / token-refresh paths all build their tunnel and mint
    their token from the same validated values instead of each re-branching on
    ``connection_method``.
    """

    method: str  # "ssh" | "ssm" | "fargate"
    ssh_host: str = ""
    remote_bin: str = ""
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    ssm_run_as: str = ""

    @property
    def target(self) -> str:
        """The human-facing target (ssh host, SSM instance id or ECS task) for messages."""
        return self.ssm_target if self.method in SSM_TRANSPORT_METHODS else self.ssh_host

    @property
    def forwards_over_ssm(self) -> bool:
        """Whether the forwarder child is ``aws ssm start-session``."""
        return self.method in SSM_TRANSPORT_METHODS

    def tunnel_kwargs(self) -> dict:
        """Transport kwargs for the ``_SshTunnel`` constructor.

        The tunnel child knows two argv shapes, ssh and the SSM port-forward. A
        fargate instance's child IS the SSM port-forward (aimed at an ECS task), so
        it is handed the ``ssm`` transport; what differs for fargate lives on the
        manager (no mint, no remote ``kirocrew``), not in the child.

        This is the single source of truth for the child's shape:
        :meth:`PeerTransport.open` returns it rather than restating it, so a
        transport cannot drift from the value object the spawn site consumes.
        """
        return {
            "transport": "ssm" if self.forwards_over_ssm else "ssh",
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
        }

    def turn_url(self, local_port: int) -> str:
        """The local turn-API URL for a fargate forward, ``""`` otherwise."""
        if self.method != "fargate":
            return ""
        return f"http://{_LOOPBACK}:{local_port}{FARGATE_TURN_PATH}"
