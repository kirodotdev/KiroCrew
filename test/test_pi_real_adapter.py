"""pi driven for real, through Kiro Crew's own launch: the pi half of the Real Adapter lane.

pi reaches Crew as TWO third-party packages -- the ``pi-acp`` adapter that serves
ACP, and the ``pi`` coding agent it spawns -- and every control Crew has over a pi
session rests on facts about them that nothing on Crew's side can assert:

- the adapter runs the executable ``PI_ACP_PI_COMMAND`` names, which is how Crew's
  gate launcher (and through it the gate extension) gets into the process at all;
- pi lists an extension's commands in its ``get_commands`` registry, with the file
  each came from, which is the read-back that proves the SEALED copy loaded;
- the adapter forwards the extension's confirm dialog as
  ``session/request_permission``, which is the only way a pi tool call ever asks;
- pi's own MCP support registers the servers the extension hands it from the
  per-session handoff file, because the adapter drops the ``session/new`` array;
- the adapter's ``/compact`` runs pi's compaction inside the prompt turn.

A pi or pi-acp release that breaks any one of them breaks pi sessions with nothing
red to say so -- a session whose gate never loads is refused before it starts, and
one whose dialog is never forwarded runs every tool call unasked if it starts at all.
So this module drives the PINNED pair (``test/real_adapters/``) end to end and
asserts each fact.

What is Crew's and what is a stand-in
-------------------------------------
The launch is Crew's: ``PiLaunch.resolve_spawn`` resolves both executables, checks
pi's version floor, verifies and seals the gate extension, writes the encrypted MCP
handoff, writes the gate launcher and runs the gate read-back, exactly as a real
session's spawn does; ``apply_spawn_env`` then names the launcher, the nonce and the
handoff on the child's environment. The ``initialize`` request is Crew's own
(``AcpClient._initialize_params``), and every permission frame is read by Crew's own
builder (``build_permission_event``) under the nonce the launch issued.

Three things are stand-ins, each for a reason:

- the OS sandbox: the preflight and the argv wraps (the gate read-back's, and the
  one the Node engine probe confines each ``node --version`` in) are replaced by
  identity, because the lane's runners (and this repository's developer hosts) may
  have no user namespaces, and the facts measured here are the adapter's and the
  agent's, not the sandbox's. The sandbox's own suites cover the mask;
- the model: a loopback OpenAI-compatible endpoint named in pi's own ``models.json``,
  scripted to call one tool per turn, so the lane needs no credential;
- the MCP server: a stdio stub that logs what it is asked, mounted through the
  session's own MCP composition seam (``_session_mcp_servers``).

The driver speaks ACP to the adapter itself rather than through ``AcpClient``'s
turn loop: what is measured is the adapter's wire, and the dispatch code that reads
it is asserted separately against the frame corpus.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from real_adapter_gate import (
    MEASURED_PI_ACP_VERSION,
    MEASURED_PI_VERSION,
    PI_ACP_PACKAGE,
    PI_PACKAGE,
    require_real_adapter,
)

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp._dispatch import build_permission_event
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_start import PROTOCOL_VERSION_PI
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_PI, gate_probe_command_for
from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

_INSTALL = f"npm ci in test/real_adapters (pins {PI_ACP_PACKAGE} and {PI_PACKAGE})"

# The prompts the scripted model keys on. Each names exactly one tool call.
_BASH = "RUN-BASH: echo the gate check"
_BASH_DENIED = "RUN-BASH-DENIED: write the denied marker"
_MCP = "CALL-MCP: echo ping through the probe server"
_BASH_COMMAND = "echo pi-gate-ran"
_DENIED_FILE = "denied-marker.txt"
_PROBE_SERVER = "probe-server"
# pi 0.99.2+ spells a server's tools with every character outside [A-Za-z0-9_] as _.
_PROBE_TOOL = "mcp__probe_server__echo"
# ...and Crew's permission builder puts the title back in the server's own
# spelling, so deny and governance rules written against the server bind.
_CREW_TITLE = "mcp__probe-server__echo"


# ── the scripted model ──────────────────────────────────────────────────────


class _LoopbackServer(ThreadingHTTPServer):
    """The model's listener. Reached by port, never by name, so the bind skips the
    ``socket.getfqdn`` lookup ``http.server`` makes, which can stall on some hosts."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Model(BaseHTTPRequestHandler):
    """An OpenAI chat-completions endpoint that calls one tool per scripted prompt.

    The LAST message decides the reply: a tool result is acknowledged in text, a
    user message that starts with one of the scripted prompts is answered with that
    prompt's tool call (only when the request offers the tool, so a compaction
    request -- or a pi that stopped offering it -- is never handed one), and
    anything else is answered with text. Every request body is logged, which is how
    a test sees what pi actually sent its model, tool results included.
    """

    log: list[dict]
    lock: threading.Lock

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
        self._send(200, "application/json", json.dumps({"object": "list", "data": []}).encode())

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(text)
        except ValueError:
            body = {}
        with self.lock:
            self.log.append({"path": self.path, "body": body})
            n = len(self.log)
        delta = self._reply(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        for chunk in (
            {"choices": [{"index": 0, "delta": {"role": "assistant", **delta}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": len(text) // 4,
                    "completion_tokens": 8,
                    "total_tokens": len(text) // 4 + 8,
                },
            },
        ):
            frame = {
                "id": f"chatcmpl-{n}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "stub-model",
                **chunk,
            }
            self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _reply(self, body: dict) -> dict:
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        offered = {(tool.get("function") or {}).get("name") for tool in body.get("tools") or []}
        if last.get("role") == "tool":
            return {"content": "TOOL-RESULT-SEEN"}
        said = _text_of(last.get("content")).strip()
        script = {
            _BASH: ("bash", {"command": _BASH_COMMAND}),
            _BASH_DENIED: ("bash", {"command": f"echo should-not-run > {_DENIED_FILE}"}),
            _MCP: (_PROBE_TOOL, {"text": "ping"}),
        }
        for prompt, (tool, args) in script.items():
            if said.startswith(prompt) and tool in offered:
                return {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{tool}_{len(messages)}",
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(args)},
                        }
                    ]
                }
        return {"content": "STUB-REPLY: nothing to run."}

    def _send(self, status: int, ctype: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


# ── the stub MCP server ─────────────────────────────────────────────────────

_STUB_MCP = r"""
import json, sys
log = open(sys.argv[1], "a", encoding="utf-8")
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    log.write(json.dumps({"method": method, "params": msg.get("params")}) + "\n")
    log.flush()
    if "id" not in msg:
        continue
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo the text back.",
                             "inputSchema": {"type": "object",
                                             "properties": {"text": {"type": "string"}},
                                             "required": ["text"]}}]}
    elif method == "tools/call":
        text = ((msg.get("params") or {}).get("arguments") or {}).get("text", "")
        result = {"content": [{"type": "text", "text": "ECHO:" + text}]}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": "n/a"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
"""


# ── the ACP driver ──────────────────────────────────────────────────────────


class _Adapter:
    """pi-acp over stdio, with every permission frame read by Crew's own builder."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stderr: Path,
        nonce: str,
        servers: tuple[str, ...],
        answers: dict[str, str],
    ) -> None:
        self._stderr = stderr.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(  # noqa: S603 - the plan Crew's launch resolved
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next = 0
        self._nonce = nonce
        self._servers = servers
        self._answers = answers
        self.frames: list[dict] = []
        self.permissions: list[dict] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _write(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def _answer_permission(self, msg: dict) -> None:
        event, _recorded = build_permission_event(
            JsonRpcMessage(id=msg["id"], method=msg["method"], params=msg.get("params")),
            gate_envelope_nonce=self._nonce,
            harness_backend=ACP_BACKEND_PI,
            harness_mcp_servers=self._servers,
        )
        title = event.title if event is not None else ""
        option = self._answers.get(title, "no")
        self.permissions.append(
            {
                "frame_index": len(self.frames) - 1,
                "title": title,
                "tool_input": event.tool_input if event is not None else "",
                "answered": option,
            }
        )
        self._write(
            {"id": msg["id"], "result": {"outcome": {"outcome": "selected", "optionId": option}}}
        )

    def until(self, done, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                return {"_eof": True, "exit": self.proc.poll()}
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.frames.append(msg)
            if "method" in msg and "id" in msg:
                if msg["method"] == "session/request_permission":
                    self._answer_permission(msg)
                else:
                    self._write(
                        {"id": msg["id"], "error": {"code": -32601, "message": "not offered"}}
                    )
            elif done(msg):
                return msg
        return {"_timeout": True}

    def call(self, method: str, params: dict, timeout: float = 120) -> dict:
        self._next += 1
        rid = self._next
        self._write({"id": rid, "method": method, "params": params})
        return self.until(lambda m: m.get("id") == rid and "method" not in m, timeout)

    def commands_advertised(self, timeout: float = 60) -> dict:
        """Wait for the ``available_commands_update`` pi-acp sends after ``session/new``.

        The adapter sends it once the session's own ``get_commands`` answers, so it
        is the signal the session is ready -- extensions loaded, MCP servers handed
        over -- rather than a sleep.
        """
        return self.until(
            lambda m: ((m.get("params") or {}).get("update") or {}).get("sessionUpdate")
            == "available_commands_update",
            timeout,
        )

    def close(self) -> None:
        for step in (self.proc.terminate, self.proc.kill):
            try:
                step()
                self.proc.wait(timeout=15)
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break
        self._stderr.close()


# ── resolution, through Crew's own ladders ──────────────────────────────────


def _resolved_pair() -> tuple[list[str] | None, str | None]:
    """The adapter argv and the pi executable Crew would spawn, or ``None`` for either.

    Crew's own resolvers, so the lane's ``PI_ACP_BIN`` / ``PI_ACP_PI_COMMAND`` are
    honoured exactly as a session honours them. A pi below the harness's floor counts
    as absent: it is not a release this lane measures, and the launch refuses it.
    """
    argv, _search = pi_mod._resolve_pi_acp_bin()
    pi_bin, _search = pi_mod._resolve_pi_bin()
    if pi_bin and pi_mod._pi_version_issue(pi_bin):
        pi_bin = None
    return argv, pi_bin


def _require_pi() -> tuple[list[str], str]:
    argv, pi_bin = _resolved_pair()
    require_real_adapter(argv, what=PI_ACP_PACKAGE, install=_INSTALL)
    floor = ".".join(str(part) for part in pi_mod.PI_MIN_VERSION)
    require_real_adapter(pi_bin, what=f"{PI_PACKAGE} {floor} or newer", install=_INSTALL)
    assert argv is not None and pi_bin is not None
    return argv, pi_bin


# ── the drive ───────────────────────────────────────────────────────────────


def _write_pi_config(agent_dir: Path, port: int) -> None:
    """pi's own config: one provider on the loopback model, quiet, offline."""
    agent_dir.mkdir(parents=True)
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "stub": {
                        "baseUrl": f"http://127.0.0.1:{port}/v1",
                        "api": "openai-completions",
                        "apiKey": "stub-key-not-a-credential",
                        "models": [
                            {
                                "id": "stub-model",
                                "name": "Stub",
                                "contextWindow": 128000,
                                "maxTokens": 4096,
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (agent_dir / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "stub",
                "defaultModel": "stub-model",
                "quietStartup": True,
                # A short scripted session is "too small" to compact under the default
                # 20k-token tail; a tiny tail lets /compact summarise what came before.
                "compaction": {"keepRecentTokens": 1},
            }
        ),
        encoding="utf-8",
    )


def _child_env(client: AcpClient) -> dict[str, str]:
    """The environment the adapter child gets, built by the bindings a spawn uses.

    The session's overlay, the spawn-env repair and the agent-subprocess scrub, then
    the launch tail's PATH augmentation -- the same steps ``_verify_pi_gate`` takes
    for the read-back, so the adapter, the read-back and this drive resolve the same
    ``node`` and the same ``pi``.
    """
    from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

    env = pi_mod.scrub_agent_subprocess_env(
        _resolve_spawn_env(
            {**os.environ, **client._agent_env_for_spawn(), **client._extra_env},
            kiro_api_key=False,
        )
    )
    env["PATH"] = augmented_path(env.get("PATH", ""))
    return env


def _readback_diagnosis(client: AcpClient, launcher: str, work: Path) -> str:
    """pi's own answer to the read-back, for a refusal that only names the fault class."""
    if not launcher:
        return "the launch refused before it wrote a gate launcher"
    env = _child_env(client)
    env["PI_OFFLINE"] = "1"
    node = shutil.which("node", path=env["PATH"])
    try:
        version = subprocess.run(  # noqa: S603 - the node the child would run
            [node or "node", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        ).stdout.strip()
        completed = subprocess.run(  # noqa: S603 - Crew's own launcher
            [launcher, *pi_mod._PI_RPC_ARGS],
            input=json.dumps(pi_mod._PI_READBACK_REQUEST) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=work,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the read-back could not be re-run: {exc}"
    return (
        f"node on the child's PATH: {node} ({version}); read-back exit {completed.returncode}\n"
        f"stderr: {completed.stderr[-2500:]}\nstdout: {completed.stdout[-1000:]}"
    )


def _drive(root: Path) -> dict:
    """Launch the pinned pair through ``PiLaunch`` and drive one scripted session."""
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    # The children's own scratch (node's compile cache, pi's jiti cache), under this
    # drive's root so nothing outlives it.
    child_tmp = root / "tmp"
    child_tmp.mkdir()
    # The node this lane installed, first on the child's PATH. pi's bin is a
    # `#!/usr/bin/env node` script and pi needs Node 22.19+; the launch tail's PATH
    # augmentation puts well-known directories (``/usr/local/bin`` among them) ahead
    # of the inherited PATH, and a runner image's system node there can be older.
    # ``~/.local/bin`` leads that list, and HOME is this drive's own home.
    node = shutil.which("node")
    if node:
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "node").symlink_to(node)
    mcp_log = root / "mcp-calls.jsonl"
    stub_mcp = root / "stub_mcp.py"
    stub_mcp.write_text(_STUB_MCP, encoding="utf-8")
    element = {
        "name": _PROBE_SERVER,
        "command": sys.executable,
        "args": [str(stub_mcp), str(mcp_log)],
        "env": [],
    }

    model_log: list[dict] = []
    handler = type("Model", (_Model,), {"log": model_log, "lock": threading.Lock()})
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent_dir = root / "agent"
    _write_pi_config(agent_dir, server.server_address[1])

    isolated = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "TMPDIR": str(child_tmp),
    }
    out: dict = {"root": str(root)}
    adapter: _Adapter | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KIROCREW_HOME", str(root / "kirocrew-home"))
        for name, value in isolated.items():
            mp.setenv(name, value)

        # The OS sandbox is the one layer stood in for (see the module docstring).
        async def _no_preflight(_preflight, _backend, _mode):
            return ()

        async def _unwrapped(argv, **_kwargs):
            return list(argv), None

        def _unwrapped_sync(argv, **_kwargs):
            return list(argv), None

        mp.setattr(launch_mod, "_run_preflight_bounded", _no_preflight)
        # Both spellings of the wrap: the read-back's async one, and the sync one
        # the Node engine probe (``_pi_probe_wrap``) confines each ``node`` child in.
        mp.setattr(pi_mod, "wrap_argv_async", _unwrapped)
        mp.setattr(pi_mod, "wrap_argv", _unwrapped_sync)
        forget_cached_resolution(ACP_BACKEND_PI)
        try:
            client = AcpClient(work_dir=work, acp_backend=ACP_BACKEND_PI, extra_env=isolated)
            # The session's own MCP composition seam, holding the probe server.
            mp.setattr(client, "_session_mcp_servers", lambda: [dict(element)])
            mp.setattr(client, "_pooled_mcp_servers", lambda: [])
            launch = pi_mod.PiLaunch()
            try:
                plan = asyncio.run(
                    launch.resolve_spawn(
                        SpawnContext(
                            agent="",
                            work_dir=work,
                            model=None,
                            environ=dict(os.environ),
                            home=home,
                            sandbox_mode="auto",
                            session=client,
                        )
                    )
                )
            except pi_mod.AcpToolGateUnroutable as exc:
                # The launch's refusal names the fault class, not pi's own words; a
                # red lane needs those, so ask the same launcher once more and say.
                pytest.fail(f"{exc}\n{_readback_diagnosis(client, launch._launcher, work)}")
            nonce = client._pi_gate_nonce
            servers = tuple(client._pi_mcp_server_names)
            out.update(
                argv=plan.argv,
                nonce=nonce,
                servers=servers,
                launcher=launch._launcher,
                handoff=launch._mcp_handoff,
                sealed=pi_mod._seal_pi_gate_extension(),
                initialize=client._initialize_params(),
            )
            env = _child_env(client)
            launch.apply_spawn_env(env)
            out["env_launcher"] = env.get("PI_ACP_PI_COMMAND")
            # pi's registry, asked again through the launcher the session runs, so
            # the read-back's answer is on the record (the launch refused nothing).
            readback = subprocess.run(  # noqa: S603 - Crew's own launcher
                [launch._launcher, *pi_mod._PI_RPC_ARGS],
                input=json.dumps(pi_mod._PI_READBACK_REQUEST) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=work,
                timeout=60,
                check=False,
            )
            out["registry"] = pi_mod._pi_commands_from_readback(readback.stdout)
            out["registry_stderr"] = readback.stderr[-2000:]

            adapter = _Adapter(
                plan.argv,
                cwd=work,
                env=env,
                stderr=root / "pi-acp.stderr",
                nonce=nonce,
                servers=servers,
                answers={"bash": "yes", _CREW_TITLE: "yes"},
            )
            out["init"] = adapter.call("initialize", out["initialize"])
            # The read-backs above each started a pi, and pi's extension consumes
            # the handoff file it reads; the client republishes it before every
            # request that starts one, exactly as here.
            asyncio.run(client._republish_pi_mcp_handoff())
            out["handoff_republished"] = Path(launch._mcp_handoff).exists()
            new = adapter.call("session/new", {"cwd": str(work), "mcpServers": [element]})
            out["new"] = new
            asyncio.run(client._discard_pi_mcp_handoff())
            sid = (new.get("result") or {}).get("sessionId")
            if sid:
                out["advertised"] = adapter.commands_advertised()

                def prompt(text: str) -> dict:
                    mark, before = len(adapter.frames), len(model_log)
                    reply = adapter.call(
                        "session/prompt",
                        {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                        timeout=180,
                    )
                    return {
                        "reply": reply,
                        "frames": adapter.frames[mark:],
                        "requests": model_log[before:],
                    }

                out["bash"] = prompt(_BASH)
                # The same tool, refused: the command must not run.
                adapter._answers = {"bash": "no", _CREW_TITLE: "yes"}
                out["bash_denied"] = prompt(_BASH_DENIED)
                adapter._answers = {"bash": "yes", _CREW_TITLE: "yes"}
                out["mcp"] = prompt(_MCP)
                out["compact"] = prompt("/compact")
                out["after_compact"] = prompt("Say anything.")
        finally:
            if adapter is not None:
                adapter.close()
                out["frames"] = adapter.frames
                out["permissions"] = adapter.permissions
            server.shutdown()
            forget_cached_resolution(ACP_BACKEND_PI)
    out["denied_file_exists"] = (work / _DENIED_FILE).exists()
    out["mcp_calls"] = [
        json.loads(line)
        for line in (mcp_log.read_text(encoding="utf-8").splitlines() if mcp_log.exists() else [])
    ]
    stderr = root / "pi-acp.stderr"
    out["stderr"] = stderr.read_text(encoding="utf-8")[-3000:] if stderr.exists() else ""
    return out


@pytest.fixture(scope="module")
def pi_session() -> dict:
    """One driven session of the pinned pair, shared by the measurements below."""
    _require_pi()
    with tempfile.TemporaryDirectory(prefix="kc-pi-real-", ignore_cleanup_errors=True) as w:
        drive = _drive(Path(w))
    drive["_context"] = _context(drive)
    assert (drive.get("new") or {}).get("result", {}).get("sessionId"), (
        "session/new failed\n" + drive["_context"]
    )
    return drive


def _context(drive: dict) -> str:
    """What a red lane needs to read: each turn's answer and tools, not the prompts."""
    turns = {}
    for name in ("bash", "bash_denied", "mcp", "compact", "after_compact"):
        turn = drive.get(name) or {}
        turns[name] = {
            "reply": turn.get("reply"),
            "permission_frames": len(_permission_frames(turn.get("frames") or [])),
            "tools_offered": sorted(
                {
                    (tool.get("function") or {}).get("name", "")
                    for request in turn.get("requests") or []
                    for tool in (request.get("body") or {}).get("tools") or []
                }
            ),
            "tool_results": [r[:200] for r in _tool_results(turn.get("requests") or [])],
        }
    summary = {
        "init": drive.get("init"),
        "new": drive.get("new"),
        "turns": turns,
        "permissions": drive.get("permissions"),
        "mcp_calls": [call.get("method") for call in drive.get("mcp_calls") or []],
        "registry": drive.get("registry"),
        "pi_acp_stderr": drive.get("stderr"),
    }
    return json.dumps(summary, default=str, indent=1)[-8000:]


def _updates(frames: list[dict]) -> list[dict]:
    return [
        (f.get("params") or {}).get("update") or {}
        for f in frames
        if f.get("method") == "session/update"
    ]


def _permission_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("method") == "session/request_permission"]


def _tool_results(requests: list[dict]) -> list[str]:
    return [
        _text_of(message.get("content"))
        for request in requests
        for message in (request.get("body") or {}).get("messages") or []
        if message.get("role") == "tool"
    ]


# ── the measurements ────────────────────────────────────────────────────────


def test_the_pi_pin_clears_the_harness_floor():
    """Runs everywhere: the lane cannot pin a pi the launch would refuse."""
    pinned = tuple(int(part) for part in MEASURED_PI_VERSION.split("."))
    assert pinned >= pi_mod.PI_MIN_VERSION, (MEASURED_PI_VERSION, pi_mod.PI_MIN_VERSION)


def test_the_scripted_model_calls_each_tool_only_where_it_is_offered():
    """Runs everywhere: the drive's model answers as the measurements assume.

    A scripted prompt gets its tool call only when the request offers that tool,
    so a pi that stopped offering a tool reads as a missing call, never a stray one;
    a tool result is acknowledged in text, which is what ends the turn.
    """
    reply = _Model._reply  # unbound: the method reads only its argument

    def ask(text: str, tools: list[str]) -> dict:
        body = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
            "tools": [{"type": "function", "function": {"name": t}} for t in tools],
        }
        return reply(None, body)  # type: ignore[arg-type]

    call = ask(_BASH, ["bash"])["tool_calls"][0]["function"]
    assert call["name"] == "bash" and json.loads(call["arguments"]) == {"command": _BASH_COMMAND}
    assert ask(_MCP, ["bash", _PROBE_TOOL])["tool_calls"][0]["function"]["name"] == _PROBE_TOOL
    assert "tool_calls" not in ask(_MCP, ["bash"])
    assert "tool_calls" not in ask(_BASH, [])
    assert reply(None, {"messages": [{"role": "tool", "content": "x"}]}) == {  # type: ignore[arg-type]
        "content": "TOOL-RESULT-SEEN"
    }


@pytest.mark.real_adapter
def test_real_pi_pair_is_the_pinned_release_at_or_above_the_floor():
    """The lane measures the pins, and the pins clear the harness's own floor."""
    argv, pi_bin = _require_pi()
    assert pi_mod.PI_ACP_NPM_PKG == PI_ACP_PACKAGE and pi_mod.PI_NPM_PKG == PI_PACKAGE
    installed = pi_mod._pi_installed_version(pi_bin)
    assert installed is not None, f"{pi_bin} is not an npm install of {PI_PACKAGE}"
    version, package = installed
    assert package == PI_PACKAGE
    assert ".".join(map(str, version)) == MEASURED_PI_VERSION
    assert version >= pi_mod.PI_MIN_VERSION
    manifest = Path(argv[-1]).resolve().parents[1] / "package.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == MEASURED_PI_ACP_VERSION


@pytest.mark.real_adapter
def test_real_pi_acp_handshake_answers_the_pi_dialect(pi_session):
    """``initialize`` with Crew's own params answers ``PROTOCOL_VERSION_PI``."""
    m, ctx = pi_session, pi_session["_context"]
    assert m["initialize"]["protocolVersion"] == PROTOCOL_VERSION_PI
    result = (m.get("init") or {}).get("result") or {}
    assert result.get("protocolVersion") == PROTOCOL_VERSION_PI, ctx
    info = result.get("agentInfo") or {}
    assert info.get("name") == PI_ACP_PACKAGE, ctx
    assert info.get("version") == MEASURED_PI_ACP_VERSION, ctx
    assert (result.get("agentCapabilities") or {}).get("loadSession") is True, ctx


@pytest.mark.real_adapter
def test_real_pi_registry_names_crews_sealed_copy(pi_session):
    """The read-back reads pi's own registry, and it names the SEALED file.

    The launch already ran the read-back and would have refused the session; this
    asks the registry again through the same launcher so the answer is on the record,
    and checks the decision is not vacuous: the same registry measured against any
    other path -- the packaged original included -- is refused.
    """
    m = pi_session
    launcher, sealed = m["launcher"], m["sealed"]
    assert m["env_launcher"] == launcher, "apply_spawn_env did not name the gate launcher"
    assert Path(sealed).resolve() != Path(pi_mod.pi_gate_extension_path()).resolve()
    commands = m["registry"]
    assert isinstance(commands, list), m["registry_stderr"]
    probe = gate_probe_command_for(ACP_BACKEND_PI)
    sources = [
        path
        for c in commands
        if isinstance(c, dict) and c.get("name") == probe
        for path in [(c.get("sourceInfo") or {}).get("path")]
        if isinstance(path, str)
    ]
    assert sources, f"pi's registry lists no {probe!r}: {commands}"
    spelled = pi_mod._same_file_spelling
    assert [spelled(s) for s in sources] == [spelled(sealed)], sources

    from kiro_crew import acp_tool_gate

    normalised = pi_mod._same_file_spelling_all(commands)
    assert acp_tool_gate.gate_extension_issue(ACP_BACKEND_PI, normalised, spelled(sealed)) == ""
    for other in (pi_mod.pi_gate_extension_path(), str(Path(m["root"]) / "elsewhere.ts")):
        assert acp_tool_gate.gate_extension_issue(ACP_BACKEND_PI, normalised, spelled(other))


@pytest.mark.real_adapter
def test_real_pi_gated_bash_call_asks_through_request_permission(pi_session):
    """pi's ``bash`` call raises the extension's dialog, forwarded with the envelope.

    Crew's builder reads the envelope under the launch's nonce and names the REAL
    tool and its arguments; the command runs only after the answer, and its output
    is what pi hands the model next.
    """
    m, ctx = pi_session, pi_session["_context"]
    turn = m["bash"]
    assert (turn["reply"].get("result") or {}).get("stopReason") == "end_turn", ctx
    frames = _permission_frames(turn["frames"])
    assert len(frames) == 1, f"expected one permission frame\n{ctx}"
    raw = frames[0]["params"]["toolCall"]["rawInput"]
    assert raw.get("method") == "confirm" and raw.get("title") == "bash", raw
    envelope = json.loads(raw["message"])
    assert envelope["kiro-crew-gate"] == 1 and envelope["nonce"] == m["nonce"], envelope
    assert envelope["tool"] == "bash" and envelope["input"] == {"command": _BASH_COMMAND}
    asked = [p for p in m["permissions"] if p["title"] == "bash" and p["answered"] == "yes"]
    assert asked and _BASH_COMMAND in asked[0]["tool_input"], m["permissions"]
    assert any("pi-gate-ran" in result for result in _tool_results(turn["requests"])), ctx


@pytest.mark.real_adapter
def test_real_pi_refused_call_does_not_run(pi_session):
    """Answering ``no`` blocks the call: the command never runs and pi says so."""
    m, ctx = pi_session, pi_session["_context"]
    turn = m["bash_denied"]
    assert len(_permission_frames(turn["frames"])) == 1, ctx
    assert not m["denied_file_exists"], "the refused command ran anyway"
    results = _tool_results(turn["requests"])
    assert any("Denied by the Kiro Crew tool gate" in r for r in results), ctx


@pytest.mark.real_adapter
def test_real_pi_mcp_handoff_mounts_crews_server(pi_session):
    """The handoff file, not the dropped array, gives pi Crew's server -- gated.

    The server is asked ``tools/call`` only after the extension's dialog for the
    pi-spelled tool name, which Crew's builder maps back to the server it placed,
    and the server's own answer reaches the model.
    """
    m, ctx = pi_session, pi_session["_context"]
    assert m["servers"] == (_PROBE_SERVER,), ctx
    turn = m["mcp"]
    frames = _permission_frames(turn["frames"])
    assert len(frames) == 1, ctx
    raw = frames[0]["params"]["toolCall"]["rawInput"]
    assert json.loads(raw["message"])["tool"] == _PROBE_TOOL, raw
    assert any(p["title"] == _CREW_TITLE and p["answered"] == "yes" for p in m["permissions"]), m[
        "permissions"
    ]
    methods = [call["method"] for call in m["mcp_calls"]]
    assert "tools/list" in methods and "tools/call" in methods, methods
    assert any("ECHO:ping" in result for result in _tool_results(turn["requests"])), ctx
    assert m["handoff_republished"], "the client's republish did not restore the handoff file"


@pytest.mark.real_adapter
def test_real_pi_manual_compact_completes_inside_the_turn(pi_session):
    """``/compact`` is advertised, calls the model, and ends the same prompt turn."""
    m, ctx = pi_session, pi_session["_context"]
    commands = {
        c.get("name") for u in _updates(m["frames"]) for c in u.get("availableCommands") or []
    }
    assert "compact" in commands, ctx
    turn = m["compact"]
    assert (turn["reply"].get("result") or {}).get("stopReason") == "end_turn", ctx
    assert turn["requests"], "/compact made no model request\n" + ctx
    chunks = [
        (u.get("content") or {}).get("text", "")
        for u in _updates(turn["frames"])
        if u.get("sessionUpdate") == "agent_message_chunk"
    ]
    assert any(c.startswith("Compaction completed") for c in chunks), ctx
    after = m["after_compact"]
    assert (after["reply"].get("result") or {}).get("stopReason") == "end_turn", ctx
