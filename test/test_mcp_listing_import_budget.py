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
#: Raised by 1 for ``mcp-cron`` (355 to 356) and ``mcp-mate`` (350 to 351): the structured
#: credential detectors are on by default, so the first ``redact_credentials`` call a listing
#: makes loads ``kiro_crew.redaction_seams`` and the ``security.redaction_structured`` package
#: (the two servers sat one module under their old ceilings, and ``mcp-core`` and
#: ``mcp-guide`` had room). The redactor runs on the listing's output, so the import cannot
#: move behind the list.
#: Raised by 1 again for ``mcp-core``, ``mcp-cron``, ``mcp-guide`` and ``mcp-mate``: the app
#: manifest parser loads ``kiro_crew.apps.manifest_memory_providers``, the
#: ``memoryProviders`` section's own small module, which the manifest dataclass needs at import.
#: Raised by 1 for ``mcp-core`` (486 to 487): the line-cap split of ``kiro_crew.hooks``
#: moved ``HookManager`` into the owner ``kiro_crew.hook_runtime.hook_manager``, which the
#: facade imports with the rest of its owners; the module count rose by that one file and
#: nothing new loads.
#: Raised by 1 for each server: the rule table reads headers and encoded credentials in
#: two modules of its own (``redaction_structured.headers`` and ``.encoded``) where
#: ``.protection`` was one.
#: Raised by 1 for each server: the line-cap split of ``security.argv_floor_ssh`` moved
#: the ssh verb gate into the owner ``security.argv_floor_ssh_gate``, which the floor
#: imports with the rest of its owners; the module count rose by that one file and nothing
#: new loads.
#: Raised by 1 again for all four: the ssh-to-self floor's substitution-body check lives in
#: its own module, ``kiro_crew.security.argv_floor_ssh_bodies``, which the deny floor that
#: every listing already loads imports.
#: Raised by 1 for each server: ``edition_locks.relay_selected``, which the composition
#: root asks, reads the relay switch through ``remote_control.hosting`` (a stdlib-only
#: leaf), since an unset ``tunnel.relay_enabled`` decides by the Crew's credential; the
#: module count rose by that one file and nothing else loads. The relay client itself
#: loads only when a tunnel starts (``platform.bootstrap.LazyRelayTunnelProvider``).
#: Raised by 1 for ``mcp-core`` (491 to 492): the resource-subscription tools'
#: declarations live in their own ``kiro_crew.mcp_tools.catalog.resources``, which
#: the core listing loads from ``mcp_tools.DOMAIN_MODULES`` like every other domain.
#: Raised by 1 for ``mcp-core`` (492 to 493): a Heartbeat's active hours live in their
#: own ``kiro_crew.member_goals_hours``, which keeps ``member_goals`` under the line cap
#: and which ``member_goals`` (loaded by the listing's ``validation`` schemas) imports;
#: the module count rose by that one file and nothing else loads.
#: Raised by 3 for all four servers by the git/tar self-target floor: ``argv_floor`` imports
#: every owner when ``kiro_crew.security`` loads so a patch through the facade reaches each
#: holder of a name, and this PR adds three owner modules -- ``argv_floor_git_tar`` (the
#: floor), ``argv_floor_tar`` (the line-cap split of its tar grammar) and
#: ``argv_floor_ssh_operand`` (the line-cap split of the ssh operand reader). The security
#: package loads before the list, so the owners cannot move behind it; nothing else loads.
#: Raised by 1 for ``mcp-core`` (496 to 497) and ``mcp-mate`` (359 to 360): the self-kill
#: floor reads the Windows by-name kill programs through its own
#: ``kiro_crew.security.self_kill_windows``, which ``argv_floor_self`` imports; the module
#: count rose by that one file and nothing else loads (``mcp-cron`` and ``mcp-guide`` had room).
#: Raised by 1 for all four: the credential redactor reads Unicode's default-ignorable
#: table from the leaf ``kiro_crew.default_ignorable`` (moved there from the artifact
#: store's tag rules, so both share one pinned table); that one file is the whole rise.
PACKAGE_MODULE_CEILING = {
    "mcp-core": 498,
    "mcp-cron": 365,
    "mcp-guide": 359,
    "mcp-mate": 361,
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


def answer_then_stop(command, relayed=None):
    answer(command, relayed)
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


#: command -> the most ``kiro_crew`` modules its HANDLER module may load. The listing
#: bounds above stop at the list; these hold what loads right after it, in every chat,
#: whether or not a tool of that server is ever called. The three small servers reach the
#: gateway through ``mcp_core_parts`` and never load the core server: importing
#: ``kiro_crew.mcp_core`` for its HTTP helpers took each of them to about 970 modules.
#: Same rules as the listing ceiling: room for a platform's extras, lowered when a change
#: brings a count down.
HANDLER_MODULE_CEILING = {
    "mcp-cron": 500,
    "mcp-guide": 380,
    "mcp-mate": 380,
}

_HANDLER_PROBE = f"""
import importlib, json, sys
importlib.import_module(sys.argv[1])
sys.stderr.write({_MARK!r} + json.dumps(sorted(sys.modules)) + "\\n")
"""


@pytest.mark.parametrize("command", sorted(HANDLER_MODULE_CEILING))
def test_a_small_servers_handlers_do_not_load_the_core_server(command: str, tmp_path: Path) -> None:
    handler_module, _ = mcp_entry.SLIM_SERVERS[command]
    (tmp_path / "home").mkdir()
    done = subprocess.run(
        [sys.executable, "-c", _HANDLER_PROBE, handler_module],
        env=_child_env(tmp_path, KIROCREW_PROJECT_DIR=str(tmp_path)),
        cwd=str(tmp_path),
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
        **UTF8_TEXT,
    )
    marked = [line for line in done.stderr.splitlines() if line.startswith(_MARK)]
    assert done.returncode == 0 and marked, done.stderr[-4000:]
    modules = set(json.loads(marked[-1][len(_MARK) :]))
    assert handler_module in modules
    assert "kiro_crew.mcp_core" not in modules
    package = sorted(name for name in modules if name.split(".")[0] == "kiro_crew")
    assert len(package) <= HANDLER_MODULE_CEILING[command], (len(package), package)
