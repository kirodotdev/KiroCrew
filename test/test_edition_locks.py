"""Enterprise-build values for the RFC's Red controls (``kiro_crew.edition_locks``).

One test per control: in an enterprise build the feature stays off even when local
config, the stored switch or the policy would turn it on; in an ``oss`` build the
same input behaves as before. RFC: ``docs/request-for-change/rfc-crew-enterprise-edition.md``,
appendix "Red".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.platform.governance_parts.archetypes import Decision

EDITIONS = ("oss", "enterprise")


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)
    return value == "enterprise"


# --- Hooks by name ---------------------------------------------------------------


def _policy_permit() -> Decision:
    """What a policy that names and enables the scope answers."""
    return Decision(True, "permitted", rule="rule2-intersect", layer="policy")


@pytest.mark.parametrize("edition", EDITIONS)
def test_script_hooks_off_in_enterprise_even_when_the_policy_enables_them(
    monkeypatch, edition
) -> None:
    from kiro_crew.hook_runtime import governance_gate
    from kiro_crew.platform import governance_profiles

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(governance_profiles, "governance_permits", lambda *a, **k: _policy_permit())

    reason = governance_gate._script_hooks_capability_denied("s")

    assert (reason is not None) is enterprise


@pytest.mark.parametrize("edition", EDITIONS)
def test_user_kiro_hooks_not_installed_in_enterprise(monkeypatch, tmp_path: Path, edition) -> None:
    from kiro_crew.agent import _apply_user_kiro_hooks

    enterprise = _set_edition(monkeypatch, edition)
    script = tmp_path / "hooks" / "a.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    config: dict = {"hooks": {}}
    mc_cfg = {
        "agent": {
            "kiro_hooks": {"preToolUse": [{"command": str(script)}]},
            "kiro_hooks_autoimport": False,
        }
    }

    _apply_user_kiro_hooks(config, mc_cfg)

    assert bool(config["hooks"].get("preToolUse")) is not enterprise


# --- Webhooks by source ----------------------------------------------------------


@pytest.mark.parametrize("edition", EDITIONS)
def test_webhooks_off_in_enterprise_even_when_switched_on(
    monkeypatch, tmp_path: Path, edition
) -> None:
    from kiro_crew import webhooks

    enterprise = _set_edition(monkeypatch, edition)
    store = webhooks.WebhookTokenStore(tmp_path)

    assert store.set_switch(True) is not enterprise
    assert store.is_switch_on() is not enterprise


@pytest.mark.parametrize("edition", EDITIONS)
def test_ops_mission_control_webhook_source_off_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(webhook, "provider_enabled", lambda _id: False)

    accepted, detail = webhook.enqueue(b"{}", "sha256=x")

    assert accepted is False
    assert ("enterprise edition" in detail) is enterprise


# --- Signed extensions only / Extension versions (install off) -------------------


class _Installer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _ok(self, name: str):
        from kiro_crew.platform.interfaces import CapabilityResult

        self.calls.append(name)
        return CapabilityResult(ok=True)

    async def install_mcp(self, server_id: str):
        return await self._ok("install_mcp")

    async def install_skill(self, package: str):
        return await self._ok("install_skill")

    async def install_agent(self, package: str):
        return await self._ok("install_agent")

    async def sync_plugins(self):
        return await self._ok("sync_plugins")


@pytest.mark.parametrize("edition", EDITIONS)
def test_extension_install_off_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.platform.capability_bound import BoundedCapabilityManager

    enterprise = _set_edition(monkeypatch, edition)
    inner = _Installer()
    mgr = BoundedCapabilityManager(inner)  # type: ignore[arg-type]

    async def _run():
        return [
            await mgr.install_mcp("x"),
            await mgr.install_skill("x"),
            await mgr.install_agent("x"),
            await mgr.sync_plugins(),
        ]

    results = asyncio.run(_run())

    assert all(r.ok is not enterprise for r in results)
    assert inner.calls == (
        [] if enterprise else ["install_mcp", "install_skill", "install_agent", "sync_plugins"]
    )


# --- Workload identity / Identity settings ---------------------------------------


@pytest.mark.parametrize("edition", EDITIONS)
def test_agentcore_off_in_enterprise_even_when_policy_enables_it(monkeypatch, edition) -> None:
    from kiro_crew.platform.governance import (
        agentcore_gateway_url,
        agentcore_posture,
        agentcore_workload_name,
        parse_policy,
    )

    enterprise = _set_edition(monkeypatch, edition)
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {
                "agentcore": {
                    "enabled": True,
                    "posture": "workload",
                    "gateway_url": "https://gateway.example.com/mcp",
                    "workload_name": "crew",
                }
            },
        }
    )

    assert agentcore_posture(ceiling) == (None if enterprise else "workload")
    assert bool(agentcore_gateway_url(ceiling)) is not enterprise
    assert bool(agentcore_workload_name(ceiling)) is not enterprise


# --- Content sent to external decisions ------------------------------------------


@pytest.mark.parametrize("edition", EDITIONS)
def test_external_decisions_off_in_enterprise_even_when_the_policy_enables_them(
    monkeypatch, edition
) -> None:
    from kiro_crew.decisions import capability

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(capability, "vet_and_audit", lambda *a, **k: _policy_permit())

    assert capability.is_decisions_denied() is enterprise
    assert capability.is_decisions_denied(local=True) is enterprise


# --- Managed update commands -----------------------------------------------------


@pytest.mark.parametrize("edition", EDITIONS)
def test_update_commands_off_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.platform import governance
    from kiro_crew.platform.governance import UpdatePins
    from kiro_crew.platform.update_provider import resolve_provider

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(governance, "active_update_pins", lambda: UpdatePins(apply_command="true"))

    assert (resolve_provider() is None) is enterprise


# --- Minimum sandbox level -------------------------------------------------------


@pytest.mark.parametrize("edition", EDITIONS)
def test_sandbox_off_cannot_be_set_locally_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.config.loader_builders import _build_agent_config

    enterprise = _set_edition(monkeypatch, edition)

    assert _build_agent_config({"sandbox": "off"}).sandbox == ("auto" if enterprise else "off")
    assert _build_agent_config({"sandbox": "strict"}).sandbox == "strict"


@pytest.mark.parametrize("edition", EDITIONS)
def test_knowledge_pool_sandbox_off_cannot_be_set_locally_in_enterprise(
    monkeypatch, edition
) -> None:
    from kiro_crew.knowledge.llm_pool import _get_sandbox_mode

    enterprise = _set_edition(monkeypatch, edition)

    assert _get_sandbox_mode({"agent": {"sandbox": "off"}}) == ("auto" if enterprise else "off")
    assert _get_sandbox_mode({"agent": {"sandbox": "strict"}}) == "strict"


@pytest.mark.parametrize("edition", EDITIONS)
def test_unsandboxed_exec_cannot_be_widened_locally_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.config import loader
    from kiro_crew.config.loader_builders import _build_agent_config

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(loader, "unsandboxed_exec_platform_default", lambda: False)

    built = _build_agent_config({"sandbox_allow_unsandboxed_exec": True})

    assert built.sandbox_allow_unsandboxed_exec is not enterprise
