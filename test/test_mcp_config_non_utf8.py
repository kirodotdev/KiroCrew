"""An MCP config file that is not UTF-8 reads like a malformed one.

A hand-edited ``mcp.json`` or agent config saved in a legacy code page fails
``read_text(encoding="utf-8")`` / ``.decode("utf-8")`` with UnicodeDecodeError.
Each reader below already has a "malformed" answer for a file it cannot parse;
a file it cannot decode gets the same answer instead of raising out of the
route (a bare 500):

- the MCP table's Kiro-global read lists the file as empty, as for bad JSON;
- the Add Custom store read refuses the write, as for bad JSON;
- the agent-config sync logs and skips, as for bad JSON, writing nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

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
