"""A harness slash command on a backend with no command channel never reaches the model.

The defect these pin: ``AcpSessionHandle.stream_command`` runs a command natively
only on kiro-cli (``_kiro.dev/commands/execute``). On KAS — the other member of
``ACP_BACKENDS_KIRO_SLASH_COMMANDS`` — it falls back to ``session/prompt``, so
``/clear`` arrives as plain user text. A live KAS session showed it: context usage
stayed at 8.42% -> 8.44% across ``/clear`` while the model replied "Context
cleared." The conversation was never dropped.

The dashboard therefore answers these commands itself on such a backend, before
any session work: ``/clear`` goes through the same discard ``reset_conversation``
queues, and any other harness command gets a short notice instead of being sent
to the model as text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import _run_chat, effective_session_key
from kiro_crew.providers.acp import AcpProvider


@pytest.fixture(autouse=True)
def _no_spec_hooks(_floor_monkeypatch):
    """KAS has Crew fire spec hooks; these doubles carry no projection to refresh."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._prepare_spec_hooks",
        AsyncMock(return_value=([], False, None)),
    )

    async def _claimed(_sessions, _key, _agent, claimed, _claim):
        return claimed

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.reproject_claimed_session", _claimed
    )


def _live(tmp_path, backend: str):
    client = MagicMock(spec=AcpProvider)
    client.capabilities = capabilities_for(backend)
    client.manual_compact_unsupported_backend = None
    client.is_process_alive.return_value = True
    calls = MagicMock()

    async def _record(msg):
        calls(msg)
        return
        yield  # pragma: no cover - generator shape only

    client.stream = _record
    client.stream_command = _record

    state = _make_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    state.sessions.reset = AsyncMock(return_value=False)
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("no-channel-slot")
    state.sessions._sessions = {effective_session_key(slot): SimpleNamespace(provider=client)}
    state.sessions.get_provider = MagicMock(return_value=client)
    state.sessions.is_provider_alive = AsyncMock(return_value=True)
    return state, slot, calls


def _texts(slot) -> list[str]:
    return [m.get("content", "") for m in slot.messages]


class TestKasClear:
    @pytest.mark.asyncio
    async def test_clear_discards_the_conversation_instead_of_prompting(self, tmp_path) -> None:
        """RED on main: ``/clear`` was handed to the KAS session as prompt text."""
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)
        slot.set_todo({"description": "plan", "tasks": [{"id": "1", "task_description": "a"}]})

        await _run_chat(state, slot, "/clear")

        dispatched.assert_not_called()
        state.sessions.get_or_create.assert_not_called()
        state.sessions.discard_conversation.assert_awaited_once_with(
            effective_session_key(slot), replay=False, skip_if_busy=True
        )
        assert any("Conversation cleared" in t for t in _texts(slot)), _texts(slot)
        # The plan goes with the conversation, or the fresh one rebuilds it.
        assert slot.todo_payload() is None

    @pytest.mark.asyncio
    async def test_a_busy_session_keeps_the_discard_queued(self, tmp_path) -> None:
        """A discard the session refuses stays armed for the next turn boundary,
        exactly like a ``reset_conversation`` call, and the reply says so."""
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot.set_todo({"description": "plan", "tasks": [{"id": "1", "task_description": "a"}]})

        await _run_chat(state, slot, "/clear")

        dispatched.assert_not_called()
        assert slot._pending_discard_conversation_key == effective_session_key(slot)
        assert not any("Conversation cleared" in t for t in _texts(slot))
        assert any("queued" in t for t in _texts(slot)), _texts(slot)
        assert slot.todo_payload() is not None

    @pytest.mark.asyncio
    async def test_a_project_reset_alone_does_not_claim_the_clear(self, tmp_path) -> None:
        """A queued project reset that tears down while the discard is refused
        must not report the conversation cleared or drop the plan."""
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)
        state.sessions.reset = AsyncMock(return_value=True)
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot._pending_reset_history_key = effective_session_key(slot)
        slot.set_todo({"description": "plan", "tasks": [{"id": "1", "task_description": "a"}]})

        await _run_chat(state, slot, "/clear")

        dispatched.assert_not_called()
        state.sessions.reset.assert_awaited()
        assert slot._pending_discard_conversation_key == effective_session_key(slot)
        assert not any("Conversation cleared" in t for t in _texts(slot)), _texts(slot)
        assert any("queued" in t for t in _texts(slot)), _texts(slot)
        assert slot.todo_payload() is not None


class TestKasOtherCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/help", "/tools", "/context add x", "/model", "/mcp"])
    async def test_unrunnable_command_is_refused_not_prompted(self, tmp_path, command) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)

        await _run_chat(state, slot, command)

        dispatched.assert_not_called()
        state.sessions.get_or_create.assert_not_called()
        state.sessions.discard_conversation.assert_not_awaited()
        first = command.split()[0]
        assert any(
            f"`{first}`" in t and "doesn't work with KAS" in t for t in _texts(slot)
        ), _texts(slot)

    @pytest.mark.asyncio
    async def test_blocked_command_stays_blocked(self, tmp_path) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)

        await _run_chat(state, slot, "/quit")

        dispatched.assert_not_called()
        assert any("is not available in the dashboard" in t for t in _texts(slot))

    @pytest.mark.asyncio
    async def test_a_kas_prompt_name_is_still_ordinary_text(self, tmp_path) -> None:
        """KAS resolves ``/<prompt>`` for its own file and MCP prompts. Those are
        not Crew harness commands, so they keep reaching the session."""
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)

        await _run_chat(state, slot, "/my-prompt fix the build")

        assert dispatched.called
        assert not any("doesn't work with KAS" in t for t in _texts(slot))


class TestKiroUnchanged:
    @pytest.mark.asyncio
    async def test_kiro_clear_still_runs_natively(self, tmp_path) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KIRO)

        await _run_chat(state, slot, "/clear")

        dispatched.assert_called_once_with("/clear")
        state.sessions.discard_conversation.assert_not_awaited()
