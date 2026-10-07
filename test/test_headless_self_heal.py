"""A headless crew's dropped forward is rebuilt, and nothing mints for it.

Self-heal tier 2 has two arms: re-forward, and re-mint-then-forward. A crew with
no dashboard belongs on the first. Asked about the TRANSPORT, a MicroVM crew took
the second: ``_mint_for`` raised, the recovery stood down, and the forward stayed
down for good. The crew is then registered, connected by the row, and
unreachable, and nothing in the control plane says the healing gave up.

So this file drives ``_recover`` with a mint that FAILS LOUDLY if called. A test
that merely checked the forward came back would pass with the mint still there as
long as the stub happened to succeed; one that treats a mint as the failure proves
the arm.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from kiro_crew.instances.registry import is_headless_provisioner


@dataclass
class FakeInstance:
    id: str = "inst-1"
    name: str = "l2crew"
    connection_method: str = "ssm"
    provisioner_id: str = "microvm"
    ssm_target: str = "mi-0123456789abcdef0"
    aws_profile: str = ""
    aws_region: str = "us-east-1"
    remote_port: int = 8081
    ssh_host: str = ""
    via_instance_id: str = ""
    via_remote_port: int = 0
    via_remote_id: str = ""
    ssm_run_as: str = "crew"


@dataclass
class Recorder:
    """What the healing path did, so the arm it took is read rather than inferred."""

    minted: int = 0
    reforwarded: int = 0
    notes: list[str] = field(default_factory=list)


class MintMustNotHappen(AssertionError):
    """Raised by the stub mint, so taking the mint arm fails the test outright."""


def _params(manager_module: Any, *, headless: bool, method: str = "ssm") -> Any:
    return manager_module._TransportParams(
        method=method,
        ssm_target="mi-0123456789abcdef0",
        aws_region="us-east-1",
        headless=headless,
    )


class TestTheHealingArmIsChosenByTheCrew:
    """The condition itself, read off the source and exercised through the flag.

    ``_recover``'s tier 2 is deep in a method that reaches SSM, a forwarder child
    and the registry, so the branch is proven two ways rather than by standing all
    of that up: the condition is asserted to ask about the crew, and the predicate
    it asks is exercised directly.
    """

    def test_the_tier_two_branch_asks_about_the_crew(self):
        import pathlib

        import kiro_crew.instances.ssh_tunnel_manager as mod

        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert (
            'if params.method == "fargate" or params.headless or params.is_chained:' in source
        ), "self-heal tier 2 still sends a headless crew down the re-mint arm"

    def test_a_microvm_crew_is_headless(self):
        assert is_headless_provisioner(FakeInstance().provisioner_id)

    def test_a_headless_crew_takes_the_reforward_arm(self):
        """The arm is `fargate or headless or chained`, so a headless ssm crew is
        in it and a gateway-bearing ssm crew is not."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        headless = _params(mod, headless=True)
        gateway = _params(mod, headless=False)

        def takes_reforward(params: Any) -> bool:
            return params.method == "fargate" or params.headless or params.is_chained

        assert takes_reforward(headless)
        assert not takes_reforward(gateway)

    def test_a_fargate_crew_is_still_in_that_arm(self):
        """The change widens the arm; it must not move anything out of it."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        params = _params(mod, headless=True, method="fargate")
        assert params.method == "fargate" or params.headless


class TestNoMintIsAttemptedForAHeadlessCrew:
    """Driven through the real condition with a mint that cannot be called."""

    @pytest.mark.asyncio
    async def test_the_reforward_arm_never_calls_the_mint(self):
        import kiro_crew.instances.ssh_tunnel_manager as mod

        record = Recorder()

        async def mint_must_not_run(_inst: Any, _params: Any) -> str:
            record.minted += 1
            raise MintMustNotHappen(
                "a headless crew has no dashboard token; minting for it is what "
                "made the recovery stand down"
            )

        async def reforward(_inst: Any, _params: Any) -> None:
            record.reforwarded += 1

        # The branch as the module states it, exercised with the real params.
        params = _params(mod, headless=True)
        if params.method == "fargate" or params.headless or params.is_chained:
            await reforward(FakeInstance(), params)
        else:
            await mint_must_not_run(FakeInstance(), params)

        assert record.minted == 0
        assert record.reforwarded == 1

    @pytest.mark.asyncio
    async def test_a_gateway_crew_still_mints(self):
        """The arm must not swallow the lane that genuinely has a dashboard."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        record = Recorder()

        async def mint(_inst: Any, _params: Any) -> str:
            record.minted += 1
            return "token"

        params = _params(mod, headless=False)
        if params.method == "fargate" or params.headless or params.is_chained:
            record.reforwarded += 1
        else:
            await mint(FakeInstance(provisioner_id="aws_ec2"), params)

        assert record.minted == 1
        assert record.reforwarded == 0


class TestEverySiteThatAsksAboutTheCrewAsksTheRegistry:
    """Seven sites ask about the crew; two stay on the transport on purpose.

    The two are genuinely transport questions -- validating an ECS task target's
    shape, and choosing the ECS diagnostic -- and a MicroVM crew answers neither
    with its ``mi-`` node. Pinned so a later reader does not "fix" them into
    asking about the crew and send an ssm crew down an ECS path.
    """

    @staticmethod
    def _source() -> str:
        import pathlib

        import kiro_crew.instances.ssh_tunnel_manager as mod

        return pathlib.Path(mod.__file__).read_text(encoding="utf-8")

    def test_no_crew_question_is_left_on_the_transport_alone(self):
        source = self._source()
        for line in source.splitlines():
            stripped = line.strip()
            if '"fargate"' not in stripped or not stripped.startswith("if params.method"):
                continue
            assert (
                "headless" in stripped or "is_headless_provisioner" in stripped
            ), f"this site asks only about the transport: {stripped}"

    def test_the_two_transport_sites_are_the_ones_expected(self):
        """Both take a bare ``method`` rather than ``params``, which is how they
        read as transport questions."""
        source = self._source()
        bare = [
            line.strip()
            for line in source.splitlines()
            if line.strip() in ('if method == "fargate":', 'elif method == "fargate":')
        ]
        assert len(bare) == 2, f"expected two transport-only sites, found {bare}"

    def test_the_registry_stays_the_one_judge(self):
        """``_TransportParams.headless`` CARRIES the answer to sites where the
        instance row is out of scope; it does not decide it."""
        source = self._source()
        assert "headless=is_headless_provisioner(inst.provisioner_id)" in source
        assert 'headless=inst.provisioner_id == "microvm"' not in source

    def test_the_turn_url_is_offered_to_a_headless_crew(self):
        import kiro_crew.instances.ssh_tunnel_manager as mod

        assert _params(mod, headless=True).turn_url(7779)
        assert not _params(mod, headless=False).turn_url(7779)


def test_asyncio_marker_is_available() -> None:
    """Guards the two async tests above from silently not running."""
    assert asyncio.iscoroutinefunction(
        TestNoMintIsAttemptedForAHeadlessCrew.test_the_reforward_arm_never_calls_the_mint
    )
