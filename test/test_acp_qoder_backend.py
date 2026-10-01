"""Qoder is KNOWN to this build and deliberately NOT selectable.

This file pins the dormant landing state, so that the day someone makes the harness
selectable they have to change these assertions on purpose. What each one holds:

* the id is spellable everywhere a consumer needs to name it (policy id, provider
  label, launch record, auth declaration, MCP projection), so a governance rule can
  already deny it and no persisted session is mistaken for a kiro session;
* it is NOT offered, and a config naming it degrades to the default with a log, because
  nothing has established that its tool calls reach Kiro Crew's security gate;
* its tool-gate routing is UNVERIFIED, which fails closed, rather than a mechanism
  borrowed from a neighbour;
* the PID-file reclaim is not taught its process name: Crew never spawns it, so no
  tracked PID is one of its processes, and naming it would let the reclaim signal an
  operator's own standalone ``qodercli``;
* a spawn refuses rather than falling through to the kiro-cli arm, which would run the
  session under the wrong identity.

The wire facts in ``ACP_BACKEND_QODER``'s comment come from a live capture of qodercli
1.1.17, committed under ``test/fixtures/acp_frames/qoder/`` and replayed by
``test_acp_frame_replay``. They are not re-driven here: a test must not depend on a
binary the CI runner does not have.
"""

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND, PROVIDER_LABEL_QODER
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import host_auth, tool_gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKEND_QODER,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELF_SERVED_ACP,
    BASELINE_SELECTABLE_BACKENDS,
    DORMANT_BACKENDS,
    POLICY_ID_BY_BACKEND,
    Routing,
    agent_process_markers,
    launch_for,
    resolve_selected_backend,
)
from kiro_crew.providers.mirrors.registry import ProjectionKind, projection_for


def test_the_id_is_known_but_not_offered() -> None:
    assert ACP_BACKEND_QODER in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_QODER not in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_QODER not in sdk_backends.selectable_backends()


def test_a_config_naming_it_degrades_to_the_default() -> None:
    """The single selectability gate, so a hand-edited config cannot start a session."""
    assert resolve_selected_backend(ACP_BACKEND_QODER) == ACP_BACKEND_KIRO


def test_the_id_is_nameable_in_a_policy_and_labelled_as_its_own_provider() -> None:
    """Absent from either table, a governance rule could not deny it and a session
    would persist as a kiro one and have its transcript pruned for want of a session
    file."""
    assert POLICY_ID_BY_BACKEND[ACP_BACKEND_QODER] == "qoder"
    assert PROVIDER_LABEL_BY_BACKEND[ACP_BACKEND_QODER] == PROVIDER_LABEL_QODER == "qoder"


def test_the_launch_is_a_record_row_for_the_flag_help_does_not_list() -> None:
    launch = launch_for(ACP_BACKEND_QODER)
    assert ACP_BACKEND_QODER in ACP_BACKEND_LAUNCH
    assert ACP_BACKEND_QODER in ACP_BACKENDS_SELF_SERVED_ACP
    assert launch.binary == "qodercli"
    assert launch.acp_args == ("--acp",)
    assert launch.spawn_label == "qodercli --acp"
    assert launch.bin_env_var == "QODERCLI_BIN"
    assert launch.install_command == "npm i -g @qoder-ai/qodercli"
    # The handshake dialect is read from the row, not restated: qodercli answered
    # ``initialize`` with an integer 1 (the spec dialect).
    assert client_mod._PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_QODER] == 1
    assert client_mod.PROTOCOL_VERSION_QODER == 1


def test_tool_gate_routing_is_unverified_and_reads_indeterminate() -> None:
    """Fail closed: an id the routing table does not name must not inherit a
    neighbour's mechanism, and must never read as ROUTED."""
    assert sdk_backends.ACP_BACKEND_ROUTING[ACP_BACKEND_QODER] is Routing.UNVERIFIED
    assert tool_gate.routing_for(ACP_BACKEND_QODER) is Routing.UNVERIFIED
    verdict, _reason = tool_gate.routing_verdict(ACP_BACKEND_QODER)
    assert verdict is tool_gate.Verdict.INDETERMINATE
    assert tool_gate.label_for(ACP_BACKEND_QODER) == "Qoder"


def test_no_credential_leaf_is_claimed_until_the_store_is_located() -> None:
    """An undeclared leaf is a stated gap; a guessed one would fence a path nobody
    checked. ``adapter_own_leaves`` must stay a subset of ``credential_leaves``."""
    declaration = host_auth.declaration_for(ACP_BACKEND_QODER)
    assert declaration.backend == ACP_BACKEND_QODER
    assert declaration.credential_leaves == ()
    assert declaration.adapter_own_leaves == ()
    assert declaration.host_logout_retires_children is False
    assert declaration.entitlement_source == host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE
    assert "qodercli login" in declaration.sign_in_remedy
    assert "qodercli login" in declaration.signed_out_message


def test_the_mcp_projection_claims_no_channel_because_none_was_measured() -> None:
    declared = projection_for(ACP_BACKEND_QODER)
    assert declared.kind is ProjectionKind.NO_CHANNEL
    assert declared.channel.strip()
    assert declared.tracking.strip()


def test_the_install_probe_names_the_binary_and_the_command(monkeypatch) -> None:
    monkeypatch.setattr(backend_install.acp_driver, "self_served_resolves", lambda _b: False)
    state = backend_install._probe_self_served(ACP_BACKEND_QODER)
    assert state.installed == backend_install.MISSING
    assert state.missing_components == ("qodercli",)
    assert state.policy_id == "qoder"
    assert "@qoder-ai/qodercli" in state.install_command


def test_an_edition_cannot_put_it_on_the_switch_while_it_is_unverified() -> None:
    """``register_selectable_backend`` refuses an UNVERIFIED harness, so KNOWN is not
    enough to reach the dashboard switch -- for the baseline or for a plugin."""
    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        with pytest.raises(ValueError) as raised:
            sdk_backends.register_selectable_backend(ACP_BACKEND_QODER)
        assert "unverified" in str(raised.value)
        assert set(sdk_backends._baseline) == baseline_before
        assert set(sdk_backends._selectable) == selectable_before
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)


def test_the_orphan_reclaim_is_not_taught_a_name_for_a_harness_crew_never_spawns() -> None:
    """A tracked PID recycled onto an operator's own ``qodercli`` must not be signalled.

    The exemption is derived from the routing row, so the same change that names a
    mechanism (and makes the harness spawnable) brings the process name back.
    """
    assert ACP_BACKEND_QODER in DORMANT_BACKENDS
    assert ACP_BACKEND_QODER not in ACP_BACKEND_PROCESS_NAMES
    assert launch_for(ACP_BACKEND_QODER).binary not in agent_process_markers()
    assert DORMANT_BACKENDS == {
        b for b, r in sdk_backends.ACP_BACKEND_ROUTING.items() if r is Routing.UNVERIFIED
    }
