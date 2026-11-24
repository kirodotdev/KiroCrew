"""``kirocrew chat`` asks the agent spec's own PreToolUse hooks before its y/n prompt.

On KAS the spec's ``hooks`` never reach the backend: its projection sends every call
a ``preToolUse`` hook covers to Crew's permission path, which in the CLI is the
terminal prompt. So the spec's guard answers first, and a call it refuses is never
put to the person at the terminal.
"""

import types

import pytest
from cli_helpers import _TestChatPermissionRequestHarness

import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookResult


class TestCliChatSpecPreToolHooks(_TestChatPermissionRequestHarness):
    class _GatedProvider(_TestChatPermissionRequestHarness._GatedProvider):
        """The harness's gated provider, on KAS, running the agent ``guard``."""

        capabilities = capabilities_for(ACP_BACKEND_KAS)
        session_id = "acp-1"
        kas_projected_agent = "guard"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exit_code", "unreadable", "refused"),
        [(2, False, True), (0, False, False), (0, True, True)],
        ids=["exit-2-refuses", "exit-0-asks", "unreadable-spec-refuses"],
    )
    async def test_the_specs_guard_answers_before_the_prompt(
        self, monkeypatch, capsys, exit_code, unreadable, refused
    ):
        ran: list = []

        async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
            ran.append((hook.command, (hook_event or {}).get("session_key")))
            return ScriptHookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                event=hook.event,
                exit_code=exit_code,
                stderr="no shell here",
            )

        guard = ScriptHook(
            id="spec:guard:preToolUse:0",
            name="guard.sh",
            event=HOOK_EVENT_PRE_TOOL_USE,
            matcher="execute_bash",
            command="guard.sh",
            spec_origin="user",
        )
        read_for: list = []

        async def fake_turn_spec_hooks(provider, agent, **_kw):
            read_for.append(agent)
            return spec_hooks.TurnSpecHooks([] if unreadable else [guard], "/w", unreadable, True)

        monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
        monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
        event = self._event(title="Run echo", command="echo hi")
        # KAS's own id for its shell tool, which an ``execute_bash`` matcher meets.
        event.harness_tool_id = "run_command"

        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="y")

        assert read_for == ["guard"]
        assert ran == ([] if unreadable else [("guard.sh", "cli_chat")])
        if refused:
            assert provider.calls == [("reject", 7, False)]
            assert reads["n"] == 0
            assert [(s["outcome"], s["error"]) for s in sels] == [("denied", "spec_hook")]
            err = capsys.readouterr().err
            assert "Blocked by the agent's PreToolUse hooks" in err
            assert ("could not be read" if unreadable else "no shell here") in err
        else:
            assert provider.calls == [("approve", 7, False)]
            assert reads["n"] == 1


_AGENT = "cli-guard"


def _write_spec(raw: str | None = None, *, name: str = _AGENT, hooks: dict | None = None) -> None:
    import json

    import kiro_crew.config.paths as paths_mod

    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    if hooks is None:
        hooks = {
            "agentSpawn": [{"command": "spawn"}],
            "userPromptSubmit": [{"command": "prompt"}],
            "postToolUse": [{"matcher": "execute_bash", "command": "post"}],
            "stop": [{"command": "stop"}],
        }
    spec = {"name": name, "prompt": "p", "tools": ["*"], "hooks": hooks}
    (agents / f"{name}.json").write_text(raw or json.dumps(spec), encoding="utf-8")


class _KasProvider:
    """A KAS session running ``cli-guard`` that runs one shell call and replies."""

    capabilities = capabilities_for(ACP_BACKEND_KAS)
    session_id = "acp-1"
    kas_projected_agent = _AGENT

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.prompts: list[str] = []

    async def stream(self, message):
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        self.prompts.append(str(message))
        yield LLMEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="t1",
            title="Running: echo hi",
            tool_kind="execute",
            is_shell=True,
            harness_tool_id="run_command",
        )
        yield LLMEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="t1", tool_status="completed", tool_output="hi"
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Ran it.")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    def context_usage_pct(self):
        return 0.0


class TestCliChatSpecObserverHooks:
    """The spec's other events fire in ``kirocrew chat`` on KAS, as in a dashboard chat."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        spec_hooks._cache.clear()
        yield
        spec_hooks._cache.clear()

    @pytest.fixture
    def runs(self, monkeypatch) -> list[tuple[str, str]]:
        """``(command, context)`` of every hook run; each prints ``out-<command>``."""
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)

        async def run(hook, context="", hook_event=None, **_kw):
            seen.append((hook.command, str(context)))
            return ScriptHookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                event=hook.event,
                exit_code=2 if hook.command == "guard" else 0,
                stdout=f"out-{hook.command}",
                stderr="no shell here" if hook.command == "guard" else "",
            )

        monkeypatch.setattr(hooks_mod, "run_script_hook", run)
        monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: None)
        return seen

    @staticmethod
    async def _send(
        tmp_path, *, new_session: bool, provider=None, message: str = "run echo hi"
    ) -> _KasProvider:
        import asyncio

        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.hooks import HookManager, HooksConfig

        provider = provider or _KasProvider(str(tmp_path))
        gate = cli_chat._ToolGate(
            hooks=HookManager(HooksConfig.from_dict({})), agent=_AGENT, kiro_agent=_AGENT
        )
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, message, interactive=False, gate=gate, new_session=new_session
            ),
            timeout=60.0,
        )
        return provider

    @pytest.mark.asyncio
    @pytest.mark.parametrize("new_session", [True, False], ids=["new-session", "later-turn"])
    async def test_spawn_prompt_post_tool_and_stop_hooks_run(self, tmp_path, runs, new_session):
        _write_spec()

        provider = await self._send(tmp_path, new_session=new_session)

        commands = [command for command, _ in runs]
        assert commands == (["spawn"] if new_session else []) + ["prompt", "post", "stop"]
        assert dict(runs)["stop"] == "Ran it."
        # The prompt-side hooks' output reaches the agent in front of the message.
        (prompt,) = provider.prompts
        assert prompt.startswith("[Hook context]")
        assert "out-prompt" in prompt and "run echo hi" in prompt
        assert ("out-spawn" in prompt) is new_session

    @pytest.mark.asyncio
    async def test_a_new_session_says_when_the_spec_cannot_be_read(self, tmp_path, runs, capsys):
        _write_spec(raw="{not json")

        await self._send(tmp_path, new_session=True)

        assert runs == []
        assert f"The hooks of agent {_AGENT} could not be read" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_a_slash_command_reaches_the_harness_bare(self, tmp_path, runs):
        _write_spec()

        provider = await self._send(tmp_path, new_session=False, message="/compact")

        assert "prompt" in [command for command, _ in runs]
        assert provider.prompts == ["/compact"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host_denies", [False, True], ids=["asked", "host-gate-denies"])
    async def test_post_tool_use_matches_the_id_the_permission_request_names(
        self, tmp_path, runs, monkeypatch, host_denies
    ):
        """KAS names a built-in's id (``fs_write``) only on the permission request.

        A call the security gate refuses never reaches the spec's guard, and its
        PostToolUse still meets it by that id.
        """
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.hooks import TOOL_DENY, HookManager

        if host_denies:
            monkeypatch.setattr(
                HookManager,
                "on_tool_call",
                lambda self, *a, **kw: types.SimpleNamespace(
                    action=TOOL_DENY, reason="blocked", tier=""
                ),
            )

        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=lambda **kw: None)
        )
        _write_spec(hooks={"postToolUse": [{"matcher": "fs_write", "command": "post"}]})

        await self._send(tmp_path, new_session=False, provider=_FileWriteProvider(str(tmp_path)))

        assert [command for command, _ in runs] == ["post"]

    @pytest.mark.asyncio
    async def test_a_mid_turn_switch_is_gated_by_the_new_agents_guard(
        self, tmp_path, runs, monkeypatch
    ):
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=lambda **kw: None)
        )
        _write_spec(hooks={})
        _write_spec(
            name="strict",
            hooks={"preToolUse": [{"matcher": "execute_bash", "command": "guard"}]},
        )
        provider = _SwitchProvider(str(tmp_path))

        await self._send(tmp_path, new_session=False, provider=provider)

        assert [command for command, _ in runs] == ["guard"]
        assert provider.rejected == ["r1"]

    @pytest.mark.asyncio
    async def test_a_native_child_calls_post_tool_use_runs(self, tmp_path, runs):
        """A KAS sub-agent's finished shell call reaches the turn only as activity."""
        _write_spec(hooks={"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})

        await self._send(tmp_path, new_session=False, provider=_ChildCallProvider(str(tmp_path)))

        assert [command for command, _ in runs] == ["post"]


class _ScriptedKasProvider(_KasProvider):
    """A KAS session whose turn raises one permission request, then completes."""

    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.rejected: list = []

    def _frames(self) -> list:
        raise NotImplementedError

    async def stream(self, message):
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        self.prompts.append(str(message))
        for frame in self._frames():
            if callable(frame):
                frame()
                continue
            yield frame
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def approve_tool(self, request_id, *, always: bool = False):
        return True


class _FileWriteProvider(_ScriptedKasProvider):
    def _frames(self) -> list:
        from kiro_crew.providers.base import (
            EVENT_PERMISSION_REQUEST,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        return [
            LLMEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="t1", title="Writing file", tool_kind="edit"
            ),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="r1",
                tool_call_id="t1",
                title="Writing file",
                tool_kind="edit",
                harness_tool_id="fs_write",
            ),
            LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t1", tool_status="completed"),
        ]


class _SwitchProvider(_ScriptedKasProvider):
    def _switch(self) -> None:
        self.kas_projected_agent = "strict"

    def _frames(self) -> list:
        import json

        from kiro_crew.providers.base import (
            EVENT_AGENT_SWITCHED,
            EVENT_PERMISSION_REQUEST,
            LLMEvent,
        )

        return [
            self._switch,
            LLMEvent(kind=EVENT_AGENT_SWITCHED, text="strict"),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="r1",
                tool_call_id="t1",
                title="Run echo",
                tool_kind="execute",
                is_shell=True,
                shell_classified=True,
                tool_input=json.dumps({"command": "echo hi"}),
                harness_tool_id="run_command",
            ),
        ]


class _ChildCallProvider(_ScriptedKasProvider):
    def _frames(self) -> list:
        from kiro_crew.providers.base import EVENT_SUBAGENT_ACTIVITY, LLMEvent

        return [
            LLMEvent(
                kind=EVENT_SUBAGENT_ACTIVITY,
                sub_session_id="sa-1",
                tool_call_id="child-tc",
                title="echo hi",
                hook_subject="echo hi",
                tool_final=True,
                tool_status="completed",
                tool_output="hi",
                is_shell=True,
            )
        ]
