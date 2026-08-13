"""Tests for shared-MCP-gateway delivery via ACP ``session/new`` injection.

The pooling mechanism under test: kiro-cli honours a server injected in
``session/new`` AHEAD of the same-named entry in the resolved agent spec, so
injecting broker stubs pools an agent's servers without writing a spec into the
user's project, their ``~/.kiro/agents/``, or a bind mount.

``test_real_kiro_cli_prefers_session_injected_server`` is the anti-drift guard:
that precedence is verified but undocumented, so it is pinned against the
shipped binary whenever one is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER
from kiro_crew.mcp_gateway.session_servers import (
    _acp_env,
    _acp_server_entry,
    pooled_session_servers,
)


def _stub(**over):
    entry = {
        _WRAPPER_MARKER: True,
        "command": "/data/mcp-gateway/stubs/mc-mcp-stub-wrapper.sh",
        "args": ["--target-command=fetch", "--socket", "/data/gateway.sock"],
        "env": {},
        "autoApprove": ["fetch___fetch"],
    }
    entry.update(over)
    return entry


def _write_overlay(tmp_path: Path, agent: str, servers: dict) -> Path:
    overlay = tmp_path / "agents"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / f"{agent}.json").write_text(
        json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8"
    )
    return overlay


# ── selection: only stubs are injected ──────────────────────────────────────


def test_injects_only_wrapped_stub_entries(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {
        "pooled": _stub(),
        "unpooled": {"command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": "s3cr3t"}},
    })
    out = pooled_session_servers(overlay, "kirocrew")
    assert [e["name"] for e in out] == ["pooled"]


def test_unpooled_server_env_is_never_transmitted(tmp_path):
    """A non-poolable server's credentials must stay in the spec file."""
    overlay = _write_overlay(tmp_path, "kirocrew", {
        "secretive": {"command": "npx", "env": {"API_KEY": "s3cr3t"}},
    })
    assert pooled_session_servers(overlay, "kirocrew") == []
    assert "s3cr3t" not in json.dumps(pooled_session_servers(overlay, "kirocrew"))


def test_stub_name_is_preserved_so_it_shadows_the_spec_entry(tmp_path):
    """The injected name must equal the original server name — that identity is
    what suppresses the agent spec's own copy (and keeps tool ids stable)."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"builder-mcp": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["name"] == "builder-mcp"


def test_entries_are_name_sorted_for_deterministic_params(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {
        "zeta": _stub(), "alpha": _stub(), "mid": _stub(),
    })
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == [
        "alpha", "mid", "zeta",
    ]


# ── shaping into the ACP element form ───────────────────────────────────────


def test_marker_is_stripped_from_the_injected_element(tmp_path):
    """kiro-cli tolerates unknown keys today; a future strict parser would not."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert _WRAPPER_MARKER not in entry


def test_operator_passthrough_keys_survive(tmp_path):
    """Dropping autoApprove would re-prompt for already-approved tools."""
    overlay = _write_overlay(tmp_path, "kirocrew", {
        "pooled": _stub(timeout=9000, type="stdio", disabledTools=["x"]),
    })
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["autoApprove"] == ["fetch___fetch"]
    assert entry["timeout"] == 9000
    assert entry["type"] == "stdio"
    assert entry["disabledTools"] == ["x"]


def test_env_is_emitted_in_acp_array_form():
    assert _acp_env({}) == []
    assert _acp_env({"A": "1"}) == [{"name": "A", "value": "1"}]
    assert _acp_env({"N": 5}) == [{"name": "N", "value": "5"}]
    assert _acp_env(None) == []
    assert _acp_env("nonsense") == []


def test_stub_entries_carry_no_env(tmp_path):
    """gatewayd spawns the pooled backend, so kiro-cli needs no env at all."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["env"] == []


def test_non_string_args_are_coerced(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub(args=["ok", 7])})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["args"] == ["ok", "7"]


def test_entry_without_command_is_skipped():
    """Injecting a commandless stub would shadow a working server with a broken
    one; leaving it out keeps the spec's own entry live."""
    assert _acp_server_entry("x", {_WRAPPER_MARKER: True, "command": ""}) is None
    assert _acp_server_entry("x", {_WRAPPER_MARKER: True}) is None


def test_commandless_stub_does_not_suppress_others(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {
        "broken": _stub(command=""), "fine": _stub(),
    })
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["fine"]


# ── the off switch and fail-soft behaviour ──────────────────────────────────


def test_disabled_gateway_injects_nothing():
    assert pooled_session_servers(None, "kirocrew") == []


def test_missing_agent_name_injects_nothing(tmp_path):
    assert pooled_session_servers(tmp_path, None) == []


def test_absent_overlay_spec_is_not_an_error(tmp_path):
    assert pooled_session_servers(tmp_path / "nope", "kirocrew") == []


def test_corrupt_overlay_degrades_to_unpooled(tmp_path):
    overlay = tmp_path / "agents"
    overlay.mkdir()
    (overlay / "kirocrew.json").write_text("{not json", encoding="utf-8")
    assert pooled_session_servers(overlay, "kirocrew") == []


@pytest.mark.parametrize("body", ["[]", '"str"', "null", '{"mcpServers": []}',
                                  '{"mcpServers": "x"}', "{}"])
def test_malformed_spec_shapes_degrade_to_unpooled(tmp_path, body):
    overlay = tmp_path / "agents"
    overlay.mkdir(exist_ok=True)
    (overlay / "kirocrew.json").write_text(body, encoding="utf-8")
    assert pooled_session_servers(overlay, "kirocrew") == []


def test_non_dict_server_entry_is_skipped(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {"bad": "x", "good": _stub()})
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["good"]


# ── the mechanism itself, pinned against the shipped binary ─────────────────


REAL_CLI = shutil.which("kiro-cli")

#: A probe MCP server that records that it launched and then lingers, in place
#: of ``sh -c "touch X; sleep 20"``. The interpreter is portable where a POSIX
#: shell is not, and the marker path arrives as ``argv`` so a Windows path's
#: backslashes never pass through a string literal.
_PROBE_SNIPPET = (
    "import pathlib,sys,time;"
    "pathlib.Path(sys.argv[1]).write_text('x');"
    "time.sleep(20)"
)

_DRIVER = r"""
import json, os, subprocess, sys, threading, time
from kiro_crew import platform_compat

w = sys.argv[1]
p = subprocess.Popen(["kiro-cli", "acp", "--agent", "pooltest"], cwd=w + "/proj",
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True,
                     env={**os.environ, "KIRO_HOME": w + "/khome"})


def teardown():
    # The whole TREE, not just kiro-cli: the probe MCP servers are ITS children
    # and inherit its cwd (the work dir), so a bare p.kill() leaves them alive
    # holding a directory handle on it. On Windows that handle makes the caller's
    # rmtree fail with WinError 32 ("used by another process"), so the test blew
    # up in TemporaryDirectory teardown after its assertions had already passed.
    try:
        platform_compat.kill_process_tree(p.pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        p.kill()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # Close our ends of the pipes too, so no descriptor of ours outlives the run.
    for stream in (p.stdin, p.stdout, p.stderr):
        try:
            stream.close()
        except OSError:
            pass


send = lambda o: (p.stdin.write(json.dumps(o) + "\n"), p.stdin.flush())
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1, "clientCapabilities":
                 {"fs": {"readTextFile": False, "writeTextFile": False}}}})
# Wait for the initialize RESPONSE rather than a fixed span, but on a THREAD with
# a bound: a bare readline() on a stalled CLI blocks forever, and this driver's
# own 180s subprocess timeout is longer than the suite's --timeout=120, so the
# hang would surface as a pytest timeout kill rather than the clean failure
# below. A cooperative CLI answers in well under a second.
_line = []
_t = threading.Thread(target=lambda: _line.append(p.stdout.readline()), daemon=True)
_t.start()
_t.join(30)
if not _line:
    teardown()
    sys.exit("kiro-cli never answered initialize within 30s")
send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": w + "/proj", "mcpServers": [
          {"name": "shared", "command": sys.executable,
           "args": ["-c", sys.argv[2], w + "/marks/INJECTED"], "env": []}]}})
# Poll for the marker the injected server writes, with a deadline generous
# enough for a cold CLI. The old 8s sleep was paid in full on every run.
deadline = time.time() + 20
injected = os.path.join(w, "marks", "INJECTED")
while time.time() < deadline and not os.path.exists(injected):
    time.sleep(0.05)
# Give a same-named spec server, if injection ever became additive, the same
# chance to write its own marker -- otherwise this driver could exit before the
# thing the test asserts is ABSENT would have appeared, making it pass vacuously.
time.sleep(1)
teardown()
"""


@pytest.mark.skipif(not REAL_CLI, reason="kiro-cli not on PATH")
def test_real_kiro_cli_prefers_session_injected_server():
    """ANTI-DRIFT GUARD. Pins the undocumented precedence pooling relies on.

    Runs on every platform now that both probe servers are launched through
    ``sys.executable`` instead of a POSIX shell. That matters because the CI
    Windows runner has no kiro-cli, so CI alone can never verify this
    assumption -- but any Windows machine with the CLI installed verifies it by
    running the suite, which is the only way this precedence gets checked on the
    platform where the transport is newest.

    kiro-cli documents priority only among the three *file* tiers (agent config
    > workspace mcp.json > global mcp.json); it does not document that a
    ``session/new`` server outranks the agent spec. If a release made injection
    additive instead, the agent's own server would launch alongside the stub —
    every poolable server would run twice, which is worse than not pooling. This
    test fails loudly at that point instead of shipping a silent regression.
    """
    # ignore_cleanup_errors: the spawned kiro-cli subprocess can still hold a
    # handle on the temp tree at teardown; Windows refuses to delete a dir with
    # open handles (POSIX does not), which would raise WinError 32 out of the
    # context manager even though the test's assertions already passed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "khome" / "agents").mkdir(parents=True)
        (root / "proj").mkdir()
        (root / "marks").mkdir()
        (root / "khome" / "agents" / "pooltest.json").write_text(json.dumps({
            "name": "pooltest",
            "description": "precedence probe",
            "model": "claude-haiku-4.5",
            "tools": [],
            "prompt": "probe",
            "mcpServers": {"shared": {
                "command": sys.executable,
                "args": ["-c", _PROBE_SNIPPET, str(root / "marks" / "FROM_SPEC")],
            }},
        }), encoding="utf-8")
        driver = root / "drive.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        subprocess.run([sys.executable, str(driver), str(root), _PROBE_SNIPPET],
                       capture_output=True, timeout=180, check=False)
        deadline = time.time() + 5
        while time.time() < deadline and not (root / "marks" / "INJECTED").exists():
            time.sleep(0.2)
        assert (root / "marks" / "INJECTED").exists(), (
            "session/new-injected server never launched — ACP injection is not "
            "taking effect at all; pooling cannot work through this channel"
        )
        assert not (root / "marks" / "FROM_SPEC").exists(), (
            "the agent spec's same-named server ALSO launched: session/new "
            "injection has become additive rather than overriding, so every "
            "pooled server would run twice. Pooling delivery must change."
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX pathing in fixture")
def test_injection_writes_nothing_to_the_work_dir(tmp_path):
    """The whole point of this channel: no file lands in the user's project."""
    work = tmp_path / "project"
    (work / ".kiro").mkdir(parents=True)
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    before = {p for p in work.rglob("*")}
    assert pooled_session_servers(overlay, "kirocrew")
    assert {p for p in work.rglob("*")} == before
