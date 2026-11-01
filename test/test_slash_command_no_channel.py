"""A harness slash command on a backend with no command channel never reaches the model.

The defect these pin: ``AcpSessionHandle.stream_command`` runs a command natively
only on kiro-cli (``_kiro.dev/commands/execute``). On KAS — the other member of
``ACP_BACKENDS_KIRO_FAMILY``, which refuses that verb — it falls back to
``session/prompt``, so
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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_LOCAL_CLEAR,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import _run_chat, effective_session_key
from kiro_crew.dashboard.chat_utils import _BLOCKED_SLASH_COMMANDS, _SLASH_COMMANDS
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
    @pytest.mark.parametrize("command", ["/help", "/context add x", "/usage"])
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


_NO_NATIVE_CLEAR = (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_COPILOT,
)


class TestOtherHarnessesClear:
    """codex, opencode, pi and deepseek have no ``/clear`` in their own prompt
    handlers either, so the text reached the model and the context stayed."""

    def test_membership_is_the_checked_set(self) -> None:
        # strands answers every command here, ``/clear`` included (see below).
        assert ACP_BACKENDS_LOCAL_CLEAR == {ACP_BACKEND_KAS, ACP_BACKEND_STRANDS, *_NO_NATIVE_CLEAR}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", _NO_NATIVE_CLEAR)
    async def test_clear_discards_instead_of_prompting(self, tmp_path, backend) -> None:
        state, slot, dispatched = _live(tmp_path, backend)
        slot.set_todo({"description": "plan", "tasks": [{"id": "1", "task_description": "a"}]})

        await _run_chat(state, slot, "/clear")

        dispatched.assert_not_called()
        state.sessions.get_or_create.assert_not_called()
        state.sessions.discard_conversation.assert_awaited_once_with(
            effective_session_key(slot), replay=False, skip_if_busy=True
        )
        assert any("Conversation cleared" in t for t in _texts(slot)), _texts(slot)
        assert slot.todo_payload() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", _NO_NATIVE_CLEAR)
    async def test_other_commands_keep_the_prompt_path(self, tmp_path, backend) -> None:
        """Only ``/clear`` is answered here: these harnesses serve commands of
        their own (``/review``, ``/session``) out of the same prompt text."""
        state, slot, dispatched = _live(tmp_path, backend)

        await _run_chat(state, slot, "/help")

        assert dispatched.called
        state.sessions.discard_conversation.assert_not_awaited()
        assert not any("doesn't work with" in t for t in _texts(slot)), _texts(slot)

    @pytest.mark.asyncio
    async def test_goose_runs_its_own_clear(self, tmp_path) -> None:
        """goose's prompt handler runs ``/clear`` itself, so it is sent on."""
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_GOOSE)

        await _run_chat(state, slot, "/clear")

        assert dispatched.called
        state.sessions.discard_conversation.assert_not_awaited()


class TestKiroUnchanged:
    @pytest.mark.asyncio
    async def test_kiro_clear_still_runs_natively(self, tmp_path) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KIRO)

        await _run_chat(state, slot, "/clear")

        dispatched.assert_called_once_with("/clear")
        state.sessions.discard_conversation.assert_not_awaited()


async def _menu_names(backend: str) -> set[str]:
    """What GET /api/slash-commands lists when *backend* is the configured default."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.agents import api_slash_commands

    app = web.Application()
    app.router.add_get("/api/slash-commands", api_slash_commands)
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="acp", acp_backend=backend))
    with patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/slash-commands")
            assert resp.status == 200
            return {item["name"] for item in await resp.json()}


class TestKasMenuMatchesTheGate:
    """The ``/`` menu and the KAS gate read one set, so the menu never offers a
    command that only ever gets the "doesn't work with KAS" notice."""

    def test_membership_is_the_checked_set(self) -> None:
        from kiro_crew.acp_backends import ACP_BACKENDS_LOCAL_HARNESS_COMMANDS

        assert ACP_BACKENDS_LOCAL_HARNESS_COMMANDS == {ACP_BACKEND_KAS, ACP_BACKEND_STRANDS}

    @pytest.mark.asyncio
    async def test_every_command_left_out_of_the_menu_is_one_the_gate_refuses(
        self, tmp_path
    ) -> None:
        """RED on main: the KAS menu listed /context, /help and the rest."""
        listed = await _menu_names(ACP_BACKEND_KAS)
        left_out = _SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS - listed
        assert {"/context", "/help", "/usage"} <= left_out

        for command in sorted(left_out):
            state, slot, dispatched = _live(tmp_path, ACP_BACKEND_KAS)
            await _run_chat(state, slot, command)
            dispatched.assert_not_called()
            assert any(
                f"`{command}`" in t and "doesn't work with KAS" in t for t in _texts(slot)
            ), (command, _texts(slot))

    @pytest.mark.asyncio
    async def test_the_menu_keeps_what_the_dashboard_serves_on_kas(self) -> None:
        listed = await _menu_names(ACP_BACKEND_KAS)
        assert listed == {
            "/agent",
            "/clear",
            "/compact",
            "/goal",
            "/mcp",
            "/model",
            "/prompts",
            "/side",
            "/tools",
            "/workflow",
        }

    @pytest.mark.asyncio
    async def test_the_gate_reads_the_set_not_the_backend_name(
        self, tmp_path, _floor_monkeypatch
    ) -> None:
        """A harness put in the set gets the same notice, so the gate and the
        menu cannot answer from two different rules."""
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.ACP_BACKENDS_LOCAL_HARNESS_COMMANDS",
            frozenset({ACP_BACKEND_CODEX}),
        )
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_CODEX)

        await _run_chat(state, slot, "/help")

        dispatched.assert_not_called()
        assert any("`/help`" in t for t in _texts(slot)), _texts(slot)

    @pytest.mark.asyncio
    async def test_kiro_menu_is_unchanged(self) -> None:
        assert await _menu_names(ACP_BACKEND_KIRO) == _SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS


class TestStrandsCommandsAreServedHere:
    """The Strands CLI's project-mode ACP parses no slash at all, so every harness
    command it was handed would reach the model as text. The dashboard answers them."""

    @pytest.mark.asyncio
    async def test_a_harness_command_gets_a_notice_naming_strands(self, tmp_path) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_STRANDS)

        await _run_chat(state, slot, "/help")

        dispatched.assert_not_called()
        assert any(
            "`/help`" in t and "doesn't work with Strands" in t for t in _texts(slot)
        ), _texts(slot)

    @pytest.mark.asyncio
    async def test_clear_drops_the_conversation_without_reaching_the_harness(
        self, tmp_path
    ) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_STRANDS)

        await _run_chat(state, slot, "/clear")

        dispatched.assert_not_called()
        assert slot._pending_discard_conversation_key is None
        assert any("Conversation cleared" in t for t in _texts(slot)), _texts(slot)

    @pytest.mark.asyncio
    async def test_the_menu_lists_only_what_the_dashboard_serves(self) -> None:
        """KAS's /model, /tools and /mcp read KAS's own registration, so they are
        left out of a Strands menu, which keeps every other dashboard command."""
        kas = await _menu_names(ACP_BACKEND_KAS)
        assert await _menu_names(ACP_BACKEND_STRANDS) == kas - {"/model", "/tools", "/mcp"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/model", "/tools", "/mcp"])
    async def test_a_kas_inspect_command_gets_the_strands_notice(self, tmp_path, command) -> None:
        state, slot, dispatched = _live(tmp_path, ACP_BACKEND_STRANDS)

        await _run_chat(state, slot, command)

        dispatched.assert_not_called()
        assert any(
            f"`{command}`" in t and "doesn't work with Strands" in t for t in _texts(slot)
        ), _texts(slot)


def test_only_kas_is_served_its_own_inspect_commands() -> None:
    from kiro_crew.dashboard.chat_utils import _DASHBOARD_SERVED_SLASH_COMMANDS
    from kiro_crew.dashboard.kas_local_commands import (
        KAS_LOCAL_INSPECT_COMMANDS,
        served_slash_commands,
    )

    served = _DASHBOARD_SERVED_SLASH_COMMANDS
    assert served_slash_commands(ACP_BACKEND_KAS, served) == served
    assert served_slash_commands(ACP_BACKEND_STRANDS, served) == served - KAS_LOCAL_INSPECT_COMMANDS
