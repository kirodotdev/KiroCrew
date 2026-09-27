"""The ``ssh`` transport: a supervised ``ssh -N -L`` child to the peer's dashboard."""

from __future__ import annotations

from typing import Any

from kiro_crew.instances.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECS,
    DEFAULT_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.registry import Instance
from kiro_crew.instances.transports.base import ChildForwardTransport, TransportSeams
from kiro_crew.instances.transports.params import TransportParams
from kiro_crew.instances.validation import validate_remote_bin, validate_ssh_host


class SshTransport(ChildForwardTransport):
    """Reaches the peer over SSH, using the operator's own SSH configuration."""

    method = "ssh"
    mints_token = True
    connect_timeout_default = DEFAULT_CONNECT_TIMEOUT_SECS
    mint_timeout_default = DEFAULT_MINT_TIMEOUT_SECS

    def validate(self, inst: Instance) -> TransportParams:
        return TransportParams(
            method="ssh",
            ssh_host=validate_ssh_host(inst.ssh_host),
            remote_bin=validate_remote_bin(inst.remote_bin),
        )

    def describe_target(self, params: TransportParams) -> str:
        return params.ssh_host

    async def mint(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        parent_port: int | None,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> str:
        return await seams.mint_ssh(
            params.ssh_host,
            remote_bin=params.remote_bin,
            ttl=inst.ttl,
            remote_port=inst.remote_port,
            embed_parent_port=parent_port,
            timeout_secs=timeout_secs,
        )

    async def diagnose(
        self,
        inst: Instance,
        *,
        local_port: int,
        connect_timeout_secs: float,
        seams: TransportSeams,
    ) -> Any:
        return await seams.diagnose_ssh(
            inst.ssh_host,
            inst.remote_port,
            local_port,
            connect_timeout_secs=connect_timeout_secs,
        )

    async def restart(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> tuple[int, str | None]:
        return await seams.restart_ssh(
            params.ssh_host,
            "restart",
            remote_bin=params.remote_bin,
            marker_port=inst.remote_port,
            connect_timeout_secs=timeout_secs,
        )
