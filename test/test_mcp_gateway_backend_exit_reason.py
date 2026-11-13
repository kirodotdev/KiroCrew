"""A pooled MCP backend that dies on its own names its cause at WARNING.

``_pump_stderr`` logs each backend stderr line only at DEBUG, and the daemon runs at
INFO, so what a server writes to stderr about its own exit reaches the log only
through the line that names the death. These tests pin that line: a REAL child
that prints a traceback and exits is named once at WARNING with its last stderr
lines, while the ``backend gone`` error its waiting stub is sent carries none of
them; the heartbeat's reaped check names a backend whose stdout never closed the
same way, and so does a ``shutdown`` that finds it already reaped, as an eviction
does, and so do the first-``initialize`` deadline and the respawn prime's timeout
for a server that died inside the handshake; a ``shutdown`` that cancels the
stdout pump inside the wait for the rest of the stderr still finds the waiting
stub answered and the death named; a death the gateway caused is not named: an
orderly shutdown, a recycle whose kill yields before it records its reason, and a
cancelled shutdown the server then exits after, seen by the pump or by a later
eviction; the warning's tail is ``agent_sdk.published_stderr_tail`` of the kept
lines, so a tail whose lines rejoin into a secret is withheld whole; a generated
PEM private key a REAL child prints, one short and one longer than the ring,
leaves no body line in the tail or the warning; a secret the per-line cut would
split is kept redacted; and the kept tail is bounded in lines and length, kept
one printable line each, and left out of the backend's repr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _make_backend, _pool_key, _reader

from kiro_crew import platform_compat
from kiro_crew.acp.stderr_tail import STDERR_WITHHELD
from kiro_crew.agent_sdk import published_stderr_tail
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway import backend_wire
from kiro_crew.mcp_gateway.backend import Backend, BackendGone, _PendingRequest, spawn_backend
from kiro_crew.security import redact
from kiro_crew.testing.wait import (
    FALLBACK_TIMEOUT_SECS,
    async_wait_until,
    await_until,
    finished,
)

LOGGER = "kiro_crew.mcp_gateway.backend"

#: The last line the dying server writes: what an operator needs to read.
CAUSE = "RuntimeError: EXIT_REASON_MARK app bundle failed to load"

#: Waits for the test's go line on stdin, then dies the way a server with a
#: startup error does: a traceback on stderr and a nonzero exit.
_DIES_ON_CUE = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "sys.stderr.write('  File \"server.py\", line 1, in <module>\\n')\n"
    f"sys.stderr.write({CAUSE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(3)\n"
)

#: Says it is ready on stderr, then serves until its stdin closes.
_SERVES_UNTIL_STDIN_CLOSES = (
    "import sys\nsys.stderr.write('ready\\n')\nsys.stderr.flush()\nsys.stdin.read()\n"
)

#: A credential the redactor recognises whole but in neither half.
_SECRET_HEAD = "AKIAIOSF"
_SECRET_TAIL = "ODNN7EXAMPLE"


#: Writes the file its first argument names to its stderr, then a traceback, and
#: exits rc 3: a server that dumped a key on its way down.
_DUMPS_A_FILE_AND_DIES = (
    "import sys\n"
    "with open(sys.argv[1], encoding='ascii') as key:\n"
    "    sys.stderr.write(key.read())\n"
    "sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "sys.stderr.write('  File \"server.py\", line 1, in <module>\\n')\n"
    f"sys.stderr.write({CAUSE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(3)\n"
)

#: Keys generated before a test gives up finding one with a body line the
#: redactor keeps when it reads that line alone. Such a line is common (most keys
#: of either kind have one), so the bound only turns a lost search into a failure.
_KEY_ATTEMPTS = 64


async def _spawn(script: str, work_dir: Path, *args: str) -> Backend:
    """A real backend running ``script``, its stdout pump started as the daemon does."""
    backend = await spawn_backend(
        _pool_key(), sys.executable, ["-c", script, *args], dict(os.environ), str(work_dir)
    )
    backend._stdout_task = asyncio.create_task(backend.run_stdout_pump())
    return backend


def _named(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The WARNING lines that name a backend's death."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.WARNING and " gone: " in r.getMessage()
    ]


def _logged_tail(message: str) -> str:
    """The stderr tail one naming WARNING carries, without its join fields."""
    return message.split("; last stderr: ", 1)[1].rsplit(" event=", 1)[0]


def _generated_pem(
    make: Callable[[], Any], private_format: serialization.PrivateFormat, *, last: int
) -> tuple[str, list[str]]:
    """A freshly generated private key in PEM, and its body lines.

    One of its last ``last`` body lines is a line ``redact`` keeps when it reads
    it alone: the line a tail redacted one line at a time lets through. Every key
    is generated here, so no key is ever written into the repository.
    """
    for _ in range(_KEY_ATTEMPTS):
        pem = (
            make()
            .private_bytes(serialization.Encoding.PEM, private_format, serialization.NoEncryption())
            .decode("ascii")
        )
        body = pem.strip().splitlines()[1:-1]
        if any(redact(line) == line for line in body[-last:]):
            return pem, body
    raise AssertionError(f"no key in {_KEY_ATTEMPTS} had a body line redact() keeps alone")


def _keep(tail: list[str], text: str) -> None:
    """Keep ``text`` in ``tail`` as the stderr pump does a line outside a PEM block."""
    assert backend_wire._keep_stderr_line(tail, text, redact(text), False) is False


async def _key_dump_is_withheld(
    pem: str,
    body: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A REAL child prints ``pem`` and a traceback and exits: the death is named
    with the traceback, and no line of the key's body is kept or logged."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    # The drain ends at the pipe's EOF when the child exits, so the wait is
    # given the suite's bound: a loaded runner cannot outlast it.
    monkeypatch.setattr(backend_mod, "_STDERR_DRAIN_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    key_file = tmp_path / "key.pem"
    key_file.write_text(pem, encoding="ascii")
    backend = await _spawn(_DUMPS_A_FILE_AND_DIES, tmp_path, str(key_file))
    try:
        assert backend._stdout_task is not None
        await await_until(backend._stdout_task, what="the stdout pump ends when the backend dies")
    finally:
        await backend.shutdown()
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "Traceback (most recent call last):" in named[0]
    assert [line for line in body if line in named[0]] == []
    assert [line for line in body if any(line in kept for kept in backend._stderr_tail)] == []
    assert backend._stderr_tail.count(backend_wire._STDERR_PEM_WITHHELD) == 1


@pytest.mark.asyncio
async def test_a_backend_that_dies_on_its_own_is_named_with_its_last_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    # The drain ends at the pipe's EOF when the child exits, so the wait is
    # given the suite's bound: a loaded runner cannot outlast it.
    monkeypatch.setattr(backend_mod, "_STDERR_DRAIN_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    backend = await _spawn(_DIES_ON_CUE, tmp_path)
    try:
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 7, "tools/call")
        backend.stdin.write(b"go\n")
        await backend.stdin.drain()
        assert backend._stdout_task is not None
        await await_until(backend._stdout_task, what="the stdout pump ends when the backend dies")
    finally:
        await backend.shutdown()

    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "Traceback (most recent call last):" in named[0]
    assert "event=backend_gone" in named[0]
    # The stub's error is the gateway's own reason: a pooled backend's other
    # stubs belong to other sessions, so nothing the server printed goes to them.
    err = json.loads(inbox.get_nowait().decode("utf-8"))
    assert err["id"] == 7
    assert err["error"]["message"] == f"backend gone: {backend.dead_reason}"
    assert backend.dead_reason in ("exit rc=3", "stdout EOF")


@pytest.mark.asyncio
async def test_an_orderly_shutdown_is_not_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    backend = await _spawn(_SERVES_UNTIL_STDIN_CLOSES, tmp_path)
    try:
        await async_wait_until(
            lambda: any("stderr: ready" in r.getMessage() for r in caplog.records),
            describe=lambda: "the backend's ready line is never read",
        )
    finally:
        await backend.shutdown()
    assert _named(caplog) == []


@pytest.mark.asyncio
async def test_the_heartbeat_names_a_reaped_backend_whose_stdout_stayed_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend(returncode=3)
    backend._stderr_tail = ["Traceback (most recent call last):", CAUSE]
    assert await backend._heartbeat_once(time.monotonic()) == "gone"
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "process exited rc=3" in named[0] and "reason=process_exit" in named[0]
    assert backend.dead_reason == "process exited rc=3"


@pytest.mark.asyncio
async def test_a_shutdown_inside_the_stderr_wait_still_answers_and_names_the_death(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    # Only the cancel can end the wait: a helper the server spawned holds the
    # stderr pipe open.
    monkeypatch.setattr(backend_mod, "_STDERR_DRAIN_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    backend = _make_backend(stdout=_reader(), returncode=3)
    backend._stderr_tail = [CAUSE]
    backend._stderr_task = asyncio.create_task(asyncio.Event().wait())
    inbox = await backend.attach_stub("s1")
    backend._pending_requests["gw-1"] = _PendingRequest("s1", 7, "tools/call")
    backend._stdout_task = asyncio.create_task(backend.run_stdout_pump())
    try:
        await async_wait_until(
            lambda: backend.dead_reason is not None,
            describe=lambda: "the stdout pump never saw the backend go",
        )
        # The pump is parked in the wait: the death is not named yet.
        assert _named(caplog) == []
    finally:
        # What an eviction of the dead entry does: it cancels the pump there.
        await backend.shutdown()
    err = json.loads(inbox.get_nowait().decode("utf-8"))
    assert err["id"] == 7
    assert err["error"]["message"] == "backend gone: exit rc=3"
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named


@pytest.mark.asyncio
async def test_a_shutdown_that_finds_the_backend_reaped_names_its_death(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    # Reaped while a helper the server spawned holds its stdout open: the pump
    # never ends, and an eviction's shutdown is the first to see the death.
    backend = _make_backend(stdout=_reader(eof=False), returncode=3)
    backend._stderr_tail = ["Traceback (most recent call last):", CAUSE]
    inbox = await backend.attach_stub("s1")
    backend._pending_requests["gw-1"] = _PendingRequest("s1", 7, "tools/call")
    backend._stdout_task = asyncio.create_task(backend.run_stdout_pump())
    await backend.shutdown()
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "exit rc=3" in named[0] and "reason=process_exit" in named[0]
    err = json.loads(inbox.get_nowait().decode("utf-8"))
    assert err["id"] == 7
    assert err["error"]["message"] == "backend gone: exit rc=3"
    assert backend._stdout_task is None


@pytest.mark.asyncio
async def test_the_init_deadline_names_a_backend_that_died_inside_the_handshake(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    # Reaped inside the handshake while a helper the server spawned holds its
    # stdout open: the pump never ends, and the deadline is the first to look.
    backend = _make_backend(returncode=3)
    backend._stderr_tail = ["Traceback (most recent call last):", CAUSE]
    inbox = await backend.attach_stub("s1")
    backend._init_state = "in_flight"
    backend._init_pending.append(("s1", 1))
    backend._pending_requests["gw-1"] = _PendingRequest("__init__", None, "initialize")
    await backend._init_deadline(0)
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "exit rc=3" in named[0] and "reason=process_exit" in named[0]
    err = json.loads(inbox.get_nowait().decode("utf-8"))
    assert err["id"] == 1
    assert err["error"]["message"] == "backend gone: exit rc=3"
    assert backend._init_state == "failed"


@pytest.mark.asyncio
async def test_the_respawn_prime_names_a_backend_that_died_inside_the_handshake(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    backend._stderr_tail = ["Traceback (most recent call last):", CAUSE]
    process = cast(Any, backend.process)
    # The server reads the replayed initialize and dies while a helper it
    # spawned holds its stdout open: only the prime's timeout looks.
    cast(Any, backend.stdin).write = MagicMock(
        side_effect=lambda _payload: setattr(process, "returncode", 3)
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    with pytest.raises(BackendGone, match="exit rc=3"):
        await backend.prime_initialize(init, timeout=0)
    named = _named(caplog)
    assert len(named) == 1 and CAUSE in named[0], named
    assert "exit rc=3" in named[0] and "reason=process_exit" in named[0]


@pytest.mark.asyncio
async def test_a_recycle_whose_kill_yields_first_is_not_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(backend_mod, "SecurityEventLog", MagicMock())
    backend = await _spawn(_SERVES_UNTIL_STDIN_CLOSES, tmp_path)
    kill = platform_compat.kill_process_tree_async

    async def _kill_then_yield(pid: int, sig: int) -> bool:
        killed = await kill(pid, sig)
        # As the Windows kill does (taskkill runs in an executor), the loop
        # runs on before the kill returns: here until the pump saw the death.
        assert backend._stdout_task is not None
        await await_until(backend._stdout_task, what="the stdout pump ends when the kill lands")
        return killed

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _kill_then_yield)
    try:
        await async_wait_until(
            lambda: backend._stderr_tail == ["ready"],
            describe=lambda: "the backend's ready line is never read",
        )
        assert await backend.recycle_if_idle() is True
    finally:
        await backend.shutdown()
    assert _named(caplog) == []
    assert backend.dead_reason == "recycled after last stub detached with in-flight work"


@pytest.mark.asyncio
async def test_a_cancelled_shutdown_is_not_named_when_the_server_then_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = await _spawn(_SERVES_UNTIL_STDIN_CLOSES, tmp_path)
    shutdowns: list[asyncio.Task[None]] = []
    close = backend.stdin.close

    def _close_then_cancel() -> None:
        # The caller is cancelled right after stdin closes, as an eviction's
        # unshielded shutdown or the daemon's teardown can be: the server has
        # not exited on that EOF yet.
        close()
        shutdowns[0].cancel()

    try:
        await async_wait_until(
            lambda: backend._stderr_tail == ["ready"],
            describe=lambda: "the backend's ready line is never read",
        )
        monkeypatch.setattr(backend.stdin, "close", _close_then_cancel)
        shutdowns.append(asyncio.create_task(backend.shutdown()))
        with pytest.raises(asyncio.CancelledError):
            await await_until(shutdowns[0], what="the cancelled shutdown ends")
        assert backend._stdout_task is not None
        await await_until(backend._stdout_task, what="the stdout pump ends when the server exits")
    finally:
        await backend.shutdown()
    assert _named(caplog) == []


@pytest.mark.asyncio
async def test_an_eviction_after_a_cancelled_shutdown_does_not_name_the_death(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    # A helper the server spawned holds its stdout open, so the eviction's
    # shutdown is the first to find the process gone.
    backend = _make_backend()
    backend._stderr_tail = ["ready"]
    process = cast(Any, backend.process)
    process.wait = AsyncMock(side_effect=asyncio.Event().wait)
    shutdowns: list[asyncio.Task[None]] = []
    cast(Any, backend.stdin).close = MagicMock(side_effect=lambda: shutdowns[0].cancel())
    shutdowns.append(asyncio.create_task(backend.shutdown()))
    with pytest.raises(asyncio.CancelledError):
        await await_until(shutdowns[0], what="the cancelled shutdown ends")
    # The server exits on the EOF of the stdin the gateway closed.
    process.returncode = 0
    await backend.shutdown()
    assert _named(caplog) == []


@pytest.mark.asyncio
async def test_a_tail_that_rejoins_into_a_secret_is_withheld_whole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend(returncode=1)
    # The server wraps the key across two lines, as the stderr pump reads them.
    reader = _reader(f"aws key {_SECRET_HEAD}\n".encode(), f"{_SECRET_TAIL}\n".encode())
    await finished(
        backend_mod._pump_stderr(reader, "label", backend._stderr_tail),
        what="the stderr pump drains to EOF",
    )
    assert await backend._heartbeat_once(time.monotonic()) == "gone"
    named = _named(caplog)
    assert len(named) == 1, named
    assert _logged_tail(named[0]) == STDERR_WITHHELD
    assert _SECRET_HEAD not in named[0] and _SECRET_TAIL not in named[0]
    assert backend.dead_reason == "process exited rc=1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lines",
    [
        pytest.param([], id="none-kept"),
        pytest.param(["Traceback (most recent call last):", CAUSE], id="plain"),
        pytest.param([f"aws key {_SECRET_HEAD}", _SECRET_TAIL], id="wrapped-secret"),
        pytest.param([f"aws key {_SECRET_HEAD}{_SECRET_TAIL} rejected"], id="one-line-secret"),
        pytest.param([f"{n} " + "word " * 99 for n in range(5)], id="long-lines"),
    ],
)
async def test_the_warning_tail_is_published_stderr_tail_of_the_kept_lines(
    lines: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend(returncode=3)
    backend._stderr_tail = list(lines)
    await finished(backend._report_backend_exit("exit rc=3"), what="the exit is reported")
    named = _named(caplog)
    assert len(named) == 1, named
    published = published_stderr_tail(lines, " | ", backend_wire._STDERR_TAIL_LINES)
    assert _logged_tail(named[0]) == (published or "none")


@pytest.mark.asyncio
async def test_a_pem_key_a_real_child_prints_is_kept_as_one_withheld_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pem, body = _generated_pem(
        lambda: ec.generate_private_key(ec.SECP256R1()),
        serialization.PrivateFormat.TraditionalOpenSSL,
        last=3,
    )
    await _key_dump_is_withheld(pem, body, tmp_path, monkeypatch, caplog)


@pytest.mark.asyncio
async def test_a_pem_key_longer_than_the_ring_leaves_no_body_line_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 26 body lines: kept one entry per line, the ring would drop the header and
    # keep body lines that, without it, do not read as part of a key.
    pem, body = _generated_pem(
        lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        serialization.PrivateFormat.PKCS8,
        last=10,
    )
    assert len(body) > backend_wire._STDERR_TAIL_LINES
    await _key_dump_is_withheld(pem, body, tmp_path, monkeypatch, caplog)


def test_the_kept_tail_stays_out_of_the_backend_repr() -> None:
    # The tail holds what the server wrote, unredacted until the warning judges it.
    backend = _make_backend()
    backend._stderr_tail = [f"aws key {_SECRET_HEAD}{_SECRET_TAIL}"]
    assert _SECRET_HEAD not in repr(backend)


def test_a_pem_block_is_kept_as_one_withheld_line_through_its_end() -> None:
    begin, end = "-----BEGIN TEST BLOCK-----", "-----END TEST BLOCK-----"
    withheld = backend_wire._STDERR_PEM_WITHHELD
    tail: list[str] = []
    in_block = False
    for text in (
        "before",
        f"key: {begin}",
        "Ym9keSBvbmU=",
        f"{end} {begin}",
        "Ym9keSB0d28=",
        end,
        "after",
        f"{begin} Ym9keQ== {end}",
        "between",
        begin,
        "never closed",
        "Traceback (most recent call last):",
    ):
        in_block = backend_wire._keep_stderr_line(tail, text, redact(text), in_block)
    assert tail == ["before", withheld, "after", withheld, "between", withheld]
    # A block that never closes withholds the rest of the stream.
    assert in_block is True


def test_a_secret_the_line_cut_would_split_is_kept_redacted() -> None:
    secret = _SECRET_HEAD + _SECRET_TAIL
    # The key starts 15 characters before the cut: the cut alone would keep its
    # first 15, too few for the redactor to recognise.
    text = "word " * ((backend_wire._STDERR_TAIL_LINE_MAX - 15) // 5) + secret + " rejected"
    assert redact(text) != text
    tail: list[str] = []
    _keep(tail, text)
    assert len(tail) == 1 and tail[0].startswith("word word")
    assert secret[:15] not in tail[0]
    assert secret[:15] not in published_stderr_tail(tail, " | ", backend_wire._STDERR_TAIL_LINES)


def test_the_kept_tail_is_bounded_and_one_printable_line_each() -> None:
    tail: list[str] = []
    for n in range(backend_wire._STDERR_TAIL_LINES + 5):
        _keep(tail, f"line {n}")
    _keep(tail, "   ")
    _keep(tail, "\x1b[2Jcleared\rforged " + "word " * 2_000)
    assert len(tail) == backend_wire._STDERR_TAIL_LINES
    assert tail[0] == "line 6"
    assert tail[-1].startswith("[2Jcleared forged word")
    assert len(tail[-1]) == backend_wire._STDERR_TAIL_LINE_MAX
    assert all(line.isprintable() for line in tail)
    published = published_stderr_tail(tail, " | ", backend_wire._STDERR_TAIL_LINES)
    assert published.endswith(" | " + tail[-1])

    # The warning logs every kept line, so the ring bounds it: lines of the cap each.
    for line in (f"{n} " + "word " * 200 for n in range(backend_wire._STDERR_TAIL_LINES)):
        _keep(tail, line)
    assert all(len(line) == backend_wire._STDERR_TAIL_LINE_MAX for line in tail)
    text = published_stderr_tail(tail, " | ", backend_wire._STDERR_TAIL_LINES)
    assert text == " | ".join(tail)
    assert len(text) == len(tail) * backend_wire._STDERR_TAIL_LINE_MAX + (len(tail) - 1) * 3
