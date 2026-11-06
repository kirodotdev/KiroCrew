"""Fixtures and spec writers the session-array MCP test modules share.

``test_acp_session_mcp.py``, ``test_acp_session_mcp_client_seam.py`` and
``test_acp_claude_mode_pin_seam.py`` import them by name. Autouse fixtures pin
the kiro-cli release the spec ``permissions`` gate sees, the installed
claude-agent-acp version and per-test seed provenance, and the two pin modules
also import the one that clears the starting-mode pin setting;
``agents_dir`` (declared ``@pytest.fixture(name=...)`` and imported under its
function name) points the agent-spec resolver at a temp directory, and the writers
put a spec there or in a project checkout. The last three stand in for the claude
adapter on the wire: one records what the starting-mode pin sends, one reads the live
claude-agent-acp 0.84.0 capture, and one answers a whole handshake from it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}

#: The starting-mode pin setting, spelled as ``configuration.md`` documents it, so a
#: rename of the operator-facing name fails here rather than following the code.
_PIN_SETTING_ENV = "KIROCREW_CC_PIN_INHERITED_MODE"


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts first pays it against the HOST's install
    with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
def _installed_claude_adapter_version(_floor_monkeypatch):
    """The installed claude-agent-acp version the spawn arm would have read.

    The settings writer applies the ``settingSources`` floor before the
    handshake from ``_claude_adapter_disk_version``; tests that drive the writer
    directly never run the spawn arm, so the floor release is set as the class
    default. A test about the floor sets its own.
    """
    _floor_monkeypatch.setattr(AcpClient, "_claude_adapter_disk_version", "0.84.0", raising=False)


@pytest.fixture(autouse=True)
def isolated_seed_provenance(_floor_monkeypatch):
    """Per-test provenance state, mirroring test_acp_seed_provenance.py.

    ``_RECORDS``, ``_LIVE`` and ``_SHARERS`` are process-wide runtime state;
    without this, a client authored in one test leaves a live claim that a
    LATER test's client (pytest truncates long test names to one shared
    tmp-dir prefix, so paths can even collide) is correctly refused against.
    """
    from kiro_crew.acp import seed_provenance

    _floor_monkeypatch.setattr(seed_provenance, "_RECORDS", {})
    _floor_monkeypatch.setattr(seed_provenance, "_LIVE", {})
    _floor_monkeypatch.setattr(seed_provenance, "_SHARERS", {})


@pytest.fixture(name="no_ambient_pin_setting", autouse=True)
def _no_ambient_pin_setting(_floor_monkeypatch):
    """``KIROCREW_CC_PIN_INHERITED_MODE`` is an input here, never the test host's.

    Unset is the default. A fixture that turns it on with its own ``monkeypatch``
    requests ``no_ambient_pin_setting``, so the removal runs first.
    """
    _floor_monkeypatch.delenv(_PIN_SETTING_ENV, raising=False)


@pytest.fixture(name="agents_dir")
def agents_dir_fixture(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # The global settings file is a second source of per-tool restrictions;
    # these tests supply it (or leave it absent) rather than read the machine's.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    # Materialization would try to REBUILD the managed default from bundled
    # defaults; these tests supply the spec themselves.
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name),
    )
    # Registry mode reads the effective config; pinned off (the default for a
    # personal install) so the symmetric filter is deterministic here. The tests
    # that care flip it explicitly.
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(
    agents_dir: Path, *, servers: dict, tools: list | None, name: str = "kirocrew"
) -> None:
    spec: dict = {"name": name, "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _write_project_spec(project_dir: Path, *, servers: dict, tools: list | None) -> None:
    """A spec in the checkout kiro-cli resolves ``--agent`` against first."""
    agents = project_dir / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


def _pin_harness(client, monkeypatch, *, fail=False):
    """Record the requests the pin sends; optionally make ``set_mode`` fail."""
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(_rid, **_kw):
        if fail:
            raise client_mod.AcpError("set_mode refused")
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    return sent, killed


def _recorded_results(scenario):
    """The adapter's own answers, in order, from a live claude-agent-acp 0.84.0 capture.

    ``test/fixtures/claude_mode_pin/<scenario>.jsonl``: agent-to-client lines
    off the wire, pruned of recording-host data (see each file's ``_meta``).
    Returns the JSON-RPC results keyed by the request they answered.
    """
    path = Path(__file__).parent / "fixtures" / "claude_mode_pin" / f"{scenario}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    head = rows[0]["_meta"]
    assert head["agent_version"] == "0.84.0" and head["recorded"] == "live"
    sent = [label.split(" ")[0] for label in head["client_sent"]]
    results = [row["result"] for row in rows[1:] if "id" in row and "result" in row]
    assert len(results) == len(sent)
    by_method: dict[str, list[dict]] = {}
    for method, result in zip(sent, results):
        by_method.setdefault(method, []).append(result)
    return by_method


def _adapter_transport(
    client,
    monkeypatch,
    *,
    started_mode,
    version="0.84.0",
    scenario="project-plan-then-auto-then-pin",
):
    """Answer the handshake with claude-agent-acp 0.84.0's recorded frames.

    Only ``sessionId`` and ``modes.currentModeId`` are set per test; a project file
    cannot start ``auto`` here (``project-auto-filtered``). ``version`` replaces
    ``agentInfo.version``, and ``None`` drops it.
    """
    recorded = _recorded_results(scenario)
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    def _with_mode(result):
        result = json.loads(json.dumps(result))
        if started_mode is not None:
            result["modes"]["currentModeId"] = started_mode
        return result

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(rid, **_kw):
        method = sent[rid - 1][0]
        if method == "initialize":
            resp = json.loads(json.dumps(recorded["initialize"][0]))
            if version is None:
                resp.pop("agentInfo", None)
            else:
                resp["agentInfo"]["version"] = version
            return resp
        if method == "session/new":
            return {**_with_mode(recorded["session/new"][0]), "sessionId": "s-new"}
        if method == "session/load":
            # The adapter answers a load for the session it was asked to load.
            return {
                **_with_mode(recorded["session/load"][0]),
                "sessionId": sent[rid - 1][1]["sessionId"],
            }
        if method == "session/set_mode":
            return recorded["session/set_mode"][-1]
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    async def _work_dir():
        return str(client._work_dir)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_drain_notifications", mock.AsyncMock())
    return sent, killed
