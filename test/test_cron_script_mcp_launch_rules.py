"""``ctx.call_tool`` starts a server only under the launch rules every other path keeps.

``_resolve_mcp_server`` turns a server name into what ``McpToolClient`` spawns, and
``KeptMcpServers.call_tool`` is the one door both ``ScriptContext.call_tool`` and
``kirocrew cron preview`` go through. The rules pinned here are the ones the
session, gateway and KAS paths already apply: a server the user muted is not
launched, a registry entry is not started as a local process, a tool the user
switched off is not called, and ``${VAR}`` in a server's ``env`` is resolved the way
kiro-cli resolves it. A remote (``url``) entry is called over streamable HTTP from
the script's own process with its ``headers``, filled in from the view a local
server's ``env`` is; the remote cases talk to a fake server on 127.0.0.1.

Every case writes Crew's agent spec and the two core MCP config scopes under this
test's own ``tmp_path``.
"""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.cron_script as cron_script
from kiro_crew.acp import kas_env_launch
from kiro_crew.cron_script import KeptMcpServers, McpToolError, _resolve_mcp_server

#: The header value the fake remote server accepts, filled in from ``CRON_REMOTE_TOKEN``.
TOKEN = "remote-38479-token"
#: Names a hung call; never reached by a passing run.
_HANG_SECS = 60.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Crew's agent spec and the crew-store and Kiro-global scopes, all under tmp."""
    from kiro_crew import mcp_discovery

    agents = tmp_path / "agents"
    store = tmp_path / "crew" / "mcp.json"
    kiro_global = tmp_path / "kiro" / "settings" / "mcp.json"
    for directory in (agents, store.parent, kiro_global.parent):
        directory.mkdir(parents=True)
    monkeypatch.setattr(cron_script, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        mcp_discovery, "_MCP_SOURCES", ((store, "kirocrew"), (kiro_global, "kiroGlobal"))
    )
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", None)
    # The file a chat session's switched-off tools are read from.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", kiro_global)
    _resolve_mcp_server.cache_clear()
    yield {"spec": agents / "kirocrew.json", "store": store, "kiro_global": kiro_global}
    _resolve_mcp_server.cache_clear()


def _write_servers(path, servers: dict[str, Any]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def started(monkeypatch):
    """The server names a ``McpToolClient`` was constructed for, in order."""
    names: list[str] = []

    class _Client:
        def __init__(self, server_name: str, session_key: str = "") -> None:
            names.append(server_name)

        def call_tool(self, name: str, arguments: dict) -> str:
            return f"ran {name}"

        def is_running(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(cron_script, "McpToolClient", _Client)
    return names


class TestMutedServer:
    @pytest.mark.parametrize("flag", [True, None, "false"], ids=["true", "null", "string"])
    def test_a_muted_entry_is_refused_not_launched(self, home, flag):
        _write_servers(
            home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"], "disabled": flag}}
        )

        with pytest.raises(RuntimeError, match="'pippin-mcp' is disabled"):
            _resolve_mcp_server("pippin-mcp")

    def test_a_mute_in_a_config_scope_refuses_an_unmuted_spec_entry(self, home):
        """The toggle writes the mute to a scope file; a spec the rebuild has not
        stamped still declares the server unmuted."""
        _write_servers(home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"]}})
        _write_servers(home["kiro_global"], {"pippin-mcp": {"disabled": True}})

        with pytest.raises(RuntimeError, match="'pippin-mcp' is disabled"):
            _resolve_mcp_server("pippin-mcp")

    def test_an_enabled_entry_still_resolves(self, home):
        _write_servers(
            home["spec"], {"pippin-mcp": {"command": "node", "args": ["s.js"], "disabled": False}}
        )

        assert _resolve_mcp_server("pippin-mcp") == (("node", "s.js"), {})


class TestUnlaunchableEntry:
    def test_a_registry_entry_is_refused(self, home):
        _write_servers(home["spec"], {"catalog": {"type": "registry", "command": "x"}})

        with pytest.raises(RuntimeError, match="'catalog' is governed by the admin registry"):
            _resolve_mcp_server("catalog")

    def test_an_entry_with_no_command_is_refused_by_name(self, home):
        _write_servers(home["spec"], {"half": {"args": ["s.js"]}})

        with pytest.raises(RuntimeError, match="'half' declares no command"):
            _resolve_mcp_server("half")

    def test_a_non_object_entry_is_not_found(self, home):
        _write_servers(home["spec"], {"bare": "node"})

        assert _resolve_mcp_server("bare") is None


def test_env_placeholders_resolve_as_kiro_cli_resolves_them(home, monkeypatch):
    """Both spellings resolve from the environment; a secret-named variable is never
    dereferenced, so its reference stays literal."""
    monkeypatch.setenv("CRON_T", "v")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    _write_servers(
        home["spec"],
        {
            "srv": {
                "command": "node",
                "env": {"A": "${CRON_T}", "B": "${env:CRON_T}", "C": "${AWS_SECRET_ACCESS_KEY}"},
            }
        },
    )

    assert _resolve_mcp_server("srv") == (
        ("node",),
        {"A": "v", "B": "v", "C": "${AWS_SECRET_ACCESS_KEY}"},
    )


def test_a_granted_or_cron_denied_variable_is_never_dereferenced(home, monkeypatch):
    """A granted run's launcher puts the vault value in ``os.environ`` and names it in
    ``_GRANTED_ENV_KEYS``, which ``_clean_cron_env`` keeps from the server. A
    reference to it, or to a ``_CRON_ENV_DENY`` name, stays literal, so neither value
    reaches the server under a name of the entry's choosing, while an ordinary
    variable still resolves."""
    monkeypatch.setattr(cron_script, "_GRANTED_ENV_KEYS", {"GITHUB_TOKEN"})
    monkeypatch.setenv("GITHUB_TOKEN", "vault-value")
    monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "internal-value")
    monkeypatch.setenv("CRON_T", "v")
    _write_servers(
        home["spec"],
        {
            "srv": {
                "command": "node",
                "env": {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
                    "S": "${env:KIROCREW_INTERNAL_SECRET}",
                    "A": "${CRON_T}",
                },
            }
        },
    )

    assert _resolve_mcp_server("srv") == (
        ("node",),
        {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
            "S": "${KIROCREW_INTERNAL_SECRET}",
            "A": "v",
        },
    )


class TestSwitchedOffTool:
    def test_a_tool_switched_off_in_the_spec_is_refused_before_a_server_starts(self, home, started):
        _write_servers(home["spec"], {"srv": {"command": "x", "disabledTools": ["t"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="tool 't' on MCP server 'srv' is switched off"):
            servers.call_tool("srv", "t", {})
        assert started == []

        assert servers.call_tool("srv", "u", {}) == "ran u"
        assert started == ["srv"]
        servers.close()

    def test_a_tool_switched_off_in_the_kiro_global_settings_is_refused(self, home, started):
        """The dashboard's tool-off action writes ``disabledTools`` to the Kiro global
        settings file and nowhere else."""
        _write_servers(home["spec"], {"srv": {"command": "x"}})
        _write_servers(home["kiro_global"], {"srv": {"disabledTools": ["t"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="switched off"):
            servers.call_tool("srv", "t", {})
        assert started == []
        servers.close()

    def test_a_tool_switched_off_under_the_server_s_alias_is_refused(self, home, started):
        """The tool-off action writes the key discovery shows, the alias of the spec's key."""
        _write_servers(home["spec"], {"npm:@playwright/mcp": {"command": "x"}})
        _write_servers(home["kiro_global"], {"playwright-mcp": {"disabledTools": ["nav"]}})
        servers = KeptMcpServers()

        with pytest.raises(McpToolError, match="switched off"):
            servers.call_tool("npm:@playwright/mcp", "nav", {})
        assert started == []
        servers.close()

    @pytest.mark.parametrize(
        ("entry", "tool", "refused"),
        [
            ("delete_*", "delete_repo", True),
            ("get_?", "get_x", True),
            ("delete_*", "list_repos", False),
            ("{a,b}_*", "list_repos", True),
        ],
        ids=["star", "question-mark", "glob-misses", "unread-glob-refuses"],
    )
    def test_a_glob_entry_is_matched_as_kiro_cli_matches_it(
        self, home, started, entry, tool, refused
    ):
        _write_servers(home["spec"], {"srv": {"command": "x", "disabledTools": [entry]}})
        servers = KeptMcpServers()

        if refused:
            with pytest.raises(McpToolError, match="switched off"):
                servers.call_tool("srv", tool, {})
            assert started == []
        else:
            assert servers.call_tool("srv", tool, {}) == f"ran {tool}"
        servers.close()


class _FakeRemote(BaseHTTPRequestHandler):
    """A streamable-HTTP MCP server with a tool ``echo``; 401 without the token.

    ``hold`` answers ``tools/call`` on an event stream and keeps that stream open
    until ``release`` is set; ``accept_only`` answers it 202 with no response.
    """

    seen: list[dict]
    hold = False
    accept_only = False
    release: threading.Event

    def log_message(self, *_a):  # quiet
        pass

    def _reply(self, status: int, body: bytes = b"", **hdrs: str) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append({"headers": dict(self.headers), "method": body.get("method")})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, b'{"error":"no token"}')
            return
        if "id" not in body or (self.accept_only and body["method"] == "tools/call"):
            self._reply(202)
            return
        if body["method"] == "initialize":
            result: dict = {"protocolVersion": "2025-06-18", "capabilities": {}}
        else:
            text = f"echo {json.dumps(body['params']['arguments'], sort_keys=True)}"
            result = {"content": [{"type": "text", "text": text}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        if self.hold and body["method"] == "tools/call":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: message\ndata: " + payload + b"\n\n")
            self.wfile.flush()
            self.release.wait(_HANG_SECS)
            return
        self._reply(200, payload, **{"Mcp-Session-Id": "sess-1"})

    def do_DELETE(self):  # noqa: N802
        self.seen.append({"headers": dict(self.headers), "method": "DELETE"})
        self._reply(200)


class _LoopbackServer(ThreadingHTTPServer):
    """Reached by port, never by name: skip the host lookup ``server_bind`` does."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


@pytest.fixture
def remote(monkeypatch):
    """The fake server on a loopback port 0, and the token's variable set; no
    process may be started while a remote server is called."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("CRON_REMOTE_TOKEN", TOKEN)

    def _no_process(*_a, **_k):
        pytest.fail("a remote server was started as a process")

    monkeypatch.setattr(cron_script, "popen_limited", _no_process)
    release = threading.Event()
    handler = type("Handler", (_FakeRemote,), {"seen": [], "release": release})
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(_HANG_SECS)


def _remote_entry(url: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "headers": {"Authorization": "Bearer ${CRON_REMOTE_TOKEN}"}, **extra}


class TestRemoteEntry:
    @pytest.mark.parametrize(
        "extra",
        [{}, {"command": "placeholder-never-run"}],
        ids=["url-only", "url-with-command"],
    )
    def test_a_remote_entry_is_called_with_its_filled_in_headers(self, home, remote, extra):
        """The url is called from this process, never a command beside it."""
        handler, url = remote
        _write_servers(home["spec"], {"remote": _remote_entry(url, **extra)})
        servers = KeptMcpServers()

        assert servers.call_tool("remote", "echo", {"q": "hi"}) == 'echo {"q": "hi"}'
        servers.close()
        methods = [r["method"] for r in handler.seen]
        assert methods[:3] == ["initialize", "notifications/initialized", "tools/call"]
        posts = [r for r in handler.seen if r["method"] != "DELETE"]
        assert all(r["headers"]["Authorization"] == f"Bearer {TOKEN}" for r in posts)

    def test_a_call_returns_when_answered_on_a_stream_the_server_keeps_open(self, home, remote):
        handler, url = remote
        handler.hold = True
        _write_servers(home["spec"], {"remote": _remote_entry(url)})
        servers = KeptMcpServers()
        results: list[str] = []
        call = threading.Thread(
            target=lambda: results.append(servers.call_tool("remote", "echo", {"q": 1})),
            daemon=True,
        )

        call.start()
        call.join(_HANG_SECS)

        assert not call.is_alive(), "the call waited for the event stream to end"
        assert results == ['echo {"q": 1}']
        servers.close()

    def test_a_request_that_ends_without_an_answer_fails_at_once(self, home, remote):
        handler, url = remote
        handler.accept_only = True
        _write_servers(home["spec"], {"remote": _remote_entry(url)})
        servers = KeptMcpServers()

        with pytest.raises(RuntimeError, match="'remote' ended its answer to 'tools/call'"):
            servers.call_tool("remote", "echo", {})
        servers.close()

    def test_a_401_is_a_transport_failure_and_the_next_call_starts_fresh(
        self, home, remote, monkeypatch
    ):
        handler, url = remote
        monkeypatch.setenv("CRON_REMOTE_TOKEN", "wrong-38479-value")
        _write_servers(home["spec"], {"remote": _remote_entry(url)})
        servers = KeptMcpServers()

        for _ in range(2):
            with pytest.raises(RuntimeError) as raised:
                servers.call_tool("remote", "echo", {})
            assert not isinstance(raised.value, McpToolError)
            assert "'remote'" in str(raised.value) and "HTTP 401" in str(raised.value)
            assert "wrong-38479-value" not in str(raised.value)
        assert [r["method"] for r in handler.seen] == ["initialize", "initialize"]
        servers.close()

    @pytest.mark.parametrize(
        ("entry", "plain_http_refused", "reason"),
        [
            ({"url": "mcp.example.invalid/mcp"}, False, "not http or https"),
            ({"type": "sse"}, False, "is an 'sse' server"),
            ({"headers": {"X-Api-Key": "secret://MY_TOKEN"}}, False, "'X-Api-Key'"),
            ({"url": "http://mcp.example.invalid/mcp"}, True, "plain http"),
        ],
        ids=["no-http-scheme", "sse", "secret-header", "plain-http-headers-opted-out"],
    )
    def test_an_uncallable_remote_entry_is_refused_by_name_before_any_request(
        self, home, remote, monkeypatch, entry, plain_http_refused, reason
    ):
        handler, url = remote
        monkeypatch.setattr(kas_env_launch, "refuses_plain_http_headers", lambda: False)
        _write_servers(home["spec"], {"remote": {**_remote_entry(url), **entry}})
        if plain_http_refused:
            # With the opt-in off the url is called, as KAS's bridge calls it.
            assert _resolve_mcp_server("remote").url == entry["url"]
            _resolve_mcp_server.cache_clear()
            monkeypatch.setattr(kas_env_launch, "refuses_plain_http_headers", lambda: True)

        with pytest.raises(RuntimeError, match=f"'remote' .*{reason}") as raised:
            KeptMcpServers().call_tool("remote", "echo", {})
        assert "MY_TOKEN" not in str(raised.value)
        assert handler.seen == []

    @pytest.mark.parametrize(
        ("variable", "value", "header"),
        [
            ("GITHUB_TOKEN", "vault-38479-value", "${GITHUB_TOKEN}"),
            ("KIROCREW_INTERNAL_SECRET", "internal-38479-value", "${KIROCREW_INTERNAL_SECRET}"),
        ],
        ids=["granted", "cron-denied"],
    )
    def test_a_granted_or_cron_denied_variable_reaches_the_server_as_written(
        self, home, remote, monkeypatch, variable, value, header
    ):
        handler, url = remote
        monkeypatch.setattr(cron_script, "_GRANTED_ENV_KEYS", {"GITHUB_TOKEN"})
        monkeypatch.setenv(variable, value)
        entry = _remote_entry(url)
        entry["headers"]["X-Passed"] = header
        _write_servers(home["spec"], {"remote": entry})
        servers = KeptMcpServers()

        assert servers.call_tool("remote", "echo", {}) == "echo {}"
        servers.close()
        assert {r["headers"]["X-Passed"] for r in handler.seen} == {header}
