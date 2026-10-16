"""Gateway-held agent identity governs remote Git before credentials are read."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_remote_git_proxy import FrameLink, HttpBackendUpstream

from kiro_crew.coder.remote_worker import RemoteWorkerLaunch
from kiro_crew.platform import context, governance_profiles
from kiro_crew.platform.governance import parse_policy
from kiro_crew.providers import acp
from kiro_crew.remote_session import git_gateway
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture
def governance(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for name, bind, denied in (
        ("dashboard", "surface", []),
        ("researcher", "task", ["github.com"]),
    ):
        (profiles / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "bind": {"type": bind, "id": name},
                    "network": {"egress": {"mode": "deny", "deny": denied}},
                }
            )
        )
    monkeypatch.setattr(governance_profiles, "_PROFILES_DIR", profiles)
    monkeypatch.setattr(governance_profiles, "_STORE", governance_profiles.ProfileStore())
    ctx = SimpleNamespace(governance=None)
    monkeypatch.setattr(context, "current_context", lambda: ctx)
    audit = Mock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    return ctx, audit


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["researcher", "builder"])
@pytest.mark.parametrize("ceiling_denies", [False, True])
async def test_agent_profile_and_policy_both_bound_forwarding(
    tmp_path, monkeypatch, governance, agent, ceiling_denies
):
    ctx, audit = governance
    if ceiling_denies:
        ctx.governance = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "network": {"egress": {"mode": "deny", "deny": ["github.com"]}},
            }
        )
    link = FrameLink()
    credential = AsyncMock(return_value="Basic dTpw")
    upstream = HttpBackendUpstream(tmp_path, status=404)
    channel = git_gateway.GitProxyChannel(
        link, "dashboard:git", agent=agent, credential_source=credential, http_session=upstream
    )
    body = asyncio.Queue()
    body.put_nowait(None)
    await asyncio.wait_for(
        channel._handle(
            1,
            {
                "method": "GET",
                "path": "/github.com/a/r/info/refs?service=git-upload-pack",
                "agent": "builder",  # The guest cannot replace the launch identity.
            },
            body,
        ),
        30,
    )
    denied = agent == "researcher" or ceiling_denies
    assert link.frames[0]["status"] == (403 if denied else 404)
    assert credential.await_count == (0 if denied else 1)
    assert len(upstream.calls) == (0 if denied else 1)
    event = audit.log_governance_decision.call_args.kwargs
    assert event["outcome"] == ("denied" if denied else "allowed")
    assert event["scope"] == "network.egress" and event["item"] == "github.com"
    assert event["tool_name"] == "git_proxy" and event["session_key"] == "dashboard:git"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "  ", None])
async def test_unknown_agent_never_reaches_credentials_or_an_egress_override(agent):
    link, credential, egress = FrameLink(), AsyncMock(), Mock(return_value=True)
    channel = git_gateway.GitProxyChannel(
        link, "dashboard:git", agent=agent, credential_source=credential, egress_permitted=egress
    )
    await asyncio.wait_for(
        channel._handle(
            1,
            {"method": "GET", "path": "/github.com/a/r/info/refs?service=git-upload-pack"},
            asyncio.Queue(),
        ),
        30,
    )
    assert link.frames[0]["status"] == 403
    credential.assert_not_awaited()
    egress.assert_not_called()
    assert not git_gateway._egress_permitted("dashboard:git", "github.com", agent)


@pytest.mark.asyncio
async def test_governance_failure_is_closed_and_audited(governance, monkeypatch):
    _, audit = governance
    monkeypatch.setattr(
        governance_profiles, "resolve_active_scope", Mock(side_effect=OSError("unreadable"))
    )
    assert not await asyncio.to_thread(
        git_gateway._egress_permitted, "dashboard:git", "github.com", "researcher"
    )
    assert audit.log_governance_decision.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "  ", None])
async def test_proxy_start_without_agent_starts_no_worker(tmp_path, monkeypatch, agent):
    launch = RemoteWorkerLaunch("git-identity", Mock(), tmp_path)
    start = AsyncMock()
    monkeypatch.setattr(launch, "start_worker", start)
    await launch.start_git_proxy("dashboard:git", agent=agent)
    start.assert_not_awaited()
    assert launch.git_proxy is None


@pytest.mark.asyncio
async def test_provider_carries_agent_through_background_start_to_audited_channel(
    tmp_path, monkeypatch, governance
):
    class StopBeforeSpawn(Exception):
        pass

    class Link(FrameLink):
        def start(self):
            pass

        async def write(self, data):
            if data.strip() != launch.git_proxy_secret.encode():
                await super().write(data)

        def close_input(self):
            self.stdout.feed_eof()

        async def aclose(self):
            pass

    link = Link()
    launch = RemoteWorkerLaunch("git-identity", Mock(), tmp_path)
    monkeypatch.setattr(launch, "workspace_server", AsyncMock(return_value={}))
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=1))
    monkeypatch.setattr(launch, "stop_worker", AsyncMock())
    monkeypatch.setattr(launch, "dial", Mock(return_value=None))
    monkeypatch.setattr(git_gateway, "GitProxyLink", lambda *a, **k: link)
    credential = AsyncMock(return_value="Basic dTpw")
    monkeypatch.setattr(git_gateway.GitProxyChannel, "_credential_now", credential)
    remote = SimpleNamespace(
        launch=launch, workdir="/workspace/app", profile_name="Dev", created=False
    )
    from kiro_crew.providers import acp_runtime_start

    monkeypatch.setattr(
        acp_runtime_start, "_session_environments_configured", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "kiro_crew.coder.session_launch.prepare_for_session", AsyncMock(return_value=remote)
    )
    monkeypatch.setattr("kiro_crew.session_environment.remember_remote_workspace", Mock())
    monkeypatch.setattr("kiro_crew.session_environment.report_start_stage", Mock())
    runtime = Mock()
    runtime.spawn = AsyncMock(side_effect=StopBeforeSpawn)
    monkeypatch.setattr(acp, "AcpRuntime", Mock(return_value=runtime))
    monkeypatch.setattr(acp, "AcpClient", Mock())
    provider = acp.AcpProvider(acp_backend="kas")
    provider._client = SimpleNamespace(
        _work_dir=tmp_path,
        _agent="researcher",
        _resume_session_id="",
        _session_key="dashboard:git",
        backend="kas",
    )
    try:
        with pytest.raises(StopBeforeSpawn):
            await asyncio.wait_for(provider._start_kiro_runtime_impl({}, {}), 30)
        assert launch._git_proxy_start is not None
        await asyncio.wait_for(launch._git_proxy_start, 30)
        link.get(1, "/github.com/a/r/info/refs?service=git-upload-pack")
        await async_wait_until(lambda: bool(link.frames))
        assert link.frames[0]["status"] == 403
        credential.assert_not_awaited()
        assert governance[1].log_governance_decision.call_args.kwargs["outcome"] == "denied"
    finally:
        await asyncio.wait_for(launch.stop_git_proxy(), 30)
