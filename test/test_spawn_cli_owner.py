"""``kirocrew spawn``: the dashboard owner at a terminal, and what that principal may do.

The CLI authenticates as the OWNER principal (``owner_gateway_client``). It mints a
short-lived owner token from ``/api/token/local?origin=cli`` over the dashboard unix
socket only, after checking that the socket's peer is the recorded gateway, and
sends the token on each spawn call. The token's signed ``origin: cli`` claim is the
only thing that marks a run as the owner's terminal spawn: such a run is governed
as the attended CLI surface (``cli_chat``) on the agent it resolves to, carries the
origin on its ``ExecutionContext``, and faces the ordinary spawn prompt.
subagent.md § "CLI: kirocrew spawn run" states the rule.

An internal-secret caller with no session neither starts nor owns a parentless run.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import http.client
import json
import os
import pkgutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

import pytest
from aiohttp import web
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request, spawn_row
from spawn_cli_fakes import MINTED, http_error, install, stub_module_clock

from kiro_crew import cli_commands, owner_gateway_client, platform_compat, subagent
from kiro_crew.dashboard import server, token_auth
from kiro_crew.dashboard.handlers import _shared, messaging
from kiro_crew.execution_context import CLI_ORIGIN
from kiro_crew.instances import run_marker

pytestmark = pytest.mark.xdist_group("member_memory_api")

env = _member_env


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _run(port: int = 7, *, fire_and_forget: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        spawn_action="run", task="hello", port=port, fire_and_forget=fire_and_forget
    )


def _token(req) -> list[str]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get("token", [])


# ── the CLI: an owner token, over the unix socket only ──


def test_the_fake_gateway_and_clock_stubs_leave_the_stdlib_clock_alone(monkeypatch) -> None:
    """The CLI tests stub each module's own ``time`` binding, never ``time`` itself:
    every event loop and thread in the worker reads the stdlib clock."""
    real = (time.sleep, time.time, time.monotonic)
    install(monkeypatch, MINTED)
    stub_module_clock(monkeypatch, cli_commands, "_time", time=lambda: 0.0)
    stub_module_clock(monkeypatch, token_auth, "time", time=lambda: 0.0)
    assert (time.sleep, time.time, time.monotonic) == real
    assert cli_commands._time.time() == token_auth.time.time() == 0.0
    assert cli_commands._time.monotonic is time.monotonic  # every other name delegates
    assert owner_gateway_client.time.sleep is not time.sleep  # the module's own stand-in


def _patch_owner(first: ast.expr, namespace: dict[str, object]) -> object:
    """The object a ``setattr`` / ``patch`` call would patch, resolved the way the call
    resolves it; ``None`` for a name this module cannot see (a parameter or a local).
    """
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        try:
            return pkgutil.resolve_name(first.value.rpartition(".")[0])
        except (ImportError, AttributeError, ValueError):
            return None
    chain: list[str] = []
    while isinstance(first, ast.Attribute):
        chain.insert(0, first.attr)
        first = first.value
    if not isinstance(first, ast.Name) or first.id not in namespace:
        return None
    owner = namespace[first.id]
    for attr in chain:
        owner = getattr(owner, attr, None)
    return owner


def test_no_spawn_cli_test_patches_the_stdlib_time_module() -> None:
    """A patch whose target IS the stdlib ``time`` module, under any spelling
    (``cli_commands._time``, ``owner_gateway_client.time``, ``"time.sleep"``),
    replaces the clock or the sleep for the whole worker (AUTOSDE D2/D11). Each target
    is resolved, so an alias of the module counts; ``stub_module_clock`` is the seam.
    """
    import spawn_cli_fakes

    offenders = []
    for path, namespace in (
        (Path(spawn_cli_fakes.__file__), vars(spawn_cli_fakes)),
        (Path(__file__), globals()),
    ):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            called = ast.unparse(node.func)
            if called.rsplit(".", 1)[-1] not in ("setattr", "patch") and not called.endswith(
                "patch.object"
            ):
                continue
            if _patch_owner(node.args[0], namespace) is time:
                offenders.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:80]}")
    assert not offenders, "stub the module's own binding with stub_module_clock:\n" + "\n".join(
        offenders
    )


def test_spawn_run_mints_a_cli_owner_token_over_the_checked_socket(monkeypatch) -> None:
    gateway = install(monkeypatch, MINTED, {"id": "run-1", "task": "hello"})
    cli_commands._spawn(_run(fire_and_forget=True))
    mint, post = gateway.calls
    assert mint.socket_path == post.socket_path == Path("/s/dash-7.sock")
    assert mint.verify_peer is not None and post.verify_peer is not None
    assert mint.req.full_url == "http://127.0.0.1:7/api/token/local?ttl=2m&origin=cli"
    assert mint.req.get_header("X-local-secret") == "s3cret"
    assert _token(post.req) == ["owner-tok"]
    assert json.loads(post.req.data) == {"task": "hello"}
    assert not post.req.has_header("X-internal-secret")


def test_a_gateway_with_no_socket_is_named_and_tcp_is_never_tried(monkeypatch, capsys) -> None:
    install(monkeypatch, urllib.error.URLError(FileNotFoundError(2, "no such file")))
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    out = capsys.readouterr().out
    assert "its bind failed, see gateway.log, or it runs on Windows" in out
    assert "spawn from the dashboard" in out


@pytest.mark.parametrize(
    "status, code, says",
    [
        (403, "member_owner_token_refused", "outside an agent sandbox; a gateway running as PID 1"),
        (403, "loopback_only", "as the user the gateway runs as, not through sudo"),
        (403, "", "restart or upgrade it"),
        (404, "", "restart or upgrade it"),
    ],
    ids=["not_the_owner", "another_user", "no_code", "no_mint_route"],
)
def test_each_mint_refusal_names_its_own_remedy(monkeypatch, capsys, status, code, says) -> None:
    refusal = {"error": "refused here", **({"code": code} if code else {})}
    install(monkeypatch, http_error(status, refusal))
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    out = capsys.readouterr().out
    assert "refused here. " in out and says in out
    assert "restart or upgrade it" not in out or code == ""


def test_a_socket_path_too_long_to_bind_is_named_as_no_socket(monkeypatch, capsys) -> None:
    """A data home too deep for ``sun_path``: the gateway could not bind it either."""
    install(monkeypatch, urllib.error.URLError(OSError("AF_UNIX path too long")))
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    assert "spawn from the dashboard" in capsys.readouterr().out


def test_a_mint_that_times_out_says_no_spawn_was_sent(monkeypatch, capsys) -> None:
    install(monkeypatch, urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    out = capsys.readouterr().out
    assert "this request was not sent" in out and "may or may not have started" not in out


@pytest.mark.parametrize(
    "dropped",
    [
        lambda: http.client.RemoteDisconnected("closed"),
        lambda: ConnectionResetError(104, "reset"),
        lambda: http.client.IncompleteRead(b"{"),
    ],
    ids=["remote_disconnected", "reset", "incomplete_read"],
)
def test_a_post_dropped_after_it_was_sent_says_the_outcome_is_unknown(
    monkeypatch, capsys, dropped
) -> None:
    """urllib raises these bare, from reading the answer: the gateway had the request."""
    install(monkeypatch, MINTED, dropped())
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    out = capsys.readouterr().out
    assert "may or may not have started" in out and "cannot reach" not in out


def test_a_secret_mismatch_is_retried_once_while_the_gateway_starts(monkeypatch, capsys) -> None:
    """A starting gateway binds its socket before it writes its new secret."""

    def stale() -> urllib.error.HTTPError:
        return http_error(403, {"error": "invalid secret", "code": "invalid_secret"})

    gateway = install(monkeypatch, stale(), MINTED, {"id": "run-1", "task": "hello"})
    cli_commands._spawn(_run(fire_and_forget=True))
    assert "Spawned subagent run-1" in capsys.readouterr().out
    assert len(gateway.calls) == 3

    install(monkeypatch, stale(), stale())
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    assert "the gateway may be restarting, so retry" in capsys.readouterr().out


def test_a_post_that_times_out_says_the_outcome_is_unknown(monkeypatch, capsys) -> None:
    install(monkeypatch, MINTED, urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run(fire_and_forget=True))
    out = capsys.readouterr().out
    assert "may or may not have started" in out and "before running this again" in out


def _retry_shortly() -> urllib.error.HTTPError:
    """The gateway's 503 for a task queue it cannot read, with its Retry-After."""
    error = http_error(503, {"error": "retry shortly", "code": "taskq_unavailable"})
    error.headers = {"Retry-After": "2"}  # type: ignore[assignment]
    return error


@pytest.mark.parametrize(
    "transient",
    [
        _retry_shortly,
        lambda: urllib.error.URLError(TimeoutError("timed out")),
        lambda: http.client.IncompleteRead(b"{"),
    ],
    ids=["503_retry_after", "read_timeout", "cut_off"],
)
def test_the_poll_asks_again_after_a_transient_failure(monkeypatch, capsys, transient) -> None:
    """A 5xx (the gateway's "retry shortly") or a lost answer to the GET is asked
    again, after the Retry-After, a bounded number of times."""
    slept: list[float] = []
    gateway = install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        *(transient() for _ in range(cli_commands._SPAWN_POLL_RETRIES)),
        {"id": "run-1", "done": True, "result": "late answer", "outcome": "completed"},
    )
    stub_module_clock(monkeypatch, cli_commands, "_time", sleep=slept.append)
    cli_commands._spawn(_run())
    assert capsys.readouterr().out.strip() == "late answer"
    assert not gateway.script
    if transient is _retry_shortly:
        assert slept.count(2.0) >= 2 * cli_commands._SPAWN_POLL_RETRIES  # poll pause + Retry-After


def test_the_poll_gives_up_after_its_retries(monkeypatch, capsys) -> None:
    gateway = install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        *(_retry_shortly() for _ in range(cli_commands._SPAWN_POLL_RETRIES + 1)),
        {"id": "run-1", "done": True, "result": "never read", "outcome": "completed"},
    )
    with pytest.raises(SystemExit) as exited:
        cli_commands._spawn(_run())
    err = capsys.readouterr().err
    assert exited.value.code == 1
    assert "retry shortly" in err and "may still be running" in err
    assert len(gateway.script) == 1


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError(OSError("reset")),
        urllib.error.URLError(FileNotFoundError(2, "gone")),
        owner_gateway_client.OwnerGatewayError("not yet", kind="not_the_gateway"),
    ],
    ids=["reset", "socket_gone", "not_the_gateway"],
)
def test_a_lost_poll_ends_and_says_the_run_may_still_be_going(monkeypatch, capsys, failure) -> None:
    """A poll that never reached the gateway is not asked again; losing the poll
    does not stop the run, so the CLI says where to find it."""
    install(monkeypatch, MINTED, {"id": "run-1", "task": "hello"}, failure)
    with pytest.raises(SystemExit) as exited:
        cli_commands._spawn(_run())
    assert exited.value.code == 1
    assert "may still be running" in capsys.readouterr().err


def test_each_tool_prompt_is_announced_even_with_no_poll_between_them(monkeypatch, capsys) -> None:
    def parked(tool: str) -> dict:
        return {"id": "run-1", "done": False, "awaiting_tool_approval": True, "last_tool": tool}

    install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        parked("shell"),
        parked("shell"),
        parked("write"),
        {"id": "run-1", "done": False},
        parked("write"),
        {"id": "run-1", "done": True, "result": "ok", "outcome": "completed"},
    )
    cli_commands._spawn(_run())
    assert capsys.readouterr().err.count("Waiting for a tool approval") == 3


def test_an_ending_nothing_recorded_keeps_the_poll_going(monkeypatch, capsys) -> None:
    """For a run whose gateway died mid-run before its recovery settled it, the
    persistence fallback answers ``done: false`` with no ``outcome`` and
    ``stopped: false``; the poll waits for the ending the recovery records."""
    gateway = install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        {"id": "run-1", "done": False, "error": "", "stopped": False, "result": "partial"},
        {
            "id": "run-1",
            "done": True,
            "error": "",
            "stopped": False,
            "outcome": "completed",
            "result": "recovered answer",
        },
    )
    cli_commands._spawn(_run())
    out = capsys.readouterr()
    assert out.out.strip() == "recovered answer" and "partial" not in out.out
    assert not gateway.script


@pytest.mark.parametrize(
    "error, code, printed",
    [("", None, "THE RESULT"), ("agent blew up", 1, "")],
    ids=["no-error-prints-the-result", "an-error-fails"],
)
def test_a_gateway_older_than_its_cli_keeps_the_old_done_rule(
    monkeypatch, capsys, error, code, printed
) -> None:
    """A gateway that predates ``outcome`` on its live answer sends neither
    ``outcome`` nor ``stopped``; there an error fails and anything else succeeds."""
    install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        {"id": "run-1", "done": True, "result": "THE RESULT", "error": error, "elapsed": 1.0},
    )
    if code is None:
        cli_commands._spawn(_run())
    else:
        with pytest.raises(SystemExit) as exited:
            cli_commands._spawn(_run())
        assert exited.value.code == code
    out = capsys.readouterr()
    assert out.out.strip() == printed
    if error:
        assert error in out.err


def test_a_stopped_run_is_not_reported_as_success(monkeypatch, capsys) -> None:
    """A stop from the dashboard, or a cancel while the run was queued, ends it with
    outcome ``stopped`` and no error; the command must still exit non-zero."""
    install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        {"id": "run-1", "done": True, "error": "", "outcome": "stopped", "result": "_No result._"},
    )
    with pytest.raises(SystemExit) as exited:
        cli_commands._spawn(_run())
    out = capsys.readouterr()
    assert exited.value.code == 1 and "subagent run-1 was stopped" in out.err
    assert "_No result._" not in out.out


def test_a_definitive_refusal_ends_the_poll_and_says_where_to_look(monkeypatch, capsys) -> None:
    install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        {"id": "run-1", "done": False},
        http_error(404, {"error": "nope"}),
    )
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run())
    err = capsys.readouterr().err
    assert "nope" in err and "may still be running" in err and "lost connection" not in err


def test_a_403_earns_exactly_one_fresh_mint(monkeypatch, capsys) -> None:
    """A logout or a clock step ends a token the CLI's clocks still call live."""
    gateway = install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        http_error(403, {"error": "Forbidden"}),
        {"token": "second", "expires_in": 120},
        {"id": "run-1", "done": True, "result": "ok", "outcome": "completed"},
    )
    cli_commands._spawn(_run())
    assert _token(gateway.calls[-1].req) == ["second"]

    install(
        monkeypatch,
        MINTED,
        {"id": "run-1", "task": "hello"},
        http_error(403, {"error": "Forbidden"}),
        MINTED,
        http_error(403, {"error": "Forbidden"}),
    )
    with pytest.raises(SystemExit):
        cli_commands._spawn(_run())
    assert "Forbidden" in capsys.readouterr().err


def test_a_wall_clock_jump_renews_the_token_before_it_is_sent(monkeypatch) -> None:
    """A resumed laptop: the monotonic clock did not see the suspend, the wall did."""
    now = {"wall": 1000.0}
    stub_module_clock(monkeypatch, cli_commands, "_time", time=lambda: now["wall"])
    gateway = install(monkeypatch, MINTED, {"id": "run-1", "task": "hello"})
    held = cli_commands._SpawnGateway(7)
    held.call("/api/spawn", {"task": "hello"})
    now["wall"] += 600
    gateway.script += [{"token": "renewed", "expires_in": 120}, {"agents": []}]
    held.call("/api/spawn")
    assert _token(gateway.calls[-1].req) == ["renewed"]


# ── the peer check: a listener that took the socket path gets nothing ──


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the dashboard socket is POSIX-only")
def test_a_listener_that_is_not_the_gateway_receives_no_credential(short_sock_dir) -> None:
    """The socket path is rewritable by its owner; the kernel's peer pid is not."""
    path = short_sock_dir / "decoy.sock"
    received = short_sock_dir / "received"
    decoy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,sys\n"
            "s=socket.socket(socket.AF_UNIX);s.bind(sys.argv[1]);s.listen(1)\n"
            "print('ready',flush=True)\n"
            "c,_=s.accept();c.settimeout(3)\n"
            "try:\n data=c.recv(65536)\nexcept OSError:\n data=b''\n"
            "open(sys.argv[2],'wb').write(data)\n",
            str(path),
            str(received),
        ],
        stdout=subprocess.PIPE,
        cwd=short_sock_dir,
        start_new_session=True,
    )
    try:
        assert decoy.stdout is not None and decoy.stdout.readline().strip() == b"ready"
        with mock.patch.object(owner_gateway_client, "dashboard_socket_path", lambda _p: path):
            with pytest.raises(owner_gateway_client.OwnerGatewayError) as refused:
                owner_gateway_client.owner_call(7, "/api/spawn", token="secret-token")
        assert refused.value.kind == "not_the_gateway"
        decoy.wait(timeout=10)
        assert received.read_bytes() == b"", "the decoy listener was sent bytes"
    finally:
        if decoy.poll() is None:
            os.killpg(decoy.pid, 9)
            decoy.wait(timeout=10)
        if decoy.stdout is not None:
            decoy.stdout.close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the dashboard socket is POSIX-only")
def test_the_recorded_gateway_passes_the_peer_check() -> None:
    run_marker.write_marker(7)
    left, right = socket.socketpair(socket.AF_UNIX)
    try:
        owner_gateway_client.gateway_peer_verifier(7)(left)
        with pytest.raises(owner_gateway_client.OwnerGatewayError):
            owner_gateway_client.gateway_peer_verifier(8)(left)
    finally:
        left.close()
        right.close()


# ── /api/token/local?origin=cli and POST /api/spawn: only the token marks the CLI ──


def test_the_cli_token_carries_the_origin_and_no_link_nonce(monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr(token_auth._state, "register_nonce", lambda *a: registered.append(a))
    token = token_auth.generate_token(
        "owner", ttl_seconds=120, extra={"origin": CLI_ORIGIN}, register_nonce=False
    )
    assert registered == []
    assert token_auth.validate_token_with_app(token, use_session_exp=True)[0] is True
    # Not a one-time link: the link path consults the nonce set it never joined.
    assert token_auth.validate_token_with_app(token, use_session_exp=False)[0] is False
    req = make_request(SimpleNamespace(), "/api/spawn", owner=True)
    req["auth_token"] = token
    assert token_auth.validated_token_origin(req) == CLI_ORIGIN


def _spawn_recorder(env) -> mock.Mock:
    spawn = mock.Mock(return_value=SimpleNamespace(id="run-1", done=False))
    env.state.subagents = SimpleNamespace(spawn=spawn)
    return spawn


def _owner(env, body: dict, *, cli_token: bool) -> web.Request:
    req = make_request(env.state, "/api/spawn", body=body, owner=True)
    extra = {"origin": CLI_ORIGIN} if cli_token else None
    req["auth_token"] = token_auth.generate_token(
        "owner", ttl_seconds=120, extra=extra, register_nonce=False
    )
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cli_token, body, origin",
    [
        (True, {"task": "t"}, CLI_ORIGIN),
        (False, {"task": "t", "origin": "cli"}, ""),
        (False, {"task": "t"}, ""),
    ],
    ids=["cli_token", "browser_claiming_cli_in_body", "browser"],
)
async def test_only_the_cli_token_marks_a_run_as_the_cli_origin(env, cli_token, body, origin):
    spawn = _spawn_recorder(env)
    response = await messaging.api_spawn(_owner(env, body, cli_token=cli_token))
    assert response.status == 200, response.text
    kwargs = spawn.call_args.kwargs
    assert kwargs["_execution_context"].get("origin", "") == origin
    assert kwargs["parent_session_key"] == ""


@pytest.mark.asyncio
async def test_a_cli_spawn_names_no_parent(env) -> None:
    spawn = _spawn_recorder(env)
    response = await messaging.api_spawn(
        _owner(env, {"task": "t", "parent_session": "dashboard:alice"}, cli_token=True)
    )
    assert response.status == 400
    assert json.loads(response.text)["code"] == "invalid_origin"
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [None, "kirocrew-cli"])
async def test_a_session_less_internal_caller_starts_no_parentless_run(
    env, caller, monkeypatch
) -> None:
    """Refused by the shared identity check alone: its empty claim fails a scope
    with no session, and that one path writes the one denial row."""
    spawn = _spawn_recorder(env)
    audited = mock.AsyncMock()
    monkeypatch.setattr(_shared, "_audit_private_memory_denial", audited)
    response = await messaging.api_spawn(
        make_request(
            env.state,
            "/api/spawn",
            body={"task": "t"},
            internal=True,
            session="",
            attested=False,
            extra_headers={"X-Internal-Caller": caller} if caller else None,
        )
    )
    assert response.status == 409
    payload = json.loads(response.text)
    assert payload["code"] == "member_identity_unavailable"
    spawn.assert_not_called()
    audited.assert_awaited_once_with("spawn.create", "The execution identity is unavailable.")


@pytest.mark.asyncio
async def test_the_shared_identity_check_has_no_parentless_arm(env) -> None:
    """Workflow, batch and session routes share this check with their own session
    claim; a session-less internal caller still gets its 409 there."""
    req = make_request(
        env.state, "/api/workflows/run", body={}, internal=True, session="", attested=False
    )
    _, refusal = await _shared.internal_memory_scope(req, "workflow.run", claimed_session="")
    assert refusal is not None and refusal.status == 409
    assert json.loads(refusal.text)["code"] == "member_identity_unavailable"


# ── run controls and the wait a terminal user needs to see ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, method, body",
    [
        ("api_spawn_status", "GET", None),
        ("api_spawn_steer", "POST", {"message": "steer", "mode": "follow_up"}),
        ("api_spawn_release", "POST", {}),
        ("api_spawn_retry", "POST", {}),
        ("api_spawn_delete", "DELETE", None),
    ],
)
async def test_an_identity_less_internal_caller_controls_no_parentless_run(
    env, handler, method, body
) -> None:
    rows = {"cli-run": spawn_row("cli-run", "")}
    env.state.subagents = SimpleNamespace(get=rows.get, all_agents=list(rows.values()))
    req = make_request(
        env.state,
        "/api/spawn/cli-run",
        method=method,
        body=body,
        internal=True,
        session="",
        attested=False,
        match_info={"agent_id": "cli-run"},
    )
    response = await getattr(messaging, handler)(req)
    assert response.status == 404, response.text
    assert json.loads(response.text)["code"] == "task_scope_denied"


@pytest.mark.asyncio
async def test_a_run_parked_on_a_tool_prompt_says_so_on_both_read_paths() -> None:
    info = SimpleNamespace(
        id="r",
        task="t",
        done=False,
        started=0,
        turns=1,
        last_tool="shell",
        streaming_text="",
        parent_session_key="",
        agent="kirocrew",
        crew="",
        include_memory=True,
        include_lessons=True,
        include_project=True,
        _awaiting_approval=True,
        _exec_started=1.0,
    )
    state = SimpleNamespace(subagents=SimpleNamespace(get=lambda _id: info, all_agents=[info]))
    req = make_request(state, "/api/spawn/r", owner=True, match_info={"agent_id": "r"})
    payload = json.loads((await messaging.api_spawn_status(req)).text)
    assert payload["awaiting_tool_approval"] is True and "awaiting_approval" not in payload
    nothing_persisted = messaging.PanelRecords([], 0, False)
    with mock.patch.object(messaging, "read_panel_records", return_value=nothing_persisted):
        listed = await messaging.api_spawn_list(make_request(state, "/api/spawn", owner=True))
    assert json.loads(listed.text)["agents"][0]["awaiting_tool_approval"] is True


@pytest.mark.asyncio
async def test_a_finished_run_reports_its_recorded_outcome() -> None:
    """The live answer carries ``outcome`` as the persistence fallback does, so the
    CLI can tell a settled run from a record nothing settled."""
    info = subagent.SubagentInfo(id="r", task="t", done=True, error="boom")
    state = SimpleNamespace(subagents=SimpleNamespace(get=lambda _id: info))
    req = make_request(state, "/api/spawn/r", owner=True, match_info={"agent_id": "r"})
    payload = json.loads((await messaging.api_spawn_status(req)).text)
    assert payload["outcome"] == "failed"


# ── the spawn gate: the ordinary prompt, governed as the CLI surface on the resolved agent ──


def _manager(on_spawn_approval) -> subagent.SubagentManager:
    sessions = mock.MagicMock()
    sessions.get_pid = mock.MagicMock(return_value=None)
    sessions.get_approval_policy = mock.MagicMock(return_value=None)
    sessions.get_agent_selection = mock.MagicMock(return_value=("template", ""))
    ctx_builder = mock.MagicMock()
    ctx_builder.hooks = SimpleNamespace(
        auto_approve_subagent_spawn=False, auto_approve_subagent_tools=False
    )
    manager = subagent.SubagentManager(
        sessions=sessions,
        ctx_builder=ctx_builder,
        on_spawn_approval=on_spawn_approval,
        is_yolo=lambda: False,
    )
    manager._run = mock.AsyncMock()
    return manager


def _cli_execution() -> dict:
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    return ExecutionContext(
        None, MemoryStoreRef("default"), "template", "kirocrew", origin=CLI_ORIGIN
    ).to_record()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_cli_spawn_faces_the_spawn_prompt() -> None:
    """The owner's terminal spawn is asked like any other caller's: with no dashboard
    client to show the prompt, it is refused there."""
    approval = mock.AsyncMock(side_effect=subagent.SpawnApprovalUnreachable("no client"))
    manager = _manager(approval)
    info = manager.spawn("task", _execution_context=_cli_execution())
    assert info is not None and not info.error, info.error
    await asyncio.wait_for(manager._tasks[info.id], timeout=10)
    approval.assert_called_once()
    manager._run.assert_not_called()
    assert info.execution_context.origin == CLI_ORIGIN


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_run_that_a_cli_run_spawns_is_not_a_cli_run() -> None:
    """The child is asked for by its parent session, not by the terminal: it faces
    the spawn prompt and is governed under its parent's key."""
    from kiro_crew.execution_context import execution_from_record

    approval = mock.AsyncMock(return_value=False)
    manager = _manager(approval)
    parent = execution_from_record({"execution_context": _cli_execution()})
    with mock.patch("kiro_crew.execution_context.read_session_execution", return_value=parent):
        child = manager.spawn("nested", parent_session_key="subagent:cli-run")
    assert child is not None and not child.error, child.error
    await asyncio.wait_for(manager._tasks[child.id], timeout=10)
    assert child.execution_context.origin == ""
    approval.assert_awaited_once()
    manager._run.assert_not_called()


def test_neither_a_child_nor_a_schedule_inherits_the_cli_origin(monkeypatch) -> None:
    from kiro_crew.cron_service import identity
    from kiro_crew.execution_context import derive_execution, execution_from_record

    parent = execution_from_record({"execution_context": _cli_execution()})
    assert derive_execution(parent).origin == ""
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda _k: parent)
    job = SimpleNamespace(
        execution_context=None,
        session_key="subagent:cli-run",
        memory_store="",
        agent_id="",
        member_id="",
    )
    identity.bind_cron_memory(job)
    assert job.execution_context is not None and "origin" not in job.execution_context


@pytest.fixture
def cli_profile(tmp_path, monkeypatch) -> Iterator:
    """A permissive ceiling, and a writer for a surface profile's spawn scope
    (``surface:cli`` unless another surface is named)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    base = build_default_context(KiroCrewConfig.load())
    policy = parse_policy({"version": 1, "boot": {"fail_closed": True}})
    ctx_mod.set_context(dataclasses.replace(base, governance=policy))

    def _write(spawn: dict, surface: str = "cli") -> None:
        (profiles / f"{surface}.json").write_text(
            json.dumps(
                {
                    "name": surface,
                    "bind": {"type": "surface", "id": surface},
                    "capabilities": {"spawn": spawn},
                }
            ),
            encoding="utf-8",
        )
        gp.reset_store()

    yield _write
    gp.reset_store()
    ctx_mod.reset_context()


def _agents(*allowed: str) -> dict:
    return {"enabled": True, "scopes": {"agents": {"mode": "allow", "allow": list(allowed)}}}


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize(
    "spawn", [{"enabled": False}, _agents("researcher")], ids=["spawn_off", "agents_scope"]
)
async def test_a_cli_profile_governs_kirocrew_spawn_run(env, cli_profile, spawn) -> None:
    """The CLI posts a task only, so the agent scope is checked on the agent the
    run resolves to (``kirocrew``). The dashboard's parentless spawn is not this
    profile's to govern."""
    cli_profile(spawn)
    for cli_token, refused in ((True, True), (False, False)):
        # Approved: ``spawn_async`` yields after the gate, so a declined prompt
        # can settle before ``api_spawn`` answers and read as a 400 refusal.
        manager = _manager(mock.AsyncMock(return_value=True))
        env.state.subagents = manager
        response = await messaging.api_spawn(_owner(env, {"task": "task"}, cli_token=cli_token))
        payload = json.loads(response.text)
        if refused:
            assert response.status == 400
            assert "spawn refused by governance" in payload["error"]
            manager._run.assert_not_called()
        else:
            assert response.status == 200, payload
            await asyncio.wait_for(manager._tasks[payload["id"]], timeout=10)


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_spawn_that_names_no_agent_is_checked_as_the_default_agent(env, cli_profile):
    """A spawn naming no agent, whose parent records no template, is checked as
    ``kirocrew`` at admission, so an allow-list without it refuses the spawn."""
    cli_profile(_agents("researcher"), surface="dashboard")
    manager = _manager(mock.AsyncMock(return_value=True))
    info = manager.spawn("task", parent_session_key="dashboard:owner")
    assert info.done and "agent 'kirocrew' not permitted" in info.error, info.error
    manager._run.assert_not_called()
    cli_profile(_agents("researcher", "kirocrew"), surface="dashboard")
    info = manager.spawn("task", parent_session_key="dashboard:owner")
    assert not info.error, info.error
    await asyncio.wait_for(manager._tasks[info.id], timeout=10)


class _Claimed(Exception):
    """Raised by the session claim, once the run has named the agent it asks for."""


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_run_naming_no_agent_runs_the_agent_its_scope_checked(env, monkeypatch):
    """The scope checks ``kirocrew`` for a run naming no agent, so the run claims
    ``kirocrew`` by name: a warm pool filled for another default agent
    (``agent.default_agent: power``) cannot hand it that agent instead."""
    from test_session_pool import _make_manager, _make_provider

    from kiro_crew.subagent_manager.run import RunEventCoordinator

    manager = _manager(mock.AsyncMock(return_value=True))
    manager._sessions.get_or_create = mock.AsyncMock(side_effect=_Claimed())
    info = manager.spawn("task", parent_session_key="dashboard:owner")
    assert not info.error, info.error
    await asyncio.wait_for(manager._tasks[info.id], timeout=10)
    assert info.execution_context.template_id == ""  # nothing named an agent

    async def _written(self, _info, writer):
        await writer

    monkeypatch.setattr(RunEventCoordinator, "_await_identity_write", _written)
    monkeypatch.setattr(RunEventCoordinator, "_write_run_agent", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        manager, "_sharing_plan", lambda _info: subagent._SharingPlan("", "", False)
    )
    with pytest.raises(_Claimed):
        await manager._run_inner(info, f"subagent:{info.id}")
    claimed = manager._sessions.get_or_create.call_args.kwargs["agent"]
    assert claimed == "kirocrew"
    pool, _ = _make_manager(pool_size=1, pool_agent="power")
    pool._warm_pool.put_nowait((_make_provider(), 0.0))
    assert pool._claim_from_pool(claimed) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_retried_cli_run_is_still_governed_as_the_cli(env, cli_profile) -> None:
    """The origin rides the run's execution record, so a retry keeps its binding."""
    cli_profile({"enabled": True})
    manager = _manager(mock.AsyncMock(return_value=True))  # approved; ``_run`` is a double
    env.state.subagents = manager
    response = await messaging.api_spawn(_owner(env, {"task": "t"}, cli_token=True))
    old = manager._agents[json.loads(response.text)["id"]]
    await asyncio.wait_for(manager._tasks[old.id], timeout=10)
    old.done, old.error = True, "boom"  # a failed run, the only kind retry restarts
    cli_profile({"enabled": False})
    req = make_request(
        env.state,
        f"/api/spawn/{old.id}/retry",
        body={},
        owner=True,
        match_info={"agent_id": old.id},
    )
    response = await messaging.api_spawn_retry(req)
    assert response.status == 400 and "spawn refused by governance" in response.text


def test_admission_and_the_run_time_recheck_name_the_same_agent() -> None:
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    template = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    member = ExecutionContext(
        None, MemoryStoreRef("default"), "member", "kirocrew", selection_name="alice"
    )
    unnamed = ExecutionContext(None, MemoryStoreRef("default"), "template", "")
    assert subagent.spawn_policy_agents("", "bob", template) == ("bob",)
    assert subagent.spawn_policy_agents("researcher", "", template) == ("researcher",)
    assert subagent.spawn_policy_agents("", "", member) == ("alice",)
    assert subagent.spawn_policy_agents("", "", template) == ("kirocrew",)
    # A spawn naming a template AND a member answers for both.
    assert subagent.spawn_policy_agents("researcher", "alice", template) == (
        "researcher",
        "alice",
    )
    # A parent with no execution record names no template: the default runs.
    assert subagent.spawn_policy_agents("", "", unnamed) == ("kirocrew",)
    denials = {"researcher": "agent 'researcher' not permitted by spawn policy"}
    with mock.patch.object(
        subagent, "_vet_spawn_governance", side_effect=lambda _k, name, app="": denials.get(name)
    ):
        assert subagent._vet_spawn_policy("k", ("researcher", "alice")) == denials["researcher"]
        assert subagent._vet_spawn_policy("k", ("alice",)) is None
    assert subagent.spawn_governance_key("", CLI_ORIGIN) == "cli_chat"
    assert subagent.spawn_governance_key("dashboard:a", "") == "dashboard:a"
    # A continuation of a CLI run that names a parent is asked for by that parent.
    assert subagent.spawn_governance_key("dashboard:a", CLI_ORIGIN) == "dashboard:a"


class _PastGovernance(Exception):
    """Raised by the first step after ``run.py``'s run-time governance check."""


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_the_run_time_recheck_uses_the_admission_key(env, cli_profile, monkeypatch) -> None:
    """``run.py`` re-checks the inherited agent before it runs, under the key
    admission used, so a ``surface:cli`` agent scope written after admission
    still stops the default agent."""
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    cli_profile({"enabled": True})
    manager = _manager(mock.AsyncMock(return_value=True))
    env.state.subagents = manager
    response = await messaging.api_spawn(_owner(env, {"task": "t"}, cli_token=True))
    info = manager._agents[json.loads(response.text)["id"]]
    await asyncio.wait_for(manager._tasks[info.id], timeout=10)

    async def _stop(self, _info, writer):
        writer.cancel()
        raise _PastGovernance()

    monkeypatch.setattr(RunEventCoordinator, "_await_identity_write", _stop)
    monkeypatch.setattr(RunEventCoordinator, "_write_run_agent", staticmethod(lambda *a, **k: None))
    cli_profile(_agents("researcher"))
    with pytest.raises(RuntimeError, match="spawn refused by governance"):
        await manager._run_inner(info, f"subagent:{info.id}")


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_a_named_agent_is_rechecked_when_its_run_starts(cli_profile, monkeypatch) -> None:
    """A spawn that names its agent passed admission; a scope tightened while it
    waited for approval still stops it before a provider is allocated."""
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    cli_profile(_agents("researcher"), surface="dashboard")
    manager = _manager(mock.AsyncMock(return_value=True))
    with mock.patch.object(subagent, "_validate_agent", side_effect=lambda n, c: (n, "", "")):
        info = manager.spawn("t", parent_session_key="dashboard:owner", agent="researcher")
    assert info is not None and not info.error, info.error
    await asyncio.wait_for(manager._tasks[info.id], timeout=10)

    async def _stop(self, _info, writer):
        writer.cancel()
        raise _PastGovernance()

    monkeypatch.setattr(RunEventCoordinator, "_await_identity_write", _stop)
    monkeypatch.setattr(RunEventCoordinator, "_write_run_agent", staticmethod(lambda *a, **k: None))
    with pytest.raises(_PastGovernance):  # still permitted: the check lets it through
        await manager._run_inner(info, f"subagent:{info.id}")
    cli_profile(_agents("other-worker"), surface="dashboard")
    with pytest.raises(RuntimeError, match="spawn refused by governance"):
        await manager._run_inner(info, f"subagent:{info.id}")


# ── end to end: the CLI against the real middleware and the shared route table ──


def test_both_servers_serve_the_owner_mint() -> None:
    """The headless API server registers the shared routes too, the mint among them."""
    import inspect

    app = web.Application()
    server._register_mcp_routes(app)
    paths = {getattr(r, "canonical", "") for r in app.router.resources()}
    assert "/api/token/local" in paths and "/api/spawn" in paths
    assert "_register_mcp_routes(app)" in inspect.getsource(server.start_api_server)
    assert "_register_mcp_routes(app)" in inspect.getsource(server.start_dashboard)


def _owner_bootstrap_holds_here() -> bool:
    """Whether this host verifies the test process as the local owner (it does not
    inside a macOS Seatbelt sandbox, nor off Linux/macOS/Windows)."""
    from kiro_crew import member_memory_auth

    return bool(
        platform_compat.get_process_start_id(os.getpid())
        and member_memory_auth._verified_host_process(os.getpid())
    )


def _raw_unix_post(path: str, body: bytes, headers: dict[str, str]) -> bytes:
    """One HTTP exchange over AF_UNIX, read to EOF, bounded and always closed."""
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(5)
    try:
        raw.connect(path)
        lines = ["POST /api/spawn HTTP/1.1", "Host: 127.0.0.1:9", "Connection: close"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        lines += [f"Content-Length: {len(body)}"]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        reply = b""
        while chunk := raw.recv(65536):
            reply += chunk
        return reply
    finally:
        raw.close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the dashboard socket is POSIX-only")
@pytest.mark.asyncio
async def test_kirocrew_spawn_end_to_end(env, short_sock_dir, monkeypatch, capsys) -> None:
    """Mint, spawn and list through the real middleware, the real owner bootstrap and
    the real peer check, on the route table both servers share; then a clock jump
    and a logout, each answered by one fresh mint."""
    if not _owner_bootstrap_holds_here():
        pytest.skip("this host does not verify the test process as the local owner")
    secret = "test-local-secret"
    spawn = _spawn_recorder(env)
    env.state.subagents.all_agents = []
    app = web.Application()
    app["state"] = env.state
    app["local_secret"] = secret
    app.middlewares.append(
        token_auth.token_auth_middleware(
            internal_paths=server._STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=server._MIXED_INTERNAL_API_PATHS,
            internal_secret=secret,
        )
    )
    server._register_mcp_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    sock_path = short_sock_dir / "dash.sock"
    await web.UnixSite(runner, str(sock_path)).start()
    run_marker.write_marker(9)
    monkeypatch.setattr(owner_gateway_client, "read_local_secret", lambda _port: secret)
    monkeypatch.setattr(owner_gateway_client, "dashboard_socket_path", lambda _port: sock_path)
    loop = asyncio.get_running_loop()

    async def _cli(args) -> None:
        await asyncio.wait_for(loop.run_in_executor(None, cli_commands._spawn, args), timeout=30)

    try:
        await _cli(_run(9, fire_and_forget=True))
        assert "Spawned subagent run-1: hello" in capsys.readouterr().out
        assert spawn.call_args.kwargs["_execution_context"]["origin"] == CLI_ORIGIN

        held = cli_commands._SpawnGateway(9)
        await loop.run_in_executor(None, held.call, "/api/spawn")
        clock = {"skew": 0.0}
        real_time = token_auth.time.time
        stub_module_clock(monkeypatch, token_auth, "time", time=lambda: real_time() + clock["skew"])
        clock["skew"] = 3600.0  # a suspended laptop: the gateway's wall clock moved on
        assert await loop.run_in_executor(None, held.call, "/api/spawn") == {"agents": []}
        token_auth.revoke_all_sessions()  # a logout
        assert await loop.run_in_executor(None, held.call, "/api/spawn") == {"agents": []}

        # The CLI's old shape over the same socket: the internal secret and a caller
        # name, no owner token. The middleware admits the secret; the handler sees
        # an identity-less internal caller, which starts no parentless run.
        reply = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                _raw_unix_post,
                str(sock_path),
                b'{"task": "t"}',
                {
                    "Content-Type": "application/json",
                    "X-Internal-Secret": secret,
                    "X-Internal-Caller": "kirocrew-cli",
                },
            ),
            timeout=30,
        )
        assert b" 409 " in reply.split(b"\r\n", 1)[0] and b"member_identity_unavailable" in reply
        assert spawn.call_count == 1
    finally:
        await runner.cleanup()
