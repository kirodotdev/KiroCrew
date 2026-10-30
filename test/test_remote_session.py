"""Remote session transport: wire protocol, guest worker, gateway link.

On loopback: the worker's child is a Python echo, and a proxy injects tunnel
faults (a trailing LF turned into CR), bit flips and cuts.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import os
import random
import subprocess
import sys
import time

import pytest

from kiro_crew import session_environment as se
from kiro_crew.coder import remote_worker
from kiro_crew.coder.session_browse import summarize_unsaved
from kiro_crew.context_assembly.turn import rail_parts
from kiro_crew.remote_session import link as link_mod
from kiro_crew.remote_session import wire
from kiro_crew.remote_session import worker as worker_mod
from kiro_crew.remote_session.relay import WorkspaceRelay
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
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


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
    path.write_text(bytes(range(32)).hex())
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError):
        worker_mod.read_secret(str(path))
    os.chmod(path, 0o600)
    assert len(worker_mod.read_secret(str(path))) == 32
    assert not path.exists()


@pytest.mark.parametrize("traffic", ["silent", "bad", "ping"])
@sync
async def test_attached_worker_orphan_clock(monkeypatch, manual_clock, traffic):
    manual_clock.install(monkeypatch, worker_mod)
    worker = worker_mod.Worker(SID, b"s" * 32, ECHO)
    gateway, worker.ep = _pair()
    worker.ep.last_inbound = manual_clock.monotonic()
    manual_clock.advance(worker.orphan_timeout + 1)
    worker.ep.feed(
        gateway.ping(1) if traffic == "ping" else b"bad\n" if traffic == "bad" else b"",
        manual_clock.monotonic(),
    )
    # Drive one tick without waiting or spawning a task.
    monkeypatch.setattr(worker_mod, "TICK_SECONDS", 0)
    ticker = worker._ticker()
    try:
        ticker.send(None)
        ticker.send(None)
    finally:
        ticker.close()
    assert worker.ep.attached
    assert worker._stopping.is_set() == (traffic != "ping")
    assert worker.exit_code == (0 if traffic == "ping" else 3)


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


def _link(port: int, secret: bytes, dial=None, **kw) -> link_mod.RemoteLink:
    async def dial_port():
        return await asyncio.open_connection("127.0.0.1", port)

    opts = dict(keepalive_seconds=0.5, stall_seconds=3, lease_seconds=10, backoff_initial=0.05)
    opts.update(kw)
    return link_mod.RemoteLink(SID, secret, dial or dial_port, **opts)


@contextlib.asynccontextmanager
async def _session(command=ECHO, *, proxy=None, link_kw=None, **worker_kw):
    """Yield a started ``(worker, task, link, proxy)``; ``proxy`` is ``(mutate, both_ways)``."""
    secret = wire.new_secret()
    w, task = await _start_worker(secret, command, **worker_kw)
    px = Proxy(w.port, proxy[0], both_ways=proxy[1]) if proxy else None
    link = _link(await px.start() if px else w.port, secret, **(link_kw or {}))
    try:
        link.start()
        yield w, task, link, px
    finally:
        if px:
            px.close()
        await _stop(w, task, link)


@pytest.mark.parametrize(
    "mutate", [None, _ssm_bug, _flips(2e-5)], ids=["clean", "ssm-bug", "flips"]
)
@sync
async def test_exact_delivery_through_faulty_tunnel(mutate):
    async with _session(proxy=(mutate, mutate is not _ssm_bug)) as (_, _, link, _):
        await link.wait_connected(10)
        await _echo_all(link, _lines(150))
        assert link.state == link_mod.CONNECTED


@sync
async def test_survives_repeated_connection_cuts_without_loss_or_duplication():
    async with _session(proxy=(None, False)) as (w, _, link, proxy):
        await link.wait_connected(10)
        await _echo_all(link, _lines(120), between=lambda i: proxy.cut() if i % 15 == 7 else None)
        assert link.connections > 1
        assert w.child.returncode is None  # the agent process never restarted


@sync
async def test_a_stalled_reader_holds_the_guest_back_instead_of_buffering():
    # A flooding guest fills its own unacked window, not gateway memory, without
    # reading as a stall; every byte arrives once the reader catches up.
    total_lines, width = 40 * 1024, 1023
    flood = [
        sys.executable,
        "-c",
        # Binary stdout: Windows text mode would turn each "\n" into "\r\n".
        "import sys\nout = sys.stdout.buffer\n"
        f"for _ in range({total_lines}): out.write(b'x' * {width} + b'\\n')\n"
        "out.write(b'DONE\\n')\nout.flush()\nsys.stdin.read()",
    ]
    skew = [0.0]
    limit = wire.MAX_LINE_BYTES * 64
    clock = {"clock": lambda: time.monotonic() + skew[0]}
    async with _session(flood, link_kw=clock) as (w, _, link, _):
        await link.wait_connected(10)
        await async_wait_until(
            lambda: w.ep.unacked_bytes + 2 * wire.MAX_LINE_BYTES >= w.ep.max_unacked_bytes,
            describe=lambda: (w.ep.unacked_bytes, len(link.stdout._buffer)),
        )
        assert not link._consumer.open.is_set()
        # ep.feed combines ep._buf (at most MAX_LINE_BYTES, a partial wire frame
        # from the prior read) with one reader.read (at most MAX_LINE_BYTES), giving
        # at most 2*MAX_LINE_BYTES of wire bytes per call.  Every decoded byte
        # comes from base64, so decoded < wire (each payload byte costs strictly
        # more than 1 wire byte due to base64 4:3 expansion plus per-frame header).
        # asyncio raises the high-water alarm after buffer > 2*limit, then
        # _connect_once waits on open before the next read, so at most one more
        # decode lands: buffer < 2*limit + 2*MAX_LINE_BYTES.
        assert len(link.stdout._buffer) < 2 * limit + 2 * wire.MAX_LINE_BYTES
        # Far past the stall window, reader still stuck: two more ticks.
        skew[0] += 10 * link.stall_seconds
        pings = link._ping_n
        await async_wait_until(lambda: link._ping_n >= pings + 2, describe=lambda: link._ping_n)
        assert link.connections == 1 and link.state == link_mod.CONNECTED
        assert len(link.stdout._buffer) < 2 * limit + 2 * wire.MAX_LINE_BYTES
        lines = 0
        while (line := await link.stdout.readline()) != b"DONE\n":
            assert line == b"x" * width + b"\n"
            lines += 1
        assert lines == total_lines and link.connections == 1


@sync
async def test_unauthenticated_connection_cannot_hijack_the_session():
    rejected = asyncio.Event()
    rejection_states = (link_mod.LOST, link_mod.REFUSED, link_mod.RECONNECTING)
    async with _session() as (w, _, link, _):
        intruder = _link(
            w.port,
            wire.new_secret(),
            on_state=lambda state: rejected.set() if state in rejection_states else None,
        )
        try:
            await link.wait_connected(10)
            intruder.start()
            await asyncio.wait_for(rejected.wait(), 30)
            assert intruder.state in rejection_states
            assert intruder.ep.stats["delivered"] == 0
            await _echo_all(link, _lines(5))
        finally:
            await intruder.aclose()


@sync
async def test_lease_expiry_marks_session_lost():
    async def dial():
        raise ConnectionRefusedError("no route")

    link = _link(
        0,
        wire.new_secret(),
        dial,
        keepalive_seconds=0.1,
        stall_seconds=0.2,
        lease_seconds=0.5,
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
    cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bye\\n' * 3000)"]
    async with _session(cmd) as (_, task, link, _):
        out = await asyncio.wait_for(link.stdout.read(), 30)
        assert out == b"bye\n" * 3000
        assert link.state == link_mod.FINISHED
        assert await await_until(task, what="guest worker exit") == 0


@sync
async def test_retire_is_reported_and_closing_input_ends_the_child():
    async with _session(retire_grace=10) as (w, _, link, _):
        await link.wait_connected(10)
        retiring = asyncio.ensure_future(w.retire("spot-reclaim"))
        await async_wait_until(lambda: link.state == link_mod.RETIRING, describe=lambda: link.state)
        assert link.state == link_mod.RETIRING and link.retire_reason == "spot-reclaim"
        link.close_input()  # the gateway finished its turn and lets the child exit
        await await_until(retiring, what="guest retirement")
        assert w.child.returncode == 0


#: A child that, like the tool server, ends its running command on SIGTERM: the
#: cleanup takes a moment and leaves a mark, then the child exits.
_CLEANS_UP_ON_TERM = (
    "import signal, sys, time\n"
    "def done(*_):\n"
    "    time.sleep(0.5)\n"
    "    open(sys.argv[1], 'w').write('cleaned')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, done)\n"
    "open(sys.argv[1] + '.ready', 'w').close()\n"
    "time.sleep(300)\n"
)


@pytest.mark.skipif(os.name == "nt", reason="the guest's stop handler is a POSIX signal")
@sync
async def test_the_gateway_stop_ends_the_child_now_and_waits_for_its_cleanup(tmp_path):
    """GPT 6.1 on 49519e2b85: a stop must not leave the child a retire grace in which
    an old command could still overwrite a replacement runtime's work."""
    mark = tmp_path / "cleaned"
    command = [sys.executable, "-c", _CLEANS_UP_ON_TERM, str(mark)]
    async with _session(command, retire_grace=3600, drain_timeout=3600) as (w, task, link, _):
        await link.wait_connected(10)
        ready = tmp_path / "cleaned.ready"
        await async_wait_until(ready.exists, describe=lambda: "child's stop handler")
        w.stop()
        await await_until(task, what="stopped guest worker")
        assert mark.read_text() == "cleaned" and w.child.returncode == 0


def test_the_gateway_stop_signal_is_the_one_the_worker_stops_on():
    assert "kill -USR1" in remote_worker._stop_command("sess-9")
    assert worker_mod.STOP_GRACE_SECONDS < remote_worker._STOP_WAIT_SECONDS
    # A relay close awaited by a runtime kill outlasts a worker stop's own wait.
    assert remote_worker._STOP_WAIT_SECONDS < remote_worker._RELAY_CLOSE_WAIT_SECONDS


@sync
async def test_child_does_not_see_api_key(monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "must-not-leak")
    cmd = [sys.executable, "-c", "import os; print(os.environ.get('KIRO_API_KEY', 'absent'))"]
    async with _session(cmd) as (_, _, link, _):
        assert (await asyncio.wait_for(link.stdout.read(), 30)).strip() == b"absent"


def test_the_orphan_timeout_is_one_shared_value_every_side_reads():
    lease = inspect.signature(link_mod.RemoteLink.__init__).parameters["lease_seconds"].default
    # A gateway restart reconnects inside the lease, before the worker quits.
    assert wire.ORPHAN_TIMEOUT_SECONDS > lease
    # A never-attached spare is retired before its own orphan clock can stop it.
    assert wire.SPARE_MAX_AGE_SECONDS < wire.ORPHAN_TIMEOUT_SECONDS
    launch = inspect.signature(remote_worker.RemoteWorkerLaunch.__init__).parameters
    assert launch["orphan_timeout"].default == wire.ORPHAN_TIMEOUT_SECONDS
    guest = inspect.signature(worker_mod.Worker.__init__).parameters
    assert guest["orphan_timeout"].default == wire.ORPHAN_TIMEOUT_SECONDS
    args, _command = worker_mod._parse(["--session-id", "s", "--secret-file", "f", "--", "x"])
    assert args.orphan_timeout == wire.ORPHAN_TIMEOUT_SECONDS
    spare = inspect.signature(WorkspaceRelay.__init__).parameters["spare_max_age"].default
    assert spare == wire.SPARE_MAX_AGE_SECONDS


def test_a_recreated_workspace_is_named_once_on_the_project_line():
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
    assert summarize_unsaved(status) == expected


linux_guest = pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="a Linux guest script")


def _sessions(home):
    sessions = home / ".kirocrew-worker" / "sessions"
    sessions.mkdir(parents=True)
    return sessions


def _sleeper(*argv):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])


def _guest_sh(home, command):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(["/bin/sh", "-c", command], env=env, check=True, timeout=30)


def _end(*procs):
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


@linux_guest
def test_stale_workers_from_another_gateway_process_are_reaped(tmp_path):
    """A crashed gateway's worker is stopped; this process's own one is not, nor a
    process that inherited a dead worker's recorded pid."""
    sessions = _sessions(tmp_path)
    epoch = remote_worker._GATEWAY_EPOCH
    stale_port = sessions / "bind-1-gdead00-0001.port"
    # Shaped like a real worker: its argv carries --port-file <its record>.
    stale = _sleeper("--port-file", str(stale_port))
    live = subprocess.Popen(["sleep", "60"])
    reused = subprocess.Popen(["sleep", "60"])  # a pid the kernel handed to someone else
    try:
        stale_port.write_text(f"41000 {stale.pid}\n")
        (sessions / "bind-1-gdead00-0001.key").write_text("k")
        (sessions / f"bind-1-g{epoch}-0002.port").write_text(f"41001 {live.pid}\n")
        (sessions / "bind-1-gdead00-0004.port").write_text(f"41003 {reused.pid}\n")
        (sessions / "other-gdead00-0003.port").write_text(f"41002 {UNALLOCATABLE_PID}\n")
        _guest_sh(tmp_path, remote_worker._reap_stale_command("bind-1", epoch))
        wait_until(lambda: stale.poll() is not None, describe=lambda: "stale worker exit")
        assert live.poll() is None and reused.poll() is None
        assert not stale_port.exists()
        assert not (sessions / "bind-1-gdead00-0001.key").exists()
        assert not (sessions / "bind-1-gdead00-0004.port").exists()  # record dropped
        assert (sessions / f"bind-1-g{epoch}-0002.port").exists()
        assert (sessions / "other-gdead00-0003.port").exists()  # another binding's
    finally:
        _end(stale, live, reused)


@linux_guest
@pytest.mark.parametrize("ours", [True, False])
def test_stop_signals_only_the_worker_its_record_names(tmp_path, ours):
    port = _sessions(tmp_path) / "sess-9.port"
    proc = _sleeper(*(["--port-file", str(port)] if ours else []))
    try:
        port.write_text(f"41000 {proc.pid}\n")
        _guest_sh(tmp_path, remote_worker._stop_command("sess-9"))
        if ours:
            wait_until(lambda: proc.poll() is not None, describe=lambda: "worker exit")
        else:
            assert proc.poll() is None
        assert not port.exists()
    finally:
        _end(proc)


@linux_guest
def test_the_stop_command_returns_only_once_the_worker_has_exited(tmp_path):
    """The relay's teardown awaits this command, so it must not return while the
    worker (and the command its child runs) is still ending."""
    port = _sessions(tmp_path) / "sess-9.port"
    gone = tmp_path / "gone"
    program = (
        "import signal, sys, time\n"
        "def stop(*_):\n"
        "    time.sleep(2)\n"
        "    open(sys.argv[1], 'w').write('gone')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGUSR1, stop)\n"
        "open(sys.argv[1] + '.ready', 'w').close()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", program, str(gone), "--port-file", str(port)])
    try:
        port.write_text(f"41000 {proc.pid}\n")
        ready = tmp_path / "gone.ready"
        wait_until(ready.exists, describe=lambda: "stop handler installed")
        _guest_sh(tmp_path, remote_worker._stop_command("sess-9"))
        assert gone.read_text() == "gone"
        assert not port.exists()
    finally:
        _end(proc)


def test_worker_ids_carry_this_process_epoch_and_reap_runs_once(tmp_path):
    calls: list[str] = []

    class Shell:
        async def run(self, command, stdin=b""):
            calls.append(command)
            return ""

    launch = remote_worker.RemoteWorkerLaunch("bind-once", Shell())
    assert f"-g{remote_worker._GATEWAY_EPOCH}-" in launch.new_worker_id()
    remote_worker._REAPED.discard("bind-once")
    asyncio.run(launch.reap_stale_workers())
    asyncio.run(launch.reap_stale_workers())
    assert len(calls) == 1 and "bind-once-*.port" in calls[0]
