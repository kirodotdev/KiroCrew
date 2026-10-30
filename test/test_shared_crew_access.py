"""Shared Crew setup paths and the capabilities payload that reports them."""

from __future__ import annotations

import dataclasses
from typing import Iterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import access
from kiro_crew.shared_crew import capabilities as caps_mod
from kiro_crew.shared_crew.access import note_tailnet, setup_paths_dict
from kiro_crew.shared_crew.capabilities import aggregate_capabilities
from kiro_crew.shared_crew.routes_capabilities import CAPABILITIES_PATH, register


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    access._reset_setup_paths_for_tests()
    caps_mod._reset_capability_sources_for_tests()
    yield
    access._reset_setup_paths_for_tests()
    caps_mod._reset_capability_sources_for_tests()


class TestSetupPaths:
    def test_default_build_reports_every_path_unavailable(self) -> None:
        assert setup_paths_dict() == {
            "tailscale": {"state": "unavailable", "reason": "not_configured"},
            "kiro_login": {"state": "unavailable", "reason": "not_in_this_build"},
            "systems_manager": {"state": "unavailable", "reason": "not_in_this_build"},
        }

    @pytest.mark.parametrize(
        "enabled, host, state, reason",
        [
            (True, "crew.tailnet.ts.net", "available", ""),
            (True, "", "degraded", "tailnet_unreachable"),
            (False, "crew.tailnet.ts.net", "unavailable", "not_configured"),
        ],
    )
    def test_tailnet_path_follows_what_the_gateway_resolved(
        self, enabled, host, state, reason
    ) -> None:
        note_tailnet(enabled=enabled, host=host)
        assert setup_paths_dict()["tailscale"] == {"state": state, "reason": reason}

    def test_identity_trust_sets_the_tailnet_path_up_without_the_enabled_flag(self) -> None:
        """``tailscale serve`` set up by hand plus an allowlist is sharing."""
        note_tailnet(enabled=False, host="", identity_trust=True)
        assert setup_paths_dict()["tailscale"] == {"state": "available", "reason": ""}


# ── the capabilities payload ──


class TestPayload:
    def test_payload_carries_setup_paths(self) -> None:
        body = aggregate_capabilities().to_dict()
        assert set(body["setup_paths"]) == {"tailscale", "kiro_login", "systems_manager"}
        assert "enterprise" not in body

    @pytest.mark.asyncio
    async def test_route_serves_the_setup_paths(self) -> None:
        app = web.Application()
        register(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(CAPABILITIES_PATH)
            body = await resp.json()
        assert body["setup_paths"]["kiro_login"]["reason"] == "not_in_this_build"


# ── no permission scoping ──


def test_no_role_or_acl_exists_in_any_contract() -> None:
    """Every admitted collaborator has the same rights: no contract names a role or ACL."""
    import inspect

    from kiro_crew.shared_crew import contracts

    banned = {"role", "roles", "acl", "acls", "permission", "permissions", "scope", "scopes"}
    for module in (contracts, access):
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if dataclasses.is_dataclass(obj):
                assert not banned & {f.name for f in dataclasses.fields(obj)}, obj
