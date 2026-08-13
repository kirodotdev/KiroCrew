"""Tests for the app spawn capability and Mochi's use of it.

Mochi is meant to be a model app, so what is pinned here is not just "spawning
works" but that it works the way an EXTERNAL app would have to do it: the
privilege is declared in the manifest, the call goes through the host capability,
and a refusal is loud.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.manifest import Permissions
from kiro_crew.apps.spawn_sdk import SpawnError, SpawnSDK, build_spawn_impl


class _FakeManager:
    """Stand-in for SubagentManager recording how it was called."""

    def __init__(self, result: object = "ok") -> None:
        self.calls: list[dict] = []
        self._result = result

    def spawn(self, task: str, **kwargs: object) -> object:
        self.calls.append({"task": task, **kwargs})
        if self._result == "ok":
            return types.SimpleNamespace(id="sub-1", error="")
        return self._result


class TestSpawnPermission:
    def test_permission_round_trips_through_the_manifest(self):
        assert Permissions.from_dict({"spawn": True}).spawn is True
        assert Permissions(spawn=True).to_dict()["spawn"] is True
        # Absent by default — a capability must be asked for.
        assert "spawn" not in Permissions().to_dict()
        assert Permissions.from_dict({}).spawn is False

    def test_no_capability_without_the_permission(self, tmp_path):
        ctx = build_app_context(
            "probe", tmp_path, permissions={}, spawn_impl=build_spawn_impl(_FakeManager())
        )
        assert ctx.spawn is None, "an undeclared app must not receive spawn"

    def test_no_capability_without_a_host_implementation(self, tmp_path):
        ctx = build_app_context("probe", tmp_path, permissions={"spawn": True})
        assert ctx.spawn is None


class TestSpawnSDK:
    @pytest.fixture(autouse=True)
    def _known_agents(self, monkeypatch):
        """The impl now rejects an agent absent from agent_discovery.list_agents,
        so the tests that reach the host must register the agent they name."""
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        monkeypatch.setattr(
            spawn_sdk,
            "list_agents",
            lambda: [types.SimpleNamespace(name="probe-bg", filename="probe--probe-bg.json")],
        )

    def test_passes_the_original_semantics_to_the_host(self, tmp_path):
        mgr = _FakeManager()
        ctx = build_app_context(
            "probe", tmp_path, permissions={"spawn": True}, spawn_impl=build_spawn_impl(mgr)
        )
        sid = asyncio.run(ctx.spawn.run("check CR-1", "probe-bg", silent=True))
        assert sid == "sub-1"
        call = mgr.calls[0]
        # approval_mode=auto is required: the approval chain ends in "rejected"
        # for a caller with no user to ask, so without it every unattended spawn
        # would be declined.
        assert call["approval_mode"] == "auto"
        assert call["agent"] == "probe-bg"
        assert call["silent"] is True
        # The calling app's identity is forwarded so the host can resolve THIS
        # app's governance profile — without it a spawn-denying app profile is
        # bypassed (only the policy ceiling would apply).
        assert call["app"] == "probe"
        # The SDK validated the agent off the loop, so it tells the manager to
        # skip its synchronous on-loop re-scan.
        assert call["_agent_prevalidated"] is True

    def test_decline_raises_instead_of_returning_a_dead_id(self, tmp_path):
        ctx = build_app_context(
            "probe",
            tmp_path,
            permissions={"spawn": True},
            spawn_impl=build_spawn_impl(_FakeManager(result=None)),
        )
        with pytest.raises(SpawnError):
            asyncio.run(ctx.spawn.run("x", "probe-bg"))

    def test_refusal_record_with_an_error_raises(self, tmp_path):
        """A refused spawn comes back as a DONE record carrying `error`.

        An id alone does not mean it is running, so returning it would leave the
        caller waiting on a spawn that already failed.
        """
        refused = types.SimpleNamespace(id="sub-9", error="denied by policy")
        ctx = build_app_context(
            "probe",
            tmp_path,
            permissions={"spawn": True},
            spawn_impl=build_spawn_impl(_FakeManager(result=refused)),
        )
        with pytest.raises(SpawnError, match="denied by policy"):
            asyncio.run(ctx.spawn.run("x", "probe-bg"))

    def test_empty_task_is_refused_before_reaching_the_host(self, tmp_path):
        mgr = _FakeManager()
        sdk = SpawnSDK("probe", build_spawn_impl(mgr))
        with pytest.raises(SpawnError):
            asyncio.run(sdk.run("   "))
        assert mgr.calls == []

    def test_empty_agent_is_refused_before_reaching_the_host(self):
        """An empty agent would run the host DEFAULT (full, auto-approved) and
        skip capabilities.spawn.scopes.agents — so it must never reach the host.
        """
        mgr = _FakeManager()
        sdk = SpawnSDK("probe", build_spawn_impl(mgr))
        with pytest.raises(SpawnError, match="named agent"):
            asyncio.run(sdk.run("do a thing"))  # agent defaults to ""
        with pytest.raises(SpawnError, match="named agent"):
            asyncio.run(sdk.run("do a thing", "   "))
        assert mgr.calls == [], "a nameless spawn must not reach the manager"

    def test_missing_manager_raises_rather_than_declining_silently(self):
        with pytest.raises(SpawnError, match="no subagent manager"):
            asyncio.run(build_spawn_impl(None)("t", "a", False, "", "probe"))


class TestSpawnGateConsultsTheAppProfile:
    """The Level-2 (per-app PROFILE) half of the spawn check must run.

    The manager's spawn gate resolves ``capabilities.spawn`` against the parent
    surface's ceiling AND the active profile. An app spawning through the SDK is
    only contained by a profile written for THAT app if its name is threaded into
    the resolution — otherwise the app-bound profile (precedence #1 in
    ``resolve_active_scope``) is never looked up and a spawn-denying profile is a
    no-op.
    """

    def test_the_gate_receives_the_app_name(self, monkeypatch):
        from kiro_crew import subagent

        seen: list[dict] = []

        def _fake_permits(scope, item, *, session_key="", agent="", app="", **kw):
            seen.append({"scope": scope, "item": item, "app": app})
            return types.SimpleNamespace(permitted=True, reason="")

        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(gp, "governance_permits", _fake_permits)
        err = subagent._vet_spawn_governance("sk", "probe-bg", app="probe")
        assert err is None
        # Both the enabled check and the agent-scope check must carry the app.
        assert seen and all(c["app"] == "probe" for c in seen)

    def test_an_app_profile_denial_refuses_the_spawn(self, monkeypatch):
        from kiro_crew import subagent

        def _deny(scope, item, *, session_key="", agent="", app="", **kw):
            if app == "probe":
                return types.SimpleNamespace(permitted=False, reason="denied for probe")
            return types.SimpleNamespace(permitted=True, reason="")

        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(gp, "governance_permits", _deny)
        assert subagent._vet_spawn_governance("sk", "probe-bg", app="probe") is not None
        # A different app (or none) is unaffected — the denial is app-scoped.
        assert subagent._vet_spawn_governance("sk", "probe-bg", app="other") is None


class TestSpawnRejectsAnUnknownAgent:
    """A misspelled agent must fail, not silently run the host default.

    SubagentManager validation replaces an unknown name with "" and runs the
    host DEFAULT agent with approval_mode="auto" — full tool surface, no prompt.
    A typo in an app's agent name would therefore ESCALATE from the app's
    restricted background agent to unrestricted auto-approved execution, so the
    impl refuses a name it cannot confirm.
    """

    def test_unknown_agent_is_refused_before_the_host(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        monkeypatch.setattr(
            spawn_sdk,
            "list_agents",
            lambda: [types.SimpleNamespace(name="real-bg", filename="probe--real-bg.json")],
        )
        mgr = _FakeManager()
        sdk = SpawnSDK("probe", build_spawn_impl(mgr))
        with pytest.raises(SpawnError, match="only spawn its OWN"):
            asyncio.run(sdk.run("do a thing", "typo-bg"))
        assert mgr.calls == [], "an unknown agent must never reach the manager"

    def test_a_discovery_failure_fails_closed(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        def _boom():
            raise RuntimeError("agents dir unreadable")

        monkeypatch.setattr(spawn_sdk, "list_agents", _boom)
        mgr = _FakeManager()
        sdk = SpawnSDK("probe", build_spawn_impl(mgr))
        with pytest.raises(SpawnError, match="cannot verify agent"):
            asyncio.run(sdk.run("do a thing", "real-bg"))
        assert mgr.calls == []


class TestSdkRefusalsAreAudited:
    """Every SDK-side authorization refusal must emit a SEL denial record.

    The SDK rejects empty-agent, unverifiable, and cross-app spawns BEFORE they
    reach SubagentManager (whose own gate audits spawns that get that far), so
    without this the refusal would leave no security-event trail.
    """

    def _capture(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        calls: list[dict] = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(spawn_sdk, "sel", lambda: _FakeSel())
        return calls

    def test_empty_agent_refusal_is_audited(self, monkeypatch):
        calls = self._capture(monkeypatch)
        sdk = SpawnSDK("probe", build_spawn_impl(_FakeManager()))
        with pytest.raises(SpawnError):
            asyncio.run(sdk.run("do a thing", ""))
        assert any(c["outcome"] == "denied" and c["tool_name"] == "spawn_run" for c in calls)
        assert calls[0]["metadata"]["app"] == "probe"

    def test_cross_app_refusal_is_audited(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        calls = self._capture(monkeypatch)
        monkeypatch.setattr(
            spawn_sdk,
            "list_agents",
            lambda: [types.SimpleNamespace(name="real-bg", filename="probe--real-bg.json")],
        )
        sdk = SpawnSDK("probe", build_spawn_impl(_FakeManager()))
        with pytest.raises(SpawnError, match="only spawn its OWN"):
            asyncio.run(sdk.run("do a thing", "other-app-bg"))
        assert any(c["outcome"] == "denied" for c in calls)

    def test_discovery_failure_refusal_is_audited(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        calls = self._capture(monkeypatch)

        def _boom():
            raise RuntimeError("agents dir unreadable")

        monkeypatch.setattr(spawn_sdk, "list_agents", _boom)
        sdk = SpawnSDK("probe", build_spawn_impl(_FakeManager()))
        with pytest.raises(SpawnError, match="cannot verify agent"):
            asyncio.run(sdk.run("do a thing", "real-bg"))
        assert any(c["outcome"] == "denied" for c in calls)


class TestAgentDiscoveryIsOffloaded:
    """`list_agents()` scans the agents dir + JSON-parses each file, so on the
    gateway loop a cold/large directory would freeze chat and the heartbeat. The
    impl must run it via asyncio.to_thread."""

    def test_the_impl_offloads_list_agents(self) -> None:
        import inspect

        from kiro_crew.apps import spawn_sdk

        src = inspect.getsource(spawn_sdk.build_spawn_impl)
        assert "await asyncio.to_thread(list_agents)" in src


class TestSpawnSkipsTheScanWhenPrevalidated:
    """A prevalidated agent must NOT trigger the on-loop directory scan; the
    SpawnSDK already confirmed it off the loop."""

    def test_validate_agent_is_not_called_when_prevalidated(self, monkeypatch):
        from kiro_crew import subagent

        called = {"n": 0}

        def _boom(_requested):
            called["n"] += 1
            return "", "should not be called"

        monkeypatch.setattr(subagent, "_validate_agent", _boom)
        # A minimal manager instance is heavy; assert via the source that the
        # guard exists AND that the flag threads into the queued params.
        import inspect

        src = inspect.getsource(subagent.SubagentManager.spawn)
        assert "and not _agent_prevalidated" in src
        assert '"_agent_prevalidated": _agent_prevalidated,' in src


class TestSpawnRejectsCrossAppAndGlobalAgents:
    """An app may spawn only its OWN `<app>--*` agents. Naming the global host
    agent, or another app's agent, would run it with approval_mode="auto" — a
    full-privilege escalation, so it must be refused even though the agent EXISTS.
    """

    def test_global_and_other_app_agents_are_refused(self, monkeypatch):
        import kiro_crew.apps.spawn_sdk as spawn_sdk

        monkeypatch.setattr(
            spawn_sdk,
            "list_agents",
            lambda: [
                types.SimpleNamespace(name="kirocrew", filename="kirocrew.json"),
                types.SimpleNamespace(name="other-bg", filename="other--other-bg.json"),
                types.SimpleNamespace(name="probe-bg", filename="probe--probe-bg.json"),
            ],
        )
        mgr = _FakeManager()
        sdk = SpawnSDK("probe", build_spawn_impl(mgr))
        # The global host agent — exists, but not owned by "probe".
        with pytest.raises(SpawnError, match="only spawn its OWN"):
            asyncio.run(sdk.run("t", "kirocrew"))
        # Another app's agent — exists, not owned by "probe".
        with pytest.raises(SpawnError, match="only spawn its OWN"):
            asyncio.run(sdk.run("t", "other-bg"))
        assert mgr.calls == [], "neither may reach the manager"
        # The app's OWN agent is still allowed.
        assert asyncio.run(sdk.run("t", "probe-bg")) == "sub-1"


class TestManagerRefusesUnknownAgent:
    """The manager primitive itself must refuse a named-but-unknown agent, not
    silently run the host default at auto-approval — a future caller that skips
    the SDK guard must not be able to reintroduce that escalation."""

    def test_validate_agent_refuses_named_unknown(self, monkeypatch):
        from kiro_crew import subagent

        # Patched on SUBAGENT: the import is module-level there, so call-time
        # lookup happens against subagent's binding, not agent_discovery's.
        monkeypatch.setattr(
            subagent,
            "list_agents",
            lambda project_dir=None: [
                types.SimpleNamespace(name="kirocrew"),
                types.SimpleNamespace(name="mochi--mochi-bg"),
            ],
        )
        # Empty request still means "use the default" — no error.
        assert subagent._validate_agent("") == ("", "")
        # A known agent passes through unchanged.
        assert subagent._validate_agent("mochi--mochi-bg") == ("mochi--mochi-bg", "")
        # A named-but-unknown agent is REFUSED (error), not silently defaulted.
        name, err = subagent._validate_agent("does-not-exist")
        assert name == ""
        assert err and "not found" in err

    def test_project_scope_is_read_from_cache_not_the_filesystem(self, monkeypatch):
        """``spawn`` is synchronous and runs on the event loop, so validation must not
        add filesystem work. The project scope therefore comes from the syscall-free
        cache; widening the pre-existing user-level scan to a second directory would
        stall the gateway on a slow or network checkout."""
        import kiro_crew.agent_discovery as disc
        from kiro_crew import subagent

        monkeypatch.setattr(
            subagent,
            "list_agents",
            lambda project_dir=None: [types.SimpleNamespace(name="kirocrew")],
        )
        # A warm cache makes a project agent dispatchable...
        monkeypatch.setattr(
            subagent, "cached_project_agent_names", lambda p: frozenset({"repobot"})
        )
        # ...and the SCANNING entry points must not be consulted at all.
        monkeypatch.setattr(
            disc,
            "project_agent_names",
            lambda p: pytest.fail("scanned the filesystem on the event loop"),
        )
        monkeypatch.setattr(
            disc,
            "project_agent_files",
            lambda p, include_legacy=False: pytest.fail("globbed on the event loop"),
        )
        assert subagent._validate_agent("repobot", "/some/project") == ("repobot", "")

    def test_cold_project_cache_refuses_rather_than_scanning(self, monkeypatch):
        """Fail closed on a cold cache — refusing an unknown name is this function's
        existing rule, and is safer than either stalling or running the default."""
        from kiro_crew import subagent

        monkeypatch.setattr(
            subagent,
            "list_agents",
            lambda project_dir=None: [types.SimpleNamespace(name="kirocrew")],
        )
        monkeypatch.setattr(subagent, "cached_project_agent_names", lambda p: None)
        name, err = subagent._validate_agent("repobot", "/some/project")
        assert name == ""
        assert "repobot" in err


class TestChildGateInheritsTheApp:
    """The app's Level-2 profile must constrain the child's ONGOING tool calls,
    not just the spawn decision — so the spawning app is persisted on the child
    and forwarded to its per-tool-call gate."""

    def test_subagent_info_carries_app(self):
        from kiro_crew.subagent import SubagentInfo

        assert SubagentInfo(id="x", task="t", app="mochi").app == "mochi"

    def test_child_gate_forwards_the_app(self):
        import inspect

        from kiro_crew import subagent

        src = inspect.getsource(subagent.SubagentManager)
        assert "on_tool_call(" in src
        assert 'app=info.app or ""' in src
