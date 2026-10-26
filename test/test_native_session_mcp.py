"""Late native runtime mounts use the authenticated broker instead of the SEL key."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import agent
from kiro_crew.config import KiroCrewConfig
from kiro_crew.mcp_gateway import claim, native_scope, native_session
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token
from kiro_crew.mcp_gateway.stub import _parse_args, _resolve_target_args


@pytest.fixture
def managed(monkeypatch, tmp_path):
    entry = {
        "command": str(tmp_path / "trusted-python"),
        "args": ["-I", "-m", "kiro_crew"],
        "env": {"KIROCREW_HOME": str(tmp_path / "crew")},
    }
    monkeypatch.setattr(agent, "managed_mcp_spec_entry", lambda name, **kwargs: entry)
    return entry


@pytest.mark.parametrize("server", ["kirocrew-dashboard", "kirocrew-panel", "kirocrew-work"])
def test_late_managed_launch_becomes_broker_stub(managed, tmp_path, server):
    direct = {"name": server, **managed, "env": [{"name": "PYTHONPATH", "value": "untrusted"}]}
    [entry] = native_session.broker_entries(
        [direct], agent_name="assigned-agent", work_dir=str(tmp_path), socket_path=tmp_path / "sock"
    )
    offset = entry["args"].index("kiro_crew.mcp_gateway.stub") + 1
    parsed = _parse_args(entry["args"][offset:])
    assert parsed.server == server
    assert parsed.agent == "assigned-agent"
    assert parsed.target_command == managed["command"]
    assert _resolve_target_args(parsed) == managed["args"]
    assert "PYTHONPATH" not in parsed.env_json
    assert entry["env"] == []
    assert not parsed.poolable


def test_reserved_name_does_not_authorize_another_command(managed, tmp_path):
    with pytest.raises(PermissionError, match="host declaration"):
        native_session.broker_entries(
            [{"name": "kirocrew-work", "command": "untrusted", "args": []}],
            agent_name="assigned-agent",
            work_dir=str(tmp_path),
            socket_path=tmp_path / "sock",
        )


def test_other_servers_are_preserved(tmp_path):
    direct = {"name": "external", "command": "external-server", "args": []}
    assert native_session.broker_entries(
        [direct], agent_name="assigned-agent", work_dir=str(tmp_path), socket_path=tmp_path / "sock"
    ) == [direct]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_token", ["", "host-token"])
async def test_final_scope_contains_late_mount_and_session_token(
    managed, monkeypatch, tmp_path, existing_token
):
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            mcp_gateway=SimpleNamespace(
                socket_path=str(tmp_path / "sock"),
                stub_servers=["kirocrew-dashboard"],
            )
        ),
    )
    steps = []

    async def own(entries, session_key):
        assert session_key == "dashboard:member"
        steps.append("identity")
        return attach_stub_session_token(entries, "host-token"), "host-token"

    async def bind(socket, pid, session_key, *, stub_session_token):
        assert (socket, pid, session_key, stub_session_token) == (
            str(tmp_path / "sock"),
            123,
            "dashboard:member",
            "host-token",
        )
        steps.append("claim")
        # An accepted claim before the stubs exist is a normal claim-noop.
        return False

    def seal(entries, scratch):
        assert scratch == tmp_path
        assert entries[0]["name"] == "kirocrew-dashboard"
        assert "kiro_crew.mcp_gateway.stub" in entries[0]["args"]
        env = {p["name"]: p["value"] for p in entries[0]["env"]}
        assert env[STUB_SESSION_TOKEN_ENV] == "host-token"
        steps.append("scope")
        return entries

    monkeypatch.setattr(native_scope, "attach", seal)
    monkeypatch.setattr(claim, "send_claim", AsyncMock(side_effect=bind))
    runtime = SimpleNamespace(
        _scratch_dir=tmp_path, pid=123, _own_stub_session=AsyncMock(side_effect=own)
    )
    entries, token = await native_session.finalize(
        runtime,
        [{"name": "kirocrew-dashboard", **managed}],
        agent_name="assigned-agent",
        work_dir=str(tmp_path),
        session_key="dashboard:member",
        token=existing_token,
    )
    assert token == "host-token"
    assert steps == (["identity"] if not existing_token else []) + ["claim", "scope"]
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_disabled_broker_route_is_not_overridden(managed, monkeypatch, tmp_path):
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(mcp_gateway=SimpleNamespace(socket_path="", stub_servers=[])),
    )
    runtime = SimpleNamespace(_scratch_dir=tmp_path, _own_stub_session=AsyncMock())
    with pytest.raises(PermissionError, match="broker routing"):
        await native_session.finalize(
            runtime,
            [{"name": "kirocrew-work", **managed}],
            agent_name="assigned-agent",
            work_dir=str(tmp_path),
            session_key="dashboard:member",
            token="host-token",
        )
    runtime._own_stub_session.assert_not_called()
