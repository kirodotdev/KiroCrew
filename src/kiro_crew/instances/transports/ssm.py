"""The ``ssm`` transport: an ``aws ssm start-session`` port-forward to an EC2 peer."""

from __future__ import annotations

from typing import Any

from kiro_crew.instances.constants import (
    DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
    DEFAULT_SSM_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.registry import Instance
from kiro_crew.instances.transports.base import ChildForwardTransport, TransportSeams
from kiro_crew.instances.transports.params import TransportParams
from kiro_crew.instances.validation import (
    SsmValidationError,
    split_ecs_target,
    validate_aws_profile,
    validate_aws_region,
    validate_remote_bin,
    validate_ssm_run_as,
    validate_ssm_target,
)


class SsmTransport(ChildForwardTransport):
    """Reaches the peer over SSM: no inbound port and no SSH key, IAM only.

    Both timeout defaults are higher than SSH's: the ``session-manager-plugin``
    completes a WebSocket handshake before it binds the local port, and the mint
    dispatches ``aws ssm send-command`` and polls for its invocation.
    """

    method = "ssm"
    mints_token = True
    connect_timeout_default = DEFAULT_SSM_CONNECT_TIMEOUT_SECS
    mint_timeout_default = DEFAULT_SSM_MINT_TIMEOUT_SECS

    def validate(self, inst: Instance) -> TransportParams:
        target = validate_ssm_target(inst.ssm_target)
        # Connect-time mirror of the registry's ssm arm, for records stored
        # before the registry refused them: an ECS task has no SSM agent to
        # run ``kirocrew token`` on, so forwarding it would only fail later
        # at the mint with a generic error. Refuse here and name the method
        # that owns the target.
        if split_ecs_target(target) is not None:
            raise SsmValidationError(
                f"ssm_target {target!r} is an ECS task target; it belongs to the "
                f"fargate connection method, not ssm"
            )
        return TransportParams(
            method="ssm",
            ssm_target=target,
            aws_profile=validate_aws_profile(inst.aws_profile),
            aws_region=validate_aws_region(inst.aws_region),
            ssm_run_as=validate_ssm_run_as(inst.ssm_run_as),
            remote_bin=validate_remote_bin(inst.remote_bin),
        )

    def describe_target(self, params: TransportParams) -> str:
        return params.ssm_target

    async def mint(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        parent_port: int | None,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> str:
        return await seams.mint_ssm(
            params.ssm_target,
            aws_profile=params.aws_profile,
            aws_region=params.aws_region,
            ssm_run_as=params.ssm_run_as,
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
        """Probe the SSM ladder, which carries its own budget: no dial timeout applies."""
        return await seams.diagnose_ssm(
            inst.ssm_target,
            inst.remote_port,
            local_port,
            aws_profile=inst.aws_profile,
            aws_region=inst.aws_region,
            ssm_run_as=inst.ssm_run_as,
        )

    async def restart(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> tuple[int, str | None]:
        """Dispatch ``kirocrew restart`` over SSM, which carries its own budget."""
        return await seams.restart_ssm(
            params.ssm_target,
            "restart",
            aws_profile=params.aws_profile,
            aws_region=params.aws_region,
            ssm_run_as=params.ssm_run_as,
            remote_bin=params.remote_bin,
            marker_port=inst.remote_port,
        )
