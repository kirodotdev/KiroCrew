"""``config set mcp_gateway.stub_servers`` names each listed server with no approved launch.

The share list names servers, but what a shared server runs is approved by content
in MCP Management. A listed server whose launch nobody approved is refused at the
next gateway start, and without a notice the write's ``✅`` line reads as if sharing
were on. The notice goes to stderr after the write, which is unchanged; a server the
store approves, a reserved ``kirocrew-*`` server and a never-pooled one are not named.

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp_launch_approval_helpers import _APPROVED_CMD, _approval_module, _approvals_for

from kiro_crew.cli_config import _config_cmd

SHARE_LIST = "mcp_gateway.stub_servers"
_NOTICE = "has no approved launch"


def _run(home: Path, *, key: str | None, value: str | None, file: str | None, local: bool) -> None:
    args = argparse.Namespace(
        config_action="set", key=key, value=value, value_base64=None, file=file, local=local
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=home / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    mod = _approval_module()
    mod._write(
        mod.approvals_path(),
        mod._document(_approvals_for(mod, "approved-srv", _APPROVED_CMD, []), {}),
    )
    return d


def _names_in(err: str) -> list[str]:
    return [line.split()[1] for line in err.splitlines() if _NOTICE in line]


@pytest.mark.parametrize("local", [False, True])
def test_a_listed_server_with_no_approved_launch_is_named(home, capsys, local):
    value = json.dumps(["wide-tools", "approved-srv", "kirocrew-cron", "creds-agent"])
    _run(home, key=SHARE_LIST, value=value, file=None, local=local)
    captured = capsys.readouterr()

    target = home / ("config.local.json" if local else "config.json")
    assert json.loads(target.read_text(encoding="utf-8"))["mcp_gateway"]["stub_servers"] == [
        "wide-tools",
        "approved-srv",
        "kirocrew-cron",
        "creds-agent",
    ]
    assert "✅" in captured.out
    assert _names_in(captured.err) == ["wide-tools"]
    assert "Customize → Connections → MCP Servers" in captured.err


def test_a_config_file_with_a_share_list_names_the_same_servers(home, tmp_path, capsys):
    source = tmp_path / "import.json"
    source.write_text(
        json.dumps({"mcp_gateway": {"stub_servers": ["approved-srv", "npm:@acme/mcp"]}}),
        encoding="utf-8",
    )
    _run(home, key=None, value=None, file=str(source), local=False)

    assert _names_in(capsys.readouterr().err) == ["npm:@acme/mcp"]


def test_an_approved_share_list_and_another_key_say_nothing(home, capsys):
    _run(home, key=SHARE_LIST, value='["approved-srv"]', file=None, local=False)
    _run(home, key="agent.log_level", value="DEBUG", file=None, local=False)

    assert _NOTICE not in capsys.readouterr().err
