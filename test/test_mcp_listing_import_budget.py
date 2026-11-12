"""What a Kiro Crew stdio server has loaded by the time it has listed its tools.

An agent engine starts every MCP server of a session when the session starts and holds
the first prompt until each has answered ``initialize`` and ``tools/list``. Whatever a
server imports before that answer is paid on every session start, once per server, so a
server answers both before it imports its tool handlers (``kiro_crew.mcp_shared_listing``).

Each check runs the server's real entry in a fresh interpreter, feeds it the two
requests, and reads the modules loaded at the moment the list went out. Two bounds hold
that set: a named denylist, which says WHICH expensive graph came back, and a count of
package modules, which catches growth no name predicted.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_mcp_slim_entry import _child_env

from kiro_crew import mcp_entry
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: Never loaded before the list, by any server. Each is the root of a large graph that
#: only a tool call needs: the HTTP and cloud client stacks, the agent client, the CLI,
#: the OS sandbox, the gateway's side of the MCP socket, the conversation store, and the
#: two facades that load every sub-agent and tool handler.
FORBIDDEN_BEFORE_THE_LIST = (
    "aiohttp",
    "awscrt",
    "boto3",
    "botocore",
    "slack_sdk",
    "kiro_crew.acp.client",
    "kiro_crew.cli",
    "kiro_crew.history",
    "kiro_crew.mcp_core",
    "kiro_crew.mcp_gateway.manager",
    "kiro_crew.mcp_tools.spawn",
    "kiro_crew.sandbox",
    "kiro_crew.session",
    "kiro_crew.subagent",
)

#: command -> the most ``kiro_crew`` modules it may hold when it has listed. A ceiling
#: with room for the modules one platform loads and another does not; a change that needs
#: more names what it added and why a listing needs it, or moves the import behind the
#: list. Lower a figure when a change brings the count down.
PACKAGE_MODULE_CEILING = {
    "mcp-core": 485,
    "mcp-cron": 355,
    "mcp-guide": 350,
    "mcp-mate": 350,
}

#: The modules that read the two live values in the spawn tools' descriptions. They are
#: on core's listing path by design, so the bounds above hold what they import too.
LIVE_HINT_READERS = (
    "kiro_crew.history_metadata_line",
    "kiro_crew.subagent_parts.host_memory",
    "kiro_crew.subagent_parts.spawn_allowlists",
    "kiro_crew.subagent_standalone",
)

_MARK = "MODULES-AT-LIST "

#: Runs the server's entry on a stdin holding the two requests, and stops it where the
#: handler import would begin, printing what is loaded.
_PROBE = f"""
import io, json, os, sys
from kiro_crew import mcp_entry

read_fd, write_fd = os.pipe()
os.write(write_fd, sys.argv[2].encode("utf-8"))
os.close(write_fd)
sys.stdin = io.TextIOWrapper(io.open(read_fd, "rb"))
answer = mcp_entry._answer_before_handlers


def answer_then_stop(command):
    answer(command)
    sys.stdout.flush()
    sys.stderr.write({_MARK!r} + json.dumps(sorted(sys.modules)) + "\\n")
    raise SystemExit(0)


mcp_entry._answer_before_handlers = answer_then_stop
mcp_entry.slim_server([sys.argv[1]])
raise SystemExit("the entry returned without answering")
"""

_REQUESTS = "".join(
    json.dumps(frame) + "\n"
    for frame in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "budget", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
)


class _Policy(http.server.BaseHTTPRequestHandler):
    """The one gateway read a listing makes: this session's tool policy."""

    def do_GET(self) -> None:  # noqa: N802 - the handler protocol's name
        body = b'{"exclude": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


class _LoopbackServer(http.server.ThreadingHTTPServer):
    """Binds without ``HTTPServer.server_bind``'s host-name lookup, which can stall."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)


@pytest.fixture(scope="module")
def gateway_port() -> Iterator[int]:
    server = _LoopbackServer(("127.0.0.1", 0), _Policy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=FALLBACK_TIMEOUT_SECS)


def _listed(command: str, tmp_path: Path, port: int) -> tuple[list[dict], set[str]]:
    """The frames *command* answered, and the modules it held when it had answered."""
    (tmp_path / "home").mkdir(exist_ok=True)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, command, _REQUESTS],
        env=_child_env(
            tmp_path,
            KIROCREW_PROJECT_DIR=str(tmp_path),
            KIROCREW_PORT=str(port),
            KIROCREW_SESSION_KEY="dashboard:budget",
        ),
        cwd=str(tmp_path),
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
        **UTF8_TEXT,
    )
    marked = [line for line in done.stderr.splitlines() if line.startswith(_MARK)]
    assert done.returncode == 0 and marked, done.stderr[-4000:]
    frames = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    return frames, set(json.loads(marked[-1][len(_MARK) :]))


@pytest.fixture(scope="module")
def listed(
    tmp_path_factory: pytest.TempPathFactory, gateway_port: int
) -> dict[str, tuple[list[dict], set[str]]]:
    return {
        command: _listed(command, tmp_path_factory.mktemp(command), gateway_port)
        for command in sorted(mcp_entry.EARLY_ANSWERS)
    }


def test_every_ceiling_names_a_server_that_answers_early() -> None:
    assert set(PACKAGE_MODULE_CEILING) == set(mcp_entry.EARLY_ANSWERS)
    assert set(mcp_entry.EARLY_ANSWERS) <= set(mcp_entry.SLIM_SERVERS)


@pytest.mark.parametrize("command", sorted(PACKAGE_MODULE_CEILING))
def test_the_server_answers_both_requests_before_its_handlers_load(
    command: str, listed: dict[str, tuple[list[dict], set[str]]]
) -> None:
    frames, modules = listed[command]
    by_id = {frame.get("id"): frame for frame in frames}
    assert by_id[1]["result"]["serverInfo"]["name"].startswith("kirocrew-")
    assert by_id[2]["result"]["tools"], "the list went out empty"
    handler_module, _ = mcp_entry.SLIM_SERVERS[command]
    assert handler_module not in modules


@pytest.mark.parametrize("command", sorted(PACKAGE_MODULE_CEILING))
def test_no_expensive_graph_is_loaded_before_the_list(
    command: str, listed: dict[str, tuple[list[dict], set[str]]]
) -> None:
    _frames, modules = listed[command]
    assert sorted(set(FORBIDDEN_BEFORE_THE_LIST) & modules) == []


@pytest.mark.parametrize("command", sorted(PACKAGE_MODULE_CEILING))
def test_the_package_modules_loaded_before_the_list_stay_under_the_ceiling(
    command: str, listed: dict[str, tuple[list[dict], set[str]]]
) -> None:
    _frames, modules = listed[command]
    package = sorted(name for name in modules if name.split(".")[0] == "kiro_crew")
    assert len(package) <= PACKAGE_MODULE_CEILING[command], (len(package), package)


def test_the_live_hint_readers_are_inside_cores_budget(
    listed: dict[str, tuple[list[dict], set[str]]],
) -> None:
    """Core's list reads the sub-agent ceiling, the parent's allowlists and the session's
    record. Were one of these readers not loaded here, the bounds would not be holding it."""
    _frames, modules = listed["mcp-core"]
    assert sorted(set(LIVE_HINT_READERS) - modules) == []
