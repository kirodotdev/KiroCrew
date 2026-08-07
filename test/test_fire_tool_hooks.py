"""Tests for fire_tool_hooks helper and global hook store accessor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookStore,
    fire_tool_hooks,
    get_global_hook_store,
    run_script_hook,
    set_global_hook_store,
)


@pytest.fixture(autouse=True)
def _reset_global_store():
    """Reset global hook store between tests."""
    set_global_hook_store(None)  # type: ignore[arg-type]
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


@pytest.fixture
def hook_store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


class TestGlobalHookStore:
    """Test get/set global hook store accessor."""

    def test_default_is_none(self):
        assert get_global_hook_store() is None

    def test_set_and_get(self, hook_store: ScriptHookStore):
        set_global_hook_store(hook_store)
        assert get_global_hook_store() is hook_store

    def test_overwrite(self, tmp_path: Path):
        store1 = ScriptHookStore(tmp_path / "a")
        store2 = ScriptHookStore(tmp_path / "b")
        set_global_hook_store(store1)
        set_global_hook_store(store2)
        assert get_global_hook_store() is store2


class TestFireToolHooks:
    """Test fire_tool_hooks helper."""

    @pytest.mark.asyncio
    async def test_none_store_is_noop(self):
        # Should not raise
        await fire_tool_hooks(None, "Running: echo hello")

    @pytest.mark.asyncio
    async def test_strips_running_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "Running: echo hello")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="echo hello",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_no_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "@builder-mcp/ReadFile")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="@builder-mcp/ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_parses_tool_input_json(self, hook_store: ScriptHookStore):
        ti = json.dumps({"path": "/tmp/test.txt"})
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", ti)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input={"path": "/tmp/test.txt"},
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_invalid_json_passes_none(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", "not-json")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_empty_title(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_fire_exception_swallowed(self, hook_store: ScriptHookStore):
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await fire_tool_hooks(hook_store, "ReadFile")

    @pytest.mark.asyncio
    async def test_none_tool_input_skipped(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", None)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_passes_subagent_metadata(self, hook_store: ScriptHookStore):
        """When called with subagent_id, parent_session_key, agent_role, those propagate to fire()."""
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(
                hook_store,
                "ReadFile",
                None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )


class TestScriptHookStoreFire:
    """Test ScriptHookStore.fire() emits subagent_id, parent_session_key, agent_role into hook_event.

    These tests register a real hook in the store, patch run_script_hook to capture
    the hook_event payload, and assert that the conditional emission branches in fire()
    add (or omit) the new fields correctly.
    """

    @pytest.fixture
    def fire_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "test-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_fire_emits_subagent_id_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_parent_session_key_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                parent_session_key="dashboard:slot-1",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert "subagent_id" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_agent_role_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["agent_role"] == "utility"
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_all_three_together(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert hook_event["agent_role"] == "utility"

    @pytest.mark.asyncio
    async def test_fire_omits_all_three_when_none(self, fire_store: ScriptHookStore):
        """Backward compatibility: when all three are None (default), payload is byte-identical to pre-CR behavior."""
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile")
            (_, _, hook_event), _ = mock_run.call_args
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event


class TestScriptHookStoreStopContext:
    """Stop hooks receive the final assistant segment on stdin, untruncated.

    The env var ``KIROCREW_HOOK_CONTEXT`` is capped at 500 chars (ARG_MAX
    safety), which drops the tail of the segment. A Stop hook that keys on tail
    content (e.g. the harness ``[OPTIONS:]`` menu line) never sees it via the env
    var. fire() therefore emits the untruncated segment into the stdin
    ``hook_event`` payload as ``assistant_text`` — mirroring the existing
    ``prompt`` key for UserPromptSubmit, but on a dedicated arg so the env value
    can stay bounded.
    """

    @pytest.fixture
    def stop_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_stop_full_context_on_stdin_and_matcher(self, stop_store: ScriptHookStore):
        # The load-bearing marker sits at the tail, past the 500-char env cap.
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context=full)
            (_, ctx_arg, hook_event), _ = mock_run.call_args
            # stdin payload carries the FULL segment, tail marker intact.
            assert hook_event["assistant_text"] == full
            assert "[OPTIONS:" in hook_event["assistant_text"]
            # fire() passes the FULL context downstream (matcher + env source);
            # the env-only 500-cap lives in run_script_hook, not here — a tail
            # matcher must therefore still be able to see the marker.
            assert ctx_arg == full

    @pytest.mark.asyncio
    async def test_stop_tail_matcher_is_not_truncated(self, tmp_path: Path):
        # A Stop hook whose matcher targets tail content must still fire — fire()
        # matches against the full context, not the 500-char env slice.
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "options-stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "*[OPTIONS:*",
            "command": "echo test",
        })
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "options-stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await store.fire(HOOK_EVENT_STOP, context=full)
            assert mock_run.await_count == 1, "tail-matching Stop hook was filtered out by env truncation"

    @pytest.mark.asyncio
    async def test_stop_empty_turn_still_emits_key(self, stop_store: ScriptHookStore):
        # An empty / no-output turn still fires Stop with context="". The key MUST
        # be present (unconditional, not truthiness-gated) so a hook that always
        # reads hook_event["assistant_text"] gets "" rather than KeyError-ing.
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context="")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["assistant_text"] == ""

    @pytest.mark.asyncio
    async def test_user_prompt_submit_still_uses_prompt_key(self, tmp_path: Path):
        # Regression guard: the Stop change must not bleed into UPS, which keeps
        # delivering its full context under the existing ``prompt`` key.
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "ups-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "echo test",
        })
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "ups-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="hello")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["prompt"] == "hello"
            assert "assistant_text" not in hook_event


class TestRunScriptHookStopEnvCap:
    """run_script_hook caps the Stop env var at 500 chars while the full
    segment still reaches the hook via stdin JSON (ARG_MAX safety)."""

    @pytest.mark.asyncio
    async def test_stop_env_context_capped_but_stdin_full(self) -> None:
        """Stop hook: KIROCREW_HOOK_CONTEXT env is capped at 500; stdin JSON is full.

        The env var is bounded by ARG_MAX (a multi-KB turn there can fail process
        creation), so run_script_hook truncates the ENV copy for Stop only — while
        the full segment still reaches the hook via the stdin ``assistant_text``
        payload that fire() built. Captures both channels off a mocked subprocess.
        """
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        hook = ScriptHook(id="s1", name="stop-hook", event=HOOK_EVENT_STOP, command="cat", timeout=5)
        hook_event = {"hook_event_name": HOOK_EVENT_STOP, "cwd": "/", "assistant_text": full}

        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(return_value=(b"", b""))
        fake_proc.returncode = 0
        captured: dict = {}

        async def fake_exec(*argv, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return fake_proc

        # Both spawn forms are patched because the choice is platform-dependent:
        # Windows hands the command line to ``create_subprocess_shell`` so
        # cmd.exe parses the operator's quotes verbatim, POSIX execs
        # ``/bin/sh -c`` as an argv. The env cap under test is identical either
        # way, so the test must not assume one host's form.
        with (
            patch("kiro_crew.sandbox.wrap_argv", lambda argv, *a, **k: (argv, None)),
            patch("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("asyncio.create_subprocess_shell", side_effect=fake_exec),
        ):
            await run_script_hook(hook, context=full, hook_event=hook_event)

        # ENV copy is capped at 500 chars — the tail marker is dropped there.
        env_ctx = captured["env"]["KIROCREW_HOOK_CONTEXT"]
        assert len(env_ctx) == 500
        assert "[OPTIONS:" not in env_ctx
        # Assert on what actually reached the subprocess stdin (not the input
        # dict): the full segment, tail marker intact, is serialized to stdin.
        stdin_bytes = fake_proc.communicate.call_args.kwargs["input"]
        parsed = json.loads(stdin_bytes)
        assert parsed["assistant_text"] == full
        assert "[OPTIONS:" in parsed["assistant_text"]
