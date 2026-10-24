"""codex's command approval under the capability Crew declares, measured live.

A sibling of ``test_codex_session_mcp.py`` (which holds the adapter's spec-projection
and session-lifecycle measurements and is at its line cap); the driver helpers and
the adapter gate are imported from there so both files measure the same installed
``codex-acp``. Collected by the Real Adapter Contract Tests lane like its sibling.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from test_codex_session_mcp import _ENTRY, _require_codex_acp, _run_driver_reaping_group

from kiro_crew.acp.harness.codex import CodexHarness

# ── command approval with clientCapabilities.elicitation declared ────────────

# The elicitation capability Crew declares (``ACP_CLIENT_CAPABILITIES``) is what a
# standard harness consults before forwarding an MCP server's ``elicitation/create``.
# The premise pinned here is that it moves NO tool approval: codex's command approval
# is routed by ``handleCommandExecution`` -> ``session/request_permission`` without
# reading the capability at all, and only a non-empty form or a URL ask consults it
# (``shouldUseAcpElicitation``). Measured, not read off the source: the loopback model
# answers the first request with an ``exec_command`` call asking for escalation, codex
# in its ``read-only`` mode (approval policy ``on-request``, reviewer ``user``) must
# then ask, and the client declares the full capability set a real session does.
_APPROVAL_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, entry, node, initialize_json = sys.argv[1:5]
initialize = json.loads(initialize_json)
work = os.path.join(root, "work")
COMMAND = "echo PROBE-RAN > probe-ran.txt"
log, lock = [], threading.Lock()
USAGE = {"input_tokens": 100, "input_tokens_details": None, "output_tokens": 5,
         "output_tokens_details": None, "total_tokens": 105}


class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [], "models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        text = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        with lock:
            n = len(log)
            log.append(text[:400])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode())
            self.wfile.flush()

        emit({"type": "response.created", "response": {"id": "resp_%d" % n}})
        if n == 0:
            # codex 2.2.2's shell tool; `require_escalated` is what makes a
            # read-only session ask the person before running it.
            emit({"type": "response.output_item.done", "item": {
                "type": "function_call", "id": "fc_0", "call_id": "call_0", "name": "exec_command",
                "arguments": json.dumps({"cmd": COMMAND, "sandbox_permissions": "require_escalated",
                                         "justification": "probe: write one file"})}})
        else:
            emit({"type": "response.output_item.done", "item": {
                "type": "message", "role": "assistant", "id": "msg_%d" % n,
                "content": [{"type": "output_text", "text": "STUB-REPLY-%d" % n}]}})
        emit({"type": "response.completed", "response": {"id": "resp_%d" % n, "usage": USAGE}})


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
with open(os.path.join(root, "codex_home", "config.toml"), "w") as fh:
    fh.write('model = "gpt-5-codex"\nmodel_provider = "stub"\n'
             '[model_providers.stub]\nname = "stub"\n'
             'base_url = "http://127.0.0.1:%d/v1"\nwire_api = "responses"\n'
             'env_key = "KIROCREW_STUB_MODEL_KEY"\n'
             'request_max_retries = 0\nstream_max_retries = 0\n' % server.server_address[1])
env = dict(os.environ, CODEX_HOME=os.path.join(root, "codex_home"), NO_BROWSER="1",
           KIROCREW_STUB_MODEL_KEY="stub-key")

p = subprocess.Popen([node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1)
q, frames, requests, counter = queue.Queue(), [], [], [0]


def pump():
    for line in p.stdout:
        q.put(line)
    q.put(None)


threading.Thread(target=pump, daemon=True).start()


def send(method, params):
    counter[0] += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter[0], "method": method, "params": params}) + "\n")
    p.stdin.flush()
    return counter[0]


def until(done, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            return {"_eof": True}
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        frames.append(msg)
        if "method" in msg and "id" in msg:
            requests.append(msg)
            if msg["method"] == "session/request_permission":
                allow = [o for o in (msg.get("params") or {}).get("options") or [] if o.get("kind") == "allow_once"]
                answer = {"result": {"outcome": {"outcome": "selected", "optionId": allow[0]["optionId"]}}} if allow else {"result": {"outcome": {"outcome": "cancelled"}}}
            else:
                answer = {"error": {"code": -32601, "message": "n/a"}}
            p.stdin.write(json.dumps(dict(answer, jsonrpc="2.0", id=msg["id"])) + "\n")
            p.stdin.flush()
        elif done(msg):
            return msg
    return {"_timeout": True}


def call(method, params, timeout=60):
    rid = send(method, params)
    return until(lambda m: m.get("id") == rid and "method" not in m, timeout)


out = {}
try:
    out["init"] = call("initialize", initialize)
    new = call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new.get("error")
    sid = (new.get("result") or {}).get("sessionId")
    out["sid"] = sid
    if sid:
        out["set_mode"] = call("session/set_mode", {"sessionId": sid, "modeId": "read-only"})
        out["prompt"] = call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "PROBE: run the command."}]}, 120)
finally:
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            break
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            break
out["requests"] = [{"method": r["method"], "params": r.get("params")} for r in requests]
out["frames"] = frames
out["probe_ran"] = os.path.exists(os.path.join(work, "probe-ran.txt"))
out["model_requests"] = len(log)
server.shutdown()
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def codex_approval() -> dict:
    """One driven run: Crew's own ``initialize`` params, then a turn that must ask."""
    from kiro_crew.acp.client_start import CLIENT_NAME, CLIENT_VERSION

    _require_codex_acp(with_node=True)
    # The handshake the shared runtime sends a codex session
    # (``runtime_spawn_launch``): Crew's client name, and the harness's own protocol
    # revision and capability declaration.
    harness = CodexHarness()
    params = {
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        "protocolVersion": harness.protocol_version,
        "clientCapabilities": harness.client_capabilities,
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        driver = root / "drive_approval.py"
        driver.write_text(_APPROVAL_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [
                sys.executable,
                str(driver),
                str(root),
                str(_ENTRY),
                shutil.which("node") or "node",
                json.dumps(params),
            ],
            timeout=600,
            cwd=root / "work",
        )
    try:
        m = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail(f"the approval driver produced no measurement\nstderr: {result.stderr[-3000:]}")
    assert m.get(
        "sid"
    ), f"session/new failed: {m.get('new_error')!r}\ninit: {m.get('init')!r}\nstderr: {result.stderr[-3000:]}"
    m["_initialize"] = params
    m["_context"] = json.dumps({k: v for k, v in m.items() if k != "frames"})[-6000:]
    return m


@pytest.mark.real_adapter
def test_real_codex_acp_command_approval_stays_on_request_permission_with_elicitation_declared(
    codex_approval,
):
    """With ``clientCapabilities.elicitation`` declared, a codex command approval is still
    a ``session/request_permission`` -- never an ``elicitation/create`` -- and the allow
    answer runs the command. This is the live half of the premise ``ACP_CLIENT_CAPABILITIES``
    rests on (``acp/types.py``); ``test_acp_client_capabilities.py`` pins the declaration."""
    m, ctx = codex_approval, codex_approval["_context"]
    declared = m["_initialize"]["clientCapabilities"].get("elicitation") or {}
    assert "form" in declared and "url" in declared, m["_initialize"]
    assert (m["prompt"].get("result") or {}).get("stopReason") == "end_turn", ctx
    methods = [r["method"] for r in m["requests"]]
    assert methods == ["session/request_permission"], f"agent->client requests: {methods}\n{ctx}"
    assert not any(f.get("method") == "elicitation/create" for f in m["frames"]), ctx
    tool_call = m["requests"][0]["params"]["toolCall"]
    assert tool_call["kind"] == "execute" and "PROBE-RAN" in tool_call["rawInput"]["command"], ctx
    assert m["probe_ran"], "the allowed command did not run\n" + ctx
