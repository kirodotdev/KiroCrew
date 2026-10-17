"""Kiro Crew's pi gate extension, run under Node against a stand-in for pi's extension API.

The extension carries Crew's MCP servers into a pi session (pi-acp drops the ACP
array) and refuses any call to a Crew server name that pi resolved to a different
server. These cases load the SHIPPED file and drive its handlers the way pi does:
the factory at load, ``session_start``, then ``tool_call``. The stand-in records
what the extension registered and answers ``getMcpServers`` with it, so the shadow
cases are about the extension's own reading of pi's ``mcp.json`` files.

Node strips the file's types itself (22.6+, behind a flag before 23.6), so no pi
install is needed. Marked ``real_adapter``: the lane that installs Node 24 runs it,
and fails rather than skips when Node is absent there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from real_adapter_gate import require_real_adapter

from kiro_crew.acp.harness.pi import pi_gate_extension_path, pi_mcp_handoff_payload

pytestmark = pytest.mark.real_adapter

_NONCE = "0123456789abcdef0123456789abcdef"
_KEY = bytes(range(32))

_ELEMENT = {
    "name": "kirocrew-core",
    "command": "/opt/kirocrew/bin/python",
    "args": ["-m", "kiro_crew.mcp_gateway.stub"],
    "env": [{"name": "KIROCREW_STUB", "value": "1"}],
    "type": "stdio",
}

# Drives the extension: load, session_start, then one tool_call per name in argv.
_HARNESS = r"""
import { pathToFileURL } from "node:url";
const [, , extPath, cwd, ...calls] = process.argv;
const handlers = {};
const registered = [];
const notes = [];
const pi = {
  registerCommand() {},
  registerMcpServer(name, config) { registered.push({ name, config, extensionPath: extPath }); },
  getMcpServers() { return registered; },
  on(event, fn) { handlers[event] = fn; },
};
const ctx = {
  cwd,
  hasUI: true,
  ui: {
    notify(message, level) { notes.push({ message, level }); },
    async confirm() { return true; },
  },
};
const mod = await import(pathToFileURL(extPath).href);
mod.default(pi);
await handlers.session_start({ type: "session_start", reason: "startup" }, ctx);
const results = {};
for (const name of calls) {
  const out = await handlers.tool_call({ toolName: name, toolCallId: "c1", input: {} }, ctx);
  results[name] = out ? out.reason : "allowed";
}
console.log(JSON.stringify({ registered, notes, results }));
"""


def _node() -> list[str] | None:
    node = shutil.which("node")
    if not node:
        return None
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, encoding="utf-8", check=False
    ).stdout
    major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        return None
    return [node, "--experimental-strip-types", "--no-warnings"]


@pytest.fixture
def node() -> list[str]:
    argv = _node()
    require_real_adapter(argv, what="Node 22.6 or later", install="install Node 24")
    assert argv is not None
    return argv


def _run(
    node: list[str],
    tmp_path: Path,
    *,
    mcp_json: str | None,
    calls: list[str],
    handoff_nonce: str = _NONCE,
    key: bytes | None = None,
) -> dict:
    """Run the extension once; *mcp_json* is the text of pi's user-level ``mcp.json``."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    if mcp_json is not None:
        (agent_dir / "mcp.json").write_text(mcp_json, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    payload, _names = pi_mcp_handoff_payload([_ELEMENT], handoff_nonce, _KEY)
    handoff = tmp_path / "handoff.json"
    handoff.write_bytes(payload)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    # A copy beside the harness, so Node resolves the type-only import it erases
    # without walking into the package tree.
    extension = tmp_path / "kiro_crew_tool_gate.ts"
    extension.write_bytes(Path(pi_gate_extension_path()).read_bytes())
    # The host's environment (Windows Node will not start without its system
    # variables), with the home and every variable the extension reads pinned here.
    home = tmp_path / "home"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("PI_", "KIROCREW_PI_"))},
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "KIROCREW_PI_GATE_SESSION": _NONCE,
        "KIROCREW_PI_MCP_HANDOFF": str(handoff),
        "KIROCREW_PI_MCP_KEY": (key if key is not None else _KEY).hex(),
    }
    completed = subprocess.run(
        [*node, str(harness), str(extension), str(project), *calls],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    result["handoff_left"] = handoff.exists()
    return result


_REFUSED = "not the server Kiro Crew placed"


def test_the_handoff_servers_are_registered_for_direct_use(node, tmp_path) -> None:
    out = _run(node, tmp_path, mcp_json=None, calls=["mcp__kirocrew_core__send_message"])
    assert [r["name"] for r in out["registered"]] == ["kirocrew-core"]
    config = out["registered"][0]["config"]
    assert config["command"] == "/opt/kirocrew/bin/python"
    assert config["env"] == {"KIROCREW_STUB": "1"}
    assert config["exposure"] == "direct"
    assert out["results"] == {"mcp__kirocrew_core__send_message": "allowed"}
    # Removed once read where the directory allows it.
    assert out["handoff_left"] is False


def test_a_same_named_server_in_pi_mcp_json_is_refused(node, tmp_path) -> None:
    """pi's own ``~/.pi/agent/mcp.json`` outranks an extension's registration."""
    shadow = json.dumps({"mcpServers": {"kirocrew-core": {"command": "/tmp/not-crew"}}})
    out = _run(
        node,
        tmp_path,
        mcp_json=shadow,
        calls=["mcp__kirocrew_core__send_message", "mcp__other__tool", "read"],
    )
    assert _REFUSED in out["results"]["mcp__kirocrew_core__send_message"]
    assert out["results"]["mcp__other__tool"] == "allowed"
    assert out["results"]["read"] == "allowed"
    assert any("kirocrew-core" in n["message"] for n in out["notes"])


def test_a_tool_name_that_keeps_the_dash_is_refused_too(node, tmp_path) -> None:
    """A release that keeps ``-`` in the server part (``mcp__kirocrew-core__x``) is matched."""
    shadow = json.dumps({"mcpServers": {"kirocrew-core": {"command": "/tmp/not-crew"}}})
    out = _run(node, tmp_path, mcp_json=shadow, calls=["mcp__kirocrew-core__send_message"])
    assert _REFUSED in out["results"]["mcp__kirocrew-core__send_message"]


def test_a_name_differing_only_in_dash_and_underscore_is_the_same_server(node, tmp_path) -> None:
    """pi counts ``kirocrew_core`` and ``kirocrew-core`` as one server."""
    shadow = json.dumps({"mcpServers": {"kirocrew_core": {"command": "/tmp/not-crew"}}})
    out = _run(node, tmp_path, mcp_json=shadow, calls=["mcp__kirocrew_core__send_message"])
    assert _REFUSED in out["results"]["mcp__kirocrew_core__send_message"]


def test_an_unreadable_mcp_json_refuses_every_crew_server(node, tmp_path) -> None:
    """What a broken file declares is unknown, so none of Crew's names is trusted."""
    out = _run(node, tmp_path, mcp_json="{not json", calls=["mcp__kirocrew_core__x"])
    assert _REFUSED in out["results"]["mcp__kirocrew_core__x"]


def test_a_handoff_under_another_key_registers_nothing(node, tmp_path) -> None:
    """A sibling session's file does not open under this process's key."""
    out = _run(node, tmp_path, mcp_json=None, calls=[], key=bytes(32))
    assert out["registered"] == []


def test_a_handoff_for_another_nonce_registers_nothing(node, tmp_path) -> None:
    """The file is bound to the session: one written for a different nonce is ignored."""
    out = _run(node, tmp_path, mcp_json=None, calls=[], handoff_nonce="f" * 32)
    assert out["registered"] == []
