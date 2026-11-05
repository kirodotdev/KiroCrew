"""An MCP change reaching a live Strands chat by relaunch + ``session/load``.

The Strands CLI reads its MCP servers once, from ``--mcp-config`` at launch. The
captures in ``test/fixtures/acp_frames/strands/mcp-relaunch-*-live.jsonl`` show a
second process, launched with a changed file, loading the same session: the new set
is offered and the earlier turns are kept. These tests pin that evidence and the
client side: a dashboard MCP write marks the client, and the next turn relaunches it
before the prompt, never while a turn holds the lock. No process is spawned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp import relaunch_reload
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MCP_RELAUNCH_RELOAD,
    ACP_BACKENDS_MCP_WIRE_REINJECT,
)
from kiro_crew.mcp_hot_reload import (
    live_sessions_apply_mcp_live,
    live_sessions_hot_reload,
    mark_reinjecting_providers,
    provider_relaunches_for_mcp,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "strands"
_SESSION = "strands-session-1"
_REPORT = re.compile(r"tools=(?P<tools>\S*) earlier_messages=(?P<earlier>\d+)")


def _frames(name: str) -> list[dict]:
    lines = (_FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _after_load(name: str) -> tuple[dict, list[str]]:
    """The ``session/load`` answer, and the agent text of every turn after it."""
    frames = _frames(name)
    load_at = next(i for i, f in enumerate(frames) if f.get("id") == 2 and "method" not in f)
    texts = [
        f["params"]["update"]["content"]["text"]
        for f in frames[load_at + 1 :]
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
    ]
    return frames[load_at], texts


def _report(text: str) -> tuple[set[str], int]:
    match = _REPORT.search(text)
    assert match, text
    return set(match["tools"].split(",")), int(match["earlier"])


class TestCapturedEvidence:
    def test_a_relaunch_with_an_added_server_offers_it_and_keeps_the_history(self) -> None:
        load, texts = _after_load("mcp-relaunch-add-live.jsonl")
        assert load["result"] == {}
        tools, earlier = _report(texts[0])
        assert {"alpha_codeword", "beta_codeword"} <= tools
        assert earlier > 0
        assert texts[-1].startswith("result=plum-17 ")

    def test_a_relaunch_without_a_server_no_longer_offers_it(self) -> None:
        load, texts = _after_load("mcp-relaunch-remove-live.jsonl")
        assert load["result"] == {}
        tools, earlier = _report(texts[-1])
        assert "beta_codeword" in tools
        assert "alpha_codeword" not in tools
        assert earlier > 0


class TestMembership:
    def test_strands_is_the_only_member(self) -> None:
        assert ACP_BACKENDS_MCP_RELAUNCH_RELOAD == frozenset({ACP_BACKEND_STRANDS})
        for backend in ACP_BACKENDS_KNOWN - {ACP_BACKEND_STRANDS}:
            assert backend not in ACP_BACKENDS_MCP_RELAUNCH_RELOAD

    def test_disjoint_from_the_other_two_mechanisms(self) -> None:
        assert not ACP_BACKENDS_MCP_RELAUNCH_RELOAD & ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
        assert not ACP_BACKENDS_MCP_RELAUNCH_RELOAD & ACP_BACKENDS_MCP_WIRE_REINJECT


class TestGate:
    def test_a_strands_client_declares_it_and_another_does_not(self, tmp_path) -> None:
        strands = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
        pi = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        assert relaunch_reload.mcp_config_relaunch_reload(strands)
        assert not relaunch_reload.mcp_config_relaunch_reload(pi)

    def test_the_provider_answers_only_for_a_direct_client(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        assert AcpProvider(acp_backend=ACP_BACKEND_STRANDS).mcp_config_relaunch_reload is True
        assert AcpProvider(acp_backend=ACP_BACKEND_PI).mcp_config_relaunch_reload is False
        fget = AcpProvider.mcp_config_relaunch_reload.fget
        assert fget is not None
        # A started runtime-backed provider holds a session provider, not a client.
        assert fget(SimpleNamespace(_client=SimpleNamespace(backend=ACP_BACKEND_STRANDS))) is False

    def test_the_abc_default_is_false(self) -> None:
        from kiro_crew.providers.base import LLMProvider

        fget = LLMProvider.mcp_config_relaunch_reload.fget
        assert fget is not None
        assert fget(SimpleNamespace()) is False

    def test_a_relaunching_provider_skips_the_reset_and_is_marked(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_STRANDS)
        assert provider_relaunches_for_mcp(provider)
        assert live_sessions_apply_mcp_live([provider])
        # The file-watcher gate keeps its own meaning.
        assert not live_sessions_hot_reload([provider])
        assert mark_reinjecting_providers([provider]) == 1
        assert provider._client._mcp_relaunch_pending is True

    def test_a_provider_outside_the_set_still_forces_the_reset(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        strands = AcpProvider(acp_backend=ACP_BACKEND_STRANDS)
        pi = AcpProvider(acp_backend=ACP_BACKEND_PI)
        assert not live_sessions_apply_mcp_live([strands, pi])
        pi.mark_mcp_config_changed()
        assert pi._client._mcp_relaunch_pending is False

    def test_a_stand_in_needs_a_literal_true(self) -> None:
        assert not provider_relaunches_for_mcp(
            SimpleNamespace(mcp_config_relaunch_reload=MagicMock())
        )
        assert not provider_relaunches_for_mcp(SimpleNamespace())


class _LiveProcess:
    returncode = None
    stdin = stdout = stderr = None


def _marked_client(tmp_path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
    client._process = _LiveProcess()
    client._session_id = _SESSION
    client.reclaim = MagicMock()
    relaunch_reload.mark_mcp_config_changed(client)
    return client


def _loads_the_same_session(client: AcpClient):
    """An ``ensure_ready`` that replaces a stalled process and loads the resume id."""

    async def _ensure_ready() -> None:
        assert client._stdin_stalled is True
        resume = client._resume_session_id
        client._stdin_stalled = False
        client._resume_session_id = None
        client._session_id = resume
        client._resumed = True

    return AsyncMock(side_effect=_ensure_ready)


class TestApplyAtTheTurnBoundary:
    @pytest.mark.asyncio
    async def test_a_marked_client_relaunches_and_loads_the_same_session(self, tmp_path) -> None:
        client = _marked_client(tmp_path)
        client.ensure_ready = _loads_the_same_session(client)

        await relaunch_reload.apply_pending_mcp_relaunch(client)

        client.ensure_ready.assert_awaited_once_with()
        assert client._session_id == _SESSION
        assert client._mcp_relaunch_pending is False
        assert client._relaunch_owed_session == ""
        # The new process's stubs are claimed before the prompt.
        client.reclaim.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_client_with_nothing_pending_reads_nothing_else(self) -> None:
        """Every other harness passes through untouched: no lock, no session read."""
        client = MagicMock(spec=AcpClient)
        await relaunch_reload.apply_pending_mcp_relaunch(client)
        client.ensure_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_mark_means_no_relaunch(self, tmp_path) -> None:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
        client._process = _LiveProcess()
        client._session_id = _SESSION
        client.ensure_ready = AsyncMock()

        await relaunch_reload.apply_pending_mcp_relaunch(client)

        client.ensure_ready.assert_not_awaited()
        assert client._stdin_stalled is False

    @pytest.mark.asyncio
    async def test_never_mid_turn_the_mark_waits_for_the_next_boundary(self, tmp_path) -> None:
        client = _marked_client(tmp_path)
        client.ensure_ready = _loads_the_same_session(client)

        await client._turn_lock.acquire()
        try:
            await relaunch_reload.apply_pending_mcp_relaunch(client)
        finally:
            client._turn_lock.release()

        client.ensure_ready.assert_not_awaited()
        assert client._stdin_stalled is False
        assert client._mcp_relaunch_pending is True
        await relaunch_reload.apply_pending_mcp_relaunch(client)
        client.ensure_ready.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_no_live_process_leaves_the_mark_to_the_next_spawn(self, tmp_path) -> None:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
        relaunch_reload.mark_mcp_config_changed(client)
        client.ensure_ready = AsyncMock()

        await relaunch_reload.apply_pending_mcp_relaunch(client)

        client.ensure_ready.assert_not_awaited()
        assert client._mcp_relaunch_pending is True

    @pytest.mark.asyncio
    async def test_a_mark_landing_during_the_relaunch_survives_it(self, tmp_path) -> None:
        client = _marked_client(tmp_path)
        inner = _loads_the_same_session(client)

        async def _ensure_ready() -> None:
            relaunch_reload.mark_mcp_config_changed(client)
            await inner()

        client.ensure_ready = AsyncMock(side_effect=_ensure_ready)

        await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert client._mcp_relaunch_pending is True

    @pytest.mark.asyncio
    async def test_a_spawn_refusal_keeps_its_own_message_and_the_load_stays_owed(
        self, tmp_path
    ) -> None:
        """A refusal that names its remedy reaches the person unchanged."""
        client = _marked_client(tmp_path)
        refusal = AcpError("Install the verified release with 'npm i -g x'", user_worded=True)

        async def _refused() -> None:
            client._session_id = None
            client._resume_session_id = None
            raise refusal

        client.ensure_ready = AsyncMock(side_effect=_refused)

        with pytest.raises(AcpError) as raised:
            await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert raised.value is refusal
        assert client._resume_session_id == _SESSION
        assert client._stdin_stalled is True
        assert client._relaunch_owed_session == _SESSION

    @pytest.mark.asyncio
    async def test_a_dead_process_stays_transient(self, tmp_path) -> None:
        client = _marked_client(tmp_path)
        died = AcpProcessDied("child exited")
        client.ensure_ready = AsyncMock(side_effect=died)

        with pytest.raises(AcpProcessDied) as raised:
            await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert raised.value is died

    @pytest.mark.asyncio
    async def test_a_non_acp_failure_refuses_the_turn_with_the_plain_message(
        self, tmp_path
    ) -> None:
        client = _marked_client(tmp_path)
        client.ensure_ready = AsyncMock(side_effect=OSError("disk full"))

        with pytest.raises(AcpError) as raised:
            await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert str(raised.value) == relaunch_reload.RELAUNCH_FAILED_TEXT
        assert raised.value.transient is False
        assert client._resume_session_id == _SESSION
        assert client._stdin_stalled is True

    @pytest.mark.asyncio
    async def test_a_load_that_fell_back_to_a_new_session_is_not_this_chat(self, tmp_path) -> None:
        client = _marked_client(tmp_path)

        async def _fresh() -> None:
            client._stdin_stalled = False
            client._resume_session_id = None
            client._session_id = "strands-session-2"
            client._resumed = False

        client.ensure_ready = AsyncMock(side_effect=_fresh)

        with pytest.raises(AcpError) as raised:
            await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert str(raised.value) == relaunch_reload.RELAUNCH_FAILED_TEXT
        # The next ensure_ready replaces the fresh process and loads the chat.
        assert client._stdin_stalled is True
        assert client._resume_session_id == _SESSION
        assert client._relaunch_owed_session == _SESSION
        # Every reader (the shutdown sweep saves this id) still names the real chat.
        assert client._session_id == _SESSION

    @pytest.mark.asyncio
    async def test_a_retry_that_opens_another_session_is_refused_again(self, tmp_path) -> None:
        """The owed load is checked on every later turn, not only the relaunch's own."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
        client._process = _LiveProcess()
        client._relaunch_owed_session = _SESSION
        # What the next ensure_ready left when its load fell back to session/new.
        client._session_id = "strands-session-3"
        client._resumed = False

        with pytest.raises(AcpError) as raised:
            await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert str(raised.value) == relaunch_reload.RELAUNCH_FAILED_TEXT
        assert client._stdin_stalled is True
        assert client._resume_session_id == _SESSION
        assert client._relaunch_owed_session == _SESSION
        assert client._session_id == _SESSION

    @pytest.mark.asyncio
    async def test_a_retry_that_loads_the_owed_session_settles_it(self, tmp_path) -> None:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
        client._process = _LiveProcess()
        client._relaunch_owed_session = _SESSION
        client._session_id = _SESSION
        client._resumed = True
        client.ensure_ready = AsyncMock()
        client.reclaim = MagicMock()

        await relaunch_reload.apply_pending_mcp_relaunch(client)

        assert client._relaunch_owed_session == ""
        assert client._stdin_stalled is False
        client.ensure_ready.assert_not_awaited()
        client.reclaim.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_provider_applies_it_before_the_confirmation_and_the_prompt(
        self, monkeypatch
    ) -> None:
        from kiro_crew.providers import acp_turns

        order: list[str] = []
        client = MagicMock(spec=AcpClient)
        client.ensure_ready = AsyncMock(side_effect=lambda: order.append("ready"))
        client.confirm_member_projection = AsyncMock(side_effect=lambda: order.append("confirm"))

        async def _apply(target: object) -> None:
            assert target is client
            order.append("apply")

        monkeypatch.setattr(relaunch_reload, "apply_pending_mcp_relaunch", _apply)

        await acp_turns._ready_confirmed_session(SimpleNamespace(_client=client))

        assert order == ["ready", "apply", "confirm"]


@pytest.mark.asyncio
async def test_the_relaunch_needs_a_session(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
    client.ensure_ready = AsyncMock()
    with pytest.raises(ValueError):
        await relaunch_reload.relaunch_and_reload(client)
    client.ensure_ready.assert_not_awaited()


def test_the_strands_launch_clears_the_mark_right_before_it_reads_the_spec() -> None:
    """A mark set before this read is applied by this launch; one set after it survives."""
    import inspect

    from kiro_crew.acp.harness import strands as strands_mod

    source = inspect.getsource(strands_mod.StrandsLaunch.resolve_spawn)
    clear = source.index("session._mcp_relaunch_pending = False")
    read = source.index("await session._prepare_session_mcp()")
    assert clear < read
    assert "await" not in source[clear:read].replace("await session._prepare_session_mcp()", "")
