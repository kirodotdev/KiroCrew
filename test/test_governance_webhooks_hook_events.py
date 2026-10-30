"""The ``webhooks`` scope and the ``capabilities.script_hooks`` ``events`` list.

RFC crew-enterprise-edition, appendix rows "Incoming webhooks", "Hook trigger
events" and Red "Webhooks by source". Each key parses, a list that names X permits
X and refuses Y, and each feature entry asks it. With no policy an ``oss`` build
behaves as before; an enterprise build answers an unnamed ``webhooks`` scope from
the baseline (off).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.platform.governance import parse_policy, resolve

EDITIONS = ("oss", "enterprise")


def _doc(**body: object) -> dict:
    return {"version": 1, "boot": {"fail_closed": True}, **body}


def _webhooks(*allowed: str) -> dict:
    return _doc(webhooks={"mode": "allow", "allow": list(allowed)})


def _hook_events(*allowed: str) -> dict:
    return _doc(
        capabilities={
            "script_hooks": {
                "enabled": True,
                "scopes": {"events": {"mode": "allow", "allow": list(allowed)}},
            }
        }
    )


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)
    return value == "enterprise"


def _install_policy(monkeypatch: pytest.MonkeyPatch, doc: dict | None) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles

    ctx = SimpleNamespace(governance=parse_policy(doc) if doc is not None else None)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)


# ── The keys, through the real evaluator ────────────────────────────────


class TestKeys:
    def test_webhooks_names_its_sources(self) -> None:
        ceiling = parse_policy(_webhooks("agent"))
        assert resolve(ceiling, None, "webhooks", "agent").permitted
        assert not resolve(ceiling, None, "webhooks", "ops-mission-control").permitted

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_unnamed_webhooks_is_off_only_in_enterprise(self, monkeypatch, edition) -> None:
        enterprise = _set_edition(monkeypatch, edition)
        ceiling = parse_policy(_doc())
        assert resolve(ceiling, None, "webhooks", "agent").permitted is not enterprise

    def test_hook_events_list_names_events_case_insensitively(self) -> None:
        ceiling = parse_policy(_hook_events("PreToolUse"))
        scope = "capabilities.script_hooks"
        assert resolve(ceiling, None, scope, "events:PreToolUse").permitted
        # kiro-cli's spelling of the same event.
        assert resolve(ceiling, None, scope, "events:preToolUse").permitted
        assert not resolve(ceiling, None, scope, "events:Stop").permitted
        # An unnamed list permits every event once the gate is on.
        open_gate = parse_policy(_doc(capabilities={"script_hooks": {"enabled": True}}))
        assert resolve(open_gate, None, scope, "events:Stop").permitted

    def test_an_event_query_checks_the_gate_first(self) -> None:
        ceiling = parse_policy(_doc(capabilities={"script_hooks": {"enabled": False}}))
        assert not resolve(ceiling, None, "capabilities.script_hooks", "events:Stop").permitted


# ── Webhook sources ─────────────────────────────────────────────────────


class TestWebhookSources:
    @pytest.mark.parametrize("edition", EDITIONS)
    def test_no_policy_refuses_only_in_enterprise(self, monkeypatch, edition) -> None:
        from kiro_crew.webhook_sources import (
            SOURCE_AGENT,
            SOURCE_OPS_MISSION_CONTROL,
            source_refusal,
        )

        enterprise = _set_edition(monkeypatch, edition)
        _install_policy(monkeypatch, None)
        for source in (SOURCE_AGENT, SOURCE_OPS_MISSION_CONTROL):
            assert bool(source_refusal(source)) is enterprise

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_a_named_source_runs_and_an_unnamed_one_does_not(self, monkeypatch, edition) -> None:
        from kiro_crew.webhook_sources import source_refusal

        _set_edition(monkeypatch, edition)
        _install_policy(monkeypatch, _webhooks("agent"))
        assert source_refusal("agent") == ""
        assert "refused by the security policy" in source_refusal("ops-mission-control")

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_an_evaluation_error_refuses_only_in_enterprise(self, monkeypatch, edition) -> None:
        from kiro_crew.platform import governance_profiles
        from kiro_crew.webhook_sources import source_refusal

        enterprise = _set_edition(monkeypatch, edition)

        def _boom(*_a, **_k):
            raise RuntimeError("transient")

        monkeypatch.setattr(governance_profiles, "governance_permits", _boom)
        assert bool(source_refusal("agent")) is enterprise

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_a_composition_error_propagates_only_in_oss(self, monkeypatch, edition) -> None:
        from kiro_crew.platform import governance_profiles
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.webhook_sources import source_refusal

        enterprise = _set_edition(monkeypatch, edition)

        def _boom(*_a, **_k):
            raise PlatformCompositionError("no context")

        monkeypatch.setattr(governance_profiles, "governance_permits", _boom)
        if enterprise:
            assert "enterprise edition" in source_refusal("agent")
        else:
            with pytest.raises(PlatformCompositionError):
                source_refusal("agent")

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_the_core_switch_follows_the_policy(self, monkeypatch, tmp_path: Path, edition) -> None:
        from kiro_crew import webhooks

        _set_edition(monkeypatch, edition)
        store = webhooks.WebhookTokenStore(tmp_path)
        _install_policy(monkeypatch, _webhooks("agent"))
        assert store.set_switch(True) is True
        assert store.is_switch_on() is True
        _install_policy(monkeypatch, _webhooks("ops-mission-control"))
        assert store.is_switch_on() is False
        assert store.set_switch(True) is False

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_ops_mission_control_asks_its_own_source(self, monkeypatch, edition) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook

        _set_edition(monkeypatch, edition)
        monkeypatch.setattr(webhook, "provider_enabled", lambda _id: False)
        _install_policy(monkeypatch, _webhooks("agent"))
        accepted, detail = webhook.enqueue(b"{}", "sha256=x")
        assert accepted is False
        assert "refused by the security policy" in detail
        # Named: the app's own checks answer next.
        _install_policy(monkeypatch, _webhooks("ops-mission-control"))
        assert webhook.enqueue(b"{}", "sha256=x") == (False, "webhook source is not enabled")


# ── Hook trigger events ─────────────────────────────────────────────────


class TestHookEvents:
    @pytest.mark.parametrize("edition", EDITIONS)
    def test_the_event_list_is_asked(self, monkeypatch, edition) -> None:
        from kiro_crew.hook_event_governance import event_refusal

        _set_edition(monkeypatch, edition)
        _install_policy(monkeypatch, _hook_events("PreToolUse"))
        assert event_refusal("s", "PreToolUse") is None
        assert "refused by the security policy" in (event_refusal("s", "Stop") or "")
        # No event is not asked.
        assert event_refusal("s", "") is None

    def test_oss_without_a_policy_runs_every_event(self, monkeypatch) -> None:
        from kiro_crew.hook_event_governance import event_refusal

        _set_edition(monkeypatch, "oss")
        _install_policy(monkeypatch, None)
        assert event_refusal("s", "Stop") is None

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_an_evaluation_error_refuses_only_in_enterprise(self, monkeypatch, edition) -> None:
        from kiro_crew.hook_event_governance import event_refusal
        from kiro_crew.platform import governance_profiles

        enterprise = _set_edition(monkeypatch, edition)

        def _boom(*_a, **_k):
            raise RuntimeError("transient")

        monkeypatch.setattr(governance_profiles, "governance_permits", _boom)
        assert (event_refusal("s", "Stop") is not None) is enterprise

    @pytest.mark.asyncio
    async def test_run_script_hook_refuses_a_refused_event(self, monkeypatch) -> None:
        from kiro_crew.hooks import HOOK_EVENT_STOP, ScriptHook, run_script_hook

        _set_edition(monkeypatch, "oss")
        _install_policy(monkeypatch, _hook_events("PreToolUse"))
        hook = ScriptHook(id="h", name="h", event=HOOK_EVENT_STOP, command="true")
        result = await run_script_hook(hook)
        assert result.exit_code == 2
        assert "governance" in (result.error or "")
        assert hook.last_status == "blocked"

    @pytest.mark.asyncio
    async def test_the_firing_event_is_judged_not_the_live_hook_field(self, monkeypatch) -> None:
        from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, run_script_hook

        _set_edition(monkeypatch, "oss")
        _install_policy(monkeypatch, _hook_events("PreToolUse"))
        # Dispatched for Stop; the store entry has since been edited to PreToolUse.
        hook = ScriptHook(id="h", name="h", event=HOOK_EVENT_PRE_TOOL_USE, command="true")
        result = await run_script_hook(hook, "", {"hook_event_name": "Stop", "cwd": "/"})
        assert result.exit_code == 2
        assert "'Stop'" in (result.error or "")

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_user_kiro_hooks_of_a_refused_event_are_not_installed(
        self, monkeypatch, tmp_path: Path, edition
    ) -> None:
        from kiro_crew.agent import _apply_user_kiro_hooks

        enterprise = _set_edition(monkeypatch, edition)
        _install_policy(monkeypatch, _hook_events("PreToolUse"))
        script = tmp_path / "hooks" / "a.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        config: dict = {"hooks": {}}
        entry = [{"command": str(script)}]
        mc_cfg = {
            "agent": {
                "kiro_hooks": {"preToolUse": entry, "stop": entry},
                "kiro_hooks_autoimport": False,
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        assert bool(config["hooks"].get("preToolUse"))
        # oss does not govern user kiro hooks, as before.
        assert bool(config["hooks"].get("stop")) is not enterprise
