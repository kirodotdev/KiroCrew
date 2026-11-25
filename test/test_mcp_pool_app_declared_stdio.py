"""An app-declared stdio MCP server never reaches the gateway pool on its own.

An installed app's manifest ``mcpServers`` entry is written, namespaced
``<app>:<server>``, into the agent config the rewriter reads. The pool spawns a
backend (outside the sandbox, with the gateway env) only for a target the
rewriter wrapped and the operator approved by content. These tests pin that
chain for an app-registered stdio server:

* naming it in ``stub_servers`` is not enough: without an approval the rewrite
  emits no target, so nothing can be spawned;
* an approval for the same name but another command does not admit it;
* gatewayd's resolver refuses it when the target map carries it unapproved;
* the positive control (the operator approved this exact launch) does produce a
  target, so the refusals above are not a vacuous harness;
* ``spawn_backend`` has one caller, gatewayd, which sits behind that resolver.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source
from mcp_launch_approval_helpers import (
    _ATTACK_CMD,
    _approval_module,
    _approvals_for,
    _pool_key,
    _rewrite,
)

from kiro_crew.apps.bridges import _register_mcp_servers
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

_APP_SERVER = "test-app:srv"


@pytest.fixture(name="app_entry")
def _app_entry_fixture(tmp_path, app_env, monkeypatch):
    """Register a stdio server through the app path; return its stored agent-config entry."""
    import kiro_crew.apps.bridges as bmod

    agents = tmp_path / "agents"
    agents.mkdir()
    config = agents / "kirocrew.json"
    monkeypatch.setattr(bmod, "_mcp_json_path", lambda: config)
    src = _make_app_source(
        tmp_path,
        mcpServers={"srv": {"command": sys.executable, "args": ["-c", "pass"]}},
    )
    install_app(src)
    manifest = AppManifest.from_json_file(
        app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
    )
    assert _register_mcp_servers("test-app", manifest) == [_APP_SERVER]
    data = json.loads(config.read_text(encoding="utf-8"))
    # The rewriter reads agent specs; give the app-written file the spec shape
    # the agent-config builder gives it, without touching its server entry.
    data.setdefault("name", "kirocrew")
    data.setdefault("tools", ["*"])
    config.write_text(json.dumps(data), encoding="utf-8")
    return data["mcpServers"][_APP_SERVER]


def test_app_stdio_server_is_not_pooled_when_not_stubbed(tmp_path, app_entry):
    approvals = _approval_module().LaunchApprovals()
    assert _rewrite(tmp_path, frozenset(), approvals) == {}


def test_app_stdio_server_is_not_pooled_when_stubbed_but_unapproved(tmp_path, app_entry):
    approvals = _approval_module().LaunchApprovals()
    target_env = _rewrite(tmp_path, frozenset({_APP_SERVER}), approvals)
    assert target_env == {}
    assert approvals.refused, "the unapproved launch is recorded as refused"


def test_approval_of_another_command_under_the_same_name_does_not_admit_it(tmp_path, app_entry):
    mod = _approval_module()
    approvals = _approvals_for(mod, _APP_SERVER, _ATTACK_CMD, ["-c", "id"])
    assert _rewrite(tmp_path, frozenset({_APP_SERVER}), approvals) == {}


def test_positive_control_an_operator_approved_launch_is_pooled(tmp_path, app_entry):
    mod = _approval_module()
    approvals = _approvals_for(mod, _APP_SERVER, app_entry["command"], list(app_entry["args"]))
    target_env = _rewrite(tmp_path, frozenset({_APP_SERVER}), approvals)
    # A bare key and an args-hash key, both naming the approved command.
    assert target_env
    assert {shlex.split(v)[0] for v in target_env.values()} == {app_entry["command"]}


def test_gatewayd_refuses_an_app_server_in_the_target_map_without_approval(
    tmp_path, monkeypatch, app_entry
):
    from kiro_crew.mcp_gateway import gatewayd

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    key = "KIROCREW_MCP_TARGET_" + _APP_SERVER.upper().replace("-", "_")
    spec = " ".join(shlex.quote(p) for p in (app_entry["command"], *app_entry["args"]))
    monkeypatch.setenv(key, spec)
    assert gatewayd.env_target_resolver(_pool_key(_APP_SERVER)) is None


def test_spawn_backend_has_one_caller_and_it_is_gatewayd():
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    callers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if re.search(r"\bawait spawn_backend\(", path.read_text(encoding="utf-8"))
    )
    assert callers == ["mcp_gateway/gatewayd.py"]
