"""The Mistral Vibe backend: vocabulary, the launch record, and the routing state.

Vibe is selectable with ``Routing.VERIFIED_SEEDED_SETTINGS``: Crew seeds
``VIBE_DEFAULT_AGENT=ask`` plus the auto-approve clamp variables
(``VIBE_ENABLE_AUTO_APPROVE``, ``VIBE_BYPASS_TOOL_PERMISSIONS``,
``VIBE_DISABLED_AGENTS``) into the child's environment -- a config layer Vibe
resolves ABOVE its user and project TOML files -- and reads the profile Vibe
resolved back off the ``modes`` block of the session-opening response before the
first prompt, refusing unless the current mode is ``ask`` AND the advertised mode
list is exactly ``ask``. Live captures on mistral-vibe 3.0.3
(``test/fixtures/acp_frames/vibe/routing-live.jsonl``) show a write asking and a
rejected write leaving no file even with ``bypass_tool_permissions = true`` in the
config file, while the same file without the seed auto-approved.

This file pins the vocabulary: the backend id, its memberships, its
``SelfServedLaunch`` record (binary ``vibe-acp``, no adapter package), its
selectability, and its absence from the capability sets it has no evidence for.
"""

from __future__ import annotations

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.vibe import VibeLaunch
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends_launch import (
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_SELF_SERVED_ACP,
    launch_for,
)
from kiro_crew.agent_sdk.backends_mcp import (
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
    ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY,
)
from kiro_crew.agent_sdk.backends_members import (
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
)
from kiro_crew.agent_sdk.backends_models import (
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    effort_config_option_id,
)
from kiro_crew.agent_sdk.backends_routing import (
    ACP_BACKEND_ROUTING,
    ACP_BACKENDS_META_IDENTITY,
    Routing,
    permission_config_for,
    permission_setting_for,
    routing_for,
)
from kiro_crew.agent_sdk.backends_sessions import (
    ACP_BACKENDS_ACP_CLIENT_SPAWNABLE,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_SESSION_EVICTION,
    ACP_BACKENDS_SESSION_SHARING,
)
from kiro_crew.agent_sdk.backends_turns import (
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_CONTEXT_RECYCLE,
    ACP_BACKENDS_HARNESS_MANAGED_COMPACTION,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
)

VIBE = acp_backends.ACP_BACKEND_VIBE

#: This harness's launch record. The override variable, the binary and the argv
#: tail are read from here rather than from module constants, because the record is
#: where they are declared and a second spelling could drift from it.
_VIBE_LAUNCH = launch_for(sdk_backends.ACP_BACKEND_VIBE)


# ── Vocabulary ──


def test_vibe_is_a_known_backend() -> None:
    """Known gates the kwarg: an unrecognized value would silently spawn kiro-cli."""
    assert VIBE == "vibe"
    assert VIBE in sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_is_selectable_because_routing_is_verified() -> None:
    """VERIFIED_SEEDED_SETTINGS puts Vibe in the baseline selectable set.

    Selectability is a claim that a session's tool calls reach the host permission
    gate; the seed plus the session-response read-back establish it, so Vibe is in
    ``BASELINE_SELECTABLE_BACKENDS`` and survives a config load.
    """
    assert routing_for(VIBE) is Routing.VERIFIED_SEEDED_SETTINGS
    assert VIBE in sdk_backends.BASELINE_SELECTABLE_BACKENDS
    assert VIBE in sdk_backends.selectable_backends()


def test_register_selectable_backend_accepts_vibe_idempotently() -> None:
    """The one gate that makes a backend selectable accepts a verified routing."""
    before = sdk_backends.selectable_backends()
    sdk_backends.register_selectable_backend(VIBE)
    assert sdk_backends.selectable_backends() == before


def test_vibe_is_in_independent_setup() -> None:
    """First-run setup can complete without Kiro CLI for harnesses that launch independently."""
    assert VIBE in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP


def test_vibe_carries_its_mcp_servers_on_the_session_array() -> None:
    """Membership here is what gives a Vibe session Crew's own tools.

    vibe-acp consumes the ``session/new`` ``mcpServers`` array itself (captured
    live on mistral-vibe 3.0.3: a stdio element was spawned and asked
    ``initialize``/``tools/list`` -- ``test/fixtures/acp_frames/vibe/routing-live.jsonl``),
    so the array is the channel and carries Crew's pooled broker stubs
    (``broker-only`` projection in ``providers/mirrors/registry.py``).
    """
    assert VIBE in ACP_BACKENDS_SESSION_MCP_ARRAY


def test_vibe_model_and_effort_ride_config_options() -> None:
    """The ``model`` and ``thinking`` selects advertised on ``session/new`` are the
    channels a model or effort switch travels down (captured on 3.0.3)."""
    assert VIBE in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert VIBE in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert VIBE in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert effort_config_option_id(VIBE) == "thinking"


def test_vibe_is_acp_client_spawnable() -> None:
    """Vibe is launched one process per session by ``AcpClient``."""
    assert VIBE in ACP_BACKENDS_ACP_CLIENT_SPAWNABLE


def test_vibe_is_not_in_the_capability_sets_it_has_no_evidence_for() -> None:
    """Absence is a decision here, not an oversight, so it is pinned as one.

    Each of these would make a claim the wire does not support: a shared process,
    a steer method, an internal sandbox that would displace the credential mask,
    session sharing, or an effort channel.
    """
    assert VIBE not in ACP_BACKENDS_ACP_RUNTIME
    assert VIBE not in ACP_BACKENDS_STEER
    assert VIBE not in ACP_BACKENDS_STEERING_REQUEST
    assert VIBE not in ACP_BACKENDS_INTERNAL_SANDBOX
    assert VIBE not in ACP_BACKENDS_SESSION_SHARING
    assert VIBE not in ACP_BACKENDS_SESSION_EVICTION
    assert VIBE not in ACP_BACKENDS_COMPACT
    assert VIBE not in ACP_BACKENDS_INLINE_COMPACTION
    assert VIBE not in ACP_BACKENDS_HARNESS_MANAGED_COMPACTION
    assert VIBE not in ACP_BACKENDS_CONTEXT_RECYCLE
    assert VIBE not in ACP_BACKENDS_KIRO_SLASH_COMMANDS
    assert VIBE not in ACP_BACKENDS_MARKDOWN_AGENT_SPECS
    assert VIBE not in ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY
    assert VIBE not in ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE
    assert VIBE not in ACP_BACKENDS_MEMBER_CAPABILITIES
    assert VIBE not in ACP_BACKENDS_MEMBER_DISPATCH
    assert VIBE not in ACP_BACKENDS_MEMBER_PANEL
    assert VIBE not in ACP_BACKENDS_META_IDENTITY


# ── The launch record ──


def test_vibe_launch_record_names_the_binary_and_no_adapter() -> None:
    """``vibe-acp`` is the ACP server; no npm adapter is needed.

    The published Python package ``mistral-vibe`` ships the ``vibe-acp`` executable,
    which serves ACP (JSON-RPC 2.0 over stdin/stdout) directly.
    """
    assert _VIBE_LAUNCH.binary == "vibe-acp"
    assert _VIBE_LAUNCH.acp_args == ()
    assert _VIBE_LAUNCH.bin_env_var == "VIBE_ACP_BIN"
    assert _VIBE_LAUNCH.protocol_version == 1
    assert _VIBE_LAUNCH.label == "Mistral Vibe"


def test_vibe_install_command_is_pip_not_npm() -> None:
    """Vibe is a Python package; the install command reflects that."""
    assert "pip" in _VIBE_LAUNCH.install_command or "uv" in _VIBE_LAUNCH.install_command
    assert "npm" not in _VIBE_LAUNCH.install_command


def test_vibe_missing_hint_names_the_python_install() -> None:
    """The hint tells the operator what to install: a Python package, not npm.

    The hint says "not an npm adapter" to steer the operator away from the wrong
    installer; the assertion checks the positive signal (pip/uv) rather than the
    absence of the word "npm", which appears in the corrective context.
    """
    assert "pip" in _VIBE_LAUNCH.missing_hint or "uv" in _VIBE_LAUNCH.missing_hint
    assert "python" in _VIBE_LAUNCH.missing_hint.lower()


def test_vibe_is_self_served_acp() -> None:
    """Vibe serves ACP from its own binary; its whole launch is in the table."""
    assert VIBE in ACP_BACKENDS_SELF_SERVED_ACP


def test_vibe_process_name_is_the_binary() -> None:
    """The PID-file reclaim recognises a vibe-acp child by its argv0 basename."""
    assert ACP_BACKEND_PROCESS_NAMES[VIBE] == "vibe-acp"


def test_vibe_is_not_a_node_adapter() -> None:
    """Vibe is a Python binary, not a Node entry script."""
    assert VIBE not in ACP_BACKEND_NODE_ADAPTER_PACKAGES


# ── The harness adapter ──


def test_vibe_has_a_process_adapter() -> None:
    """``AcpClient`` launches one vibe-acp process per session through this adapter."""
    adapter = process_adapter_for(VIBE)
    assert adapter is not None
    assert isinstance(adapter, VibeLaunch)
    assert adapter.backend == VIBE


def test_vibe_launch_backend_id_matches() -> None:
    """The adapter's backend id is the vocabulary constant, not a literal."""
    assert VibeLaunch.backend == sdk_backends.ACP_BACKEND_VIBE


# ── The routing state ──


def test_vibe_routing_is_verified_seeded_settings_not_absent() -> None:
    """The routing table names Vibe with the seeded-settings mechanism.

    A ``.get(backend, Routing.UNVERIFIED)`` read is deliberate: an id the table
    does not name fails closed rather than inheriting a neighbour's mechanism.
    Vibe IS named, with ``VERIFIED_SEEDED_SETTINGS``, so the session-response
    read-back runs for it.
    """
    assert VIBE in ACP_BACKEND_ROUTING
    assert ACP_BACKEND_ROUTING[VIBE] is Routing.VERIFIED_SEEDED_SETTINGS


def test_vibe_seeded_permission_setting_is_the_env_pair() -> None:
    """The primary seed is ``VIBE_DEFAULT_AGENT=ask``, an env var Vibe resolves above
    its config files; the read-back compares ``modes.currentModeId`` against it."""
    assert permission_setting_for(VIBE) == ("VIBE_DEFAULT_AGENT", "ask")
    assert permission_config_for(VIBE) == ("", "")


def test_vibe_is_in_enforced_routings() -> None:
    """VERIFIED_SEEDED_SETTINGS is in ENFORCED_ROUTINGS: a session whose read-back
    fails is refused, and the OS credential mask applies to the child."""
    assert routing_for(VIBE) in gate.ENFORCED_ROUTINGS
    assert gate.is_enforced(VIBE)


# ── The spawn seed ──


def test_vibe_spawn_seed_carries_the_clamp_variables() -> None:
    """The seed is four ``VIBE_*`` variables, the primary read from the routing
    table so the seed, the read-back and the refusal cannot drift apart."""
    from kiro_crew.acp.harness import vibe as vibe_mod

    seed = dict(vibe_mod._VIBE_ROUTING_SEED_ENV)
    assert seed["VIBE_DEFAULT_AGENT"] == permission_setting_for(VIBE)[1]
    assert seed["VIBE_ENABLE_AUTO_APPROVE"] == "false"
    assert seed["VIBE_BYPASS_TOOL_PERMISSIONS"] == "false"
    assert "auto-approve" in seed["VIBE_DISABLED_AGENTS"]
    assert "accept-edits" in seed["VIBE_DISABLED_AGENTS"]
    assert sorted(vibe_mod.VIBE_REQUIRED_MODE_IDS) == ["ask"]


# ── The read-back ──


def _vibe_client(backend: str = VIBE):
    """A minimal stand-in exposing the two attributes the verifier reads."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.backend = backend
    return client


def _session_resp(modes: object) -> dict:
    return {"sessionId": "s", "modes": modes}


def test_verify_vibe_routing_accepts_exactly_ask() -> None:
    """currentModeId=ask with availableModes exactly [ask] passes."""
    from kiro_crew.acp import client_permissions

    resp = _session_resp({"currentModeId": "ask", "availableModes": [{"id": "ask"}]})
    client_permissions._verify_vibe_routing(_vibe_client(), resp)


def test_verify_vibe_routing_refuses_a_non_ask_current_mode() -> None:
    """A restored/created session in auto-approve is refused before the first prompt."""
    from kiro_crew.acp import client_permissions
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    resp = _session_resp(
        {
            "currentModeId": "auto-approve",
            "availableModes": [{"id": "ask"}, {"id": "auto-approve"}],
        }
    )
    with pytest.raises(AcpToolGateUnroutable):
        client_permissions._verify_vibe_routing(_vibe_client(), resp)


def test_verify_vibe_routing_refuses_when_a_bypass_mode_is_still_advertised() -> None:
    """currentModeId=ask but accept-edits still available: the clamp did not take
    effect (a config source outranked the seed), so the session is refused."""
    from kiro_crew.acp import client_permissions
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    resp = _session_resp(
        {
            "currentModeId": "ask",
            "availableModes": [{"id": "ask"}, {"id": "accept-edits"}],
        }
    )
    with pytest.raises(AcpToolGateUnroutable):
        client_permissions._verify_vibe_routing(_vibe_client(), resp)


def test_verify_vibe_routing_refuses_an_omitted_modes_block() -> None:
    """No modes block at all is INDETERMINATE, not a pass."""
    from kiro_crew.acp import client_permissions
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable):
        client_permissions._verify_vibe_routing(_vibe_client(), {"sessionId": "s"})


def test_verify_vibe_routing_ignores_other_backends() -> None:
    """The verifier is a no-op for any backend but vibe (harness-parity H13)."""
    from kiro_crew.acp import client_permissions

    resp = _session_resp(
        {
            "currentModeId": "auto-approve",
            "availableModes": [{"id": "auto-approve"}],
        }
    )
    client_permissions._verify_vibe_routing(_vibe_client("goose"), resp)


# ── Capability set containment ──


def test_every_capability_set_is_a_subset_of_known_backends() -> None:
    """H8: every capability set is a subset of ACP_BACKENDS_KNOWN."""
    known = sdk_backends.ACP_BACKENDS_KNOWN
    for name in dir(sdk_backends):
        if not name.startswith("ACP_BACKENDS_"):
            continue
        value = getattr(sdk_backends, name)
        if isinstance(value, frozenset):
            assert value <= known, f"{name} has members outside ACP_BACKENDS_KNOWN: {value - known}"


def test_vibe_in_session_mcp_array_is_a_subset_of_known() -> None:
    """The session MCP array set contains Vibe and stays within known backends."""
    assert VIBE in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKENDS_SESSION_MCP_ARRAY <= sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_in_acp_client_spawnable_is_a_subset_of_known() -> None:
    """The client-spawnable set contains Vibe and stays within known backends."""
    assert VIBE in ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    assert ACP_BACKENDS_ACP_CLIENT_SPAWNABLE <= sdk_backends.ACP_BACKENDS_KNOWN


def test_vibe_in_independent_setup_is_a_subset_of_known() -> None:
    """The independent-setup set contains Vibe and stays within known backends."""
    assert VIBE in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP
    assert sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP <= sdk_backends.ACP_BACKENDS_KNOWN


# ── Re-exports ──


def test_vibe_is_reexported_from_acp_backends_shim() -> None:
    """The shim re-exports the vocabulary so existing call sites keep working."""
    assert acp_backends.ACP_BACKEND_VIBE == "vibe"


def test_vibe_is_reexported_from_acp_types() -> None:
    """``acp.types`` re-exports the vocabulary for existing importers."""
    import kiro_crew.acp.types as acp_types

    assert "ACP_BACKEND_VIBE" in vars(acp_types)
