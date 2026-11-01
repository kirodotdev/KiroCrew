"""``/model``, ``/tools`` and ``/mcp`` on KAS are answered by the dashboard.

KAS has no command channel, so the slash gate answers these three itself. They
are read from state the dashboard already holds: the slot's model pin
and the session's advertised list, the agent entry the session registered with
KAS, and the slot's MCP session report. None of them reaches the model or claims
a session, and none prints a server's env, headers, command or args.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from chat_test_helpers import _make_state

from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import _run_chat, effective_session_key
from kiro_crew.providers.acp import AcpProvider

_SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


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


def _live(tmp_path, backend: str = ACP_BACKEND_KAS, *, alive: bool = True):
    client = MagicMock(spec=AcpProvider)
    client.capabilities = capabilities_for(backend)
    client.manual_compact_unsupported_backend = None
    client.is_process_alive.return_value = True
    client.available_models.return_value = []
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
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("inspect-slot")
    state.sessions._sessions = {effective_session_key(slot): SimpleNamespace(provider=client)}
    state.sessions.get_provider = MagicMock(return_value=client)
    state.sessions.is_provider_alive = AsyncMock(return_value=alive)
    return state, slot, client, calls


def _reply(slot) -> str:
    return "\n".join(m.get("content", "") for m in slot.messages)


def _assert_local(state, calls) -> None:
    calls.assert_not_called()
    state.sessions.get_or_create.assert_not_called()


class TestModel:
    @pytest.mark.asyncio
    async def test_shows_the_pin_and_the_sessions_list(self, tmp_path) -> None:
        """RED on main: ``/model`` only got the "doesn't work with KAS" notice."""
        state, slot, client, calls = _live(tmp_path)
        slot.model = "claude-x"
        client.available_models.return_value = [{"modelId": "auto"}, {"modelId": "claude-x"}]

        await _run_chat(state, slot, "/model")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "Model: `claude-x`" in text, text
        assert "`auto`, `claude-x`" in text, text
        assert "doesn't work with KAS" not in text

    @pytest.mark.asyncio
    async def test_falls_back_to_the_pickers_catalog(self, tmp_path, _floor_monkeypatch) -> None:
        """On the kiro-cli relay route the picker lists the ``--list-models``
        catalog while the session advertises nothing; ``/model`` lists the same."""
        from kiro_crew.dashboard.handlers import agents as picker

        state, slot, _client, calls = _live(tmp_path)
        _floor_monkeypatch.setattr(
            picker._catalog_cache,
            "models",
            [{"model_id": "auto", "model_name": "auto"}, {"model_id": "m-2", "model_name": "m-2"}],
        )

        await _run_chat(state, slot, "/model")

        _assert_local(state, calls)
        assert "`auto`, `m-2`" in _reply(slot), _reply(slot)

    @pytest.mark.asyncio
    async def test_the_pickers_catalog_is_narrowed_as_the_picker_narrows_it(
        self, tmp_path, _floor_monkeypatch
    ) -> None:
        """Another live session that advertises only ``auto`` narrows the picker's
        list; ``/model`` lists the same narrowed set, not the raw catalog."""
        from kiro_crew.dashboard.handlers import agents as picker

        state, slot, client, calls = _live(tmp_path, alive=False)
        other = MagicMock(spec=AcpProvider)
        other.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        other.available_models.return_value = [{"modelId": "auto"}]
        other.maybe_refresh_available_models = AsyncMock(return_value=[{"modelId": "auto"}])
        state.sessions.active_providers = MagicMock(return_value=[other])
        _floor_monkeypatch.setattr(
            picker._catalog_cache,
            "models",
            [{"model_id": "auto", "model_name": "auto"}, {"model_id": "m-2", "model_name": "m-2"}],
        )

        await _run_chat(state, slot, "/model")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "Available: `auto`" in text, text
        assert "m-2" not in text, text

    @pytest.mark.asyncio
    async def test_falls_back_to_the_recorded_catalog(self, tmp_path) -> None:
        state, slot, _client, calls = _live(tmp_path, alive=False)

        with patch(
            "kiro_crew.dashboard.kas_local_commands._inspect_catalog_model_ids",
            return_value=["auto", "m-1"],
        ):
            await _run_chat(state, slot, "/model")

        _assert_local(state, calls)
        assert "`auto`, `m-1`" in _reply(slot)

    @pytest.mark.asyncio
    async def test_an_id_runs_the_pickers_transaction_as_its_own_turn(self, tmp_path) -> None:
        state, slot, _client, calls = _live(tmp_path)
        switch = AsyncMock(
            return_value=web.json_response({"ok": True, "model": "m-1", "deferred": True})
        )

        with patch("kiro_crew.dashboard.chat_handlers.switch_slot_model", switch):
            await _run_chat(state, slot, "/model  m-1 ")

        _assert_local(state, calls)
        switch.assert_awaited_once()
        args = switch.await_args.args
        assert args[3] == "m-1"
        caller = args[4]
        assert caller.own_turn is True and caller.owner is False and caller.request is None
        assert "Model set to `m-1`. It applies when the current turn ends." in _reply(slot)

    @pytest.mark.asyncio
    async def test_a_refused_pick_says_why(self, tmp_path) -> None:
        state, slot, _client, calls = _live(tmp_path)
        switch = AsyncMock(
            return_value=web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        )

        with patch("kiro_crew.dashboard.chat_handlers.switch_slot_model", switch):
            await _run_chat(state, slot, "/model m-1")

        _assert_local(state, calls)
        assert "Could not switch the model to `m-1`: a turn is in flight" in _reply(slot)


class TestSwitchTransaction:
    """The picker and the command run ONE transaction; only who asked differs."""

    @pytest.mark.asyncio
    async def test_the_command_is_not_busy_on_its_own_turn(self, tmp_path) -> None:
        from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, switch_slot_model

        state, slot, _client, _calls = _live(tmp_path)
        state.sessions.get_provider = MagicMock(return_value=None)
        slot._turn_admission_reserved = True

        picker = await switch_slot_model(
            state, slot, slot.key, "auto", SlotAgentSwitchCaller(app="", remote="", owner=False)
        )
        assert picker.status == 200
        assert '"deferred": true' in picker.text

        slot.model = ""
        slot._deferred_model_switch_key = None
        own = await switch_slot_model(
            state, slot, slot.key, "auto", SlotAgentSwitchCaller.for_turn(slot)
        )
        assert own.status == 200, own.text
        assert "deferred" not in own.text
        assert slot.model == "auto"


def _registered_entry() -> dict:
    return {
        "id": "kirocrew",
        "tools": ["read_file", "shell", "@kirocrew-core", "@gh"],
        "excludedTools": ["delete_file"],
        "permissions": {
            "rules": [
                {"capability": "mcp", "match": ["kirocrew-core/*"], "effect": "allow"},
                {"capability": "web_fetch", "effect": "allow"},
                {"capability": "fs_write", "effect": "allow"},
                {"capability": "fs_write", "effect": "ask"},
            ]
        },
        "_crewAnsweredBuiltins": ["read_file"],
        "mcpServers": {
            "gh": {
                "command": "gh-mcp",
                "args": ["--token", _SECRET],
                "env": {"GITHUB_TOKEN": _SECRET},
            },
            "web": {"url": "https://example.test/mcp", "headers": {"X-Api-Key": _SECRET}},
        },
    }


class TestTools:
    @pytest.mark.asyncio
    async def test_lists_the_registered_agent_without_credentials(self, tmp_path) -> None:
        """RED on main: ``/tools`` only got the "doesn't work with KAS" notice."""
        state, slot, client, calls = _live(tmp_path)
        client.kas_registered_agents = [{"id": "other", "tools": "*"}, _registered_entry()]
        client.kas_projected_agent = "kirocrew"
        client.kas_active_mode_id = ""

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "Tools for agent `kirocrew`" in text, text
        assert "`read_file`, `shell`, `@kirocrew-core`, `@gh`" in text
        assert "Not offered: `delete_file`" in text
        assert "MCP servers in this agent: `gh`, `web`" in text
        assert "Runs without asking: `@kirocrew-core/*`, `web_fetch`" in text
        # An unscoped ask cancels the allow, as KAS resolves it.
        assert "fs_write" not in text
        assert "after Crew's checks: `read_file`" in text
        for leaked in (_SECRET, "GITHUB_TOKEN", "X-Api-Key", "gh-mcp", "example.test"):
            assert leaked not in text, leaked

    @staticmethod
    def _switched(tmp_path, mode_id: str):
        """A session whose batch holds a crewmate under a KAS-reserved id, now on *mode_id*.

        The handle sets ``kas_projected_agent`` to the crewmate id behind the
        mode, which is the mode id itself for a mode Crew did not register.
        """
        from kiro_crew.acp._dispatch import kas_crew_agent_id

        state, slot, client, calls = _live(tmp_path)
        entries = [
            {"id": "kirocrew", "tools": ["read_file"]},
            {"id": "vibe", "tools": ["crewmate_only_tool"]},
        ]
        client.kas_registered_agents = entries
        client.kas_projected_agent = kas_crew_agent_id(entries, mode_id)
        client.kas_active_mode_id = mode_id
        return state, slot, calls

    @pytest.mark.asyncio
    async def test_a_switch_to_the_crewmate_reads_its_entry(self, tmp_path) -> None:
        from kiro_crew.agent_files import kas_wire_agent_id

        wire = kas_wire_agent_id("vibe")
        assert wire != "vibe", "the case below needs an id the wire renames"
        state, slot, calls = self._switched(tmp_path, wire)

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        assert "Tools for agent `vibe`" in _reply(slot)
        assert "`crewmate_only_tool`" in _reply(slot)

    @pytest.mark.asyncio
    async def test_a_builtin_mode_of_the_same_name_is_not_the_crewmate(self, tmp_path) -> None:
        """The KAS built-in ``vibe`` runs under the crewmate's name but not its
        entry: matched by wire id, as the session's own approvals are."""
        state, slot, calls = self._switched(tmp_path, "vibe")

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "crewmate_only_tool" not in text, text
        assert "Kiro Crew did not register" in text

    @pytest.mark.asyncio
    async def test_an_unmatched_agent_does_not_borrow_the_first_entry(self, tmp_path) -> None:
        state, slot, client, calls = _live(tmp_path)
        client.kas_registered_agents = [{"id": "other", "tools": ["other_tool"]}]
        client.kas_projected_agent = "kirocrew"
        client.kas_active_mode_id = ""

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        assert "other_tool" not in _reply(slot)
        assert "Kiro Crew did not register" in _reply(slot)

    @pytest.mark.asyncio
    async def test_a_narrowed_grant_is_not_shown_as_unconditional(self, tmp_path) -> None:
        """A scoped deny or an ``exclude`` keeps the grant but takes tools out of
        it, so the summary must not read as "every tool on this server"."""
        state, slot, client, calls = _live(tmp_path)
        client.kas_registered_agents = [
            {
                "id": "kirocrew",
                "tools": ["@srv", "@other"],
                "permissions": {
                    "rules": [
                        {"capability": "mcp", "match": ["srv/*"], "effect": "allow"},
                        {"capability": "mcp", "match": ["srv/drop"], "effect": "deny"},
                        {"capability": "web_fetch", "effect": "allow", "exclude": ["*.test"]},
                    ]
                },
            }
        ]
        client.kas_projected_agent = "kirocrew"
        client.kas_active_mode_id = ""

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "`@srv/* (with exceptions)`" in text, text
        assert "`web_fetch (with exceptions)`" in text, text

    @pytest.mark.asyncio
    async def test_a_provider_with_no_batch_says_so(self, tmp_path) -> None:
        state, slot, client, calls = _live(tmp_path)
        client.kas_registered_agents = None

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        assert "no agent batch registered with KAS" in _reply(slot)

    def test_the_direct_client_provider_reports_no_batch(self) -> None:
        """``AcpProvider`` wraps an ``AcpClient``, which holds no batch: that is
        ``None`` (no batch), not ``[]`` (a batch with nothing in it)."""
        provider = AcpProvider.__new__(AcpProvider)
        provider._client = SimpleNamespace()
        assert provider.kas_registered_agents is None

    @pytest.mark.asyncio
    async def test_no_session_says_so(self, tmp_path) -> None:
        state, slot, _client, calls = _live(tmp_path, alive=False)

        await _run_chat(state, slot, "/tools")

        _assert_local(state, calls)
        assert "No tool list yet" in _reply(slot)


class TestMcp:
    @pytest.mark.asyncio
    async def test_lists_each_server_with_its_state(self, tmp_path) -> None:
        """RED on main: ``/mcp`` only got the "doesn't work with KAS" notice."""
        state, slot, _client, calls = _live(tmp_path)
        slot.set_mcp_report(
            {
                "configured": ["kirocrew-core", "gh", "jira", "slow"],
                "ready": ["kirocrew-core", "empty"],
                "failed": ["gh"],
                "awaiting_auth": ["jira"],
                "failures": {"gh": f"spawn failed token={_SECRET}"},
                "no_tools": ["empty"],
                "restart_to_load": ["slow"],
            },
            "sid-1",
        )

        await _run_chat(state, slot, "/mcp")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "`kirocrew-core`: ready" in text, text
        assert "`gh`: failed: spawn failed" in text
        assert "`jira`: waiting for sign-in" in text
        assert "`slow`: not reported yet (start a new session to load it)" in text
        assert "`empty`: ready, but it offers no tools" in text
        assert _SECRET not in text

    @pytest.mark.asyncio
    async def test_a_declared_server_with_no_report_is_listed(self, tmp_path) -> None:
        """A server the agent entry declares is listed even before it reports, so
        ``/mcp`` names every server ``/tools`` names."""
        state, slot, client, calls = _live(tmp_path)
        client.kas_registered_agents = [_registered_entry()]
        client.kas_projected_agent = "kirocrew"
        client.kas_active_mode_id = ""
        slot.set_mcp_report({"configured": ["kirocrew-core"], "ready": ["kirocrew-core"]}, "sid-2")

        await _run_chat(state, slot, "/mcp")

        _assert_local(state, calls)
        text = _reply(slot)
        assert "`kirocrew-core`: ready" in text, text
        assert "`gh`: not reported yet" in text, text
        assert "`web`: not reported yet" in text, text
        assert _SECRET not in text

    @pytest.mark.asyncio
    async def test_no_report_says_so(self, tmp_path) -> None:
        state, slot, _client, calls = _live(tmp_path)

        await _run_chat(state, slot, "/mcp")

        _assert_local(state, calls)
        assert "No MCP report yet" in _reply(slot)


class TestKiroUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/model", "/tools", "/mcp"])
    async def test_kiro_still_runs_them_natively(self, tmp_path, command) -> None:
        state, slot, _client, calls = _live(tmp_path, ACP_BACKEND_KIRO)

        await _run_chat(state, slot, command)

        calls.assert_called_once_with(command)
