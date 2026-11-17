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

        async def fake_turn_spec_hooks(provider, agent):
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
