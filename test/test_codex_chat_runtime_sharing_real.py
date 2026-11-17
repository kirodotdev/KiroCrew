"""codex's membership in ``ACP_BACKENDS_CHAT_RUNTIME_SHARING``, measured live.

Two dashboard chats on ONE codex-acp process is what chat runtime sharing does on
codex. The membership rests on two facts about the real adapter, and this test
measures both against the pinned ``codex-acp``:

1. Turns of different sessions run in parallel. One session runs a real shell
   command held open until the driver releases it; the other session's turn, sent
   while it is held, must be answered first. A shared chat must never wait for
   another chat.
2. The shared teardown keeps the record. A chat leaving a shared process is ended
   with the verb ``CodexHarness.teardown`` names, sent the way it says (a request,
   not a notification). After it the other session still answers, and
   ``session/load`` restores the departed one with its first turn, on the same
   process and on a restarted one.

A sibling of ``test_codex_session_mcp.py`` (at its line cap); the gate and the
driver runner are imported from there, so both files measure the same installed
adapter. The model is a loopback stub on 127.0.0.1, so no credential is needed.
Collected by the Real Adapter Contract Tests lane.
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

#: The lost-run deadline for every wait. Nothing is asserted against it: the slow
#: command is held on a release file the driver writes only after the fast chat has
#: answered, so the order is the measurement and no latency is compared to a limit.
DEADLINE_S = 300

_SHARE_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, entry, node, deadline_s, teardown_json = sys.argv[1:6]
deadline_s = int(deadline_s)
teardown = json.loads(teardown_json)
work = os.path.join(root, "work")
home = os.path.join(root, "codex_home")
release = os.path.join(root, "release-slow-command")
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
            log.append({"t": time.time(), "body": text})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode())
            self.wfile.flush()

        emit({"type": "response.created", "response": {"id": "resp_%d" % n}})
        req = json.loads(text) if text.startswith("{") else {}
        last = (req.get("input") or [{}])[-1]
        output = str(last.get("output"))
        running = last.get("type") == "function_call_output" and "Process running with session ID" in output
        if "SLOW-SESSION" in text and running:
            # exec_command yields while the command runs; keep waiting on it.
            psid = int(output.split("Process running with session ID ")[1].split()[0])
            emit({"type": "response.output_item.done", "item": {
                "type": "function_call", "id": "fc_%d" % n, "call_id": "call_%d" % n,
                "name": "write_stdin",
                "arguments": json.dumps({"session_id": psid, "chars": "", "yield_time_ms": 30000})}})
        elif "SLOW-SESSION" in text and "function_call_output" not in text:
            emit({"type": "response.output_item.done", "item": {
                "type": "function_call", "id": "fc_%d" % n, "call_id": "call_%d" % n,
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "i=0; while [ ! -f %s ] && [ $i -lt %d ]; do sleep 0.2; i=$((i+1)); done; "
                           "[ -f %s ] && echo SLEPT-OK" % (release, deadline_s * 5, release),
                    "yield_time_ms": 30000})}})
        else:
            emit({"type": "response.output_item.done", "item": {
                "type": "message", "role": "assistant", "id": "msg_%d" % n,
                "content": [{"type": "output_text", "text": "STUB-REPLY-%d" % n}]}})
        emit({"type": "response.completed", "response": {"id": "resp_%d" % n, "usage": USAGE}})


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
threading.Thread(target=server.serve_forever, daemon=True).start()
with open(os.path.join(home, "config.toml"), "w") as fh:
    fh.write('model = "gpt-5-codex"\nmodel_provider = "stub"\napproval_policy = "never"\n'
             'sandbox_mode = "danger-full-access"\n'
             '[model_providers.stub]\nname = "stub"\n'
             'base_url = "http://127.0.0.1:%d/v1"\nwire_api = "responses"\n'
             'env_key = "KIROCREW_STUB_MODEL_KEY"\n'
             'request_max_retries = 0\nstream_max_retries = 0\n' % server.server_address[1])
env = dict(os.environ, CODEX_HOME=home, NO_BROWSER="1", KIROCREW_STUB_MODEL_KEY="stub-key",
           CODEX_ACP_EXTERNAL_SANDBOX="true", DISABLE_MCP_CONFIG_FILTERING="true",
           # Under root too: an operator's own CODEX_SQLITE_HOME would otherwise take
           # this test's session records into their real Codex index.
           CODEX_SQLITE_HOME=os.path.join(root, "codex_sqlite"))


class Agent:
    def __init__(self):
        self.p = subprocess.Popen([node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1)
        self.frames, self.next, self.replies = [], 0, {}
        self.wlock, self.cv = threading.Lock(), threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _write(self, msg):
        with self.wlock:
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()

    def _pump(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self.cv:
                self.frames.append(msg)
                if "method" in msg and "id" in msg:
                    opts = (msg.get("params") or {}).get("options") or []
                    allow = next((o["optionId"] for o in opts if "allow" in o.get("kind", "")), None)
                    if msg["method"] == "session/request_permission" and allow:
                        ans = {"result": {"outcome": {"outcome": "selected", "optionId": allow}}}
                    else:
                        ans = {"error": {"code": -32601, "message": "n/a"}}
                    self._write(dict(ans, jsonrpc="2.0", id=msg["id"]))
                elif "id" in msg:
                    self.replies[msg["id"]] = msg
                self.cv.notify_all()

    def send(self, method, params, notification=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            with self.wlock:
                self.next += 1
                msg["id"] = self.next
        self._write(msg)
        return msg.get("id")

    def wait(self, rid, timeout=120):
        deadline = time.time() + timeout
        with self.cv:
            while rid not in self.replies and time.time() < deadline:
                self.cv.wait(0.5)
        return self.replies.get(rid, {"_timeout": True})

    def call(self, method, params, timeout=120):
        return self.wait(self.send(method, params), timeout)

    def reap(self):
        try:
            self.p.terminate()
            self.p.wait(15)
        except Exception:
            self.p.kill()


def text(s):
    return [{"type": "text", "text": s}]


def tool_started(agent, sid):
    return any(((f.get("params") or {}).get("update") or {}).get("sessionUpdate") == "tool_call"
               and (f.get("params") or {}).get("sessionId") == sid for f in agent.frames)


def recalled(tag, since):
    return any(tag in e["body"] and "FAST-SESSION" in e["body"] for e in log[since:])


out = {}
a = Agent()
try:
    a.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})
    new_s = a.call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new_s.get("error")
    s = (new_s.get("result") or {}).get("sessionId")
    f = (a.call("session/new", {"cwd": work, "mcpServers": []}).get("result") or {}).get("sessionId")
    out["sids"] = [s, f]
    if s and f:
        for sid in (s, f):
            a.call("session/set_mode", {"sessionId": sid, "modeId": "agent-full-access"})
        t0 = time.time()
        ps = a.send("session/prompt", {"sessionId": s, "prompt": text("SLOW-SESSION run the command")})
        while time.time() - t0 < deadline_s and not tool_started(a, s):
            time.sleep(0.2)
        out["slow_tool_started"] = tool_started(a, s)
        # The slow command is held on the release file, so it is still running
        # whatever this host's speed while the fast chat's turn goes through.
        rf = a.call("session/prompt", {"sessionId": f, "prompt": text("FAST-SESSION quick question")},
                    deadline_s)
        out["fast_stop"] = (rf.get("result") or {}).get("stopReason")
        out["slow_running_when_fast_answered"] = ps not in a.replies
        out["released_before_fast_answer"] = os.path.exists(release)
        open(release, "w").close()
        rs = a.wait(ps, deadline_s)
        out["slow_stop"] = (rs.get("result") or {}).get("stopReason")
        out["slept_ok"] = any("SLEPT-OK" in e["body"] for e in log)
        out["crosstalk"] = any("FAST-SESSION" in e["body"] and "SLOW-SESSION" in e["body"] for e in log)
        # The fast chat leaves the shared process with Crew's own teardown verb.
        rid = a.send(teardown["method"], {"sessionId": f}, notification=teardown["notification"])
        out["teardown_reply"] = a.wait(rid, 30) if rid is not None else None
        rs2 = a.call("session/prompt", {"sessionId": s, "prompt": text("SLOW-SESSION-AGAIN hello")})
        out["other_alive_after_teardown"] = (rs2.get("result") or {}).get("stopReason")
        out["load_same"] = a.call("session/load", {"sessionId": f, "cwd": work, "mcpServers": []})
        n0 = len(log)
        a.call("session/prompt", {"sessionId": f, "prompt": text("RECALL-SAME which session?")})
        out["recall_same"] = recalled("RECALL-SAME", n0)
finally:
    a.reap()
if out.get("sids") and all(out["sids"]):
    b = Agent()
    try:
        b.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})
        out["load_fresh"] = b.call("session/load", {"sessionId": f, "cwd": work, "mcpServers": []})
        n0 = len(log)
        b.call("session/prompt", {"sessionId": f, "prompt": text("RECALL-FRESH which session?")})
        out["recall_fresh"] = recalled("RECALL-FRESH", n0)
    finally:
        b.reap()
server.shutdown()
print(json.dumps(out))
"""


@pytest.mark.real_adapter
def test_real_codex_acp_two_chats_share_one_process():
    """ANTI-DRIFT GUARD for codex's membership in ``ACP_BACKENDS_CHAT_RUNTIME_SHARING``."""
    _require_codex_acp(with_node=True)
    policy = CodexHarness().teardown
    teardown = {"method": policy.method, "notification": policy.notification}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_sqlite").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        driver = root / "drive_share.py"
        driver.write_text(_SHARE_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [
                sys.executable,
                str(driver),
                str(root),
                str(_ENTRY),
                shutil.which("node") or "node",
                str(DEADLINE_S),
                json.dumps(teardown),
            ],
            timeout=DEADLINE_S * 3,  # flake-ok: lost-run backstop; only the order is asserted
            cwd=root / "work",
        )
    ctx = f"driver exit: {result.returncode}\nstdout: {result.stdout[-4000:]}\nstderr: {result.stderr[-3000:]}"
    try:
        m = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail("the codex-acp share driver produced no measurement\n" + ctx)
    # session/new makes no model call, so a refusal is a contract break, never a skip.
    assert all(m.get("sids") or []), f"session/new failed: {m.get('new_error')!r}\n{ctx}"

    # 1. parallel turns, by order alone: the slow command cannot finish until the
    # driver releases it, and the driver releases it only after the fast chat has
    # answered. A codex that serialized turns across sessions would never answer
    # the fast chat, and the fast prompt would hit the lost-run deadline instead.
    assert m["slow_tool_started"], "the slow session never started its command\n" + ctx
    assert m["fast_stop"] == "end_turn", (
        "the fast chat got no answer while the other chat's command was held: codex "
        "turns are serialized across sessions, so codex must leave "
        "ACP_BACKENDS_CHAT_RUNTIME_SHARING\n" + ctx
    )
    assert not m["released_before_fast_answer"], ctx
    assert m["slow_running_when_fast_answered"], (
        "the slow turn had already ended when the fast one was answered, so nothing "
        "was measured in parallel\n" + ctx
    )
    assert m["slow_stop"] == "end_turn" and m["slept_ok"], (
        "the held command did not finish after its release\n" + ctx
    )
    assert not m["crosstalk"], "one model request carried both chats\n" + ctx

    # 2. the shared teardown keeps the record.
    assert "result" in (m.get("teardown_reply") or {}), (
        f"the teardown verb {teardown!r} was not answered\n" + ctx
    )
    assert m["other_alive_after_teardown"] == "end_turn", (
        "the other chat stopped answering after one chat left the process\n" + ctx
    )
    assert "result" in (m.get("load_same") or {}), (
        "session/load after the shared teardown failed on the same process, so the "
        "teardown disposed the record\n" + ctx
    )
    assert m["recall_same"], "the reloaded chat lost its first turn\n" + ctx
    assert "result" in (m.get("load_fresh") or {}), (
        "session/load from a restarted adapter failed after the shared teardown\n" + ctx
    )
    assert m["recall_fresh"], "the chat reloaded on a fresh process lost its first turn\n" + ctx
