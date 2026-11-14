from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any

import kiro_byo.acp_server as acp_mod
import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mcp_client import DEFERRED_TOOL_SEARCH_NAME, ToolBinding
from kiro_byo.providers import ChatTurn, OpenAICompatibleInterrupted, ProviderTransportError, ToolCall, UsageInfo
from kiro_byo.protocol import type_selection_id
from kiro_byo.tool_display import tool_display_title
from kiro_byo.sandbox import SandboxError
from kiro_byo.telemetry import TurnTelemetryStore


class QueueInput:
    def __init__(self) -> None:
        self.q: queue.Queue[str | None] = queue.Queue()

    def put(self, payload: dict[str, Any]) -> None:
        self.q.put(json.dumps(payload, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.q.put(None)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            yield item


class CaptureOutput:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._buffer = ""
        self._messages: list[dict[str, Any]] = []

    def write(self, value: str) -> int:
        with self._cv:
            self._buffer += value
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._messages.append(json.loads(line))
            self._cv.notify_all()
        return len(value)

    def flush(self) -> None:
        return

    def wait_for(self, predicate, timeout: float = 3.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for msg in self._messages:
                    if predicate(msg):
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; messages={self._messages!r}")
                self._cv.wait(min(0.05, remaining))

    def messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._messages)


class FakeMcp:
    def __init__(self) -> None:
        binding = ToolBinding(
            public_name="mcp__core__ping",
            server_name="core",
            tool_name="ping",
            description="ping",
            input_schema={"type": "object", "properties": {}},
        )
        self.bindings = {binding.public_name: binding}
        self.servers = {"core": object()}
        self.startup_durations_ms: dict[str, float] = {"core": 0.0}
        self.startup_failures: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.catalog_cache_hits: set[str] = set()
        self.wait_calls = 0

    def start(self) -> None:
        return

    def start_background(self) -> None:
        return

    def wait_for_catalog(self, timeout: float = 20.0) -> bool:
        self.wait_calls += 1
        return True

    def catalog_ready(self) -> bool:
        return True

    @staticmethod
    def deferred_tool_search_schema() -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": DEFERRED_TOOL_SEARCH_NAME,
                "description": "search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }

    def tool_specs(self, names=None) -> list[dict[str, Any]]:
        selected = set(names) if names is not None else None
        if selected is not None and "mcp__core__ping" not in selected:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "mcp__core__ping",
                    "description": "ping",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def search_tools(self, query: str, *, limit=8, server="", timeout=20.0, allowed=None):
        self.wait_calls += 1
        binding = self.bindings["mcp__core__ping"]
        if allowed is not None and not allowed(binding):
            return [], 0.0
        return [binding], 0.0

    def call(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return "pong"

    def close(self) -> None:
        self.closed = True


def make_test_config() -> KiroByoConfig:
    model = ModelSpec(
        provider_id="test",
        provider_name="Test",
        model_id="test-model",
        name="Test Model",
        tier="strong",
        input_modalities=("text", "image"),
    )
    provider = ProviderSpec(
        id="test",
        name="Test",
        type="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="",
        models=(model,),
        stream=True,
    )
    return KiroByoConfig(providers=(provider,), default_model="Test Model")


class AcpControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.config = make_test_config()
        self.orig_config = AcpServer._config
        self.orig_mcp = AcpServer._mcp_from_params
        self.orig_turn = turn_mod.chat_completion_turn
        AcpServer._config = staticmethod(lambda: self.config)

    def tearDown(self) -> None:
        AcpServer._config = self.orig_config
        AcpServer._mcp_from_params = self.orig_mcp
        turn_mod.chat_completion_turn = self.orig_turn
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_permission_round_trip_is_full_duplex(self) -> None:
        out = CaptureOutput()
        server = AcpServer(stdin=StringIO(), stdout=out)
        fake_mcp = FakeMcp()
        session = Session("s1", "", "Test Model", mcp=fake_mcp)
        server._put_session(session)

        result: list[bool] = []
        worker = threading.Thread(
            target=lambda: result.append(
                server._request_tool_permission(session, "call-1", "mcp__core__ping", {"x": 1})
            )
        )
        worker.start()
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        tool_call = req["params"]["toolCall"]
        self.assertEqual(tool_call["_meta"]["kiro"]["mcpServerName"], "core")
        self.assertEqual(tool_call["_meta"]["kiro"]["toolName"], "ping")
        self.assertEqual(
            tool_call["_meta"]["kiro"]["mcpTool"]["identity"],
            {"serverName": "core", "toolName": "ping"},
        )
        self.assertEqual(
            req["params"]["_meta"]["kiro"]["mcpTool"]["identity"],
            {"serverName": "core", "toolName": "ping"},
        )
        consent = req["params"]["_meta"]["kiro"]["consent"]
        self.assertEqual(consent["capability"], "mcp")
        self.assertEqual(consent["resource"], "core/ping")
        server._resolve_client_response(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
            }
        )
        worker.join(2)
        self.assertEqual(result, [True])
        server._executor.shutdown(wait=True)

    def test_tool_call_does_not_execute_before_permission(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        turns = iter(
            [
                ChatTurn(
                    text="",
                    tool_calls=(ToolCall("call-1", "mcp__core__ping", '{"value":1}'),),
                ),
                ChatTurn(text="done"),
            ]
        )
        turn_mod.chat_completion_turn = lambda *a, **kw: next(turns)
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()

        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        created = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)
        sid = created["result"]["sessionId"]
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "use tool"}]},
            }
        )
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        self.assertEqual(fake_mcp.calls, [])
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(fake_mcp.calls, [("mcp__core__ping", {"value": 1})])
        inp.close()
        t.join(3)
        self.assertFalse(t.is_alive())

    def test_rejected_permission_never_executes_tool(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        turns = iter(
            [
                ChatTurn(tool_calls=(ToolCall("call-1", "mcp__core__ping", "{}"),), text=""),
                ChatTurn(text="recovered"),
            ]
        )
        turn_mod.chat_completion_turn = lambda *a, **kw: next(turns)
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]}})
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        inp.put({"jsonrpc": "2.0", "id": req["id"], "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}}})
        out.wait_for(lambda m: m.get("id") == 2)
        self.assertEqual(fake_mcp.calls, [])
        self.assertTrue(any(
            m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("status") == "refused"
            for m in out.messages()
        ))
        inp.close(); t.join(3)

    def test_allowed_mcp_tool_skips_permission_for_unattended_surface(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        # Reproduce the Crew default shape from the user's cron failure: the
        # active saved agent grants the whole core MCP server in allowedTools.
        core = ToolBinding(
            public_name="mcp__kirocrew-core__skill_search",
            server_name="kirocrew-core",
            tool_name="skill_search",
            description="search skills",
            input_schema={"type": "object", "properties": {}},
        )
        fake_mcp.bindings = {core.public_name: core}
        fake_mcp.servers = {"kirocrew-core": object()}
        fake_mcp.tool_specs = lambda names=None: [{
            "type": "function",
            "function": {"name": core.public_name, "description": "search", "parameters": core.input_schema},
        }] if names is None or core.public_name in set(names) else []
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        turns = iter([
            ChatTurn(tool_calls=(ToolCall("skill-1", core.public_name, '{"query":"web search news fetch"}'),), text=""),
            ChatTurn(text="done"),
        ])
        turn_mod.chat_completion_turn = lambda *a, **kw: next(turns)
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put({
            "jsonrpc": "2.0", "id": 1, "method": "session/new",
            "params": {
                "cwd": str(Path(self.tmp.name).resolve()),
                "mcpServers": [],
                "_meta": {"kiro": {
                    "modeId": "kirocrew",
                    "customAgents": [{
                        "id": "kirocrew",
                        "prompt": "default",
                        "tools": ["@kirocrew-core"],
                        "allowedTools": ["@kirocrew-core"],
                    }],
                }},
            },
        })
        sid = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)["result"]["sessionId"]
        inp.put({
            "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "search"}]},
        })
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(fake_mcp.calls, [(core.public_name, {"query": "web search news fetch"})])
        self.assertFalse(
            any(m.get("method") == "session/request_permission" for m in out.messages()),
            "allowedTools must not turn an unattended cron-safe Crew tool into an interactive approval",
        )
        self.assertTrue(any(
            m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("toolCallId") == "skill-1"
            for m in out.messages()
        ))
        inp.close(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_unlisted_mcp_tool_still_requests_crew_permission(self) -> None:
        projection = AgentProjection(
            agent_id="kirocrew", prompt="x", allowed_tools=("@kirocrew-core",)
        )
        self.assertFalse(projection.auto_approves_mcp(
            server_name="other", tool_name="mutate", public_name="mcp__other__mutate"
        ))

    def test_mid_turn_steer_stays_in_same_prompt(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        fake_mcp.bindings = {}
        fake_mcp.servers = {}
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        first_started = threading.Event()
        calls: list[list[dict[str, Any]]] = []

        def fake_turn(_provider, _model, messages, on_text, **kwargs):
            calls.append(list(messages))
            if len(calls) == 1:
                first_started.set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if kwargs["should_interrupt"]():
                        on_text("partial")
                        raise OpenAICompatibleInterrupted("partial")
                    time.sleep(0.01)
                raise AssertionError("steer never reached provider")
            return ChatTurn(text="steered answer")

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "long task"}]}})
        self.assertTrue(first_started.wait(2))
        inp.put({"jsonrpc": "2.0", "id": 3, "method": "_session/steer", "params": {"sessionId": sid, "message": "<user_message>\nchange direction\n</user_message>"}})
        steer_ack = out.wait_for(lambda m: m.get("id") == 3)
        self.assertTrue(steer_ack["result"]["queued"])
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(len(calls), 2)
        second_messages = calls[1]
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == "change direction" for m in second_messages))
        steer_infos = [
            m.get("params", {}).get("update", {}).get("_meta", {}).get("kiro", {})
            for m in out.messages()
            if m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("sessionUpdate") == "session_info_update"
        ]
        self.assertTrue(any(info.get("kind") == "steering_queued" for info in steer_infos))
        self.assertTrue(any(
            info.get("kind") == "steering_injected" and info.get("content") == "change direction"
            for info in steer_infos
        ))
        inp.close(); t.join(3)

    def test_cancel_clears_queued_steer_and_emits_v3_lifecycle_event(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        fake_mcp.bindings = {}
        fake_mcp.servers = {}
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        started = threading.Event()

        def fake_turn(_provider, _model, _messages, _on_text, **kwargs):
            started.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if kwargs["should_interrupt"]():
                    raise OpenAICompatibleInterrupted("")
                time.sleep(0.01)
            raise AssertionError("cancel never reached provider")

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "long task"}]}})
        self.assertTrue(started.wait(2))
        inp.put({"jsonrpc": "2.0", "id": 3, "method": "_session/steer", "params": {"sessionId": sid, "message": "later correction"}})
        self.assertTrue(out.wait_for(lambda m: m.get("id") == 3)["result"]["queued"])
        inp.put({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        result = out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(result["result"]["stopReason"], "cancelled")
        session = server._get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(list(session.steer_queue), [])
        self.assertFalse(session.steer_event.is_set())
        steer_infos = [
            m.get("params", {}).get("update", {}).get("_meta", {}).get("kiro", {})
            for m in out.messages()
            if m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("sessionUpdate") == "session_info_update"
        ]
        self.assertTrue(any(info.get("kind") == "steering_cleared" for info in steer_infos))
        cancel_events = [
            e for e in server.runtime_trace.recent(100)
            if e.get("event") == "control.cancel.requested" and e.get("session_id") == sid
        ]
        self.assertTrue(cancel_events)
        cancel_data = cancel_events[-1].get("data", {})
        self.assertEqual(cancel_data.get("source"), "client_session_cancel")
        self.assertTrue(cancel_data.get("active_turn"))
        self.assertGreaterEqual(float(cancel_data.get("elapsed_ms") or 0.0), 0.0)
        inp.close(); t.join(3)

    def test_transport_failure_does_not_poison_session_for_next_prompt(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        fake_mcp.bindings = {}
        fake_mcp.servers = {}
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        calls = 0

        def fake_turn(_provider, _model, _messages, on_text, **_kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:  # initial call + the one bounded transport retry
                raise ProviderTransportError("synthetic truncated stream")
            on_text("recovered session")
            return ChatTurn(text="recovered session")

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]

        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "first"}]}})
        first = out.wait_for(lambda m: m.get("id") == 2)
        self.assertIn("result", first)
        self.assertNotIn("error", first)
        session = server._get_session(sid)
        self.assertIsNotNone(session)
        self.assertFalse(session.active_turn)
        self.assertEqual(session.messages, [])

        inp.put({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "second"}]}})
        second = out.wait_for(lambda m: m.get("id") == 3)
        self.assertIn("result", second)
        self.assertNotIn("error", second)
        self.assertEqual(calls, 3)
        self.assertEqual(session.messages[-1]["content"], "recovered session")

        rows = server.telemetry.recent(10)
        self.assertEqual(rows[-2]["outcome"], "error")
        self.assertEqual(rows[-2]["error_category"], "transport")
        self.assertEqual(rows[-2]["transport_retries"], 1)
        self.assertEqual(rows[-1]["outcome"], "success")
        inp.close(); t.join(3)

    def test_second_plain_prompt_is_rejected_so_crew_owns_queueing(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        fake_mcp.bindings = {}
        fake_mcp.servers = {}
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        started = threading.Event()
        release = threading.Event()

        def fake_turn(_provider, _model, _messages, _on_text, **_kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return ChatTurn(text="first answer")

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "first"}]}})
        self.assertTrue(started.wait(2))
        inp.put({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "second"}]}})
        busy = out.wait_for(lambda m: m.get("id") == 3 and "error" in m)
        self.assertEqual(busy["error"]["code"], -32000)
        self.assertIn("session busy", busy["error"]["message"])
        release.set()
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        inp.close(); t.join(3)

    def test_successful_prompt_writes_turn_telemetry(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        fake_mcp.bindings = {}
        fake_mcp.servers = {}
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        def telemetry_turn(_provider, _model, _messages, on_text, **_kwargs):
            on_text("hello")
            return ChatTurn(text="hello")
        turn_mod.chat_completion_turn = telemetry_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]}})
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        summary = TurnTelemetryStore().summary()
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["success"], 1)
        recent = TurnTelemetryStore().recent(1)[0]
        self.assertGreaterEqual(float(recent["first_visible_ms"]), 0.0)
        inp.close(); t.join(3)

    def test_plain_text_turn_does_not_wait_for_full_mcp_catalogue(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        seen_tools: list[list[dict[str, Any]]] = []

        def fake_turn(_provider, _model, _messages, _on_text, **kwargs):
            seen_tools.append(list(kwargs.get("tools") or []))
            return ChatTurn(text="hello")

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]}})
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(fake_mcp.wait_calls, 0)
        self.assertEqual(
            [x["function"]["name"] for x in seen_tools[0]],
            [
                "read", "glob", "grep", "read_files", "list_directory",
                "write", "edit", "delete_file", "shell",
                "execute_shell_process", "control_shell_process", "code_symbols",
                "web_fetch", "web_search", "update_plan", DEFERRED_TOOL_SEARCH_NAME,
            ],
        )
        inp.close(); t.join(3)

    def test_deferred_tool_search_loads_schema_without_host_permission(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        fake_mcp = FakeMcp()
        AcpServer._mcp_from_params = staticmethod(lambda _params: fake_mcp)
        observed_tools: list[list[str]] = []
        turns = iter([
            ChatTurn(
                text="",
                tool_calls=(ToolCall("search-1", DEFERRED_TOOL_SEARCH_NAME, '{"query":"ping"}'),),
            ),
            ChatTurn(text="done"),
        ])

        def fake_turn(_provider, _model, _messages, _on_text, **kwargs):
            observed_tools.append([x["function"]["name"] for x in (kwargs.get("tools") or [])])
            return next(turns)

        turn_mod.chat_completion_turn = fake_turn
        server = AcpServer(stdin=inp, stdout=out)
        t = threading.Thread(target=server.run)
        t.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "find ping"}]}})
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        # Search is metadata-only, so no Crew permission request was emitted.
        self.assertFalse(any(m.get("method") == "session/request_permission" for m in out.messages()))
        expected_native = [
            "read", "glob", "grep", "read_files", "list_directory",
            "write", "edit", "delete_file", "shell", "execute_shell_process",
            "control_shell_process", "code_symbols", "web_fetch", "web_search", "update_plan",
        ]
        self.assertEqual(observed_tools[0], [*expected_native, DEFERRED_TOOL_SEARCH_NAME])
        self.assertEqual(observed_tools[1], [*expected_native, DEFERRED_TOOL_SEARCH_NAME, "mcp__core__ping"])
        restored = server._get_session(sid)
        assert restored is not None
        self.assertIn("mcp__core__ping", restored.loaded_tool_names)
        inp.close(); t.join(3)

    def test_close_then_load_restores_persisted_thread(self) -> None:
        out = CaptureOutput()
        AcpServer._mcp_from_params = staticmethod(lambda _params: FakeMcp())
        turn_mod.chat_completion_turn = lambda *a, **kw: ChatTurn(text="remember this")
        server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "remember"}]}})
        server.handle({"jsonrpc": "2.0", "id": 3, "method": "session/close", "params": {"sessionId": sid}})
        self.assertTrue(AcpServer._session_path(sid).exists())
        server._executor.shutdown(wait=True)

        out2 = CaptureOutput()
        server2 = AcpServer(stdin=StringIO(), stdout=out2)
        server2.handle({"jsonrpc": "2.0", "id": 4, "method": "session/load", "params": {"sessionId": sid, "cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []}})
        loaded = out2.wait_for(lambda m: m.get("id") == 4)
        self.assertEqual(loaded["result"]["sessionId"], sid)
        restored = server2._get_session(sid)
        assert restored is not None
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == "remember" for m in restored.messages))
        self.assertTrue(any(m.get("role") == "assistant" and m.get("content") == "remember this" for m in restored.messages))
        server2.handle({"jsonrpc": "2.0", "id": 5, "method": "_kiro/session/delete", "params": {"sessionId": sid}})
        self.assertFalse(AcpServer._session_path(sid).exists())
        server2._executor.shutdown(wait=True)

    def test_sticky_route_pins_survive_session_reload(self) -> None:
        AcpServer._mcp_from_params = staticmethod(lambda _params: FakeMcp())
        server = AcpServer(stdin=StringIO(), stdout=CaptureOutput())
        session = Session(
            "sticky",
            str(Path(self.tmp.name).resolve()),
            type_selection_id("tiga", "glitter"),
        )
        session.route_pins["tiga:glitter"] = ("p", "strong")
        server._persist_session(session)
        server._executor.shutdown(wait=True)

        server2 = AcpServer(stdin=StringIO(), stdout=CaptureOutput())
        restored, _config = server2._restore_session(
            "sticky",
            {"cwd": str(Path(self.tmp.name).resolve()), "mcpServers": []},
            replay=False,
        )
        self.assertEqual(restored.route_pins.get("tiga:glitter"), ("p", "strong"))
        server2._executor.shutdown(wait=True)

    def test_initialize_advertises_load_session(self) -> None:
        out = CaptureOutput()
        server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        msg = out.wait_for(lambda m: m.get("id") == 1)
        self.assertTrue(msg["result"]["agentCapabilities"]["loadSession"])
        server._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()


class NativeToolAcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "hello.txt").write_text("hello native\n", encoding="utf-8")
        self.config = make_test_config()
        self.orig_config = AcpServer._config
        self.orig_mcp = AcpServer._mcp_from_params
        self.orig_turn = turn_mod.chat_completion_turn
        AcpServer._config = staticmethod(lambda: self.config)
        self.fake_mcp = FakeMcp()
        AcpServer._mcp_from_params = staticmethod(lambda _params: self.fake_mcp)

    def tearDown(self) -> None:
        AcpServer._config = self.orig_config
        AcpServer._mcp_from_params = self.orig_mcp
        turn_mod.chat_completion_turn = self.orig_turn
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def _start(self, turn_fn):
        inp, out = QueueInput(), CaptureOutput()
        turn_mod.chat_completion_turn = turn_fn
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": str(self.workspace), "mcpServers": []},
            }
        )
        created = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)
        return inp, out, server, thread, created["result"]["sessionId"]

    @staticmethod
    def _stop(inp: QueueInput, thread: threading.Thread) -> None:
        inp.close()
        thread.join(3)
        if thread.is_alive():
            raise AssertionError("ACP server thread did not stop")


    def test_glitter_auto_runs_tool_free_final_review(self) -> None:
        self.config = replace(self.config, mode_type="glitter")
        turns = iter([ChatTurn(text="draft answer"), ChatTurn(text="reviewed answer")])
        calls: list[dict[str, Any]] = []

        def turn(*args, **kwargs):
            calls.append({"model": args[1], "tools": kwargs.get("tools"), "messages": args[2]})
            return next(turns)

        inp, out, server, thread, sid = self._start(turn)
        session = server._get_session(sid)
        self.assertIsNotNone(session)
        session.model = type_selection_id("tiga", "glitter")
        inp.put({
            "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "analyze carefully"}]},
        })
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(calls[0]["tools"], list)
        self.assertEqual(calls[1]["tools"], [])
        deliverables = [
            m["params"]["update"]["content"]["text"]
            for m in out.messages()
            if m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"
            and m["params"]["update"].get("content", {}).get("type") == "text"
        ]
        self.assertEqual(deliverables[-1], "reviewed answer")
        self.assertNotIn("draft answer", deliverables)
        live_session = server._get_session(sid)
        assert live_session is not None
        self.assertEqual(live_session.messages[-1]["content"], "reviewed answer")
        roles = {task.spec.role: task.status for task in live_session.agent_registry.list()}
        self.assertEqual(roles.get("lead"), "completed")
        self.assertEqual(roles.get("worker"), "skipped")
        self.assertEqual(roles.get("review"), "completed")
        self.assertEqual(live_session.active_agent_plan_id, "")
        self._stop(inp, thread)

    def test_session_roots_follow_acp_additional_directories_and_must_be_resent_on_load(self) -> None:
        shared = Path(self.tmp.name) / "shared"
        shared.mkdir()
        shared_file = shared / "shared.txt"
        shared_file.write_text("shared\n", encoding="utf-8")

        out = CaptureOutput()
        server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {
                    "cwd": str(self.workspace),
                    "additionalDirectories": [str(shared)],
                    "mcpServers": [],
                },
            }
        )
        sid = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)["result"]["sessionId"]
        native = server.sessions[sid].native
        assert native is not None
        invocation = native.validate("read", {"path": str(shared_file)})
        native.preflight(invocation)

        server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/close", "params": {"sessionId": sid}})
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)

        # ACP client-owned roots are not resurrected from Kiro BYO persistence.
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/load",
                "params": {"sessionId": sid, "cwd": str(self.workspace), "mcpServers": []},
            }
        )
        out.wait_for(lambda m: m.get("id") == 3 and "result" in m)
        native = server.sessions[sid].native
        assert native is not None
        with self.assertRaises(SandboxError):
            native.preflight(native.validate("read", {"path": str(shared_file)}))

        server.handle({"jsonrpc": "2.0", "id": 4, "method": "session/close", "params": {"sessionId": sid}})
        out.wait_for(lambda m: m.get("id") == 4 and "result" in m)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/load",
                "params": {
                    "sessionId": sid,
                    "cwd": str(self.workspace),
                    "additionalDirectories": [str(shared)],
                    "mcpServers": [],
                },
            }
        )
        out.wait_for(lambda m: m.get("id") == 5 and "result" in m)
        native = server.sessions[sid].native
        assert native is not None
        native.preflight(native.validate("read", {"path": str(shared_file)}))
        server._executor.shutdown(wait=True)

    def test_session_new_rejects_missing_or_relative_cwd_as_invalid_params(self) -> None:
        out = CaptureOutput()
        server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"mcpServers": []}})
        missing = out.wait_for(lambda m: m.get("id") == 1 and "error" in m)
        self.assertEqual(missing["error"]["code"], -32602)
        self.assertIn("cwd is required", missing["error"]["message"])
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "relative/path", "mcpServers": []},
            }
        )
        relative = out.wait_for(lambda m: m.get("id") == 2 and "error" in m)
        self.assertEqual(relative["error"]["code"], -32602)
        self.assertIn("absolute", relative["error"]["message"])
        server._executor.shutdown(wait=True)

    def test_native_schemas_are_visible_alongside_deferred_mcp_search(self) -> None:
        seen_tools: list[list[str]] = []

        def turn(*args, **kwargs):
            seen_tools.append(
                [tool["function"]["name"] for tool in (kwargs.get("tools") or [])]
            )
            return ChatTurn(text="done")

        inp, out, _server, thread, sid = self._start(turn)
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "inspect"}]},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2)
        self.assertEqual(
            seen_tools[0][:3],
            ["read", "glob", "grep"],
        )
        self.assertIn(DEFERRED_TOOL_SEARCH_NAME, seen_tools[0])
        self._stop(inp, thread)

    def test_legacy_tool_approval_config_is_ignored_and_crew_still_gates(self) -> None:
        policy = Path(self.tmp.name) / "config" / "native_tools.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(
            json.dumps({"tool_approval": {"write": "trusted"}}), encoding="utf-8"
        )
        # Kiro BYO no longer maintains an App-local approval policy. The legacy
        # key is tolerated for migration, but it must not appear in the active
        # policy or bypass Crew's host approval surface.
        turns = iter(
            [
                ChatTurn(
                    tool_calls=(ToolCall("write-1", "write", '{"path":"trusted.txt","content":"x"}'),),
                    text="",
                ),
                ChatTurn(text="done"),
            ]
        )
        inp, out, _server, thread, sid = self._start(lambda *a, **kw: next(turns))
        session = _server._get_session(sid)
        self.assertIsNotNone(session)
        self.assertNotIn("tool_approval", session.native.policy_summary())
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "write"}]},
            }
        )
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        self.assertEqual(req["params"]["toolCall"]["kind"], "edit")
        self.assertFalse((self.workspace / "trusted.txt").exists())
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual((self.workspace / "trusted.txt").read_text(encoding="utf-8"), "x")
        self._stop(inp, thread)

    def test_allowed_native_read_skips_permission_but_keeps_sandbox(self) -> None:
        turns = iter([
            ChatTurn(tool_calls=(ToolCall("native-auto", "read", '{"path":"hello.txt"}'),), text=""),
            ChatTurn(text="done"),
        ])
        inp, out = QueueInput(), CaptureOutput()
        turn_mod.chat_completion_turn = lambda *a, **kw: next(turns)
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put({
            "jsonrpc": "2.0", "id": 1, "method": "session/new",
            "params": {
                "cwd": str(self.workspace), "mcpServers": [],
                "_meta": {"kiro": {
                    "modeId": "kirocrew",
                    "customAgents": [{
                        "id": "kirocrew", "prompt": "default",
                        "tools": ["read"], "allowedTools": ["read"],
                    }],
                }},
            },
        })
        sid = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)["result"]["sessionId"]
        inp.put({
            "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "read"}]},
        })
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertFalse(any(m.get("method") == "session/request_permission" for m in out.messages()))
        self.assertTrue(any(
            msg.get("role") == "tool" and "hello native" in str(msg.get("content"))
            for msg in server.sessions[sid].messages
        ))
        inp.close(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_mid_tool_steer_interrupts_shell_and_replans_same_turn(self) -> None:
        command = "Start-Sleep -Seconds 5" if os.name == "nt" else "sleep 5"
        calls: list[list[dict[str, Any]]] = []

        def turn(*args, **kwargs):
            messages = list(args[2])
            calls.append(messages)
            if len(calls) == 1:
                return ChatTurn(
                    tool_calls=(
                        ToolCall(
                            "shell-steer-1",
                            "shell",
                            json.dumps(
                                {"command": command, "cwd": ".", "timeout_seconds": 10}
                            ),
                        ),
                    ),
                    text="",
                )
            return ChatTurn(text="short answer")

        inp, out = QueueInput(), CaptureOutput()
        turn_mod.chat_completion_turn = turn
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {
                    "cwd": str(self.workspace),
                    "mcpServers": [],
                    "_meta": {
                        "kiro": {
                            "modeId": "kirocrew",
                            "customAgents": [
                                {
                                    "id": "kirocrew",
                                    "prompt": "default",
                                    "tools": ["shell"],
                                    "allowedTools": ["shell"],
                                }
                            ],
                        }
                    },
                },
            }
        )
        sid = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)["result"]["sessionId"]

        started = time.monotonic()
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "inspect deeply"}]},
            }
        )
        out.wait_for(
            lambda m: m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("toolCallId") == "shell-steer-1"
            and m.get("params", {}).get("update", {}).get("status") == "in_progress"
        )
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "_session/steer",
                "params": {"sessionId": sid, "message": "不要继续跑，简单看看"},
            }
        )
        ack = out.wait_for(lambda m: m.get("id") == 3)
        self.assertTrue(ack["result"]["queued"])
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m, timeout=3.0)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(calls), 2)
        second = calls[1]
        self.assertTrue(
            any(
                m.get("role") == "tool"
                and "interrupted by user steering" in str(m.get("content", ""))
                for m in second
            )
        )
        self.assertTrue(
            any(
                m.get("role") == "user" and m.get("content") == "不要继续跑，简单看看"
                for m in second
            )
        )
        self.assertFalse(any(m.get("method") == "session/request_permission" for m in out.messages()))
        self._stop(inp, thread)

    def test_native_read_waits_for_host_permission_then_executes(self) -> None:
        turns = iter(
            [
                ChatTurn(tool_calls=(ToolCall("native-1", "read", '{"path":"hello.txt"}'),), text=""),
                ChatTurn(text="done"),
            ]
        )
        inp, out, server, thread, sid = self._start(lambda *a, **kw: next(turns))
        native = server.sessions[sid].native
        self.assertIsNotNone(native)
        assert native is not None
        original_execute = native.execute
        executions: list[str] = []

        def execute(invocation):
            executions.append(invocation.spec.name)
            return original_execute(invocation)

        native.execute = execute  # type: ignore[method-assign]
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "read it"}]},
            }
        )
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        tool_call = req["params"]["toolCall"]
        self.assertEqual(tool_call["kind"], "read")
        self.assertEqual(tool_call["rawInput"]["path"], "hello.txt")
        self.assertEqual(tool_call["_meta"]["kiro"]["toolId"], "read")
        self.assertEqual(executions, [])
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(executions, ["read"])
        self.assertTrue(
            any(
                msg.get("role") == "tool" and "hello native" in str(msg.get("content"))
                for msg in server.sessions[sid].messages
            )
        )
        self._stop(inp, thread)

    def test_schema_validation_fails_before_permission_request(self) -> None:
        turns = iter(
            [
                ChatTurn(tool_calls=(ToolCall("bad-1", "read", "{}"),), text=""),
                ChatTurn(text="done"),
            ]
        )
        inp, out, server, thread, sid = self._start(lambda *a, **kw: next(turns))
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "read"}]},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        permission_requests = [
            m for m in out.messages() if m.get("method") == "session/request_permission"
        ]
        self.assertEqual(permission_requests, [])
        self.assertTrue(
            any(
                msg.get("role") == "tool" and "invalid read arguments" in str(msg.get("content"))
                for msg in server.sessions[sid].messages
            )
        )
        self._stop(inp, thread)

    def test_prompt_result_reports_real_tokens_and_context_usage(self) -> None:
        model = replace(self.config.models[0], context_window=32000)
        provider = replace(self.config.providers[0], models=(model,))
        self.config = replace(self.config, providers=(provider,))
        turn = ChatTurn(
            text="done",
            usage=UsageInfo(
                input_tokens=1200,
                output_tokens=80,
                cached_tokens=300,
                cache_write_tokens=55,
                reasoning_tokens=40,
                cached_tokens_reported=True,
                cache_write_tokens_reported=True,
                reasoning_tokens_reported=True,
            ),
        )
        inp, out, _server, thread, sid = self._start(lambda *a, **kw: turn)
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "usage"}]},
            }
        )
        response = out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertEqual(
            {k: response["result"][k] for k in ("inputTokens", "outputTokens", "cachedReadTokens", "cachedWriteTokens", "reasoningTokens")},
            {
                "inputTokens": 1200,
                "outputTokens": 80,
                "cachedReadTokens": 300,
                "cachedWriteTokens": 55,
                "reasoningTokens": 40,
            },
        )
        usage = out.wait_for(
            lambda m: m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("sessionUpdate") == "usage_update"
        )
        self.assertEqual(usage["params"]["update"]["used"], 1280)
        self.assertEqual(usage["params"]["update"]["size"], 32000)
        context_info = out.wait_for(
            lambda m: m.get("method") == "session/update"
            and m.get("params", {}).get("update", {}).get("sessionUpdate") == "session_info_update"
            and m.get("params", {}).get("update", {}).get("_meta", {}).get("kiro", {}).get("kind") == "context_usage"
        )
        self.assertAlmostEqual(
            context_info["params"]["update"]["_meta"]["kiro"]["usagePercentage"],
            4.0,
        )
        self._stop(inp, thread)


    def test_web_preflight_failure_is_tool_result_not_prompt_fatal(self) -> None:
        from unittest.mock import patch
        from kiro_byo.web_tools import WebToolError

        turns = iter(
            [
                ChatTurn(tool_calls=(ToolCall("web-1", "web_fetch", '{"url":"https://blocked.example/"}'),), text=""),
                ChatTurn(text="recovered after blocked source"),
            ]
        )
        inp, out, server, thread, sid = self._start(lambda *a, **kw: next(turns))
        with patch(
            "kiro_byo.web_tools.SafeWebClient._assert_public_url",
            side_effect=WebToolError("web host blocked.example resolved only to blocked destinations (private)"),
        ):
            inp.put(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "fetch"}]},
                }
            )
            response = out.wait_for(lambda m: m.get("id") == 2)
        self.assertIn("result", response)
        self.assertNotIn("error", response)
        self.assertTrue(
            any(
                msg.get("role") == "tool"
                and "blocked destinations (private)" in str(msg.get("content"))
                for msg in server.sessions[sid].messages
            )
        )
        self._stop(inp, thread)

    def test_unknown_context_window_does_not_publish_fake_usage_window(self) -> None:
        turn = ChatTurn(text="done", usage=UsageInfo(input_tokens=100, output_tokens=10))
        inp, out, _server, thread, sid = self._start(lambda *a, **kw: turn)
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "usage"}]},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertFalse(
            any(
                m.get("method") == "session/update"
                and m.get("params", {}).get("update", {}).get("sessionUpdate") == "usage_update"
                for m in out.messages()
            )
        )
        self._stop(inp, thread)

    def test_out_of_scope_read_requests_host_permission_and_allow_once_is_ephemeral(self) -> None:
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("user approved external file\n", encoding="utf-8")
        turns = iter(
            [
                ChatTurn(tool_calls=(ToolCall("scope-1", "read", '{"path":"../secret.txt"}'),), text=""),
                ChatTurn(text="done"),
            ]
        )
        inp, out, server, thread, sid = self._start(lambda *a, **kw: next(turns))
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "inspect it"}]},
            }
        )
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        self.assertEqual(
            req["params"]["toolCall"]["title"],
            tool_display_title("read", native_name="read"),
        )
        self.assertIn("../secret.txt", req["params"]["_meta"]["kiro"]["consent"]["resource"])
        # Nothing executes until the user/host approves the directory access.
        self.assertFalse(
            any(
                msg.get("role") == "tool" and "user approved external file" in str(msg.get("content"))
                for msg in server.sessions[sid].messages
            )
        )
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        self.assertTrue(
            any(
                msg.get("role") == "tool" and "user approved external file" in str(msg.get("content"))
                for msg in server.sessions[sid].messages
            )
        )
        native = server.sessions[sid].native
        assert native is not None
        # allow_once must not silently turn into a permanent workspace expansion.
        with self.assertRaises(SandboxError):
            native.preflight(native.validate("read", {"path": str(outside)}))
        audit_path = Path(self.tmp.name) / "data" / "audit" / "native_tools.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["permission"], "scope_once")
        self.assertEqual(records[-1]["outcome"], "completed")
        self._stop(inp, thread)

    def test_out_of_scope_allow_always_keeps_directory_for_session_only(self) -> None:
        external = Path(self.tmp.name) / "external-project"
        external.mkdir()
        target = external / "README.md"
        target.write_text("external project\n", encoding="utf-8")
        turns = iter(
            [
                ChatTurn(
                    tool_calls=(
                        ToolCall("scope-session-1", "read", json.dumps({"path": str(target)})),
                    ),
                    text="",
                ),
                ChatTurn(text="done"),
            ]
        )
        inp, out, server, thread, sid = self._start(lambda *a, **kw: next(turns))
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "inspect project"}]},
            }
        )
        req = out.wait_for(lambda m: m.get("method") == "session/request_permission")
        inp.put(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_always"}},
            }
        )
        out.wait_for(lambda m: m.get("id") == 2 and "result" in m)
        native = server.sessions[sid].native
        assert native is not None
        # The approved root remains usable for this live session.
        native.preflight(native.validate("read", {"path": str(target)}))
        self.assertTrue(any(Path(root) == external.resolve() for root in native.sandbox.allowed_roots))
        self._stop(inp, thread)

class AcpCrew09SavedAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.config = make_test_config()
        self.orig_config = AcpServer._config
        self.orig_mcp = AcpServer._mcp_from_params
        self.orig_turn = turn_mod.chat_completion_turn
        AcpServer._config = staticmethod(lambda: self.config)
        AcpServer._mcp_from_params = staticmethod(lambda _params: FakeMcp())

    def tearDown(self) -> None:
        AcpServer._config = self.orig_config
        AcpServer._mcp_from_params = self.orig_mcp
        turn_mod.chat_completion_turn = self.orig_turn
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def _params(self, prompt: str = "You are Crew Agent A.") -> dict[str, Any]:
        return {
            "cwd": str(Path(self.tmp.name).resolve()),
            "mcpServers": [],
            "_meta": {"kiro": {"customAgents": [{"id": "agent-a", "prompt": prompt, "tools": ["*"]}]}},
        }

    def test_session_new_acknowledges_consumed_saved_agent(self) -> None:
        inp, out = QueueInput(), CaptureOutput()
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": self._params()})
        result = out.wait_for(lambda m: m.get("id") == 1)["result"]
        self.assertEqual(result["modes"]["currentModeId"], "agent-a")
        sid = result["sessionId"]
        session = server._get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session.agent.agent_id, "agent-a")
        self.assertEqual(session.agent.prompt, "You are Crew Agent A.")
        inp.close()
        thread.join(3)

    def test_saved_agent_prompt_is_sent_to_provider(self) -> None:
        captured: list[list[dict[str, Any]]] = []
        def fake_turn(provider, model, messages, emit, **kwargs):
            captured.append(messages)
            emit("ok")
            return ChatTurn(text="ok")
        turn_mod.chat_completion_turn = fake_turn
        inp, out = QueueInput(), CaptureOutput()
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": self._params("SAVED AGENT PROMPT")})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]}})
        out.wait_for(lambda m: m.get("id") == 2)
        self.assertTrue(captured)
        self.assertEqual(captured[0][0], {"role": "system", "content": "SAVED AGENT PROMPT"})
        inp.close()
        thread.join(3)

    def test_real_provider_receives_crew_identity_and_widget_guidance_on_first_turn(self) -> None:
        captured: list[list[dict[str, Any]]] = []
        def fake_turn(provider, model, messages, emit, **kwargs):
            captured.append(messages)
            emit('hello')
            return ChatTurn(text='hello')
        turn_mod.chat_completion_turn = fake_turn
        inp, out = QueueInput(), CaptureOutput()
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            inp.put({'jsonrpc': '2.0', 'id': 1, 'method': 'session/new', 'params': self._params('Crew member persona')})
            sid = out.wait_for(lambda m: m.get('id') == 1)['result']['sessionId']
            inp.put({'jsonrpc': '2.0', 'id': 2, 'method': 'session/prompt', 'params': {
                'sessionId': sid, 'prompt': [{'type': 'text', 'text': '画一下tiga和dyna的形态图对比分析'}]
            }})
            out.wait_for(lambda m: m.get('id') == 2, timeout=8.0)
            assert captured
            self.assertEqual(captured[0][0], {'role': 'system', 'content': 'Crew member persona'})
            joined = '\n'.join(str(row.get('content') or '') for row in captured[0] if row.get('role') == 'system')
            self.assertIn('Host application: KiroCrew', joined)
            self.assertIn('Execution backend: Kiro BYO', joined)
            self.assertIn('Crew-projected agent id: agent-a', joined)
            self.assertIn('<mcwidget title=', joined)
            self.assertIn('not the model\'s personal identity', joined) if 'Active BYO Mode:' in joined else self.assertIn('concrete model direct', joined)
        finally:
            inp.close()
            thread.join(4)
            server._executor.shutdown(wait=True)

    def test_dyna_flash_is_explained_as_current_crew_backend_type(self) -> None:
        self.config = replace(self.config, mode_pack="dyna", mode_type="flash")
        captured: list[list[dict[str, Any]]] = []
        def fake_turn(provider, model, messages, emit, **kwargs):
            captured.append(messages)
            emit("ok")
            return ChatTurn(text="ok")
        turn_mod.chat_completion_turn = fake_turn
        inp, out = QueueInput(), CaptureOutput()
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": self._params()})
            sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
            self.assertEqual(server._get_session(sid).model, type_selection_id("dyna", "flash"))
            inp.put({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {
                "sessionId": sid, "prompt": [{"type": "text", "text": "你是哪种形态？"}]
            }})
            out.wait_for(lambda m: m.get("id") == 2, timeout=8.0)
            self.assertTrue(captured)
            system = "\n".join(str(row.get("content") or "") for row in captured[0] if row.get("role") == "system")
            self.assertIn("Host application: KiroCrew", system)
            self.assertIn("Active BYO Mode: dyna; active Type: flash", system)
            self.assertNotIn("Model selection: concrete model direct", system)
        finally:
            inp.close()
            thread.join(4)
            server._executor.shutdown(wait=True)

    def test_prompt_injects_auto_reasoning_language_into_real_provider_messages(self) -> None:
        captured: list[list[dict[str, Any]]] = []
        def fake_turn(provider, model, messages, emit, **kwargs):
            captured.append(messages)
            emit("ok")
            return ChatTurn(text="ok")
        turn_mod.chat_completion_turn = fake_turn
        inp, out = QueueInput(), CaptureOutput()
        server = AcpServer(stdin=inp, stdout=out)
        thread = threading.Thread(target=server.run)
        thread.start()
        inp.put({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": self._params()})
        sid = out.wait_for(lambda m: m.get("id") == 1)["result"]["sessionId"]
        inp.put({
            "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "帮我检查 ACP 的 Token 路径"}]},
        })
        out.wait_for(lambda m: m.get("id") == 2)
        self.assertTrue(captured)
        system_text = "\n".join(
            str(row.get("content") or "") for row in captured[0] if row.get("role") == "system"
        )
        self.assertIn("Reasoning presentation language", system_text)
        self.assertIn("Simplified Chinese (zh-CN)", system_text)
        self.assertIn("ACP, LLM, Token, API", system_text)
        inp.close()
        thread.join(3)

    def test_session_load_reprojects_fresh_saved_agent_prompt(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=CaptureOutput())
        cfg = self.config
        first = Session(
            "persisted",
            str(Path(self.tmp.name).resolve()),
            "Test Model",
            agent=AgentProjection(agent_id="agent-a", prompt="old"),
        )
        server._persist_session(first)
        # Simulate Crew 0.9 loading the durable id while projecting an updated template.
        params = self._params("new") | {"sessionId": "persisted"}
        out = CaptureOutput()
        server.stdout = out
        server.handle({"jsonrpc": "2.0", "id": 7, "method": "session/load", "params": params})
        result = out.wait_for(lambda m: m.get("id") == 7)["result"]
        self.assertEqual(result["modes"]["currentModeId"], "agent-a")
        self.assertEqual(server._get_session("persisted").agent.prompt, "new")


    def test_session_load_requires_fresh_authorization_projection(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=CaptureOutput())
        first = Session(
            "persisted-auth",
            str(Path(self.tmp.name).resolve()),
            "Test Model",
            agent=AgentProjection(
                agent_id="agent-a",
                prompt="old",
                allowed_tools=("@kirocrew-dashboard",),
            ),
        )
        server._persist_session(first)

        # Authorization is client-owned and must never survive only because Kiro BYO
        # happened to persist the conversation. A resumed member gets a fresh Crew
        # projection on session/load instead.
        persisted = json.loads(AcpServer._session_path("persisted-auth").read_text(encoding="utf-8"))
        self.assertNotIn("allowedTools", persisted["agent"])

        params = self._params("fresh") | {"sessionId": "persisted-auth"}
        params["_meta"]["kiro"]["customAgents"][0]["allowedTools"] = ["@kirocrew-panel"]
        out = CaptureOutput()
        server.stdout = out
        server.handle({"jsonrpc": "2.0", "id": 8, "method": "session/load", "params": params})
        out.wait_for(lambda m: m.get("id") == 8 and "result" in m)
        restored = server._get_session("persisted-auth")
        assert restored is not None
        self.assertEqual(restored.agent.allowed_tools, ("@kirocrew-panel",))
        self.assertNotIn("@kirocrew-dashboard", restored.agent.allowed_tools)


class SelectionTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.config = make_test_config()
        self.orig_config = AcpServer._config
        self.orig_mcp = AcpServer._mcp_from_params
        AcpServer._config = staticmethod(lambda: self.config)
        AcpServer._mcp_from_params = staticmethod(lambda _params: FakeMcp())

    def tearDown(self) -> None:
        AcpServer._config = self.orig_config
        AcpServer._mcp_from_params = self.orig_mcp
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    def _new(self, *, model_id: str = "") -> tuple[AcpServer, Session]:
        out = CaptureOutput()
        server = AcpServer(stdin=StringIO(), stdout=out)
        params: dict[str, Any] = {"cwd": str(self.workspace), "mcpServers": []}
        if model_id:
            params["_meta"] = {"kiro": {"modelId": model_id}}
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": params})
        result = out.wait_for(lambda m: m.get("id") == 1 and "result" in m)
        session = server._get_session(result["result"]["sessionId"])
        assert session is not None
        return server, session

    def test_default_session_selection_is_truthful_tiga_type(self) -> None:
        server, session = self._new()
        truth = server._selection_truth_payload(session, self.config)
        self.assertEqual(session.model, type_selection_id(self.config.mode_pack, self.config.mode_type))
        self.assertEqual(truth["kind"], "type")
        self.assertEqual(truth["source"], "default_type")
        self.assertTrue(truth["routing_applied"])
        server._executor.shutdown(wait=True)

    def test_host_concrete_model_pin_is_explicit_and_bypasses_type_routing(self) -> None:
        server, session = self._new(model_id="test-model")
        truth = server._selection_truth_payload(session, self.config)
        self.assertEqual(truth["kind"], "concrete")
        self.assertEqual(truth["source"], "host_session_new")
        self.assertEqual(truth["requested"], "test-model")
        self.assertFalse(truth["routing_applied"])
        server._executor.shutdown(wait=True)

    def test_switching_back_to_type_updates_selection_truth(self) -> None:
        server, session = self._new(model_id="test-model")
        requested = type_selection_id("tiga", "power")
        self.assertTrue(server._set_model(session, self.config, requested))
        truth = server._selection_truth_payload(session, self.config)
        self.assertEqual(truth["kind"], "type")
        self.assertEqual(truth["source"], "user_set_config")
        self.assertEqual(truth["requested"], requested)
        self.assertTrue(truth["routing_applied"])
        server._executor.shutdown(wait=True)

    def test_selection_truth_survives_persist_and_load(self) -> None:
        server, session = self._new(model_id="test-model")
        sid = session.session_id
        server._persist_session(session)
        server._executor.shutdown(wait=True)

        server2 = AcpServer(stdin=StringIO(), stdout=CaptureOutput())
        restored, _ = server2._restore_session(
            sid, {"cwd": str(self.workspace), "mcpServers": []}, replay=False
        )
        self.assertEqual(restored.selection_kind, "concrete")
        self.assertEqual(restored.selection_source, "host_session_new")
        self.assertEqual(restored.selection_requested, "test-model")
        server2._executor.shutdown(wait=True)
