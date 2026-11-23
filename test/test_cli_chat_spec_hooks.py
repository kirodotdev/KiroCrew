"""``kirocrew chat`` asks the agent spec's own PreToolUse hooks before its y/n prompt.

On KAS the spec's ``hooks`` never reach the backend: its projection sends every call
a ``preToolUse`` hook covers to Crew's permission path, which in the CLI is the
terminal prompt. So the spec's guard answers first, and a call it refuses is never
put to the person at the terminal.
"""

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


def _write_spec(raw: str | None = None) -> None:
    import json

    import kiro_crew.config.paths as paths_mod

    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    hooks = {
        "agentSpawn": [{"command": "spawn"}],
        "userPromptSubmit": [{"command": "prompt"}],
        "postToolUse": [{"matcher": "execute_bash", "command": "post"}],
        "stop": [{"command": "stop"}],
    }
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "hooks": hooks}
    (agents / f"{_AGENT}.json").write_text(raw or json.dumps(spec), encoding="utf-8")


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
                exit_code=0,
                stdout=f"out-{hook.command}",
            )

        monkeypatch.setattr(hooks_mod, "run_script_hook", run)
        monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: None)
        return seen

    @staticmethod
    async def _send(tmp_path, *, new_session: bool) -> _KasProvider:
        import asyncio

        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.hooks import HookManager, HooksConfig

        provider = _KasProvider(str(tmp_path))
        gate = cli_chat._ToolGate(
            hooks=HookManager(HooksConfig.from_dict({})), agent=_AGENT, kiro_agent=_AGENT
        )
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, "run echo hi", interactive=False, gate=gate, new_session=new_session
            ),
            timeout=10.0,
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
