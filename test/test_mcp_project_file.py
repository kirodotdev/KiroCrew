"""``POST /api/mcp/project-file``'s write: one server added to a chat folder's MCP file.

The card that sends it is ``test_mcp_server_request_card_project.py``; here is the
file write itself: what it keeps, what it refuses, and the links it never follows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers import mcp_project_file as mpf
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE
from kiro_crew.testing.links import make_dir_link

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="a file symlink needs a privilege")
ENTRY = {"command": "npx", "args": ["-y", "@acme/weather-mcp"]}


def test_an_absent_kiro_file_is_created_with_its_settings_folder(tmp_path):
    assert mpf.write_project_server(str(tmp_path), KIRO_PROJECT_MCP_FILE, "weather", ENTRY) == (
        mpf.WRITTEN
    )
    data = json.loads((tmp_path / KIRO_PROJECT_MCP_FILE).read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"weather": ENTRY}}


def test_a_kiro_file_gets_the_oauth_hints_in_kiro_clis_spelling(tmp_path):
    remote = {"url": "https://mcp.example.com/mcp", "scopes": ["read"], "clientId": "crew-app"}
    assert mpf.write_project_server(str(tmp_path), KIRO_PROJECT_MCP_FILE, "docs", remote) == (
        mpf.WRITTEN
    )
    data = json.loads((tmp_path / KIRO_PROJECT_MCP_FILE).read_text(encoding="utf-8"))
    assert data["mcpServers"]["docs"] == {
        "url": "https://mcp.example.com/mcp",
        "oauthScopes": ["read"],
        "oauth": {"clientId": "crew-app"},
    }


def test_a_file_saved_with_a_byte_order_mark_takes_the_add(tmp_path):
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"git": {}}}).encode("utf-8"))
    assert mpf.read_project_servers(str(path)) == {
        "exists": True,
        "corrupt": False,
        "servers": ["git"],
    }
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.WRITTEN
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"git": {}, "w": ENTRY}}


def test_an_add_keeps_every_other_key_and_server(tmp_path):
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    path.write_text(json.dumps({"mcpServers": {"git": {"command": "git-mcp"}}, "x": 1}), "utf-8")
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "weather", ENTRY) == (
        mpf.WRITTEN
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"git": {"command": "git-mcp"}, "weather": ENTRY}, "x": 1}


@pytest.mark.parametrize(
    "text,outcome",
    [
        (json.dumps({"mcpServers": {"weather": {"command": "old"}}}), mpf.ALREADY_EXISTS),
        ("{not json", mpf.NOT_EDITABLE),
        ("[]", mpf.NOT_EDITABLE),
        (json.dumps({"mcpServers": []}), mpf.NOT_EDITABLE),
    ],
)
def test_a_file_the_add_would_have_to_rewrite_is_left_as_it_is(tmp_path, text, outcome):
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    path.write_text(text, encoding="utf-8")
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "weather", ENTRY) == (
        outcome
    )
    assert path.read_text(encoding="utf-8") == text


def _plant_link(tmp_path: Path, linked: str) -> tuple[Path, Path, str]:
    """A project whose *linked* name points elsewhere: a file symlink, or a folder link.

    The folder link is a junction on Windows (``make_dir_link``), so that case runs
    on every platform; a file symlink needs a privilege Windows runners lack.
    """
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if linked == ".mcp.json":
        (project / linked).symlink_to(elsewhere / "config.json")
        return project, elsewhere, CLAUDE_PROJECT_MCP_FILE
    make_dir_link(project / linked, elsewhere)
    return project, elsewhere, KIRO_PROJECT_MCP_FILE


_LINKED = [pytest.param(".mcp.json", marks=_POSIX_ONLY), ".kiro"]


@pytest.mark.parametrize("linked", _LINKED)
def test_a_file_reached_through_a_link_is_never_written(tmp_path, linked):
    project, elsewhere, file = _plant_link(tmp_path, linked)
    assert mpf.write_project_server(str(project), file, "weather", ENTRY) == mpf.NOT_EDITABLE
    assert list(elsewhere.iterdir()) == []
    assert mpf.read_project_servers(str(project / file))["corrupt"] is True


@pytest.mark.parametrize("linked", _LINKED)
def test_a_link_planted_after_the_name_check_is_still_refused(tmp_path, monkeypatch, linked):
    # The name check saw no link; the shell plants one before the write opens the folders.
    project, elsewhere, file = _plant_link(tmp_path, linked)
    monkeypatch.setattr(mpf, "_crosses_a_link", lambda folder, file: False)
    assert mpf.write_project_server(str(project), file, "weather", ENTRY) == mpf.NOT_EDITABLE
    assert list(elsewhere.iterdir()) == []


def test_a_file_past_the_size_cap_is_neither_read_nor_rewritten(tmp_path):
    # Valid JSON, so only the cap refuses it.
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    text = json.dumps({"mcpServers": {}, "pad": "x" * mpf._MAX_FILE_BYTES})
    path.write_text(text, encoding="utf-8")
    assert mpf.read_project_servers(str(path))["corrupt"] is True
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.NOT_EDITABLE
    )
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.timeout(30)
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_swapped_in_after_the_read_cannot_stall_the_write(tmp_path, monkeypatch):
    # The mode and ACLs come from the descriptor the file was read through, so a
    # writerless FIFO planted at the name afterwards is never opened.
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    read = mpf._read_open

    def read_then_swap(fd, name):
        text = read(fd, name)
        path.unlink()
        os.mkfifo(path)
        return text

    monkeypatch.setattr(mpf, "_read_open", read_then_swap)
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.WRITTEN
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"w": ENTRY}}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_is_refused_without_blocking(tmp_path):
    os.mkfifo(tmp_path / CLAUDE_PROJECT_MCP_FILE)
    assert mpf.read_project_servers(str(tmp_path / CLAUDE_PROJECT_MCP_FILE))["corrupt"] is True
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.NOT_EDITABLE
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_replaced_file_keeps_its_mode_bits(tmp_path):
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    path.chmod(0o600)
    assert mpf.write_project_server(str(tmp_path), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.WRITTEN
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_junction_below_the_folder_is_never_written(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    (tmp_path / ".kiro").mkdir()
    kiro = str(tmp_path / ".kiro")
    monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda p: str(p) == kiro)
    assert mpf.write_project_server(str(tmp_path), KIRO_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.NOT_EDITABLE
    )
    assert not (tmp_path / KIRO_PROJECT_MCP_FILE).exists()


def test_a_missing_folder_is_not_created(tmp_path):
    gone = tmp_path / "gone"
    assert mpf.write_project_server(str(gone), CLAUDE_PROJECT_MCP_FILE, "w", ENTRY) == (
        mpf.FOLDER_NOT_FOUND
    )
    assert not gone.exists()


def test_the_reader_names_the_servers_a_file_holds(tmp_path):
    path = tmp_path / CLAUDE_PROJECT_MCP_FILE
    assert mpf.read_project_servers(str(path)) == {"exists": False, "corrupt": False, "servers": []}
    path.write_text(json.dumps({"mcpServers": {"b": {}, "a": {}}}), encoding="utf-8")
    assert mpf.read_project_servers(str(path))["servers"] == ["a", "b"]


# ── who may call the route ──


def _caller(claims: dict):
    """Stands in for the auth middleware: publishes *claims* the way it would."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    return middleware


#: The callers the route refuses, each as the auth middleware publishes it.
_REFUSED = {
    # An app's scoped token: a non-empty app claim.
    "app_token": ({"app": "weather-app", "user": "local-app"}, {}),
    # An agent or MCP tool on the loopback internal secret: no app claim at all.
    "agent": ({"internal_auth": True, "user": "internal"}, {"X-Session-Key": "dashboard:chat-1"}),
    # A Bot's own session on the same secret.
    "member": (
        {"internal_auth": True, "user": "internal"},
        {"X-Session-Key": "member-dm:tide"},
    ),
    # Another signed-in subject that is not the owner.
    "other_user": ({"app": "", "user": "someone-else"}, {}),
}


async def _post(tmp_path, monkeypatch, claims: dict, headers: dict) -> tuple[int, dict, list]:
    audit = MagicMock()
    monkeypatch.setattr(mpf, "sel", lambda: audit)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    state = _make_state(tmp_path)
    state.owner_id = ""
    app = web.Application(middlewares=[_caller(claims)])
    app["state"] = state
    app.router.add_post("/api/mcp/project-file", mpf.api_mcp_project_file)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    body = {"folder": str(project), "file": ".mcp.json", "name": "weather", "spec": ENTRY}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/mcp/project-file", json=body, headers=headers)
        out = await resp.json()
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    return resp.status, out, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("who", sorted(_REFUSED))
async def test_every_caller_but_the_owner_is_refused_and_audited(tmp_path, monkeypatch, who):
    claims, headers = _REFUSED[who]
    status, out, rows = await _post(tmp_path, monkeypatch, claims, headers)
    assert status == 403 and out["code"] == "owner_only", out
    assert not (tmp_path / "project" / ".mcp.json").exists()
    assert [(r["operation"], r["outcome"]) for r in rows] == [("mcp_project_file", "denied")]


@pytest.mark.asyncio
async def test_the_owner_is_refused_outside_a_card_so_no_caller_picks_the_folder(
    tmp_path, monkeypatch
):
    status, out, rows = await _post(tmp_path, monkeypatch, {"app": "", "user": "local-app"}, {})
    assert status == 403 and out["code"] == "card_required", out
    assert not (tmp_path / "project" / ".mcp.json").exists()
    assert [(r["outcome"], r["resources"]) for r in rows] == [("denied", "not_a_card_step")]
