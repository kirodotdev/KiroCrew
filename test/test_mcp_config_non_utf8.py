"""An MCP config file that is not UTF-8 reads like a malformed one.

A hand-edited ``mcp.json`` or agent config saved in a legacy code page fails
``read_text(encoding="utf-8")`` / ``.decode("utf-8")`` with UnicodeDecodeError.
Each reader below already has a "malformed" answer for a file it cannot parse;
a file it cannot decode gets the same answer instead of raising out of the
route (a bare 500):

- the MCP table's Kiro-global read lists the file as empty, as for bad JSON;
- the Add Custom store read refuses the write, as for bad JSON;
- the agent-config sync logs and skips, as for bad JSON, writing nothing;
- the MCP mutation routes answer ``500 config_corrupt`` naming the file, and
  leave both files as they were.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _request, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod

# "café" in latin-1: the 0xE9 byte is not valid UTF-8.
_LATIN1 = '{"mcpServers": {"caf\xe9": {"command": "x"}}}'.encode("latin-1")


def _no_raise(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except UnicodeDecodeError as exc:  # a raise here is a 500 at the route
        pytest.fail(f"the read raised instead of treating the file as malformed: {exc!r}")


def test_global_mcp_json_that_is_not_utf8_lists_as_empty(tmp_path: Path) -> None:
    from kiro_crew.dashboard.handlers import mcp_probe

    path = tmp_path / "mcp.json"
    path.write_bytes(_LATIN1)
    # Through the facade: it mirrors the path onto every owner that binds it.
    with patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", path):
        assert _no_raise(mcp_probe._read_global_mcps) == {}


def test_custom_store_that_is_not_utf8_refuses_the_write(tmp_path: Path) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.dashboard.handlers.mcp_custom import _load_kirocrew_config_strict

    path = tmp_path / "mcp.json"
    path.write_bytes(_LATIN1)
    with patch.object(mcp_mod, "_kirocrew_mcp_json", return_value=path):
        assert _no_raise(_load_kirocrew_config_strict) is None
    assert path.read_bytes() == _LATIN1


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_agent_config_that_is_not_utf8_skips_the_sync(tmp_path: Path, batch: bool) -> None:
    from kiro_crew.dashboard.handlers import mcp_agent_sync

    agent_cfg = tmp_path / "kirocrew.json"
    agent_cfg.write_bytes(_LATIN1)
    global_json = tmp_path / "mcp.json"
    global_json.write_text(json.dumps({"mcpServers": {"slack-mcp": {"command": "slack-mcp"}}}))
    with (
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config",
            return_value=agent_cfg,
        ),
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", global_json),
    ):
        if batch:
            _no_raise(mcp_agent_sync._sync_mcp_to_agent_batch_unlocked, ["slack-mcp"], True)
        else:
            _no_raise(mcp_agent_sync._sync_mcp_to_agent_unlocked, "slack-mcp", True)
    assert agent_cfg.read_bytes() == _LATIN1


# The two ordinary ways a hand-edited file stops being UTF-8 on Windows: a
# PowerShell 5.1 ``>`` save (UTF-16LE with a BOM) and a Notepad "ANSI" save
# (cp1252, one raw 0xE9 byte).
_NOT_UTF8 = {
    "utf16le-bom": b"\xff\xfe" + '{"mcpServers": {"srv": {"command": "x"}}}'.encode("utf-16-le"),
    "cp1252": '{"mcpServers": {"srv": {"command": "caf\xe9"}}}'.encode("cp1252"),
}

_MUTATIONS = {
    "toggle": lambda: mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False})),
    "toggle-tool": lambda: mcp_mod.api_mcp_toggle_tool(
        _request({"server": "srv", "tool": "t1", "enabled": False})
    ),
    "toggle-all": lambda: mcp_mod.api_mcp_toggle_all(_request({"enabled": False})),
    "remove": lambda: mcp_mod.api_mcp_remove(_request({"name": "srv"})),
    "delete": lambda: mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": "srv"}, method="DELETE")
    ),
    "apply": lambda: mcp_mod.api_mcp_apply(
        _request({"changes": [{"name": "srv", "kiroGlobal": False}]})
    ),
}


# The refusal names the file that does not decode.
_REFUSAL = {
    "global": {"error": "cannot parse global mcp.json", "code": "config_corrupt"},
    "store": {"error": "cannot parse the Kiro Crew mcp.json", "code": "config_corrupt"},
}


def _bytes_or_none(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, which",
    [(route, "global") for route in sorted(set(_MUTATIONS) - {"apply"})]
    + [("remove", "store"), ("apply", "store")],
)
@pytest.mark.parametrize("encoding", sorted(_NOT_UTF8))
async def test_a_mutation_route_refuses_an_mcp_json_that_is_not_utf8(
    sandbox, monkeypatch: pytest.MonkeyPatch, route: str, which: str, encoding: str
) -> None:
    # Coded like a file that does not parse, never an uncoded 500, and neither
    # file is rewritten.
    from kiro_crew.dashboard.handlers import _shared

    files = (sandbox.global_json, mcp_mod._kirocrew_mcp_json())
    target = files[0] if which == "global" else files[1]
    _write_global(sandbox, {"srv": {"command": "x"}})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_NOT_UTF8[encoding])
    before = [_bytes_or_none(f) for f in files]
    no_manager = MagicMock()
    no_manager.available.return_value = False
    monkeypatch.setattr(_shared, "_capability_manager", lambda: no_manager)

    try:
        resp = await _MUTATIONS[route]()
    except UnicodeDecodeError as exc:  # a raise here is an uncoded 500 at the route
        pytest.fail(f"the route raised instead of refusing the file: {exc!r}")

    assert resp.status == 500
    assert json.loads(resp.body) == _REFUSAL[which]
    assert [_bytes_or_none(f) for f in files] == before
    assert sandbox.synced == [] and sandbox.batched == []
