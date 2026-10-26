from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from kiro_byo.acp_worker import AcpAgentDefinition, run_acp_agent_task
from kiro_byo.agent_runtime import AgentSpec, AgentTask


_FAKE = r'''
import json, sys
for line in sys.stdin:
    msg=json.loads(line)
    method=msg.get("method")
    rid=msg.get("id")
    if method=="initialize":
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":1,"agentCapabilities":{"sessionCapabilities":{}},"authMethods":[],"agentInfo":{"name":"fake","version":"1"}}}), flush=True)
    elif method=="session/new":
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"sessionId":"fake-session"}}), flush=True)
    elif method=="session/prompt":
        sid=msg["params"]["sessionId"]
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"external finding"}}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"stopReason":"end_turn"}}), flush=True)
    elif method=="session/cancel":
        pass
'''


class AcpWorkerTests(unittest.TestCase):
    def test_external_acp_agent_projects_to_agent_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "fake_acp.py"
            script.write_text(_FAKE, encoding="utf-8")
            definition = AcpAgentDefinition(
                agent_id="fake", command=(sys.executable, str(script)), timeout_seconds=15.0,
            )
            task = AgentTask(
                task_id="t", plan_id="p", session_id="s",
                spec=AgentSpec(role="worker", name="external", objective="inspect", backend="acp", backend_id="fake"),
                context_messages=[{"role":"user","content":"context"}],
            )
            result = run_acp_agent_task(task, definition, cwd=str(root))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.provider_id, "acp")
            self.assertEqual(result.model_id, "fake")
            self.assertIn("external finding", result.summary)


if __name__ == "__main__":
    unittest.main()
