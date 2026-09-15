"""Real ACP bytes for workflow warm reuse; no gateway or namespace is replaced."""

import json
import subprocess
import sys

from kiro_crew.testing import fake_acp_backend


def test_workflow_warm_reset_allocates_distinct_native_sessions(tmp_path):
    requests = [
        {"id": 1, "method": "initialize", "params": {}},
        {"id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}},
        {"id": 3, "method": "session/new", "params": {"cwd": str(tmp_path)}},
        {"id": 4, "method": "_kiro.dev/session/terminate", "params": {"sessionId": "fake-1"}},
        {
            "id": 5,
            "method": "session/prompt",
            "params": {
                "sessionId": "fake-2",
                "prompt": [{"type": "text", "text": "warm workflow worker"}],
            },
        },
    ]
    result = subprocess.run(
        [sys.executable, "-m", "kiro_crew.testing.fake_acp_backend", "acp"],
        input="".join(json.dumps({"jsonrpc": "2.0", **row}) + "\n" for row in requests),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=20,
        check=True,
    )
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    replies = {row["id"]: row["result"] for row in messages if "id" in row}
    # AcpSessionProvider creates the replacement before destroying the old handle.
    # Reusing its ID lets old.destroy() unregister the NEW notification queue.
    assert replies[2]["sessionId"] != replies[3]["sessionId"]
    assert replies[3]["sessionId"] == "fake-2"
    chunks = [row["params"] for row in messages if row.get("method") == "session/update"]
    assert chunks == [
        {
            "sessionId": replies[3]["sessionId"],
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": fake_acp_backend.REPLY_TEXT},
            },
        }
    ]
    assert replies[5]["stopReason"] == "end_turn"


def test_workflow_timeout_summary_omits_private_payloads():
    from kiro_crew.testing.workflow_memory_scenario import progress_summary

    private = "PRIVATE_PAYLOAD_MUST_NOT_APPEAR"
    run = {
        "status": "running",
        "source": private,
        "result": private,
        "agent_errors": {"0": private},
        "events": [
            {"type": "run_started", "data": {"args": private}},
            {"type": "agent_started", "data": {"call_index": 0, "label": private}},
            {"type": "agent_finished", "data": {"agent_id": "a0", "error": private}},
            {"type": "agent_started", "data": {"call_index": 1, "label": private}},
            {"type": "log", "data": {"message": private}},
        ],
    }
    assert progress_summary(run) == {
        "status": "running",
        "events": {"run_started": 1, "agent_started": 2, "agent_finished": 1, "log": 1},
        "pending_calls": [1],
    }
    assert private not in json.dumps(progress_summary(run))


def test_workflow_backend_routes_each_session_to_its_own_projection(tmp_path, monkeypatch):
    """Only observe the scenario entry; projection and proof code are not replaced."""
    import io

    from kiro_crew.testing import workflow_memory_scenario

    captured = io.StringIO()
    monkeypatch.setattr(fake_acp_backend, "_SESSIONS", {})
    monkeypatch.setattr(fake_acp_backend.sys, "stdout", captured)
    calls = []

    def decision(text, servers, cwd):
        calls.append((text, servers, cwd))
        return "model decision"

    monkeypatch.setattr(workflow_memory_scenario, "respond", decision)

    def request(index, method, params):
        captured.seek(0)
        captured.truncate()
        fake_acp_backend._handle({"id": index, "method": method, "params": params})
        return [json.loads(line) for line in captured.getvalue().splitlines()]

    projections = [
        {"cwd": str(tmp_path / name), "mcpServers": [{"name": name, "command": name}]}
        for name in ("member-a", "member-b")
    ]
    a, b = [
        request(index, "session/new", params)[0]["result"]["sessionId"]
        for index, params in enumerate(projections, 1)
    ]
    prompt = [{"type": "text", "text": "[[WF_E2E:WORK:A]]"}]
    request(3, "session/prompt", {"sessionId": a, "prompt": prompt})
    assert calls[-1][1:] == (projections[0]["mcpServers"], projections[0]["cwd"])
    request(4, "_kiro.dev/session/terminate", {"sessionId": a})
    request(5, "session/prompt", {"sessionId": b, "prompt": prompt})
    assert calls[-1][1:] == (projections[1]["mcpServers"], projections[1]["cwd"])
    count = len(calls)
    for index, params in enumerate(
        (
            {"sessionId": a, "prompt": prompt},
            {"sessionId": "unknown", "prompt": prompt},
            {"prompt": prompt},
        ),
        6,
    ):
        response = request(index, "session/prompt", params)
        assert len(response) == 1 and "error" in response[0]
        assert len(calls) == count, "Unknown session must not borrow a live projection"


def test_workflow_worker_publishes_identity_before_mcp_http(tmp_path, monkeypatch):
    """Real child + _post transport; not a replacement for private namespace E2E.

    The model process has no ambient key, as with AcpRuntime. The HTTP observer
    reports only whether the worker's own key arrived. Publishing through the
    normal host writer is the positive control; neither resolver is mocked.
    """
    import asyncio
    import os

    from kiro_crew.messaging.identity import publish_turn_identity
    from kiro_crew.providers.base import LLMEvent
    from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    key = "wf-pool:wf_1:identity-probe"
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.memory_stores import persist_member_config, provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["identity-probe"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "identity-probe")
    persist_member_config(cfg, "identity-probe", create=True)
    bind_private_session_store(key, store)
    ConversationLog().update_metadata(key, {"memory_store": store})

    async def exercise():
        async def observe(reader, writer):
            try:
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                fields = dict(
                    line.decode().split(": ", 1)
                    for line in headers.split(b"\r\n")[1:]
                    if b": " in line
                )
                await reader.readexactly(int(fields.get("Content-Length", "0")))
                present = {name.lower(): value for name, value in fields.items()}.get(
                    "x-session-key"
                ) == key
                body = json.dumps({"worker_identity": present}).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(observe, "127.0.0.1", 0)
        async with server:
            env = dict(os.environ)
            for name in ("KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID", "KIROCREW_CHANNEL_ID"):
                env.pop(name, None)
            port = str(server.sockets[0].getsockname()[1])
            env["KIROCREW_PORT"] = port
            env["KIROCREW_BOUND_PORT"] = port
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import json,sys\nfrom kiro_crew.mcp_core import _api_port, _post\n"
                "assert _api_port() == int(sys.argv[1])\n"
                "for line in sys.stdin:\n"
                " print(json.dumps(_post('/identity-probe', {}, timeout=5)), flush=True)\n",
                port,
                cwd=tmp_path,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:

                class Model:
                    async def stream(self, message):
                        child.stdin.write(b"call\n")
                        await child.stdin.drain()
                        reply = await asyncio.wait_for(child.stdout.readline(), 10)
                        assert reply, "MCP transport child exited before answering"
                        yield LLMEvent(kind="text_chunk", text=reply.decode())
                        yield LLMEvent(kind="complete")

                class Sessions:
                    async def get_or_create(self, session_key, **kwargs):
                        assert session_key == key
                        return Model(), True, False

                    def get_pid(self, session_key):
                        assert session_key == key
                        return child.pid

                sessions = Sessions()
                worker = _WorkflowSessionWorker(
                    sessions, key=key, agent=None, model=None, cwd=str(tmp_path)
                )
                before = json.loads(await worker.send_message("probe", timeout=15))
                await publish_turn_identity(sessions, key)
                control = json.loads(await worker.send_message("probe", timeout=15))
                assert control == {"worker_identity": True}, "Host publication must reach _post"
                assert before == {"worker_identity": True}, "Worker omitted identity publication"
            finally:
                child.stdin.close()
                try:
                    await asyncio.wait_for(child.wait(), 5)
                except asyncio.TimeoutError:
                    child.kill()
                    await asyncio.wait_for(child.wait(), 5)

    asyncio.run(exercise())


def test_unpooled_paths_publish_identity_before_prompt(monkeypatch):
    """The named ``session=`` chain and the identity-cap overflow both run on a
    one-shot ``get_or_create`` session outside the pool. Each must publish its
    OWN key through the real ``publish_turn_identity`` (not mocked) before the
    prompt is built or streamed, using the pid ``sessions.get_pid`` reports.
    Only the final filesystem writer is captured, at its defining module."""
    import asyncio

    from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

    events: list[tuple] = []

    def _capture_publish(pid, session_key):
        events.append(("publish", pid, session_key))

    monkeypatch.setattr("kiro_crew.messaging.identity.publish_session_pid", _capture_publish)

    async def _stream(provider, prompt, **kwargs):
        events.append(("stream", provider.key))
        return prompt

    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _stream)

    class Provider:
        def __init__(self, key):
            self.key = key

    class Sessions:
        pids = {"chain-A": 4101, "wf-pool:run:0": 4102, "wf-unpooled:run:0": 4103}

        async def get_or_create(self, session_key, **kwargs):
            return Provider(session_key), True, False

        def get_pid(self, session_key):
            return self.pids[session_key]

        def release(self, key, *, cleanup=False):
            pass

        async def destroy(self, key):
            pass

    async def exercise():
        agent_fn, pool = build_pooled_agent_fn(
            Sessions(), run_id="run", max_workers=1, max_identities=1
        )
        try:
            await agent_fn("named", {"session": "chain-A"})
            await agent_fn("warm", {"model": "m1"})  # fills the single identity slot
            await agent_fn("warm again", {"model": "m1"})  # WARM reuse of that worker
            await agent_fn("overflow", {"model": "m2"})  # unpooled overflow valve
        finally:
            await pool.shutdown()

    asyncio.run(exercise())
    # Every session published its own key, with its own pid, strictly before it
    # streamed — no key is borrowed from a parent and none is skipped. The warm
    # reuse turn publishes AGAIN: publication is per send, not once at start, so
    # a pid mapping overwritten or rebuilt between turns is never stale.
    assert events == [
        ("publish", 4101, "chain-A"),
        ("stream", "chain-A"),
        ("publish", 4102, "wf-pool:run:0"),
        ("stream", "wf-pool:run:0"),
        ("publish", 4102, "wf-pool:run:0"),
        ("stream", "wf-pool:run:0"),
        ("publish", 4103, "wf-unpooled:run:0"),
        ("stream", "wf-unpooled:run:0"),
    ]
