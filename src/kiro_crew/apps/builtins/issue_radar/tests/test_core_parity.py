"""Issue Radar reads what it shares with the core from the core's one home.

Each pin here once sat in a core test file beside that file's core cases; the core
sites are still pinned there. The app reads the core; the core never reads the app.

- The ``gh`` URL parser and its error class are the shared runner's own objects, so
  a repository URL the dashboard sidebar refuses is refused here too
  (``test/test_github_runner.py``).
- The ``kirocrew-core`` MCP server gates this app's three tools on the
  ``installed.json`` of the app id it spells as a literal; that literal must be the
  id the app's routes read (``test/test_mcp_apps_tool_gate.py``).
- With the crew log off, the crew store's refusal names the env file through
  ``env_file_display()``, as every other crew-log-off refusal does
  (``test/test_crew_log_routes_dispatch_fence.py``).
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from kiro_crew import github_runner as runner
from kiro_crew.apps.builtins.issue_radar.backend import crew_store, errors, github_client, store
from kiro_crew.mcp_tools import apps as apps_tools

_TOOLS = frozenset(
    {"issue_radar_crew_read", "issue_radar_crew_record", "issue_radar_record_investigation"}
)


def test_github_client_url_parser_is_the_shared_function():
    assert github_client.parse_github_repo_url is runner.parse_github_repo_url


def test_repo_url_error_is_one_class_across_layers():
    assert errors.RepoUrlError is runner.RepoUrlError
    assert github_client.RepoUrlError is runner.RepoUrlError
    with pytest.raises(errors.RepoUrlError):
        runner.parse_github_repo_url("https://evil.example/o/r")


def test_the_core_gates_this_apps_tools_on_its_own_app_id():
    assert {tool: apps_tools.TOOL_APPS.get(tool) for tool in _TOOLS} == {
        tool: store.APP_NAME for tool in _TOOLS
    }


def test_the_crew_log_off_refusal_names_the_env_file_the_gateway_reads():
    # A mirror facade: its code is in its owners.
    source = inspect.getsource(crew_store) + "".join(
        inspect.getsource(importlib.import_module(owner)) for owner in crew_store._OWNERS
    )
    assert "~/.kiro/crew/.env" not in source
    assert "env_file_display()" in source
