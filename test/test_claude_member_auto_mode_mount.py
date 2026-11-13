"""A claude Bot DM in ``auto`` mode keeps its session-control server.

Seam: the real ``session/new`` params an ``AcpClient`` on the claude backend sends,
built from a real agent spec and a real ``.claude/settings.local.json`` in the work
dir. When that file is not Crew's (the project's own, or another open session's seed)
the session leaves it out and pins ``default``; a requested ``auto`` must not cost the
Bot its ``kirocrew-dashboard`` mount there. A plain claude chat in the same state keeps
Crew's servers and never gets the dashboard server. Fixtures are in
``acp_session_mcp_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_spec

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.members import MEMBER_DISPATCH_SERVER


async def _session_new_params(client, tmp_path, monkeypatch):
    """Drive the real ``session/new`` call site and return the params it sent."""
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return 1

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    await client._new_session_following_substitution()
    assert len(sent) == 1
    return sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_file", [True, False])
@pytest.mark.parametrize(
    ("session_key", "dashboard"),
    [("dashboard:member-probe", True), ("dashboard_chat-1", False)],
)
async def test_a_claude_bot_in_auto_mode_keeps_the_dashboard_server(
    tmp_path, agents_dir, monkeypatch, foreign_file, session_key, dashboard
):
    """The Bot DM gets ``kirocrew-dashboard`` with or without a foreign file.

    A plain claude chat (``dashboard_chat-1``) gets Crew's own servers in both cases
    and no dashboard server, exactly as before.
    """
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    if foreign_file:
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew",
        acp_backend=ACP_BACKEND_CLAUDE,
        permission_mode="auto",
        session_key=session_key,
    )
    client._write_claude_local_settings()

    assert client._permission_surface_governed is True
    assert client._claude_local_settings_excluded is foreign_file
    params = await _session_new_params(client, tmp_path, monkeypatch)
    names = _by_name(params["mcpServers"])
    assert "foo" in names
    assert (MEMBER_DISPATCH_SERVER in names) is dashboard
    assert client.member_dispatch_mounted is dashboard


@pytest.mark.parametrize(
    ("mode", "foreign_file", "shown"),
    [
        ("auto", True, True),
        ("default", True, False),
        (None, True, False),
        ("auto", False, False),
    ],
)
def test_the_chat_says_once_why_auto_runs_as_default(
    tmp_path, agents_dir, monkeypatch, mode, foreign_file, shown
):
    """The new session's notice row names the reason and both ways back.

    Only a requested ``auto`` beside a foreign file runs as ``default``; a session
    that asked for ``default`` or nothing, or one whose folder has no foreign file,
    shows no row. Read through the provider property the first-turn hook reads.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.providers.acp import AcpProvider

    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    if foreign_file:
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew",
        acp_backend=ACP_BACKEND_CLAUDE,
        permission_mode=mode,
        session_key="dashboard:member-probe",
    )
    client._write_claude_local_settings()
    notice = AcpProvider.mode_pinned_notice.fget(SimpleNamespace(_client=client))

    rows: list[tuple] = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(mode_pinned_notice=notice)
    )

    if not shown:
        assert notice == ""
        assert rows == []
        return
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    text = rows[0][3]
    assert "Default mode" in text and "auto" in text
    assert ".claude/settings.local.json" in text
    assert "remove that file" in text and "Kiro CLI harness" in text
