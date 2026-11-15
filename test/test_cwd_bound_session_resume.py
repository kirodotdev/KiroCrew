"""A harness that binds a saved session to its folder is not resumed in another folder.

opencode keeps a session on the directory it was created in, so a ``session/load`` that
names a different ``cwd`` splits the session across two of its instances: approvals
answered in one are dropped by the other, and the MCP servers sent with the load are
mounted where the model is not. When a chat's folder changed, the next start therefore
opens a fresh session in the new folder and the conversation is replayed as text.
Driven through the real ``SessionManager`` allocator, with only the provider faked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

KEY = "dashboard:test"


def _factory(backend: str, made: list):
    from kiro_crew.providers.acp import AcpProvider

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = object.__new__(AcpProvider)
        provider._client = MagicMock()
        provider._client._session_id = "new-sid"
        provider._client._work_dir = kwargs["cwd"]
        provider._client.cwd = kwargs["cwd"]
        provider._client._pid = None
        provider._client.backend = backend
        provider._client.resumed = False
        provider._client.set_resume_session_id = MagicMock()
        provider._history_replay_needed = False
        provider._defer_replay_sid_promotion = False
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        made.append(provider)
        return provider

    return factory


async def _start(tmp_path, monkeypatch, backend: str, stored: str, requested: str):
    """Start ``KEY`` in *requested* when its saved session was opened in *stored*."""
    from kiro_crew.providers.acp import provider_label

    native = tmp_path / "native-sessions"
    native.mkdir()
    (native / "old-sid.json").write_text("{}", encoding="utf-8")
    (native / "old-sid.jsonl").write_text('{"role":"user","content":"x"}\n', encoding="utf-8")
    monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: native)
    for name in {stored, requested} - {""}:
        (tmp_path / name).mkdir(exist_ok=True)
    made: list = []
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    mgr = SessionManager(cfg, provider_factory=_factory(backend, made))
    probe = _factory(backend, [])(cwd=str(tmp_path))
    stored_cwd = str(tmp_path / stored) if stored else ""
    mgr._session_map.set(KEY, "old-sid", provider=provider_label(probe), cwd=stored_cwd)
    await mgr.get_or_create(KEY, cwd=str(tmp_path / requested))
    await mgr.close_all()
    return made[-1]._client.set_resume_session_id


@pytest.mark.asyncio
async def test_an_opencode_session_is_not_loaded_in_a_folder_it_was_not_opened_in(
    tmp_path, monkeypatch
):
    resume = await _start(tmp_path, monkeypatch, ACP_BACKEND_OPENCODE, "workspace", "repo")

    resume.assert_not_called()


@pytest.mark.asyncio
async def test_an_opencode_session_whose_folder_was_not_recorded_starts_fresh(
    tmp_path, monkeypatch
):
    resume = await _start(tmp_path, monkeypatch, ACP_BACKEND_OPENCODE, "", "repo")

    resume.assert_not_called()


@pytest.mark.asyncio
async def test_an_opencode_session_still_resumes_in_its_own_folder(tmp_path, monkeypatch):
    resume = await _start(tmp_path, monkeypatch, ACP_BACKEND_OPENCODE, "repo", "repo")

    resume.assert_called_once_with("old-sid")


@pytest.mark.asyncio
async def test_a_kiro_session_still_resumes_after_its_folder_changed(tmp_path, monkeypatch):
    """kiro-cli takes the cwd from each load, so a moved chat keeps its native history."""
    resume = await _start(tmp_path, monkeypatch, ACP_BACKEND_KIRO, "workspace", "repo")

    resume.assert_called_once_with("old-sid")
