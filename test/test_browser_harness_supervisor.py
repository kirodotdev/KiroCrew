"""The supervisor (``browser_harness/supervisor.py``) without a browser: status shape,
the helper allowlist, the IPC leaves, the DevTools port probe and the worker protocol.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX

from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, helper_worker, supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.security import paths_lists


@pytest.fixture
def short_home() -> Any:
    """A data home short enough for the daemon's AF_UNIX socket path.

    pytest's ``tmp_path`` is deeper than :data:`supervisor._MAX_SOCKET_PATH` allows,
    and the supervisor refuses such a home rather than relocating the socket.
    """
    base = None if sys.platform == "win32" else "/tmp"
    home = Path(tempfile.mkdtemp(prefix="kc", dir=base))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_the_hidden_leaves_are_spelled_the_same_in_every_fence() -> None:
    for leaf in (supervisor.IPC_LEAF, supervisor.PROFILE_LEAF):
        assert leaf in sandbox_leaves._CREW_HIDDEN_LEAVES, leaf
        assert leaf in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES, leaf
        assert leaf in paths_lists._CREW_SECRET_LEAVES, leaf
    assert supervisor.IPC_LEAF == "browser-harness-ipc"
    assert supervisor.PROFILE_LEAF == "browser-profile"


def test_the_supervisor_allowlist_matches_the_worker_allowlist() -> None:
    worker = {name: accepted for name, (_fn, accepted) in helper_worker._ALLOWLIST.items()}
    assert dict(supervisor.HELPERS) == worker
    for forbidden in ("http_get", "browser_js", "browser_cdp", "run", "upload_file", "fill_input"):
        assert forbidden not in supervisor.HELPERS


def test_status_on_an_empty_home_has_the_contract_shape(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    status = sup.status()
    assert set(status) >= {"installed", "version", "browser", "connection", "detail"}
    assert status["installed"] is False and status["version"] is None
    assert status["wanted_version"] == HARNESS_VERSION
    assert status["browser"] == {"kind": "kiro_chromium", "headless": True}
    assert status["connection"] == supervisor.DISCONNECTED
    assert status["agent_tabs"] == 0
    assert sup.relay is None and sup.connected is False


@pytest.mark.asyncio
async def test_ensure_refuses_before_install_and_says_so_in_status(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(supervisor.HarnessNotInstalled):
        await sup.ensure()
    assert sup.status()["connection"] == supervisor.DISCONNECTED
    assert "not installed" in sup.status()["detail"]


@pytest.mark.asyncio
async def test_call_refuses_an_unlisted_helper_or_argument_before_connecting(
    tmp_path: Path,
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(supervisor.HelperError):
        await sup.call("http_get", url="https://example.invalid")
    with pytest.raises(supervisor.HelperError):
        await sup.call("goto", url="https://example.invalid", referrer="x")
    with pytest.raises(supervisor.HarnessNotInstalled):
        await sup.call("goto", url="https://example.invalid")


@pytest.mark.asyncio
async def test_test_reports_the_state_reached_within_its_wait(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    report = await sup.test(wait_secs=1.0)
    assert report["connection"] == supervisor.DISCONNECTED
    assert report["installed"] is False


@pytest.mark.asyncio
async def test_stop_on_a_fresh_supervisor_is_a_no_op(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    await sup.stop()
    assert sup.status()["connection"] == supervisor.DISCONNECTED


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX path limit is POSIX-only")
@pytest.mark.asyncio
async def test_a_data_home_too_deep_for_the_socket_is_refused_not_relocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket stays inside the HIDDEN leaf or does not exist: never in system temp."""
    short = HarnessSupervisor(Path("/tmp/k"))
    assert short.runtime_dir() == Path("/tmp/k") / supervisor.IPC_LEAF / "run"
    deep = HarnessSupervisor(tmp_path / ("x" * 120), config_reader=lambda: {})
    with pytest.raises(supervisor.BrowserUnavailable, match="too deep .* limit 100"):
        deep.runtime_dir()
    # The refusal reaches ensure() as the connection error, and stop() stays a no-op.
    monkeypatch.setattr(
        supervisor.install,
        "install_state",
        lambda home: supervisor.install.InstallState(
            prefix=home,
            installed=True,
            version=HARNESS_VERSION,
            python=Path(sys.executable),
            chromium=supervisor.install.chromium.chromium_state(home),
        ),
    )
    with pytest.raises(supervisor.BrowserUnavailable, match="too deep"):
        await deep.ensure()
    assert deep.status()["connection"] == supervisor.ERROR
    assert "too deep" in deep.status()["detail"]
    await deep.stop()
    assert not list(Path(tempfile.gettempdir()).glob("kirocrew-bh-*"))


@pytest.mark.asyncio
async def test_the_devtools_port_file_is_trusted_only_when_something_listens(
    tmp_path: Path,
) -> None:
    assert await supervisor._devtools_ws_url(tmp_path) is None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
            f"{port}\n/devtools/browser/abc\n", encoding="utf-8"
        )
        assert (
            await supervisor._devtools_ws_url(tmp_path)
            == f"ws://127.0.0.1:{port}/devtools/browser/abc"
        )
        (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
            f"{port}\nnot-a-path\n", encoding="utf-8"
        )
        assert await supervisor._devtools_ws_url(tmp_path) is None
    finally:
        listener.close()
    (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8"
    )
    assert await supervisor._devtools_ws_url(tmp_path) is None


def test_the_port_probe_never_runs_on_the_event_loop() -> None:
    """Rule ``no-blocking-call-on-event-loop``: no synchronous socket connect in the module."""
    source = Path(supervisor.__file__).read_text(encoding="utf-8")
    assert "socket.create_connection" not in source
    assert "import socket" not in source


@pytest.mark.skipif(sys.platform == "win32", reason="the pipe launcher is POSIX-only")
@pytest.mark.asyncio
async def test_the_pipe_launcher_puts_the_relay_on_fds_3_and_4_and_opens_no_port(
    tmp_path: Path,
) -> None:
    """Run the real ``/bin/sh`` remap with a Python stand-in for Chromium.

    The stand-in reads one ``\\0``-terminated message on fd 3 and answers on fd 4 the
    way ``--remote-debugging-pipe`` does, then exits; the gateway side is the real
    :class:`PipeUpstream`. A flat ``--remote-debugging-port`` must not be in argv.
    """
    sup = HarnessSupervisor(tmp_path, base_env={"PATH": "/usr/bin:/bin"})
    seen: list[list[str]] = []
    original = sup._spawn

    async def _spy(argv: list[str], **kw: Any) -> Any:
        seen.append(list(argv))
        return await original(argv, **kw)

    sup._spawn = _spy  # type: ignore[method-assign]
    fake_chromium = [
        sys.executable,
        "-c",
        "import os,json\n"
        "buf=b''\n"
        "while not buf.endswith(b'\\0'): buf+=os.read(3,1)\n"
        "req=json.loads(buf[:-1])\n"
        "os.write(4, json.dumps({'id':req['id'],'result':{'product':'fake/1'}}).encode()+b'\\0')\n"
        # Like Chromium, hold fd 3 open until the gateway closes its end: exiting right
        # after the reply could close fd 3 before ``send`` drains and fail it as reset.
        "os.close(4)\n" "while os.read(3,4096): pass\n",
    ]
    proc, upstream = await sup._launch_chromium_pipe(fake_chromium)
    try:
        argv = seen[-1]
        assert argv[0] == supervisor._SH and argv[-1] == supervisor._CHROMIUM_PIPE_ARG
        assert not any(a.startswith("--remote-debugging-port") for a in argv)
        assert int(argv[4]) >= supervisor._FIRST_SAFE_FD
        assert int(argv[5]) >= supervisor._FIRST_SAFE_FD
        await upstream.send(json.dumps({"id": 7, "method": "Browser.getVersion"}))
        replies = []
        async for raw in upstream.messages():
            replies.append(raw)
            break
    finally:
        await upstream.close()
        await supervisor._stop_process(proc)
    assert json.loads(replies[0]) == {"id": 7, "result": {"product": "fake/1"}}


def test_chromium_argv_has_no_flat_debugging_port_on_posix() -> None:
    assert "--remote-debugging-port=0" not in supervisor._CHROMIUM_ARGS
    assert supervisor._CHROMIUM_PIPE_ARG == "--remote-debugging-pipe"
    assert supervisor._CHROMIUM_PORT_ARG == "--remote-debugging-port=0"


@pytest.mark.asyncio
async def test_the_daemon_is_started_in_isolated_mode(
    short_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-I`` keeps the gateway's cwd off ``sys.path``: a project's ``browser_harness/``
    directory can never shadow the pinned package the daemon imports."""
    sup = HarnessSupervisor(short_home, base_env={"PATH": "/usr/bin"})
    seen: list[list[str]] = []

    class _Gone:
        returncode = 1

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        seen.append(list(argv))
        return _Gone()

    async def _no_ping() -> bool:
        return False

    monkeypatch.setattr(sup, "_spawn", _spawn)
    monkeypatch.setattr(sup, "_ipc_ping", _no_ping)
    with pytest.raises(supervisor.BrowserUnavailable):
        await sup._start_daemon(Path(sys.executable), "http://127.0.0.1:1/s")
    assert seen[0][1:] == ["-I", "-m", "browser_harness.daemon"]


def test_get_supervisor_is_one_per_home(tmp_path: Path) -> None:
    supervisor._reset_supervisors_for_tests()
    try:
        a = supervisor.get_supervisor(tmp_path)
        assert supervisor.get_supervisor(tmp_path) is a
        assert supervisor.get_supervisor(tmp_path / "other") is not a
    finally:
        supervisor._reset_supervisors_for_tests()


def test_the_daemon_env_pins_name_and_hidden_roots(short_home: Path) -> None:
    tmp_path = short_home
    sup = HarnessSupervisor(tmp_path, base_env={"PATH": "/usr/bin", "BU_CDP_WS": "ws://evil"})
    env = sup._daemon_env("http://127.0.0.1:9999")
    assert env["BU_NAME"] == supervisor.DAEMON_NAME != "default"
    assert env["BU_CDP_URL"] == "http://127.0.0.1:9999"
    assert "BU_CDP_WS" not in env
    assert Path(env["BH_HOME"]) == tmp_path / supervisor.IPC_LEAF
    assert Path(env["BH_RUNTIME_DIR"]) == sup.runtime_dir()
    assert Path(env["BH_AGENT_WORKSPACE"]).is_dir()
    assert env["BH_TELEMETRY"] == "false"


@pytest.mark.asyncio
async def test_kiro_chromium_is_launched_with_a_mock_keystore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: without ``--use-mock-keychain`` a fresh Chrome for Testing on macOS
    blocks its network service on a Keychain prompt nobody can answer headless, and
    every http(s) ``Page.navigate`` hangs forever (``data:`` pages still load).
    """
    from kiro_crew.browser_harness import chromium, discover, install

    exe = tmp_path / "chrome"
    exe.write_text("", encoding="utf-8")
    spawned: list[list[str]] = []

    class _Proc:
        returncode: int | None = None

    # Both launch paths receive the same base argv; capture it at the seam
    # _launch_chromium hands it to, so the test holds on every OS.
    async def fake_launch(self: HarnessSupervisor, argv: list[str], *_rest: Any) -> Any:
        spawned.append(argv)
        return _Proc(), "ws://127.0.0.1:1/x"

    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_pipe", fake_launch)
    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_port", fake_launch)
    monkeypatch.setattr(supervisor.platform_compat, "effective_uid", lambda: 501)
    sup = HarnessSupervisor(tmp_path, base_env={})
    state = install.InstallState(
        prefix=tmp_path,
        installed=True,
        version=HARNESS_VERSION,
        python=None,
        chromium=chromium.ChromiumState(
            version="x", platform="mac-arm64", present=True, executable=exe
        ),
    )
    for headless in (True, False):
        spawned.clear()
        choice = discover.BrowserChoice(
            kind="kiro_chromium", executable=exe, headless=headless, user_data_dir=None
        )
        _proc, url = await sup._launch_chromium(choice, state)
        assert url == "ws://127.0.0.1:1/x"
        (argv,) = spawned
        assert argv[0] == str(exe)
        for flag in supervisor._KEYSTORE_ARGS:
            assert flag in argv, flag
        assert "--use-mock-keychain" in argv and "--password-store=basic" in argv
        assert (supervisor._HEADLESS_ARG in argv) is headless
        assert supervisor._NO_SANDBOX_ARG not in argv
        assert f"--user-data-dir={sup.profile_dir}" in argv


# ── the worker's protocol, with a fake helpers module ──


def _fake_helpers() -> types.ModuleType:
    mod = types.ModuleType("helpers")
    mod.goto_url = lambda url: {"frameId": "f", "url": url}  # type: ignore[attr-defined]
    mod.page_info = lambda: {"url": "about:blank"}  # type: ignore[attr-defined]

    def boom(**_kw):
        raise RuntimeError("Session with given id not found")

    mod.click_at_xy = boom  # type: ignore[attr-defined]
    mod.http_get = lambda url: "never"  # type: ignore[attr-defined]
    return mod


def test_the_worker_runs_only_allowlisted_helpers_with_allowlisted_arguments() -> None:
    helpers = _fake_helpers()
    ok = helper_worker._handle(helpers, {"id": 1, "helper": "goto", "kwargs": {"url": "https://a"}})
    assert ok == {"id": 1, "ok": True, "result": {"frameId": "f", "url": "https://a"}}
    assert helper_worker._handle(helpers, {"id": 2, "ping": True})["result"] == {"pong": True}
    denied = helper_worker._handle(helpers, {"id": 3, "helper": "http_get", "kwargs": {"url": "x"}})
    assert denied["ok"] is False and "not allowed" in denied["error"]
    extra = helper_worker._handle(
        helpers, {"id": 4, "helper": "goto", "kwargs": {"url": "x", "evil": 1}}
    )
    assert extra["ok"] is False and "unexpected arguments" in extra["error"]
    failed = helper_worker._handle(
        helpers, {"id": 5, "helper": "click", "kwargs": {"x": 1, "y": 2}}
    )
    assert failed["ok"] is False and "Session with given id" in failed["error"]
    assert "Traceback" not in failed["error"]


#: A worker stand-in: answers pings and helpers at once, except ``wait_for_load``,
#: whose reply it HOLDS until the next request arrives, then writes both in order.
#: That is the shape of a cancelled screenshot whose result lands during the next op.
_HOLDING_WORKER = (
    "import sys, json\n"
    "held = None\n"
    "for line in sys.stdin:\n"
    "    req = json.loads(line)\n"
    "    if req.get('ping'):\n"
    "        print(json.dumps({'id': req['id'], 'ok': True, 'result': {'pong': True}}), flush=True)\n"
    "        continue\n"
    "    if req['helper'] == 'wait_for_load':\n"
    "        held = req\n"
    "        continue\n"
    "    if held is not None:\n"
    "        print(json.dumps({'id': held['id'], 'ok': True, 'result': 'OLD'}), flush=True)\n"
    "        held = None\n"
    "    print(json.dumps({'id': req['id'], 'ok': True, 'result': req['helper']}), flush=True)\n"
)


class _WorkerOnlySupervisor(HarnessSupervisor):
    """``ensure()`` hands back a handle with no browser; the worker is the stand-in above."""

    def __init__(self, home: Path) -> None:
        super().__init__(home, config_reader=lambda: {})
        self.handle = supervisor.HarnessHandle(
            choice=supervisor.discover.BrowserChoice("kiro_chromium", None, True, None),
            relay=supervisor.CdpRelay(),
            daemon=None,
            worker=None,
            browser=None,
        )
        self.workers_started = 0

    async def ensure(self) -> supervisor.HarnessHandle:
        return self.handle

    async def _start_worker(self, handle: supervisor.HarnessHandle) -> Any:
        self.workers_started += 1
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _HOLDING_WORKER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )


@pytest.mark.asyncio
async def test_a_timed_out_helper_never_answers_the_next_call(tmp_path: Path) -> None:
    """The held ``wait_for_load`` reply must not become ``page_info``'s result."""
    sup = _WorkerOnlySupervisor(tmp_path)
    sup.call_timeout = 0.3
    try:
        with pytest.raises(supervisor.HelperError, match="timed out"):
            await sup.call("wait_for_load", timeout=1)
        assert sup.handle.worker is None, "a worker that owes a reply is discarded"
        assert await sup.call("page_info") == "page_info"
        assert sup.workers_started == 2
    finally:
        await supervisor._stop_process(sup.handle.worker)


@pytest.mark.asyncio
async def test_a_cancelled_helper_discards_the_worker(tmp_path: Path) -> None:
    sup = _WorkerOnlySupervisor(tmp_path)
    try:
        task = asyncio.ensure_future(sup.call("wait_for_load", timeout=1))
        # The request is on the pipe once the worker has been started.
        while sup.handle.worker is None:
            await asyncio.sleep(0)
        held = sup.handle.worker
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sup.handle.worker is None and held.returncode is not None
        assert await sup.call("page_info") == "page_info"
    finally:
        await supervisor._stop_process(sup.handle.worker)


@pytest.mark.asyncio
async def test_worker_request_matches_replies_by_id() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HOLDING_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        # Park a reply the worker will write before the next one.
        proc.stdin.write(b'{"id": 1, "helper": "wait_for_load"}\n')
        await proc.stdin.drain()
        reply = await supervisor._worker_request(proc, {"id": 2, "helper": "page_info"}, 10.0)
        assert reply == {"id": 2, "ok": True, "result": "page_info"}
    finally:
        await supervisor._stop_process(proc)


@pytest.mark.asyncio
async def test_the_worker_script_reports_a_missing_harness_instead_of_hanging() -> None:
    """Run the real script under the gateway's interpreter, which has no ``browser_harness``."""
    script = Path(helper_worker.__file__)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        reply = await supervisor._worker_request(proc, {"id": 0, "ping": True}, 30.0)
    finally:
        await supervisor._stop_process(proc)
    assert reply["ok"] is False
    assert "import failed" in reply["error"]
    assert json.dumps(reply)


_BIG_REPLY_WORKER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    req = json.loads(line)\n"
    "    n = int(req['kwargs']['n'])\n"
    "    print(json.dumps({'id': req['id'], 'ok': True, 'result': 'x' * n}), flush=True)\n"
)


@pytest.mark.asyncio
async def test_a_reply_longer_than_asyncios_default_line_is_read_whole() -> None:
    """A 100 KB snapshot reply (600 links with long names) is an ordinary page, not an error.

    asyncio's default stream limit is 64 KiB; ``_spawn`` raises it to
    ``WORKER_LINE_LIMIT`` for the worker pipe.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _BIG_REPLY_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=supervisor.WORKER_LINE_LIMIT,
    )
    try:
        reply = await supervisor._worker_request(
            proc, {"id": 1, "helper": "js", "kwargs": {"n": 100_000}}, 10.0
        )
    finally:
        await supervisor._stop_process(proc)
    assert reply["ok"] is True and len(reply["result"]) == 100_000


@pytest.mark.asyncio
async def test_a_reply_past_the_line_limit_is_fatal_not_reusable() -> None:
    """Over the limit ``readline`` raises mid-line; the worker is marked fatal so the
    bytes still in the pipe can never be read as the next call's reply."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _BIG_REPLY_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=4096,
    )
    try:
        reply = await supervisor._worker_request(
            proc, {"id": 1, "helper": "js", "kwargs": {"n": 10_000}}, 10.0
        )
    finally:
        await supervisor._stop_process(proc)
    assert reply["ok"] is False and reply["fatal"] is True
    assert "too long" in reply["error"]


def test_the_spawn_raises_the_pipe_limit_and_the_worker_caps_below_it() -> None:
    source = Path(supervisor.__file__).read_text(encoding="utf-8")
    assert 'kwargs["limit"] = WORKER_LINE_LIMIT' in source
    assert helper_worker.REPLY_CAP_BYTES < supervisor.WORKER_LINE_LIMIT
    too_big = {"id": 1, "ok": True, "result": "x" * (helper_worker.REPLY_CAP_BYTES + 1)}
    bounded = helper_worker._bounded(too_big)
    assert bounded["ok"] is False and bounded["id"] == 1 and "too large" in bounded["error"]
    small = {"id": 2, "ok": True, "result": "fine"}
    assert helper_worker._bounded(small) is small


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX path limit is POSIX-only")
def test_the_socket_path_guard_measures_the_resolved_home(tmp_path: Path) -> None:
    """Harness binds ``Path(BH_RUNTIME_DIR).resolve()``: a short symlink to a deep real
    path passes a check on the spelling and fails at bind. Measure what will bind."""
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    # pytest's tmp_path is itself too long for the premise; the SHORT side of the
    # link lives directly under /tmp and is removed below.
    # The run-owned short root is itself too long here, so this uses the shared /tmp.
    short_root = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "bh-", dir="/tmp"))
    try:
        link = short_root / "k"
        link.symlink_to(deep, target_is_directory=True)
        assert len(str(link / supervisor.IPC_LEAF / "run" / "bu.sock")) <= 100
        sup = HarnessSupervisor(link, config_reader=lambda: {})
        with pytest.raises(supervisor.BrowserUnavailable, match="too deep") as info:
            sup.runtime_dir()
        assert str(deep.resolve()) in str(info.value), "the message names the path that binds"
        # A short real path behind a short symlink is fine, and the preferred
        # (unresolved) spelling is what the daemon is told.
        real = short_root / "r"
        real.mkdir()
        (short_root / "s").symlink_to(real, target_is_directory=True)
        assert HarnessSupervisor(short_root / "s").runtime_dir() == (
            short_root / "s" / supervisor.IPC_LEAF / "run"
        )
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_daemon_started_before_the_worker_failed_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_start`` spawned the daemon, then ``_start_worker`` raised: the daemon must
    not be left running in its own session holding the hidden socket."""
    stopped: list[Any] = []

    async def fake_stop(proc: Any) -> None:
        stopped.append(proc)

    monkeypatch.setattr(supervisor, "_stop_process", fake_stop)
    monkeypatch.setattr(
        supervisor.install,
        "install_state",
        lambda home: supervisor.install.InstallState(
            prefix=home,
            installed=True,
            version=HARNESS_VERSION,
            python=Path(sys.executable),
            chromium=supervisor.install.chromium.chromium_state(home),
        ),
    )
    sup = HarnessSupervisor(tmp_path / "h", config_reader=lambda: {})
    monkeypatch.setattr(sup, "runtime_dir", lambda: tmp_path / "h" / "run")  # tmp_path is deep
    choice = supervisor.discover.BrowserChoice("user_chrome", Path("/x/chrome"), False, None)
    monkeypatch.setattr(sup, "_choose", lambda state: choice)

    async def ws_url(_choice: Any) -> str:
        return "ws://127.0.0.1:1/devtools/browser/x"

    class FakeRelay:
        on_upstream_closed: Any = None
        closed = False

        async def start(self, upstream: Any) -> str:
            return "http://127.0.0.1:1/s"

        async def close(self) -> None:
            self.closed = True

    relay = FakeRelay()
    monkeypatch.setattr(supervisor, "CdpRelay", lambda: relay)
    monkeypatch.setattr(sup, "_user_browser_ws_url", ws_url)
    daemon = object()

    async def start_daemon(python: Any, relay_url: str) -> Any:
        return daemon

    async def start_worker(handle: Any) -> Any:
        raise supervisor.BrowserUnavailable("the helper worker did not start")

    monkeypatch.setattr(sup, "_start_daemon", start_daemon)
    monkeypatch.setattr(sup, "_start_worker", start_worker)
    with pytest.raises(supervisor.BrowserUnavailable, match="helper worker"):
        await sup.ensure()
    assert daemon in stopped
    assert relay.closed
    assert sup.status()["connection"] == supervisor.ERROR
