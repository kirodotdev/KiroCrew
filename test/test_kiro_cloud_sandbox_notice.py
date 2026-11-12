"""A Kiro Cloud chat that moves onto a new sandbox says so, once.

Edit & Resend, a reset and compaction drop the session's resume id, and a reopen
that fails (or replays no turn) falls back: each start then opens a new cloud
sandbox without the earlier one's files. The start leaves a notice that the chat
moved and that the earlier sandbox was not deleted; a reopen of the recorded
session, or a first start, leaves none. Runs against the KAS stub of
``test_kiro_cloud_session``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from test_kiro_cloud_session import (  # noqa: F401 -- fixtures, used by name
    _choice_rows,
    _developer_flag_on,
    _fast_no_report_ceiling,
    _governed,
    _requests,
    crew_agent,
    handlers,
    section,
    stub,
)

from kiro_crew.acp.harness.kas_cloud import KasCloudSandboxHarness
from kiro_crew.acp.types import ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_new_cloud_session_replacing_a_recorded_one_surfaces_once(
    handlers, stub, crew_agent, tmp_path, monkeypatch, caplog  # noqa: F811
):
    """Edit & Resend, compaction and resets discard the resume id, so the next start
    reaches ``session/new`` with no reopen attempted. The chat still had a cloud
    session, and the new one does not hold its files: say so, as a failed reopen does."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-replaced"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert handlers._choices().record_cloud_session(key, "old-cloud", profile_id="cloud")
    _governed(monkeypatch, None)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    meta: dict[str, object] = {}
    rows = []
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **kw: rows.append((a, kw)))
    try:
        await provider._start_kiro_runtime_impl({}, meta)
        assert "resume_outcome" not in meta
        assert len(_requests(stub, "session/new")) == 1
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        assert "now runs on a new Kiro Cloud Sessions sandbox" in caplog.text
        chat_runner._surface_startup_notice(MagicMock(), MagicMock(), provider)
        chat_runner._surface_startup_notice(MagicMock(), MagicMock(), provider)
        assert len(rows) == 1
        assert rows[0][0][2] == "error"
        assert "earlier sandbox aren't in it" in rows[0][0][3]
        assert "wasn't deleted and is still listed in Kiro Web (app.kiro.dev)" in rows[0][0][3]
        assert rows[0][1]["meta"] == {"kind": "cloud_session_replaced"}
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
async def test_a_first_cloud_session_surfaces_nothing(
    handlers, stub, crew_agent, tmp_path, monkeypatch  # noqa: F811
):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-first"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    try:
        await provider._start_kiro_runtime_impl({}, {})
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        assert getattr(provider, "startup_notice", None) is None
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


def _cloud_provider(key: str, resume_sid: str | None):
    from kiro_crew.providers import acp

    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    if resume_sid:
        provider._client.set_resume_session_id(resume_sid)
    return provider


@pytest.mark.asyncio
async def test_reopening_the_recorded_cloud_session_surfaces_nothing(
    handlers, stub, crew_agent, tmp_path, monkeypatch  # noqa: F811
):
    """The same sandbox came back, with its files: no notice."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-reopened"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert handlers._choices().record_cloud_session(key, "old-cloud", profile_id="cloud")
    _governed(monkeypatch, None)
    provider = _cloud_provider(key, "old-cloud")
    meta: dict[str, object] = {}
    try:
        await provider._start_kiro_runtime_impl({}, meta)
        assert meta["resume_outcome"] == "loaded"
        assert _requests(stub, "session/new") == []
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "old-cloud"
        assert getattr(provider, "startup_notice", None) is None
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
async def test_a_reopen_that_replays_no_turn_starts_a_new_sandbox_and_says_so(
    handlers, stub, crew_agent, tmp_path, monkeypatch  # noqa: F811
):
    """KAS answers a load of a lost record with an empty one: ``require_replay``
    refuses it, so the start falls back to a new sandbox, and the notice says the
    earlier session was not deleted. The refused load deletes nothing in the cloud."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-empty-load"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert handlers._choices().record_cloud_session(key, "old-cloud", profile_id="cloud")
    _governed(monkeypatch, None)
    monkeypatch.setenv("KAS_STUB_EMPTY_LOAD", "1")
    provider = _cloud_provider(key, "old-cloud")
    meta: dict[str, object] = {}
    try:
        await provider._start_kiro_runtime_impl({}, meta)
        assert meta["resume_outcome"] == "fallback_replay"
        assert len(_requests(stub, "session/load")) == 1
        assert len(_requests(stub, "session/new")) == 1
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        kind, text = provider.startup_notice
        assert kind == "cloud_session_replayed"
        assert "wasn't deleted and is still listed in Kiro Web (app.kiro.dev)" in text
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


async def _edit_and_resend(manager, key):
    # chat_rewind.py: Edit & Resend drops the native conversation, keeping the slot.
    return await manager.discard_conversation(key, skip_if_busy=True)


async def _session_reset(manager, key):
    # chat_turn/session_reset.py: a reset that starts the next turn without replay.
    return await manager.discard_conversation(key, replay=False, skip_if_busy=True)


async def _compaction_reset(manager, key):
    # session_compaction.py: still critical after compaction, so the session resets.
    return await manager.reset(key, skip_if_busy=True, clear_conversation=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("drop", [_edit_and_resend, _session_reset, _compaction_reset])
async def test_each_path_that_drops_the_resume_id_says_the_chat_moved_sandbox(
    handlers, stub, crew_agent, tmp_path, monkeypatch, drop  # noqa: F811
):
    """Edit & Resend, a reset and compaction each clear the session's resume id, so
    the next start opens a new sandbox; that start must say so, once."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager, _Session
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-dropped"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    first = _cloud_provider(key, None)
    manager = SessionManager(KiroCrewConfig())
    second = None
    try:
        await first._start_kiro_runtime_impl({}, {})
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        manager._sessions[key] = _Session(provider=first)
        manager._session_map.set(key, "cloud-session", provider=ACP_BACKEND_KAS)
        assert manager._session_map.get(key) == "cloud-session"
        assert await drop(manager, key)
        # ``get`` is what a start resumes from (``mapped_sid`` keeps the dropped id
        # for history, so it is not the probe here).
        assert not manager._session_map.get(key)
        # The next start has no id to reopen, so Kiro's cloud opens a new sandbox.
        monkeypatch.setenv("KAS_STUB_NEW_ID", "cloud-session-2")
        second = _cloud_provider(key, manager._session_map.get(key) or None)
        await second._start_kiro_runtime_impl({}, {})
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session-2"
        kind, text = second.startup_notice
        assert kind == "cloud_session_replaced"
        assert text.startswith("This chat now runs on a new Kiro Cloud Sessions sandbox.")
        assert "wasn't deleted and is still listed in Kiro Web (app.kiro.dev)" in text
        # Neither the drop nor the new start deleted the earlier sandbox.
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        manager._sessions.clear()
        await first.shutdown()
        if second is not None:
            await second.shutdown()
        forget_remote_workspace(key)
