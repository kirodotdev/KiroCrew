"""A store toggle or tool override must not rewrite an mcp.json it could not read.

``<data home>/mcp.json`` holds every server Kiro Crew keeps for itself. Read as an
empty document, a file that does not parse or cannot be opened would be written
back holding only the entry being changed, and every other server would be gone
with no error. These writers read it strictly instead: a missing file is empty,
and anything else they cannot read is answered as ``"corrupt"`` (or changes no
tool) with its bytes left exactly as they were.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import attach_body
from dashboard_owner_helpers import owner_claims

from kiro_crew.dashboard.handlers import mcp as mcp_mod

#: Two servers, then a cut-off write: the closing braces never landed.
_TRUNCATED = b'{"mcpServers": {"github": {"command": "gh-mcp"}, "jira": {"command": "jira-mcp"}'
#: Valid JSON whose ``mcpServers`` is not an object.
_WRONG_SHAPE = b'{"mcpServers": ["github", "jira"]}'


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "crew" / "mcp.json"
    path.parent.mkdir()
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", path)
    monkeypatch.setattr(mcp_mod, "_find_server_spec_anywhere", lambda name: {"command": "seed"})
    return path


@pytest.mark.parametrize("raw", [_TRUNCATED, _WRONG_SHAPE], ids=["truncated", "wrong-shape"])
@pytest.mark.parametrize(
    ("enabled", "spec"),
    [(False, None), (False, {"command": "x"}), (True, {"command": "x"})],
    ids=["disable", "disable-with-spec", "enable-with-spec"],
)
def test_a_toggle_leaves_an_unparseable_store_as_it_was(store, raw, enabled, spec) -> None:
    store.write_bytes(raw)

    action = mcp_mod._set_kirocrew_entry("srv", enabled=enabled, spec=spec)

    assert action == "corrupt"
    assert store.read_bytes() == raw


@pytest.mark.parametrize("raw", [_TRUNCATED, _WRONG_SHAPE], ids=["truncated", "wrong-shape"])
def test_a_tool_override_leaves_an_unparseable_store_as_it_was(store, raw) -> None:
    store.write_bytes(raw)

    changed = mcp_mod._set_tool_overrides("github", {"delete_repo": False})

    assert changed == []
    assert store.read_bytes() == raw


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a POSIX file mode that the test user cannot read through",
)
def test_a_store_that_cannot_be_opened_is_not_rewritten(store) -> None:
    servers = {"github": {"command": "gh-mcp"}, "jira": {"command": "jira-mcp"}}
    store.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    store.chmod(0o000)
    try:
        assert mcp_mod._set_kirocrew_entry("srv", enabled=False) == "corrupt"
        assert mcp_mod._set_tool_overrides("github", {"delete_repo": False}) == []
    finally:
        store.chmod(0o600)
    assert json.loads(store.read_text(encoding="utf-8")) == {"mcpServers": servers}


def test_a_missing_store_is_still_created(store) -> None:
    assert mcp_mod._set_kirocrew_entry("srv", enabled=False, spec={"command": "x"}) == "disabled"
    assert mcp_mod._set_tool_overrides("srv", {"delete_repo": False}) == ["delete_repo"]
    entry = json.loads(store.read_text(encoding="utf-8"))["mcpServers"]["srv"]
    assert entry == {"command": "x", "disabled": True, "disabledTools": ["delete_repo"]}


def test_a_readable_store_keeps_its_other_servers(store) -> None:
    servers = {"github": {"command": "gh-mcp"}, "jira": {"command": "jira-mcp"}}
    store.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    assert mcp_mod._set_kirocrew_entry("github", enabled=False) == "disabled"
    assert mcp_mod._set_tool_overrides("jira", {"delete_issue": False}) == ["delete_issue"]

    written = json.loads(store.read_text(encoding="utf-8"))["mcpServers"]
    assert written == {
        "github": {"command": "gh-mcp", "disabled": True},
        "jira": {"command": "jira-mcp", "disabledTools": ["delete_issue"]},
    }


class _NoLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return None


def _apply_request(body: dict) -> MagicMock:
    """An owner's ``POST /api/mcp/apply`` carrying *body*, as test_mcp_apply builds it."""
    state = MagicMock()
    state._background_tasks = set()
    state.owner_id = ""
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    attach_body(request, body)
    return owner_claims(request)


@pytest.mark.asyncio
async def test_an_apply_is_refused_before_any_write_when_the_store_does_not_parse(
    store, tmp_path, monkeypatch
) -> None:
    kiro_path = tmp_path / "kiro_global.json"
    kiro_path.write_text(json.dumps({"mcpServers": {"srv": {"command": "c"}}}), encoding="utf-8")
    store.write_bytes(_TRUNCATED)
    before = {p: p.read_bytes() for p in (store, kiro_path)}
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_path)
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
    monkeypatch.setattr(mcp_mod, "kiro_agents_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared._capability_manager",
        lambda: MagicMock(**{"available.return_value": False}),
    )
    rebuild = MagicMock(return_value=(Path(), True))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp.rebuild_agent_config_reporting", rebuild)
    monkeypatch.setattr(mcp_mod, "sel", MagicMock)
    monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())

    # Moving the server from the Kiro global into the store: without the refusal up
    # front, the global write would land and the server would be in neither file.
    resp = await mcp_mod.api_mcp_apply(
        _apply_request({"changes": [{"name": "srv", "kirocrew": True, "kiroGlobal": False}]})
    )

    assert resp.status == 500
    assert json.loads(resp.body) == {
        "error": "cannot parse the Kiro Crew mcp.json",
        "code": "config_corrupt",
    }
    for path, raw in before.items():
        assert path.read_bytes() == raw, path
    rebuild.assert_not_called()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a POSIX file mode that the test user cannot read through",
)
@pytest.mark.asyncio
async def test_an_apply_names_a_store_it_cannot_open_as_unreadable(
    store, tmp_path, monkeypatch
) -> None:
    store.write_text(json.dumps({"mcpServers": {"github": {"command": "gh"}}}), encoding="utf-8")
    store.chmod(0o000)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro_global.json")
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
    monkeypatch.setattr(mcp_mod, "kiro_agents_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared._capability_manager",
        lambda: MagicMock(**{"available.return_value": False}),
    )
    monkeypatch.setattr(mcp_mod, "sel", MagicMock)
    monkeypatch.setattr(mcp_mod, "_get_mcp_lock", lambda: _NoLock())
    try:
        resp = await mcp_mod.api_mcp_apply(
            _apply_request({"changes": [{"name": "srv", "kirocrew": False}]})
        )
    finally:
        store.chmod(0o600)

    assert resp.status == 500
    assert json.loads(resp.body) == {
        "error": "cannot read the Kiro Crew mcp.json",
        "code": "config_unreadable",
    }
    assert json.loads(store.read_text(encoding="utf-8")) == {
        "mcpServers": {"github": {"command": "gh"}}
    }
