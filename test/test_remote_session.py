"""Tests for the remote session transport (wire protocol, guest worker, gateway link).

Everything runs on loopback in this process: the worker listens on port 0, its
child is a tiny Python echo, and an in-process proxy injects the tunnel faults a
port-forwarding tunnel can introduce (a trailing LF on a large write turned
into CR) plus random bit flips and dropped connections. Every task and child is
stopped in a ``finally``.
"""

from __future__ import annotations

import asyncio
import functools
import os
import random
import sys

import pytest

from kiro_crew.remote_session import link as link_mod
from kiro_crew.remote_session import wire
from kiro_crew.remote_session import worker as worker_mod
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until, await_until, wait_until

SID = "sess-1"
ECHO = [
    sys.executable,
    "-u",
    "-c",
    "import sys\nfor l in sys.stdin.buffer:\n sys.stdout.buffer.write(l); sys.stdout.buffer.flush()",
]


def sync(fn):
    """Run an async test body on a fresh event loop (no async pytest plugin here)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


# -- wire unit tests ------------------------------------------------------------------


def _pair(secret: bytes | None = None, worker_secret: bytes | None = None):
    secret = secret or wire.new_secret()
    g, w = wire.Endpoint(wire.GATEWAY), wire.Endpoint(wire.WORKER)
    gs = wire.GatewayHandshake(SID, secret)
    ws = wire.WorkerHandshake(SID, worker_secret or secret)
    join = gs.on_hello(ws.hello().strip(), g.recv_next)
    w_att, welcome = ws.on_join(join.strip(), w.recv_next)
    g_att = gs.on_welcome(welcome.strip())
    assert g.attach(g_att, 0.0) == b"" and w.attach(w_att, 0.0) == b""
    return g, w


def test_roundtrip_and_cr_terminator():
    g, w = _pair()
    frames = g.write(b"hello\n" * 10, 0.0)
    data, _, _ = w.feed(frames.replace(b"\n", b"\r"), 0.0)
    assert data == b"hello\n" * 10


def test_large_payload_is_pieced_and_reassembled(seeded_rng):
    g, w = _pair()
    payload = seeded_rng.randbytes(200_000)
    frames = g.write(payload, 0.0)
    assert frames.count(b"\n") == -(-len(payload) // wire.PIECE_BYTES)
    assert w.feed(frames, 0.0)[0] == payload


def test_wrong_secret_is_refused_both_ways():
    with pytest.raises(wire.AuthError):
        _pair(worker_secret=wire.new_secret())


def test_reflected_and_foreign_frames_are_rejected():
    g, w = _pair()
    frame = g.write(b"x\n", 0.0)
    # Reflected back at its sender: wrong direction label.
    assert g.feed(frame, 0.0)[0] == b"" and g.stats["bad"] == 1
    # Replayed onto a different connection: wrong connection key.
    _, other = _pair()
    assert other.feed(frame, 0.0)[0] == b"" and other.stats["bad"] == 1


def test_tampered_frame_is_nacked_then_resent():
    g, w = _pair()
    frame = bytearray(g.write(b"payload\n", 0.0))
    frame[-3] ^= 0x01
    data, out, _ = w.feed(bytes(frame), 0.0)
    assert data == b"" and out.startswith(b"N 0 ")
    data, _, _ = w.feed(g.feed(out, 0.0)[1], 0.0)
    assert data == b"payload\n"


def test_duplicate_is_dropped_and_gap_is_nacked():
    g, w = _pair()
    f0 = g.write(b"a\n", 0.0)
    f1 = g.write(b"b\n", 0.0)
    assert w.feed(f1, 0.0)[1].startswith(b"N 0 ")
    assert w.feed(f0, 0.0)[0] == b"a\n"
    assert w.feed(f0, 0.0)[0] == b"" and w.stats["dup"] == 1


def test_unterminated_flood_is_bounded():
    _, w = _pair()
    w.feed(b"D " + b"A" * (wire.MAX_LINE_BYTES + 10), 0.0)
    assert w._buf == b"" and w.stats["bad"] == 1


def test_resume_outside_window_is_a_protocol_error():
    g, _ = _pair()
    with pytest.raises(wire.WireError):
        g.attach(wire.Attachment(b"k" * 32, 5), 0.0)


def test_unacked_bound_applies_backpressure():
    g = wire.Endpoint(wire.GATEWAY, max_unacked_bytes=1000)
    assert not g.can_accept(2000)
    with pytest.raises(wire.BufferFull):
        g.write(b"x" * 2000, 0.0)


def test_retransmit_backs_off_and_resends_only_oldest():
    g, _ = _pair()
    g.write(b"a\n", 0.0)
    g.write(b"b\n", 0.0)
    first = g.tick(1.0)
    assert first.count(b"\n") == 1 and first.startswith(b"D 0 ")
    assert g.tick(2.5) == b""  # rto doubled to 2s
    assert g.tick(3.1).startswith(b"D 0 ")


def test_child_env_withholds_credentials():
    env = worker_mod.child_env({"KIRO_API_KEY": "k", "PATH": "/bin", "AWS_SESSION_TOKEN": "t"})
    assert env == {"PATH": "/bin"}


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permissions")
def test_read_secret_requires_private_file_and_unlinks(tmp_path):
    path = tmp_path / "s.key"
    path.write_text(os.urandom(32).hex())
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError):
        worker_mod.read_secret(str(path))
    os.chmod(path, 0o600)
    assert len(worker_mod.read_secret(str(path))) == 32
    assert not path.exists()


# -- integration --------------------------------------------------------------------------


def _ssm_bug(chunk: bytes, rng: random.Random) -> bytes:
    b = bytearray(chunk)
    for i in range(1016, len(b), 1024):
        if b[i] == 0x0A:
            b[i] = 0x0D
    return bytes(b)


def _flips(rate: float):
    def mutate(chunk: bytes, rng: random.Random) -> bytes:
        b = bytearray(chunk)
        for i in range(len(b)):
            if rng.random() < rate:
                b[i] ^= 1 << rng.randrange(8)
        return bytes(b)

    return mutate


class Proxy:
    """Loopback proxy in front of the worker that can mutate or cut traffic."""

    def __init__(self, target: int, mutate=None, *, both_ways: bool = False) -> None:
        self.target, self.mutate, self.both_ways = target, mutate, both_ways
        self.rng = random.Random(3)
        self.conns: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, cr, cw) -> None:
        ur, uw = await asyncio.open_connection("127.0.0.1", self.target)
        self.conns += [cw, uw]

        async def pump(src, dst, mutate):
            try:
                while chunk := await src.read(65536):
                    dst.write(mutate(chunk, self.rng) if mutate else chunk)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst.close()

        await asyncio.gather(
            pump(cr, uw, self.mutate), pump(ur, cw, self.mutate if self.both_ways else None)
        )

    def cut(self) -> None:
        for w in self.conns:
            w.close()
        self.conns.clear()

    def close(self) -> None:
        self.cut()
        self.server.close()


async def _start_worker(secret: bytes, command=ECHO, **kw):
    kw.setdefault("drain_timeout", 2.0)
    w = worker_mod.Worker(SID, secret, command, **kw)
    task = asyncio.ensure_future(w.run("127.0.0.1", 0))
    await async_wait_until(lambda: getattr(w, "port", None), describe=lambda: "worker listener")
    return w, task


async def _stop(w, task, link=None):
    if link is not None:
        await link.aclose()
    if w.child and w.child.returncode is None:
        w.child.kill()
    try:
        await await_until(task, what="guest worker exit")
    except Exception:
        task.cancel()


def _lines(n: int, seed: int = 5) -> list[bytes]:
    rng = random.Random(seed)
    sizes = [rng.randint(1, 3000) for _ in range(n)] + [1017, 2041, 3065, 4089, 100_000]
    return [b'{"p":"' + bytes(rng.choice(b"abcxyz") for _ in range(s)) + b'"}\n' for s in sizes]


async def _echo_all(link, lines, *, between=None):
    for i, line in enumerate(lines):
        await link.write(line)
        got = await asyncio.wait_for(link.stdout.readline(), 30)
        assert got == line, f"line {i} differs"
        if between:
            between(i)


def _link(port: int, secret: bytes, **kw) -> link_mod.RemoteLink:
    async def dial():
        return await asyncio.open_connection("127.0.0.1", port)

    opts = dict(keepalive_seconds=0.5, stall_seconds=3, lease_seconds=10, backoff_initial=0.05)
    opts.update(kw)
    return link_mod.RemoteLink(SID, secret, dial, **opts)


@pytest.mark.parametrize(
    "mutate", [None, _ssm_bug, _flips(2e-5)], ids=["clean", "ssm-bug", "flips"]
)
@sync
async def test_exact_delivery_through_faulty_tunnel(mutate):
    secret = wire.new_secret()
    w, task = await _start_worker(secret)
    proxy = Proxy(w.port, mutate, both_ways=mutate is not _ssm_bug)
    link = _link(await proxy.start(), secret)
    try:
        link.start()
        await link.wait_connected(10)
        await _echo_all(link, _lines(150))
        assert link.state == link_mod.CONNECTED
    finally:
        proxy.close()
        await _stop(w, task, link)


@sync
async def test_survives_repeated_connection_cuts_without_loss_or_duplication():
    secret = wire.new_secret()
    w, task = await _start_worker(secret)
    proxy = Proxy(w.port)
    link = _link(await proxy.start(), secret)
    try:
        link.start()
        await link.wait_connected(10)
        await _echo_all(link, _lines(120), between=lambda i: proxy.cut() if i % 15 == 7 else None)
        assert link.connections > 1
        assert w.child.returncode is None  # the agent process never restarted
    finally:
        proxy.close()
        await _stop(w, task, link)


@sync
async def test_unauthenticated_connection_cannot_hijack_the_session():
    secret = wire.new_secret()
    w, task = await _start_worker(secret)
    link = _link(w.port, secret)
    rejected = asyncio.Event()
    rejection_states = (link_mod.LOST, link_mod.REFUSED, link_mod.RECONNECTING)
    intruder = _link(
        w.port,
        wire.new_secret(),
        on_state=lambda state: rejected.set() if state in rejection_states else None,
    )
    try:
        link.start()
        await link.wait_connected(10)
        intruder.start()
        await asyncio.wait_for(rejected.wait(), 30)
        assert intruder.state in rejection_states
        assert intruder.ep.stats["delivered"] == 0
        await _echo_all(link, _lines(5))
    finally:
        await intruder.aclose()
        await _stop(w, task, link)


@sync
async def test_lease_expiry_marks_session_lost():
    async def dial():
        raise ConnectionRefusedError("no route")

    link = link_mod.RemoteLink(
        SID,
        wire.new_secret(),
        dial,
        keepalive_seconds=0.1,
        stall_seconds=0.2,
        lease_seconds=0.5,
        backoff_initial=0.05,
        backoff_max=0.1,
    )
    link.start()
    try:
        await async_wait_until(lambda: link.state == link_mod.LOST, describe=lambda: link.state)
        assert link.state == link_mod.LOST
        with pytest.raises(link_mod.LinkUnavailable):
            await link.write(b"x\n")
    finally:
        await link.aclose()


@sync
async def test_child_exit_flushes_output_and_finishes_the_link():
    secret = wire.new_secret()
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('bye\\n' * 3000)"]
    w, task = await _start_worker(secret, command=cmd)
    link = _link(w.port, secret)
    try:
        link.start()
        out = await asyncio.wait_for(link.stdout.read(), 30)
        assert out == b"bye\n" * 3000
        assert link.state == link_mod.FINISHED
        assert await await_until(task, what="guest worker exit") == 0
    finally:
        await _stop(w, task, link)


@sync
async def test_retire_is_reported_and_closing_input_ends_the_child():
    secret = wire.new_secret()
    w, task = await _start_worker(secret, retire_grace=10)
    link = _link(w.port, secret)
    try:
        link.start()
        await link.wait_connected(10)
        retiring = asyncio.ensure_future(w.retire("spot-reclaim"))
        await async_wait_until(lambda: link.state == link_mod.RETIRING, describe=lambda: link.state)
        assert link.state == link_mod.RETIRING and link.retire_reason == "spot-reclaim"
        link.close_input()  # the gateway finished its turn and lets the child exit
        await await_until(retiring, what="guest retirement")
        assert w.child.returncode == 0
    finally:
        await _stop(w, task, link)


@sync
async def test_child_does_not_see_api_key(monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "must-not-leak")
    secret = wire.new_secret()
    cmd = [sys.executable, "-c", "import os; print(os.environ.get('KIRO_API_KEY', 'absent'))"]
    w, task = await _start_worker(secret, command=cmd)
    link = _link(w.port, secret)
    try:
        link.start()
        assert (await asyncio.wait_for(link.stdout.read(), 30)).strip() == b"absent"
    finally:
        await _stop(w, task, link)


def test_the_orphan_timeout_is_one_shared_value_every_side_reads():
    """The guest's give-up time, the gateway's launch default and the spare age agree."""
    import inspect

    from kiro_crew.coder import remote_worker
    from kiro_crew.remote_session import link, relay, wire, worker

    lease = inspect.signature(link.RemoteLink.__init__).parameters["lease_seconds"].default
    # A gateway restart reconnects inside the lease, before the worker gives up.
    assert wire.ORPHAN_TIMEOUT_SECONDS > lease
    # A never-attached spare is retired before its own orphan clock can stop it.
    assert wire.SPARE_MAX_AGE_SECONDS < wire.ORPHAN_TIMEOUT_SECONDS
    launch = inspect.signature(remote_worker.RemoteWorkerLaunch.__init__).parameters
    assert launch["orphan_timeout"].default == wire.ORPHAN_TIMEOUT_SECONDS
    guest = inspect.signature(worker.Worker.__init__).parameters
    assert guest["orphan_timeout"].default == wire.ORPHAN_TIMEOUT_SECONDS
    args, _command = worker._parse(["--session-id", "s", "--secret-file", "f", "--", "x"])
    assert args.orphan_timeout == wire.ORPHAN_TIMEOUT_SECONDS
    spare = inspect.signature(relay.WorkspaceRelay.__init__).parameters["spare_max_age"].default
    assert spare == wire.SPARE_MAX_AGE_SECONDS


def test_a_recreated_workspace_is_named_once_on_the_project_line():
    from kiro_crew import session_environment as se
    from kiro_crew.context_assembly.turn import rail_parts

    key = "dashboard:recreated"
    se.remember_remote_workspace(key, "Coder MicroVM", "/workspace/work")
    se.mark_workspace_recreated(key)
    try:

        def lines():
            return "".join(
                rail_parts(
                    project="/Users/me/repo",
                    context_groups=None,
                    board_tags=None,
                    minimal_context=True,
                    folder_path=None,
                    session_key=key,
                    agent=None,
                    request_prefix_context=None,
                )
            )

        first, second = lines(), lines()
    finally:
        se.forget_remote_workspace(key)
        se.take_workspace_recreated(key)
    assert "was recreated empty" in first and "/workspace/work" in first
    assert "was recreated empty" not in second  # said once, not every turn


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("## main...origin/main\n", None),
        (
            "## main...origin/main [ahead 2]\n M a.py\n?? b.txt\n",
            "2 uncommitted files, 2 unpushed commits",
        ),
        ("## main...origin/main [behind 1]\n", None),
        ("## feature\n", "branch feature was never pushed"),
        ("## No commits yet on main\n?? x\n", "1 uncommitted file"),
        ("", None),
    ],
)
def test_unsaved_work_is_read_from_git_status(status, expected):
    from kiro_crew.coder.session_browse import summarize_unsaved

    assert summarize_unsaved(status) == expected


def test_stale_workers_from_another_gateway_process_are_reaped(tmp_path):
    """A crashed gateway's worker is stopped; this process's own live one is not."""
    import subprocess

    from kiro_crew.coder import remote_worker

    sessions = tmp_path / ".kirocrew-worker" / "sessions"
    sessions.mkdir(parents=True)
    epoch = remote_worker._GATEWAY_EPOCH
    stale = subprocess.Popen(["sleep", "60"])
    live = subprocess.Popen(["sleep", "60"])
    try:
        (sessions / "bind-1-gdead00-0001.port").write_text(f"41000 {stale.pid}\n")
        (sessions / "bind-1-gdead00-0001.key").write_text("k")
        (sessions / f"bind-1-g{epoch}-0002.port").write_text(f"41001 {live.pid}\n")
        (sessions / "other-gdead00-0003.port").write_text(f"41002 {UNALLOCATABLE_PID}\n")
        command = remote_worker._reap_stale_command("bind-1", epoch)
        subprocess.run(
            ["/bin/sh", "-c", command],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            check=True,
            timeout=30,
        )
        wait_until(lambda: stale.poll() is not None, describe=lambda: "stale worker exit")
        assert stale.poll() is not None
        assert live.poll() is None
        assert not (sessions / "bind-1-gdead00-0001.port").exists()
        assert not (sessions / "bind-1-gdead00-0001.key").exists()
        assert (sessions / f"bind-1-g{epoch}-0002.port").exists()
        assert (sessions / "other-gdead00-0003.port").exists()  # another binding's
    finally:
        for proc in (stale, live):
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)


def test_worker_ids_carry_this_process_epoch_and_reap_runs_once(tmp_path):
    import asyncio

    from kiro_crew.coder import remote_worker

    calls: list[str] = []

    class Shell:
        async def run(self, command, stdin=b""):
            calls.append(command)
            return ""

    launch = remote_worker.RemoteWorkerLaunch("bind-once", Shell(), tmp_path)
    assert f"-g{remote_worker._GATEWAY_EPOCH}-" in launch.new_worker_id()
    remote_worker._REAPED.discard("bind-once")
    asyncio.run(launch.reap_stale_workers())
    asyncio.run(launch.reap_stale_workers())
    assert len(calls) == 1 and "bind-once-*.port" in calls[0]
