"""The vibe backend: vocabulary and the dormant state it lands in.

Mistral's Vibe coding agent joins as a DORMANT harness: KNOWN, so a governance
rule can name and deny it and an edition can spell it, but deliberately NOT
selectable. This file pins the state the onboarding doc describes rather than a
behaviour a live ``vibe-acp`` capture has not measured yet. Every fact asserted
here is either a table row this change adds (the launch record, the auth
declaration, the routing entry) or the absence of one it deliberately refuses to
claim.

What this file does NOT do is drive the harness. Nothing in the tree can: the
ACP surface was read from mistral-vibe v3.0.2's source (``vibe-acp`` is a
no-args self-served binary; its ``initialize`` advertises a ``mode`` config
option and it implements ``session/set_mode``) and no live capture exists. The
moment one does, the selectability PR moves the routing entry, adds the
measured config row, revisits the no-channel projection, and replaces the
absence pins below with the positive claims the capture supports.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import launch_for

VIBE = sdk_backends.ACP_BACKEND_VIBE

#: This harness's launch record. Read from here rather than spelled twice,
#: because the record is where the launch is declared and a second spelling
#: could drift from it.
_VIBE_LAUNCH = launch_for(sdk_backends.ACP_BACKEND_VIBE)


# ── Vocabulary: known, dormant ───────────────────────────────────────────────


def test_vibe_is_a_known_but_not_selectable_backend() -> None:
    """Known gates the kwarg; the baseline is what the switch may persist.

    Selectability is refused by ``register_selectable_backend`` while the routing
    is UNVERIFIED (the assertion below), so the two facts this test pins are the
    two halves of DORMANCY: the build can spell the id, and it deliberately does
    not offer it.
    """
    assert VIBE == "vibe"
    assert VIBE in sdk_backends.ACP_BACKENDS_KNOWN
    assert VIBE not in sdk_backends.BASELINE_SELECTABLE_BACKENDS


def test_vibe_cannot_be_registered_selectable_while_unverified() -> None:
    """The registry's own refusal is the load-bearing half of dormancy.

    Without it an out-of-repo edition could put the harness on the switch with
    no routing, no refusal and no mask -- the exact state the refusal's message
    names. Asserted with the registry state restored, because
    ``register_selectable_backend`` writes both sets and a leaked wider
    baseline would follow the rest of the run.
    """
    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        with pytest.raises(ValueError, match="routing is 'unverified'"):
            sdk_backends.register_selectable_backend(VIBE)
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)
    assert VIBE not in sdk_backends.selectable_backends()


def test_vibe_policy_id_is_nameable() -> None:
    """Every known id can be denied, which is what the policy mapping makes true."""
    assert sdk_backends.POLICY_ID_BY_BACKEND[VIBE] == "vibe"


# ── Launch ───────────────────────────────────────────────────────────────────


def test_the_launch_record_holds_the_self_served_facts() -> None:
    """One row answers the binary, the override, the install and the dialect.

    ``vibe-acp`` is a self-served binary in the opencode/goose shape: it serves
    ACP from its own executable with no arguments, so ``acp_args`` is empty and
    the whole launch is values the row holds. Read from the record so a second
    spelling cannot drift.
    """
    assert _VIBE_LAUNCH.label == "Mistral Vibe"
    assert _VIBE_LAUNCH.binary == "vibe-acp"
    assert _VIBE_LAUNCH.acp_args == ()
    assert _VIBE_LAUNCH.bin_env_var == "VIBE_ACP_BIN"
    assert _VIBE_LAUNCH.install_command == "uv tool install mistral-vibe"
    assert _VIBE_LAUNCH.protocol_version == 1
    assert _VIBE_LAUNCH.spawn_label == "vibe-acp"


def test_vibe_derives_its_launch_memberships_from_the_table() -> None:
    """Self-served and client-spawnable are read from the row, not restated."""
    assert VIBE in sdk_backends.ACP_BACKENDS_SELF_SERVED_ACP
    assert VIBE in sdk_backends.ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    assert sdk_backends.ACP_BACKEND_PROCESS_NAMES[VIBE] == "vibe-acp"
    assert VIBE not in sdk_backends.ACP_BACKENDS_ACP_RUNTIME


def test_the_process_adapter_is_the_launch_row_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Dormancy in code: the plan carries no MCP array, no mask and no seed.

    The adapter resolves its plan off the shared self-served ladder; the
    absences are what the no-channel projection and the UNVERIFIED routing
    already declare, so a plan that grew an array warm or a hidden-dirs mask
    would be the first place the two declarations and the code disagreed.
    """
    import asyncio

    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    async def stubbed(_backend: str):
        return "/opt/bin/vibe-acp", ["/opt/bin/vibe-acp"], "vibe-acp", "vibe-acp"

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", stubbed)
    adapter = process_adapter_for(VIBE)
    assert adapter is not None and adapter.backend == VIBE
    ctx = SpawnContext(
        agent="kirocrew",
        work_dir=tmp_path,
        model=None,
        environ={},
        home=tmp_path,
        sandbox_mode="auto",
        session=None,
    )
    plan = asyncio.run(adapter.resolve_spawn(ctx))
    assert plan.argv == ["/opt/bin/vibe-acp"]
    assert (plan.spawn_label, plan.stderr_label) == ("vibe-acp", "vibe-acp")
    assert plan.extra_hidden_dirs == ()
    # The no-op environment: nothing this harness adds reaches the child.
    env: dict[str, str] = {"PATH": "/usr/bin"}
    adapter.apply_spawn_env(env)
    assert env == {"PATH": "/usr/bin"}


def test_the_install_probe_names_the_binary_and_the_command() -> None:
    """The self-served probe derives from the row, so the advice cannot drift.

    The probe itself is the shared ``_probe_self_served``; what is pinned here
    is that vibe reaches it (a KNOWN backend with no probe would read UNKNOWN on
    the dashboard) and that its answers name the row's own values.
    """
    from kiro_crew.agent_sdk import backend_install

    assert VIBE in sdk_backends.ACP_BACKENDS_SELF_SERVED_ACP
    state = backend_install.probe_backend(VIBE)
    assert state.backend == VIBE
    assert state.policy_id == "vibe"
    assert state.installed in (
        backend_install.INSTALLED,
        backend_install.MISSING,
        backend_install.UNKNOWN,
    )
    if state.installed == backend_install.MISSING:
        assert state.missing_components == ("vibe-acp",)
        assert "uv tool install mistral-vibe" in state.install_command


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_the_auth_declaration_pins_the_source_read_credential_leaves() -> None:
    """One leaf on the floor, one override that relocates it, nothing spared.

    Source-read off mistral-vibe v3.0.2: ``~/.vibe`` is the home (relocated
    whole by ``VIBE_HOME``), the OS keyring is preferred, and ``~/.vibe/.env``
    is the 0600 plaintext fallback -- so that file is the leaf the floor fences
    and the keyring is the residual named in the declaration's comment.
    """
    from kiro_crew.agent_sdk.host_auth import (
        ENTITLEMENT_OWN_CREDENTIAL_FILE,
        declaration_for,
    )

    declaration = declaration_for(VIBE)
    assert declaration.credential_leaves == (".vibe/.env",)
    assert declaration.home_override_env_vars == ("VIBE_HOME",)
    # No mask is applied to this harness (routing is not ENFORCED), so there is
    # nothing to carve back out -- the claude shape, asserted so the exclusions
    # cannot appear before the mask does.
    assert declaration.adapter_own_leaves == ()
    assert declaration.host_logout_retires_children is False
    assert declaration.entitlement_source == ENTITLEMENT_OWN_CREDENTIAL_FILE
    # No signature: a phrase copied from a live capture is required evidence, and
    # a dormant harness has had no driven session to read one off.
    assert declaration.signed_out_signature == ""


# ── The capability sets vibe is deliberately absent from ─────────────────────


def test_vibe_is_in_no_capability_set_a_capture_has_not_measured() -> None:
    """Absence is a decision here, not an oversight, so it is pinned as one.

    Every one of these would be a claim about the wire that only a live capture
    can support: the MCP array mount, the steer channels, the effort and model
    paths, the session verbs and the member surfaces. The one set a dormant
    harness still owes is the launch membership, asserted above.
    """
    assert VIBE not in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert VIBE not in sdk_backends.ACP_BACKENDS_STEER
    assert VIBE not in sdk_backends.ACP_BACKENDS_STEERING_REQUEST
    assert VIBE not in sdk_backends.ACP_BACKENDS_STEER_CONCURRENT_PROMPT
    assert VIBE not in sdk_backends.ACP_BACKENDS_COMPACT
    assert VIBE not in sdk_backends.ACP_BACKENDS_INTERNAL_SANDBOX
    assert VIBE not in sdk_backends.ACP_BACKENDS_POD_HOME_REMAP
    assert VIBE not in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP
    assert VIBE not in sdk_backends.ACP_BACKENDS_SESSION_SHARING
    assert VIBE not in sdk_backends.ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert VIBE not in sdk_backends.ACP_BACKENDS_FORK_AT_MESSAGE
    for name in (
        "ACP_BACKENDS_MEMBER_CAPABILITIES",
        "ACP_BACKENDS_MEMBER_TOOL_LIMITS",
        "ACP_BACKENDS_MATE_WORKER_KIT",
        "ACP_BACKENDS_MEMBER_DISPATCH",
        "ACP_BACKENDS_MEMBER_PANEL",
        "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
        "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION",
        "ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION",
    ):
        asserted = getattr(sdk_backends, name, None)
        assert asserted is not None, f"agent_sdk.backends no longer declares {name}"
        assert VIBE not in asserted, f"vibe joined {name} without a live capture"


def test_vibe_routing_is_unverified_and_not_enforced() -> None:
    """The fail-closed member, asserted against the enum rather than its string.

    ``UNVERIFIED`` always resolves INDETERMINATE -- which refuses -- and the
    harness is outside ``ENFORCED_ROUTINGS``, so no compensating credential
    mask is applied to its child. Both are the state the selectability PR has
    to move, and each with its own measurement.
    """
    from kiro_crew import acp_tool_gate

    assert sdk_backends.routing_for(VIBE) is sdk_backends.Routing.UNVERIFIED
    assert VIBE not in acp_tool_gate.ENFORCED_ROUTINGS
    assert acp_tool_gate.is_enforced(VIBE) is False
    # No config row: the candidate mechanism (the ``mode`` option /
    # ``session/set_mode`` route to ``ask``) is source-read, not measured.
    assert sdk_backends.permission_config_for(VIBE) == ("", "")


def test_the_projection_declares_no_channel_rather_than_a_guess() -> None:
    """vibe-acp advertises no mcpCapabilities, and no capture has mounted the array.

    The ``no-channel`` kind is the honest state: it NAMES the candidate channel
    (the ``session/new`` ``mcpServers`` array, once a live capture shows a
    server mounted, asked, listed and called) and the tracking anchor for the
    decision, which is the onboarding doc's worked example for this harness.
    """
    from kiro_crew.providers.mirrors.registry import ProjectionKind, projection_for

    declared = projection_for(VIBE)
    assert declared.kind is ProjectionKind.NO_CHANNEL
    assert "mcpServers" in declared.channel
    assert declared.tracking == (
        "docs/system-specs/modules/harness-onboarding.md#worked-example-the-vibe-harness-dormant"
    )


def test_vibe_gets_a_fresh_model_namespace_even_while_dormant() -> None:
    """Its ids are Mistral aliases, so the shared ``acp`` bucket would mix them.

    The namespace is claimed now rather than at selectability, because a harness
    that starts in the shared bucket and moves later orphans every alias the
    picker persisted under it.
    """
    from kiro_crew.agent_sdk.backends import model_registry_namespace

    assert model_registry_namespace(VIBE) == "vibe"
