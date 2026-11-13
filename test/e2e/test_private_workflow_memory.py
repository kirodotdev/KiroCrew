"""Real gateway, scheduled workflows, ordinary MCP authentication and SQLite routing.

Only the model decisions are deterministic. Canonical owner records, transport,
SessionManager, store APIs and workflow execution use their production paths.
"""

import asyncio
import contextlib
import json
import os
import re
import sqlite3
import time
import urllib.request

import pytest
from e2e.test_gateway_boot_matrix import _await_assistant_reply, _booted

from kiro_crew.testing.wait import finished, wait_until
from kiro_crew.testing.workflow_memory_scenario import (
    progress_summary,
    source,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"), reason="Set KIROCREW_E2E=1 for real private workflow E2E"
)

# One wait in this module may take _PER_WAIT_SECONDS, and a test makes nine of
# them, so per-wait budgets alone allow far more time than CI grants the whole
# test (`--timeout=600` on the e2e-private-namespace job). _TEST_BUDGET_SECONDS
# is the ceiling on all of a test's waiting together, anchored at test start and
# set below that cap, so the last wait a stalled run can reach still fails HERE
# -- naming the run and its progress -- instead of dying at the outer cap, which
# reports only `Timeout >600.0s` and never says which workflow hung.
_PER_WAIT_SECONDS = 120
_TEST_BUDGET_SECONDS = 570
_test_deadline = None


@pytest.fixture(autouse=True)
def _wait_budget():
    """Anchor this test's whole waiting budget at its start."""
    global _test_deadline
    _test_deadline = time.monotonic() + _TEST_BUDGET_SECONDS
    try:
        yield
    finally:
        _test_deadline = None


def _wait_deadline():
    """The earlier of this wait's own cap and what is left of the test's budget."""
    own = time.monotonic() + _PER_WAIT_SECONDS
    return own if _test_deadline is None else min(own, _test_deadline)


def _post_as(client, path, body, session):
    request = urllib.request.Request(
        f"http://localhost:{client._port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Session-Key": session},
    )
    return client._open(request, timeout=60)


def _finished(client, run_id):
    run = None

    def _terminated() -> bool:
        nonlocal run
        run = client.get(f"/api/workflows/runs/{run_id}")
        return run["status"] != "running"

    wait_until(
        _terminated,
        timeout=max(0.0, _wait_deadline() - time.monotonic()),
        interval=0.1,
        describe=lambda: (
            f"Workflow did not terminate: {run_id}; "
            f"{json.dumps(progress_summary(run)) if run is not None else 'never polled'}"
        ),
    )
    assert run["status"] == "finished", run
    assert not run.get("agent_errors"), run
    return run


def _assert_result(run, marker):
    result = json.dumps(run["result"])
    assert f"WF_E2E_{marker}_PRIVATE_LESSON" in result, run
    assert '"isError": true' not in result, run
    for other in {"A", "B", "V"} - {marker}:
        assert f"WF_E2E_{other}_PRIVATE_LESSON" not in result, run


async def _nested_with_owner_approval(client, session, nested_source):
    """Attach the authenticated owner before dispatch; approve exactly this spawn."""
    import aiohttp

    request = urllib.request.Request(f"http://localhost:{client._port}/api/ws")
    for handler in client._opener.handlers:
        if isinstance(handler, urllib.request.HTTPCookieProcessor):
            handler.cookiejar.add_cookie_header(request)
    approvals = asyncio.Queue()
    async with aiohttp.ClientSession(headers=dict(request.header_items())) as owner:
        async with owner.ws_connect(request.full_url) as ws:
            # HTTP upgrade precedes registration. The initial slots frame proves
            # the owner is registered and can actually receive approval prompts.
            frame = await ws.receive_json(timeout=10)
            assert frame.get("type") == "slots", "Owner WebSocket did not register"

            async def receive():
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        if event.get("type") == "approval":
                            await approvals.put(event["data"])

            reader = asyncio.create_task(receive())
            try:
                start = await asyncio.to_thread(
                    _post_as, client, "/api/workflows/run", {"source": nested_source}, session
                )
                run = await asyncio.to_thread(_finished, client, start["run_id"])
                nested = json.loads(run["result"])
                text = "\n".join(
                    item.get("text", "") for item in nested["nested_spawn"].get("content", [])
                )
                match = re.search(
                    r"^\s+([A-Za-z0-9_-]+)(?: \([^)]*\))?: \[\[WF_E2E", text, re.MULTILINE
                )
                assert match, "Nested spawn response did not contain a run id"
                spawn_id = match.group(1)

                async def approve_once():
                    while True:
                        approval = await approvals.get()
                        if approval.get("id") != f"spawn:{spawn_id}":
                            continue
                        assert approval.get("source") == "subagent"
                        assert approval.get("tool") == "spawn_run([[WF_E2E:WORK:A]])"
                        resolved = await asyncio.to_thread(
                            client.post, f"/api/approvals/spawn:{spawn_id}/approve", {}
                        )
                        assert resolved.get("ok") is True, "Spawn approval was not accepted"
                        return

                await finished(approve_once(), "the nested spawn's approval request", timeout=10)
                return nested, spawn_id
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader


def _next_reply(client, slot, message):
    """Send *message* on *slot* and return the assistant reply that follows it."""

    def replies():
        messages = client.get(f"/api/chat/slots/{slot}").get("messages", [])
        return [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "assistant" and str(m.get("content", "")).strip()
        ]

    seen = len(replies())
    client.post("/api/chat?ws=1", {"slot": slot, "message": message})
    current = wait_until(
        lambda: (lambda now: now if len(now) > seen else None)(replies()),
        timeout=max(0.0, _wait_deadline() - time.monotonic()),
        describe=lambda: f"no reply on {slot} to {message}; last messages: "
        + repr(
            [
                (m.get("role"), str(m.get("content", ""))[:300])
                for m in client.get(f"/api/chat/slots/{slot}").get("messages", [])[-6:]
            ]
        )
        + f"\n{client.diagnostics()}",
    )
    return current[seen]


def _assert_private_worker_child(handle, client):
    """Crewmate A's conductor opens a session; the child reads and writes A's store."""
    from kiro_crew.execution_context import execution_from_record
    from kiro_crew.history_files import transcript_stem

    # The hand-off runs from A's own DM thread: only a member thread mounts the
    # dashboard tools (``member_dispatch``); a plain chat bound to A does not.
    slot = client.post("/api/members/workflow-e2e-a/thread", {})["slot_key"]
    opened = _next_reply(client, slot, "[[WF_E2E:HANDOFF:A]]")
    match = re.search(r"Opened `([^`]+)`", opened)
    assert match, opened
    child = match.group(1)
    client.post("/api/chat?ws=1", {"slot": child, "message": "[[WF_E2E:WORK:A]]"})
    work = json.loads(str(_await_assistant_reply(client, child)["content"]))
    recall = json.dumps(work["recall"])
    assert "WF_E2E_A_PRIVATE_LESSON" in recall, work
    assert '"isError": true' not in json.dumps(work), work
    for other in ("B", "V"):
        assert f"WF_E2E_{other}_PRIVATE_LESSON" not in recall, work
    session_file = handle.home / "sessions" / f"{transcript_stem(f'dashboard:{child}')}.jsonl"
    with session_file.open(encoding="utf-8") as lines:
        execution = execution_from_record(json.loads(lines.readline()))
    config = json.loads((handle.home / "config.json").read_text(encoding="utf-8"))
    binding = config["agents"]["workflow-e2e-a"]
    assert execution.template_id == "kirocrew-mate-worker"
    assert execution.store.store_id == binding["memory_store"]
    assert execution.member_id == binding["member_id"]


def test_private_workflows_execute_over_real_projected_mcp():
    with _booted("rich") as (handle, client):
        members = {}
        for marker in ("A", "B"):
            member = f"workflow-e2e-{marker.lower()}"
            client.post("/api/agents", {"name": member, "kiro_agent": "kirocrew"})
            members[marker] = client.post("/api/chat/slots", {"agent": member})["key"]
        members["V"] = client.post("/api/chat/slots", {"agent": "default"})["key"]
        starts = {}
        # All three runs are admitted before waiting, so their workers overlap.
        for marker, slot in members.items():
            client.post("/api/chat?ws=1", {"slot": slot, "message": f"[[WF_E2E:START:{marker}]]"})
        for marker, slot in members.items():
            response = _await_assistant_reply(client, slot)
            match = re.search(r"wf_\d+", str(response["content"]))
            assert match, response
            starts[marker] = match.group()
        for marker, run_id in starts.items():
            _assert_result(_finished(client, run_id), marker)
        session = f"dashboard:{members['A']}"
        authored = _post_as(
            client, "/api/workflows/author", {"intent": "[[WF_E2E:AUTHOR:A]]"}, session
        )
        assert authored["ok"], authored
        assert authored["source"] == source("A")
        intent = _post_as(
            client, "/api/workflows/run_intent", {"intent": "[[WF_E2E:AUTHOR:A]]"}, session
        )
        _assert_result(_finished(client, intent["run_id"]), "A")
        definition = client.post("/api/workflows/definitions", {"source": source("A")})[
            "definition"
        ]
        saved = _post_as(client, f"/api/workflows/definitions/{definition['id']}/run", {}, session)
        _assert_result(_finished(client, saved["run_id"]), "A")
        rerun = _post_as(
            client, f"/api/workflows/runs/{starts['A']}/rerun", {"from_index": 2}, session
        )
        _assert_result(_finished(client, rerun["run_id"]), "A")
        # These calls originate in workflow workers, not the owner HTTP client.
        nested_source = 'META = {"name": "nested"}\nasync def workflow(ctx):\n    return await ctx.agent("[[WF_E2E:NEST:A]]")\n'
        # A crewmate's chat runs on its conductor, which starts no sub-agents, and
        # a workflow it starts runs its steps on that same template: the nested
        # workflow runs on the crewmate's store, the nested spawn is refused.
        start = _post_as(client, "/api/workflows/run", {"source": nested_source}, session)
        nested = json.loads(_finished(client, start["run_id"])["result"])
        nested_id = re.search(r"wf_\d+", json.dumps(nested["nested_workflow"])).group()
        _assert_result(_finished(client, nested_id), "A")
        refused = json.dumps(nested["nested_spawn"])
        assert "starts no sub-agents" in refused, nested["nested_spawn"]
        # The conductor hands work off with session_create instead: the child is
        # the crewmate's worker, on its private store and member identity.
        _assert_private_worker_child(handle, client)
        for marker in ("B", "V"):
            control_source = f'META = {{"name": "foreign control"}}\nasync def workflow(ctx):\n    return await ctx.agent("[[WF_E2E:CONTROL:{marker}:{starts["A"]}]]")\n'
            control = _post_as(
                client,
                "/api/workflows/run",
                {"source": control_source},
                f"dashboard:{members[marker]}",
            )
            results = json.loads(_finished(client, control["run_id"])["result"])
            for tool, result in results.items():
                text = json.dumps(result)
                if tool == "workflow_list":
                    assert starts["A"] in text
                elif tool == "workflow_result":
                    assert "WF_E2E_A_PRIVATE_LESSON" in text
                elif tool == "workflow_rerun_subtree":
                    assert not result.get("isError"), (tool, result)
                    replay_id = re.search(r"wf_\d+", text).group()
                    _assert_result(_finished(client, replay_id), "A")
                else:
                    assert "refused" not in text.lower(), (tool, result)
        config = json.loads((handle.home / "config.json").read_text(encoding="utf-8"))
        for marker in ("A", "B", "V"):
            if marker == "V":
                path = handle.home / "memory.db"
            else:
                store = config["agents"][f"workflow-e2e-{marker.lower()}"]["memory_store"]
                path = handle.home / "memory_stores" / store / "memory.db"
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                content = "\n".join(db.iterdump())
            assert f"WF_E2E_{marker}_PRIVATE_LESSON" in content
            for other in {"A", "B", "V"} - {marker}:
                assert f"WF_E2E_{other}_PRIVATE_LESSON" not in content
        from e2e.test_gateway_boot_matrix import _await_memory_recovery, _Client

        old_pid, old_home = handle.proc.pid, handle.home
        handle = handle.restart()
        assert handle.proc.pid != old_pid
        assert handle.home == old_home
        client = _Client(handle.port, handle.token)
        client.diagnostics = handle.diagnostics
        # A restart re-enters the READY-before-recovery window, and a run read
        # validates its memory store, so it is refused until recovery ends.
        _await_memory_recovery(client)
        restored = client.get(f"/api/workflows/runs/{starts['A']}")
        _assert_result(restored, "A")
        replay = _post_as(
            client, f"/api/workflows/runs/{starts['A']}/rerun", {"from_index": 2}, session
        )
        assert replay["run_id"] != starts["A"]
        assert replay["replayed_before"] == 2
        _assert_result(_finished(client, replay["run_id"]), "A")
