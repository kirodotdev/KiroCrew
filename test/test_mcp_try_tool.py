"""``POST /api/mcp/try-tool``, the MCP Servers page's Try a tool panel.

Off (the default) the path is not registered, so it answers as any unknown route.
On it is owner-only and runs the server through the discovery probe, with one
``tools/call`` after the listing: a tool not declared read-only needs a confirm,
every run and refusal is in the security event log, a run is stopped at the time
limit or on a cancel with its whole process tree, and the result is redacted and
then cut to size. Most tests drive the route against an in-memory stand-in for the
probe (:class:`_FakeServer`); the few that start a real, tiny MCP server written under
``tmp_path`` are the ones whose point is the spawn, the handshake bytes, the teardown, a
cancel or the stderr pipe, and they say so. The code under test spawns its own server, so
those cases cannot share one interpreter; the module is one ``xdist_group`` instead, so
their children never cold-start on several workers at once.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest_floor_determinism import _pool_activity
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew import platform_compat
from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group(name="mcp_try_tool")]

_PATH = "/api/mcp/try-tool"

#: A stdio MCP server driven by a JSON file. It records its pid and every message it
#: reads; ``block`` leaves ``tools/call`` unanswered until stdin closes, ``list_error``
#: answers ``tools/list`` with that RPC error, ``grandchild`` starts a sleeper first,
#: ``stderr_text``, then ``stderr_bytes`` of filler, then ``stderr_last`` go to stderr
#: before it answers ``tools/call``, and ``die`` exits right after writing them.
#: ``silent`` answers nothing, and ``close_stdout`` closes its stdout on ``tools/call``
#: and then keeps running whatever happens to its stdin.
_SERVER_SOURCE = """\
import json, os, subprocess, sys, time
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
record = sys.argv[2]

def note(entry):
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")

pgid = os.getpgid(0) if hasattr(os, "getpgid") else None
note({"pid": os.getpid(), "pgid": pgid})
if cfg.get("grandchild"):
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    note({"grandchild": sleeper.pid})
for line in sys.stdin:
    msg = json.loads(line)
    note({"method": msg.get("method"), "params": msg.get("params")})
    if "id" not in msg or cfg.get("silent"):
        continue
    method, reply = msg.get("method"), {"jsonrpc": "2.0", "id": msg["id"]}
    if method == "tools/call" and cfg.get("close_stdout"):
        os.close(1)
        time.sleep(300)
    if method == "tools/call" and (cfg.get("stderr_bytes") or cfg.get("stderr_text")):
        flood = "e" * cfg.get("stderr_bytes", 0)
        sys.stderr.write(cfg.get("stderr_text", "") + flood + cfg.get("stderr_last", ""))
        sys.stderr.flush()
        if cfg.get("die"):
            sys.exit(3)
    if method == "initialize":
        reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {}}
    elif method == "tools/list" and "list_error" in cfg:
        reply["error"] = cfg["list_error"]
    elif method == "tools/list":
        reply["result"] = {"tools": cfg["tools"]}
    elif method == "tools/call" and cfg.get("block"):
        continue
    elif method == "tools/call":
        reply["result"] = cfg["answer"]
    else:
        reply["error"] = {"code": -32601, "message": "no such method"}
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
"""


class _FakeState:
    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for token_auth_middleware, as in the owner-gate invariant test.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class _RealServer:
    """The tiny server's script, its config and what it recorded; a real child process."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.script = root / "server.py"
        self.script.write_text(_SERVER_SOURCE, encoding="utf-8")
        self.config = root / "config.json"
        self.record = root / "record.jsonl"
        self.set(tools=[], answer=_text("done"))

    def set(self, **cfg: Any) -> None:
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    @property
    def argv(self) -> list[str]:
        return [str(self.script), str(self.config), str(self.record)]

    def entries(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        lines = self.record.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def methods(self) -> list[str]:
        return [e["method"] for e in self.entries() if "method" in e]

    def pids(self) -> list[int]:
        found = [e.get("pid") for e in self.entries()] + [
            e.get("grandchild") for e in self.entries()
        ]
        return [pid for pid in found if isinstance(pid, int)]


def _gone(pid: int) -> bool:
    # A killed orphan stays a zombie where PID 1 does not reap; it has exited.
    if platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD:
        return True
    return platform_compat.pid_is_zombie(pid) is True


class _Sel:
    """Records what the route writes to the security event log."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_critical = False

    def log_tool_invocation(self, **kw: Any) -> None:
        if kw.get("critical") and self.fail_critical:
            raise OSError("security log unwritable")
        self.events.append(kw)

    def outcomes(self) -> list[tuple[str, str]]:
        return [(e["outcome"], e["error"]) for e in self.events]


class _FakeServer:
    """An in-memory server behind the probe seam: what it was asked and what it answers.

    ``probe`` replaces :func:`kiro_crew.mcp_discovery.probe_server` for the route: the
    handshake is recorded, the listing comes from ``tools``, the step's ``choose`` runs
    as it does on the probe's loop, and the answer lands in the step as the real call's
    does. ``set`` takes the keys the real script's config takes (``tools``, ``answer``,
    ``list_error``, ``block``) and ``teardown_hangs``, which answers and then never
    ends, as a server that ignores its closed stdin does while the probe waits for it.
    """

    def __init__(self) -> None:
        self.cfg: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.cancelled = False
        self.sel: _Sel
        self.set(tools=[], answer=_text("done"))

    def set(self, **cfg: Any) -> None:
        self.cfg = cfg

    def entries(self) -> list[dict[str, Any]]:
        return list(self.log)

    def methods(self) -> list[str]:
        return [e["method"] for e in self.log if "method" in e]

    async def probe(
        self, server: McpServerInfo, *, tool_call: Any = None, **_ignored: Any
    ) -> McpServerInfo:
        for method in ("initialize", "notifications/initialized", "tools/list"):
            self.log.append({"method": method, "params": None})
        if "list_error" in self.cfg:
            server.status = "error"
            server.error = "tools/list: " + self.cfg["list_error"]["message"]
            return server
        tools = self.cfg["tools"]
        params = tool_call.choose(tools) if tool_call is not None else None
        if params is not None:
            self.log.append({"method": "tools/call", "params": params})
            if not self.cfg.get("block"):
                tool_call.answer = {"jsonrpc": "2.0", "id": 4, "result": self.cfg["answer"]}
            if self.cfg.get("block") or self.cfg.get("teardown_hangs"):
                try:
                    await asyncio.Event().wait()  # until the run's limit cancels this task
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
        server.tools = [t["name"] for t in tools if isinstance(t, dict) and t.get("name")]
        server.status = "ok"
        return server


def _route_patches(_floor_monkeypatch: pytest.MonkeyPatch, row: Any) -> _Sel:
    """The route switched on for one local server ``s``, nothing muted or switched off."""
    from kiro_crew import cron_script, mcp_cleanup, mcp_discovery, sel

    _floor_monkeypatch.setattr(agent_config_routes, "tool_tester_enabled", lambda: True)
    _floor_monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [row()])
    _floor_monkeypatch.setattr(mcp_cleanup, "mcp_server_muted_in_any_store", lambda *a, **k: False)
    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda server: frozenset())
    log = _Sel()
    _floor_monkeypatch.setattr(sel, "sel", lambda: log)
    return log


@pytest.fixture
def server(_floor_monkeypatch):
    """The route against an in-memory server named ``s``: no child process is started."""
    from kiro_crew import mcp_discovery

    fake = _FakeServer()
    fake.sel = _route_patches(
        _floor_monkeypatch, lambda: McpServerInfo(name="s", command=sys.executable, args=["s.py"])
    )
    _floor_monkeypatch.setattr(mcp_discovery, "probe_server", fake.probe)
    return fake


@pytest.fixture
def real_server(_floor_monkeypatch, tmp_path):
    """The route against a real, tiny server named ``s``, killed on the way out."""
    from kiro_crew import cron_script

    fake = _RealServer(tmp_path / "srv")
    fake.sel = _route_patches(
        _floor_monkeypatch,
        lambda: McpServerInfo(name="s", command=sys.executable, args=fake.argv),
    )

    # The same server for a client that reads the cron agent spec, so both launchers see it.
    @functools.lru_cache(maxsize=16)
    def resolve(name: str):
        return ((sys.executable, *fake.argv), {}) if name == "s" else None

    _floor_monkeypatch.setattr(cron_script, "_resolve_mcp_server", resolve)
    _floor_monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, mode: (argv, None))
    _floor_monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: argv)
    try:
        yield fake
    finally:
        for pid in fake.pids():
            if platform_compat.IS_POSIX and not _gone(pid):
                platform_compat.kill_pid(pid, platform_compat.SIGKILL)


async def _within(awaitable: Any, what: str) -> Any:
    """Await *awaitable* under the lost-run bound, so a hang fails naming *what* (rule D8)."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), FALLBACK_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        if task.done():
            raise  # the awaited call's own timeout, not the bound
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail(f"{what} did not finish within {FALLBACK_TIMEOUT_SECS} s")


async def _post(body: object, **headers: str) -> tuple[int, Any]:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await _within(client.post(_PATH, json=body, headers=headers), "the request")
        text = await resp.text()
        return resp.status, (json.loads(text) if text.startswith("{") else text)


async def _all_gone(fake: _RealServer) -> None:
    await async_wait_until(
        lambda: fake.pids() and all(_gone(pid) for pid in fake.pids()),
        describe=lambda: f"still running: {[p for p in fake.pids() if not _gone(p)]}",
        off_loop=True,
    )


async def _probe_pool_idle() -> None:
    await async_wait_until(
        lambda: "_mcp_probe_pool" not in _pool_activity(),
        describe=lambda: f"probe pool still busy: {_pool_activity()}",
    )


async def test_off_the_path_answers_as_an_unknown_route() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        unknown = await client.post("/api/mcp/no-such-route", json={"server": "x"})
        tester = await client.post(_PATH, json={"server": "x"})
        assert (tester.status, await tester.text()) == (unknown.status, await unknown.text())
        assert tester.status == 404


async def test_a_probe_sends_a_tool_call_only_when_it_is_given_a_step(tmp_path) -> None:
    """Real child: the handshake bytes stay as main's, and a step adds one ``tools/call``."""
    from kiro_crew.mcp_discovery import ToolCallStep

    fake = _RealServer(tmp_path / "step")
    fake.set(tools=[{"name": "t"}], answer=_text("done"))
    handshake = ["initialize", "notifications/initialized", "tools/list"]
    server = McpServerInfo(name="p", command=sys.executable, args=fake.argv)
    plain = await _within(probe_server(server), "the probe without a step")
    assert plain.status == "ok"
    assert plain.tools == ["t"]
    assert fake.methods() == handshake
    seen: list[list[Any]] = []

    def choose(tools: list[Any]) -> dict[str, Any]:
        seen.append(tools)
        return {"name": "t", "arguments": {"a": 1}}

    step = ToolCallStep(choose)
    probed = await _within(
        probe_server(
            McpServerInfo(name="p", command=sys.executable, args=fake.argv),
            client_info={"name": "x", "version": "1"},
            tool_call=step,
        ),
        "the probe with a step",
    )
    assert probed.status == "ok"
    assert seen == [[{"name": "t"}]]
    assert step.answer is not None and step.answer["result"] == _text("done")
    assert fake.methods() == handshake * 2 + ["tools/call"]
    assert fake.entries()[-1]["params"] == {"name": "t", "arguments": {"a": 1}}
    await _all_gone(fake)


async def test_on_a_non_owner_is_refused_before_anything_starts(server) -> None:
    status, body = await _post({"server": "s", "tool": "t"}, **{"X-Test-User": "someone-else"})
    assert status in (401, 403)
    assert body["code"] == "owner_only"
    assert server.entries() == []


async def test_without_a_tool_it_lists_tools_with_their_confirm_need(server) -> None:
    server.set(
        tools=[
            {
                "name": "read",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {"type": "object"},
            },
            {"name": "write", "annotations": {"readOnlyHint": False, "destructiveHint": False}},
            {"name": "bare", "description": "no annotations"},
        ]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert [(r["name"], r["needsConfirm"]) for r in body["tools"]] == [
        ("read", False),
        ("write", True),
        ("bare", True),
    ]
    assert body["tools"][0]["inputSchema"] == {"type": "object"}
    assert "tools/call" not in server.methods()
    assert server.sel.events == []


@pytest.mark.parametrize(
    "annotations",
    [None, {}, {"readOnlyHint": False}, {"destructiveHint": True}, {"readOnlyHint": "true"}],
)
async def test_a_tool_not_declared_read_only_is_refused_without_the_confirm(
    server, annotations
) -> None:
    tool: dict[str, Any] = {"name": "t"}
    if annotations is not None:
        tool["annotations"] = annotations
    server.set(tools=[tool], answer=_text("done"))
    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}})
    assert status == 409
    assert body["code"] == "confirm_required"
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "confirm_required")]

    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}, "confirm": True})
    assert status == 200
    assert server.entries()[-1]["params"] == {"name": "t", "arguments": {"a": 1}}
    assert server.sel.outcomes()[1:] == [("invoked", ""), ("completed", "")]


async def test_a_tool_is_confirmed_and_run_on_a_real_server(real_server) -> None:
    """Real child, through the route: the spawn, the refusal after the listing, the run."""
    real_server.set(tools=[{"name": "t"}], answer=_text("done"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (409, "confirm_required")
    assert "tools/call" not in real_server.methods()
    await _all_gone(real_server)
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert (status, body["isError"]) == (200, False)
    assert json.loads(body["result"]) == _text("done")
    assert real_server.methods().count("tools/call") == 1
    assert real_server.sel.outcomes() == [
        ("denied", "confirm_required"),
        ("invoked", ""),
        ("completed", ""),
    ]
    await _all_gone(real_server)


async def test_every_call_is_in_the_security_log_written_ahead(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    status, _ = await _post({"server": "s", "tool": "t", "confirm": True})
    assert status == 200
    invoked, completed = server.sel.events
    for event in (invoked, completed):
        assert event["session_key"] == "dashboard"
        assert event["source"] == "dashboard"
        assert event["tool_name"] == "s/t"
        assert event["tool_kind"] == "dashboard_mcp_tool"
        assert event["metadata"] == {"confirm": True}
        assert event["critical"] is True
    assert (invoked["outcome"], completed["outcome"]) == ("invoked", "completed")


async def test_a_call_that_cannot_be_recorded_never_runs(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    server.sel.fail_critical = True
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (503, "audit_unavailable")
    assert "tools/call" not in server.methods()


async def test_a_tool_error_is_recorded_as_an_error(server) -> None:
    answer = {"content": [{"type": "text", "text": "no"}], "isError": True}
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=answer)
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["isError"]) == (200, True)
    assert [o for o, _ in server.sel.outcomes()] == ["invoked", "error"]


async def test_a_read_only_tool_runs_without_a_confirm(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("done"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert body["isError"] is False
    assert json.loads(body["result"]) == _text("done")
    assert server.sel.events[0]["metadata"] == {"confirm": False}


async def test_a_switched_off_tool_is_refused(server, _floor_monkeypatch) -> None:
    from kiro_crew import cron_script

    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda s: frozenset({"t"}))
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}])
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert status == 403
    assert body["code"] == "tool_switched_off"
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "tool_switched_off")]


@pytest.mark.parametrize("muted_in", ["row", "scope"])
async def test_a_switched_off_server_is_refused_before_it_starts(
    server, _floor_monkeypatch, muted_in
) -> None:
    from kiro_crew import mcp_cleanup, mcp_discovery

    row = McpServerInfo(name="s", command=sys.executable, args=["s.py"])
    if muted_in == "row":
        row.disabled = True
    else:
        _floor_monkeypatch.setattr(
            mcp_cleanup, "mcp_server_muted_in_any_store", lambda *a, **k: True
        )
    _floor_monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [row])
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert (status, body["code"]) == (409, "server_disabled")
    assert server.entries() == []


async def test_a_remote_or_unknown_server_is_refused(server, _floor_monkeypatch) -> None:
    from kiro_crew import mcp_discovery

    remote = McpServerInfo(name="r", url="https://mcp.example.test/mcp")
    _floor_monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [remote])
    status, body = await _post({"server": "r"})
    assert (status, body["code"]) == (409, "not_local")
    status, body = await _post({"server": "nope"})
    assert (status, body["code"]) == (404, "unknown_server")


async def test_a_listing_error_is_a_coded_redacted_failure(server) -> None:
    token = github_classic(seed=5)
    server.set(list_error={"code": -32000, "message": f"bad key {token}"})
    status, body = await _post({"server": "s"})
    assert (status, body["code"]) == (502, "server_failed")
    assert body["error"].startswith("tools/list: bad key")
    assert token not in body["error"]
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert (status, body["code"]) == (502, "server_failed")


async def test_a_run_past_the_time_limit_stops_the_server(real_server, _floor_monkeypatch) -> None:
    """Real child: the probe stops at the run's limit, and its teardown reaps the server."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    # The call is never answered, so any limit expires.
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 0.01)  # flake-ok: never answered
    real_server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], block=True)
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 504
    assert body["code"] == "timeout"
    # The cancel reached the probe's private task; its teardown ends the run off the loop.
    await _probe_pool_idle()
    if real_server.pids():
        await _all_gone(real_server)


async def test_a_run_past_the_time_limit_is_a_timeout_in_the_log(
    server, _floor_monkeypatch
) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 0.01)  # flake-ok: never answered
    # The in-memory probe has no limit of its own, so the backstop stops it.
    _floor_monkeypatch.setattr(module, "_TEARDOWN_MARGIN_SECS", 0.0)
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], block=True)
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (504, "timeout")
    assert server.cancelled is True
    assert server.sel.outcomes() == [("invoked", ""), ("timeout", "no answer in time")]


async def test_an_answer_that_came_before_the_limit_is_kept_when_the_teardown_overruns(
    server, _floor_monkeypatch
) -> None:
    """The server answers, then ignores its closed stdin until the limit cancels the probe."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    # The answer is set before the probe first waits, so any limit expires after it.
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 0.01)  # flake-ok: ends only by cancel
    _floor_monkeypatch.setattr(module, "_TEARDOWN_MARGIN_SECS", 0.0)
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}],
        answer=_text("done"),
        teardown_hangs=True,
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200, body
    assert (body["isError"], json.loads(body["result"])) == (False, _text("done"))
    assert server.cancelled is True
    assert server.sel.outcomes() == [("invoked", ""), ("completed", "")]


async def test_a_timed_out_run_is_answered_only_once_its_teardown_stopped_the_server(
    real_server, _floor_monkeypatch
) -> None:
    """Real child: it closes its stdout instead of answering and ignores its closed stdin.

    The probe's teardown then waits for it to exit and kills it. The backstop waits past
    that teardown: a cancel landing inside it would skip the kill and leave it running.
    """
    from kiro_crew import mcp_discovery

    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    # The handshake ends well inside the limit; the teardown's exit wait outlasts what is left.
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 6.0)  # flake-ok: start-up slack
    _floor_monkeypatch.setattr(
        mcp_discovery, "_PROBE_TEARDOWN_WAIT_SECS", 8.0  # flake-ok: outlasts the run's limit
    )
    real_server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], close_stdout=True)
    status, body = await _post({"server": "s", "tool": "t"})
    still_running = [pid for pid in real_server.pids() if not _gone(pid)]
    assert (status, body["code"], still_running) == (502, "server_failed", []), body
    assert real_server.methods()[-1] == "tools/call"


async def test_a_server_that_never_answers_the_handshake_stops_at_the_run_limit(
    real_server, _floor_monkeypatch
) -> None:
    """Real child: a probe timeout raised far past the run's limit does not lengthen the run."""
    from kiro_crew import mcp_discovery
    from kiro_crew import mcp_discovery_probe as probe

    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    real_read = probe._read_stdio_jsonrpc_response
    waits: list[float] = []

    async def read(stream: Any, timeout: float, *args: Any, **kw: Any) -> Any:
        waits.append(timeout)
        return await real_read(stream, timeout, *args, **kw)

    _floor_monkeypatch.setattr(mcp_discovery, "_read_stdio_jsonrpc_response", read)
    _floor_monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 60.0)
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 2.0)  # flake-ok: never answered
    real_server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], silent=True)
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (504, "timeout"), body
    assert waits and max(waits) <= 2.0
    await _all_gone(real_server)


async def test_the_backstop_waits_longer_than_the_probes_teardown_can_take() -> None:
    """A longer teardown wait in the probe cannot quietly bring back a cancelled teardown."""
    from kiro_crew import mcp_discovery

    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    stderr_read = mcp_discovery._STDERR_FIRST_WAIT_SECS + (
        (mcp_discovery._STDERR_READS + 1) * mcp_discovery._STDERR_QUIET_SECS
    )
    exit_waits = 2 * mcp_discovery._PROBE_TEARDOWN_WAIT_SECS
    tree_kill = 5.0  # kill_process_tree's limit on its taskkill run, on Windows
    assert module._TEARDOWN_MARGIN_SECS > stderr_read + exit_waits + tree_kill


async def test_a_cancelled_request_stops_the_server_and_what_it_started(real_server) -> None:
    """Real child and grandchild: a cancelled request reaps the whole process group."""
    if not platform_compat.IS_POSIX:
        pytest.skip("the grandchild is reaped through the probe's process group")
    real_server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], block=True, grandchild=True
    )
    async with TestClient(TestServer(_build_app())) as client:
        pending = asyncio.ensure_future(client.post(_PATH, json={"server": "s", "tool": "t"}))
        try:
            await async_wait_until(lambda: "tools/call" in real_server.methods(), off_loop=True)
            # A gateway shutdown cancels each connection's task, and the handler with it.
            for task in asyncio.all_tasks():
                if "RequestHandler.start" in getattr(task.get_coro(), "__qualname__", ""):
                    task.cancel()
            with pytest.raises(Exception):
                await _within(pending, "the cancelled request")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    await _probe_pool_idle()
    assert len(real_server.pids()) == 2
    await _all_gone(real_server)


async def test_a_large_result_is_cut_with_a_note(server) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}],
        answer=_text("x" * (module._MAX_RESULT_CHARS * 2)),
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert len(body["result"]) == module._MAX_RESULT_CHARS
    assert body["truncated"] is True


async def test_a_credential_in_the_result_is_redacted(server) -> None:
    token = github_classic()
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}, "description": token}],
        answer=_text(f"token={token}"),
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert token not in body["result"]
    status, body = await _post({"server": "s"})
    assert token not in body["tools"][0]["description"]


async def test_a_listing_is_redacted_and_bounded(server) -> None:
    secret = github_classic(seed=3)
    schema = {"type": "object", "properties": {"k": {"type": "string", "default": secret}}}
    tools: list[dict[str, Any]] = [{"name": "t", "inputSchema": schema}, {"name": "n" * 5000}]
    tools += [{"name": f"big{i}", "inputSchema": {"description": "x" * 20000}} for i in range(20)]
    server.set(tools=tools)
    status, body = await _post({"server": "s"})
    assert status == 200
    assert secret not in json.dumps(body)
    assert body["tools"][0]["inputSchema"]["properties"]["k"]["type"] == "string"
    assert all(len(row["name"]) <= 1000 for row in body["tools"])
    assert len(json.dumps(body)) < 512 * 1024
    # Every tool the server listed is either a row or counted as left out.
    assert len(body["tools"]) + body["leftOut"] == 22
    assert body["leftOut"] >= 1


async def test_tools_past_the_cap_are_counted_and_named_when_run(server) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    count = module._MAX_TOOLS + 5
    server.set(
        tools=[{"name": f"t{i}", "annotations": {"readOnlyHint": True}} for i in range(count)]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert len(body["tools"]) == module._MAX_TOOLS
    assert body["leftOut"] == 5

    status, body = await _post({"server": "s", "tool": f"t{count - 1}"})
    assert (status, body["code"]) == (404, "tool_not_listed")
    assert body["error"] == "not listed: over the limit"
    assert "tools/call" not in server.methods()

    status, body = await _post({"server": "s", "tool": "never-announced"})
    assert (status, body["code"]) == (404, "unknown_tool")


async def test_a_listing_inside_the_limits_reports_nothing_left_out(server) -> None:
    server.set(tools=[{"name": "t"}])
    status, body = await _post({"server": "s"})
    assert (status, body["leftOut"]) == (200, 0)


async def test_a_tool_name_the_redactor_would_change_is_counted_and_never_listed(server) -> None:
    secret = github_classic(seed=5)
    tools = [
        {"name": f"fetch_{secret}", "annotations": {"readOnlyHint": True}},
        {"name": f"{secret}", "annotations": {"readOnlyHint": True}},
        {"name": "ordinary-name", "annotations": {"readOnlyHint": True}},
    ]
    server.set(tools=tools)
    status, body = await _post({"server": "s"})
    assert status == 200
    assert secret not in json.dumps(body)
    assert [row["name"] for row in body["tools"]] == ["ordinary-name"]
    assert (body["withheld"], body["leftOut"]) == (2, 0)


async def test_a_withheld_tool_name_is_refused_when_it_is_the_tool_asked_for(server) -> None:
    secret = github_classic(seed=6)
    name = f"fetch_{secret}"
    server.set(tools=[{"name": name, "annotations": {"readOnlyHint": True}}])
    status, body = await _post({"server": "s", "tool": name, "confirm": True})
    assert (status, body["code"]) == (403, "tool_name_withheld")
    assert name not in json.dumps(body)
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "tool_name_withheld")]
    # The security log names the refused call without the credential the name carries.
    assert all(secret not in event["tool_name"] for event in server.sel.events)


async def test_an_ordinary_listing_withholds_nothing(server) -> None:
    server.set(tools=[{"name": "read_file"}, {"name": "list-things.v2"}])
    status, body = await _post({"server": "s"})
    assert (status, body["withheld"], len(body["tools"])) == (200, 0, 2)


async def test_a_schema_the_gateway_discards_is_null_and_an_empty_one_is_kept(server) -> None:
    server.set(
        tools=[
            {"name": "none", "annotations": {"readOnlyHint": True}},
            {"name": "list", "inputSchema": ["a"]},
            {"name": "huge", "inputSchema": {"description": "x" * (64 * 1024)}},
            {"name": "empty", "inputSchema": {}},
        ]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert {row["name"]: row["inputSchema"] for row in body["tools"]} == {
        "none": None,
        "list": None,
        "huge": None,
        "empty": {},
    }


async def test_a_schema_that_stops_being_json_once_redacted_is_null(
    server, _floor_monkeypatch
) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    real = module._redacted
    _floor_monkeypatch.setattr(
        module, "_redacted", lambda text: "{" if text.startswith('{"type"') else real(text)
    )
    server.set(tools=[{"name": "t", "inputSchema": {"type": "object"}}])
    status, body = await _post({"server": "s"})
    assert (status, body["tools"][0]["inputSchema"]) == (200, None)


@pytest.mark.parametrize("body", [{}, {"server": ""}, {"server": "s", "arguments": [1]}])
async def test_a_malformed_request_is_refused(server, body) -> None:
    status, answer = await _post(body)
    assert status == 400
    assert answer["code"] == "bad_args"
    assert server.entries() == []


async def test_the_setting_is_off_unless_it_is_a_real_true() -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.section_builders import _build_mcp_config

    assert KiroCrewConfig().mcp.tool_tester is False
    assert _build_mcp_config({"tool_tester": "true"}).tool_tester is False
    assert _build_mcp_config({"tool_tester": True}).tool_tester is True


def _governed(_floor_monkeypatch: pytest.MonkeyPatch, governance: object) -> None:
    """The dashboard process's installed ceiling, as boot composes it."""
    import types

    from kiro_crew.platform import context as platform_context

    _floor_monkeypatch.setattr(
        platform_context, "current_context", lambda: types.SimpleNamespace(governance=governance)
    )


async def test_a_tool_the_governance_ceiling_denies_never_runs(server, _floor_monkeypatch) -> None:
    """An administrator's deny of ``@server/tool`` binds this run as it binds an agent's call."""
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy({"version": 1, "boot": {}, "mcp": {"mode": "deny", "deny": ["@s/t"]}})
    _governed(_floor_monkeypatch, ceiling)
    read_only = {"annotations": {"readOnlyHint": True}}
    server.set(tools=[{"name": "t", **read_only}, {"name": "u", **read_only}], answer=_text("ok"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (403, "governance_denied")
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "governance_denied")]
    status, body = await _post({"server": "s", "tool": "u"})
    assert status == 200 and body["isError"] is False


async def test_a_governance_check_that_cannot_run_refuses_the_call(
    server, _floor_monkeypatch
) -> None:
    """Fail closed: a ceiling that cannot be read is never read as no ceiling."""
    from kiro_crew.platform import context as platform_context

    def unreadable() -> object:
        raise RuntimeError("no composed context")

    _floor_monkeypatch.setattr(platform_context, "current_context", unreadable)
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (503, "governance_unavailable")
    assert "tools/call" not in server.methods()


async def test_a_timeout_reported_with_the_servers_output_is_still_a_timeout() -> None:
    """The probe appends the server's last output to an error; a timeout stays a timeout."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    probed = McpServerInfo(name="s", command="x")
    probed.status, probed.error = "error", "timeout\nstderr: still working"
    assert module._failure(probed)[:2] == (504, "timeout")
    probed.error = "tools/call: no response\nstderr: timeout"
    assert module._failure(probed)[:2] == (502, "server_failed")


async def test_the_tool_answer_is_read_against_the_run_limit(_floor_monkeypatch) -> None:
    """A tool slower than the handshake timeout still has the run's own limit to answer in."""
    import types

    from kiro_crew import mcp_discovery
    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    waited: list[float] = []
    answer = {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    async def read(_stdout: object, timeout: float, **_kw: object) -> dict[str, Any]:
        waited.append(timeout)
        return answer

    class _Pipe:
        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    _floor_monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 15.0)
    proc = types.SimpleNamespace(stdin=_Pipe(), stdout=None)
    step = ToolCallStep(  # flake-ok: the reader is faked, so no time passes against this limit
        lambda _tools: {"name": "t", "arguments": {}}, timeout=600.0
    )
    assert await probe._call_one_tool(proc, "s", step, [{"name": "t"}], [])
    assert step.answer == answer
    assert len(waited) == 1 and 500.0 < waited[0] <= 600.0
    # Without a limit of its own the step keeps the probe's timeout.
    waited.clear()
    plain = ToolCallStep(lambda _tools: {"name": "t", "arguments": {}})
    assert await probe._call_one_tool(proc, "s", plain, [{"name": "t"}], [])
    assert waited == [15.0]


async def test_the_profile_bound_to_the_dashboard_surface_governs_the_run(
    server, _floor_monkeypatch, tmp_path
) -> None:
    """The run is asked as the dashboard surface, so a profile bound to it denies the tool."""
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.sel import _infer_source

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    deny = {"mode": "deny", "deny": ["@s/t"]}
    profile = {"name": "dash", "bind": {"type": "surface", "id": "dashboard"}, "mcp": deny}
    (profiles / "dash.json").write_text(json.dumps(profile), encoding="utf-8")
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    _governed(_floor_monkeypatch, None)
    gp.reset_store()
    try:
        read_only = {"annotations": {"readOnlyHint": True}}
        server.set(tools=[{"name": "t", **read_only}], answer=_text("ok"))
        status, body = await _post({"server": "s", "tool": "t"})
    finally:
        gp.reset_store()
    assert status == 403, body
    assert body["code"] == "governance_denied"
    assert "tools/call" not in server.methods()
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    assert _infer_source(module._SESSION_KEY) == "dashboard"


async def test_registering_the_routes_loads_no_config_and_no_tester_module(
    _floor_monkeypatch,
) -> None:
    """Start-up only adds the route: no config read, and no import of the tester's code."""
    import ast
    import builtins

    from kiro_crew.config.loader import KiroCrewConfig

    loads: list[int] = []
    asked: list[str] = []
    real_import = builtins.__import__

    def spy(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level=0):
        asked.extend([name, *(fromlist or ())])
        return real_import(name, globals_, locals_, fromlist, level)

    with _floor_monkeypatch.context() as patched:
        patched.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: loads.append(1)))
        patched.setattr(builtins, "__import__", spy)
        agent_config_routes.register(web.Application())
    assert loads == []
    assert not any("mcp_try_tool" in str(name) for name in asked)
    # Importing the routes module (as start-up does) names the tester nowhere at its top.
    tree = ast.parse(Path(agent_config_routes.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any("mcp_try_tool" in ast.dump(node) for node in top)


async def test_the_setting_is_read_per_request_so_it_needs_no_restart(
    server, _floor_monkeypatch
) -> None:
    """One registered app answers as an unknown route, then runs once the setting is on."""
    flag = [False]
    _floor_monkeypatch.setattr(agent_config_routes, "tool_tester_enabled", lambda: flag[0])
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    async with TestClient(TestServer(_build_app())) as client:
        off = await client.post(_PATH, json={"server": "s"})
        assert off.status == 404
        flag[0] = True
        on = await client.post(_PATH, json={"server": "s"})
        assert on.status == 200
        assert [row["name"] for row in (await on.json())["tools"]] == ["t"]


class _RecordingPipe:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


async def test_a_cancel_queued_while_the_call_is_chosen_sends_nothing(
    _floor_monkeypatch,
) -> None:
    """A cancel that arrives while ``choose`` blocks stops the run before the call is written."""
    import types

    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    async def read(_stdout: object, _timeout: float, **_kw: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    pipe = _RecordingPipe()
    proc = types.SimpleNamespace(stdin=pipe, stdout=None)

    def choose(_tools: list[Any]) -> dict[str, Any]:
        # As the probe's private loop gets a request's cancel: queued, not yet run.
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return {"name": "t", "arguments": {}}

    with pytest.raises(asyncio.CancelledError):
        await asyncio.ensure_future(
            probe._call_one_tool(proc, "s", ToolCallStep(choose), [{"name": "t"}], [])
        )
    assert pipe.written == []


async def test_a_run_whose_limit_ran_out_while_choosing_sends_nothing(_floor_monkeypatch) -> None:
    """With no time left once the call is chosen, nothing is written and it reads as a timeout."""
    import types

    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    async def read(_stdout: object, _timeout: float, **_kw: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    pipe = _RecordingPipe()
    proc = types.SimpleNamespace(stdin=pipe, stdout=None)
    step = ToolCallStep(lambda _tools: {"name": "t", "arguments": {}}, timeout=0)
    with pytest.raises(asyncio.TimeoutError):
        await probe._call_one_tool(proc, "s", step, [{"name": "t"}], [])
    assert pipe.written == []


async def test_the_result_is_dumped_and_redacted_off_the_gateway_loop(
    server, _floor_monkeypatch
) -> None:
    """A big result is turned to text and redacted in a worker thread, and is still cut."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    token = github_classic()
    callees: list[str] = []
    real = asyncio.to_thread

    async def recording(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        callees.append(getattr(func, "__name__", ""))
        return await real(func, *args, **kwargs)

    _floor_monkeypatch.setattr(asyncio, "to_thread", recording)
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}],
        answer=_text(f"{token} " + "x" * (module._MAX_RESULT_CHARS * 2)),
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200, body
    assert "_result_text" in callees
    assert token not in body["result"]
    assert len(body["result"]) == module._MAX_RESULT_CHARS
    assert body["truncated"] is True


async def test_a_credential_across_the_cut_is_redacted_before_the_text_is_cut() -> None:
    """The cut falls inside the credential, so only a redact-then-cut order hides all of it."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    token = github_classic()
    lead = len('{\n  "t": "')
    filler = "a" * (module._MAX_RESULT_CHARS - lead - 8)
    text, cut = module._result_text({"t": filler + token})
    assert cut is True
    assert len(text) <= module._MAX_RESULT_CHARS
    assert token[:8] not in text
    short, cut = module._result_text({"t": "ok"})
    assert (short, cut) == ('{\n  "t": "ok"\n}', False)


class _Stderr:
    """A server's stderr: its chunks, then a read that waits until the stream is closed."""

    def __init__(self, chunks: list[bytes], *, then_end: bool = False) -> None:
        self.chunks = list(chunks)
        self.then_end = then_end
        self.waiting = asyncio.Event()
        self.reads = 0

    async def read(self, _size: int) -> bytes:
        self.reads += 1
        if self.chunks:
            return self.chunks.pop(0)
        if self.then_end:
            return b""
        self.waiting.set()
        await asyncio.Event().wait()
        return b""


def _drains() -> list[asyncio.Task[Any]]:
    return [
        t
        for t in asyncio.all_tasks()
        if "_drain_stderr" in getattr(t.get_coro(), "__qualname__", "")
    ]


async def test_a_drain_keeps_the_start_of_what_a_server_wrote_and_says_what_it_dropped() -> None:
    from kiro_crew import mcp_discovery_probe as probe

    size = probe._STDERR_KEPT_BYTES
    # More than it keeps: the first bytes stay, the rest is read and thrown away.
    stream = _Stderr([b"a" * size, b"b" * 10, b"c" * 5], then_end=True)
    kept = probe._StderrHead()
    await _within(probe._drain_stderr(stream, kept), "the drain of a finished stream")
    assert bytes(kept.data) == b"a" * size
    assert kept.cut is True
    assert stream.reads == 4
    # A chunk that straddles the bound is cut inside it.
    stream = _Stderr([b"a" * (size - 3), b"b" * 10], then_end=True)
    straddle = probe._StderrHead()
    await _within(probe._drain_stderr(stream, straddle), "the drain of a straddling chunk")
    assert bytes(straddle.data) == b"a" * (size - 3) + b"bbb"
    assert straddle.cut is True
    # Exactly what it keeps drops nothing.
    stream = _Stderr([b"a" * size], then_end=True)
    exact = probe._StderrHead()
    await _within(probe._drain_stderr(stream, exact), "the drain of an exact fit")
    assert (bytes(exact.data), exact.cut) == (b"a" * size, False)
    # Anything but bytes ends the read, as it does for the failure report.
    odd = probe._StderrHead()
    stream = _Stderr([b"x", "text"])  # type: ignore[list-item]
    await _within(probe._drain_stderr(stream, odd), "the drain of a stream that sent text")
    assert (bytes(odd.data), odd.cut) == (b"x", False)


async def test_the_failure_report_starts_with_what_the_drain_took(_floor_monkeypatch) -> None:
    from kiro_crew import mcp_discovery_probe as probe

    kept = probe._StderrHead()
    kept.data += b"taken by the drain\n"
    stream = _Stderr([b" left in the pipe\n"], then_end=True)
    text = await _within(probe._stderr_text(stream, kept), "the report")
    assert text == ("taken by the drain\n left in the pipe")
    empty = await _within(probe._stderr_text(_Stderr([], then_end=True)), "the empty report")
    assert empty == ""


async def test_nothing_is_appended_after_what_the_drain_dropped(_floor_monkeypatch) -> None:
    from kiro_crew import mcp_discovery_probe as probe

    kept = probe._StderrHead()
    kept.data += b"the first bytes"
    kept.cut = True
    stream = _Stderr([b"what followed the dropped stretch"], then_end=True)
    text = await _within(probe._stderr_text(stream, kept), "the report after a cut")
    assert text == "the first bytes"
    assert stream.reads == 0


async def test_a_full_report_says_so_only_when_more_was_left_in_the_pipe(
    _floor_monkeypatch,
) -> None:
    from kiro_crew import mcp_discovery_probe as probe

    for after, more in (([b"x"], True), ([], False)):
        kept = probe._StderrHead()
        kept.data += b"a" * probe._STDERR_KEPT_BYTES
        stream = _Stderr(after, then_end=True)
        text = await _within(probe._stderr_text(stream, kept), "the full report")
        assert text == "a" * probe._STDERR_KEPT_BYTES
        assert kept.cut is more


def _stalled_call(
    _floor_monkeypatch: pytest.MonkeyPatch, after_drain: Any
) -> tuple[Any, Any, _Stderr, Any]:
    """A call whose reply waits for the drain to be reading, then is *after_drain*'s.

    The fake reader honours the timeout it is given, so a drain that never reads fails
    this wait by the step's own limit, never by hanging the test.
    """
    import types

    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    stderr = _Stderr([b"warming up\n"])

    async def read(_stdout: object, timeout: float, **_kw: object) -> dict[str, Any]:
        await asyncio.wait_for(stderr.waiting.wait(), timeout)
        return await after_drain()

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    proc = types.SimpleNamespace(stdin=_RecordingPipe(), stdout=None, stderr=stderr)
    step = ToolCallStep(
        lambda _tools: {"name": "t", "arguments": {}}, timeout=FALLBACK_TIMEOUT_SECS
    )  # flake-ok: the lost-run bound of a stalled fake; a working drain never reaches it
    return probe, proc, stderr, step


def _stop_drains() -> None:
    """Cancel a drain a failing run left behind, so it cannot outlive its test."""
    for task in _drains():
        task.cancel()


async def test_the_drain_is_gone_once_the_answer_arrives(_floor_monkeypatch) -> None:
    async def answer() -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 4, "result": {}}

    probe, proc, stderr, step = _stalled_call(_floor_monkeypatch, answer)
    try:
        sent = await _within(probe._call_one_tool(proc, "s", step, [{"name": "t"}], []), "the call")
        assert sent
        assert step.answer is not None
        assert bytes(step.stderr.data) == b"warming up\n"
        assert _drains() == []
    finally:
        _stop_drains()


async def test_the_drain_is_gone_after_a_timeout(_floor_monkeypatch) -> None:
    async def never() -> dict[str, Any]:
        raise asyncio.TimeoutError

    probe, proc, stderr, step = _stalled_call(_floor_monkeypatch, never)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await _within(probe._call_one_tool(proc, "s", step, [{"name": "t"}], []), "the call")
        assert stderr.waiting.is_set()
        assert _drains() == []
    finally:
        _stop_drains()


async def test_the_drain_is_gone_after_a_cancel(_floor_monkeypatch) -> None:
    async def silence() -> dict[str, Any]:
        await asyncio.Event().wait()  # no answer ever; the cancel below ends it
        return {}

    probe, proc, stderr, step = _stalled_call(_floor_monkeypatch, silence)
    call = asyncio.ensure_future(probe._call_one_tool(proc, "s", step, [{"name": "t"}], []))
    try:
        await _within(stderr.waiting.wait(), "the drain's first idle read")
        assert len(_drains()) == 1
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _within(call, "the cancelled call")
        assert _drains() == []
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        _stop_drains()


async def test_a_server_that_fills_its_stderr_before_it_answers_still_answers(
    tmp_path, _floor_monkeypatch
) -> None:
    """Real child: about 1 MiB on stderr stops the server on a full pipe if nothing reads it."""
    from kiro_crew.mcp_discovery import ToolCallStep

    # The reader's own buffer is what the pipe fills into first; a small one makes 1 MiB enough.
    _floor_monkeypatch.setenv("KIROCREW_MCP_READ_LIMIT", "1024")
    fake = _RealServer(tmp_path / "noisy")
    fake.set(tools=[{"name": "t"}], answer=_text("done"), stderr_bytes=1024 * 1024)
    # A limit the run never reaches when the drain works; it only bounds a stalled one.
    step = ToolCallStep(  # flake-ok: bounds the run on the failing head only
        lambda _tools: {"name": "t", "arguments": {}}, timeout=20.0
    )
    probed = await _within(
        probe_server(
            McpServerInfo(name="p", command=sys.executable, args=fake.argv),
            client_info={"name": "x", "version": "1"},
            tool_call=step,
        ),
        "the probe of a noisy server",
    )
    assert probed.status == "ok", probed.error
    assert step.answer is not None and step.answer["result"] == _text("done")
    await _all_gone(fake)


async def test_a_call_that_fails_after_noisy_stderr_shows_its_start_and_says_more_was_cut(
    tmp_path, _floor_monkeypatch
) -> None:
    """Real child: a server that dies after a flood reports what it kept, as a probe does."""
    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    _floor_monkeypatch.setenv("KIROCREW_MCP_READ_LIMIT", "1024")
    fake = _RealServer(tmp_path / "dies")
    fake.set(
        tools=[{"name": "t"}],
        stderr_bytes=1024 * 1024,
        stderr_last="\nLAST-WORDS: out of memory",
        die=True,
    )
    step = ToolCallStep(  # flake-ok: bounds the run on the failing head only
        lambda _tools: {"name": "t", "arguments": {}}, timeout=20.0
    )
    probed = await _within(
        probe_server(
            McpServerInfo(name="p", command=sys.executable, args=fake.argv),
            client_info={"name": "x", "version": "1"},
            tool_call=step,
        ),
        "the probe of a dying server",
    )
    assert probed.status == "error"
    assert probed.error.startswith("tools/call: no response")
    shown = probed.error.partition("\nstderr: ")[2]
    assert shown.endswith("\n" + probe._STDERR_CUT_NOTE)
    assert "LAST-WORDS" not in probed.error
    assert len(step.stderr.data) == probe._STDERR_KEPT_BYTES
    await _all_gone(fake)


async def test_a_private_key_longer_than_what_is_kept_is_never_shown_in_part(real_server) -> None:
    """Real child, through the route: the key's first line stays, so the block is redacted whole.

    The server writes an armored block larger than the kept bytes and stops without an
    answer. Keeping the last bytes would cut the BEGIN line away and show base64 lines
    the redactor cannot know for key material.
    """
    from kiro_crew import mcp_discovery_probe as probe

    begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    end = "-----END " + "RSA PRIVATE KEY-----"
    lines = [base64.b64encode(bytes([n % 251]) * 48).decode() for n in range(800)]
    block = "\n".join([begin, *lines, end]) + "\n"
    assert len(block) > probe._STDERR_KEPT_BYTES * 1.3
    real_server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}],
        stderr_text=block,
        die=True,
    )
    status, reply = await _post({"server": "s", "tool": "t"})
    assert (status, reply["code"]) == (502, "server_failed"), reply
    # A Windows child ends its stderr lines with CRLF.
    error = reply["error"].replace("\r\n", "\n")
    for line in lines:
        assert line not in error
    assert "[REDACTED" in error
    # The block is redacted from its first line to the end of the text, so the note
    # that follows the kept bytes is inside it; the first line itself may stay.
    assert begin not in error or error.endswith(begin + "\n[REDACTED: credential]")
    await _all_gone(real_server)
