"""A project MCP file the settings reader refuses still has its servers held by name.

``project_mcp_approvals._file_servers`` reads such a file again for its server entries
(``project_mcp_holds.read_server_entries``). A link, or a file that is not JSON, keeps
the whole-file handling: a Claude Code session runs with ``strictMcpConfig`` and a
kiro-cli or KAS chat shows one notice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.acp.session_mcp_settings_read import SettingsReadRefusal
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

_ENTRY = {"command": "node", "args": ["planted.js"]}
_SERVERS = {"planted": _ENTRY, "muted": {"command": "m", "disabled": True}, "bad": "x"}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    first = tmp_path / "first"
    first.mkdir()
    approvals.pending_servers(first)  # the one-time record, before any file exists
    yield crew
    approvals._MIGRATION_SETTLED.set()


@pytest.fixture
def refused(monkeypatch) -> None:
    def _refuse(path):
        raise SettingsReadRefusal("MCP settings exceed the safe read limit", "too large")

    monkeypatch.setattr(holds, "read_settings_file", _refuse)


def _spec(monkeypatch, spec: dict[str, Any]) -> None:
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda agent, work_dir=None: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda directory, *_a: (spec, directory))


def _write(folder: Path, file: str, text: str) -> Path:
    path = folder / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_kiro_hosts_hold_a_refused_file_by_name(home, tmp_path, monkeypatch, refused, backend):
    folder = tmp_path / "repo"
    _write(folder, KIRO_PROJECT_MCP_FILE, json.dumps({"mcpServers": _SERVERS}))
    _spec(monkeypatch, {"name": "a", "includeMcpJson": True})
    hold = holds.project_hold(backend, "a", folder)
    assert hold.unreadable == ()
    [server] = hold.pending
    assert (server.name, dict(server.entry)) == ("planted", _ENTRY)
    assert server.fingerprint == approvals.fingerprint(_ENTRY)
    assert holds.unreadable_notice(backend, hold) == ""


def test_claude_holds_a_refused_file_by_name(home, tmp_path, refused):
    folder = tmp_path / "repo"
    _write(folder, CLAUDE_PROJECT_MCP_FILE, "\ufeff" + json.dumps({"mcpServers": _SERVERS}))
    hold = holds.project_hold(ACP_BACKEND_CLAUDE, "a", folder)
    assert hold.unreadable == ()
    assert sorted(s.name for s in hold.pending) == ["muted", "planted"]


@pytest.mark.asyncio
async def test_claude_rejects_the_held_names_of_a_refused_file(home, tmp_path, refused):
    from kiro_crew.acp.client import ClaudeAcpClient

    folder = tmp_path / "repo"
    _write(folder, CLAUDE_PROJECT_MCP_FILE, json.dumps({"mcpServers": {"planted": _ENTRY}}))
    client = ClaudeAcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    payload = json.loads(client._render_claude_settings_payload())
    assert "disabledMcpjsonServers" not in payload
    await client._prepare_claude_held_asks()
    options = client._claude_session_meta()["claudeCode"]["options"]
    assert options["settings"]["disabledMcpjsonServers"] == ["planted"]
    assert "strictMcpConfig" not in options


@requires_symlinks
@pytest.mark.parametrize("inside", [True, False])
def test_a_linked_file_is_not_read_for_names(home, tmp_path, refused, inside):
    folder = tmp_path / "repo"
    folder.mkdir()
    where = folder if inside else tmp_path / "elsewhere"
    target = _write(where, "servers.json", json.dumps({"mcpServers": _SERVERS}))
    (folder / CLAUDE_PROJECT_MCP_FILE).symlink_to(target)
    found = approvals.check_folder(folder)
    assert found.pending == []
    assert found.unreadable == (str(folder.resolve() / CLAUDE_PROJECT_MCP_FILE),)


def test_a_file_under_a_linked_folder_is_not_read_for_names(home, tmp_path, refused):
    folder = tmp_path / "repo"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, "settings/mcp.json", json.dumps({"mcpServers": _SERVERS}))
    make_dir_link(folder / ".kiro", elsewhere)
    found = approvals.check_folder(folder)
    assert found.pending == []
    assert found.unreadable == (str(folder.resolve() / KIRO_PROJECT_MCP_FILE),)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_a_file_that_is_not_json_keeps_the_notice(home, tmp_path, monkeypatch, refused, backend):
    folder = tmp_path / "repo"
    _write(folder, KIRO_PROJECT_MCP_FILE, '{"mcpServers": {"planted": ')
    _spec(monkeypatch, {"name": "a", "includeMcpJson": True})
    hold = holds.project_hold(backend, "a", folder)
    assert hold.pending == []
    assert hold.unreadable == (str(folder.resolve() / KIRO_PROJECT_MCP_FILE),)
    assert "Fix or delete that file" in holds.unreadable_notice(backend, hold)


def test_a_path_the_screen_still_refuses_is_not_read_again(home, tmp_path, monkeypatch):
    from kiro_crew import hooks
    from kiro_crew.acp.session_mcp_settings_read import _UNREADABLE_REFUSED_PATH

    monkeypatch.setattr(hooks, "validate_file_path", lambda raw: None)

    def _refuse(path):
        raise SettingsReadRefusal("MCP settings path was refused", _UNREADABLE_REFUSED_PATH)

    monkeypatch.setattr(holds, "read_settings_file", _refuse)
    folder = tmp_path / "repo"
    _write(folder, CLAUDE_PROJECT_MCP_FILE, json.dumps({"mcpServers": _SERVERS}))
    found = approvals.check_folder(folder)
    assert found.pending == []
    assert found.unreadable == (str(folder.resolve() / CLAUDE_PROJECT_MCP_FILE),)


def _handshake_client(tmp_path, monkeypatch, version: str, **hold: Any):
    """A Claude client whose handshake reported *version*, recording sends and kills."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    for name, value in hold.items():
        setattr(client, name, value)
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(_rid, **_kw):
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    client._agent_version_read = True
    client._agent_version = version
    return client, sent, killed


_HOLDS = [
    {"_claude_held_project_servers": ("planted",)},
    {"_claude_project_mcp_unreadable": True},
]
_RESP = {"sessionId": "s-1", "modes": {"currentModeId": "default"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", _HOLDS, ids=["held", "unreadable"])
@pytest.mark.parametrize("version", ["0.83.9", ""])
async def test_a_hold_alone_is_stopped_on_an_unverified_adapter(
    tmp_path, monkeypatch, hold, version
):
    """The hold rides the session options, which such an adapter may ignore."""
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    client, sent, killed = _handshake_client(tmp_path, monkeypatch, version, **hold)
    # A held server can be allowed, so that refusal names where.
    wording = (
        "Allow them in a dashboard Claude Code chat"
        if "_claude_held_project_servers" in hold
        else "verified"
    )
    with pytest.raises(AcpToolGateUnroutable, match=wording):
        await client._pin_claude_starting_mode(_RESP)
    assert killed == [True]
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", _HOLDS, ids=["held", "unreadable"])
async def test_a_hold_alone_starts_on_a_verified_adapter(tmp_path, monkeypatch, hold):
    client, sent, killed = _handshake_client(tmp_path, monkeypatch, "0.93.0", **hold)
    await client._pin_claude_starting_mode({**_RESP, "modes": {"currentModeId": "auto"}})
    assert sent == []
    assert killed == []
