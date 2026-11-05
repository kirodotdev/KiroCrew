"""Real MXC and four managed MCP servers, synthetic home and authenticated API fixture."""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT_PROGRAM = r"""
import asyncio, json, os, subprocess, sys

print(json.dumps({"pid": os.getpid()}), flush=True)
instruction = json.loads(sys.stdin.readline())
direct_pipe = None
minimal_pipe = None
if instruction.get("broker"):
    from kiro_crew.mcp_gateway.transport import connect

    try:
        asyncio.run(connect(instruction["broker"]))
    except OSError as exc:
        assert exc.winerror == 5, type(exc).__name__
        direct_pipe = {"operation": "ConnectPipe", "winerror": exc.winerror}
    else:
        raise AssertionError("Fixture no longer reproduces direct MXC pipe denial")
    import ctypes
    from ctypes import wintypes
    from kiro_crew.mcp_gateway.transport import resolve_address

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(resolve_address(instruction["broker"]), 0x00100003, 3, None, 3, 0x40000000, None)
    if handle not in (None, 0, ctypes.c_void_p(-1).value):
        kernel.CloseHandle(handle)
        minimal_pipe = {"operation":"CreateFileW", "access":"data-read-write-sync", "allowed":True}
    else:
        minimal_pipe = {"operation":"CreateFileW", "access":"data-read-write-sync", "winerror":ctypes.get_last_error()}
env = dict(os.environ)
env.update(instruction["env"])
try:
    child = subprocess.run(
        [sys.executable, "-c", instruction["program"], json.dumps(instruction["cases"])],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
except subprocess.TimeoutExpired:
    print(json.dumps({"timeout": True}), flush=True)
    sys.exit(1)
print(
    json.dumps(
        {
            "rc": child.returncode,
            "stdout": child.stdout,
            "stderr": child.stderr,
            "direct_pipe_denial": direct_pipe,
            "minimal_pipe": minimal_pipe,
        }
    ),
    flush=True,
)
sys.exit(child.returncode)
"""

CHILD_PROGRAM = r"""
import asyncio, json, os, sys
from pathlib import Path


async def main():
    try:
        Path(os.environ["KIROCREW_HOME"], "trust", "sel_hmac.key").read_bytes()
        raise AssertionError("native key readable")
    except PermissionError:
        pass
    results = []
    for case in json.loads(sys.argv[1]):
        env = dict(os.environ)
        env.update(case["env"])
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kiro_crew.mcp_gateway.stub",
            *case["args"],
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1048576,
        )

        async def rpc(id, method, params):
            p.stdin.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
                ).encode()
                + b"\n"
            )
            await p.stdin.drain()
            for _ in range(64):
                line = await asyncio.wait_for(p.stdout.readline(), 35)
                if not line:
                    raise RuntimeError("MCP EOF: " + (await p.stderr.read()).decode("utf-8", errors="replace"))
                msg = json.loads(line)
                if msg.get("id") == id:
                    return msg
            raise AssertionError("too many notifications")

        try:
            init = await rpc(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "native-synthetic", "version": "1"},
                },
            )
            if "result" not in init:
                await p.wait()
                raise AssertionError((init, (await p.stderr.read()).decode("utf-8", errors="replace")))
            p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await p.stdin.drain()
            tools = await rpc(2, "tools/list", {})
            if case["server"] in {"kirocrew-work", "kirocrew-dashboard"}:
                assert tools["result"]["tools"], tools
                results.append({
                    "server": case["server"], "tools_list": True,
                    "tool_count": len(tools["result"]["tools"]),
                })
                continue
            assert case["tool"] in [t["name"] for t in tools["result"]["tools"]], tools
            call = await rpc(
                3, "tools/call", {"name": case["tool"], "arguments": case["arguments"]}
            )
            assert "result" in call and not call["result"].get("isError"), call
            text = "\n".join(c.get("text", "") for c in call["result"].get("content", []))
            assert not text.startswith("Error:"), text
            results.append(
                {
                    "server": case["server"],
                    "tool": case["tool"],
                    "tools_list": True,
                    "result": text[:800],
                }
            )
        finally:
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 10)
            stderr = (await p.stderr.read()).decode("utf-8", errors="replace")
            if stderr:
                print(stderr, file=sys.stderr)
    print(json.dumps({"key_denied": True, "results": results}))


asyncio.run(main())
"""

ACP_PROGRAM = r"""
import asyncio, json, os, sys
from pathlib import Path

async def main():
    try:
        Path(os.environ['KIROCREW_HOME'], 'trust/sel_hmac.key').read_bytes()
        raise AssertionError('native key readable')
    except PermissionError:
        pass
    env = dict(os.environ, CODEX_PATH=os.environ['NATIVE_TEST_CODEX'], DISABLE_MCP_CONFIG_FILTERING='true')
    p = await asyncio.create_subprocess_exec(
        env['NATIVE_TEST_NODE'], env['NATIVE_TEST_ADAPTER'], env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16_777_216,
    )
    stderr = asyncio.create_task(p.stderr.read())
    permission_count = 0
    async def rpc(i, method, params):
        nonlocal permission_count
        p.stdin.write((json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\n').encode())
        await p.stdin.drain()
        while True:
            line = await asyncio.wait_for(p.stdout.readline(), 50)
            if not line:
                raise AssertionError('ACP adapter closed')
            obj = json.loads(line)
            if obj.get('id') == i:
                assert 'error' not in obj, obj
                return obj['result']
            if 'id' in obj:
                assert obj.get('method') == 'session/request_permission', obj
                permission_count += 1
                option = next(o['optionId'] for o in obj['params']['options'] if o['kind'] == 'allow_once')
                p.stdin.write((json.dumps({'jsonrpc':'2.0','id':obj['id'],'result':{'outcome':{'outcome':'selected','optionId':option}}})+'\n').encode())
                await p.stdin.drain()
    try:
        await rpc(1, 'initialize', {'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'kirocrew','version':'synthetic-fixture'}})
        entries = [{'name':x['server'],'command':sys.executable,
                    'args':['-s','-P','-m','kiro_crew.mcp_gateway.stub',*x['args']],
                    'env':[{'name':k,'value':v} for k,v in x['env'].items()]} for x in json.loads(sys.argv[1])]
        sid = (await rpc(2, 'session/new', {'cwd':os.getcwd(),'mcpServers':entries,
               '_meta':{'mcpStartupAwaitTimeoutMs':15000}}))['sessionId']
        configured = await rpc(3, 'session/set_config_option', {'sessionId':sid,'configId':'mode','value':'read-only'})
        assert any(o.get('currentValue') == 'read-only' for o in configured.get('configOptions',[])), configured
        result = await rpc(4, 'session/prompt', {'sessionId':sid,'prompt':[{'type':'text','text':'Use Crew skill_search and cron_list, then finish.'}]})
        assert result['stopReason'] == 'end_turn', result
        assert permission_count >= 2, 'Crew tool approvals did not reach ACP'
        await rpc(5, 'session/close', {'sessionId':sid})
        print(json.dumps({'key_denied':True,'acp_session_verified':True,'permission_requests':permission_count}), flush=True)
    finally:
        p.stdin.close()
        try:
            await asyncio.wait_for(p.wait(), 10)
        except TimeoutError:
            p.kill()
            await p.wait()
        print((await stderr).decode('utf-8',errors='replace'), file=sys.stderr)

asyncio.run(main())
"""

CODEX_PROGRAM = r"""
import json, os, subprocess, sys
from pathlib import Path

try:
    Path(os.environ["KIROCREW_HOME"], "trust", "sel_hmac.key").read_bytes()
    raise AssertionError("native key readable")
except PermissionError:
    pass
p = subprocess.run(
    [
        os.environ["NATIVE_TEST_CODEX"],
        "exec",
        "--json",
        "--skip-git-repo-check",
        "Use the configured Crew tools to search installed skills for synthetic and list cron jobs. Then reply native Codex MCP passed.",
    ],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=75,
)
print(
    json.dumps(
        {"codex_rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "key_denied": True}
    )
)
sys.exit(p.returncode)
"""

NEGATIVE_PROGRAM = r"""
import asyncio, json, os, sys
from pathlib import Path


async def main():
    results = []
    for case in json.loads(sys.argv[1]):
        env = dict(os.environ)
        env.update(case["env"])
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kiro_crew.mcp_gateway.stub",
            *case["args"],
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1048576,
        )
        stdout, stderr = await asyncio.wait_for(
            p.communicate(b'{"jsonrpc":"2.0","id":42,"method":"initialize","params":{}}\n'), 15
        )
        reply = json.loads(stdout)
        assert reply["id"] == 42 and reply["error"]["data"]["class"] == case["expected"], (
            reply,
            stderr.decode("utf-8", errors="replace"),
        )
        assert reply["error"]["data"]["direct_fallback"] is False
        assert p.returncode != 0
        results.append(
            {
                "server": case["server"],
                "class": reply["error"]["data"]["class"],
                "direct_fallback": False,
                "stub_exited": True,
            }
        )
    print(json.dumps({"negative_results": results}))


asyncio.run(main())
"""

FOREIGN_PROGRAM = r"""
import json, os, sys, uuid
from pathlib import Path
from kiro_crew.mcp_gateway import native_handles as h, native_scope

case = json.loads(sys.argv[1])[0]
info = json.loads(Path(case["env"][native_scope.DESCRIPTOR_ENV]).read_text())
k = h._kernel()
current = k.GetCurrentProcess()
with h._process(info["pid"]) as root:
    request = h._duplicate(root, info["request"], current)
    response = h._duplicate(root, info["response"], current)
try:
    rid = uuid.uuid4().hex
    h._write(
        request,
        {
            "request_id": rid,
            "pid": case["foreign"][0],
            "start": case["foreign"][1],
            "scope": case["env"][native_scope.SCOPE_ENV],
            "register": case["register"],
        },
    )
    result = h._read_reply(response, rid, *case["foreign"])
    assert result["error"] == "identity_rejected" and "handle" not in result, result
    print(json.dumps({"foreign_process_rejected": True}))
finally:
    k.CloseHandle(request)
    k.CloseHandle(response)
"""


async def _stop_gateway(task, event):
    event.set()
    try:
        await asyncio.wait_for(task, 30)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def exercise(
    root, sandbox_runner, codex=None, mode="mcp", report_path=None, *, adapter=None, node=None
):
    async with contextlib.AsyncExitStack() as cleanup:
        home = root / "crew"
        user = root / "home"
        workspace = root / "workspace"
        codex_home = user / "codex-profile"
        for path in (home / "trust", user, workspace, home / "run", codex_home):
            path.mkdir(parents=True, exist_ok=True)
        retained = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "PATHEXT",
                "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE",
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
                "PROGRAMDATA",
                "SYSTEMDRIVE",
                "ALLUSERSPROFILE",
            }
        }
        os.environ.clear()
        os.environ.update(retained)
        os.environ.update(
            {
                "KIROCREW_HOME": str(home),
                "HOME": str(user),
                "USERPROFILE": str(user),
                "USERNAME": "native-fixture",
                "USERDOMAIN": "synthetic",
                "APPDATA": str(user / "AppData/Roaming"),
                "LOCALAPPDATA": str(user / "AppData/Local"),
                "CODEX_HOME": str(codex_home),
                "PATH": str(Path(sys.executable).parent)
                + os.pathsep
                + os.environ["SystemRoot"]
                + r"\System32",
                "KIROCREW_WINDOWS_SANDBOX_EXE": str(sandbox_runner),
            }
        )
        (home / "trust/sel_hmac.key").write_bytes(b"synthetic-only-test-key-32-bytes!!")
        from aiohttp import web

        from kiro_crew import agent, platform_compat
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.paths import kiro_agents_dir
        from kiro_crew.instances import run_marker
        from kiro_crew.mcp_gateway import native_scope, native_session, transport
        from kiro_crew.mcp_gateway.claim import (
            STUB_SESSION_TOKEN_ENV,
            mint_stub_session_token,
            send_claim,
        )
        from kiro_crew.mcp_gateway.gatewayd import run_gatewayd
        from kiro_crew.mcp_gateway.hashing import hash_effective_env
        from kiro_crew.mcp_gateway.launch_approval import (
            ResolvedLaunch,
            approve,
            launch_fingerprint,
        )
        from kiro_crew.mcp_gateway.stub import _parse_args
        from kiro_crew.mcp_gateway.stub_register import build_register_payload
        from kiro_crew.sandbox_windows import _cleanup_windows_profile, windows_sandbox_argv
        from kiro_crew.session_token_sig import publish_session_token, verify_session_token

        platform_compat.restrict_to_owner(home / "trust/sel_hmac.key")
        token = mint_stub_session_token()
        session = "dashboard:native-synthetic"
        if mode != "identity":
            publish_session_token(token, session)
        calls = []
        secret = "synthetic-api-secret"

        async def api(request):
            assert (
                request.headers.get("X-Internal-Secret") == secret
            ), "synthetic API authentication failed"
            assert (
                request.headers.get("X-Session-Key") == session
            ), "synthetic API authentication failed"
            assert verify_session_token(request.headers.get("X-Session-Token", "")) == session
            body = await request.json() if request.can_read_body else dict(request.query)
            calls.append(
                {
                    "path": request.path,
                    "session": request.headers.get("X-Session-Key"),
                    "tool": body.get("name"),
                }
            )
            if request.path == "/api/session-tool-policy":
                return web.json_response({"exclude": []})
            if request.path == "/api/crons/tools":
                return web.json_response({"result": "[]"})
            return web.json_response({"matches": [], "scoped": True})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", api)
        runner = web.AppRunner(app)
        cleanup.push_async_callback(runner.cleanup)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        os.environ["KIROCREW_PORT"] = str(port)
        os.environ["KIROCREW_BOUND_PORT"] = str(port)
        run_marker.secret_path(port).write_text(secret)
        platform_compat.restrict_to_owner(run_marker.secret_path(port))
        model_calls = []

        async def model(request):
            if request.method == "GET":
                return web.json_response({"object": "list", "data": []})
            assert request.headers.get("Authorization") == "Bearer synthetic-model-key"
            body = await request.json()
            definitions = []

            def walk(items, namespace=None):
                for item in items:
                    if item.get("type") == "namespace":
                        walk(item.get("tools", []), item.get("name"))
                    elif item.get("type") == "function":
                        definitions.append((item.get("name"), namespace))

            walk(body.get("tools", []))
            model_calls.append(
                {
                    "tools": definitions,
                    "tool_types": [
                        {"type": item.get("type"), "name": item.get("name"), "keys": list(item)}
                        for item in body.get("tools", [])
                    ],
                    "outputs": [
                        item.get("call_id")
                        for item in body.get("input", [])
                        if isinstance(item, dict) and item.get("type") == "function_call_output"
                    ],
                }
            )
            assert len(model_calls) < 8, "model fixture did not observe both tool calls"
            done_core = any(row["path"] == "/api/skills/-/discover" for row in calls)
            done_cron = any(row["path"] == "/api/crons/tools" for row in calls)
            wanted = "skill_search" if not done_core else "cron_list" if not done_cron else None
            output = []
            if wanted:
                candidates = [
                    (name, namespace)
                    for name, namespace in definitions
                    if name and name.endswith(wanted)
                ]
                if not candidates:
                    raise AssertionError({"missing_tool": wanted, "definitions": definitions})
                name, namespace = candidates[0]
                item = {
                    "type": "function_call",
                    "id": "fc_" + str(len(model_calls)),
                    "call_id": "call_" + str(len(model_calls)),
                    "name": name,
                    "arguments": json.dumps(
                        {"query": "synthetic"} if wanted == "skill_search" else {}
                    ),
                }
                if namespace:
                    item["namespace"] = namespace
                output = [item]
            else:
                output = [
                    {
                        "type": "message",
                        "id": "msg_final",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "native Codex MCP passed",
                                "annotations": [],
                            }
                        ],
                    }
                ]
            response = {
                "id": "resp_" + str(len(model_calls)),
                "object": "response",
                "created_at": 1,
                "model": body.get("model"),
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }
            frames = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                }
            ]
            for index, item in enumerate(output):
                frames.append(
                    {"type": "response.output_item.added", "output_index": index, "item": item}
                )
                frames.append(
                    {"type": "response.output_item.done", "output_index": index, "item": item}
                )
            frames.append({"type": "response.completed", "response": response})
            data = "".join(
                "event: " + frame["type"] + "\ndata: " + json.dumps(frame) + "\n\n"
                for frame in frames
            )
            return web.Response(text=data, content_type="text/event-stream")

        model_app = web.Application()
        model_app.router.add_route("*", "/{tail:.*}", model)
        model_runner = web.AppRunner(model_app)
        cleanup.push_async_callback(model_runner.cleanup)
        await model_runner.setup()
        model_site = web.TCPSite(model_runner, "127.0.0.1", 0)
        await model_site.start()
        model_port = model_site._server.sockets[0].getsockname()[1]
        socket = home / "fixture.sock"
        (home / "config.json").write_text(
            json.dumps(
                {
                    "mcp_gateway": {
                        "socket_path": str(socket),
                        "stub_overrides": {
                            "kirocrew-core": True,
                            "kirocrew-cron": True,
                            "kirocrew-work": True,
                            "kirocrew-dashboard": True,
                        },
                    }
                }
            )
        )
        KiroCrewConfig.load()
        specs = {
            name: agent.managed_mcp_spec_entry(name, include_opt_in=True)
            for name in ("kirocrew-core", "kirocrew-cron", "kirocrew-work", "kirocrew-dashboard")
        }
        assert all(specs.values()), specs
        agents = kiro_agents_dir()
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "synthetic-agent.json").write_text(
            json.dumps(
                {
                    "name": "synthetic-agent",
                    "prompt": "synthetic fixture",
                    "mcpServers": specs,
                    "tools": [
                        "@kirocrew-core",
                        "@kirocrew-cron",
                        "@kirocrew-work",
                        "@kirocrew-dashboard",
                    ],
                }
            )
        )
        cases = []
        targets = {}
        resolutions = []
        for name, spec in specs.items():
            command, args = spec["command"], spec["args"]
            targets[name] = (command, args, dict(os.environ), str(workspace))
            [routed] = native_session.broker_entries(
                [{"name": name, **spec}],
                agent_name="synthetic-agent",
                work_dir=str(workspace),
                socket_path=socket,
            )
            argv = routed["args"][routed["args"].index("kiro_crew.mcp_gateway.stub") + 1 :]
            parsed = _parse_args(argv)
            payload = build_register_payload(
                parsed,
                session_token=token,
                caller={
                    "session_key": "",
                    "session_type": "unknown",
                    "principal_id": "",
                    "channel_id": "",
                },
            )
            payload["channel_id"] = "dashboard"
            if mode != "approval":
                approve(
                    {
                        name: [
                            ResolvedLaunch(
                                launch_fingerprint(command, args, spec.get("env", {})),
                                command,
                                tuple(args),
                                frozenset({hash_effective_env(spec.get("env", {}))}),
                            )
                        ]
                    }
                )
            cases.append(
                {
                    "server": name,
                    "args": argv,
                    "payload": payload,
                    "env": {
                        STUB_SESSION_TOKEN_ENV: token,
                        "KIROCREW_CHANNEL_ID": "dashboard",
                        "KIROCREW_HOME": str(home),
                    },
                    "tool": "skill_search" if name.endswith("core") else "cron_list",
                    "arguments": {"query": "synthetic"} if name.endswith("core") else {},
                }
            )
        stop = asyncio.Event()

        def resolve(key):
            resolutions.append(key.server_name)
            assert targets[key.server_name][2]["KIROCREW_HOME"] == str(home)
            return targets[key.server_name]

        gateway = None
        if mode != "outage":
            gateway = asyncio.create_task(
                run_gatewayd(
                    socket,
                    max_backends=4,
                    idle_timeout_secs=5,
                    stop_event=stop,
                    target_resolver=resolve,
                    credential_watch_paths=[],
                )
            )
            cleanup.push_async_callback(_stop_gateway, gateway, stop)
            for _ in range(300):
                if await asyncio.to_thread(transport.endpoint_exists, socket):
                    break
                if gateway.done():
                    await gateway
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("gateway not ready")
        scratch = home / "scratch/runtime-probe"
        scratch.mkdir(parents=True)
        wrapped, artifact = await asyncio.to_thread(
            windows_sandbox_argv,
            [sys.executable, "-c", ROOT_PROGRAM],
            extra_hidden_dirs=(str(home / "trust"),),
            extra_private_dirs=(str(scratch),),
            extra_visible_dirs=(str(codex_home),),
        )
        cleanup.callback(_cleanup_windows_profile, Path(artifact))

        async def spawn(windows_cleanup_owner):
            return await platform_compat._create_windows_subprocess_owned(
                windows_cleanup_owner,
                *wrapped,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )

        process = await platform_compat.create_windows_cleanup_owned_process(spawn)

        async def retire_native():
            if not await platform_compat.terminate_windows_asyncio_tree(process):
                raise AssertionError("Native process-tree retirement was not confirmed")

        cleanup.push_async_callback(retire_native)
        try:
            first = await asyncio.wait_for(process.stdout.readline(), 20)
            if not first:
                raise RuntimeError((await process.stderr.read()).decode("utf-8", errors="replace"))
            descriptor = scratch / native_scope.DESCRIPTOR_NAME
            for _ in range(500):
                try:
                    info = json.loads(descriptor.read_text("ascii"))
                    if "request" in info:
                        break
                except (FileNotFoundError, ValueError):
                    pass
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("descriptor not ready")
            attached_scopes = {}
            if mode == "mcp":
                attached, _ = await native_session.finalize(
                    SimpleNamespace(_scratch_dir=scratch, pid=process.pid),
                    [
                        {
                            "name": case["server"],
                            "command": sys.executable,
                            "args": ["-m", "kiro_crew.mcp_gateway.stub", *case["args"]],
                            "env": [{"name": k, "value": v} for k, v in case["env"].items()],
                        }
                        for case in cases
                    ],
                    agent_name="synthetic-agent",
                    work_dir=str(workspace),
                    session_key=session,
                    token=token,
                )
                for entry in attached:
                    argv = entry["args"]
                    case = next(case for case in cases if case["server"] == entry["name"])
                    case["args"] = argv[argv.index("kiro_crew.mcp_gateway.stub") + 1 :]
                    case["env"].update({item["name"]: item["value"] for item in entry["env"]})
                    attached_scopes[entry["name"]] = {
                        item["name"]: item["value"] for item in entry["env"]
                    }[native_scope.SCOPE_ENV]
            if mode not in {"identity", "outage", "mcp"}:
                await send_claim(str(socket), process.pid, session, stub_session_token=token)
            for case in cases:
                payload = case.pop("payload")
                scope = (
                    attached_scopes[case["server"]]
                    if mode == "mcp"
                    else native_scope.issue(
                        payload,
                        "0" * 64 if mode == "foreign_scope" else info["instance"],
                        now=0 if mode == "expired" else None,
                    )
                )
                if mode == "tamper":
                    invalid = json.loads(scope)
                    invalid["mac"] = "0" * 64
                    scope = json.dumps(invalid)
                if mode == "foreign":
                    from kiro_crew.mcp_gateway.native_handles import _identity, _process

                    with _process(os.getpid()) as own:
                        case["foreign"] = _identity(own)
                    case["register"] = payload
                case["env"].update(
                    {
                        native_scope.DESCRIPTOR_ENV: str(descriptor),
                        native_scope.SCOPE_ENV: scope,
                        native_scope.REQUIRED_ENV: "1",
                    }
                )
                case["expected"] = {
                    "outage": "connection_failed",
                    "identity": "identity_unattested",
                    "approval": "launch_approval_required",
                    "tamper": "identity_unattested",
                    "expired": "identity_unattested",
                    "foreign_scope": "identity_unattested",
                }.get(mode)
            environment = {}
            if codex:
                lines = [
                    # A custom local model uses Codex's fallback metadata. This
                    # fixture speaks function calls, not code-mode/tool-search.
                    'model = "synthetic-native-mcp"',
                    'model_provider = "synthetic"',
                    'cli_auth_credentials_store = "file"',
                    'approval_policy = "never"',
                    'sandbox_mode = "danger-full-access"',
                    "project_root_markers = []",
                    "[features]",
                    "tool_search = false",
                    "[model_providers.synthetic]",
                    'name = "Synthetic local test"',
                    'wire_api = "responses"',
                    "requires_openai_auth = false",
                    "supports_websockets = false",
                    "request_max_retries = 0",
                    "stream_max_retries = 0",
                    'env_key = "NATIVE_TEST_API_KEY"',
                    f'base_url = "http://127.0.0.1:{model_port}/v1"',
                ]
                for case in ([] if adapter else cases):
                    name = case["server"].replace("-", "_")
                    lines.extend(
                        [
                            f"[mcp_servers.{name}]",
                            "command = " + json.dumps(sys.executable),
                            "args = "
                            + json.dumps(["-m", "kiro_crew.mcp_gateway.stub", *case["args"]]),
                            "startup_timeout_sec = 35",
                            "required = true",
                            f"[mcp_servers.{name}.env]",
                        ]
                    )
                    lines.extend(
                        key + " = " + json.dumps(value) for key, value in case["env"].items()
                    )
                (codex_home / "config.toml").write_text("\n".join(lines) + "\n")
                environment = {
                    "NATIVE_TEST_CODEX": str(codex),
                    "NATIVE_TEST_API_KEY": "synthetic-model-key",
                    "CODEX_SQLITE_HOME": str(scratch),
                    "TEMP": str(scratch),
                    "TMP": str(scratch),
                }
                if adapter:
                    environment.update(
                        {"NATIVE_TEST_ADAPTER": str(adapter), "NATIVE_TEST_NODE": str(node)}
                    )
            program = (
                (ACP_PROGRAM if adapter else CODEX_PROGRAM)
                if codex
                else (
                    FOREIGN_PROGRAM
                    if mode == "foreign"
                    else NEGATIVE_PROGRAM if mode != "mcp" else CHILD_PROGRAM
                )
            )
            from kiro_crew import windows_settings
            from kiro_crew.atomic_write import atomic_write

            before = {
                name: (home / name).read_bytes() if (home / name).exists() else None
                for name in windows_settings.DOCUMENTS
            }
            for name in before:
                try:
                    atomic_write(home / name, '{"synthetic":true}')
                except windows_settings.NativeSettingsRestartRequired:
                    pass
                else:
                    raise AssertionError("Live native reader did not fence protected publication")
                assert ((home / name).read_bytes() if (home / name).exists() else None) == before[
                    name
                ]
            from kiro_crew.platform_compat_wintree import (
                WindowsCleanupCapacityError,
                block_windows_tree_starts,
                unblock_windows_tree_starts,
            )

            owner = object()
            assert block_windows_tree_starts(owner)

            async def forbidden_factory(**kwargs):
                raise AssertionError("Physical spawn crossed the settings fence")

            try:
                try:
                    await platform_compat.create_windows_cleanup_owned_process(forbidden_factory)
                except WindowsCleanupCapacityError:
                    pass
                else:
                    raise AssertionError("New start was admitted during publication")
            finally:
                unblock_windows_tree_starts(owner)
            process.stdin.write(
                json.dumps(
                    {
                        "env": environment,
                        "program": program,
                        "cases": cases,
                        "broker": str(socket) if mode != "outage" else None,
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 100)
            report = {
                "rc": process.returncode,
                "stdout": stdout.decode(),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "authenticated_api_calls": calls,
                "model_calls": model_calls,
                "resolutions": resolutions,
            }
            if report_path:
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            assert process.returncode == 0, report
            if mode != "outage":
                assert json.loads(stdout)["direct_pipe_denial"] == {
                    "operation": "ConnectPipe",
                    "winerror": 5,
                }
                assert json.loads(stdout)["minimal_pipe"] == {
                    "operation": "CreateFileW",
                    "access": "data-read-write-sync",
                    "winerror": 5,
                }
            if adapter:
                actual = json.loads(json.loads(stdout)["stdout"])
                assert actual["acp_session_verified"] and actual["key_denied"], actual
                namespaces = {namespace or "" for name, namespace in model_calls[0]["tools"]}
                for server in specs:
                    assert any(
                        server.replace("-", "_") in namespace for namespace in namespaces
                    ), server
            elif codex:
                child = json.loads(stdout)
                actual = json.loads(child["stdout"])
                events = [
                    json.loads(line) for line in actual["stdout"].splitlines() if line.strip()
                ]
                completed = {
                    item["item"].get("tool")
                    for item in events
                    if item.get("type") == "item.completed"
                    and item.get("item", {}).get("type") == "mcp_tool_call"
                    and item["item"].get("status") == "completed"
                }
                assert {"skill_search", "cron_list"} <= completed, events
                assert any(item.get("type") == "turn.completed" for item in events), events
                thread_id = next(
                    item["thread_id"] for item in events if item.get("type") == "thread.started"
                )
                assert list(
                    (codex_home / "sessions").rglob("*" + thread_id + "*.jsonl")
                ), "Codex rollout did not persist in its isolated profile"
            if mode == "mcp":
                assert {"/api/skills/-/discover", "/api/crons/tools"}.issubset(
                    {row["path"] for row in calls}
                ), calls
            else:
                assert not calls and not resolutions, (calls, resolutions)
            with windows_settings.write_transaction():
                for name in before:
                    atomic_write(home / name, '{"synthetic":true}')
                    assert json.loads((home / name).read_text()) == {"synthetic": True}
        finally:
            await cleanup.aclose()
        from kiro_crew.platform_compat_wintree import windows_tree_admissions_pending

        assert windows_tree_admissions_pending() == 0, "Owned cleanup receipts remain pending"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner", required=True, type=lambda value: Path(value).resolve(strict=True)
    )
    parser.add_argument("--codex", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument("--adapter", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument("--node", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument(
        "--mode",
        choices=(
            "mcp",
            "identity",
            "outage",
            "approval",
            "tamper",
            "foreign",
            "expired",
            "foreign_scope",
        ),
        default="mcp",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Real Windows MXC is required")
    if args.codex and args.mode != "mcp":
        parser.error("Codex is used for the positive MCP test only")
    if bool(args.adapter) != bool(args.node) or (args.adapter and not args.codex):
        parser.error("ACP verification requires --adapter, --node and --codex together")
    with tempfile.TemporaryDirectory(prefix="crew-native-real-mcp-") as temp:
        asyncio.run(
            exercise(
                Path(temp),
                args.runner,
                args.codex,
                args.mode,
                args.report,
                adapter=args.adapter,
                node=args.node,
            )
        )
