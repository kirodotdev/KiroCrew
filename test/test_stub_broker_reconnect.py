"""Integration test: a session must survive the MCP broker going away.

Every other bridge test drives ``run_bridge`` in-process with an injected
socket, which can prove how the stub REACTS to a dead peer but not what happens
to the session afterwards. That leaves the property this module exists for
unasserted: *after the broker comes back, can the session still call its
servers?*

The answer used to be no, and the failure was silent and permanent. A session's
MCP toolset is frozen at ``session/new``, so the tools stayed listed and simply
failed for the rest of the session's life; the only recovery was opening a new
one. Two shapes, neither observable from a "nothing errored" assertion:

* with a call in flight, the liveness monitor failed it with ``-32603`` and a
  message telling the caller to retry -- advice that could never succeed,
  because nothing reconnected;
* with the bridge idle, the socket simply EOF'd and the stub exited with no
  error frame at all.

Both are reachable by ordinary operation rather than by a crash: the daemon's
own supervisor respawns it, so any broker restart did this to every attached
session.

So this file counts the property from the OUTSIDE. It runs a REAL daemon, a REAL
stub process, stops the daemon the way an operator restart does (its stop event),
starts a fresh one on the same endpoint, and then asks the same stub for another
tool call. A closed-box round trip survives refactors of the reconnect internals
in a way that reading the stub's private state would not.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import transport

_FAKE_SERVER = Path(__file__).with_name("fake_pool_mcp_server.py")

#: Generous on purpose: this bounds a stub reconnect plus a Windows named-pipe
#: round trip plus a backend spawn. A tight value here would read as "reconnect
#: broke" on a slow runner.
_REPLY_TIMEOUT = 60.0

#: How long to wait for a freshly started daemon to bind its endpoint.
_BIND_TIMEOUT = 10.0

#: The verdict when the stub loses its servers on a broker restart -- the
#: process exits, its toolset frozen at session/new, so tools stay listed
#: while every later call fails with no way back short of a new session.
SERVERS_LOST = (
    "the stub process exited when the broker went away, so this "
    "session lost those MCP servers permanently -- its toolset is "
    "frozen at session/new, so the tools stay listed and every "
    "later call fails with no way back short of a new session"
)

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def _clean_env() -> dict[str, str]:
    # Drop any inherited channel id: it feeds PoolKey.channel_id, and a value
    # leaking in from the developer's shell would silently change partitioning.
    return {k: v for k, v in os.environ.items() if k != "KIROCREW_CHANNEL_ID"}


def _init_frame(req_id: int) -> str:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "reconnect-integ", "version": "0.0.0"},
                },
            }
        )
        + "\n"
    )


def _tool_frame(req_id: int) -> str:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }
        )
        + "\n"
    )


async def _spawn_stub(
    *,
    socket_path: Path,
    work_dir: Path,
    home: Path,
    session_key: str,
) -> asyncio.subprocess.Process:
    """Launch a REAL stub process, exactly as the rewriter's overlay would."""
    env = {**_clean_env(), "KIROCREW_HOME": str(home)}
    env["KIROCREW_SESSION_KEY"] = session_key
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.mcp_gateway.stub",
        "--server",
        "fake",
        "--agent",
        "probe",
        "--target-command",
        sys.executable,
        "--target-args",
        str(_FAKE_SERVER),
        "--work-dir",
        str(work_dir),
        "--socket",
        str(socket_path),
        "--sandbox-mode",
        "off",
        "--approval-mode",
        "auto",
        "--poolable",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Its own directory, not pytest's: a child inheriting the repository CWD
        # can leave a relative artifact behind in the checkout.
        cwd=str(work_dir),
        # KIROCREW_HOME redirects the stub's fallback audit log into the test's
        # own tree, so a degradation is observable instead of landing in the
        # developer's real home.
        env=env,
    )


async def _read_reply_id(proc: asyncio.subprocess.Process, req_id: int) -> dict:
    """Read (only) the reply matching ``req_id`` from the stub's stdout.

    Split out from :func:`_drive` so a caller that has already written a frame
    can wait for its reply without re-writing it -- the post-restart tools/call
    is sent exactly once and then raced against the process dying, so writing
    it here too would duplicate the frame and inflate the observed caller count.
    """
    assert proc.stdout is not None
    stdout = proc.stdout

    async def _read_reply() -> dict:
        while True:
            line = await stdout.readline()
            if not line:
                raise AssertionError(f"stub closed stdout before replying to id={req_id}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                return msg

    return await asyncio.wait_for(_read_reply(), timeout=_REPLY_TIMEOUT)


async def _drive(proc: asyncio.subprocess.Process, frame: str, req_id: int) -> dict:
    """Write one JSON-RPC frame through the stub and read the matching reply."""
    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin
    stdin.write(frame.encode("utf-8"))
    await stdin.drain()
    return await _read_reply_id(proc, req_id)


def _resolver_for(
    launch_log: Path, caller_log: Path, work_dir: Path
):  # noqa: ANN202 - matches gatewayd's target_resolver shape
    def _resolver(_key: object) -> tuple[str, list[str], dict[str, str], str]:
        return (
            sys.executable,
            [str(_FAKE_SERVER), str(launch_log), str(caller_log)],
            {},
            str(work_dir),
        )

    return _resolver


def _observed_callers(log: Path) -> list[str]:
    """The session key each ``tools/call`` reached the backend carrying."""
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


async def _start_daemon(sock: Path, resolver) -> tuple[asyncio.Event, asyncio.Task]:
    """Start one gatewayd generation and wait for its endpoint to be live."""
    stop = asyncio.Event()
    task = asyncio.create_task(
        gw.run_gatewayd(
            socket_path=sock,
            max_backends=8,
            idle_timeout_secs=300,
            stop_event=stop,
            target_resolver=resolver,
            prewarm_count=0,
        )
    )
    deadline = asyncio.get_running_loop().time() + _BIND_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if transport.endpoint_exists(sock):
            return stop, task
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=30)
    raise AssertionError("gatewayd never bound its endpoint")


async def _stop_daemon(stop: asyncio.Event, task: asyncio.Task) -> None:
    """Stop a generation the way an operator restart does."""
    stop.set()
    await asyncio.wait_for(task, timeout=60)


async def _reap(procs: list[asyncio.subprocess.Process]) -> None:
    for p in procs:
        if p.returncode is None:
            try:
                await pc.kill_process_tree_async(p.pid, pc.SIGKILL)
            except Exception:  # noqa: BLE001 - teardown must never mask a failure
                pass
    for p in procs:
        try:
            await asyncio.wait_for(p.wait(), timeout=15)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


@pytest.mark.asyncio
async def test_session_survives_a_broker_restart(tmp_path: Path, short_sock_dir) -> None:
    """THE reconnect assertion: restart the broker, the session keeps working.

    The stub is asked for a tool call before the restart (proving the chain was
    live), then for another one after a fresh daemon owns the same endpoint. A
    stub with no reconnect path fails the second one -- and fails it by having
    already exited, which is why the liveness of the process is asserted first:
    it names the defect without waiting out a round-trip timeout.
    """
    sock = Path(short_sock_dir) / "gw.sock"
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    launch_log = tmp_path / "launches.txt"
    caller_log = tmp_path / "callers.txt"
    resolver = _resolver_for(launch_log, caller_log, work_dir)

    stop, daemon = await _start_daemon(sock, resolver)
    procs: list[asyncio.subprocess.Process] = []
    second_stop: asyncio.Event | None = None
    second_daemon: asyncio.Task | None = None
    try:
        proc = await _spawn_stub(
            socket_path=sock,
            work_dir=work_dir,
            home=home,
            session_key="dashboard:reconnect",
        )
        procs.append(proc)

        reply = await _drive(proc, _init_frame(1), req_id=1)
        assert "result" in reply, f"no initialize result before restart: {reply}"
        reply = await _drive(proc, _tool_frame(2), req_id=2)
        assert "result" in reply, f"no tools/call result before restart: {reply}"

        # The broker goes away. This is the supervisor-respawn / operator-restart
        # path, not a crash: the daemon drains and exits through its stop event.
        await _stop_daemon(stop, daemon)
        daemon = None  # type: ignore[assignment]

        second_stop, second_daemon = await _start_daemon(sock, resolver)

        assert proc.stdin is not None
        # Write the post-restart call ONCE. If the stub already lost its bridge
        # on the broker's departure its stdin is closed and the write raises --
        # that is the servers-lost defect itself, named here instead of
        # surfacing as a bare pipe error. OSError is the superclass of
        # ConnectionResetError/BrokenPipeError AND the bare OSError (winerror
        # 232) a dead-child pipe write raises on Windows, so catch it whole.
        try:
            proc.stdin.write(_tool_frame(3).encode("utf-8"))
            await proc.stdin.drain()
        except OSError as exc:
            raise AssertionError(SERVERS_LOST) from exc

        # Wait event-driven for whichever happens first: the stub replies (it
        # re-handshook against the new generation and carried the call through)
        # or the stub process exits (it lost its servers permanently). A fixed
        # sleep here would burn the whole ceiling on every success; racing the
        # reply against the exit advances the instant the reconnect completes.
        # The ceiling is _REPLY_TIMEOUT, not a tight 30s: production may spend
        # up to its full reconnect budget (see the note at _REPLY_TIMEOUT), so a
        # tighter ceiling would read a legal slow reconnect as a hang. It costs
        # nothing on the success path, which advances the instant the reply
        # lands.
        #
        # The id=3 frame is written exactly once above. The one case that sends
        # a second frame is the gateway-restart race below, and it is counted.
        expected_callers = 2
        next_id = 3
        while True:
            reply_task = asyncio.create_task(_read_reply_id(proc, next_id))
            exit_task = asyncio.create_task(proc.wait())
            done, pending = await asyncio.wait(
                {reply_task, exit_task},
                timeout=_REPLY_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            reply_exc = reply_task.exception() if reply_task in done else None

            if reply_task in done and reply_exc is None:
                reply = reply_task.result()
            elif exit_task in done:
                # The process exited: the stub lost those MCP servers
                # permanently -- its toolset is frozen at session/new, so the
                # tools stay listed and every later call fails with no way back
                # short of a new session.
                raise AssertionError(SERVERS_LOST)
            elif reply_exc is not None:
                # The read failed with a connection error while the process is
                # still alive: the stub tore down the bridge that served these
                # servers, which is the same servers-lost outcome for the
                # session even though the process itself lingers.
                raise AssertionError(SERVERS_LOST) from reply_exc
            else:
                raise AssertionError(
                    "the stub neither replied to nor died from the "
                    f"post-restart call within the {_REPLY_TIMEOUT:.0f}s "
                    "ceiling -- the reconnect hung"
                )

            # The gateway-restart race: if id=3 reached the dying bridge before
            # the peer death was noticed, the stub fails it RETRYABLY rather
            # than carrying it, exactly as it is designed to ("Gateway
            # restarted; ... Retry it."). That is not a lost session -- retry
            # once with a fresh id; the retry produces the one post-restart
            # caller line the errored frame never did.
            if "error" in reply and next_id == 3:
                # The errored id=3 was failed AT THE STUB, never carried to the
                # backend, so it wrote no caller line; the retry produces the
                # single post-restart line. The expected count is unchanged.
                next_id = 4
                assert proc.stdin is not None
                try:
                    proc.stdin.write(_tool_frame(next_id).encode("utf-8"))
                    await proc.stdin.drain()
                except OSError as exc:
                    raise AssertionError(SERVERS_LOST) from exc
                continue
            break

        assert "result" in reply, f"the stub did not carry a call to the restarted broker: {reply}"

        # A reconnect that reopened the socket but did not re-register would
        # carry an empty caller block, and every session-scoped path in the
        # backend would silently fall back to unattached behaviour.
        assert _observed_callers(caller_log) == (["dashboard:reconnect"] * expected_callers), (
            "the post-restart call did not reach the backend carrying this "
            f"session's identity: {_observed_callers(caller_log)!r}"
        )
    finally:
        await _reap(procs)
        if second_stop is not None and second_daemon is not None:
            await _stop_daemon(second_stop, second_daemon)
        if daemon is not None:
            await _stop_daemon(stop, daemon)


# --- The branches the round trip above cannot reach -------------------------
# A reconnect that carried on regardless would be worse than the outage it
# fixes, and a reconnect loop with no floor would replace a terminal exit with
# an unbounded one. Neither shows up in a happy-path round trip, so both are
# pinned directly.


class _CaptureWriter:
    """Minimal ``asyncio.StreamWriter`` stand-in for the gateway socket."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._mc_write_lock = asyncio.Lock()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _reader_with(*frames: dict) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
    reader.feed_eof()
    return reader


def _session_with_captured_init(init_result: dict | None) -> object:
    from kiro_crew.mcp_gateway.stub import StubSession

    session = StubSession()
    session.captured_init = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        + "\n"
    ).encode("utf-8")
    session.init_result = init_result
    return session


_SERVER_RESULT = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "fake", "version": "1"},
}


@pytest.mark.asyncio
async def test_replay_accepts_a_generation_that_answers_identically() -> None:
    """The ordinary case: same server, so the session may carry on."""
    from kiro_crew.mcp_gateway.stub import _REPLAY_OK, _replay_initialize

    session = _session_with_captured_init(_SERVER_RESULT)
    status, forward, detail = await _replay_initialize(
        _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)}),
        _CaptureWriter(),  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
    )
    assert (status, forward, detail) == (_REPLAY_OK, None, "result_matched")


@pytest.mark.asyncio
async def test_replay_refuses_a_generation_that_answers_differently() -> None:
    """A moved-on daemon must NOT be passed off as the same server.

    The session's toolset was frozen at ``session/new`` against the first
    answer. If a new generation resolves this server to something else -- a
    different binary, a changed config -- then reconnecting would leave the
    session calling tools that are no longer the ones it was offered. Answering
    wrongly is worse than the terminal exit, so this fails closed. It is also
    REFUSE rather than RETRY: that generation owns the endpoint, so the next
    attempt would get the same answer.
    """
    from kiro_crew.mcp_gateway.stub import _REPLAY_REFUSE, _replay_initialize

    moved_on = {**_SERVER_RESULT, "serverInfo": {"name": "other", "version": "9"}}
    session = _session_with_captured_init(_SERVER_RESULT)
    status, forward, detail = await _replay_initialize(
        _reader_with({"jsonrpc": "2.0", "id": 7, "result": moved_on}),
        _CaptureWriter(),  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
    )
    assert status == _REPLAY_REFUSE
    assert forward is None
    assert detail == "handshake_result_changed"


@pytest.mark.asyncio
async def test_a_connection_lost_during_replay_is_retryable_not_terminal() -> None:
    """Losing the connection mid-replay says nothing about the answer.

    This is the likeliest shape of the very event the reconnect exists for: a
    supervisor respawn, where the daemon reached first may still be starting up
    or may itself die again. Treating it as terminal would spend the session in
    exactly the case the budget was bought for.
    """
    from kiro_crew.mcp_gateway.stub import _REPLAY_RETRY, _replay_initialize

    closed = asyncio.StreamReader()
    closed.feed_eof()
    session = _session_with_captured_init(_SERVER_RESULT)
    status, forward, detail = await _replay_initialize(
        closed,
        _CaptureWriter(),  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
    )
    assert status == _REPLAY_RETRY, (
        f"a mid-replay transport loss was classified {status!r} ({detail}), so "
        "the reconnect gives up on a daemon that was merely still starting"
    )
    assert forward is None


@pytest.mark.asyncio
async def test_replay_forwards_the_answer_a_session_never_received() -> None:
    """A daemon that died mid-handshake leaves kiro-cli still waiting.

    There is no earlier answer to compare against and no duplicate to avoid, so
    the replayed reply is the one the caller is owed and must reach it.
    """
    from kiro_crew.mcp_gateway.stub import _REPLAY_OK, _replay_initialize

    session = _session_with_captured_init(None)
    status, forward, detail = await _replay_initialize(
        _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)}),
        _CaptureWriter(),  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
    )
    assert status == _REPLAY_OK
    assert forward is not None and b'"id":7' in forward
    assert detail == "forwarded_first_answer"


@pytest.mark.asyncio
async def test_reconnect_retries_past_a_daemon_that_dies_mid_replay(
    monkeypatch,
) -> None:
    """The budget must actually be spent on the case it was bought for.

    First attempt: the handshake succeeds and the connection then dies during the
    replay -- a daemon still starting, or one that died again. Second attempt
    answers. Giving up after the first would lose the session to the very event
    the reconnect exists to survive.
    """
    from kiro_crew.mcp_gateway import stub as stub_mod

    monkeypatch.setattr(stub_mod, "_RECONNECT_BACKOFF_START_SECS", 0.01)
    attempts: list[int] = []
    seen_uuids: list[str] = []

    async def _hs(_socket_path: str, _payload: dict):
        attempts.append(1)
        seen_uuids.append(_payload["stub_uuid"])
        if len(attempts) == 1:
            dead = asyncio.StreamReader()
            dead.feed_eof()
            reader = dead
        else:
            reader = _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)})
        return (
            reader,
            _CaptureWriter(),
            "stub-uuid",
            {
                "type": "registered",
                "capabilities": ["poolable_ack"],
            },
        )

    monkeypatch.setattr(stub_mod, "handshake", _hs)
    session = _session_with_captured_init(_SERVER_RESULT)
    attached = await stub_mod._reconnect(
        "unused",
        {"stub_uuid": "u", "session_key": "dashboard:x"},
        session,  # type: ignore[arg-type]
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )
    assert attached is not None, (
        "the reconnect gave up after one lost replay, so a daemon that was "
        "merely still starting costs the session its servers"
    )
    assert len(attempts) == 2
    assert len(set(seen_uuids)) == 2, (
        f"both handshake attempts registered under {seen_uuids!r}: a retry that "
        "reuses a live registration handle lets the predecessor's teardown "
        "remove the replacement's backend"
    )
    assert attached[3] == seen_uuids[-1], (
        "the reconnect reported a different uuid than the one it registered, so "
        "the audit record names a registration that never existed"
    )


@pytest.mark.asyncio
async def test_reconnect_gives_up_within_its_budget(tmp_path: Path, monkeypatch) -> None:
    """A gateway that never comes back must reach the terminal exit.

    Retrying is what survives an ordinary supervisor respawn; retrying forever
    would replace "this session lost its servers" with a stub that never exits
    and never tells kiro-cli the server is done.
    """
    from kiro_crew.mcp_gateway import stub as stub_mod

    monkeypatch.setattr(stub_mod, "_RECONNECT_TOTAL_BUDGET_SECS", 0.4)
    monkeypatch.setattr(stub_mod, "_RECONNECT_BACKOFF_START_SECS", 0.05)
    monkeypatch.setattr(stub_mod, "_RECONNECT_BACKOFF_MAX_SECS", 0.1)

    session = _session_with_captured_init(_SERVER_RESULT)
    started = asyncio.get_running_loop().time()
    attached = await stub_mod._reconnect(
        str(tmp_path / "definitely-not-bound.sock"),
        {"stub_uuid": "u", "session_key": "dashboard:x"},
        session,  # type: ignore[arg-type]
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert attached is None
    assert elapsed < 10.0, (
        f"reconnect ran {elapsed:.1f}s against a 0.4s budget, so the budget is "
        "not what bounds it"
    )


@pytest.mark.asyncio
async def test_a_closed_stdin_is_not_a_reconnectable_ending() -> None:
    """kiro-cli closing stdin is a shutdown, not an outage.

    Treating it as reconnectable would make every normal exit spend the whole
    reconnect budget re-attaching a bridge with nobody left to serve.
    """
    from kiro_crew.mcp_gateway.stub import StubSession, run_bridge

    session = StubSession()
    stdin = asyncio.StreamReader()
    stdin.feed_eof()
    gw_reader = asyncio.StreamReader()
    stdout_target = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    stdout_writer = asyncio.StreamWriter(
        _NullTransport(),
        asyncio.StreamReaderProtocol(stdout_target),
        stdout_target,
        loop,
    )

    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _CaptureWriter(),  # type: ignore[arg-type]
            asyncio.Event(),
            stdin=stdin,
            stdout_writer=stdout_writer,
            session=session,
        ),
        timeout=10,
    )
    assert session.reason == "stdin_eof"
    assert session.reason not in StubSession.RECONNECTABLE


# --- A reconnect must not silently drop resource subscriptions --------------
# The daemon's subscription table lives in its process. Replaying only
# ``initialize`` would return a connection that answers calls while resource
# updates never arrive again -- a quiet degradation where there used to be a
# visible one, which is the opposite of what this fix is for. A subscribed
# session is refused the reconnect until replaying subscriptions is done
# properly.


def _session_after(*frames: dict) -> object:
    from kiro_crew.mcp_gateway.stub import StubSession

    session = StubSession()
    for frame in frames:
        line = (json.dumps(frame) + "\n").encode("utf-8")
        session.note_outbound(line, frame)
    return session


def test_a_subscribed_session_is_not_offered_a_reconnect() -> None:
    session = _session_after(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/subscribe",
            "params": {"uri": "file:///a"},
        }
    )
    assert session.has_live_subscriptions() is True  # type: ignore[attr-defined]


def test_unsubscribing_does_not_make_a_session_reconnectable_again() -> None:
    """An unsubscribe is only a REQUEST, so it cannot clear the refusal.

    A server that rejected the unsubscribe leaves the subscription live upstream
    while the stub had already forgotten it -- and the reconnect would then be
    allowed for a session whose resource updates are about to stop with nothing
    said. Confirming it would need per-id response tracking, which is more
    machinery than the recovery is worth while replaying subscriptions is still
    a follow-up. So the latch never releases.
    """
    session = _session_after(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/subscribe",
            "params": {"uri": "file:///a"},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/unsubscribe",
            "params": {"uri": "file:///a"},
        },
    )
    assert session.has_live_subscriptions() is True  # type: ignore[attr-defined]


def test_a_subscribe_with_an_unreadable_uri_still_counts() -> None:
    """Failing to parse the uri must not read as "no subscription"."""
    session = _session_after(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/subscribe",
            "params": None,
        }
    )
    assert session.has_live_subscriptions() is True  # type: ignore[attr-defined]


def test_an_ordinary_session_holds_no_subscriptions() -> None:
    session = _session_after(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "noop"}},
    )
    assert session.has_live_subscriptions() is False  # type: ignore[attr-defined]


class _NullTransport(asyncio.Transport):
    """Swallows writes; the stdout side is not what these cases assert on."""

    def write(self, data: object) -> None:  # noqa: D102
        pass

    def is_closing(self) -> bool:  # noqa: D102
        return False

    def close(self) -> None:  # noqa: D102
        pass


# --- Answer each abandoned request exactly once ------------------------------
# Two responses for one id is a protocol violation, and "an error, then a
# success" for the same id is worse than either alone. Both were reachable: a
# failed reconnect errored ids the loop had already errored, and an unanswered
# initialize could be errored here and then answered for real by the replay.


def test_abandoned_calls_are_failed_once_when_no_replay_can_answer_them() -> None:
    from kiro_crew.mcp_gateway.stub import StubSession, _split_abandoned_ids

    session = StubSession()
    session.outstanding_ids = [11, 12]
    to_fail, deferred = _split_abandoned_ids(session)
    assert to_fail == [11, 12]
    assert deferred is None


def test_an_unanswered_initialize_is_deferred_to_the_replay() -> None:
    """Erroring it here would contradict a replay that answers it for real."""
    from kiro_crew.mcp_gateway.stub import _split_abandoned_ids

    session = _session_with_captured_init(None)
    session.outstanding_ids = [7, 12]  # type: ignore[attr-defined]
    to_fail, deferred = _split_abandoned_ids(session)  # type: ignore[arg-type]
    assert to_fail == [12], (
        "the initialize id was failed here, so a successful replay would hand "
        "kiro-cli a second, contradicting response for the same id"
    )
    assert deferred == 7


def test_an_already_answered_initialize_is_not_deferred() -> None:
    """The replay swallows that reply, so nobody downstream would answer it."""
    from kiro_crew.mcp_gateway.stub import _split_abandoned_ids

    session = _session_with_captured_init(_SERVER_RESULT)
    session.outstanding_ids = [7]  # type: ignore[attr-defined]
    to_fail, deferred = _split_abandoned_ids(session)  # type: ignore[arg-type]
    assert to_fail == [7]
    assert deferred is None


# --- A reconnect must re-apply the private-backend check --------------------
# The endpoint a reconnect binds to need not be the generation this stub first
# registered with, and the manager adopts anything answering ``pong`` with no
# version handshake -- so a daemon predating the ``poolable`` field can serve the
# reconnect and silently co-tenant a server the operator never allowlisted.


def _fake_handshake(capabilities: list[str]):  # noqa: ANN202
    async def _hs(_socket_path: str, _payload: dict):
        return (
            _reader_with({"jsonrpc": "2.0", "id": 7, "result": dict(_SERVER_RESULT)}),
            _CaptureWriter(),
            "stub-uuid",
            {"type": "registered", "capabilities": capabilities},
        )

    return _hs


@pytest.mark.asyncio
async def test_reconnect_refuses_a_daemon_that_ignores_the_poolable_field(
    monkeypatch,
) -> None:
    """A private server must never be silently pooled by an older generation."""
    from kiro_crew.mcp_gateway import stub as stub_mod

    monkeypatch.setattr(stub_mod, "handshake", _fake_handshake([]))
    session = _session_with_captured_init(_SERVER_RESULT)
    attached = await stub_mod._reconnect(
        "unused",
        {"stub_uuid": "u", "session_key": "dashboard:x"},
        session,  # type: ignore[arg-type]
        asyncio.Event(),
        poolable=False,
        pool_label="probe:fake",
    )
    assert attached is None, (
        "the reconnect accepted a registration from a daemon that does not "
        "advertise poolable_ack, so this unshareable server is now co-tenanted"
    )


@pytest.mark.asyncio
async def test_reconnect_accepts_that_daemon_when_sharing_was_requested(
    monkeypatch,
) -> None:
    """A stub that ASKED to share needs no attestation: pooling is what it wanted."""
    from kiro_crew.mcp_gateway import stub as stub_mod

    monkeypatch.setattr(stub_mod, "handshake", _fake_handshake(["poolable_ack"]))
    session = _session_with_captured_init(_SERVER_RESULT)
    attached = await stub_mod._reconnect(
        "unused",
        {"stub_uuid": "u", "session_key": "dashboard:x"},
        session,  # type: ignore[arg-type]
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )
    assert attached is not None
