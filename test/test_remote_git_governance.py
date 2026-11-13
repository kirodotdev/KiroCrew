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
        ("mailer", "app", ["github.com"]),
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
    # The row names the launch agent whose profile decided, not the guest's claim.
    assert event["agent"] == agent


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
    launch = RemoteWorkerLaunch("git-identity", Mock())
    start = AsyncMock()
    monkeypatch.setattr(launch, "start_worker", start)
    await launch.start_git_proxy("dashboard:git", agent=agent)
    start.assert_not_awaited()
    assert launch.git_proxy is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "owning_app", "crew"),
    # Denied by the agent's own profile, then by the owning app's (whose agent
    # allows), then by a crew member's own profile when the template allows.
    [("researcher", "", ""), ("builder", "mailer", ""), ("kirocrew", "", "researcher")],
)
async def test_provider_carries_agent_through_background_start_to_audited_channel(
    tmp_path, monkeypatch, governance, agent, owning_app, crew
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
    launch = RemoteWorkerLaunch("git-identity", Mock())
    monkeypatch.setattr(launch, "workspace_server", AsyncMock(return_value={}))
    # What workspace_server leaves behind: the relay whose token the runtime writes.
    launch.relay = SimpleNamespace(write_token=Mock(), close=AsyncMock(), name="relay")
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=1))
    monkeypatch.setattr(launch, "stop_worker", AsyncMock())
    monkeypatch.setattr(launch, "dial", Mock(return_value=None))
    monkeypatch.setattr(git_gateway, "GitProxyLink", lambda *a, **k: link)
    credential = AsyncMock(return_value="Basic dTpw")
    monkeypatch.setattr(git_gateway.GitProxyChannel, "_credential_now", credential)
    remote = SimpleNamespace(
        launch=launch, workdir="/workspace/app", profile_name="Dev", created=False
    )
    from kiro_crew.config import live

    # The start gate prepares a remote workspace once Session Environments is set up,
    # which needs the developer flag.
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    section = SimpleNamespace(coder_url="https://coder.example", profiles=[])
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    prepare = AsyncMock(return_value=remote)
    monkeypatch.setattr("kiro_crew.coder.session_launch.prepare_for_session", prepare)
    monkeypatch.setattr("kiro_crew.session_environment.remember_remote_workspace", Mock())
    monkeypatch.setattr("kiro_crew.session_environment.report_start_stage", Mock())
    runtime = Mock()
    runtime.spawn = AsyncMock(side_effect=StopBeforeSpawn)
    monkeypatch.setattr(acp, "AcpRuntime", Mock(return_value=runtime))
    monkeypatch.setattr(acp, "AcpClient", Mock())
    provider = acp.AcpProvider(acp_backend="kas")
    provider.owning_app = owning_app
    # A crew member runs on a template: its own name is what profiles bind to.
    provider._crew_agent = crew
    provider._client = SimpleNamespace(
        _work_dir=tmp_path,
        _agent=agent,
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
        decision = governance[1].log_governance_decision.call_args.kwargs
        governed = crew or agent
        assert decision["outcome"] == "denied" and decision["agent"] == governed
        # The cloud and workspace start gate judges the same identity.
        assert prepare.await_args.kwargs["agent"] == governed
    finally:
        await asyncio.wait_for(launch.stop_git_proxy(), 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(("app", "denied"), [("mailer", True), ("", False)])
async def test_the_owning_apps_profile_binds_git_egress(tmp_path, governance, app, denied):
    """An app-owned chat's app profile decides first, as for its tool calls: the
    agent (no profile of its own) and the dashboard surface both allow github.com."""
    link = FrameLink()
    credential = AsyncMock(return_value="Basic dTpw")
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:git",
        agent="builder",
        app=app,
        credential_source=credential,
        http_session=HttpBackendUpstream(tmp_path, status=404),
    )
    body = asyncio.Queue()
    body.put_nowait(None)
    await asyncio.wait_for(
        channel._handle(
            1, {"method": "GET", "path": "/github.com/a/r/info/refs?service=git-upload-pack"}, body
        ),
        30,
    )
    assert link.frames[0]["status"] == (403 if denied else 404)
    assert credential.await_count == (0 if denied else 1)
