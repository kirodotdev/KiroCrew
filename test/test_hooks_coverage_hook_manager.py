"""Coverage of ``kiro_crew.hooks``' hook manager beyond the file helpers.

The boot-warmed builtin-app registries the gate reads, the computer-use and
tool-kind read-only auto-approve branches, message transforms, script hooks
(their dataclasses, the store's persistence and rollback contract, governance,
and dispatch with its ordering, matcher filtering and failure isolation), and
the fail-soft and fail-closed discipline of the gate's governance helpers.
Shared helpers live in ``hooks_coverage_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from hooks_coverage_helpers import (
    _write,
)

from kiro_crew import hooks as hooks_mod
from kiro_crew import security, webhooks
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_MODIFY,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    HookManager,
    HooksConfig,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
    TransformHook,
    _app_owns_mcp_server,
    _audit_governance,
    _builtin_app_for_agent,
    _cu_read_only_auto_approve,
    _governance_denial,
    _governance_pinned_command_ids,
    _is_declared_builtin_mcp_server,
    _is_first_party_app,
    _script_hooks_capability_denied,
    fire_tool_hooks,
    get_global_hook_store,
    run_script_hook,
    set_builtin_app_agents,
    set_builtin_app_mcp_servers,
    set_builtin_app_names,
    set_global_hook_store,
)


class _StubDecision:
    def __init__(self, permitted: bool, reason: str = "") -> None:
        self.permitted = permitted
        self.reason = reason


@pytest.fixture
def restore_builtin_registries():
    """Snapshot/restore the boot-warmed module globals the gate reads."""
    saved = (
        hooks_mod._BUILTIN_APP_NAMES,
        hooks_mod._BUILTIN_APP_MCP_SERVERS,
        dict(hooks_mod._BUILTIN_APP_AGENTS),
    )
    yield
    hooks_mod._BUILTIN_APP_NAMES = saved[0]
    hooks_mod._BUILTIN_APP_MCP_SERVERS = saved[1]
    hooks_mod._BUILTIN_APP_AGENTS = saved[2]


# ── boot-warmed builtin-app registries ──


class TestBuiltinAppRegistries:
    def test_mcp_server_set_is_casefolded_and_junk_dropped(self, restore_builtin_registries):
        set_builtin_app_mcp_servers(["Meetings:Srv", "", None, 5, "papyrus:tools"])
        assert _is_declared_builtin_mcp_server("meetings:srv") is True
        assert _is_declared_builtin_mcp_server("MEETINGS:SRV") is True
        assert _is_declared_builtin_mcp_server("papyrus:tools") is True
        assert _is_declared_builtin_mcp_server("meetings:evil") is False
        assert _is_declared_builtin_mcp_server("") is False

    def test_unwarmed_mcp_set_fails_closed(self, restore_builtin_registries):
        set_builtin_app_mcp_servers([])
        assert _is_declared_builtin_mcp_server("meetings:srv") is False

    def test_first_party_lookup_is_casefolded(self, restore_builtin_registries):
        set_builtin_app_names(["Meetings", "", 7])
        assert _is_first_party_app("meetings") is True
        assert _is_first_party_app("MEETINGS") is True
        assert _is_first_party_app("third-party") is False
        assert _is_first_party_app("") is False

    def test_agent_map_is_casefolded_and_junk_dropped(self, restore_builtin_registries):
        set_builtin_app_agents({"Mochi": "mochi", "": "x", "a": "", 3: "y"})
        assert _builtin_app_for_agent("mochi") == "mochi"
        assert _builtin_app_for_agent("MOCHI") == "mochi"
        assert _builtin_app_for_agent("unknown") == ""
        assert _builtin_app_for_agent("") == ""

    def test_agent_map_replacement_is_idempotent(self, restore_builtin_registries):
        set_builtin_app_agents({"a": "app-a"})
        set_builtin_app_agents({"b": "app-b"})
        assert _builtin_app_for_agent("a") == ""
        assert _builtin_app_for_agent("b") == "app-b"

    @pytest.mark.parametrize(
        ("server", "app", "expected"),
        [
            ("meetings:srv", "meetings", True),
            ("Meetings:srv", "MEETINGS", True),
            ("meetings:srv", "papyrus", False),
            ("kirocrew-cron", "meetings", False),
            ("", "meetings", False),
            ("meetings:srv", "", False),
        ],
    )
    def test_app_owns_mcp_server(self, server, app, expected):
        assert _app_owns_mcp_server(server, app) is expected


class TestComputerUseReadOnlyAutoApprove:
    def test_a_non_computer_use_title_never_auto_approves(self):
        assert _cu_read_only_auto_approve("execute_bash") is False
        assert _cu_read_only_auto_approve("") is False

    def test_enable_state_probe_failure_fails_closed(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "computer_use_action_from_title", lambda name: "get_state")
        monkeypatch.setattr(
            hooks_mod, "computer_use_action_classes", lambda action: (hooks_mod.CU_CLASS_OBSERVE,)
        )
        import kiro_crew.computer_use as cu

        class _Boom:
            @staticmethod
            def is_enabled():
                raise RuntimeError("keystone unreadable")

        monkeypatch.setattr(cu, "enable_state", _Boom, raising=False)
        assert _cu_read_only_auto_approve("mcp__kirocrew-computer__computer_get_state") is False

    def test_a_mutating_action_is_not_read_only(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "computer_use_action_from_title", lambda name: "click")
        monkeypatch.setattr(hooks_mod, "computer_use_action_classes", lambda action: ("mutate",))
        assert _cu_read_only_auto_approve("mcp__kirocrew-computer__computer_click") is False


# ── script hook dataclasses ──


class TestScriptHookDataclasses:
    def test_legacy_pattern_field_maps_to_matcher(self):
        hook = ScriptHook.from_dict({"pattern": "fs_*"})
        assert hook.matcher == "fs_*"

    def test_matcher_wins_over_legacy_pattern(self):
        hook = ScriptHook.from_dict({"pattern": "old", "matcher": "new"})
        assert hook.matcher == "new"

    def test_defaults_are_filled(self):
        hook = ScriptHook.from_dict({})
        assert hook.id and hook.event == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert hook.timeout == 30 and hook.enabled is True

    def test_result_classification(self):
        blocked = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=2)
        ok = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=0)
        failed = ScriptHookResult(hook_id="a", hook_name="a", event="x", exit_code=1)
        assert blocked.blocked is True and blocked.succeeded is False
        assert ok.succeeded is True and ok.blocked is False
        assert failed.succeeded is False and failed.blocked is False


# ── script hook store persistence ──


class TestScriptHookStorePersistence:
    def test_foreign_top_level_keys_survive_a_mutation(self, tmp_path):
        path = tmp_path / "hooks.json"
        _write(path, json.dumps({"webhook-ctx-1": {"note": "resume me"}, "hooks": []}))
        store = ScriptHookStore(tmp_path)
        store.create({"name": "h1", "command": "true"})
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["webhook-ctx-1"] == {"note": "resume me"}
        assert len(on_disk["hooks"]) == 1

    def test_corrupt_file_is_not_overwritten(self, tmp_path):
        path = _write(tmp_path / "hooks.json", "{not json")
        store = ScriptHookStore(tmp_path)  # _load logs and continues
        assert store.list_all() == []
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.create({"name": "h1", "command": "true"})
        # The unreadable file is left for an operator to repair.
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_a_failed_persist_rolls_back_the_in_memory_set(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        _write(tmp_path / "hooks.json", "{not json")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.delete(hook.id)
        # The delete did not reach disk, so it must not be visible in memory.
        assert store.get(hook.id) is not None

    def test_toggle_rollback_restores_the_stored_object(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true", "enabled": True})
        _write(tmp_path / "hooks.json", "{not json")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.toggle(hook.id)
        # A shallow dict copy would share the ScriptHook and restore nothing.
        assert store.get(hook.id).enabled is True

    def test_load_reads_hooks_back(self, tmp_path):
        first = ScriptHookStore(tmp_path)
        first.create({"id": "keepme", "name": "h1", "command": "true"})
        second = ScriptHookStore(tmp_path)
        assert [h.id for h in second.list_all()] == ["keepme"]

    def test_update_rejects_an_unknown_event(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="invalid event"):
            store.update(hook.id, {"event": "NotAnEvent"})

    @pytest.mark.parametrize("bad", [0, 301, -5, "30", 3.5, None])
    def test_update_rejects_an_out_of_range_timeout(self, tmp_path, bad):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="timeout must be"):
            store.update(hook.id, {"timeout": bad})

    def test_update_accepts_the_range_bounds(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        assert store.update(hook.id, {"timeout": 1}).timeout == 1
        assert store.update(hook.id, {"timeout": 300}).timeout == 300

    def test_a_bool_timeout_is_rejected(self, tmp_path):
        # ``bool`` is an ``int`` subclass, but ``True`` as a timeout is
        # meaningless, so the shared validator rejects it at the update
        # boundary rather than silently landing a 1-second timeout.
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        with pytest.raises(ValueError, match="timeout must be an integer"):
            store.update(hook.id, {"timeout": True})

    def test_update_applies_only_known_fields(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        updated = store.update(
            hook.id, {"name": "h2", "timeout": 12, "run_count": 999, "unknown": "x"}
        )
        assert updated is not None
        assert updated.name == "h2" and updated.timeout == 12
        assert updated.run_count == 0
        assert not hasattr(updated, "unknown")

    def test_mutations_on_a_missing_hook_are_no_ops(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        assert store.update("nope", {"name": "x"}) is None
        assert store.delete("nope") is False
        assert store.toggle("nope") is None

    def test_toggle_flips_and_persists(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true", "enabled": True})
        assert store.toggle(hook.id).enabled is False
        assert ScriptHookStore(tmp_path).get(hook.id).enabled is False

    def test_delete_removes_from_disk(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "h1", "command": "true"})
        assert store.delete(hook.id) is True
        assert ScriptHookStore(tmp_path).list_all() == []

    def test_status_bookkeeping_never_raises_out_of_persist(self, tmp_path, caplog):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "h1", "command": "true"})
        _write(tmp_path / "hooks.json", "{not json")
        # fire() is awaited from the PreToolUse path, so a corrupt file must not
        # turn every tool call into a rejection.
        store._persist_current()
        assert any("bookkeeping" in r.message for r in caplog.records)

    def test_save_snapshot_writes_the_given_list(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        store._save_snapshot([{"id": "snap", "name": "s", "command": "true"}])
        on_disk = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        assert [h["id"] for h in on_disk["hooks"]] == ["snap"]


class TestGlobalHookStore:
    def test_set_and_get(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
        assert get_global_hook_store() is None
        store = ScriptHookStore(tmp_path)
        set_global_hook_store(store)
        assert get_global_hook_store() is store


# ── script hook governance + dispatch ──


class TestScriptHookGovernance:
    def test_no_opinion_when_governance_permits(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(
            gp, "governance_permits", lambda *a, **k: _StubDecision(True), raising=False
        )
        assert _script_hooks_capability_denied("slot:1") is None

    def test_denial_reason_is_returned(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(
            gp,
            "governance_permits",
            lambda *a, **k: _StubDecision(False, "script hooks off"),
            raising=False,
        )
        assert _script_hooks_capability_denied() == "script hooks off"

    def test_a_transient_governance_error_degrades_to_no_opinion(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        def _boom(*a, **k):
            raise RuntimeError("profile store glitch")

        monkeypatch.setattr(gp, "governance_permits", _boom, raising=False)
        monkeypatch.setattr(gp, "audit_governance_degraded", _boom, raising=False)
        # A glitch must not wedge every script hook.
        assert _script_hooks_capability_denied() is None

    def test_composition_error_fails_closed(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(*a, **k):
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(gp, "governance_permits", _boom, raising=False)
        with pytest.raises(PlatformCompositionError):
            _script_hooks_capability_denied()

    @pytest.mark.asyncio
    async def test_a_denied_hook_never_spawns_a_subprocess(self, monkeypatch):
        monkeypatch.setattr(
            hooks_mod, "_script_hooks_capability_denied", lambda sk: "capability disabled"
        )

        def _no_spawn(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("run_script_hook spawned a subprocess despite the deny")

        monkeypatch.setattr(hooks_mod.asyncio, "create_subprocess_shell", _no_spawn)
        hook = ScriptHook(id="h1", name="blocked-hook", command="echo hi")
        result = await run_script_hook(hook, "ctx", {"parent_session_key": "slot:1"})
        assert result.exit_code == 2  # PreToolUse "block tool" convention
        assert "capability disabled" in result.error
        assert hook.last_status == "blocked"
        assert hook.run_count == 1

    @pytest.mark.asyncio
    async def test_the_deny_audit_never_breaks_the_caller(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk: "nope")
        import kiro_crew.sel as sel_mod

        class _Sel:
            def log_governance_decision(self, **kwargs):
                raise RuntimeError("sel down")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        result = await run_script_hook(ScriptHook(id="h1", command="echo hi"))
        assert result.exit_code == 2

    @pytest.mark.asyncio
    async def test_session_key_is_taken_from_the_event(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_script_hooks_capability_denied",
            lambda sk: seen.append(sk) or "denied",
        )
        await run_script_hook(ScriptHook(id="h1", command="x"), "", {"session_key": "slot:9"})
        await run_script_hook(
            ScriptHook(id="h2", command="x"), "", {"parent_session_key": "slot:7"}
        )
        await run_script_hook(ScriptHook(id="h3", command="x"))
        assert seen == ["slot:9", "slot:7", ""]


class TestFireDispatch:
    """Registration, ordering, matcher filtering, and failure isolation.

    ``run_script_hook`` is replaced so no real subprocess is spawned; the
    substitute records what it was handed, which is what these assertions are
    about.
    """

    @pytest.fixture
    def recorder(self, monkeypatch):
        calls: list[tuple[ScriptHook, str, dict]] = []

        async def _fake_run(hook, context="", hook_event=None):
            calls.append((hook, context, dict(hook_event or {})))
            # One registered hook failing must not stop the ones after it.
            exit_code = 1 if hook.name == "boom" else 0
            return ScriptHookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                event=hook.event,
                exit_code=exit_code,
                stderr="failed" if exit_code else "",
            )

        monkeypatch.setattr(hooks_mod, "run_script_hook", _fake_run)
        return calls

    @pytest.mark.asyncio
    async def test_hooks_fire_in_registration_order(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        for name in ("first", "second", "third"):
            store.create({"name": name, "command": "true", "event": HOOK_EVENT_STOP})
        results = await store.fire(HOOK_EVENT_STOP, context="done")
        assert [h.name for h, _c, _e in recorder] == ["first", "second", "third"]
        assert [r.hook_name for r in results] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_one_failing_hook_does_not_stop_the_rest(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        for name in ("before", "boom", "after"):
            store.create({"name": name, "command": "true", "event": HOOK_EVENT_STOP})
        results = await store.fire(HOOK_EVENT_STOP, context="x")
        assert [r.hook_name for r in results] == ["before", "boom", "after"]
        assert [r.exit_code for r in results] == [0, 1, 0]

    @pytest.mark.asyncio
    async def test_disabled_and_other_event_hooks_are_skipped(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "on", "command": "true", "event": HOOK_EVENT_STOP})
        store.create({"name": "off", "command": "true", "event": HOOK_EVENT_STOP, "enabled": False})
        store.create({"name": "other", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(HOOK_EVENT_STOP, context="x")
        assert [h.name for h, _c, _e in recorder] == ["on"]

    @pytest.mark.asyncio
    async def test_tool_matcher_filters_by_tool_name(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "fs-only",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "fs_*",
            }
        )
        store.create(
            {
                "name": "all-tools",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "",
            }
        )
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="fs_write")
        assert sorted(h.name for h, _c, _e in recorder) == ["all-tools", "fs-only"]
        recorder.clear()
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="execute_bash")
        assert [h.name for h, _c, _e in recorder] == ["all-tools"]

    @pytest.mark.asyncio
    async def test_post_tool_use_uses_the_tool_matcher_too(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "post",
                "command": "true",
                "event": HOOK_EVENT_POST_TOOL_USE,
                "matcher": "fs_*",
            }
        )
        await store.fire(HOOK_EVENT_POST_TOOL_USE, tool_name="other")
        assert recorder == []
        await store.fire(HOOK_EVENT_POST_TOOL_USE, tool_name="fs_read", tool_response={"ok": True})
        assert [h.name for h, _c, _e in recorder] == ["post"]
        assert recorder[0][2]["tool_response"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_non_tool_matcher_globs_the_context(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create(
            {
                "name": "prompt",
                "command": "true",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "matcher": "*deploy*",
            }
        )
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="please DEPLOY now")
        assert [h.name for h, _c, _e in recorder] == ["prompt"]
        recorder.clear()
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="nothing relevant")
        assert recorder == []

    @pytest.mark.asyncio
    async def test_prompt_event_carries_the_prompt(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "p", "command": "true", "event": HOOK_EVENT_USER_PROMPT_SUBMIT})
        await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="hi there")
        event = recorder[0][2]
        assert event["prompt"] == "hi there"
        assert event["hook_event_name"] == HOOK_EVENT_USER_PROMPT_SUBMIT
        assert "cwd" in event

    @pytest.mark.asyncio
    async def test_stop_event_always_carries_assistant_text(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "s", "command": "true", "event": HOOK_EVENT_STOP})
        await store.fire(HOOK_EVENT_STOP, context="")
        # Unconditional, so a hook that always reads it never KeyErrors.
        assert recorder[0][2]["assistant_text"] == ""

    @pytest.mark.asyncio
    async def test_attribution_fields_are_forwarded(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "t", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="fs_read",
            tool_input={"path": "/tmp/x"},
            subagent_id="sub-1",
            parent_session_key="slot:3",
            agent_role="reviewer",
        )
        event = recorder[0][2]
        assert event["tool_name"] == "fs_read"
        assert event["tool_input"] == {"path": "/tmp/x"}
        assert event["subagent_id"] == "sub-1"
        assert event["parent_session_key"] == "slot:3"
        assert event["agent_role"] == "reviewer"

    @pytest.mark.asyncio
    async def test_absent_attribution_fields_are_omitted(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"name": "t", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE})
        await store.fire(HOOK_EVENT_PRE_TOOL_USE)
        event = recorder[0][2]
        for key in ("tool_name", "tool_input", "subagent_id", "parent_session_key", "agent_role"):
            assert key not in event

    @pytest.mark.asyncio
    async def test_fire_persists_status_bookkeeping(self, tmp_path, recorder):
        store = ScriptHookStore(tmp_path)
        store.create({"id": "h1", "name": "s", "command": "true", "event": HOOK_EVENT_STOP})
        await store.fire(HOOK_EVENT_STOP, context="x")
        assert (tmp_path / "hooks.json").exists()


class TestFireToolHooks:
    @pytest.mark.asyncio
    async def test_a_missing_store_is_a_no_op(self):
        assert await fire_tool_hooks(None, "Running: ls") is None

    @pytest.mark.asyncio
    async def test_running_prefix_is_stripped_and_input_parsed(self, tmp_path, monkeypatch):
        seen: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                seen.append({"event": event, **kwargs})
                return []

        await fire_tool_hooks(
            _Store(),  # type: ignore[arg-type]
            "Running: execute_bash",
            '{"command": "ls"}',
            subagent_id="sub-1",
            parent_session_key="slot:2",
            agent_role="worker",
        )
        assert seen[0]["event"] == HOOK_EVENT_PRE_TOOL_USE
        assert seen[0]["tool_name"] == "execute_bash"
        assert seen[0]["tool_input"] == {"command": "ls"}
        assert seen[0]["subagent_id"] == "sub-1"
        assert seen[0]["parent_session_key"] == "slot:2"
        assert seen[0]["agent_role"] == "worker"

    @pytest.mark.asyncio
    async def test_unparseable_tool_input_degrades_to_none(self):
        seen: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                seen.append(kwargs)
                return []

        await fire_tool_hooks(_Store(), "", "{not json")  # type: ignore[arg-type]
        assert seen[0]["tool_input"] is None
        assert seen[0]["tool_name"] == ""

    @pytest.mark.asyncio
    async def test_a_raising_store_is_swallowed(self):
        class _Store:
            async def fire(self, event, **kwargs):
                raise RuntimeError("store on fire")

        # Informational hooks must never break the tool-call notification path.
        assert await fire_tool_hooks(_Store(), "Running: ls") is None  # type: ignore[arg-type]


# ── governance helper fail-soft / fail-closed discipline ──


class TestGovernancePinResolution:
    def test_a_glitch_degrades_to_no_pins(self, monkeypatch):
        def _boom():
            raise RuntimeError("policy store glitch")

        monkeypatch.setattr(security, "pinned_builtin_command_ids", _boom)
        # An empty set, not an exception: a glitch here must not wedge the gate.
        assert _governance_pinned_command_ids(None) == set()

    def test_composition_error_propagates(self, monkeypatch):
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom():
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(security, "pinned_builtin_command_ids", _boom)
        with pytest.raises(PlatformCompositionError):
            _governance_pinned_command_ids(None)


class TestGovernanceDenial:
    def test_composition_error_propagates(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(*a, **k):
            raise PlatformCompositionError("cannot compose")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom, raising=False)
        with pytest.raises(PlatformCompositionError):
            _governance_denial(object(), "some_tool", "", "", "")

    def test_a_glitch_degrades_to_no_opinion(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        def _boom(*a, **k):
            raise RuntimeError("profile store glitch")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom, raising=False)
        monkeypatch.setattr(gp, "audit_governance_degraded", _boom, raising=False)
        assert _governance_denial(object(), "some_tool", "", "", "") is None

    def test_ungoverned_host_is_a_fast_no_op(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setattr(gp, "resolve_active_scope", lambda *a, **k: None, raising=False)

        class _Ctx:
            governance = None

        assert _governance_denial(_Ctx(), "some_tool", "", "", "") is None


class TestGovernanceAudit:
    def test_a_raising_sel_never_breaks_the_gate(self, monkeypatch):
        import kiro_crew.sel as sel_mod

        class _Sel:
            def log_governance_decision(self, **kwargs):
                raise RuntimeError("sel down")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        # Returns None rather than propagating -- the audit is best effort.
        assert _audit_governance("slot:1", "kirocrew", "some_tool", object()) is None


# ── message transforms and the read-only auto-approve branch ──


class TestMessageTransformSuffix:
    def test_prefix_and_suffix_are_both_applied(self):
        cfg = HooksConfig(
            transforms=[TransformHook(pattern="deploy", prefix="[BEFORE]", suffix="[AFTER]")]
        )
        result = HookManager(cfg).on_message("please deploy")
        assert result.action == HOOK_MODIFY
        assert result.text == "[BEFORE]\nplease deploy\n[AFTER]"

    def test_suffix_only(self):
        cfg = HooksConfig(transforms=[TransformHook(pattern="deploy", suffix="[AFTER]")])
        assert HookManager(cfg).on_message("deploy").text == "deploy\n[AFTER]"


class TestReadOnlyKindAutoApprove:
    @pytest.mark.parametrize("kind", ["read", "fetch", "READ"])
    def test_a_read_only_kind_auto_approves(self, kind):
        got = HookManager().on_tool_call("some_tool", tool_kind=kind)
        assert got.action == TOOL_AUTO_APPROVE

    def test_a_computer_use_observation_never_reaches_its_own_gate(self, monkeypatch):
        """Documents that the computer-use observation branch is unreachable.

        ``on_tool_call`` returns ``auto_approve`` for every kind in
        ``_READ_ONLY_TOOL_KINDS`` one branch earlier, so the follow-up condition
        ``kind in _READ_ONLY_TOOL_KINDS and _cu_read_only_auto_approve(...)``
        can never be evaluated -- and with it the keystone computer-use
        enable-state check it carries. Asserted so the dead branch is visible
        rather than silently trusted.
        """
        consulted: list[str] = []
        monkeypatch.setattr(
            hooks_mod,
            "_cu_read_only_auto_approve",
            lambda name: consulted.append(name) or True,
        )
        got = HookManager().on_tool_call(
            "mcp__kirocrew-computer__computer_get_state", tool_kind="read"
        )
        assert got.action == TOOL_AUTO_APPROVE
        assert consulted == []

    def test_a_mutating_kind_falls_through_to_interactive_approval(self):
        assert HookManager().on_tool_call("some_tool", tool_kind="edit").action == TOOL_ALLOW
        assert HookManager().on_tool_call("some_tool", tool_kind="other").action == TOOL_ALLOW
