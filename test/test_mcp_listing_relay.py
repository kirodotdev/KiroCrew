"""A server relays the ``initialize`` and ``tools/list`` its gateway built, or builds its own.

``kiro_crew.mcp_listing_relay`` is the server side: one request before the server loads
its configuration or the platform. ``kiro_crew.mcp_listing_gateway`` is the gateway side:
the same two answers the server would have built, for the session that asked, on the
route it reads its tool policy from.

What is held here: the bytes a client reads are the same whichever of the two built them,
for all four servers and with a policy that withholds tools; a server that gets no list
has loaded no configuration loader, platform or audit module by then and answers what it
always answered; the gateway builds off the event loop, under the asking session, keeps
nothing, and answers no list to a caller it did not authenticate as that session.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import http.server
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import types
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from mcp_shared_helpers import _framing_undetected  # noqa: F401 -- autouse fixture
from test_mcp_listing_import_budget import _REQUESTS, _LoopbackServer
from test_mcp_slim_entry import _child_env

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import mcp_caller, mcp_entry, mcp_listing_gateway
from kiro_crew import mcp_listing_relay as relay
from kiro_crew import mcp_shared_listing as listing
from kiro_crew import resource_status, sandbox_tiers
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import loader as config_loader
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, wait_until

COMMANDS = sorted(mcp_entry.EARLY_ANSWERS)

# ── the names and bounds the two sides share ────────────────────────────────


def test_the_repeated_names_are_the_ones_their_owners_define() -> None:
    assert relay.SESSION_TOKEN_ENV == STUB_SESSION_TOKEN_ENV
    assert relay.CALLER_META_KEY == mcp_caller.CALLER_META_KEY
    assert relay.RELAY_FRAME_WAIT_SECS == listing.EARLY_FRAME_WAIT_SECS


@pytest.mark.parametrize("name", ["KIROCREW_PORT", "KIROCREW_BOUND_PORT"])
def test_the_relay_dials_the_port_the_policy_read_dials(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIROCREW_PORT", raising=False)
    monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
    assert relay._env_port() is None
    monkeypatch.setenv(name, "45123")
    assert relay._env_port() == resolve_client_port_src(None)[0] == 45123


def test_a_stamp_names_the_files_and_folders_and_changes_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert relay.code_stamp() and relay.code_stamp() == relay.code_stamp()
    assert relay.place_stamp() and relay.place_stamp() == relay.place_stamp()
    monkeypatch.setattr("kiro_crew.config.paths._agents_dir_override", lambda: tmp_path / "gone")
    absent = relay.place_stamp()
    monkeypatch.setattr("kiro_crew.config.paths._agents_dir_override", lambda: tmp_path)
    present = relay.place_stamp()
    (tmp_path / "other").mkdir()
    monkeypatch.setattr("kiro_crew.config.paths._agents_dir_override", lambda: tmp_path / "other")
    assert len({"", absent, present, relay.place_stamp()}) == 4


_PACKAGE = Path(relay.__file__).resolve().parent


def _module_file(dotted: str) -> str | None:
    """The file of the package module *dotted* names, relative to the package."""
    head, _, rest = dotted.partition(".")
    if head != "kiro_crew":
        return None
    base = _PACKAGE.joinpath(*rest.split(".")) if rest else _PACKAGE
    for found in (base.with_suffix(".py"), base / "__init__.py"):
        if found.is_file():
            return found.relative_to(_PACKAGE).as_posix()
    return None


def _imported_by(builder: str) -> set[str]:
    """Every package module the module at *builder* imports by name, at any depth of
    its own source: at the top, inside a function, under ``TYPE_CHECKING``."""
    tree = ast.parse((_PACKAGE / builder).read_text(encoding="utf-8"))
    here = ["kiro_crew", *builder.split("/")[:-1]]
    named: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            above = here[: len(here) - (node.level - 1)] if node.level else []
            module = ".".join([*above, *([node.module] if node.module else [])])
            named += [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return {found for found in map(_module_file, named) if found}


def test_the_code_stamp_reaches_every_module_a_list_builder_imports() -> None:
    """A description's text, names and limits live in what the builders import. The
    expected set is read off their import statements, so a builder that starts to
    import a module fails here until the stamp covers it, and a row nothing imports
    any more fails too."""
    builders = set(relay.listing_builders(str(_PACKAGE)))
    assert {"mcp_instructions.py", "mcp_cron_tool_list.py"} <= builders
    imported = set().union(*map(_imported_by, sorted(builders)))
    assert "cron_service/model.py" in imported  # the cron list's limits
    expected = imported - builders - set(relay._STAMPED_BESIDES)
    listed = set(relay._LISTING_IMPORTS)
    assert listed == expected, (
        f"add to mcp_listing_relay._LISTING_IMPORTS: {sorted(expected - listed)}; "
        f"remove from it: {sorted(listed - expected)}"
    )
    assert len(relay._LISTING_IMPORTS) == len(listed), "a row is listed twice"


def test_the_code_stamp_reads_every_stamped_file(monkeypatch: pytest.MonkeyPatch) -> None:
    read: set[str] = set()
    real = os.stat

    def noting(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        read.add(Path(path).as_posix())
        return real(path, *args, **kwargs)

    monkeypatch.setattr(relay.os, "stat", noting)
    assert relay.code_stamp()
    stamped = {
        *relay.listing_builders(str(_PACKAGE)),
        *relay._LISTING_IMPORTS,
        *relay._STAMPED_BESIDES,
    }
    assert {(_PACKAGE / name).as_posix() for name in stamped} <= read


def test_a_stamped_file_that_is_gone_is_no_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stamp means the server never asks, so a renamed source turns the relay off
    instead of vouching for code it did not read."""
    monkeypatch.setattr(relay, "_LISTING_IMPORTS", (*relay._LISTING_IMPORTS, "no_such_module.py"))
    assert relay.code_stamp() == ""


# ── the server's one request ────────────────────────────────────────────────

_LISTING = {
    "initialize": {"serverInfo": {"name": "kirocrew-test", "version": "1"}},
    "tools": [{"name": "alpha"}, {"name": "beta"}],
}


class _Gateway:
    """A loopback listener that answers every request with one prepared reply."""

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {relay.LISTING_KEY: _LISTING}
        self.requests: list[tuple[str, dict[str, str]]] = []
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - the handler protocol's name
                gateway.requests.append((self.path, dict(self.headers.items())))
                raw = json.dumps(gateway.body).encode("utf-8")
                self.send_response(gateway.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = _LoopbackServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=FALLBACK_TIMEOUT_SECS)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Gateway]:
    """A listener, and an environment that names it and a session of its own."""
    made = _Gateway()
    monkeypatch.setenv("KIROCREW_PORT", str(made.port))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:mine")
    monkeypatch.setenv(relay.SESSION_TOKEN_ENV, "token-of-mine")
    monkeypatch.delenv("KIROCREW_IPC_PICKUP", raising=False)
    monkeypatch.setattr(config_loader, "read_local_secret", lambda _port, dial_host=None: "s3cret")
    yield made
    made.close()


def test_one_request_carries_the_policy_reads_identity_and_both_stamps(gateway: _Gateway) -> None:
    got, why = relay.fetch_listing("mcp-core", timeout=FALLBACK_TIMEOUT_SECS)
    assert why == "" and got == relay.Listing(_LISTING["initialize"], _LISTING["tools"])
    ((path, headers),) = gateway.requests
    assert path.startswith("/api/session-tool-policy?")
    assert f"{relay.LISTING_PARAM}=mcp-core" in path
    assert f"{relay.CODE_PARAM}={relay.code_stamp()}" in path
    assert f"{relay.PLACE_PARAM}={relay.place_stamp()}" in path
    sent = {name.lower(): value for name, value in headers.items()}
    assert sent["x-internal-secret"] == "s3cret"
    assert sent["x-session-key"] == "dashboard:mine"
    assert sent["x-session-token"] == "token-of-mine"


@pytest.mark.parametrize(
    ("status", "body", "why"),
    [
        (200, {relay.LISTING_KEY: {relay.LOCAL_KEY: "a platform overlay"}}, "gateway: a platform"),
        (200, {"exclude": []}, "no listing"),
        (200, {relay.LISTING_KEY: {"initialize": {}, "tools": "all"}}, "malformed"),
        (200, ["not", "an", "object"], "no listing"),
        (409, {"code": "member_identity_unavailable"}, "409"),
        (404, {"error": "agent not resolved"}, "404"),
        (500, {}, "500"),
    ],
)
def test_any_answer_but_a_list_is_a_named_fallback(
    status: int, body: Any, why: str, gateway: _Gateway
) -> None:
    gateway.status, gateway.body = status, body
    got, reason = relay.fetch_listing("mcp-core", timeout=FALLBACK_TIMEOUT_SECS)
    assert got is None and why in reason


@pytest.mark.parametrize(
    "raised", [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))]
)
def test_a_gateway_that_does_not_answer_in_time_is_the_timed_out_fallback(
    raised: Exception, gateway: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wait that ran out while reading, and one that ran out while connecting."""

    def never_answers(_request: Any, timeout: float) -> Any:
        assert timeout == relay.RELAY_FETCH_TIMEOUT_SECS
        raise raised

    monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", never_answers)
    got, reason = relay.fetch_listing("mcp-core")
    assert got is None and reason == relay._TIMED_OUT


@pytest.mark.parametrize(
    ("unset", "set_to", "why"),
    [
        ("KIROCREW_SESSION_KEY", None, "session key"),
        (relay.SESSION_TOKEN_ENV, None, "session key"),
        ("KIROCREW_PORT", None, "port"),
        (None, ("KIROCREW_IPC_PICKUP", "pickup"), "pickup"),
    ],
)
def test_a_process_without_the_whole_identity_never_asks(
    unset: str | None,
    set_to: tuple[str, str] | None,
    why: str,
    gateway: _Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pooled backend has no session of its own; an enforced harness's server holds a
    pickup in place of the credential. Neither sends a request."""
    if unset:
        monkeypatch.delenv(unset)
    if set_to:
        monkeypatch.setenv(*set_to)
    got, reason = relay.fetch_listing("mcp-core")
    assert got is None and why in reason
    assert gateway.requests == []


def test_no_credential_no_request(gateway: _Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_loader, "read_local_secret", lambda _port, dial_host=None: "")
    assert relay.fetch_listing("mcp-core") == (None, "no gateway credential")
    assert gateway.requests == []


# ── the frames the relay answers and the ones it leaves ─────────────────────

_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
_CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "alpha"}}


class _Client:
    """A pipe-backed stdin, every answer written to it, and a fresh relay attempt."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read_fd, self._write_fd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(read_fd, "rb"))
        self.answers: list[tuple[Any, Any]] = []
        self.fetches = 0
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(mcp_shared, "snapshot_stdout_fd", lambda: None)
        monkeypatch.setattr(mcp_shared, "_EARLY", listing._EarlyState())
        monkeypatch.setattr(relay, "_OUTCOME", relay._Outcome())
        monkeypatch.setattr(relay, "fetch_listing", self._fetch)
        self.listing: relay.Listing | None = relay.Listing(
            _LISTING["initialize"], _LISTING["tools"]
        )

    def _fetch(self, _command: str) -> tuple[relay.Listing | None, str]:
        self.fetches += 1
        return (self.listing, "") if self.listing else (None, "the gateway answered 500")

    def _record(self, req_id: Any, result: Any, error: Any = None) -> bool:
        self.answers.append((req_id, result))
        return True

    def send(self, *frames: dict[str, Any]) -> None:
        for frame in frames:
            os.write(self._write_fd, (json.dumps(frame) + "\n").encode("utf-8"))

    def close(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1
        self._stdin.close()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Client]:
    made = _Client(monkeypatch)
    yield made
    made.close()


def test_the_relay_answers_both_requests_with_the_gateways_answers(client: _Client) -> None:
    client.send(_INITIALIZE, _INITIALIZED, _LIST, _CALL)
    frames = relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS)
    assert client.answers == [(1, _LISTING["initialize"]), (2, {"tools": _LISTING["tools"]})]
    assert frames is not None and frames.listed
    assert frames.init_params == _INITIALIZE["params"]
    assert list(frames.pending) == []  # the call is still in the pipe, for the loop


def test_a_frame_the_relay_does_not_serve_is_left_unanswered_and_in_order(
    client: _Client,
) -> None:
    client.send(_INITIALIZE, _CALL, _LIST)
    frames = relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS)
    assert client.answers == [(1, _LISTING["initialize"])]
    assert frames is not None and not frames.listed
    assert list(frames.pending) == [_CALL]


def test_a_list_request_that_names_its_caller_is_left_for_the_loop(client: _Client) -> None:
    """The gateway stamps a caller on a frame for a backend several sessions share, and
    the list in hand is this process's own."""
    stamped = {
        **_LIST,
        "params": {"_meta": {mcp_caller.CALLER_META_KEY: {"sessionKey": "dashboard:other"}}},
    }
    client.send(_INITIALIZE, stamped)
    frames = relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS)
    assert [req_id for req_id, _ in client.answers] == [1]
    assert frames is not None and list(frames.pending) == [stamped]


def test_no_list_from_the_gateway_reads_nothing(client: _Client) -> None:
    client.listing = None
    client.send(_INITIALIZE, _LIST)
    assert relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS) is None
    assert client.answers == []
    assert relay._OUTCOME.fallback == "the gateway answered 500"


def test_a_second_attempt_returns_the_first_ones_frames_and_asks_nobody(
    client: _Client,
) -> None:
    client.send(_INITIALIZE, _LIST)
    first = relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS)
    assert relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS) is first
    assert client.fetches == 1 and len(client.answers) == 2


def test_the_servers_own_early_stage_hands_the_relayed_frames_to_the_loop(
    client: _Client,
) -> None:
    client.send(_INITIALIZE, _CALL)
    frames = relay.relay_before_boot("mcp-core", wait_secs=FALLBACK_TIMEOUT_SECS)
    steps: list[str] = []
    server = listing.StdioServer(
        name="kirocrew-test",
        version="1",
        list_tools=lambda: steps.append("listed") or [],
        before_first_request=lambda: steps.append("step"),
    )
    client.send(_LIST)
    listing.serve_before_handlers(server, wait_secs=FALLBACK_TIMEOUT_SECS, relayed=frames)
    assert steps == ["step"]  # the owed step ran; nothing more was read or built
    taken = listing.take_early_frames()
    assert taken is not None and taken.init_params == _INITIALIZE["params"]
    assert list(taken.pending) == [_CALL]


@pytest.mark.parametrize(
    ("fallback", "level"),
    [(relay._TIMED_OUT, "WARNING"), ("gateway: a platform overlay", "INFO"), ("", "DEBUG")],
)
def test_each_attempt_is_logged_once_and_a_timeout_as_a_warning(
    fallback: str,
    level: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = relay._Outcome()
    outcome.made, outcome.command, outcome.fallback = True, "mcp-core", fallback
    monkeypatch.setattr(relay, "_OUTCOME", outcome)
    with caplog.at_level("DEBUG", logger=relay.logger.name):
        relay.report_outcome()
        relay.report_outcome()
    (record,) = caplog.records
    assert record.levelname == level and "mcp-core" in record.getMessage()


@pytest.mark.parametrize(("command", "detached"), [("mcp-core", True), ("mcp-panel", False)])
def test_the_entry_does_not_ask_for_a_detached_server_or_one_with_no_early_answers(
    command: str, detached: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose stderr is the gateway log is handed to the full CLI unanswered,
    and a server that declares no early answers has none to relay."""
    monkeypatch.setattr(mcp_entry, "_stderr_is", lambda _path: detached)
    monkeypatch.setattr(relay, "relay_before_boot", lambda _command: pytest.fail("it asked"))
    assert mcp_entry._relay_before_boot(command) is None


# ── the gateway's builder ───────────────────────────────────────────────────


@pytest.fixture
def packaged_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway on an install the sandbox seals, that has read its code stamp.

    That is the one gateway that builds a list, and these tests run from a source
    checkout, which the sandbox leaves writable. Every test that has the builder build
    asks for this; the tests about the other installs, and about a gateway with no
    stamp yet, then set their own.
    """
    monkeypatch.setattr(
        mcp_listing_gateway, "install_root_targets_for", lambda package_dir: [package_dir]
    )
    monkeypatch.setattr(mcp_listing_gateway, "_CODE_AT_START", relay.code_stamp())


class _Declared:
    """One declared server whose list function records how it was called."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Any, bool, int]] = []
        self.fail = False
        server = listing.StdioServer(
            name="kirocrew-test", version="1.2.3", list_tools=self._list, instructions="read me"
        )
        monkeypatch.setitem(sys.modules, "relay_test_server", types.SimpleNamespace(SERVER=server))
        monkeypatch.setattr(
            mcp_listing_gateway, "EARLY_ANSWERS", {"mcp-test": ("relay_test_server", "SERVER")}
        )
        self.server = server

    def _list(self) -> list[dict[str, Any]]:
        self.calls.append(
            (mcp_caller.current_caller(), relay.built_for_a_tool_server(), threading.get_ident())
        )
        if self.fail:
            raise RuntimeError("no list today")
        return [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]

    def body(self, policy: Any = None, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "code": mcp_listing_gateway._CODE_AT_START,
            "place": relay.place_stamp(),
            "private_member": False,
        }
        values.update(overrides)
        raw = mcp_listing_gateway.session_listing_body(
            policy, "mcp-test", "dashboard:mine", "kirocrew", **values
        )
        return json.loads(raw)


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch, packaged_install: None) -> _Declared:
    return _Declared(monkeypatch)


def test_the_gateway_builds_under_the_asking_session_as_a_tool_server(
    declared: _Declared,
) -> None:
    before = mcp_caller.current_caller()
    body = declared.body()
    ((caller, as_tool_server, _thread),) = declared.calls
    assert caller.session_key == "dashboard:mine" and caller.from_gateway
    assert as_tool_server is True
    assert mcp_caller.current_caller() is before and not relay.built_for_a_tool_server()
    assert body[relay.LISTING_KEY] == {
        "initialize": listing.initialize_result("kirocrew-test", "1.2.3", instructions="read me"),
        "tools": [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}],
    }


def test_the_policy_is_repeated_and_applied_by_the_servers_own_filter(
    declared: _Declared,
) -> None:
    policy = {"exclude": ["beta"], "include": {"kirocrew-test": ["alpha", "beta"]}}
    body = declared.body(policy)
    assert {key: body[key] for key in policy} == policy
    assert body[relay.LISTING_KEY]["tools"] == [{"name": "alpha"}]


def test_nothing_is_kept_between_requests(declared: _Declared) -> None:
    declared.body()
    declared.body()
    assert len(declared.calls) == 2


def test_a_build_that_fails_leaves_the_policy_answer_and_no_caller_behind(
    declared: _Declared,
) -> None:
    declared.fail = True
    before = mcp_caller.current_caller()
    body = declared.body({"exclude": ["beta"]})
    assert body == {
        "exclude": ["beta"],
        relay.LISTING_KEY: {relay.LOCAL_KEY: "the list could not be built here"},
    }
    assert mcp_caller.current_caller() is before and not relay.built_for_a_tool_server()


@pytest.mark.parametrize(
    "policy", [None, {"exclude": []}, {"exclude": ["a_tool_of_another_server"]}]
)
def test_an_unregistered_session_gets_the_unfiltered_list_and_no_policy(
    policy: Any, declared: _Declared
) -> None:
    """Its own policy read answers "agent not resolved" in that window, so a server that
    builds its list then lists everything: the relayed list is that list or nothing."""
    body = declared.body(policy, unregistered=True)
    assert set(body) == {relay.LISTING_KEY}
    assert body[relay.LISTING_KEY]["tools"] == declared.server.list_tools()


@pytest.mark.parametrize(
    "policy",
    [{"exclude": ["beta"]}, {"include": {"kirocrew-test": ["alpha"]}}, {"exclude": "all"}],
)
def test_an_unregistered_session_whose_policy_withholds_a_tool_gets_no_list(
    policy: Any, declared: _Declared
) -> None:
    body = declared.body(policy, unregistered=True)
    assert set(body) == {relay.LISTING_KEY}  # the policy itself is not answered either
    assert set(body[relay.LISTING_KEY]) == {relay.LOCAL_KEY}


def _context(**fields: Any) -> Any:
    return lambda: types.SimpleNamespace(**{"profile": "standalone", "governance": None, **fields})


@pytest.mark.parametrize(
    ("why", "overrides", "patch", "policy"),
    [
        ("other code", {"code": "0" * 32}, None, None),
        ("other code", {"code": ""}, None, None),
        ("another data home", {"place": "0" * 32}, None, None),
        ("private member", {"private_member": True}, None, None),
        ("platform overlay", {}, ("current_context", _context(profile="enterprise")), None),
        ("governance policy", {}, ("current_context", _context(governance=object())), None),
        ("governance profile", {}, ("resolve_active_scope", lambda *_a, **_k: object()), None),
        ("read itself", {}, None, {"exclude": "everything"}),
        ("read itself", {}, None, {"exclude": [], "include": {"kirocrew-test": "alpha"}}),
    ],
)
def test_where_the_list_is_not_shown_to_be_the_servers_the_gateway_builds_none(
    why: str,
    overrides: dict[str, Any],
    patch: tuple[str, Any] | None,
    policy: Any,
    declared: _Declared,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if patch:
        monkeypatch.setattr(mcp_listing_gateway, *patch)
    listed = declared.body(policy, **overrides)[relay.LISTING_KEY]
    assert set(listed) == {relay.LOCAL_KEY} and why in listed[relay.LOCAL_KEY]
    assert declared.calls == []


def test_a_server_the_gateway_does_not_know_gets_no_list(declared: _Declared) -> None:
    raw = mcp_listing_gateway.session_listing_body(
        None,
        "mcp-core",  # not in this test's table
        "dashboard:mine",
        "kirocrew",
        code=mcp_listing_gateway._CODE_AT_START,
        place=relay.place_stamp(),
        private_member=False,
    )
    assert json.loads(raw) == {relay.LISTING_KEY: {relay.LOCAL_KEY: "unknown server"}}


# ── which install is answered a list ────────────────────────────────────────

_NOT_SEALED = "an install whose package directory the sandbox does not seal"
#: The builder's own two bindings, read before any test replaces them.
_BUILDER_CLASSIFIES_WITH = mcp_listing_gateway.install_root_targets_for
_BUILDER_PACKAGE_DIR = mcp_listing_gateway._gateway_package_dir


def _raises(_package_dir: str) -> list[str]:
    raise OSError("the classification cannot be had")


@pytest.mark.parametrize(
    ("install", "package_dir", "classify", "sandbox", "why_not"),
    [
        # The sandbox's own classification, on the path a wheel installs to.
        ("packaged", "/opt/venv/lib/python3.12/site-packages/kiro_crew", None, "auto", ""),
        ("packaged, distro layout", "/opt/py/dist-packages/kiro_crew", None, "auto", ""),
        # The developer's working copy: the sandbox leaves it writable.
        ("source checkout", "/srv/checkout/src/kiro_crew", None, "auto", _NOT_SEALED),
        ("editable install", "/srv/venv/src/kiro-crew/src/kiro_crew", None, "auto", _NOT_SEALED),
        # The classification does not read the sandbox setting: with the sandbox off
        # an agent is not confined by it, and the install kind still decides.
        ("packaged, sandbox off", "/opt/venv/lib/site-packages/kiro_crew", None, "off", ""),
        ("source, sandbox off", "/srv/checkout/src/kiro_crew", None, "off", _NOT_SEALED),
        # Whatever the sandbox says it seals is what is answered, not the path.
        ("a source tree the sandbox seals", "/srv/checkout/src/kiro_crew", list, "auto", ""),
        (
            "unknown",
            "/opt/venv/lib/site-packages/kiro_crew",
            lambda _dir: None,
            "auto",
            _NOT_SEALED,
        ),
        ("the check raises", "/opt/venv/lib/site-packages/kiro_crew", _raises, "auto", _NOT_SEALED),
    ],
)
def test_a_list_is_built_only_where_the_sandbox_seals_the_package_tree(
    install: str,
    package_dir: str,
    classify: Any,
    sandbox: str,
    why_not: str,
    declared: _Declared,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building a list imports listing modules into the gateway, outside the sandbox,
    on a request a session can cause. Where a session's agent can write the package
    tree, no list is built and no listing code runs: the server builds its own."""
    sealed = (lambda _dir: [package_dir]) if classify is list else classify
    monkeypatch.setattr(
        mcp_listing_gateway,
        "install_root_targets_for",
        sealed or sandbox_tiers.install_root_targets_for,
    )
    monkeypatch.setattr(mcp_listing_gateway, "_gateway_package_dir", lambda: package_dir)
    monkeypatch.setattr(sandbox_tiers, "configured_sandbox_mode", lambda: sandbox)
    listed = declared.body()[relay.LISTING_KEY]
    if why_not:
        assert listed == {relay.LOCAL_KEY: why_not}, install
        assert declared.calls == [], "listing code ran for an install that is not sealed"
    else:
        assert [tool["name"] for tool in listed["tools"]] == ["alpha", "beta", "gamma"], install


def test_the_gateway_reads_the_sandboxes_own_classification() -> None:
    """One source of truth: the builder's names are the sandbox's, not a copy of its rule."""
    assert _BUILDER_CLASSIFIES_WITH is sandbox_tiers.install_root_targets_for
    assert _BUILDER_PACKAGE_DIR is sandbox_tiers._gateway_package_dir


def test_no_list_is_built_before_the_gateway_has_read_its_code_stamp(
    declared: _Declared, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_listing_gateway, "_CODE_AT_START", "")
    listed = declared.body(code=relay.code_stamp())[relay.LISTING_KEY]
    assert listed == {relay.LOCAL_KEY: "this gateway has not read its own code yet"}
    assert declared.calls == []


def test_importing_the_route_module_reads_no_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route module is imported as the gateway starts, before its socket accepts a
    request, and it imports the builder at module scope. Loading the builder reads no
    stamp: that is the startup hook's, off the loop. Shown on a private copy of the
    builder's source, so the module every other test shares is never executed again."""
    route_module = importlib.import_module(
        "kiro_crew.dashboard.handlers.sessions_parts.tool_policy"
    )
    assert route_module.session_listing_body.__module__ == mcp_listing_gateway.__name__
    monkeypatch.setattr(relay, "code_stamp", lambda: pytest.fail("a stamp was read at import"))
    spec = importlib.util.spec_from_file_location(
        "relay_test_private_builder", mcp_listing_gateway.__file__
    )
    assert spec is not None and spec.loader is not None
    private = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, private)
    spec.loader.exec_module(private)
    assert private._CODE_AT_START == "" and not private._STAMP_TASKS


def test_both_entrypoints_read_the_stamp_at_startup_and_not_before() -> None:
    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    assert mcp_listing_gateway.startup_stamp in list(app.on_startup)


@pytest.mark.asyncio
async def test_the_startup_hook_returns_at_once_and_the_stamp_lands_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_listing_gateway, "_CODE_AT_START", "")
    reading, release = threading.Event(), threading.Event()
    threads: list[int] = []

    def held_stamp() -> str:
        threads.append(threading.get_ident())
        reading.set()
        assert release.wait(timeout=FALLBACK_TIMEOUT_SECS), "the stamp was never released"
        return "c0de"

    monkeypatch.setattr(relay, "code_stamp", held_stamp)
    await mcp_listing_gateway.startup_stamp()
    (task,) = mcp_listing_gateway._STAMP_TASKS
    await async_wait_until(reading.is_set, describe=lambda: "the stamp read never started")
    assert mcp_listing_gateway._CODE_AT_START == "", "the hook waited for the read"
    release.set()
    await asyncio.wait_for(task, timeout=FALLBACK_TIMEOUT_SECS)
    assert mcp_listing_gateway._CODE_AT_START == "c0de"
    assert threads and threads != [threading.get_ident()]
    assert not mcp_listing_gateway._STAMP_TASKS


# ── kirocrew-core's live descriptions: the gateway's list is the server's ───

_SESSION = "dashboard:relay-matrix"


def _spec(name: str, **extra: Any) -> None:
    folder = kiro_agents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"name": name, **extra}), encoding="utf-8")


def _wire(tools: list[dict[str, Any]]) -> str:
    """*tools* as a client reads them: through JSON, in the order they were built."""
    return json.dumps({"tools": tools}, ensure_ascii=False)


@pytest.mark.parametrize("backend", ["kas", "kiro"])
@pytest.mark.parametrize("tool_search", [True, False])
@pytest.mark.parametrize("ceiling", [7, 0])
@pytest.mark.parametrize("restricted", [True, False])
def test_cores_gateway_built_list_is_its_server_built_list(
    backend: str,
    tool_search: bool,
    ceiling: int,
    restricted: bool,
    monkeypatch: pytest.MonkeyPatch,
    packaged_install: None,
) -> None:
    """Across the backend, Tool Search, a pinned and an automatic sub-agent ceiling, and a
    parent with and without ``availableAgents``: the same bytes from both builders, while
    the gateway holds a cap in force that a tool server cannot read."""
    cfg = KiroCrewConfig.load()
    cfg = dataclasses.replace(
        cfg,
        agent=dataclasses.replace(
            cfg.agent, acp_backend=backend, tool_search=tool_search, max_subagents=ceiling
        ),
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    for name in ("probe", "scout"):
        _spec(name)
    _spec("parent", toolsSettings={"subagent": {"availableAgents": ["scout"]}})
    monkeypatch.setattr(
        spawn_tools,
        "read_session_execution",
        lambda key, **_k: (
            types.SimpleNamespace(template_id="parent") if restricted and key == _SESSION else None
        ),
    )
    monkeypatch.setattr(
        mcp_shared, "_resolve_tool_policy", lambda *_a, **_k: mcp_shared.ToolPolicy(frozenset(), "")
    )
    monkeypatch.setenv("KIROCREW_SESSION_KEY", _SESSION)
    monkeypatch.delenv(relay.SESSION_TOKEN_ENV, raising=False)
    module, attribute = mcp_entry.EARLY_ANSWERS["mcp-core"]
    server = getattr(importlib.import_module(module), attribute)

    by_server = listing.listable_tools(server.name, server.list_tools, None, applies_policy=True)

    monkeypatch.delenv("KIROCREW_SESSION_KEY")  # the gateway holds no session of its own
    monkeypatch.setattr(resource_status, "adaptive_exec_cap", lambda: 3)
    raw = mcp_listing_gateway.session_listing_body(
        None,
        "mcp-core",
        _SESSION,
        "kirocrew",
        code=mcp_listing_gateway._CODE_AT_START,
        place=relay.place_stamp(),
        private_member=False,
    )
    by_gateway = json.loads(raw)[relay.LISTING_KEY]["tools"]

    assert _wire(by_gateway) == _wire(by_server)
    spawn_run = next(tool for tool in by_server if tool["name"] == "spawn_run")
    assert "configured sub-agent ceiling" in spawn_run["description"]
    assert ("restricted by this agent's" in json.dumps(spawn_run)) is restricted
    as_the_gateway_lists = _wire(server.list_tools())
    assert "right now (the cap in force" in as_the_gateway_lists  # the read the relay turns off


# ── the route ───────────────────────────────────────────────────────────────


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class _Request(dict):  # type: ignore[type-arg]
    """What the handler reads of a request: its flags, headers, query and app."""

    def __init__(
        self, headers: dict[str, str], query: dict[str, str], *, starting: str = "", **flags: Any
    ) -> None:
        super().__init__(flags)
        self.headers = headers
        self.query = query
        # *starting*: the session is not registered yet. Its slot names no agent and
        # the session manager knows it only as a start in flight, as that agent.
        slot = types.SimpleNamespace(agent="" if starting else "kirocrew")
        sessions = types.SimpleNamespace(
            get_agent=lambda _key: "",
            starting_agent=lambda key: starting if key == "dashboard:mine" else "",
        )
        state = types.SimpleNamespace(
            get_slot=lambda _key: slot, sessions=sessions if starting else None, subagents=None
        )
        self.app = {"state": state}


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[Any, ...]]:
    """The policy route over an empty agents folder, with the builder recorded."""
    built: list[tuple[Any, ...]] = []

    def build(policy: Any, command: str, session: str, agent: str, **kwargs: Any) -> str:
        built.append((policy, command, session, agent, kwargs, threading.get_ident()))
        return json.dumps({**(policy or {}), relay.LISTING_KEY: _LISTING})

    monkeypatch.setattr(sessions_mod, "session_listing_body", build)
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: types.SimpleNamespace(log_api_access=_noop))
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.verify_session_token",
        lambda token: {"token-of-mine": "dashboard:mine"}.get(token, ""),
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key, **_k: None
    )
    return built


def _ask(
    session: str = "dashboard:mine",
    listing_for: str = "mcp-core",
    *,
    starting: str = "",
    **flags: Any,
) -> _Request:
    query = {relay.CODE_PARAM: "c0de", relay.PLACE_PARAM: "p1ace"}
    if listing_for:
        query[relay.LISTING_PARAM] = listing_for
    headers = {"X-Session-Key": session, "X-Session-Token": "token-of-mine"}
    return _Request(headers, query, starting=starting, **{"internal_auth": True, **flags})


def _answer(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_the_route_builds_the_callers_listing_off_the_event_loop(
    route: list[tuple[Any, ...]],
) -> None:
    response = await sessions_mod.api_session_tool_policy(_ask())
    assert response.status == 200 and _answer(response) == {relay.LISTING_KEY: _LISTING}
    ((policy, command, session, agent, kwargs, thread),) = route
    assert (policy, command, session, agent) == (None, "mcp-core", "dashboard:mine", "kirocrew")
    assert kwargs == {
        "code": "c0de",
        "place": "p1ace",
        "private_member": False,
        "unregistered": False,
    }
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_another_sessions_key_gets_the_refusal_and_no_list(
    route: list[tuple[Any, ...]],
) -> None:
    """The token names ``dashboard:mine``; the key asks for someone else's list."""
    response = await sessions_mod.api_session_tool_policy(_ask(session="dashboard:other"))
    assert response.status == 409
    assert _answer(response)["code"] == "member_identity_unavailable"
    assert route == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, {"exclude": ["shell"]}])
async def test_a_read_that_does_not_ask_gets_the_answer_it_always_got(
    policy: Any, route: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    if policy:
        (tmp_path / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "managedToolPolicy": policy}), encoding="utf-8"
        )
    response = await sessions_mod.api_session_tool_policy(_ask(listing_for=""))
    assert _answer(response) == (policy or {})
    assert route == []


@pytest.mark.asyncio
async def test_a_policy_is_repeated_beside_the_listing(
    route: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "managedToolPolicy": {"exclude": ["shell"]}}),
        encoding="utf-8",
    )
    response = await sessions_mod.api_session_tool_policy(_ask())
    assert _answer(response) == {"exclude": ["shell"], relay.LISTING_KEY: _LISTING}
    assert route[0][0] == {"exclude": ["shell"]}


@pytest.mark.asyncio
async def test_a_private_members_session_reaches_the_builder_as_one(
    route: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's own execution record names a member's store, and the route's real
    member scope reads it: the builder is told, and answers such a session "build
    locally"."""
    members = ExecutionContext(
        member_id="someone",
        store=MemoryStoreRef("member-someone", "someone"),
        selection_kind="member",
        template_id="kirocrew",
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key, **_k: members
    )
    await sessions_mod.api_session_tool_policy(_ask())
    assert route[0][4]["private_member"] is True


@pytest.mark.asyncio
async def test_a_caller_the_route_did_not_authenticate_internally_gets_no_list(
    route: list[tuple[Any, ...]],
) -> None:
    response = await sessions_mod.api_session_tool_policy(_ask(internal_auth=False))
    assert response.body == b"{}"
    assert route == []


# ── the route, while the session's start has not registered it yet ──────────


@pytest.mark.asyncio
async def test_a_starting_sessions_listing_request_is_answered_as_its_starting_agent(
    route: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    (tmp_path / "scout.json").write_text(
        json.dumps({"name": "scout", "managedToolPolicy": {"exclude": ["shell"]}}),
        encoding="utf-8",
    )
    response = await sessions_mod.api_session_tool_policy(_ask(starting="scout"))
    assert response.status == 200
    ((policy, _command, session, agent, kwargs, _thread),) = route
    assert (policy, session, agent) == ({"exclude": ["shell"]}, "dashboard:mine", "scout")
    assert kwargs["unregistered"] is True


@pytest.mark.asyncio
async def test_the_agent_a_session_later_registers_as_is_not_read_in_the_start_window(
    route: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """A start recorded as one agent may register as another (a re-bind, a mode switch).
    Either way the window's list is the unfiltered one a server builds there: the route
    hands the builder the recorded agent's policy, and the builder answers a list only
    when that policy withholds nothing."""
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"name": "kirocrew", "managedToolPolicy": {"exclude": ["shell"]}}),
        encoding="utf-8",
    )
    await sessions_mod.api_session_tool_policy(_ask(starting="scout"))
    ((policy, _command, _session, agent, kwargs, _thread),) = route
    assert (policy, agent, kwargs["unregistered"]) == (None, "scout", True)


@pytest.mark.asyncio
async def test_a_starting_sessions_plain_policy_read_keeps_its_404(
    route: list[tuple[Any, ...]],
) -> None:
    """The start window moves nothing a call is judged by: only a listing request reads
    the starting agent."""
    response = await sessions_mod.api_session_tool_policy(_ask(listing_for="", starting="scout"))
    assert response.status == 404 and _answer(response) == {"error": "agent not resolved"}
    assert route == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ask",
    [
        {"session": "dashboard:other"},  # another session's key: refused before any read
        {"internal_auth": False},  # not this route's own authentication
    ],
)
async def test_a_starting_agent_is_read_for_no_other_caller(
    ask: dict[str, Any], route: list[tuple[Any, ...]]
) -> None:
    response = await sessions_mod.api_session_tool_policy(_ask(starting="scout", **ask))
    assert response.status in (404, 409)
    assert relay.LISTING_KEY not in _answer(response)
    assert route == []


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["{not json", json.dumps(["a", "list"])])
async def test_a_starting_session_with_an_unreadable_policy_keeps_its_404(
    spec: str, route: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    (tmp_path / "scout.json").write_text(spec, encoding="utf-8")
    response = await sessions_mod.api_session_tool_policy(_ask(starting="scout"))
    assert response.status == 404 and _answer(response) == {"error": "agent not resolved"}
    assert route == []


# ── the real entry, in two processes ────────────────────────────────────────

_AT_RELAY = "MODULES-AT-RELAY "
_AT_LIST = "MODULES-AT-LIST "

#: The server's real entry on a stdin holding the two requests. Notes what is loaded
#: when the relay step returns, and stops where the handler import would begin.
_SERVER = f"""
import io, json, os, sys
from kiro_crew import mcp_entry, mcp_listing_relay

# The gateway here is a second process on a shared runner, and stdin is a pipe. Whether
# either answers inside a production bound is not what these tests ask, so the server
# waits the test's own bound for the gateway's answer and for each frame.
WAIT = {FALLBACK_TIMEOUT_SECS!r}
fetch, relay_step = mcp_listing_relay.fetch_listing, mcp_listing_relay.relay_before_boot
mcp_listing_relay.fetch_listing = lambda command: fetch(command, timeout=WAIT)
mcp_listing_relay.relay_before_boot = lambda command: relay_step(command, wait_secs=WAIT)

read_fd, write_fd = os.pipe()
os.write(write_fd, sys.argv[2].encode("utf-8"))
os.close(write_fd)
sys.stdin = io.TextIOWrapper(io.open(read_fd, "rb"))
relay, answer = mcp_entry._relay_before_boot, mcp_entry._answer_before_handlers


def relay_then_note(command):
    frames = relay(command)
    noted = {{"listed": bool(frames and frames.listed), "modules": sorted(sys.modules)}}
    sys.stderr.write({_AT_RELAY!r} + json.dumps(noted) + "\\n")
    return frames


def answer_then_stop(command, relayed=None):
    from kiro_crew import mcp_shared_listing

    serve = mcp_shared_listing.serve_before_handlers
    mcp_shared_listing.serve_before_handlers = lambda server, relayed=None: serve(
        server, wait_secs=WAIT, relayed=relayed
    )
    answer(command, relayed)
    sys.stdout.flush()
    sys.stderr.write({_AT_LIST!r} + json.dumps(sorted(sys.modules)) + "\\n")
    raise SystemExit(0)


mcp_entry._relay_before_boot = relay_then_note
mcp_entry._answer_before_handlers = answer_then_stop
mcp_entry.slim_server([sys.argv[1]])
raise SystemExit("the entry returned without answering")
"""

#: A booted process answering the policy route with the gateway's real builder. The
#: session key picks the case, ``dashboard:<case>.<command>``: a policy name, ``refused``,
#: ``elsewhere``, ``source`` for a gateway on the source checkout these tests run from, or
#: ``unregistered...`` for a session whose own policy read is still answered "agent not
#: resolved".
_GATEWAY = """
import http.server, json, socketserver, sys, urllib.parse
from pathlib import Path

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform import boot_platform

boot_platform(KiroCrewConfig.load())
from kiro_crew import mcp_listing_gateway
from kiro_crew.mcp_listing_relay import CODE_PARAM, LISTING_PARAM, PLACE_PARAM

# What a gateway's startup hook does. And this rig stands for a packaged install: it
# runs from a source checkout, which the sandbox leaves writable and a gateway builds
# no list for. The case "source" keeps the sandbox's real answer for this checkout.
mcp_listing_gateway.note_code_at_start()
classify = mcp_listing_gateway.install_root_targets_for

POLICIES = json.loads(sys.argv[1])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        session = self.headers.get("X-Session-Key", "")
        case = session.partition(":")[2].partition(".")[0]
        unregistered = case.startswith("unregistered")
        command = (query.get(LISTING_PARAM) or [""])[0]
        status, policy = 200, POLICIES.get(case)
        if case == "refused":
            status, body = 409, json.dumps({"code": "member_identity_unavailable"})
        elif unregistered and not command:
            status, body = 404, json.dumps({"error": "agent not resolved"})
        elif command:
            mcp_listing_gateway.install_root_targets_for = (
                classify if case == "source" else (lambda package_dir: [package_dir])
            )
            place = (query.get(PLACE_PARAM) or [""])[0]
            body = mcp_listing_gateway.session_listing_body(
                policy,
                command,
                session,
                "kirocrew",
                code=(query.get(CODE_PARAM) or [""])[0],
                place="elsewhere" if case == "elsewhere" else place,
                private_member=False,
                unregistered=unregistered,
            )
        else:
            body = json.dumps(policy or {})
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)


server = Server(("127.0.0.1", 0), Handler)
Path(sys.argv[2]).write_text(str(server.server_address[1]), encoding="utf-8")
server.serve_forever()
"""

#: Policy name -> the policy the gateway answers for that session. One tool of each
#: server is withheld by name, and each server is narrowed to a list of its own.
_POLICIES: dict[str, Any] = {
    "open": None,
    "excluding": {"exclude": ["spawn_run", "send_message", "cron_add", "search_docs", "set_goal"]},
    "including": {
        "exclude": ["spawn_list"],
        "include": {
            "kirocrew-core": ["spawn_list", "spawn_run", "send_message"],
            "kirocrew-cron": ["cron_list"],
            "kirocrew-guide": [],
            "kirocrew-mate": ["goal_read", "set_goal"],
        },
    },
    "unregistered": None,
    "source": None,
}
_POLICIES["unregistered-excluding"] = _POLICIES["excluding"]

#: The cases in which the gateway answers a list, and the ones in which the server
#: builds its own although it asked.
RELAYED = ("open", "excluding", "including", "unregistered")
LEFT_TO_THE_SERVER = ("refused", "unregistered-excluding", "source")

#: What the relay step must not have loaded: the configuration loader, the platform and
#: the audit log, and the module that builds a list in the server.
_NOT_BEFORE_BOOT = (
    "kiro_crew.config.loader",
    "kiro_crew.mcp_shared_listing",
    "kiro_crew.platform",
    "kiro_crew.sel",
)


def _loaded_too_early(modules: list[str]) -> list[str]:
    inside = tuple(f"{name}." for name in _NOT_BEFORE_BOOT)
    return sorted(m for m in modules if m in _NOT_BEFORE_BOOT or m.startswith(inside))


class _Rig:
    """One home, one gateway process over it, and the server runs made against it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = home = root / "home"
        home.mkdir()
        (home / "config.json").write_text('{"agent": {"max_subagents": 7}}', encoding="utf-8")
        (home / ".local_secret").write_text("s3cret", encoding="utf-8")
        agents = root / ".kiro" / "agents"
        agents.mkdir(parents=True)
        for name in ("probe", "scout"):
            (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        port_file = root / "port"
        self._gateway = subprocess.Popen(
            [sys.executable, "-c", _GATEWAY, json.dumps(_POLICIES), str(port_file)],
            env=_child_env(root),
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **UTF8_TEXT,
        )
        try:
            wait_until(
                lambda: port_file.exists() and port_file.read_text(encoding="utf-8").strip(),
                timeout=FALLBACK_TIMEOUT_SECS,
                describe=lambda: f"the gateway process (exit {self._gateway.poll()})",
            )
        except BaseException:
            self.close()
            raise
        self.port = port_file.read_text(encoding="utf-8").strip()
        self._runs: dict[tuple[str, str, bool], tuple[list[str], dict[str, Any]]] = {}

    def close(self) -> None:
        self._gateway.kill()
        self._gateway.communicate(timeout=FALLBACK_TIMEOUT_SECS)

    @staticmethod
    def session(command: str, case: str) -> str:
        return f"dashboard:{case}.{command}"

    def audited(self, command: str, case: str) -> list[str]:
        """The audit operations recorded so far for *command*'s session of *case*."""
        log = self.home / "security_events.jsonl"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        session = self.session(command, case)
        return [str(row.get("operation")) for row in rows if session in json.dumps(row)]

    def run(self, command: str, case: str, *, asks: bool) -> tuple[list[str], dict[str, Any]]:
        """The frames *command* wrote, as written, and what the relay step left: whether
        it listed, and the modules loaded when it returned. *asks* gives the server the
        session token, without which it never asks."""
        key = (command, case, asks)
        if key not in self._runs:
            extra = {relay.SESSION_TOKEN_ENV: "token"} if asks else {}
            done = subprocess.run(
                [sys.executable, "-c", _SERVER, command, _REQUESTS],
                env=_child_env(
                    self.root,
                    KIROCREW_PROJECT_DIR=str(self.root),
                    KIROCREW_PORT=self.port,
                    KIROCREW_SESSION_KEY=self.session(command, case),
                    **extra,
                ),
                cwd=str(self.root),
                capture_output=True,
                timeout=FALLBACK_TIMEOUT_SECS,
                check=False,
                **UTF8_TEXT,
            )
            noted = [line for line in done.stderr.splitlines() if line.startswith(_AT_RELAY)]
            assert done.returncode == 0 and noted, done.stderr[-4000:]
            frames = [line for line in done.stdout.splitlines() if line.startswith("{")]
            self._runs[key] = (frames, json.loads(noted[-1][len(_AT_RELAY) :]))
        return self._runs[key]


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Rig]:
    made = _Rig(tmp_path_factory.mktemp("relay"))
    yield made
    made.close()


def _names(frame: str) -> list[str]:
    return [tool["name"] for tool in json.loads(frame)["result"]["tools"]]


@pytest.mark.parametrize("case", RELAYED)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_client_reads_the_same_bytes_from_a_relayed_and_a_server_built_list(
    command: str, case: str, rig: _Rig
) -> None:
    relayed, asked = rig.run(command, case, asks=True)
    built, never_asked = rig.run(command, case, asks=False)
    assert asked["listed"] and not never_asked["listed"]  # two builders, not one twice
    assert relayed == built and len(relayed) == 2
    # The relayed pair was written before the server loaded what builds it.
    assert _loaded_too_early(asked["modules"]) == []
    names = _names(relayed[1])
    if case == "unregistered":
        # Nothing was unresolved and nothing went out unfiltered by default, so the
        # relayed start records neither; the server that builds its own records both.
        assert rig.audited(command, case).count("tool_policy.unfiltered_listing") == 1
    policy = _POLICIES[case] or {}
    assert not set(names) & set(policy.get("exclude", ()))
    name = json.loads(relayed[0])["result"]["serverInfo"]["name"]
    if name in policy.get("include", {}):
        assert set(names) <= set(policy["include"][name])
    else:
        assert names, "the list went out empty"


@pytest.mark.parametrize(
    ("command", "case"),
    [(command, case) for case in LEFT_TO_THE_SERVER for command in COMMANDS]
    + [("mcp-core", "elsewhere")],
)
def test_a_server_that_gets_no_list_has_loaded_nothing_and_answers_as_it_always_did(
    command: str, case: str, rig: _Rig
) -> None:
    """A refusal, and a gateway that says "build locally": at the moment the relay step
    returns, no configuration loader, platform or audit module is loaded, so nothing it
    did can have audited or composed anything; the two answers are the ones a server
    that never asked writes."""
    fell_back, asked = rig.run(command, case, asks=True)
    never_asked, _ = rig.run(command, case, asks=False)
    assert not asked["listed"]
    assert _loaded_too_early(asked["modules"]) == []
    assert fell_back == never_asked and len(fell_back) == 2
    assert _names(fell_back[1]), "the list went out empty"
    if case == "unregistered-excluding":
        # An unregistered session whose policy withholds tools: each run listed
        # everything on an unresolved policy and recorded both rows, as on main.
        audited = rig.audited(command, case)
        assert audited.count("tool_policy.agent_not_resolved") == 2
        assert audited.count("tool_policy.unfiltered_listing") == 2
        assert "spawn_run" in _names(fell_back[1]) or command != "mcp-core"
