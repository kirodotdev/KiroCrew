"""KAS's membership in ``ACP_BACKENDS_SESSION_SHARING``, measured on the real relay.

A KAS chat's sub-agents run as sessions on the chat's own ``kiro-cli acp
--agent-engine v3`` process. The membership rests on facts about the installed
engine, and this file measures each one against it:

1. MCP dedupe: sessions in one folder with the same user server share one copy of
   it, while a server whose env carries the session (Crew's own per-session
   servers) starts once per session.
2. ``session/close`` reaches the engine through the relay and frees only the
   addressed session: its per-session server stops, a sibling's keeps running.
3. The closed session's record survives: ``session/load`` restores it on the same
   process and on a restarted one (the shape ``spawn_continue`` takes, since a
   continuation runs on a dedicated process).
4. A same-named per-session server on a shared process answers each session from
   its own instance, never the newest session's: a process-wide pool keyed by
   server name would hand every session the instance whose env names the last one.

An opt-in lane: it runs only with ``KIROCREW_KAS_LIVE=1`` and an installed,
signed-in kiro-cli, and skips otherwise (CI has neither). 3's recall and 4 make
model calls, so they also need ``KIROCREW_KAS_LIVE_MODEL=1``.

Where it writes: each relay runs with ``HOME`` set to a directory under the
test's ``tmp_path`` and its process cwd there too, so the engine's session store,
settings and the operator's own MCP servers stay out of it. The one exception is
kiro-cli's data directory, linked into that home so the relay can use the
operator's sign-in; the relay may update its own state there as any kiro-cli run
does. Every session a test opens is deleted at its end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew.platform_compat import pid_exists
from kiro_crew.testing.wait import wait_until

#: The whole test's bound; every RPC wait is at most half of it, so a request the
#: relay never answers fails with its name and the fixture still cleans up.
pytestmark = pytest.mark.timeout(600)

ARGV = ["kiro-cli", "acp", "--agent-engine", "v3", "--auth-method", "cli"]
TIMEOUT_S = 240

_FAKE_SERVER = r"""
import json, os, sys
log, mark, tag = os.environ["R2B_LOG"], os.environ.get("R2B_MARK", "-"), sys.argv[1]
def note(ev):
    with open(log, "a") as f:
        f.write(json.dumps({"ev": ev, "tag": tag, "mark": mark, "pid": os.getpid()}) + "\n")
note("start")
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("id") is None:
        continue
    m = msg.get("method")
    if m == "initialize":
        res = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
               "capabilities": {"tools": {}}, "serverInfo": {"name": tag, "version": "0"}}
    elif m == "tools/list":
        name = "whoami" if tag == "crew" else "ping_" + tag
        res = {"tools": [{"name": name, "description": "Return the caller's mark.",
                          "inputSchema": {"type": "object", "properties": {}}}]}
    elif m == "tools/call":
        note("call")
        res = {"content": [{"type": "text", "text": "MARK-" + mark}]}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": "no"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
"""


def _kiro_cli_data_dir() -> Path | None:
    """kiro-cli's data directory (sign-in and engine extracts), or None."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    found = base / "kiro-cli"
    return found if found.is_dir() else None


def _require_kas() -> Path:
    if os.environ.get("KIROCREW_KAS_LIVE") != "1":
        pytest.skip("set KIROCREW_KAS_LIVE=1 to run the live KAS lane")
    data = _kiro_cli_data_dir()
    if shutil.which("kiro-cli") is None or data is None:
        pytest.skip("kiro-cli is not installed or has no data directory")
    return data


def _require_model() -> None:
    if os.environ.get("KIROCREW_KAS_LIVE_MODEL") != "1":
        pytest.skip("set KIROCREW_KAS_LIVE_MODEL=1 to spend model calls on the KAS lane")


class _Acp:
    """A minimal ACP client over the relay's stdio. Answers permission asks with allow."""

    def __init__(self, stderr: Path, home: Path, cwd: Path) -> None:
        self.proc = subprocess.Popen(
            ARGV,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr.open("w"),
            text=True,
            bufsize=1,
            encoding="utf-8",
            cwd=cwd,
            env=dict(os.environ, HOME=str(home)),
        )
        self._next = 1
        self._pending: dict[int, list] = {}
        self.updates: list[dict] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            result = self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        except BaseException:
            self.close()
            raise
        self.capabilities = (result.get("agentCapabilities") or {}).get("sessionCapabilities") or {}

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" in msg and "id" in msg:
                self._answer(msg)
            elif "method" in msg:
                self.updates.append(msg)
            elif msg.get("id") in self._pending:
                slot = self._pending[msg["id"]]
                slot[1] = msg
                slot[0].set()

    def _answer(self, msg: dict) -> None:
        if msg["method"] == "session/request_permission":
            options = msg.get("params", {}).get("options", [])
            pick = next((o for o in options if o.get("kind") == "allow_once"), None)
            outcome = (
                {"outcome": "selected", "optionId": pick["optionId"]}
                if pick
                else {"outcome": "cancelled"}
            )
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}})
        else:
            self._send(
                {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}
            )

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        with self._lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        mid = self._next
        self._next += 1
        slot: list = [threading.Event(), None]
        self._pending[mid] = slot
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        assert slot[0].wait(TIMEOUT_S), f"{method} unanswered after {TIMEOUT_S}s"
        msg = slot[1]
        assert "error" not in msg, f"{method}: {msg['error']}"
        return msg.get("result") or {}

    def reply(self, sid: str, text: str) -> str:
        mark = len(self.updates)
        self.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
        chunks = []
        for update in self.updates[mark:]:
            params = update.get("params", {})
            body = params.get("update", {})
            if (
                params.get("sessionId") == sid
                and body.get("sessionUpdate") == "agent_message_chunk"
            ):
                chunks.append((body.get("content") or {}).get("text", ""))
        return "".join(chunks).strip()

    def servers(self, sid: str) -> dict[str, str]:
        """The engine's latest ``_kiro/mcp/status`` for *sid*: server name -> status."""
        for update in reversed(self.updates):
            params = update.get("params", {})
            if update.get("method") == "_kiro/mcp/status" and params.get("sessionId") == sid:
                return {s.get("name"): s.get("status") for s in params.get("servers", [])}
        return {}

    def wait_connected(self, sid: str, names: tuple[str, ...]) -> None:
        """Wait until the engine reports every server in *names* connected for *sid*."""
        wait_until(
            lambda: all(self.servers(sid).get(n) == "connected" for n in names),
            describe=lambda: f"{sid}: servers not connected: {self.servers(sid)}",
        )

    def close(self) -> None:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(TIMEOUT_S)


class _Lane:
    """One test's folder, fake servers and the processes it started."""

    def __init__(self, root: Path, kiro_cli_data: Path) -> None:
        self.root = root
        self.work = root / "work"
        self.work.mkdir()
        self.home = root / "home"
        linked = self.home / ".local" / "share" / "kiro-cli"
        if sys.platform == "darwin":
            linked = self.home / "Library" / "Application Support" / "kiro-cli"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(kiro_cli_data, target_is_directory=True)
        self.server = root / "fake_server.py"
        self.server.write_text(_FAKE_SERVER, encoding="utf-8")
        self.log = root / "servers.log"
        self.sids: list[str] = []
        self._procs: list[_Acp] = []

    def start(self) -> _Acp:
        acp = _Acp(self.root / f"stderr-{len(self._procs)}.log", self.home, self.work)
        self._procs.append(acp)
        return acp

    def _entry(self, tag: str, mark: str | None) -> dict:
        env = {"R2B_LOG": str(self.log)}
        if mark is not None:
            env["R2B_MARK"] = mark
        return {"command": sys.executable, "args": [str(self.server), tag], "env": env}

    def params(self, mark: str) -> dict:
        """A ``session/new`` body: one user server shared by config, one per-session one."""
        crew = self._entry("crew", mark)
        session_servers = [
            {
                "name": "crewSession",
                "command": crew["command"],
                "args": crew["args"],
                "env": [{"name": k, "value": v} for k, v in crew["env"].items()],
            }
        ]
        agent = {
            "id": "r2b-lane",
            "description": "KAS sharing lane",
            "prompt": "You are a probe. Answer in as few words as possible.",
            "tools": "*",
            "mcpServers": {"userServer": self._entry("user", None)},
        }
        return {
            "cwd": str(self.work),
            "mcpServers": session_servers,
            "_meta": {"kiro": {"customAgents": [agent], "modeId": agent["id"]}},
        }

    def new(self, acp: _Acp, mark: str) -> str:
        sid = acp.call("session/new", self.params(mark))["sessionId"]
        self.sids.append(sid)
        acp.wait_connected(sid, SERVERS)
        return sid

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x.strip()]

    def live(self, tag: str) -> list[str]:
        """Marks of the live server processes with *tag*, one entry per process."""
        out = []
        for ev in self.events():
            if ev["ev"] == "start" and ev["tag"] == tag and _alive(ev["pid"]):
                out.append(ev["mark"])
        return sorted(out)

    def settle(self, predicate, what: str) -> None:
        wait_until(predicate, describe=lambda: f"{what}: {self.events()}")

    def cleanup(self) -> None:
        acp = next((p for p in reversed(self._procs) if p.proc.poll() is None), None)
        if acp is None and self.sids:
            acp = self.start()
        for sid in self.sids:
            try:
                acp.call("_kiro/session/delete", {"sessionId": sid}) if acp else None
            except AssertionError:
                pass
        for proc in self._procs:
            proc.close()


#: The two servers every lane session declares.
SERVERS = ("userServer", "crewSession")


def _alive(pid: int) -> bool:
    return pid_exists(pid)


@pytest.fixture
def lane(tmp_path):
    value = _Lane(tmp_path, _require_kas())
    try:
        yield value
    finally:
        value.cleanup()


def test_the_engine_advertises_close(lane):
    assert "close" in lane.start().capabilities


def test_n_children_share_the_user_server_and_keep_their_own(lane):
    """A parent and three children in one folder: one user server, four per-session ones."""
    acp = lane.start()
    for mark in ("parent", "c1", "c2", "c3"):
        lane.new(acp, mark)
    # Every session has reported both servers connected, so each instance it uses
    # has started: the population is final.
    assert lane.live("user") == ["-"], "one shared user server for four sessions"
    assert lane.live("crew") == ["c1", "c2", "c3", "parent"]


def test_close_stops_only_the_closed_sessions_server(lane):
    acp = lane.start()
    first = lane.new(acp, "s1")
    lane.new(acp, "s2")
    assert lane.live("crew") == ["s1", "s2"]
    acp.call("session/close", {"sessionId": first})
    lane.settle(lambda: lane.live("crew") == ["s2"], "only the closed session's server stops")
    assert lane.live("user") == ["-"], "the shared user server stays for the sibling"


def test_a_closed_session_loads_on_the_same_and_a_restarted_process(lane):
    acp = lane.start()
    sid = lane.new(acp, "s1")
    acp.call("session/close", {"sessionId": sid})
    acp.call("session/load", dict(lane.params("s1"), sessionId=sid))
    acp.call("session/close", {"sessionId": sid})
    acp.close()
    restarted = lane.start()
    restarted.call("session/load", dict(lane.params("s1"), sessionId=sid))


def test_a_closed_session_recalls_its_first_turn_after_a_restart(lane):
    """``spawn_continue``'s shape: close on one process, load and continue on another."""
    _require_model()
    acp = lane.start()
    sid = lane.new(acp, "s1")
    assert "ALPHA-42" in acp.reply(sid, "Reply with exactly: ALPHA-42")
    acp.call("session/close", {"sessionId": sid})
    acp.close()
    restarted = lane.start()
    restarted.call("session/load", dict(lane.params("s1"), sessionId=sid))
    answer = restarted.reply(
        sid,
        "Earlier in this conversation you replied with a code that starts with ALPHA-. "
        "Reply with only that code.",
    )
    assert "ALPHA-42" in answer


def test_a_same_named_per_session_server_answers_its_own_session(lane):
    """Each session's call reaches the same-named server instance with its own env."""
    _require_model()
    acp = lane.start()
    sids = {mark: lane.new(acp, mark) for mark in ("s1", "s2", "s3")}
    assert lane.live("crew") == ["s1", "s2", "s3"]
    ask = "Call the whoami tool once and reply with only the exact text it returned."
    for mark in ("s1", "s2", "s3"):
        assert f"MARK-{mark}" in acp.reply(sids[mark], ask)
    calls = [ev["mark"] for ev in lane.events() if ev["ev"] == "call" and ev["tag"] == "crew"]
    assert sorted(calls) == ["s1", "s2", "s3"]
