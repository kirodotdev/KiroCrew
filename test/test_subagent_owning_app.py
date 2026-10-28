"""A subagent's session belongs to the app its spawn was vetted for.

The child provider's ``owning_app`` is what its own governance reads: a remote
workspace's git proxy judges egress with it, so an app-owned chat's subagent
must not run as if no app owned it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentInfo, SubagentManager


@pytest.mark.asyncio
@pytest.mark.parametrize(("app", "expected"), [("mailer", "mailer"), ("", None)])
async def test_the_subagent_session_carries_its_owning_app(app, expected) -> None:
    sessions = _mock_sessions()
    sessions.get_approval_policy = MagicMock(return_value="")
    manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="app01",
        task="clone the repo",
        parent_session_key="dashboard:app-chat",
        app=app,
    )
    manager._log_spawned(info)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._run_inner(info, "subagent:app01")
    sessions.get_or_create.assert_awaited_once()
    kwargs = sessions.get_or_create.call_args.kwargs
    assert kwargs.get("owning_app") == expected
    # Not ``app_name``: that would also give the child the app's model pin,
    # which the spawn path neither resolves nor reports.
    assert "app_name" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["boardroom", ""])
async def test_session_allocation_hands_the_slot_app_to_the_provider(monkeypatch, app_name):
    """``owning_app`` is the caller's ``app_name`` (the slot's own app), not a factory
    kwarg: it is what the remote git proxy's egress check governs an app-owned
    session with."""
    from test_session import _mock_provider_factory

    from kiro_crew import session
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(session, "_is_acp_provider", lambda _provider: True)
    made = []
    inner = _mock_provider_factory()

    def factory(*args, **kwargs):
        assert "app_name" not in kwargs
        provider = inner(*args, **kwargs)
        made.append(provider)
        return provider

    mgr = session.SessionManager(KiroCrewConfig(), provider_factory=factory)
    try:
        kwargs = {"app_name": app_name} if app_name else {}
        await mgr.get_or_create("dashboard:app-owned-alloc", **kwargs)
        mgr.release("dashboard:app-owned-alloc")
        assert made and made[0].owning_app == app_name
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_an_owning_app_alone_governs_the_session_without_its_model(monkeypatch):
    """A subagent's ``owning_app`` sets the provider's governance app, but the
    session's model is resolved as for no app, as the spawn path reports it."""
    from test_session import _mock_provider_factory

    from kiro_crew import session
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(session, "_is_acp_provider", lambda _provider: True)
    seen_apps: list[str] = []
    real_model = session._session_model

    def session_model(*args, **kwargs):
        seen_apps.append(kwargs.get("app_name", ""))
        return real_model(*args, **kwargs)

    monkeypatch.setattr(session, "_session_model", session_model)
    made = []
    inner = _mock_provider_factory()

    def factory(*args, **kwargs):
        assert "owning_app" not in kwargs and "app_name" not in kwargs
        provider = inner(*args, **kwargs)
        made.append(provider)
        return provider

    mgr = session.SessionManager(KiroCrewConfig(), provider_factory=factory)
    try:
        await mgr.get_or_create("subagent:app-owned", owning_app="mailer")
        mgr.release("subagent:app-owned")
        assert made and made[0].owning_app == "mailer"
        assert seen_apps and all(app == "" for app in seen_apps)
    finally:
        await mgr.close_all()
