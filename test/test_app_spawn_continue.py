"""``ctx.spawn.continue_``: an app hands a follow-up task to a finished run.

The checks are those of ``ctx.spawn.run``: the conversation must be one the app
started with one of its own ``<app>--*`` agents, the follow-up names that agent so
the manager's governance gate evaluates it, and the run is auto-approved. A
conversation that expired or is busy raises with a ``code`` the app switches on
to fall back to a fresh ``run``.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager

from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.spawn_sdk import (
    CONVERSATION_BUSY,
    CONVERSATION_GONE,
    SpawnError,
    SpawnSDK,
    build_spawn_impl,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers


class _FakeManager:
    """Stand-in for SubagentManager recording spawns and continuations."""

    def __init__(self, *, owner=("probe", "probe-bg"), result: object = "ok") -> None:
        self.spawns: list[dict] = []
        self.continues: list[dict] = []
        self._owner = owner
        self._result = result

    def spawn(self, task: str, **kwargs: object) -> object:
        self.spawns.append({"task": task, **kwargs})
        return types.SimpleNamespace(id="sub-1", error="")

    def get(self, run_id: str) -> object:
        if self._owner is None:
            return None
        app, agent = self._owner
        return types.SimpleNamespace(id=run_id, app=app, agent=agent, done=True)

    def recorded_cwd(self, conv_id: str) -> str:
        return "/work/tree"

    async def continue_conversation_async(self, conv_id: str, task: str, **kwargs: object):
        self.continues.append({"conv": conv_id, "task": task, **kwargs})
        if self._result == "ok":
            return types.SimpleNamespace(
                id="sub-2", error="", conversation_key=f"subagent:{conv_id}"
            )
        return self._result


def _refusal(error: str) -> object:
    return types.SimpleNamespace(id="x", done=True, error=error, error_code="")


@pytest.fixture(autouse=True)
def _known_agents(_floor_monkeypatch):
    import kiro_crew.apps.spawn_sdk as spawn_sdk

    _floor_monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [
            types.SimpleNamespace(name="probe-bg", filename="probe--probe-bg.json"),
            types.SimpleNamespace(name="other-bg", filename="other--other-bg.json"),
            types.SimpleNamespace(name="kirocrew", filename="kirocrew.json"),
        ],
    )


@pytest.fixture
def denials(monkeypatch):
    calls: list[dict] = []
    import kiro_crew.apps.spawn_sdk as spawn_sdk

    class _Sel:
        def log_tool_invocation(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(spawn_sdk, "sel", lambda: _Sel())
    return calls


def _sdk(tmp_path, mgr) -> SpawnSDK:
    ctx = build_app_context(
        "probe", tmp_path, permissions={"spawn": True}, spawn_impl=build_spawn_impl(mgr)
    )
    return ctx.spawn


class TestContinueRunsWithTheSpawnChecks:
    def test_continues_with_the_runs_agent_auto_approved(self, tmp_path):
        mgr = _FakeManager()
        run_id = asyncio.run(
            _sdk(tmp_path, mgr).continue_("sub-1", "fix the findings", silent=True)
        )
        assert run_id == "sub-2"
        call = mgr.continues[0]
        assert call["conv"] == "sub-1" and call["task"] == "fix the findings"
        # No user is present to approve a tool call, exactly as for run().
        assert call["approval_mode"] == "auto"
        # The agent is named, so the governance gate's agent scope evaluates it.
        assert call["agent"] == "probe-bg"
        assert call["silent"] is True
        assert call["cwd"] == "/work/tree"
        assert call["model"] is None

    def test_a_named_model_is_forwarded(self, tmp_path):
        mgr = _FakeManager()
        asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "again", model="cheap"))
        assert mgr.continues[0]["model"] == "cheap"

    def test_another_apps_conversation_is_refused_and_audited(self, tmp_path, denials):
        mgr = _FakeManager(owner=("other", "other-bg"))
        with pytest.raises(SpawnError, match="may only continue"):
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "steal it"))
        assert mgr.continues == []
        assert denials and denials[0]["outcome"] == "denied"
        assert denials[0]["tool_name"] == "spawn_continue"

    def test_a_user_conversation_is_refused(self, tmp_path, denials):
        # A chat-spawned run carries no app; an app must not drive it.
        mgr = _FakeManager(owner=("", "kirocrew"))
        with pytest.raises(SpawnError):
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "drive it"))
        assert mgr.continues == []

    def test_a_run_of_a_non_app_agent_is_refused(self, tmp_path, denials):
        mgr = _FakeManager(owner=("probe", "kirocrew"))
        with pytest.raises(SpawnError, match="OWN agents"):
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "escalate"))
        assert mgr.continues == []
        assert denials

    def test_an_unreadable_owner_fails_closed(self, tmp_path, denials, monkeypatch):
        import kiro_crew.subagent_persistence as persistence

        def _gone(_conv):
            raise ValueError("memory_unavailable: run record is unavailable")

        monkeypatch.setattr(persistence, "read_run_execution", _gone)
        # The folder is there, so this is an unreadable record, not an expiry.
        persistence._agent_dir("sub-1").mkdir(parents=True)
        mgr = _FakeManager(owner=None)
        with pytest.raises(SpawnError, match="cannot verify") as caught:
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "resume"))
        assert caught.value.code == ""
        assert mgr.continues == []
        assert denials

    def test_a_pruned_run_is_gone_not_unverifiable(self, tmp_path, denials, monkeypatch):
        # Retention removed the folder and eviction the live record: an ordinary
        # expiry, which the app answers with a fresh run.
        import kiro_crew.subagent_persistence as persistence

        assert not persistence._agent_dir("sub-1").exists()
        mgr = _FakeManager(owner=None)
        with pytest.raises(SpawnError) as caught:
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "resume"))
        assert caught.value.code == CONVERSATION_GONE
        assert mgr.continues == []
        assert denials and denials[0]["tool_name"] == "spawn_continue"

    def test_an_evicted_run_is_checked_from_its_run_record(self, tmp_path, monkeypatch):
        import kiro_crew.subagent_persistence as persistence

        monkeypatch.setattr(
            persistence,
            "read_run_execution",
            lambda _conv: types.SimpleNamespace(app="probe", template_id="probe-bg"),
        )
        mgr = _FakeManager(owner=None)
        assert asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "resume")) == "sub-2"
        assert mgr.continues[0]["agent"] == "probe-bg"


class TestContinueRefusalsAreTyped:
    @pytest.mark.parametrize(
        "error,code",
        [
            ("conversation_gone: no resumable session remains for sub-1", CONVERSATION_GONE),
            ("conversation_busy: run sub-3 is in flight on this conversation", CONVERSATION_BUSY),
        ],
    )
    def test_gone_and_busy_carry_a_code(self, tmp_path, error, code):
        mgr = _FakeManager(result=_refusal(error))
        with pytest.raises(SpawnError) as raised:
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "again"))
        assert raised.value.code == code
        assert raised.value.declined is False

    def test_any_other_refusal_has_no_code(self, tmp_path):
        mgr = _FakeManager(result=_refusal("spawn refused by governance"))
        with pytest.raises(SpawnError) as raised:
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "again"))
        assert raised.value.code == ""

    def test_a_capacity_decline_is_declined(self, tmp_path):
        mgr = _FakeManager(result=None)
        with pytest.raises(SpawnError) as raised:
            asyncio.run(_sdk(tmp_path, mgr).continue_("sub-1", "again"))
        assert raised.value.declined is True

    def test_empty_task_and_id_are_refused_before_the_host(self, tmp_path):
        mgr = _FakeManager()
        sdk = _sdk(tmp_path, mgr)
        with pytest.raises(SpawnError):
            asyncio.run(sdk.continue_("sub-1", "  "))
        with pytest.raises(SpawnError):
            asyncio.run(sdk.continue_("", "again"))
        assert mgr.continues == []

    def test_a_host_without_continuation_raises(self):
        async def _impl(*_a, **_k):
            return "sub-1"

        with pytest.raises(SpawnError, match="cannot continue"):
            asyncio.run(SpawnSDK("probe", _impl).continue_("sub-1", "again"))


class TestRunCanAskToKeepTheConversation:
    def test_keep_reaches_the_host(self, tmp_path):
        mgr = _FakeManager()
        asyncio.run(_sdk(tmp_path, mgr).run("work", "probe-bg", keep=True))
        assert mgr.spawns[0]["keep"] is True

    def test_keep_is_off_by_default(self, tmp_path):
        mgr = _FakeManager()
        asyncio.run(_sdk(tmp_path, mgr).run("work", "probe-bg"))
        assert mgr.spawns[0]["keep"] is False

    def test_an_impl_without_keep_still_serves_a_plain_run(self):
        seen: list[tuple] = []

        async def _impl(task, agent, silent, model, app):
            seen.append((task, agent))
            return "sub-1"

        assert asyncio.run(SpawnSDK("probe", _impl).run("work", "probe-bg")) == "sub-1"
        assert seen == [("work", "probe-bg")]


class TestManagerForwardsTheFollowUpsApprovalMode:
    @pytest.mark.asyncio
    async def test_approval_mode_and_silent_reach_spawn(self):
        manager = _manager()
        captured: list[dict] = []

        async def _spawn_async(**kwargs):
            captured.append(kwargs)
            return SubagentInfo(id="cont0001", task=kwargs["task"])

        prelude = {"task": "again", "agent": "probe-bg", "app": "probe", "keep": True}
        with (
            patch.object(
                SubagentManager, "_continue_prelude", side_effect=lambda *a, **k: dict(prelude)
            ),
            patch.object(manager, "spawn_async", new=_spawn_async),
        ):
            await manager.continue_conversation_async(
                "orig0001", "again", approval_mode="auto", silent=True
            )
            await manager.continue_conversation_async("orig0002", "again")
        assert captured[0]["approval_mode"] == "auto" and captured[0]["silent"] is True
        # A caller that names neither keeps spawn's own defaults.
        assert "approval_mode" not in captured[1] and "silent" not in captured[1]
