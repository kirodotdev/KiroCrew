"""End-to-end terminal tests over a real PTY shell and an aiohttp test client.

``TestTerminalWsIntegration`` drives the whole WebSocket lifecycle. Its
Ctrl+C assertions call ``_sigint_failure_evidence``, which reads
``_collect_sigint_evidence`` from this module's globals, and the tests that
patch that helper do so through ``sys.modules[__name__]``; the helpers and
those tests share this module so the patch reaches the reader.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shlex
import shutil
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _drain_ws_until,
    _drain_ws_while,
    _make_app,
    _make_session,
    _pty_foreground_pgid,
    _recv_control,
    _recv_pty_output,
    _unwrapped,
)

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.wait import async_wait_until

# ── Integration tests using aiohttp TestClient ──


def _collect_sigint_evidence(sess):
    """Inspect only the test PTY and the shell's bounded direct-child list."""
    import termios

    evidence = {
        "output_tail": repr(bytes(sess.scrollback[-2048:]))[-2048:],
        "shell_pid": sess.proc.pid,
        "shell_returncode": sess.proc.returncode,
        "processes": [],
        "errors": [],
    }
    # The existing session API uses a legacy field name; keep it at this boundary.
    controller_fd = sess.master_fd  # wokeignore:rule=master
    try:
        evidence["foreground_pgid"] = os.tcgetpgrp(controller_fd)
        attrs = termios.tcgetattr(controller_fd)
        evidence["ISIG"] = bool(attrs[3] & termios.ISIG)
        evidence["VINTR"] = repr(attrs[6][termios.VINTR])
    except OSError as error:
        evidence["errors"].append(f"tty:{type(error).__name__}")
    if sys.platform != "linux":
        return evidence

    def read(source):
        try:
            with source.open(encoding="utf-8") as stream:
                return stream.read(4096)
        except OSError as error:
            evidence["errors"].append(f"{source}:{type(error).__name__}")
            return ""

    shell_pid = sess.proc.pid
    children = read(pathlib.Path(f"/proc/{shell_pid}/task/{shell_pid}/children")).split()
    for pid in [str(shell_pid), *children[:16]]:
        if not pid.isdecimal():
            continue
        status = read(pathlib.Path(f"/proc/{pid}/status"))
        fields = dict(row.split(":", 1) for row in status.splitlines() if ":" in row)
        # A child may exit before inspection. Do not report a reused stranger.
        if pid != str(shell_pid) and fields.get("PPid", "").strip() != str(shell_pid):
            continue
        selected = {
            key: value.strip()
            for key, value in fields.items()
            if key in {"State", "Pid", "PPid", "SigPnd", "ShdPnd", "SigBlk", "SigIgn", "SigCgt"}
        }
        evidence["processes"].append(selected)
    return evidence


def _sigint_failure_evidence(sess):
    try:
        return json.dumps(_collect_sigint_evidence(sess), sort_keys=True)[:8192]
    except Exception as error:
        return f"SIGINT evidence unavailable: {type(error).__name__}"


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_sigint_evidence_preserves_assertion_on_probe_error(monkeypatch, error_type):
    def broken(_sess):
        raise error_type("must not replace the original assertion")

    monkeypatch.setattr(sys.modules[__name__], "_collect_sigint_evidence", broken)
    with pytest.raises(AssertionError, match="SIGINT evidence unavailable"):
        assert False, _sigint_failure_evidence(_make_session())


def test_sigint_evidence_is_lazy_and_bounded(monkeypatch):
    probe = MagicMock(return_value={"rows": "x" * 20000})
    monkeypatch.setattr(sys.modules[__name__], "_collect_sigint_evidence", probe)
    assert True, _sigint_failure_evidence(_make_session())
    probe.assert_not_called()
    assert len(_sigint_failure_evidence(_make_session())) == 8192


def test_sigint_evidence_omits_unrelated_process_fields(monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    sess = _make_session()
    sess.scrollback.extend(b"x" * 10000 + b"test-output")
    pid = sess.proc.pid
    sources = {
        pathlib.Path(f"/proc/{pid}/task/{pid}/children"): "12346 12347",
        pathlib.Path(f"/proc/{pid}/status"): f"Pid:\t{pid}\nState:\tS (sleeping)\nSigIgn:\t0002\n",
        pathlib.Path(
            "/proc/12346/status"
        ): f"Pid:\t12346\nPPid:\t{pid}\nSigBlk:\t0000\nName:\tprivate-name\n",
        # The child exited and this pid now belongs to someone else.
        pathlib.Path("/proc/12347/status"): "Pid:\t12347\nPPid:\t1\nSigBlk:\tffff\n",
    }
    opened = []

    def fake_open(source, **_kwargs):
        opened.append(source)
        return StringIO(sources[source])

    attrs = [0, 0, 0, 1, 0, 0, [b"\x03"]]
    with monkeypatch.context() as scoped:
        scoped.setitem(
            sys.modules,
            "termios",
            SimpleNamespace(
                tcgetattr=lambda _fd: attrs,
                ISIG=1,
                VINTR=0,
            ),
        )
        scoped.setattr(os, "tcgetpgrp", lambda _fd: 12346, raising=False)
        scoped.setattr(sys, "platform", "linux")
        scoped.setattr(pathlib.Path, "open", fake_open)
        evidence = _collect_sigint_evidence(sess)
    assert evidence["foreground_pgid"] == 12346
    assert evidence["ISIG"] is True
    assert evidence["VINTR"] == repr(b"\x03")
    assert evidence["output_tail"] == repr(bytes(sess.scrollback[-2048:]))[-2048:]
    assert len(evidence["processes"]) == 2
    assert evidence["processes"][1] == {"Pid": "12346", "PPid": str(pid), "SigBlk": "0000"}
    assert "private-name" not in json.dumps(evidence)
    assert "ffff" not in json.dumps(evidence)
    assert set(opened) == set(sources)


@pytest.mark.xdist_group("pty_integration")
class TestTerminalWsIntegration:
    """Integration tests that exercise the full WebSocket PTY lifecycle.

    Pinned to one xdist worker (requires ``--dist loadgroup``): each test forks
    a real PTY shell and waits on multi-second drain budgets for interactive
    output. Under ``-n auto`` these competed with the gateway integration
    subprocess storm for CPU and the forked shell could go unscheduled past the
    10s readiness budget ("shell never produced any PTY output"). Sharing one
    group serializes the heavy PTY tests, matching the gateway-test pattern.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the family calls monkeypatch.undo()
    def _isolated_terminal_shell(self, monkeypatch, tmp_path):
        """Keep ordinary PTY tests out of the operator's login profiles.

        A developer may auto-attach every interactive login to an existing tmux
        session. Letting these transport tests start the configured ``bash -l``
        would then write payloads such as the multibyte boundary probe into that
        live pane. The shim keeps a Bash basename so the readiness-marker branch
        is still exercised, but the real shell starts without system or user
        profiles. Tests whose subject is login-profile behavior explicitly move
        ``HOME`` to their own synthetic profile; those calls retain the shipped
        login-shell path.
        """
        if not terminal.platform_compat.IS_POSIX:
            yield None
            return

        # Resolve Bash from a fixed set of system directories rather than the
        # developer's PATH: a PATH-planted wrapper named ``bash`` is exactly the
        # kind of interposer this fixture exists to keep away from the test
        # payload, so selecting the shell through the ambient PATH would reopen
        # that door. The trusted list still covers the ordinary developer host
        # (``/bin`` and ``/usr/bin`` on Linux, ``/opt/homebrew/bin`` and
        # ``/usr/local/bin`` for a Homebrew Bash on macOS), so the readiness
        # marker branch keeps exercising the same real Bash.
        _TRUSTED_BASH_PATH = os.pathsep.join(
            (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                # NixOS and Nix-managed hosts expose the system Bash here rather
                # than under /bin or /usr/bin.
                "/run/current-system/sw/bin",
            )
        )
        real_bash = shutil.which("bash", path=_TRUSTED_BASH_PATH)
        if real_bash is None:
            # This is an AUTOUSE fixture on the whole PTY-integration class, so a
            # no-op or a skip here would silently hand the sibling tests back to
            # the shipped resolver, which spawns ``$SHELL -l`` under the ambient
            # HOME (the test harness pins KIROCREW_HOME, not HOME) -- exactly the
            # side-effect this fixture exists to prevent, with no assertion left
            # to witness it. Fail loudly instead: a host with no Bash in any
            # trusted location must extend the list above, not run these tests
            # unisolated.
            raise RuntimeError(
                "no Bash found in a trusted system location "
                f"({_TRUSTED_BASH_PATH!r}); extend the trusted list for this host "
                "rather than running the PTY integration tests unisolated"
            )

        ambient_home = tmp_path / "ambient-home"
        ambient_home.mkdir()
        profile_sentinel = tmp_path / "ambient-profile-ran"
        profile_marker = b"__KIROCREW_AMBIENT_PROFILE_RAN__"
        # The profile ALSO installs a PROMPT_COMMAND hook: a developer whose
        # login profile sets PROMPT_COMMAND (e.g. an auto tmux attach, a
        # `history -a`) is the exact case that must not fire inside the transport
        # tests. A `--noprofile --norc` shell never sources this file, so neither
        # the profile body nor the PROMPT_COMMAND it would install ever runs.
        prompt_command_sentinel = tmp_path / "ambient-prompt-command-ran"
        prompt_command_marker = b"__KIROCREW_AMBIENT_PROMPT_COMMAND_RAN__"
        (ambient_home / ".bash_profile").write_text(
            "printf '__KIROCREW_AMBIENT_PROFILE_RAN__\\n'\n"
            f": > {shlex.quote(str(profile_sentinel))}\n"
            "export PROMPT_COMMAND="
            + shlex.quote(
                "printf '__KIROCREW_AMBIENT_PROMPT_COMMAND_RAN__\\n'; "
                f": > {shlex.quote(str(prompt_command_sentinel))}"
            )
            + "\n"
        )

        shim_dir = tmp_path / "isolated-shell"
        shim_dir.mkdir()
        shim = shim_dir / "bash"
        shim.write_text("#!/bin/sh\n" f"exec {shlex.quote(real_bash)} --noprofile --norc -i\n")
        shim.chmod(0o755)

        ambient_home_text = str(ambient_home)
        original_resolve = terminal._resolve_shell
        original_resolve_with_fences = terminal._resolve_shell_with_fence_shells
        monkeypatch.setenv("HOME", ambient_home_text)
        # The readiness helper deliberately preserves a PROMPT_COMMAND exported
        # by a real gateway. Generic tests must not execute the developer's
        # exported hook; the dedicated preservation test installs its own value
        # after this fixture runs.
        monkeypatch.delenv("PROMPT_COMMAND", raising=False)
        # An operator may export an ABSOLUTE HISTFILE from their own dotfiles.
        # Pinning HOME does not contain it: Bash reads HISTFILE straight from the
        # environment, and the interactive teardown flushes history on SIGHUP, so
        # a stale absolute value would write these tests' commands outside
        # tmp_path. Point it inside the temp home to keep the run self-contained.
        monkeypatch.setenv("HISTFILE", str(ambient_home / ".bash_history"))

        def _profiles_are_under_test() -> bool:
            return os.environ.get("HOME") != ambient_home_text

        def _resolve(cfg):
            if not terminal.platform_compat.IS_POSIX or _profiles_are_under_test():
                return original_resolve(cfg)
            return str(shim), None

        def _resolve_with_fences(cfg):
            if not terminal.platform_compat.IS_POSIX or _profiles_are_under_test():
                return original_resolve_with_fences(cfg)
            return str(shim), None, {}

        monkeypatch.setattr(terminal, "_resolve_shell", _resolve)
        monkeypatch.setattr(terminal, "_resolve_shell_with_fence_shells", _resolve_with_fences)
        yield {
            "marker": profile_marker,
            "sentinel": profile_sentinel,
            "prompt_command_marker": prompt_command_marker,
            "prompt_command_sentinel": prompt_command_sentinel,
            "shell": shim,
        }

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX login-profile isolation; Windows uses ConPTY",
    )
    @pytest.mark.asyncio
    async def test_default_shell_does_not_source_ambient_profiles(
        self,
        monkeypatch,
        tmp_path,
        _isolated_terminal_shell,
    ):
        """The ordinary integration shell cannot execute an ambient profile."""
        isolation = _isolated_terminal_shell
        assert isolation is not None
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        output = bytearray()
        ready_seen = False
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/profile-isolation") as ws:
                    loop = asyncio.get_event_loop()
                    # flake-ok: pre-existing real loop-clock deadline that bounds the 15 s wait for the isolated shell's ready frame, moved verbatim; tracked for a de-flake pass
                    deadline = loop.time() + 15
                    # flake-ok: pre-existing real loop-clock read that ends the ready-frame loop once the 15 s window lapses, moved verbatim; tracked for a de-flake pass
                    while loop.time() < deadline:
                        # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 15 s ready window, moved verbatim; tracked for a de-flake pass
                        msg = await ws.receive(timeout=deadline - loop.time())
                        if msg.type == web.WSMsgType.BINARY:
                            output.extend(msg.data)
                        elif msg.type == web.WSMsgType.TEXT:
                            if json.loads(msg.data).get("type") == "ready":
                                ready_seen = True
                                break
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break
                    await ws.close()
        finally:
            spawned = registry.get("profile-isolation")
            if spawned is not None:
                await terminal._kill_session(spawned)

        assert ready_seen, "isolated Bash never emitted its readiness marker"
        # The reader end reaps the session as its shell goes, so the registry
        # entry is gone once the teardown above returns. Assert on the session
        # object captured before the kill.
        assert spawned is not None
        assert spawned.shell == str(isolation["shell"])
        assert isolation["marker"] not in bytes(output)
        assert not isolation["sentinel"].exists()
        # A PROMPT_COMMAND set by the ambient login profile (e.g. a developer's
        # auto tmux attach) must not fire either: --noprofile --norc never
        # sources the profile that would export it.
        assert isolation["prompt_command_marker"] not in bytes(
            output
        ), "the ambient profile's PROMPT_COMMAND ran inside the isolated shell"
        assert not isolation["prompt_command_sentinel"].exists()

    @pytest.mark.asyncio
    async def test_ws_spawn_and_disconnect(self, monkeypatch, tmp_path):
        """Connect via WS, spawn a PTY, then disconnect — session stays in registry."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/test-sess-1") as ws:
                # The upgrade precedes setup; readiness proves publication.
                await _recv_control(ws, "ready")
                assert "test-sess-1" in registry
                sess = registry["test-sess-1"]
                assert terminal._sess_alive(sess)  # alive (pty or ConPTY backend)
                await ws.close()

            # After WS close, session stays (orphan reaper handles cleanup)
            assert "test-sess-1" in registry
            sess = registry["test-sess-1"]
            assert sess.ws is None
            assert sess.last_ws_disconnect is not None

            # Cleanup: kill the PTY
            await terminal._kill_session(sess)

    @pytest.mark.asyncio
    async def test_ws_ping_pong(self, monkeypatch, tmp_path):
        """Send a ping control message, receive pong."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/ping-sess") as ws:
                await ws.send_str(json.dumps({"type": "ping"}))
                assert await _recv_control(ws, "pong") == {"type": "pong"}
                await ws.close()

            await terminal._kill_session(registry["ping-sess"])

    @pytest.mark.asyncio
    async def test_ws_resize(self, monkeypatch, tmp_path):
        """Send a resize control message, verify session cols/rows update."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/resize-sess") as ws:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "resize",
                            "cols": 200,
                            "rows": 50,
                        }
                    )
                )
                # Give a moment for the message to be processed
                # flake-ok: pre-existing 100 ms pause for the resize frame to reach the handler, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.1)
                sess = registry["resize-sess"]
                assert sess.cols == 200
                assert sess.rows == 50
                await ws.close()

            await terminal._kill_session(registry["resize-sess"])

    @pytest.mark.asyncio
    async def test_ws_binary_io(self, monkeypatch, tmp_path):
        """Send binary data through WS, verify PTY receives it."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/io-sess") as ws:
                # Send a command — the PTY should echo something back
                await ws.send_bytes(b"echo hello\n")
                assert len(await _recv_pty_output(ws)) > 0
                await ws.close()

            await terminal._kill_session(registry["io-sess"])

    @pytest.mark.asyncio
    async def test_stream_and_replay_forward_output_verbatim(self, monkeypatch, tmp_path):
        """End-to-end through the real PTY read loop: a credential the user's own
        shell printed reaches the browser unchanged, and so does the scrollback
        replayed on reconnect. Neither path scans or rewrites bytes — the scan
        lives at the selection hand-off (``TestApiTerminalRedact``), which is the
        only place this output can reach a model. Drives the shipped loop rather
        than re-deriving it, so a regression in the wiring fails here too."""
        key = "AKIAIOSFODNN7EXAMPLE"
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        # flake-ok: pre-existing 0.0 enabled-cache stamp that the handler compares with the real monotonic clock, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_enabled_cache", [True, 0.0])
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain(ws, needle, budget=60):
            # The shell echoes the typed line before its output, so read until a
            # marker resolves rather than trusting a frame count. Stops on
            # REDACTED too: if a regression starts scanning this path the needle
            # never arrives, and without that stop the test would burn the whole
            # budget in receive timeouts instead of failing on its assertion.
            seen = b""
            for _ in range(budget):
                msg = await ws.receive(timeout=5)
                if msg.type != web.WSMsgType.BINARY:
                    continue
                seen += msg.data
                if needle in seen or b"REDACTED" in seen:
                    break
            return seen

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/verbatim") as ws:
                # Split the literal in the TYPED line so the shell's echo of it
                # cannot satisfy the needle — only the printf's real output joins
                # the halves. Asserting on the echo would prove far less: it
                # never reaches the redactors' notion of a credential either way.
                await ws.send_bytes(f"printf '{key[:13]}''{key[13:]}\\n'\n".encode())
                live = await _drain(ws, key.encode())
                await ws.close()
            assert key.encode() in live
            assert b"REDACTED" not in live

            # Reconnect: the ring buffer is replayed as the same raw bytes.
            async with client.ws_connect("/api/ws/terminal/verbatim") as ws:
                replay = await _drain(ws, key.encode())
                await ws.close()
            assert key.encode() in replay
            assert b"REDACTED" not in replay
            await terminal._kill_session(registry["verbatim"])

    @pytest.mark.asyncio
    async def test_multibyte_output_survives_read_boundaries(self, monkeypatch, tmp_path):
        """A PTY read ends wherever the kernel had bytes, so a multi-byte character
        is routinely split across two reads. Nothing decodes server-side, so both
        halves are forwarded and the client's own decoder joins them.

        Decoding each read independently (which scanning the stream required)
        turned every such split into two U+FFFD, permanently corrupting CJK,
        emoji and a TUI's box-drawing glyphs — the client cannot recover the
        original bytes from a replacement char.

        The payload is deliberately ONE line with no newline in it, because a
        shell that writes line by line hands the reader whole lines and every
        read then lands on a character boundary by accident — an earlier version
        of this test passed against the corrupting code for exactly that reason.
        The repeated token starts with a 4-byte ghost emoji and is 9 bytes in
        total. A 4096-byte read retains one byte of the next token, so the read
        boundary cuts through that emoji instead of landing between code points."""
        token = "👻Kiro!"
        count = 1000  # 9000 bytes: at least two reads, first splits the emoji
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        # flake-ok: pre-existing 0.0 enabled-cache stamp that the handler compares with the real monotonic clock, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_enabled_cache", [True, 0.0])
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/multibyte") as ws:
                await ws.send_bytes(
                    f"printf '{token}%.0s' $(seq 1 {count}); printf 'DO''NE\\n'\n".encode()
                )
                seen = b""
                for _ in range(400):
                    msg = await ws.receive(timeout=10)
                    if msg.type != web.WSMsgType.BINARY:
                        continue
                    seen += msg.data
                    if b"DONE" in seen:
                        break
                await ws.close()
            await terminal._kill_session(registry["multibyte"])

        assert "\ufffd".encode() not in seen, "a read boundary corrupted a character"
        assert seen.count(token.encode()) >= count

    @pytest.mark.asyncio
    async def test_submitted_line_invalidates_the_cwd_memo(self, monkeypatch, tmp_path):
        """A submitted line may be a `cd`, so it drops the completion route's cwd
        memo and re-arms the title poller; a keystroke that submits nothing
        leaves the memo intact (otherwise every character typed would force a
        fresh cwd probe)."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain_to_pong(ws):
            # The write loop handles frames in order, so a pong proves the
            # preceding binary frame has already been processed. Bounded by a
            # DEADLINE, not a frame count: the shell's own startup output is
            # what shares this stream, and how many frames it takes is a
            # property of the host's shell and profile chain, not of the
            # handler. A macOS runner's login shell emits enough startup frames
            # to spend a 40-frame budget before the pong is reached, which makes
            # a count-bounded drain either report a missing pong or sit in
            # 3-second receives until the file's own timeout fires.
            loop = asyncio.get_event_loop()
            # flake-ok: pre-existing real loop-clock deadline that bounds each pong drain to 20 s, moved verbatim; tracked for a de-flake pass
            deadline = loop.time() + 20
            await ws.send_str(json.dumps({"type": "ping"}))
            while True:
                # flake-ok: pre-existing real loop-clock read of the time left in the 20 s pong window, moved verbatim; tracked for a de-flake pass
                remaining = deadline - loop.time()
                # flake-ok: pre-existing elapsed ceiling that fails the drain once 20 s of real loop time pass without a pong, moved verbatim; tracked for a de-flake pass
                assert remaining > 0, "no pong received within 20s"
                msg = await ws.receive(timeout=remaining)
                if msg.type == web.WSMsgType.TEXT and json.loads(msg.data).get("type") == "pong":
                    return
                if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    raise AssertionError(f"socket closed before pong: {msg.type}")

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/cwd-memo-sess") as ws:
                # Let the shell finish starting before the assertions begin, so
                # its startup output is already drained and cannot interleave
                # with the bookkeeping this test is about.
                await _drain_to_pong(ws)
                sess = registry["cwd-memo-sess"]

                # flake-ok: pre-existing real monotonic stamp that makes the seeded cwd memo read as fresh, moved verbatim; tracked for a de-flake pass
                sess.cwd_probe = (time.monotonic(), "/tmp/old")
                await ws.send_bytes(b"c")
                await _drain_to_pong(ws)
                assert sess.cwd_probe is not None

                await ws.send_bytes(b"d /tmp\r")
                await _drain_to_pong(ws)
                assert sess.cwd_probe is None

                # The submitted line is the one input that can change the cwd,
                # so it re-arms the title poller directly rather than relying on
                # the shell's echo to do it. The session's own reader has to
                # stop setting the flag for that assertion to mean anything,
                # BUT the PTY still has to be drained: a shell blocked writing
                # into a controller buffer nobody reads cannot exit, so leaving
                # the fd undrained makes teardown wait on a child that is wedged
                # until the fd closes. Swap the reader for a drain-only task
                # that consumes output and touches no session state, and keep it
                # in `reader_task` so teardown stops it the way it stops the
                # real one.
                assert sess.reader_task is not None
                sess.reader_task.cancel()
                try:
                    # flake-ok: pre-existing unbounded await of the cancelled reader task before the drain-only task replaces it, moved verbatim; tracked for a de-flake pass
                    await sess.reader_task
                except (asyncio.CancelledError, Exception):
                    pass

                async def _drain_only(fd=sess.master_fd):  # wokeignore:rule=master
                    loop = asyncio.get_running_loop()
                    while True:
                        try:
                            # flake-ok: pre-existing unbounded executor read that parks the drain-only task until the PTY descriptor closes at teardown, moved verbatim; tracked for a de-flake pass
                            if not await loop.run_in_executor(None, os.read, fd, 4096):
                                return
                        except OSError:
                            return

                sess.reader_task = asyncio.ensure_future(_drain_only())
                sess.frames_dirty = False
                await ws.send_bytes(b"cd /tmp\r")
                await _drain_to_pong(ws)
                assert sess.frames_dirty is True

                await ws.close()

            await terminal._kill_session(registry["cwd-memo-sess"])

    @pytest.mark.asyncio
    async def test_invalid_workspace_error_reaches_websocket_client(self, monkeypatch, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        registry: dict = {}
        spawn = MagicMock(side_effect=AssertionError("invalid cwd must not spawn"))
        monkeypatch.setattr(terminal, "_pty", MagicMock(openpty=spawn))
        monkeypatch.setattr(terminal, "_pty_child_env", spawn)
        async with TestClient(TestServer(_make_app(registry=registry))) as client:
            async with client.ws_connect(
                "/api/ws/terminal/invalid-workspace",
                params={"cwd": str(tmp_path / "missing")},
            ) as ws:
                frame = await ws.receive_json(timeout=3)
                assert frame == {
                    "type": "error",
                    "code": "terminal_invalid_cwd",
                    "message": "Terminal working directory is unavailable",
                }
                assert (await ws.receive(timeout=3)).type == web.WSMsgType.CLOSE
        assert registry == {}
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_ws_reconnect_existing_session(self, monkeypatch, tmp_path):
        """Reconnect to an existing PTY session."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            # First connection
            async with client.ws_connect("/api/ws/terminal/recon-sess") as ws:
                await ws.close()

            sess = registry["recon-sess"]
            original_pid = terminal._sess_pid(sess)
            assert sess.ws is None  # disconnected
            # The startup barrier has already completed for this PTY. A new
            # browser must receive the same readiness state after replay.
            sess.shell_ready = True

            # A live PTY keeps its cwd even if the browser's saved project path
            # is missing. Validation applies only when spawning a shell.
            async with client.ws_connect(
                "/api/ws/terminal/recon-sess",
                params={"cwd": str(tmp_path / "missing")},
            ) as ws:
                sess = registry["recon-sess"]
                assert terminal._sess_pid(sess) == original_pid  # same PTY
                assert sess.ws is not None  # reconnected
                assert sess.last_ws_disconnect is None
                for _ in range(40):
                    msg = await ws.receive(timeout=3)
                    if msg.type == web.WSMsgType.TEXT:
                        frame = json.loads(msg.data)
                        if frame.get("type") == "ready":
                            # The reconnecting client learns which shell this
                            # PTY launched. It mints the session id and opens
                            # the socket without asking what got spawned, so
                            # the ready frame is its only source -- and a
                            # caller writing shell syntax into the PTY has to
                            # know which shell will read it.
                            assert frame["shell"] == sess.shell
                            assert frame["shell"]
                            # Fence-nameable shells are reported by ABSOLUTE
                            # path: a bare name would be re-resolved in the
                            # terminal's project cwd, where a relative PATH
                            # entry could supply a planted binary.
                            assert frame["fence_shells"] == sess.fence_shells
                            for name, path in frame["fence_shells"].items():
                                assert os.path.isabs(path), (name, path)
                            break
                else:
                    raise AssertionError("reconnected initialized shell did not send ready")
                await ws.close()

            await terminal._kill_session(registry["recon-sess"])

    @pytest.mark.asyncio
    async def test_ws_takeover_displaced_socket_keeps_new_ws(self, monkeypatch, tmp_path):
        """A displaced socket's cleanup must not clobber the takeover socket.

        A second window (e.g. the terminal panel popping out) reconnects to a
        session WHILE the first window's WS is still attached. The handler
        replaces ``sess.ws`` with the new socket; when the displaced handler's
        write loop then unwinds, its ``finally`` block must leave ``sess.ws``
        pointing at the live takeover socket -- clearing it would silence PTY
        output to the new window even though it is connected.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        audit = MagicMock()
        monkeypatch.setattr(terminal, "_sel", lambda: audit)

        async def leave_displaced_socket_open(*_args, **_kwargs):
            # A bounded close may time out. Keep the predecessor readable so
            # this integration test exercises the stale-handler fallback.
            return None

        monkeypatch.setattr(
            terminal,
            "_close_terminal_ws_bounded",
            leave_displaced_socket_open,
        )

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            ws1 = await client.ws_connect("/api/ws/terminal/takeover-sess")
            await _recv_control(ws1, "ready")
            sess = registry["takeover-sess"]
            original_pid = terminal._sess_pid(sess)
            first_ws = sess.ws
            assert first_ws is not None

            # Second client takes over the session while ws1 is still open.
            ws2 = await client.ws_connect("/api/ws/terminal/takeover-sess")
            sess = registry["takeover-sess"]
            assert terminal._sess_pid(sess) == original_pid  # same PTY
            takeover_ws = sess.ws
            assert takeover_ws is not None
            assert takeover_ws is not first_ws  # replaced by the new socket

            # Multiple queued stale input frames terminate the displaced
            # handler after one coarse audit; no terminal content is recorded.
            # flake-ok: pre-existing real monotonic stamp on the owner's cwd memo that the displaced input must leave untouched, moved verbatim; tracked for a de-flake pass
            cwd_probe = (time.monotonic(), "/tmp/takeover-owner")
            sess.cwd_probe = cwd_probe
            owned_size = (sess.cols, sess.rows)
            await ws1.send_bytes(b"echo displaced-must-not-run\r")
            await ws1.send_bytes(b"echo still-displaced\r")
            for _ in range(40):
                message = await ws1.receive(timeout=3)
                if message.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSED,
                    web.WSMsgType.ERROR,
                ):
                    break
            assert sess.cwd_probe == cwd_probe
            assert (sess.cols, sess.rows) == owned_size
            input_denials = [
                call.kwargs
                for call in audit.log_api_access.call_args_list
                if call.kwargs.get("operation") == "terminal.ws.input"
            ]
            assert input_denials == [
                {
                    "caller": "testuser",
                    "operation": "terminal.ws.input",
                    "outcome": "denied",
                    "source": "dashboard",
                    "resources": "session=takeover-sess,stale_owner=1",
                }
            ]

            # Its cleanup must leave the takeover socket authoritative.
            def takeover_socket_is_authoritative():
                return sess.ws is takeover_ws and sess.last_ws_disconnect is None

            await async_wait_until(
                takeover_socket_is_authoritative,
                describe=lambda: f"ws={sess.ws!r} last_ws_disconnect={sess.last_ws_disconnect!r}",
            )
            assert sess.ws is takeover_ws  # NOT clobbered to None
            assert sess.last_ws_disconnect is None

            # A third connection displaces ws2. Multiple stale resize frames
            # likewise produce one audit and cannot alter the PTY dimensions.
            ws3 = await client.ws_connect("/api/ws/terminal/takeover-sess")
            latest_ws = sess.ws
            assert latest_ws is not None
            assert latest_ws is not takeover_ws
            await ws2.send_str(json.dumps({"type": "resize", "cols": 321, "rows": 123}))
            await ws2.send_str(json.dumps({"type": "resize", "cols": 322, "rows": 124}))
            for _ in range(40):
                message = await ws2.receive(timeout=3)
                if message.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSED,
                    web.WSMsgType.ERROR,
                ):
                    break
            assert (sess.cols, sess.rows) == owned_size
            resize_denials = [
                call.kwargs
                for call in audit.log_api_access.call_args_list
                if call.kwargs.get("operation") == "terminal.ws.resize"
            ]
            assert resize_denials == [
                {
                    "caller": "testuser",
                    "operation": "terminal.ws.resize",
                    "outcome": "denied",
                    "source": "dashboard",
                    "resources": "session=takeover-sess,stale_owner=1",
                }
            ]

            # The latest owner still works end-to-end.
            await ws3.send_str(json.dumps({"type": "ping"}))
            got_pong = False
            for _ in range(40):
                msg = await ws3.receive(timeout=3)
                if msg.type == web.WSMsgType.TEXT and json.loads(msg.data).get("type") == "pong":
                    got_pong = True
                    break
            assert got_pong

            await ws3.close()
            await terminal._kill_session(registry["takeover-sess"])

    @pytest.mark.asyncio
    async def test_non_owner_cannot_list_or_displace_owned_terminal(self):
        owner_ws = MagicMock()
        owner_ws.closed = False
        sess = _make_session(session_id="owned-session", ws=owner_ws)
        sess.scrollback.extend(b"owner scrollback")
        registry = {"owned-session": sess}
        app = _make_app(registry=registry, user="owner", owner_id="owner")

        from aiohttp import WSServerHandshakeError
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                "/api/terminal/sessions",
                headers={"X-Test-User": "intruder"},
            )
            assert response.status == 403
            assert (await response.json())["code"] == "owner_only"
            assert "owned-session" not in await response.text()

            with pytest.raises(WSServerHandshakeError) as exc_info:
                await client.ws_connect(
                    "/api/ws/terminal/owned-session",
                    headers={"X-Test-User": "intruder"},
                )
            assert exc_info.value.status == 403

        assert sess.ws is owner_ws

    @pytest.mark.asyncio
    async def test_ws_invalid_json_ignored(self, monkeypatch, tmp_path):
        """Invalid JSON text frames are silently ignored."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/json-sess") as ws:
                await ws.send_str("not valid json")
                # Should not crash — send a ping to verify connection alive
                await ws.send_str(json.dumps({"type": "ping"}))
                assert await _recv_control(ws, "pong") == {"type": "pong"}
                await ws.close()

            await terminal._kill_session(registry["json-sess"])

    @pytest.mark.asyncio
    async def test_ws_windows_spawns_conpty(self, monkeypatch, tmp_path):
        """On Windows a new WS session spawns a ConPTY-backed shell instead of
        the old 'not supported' refusal. WindowsPty is mocked so the test needs
        no real pseudo-console (and runs on POSIX CI without pywinpty); forcing
        IS_WINDOWS makes the exercised code path identical on Windows and POSIX.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setattr(terminal.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)

        class _FakeWinPty:
            def __init__(self, argv, cwd=None, env=None, cols=80, rows=24):
                # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid moved verbatim; the process double is never signalled
                self.pid = 4321
                self._alive = True
                self._reads = iter((b"PS> ", b""))

            def read(self, size=4096):
                return next(self._reads)  # prompt, then EOF

            def write(self, data):
                return len(data)

            def resize(self, cols, rows):
                pass

            def isalive(self):
                return self._alive

            def terminate(self, force=True):
                self._alive = False

        monkeypatch.setattr("kiro_crew.conpty.WindowsPty", _FakeWinPty)

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/winok-sess") as ws:
                # A ConPTY session is registered (not refused).
                assert "winok-sess" in registry
                sess = registry["winok-sess"]
                assert sess.winpty is not None
                assert sess.proc is None  # Windows backend has no asyncio proc
                prompt = await ws.receive(timeout=3)
                ready = await ws.receive(timeout=3)
                assert prompt.type == web.WSMsgType.BINARY
                assert prompt.data == b"PS> "
                assert ready.type == web.WSMsgType.TEXT
                frame = json.loads(ready.data)
                assert frame["type"] == "ready"
                assert frame["shell"] == sess.shell
                assert frame["shell"]
                assert frame["fence_shells"] == sess.fence_shells
                await ws.close()

        if "winok-sess" in registry:
            await terminal._kill_session(registry["winok-sess"])

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX SIGINT via PTY; on Windows Ctrl+C is handled inside ConPTY",
    )
    @pytest.mark.asyncio
    async def test_ws_ctrl_c_delivers_sigint(self, monkeypatch, tmp_path):
        """Send \\x03 (Ctrl+C) and verify the child process receives SIGINT.

        Deflake notes: the original version used three fixed ``asyncio.sleep``
        calls (1.0s + 1.0s + 1.5s) which intermittently fired before the shell
        had printed its prompt, echoed ``sleep 30``, or recovered after SIGINT
        on a busy CI host — leaving the final ``echo SIGINT_OK`` probe stuck
        in the input buffer of a shell that hadn't yet returned to a prompt.
        Replaced with bounded "drain until marker appears in accumulated PTY
        output" helpers: deterministic on a fast host, falls back to a
        generous overall budget on a slow one.
        """
        await self._drive_ctrl_c_through_the_pty(monkeypatch, tmp_path)

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX SIGINT via PTY; on Windows Ctrl+C is handled inside ConPTY",
    )
    @pytest.mark.asyncio
    async def test_ws_ctrl_c_reaches_the_child_when_the_gateway_ignores_sigint(
        self, monkeypatch, tmp_path
    ):
        """A gateway that inherited ``SIGINT`` as ignored still ships a live Ctrl+C.

        ``SIG_IGN`` survives ``exec``: a shell script that backgrounds the gateway
        with ``&`` hands it SIGINT and SIGQUIT ignored, and an interactive shell
        keeps a signal ignored on entry ignored in every command it runs, so
        without a reset in the spawn path ``sleep`` here never dies and Ctrl+C is
        dead in every terminal that gateway opens -- with bash's own disposition
        looking normal. The post-exec shim restores the login defaults; this
        pins it by running the whole Ctrl+C exchange from a process whose SIGINT
        is ignored. Restored in ``finally`` so the disposition never leaks past
        this test into the worker.
        """
        import signal

        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            await self._drive_ctrl_c_through_the_pty(monkeypatch, tmp_path)
        finally:
            signal.signal(signal.SIGINT, previous)

    async def _drive_ctrl_c_through_the_pty(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/sigint-sess") as ws:
                    await self._exchange_ctrl_c(ws, registry)
        finally:
            # On the failure path as well: a ``sleep`` that ignored SIGINT, the
            # shell waiting on it and the reader task must not outlive the test.
            sess = registry.get("sigint-sess")
            if sess is not None:
                await terminal._kill_session(sess)

    @staticmethod
    async def _exchange_ctrl_c(ws, registry):
        # The backend's Bash init stream emits readiness only after the
        # login profile chain returns. Wait for that control frame
        # before writing the first probe, exactly as Run in terminal
        # does; prompt text and timing are deliberately irrelevant.
        loop = asyncio.get_event_loop()
        # flake-ok: pre-existing real loop-clock deadline that bounds the 15 s wait for the post-profile ready frame, moved verbatim; tracked for a de-flake pass
        ready_deadline = loop.time() + 15
        ready_seen = False
        # flake-ok: pre-existing real loop-clock read that ends the ready-frame loop once the 15 s window lapses, moved verbatim; tracked for a de-flake pass
        while loop.time() < ready_deadline:
            # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 15 s ready window, moved verbatim; tracked for a de-flake pass
            msg = await ws.receive(timeout=ready_deadline - loop.time())
            if msg.type == web.WSMsgType.TEXT:
                if json.loads(msg.data).get("type") == "ready":
                    ready_seen = True
                    break
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
        assert ready_seen, "shell never emitted the post-profile ready frame"

        # Drive the PTY off INPUT ECHO, not an unsolicited prompt. A
        # login shell on a minimal build host (no MOTD, empty PS1) may
        # render no recognizable prompt. The probe confirms the shell
        # is interactive and consuming stdin without depending on one.
        await ws.send_bytes(b"echo __PTY_READY__\n")
        ready = await _drain_ws_until(
            ws,
            lambda b: b"__PTY_READY__" in _unwrapped(b),
            budget_secs=15,
        )
        assert b"__PTY_READY__" in _unwrapped(ready), (
            "shell never echoed the readiness probe — PTY input/echo " "path is not live"
        )

        # Run a long-lived sleep in the foreground; drain until we see
        # the command echoed back (so we know the shell is processing
        # it, not buffering it pre-prompt). The duration is chosen to be
        # far larger than the SIGINT-verification budget below: a
        # genuinely dropped SIGINT must leave `sleep` running for the
        # whole budget, so it can never end on its own and let the shell
        # run the queued marker echo (which would be a false pass). Here
        # this drain WANTS the line-discipline echo — it only proves the
        # shell received the input, so matching the echoed command text
        # is correct.
        await ws.send_bytes(b"sleep 120\n")
        echoed = await _drain_ws_until(
            ws,
            lambda b: b"sleep120" in _unwrapped(b),
            budget_secs=5,
        )
        assert b"sleep120" in _unwrapped(echoed), (
            "shell did not echo `sleep 120` within 5s — "
            f"input may not have reached an interactive shell: {echoed[-200:]!r}"
        )

        # The echo above is emitted by the line discipline the instant the
        # bytes arrive -- BEFORE the shell has read the line and put ``sleep``
        # in the foreground. A ``\x03`` that lands in that window is a VINTR
        # on a canonical-mode PTY: the kernel FLUSHES the unread ``sleep 120``
        # line and signals the shell at its prompt, which redraws it and then
        # runs the marker echo -- a pass with no ``sleep`` ever born, and so
        # no evidence about whether the child would have received SIGINT. The
        # sweep saw exactly that: the run that passed took 48 ms while the
        # four that failed showed ``sleep`` alive with SIGINT ignored. So wait
        # until the terminal's foreground process group differs from the
        # shell's own before the first Ctrl+C: bash runs a job in its own
        # group and hands it the terminal, so that transition IS ``sleep``
        # being the thing Ctrl+C will reach.
        sess = registry["sigint-sess"]
        shell_pgid = sess.proc.pid
        job_in_foreground = await _drain_ws_while(
            ws,
            lambda: _pty_foreground_pgid(sess) not in (shell_pgid, None),
            budget_secs=10,
        )
        assert job_in_foreground, (
            "the shell never handed the terminal to a foreground job: "
            f"foreground pgid is still {_pty_foreground_pgid(sess)} "
            f"(shell {shell_pgid})"
        )

        # Deliver SIGINT and confirm the child actually received it.
        # This step is racy against shell scheduling on a loaded CI host:
        # under this class's xdist integration group the forked shell /
        # reader thread can go unscheduled past a fixed budget, so a single
        # marker drain can time out even when delivery would eventually
        # succeed. Handle it by re-poking with Ctrl+C and re-probing the
        # marker over a generous overall budget, rather than the old "\x03
        # once, wait for a prompt redraw, probe once" — a login shell on a
        # minimal host may render no prompt at all (see the readiness-gate
        # note above), which made the prompt drain a pure time sink and
        # left the single probe to absorb the whole race.
        #
        # EXECUTION-only marker: the probe command is written so the PTY
        # line-discipline echo of our own keystrokes never contains the
        # search token. The typed bytes are ``echo SIG''INT_OK`` (an
        # empty '' splits the literal), so the echoed input reads
        # ``SIG''INT_OK`` — no ``SIGINT_OK`` substring — while only the
        # shell's *execution* of the echo emits the concatenated
        # ``SIGINT_OK`` on stdout. A match therefore proves the shell ran
        # a command, i.e. SIGINT killed the foreground ``sleep`` and
        # returned the shell to its prompt. Matching the bare echoed input
        # (the previous version) let the test pass even when SIGINT was
        # never delivered — a false pass that would hide a real
        # terminal-signal regression.
        # flake-ok: pre-existing real loop-clock deadline for the 25 s Ctrl+C retry budget, moved verbatim; tracked for a de-flake pass
        overall_deadline = loop.time() + 25
        found = False
        while True:
            # flake-ok: pre-existing real loop-clock read of the 25 s Ctrl+C budget left, which clamps each marker drain, moved verbatim; tracked for a de-flake pass
            remaining = overall_deadline - loop.time()
            if remaining <= 0:
                break
            # Ctrl+C (ETX): kills the foreground `sleep` if it is
            # running, or harmlessly aborts an empty prompt line if a
            # previous iteration already recovered the shell.
            await ws.send_bytes(b"\x03")
            await ws.send_bytes(b"echo SIG''INT_OK\n")
            # Clamp each drain to the remaining budget so the overall
            # wait cannot overshoot ``overall_deadline`` by a full drain.
            tail = await _drain_ws_until(
                ws,
                lambda b: b"SIGINT_OK" in _unwrapped(b),
                budget_secs=min(5.0, remaining),
            )
            if b"SIGINT_OK" in _unwrapped(tail):
                found = True
                break
            # Signal may instead have torn down the whole session — that
            # is also a valid "SIGINT was delivered" outcome.
            if sess.proc.returncode is not None:
                break

        # Success: the shell executed a command after Ctrl+C (SIGINT
        # killed sleep, shell continued) OR the process exited (signal
        # was delivered, just tore the whole session down). A dropped
        # SIGINT leaves `sleep 120` running for the whole 25s budget, so
        # neither branch can become true — the test correctly fails.
        assert found or sess.proc.returncode is not None, _sigint_failure_evidence(sess)
        if found:
            # The shell only runs the marker once its foreground job is gone,
            # so the terminal is back in the shell's own group.
            assert _pty_foreground_pgid(sess) == shell_pgid, _sigint_failure_evidence(sess)
        await ws.close()

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS or not shutil.which("bash"),
        reason="POSIX login-shell semantics; needs a real bash on PATH",
    )
    @pytest.mark.asyncio
    async def test_ws_bash_runs_a_login_guarded_profile(self, monkeypatch, tmp_path):
        """A profile stanza behind a login-shell guard must
        run in a Kiro Crew terminal.

        The shell is spawned with ``-l``, so ``shopt -q login_shell`` is true and
        the guard passes. Emulating the profile chain from an rc file (what an ``--init-file`` rc file did) cannot substitute: the option is read-only,
        stays off, and every such stanza silently no-ops — which is precisely
        what the reporter saw. On that code this test fails at the final assert
        with an EMPTY value, having still received the ready frame.

        EXECUTION-only marker, same idiom as the SIGINT test above: the typed
        bytes carry ``PROFILE''_OK`` so the PTY's echo of our own keystrokes can
        never contain the token — only the shell's execution of the echo emits
        the concatenated form, and only if the guarded export really ran.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bash_profile").write_text(
            "if shopt -q login_shell; then\n" "    export KC_5885_PROFILE=PROFILE_OK\n" "fi\n"
        )
        # ~/.bashrc is deliberately NOT part of this contract: an interactive
        # login bash has never read it, on any arm of this bug.
        (home / ".bashrc").write_text("export KC_5885_PROFILE=BASHRC_WRONG\n")

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": True, "shell": "bash"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain_until(ws, token: bytes, *, budget_secs: float) -> bytes:
            loop = asyncio.get_event_loop()
            # flake-ok: pre-existing real loop-clock deadline that bounds the profile-marker drain to its budget, moved verbatim; tracked for a de-flake pass
            deadline = loop.time() + budget_secs
            buf = bytearray()
            while True:
                # flake-ok: pre-existing real loop-clock read of the drain budget left, which returns the buffer once it runs out, moved verbatim; tracked for a de-flake pass
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return bytes(buf)
                try:
                    msg = await ws.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    return bytes(buf)
                if msg.type == web.WSMsgType.BINARY:
                    buf.extend(msg.data)
                    if token in bytes(buf):
                        return bytes(buf)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    return bytes(buf)

        out = b""
        # A real Bash is spawned below, so every exit from here on — assertion,
        # timeout, cancellation — must still reap it. TestClient closes the
        # socket, not the child.
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/login-sess") as ws:
                    loop = asyncio.get_event_loop()
                    # flake-ok: pre-existing real loop-clock deadline that bounds the 15 s wait for the post-profile ready frame, moved verbatim; tracked for a de-flake pass
                    ready_deadline = loop.time() + 15
                    ready_seen = False
                    # flake-ok: pre-existing real loop-clock read that ends the ready-frame loop once the 15 s window lapses, moved verbatim; tracked for a de-flake pass
                    while loop.time() < ready_deadline:
                        # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 15 s ready window, moved verbatim; tracked for a de-flake pass
                        msg = await ws.receive(timeout=ready_deadline - loop.time())
                        if msg.type == web.WSMsgType.TEXT:
                            if json.loads(msg.data).get("type") == "ready":
                                ready_seen = True
                                break
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break
                    assert ready_seen, "shell never emitted the post-profile ready frame"

                    await ws.send_bytes(b"echo KC=$KC_5885_PROFILE.\n")
                    out = await _drain_until(ws, b"KC=PROFILE_OK.", budget_secs=15)
                    await ws.close()
        finally:
            spawned = registry.get("login-sess")
            if spawned is not None:
                await terminal._kill_session(spawned)

        assert b"KC=PROFILE_OK." in out, (
            "a login-guarded profile stanza did not run: the terminal is not a "
            "login shell (#5885). Observed PTY tail: "
            f"{out[-400:]!r}"
        )
        assert b"BASHRC_WRONG" not in out

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS or not shutil.which("bash"),
        reason="POSIX login-shell semantics; needs a real bash on PATH",
    )
    @pytest.mark.asyncio
    async def test_ws_bash_keeps_a_profile_appended_prompt_command(
        self,
        monkeypatch,
        tmp_path,
    ):
        """The readiness hook withdraws ITSELF, never the user's own prompt hook.

        Bash 5.1+ lets `PROMPT_COMMAND+=(...)` turn the exported scalar into an
        array whose element zero is still the hook, so a scalar-equality test
        alone would match and unset the WHOLE array — taking the profile's own
        element with it and silently disabling it after the first prompt.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bash_profile").write_text(
            "export KC_5885_PROFILE=PROFILE_OK\n" "PROMPT_COMMAND+=(true)\n"
        )

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": True, "shell": "bash"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        out = bytearray()
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/pcarray-sess") as ws:
                    loop = asyncio.get_event_loop()
                    # flake-ok: pre-existing real loop-clock deadline that bounds the 20 s wait for the PROMPT_COMMAND probe output, moved verbatim; tracked for a de-flake pass
                    deadline = loop.time() + 20
                    # flake-ok: pre-existing real loop-clock read that ends the probe loop once the 20 s window lapses, moved verbatim; tracked for a de-flake pass
                    while loop.time() < deadline:
                        # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 20 s probe window, moved verbatim; tracked for a de-flake pass
                        msg = await ws.receive(timeout=deadline - loop.time())
                        if msg.type == web.WSMsgType.BINARY:
                            out.extend(msg.data)
                            if b"PC1=" in bytes(out):
                                break
                        elif msg.type == web.WSMsgType.TEXT:
                            if json.loads(msg.data).get("type") == "ready":
                                # First prompt reached, so the hook has fired and
                                # made its keep-or-withdraw decision by now.
                                # EXECUTION-only marker: the typed bytes carry
                                # PC''1= so the line-discipline echo of our own
                                # keystrokes cannot satisfy the match below.
                                await ws.send_bytes(b"echo PC''1=${PROMPT_COMMAND[1]:-GONE}.\n")
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break
                    await ws.close()
        finally:
            spawned = registry.get("pcarray-sess")
            if spawned is not None:
                await terminal._kill_session(spawned)

        tail = bytes(out)
        assert b"PC1=true." in tail, (
            "the profile's own PROMPT_COMMAND element did not survive the "
            f"readiness hook's self-withdrawal. PTY tail: {tail[-400:]!r}"
        )

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS or not shutil.which("bash"),
        reason="POSIX login-shell semantics; needs a real bash on PATH",
    )
    @pytest.mark.asyncio
    async def test_ws_bash_profile_assigning_prompt_command_stays_fail_closed(
        self,
        monkeypatch,
        tmp_path,
    ):
        """A profile that ASSIGNS PROMPT_COMMAND drops the hook, and the barrier
        must then stay SHUT rather than open on inferred progress.

        `_bash_ready_env`'s docstring calls this outcome deliberate: the readiness
        marker rides an inherited `PROMPT_COMMAND` because Bash reads an
        `--init-file` only for a NON-login shell, so `PROMPT_COMMAND='history -a'`
        in a profile replaces the hook and the marker never fires. Releasing the
        barrier anyway -- on a timeout, or on a line-discipline guess -- risks
        handing a queued command to a profile still blocked in `read`, which
        consumes it silently: executed never, reported sent. An earlier build shipped such a
        release and had it reviewed back out.

        Every sibling case here covers an arm where the hook SURVIVES: appended
        scalar, appended array, inherited, restored, preserved, blank-inherited.
        This is the arm where it does not. Without it, the barrier could be made
        to open on a clobbered session and the whole suite would stay green --
        which is exactly how the reviewed-out release passed local gates.

        The assertion is deliberately PAIRED. A session that never spawned would
        satisfy "no ready frame" on its own, so the shell is first proved live:
        the profile chain ran, and a typed command executes (client input is not
        gated on `shell_ready`, only the frontend's registration is). The point is
        that the shell is genuinely usable while the gateway's barrier stays shut.

        The remedy directions are tracked separately; this pins only the
        CURRENT deliberate behaviour without obstructing them: directions 1 and 3
        both keep queued injection fail-closed and change only interactive typing,
        so both survive this invariant.
        """
        home = tmp_path / "home"
        home.mkdir()
        # ASSIGN, not append -- this is the shape that replaces the exported
        # readiness hook, and it is the assignment (which clobbers the hook), not
        # the value, that this arm pins. The value is the side-effect-free `:`
        # no-op rather than a realistic `history -a`, deliberately: a login Bash
        # sources the system profile chain before this file, and `history -a`
        # would append to whatever HISTFILE that chain leaves set -- which can be
        # an absolute path outside tmp_path on a real dev box or CI runner. `:`
        # drops the hook just as completely while writing nothing anywhere. The
        # echo is the positive control: a sourced profile's text is not echoed to
        # the PTY, so seeing it proves execution.
        (home / ".bash_profile").write_text("PROMPT_COMMAND=':'\n" "echo KC_PROFILE_RAN\n")

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": True, "shell": "bash"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        out = bytearray()
        ready_frames: list[dict] = []
        typed = False
        shell_ready = None
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/pcassign-sess") as ws:
                    loop = asyncio.get_event_loop()
                    # flake-ok: pre-existing real loop-clock deadline for the 20 s window in which the clobbered hook must send no ready frame, moved verbatim; tracked for a de-flake pass
                    deadline = loop.time() + 20
                    # flake-ok: pre-existing real loop-clock read that ends the observation loop once the 20 s window lapses, moved verbatim; tracked for a de-flake pass
                    while loop.time() < deadline:
                        try:
                            # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 20 s window, moved verbatim; tracked for a de-flake pass
                            msg = await ws.receive(timeout=deadline - loop.time())
                        except asyncio.TimeoutError:
                            # No `ready` is the EXPECTED outcome here, so the
                            # window closing must surface as the assertions below
                            # rather than as a TimeoutError from the harness.
                            break
                        if msg.type == web.WSMsgType.BINARY:
                            out.extend(msg.data)
                            blob = bytes(out)
                            if not typed and b"KC_PROFILE_RAN" in blob:
                                # Profile chain done, so the shell is at its first
                                # prompt. EXECUTION-only marker: the typed bytes
                                # carry LIVE''OK so the line-discipline echo of
                                # our own keystrokes cannot satisfy the match.
                                typed = True
                                await ws.send_bytes(b"echo LIVE''OK=1.\n")
                            elif typed and b"LIVEOK=1." in blob:
                                break
                        elif msg.type == web.WSMsgType.TEXT:
                            frame = json.loads(msg.data)
                            if frame.get("type") == "ready":
                                ready_frames.append(frame)
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break
                    await ws.close()
        finally:
            spawned = registry.get("pcassign-sess")
            if spawned is not None:
                shell_ready = spawned.shell_ready
                await terminal._kill_session(spawned)

        tail = bytes(out)
        # Positive control first: without these two, "no ready frame" is also
        # satisfied by a session that never started, and the test would pass for
        # the wrong reason.
        assert b"KC_PROFILE_RAN" in tail, (
            "the login profile never ran, so this session proves nothing about "
            f"the readiness barrier. PTY tail: {tail[-400:]!r}"
        )
        assert b"LIVEOK=1." in tail, (
            "the shell never executed a typed command, so it was not interactive "
            f"and the barrier was not the thing under test. PTY tail: {tail[-400:]!r}"
        )
        # The invariant.
        assert ready_frames == [], (
            "the readiness barrier OPENED for a session whose profile ASSIGNED "
            "PROMPT_COMMAND and therefore dropped the hook. Releasing here risks "
            "handing a queued command to a profile still reading input, which is "
            f"why #7641's release was reviewed out. frames={ready_frames!r}"
        )
        assert shell_ready is False, (
            "shell_ready must stay False while the hook is clobbered (None means "
            f"the session was never registered, which is also a failure), got {shell_ready!r}"
        )

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS or not shutil.which("bash"),
        reason="POSIX login-shell semantics; needs a real bash on PATH",
    )
    @pytest.mark.asyncio
    async def test_ws_bash_restores_an_inherited_prompt_command(
        self,
        monkeypatch,
        tmp_path,
    ):
        """An exported PROMPT_COMMAND in the GATEWAY's environment is preserved.

        Replacing it loses data rather than a nicety: `history -a` is what makes
        concurrent shells append to HISTFILE instead of the last one to exit
        overwriting it. Drives a real Bash and asks the live shell what
        PROMPT_COMMAND holds once the hook has withdrawn.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bash_profile").write_text("export KC_5885_PROFILE=PROFILE_OK\n")

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": True, "shell": "bash"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")
        # What the operator exported into the gateway's own environment. It
        # PRINTS, so the transcript shows whether it ran at the FIRST prompt
        # (appended after the hook) or only from the second one (restored but
        # not appended) -- the difference this test exists to pin.
        monkeypatch.setenv("PROMPT_COMMAND", "builtin printf PREV_RAN")

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        out = bytearray()
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/ws/terminal/pcprev-sess") as ws:
                    loop = asyncio.get_event_loop()
                    # flake-ok: pre-existing real loop-clock deadline that bounds the 20 s wait for the restored PROMPT_COMMAND probe, moved verbatim; tracked for a de-flake pass
                    deadline = loop.time() + 20
                    # flake-ok: pre-existing real loop-clock read that ends the probe loop once the 20 s window lapses, moved verbatim; tracked for a de-flake pass
                    while loop.time() < deadline:
                        # flake-ok: pre-existing real loop-clock read that sizes each receive to the rest of the 20 s probe window, moved verbatim; tracked for a de-flake pass
                        msg = await ws.receive(timeout=deadline - loop.time())
                        if msg.type == web.WSMsgType.BINARY:
                            out.extend(msg.data)
                            if b"PCNOW=" in _unwrapped(bytes(out)):
                                break
                        elif msg.type == web.WSMsgType.TEXT:
                            if json.loads(msg.data).get("type") == "ready":
                                # EXECUTION-only marker, as above.
                                await ws.send_bytes(b"echo PC''NOW=[${PROMPT_COMMAND-unset}]\n")
                        elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                            break
                    await ws.close()
        finally:
            spawned = registry.get("pcprev-sess")
            if spawned is not None:
                await terminal._kill_session(spawned)

        tail = bytes(out)
        # Compared through ``_unwrapped``: a long host prompt makes the typed probe
        # reach the 80-column margin, and bash redraws the wrap point mid-word
        # (``PC' \r'NOW``), which is where the raw form failed on a hosted runner.
        flat = _unwrapped(tail)
        # (1) It ran at the FIRST prompt, i.e. it was appended after the hook
        # rather than only restored: its output precedes the probe's EXECUTION
        # output. The PTY may echo the input before the first hook finishes;
        # PC''NOW in that echo cannot match the execution-only PCNOW= marker.
        assert (
            b"PREV_RAN" in flat and b"PCNOW=" in flat
        ), f"probe never completed. PTY tail: {tail[-500:]!r}"
        assert flat.index(b"PREV_RAN") < flat.index(b"PCNOW="), (
            "the gateway's exported PROMPT_COMMAND did not run at the first "
            f"prompt, so it was not appended after the hook. PTY: {tail[:600]!r}"
        )
        # (2) The withdrawal restored it instead of unsetting the variable.
        assert b"PCNOW=[builtinprintfPREV_RAN]" in flat, (
            "the gateway's exported PROMPT_COMMAND was not restored after the "
            f"readiness hook withdrew. PTY tail: {tail[-500:]!r}"
        )
        # The Kiro Crew names are gone from the session either way.
        assert b"KIROCREW_TERMINAL_READY" not in tail

    @pytest.mark.asyncio
    async def test_rest_create_list_delete(self, monkeypatch, tmp_path):
        """Full REST lifecycle: create, list, delete."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        # This test seeds a mock session (an unallocatable proc.pid) and deletes
        # it. Stub the tree-kill so teardown does no real process signalling at
        # all; on Windows taskkill /T /PID would still spawn for the number. Real
        # teardown is covered by the ConPTY integration test.
        monkeypatch.setattr(terminal.platform_compat, "kill_process_tree_async", AsyncMock())

        app = _make_app()

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            # Create
            resp = await client.post("/api/terminal/sessions")
            assert resp.status == 200
            body = await resp.json()
            sid = body["session_id"]
            assert len(sid) == 12

            # List (empty — create only returns ID, doesn't spawn PTY)
            resp = await client.get("/api/terminal/sessions")
            assert resp.status == 200

            # Delete — session not in registry (no WS connected), returns 404
            resp = await client.delete(f"/api/terminal/sessions/{sid}")
            assert resp.status == 404

            # Seed registry directly, then delete
            from kiro_crew.dashboard.handlers import terminal as _term

            registry = _term._get_registry(
                type("R", (), {"app": client.app})()  # type: ignore[arg-type]
            )
            registry[sid] = _make_session(session_id=sid)
            # No PTY was spawned; a fake descriptor could close the test server.
            registry[sid].master_fd = -1  # wokeignore:rule=master
            resp = await client.delete(f"/api/terminal/sessions/{sid}")
            assert resp.status == 200
            body = await resp.json()
            assert body["deleted"] == sid
            assert sid not in registry
