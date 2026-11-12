"""A KAS session's own agent servers never queue ahead of Kiro Crew's servers.

KAS connects one process's MCP servers through a queue four wide, in the order a
session declares them: the running agent's ``mcpServers`` block first, the
session-level array last, and a mode switch declares the new agent's block
(measured on kiro-cli 3.2.0 with twelve slow block servers: the array's servers
connected at 13.5 s when the session started in its own agent and at 1.3 s when
it started in an agent with no block).

Session start waits for Kiro Crew's servers, which travel in the array. So a new
session starts in an agent that declares no server, and the ``set_mode`` every
start already sends declares its own agent's block behind them
(``kiro_crew.acp.kas_start_agent``).

The stub here models that queue rather than accepting everything. A server is
``connected`` only once it has answered both ``initialize`` and ``tools/list``;
one that answers neither, or only the first, holds its slot as ``connecting``
until the engine's connect timeout and then reports ``failed``. Both were
measured to look the same on the wire (kiro-cli 3.2.0: ``connecting``, then
``failed`` with ``connection timed out after <n>ms``). A server named ``slow-*``
never answers ``initialize``, so a session whose own servers sit behind four of
them never becomes ready.
"""

from __future__ import annotations

import json
import sys

import pytest

from kiro_crew.acp import runtime_session_load
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, EVENT_TEXT_CHUNK, METHOD_KAS_SESSION_DELETE
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_CORE = "kirocrew-core"

#: A KAS-shaped agent that connects servers the way the engine's queue does.
_QUEUE_STUB = """
import json, os, sys

SLOTS = 4
TIMEOUT_MS = 120000
BUILTIN = ["vibe", "spec", "plan"]
RECORD = os.environ["KAS_STUB_RECORD"]
SID = "kas-queue-session"
CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
# What a server never answers: "initialize" or "tools/list". Absent: it answers both.
NEVER = json.loads(os.environ.get("KAS_STUB_NEVER_ANSWERS", "{}"))
state = {"queue": [], "start_mode": None, "start_block": None, "set_mode": None,
         "batch": [], "current": None, "released": False, "timed_out": False,
         "methods": []}
agents = {}

def hangs(name):
    # Connected needs both answers, so either missing one leaves it connecting.
    never = "initialize" if name.startswith("slow-") else NEVER.get(name)
    return never in ("initialize", "tools/list") and not state["released"]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def record():
    with open(RECORD, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

def declare(names):
    for name in names:
        if name not in state["queue"]:
            state["queue"].append(name)

def block(mode_id):
    servers = (agents.get(mode_id) or {}).get("mcpServers")
    return list(servers) if isinstance(servers, dict) else []

def connected():
    # A hung server holds its slot until its connect timeout; a fast one hands it back.
    done, busy = [], 0
    for name in state["queue"]:
        if busy == SLOTS:
            break
        if hangs(name):
            if not state["timed_out"]:
                busy += 1
        else:
            done.append(name)
    return done

def status():
    done = connected()
    servers = []
    for name in state["queue"]:
        entry = {"name": name, "status": "connecting", "_meta": CLIENT}
        if name in done:
            entry["status"] = "connected"
            entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
        elif hangs(name) and state["timed_out"]:
            entry["status"] = "failed"
            entry["errorMessage"] = (
                "MCP server '%s' connection timed out after %dms" % (name, TIMEOUT_MS))
        servers.append(entry)
    send({"jsonrpc": "2.0", "method": "_kiro/mcp/status",
          "params": {"sessionId": SID, "servers": servers}})

def mode_entry(mode_id):
    origin = "bundled" if mode_id in BUILTIN else "client"
    return {"id": mode_id, "name": mode_id,
            "_meta": {"kiro": {"resource": {"resourceType": "agent", "source": {"origin": origin}}}}}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    state["methods"].append(method)
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        kiro = (params.get("_meta") or {}).get("kiro") or {}
        batch = [a for a in kiro.get("customAgents") or [] if isinstance(a, dict) and a.get("id")]
        agents = {a["id"]: a for a in batch}
        modes = BUILTIN + [a["id"] for a in batch]
        state["batch"] = [a["id"] for a in batch]
        state["start_mode"] = kiro.get("modeId")
        state["current"] = kiro.get("modeId") if kiro.get("modeId") in modes else "vibe"
        state["start_block"] = block(state["current"])
        # The running agent's block is queued ahead of the session-level array.
        declare(state["start_block"])
        declare([s.get("name") for s in params.get("mcpServers") or []])
        record()
        status()
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": SID,
            "modes": {"currentModeId": state["current"],
                      "availableModes": [mode_entry(m) for m in modes]}}})
    elif method == "session/set_mode":
        requested = params.get("modeId")
        state["set_mode"] = requested
        if requested not in BUILTIN and requested not in agents:
            record()
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603, "message": "Mode '%s' not found" % requested}})
            continue
        if requested != state["current"]:
            # A real switch queues the new agent's block; a re-select changes nothing.
            state["current"] = requested
            declare(block(requested))
            if not os.environ.get("KAS_STUB_SILENT_SWITCH"):
                status()
        record()
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "_test/release":
        # Every slow server answers at last, as one does after the first prompt went out.
        state["released"] = True
        record()
        status()
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "_test/timeout":
        # The engine's connect timeout runs out for every server still hung.
        state["timed_out"] = True
        record()
        status()
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": SID,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "pong"}}}})
        record()
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        record()
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        record()
"""


@pytest.fixture
def queue_stub(tmp_path, monkeypatch):
    """The queue-modelling KAS stub, started in place of kiro-cli; returns its record file."""
    script = tmp_path / "kas_queue_stub.py"
    script.write_text(_QUEUE_STUB, encoding="utf-8")
    record = tmp_path / "stub-record.json"
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    async def budget(self) -> float:
        # A start stuck behind the queue fails by name at the lost-run bound,
        # not at the 90 s production budget.
        return FALLBACK_TIMEOUT_SECS

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setattr(AcpRuntime, "_session_start_budget", budget)
    monkeypatch.setenv("KAS_STUB_RECORD", str(record))
    # The bounded wait for the agent's own servers has run: its cap is spent.
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0)
    monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
    return record


def _write_agent(tmp_path, monkeypatch, slow: int, named: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Kiro Crew's agent with its own server, *slow* servers that never answer and *named* ones.

    Returns the names of the servers beside Kiro Crew's, in declaration order.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    names = (*(f"slow-{i:02d}" for i in range(slow)), *named)
    servers: dict = {name: {"command": "user-server"} for name in names}
    servers[_CORE] = {"command": "kirocrew", "args": ["mcp-core"]}
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "description": "test crew agent",
                "prompt": "You are Kiro.",
                "tools": ["fs_read", f"@{_CORE}", *(f"@{name}" for name in names)],
                "mcpServers": servers,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr("kiro_crew.acp.default_crew_servers.DEFAULT_CREW_SERVERS", ())
    return names


async def _start(tmp_path):
    runtime = AcpRuntime(
        work_dir=tmp_path / "ws", agent="kirocrew", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS
    )
    try:
        await runtime.spawn()
        return runtime, await runtime.create_session(cwd=tmp_path / "ws")
    except BaseException:
        # A start that fails or is cancelled never hands the runtime to its test,
        # so the stub process is ended here.
        await runtime.kill()
        raise


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestSessionStartBehindSlowServers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("slow", [4, 30], ids=["a-full-queue", "thirty"])
    async def test_the_session_starts_while_the_agents_servers_still_connect(
        self, queue_stub, tmp_path, monkeypatch, slow
    ):
        """Four servers that never answer hold every connect slot, and the start
        still ends: Kiro Crew's server was queued ahead of them."""
        names = _write_agent(tmp_path, monkeypatch, slow)
        runtime, handle = await _start(tmp_path)
        try:
            report = handle.mcp_session_report()
            assert report.unreported((_CORE,)) == ()
            assert report.unreported(names) == names
            assert handle.active_agent == "kirocrew"
        finally:
            await runtime.kill()
        seen = json.loads(queue_stub.read_text(encoding="utf-8"))
        assert seen["queue"] == [_CORE, *names]
        # The session started in the entry registered after its own agent, one
        # that declares no server, and then switched: no prompt runs there.
        assert seen["batch"][0] == "kirocrew"
        assert seen["start_mode"] == seen["batch"][-1] != "kirocrew"
        assert seen["start_block"] == []
        assert seen["set_mode"] == "kirocrew"
        assert seen["current"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_a_report_staged_before_the_switch_still_counts(
        self, queue_stub, tmp_path, monkeypatch
    ):
        """Kiro Crew's server connected before ``set_mode`` went out, and the engine
        sends no status frame for the switch: the start reads the staged report
        instead of waiting for one that never comes."""
        monkeypatch.setenv("KAS_STUB_SILENT_SWITCH", "1")
        names = _write_agent(tmp_path, monkeypatch, 4)
        runtime, handle = await _start(tmp_path)
        try:
            report = handle.mcp_session_report()
            assert report.unreported((_CORE,)) == ()
            assert report.unreported(names) == names
        finally:
            await runtime.kill()
        seen = json.loads(queue_stub.read_text(encoding="utf-8"))
        assert seen["current"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_a_server_that_answers_after_the_start_is_reported(
        self, queue_stub, tmp_path, monkeypatch
    ):
        """A late server needs no restart: its report is recorded when it arrives."""
        names = _write_agent(tmp_path, monkeypatch, 4)
        runtime, handle = await _start(tmp_path)
        try:
            assert handle.mcp_session_report().unreported(names) == names
            await runtime._send_and_await("_test/release", {}, timeout=FALLBACK_TIMEOUT_SECS)
            await handle.wait_mounted_mcp(names, FALLBACK_TIMEOUT_SECS)
            assert handle.mcp_session_report().unreported(names) == ()
        finally:
            await runtime.kill()

    @pytest.mark.asyncio
    async def test_an_agent_with_only_kiro_crews_server_starts_in_itself(
        self, queue_stub, tmp_path, monkeypatch
    ):
        """Nothing to hold back, so no extra entry and no real mode switch."""
        _write_agent(tmp_path, monkeypatch, 0)
        runtime, handle = await _start(tmp_path)
        try:
            assert handle.mcp_session_report().unreported((_CORE,)) == ()
        finally:
            await runtime.kill()
        seen = json.loads(queue_stub.read_text(encoding="utf-8"))
        assert seen["start_mode"] == "kirocrew"
        assert seen["batch"] == ["kirocrew"]
        assert seen["set_mode"] == "kirocrew"


_HUNG = "desktop-tool"
_FAST = ("github", "fetch")


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize(
    "never_answers", ["initialize", "tools/list"], ids=["no-initialize", "no-tools-list"]
)
class TestOneServerThatHangs:
    """One of the agent's servers never finishes connecting; the others are healthy.

    Declared first, so it takes the first free slot and keeps it until the
    engine's connect timeout. Session start does not wait for that timeout.
    """

    @pytest.fixture
    def hung_session(self, queue_stub, tmp_path, monkeypatch, never_answers):
        monkeypatch.setenv("KAS_STUB_NEVER_ANSWERS", json.dumps({_HUNG: never_answers}))
        _write_agent(tmp_path, monkeypatch, 0, named=(_HUNG, *_FAST))
        return queue_stub

    @pytest.mark.asyncio
    async def test_the_prompt_is_released_and_the_session_answers(self, hung_session, tmp_path):
        runtime, handle = await _start(tmp_path)
        try:
            report = handle.mcp_session_report()
            # Kiro Crew's server is ready, the hung one is still not reported.
            assert report.unreported((_CORE,)) == ()
            assert report.unreported((_HUNG,)) == (_HUNG,)
            # Its healthy neighbours connect behind Kiro Crew's server.
            await handle.wait_mounted_mcp(_FAST, FALLBACK_TIMEOUT_SECS)
            assert report.unreported(_FAST) == ()
            text = [
                event.text
                async for event in handle.prompt("ping", timeout=FALLBACK_TIMEOUT_SECS)
                if event.kind == EVENT_TEXT_CHUNK
            ]
            assert "".join(text) == "pong"
            assert report.unreported((_HUNG,)) == (_HUNG,)
        finally:
            await runtime.kill()
        seen = json.loads(hung_session.read_text(encoding="utf-8"))
        assert seen["queue"] == [_CORE, _HUNG, *_FAST]

    @pytest.mark.asyncio
    async def test_the_hung_server_is_reported_failed_when_its_timeout_runs_out(
        self, hung_session, tmp_path
    ):
        """The timeout keeps running after the start, and its failure is recorded as before."""
        runtime, handle = await _start(tmp_path)
        try:
            report = handle.mcp_session_report()
            assert _HUNG not in report.payload()["failed"]
            await runtime._send_and_await("_test/timeout", {}, timeout=FALLBACK_TIMEOUT_SECS)
            await handle.wait_mounted_mcp((_HUNG,), FALLBACK_TIMEOUT_SECS)
            payload = report.payload()
            assert _HUNG in payload["failed"]
            assert "timed out" in payload["failures"][_HUNG]
            assert _CORE in payload["ready"]
        finally:
            await runtime.kill()

    @pytest.mark.asyncio
    async def test_closing_the_session_frees_it_while_the_server_still_hangs(
        self, hung_session, tmp_path
    ):
        runtime, handle = await _start(tmp_path)
        try:
            session_id = handle.session_id
            assert session_id in runtime._session_queues
            await runtime.terminate_session(session_id)
            assert session_id not in runtime._session_queues
            assert session_id not in runtime._session_cwds
        finally:
            await runtime.kill()
        assert not runtime.is_alive()
        seen = json.loads(hung_session.read_text(encoding="utf-8"))
        assert METHOD_KAS_SESSION_DELETE in seen["methods"]
