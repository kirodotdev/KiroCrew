"""The ``fargate`` transport: an SSM port-forward aimed at an ECS task's turn API.

The child process is the same SSM port-forward the ``ssm`` transport spawns; what
differs is what sits at the other end. The task runs no Kiro Crew gateway, so two
operations that exist for every other transport do not exist here — minting a
dashboard token and restarting a remote gateway — and both are refused before a
command line is built rather than dispatched and failed remotely.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.instances.constants import (
    DEFAULT_MINT_TIMEOUT_SECS,
    DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.registry import Instance
from kiro_crew.instances.token_mint import TokenMintError
from kiro_crew.instances.transports.base import (
    ChildForwardTransport,
    TransportRefusal,
    TransportSeams,
)
from kiro_crew.instances.transports.params import TransportParams
from kiro_crew.instances.validation import (
    SsmValidationError,
    split_ecs_target,
    validate_aws_profile,
    validate_aws_region,
    validate_ssm_target,
)


class FargateTransport(ChildForwardTransport):
    """Reaches an ECS task's turn API over an SSM port-forward."""

    method = "fargate"
    #: No dashboard, so no token: this is what the connect path and self-heal
    #: tier 2 test instead of comparing the method name to ``"fargate"``.
    mints_token = False
    connect_timeout_default = DEFAULT_SSM_CONNECT_TIMEOUT_SECS
    #: Unused (this transport never mints), and deliberately the plain default
    #: rather than the SSM one, so the manager's mint-timeout answer for a
    #: fargate instance is unchanged by the move into this class.
    mint_timeout_default = DEFAULT_MINT_TIMEOUT_SECS

    def validate(self, inst: Instance) -> TransportParams:
        target = validate_ssm_target(inst.ssm_target)
        # validate_ssm_target admits every SSM target shape; this method only
        # forwards to an ECS task, so an EC2 id is refused here rather than
        # handed to a forward that would reach a box with no turn API.
        if split_ecs_target(target) is None:
            raise SsmValidationError(
                f"ssm_target {target!r} must be an ECS task target "
                f"(ecs:<cluster>_<task-id>_<runtime-id>) for a fargate instance"
            )
        return TransportParams(
            method="fargate",
            ssm_target=target,
            aws_profile=validate_aws_profile(inst.aws_profile),
            aws_region=validate_aws_region(inst.aws_region),
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
        """Refuse: every mint path funnels here, so nothing can reach an ECS task."""
        raise TokenMintError(
            "a fargate instance has no dashboard token: the task serves only its "
            "turn API, reached at the tunnel's turn_url"
        )

    async def diagnose(
        self,
        inst: Instance,
        *,
        local_port: int,
        connect_timeout_secs: float,
        seams: TransportSeams,
    ) -> Any:
        return await seams.diagnose_fargate(
            inst.ssm_target,
            local_port,
            aws_profile=inst.aws_profile,
            aws_region=inst.aws_region,
        )

    async def restart(
        self,
        inst: Instance,
        params: TransportParams,
        *,
        timeout_secs: float,
        seams: TransportSeams,
    ) -> tuple[int, str | None]:
        """Refuse before any command is built: there is no gateway at the far end."""
        raise TransportRefusal(
            "a fargate instance runs no Kiro Crew gateway to restart; "
            "stop and relaunch the task instead"
        )
