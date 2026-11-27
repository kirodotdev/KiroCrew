"""``AcpSessionProvider.cleanup_session`` deletes the record on the host the session ran on.

kiro-cli keeps a session's record as ``<sid>.json`` + ``.jsonl`` under its sessions
directory. KAS keeps it in the engine's own store, which only ``_kiro/session/delete``
removes, and codex keeps the thread's rollout under the ``CODEX_HOME`` its child ran
with. The crewmate lessons draft (``member_export._draft_turn``) relies on this call to
drop its one-shot session's record after the owning runtime is gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import released_sessions, sandbox_launch_mark
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, METHOD_KAS_SESSION_DELETE
from kiro_crew.providers.acp import AcpProvider

_KAS_SID = "sess_3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"
_CODEX_SID = "01a1c612-b002-72e3-89c4-571c49738762"


def _runtime(tmp_path: Path, backend: str, *, alive: bool = True) -> AcpRuntime:
    runtime = AcpRuntime(
        work_dir=tmp_path / f"ws-{backend}", sandbox_mode="off", acp_backend=backend
    )
    runtime._process = object() if alive else None  # type: ignore[assignment]
    return runtime


def _provider(runtime: AcpRuntime, sid: str) -> AcpSessionProvider:
    handle = MagicMock()
    handle.session_id = sid
    return AcpSessionProvider(handle, runtime)


def _entries() -> list[dict]:
    directory = released_sessions.released_sessions_dir()
    if not directory.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


class TestKas:
    @pytest.mark.asyncio
    async def test_a_live_runtime_sends_the_engine_delete(self, tmp_path):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        send = AsyncMock(return_value={})
        runtime._send_and_await = send  # type: ignore[method-assign]

        await _provider(runtime, _KAS_SID).cleanup_session(_KAS_SID)

        assert [c.args[0] for c in send.await_args_list] == [METHOD_KAS_SESSION_DELETE]
        assert send.await_args.args[1] == {"sessionId": _KAS_SID}
        assert _entries() == []

    @pytest.mark.asyncio
    async def test_a_dead_runtime_schedules_the_delete_due_now(self, tmp_path):
        """The lessons draft's case: the owning provider's shutdown killed the process."""
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, alive=False)

        await _provider(runtime, _KAS_SID).cleanup_session(_KAS_SID)

        [entry] = _entries()
        assert entry["session_id"] == _KAS_SID
        assert entry["backend"] == ACP_BACKEND_KAS
        assert entry["delete_at"] == entry["released_at"]
        assert [e.session_id for e in released_sessions.due()] == [_KAS_SID]

    @pytest.mark.asyncio
    async def test_a_failed_delete_schedules_it(self, tmp_path):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        runtime._send_and_await = AsyncMock(side_effect=TimeoutError())  # type: ignore[method-assign]

        await _provider(runtime, _KAS_SID).cleanup_session(_KAS_SID)

        assert [e["session_id"] for e in _entries()] == [_KAS_SID]

    @pytest.mark.asyncio
    async def test_a_sandboxed_process_writes_no_entry(self, tmp_path, monkeypatch, caplog):
        """Its ``released-sessions/`` is masked, so an entry there would be lost."""
        monkeypatch.setattr(sandbox_launch_mark, "_launched_in_sandbox", True)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, alive=False)

        with caplog.at_level("WARNING", logger="kiro_crew.acp.session_handle"):
            await _provider(runtime, _KAS_SID).cleanup_session(_KAS_SID)

        assert _entries() == []
        assert "could not delete the record" in caplog.text

    @pytest.mark.asyncio
    async def test_an_unsafe_id_is_refused_without_raising(self, tmp_path, caplog):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, alive=False)

        with caplog.at_level("WARNING", logger="kiro_crew.acp.session_handle"):
            await _provider(runtime, "../x").cleanup_session("../x")

        assert _entries() == []
        assert "could not schedule the delete" in caplog.text


def _kas_acp_provider(tmp_path: Path, *, alive: bool) -> AcpProvider:
    """A KAS ``AcpProvider`` after its runtime start put an ``AcpSessionProvider`` in ``_client``."""
    provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
    provider._client = _provider(  # type: ignore[assignment]
        _runtime(tmp_path, ACP_BACKEND_KAS, alive=alive), _KAS_SID
    )
    return provider


class TestAcpProviderOnKas:
    """The provider ``SessionManager.get_or_create`` hands out when the runtime is not shared."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    async def test_a_disposable_chats_shutdown_writes_no_entry(self, tmp_path, memory_mode):
        """Its handle's ``destroy`` already sent ``_kiro/session/delete``; a second one is redundant."""
        provider = _kas_acp_provider(tmp_path, alive=True)
        provider.memory_mode = memory_mode
        runtime = provider._client._runtime

        async def _shutdown() -> None:
            runtime._process = None  # the owned runtime is killed after the destroy

        provider._client.shutdown = _shutdown  # type: ignore[method-assign]

        await provider.shutdown()

        assert _entries() == []

    @pytest.mark.asyncio
    async def test_a_shut_down_provider_schedules_the_delete(self, tmp_path):
        await _kas_acp_provider(tmp_path, alive=False).cleanup_session(_KAS_SID)

        assert [e.session_id for e in released_sessions.due()] == [_KAS_SID]

    @pytest.mark.asyncio
    async def test_the_lessons_draft_leaves_a_delete_for_its_record(self, tmp_path, monkeypatch):
        from kiro_crew import llm_helpers
        from kiro_crew.dashboard import request_priority
        from kiro_crew.dashboard.handlers import member_export

        provider = _kas_acp_provider(tmp_path, alive=False)
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.destroy = AsyncMock()
        request = MagicMock()
        request.app = {"state": MagicMock(sessions=sessions)}
        monkeypatch.setattr(llm_helpers, "stream_and_collect", AsyncMock(return_value="draft"))
        monkeypatch.setattr(request_priority, "owner_start_priority", lambda _r: None)

        assert await member_export._draft_turn(request, "prompt") == "draft"

        sessions.destroy.assert_awaited_once()
        assert [e.session_id for e in released_sessions.due()] == [_KAS_SID]


class TestCodex:
    @pytest.mark.asyncio
    async def test_the_rollout_under_the_runtimes_codex_home_is_deleted(self, tmp_path):
        home = tmp_path / "codex-home"
        day = home / "sessions" / "2026" / "11" / "10"
        day.mkdir(parents=True)
        rollout = day / f"rollout-2026-11-10T15-27-55-{_CODEX_SID}.jsonl"
        other = day / "rollout-2026-11-10T15-27-55-01a1c611-b673-7e80-ac7a-9ff336984caf.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        other.write_text("{}\n", encoding="utf-8")
        runtime = _runtime(tmp_path, ACP_BACKEND_CODEX)
        runtime._extra_env = {"CODEX_HOME": str(home)}

        await _provider(runtime, _CODEX_SID).cleanup_session(_CODEX_SID)

        assert not rollout.exists()
        assert other.exists()


class TestKiroCli:
    @pytest.mark.asyncio
    async def test_the_transcript_pair_is_unlinked_and_no_verb_is_sent(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions" / "cli"
        sessions.mkdir(parents=True)
        files = [sessions / "sid-k.json", sessions / "sid-k.jsonl"]
        for f in files:
            f.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions)
        runtime = _runtime(tmp_path, "")
        send = AsyncMock(return_value={})
        runtime._send_and_await = send  # type: ignore[method-assign]

        await _provider(runtime, "sid-k").cleanup_session("sid-k")

        assert not any(f.exists() for f in files)
        send.assert_not_awaited()
        assert _entries() == []
