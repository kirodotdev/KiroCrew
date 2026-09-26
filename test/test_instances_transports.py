"""Tests for the ``PeerTransport`` seam (RFC: outbound instance transport, phase 2).

The seam exists so a fourth way of reaching a remote crew is an addition rather
than a fourth arm on every conditional. These tests pin the three properties that
make that true, and one that stops the move from quietly breaking the suite:

* the registry is EXHAUSTIVE and its keys are the shipped methods, so a new
  transport has to be registered deliberately;
* an unregistered method is REFUSED rather than silently treated as SSH;
* the per-method answers (capability, timeouts, refusals) come from the transport
  and not from a comparison at the call site;
* the manager module is still the substitution point for the outward calls the
  transports make, so existing tests can drive them without a remote.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from kiro_crew.instances.registry import CONNECTION_METHODS, Instance
from kiro_crew.instances.token_mint import TokenMintError
from kiro_crew.instances.transports import (
    DEFAULT_METHOD,
    FargateTransport,
    PeerTransport,
    SshTransport,
    SsmTransport,
    TransportParams,
    TransportRefusal,
    TransportSeams,
    UnknownTransportError,
    normalize_method,
    registered_methods,
    resolve_peer_transport,
)
from kiro_crew.instances.validation import SshValidationError, SsmValidationError

_TASK = "0123456789abcdef0123456789abcdef"
_ECS_TARGET = f"ecs:crews_{_TASK}_{_TASK}-1234567890"


def _seams(**over) -> TransportSeams:
    """A seam bundle whose every field fails loudly unless a test replaces it."""

    async def _unexpected(*a, **k):
        raise AssertionError("transport used a seam this test did not provide")

    fields = {
        "mint_ssh": _unexpected,
        "mint_ssm": _unexpected,
        "restart_ssh": _unexpected,
        "restart_ssm": _unexpected,
        "diagnose_ssh": _unexpected,
        "diagnose_ssm": _unexpected,
        "diagnose_fargate": _unexpected,
    }
    fields.update(over)
    return TransportSeams(**fields)


class TestRegistry:
    def test_registered_methods_are_exactly_the_shipped_connection_methods(self) -> None:
        """The registry and the record's accepted values are ONE list, not two.

        Asserted against ``CONNECTION_METHODS`` rather than a literal tuple
        repeated here: a method a record may carry but no transport owns is
        unreachable, and a transport no record may name is dead. Phase 3 has to
        extend both together.
        """
        assert set(registered_methods()) == set(CONNECTION_METHODS)
        assert registered_methods() == ("ssh", "ssm", "fargate")

    def test_every_registered_transport_satisfies_the_protocol(self) -> None:
        for method in registered_methods():
            transport = resolve_peer_transport(method)
            assert isinstance(transport, PeerTransport)
            assert transport.method == method

    def test_an_unregistered_method_is_refused_rather_than_treated_as_ssh(self) -> None:
        """The fail-closed half of the seam.

        The conditional this replaced ended in an ``else`` that returned SSH
        params, so an unmapped method inherited SSH's addressing, mint and
        diagnosis. That is the defect class this RFC documents on the dashboard
        side; here it is refused, and the message names the method.
        """
        with pytest.raises(UnknownTransportError) as excinfo:
            resolve_peer_transport("outbound")
        assert "outbound" in str(excinfo.value)

    def test_the_refusal_is_catchable_as_either_validation_error(self) -> None:
        """Every resolve site already catches these two; the refusal must land there.

        Otherwise a record with an unmapped method escapes a per-instance error
        message and becomes an unhandled 500.
        """
        with pytest.raises(SshValidationError):
            resolve_peer_transport("outbound")
        with pytest.raises(SsmValidationError):
            resolve_peer_transport("outbound")

    @pytest.mark.parametrize("absent", [None, "", "   "])
    def test_an_absent_method_still_means_ssh(self, absent: str | None) -> None:
        """Absent and unrecognized are DIFFERENT: records predate the field."""
        assert normalize_method(absent) == DEFAULT_METHOD
        assert resolve_peer_transport(absent).method == "ssh"

    def test_a_method_is_matched_case_and_space_insensitively(self) -> None:
        assert resolve_peer_transport("  SSM  ").method == "ssm"


class TestCapabilities:
    def test_only_the_gateway_bearing_transports_mint_a_token(self) -> None:
        """The capability the connect path and self-heal tier 2 now test.

        Read as a capability rather than ``method != "fargate"`` so a later
        transport answers for itself.
        """
        assert SshTransport().mints_token is True
        assert SsmTransport().mints_token is True
        assert FargateTransport().mints_token is False

    def test_the_timeout_defaults_are_unchanged_by_the_move(self) -> None:
        """Pins the quirk exactly: the SSM *connect* default covers both SSM-family
        methods, while the raised *mint* default is SSM's alone — fargate never
        mints, and giving it the raised value would silently change the manager's
        answer for a code path that used to compare to the string ``"ssm"``.
        """
        ssh, ssm, fargate = SshTransport(), SsmTransport(), FargateTransport()
        assert ssm.connect_timeout_default == fargate.connect_timeout_default
        assert ssh.connect_timeout_default < ssm.connect_timeout_default
        assert ssm.mint_timeout_default > ssh.mint_timeout_default
        assert fargate.mint_timeout_default == ssh.mint_timeout_default

    def test_the_child_shape_comes_from_the_params_object(self) -> None:
        """``open`` must not restate the argv dimensions the spawn site consumes."""
        params = TransportParams(method="ssm", ssm_target="i-0123456789abcdef0")
        assert SsmTransport().open(params) == params.tunnel_kwargs()
        assert SsmTransport().open(params)["transport"] == "ssm"
        assert (
            SshTransport().open(TransportParams(method="ssh", ssh_host="h"))["transport"] == "ssh"
        )
        # A fargate child IS the SSM port-forward; only the far end differs.
        assert FargateTransport().open(TransportParams(method="fargate"))["transport"] == "ssm"


class TestValidation:
    def test_ssm_refuses_an_ecs_target_and_names_the_method_that_owns_it(self) -> None:
        inst = Instance(id="i", name="i", connection_method="ssm", ssm_target=_ECS_TARGET)
        with pytest.raises(SsmValidationError) as excinfo:
            SsmTransport().validate(inst)
        assert "fargate" in str(excinfo.value)

    def test_fargate_refuses_an_ec2_id(self) -> None:
        inst = Instance(
            id="i", name="i", connection_method="fargate", ssm_target="i-0123456789abcdef0"
        )
        with pytest.raises(SsmValidationError) as excinfo:
            FargateTransport().validate(inst)
        assert "ECS task target" in str(excinfo.value)

    def test_each_transport_reports_the_field_that_addresses_its_peer(self) -> None:
        """An unmapped method can therefore print nothing instead of an empty host."""
        assert SshTransport().describe_target(TransportParams(method="ssh", ssh_host="h")) == "h"
        assert (
            SsmTransport().describe_target(TransportParams(method="ssm", ssm_target="i-0")) == "i-0"
        )


class TestRefusals:
    def test_fargate_refuses_to_mint_and_says_where_the_turn_api_is(self) -> None:
        inst = Instance(id="i", name="i", connection_method="fargate", ssm_target=_ECS_TARGET)
        params = FargateTransport().validate(inst)
        with pytest.raises(TokenMintError) as excinfo:
            asyncio.run(
                FargateTransport().mint(
                    inst, params, parent_port=None, timeout_secs=1.0, seams=_seams()
                )
            )
        assert "turn_url" in str(excinfo.value)

    def test_fargate_refuses_a_restart_before_building_a_command(self) -> None:
        """A refusal, not a dispatch that fails remotely: the seam bundle here has
        no working restart, so reaching one would raise the bundle's own error."""
        inst = Instance(id="i", name="i", connection_method="fargate", ssm_target=_ECS_TARGET)
        params = FargateTransport().validate(inst)
        with pytest.raises(TransportRefusal) as excinfo:
            asyncio.run(FargateTransport().restart(inst, params, timeout_secs=1.0, seams=_seams()))
        assert "runs no Kiro Crew gateway to restart" in str(excinfo.value)


class TestSeams:
    def test_the_manager_module_is_still_the_substitution_point(self) -> None:
        """Moving a call into a transport must not move where it can be faked.

        The transports take their outward calls from the bundle the manager
        builds, and the manager reads them from its OWN module globals — the
        names the existing suite patches. Asserted by name so a rename that
        breaks those tests fails here first, with the reason.
        """
        from kiro_crew.instances import ssh_tunnel_manager as mgr

        for name in (
            "mint_remote_token_ssm",
            "run_remote_kirocrew",
            "run_remote_kirocrew_ssm",
            "diagnose_instance",
            "diagnose_instance_ssm",
            "diagnose_instance_fargate",
        ):
            assert callable(getattr(mgr, name)), name

        source = inspect.getsource(mgr.SshTunnelManager._seams)
        for name in ("mint_remote_token_ssm", "run_remote_kirocrew_ssm", "diagnose_instance_ssm"):
            assert (
                name in source
            ), f"{name} must be read from this module, not imported by the transport"

    def test_a_transport_uses_only_the_seam_it_needs(self) -> None:
        """Every other field of the bundle raises, so a stray call is a failure."""
        calls: list[str] = []

        async def _mint_ssm(*a, **k) -> str:
            calls.append("ssm")
            return "TOKEN"

        inst = Instance(id="i", name="i", connection_method="ssm", ssm_target="i-0123456789abcdef0")
        transport = SsmTransport()
        params = transport.validate(inst)
        token = asyncio.run(
            transport.mint(
                inst,
                params,
                parent_port=None,
                timeout_secs=1.0,
                seams=_seams(mint_ssm=_mint_ssm),
            )
        )
        assert token == "TOKEN"
        assert calls == ["ssm"]
