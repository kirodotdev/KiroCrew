"""Claude Code's own slash commands reach the harness on ``acp_backend = "claude"``.

The defect these pin: the dashboard forwarded an arbitrary leading-slash word
(``/review``, ``/init``, a ``.claude/commands`` name, a skill) only when
``agent.provider == "claude_code"``, a label the public schema never admits. On the
real Claude harness (``provider = "acp"``, ``acp_backend = "claude"``) the word was
treated as prose: ``build_message`` prepended Crew's context and the adapter never
saw a command. The forward is now granted by the backend's positive
``runs_own_slash_commands`` capability (``ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from turn_harness import ScriptedProvider, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS
from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.dashboard import chat_slack
from kiro_crew.dashboard.chat_handlers import _send_harness_command
from kiro_crew.dashboard.chat_runner import (
    _forwards_harness_command,
    _run_chat,
    effective_session_key,
)
from kiro_crew.dashboard.chat_slack import maybe_auto_link_slack
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.providers.acp import AcpProvider


@pytest.fixture(autouse=True)
def _no_spec_hooks(_floor_monkeypatch):
    """These doubles carry no spec projection to refresh."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._prepare_spec_hooks",
        AsyncMock(return_value=([], False, None)),
    )

    async def _claimed(_sessions, _key, _agent, claimed, _claim):
        return claimed

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.reproject_claimed_session", _claimed
    )


def _provider(backend: str) -> MagicMock:
    client = MagicMock(spec=AcpProvider)
    client.capabilities = capabilities_for(backend)
    client.manual_compact_unsupported_backend = None
    client.is_process_alive.return_value = True
    return client


def _live(tmp_path, backend: str):
    """A slot whose live provider is *backend*, recording each send channel apart."""
    client = _provider(backend)
    commands = MagicMock()
    prompts = MagicMock()

    def _recorder(calls):
        async def _record(msg, *_a, **_k):
            calls(msg)
            return
            yield  # pragma: no cover - generator shape only

        return _record

    client.stream = _recorder(prompts)
    client.stream_command = _recorder(commands)

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
    slot = state.get_or_create_slot("claude-slash-slot")
    state.sessions._sessions = {effective_session_key(slot): SimpleNamespace(provider=client)}
    state.sessions.get_provider = MagicMock(return_value=client)
    state.sessions.is_provider_alive = AsyncMock(return_value=True)
    return state, slot, commands, prompts


class TestMembership:
    def test_only_claude_is_a_member(self) -> None:
        assert ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS == {ACP_BACKEND_CLAUDE}

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX])
    def test_other_harnesses_do_not_inherit_it(self, backend) -> None:
        assert capabilities_for(backend).runs_own_slash_commands is False

    def test_an_unknown_backend_fails_closed(self) -> None:
        assert capabilities_for("not-a-harness").runs_own_slash_commands is False
        assert UNKNOWN_BACKEND_CAPABILITIES.runs_own_slash_commands is False
        assert capabilities_for(ACP_BACKEND_CLAUDE).runs_own_slash_commands is True


class TestDashboardTurn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", ["/review the diff", "/init", "/security-review"])
    async def test_claude_command_is_forwarded_raw(self, tmp_path, message) -> None:
        """RED on main: the word went through ``stream`` with Crew's context."""
        state, slot, commands, prompts = _live(tmp_path, ACP_BACKEND_CLAUDE)

        await _run_chat(state, slot, message)

        commands.assert_called_once_with(message)
        prompts.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_quick_prompt_is_still_expanded_on_claude(self, tmp_path) -> None:
        """A macro is not a command; forwarding it would make it do nothing."""
        state, slot, commands, prompts = _live(tmp_path, ACP_BACKEND_CLAUDE)

        await _run_chat(state, slot, "/plain explain this")

        commands.assert_not_called()
        assert prompts.called

    @pytest.mark.asyncio
    async def test_kiro_keeps_an_unknown_word_as_prompt_text(self, tmp_path) -> None:
        state, slot, commands, prompts = _live(tmp_path, ACP_BACKEND_KIRO)

        await _run_chat(state, slot, "/review the diff")

        commands.assert_not_called()
        assert prompts.called


class TestBackendResolution:
    @pytest.mark.asyncio
    async def test_no_live_provider_reads_the_member_aware_selection(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.sessions.get_provider = MagicMock(return_value=None)
        with patch(
            "kiro_crew.dashboard.chat_runner.select_provider_backend",
            return_value=ACP_BACKEND_CLAUDE,
        ):
            assert await _forwards_harness_command(
                state, "dashboard:s1", "/review", cc_provider=False
            )

    @pytest.mark.asyncio
    async def test_a_live_provider_answers_for_itself(self, tmp_path) -> None:
        """A member session pinned to kiro-cli is not misrouted by a claude default."""
        state = _make_state(tmp_path)
        state.sessions.get_provider = MagicMock(return_value=_provider(ACP_BACKEND_KIRO))
        state.sessions.is_provider_alive = AsyncMock(return_value=True)
        with patch(
            "kiro_crew.dashboard.chat_runner.select_provider_backend",
            return_value=ACP_BACKEND_CLAUDE,
        ):
            assert not await _forwards_harness_command(
                state, "dashboard:s1", "/review", cc_provider=False
            )

    @pytest.mark.asyncio
    async def test_plain_text_never_looks_the_backend_up(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.sessions.get_provider = MagicMock(side_effect=AssertionError("looked up"))

        assert not await _forwards_harness_command(
            state, "dashboard:s1", "hello", cc_provider=False
        )
        assert await _forwards_harness_command(state, "dashboard:s1", "/model", cc_provider=False)


class TestOtherSurfaces:
    @pytest.mark.asyncio
    async def test_send_reports_a_claude_command(self, tmp_path) -> None:
        """The app-grant refusal reads the runner's predicate, so it sees the same
        commands the runner forwards."""
        state, slot, _commands, _prompts = _live(tmp_path, ACP_BACKEND_CLAUDE)

        assert await _send_harness_command("/review the diff", state, slot) == "/review"
        assert await _send_harness_command("/plain explain", state, slot) == ""

    @pytest.mark.asyncio
    async def test_send_leaves_an_unknown_word_alone_on_kiro(self, tmp_path) -> None:
        state, slot, _commands, _prompts = _live(tmp_path, ACP_BACKEND_KIRO)

        assert await _send_harness_command("/review the diff", state, slot) == ""

    @pytest.mark.asyncio
    async def test_slack_opens_a_thread_for_a_claude_command(self, tmp_path, monkeypatch) -> None:
        """Its answer is mirrored like a prompt's, so the thread it opens holds it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state, _slot, _commands, _prompts = _live(tmp_path, ACP_BACKEND_CLAUDE)
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="D_OWNER")
        state.slack_client.post_message = AsyncMock(return_value="ts_anchor")
        state.owner_id = "U123"
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.append("user", "/review the diff")
        slot.drain()
        monkeypatch.setattr(chat_slack, "_read_auto_link_settings", lambda: (True, False))
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *_a, **_k: None)

        assert await maybe_auto_link_slack(state, slot) is True
        state.slack_client.post_message.assert_awaited_once()


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are the Claude harness's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


_ANSWER = "The diff looks right; one nit in the test."


async def _linked_turn(message: str) -> tuple[TurnRecord, str]:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value="ts_reply")

    def _link(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    record = await run_turn(
        TurnScript(
            message=message,
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
            setup=_link,
            provider=_ClaudeProvider,
        ),
        config={"agent": {"acp_backend": ACP_BACKEND_CLAUDE}},
    )
    posted = " ".join(str(c.kwargs.get("text", c.args)) for c in slack.post_message.await_args_list)
    return record, posted


class TestLinkedThread:
    """A forwarded claude command still owes the linked Slack thread its answer."""

    @pytest.mark.asyncio
    async def test_a_claude_command_answers_in_the_linked_thread(self) -> None:
        record, posted = await _linked_turn("/review the diff")

        assert [c.args for c in record.provider_calls if c.name == "stream_command"] == [
            ("/review the diff",)
        ]
        assert _ANSWER in posted

    @pytest.mark.asyncio
    async def test_a_kiro_command_name_still_keeps_off_the_thread(self) -> None:
        _record, posted = await _linked_turn("/model")

        assert _ANSWER not in posted
