"""Deterministic model decisions; MCP, HTTP and memory authority stay real."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv

TRIGGER = re.compile(r"\[\[WF_E2E:(START|AUTHOR|WORK|NEST|CONTROL):([ABV])(?::(wf_\d+))?\]\]")


def source(marker: str) -> str:
    prompt = f"[[WF_E2E:WORK:{marker}]]"
    return (
        'META = {"name": "private workflow MCP canary"}\n'
        "async def workflow(ctx):\n"
        f"    first = await ctx.parallel([lambda: ctx.agent({prompt!r}), lambda: ctx.agent({prompt!r})])\n"
        f"    warm = await ctx.agent({prompt!r})\n"
        f"    named = await ctx.agent({prompt!r}, session='same-label')\n"
        f"    last = await ctx.agent({prompt!r}, session='same-label')\n"
        "    return [first, warm, named, last]\n"
    )


def respond(text: str, servers: list[dict[str, Any]], cwd: str) -> str | None:
    matches = TRIGGER.findall(text)
    if not matches:
        return None
    operation, marker, target = matches[-1]
    if operation == "AUTHOR":
        return source(marker)
    from kiro_crew.agent_sdk.drivers.acp import projected_session_mcp_servers

    # Kiro reads its spec; adapted harnesses receive these same transports in
    # session/new. Never invent a server command, proof or store selection.
    agent = "kirocrew"
    if "--agent" in sys.argv:
        agent = sys.argv[sys.argv.index("--agent") + 1]
    projected = {item["name"]: item for item in projected_session_mcp_servers(agent, work_dir=cwd)}
    projected.update({item["name"]: item for item in servers})
    core = projected.get("kirocrew-core")
    if core is None or core.get("type", "stdio") != "stdio":
        raise RuntimeError("The workflow canary requires the real projected core MCP transport")
    env = dict(os.environ)
    env.update({item["name"]: item["value"] for item in core.get("env", [])})
    responses: queue.Queue[dict | None] = queue.Queue()
    with ExitStack() as cleanup, tempfile.TemporaryFile(mode="w+b") as errors:
        # Dynamic spec input is never a benign fixed command. The common seam
        # retains the outer private namespace when already confined; no proof
        # or private identity is manufactured here.
        argv, child_env, profile = sandboxed_spawn_argv(
            [core["command"], *core.get("args", [])], env=env
        )
        if profile is not None:
            cleanup.callback(Path(profile).unlink, missing_ok=True)
        process = popen_limited(
            argv,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
        )
        assert process.stdout is not None and process.stdin is not None

        def read() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            finally:
                responses.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def request(index: int, method: str, params: dict) -> dict:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params})
                + "\n"
            )
            process.stdin.flush()
            while True:
                response = responses.get(timeout=30)
                if response is None:
                    raise RuntimeError("Projected MCP server exited before answering")
                if response.get("id") == index:
                    if "error" in response:
                        raise RuntimeError(f"Projected MCP request failed: {response['error']}")
                    return response["result"]

        try:
            request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "workflow-canary", "version": "1"},
                },
            )
            process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.flush()
            if operation == "START":
                return json.dumps(
                    request(
                        2,
                        "tools/call",
                        {"name": "workflow_run", "arguments": {"source": source(marker)}},
                    )
                )
            if operation == "CONTROL":
                if not target:
                    raise ValueError("CONTROL requires a run id")
                results = {}
                for index, tool in enumerate(
                    (
                        "workflow_result",
                        "workflow_list",
                        "workflow_cancel",
                        "workflow_rerun_subtree",
                    ),
                    start=2,
                ):
                    arguments = {} if tool == "workflow_list" else {"run_id": target}
                    results[tool] = request(
                        index, "tools/call", {"name": tool, "arguments": arguments}
                    )
                return json.dumps(results)
            if operation == "NEST":
                nested = request(
                    2,
                    "tools/call",
                    {"name": "workflow_run", "arguments": {"source": source(marker)}},
                )
                spawned = request(
                    3,
                    "tools/call",
                    {
                        "name": "spawn_run",
                        "arguments": {
                            "task": f"[[WF_E2E:WORK:{marker}]]",
                            "keep": True,
                        },
                    },
                )
                return json.dumps({"nested_workflow": nested, "nested_spawn": spawned})
            written = request(
                2,
                "tools/call",
                {
                    "name": "learn_add",
                    "arguments": {"rule": f"WF_E2E_{marker}_PRIVATE_LESSON", "category": "tool"},
                },
            )
            recalled = request(
                3,
                "tools/call",
                {"name": "memory_recall", "arguments": {"query": "WF_E2E private lesson"}},
            )
            return json.dumps({"write": written, "recall": recalled})
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()


def progress_summary(run: dict[str, Any]) -> dict[str, Any]:
    """Payload-free E2E failure state: never log source, results or error text."""
    from kiro_crew.workflows import EVENT_TYPES

    counts = {kind: 0 for kind in EVENT_TYPES}
    pending: set[int] = set()
    for event in run.get("events", []):
        kind = event.get("type")
        if kind not in counts:
            continue
        counts[kind] += 1
        data = event.get("data", {})
        if kind == "agent_started" and type(data.get("call_index")) is int:
            pending.add(data["call_index"])
        elif kind == "agent_finished":
            agent_id = data.get("agent_id", "")
            if isinstance(agent_id, str) and re.fullmatch(r"a[0-9]{1,6}", agent_id):
                pending.discard(int(agent_id[1:]))
    status = run.get("status")
    return {
        "status": status if status in {"running", "finished", "failed", "cancelled"} else "unknown",
        "events": {kind: count for kind, count in counts.items() if count},
        "pending_calls": sorted(pending),
    }


def spawn_progress_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Keep only fixed state labels; never echo tasks, results, errors or proofs."""
    done = state.get("done") is True
    error = state.get("error")
    reason = "none"
    if done and error:
        reason = "failed"
        if isinstance(error, str):
            if error.startswith("spawn rejected: no surface could show the approval prompt"):
                reason = "no_approval_surface"
            elif error.startswith("spawn rejected"):
                reason = "spawn_rejected"
            elif error.startswith("memory_unavailable:"):
                reason = "memory_unavailable"
            elif error.startswith("resume_failed:"):
                reason = "resume_failed"
            elif error == "cancelled":
                reason = "cancelled"
    return {
        "status": "done" if done else "running",
        "awaiting_approval": state.get("awaiting_approval") is True,
        "terminal_reason": reason,
    }


def wait_for_spawn_result(client: Any, home: Path, spawn_id: str) -> Any:
    """Read the full, unchanged stream as JSON; fail promptly on a terminal error."""
    import time

    deadline = time.monotonic() + 120
    missing = invalid_json = 0
    summary: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            summary = spawn_progress_summary(client.get(f"/api/spawn/{spawn_id}?limit=1"))
        except Exception:
            summary = {"status": "unknown", "terminal_reason": "status_unavailable"}
            break
        if summary["terminal_reason"] != "none":
            break
        try:
            result = json.loads(
                (home / "subagents" / spawn_id / "result.txt").read_text(encoding="utf-8")
            )
            if summary["status"] == "done":
                return result
        except FileNotFoundError:
            missing += 1
        except json.JSONDecodeError:
            invalid_json += 1
        except (OSError, UnicodeError):
            summary["terminal_reason"] = "result_unreadable"
            break
        if summary["status"] == "done":
            break
        time.sleep(0.1)
    summary.update(missing_file_polls=missing, non_json_polls=invalid_json)
    raise AssertionError("Nested MCP spawn result unavailable: " + json.dumps(summary))
