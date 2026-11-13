"""A running Kiro Cloud chat is judged by the governance ceiling on every turn.

The start's check is not enough: a policy refresh or a profile edit can withdraw
``capabilities.kiro_cloud_sessions``, or govern a new scope, while the chat runs,
and a cloud session's tools run in Kiro's cloud outside every gate Kiro Crew
enforces. So each turn on a cloud runtime re-runs the start's check; a refusal
stops the cloud runtime without deleting the sandbox and refuses the turn. Runs
against the KAS stub of ``test_kiro_cloud_session``.
"""

from __future__ import annotations

import pytest
from test_kiro_cloud_session import (  # noqa: F401 -- fixtures, used by name
    _DENY_NC,
    _ceiling,
    _developer_flag_on,
    _fast_no_report_ceiling,
    _governed,
    _requests,
    crew_agent,
    handlers,
    section,
    stub,
)

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.harness.kas_cloud import KasCloudSandboxHarness
from kiro_crew.acp.types import ACP_BACKEND_KAS


async def _turn(provider) -> None:
    async for _event in provider.stream("hello"):
        pass


async def _cloud_chat(handlers, key):  # noqa: F811
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp

    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    await provider._start_kiro_runtime_impl({}, {})
    return provider


@pytest.mark.asyncio
async def test_a_narrowed_ceiling_refuses_the_next_cloud_turn_and_deletes_nothing(
    handlers, stub, crew_agent, monkeypatch  # noqa: F811
):
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-narrowed"
    _governed(monkeypatch, None)
    provider = await _cloud_chat(handlers, key)
    try:
        runtime = provider._client._runtime
        assert runtime.is_cloud_sandbox is True
        # An administrator now governs tool execution, and the policy does not
        # opt in to cloud sessions: the running chat must not keep its sandbox.
        _governed(monkeypatch, _ceiling(**_DENY_NC))
        with pytest.raises(AcpError) as refused:
            await _turn(provider)
        assert refused.value.transient is False
        assert refused.value.user_worded is True
        assert "no longer allowed for this chat" in str(refused.value)
        assert "commands" in str(refused.value)
        # Refused before the prompt; the runtime is stopped, the sandbox kept.
        assert _requests(stub, "session/prompt") == []
        assert not runtime.is_alive()
        assert not provider.is_process_alive()
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
async def test_a_cloud_chat_the_ceiling_still_allows_keeps_working(
    handlers, stub, crew_agent, monkeypatch  # noqa: F811
):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-allowed"
    allowed = {**_DENY_NC, "capabilities": {"kiro_cloud_sessions": {"enabled": True}}}
    _governed(monkeypatch, _ceiling(**allowed))
    checked = []
    real = sl.kiro_cloud_governance_refusal

    def recording(*args, **kwargs):
        checked.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(sl, "kiro_cloud_governance_refusal", recording)
    provider = await _cloud_chat(handlers, key)
    try:
        start_checks = len(checked)
        await _turn(provider)
        await _turn(provider)
        assert len(_requests(stub, "session/prompt")) == 2
        assert provider.is_process_alive()
        # Each turn was judged as the start was: same session, agent and app.
        assert len(checked) == start_checks + 2
        assert {(args, tuple(sorted(kw.items()))) for args, kw in checked[start_checks:]} == {
            ((key,), (("agent", "kirocrew"), ("app", "")))
        }
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
async def test_a_local_turn_is_never_checked_for_kiro_cloud(monkeypatch, tmp_path):
    """A local KAS session: no scope is recorded, so the turn's admission makes
    no governance call at all, under a ceiling that would refuse a cloud chat."""
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp

    def no_check(*_a, **_k):
        raise AssertionError("a session that is not a cloud sandbox is not re-checked")

    monkeypatch.setattr(sl, "kiro_cloud_governance_refusal", no_check)
    _governed(monkeypatch, _ceiling(**_DENY_NC))
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS)
    assert runtime.is_cloud_sandbox is False
    provider = acp.AcpProvider(agent="kirocrew", acp_backend=ACP_BACKEND_KAS)
    provider._client = AcpSessionProvider(SimpleNamespace(session_id="local-1"), runtime)  # type: ignore[arg-type]
    assert getattr(provider, "_cloud_governance_scope", None) is None
    await provider._ready_confirmed_session()


@pytest.mark.asyncio
async def test_a_remote_workspace_runtime_is_never_checked_for_kiro_cloud(monkeypatch, tmp_path):
    """A Coder (or Sprites, boxd) session runs on a runtime that is not a cloud
    sandbox: even a stray scope on its provider must not refuse its turns."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp, acp_turns

    def no_check(*_a, **_k):
        raise AssertionError("a remote workspace runtime is not re-checked for Kiro Cloud")

    monkeypatch.setattr(sl, "kiro_cloud_governance_refusal", no_check)
    runtime = SimpleNamespace(is_cloud_sandbox=False)
    handle = SimpleNamespace(session_id="coder-1")
    provider = acp.AcpProvider(agent="kirocrew", acp_backend=ACP_BACKEND_KAS)
    provider._client = AcpSessionProvider(handle, runtime)  # type: ignore[arg-type]
    await acp_turns._readmit_cloud_session(provider, ("dashboard:coder", "kirocrew", ""))
