"""codex's own OS sandbox, measured live with nothing of Crew's wrapped around it.

The Real Adapter Contract Tests lane runs ``codex-acp`` outside Crew's sandbox, which
is how a codex session is spawned once codex is a member of
``ACP_BACKENDS_INTERNAL_SANDBOX``: codex's own sandbox (bubblewrap on Linux) is then
the only OS confinement, and Crew's approval gate is still the only thing that
answers an ask. This module measures both halves in one session, in the mode Crew
writes (``ACP_BACKEND_PERMISSION_CONFIG``: ``read-only``, approval ``on-request``):

* an ordinary ``git status`` runs inside codex's sandbox without asking -- the thing
  a nested sandbox broke;
* an escalated command and a command asking for network access each raise
  ``session/request_permission``; a deny means the command does not run, an
  ``allow_once`` means it does.

Every ask is read by Crew's own builder (``build_permission_event``), judged by
Crew's own floor (``permission_floor.refusal_for``), and answered with the option id
Crew's ``approve_tool`` / ``reject_tool`` would send -- the ``once`` / ``reject``
entries of the options map the builder records. So the answer on the wire is the one
Crew's gate picks, not a hand-written id. Collected by the Real Adapter Contract
Tests lane like its siblings.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from test_codex_session_mcp import _ENTRY, _require_codex_acp, _run_driver_reaping_group

from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX
from kiro_crew.agent_sdk.backends_routing import ACP_BACKEND_PERMISSION_CONFIG
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: One step per prompt: the shell call the stub model makes, and Crew's answer to
#: any ask it raises (``None`` = the step must not ask at all).
_STEPS = {
    "git": {"cmd": "git status --short --branch", "perm": "use_default", "answer": None},
    "esc-deny": {
        "cmd": "echo ran > esc-denied.txt",
        "perm": "require_escalated",
        "answer": "deny",
    },
    "esc-allow": {
        "cmd": "echo ran > esc-allowed.txt",
        "perm": "require_escalated",
        "answer": "allow",
    },
    "net-deny": {"cmd": "NET", "perm": "with_additional_permissions", "answer": "deny"},
    "net-allow": {"cmd": "NET", "perm": "with_additional_permissions", "answer": "allow"},
}

_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, entry, node, initialize_json, steps_json, mode_json = sys.argv[1:7]
initialize, steps, (mode_option, mode_value) = (
    json.loads(initialize_json), json.loads(steps_json), json.loads(mode_json))
work = os.path.join(root, "work")
USAGE = {"input_tokens": 100, "input_tokens_details": None, "output_tokens": 5,
         "output_tokens_details": None, "total_tokens": 105}

from kiro_crew import permission_floor
from kiro_crew.acp._dispatch import build_permission_event
from kiro_crew.acp.types import JsonRpcMessage

port, tool_results = [0], {}


def net_cmd():
    # Reaches the loopback stub only when the command is given network access:
    # codex's read-only sandbox runs a command in a network namespace of its own.
    return (sys.executable + " -c \"import urllib.request,sys;"
            "sys.stdout.write('NET-%d' % urllib.request.urlopen("
            "'http://127.0.0.1:" + str(port[0]) + "/v1/models', timeout=10).status)\"")


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
        try:
            items = json.loads(text).get("input") or []
        except ValueError:
            items = []
        last = items[-1] if items else {}
        step = ""
        for item in reversed(items):
            if item.get("role") == "user":
                content = item.get("content")
                blob = json.dumps(content)
                for name in steps:
                    if "STEP:" + name + "." in blob:
                        step = name
                        break
                break
        if step and last.get("type") == "function_call_output":
            # What codex handed the model back: the one place a sandbox that
            # failed to start is reported when no terminal frame is emitted.
            tool_results[step] = str(last.get("output"))[:2000]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode())
            self.wfile.flush()

        emit({"type": "response.created", "response": {"id": "resp"}})
        if step and last.get("type") != "function_call_output":
            spec = steps[step]
            args = {"cmd": net_cmd() if spec["cmd"] == "NET" else spec["cmd"],
                    "sandbox_permissions": spec["perm"],
                    "justification": "probe: " + step}
            if spec["perm"] == "with_additional_permissions":
                args["additional_permissions"] = {"network": {"enabled": True}}
            emit({"type": "response.output_item.done", "item": {
                "type": "function_call", "id": "fc_" + step, "call_id": "call_" + step,
                "name": "exec_command", "arguments": json.dumps(args)}})
        else:
            emit({"type": "response.output_item.done", "item": {
                "type": "message", "role": "assistant", "id": "msg",
                "content": [{"type": "output_text", "text": "STUB-REPLY"}]}})
        emit({"type": "response.completed", "response": {"id": "resp", "usage": USAGE}})


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
port[0] = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
with open(os.path.join(root, "codex_home", "config.toml"), "w") as fh:
    fh.write('model = "gpt-5-codex"\nmodel_provider = "stub"\n'
             '[features]\nexec_permission_approvals = true\n'
             '[model_providers.stub]\nname = "stub"\n'
             'base_url = "http://127.0.0.1:%d/v1"\nwire_api = "responses"\n'
             'env_key = "KIROCREW_STUB_MODEL_KEY"\n'
             'request_max_retries = 0\nstream_max_retries = 0\n' % port[0])
subprocess.run(["git", "init", "-q", work], check=True)
# codex's bubblewrap stages mount targets under TMPDIR; keep them in this run's
# own directory so the measurement leaves nothing behind in the shared one.
child_tmp = os.path.join(root, "tmp")
os.makedirs(child_tmp, exist_ok=True)
env = dict(os.environ, CODEX_HOME=os.path.join(root, "codex_home"), NO_BROWSER="1",
           KIROCREW_STUB_MODEL_KEY="stub-key", TMPDIR=child_tmp)

p = subprocess.Popen([node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1)
q, frames, asks, counter, current = queue.Queue(), [], [], [0], [""]


def pump():
    for line in p.stdout:
        q.put(line)
    q.put(None)


threading.Thread(target=pump, daemon=True).start()


def write(msg):
    p.stdin.write(json.dumps(dict(msg, jsonrpc="2.0")) + "\n")
    p.stdin.flush()


def answer_permission(msg):
    # Crew's own reading of the ask, Crew's own floor, and the option id Crew's
    # approve_tool / reject_tool would echo from the recorded options map.
    event, recorded = build_permission_event(
        JsonRpcMessage(id=msg["id"], method=msg["method"], params=msg.get("params")),
        harness_backend="codex")
    floor = permission_floor.refusal_for(event) if event is not None else "no event"
    want = steps.get(current[0], {}).get("answer")
    recorded = recorded or {}
    if want == "allow" and floor is None and recorded.get("once"):
        outcome = {"outcome": "selected", "optionId": recorded["once"]}
    elif recorded.get("reject"):
        outcome = {"outcome": "selected", "optionId": recorded["reject"]}
    else:
        outcome = {"outcome": "cancelled"}
    asks.append({"step": current[0], "want": want, "floor": floor, "recorded": recorded,
                 "event_kind": getattr(event, "tool_kind", None) if event else None,
                 "title": getattr(event, "title", None) if event else None,
                 "params": msg.get("params"), "sent": outcome})
    write({"id": msg["id"], "result": {"outcome": outcome}})


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
        frames.append(dict(msg, _step=current[0]))
        if "method" in msg and "id" in msg:
            if msg["method"] == "session/request_permission":
                answer_permission(msg)
            else:
                write({"id": msg["id"], "error": {"code": -32601, "message": "n/a"}})
        elif done(msg):
            return msg
    return {"_timeout": True}


def call(method, params, timeout=60):
    counter[0] += 1
    rid = counter[0]
    write({"id": rid, "method": method, "params": params})
    return until(lambda m: m.get("id") == rid and "method" not in m, timeout)


out = {"turns": {}}
try:
    out["init"] = call("initialize", initialize)
    new = call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new.get("error")
    sid = (new.get("result") or {}).get("sessionId")
    out["sid"] = sid
    if sid:
        # Crew's own routing write: the SESSION_CONFIG option and value it requires.
        out["routing"] = call("session/set_config_option",
                              {"sessionId": sid, "configId": mode_option, "value": mode_value})
        for name in steps:
            current[0] = name
            out["turns"][name] = call("session/prompt", {"sessionId": sid, "prompt": [
                {"type": "text", "text": "STEP:" + name + ". Run the command."}]}, 180)
        current[0] = ""
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
server.shutdown()
out["asks"] = asks
out["tool_results"] = tool_results
out["frames"] = frames
out["files"] = sorted(os.listdir(work))
print(json.dumps(out, default=str))
"""


@pytest.fixture(scope="module")
def codex_inner_sandbox() -> dict:
    """One session in Crew's routing mode; one prompt per entry of ``_STEPS``."""
    from kiro_crew.acp.client_start import CLIENT_NAME, CLIENT_VERSION

    _require_codex_acp(with_node=True)
    if not shutil.which("git"):
        pytest.fail("git is not on PATH; the lane's runner image always carries it")
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
        (root / "kirocrew_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        driver = root / "drive_inner_sandbox.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            # The floor reads the live config; an empty data home is the default one.
            mp.setenv("KIROCREW_HOME", str(root / "kirocrew_home"))
            result = _run_driver_reaping_group(
                [
                    sys.executable,
                    str(driver),
                    str(root),
                    str(_ENTRY),
                    shutil.which("node") or "node",
                    json.dumps(params),
                    json.dumps(_STEPS),
                    json.dumps(list(ACP_BACKEND_PERMISSION_CONFIG[ACP_BACKEND_CODEX])),
                ],
                timeout=900,  # flake-ok: outer wall bound for a live adapter run, below pytest's 1200 s
                cwd=root / "work",
            )
    try:
        m = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail(f"the driver produced no measurement\nstderr: {result.stderr[-3000:]}")
    assert m.get(
        "sid"
    ), f"session/new failed: {m.get('new_error')!r}\nstderr: {result.stderr[-3000:]}"
    m["_context"] = json.dumps({k: v for k, v in m.items() if k != "frames"}, default=str)[-8000:]
    return m


def _asks(m: dict, step: str) -> list[dict]:
    return [a for a in m["asks"] if a["step"] == step]


def _output(m: dict, step: str) -> tuple[str, int | None]:
    """The output and exit code codex handed back to the model for *step*'s call.

    Read from the function-call output rather than ACP terminal frames: a call run
    inside codex's sandbox reports only there, including a sandbox that failed to
    start (``bwrap: ...``). ``(\"\", None)`` when the command never ran.
    """
    result = (m.get("tool_results") or {}).get(step, "")
    found = re.search(r"Process exited with code (-?\d+)", result)
    return result, int(found.group(1)) if found else None


def _ended(m: dict, step: str) -> bool:
    return ((m["turns"].get(step) or {}).get("result") or {}).get("stopReason") == "end_turn"


#: Asked of a throwaway child, so this process keeps its own namespaces.
_USERNS_PROBE = "import os; os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)"


@functools.lru_cache(maxsize=1)
def _unprivileged_userns() -> bool:
    """Whether this host lets an unprivileged process build codex's sandbox.

    On Linux codex's sandbox is bubblewrap, which needs a new user and mount
    namespace. Many hosts turn that off (a sysctl, an AppArmor restriction, a
    container's seccomp profile), and then every command codex runs inside its
    own sandbox fails before it starts. Off Linux the sandbox is not bubblewrap.
    """
    if not sys.platform.startswith("linux"):
        return True
    probe = subprocess.run(  # noqa: S603 - this interpreter, a fixed program
        [sys.executable, "-c", _USERNS_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    return probe.returncode == 0


def _assert_sandboxed_step(m: dict, step: str, ran: str) -> None:
    """A step that runs inside codex's own sandbox: it ran, or the host forbids one.

    With unprivileged user namespaces on, the step must run (*ran* in its output,
    exit 0). With them off it must fail the way codex reports a sandbox it could
    not build -- recorded rather than skipped, because the lane counts a skip as
    a contract it did not measure, and this IS the measurement on such a host.
    """
    text, exit_code = _output(m, step)
    context = f"{step}: exit={exit_code!r} out={text!r}\n{m['_context']}"
    if _unprivileged_userns():
        assert exit_code == 0 and ran in text, context
    else:
        assert exit_code not in (0, None) and "bwrap:" in text, context


@pytest.mark.real_adapter
def test_real_codex_acp_routing_write_is_accepted(codex_inner_sandbox):
    """Crew's SESSION_CONFIG write lands; every step below runs in that mode."""
    m = codex_inner_sandbox
    assert ACP_BACKEND_PERMISSION_CONFIG[ACP_BACKEND_CODEX] == ("mode", "read-only")
    assert "error" not in (m.get("routing") or {"error": "no answer"}), m["_context"]


@pytest.mark.real_adapter
def test_real_codex_acp_git_status_runs_inside_its_own_sandbox(codex_inner_sandbox):
    """``git status`` runs with no ask: codex's own sandbox starts and lets it read."""
    m = codex_inner_sandbox
    assert _ended(m, "git"), m["_context"]
    assert _asks(m, "git") == [], m["_context"]
    _assert_sandboxed_step(m, "git", "##")


@pytest.mark.real_adapter
@pytest.mark.parametrize("step", ["esc-deny", "esc-allow", "net-deny", "net-allow"])
def test_real_codex_acp_asks_reach_crews_gate(codex_inner_sandbox, step):
    """Each escalated or network step asks once; Crew's builder reads it as a call it
    can judge, the floor lets an allow through, and the id sent is Crew's own."""
    m = codex_inner_sandbox
    asks = _asks(m, step)
    assert len(asks) == 1, f"{step}: {len(asks)} asks\n{m['_context']}"
    ask = asks[0]
    assert ask["title"], ask
    assert ask["recorded"].get("once") and ask["recorded"].get("reject"), ask
    if _STEPS[step]["answer"] == "allow":
        assert ask["floor"] is None, ask
        assert ask["sent"] == {"outcome": "selected", "optionId": ask["recorded"]["once"]}, ask
    else:
        assert ask["sent"] == {"outcome": "selected", "optionId": ask["recorded"]["reject"]}, ask


@pytest.mark.real_adapter
def test_real_codex_acp_escalation_runs_only_when_crew_allows(codex_inner_sandbox):
    m = codex_inner_sandbox
    assert "esc-denied.txt" not in m["files"], m["_context"]
    assert "esc-allowed.txt" in m["files"], m["_context"]


@pytest.mark.real_adapter
def test_real_codex_acp_network_runs_only_when_crew_allows(codex_inner_sandbox):
    m = codex_inner_sandbox
    denied, _ = _output(m, "net-deny")
    assert "NET-200" not in denied, f"denied network step ran: {denied!r}\n{m['_context']}"
    _assert_sandboxed_step(m, "net-allow", "NET-200")
