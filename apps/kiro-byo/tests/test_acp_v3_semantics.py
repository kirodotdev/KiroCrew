from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO
from pathlib import Path

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.protocol import EFFORT_CONFIG_ID, EXTENSION_COMPACT, EXTENSION_STEER
from kiro_byo.providers import ChatTurn


class EmptyMcp:
    servers = {}
    bindings = {}
    loaded_tool_names = []
    startup_durations_ms = {}
    startup_failures = {}
    catalog_cache_hits = set()
    def start_background(self): pass
    def close(self): pass
    def signature(self): return ""


class AcpV3SemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        model = ModelSpec(
            provider_id="p", provider_name="P", model_id="m", name="Model",
            effort_supported_levels=("low", "high"), effort_default_level="low",
        )
        provider = ProviderSpec(
            id="p", name="P", type="openai-compatible",
            base_url="https://example.invalid/v1", api_key="", models=(model,),
        )
        self.config = KiroByoConfig(providers=(provider,), default_model="Model")
        self.old_config = AcpServer._config
        self.old_mcp = AcpServer._mcp_from_params
        AcpServer._config = staticmethod(lambda: self.config)
        AcpServer._mcp_from_params = staticmethod(lambda _params: EmptyMcp())

    def tearDown(self) -> None:
        AcpServer._config = self.old_config
        AcpServer._mcp_from_params = self.old_mcp
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()

    @staticmethod
    def _messages(out: StringIO) -> list[dict]:
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def test_initialize_is_capability_negotiated_not_product_guessed(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}})
        result = self._messages(out)[-1]["result"]
        self.assertEqual(result["protocolVersion"], 1)
        self.assertTrue(result["agentCapabilities"]["loadSession"])
        caps = result["agentCapabilities"]["sessionCapabilities"]
        self.assertEqual(set(caps), {"list", "delete", "resume", "close", "additionalDirectories"})
        self.assertTrue(result["agentCapabilities"]["mcpCapabilities"]["http"])
        prompt_caps = result["agentCapabilities"]["promptCapabilities"]
        self.assertTrue(prompt_caps["image"])
        self.assertFalse(prompt_caps["embeddedContext"])
        methods = result["agentCapabilities"]["_meta"]["kiro"]["extensionMethods"]
        self.assertEqual(methods, [EXTENSION_STEER, EXTENSION_COMPACT])
        server._executor.shutdown(wait=True)

    def test_prompt_message_preserves_baseline_resource_links(self) -> None:
        message, has_image = AcpServer.prompt_message({
            "prompt": [
                {"type":"text", "text":"Inspect this: "},
                {
                    "type":"resource_link",
                    "name":"notes",
                    "uri":"file:///workspace/notes.md",
                    "description":"project notes",
                },
            ]
        })
        self.assertFalse(has_image)
        self.assertIn("Inspect this:", message["content"])
        self.assertIn("file:///workspace/notes.md", message["content"])
        self.assertIn("project notes", message["content"])

    def test_prompt_message_tolerates_text_embedded_resource_without_advertising_it(self) -> None:
        message, has_image = AcpServer.prompt_message({
            "prompt": [{
                "type":"resource",
                "resource": {
                    "uri":"file:///workspace/context.txt",
                    "mimeType":"text/plain",
                    "text":"embedded facts",
                },
            }]
        })
        self.assertFalse(has_image)
        self.assertIn("embedded facts", message["content"])
        self.assertIn("context.txt", message["content"])

    def test_load_replays_history_before_response_resume_does_not(self) -> None:
        seed = AcpServer(stdin=StringIO(), stdout=StringIO())
        s = Session("saved", str(Path(self.tmp.name).resolve()), "Model")
        s.messages = [
            {"role":"user","content":"hello"},
            {"role":"assistant","content":"world"},
        ]
        seed._persist_session(s); seed._executor.shutdown(wait=True)

        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/load","params":{"sessionId":"saved","cwd":s.cwd,"mcpServers":[]}})
        frames = self._messages(out)
        response_index = next(i for i, frame in enumerate(frames) if frame.get("id") == 2)
        replay = [f for f in frames[:response_index] if f.get("method") == "session/update"]
        self.assertEqual([f["params"]["update"]["sessionUpdate"] for f in replay], ["user_message_chunk", "agent_message_chunk"])
        load_result = frames[response_index]["result"]
        self.assertEqual(load_result["modes"]["currentModeId"], "default")
        self.assertEqual(load_result["modes"]["availableModes"][0]["id"], "default")
        server.handle({"jsonrpc":"2.0","id":3,"method":"session/close","params":{"sessionId":"saved"}})
        server._executor.shutdown(wait=True)

        out2 = StringIO(); server2 = AcpServer(stdin=StringIO(), stdout=out2)
        server2.handle({"jsonrpc":"2.0","id":4,"method":"session/resume","params":{"sessionId":"saved","cwd":s.cwd,"mcpServers":[]}})
        frames2 = self._messages(out2)
        self.assertFalse(any(f.get("method") == "session/update" for f in frames2))
        self.assertEqual(frames2[-1]["id"], 4)
        server2._executor.shutdown(wait=True)


    def test_schema_v3_session_persistence_uses_events_as_canonical_source(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("evented", str(Path(self.tmp.name).resolve()), "Model")
        # Direct message mutation is intentionally tolerated for old integrations;
        # persistence converts it into an explicit transcript/reset event.
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        server._persist_session(session)
        raw = json.loads(server._session_path("evented").read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 3)
        self.assertEqual(raw["session_event_schema_version"], 2)
        self.assertTrue(raw["events"])

        # Corrupt only a synthetic compatibility snapshot. Schema-v3 restore must ignore
        # it and derive the active transcript from the canonical event stream.
        raw["messages"] = [{"role": "assistant", "content": "WRONG SNAPSHOT"}]
        server._session_path("evented").write_text(json.dumps(raw), encoding="utf-8")
        record = server._load_persisted_record("evented")
        self.assertIsNotNone(record)
        self.assertEqual(
            record["messages"],
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        )
        server._executor.shutdown(wait=True)

    def test_schema_v1_session_record_migrates_to_events_on_read_and_v3_on_write(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        sid = "legacy-event-migration"
        path = server._session_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "schema_version": 1,
            "session_id": sid,
            "cwd": str(Path(self.tmp.name).resolve()),
            "model": "Model",
            "messages": [{"role": "user", "content": "legacy"}],
        }
        path.write_text(json.dumps(legacy), encoding="utf-8")
        record = server._load_persisted_record(sid)
        self.assertIsNotNone(record)
        self.assertEqual(record["messages"], legacy["messages"])
        self.assertEqual(record["events"][0]["type"], "user/message")

        restored, _ = server._restore_session(
            sid, {"cwd": legacy["cwd"], "mcpServers": []}, replay=False
        )
        server._persist_session(restored)
        upgraded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertTrue(upgraded["events"])
        server._executor.shutdown(wait=True)


    def test_schema_v2_route_pins_migrate_into_event_stream(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        sid = "legacy-route-pin"
        path = server._session_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "session_event_schema_version": 1,
            "session_id": sid,
            "cwd": str(Path(self.tmp.name).resolve()),
            "model": "Model",
            "events": [
                {"seq": 0, "time": 1.0, "type": "user/message", "data": {"message": {"role": "user", "content": "hello"}}}
            ],
            "route_pins": {"tiga:glitter": ["p", "strong"]},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        record = server._load_persisted_record(sid)
        self.assertIsNotNone(record)
        self.assertEqual(record["route_pins"], {"tiga:glitter": ("p", "strong")})
        self.assertEqual(record["events"][-1]["type"], "routing/pin")

        restored, _ = server._restore_session(
            sid, {"cwd": payload["cwd"], "mcpServers": []}, replay=False
        )
        server._persist_session(restored)
        upgraded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertNotIn("route_pins", upgraded)
        self.assertTrue(any(event["type"] == "routing/pin" for event in upgraded["events"]))
        server._executor.shutdown(wait=True)

    def test_session_list_returns_persisted_session_info(self) -> None:
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        a = Session("a", str(Path(self.tmp.name).resolve()), "Model")
        bdir = Path(self.tmp.name) / "b"; bdir.mkdir()
        b = Session("b", str(bdir.resolve()), "Model")
        server._persist_session(a); server._persist_session(b); server._executor.shutdown(wait=True)
        out = StringIO(); listed = AcpServer(stdin=StringIO(), stdout=out)
        listed.handle({"jsonrpc":"2.0","id":7,"method":"session/list","params":{"cwd":a.cwd}})
        result = self._messages(out)[-1]["result"]
        self.assertEqual([x["sessionId"] for x in result["sessions"]], ["a"])
        self.assertEqual(result["sessions"][0]["cwd"], a.cwd)
        listed._executor.shutdown(wait=True)

    def test_latest_effort_config_id_and_legacy_aliases(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[],"_meta":{"kiro":{"modelId":"Model"}}}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/set_config_option","params":{"sessionId":sid,"configId":EFFORT_CONFIG_ID,"value":"high"}})
        self.assertEqual(server._get_session(sid).effort, "high")
        response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 2)
        by_id = {item["id"]: item for item in response["result"]["configOptions"]}
        self.assertEqual(by_id[EFFORT_CONFIG_ID]["currentValue"], "high")
        self.assertEqual(by_id[EFFORT_CONFIG_ID]["category"], "thought_level")
        server.handle({"jsonrpc":"2.0","id":3,"method":"session/set_config_option","params":{"sessionId":sid,"configId":"reasoning_effort","value":"low"}})
        self.assertEqual(server._get_session(sid).effort, "low")
        legacy_response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 3)
        legacy_by_id = {item["id"]: item for item in legacy_response["result"]["configOptions"]}
        self.assertEqual(legacy_by_id[EFFORT_CONFIG_ID]["currentValue"], "low")
        server._executor.shutdown(wait=True)

    def test_effort_default_reset_returns_control_to_type_auto(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[],"_meta":{"kiro":{"modelId":"Model"}}}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/set_config_option","params":{"sessionId":sid,"configId":EFFORT_CONFIG_ID,"value":"high"}})
        self.assertTrue(server._get_session(sid).effort_user_set)
        server.handle({"jsonrpc":"2.0","id":3,"method":"session/set_config_option","params":{"sessionId":sid,"configId":EFFORT_CONFIG_ID,"value":"default"}})
        session = server._get_session(sid)
        self.assertFalse(session.effort_user_set)
        self.assertEqual(session.effort, "low")
        server._executor.shutdown(wait=True)

    def test_type_session_rejects_manual_effort_config(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[]}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        session = server._get_session(sid)
        self.assertTrue(session.model.startswith("kiro-byo-type:"))
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/set_config_option","params":{"sessionId":sid,"configId":EFFORT_CONFIG_ID,"value":"high"}})
        response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 2)
        self.assertIn("managed automatically by the active Type", response["error"]["message"])
        self.assertEqual(session.effort, "")
        self.assertFalse(session.effort_user_set)
        server._executor.shutdown(wait=True)

    def test_steer_returns_receipt_and_rejects_invalid_requests(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[]}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        session = server._get_session(sid)
        with session.state_lock:
            session.active_turn = True
        server.handle({"jsonrpc":"2.0","id":2,"method":EXTENSION_STEER,"params":{"sessionId":sid,"message":"please continue","messageId":"m-1"}})
        response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 2)
        self.assertEqual(response["result"], {"queued": True, "messageId": "m-1"})
        self.assertEqual(list(session.steer_queue), ["please continue"])

        server.handle({"jsonrpc":"2.0","id":3,"method":EXTENSION_STEER,"params":{"sessionId":"missing","message":"x"}})
        missing = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 3)
        self.assertEqual(missing["error"]["code"], -32004)
        server.handle({"jsonrpc":"2.0","id":4,"method":EXTENSION_STEER,"params":{"sessionId":sid,"message":"  "}})
        empty = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 4)
        self.assertEqual(empty["error"]["code"], -32602)
        server._executor.shutdown(wait=True)

    def test_advertised_compact_extension_is_a_real_rpc_and_persists_summary(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[]}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        session = server._get_session(sid)
        session.messages = [
            {"role":"user","content":"remember alpha"},
            {"role":"assistant","content":"alpha acknowledged"},
        ]
        with patch("kiro_byo.acp_server.chat_completion_turn", return_value=ChatTurn(text="alpha summary")):
            server.handle({"jsonrpc":"2.0","id":2,"method":EXTENSION_COMPACT,"params":{"sessionId":sid}})
        frames = self._messages(out)
        response = next(frame for frame in reversed(frames) if frame.get("id") == 2)
        self.assertEqual(response["result"], {"success": True})
        kinds = [
            f.get("params",{}).get("update",{}).get("_meta",{}).get("kiro",{}).get("kind")
            for f in frames if f.get("method") == "session/update"
        ]
        self.assertIn("summarization_started", kinds)
        self.assertIn("summarization_completed", kinds)
        self.assertEqual(len(session.messages), 1)
        self.assertIn("alpha summary", session.messages[0]["content"])
        record = server._load_persisted_record(sid)
        self.assertIsNotNone(record)
        self.assertIn("alpha summary", json.dumps(record, ensure_ascii=False))
        server._executor.shutdown(wait=True)

    def test_compact_extension_returns_false_while_session_busy(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[]}})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        session = server._get_session(sid)
        self.assertTrue(session.turn_lock.acquire(blocking=False))
        try:
            server.handle({"jsonrpc":"2.0","id":2,"method":EXTENSION_COMPACT,"params":{"sessionId":sid}})
        finally:
            session.turn_lock.release()
        response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 2)
        self.assertEqual(response["result"], {"success": False})
        server._executor.shutdown(wait=True)

    def test_compact_extension_rejects_unknown_session_instead_of_method_not_found(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":EXTENSION_COMPACT,"params":{"sessionId":"missing"}})
        frame = self._messages(out)[-1]
        self.assertEqual(frame["error"]["code"], -32004)
        server._executor.shutdown(wait=True)

    def test_default_mode_is_advertised_and_idempotent(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":str(Path(self.tmp.name).resolve()),"mcpServers":[]}})
        start = self._messages(out)[-1]["result"]
        sid = start["sessionId"]
        self.assertEqual(start["modes"]["currentModeId"], "default")
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/set_mode","params":{"sessionId":sid,"modeId":"default"}})
        response = next(frame for frame in reversed(self._messages(out)) if frame.get("id") == 2)
        self.assertEqual(response["result"], {})
        server._executor.shutdown(wait=True)

    def test_set_mode_refuses_unknown_mode(self) -> None:
        out = StringIO(); server = AcpServer(stdin=StringIO(), stdout=out)
        server.handle({"jsonrpc":"2.0","id":1,"method":"session/new","params":{
            "cwd": str(Path(self.tmp.name).resolve()),
            "mcpServers": [],
            "_meta":{"kiro":{"customAgents":[{"id":"a","prompt":"A","tools":["read"]}]}}
        }})
        sid = self._messages(out)[-1]["result"]["sessionId"]
        server.handle({"jsonrpc":"2.0","id":2,"method":"session/set_mode","params":{"sessionId":sid,"modeId":"other"}})
        frame = self._messages(out)[-1]
        self.assertEqual(frame["error"]["code"], -32602)
        server._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
