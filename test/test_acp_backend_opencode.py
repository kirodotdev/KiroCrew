"""OpenCode is known but cannot execute before pre-execution routing is verified.

The real 1.18.23 probe showed named allow rules bypassing an ACP rejection.
These tests pin containment, not a claim that OpenCode enforces Crew policy.
All protocol helpers below are exercised as pure objects with no child process.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.agent_sdk import backends, host_auth
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.config import paths
from kiro_crew.providers.acp import AcpProvider, provider_label

_BASELINE = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_CODEX})


def test_opencode_is_known_but_cannot_be_selected():
    assert ACP_BACKEND_OPENCODE in backends.ACP_BACKENDS_KNOWN
    assert backends.BASELINE_SELECTABLE_BACKENDS == _BASELINE
    assert ACP_BACKEND_OPENCODE not in backends.selectable_backends()
    assert backends.resolve_selected_backend(ACP_BACKEND_OPENCODE) == ACP_BACKEND_KIRO


def test_registration_cannot_remove_the_admission_block():
    before_baseline = set(backends._baseline)
    before_selectable = set(backends._selectable)
    with pytest.raises(ValueError, match="OpenCode is not admitted"):
        backends.register_selectable_backend(ACP_BACKEND_OPENCODE)
    assert backends._baseline == before_baseline
    assert backends._selectable == before_selectable


@pytest.mark.parametrize("factory", [AcpClient, AcpRuntime, AcpProvider])
@pytest.mark.parametrize("explicit_workspace", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"model": "test-provider/model"},
        {"extra_env": {"OPENCODE_PERMISSION": '{"*":"allow"}'}},
        {"sandbox_mode": "off"},
    ],
)
def test_direct_construction_refuses_before_filesystem_or_spawn(
    factory, explicit_workspace, options, tmp_path, monkeypatch
):
    filesystem = Mock(side_effect=AssertionError("filesystem reached before admission"))
    spawn = Mock(side_effect=AssertionError("spawn reached before admission"))
    monkeypatch.setattr(paths, "config_dir", filesystem)
    monkeypatch.setattr(client_mod, "Path", filesystem)
    monkeypatch.setattr(runtime_mod, "Path", filesystem)
    monkeypatch.setattr(client_mod, "create_subprocess_limited", spawn)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", spawn)
    kwargs = dict(options)
    if explicit_workspace:
        kwargs["work_dir"] = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="non-overridable"):
        factory(acp_backend=ACP_BACKEND_OPENCODE, **kwargs)
    filesystem.assert_not_called()
    spawn.assert_not_called()
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("backend", sorted(_BASELINE))
def test_admission_leaves_existing_backends_unchanged(backend, tmp_path):
    assert backends.require_backend_admission(backend) is None
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    assert client.backend == backend
    assert client._process is None


def test_unrelated_extension_admission_is_not_inferred_from_routing():
    assert backends.require_backend_admission("unregistered-extension") is None


def test_opencode_never_inherits_privileged_capabilities():
    for capabilities in (
        backends.ACP_BACKENDS_INTERNAL_SANDBOX,
        backends.ACP_BACKENDS_ACP_RUNTIME,
        backends.ACP_BACKENDS_SESSION_SHARING,
        backends.ACP_BACKENDS_STEER,
        backends.ACP_BACKENDS_COMPACT,
        backends.ACP_BACKENDS_SEED_LOCAL_SETTINGS,
        backends.ACP_BACKENDS_HOST_AUTH_CALLBACK,
    ):
        assert ACP_BACKEND_OPENCODE not in capabilities
    assert backends.routing_for(ACP_BACKEND_OPENCODE) == backends.Routing.UNVERIFIED
    assert ACP_BACKEND_OPENCODE not in host_auth.backends_retired_by_host_logout()


def _identity_parser(servers):
    client = object.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_OPENCODE
    client._remember_opencode_mcp_servers(servers)
    return client


def test_wire_identity_only_matches_the_exact_session_server():
    client = _identity_parser([{"name": "kirocrew-core"}])
    assert client._opencode_mcp_identity("kirocrew-core_resource_status") == (
        "kirocrew-core",
        "resource_status",
    )
    assert client._opencode_mcp_identity("unknown_resource_status") == ("", "")


@pytest.mark.parametrize(
    "servers,wire",
    [
        ([{"name": "same/name"}, {"name": "same.name"}], "same_name_tool"),
        ([{"name": "prefix"}, {"name": "prefix_nested"}], "prefix_nested_tool"),
        ([{"name": "kirocrew-core"}], "kirocrew-core_"),
        ([{"name": "kirocrew-core"}], "kirocrew-core_../tool"),
    ],
)
def test_ambiguous_or_malformed_wire_identity_is_not_trusted(servers, wire):
    assert _identity_parser(servers)._opencode_mcp_identity(wire) == ("", "")


def test_opencode_has_its_own_persistence_label():
    provider = object.__new__(AcpProvider)
    provider._client = _identity_parser([])
    assert provider_label(provider) == "opencode"
