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
from webhooks_api_helpers import wired_fixture  # noqa: F401

from kiro_crew.platform.governance_parts.archetypes import Decision

EDITIONS = ("oss", "enterprise")


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)
    return value == "enterprise"


# --- Hooks by name ---------------------------------------------------------------


def _policy_permit() -> Decision:
    """What a policy that names and enables the scope answers."""
    return Decision(True, "permitted", rule="rule2-intersect", layer="policy")


def _default_permit() -> Decision:
    """What an ungoverned scope answers: permitted, from no policy."""
    return Decision(True, "not governed", rule="default", layer="default")


def _profile_permit() -> Decision:
    """What a profile alone answers when no policy names the scope."""
    return Decision(True, "permitted", rule="rule2-intersect", layer="profile")


@pytest.mark.parametrize("edition", EDITIONS)
@pytest.mark.parametrize("answer", [_default_permit, _profile_permit])
def test_script_hooks_off_by_default_in_enterprise(monkeypatch, edition, answer) -> None:
    from kiro_crew.hook_runtime import governance_gate
    from kiro_crew.platform import governance_profiles

    enterprise = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(governance_profiles, "governance_permits", lambda *a, **k: answer())

    reason = governance_gate._script_hooks_capability_denied("s")

    assert (reason is not None) is enterprise


@pytest.mark.parametrize("edition", EDITIONS)
def test_script_hooks_follow_the_policy_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.hook_runtime import governance_gate
    from kiro_crew.platform import governance_profiles

    _set_edition(monkeypatch, edition)
    monkeypatch.setattr(governance_profiles, "governance_permits", lambda *a, **k: _policy_permit())

    assert governance_gate._script_hooks_capability_denied("s") is None


@pytest.mark.parametrize("edition", EDITIONS)
def test_script_hooks_fail_closed_on_a_governance_error_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.hook_runtime import governance_gate
    from kiro_crew.platform import governance_profiles

    enterprise = _set_edition(monkeypatch, edition)

    def _boom(*_a, **_k):
        raise RuntimeError("transient")

    monkeypatch.setattr(governance_profiles, "governance_permits", _boom)

    assert (governance_gate._script_hooks_capability_denied("s") is not None) is enterprise


@pytest.mark.parametrize("edition", EDITIONS)
@pytest.mark.parametrize("policy_enables", [False, True])
def test_user_kiro_hooks_follow_the_policy_in_enterprise(
    monkeypatch, tmp_path: Path, edition, policy_enables
) -> None:
    from kiro_crew.agent import _apply_user_kiro_hooks
    from kiro_crew.platform import governance_profiles

    enterprise = _set_edition(monkeypatch, edition)
    seen: list[str] = []

    def _answer(*_a, session_key: str = "", **_k) -> Decision:
        seen.append(session_key)
        return _policy_permit() if policy_enables else _default_permit()

    monkeypatch.setattr(governance_profiles, "governance_permits", _answer)
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

    installed = bool(config["hooks"].get("preToolUse"))
    assert installed is (policy_enables or not enterprise)
    # Asked on the host key only: the gate, then each event's list entry.
    assert set(seen) == ({governance_profiles.HOST_SESSION_KEY} if enterprise else set())


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


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", EDITIONS)
async def test_webhook_switch_enable_refused_in_enterprise(wired, monkeypatch, edition) -> None:
    from webhooks_api_helpers import _payload, _req

    from kiro_crew.dashboard.handlers import hooks as H

    enterprise = _set_edition(monkeypatch, edition)

    on = await H.api_webhooks_switch(_req("POST", "/api/webhooks/switch", {"enabled": True}))
    off = await H.api_webhooks_switch(_req("POST", "/api/webhooks/switch", {"enabled": False}))

    assert on.status == (409 if enterprise else 200)
    if enterprise:
        assert (await _payload(on))["code"] == "locked_by_policy"
    assert off.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", EDITIONS)
async def test_webhook_refusal_names_the_edition_lock_in_enterprise(
    wired, monkeypatch, edition
) -> None:
    from webhooks_api_helpers import _req

    from kiro_crew import webhooks
    from kiro_crew.dashboard.handlers import hooks as H

    enterprise = _set_edition(monkeypatch, edition)
    if not enterprise:
        webhooks.token_store().set_switch(False)

    resp = await H.api_hooks_agent(_req("POST", "/api/hooks/agent", {"message": "hi"}))

    assert resp.status == 503
    detail = webhooks.run_store().list_runs()[0]["detail"]
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

    async def plugins_out_of_sync(self):
        return []


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


def _patch_decisions(monkeypatch, answer) -> list[str]:
    """Answer both decision seams with *answer* and return the audit outcomes written."""
    from kiro_crew import sel as sel_mod
    from kiro_crew.decisions import capability

    rows: list[str] = []

    class _Sel:
        def log_governance_decision(self, **kw) -> None:
            rows.append(kw["outcome"])

    def _audited(*_a, **_k) -> Decision:
        decision = answer()
        rows.append("allowed" if decision.permitted else "denied")
        return decision

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    monkeypatch.setattr(capability, "vet_and_audit", _audited)
    monkeypatch.setattr(capability, "governance_permits", lambda *a, **k: answer())
    return rows


@pytest.mark.parametrize("edition", EDITIONS)
@pytest.mark.parametrize("answer", [_default_permit, _profile_permit])
def test_external_decisions_off_by_default_in_enterprise(monkeypatch, edition, answer) -> None:
    from kiro_crew.decisions import capability

    enterprise = _set_edition(monkeypatch, edition)
    rows = _patch_decisions(monkeypatch, answer)

    assert capability.is_decisions_denied() is enterprise
    assert rows == ["denied" if enterprise else "allowed"]
    assert capability.is_decisions_denied(local=True) is enterprise


@pytest.mark.parametrize("edition", EDITIONS)
def test_external_decisions_follow_the_policy_in_enterprise(monkeypatch, edition) -> None:
    from kiro_crew.decisions import capability

    _set_edition(monkeypatch, edition)
    rows = _patch_decisions(monkeypatch, _policy_permit)

    assert capability.is_decisions_denied() is False
    assert rows == ["allowed"]


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
    from kiro_crew import sandbox_tiers

    enterprise = _set_edition(monkeypatch, edition)

    assert sandbox_tiers._clamp_sandbox_mode_to_floor("off", None) == (
        "auto" if enterprise else "off"
    )
    assert sandbox_tiers._clamp_sandbox_mode_to_floor("strict", None) == "strict"
    assert sandbox_tiers._clamp_sandbox_mode_to_floor("off", "cc") == "cc"


@pytest.mark.parametrize("edition", EDITIONS)
def test_unsandboxed_exec_cannot_be_widened_locally_in_enterprise(monkeypatch, edition) -> None:
    from types import SimpleNamespace

    from kiro_crew import sandbox_unavailable
    from kiro_crew.config import loader

    enterprise = _set_edition(monkeypatch, edition)
    loaded = SimpleNamespace(agent=SimpleNamespace(sandbox_allow_unsandboxed_exec=True))
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: loaded))
    monkeypatch.setattr(loader, "unsandboxed_exec_platform_default", lambda: False)

    assert sandbox_unavailable._allow_unsandboxed_exec() is not enterprise
